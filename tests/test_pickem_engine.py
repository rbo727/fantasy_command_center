"""The FantasyGuru -> ESPN join.

These tests encode the failure modes that would be invisible in production: a
pick submitted for the wrong side, a week that silently covers half the slate.
"""

from datetime import UTC, datetime, timedelta

import pytest

from fcc.engines.pickem import (
    COINFLIP_EDGE,
    JoinResult,
    StaffPick,
    assign_confidence_points,
    join,
    straight_up_side,
    win_probability,
)
from fcc.platforms.espn_pickem import Option, Pick, Proposition


def prop(pid: str, away: str, home: str, lock: datetime | None = None) -> Proposition:
    return Proposition(
        id=pid,
        week=1,
        lock_time=lock,
        options=[
            Option(id=f"{pid}-{away}", team_code=away, label=away),
            Option(id=f"{pid}-{home}", team_code=home, label=home),
        ],
    )


SLATE = [prop("p1", "BAL", "KC"), prop("p2", "SF", "SEA"), prop("p3", "NYG", "DAL")]


# --- win probability -------------------------------------------------------
def test_win_probability_matches_known_nfl_rates():
    assert win_probability(0) == pytest.approx(0.5)
    assert win_probability(-3) == pytest.approx(0.59, abs=0.02)
    assert win_probability(-7) == pytest.approx(0.70, abs=0.02)
    # Symmetry: a team's chance plus its opponent's must be 1.
    assert win_probability(-6) + win_probability(6) == pytest.approx(1.0)


# --- market translation ----------------------------------------------------
def test_ats_pick_on_favorite_keeps_the_side():
    code, prob, _ = straight_up_side(
        StaffPick(matchup="BAL@KC", team="Chiefs", market="ATS", spread=-6.5, conviction=4)
    )
    assert code == "KC"
    assert prob > 0.6


def test_ats_pick_on_underdog_does_not_imply_a_winner():
    """The trap: liking a +7 dog against the spread is not picking them to win."""
    code, prob, why = straight_up_side(
        StaffPick(matchup="BAL@KC", team="Ravens", market="ATS", spread=7.0, conviction=5)
    )
    assert code is None
    assert "other side" in why
    # Probability returned is the *opponent's* chance to win outright.
    assert prob > 0.6


def test_moneyline_pick_is_taken_directly():
    code, _, why = straight_up_side(
        StaffPick(matchup="BAL@KC", team="Ravens", market="ML", spread=3.0)
    )
    assert code == "BAL"
    assert "taken directly" in why


def test_over_under_is_never_a_winner_pick():
    code, _, why = straight_up_side(
        StaffPick(matchup="BAL@KC", team="Chiefs", market="OU", spread=-3)
    )
    assert code is None
    assert "total" in why.lower()


def test_ats_without_spread_is_refused():
    code, _, why = straight_up_side(StaffPick(matchup="BAL@KC", team="Chiefs", market="ATS"))
    assert code is None
    assert "no spread" in why


def test_unresolvable_team_is_refused():
    code, _, why = straight_up_side(StaffPick(matchup="?", team="New York", market="ML"))
    assert code is None
    assert "resolve" in why


# --- joining ---------------------------------------------------------------
def test_join_produces_submittable_picks():
    result = join(
        [
            StaffPick("BAL@KC", "Chiefs", "ATS", -6.5, 4),
            StaffPick("SF@SEA", "49ers", "ML", -3.0, 3),
        ],
        SLATE,
    )
    assert result.clean, result.review
    assert {p.team_code for p in result.picks} == {"KC", "SF"}
    assert all(isinstance(p, Pick) and p.option_id for p in result.picks)


def test_join_flips_to_the_opponent_for_an_underdog_ats_lean():
    result = join([StaffPick("BAL@KC", "Ravens", "ATS", 7.0, 5)], SLATE)
    assert result.clean, result.review
    assert [p.team_code for p in result.picks] == ["KC"]


def test_unmatched_pick_goes_to_review_not_silently_dropped():
    result = join([StaffPick("MIA@BUF", "Dolphins", "ML", -2.0)], SLATE)
    assert result.picks == []
    assert not result.clean
    assert result.review[0].reason == "no_matching_game"


def test_duplicate_picks_on_one_game_are_flagged():
    result = join(
        [StaffPick("BAL@KC", "Chiefs", "ML", -6.5), StaffPick("BAL@KC", "Ravens", "ML", 6.5)],
        SLATE,
    )
    assert len(result.picks) == 1
    assert any(r.reason == "duplicate_pick" for r in result.review)


def test_locked_game_is_not_picked():
    past = datetime.now(UTC) - timedelta(hours=1)
    result = join([StaffPick("BAL@KC", "Chiefs", "ML", -6.5)], [prop("p1", "BAL", "KC", lock=past)])
    assert result.picks == []
    assert result.review[0].reason == "locked"


def test_proposition_with_a_half_mapped_option_is_refused():
    """If ESPN hands us a label we can't map, we refuse the whole game.

    This is the realistic version of the identity trap: one side resolves, the
    other is an abbreviation we've never seen. Picking the side we recognise
    would look fine right up until it was the wrong game.
    """
    half_mapped = Proposition(
        id="p9",
        week=1,
        options=[
            Option(id="a", team_code="KC", label="KC"),
            Option(id="b", team_code=None, label="ZZZ"),
        ],
    )
    result = join([StaffPick("ZZZ@KC", "Chiefs", "ML", -3)], [half_mapped])
    assert result.picks == []
    assert result.review[0].reason == "unresolved_proposition"


def test_team_appearing_in_two_propositions_is_refused():
    """Two games containing the same team means our week filter is wrong."""
    result = join([StaffPick("BAL@KC", "Chiefs", "ML", -3)], [*SLATE, prop("p4", "KC", "DEN")])
    assert result.picks == []
    assert result.review[0].reason == "no_matching_game"


def test_coinflip_games_still_get_picked():
    """You must pick someone; a near-50/50 game is flagged, not skipped."""
    result = join([StaffPick("SF@SEA", "49ers", "ML", -0.5, 1)], SLATE)
    assert len(result.picks) == 1
    prob = result.confidence_by_prop["p2"]
    assert abs(prob - 0.5) < COINFLIP_EDGE


# --- confidence points -----------------------------------------------------
def test_confidence_points_rank_strongest_edge_highest():
    result = JoinResult(
        picks=[
            Pick("p1", "o1", "KC"),
            Pick("p2", "o2", "SF"),
            Pick("p3", "o3", "DAL"),
        ],
        confidence_by_prop={"p1": 0.55, "p2": 0.85, "p3": 0.70},
    )
    assign_confidence_points(result)
    by_prop = {p.proposition_id: p.confidence for p in result.picks}
    assert by_prop == {"p2": 3, "p3": 2, "p1": 1}


def test_join_assigns_confidence_when_requested():
    result = join(
        [StaffPick("BAL@KC", "Chiefs", "ML", -10.0), StaffPick("SF@SEA", "49ers", "ML", -1.0)],
        SLATE,
        use_confidence_points=True,
    )
    confidences = sorted(p.confidence for p in result.picks)
    assert confidences == [1, 2]
