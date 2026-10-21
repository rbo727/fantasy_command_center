"""Waiver target discovery.

The distinction worth protecting is how long a hole lasts. A bye is a one-week
rental; an injured starter keeps paying every week he's out. Paying a
multi-week price for a one-week patch is the classic way a FAAB budget bleeds
out, so that case is flagged rather than merely priced.
"""

from fcc.engines.targets import (
    Need,
    Target,
    find_targets,
    my_best_at,
    rental_warning,
    roster_needs,
)
from fcc.engines.value import RankedPlayer
from fcc.platforms.base import Player, PlayerStatus, Roster, RosterSlot


def rp(name, pos, pts):
    return RankedPlayer(name=name, position=pos, projected_points=pts)


def starter(slot, player):
    return RosterSlot(slot=slot, player=player, starter=True)


def bench(player):
    return RosterSlot(slot="BN", player=player, starter=False)


def pl(name, pos, pts=None, status=PlayerStatus.ACTIVE, bye=None):
    return Player(
        player_id=name, name=name, position=pos, projected_points=pts,
        status=status, bye_week=bye,
    )


# --- detecting holes -------------------------------------------------------
def test_a_bye_is_a_one_week_hole():
    roster = Roster(
        league_key="k", team_id="1", week=7,
        slots=[starter("RB", pl("Bye RB", "RB", 12, bye=7))],
    )
    needs = roster_needs(roster)
    assert len(needs) == 1
    assert needs[0].cause == "bye"
    assert needs[0].is_rental


def test_an_injured_starter_is_a_multi_week_hole():
    roster = Roster(
        league_key="k", team_id="1", week=7,
        slots=[starter("TE", pl("Hurt TE", "TE", 11, status=PlayerStatus.INJURED_RESERVE))],
    )
    need = roster_needs(roster)[0]
    assert need.cause == "ir"
    assert not need.is_rental
    assert need.weeks > 1


def test_a_healthy_starter_is_not_a_hole():
    roster = Roster(
        league_key="k", team_id="1", week=7,
        slots=[starter("RB", pl("Fine RB", "RB", 14))],
    )
    assert roster_needs(roster) == []


def test_an_empty_slot_is_a_hole():
    roster = Roster(
        league_key="k", team_id="1", week=7,
        slots=[RosterSlot(slot="WR", player=None, starter=True)],
    )
    assert roster_needs(roster)[0].cause == "empty"


def test_bench_players_are_not_holes():
    roster = Roster(
        league_key="k", team_id="1", week=7,
        slots=[bench(pl("Bye bench", "RB", 9, bye=7))],
    )
    assert roster_needs(roster) == []


# --- what I already have ---------------------------------------------------
def test_my_best_at_ignores_players_who_cannot_play():
    roster = Roster(
        league_key="k", team_id="1", week=7,
        slots=[
            starter("RB", pl("Bye RB", "RB", 20, bye=7)),
            bench(pl("Hurt RB", "RB", 18, status=PlayerStatus.OUT)),
            bench(pl("Usable RB", "RB", 7)),
        ],
    )
    assert my_best_at(roster, "RB") == 7


def test_my_best_is_none_when_nobody_is_usable():
    roster = Roster(
        league_key="k", team_id="1", week=7,
        slots=[starter("RB", pl("Bye RB", "RB", 20, bye=7))],
    )
    assert my_best_at(roster, "RB") is None


# --- ranking the wire ------------------------------------------------------
# A deep universe so replacement level is actually visible.
UNIVERSE = (
    [rp(f"RB{i}", "RB", 22 - i * 0.5) for i in range(40)]
    + [rp(f"WR{i}", "WR", 20 - i * 0.4) for i in range(40)]
    + [rp(f"TE{i}", "TE", 16 - i * 0.7) for i in range(20)]
)

ROSTER_WITH_RB_BYE = Roster(
    league_key="k", team_id="1", week=7,
    slots=[
        starter("RB", pl("Bye RB", "RB", 15, bye=7)),
        starter("WR", pl("Fine WR", "WR", 14)),
        bench(pl("Scrub RB", "RB", 4)),
    ],
)


def test_targets_prefer_the_position_with_a_hole():
    available = [rp("Free RB", "RB", 14.0), rp("Free WR", "WR", 14.0)]
    targets, needs = find_targets(available, UNIVERSE, ROSTER_WITH_RB_BYE, teams=12)
    assert [n.position for n in needs] == ["RB"]
    assert targets[0].player.name == "Free RB"


def test_a_multi_week_hole_outranks_a_one_week_one_at_equal_value():
    roster = Roster(
        league_key="k", team_id="1", week=7,
        slots=[
            starter("RB", pl("Bye RB", "RB", 15, bye=7)),
            starter("TE", pl("IR TE", "TE", 11, status=PlayerStatus.INJURED_RESERVE)),
        ],
    )
    rb = Target(player=rp("RB cand", "RB", 12), vor=5.0,
                fills=Need("RB", "RB", "bye", weeks=1))
    te = Target(player=rp("TE cand", "TE", 12), vor=5.0,
                fills=Need("TE", "TE", "ir", weeks=6))
    assert te.score > rb.score
    assert roster_needs(roster)[1].weeks > roster_needs(roster)[0].weeks


def test_a_player_no_better_than_another_free_agent_is_not_offered():
    """If a comparable body is also free, this one is not worth money."""
    available = [rp("Scrub", "RB", 6.0), rp("Equally Free", "RB", 6.5)]
    targets, _ = find_targets(available, UNIVERSE, ROSTER_WITH_RB_BYE, teams=12)
    assert "Scrub" not in [t.player.name for t in targets]


def test_the_only_option_at_an_empty_position_is_still_worth_something():
    """With nobody else free, the alternative is starting nobody."""
    targets, _ = find_targets([rp("Only One", "RB", 6.0)], UNIVERSE, ROSTER_WITH_RB_BYE,
                              teams=12)
    assert [t.player.name for t in targets] == ["Only One"]


def test_players_without_a_projection_are_skipped():
    targets, _ = find_targets(
        [RankedPlayer(name="Mystery", position="RB")], UNIVERSE, ROSTER_WITH_RB_BYE, teams=12
    )
    assert targets == []


def test_position_filter_narrows_the_search():
    available = [rp("Free RB", "RB", 14.0), rp("Free WR", "WR", 18.0)]
    targets, _ = find_targets(
        available, UNIVERSE, ROSTER_WITH_RB_BYE, teams=12, positions=["RB"]
    )
    assert [t.player.pos for t in targets] == ["RB"]


def test_value_is_the_measured_gap_to_the_next_free_agent():
    """Measure the real alternative rather than estimating it from rank.

    An earlier version ranked free agents against a rank-based guess at who was
    rostered. That was a proxy for a fact we already hold - the rostered set is
    known - and it drifts worst in a shrinking guillotine field, exactly where
    the money is. Now the baseline is simply the best other free agent.
    """
    available = [rp("Good RB", "RB", 14.0), rp("Next Best", "RB", 9.0)]
    targets, _ = find_targets(available, UNIVERSE, ROSTER_WITH_RB_BYE, teams=12)
    top = next(t for t in targets if t.player.name == "Good RB")
    assert top.vor == 5.0           # 14.0 - 9.0, not a rank-derived estimate


def test_two_comparable_free_agents_are_each_worth_little():
    """You will end up with one of them regardless, so neither commands money."""
    available = [rp("RB A", "RB", 14.0), rp("RB B", "RB", 13.8)]
    targets, _ = find_targets(available, UNIVERSE, ROSTER_WITH_RB_BYE, teams=12)
    assert all(t.vor is not None and t.vor < 1.0 for t in targets)


def test_the_longest_need_at_a_position_is_the_one_that_counts():
    roster = Roster(
        league_key="k", team_id="1", week=7,
        slots=[
            starter("RB", pl("Bye RB", "RB", 15, bye=7)),
            starter("FLEX", pl("IR RB", "RB", 13, status=PlayerStatus.INJURED_RESERVE)),
        ],
    )
    targets, _ = find_targets([rp("Free RB", "RB", 14.0)], UNIVERSE, roster, teams=12)
    assert not targets[0].fills.is_rental       # the IR hole, not the bye


def test_reasons_explain_the_recommendation():
    targets, _ = find_targets([rp("Free RB", "RB", 14.0)], UNIVERSE, ROSTER_WITH_RB_BYE, teams=12)
    joined = " ".join(targets[0].reasons)
    assert "over the next free" in joined
    assert "bye" in joined
    assert "this week only" in joined


# --- the rental guard ------------------------------------------------------
def test_an_expensive_bye_patch_is_flagged():
    target = Target(player=rp("Rental", "RB", 11), vor=3.0,
                    fills=Need("RB", "RB", "bye", weeks=1))
    warning = rental_warning(target, bid=120, budget_remaining=600)
    assert warning is not None
    assert "one-week" in warning


def test_a_cheap_bye_patch_is_not_flagged():
    target = Target(player=rp("Rental", "RB", 11), vor=3.0,
                    fills=Need("RB", "RB", "bye", weeks=1))
    assert rental_warning(target, bid=10, budget_remaining=600) is None


def test_a_multi_week_need_is_never_called_a_rental():
    target = Target(player=rp("Starter", "RB", 15), vor=6.0,
                    fills=Need("RB", "RB", "ir", weeks=6))
    assert rental_warning(target, bid=300, budget_remaining=600) is None
