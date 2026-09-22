"""Guillotine-league bidding.

The headline number is `price_to_guarantee`: in a league where you are
eliminated for being worst, knowing that no surviving rival can match your
budget settles most bids outright. Everything else here is pacing.
"""

import pytest

from fcc.engines.guillotine import GuillotineState, from_budget_rows


def state(mine=500, rivals=(400, 300, 200), start=12, alive=None):
    return GuillotineState(
        my_remaining=mine,
        rival_remaining=list(rivals),
        teams_alive=alive,
        teams_at_start=start,
    )


# --- who can outbid whom ---------------------------------------------------
def test_holding_more_than_the_richest_rival_guarantees_a_win():
    s = state(mine=500, rivals=(400, 300))
    assert s.can_outbid_anyone
    assert s.price_to_guarantee == 401


def test_the_guarantee_is_about_the_richest_rival_not_their_total():
    """Rivals bid against you individually, not as a pool."""
    s = state(mine=500, rivals=(400, 400, 400))   # field total 1200
    assert s.can_outbid_anyone
    assert s.price_to_guarantee == 401


def test_no_guarantee_when_a_rival_can_match_you():
    s = state(mine=300, rivals=(900, 100))
    assert not s.can_outbid_anyone
    assert s.price_to_guarantee is None
    assert "cannot buy a guaranteed win" in " ".join(s.advice())


def test_a_lone_survivor_faces_no_competition():
    s = GuillotineState(my_remaining=50, rival_remaining=[])
    assert s.max_rival_budget == 0
    assert s.price_to_guarantee == 1


def test_my_share_of_live_faab():
    s = state(mine=250, rivals=(250, 250, 250))
    assert s.my_share == pytest.approx(0.25)


def test_share_is_none_when_nobody_has_money():
    assert GuillotineState(my_remaining=0, rival_remaining=[0]).my_share is None


# --- pacing ----------------------------------------------------------------
def test_pacing_loosens_as_the_field_thins():
    early = GuillotineState(my_remaining=1000, rival_remaining=[500] * 11, teams_at_start=12)
    late = GuillotineState(my_remaining=1000, rival_remaining=[500] * 2, teams_at_start=12)
    assert early.pacing_fraction() < late.pacing_fraction()
    assert early.spend_cap() < late.spend_cap()


def test_early_pacing_is_restrained():
    """Blowing a big share of budget in week 1 is the classic way to lose."""
    early = GuillotineState(my_remaining=1000, rival_remaining=[1000] * 11, teams_at_start=12)
    assert early.pacing_fraction() <= 0.15


def test_progress_is_bounded_even_with_odd_inputs():
    assert GuillotineState(my_remaining=10, rival_remaining=[], teams_at_start=1).progress == 0.0
    weird = GuillotineState(my_remaining=10, rival_remaining=[], teams_alive=99, teams_at_start=2)
    assert 0.0 <= weird.progress <= 1.0


def test_spend_cap_is_always_at_least_a_dollar():
    assert GuillotineState(my_remaining=1, rival_remaining=[50]).spend_cap() >= 1


def test_advice_always_mentions_the_pacing_cap():
    assert any("$" in line and "single buy" in line for line in state().advice())


# --- building from Sleeper rows -------------------------------------------
ROWS = [
    {"roster_id": 1, "is_me": True, "budget_remaining": 640, "likely_chopped": False,
     "team_name": "Me"},
    {"roster_id": 2, "is_me": False, "budget_remaining": 910, "likely_chopped": False,
     "team_name": "Rich Rival"},
    {"roster_id": 3, "is_me": False, "budget_remaining": 300, "likely_chopped": False,
     "team_name": "Alive"},
    # Chopped: their money can never be bid against you again.
    {"roster_id": 4, "is_me": False, "budget_remaining": 990, "likely_chopped": True,
     "team_name": "Chopped"},
]


def test_chopped_teams_are_excluded_from_the_live_field():
    s = from_budget_rows(ROWS)
    assert s.max_rival_budget == 910          # not the chopped team's 990
    assert s.teams_alive == 3
    assert 990 not in s.rival_remaining


def test_a_chopped_rich_team_does_not_deny_you_a_guarantee():
    rows = [
        {"roster_id": 1, "is_me": True, "budget_remaining": 500, "likely_chopped": False},
        {"roster_id": 2, "is_me": False, "budget_remaining": 9999, "likely_chopped": True},
        {"roster_id": 3, "is_me": False, "budget_remaining": 100, "likely_chopped": False},
    ]
    s = from_budget_rows(rows)
    assert s.can_outbid_anyone
    assert s.price_to_guarantee == 101


def test_missing_own_roster_fails_loudly():
    rows = [{"roster_id": 2, "is_me": False, "budget_remaining": 100, "likely_chopped": False}]
    with pytest.raises(ValueError, match="team_id"):
        from_budget_rows(rows)


def test_rosters_with_unknown_budgets_are_skipped_not_counted_as_zero():
    rows = [
        {"roster_id": 1, "is_me": True, "budget_remaining": 500, "likely_chopped": False},
        {"roster_id": 2, "is_me": False, "budget_remaining": None, "likely_chopped": False},
        {"roster_id": 3, "is_me": False, "budget_remaining": 200, "likely_chopped": False},
    ]
    s = from_budget_rows(rows)
    assert s.rival_remaining == [200]
