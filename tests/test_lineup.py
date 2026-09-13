"""The lineup guardian.

Most of these test what it *refuses* to do. An unnecessary warning costs ten
seconds; an automatic swap on a wrong assumption costs a week, so the refusals
are the safety property worth pinning.
"""

import pytest

from fcc.engines.lineup import (
    apply_plan,
    candidates_for,
    eligible_positions,
    is_unavailable,
    plan_lineup,
    unavailable_starters,
    verify_plan_applied,
)
from fcc.platforms.base import Player, PlayerStatus, Roster, RosterSlot


def p(pid, name, pos, status=PlayerStatus.ACTIVE, proj=None, positions=None, bye=None):
    return Player(
        player_id=pid,
        name=name,
        position=pos,
        status=status,
        projected_points=proj,
        positions=positions or [pos],
        bye_week=bye,
    )


def roster(*slots, week=5):
    return Roster(league_key="sleeper_main", team_id="1", week=week, slots=list(slots))


def starter(slot, player):
    return RosterSlot(slot=slot, player=player, starter=True)


def bench(player, slot="BN"):
    return RosterSlot(slot=slot, player=player, starter=False)


# --- slot eligibility ------------------------------------------------------
def test_flex_is_rb_wr_te():
    assert eligible_positions("FLEX") == {"RB", "WR", "TE"}
    assert eligible_positions("W/R/T") == {"RB", "WR", "TE"}


def test_superflex_adds_qb():
    assert "QB" in eligible_positions("SUPER_FLEX")
    assert "QB" not in eligible_positions("FLEX")


def test_unknown_slot_returns_none_not_an_empty_set():
    """None means 'we don't know the rules', which must not read as 'nothing fits'."""
    assert eligible_positions("WHATEVER_FLEX") is None


# --- unavailability --------------------------------------------------------
@pytest.mark.parametrize(
    "status",
    [PlayerStatus.OUT, PlayerStatus.INJURED_RESERVE, PlayerStatus.SUSPENDED, PlayerStatus.PUP],
)
def test_definitely_out_players_are_unavailable(status):
    assert is_unavailable(p("1", "X", "RB", status))


def test_bye_week_counts_even_for_a_healthy_player():
    healthy_but_bye = p("1", "X", "RB", bye=5)
    assert is_unavailable(healthy_but_bye, week=5)
    assert not is_unavailable(healthy_but_bye, week=6)


@pytest.mark.parametrize("status", [PlayerStatus.ACTIVE, PlayerStatus.QUESTIONABLE])
def test_playable_players_are_not_unavailable(status):
    assert not is_unavailable(p("1", "X", "RB", status))


# --- the plan --------------------------------------------------------------
def test_out_starter_is_swapped_for_the_best_eligible_bench_player():
    plan = plan_lineup(
        roster(
            starter("RB", p("out", "Hurt RB", "RB", PlayerStatus.OUT)),
            bench(p("b1", "Bench RB", "RB", proj=9.0)),
            bench(p("b2", "Better RB", "RB", proj=14.5)),
        )
    )
    assert len(plan.swaps) == 1
    swap = plan.swaps[0]
    assert swap.out_player.name == "Hurt RB"
    assert swap.in_player.name == "Better RB"   # higher projection wins
    assert "out" in swap.reason


def test_questionable_starter_is_warned_about_never_swapped():
    """Benching a Questionable starter who then plays is its own loss."""
    plan = plan_lineup(
        roster(
            starter("WR", p("q", "Maybe WR", "WR", PlayerStatus.QUESTIONABLE)),
            bench(p("b", "Healthy WR", "WR", proj=20.0)),
        )
    )
    assert plan.swaps == []
    assert [w.kind for w in plan.warnings] == ["questionable"]


def test_unknown_status_starter_is_warned_not_benched():
    plan = plan_lineup(
        roster(
            starter("WR", p("u", "Mystery WR", "WR", PlayerStatus.UNKNOWN)),
            bench(p("b", "Healthy WR", "WR")),
        )
    )
    assert plan.swaps == []
    assert plan.warnings[0].kind == "questionable"


def test_flex_accepts_rb_wr_and_te_but_not_qb():
    for pos in ("RB", "WR", "TE"):
        found = candidates_for("FLEX", [bench(p("b", f"Bench {pos}", pos))])
        assert [c.position for c in found] == [pos]
    assert candidates_for("FLEX", [bench(p("b", "Bench QB", "QB"))]) == []


def test_dual_eligible_player_can_fill_a_slot_his_primary_position_cannot():
    """A WR/RB must be usable at RB even though his primary label is WR."""
    hybrid = p("h", "Hybrid", "WR", positions=["WR", "RB"])
    assert candidates_for("RB", [bench(hybrid)])[0].name == "Hybrid"


def test_no_eligible_replacement_produces_a_warning_not_a_bad_swap():
    plan = plan_lineup(
        roster(
            starter("QB", p("out", "Hurt QB", "QB", PlayerStatus.OUT)),
            bench(p("b", "Bench RB", "RB")),        # wrong position
        )
    )
    assert plan.swaps == []
    assert plan.warnings[0].kind == "no_replacement"
    assert "Hurt QB" in plan.warnings[0].detail


def test_an_unavailable_bench_player_is_never_swapped_in():
    plan = plan_lineup(
        roster(
            starter("RB", p("out", "Hurt RB", "RB", PlayerStatus.OUT)),
            bench(p("b1", "Also Hurt", "RB", PlayerStatus.INJURED_RESERVE, proj=30.0)),
            bench(p("b2", "Fine RB", "RB", proj=5.0)),
        )
    )
    assert plan.swaps[0].in_player.name == "Fine RB"


def test_a_bench_player_on_bye_is_not_swapped_in():
    plan = plan_lineup(
        roster(
            starter("RB", p("out", "Hurt RB", "RB", PlayerStatus.OUT)),
            bench(p("b1", "Bye RB", "RB", proj=30.0, bye=5)),
            bench(p("b2", "Fine RB", "RB", proj=5.0)),
            week=5,
        )
    )
    assert plan.swaps[0].in_player.name == "Fine RB"


def test_one_bench_player_is_not_used_for_two_holes():
    """Two outs, one replacement: the second slot warns rather than double-booking."""
    plan = plan_lineup(
        roster(
            starter("RB", p("o1", "Out RB1", "RB", PlayerStatus.OUT)),
            starter("WR", p("o2", "Out WR", "WR", PlayerStatus.OUT)),
            bench(p("b", "Only Flex", "RB", positions=["RB", "WR"], proj=12.0)),
        )
    )
    assert len(plan.swaps) == 1
    assert plan.swaps[0].in_player.name == "Only Flex"
    assert any(w.kind == "no_replacement" for w in plan.warnings)


def test_unknown_slot_type_is_never_auto_filled():
    """Guessing a slot's rules produces illegal lineups that look correct."""
    plan = plan_lineup(
        roster(
            starter("MYSTERY_SLOT", p("out", "Hurt Guy", "RB", PlayerStatus.OUT)),
            bench(p("b", "Healthy RB", "RB")),
        )
    )
    assert plan.swaps == []
    assert plan.warnings[0].kind == "unknown_slot"
    assert "will not guess" in plan.warnings[0].detail


def test_empty_starting_slot_is_reported():
    plan = plan_lineup(roster(RosterSlot(slot="TE", player=None, starter=True)))
    assert plan.warnings[0].kind == "empty_slot"


def test_ir_players_are_not_treated_as_bench_candidates():
    plan = plan_lineup(
        roster(
            starter("RB", p("out", "Hurt RB", "RB", PlayerStatus.OUT)),
            RosterSlot(slot="IR", player=p("ir", "IR RB", "RB", proj=99.0), starter=False),
        )
    )
    assert plan.swaps == []
    assert plan.warnings[0].kind == "no_replacement"


def test_a_healthy_lineup_produces_nothing():
    plan = plan_lineup(
        roster(
            starter("QB", p("1", "Good QB", "QB")),
            starter("RB", p("2", "Good RB", "RB")),
            bench(p("3", "Bench", "WR")),
        )
    )
    assert plan.clean
    assert plan.summary() == "0 swap(s), 0 warning(s)"


def test_plan_is_deterministic_when_projections_are_missing():
    """Same input, same swap - a guardian that varies can't be trusted or tested."""
    def build():
        return roster(
            starter("RB", p("out", "Hurt", "RB", PlayerStatus.OUT)),
            bench(p("b1", "Zeta RB", "RB")),
            bench(p("b2", "Alpha RB", "RB")),
        )

    first = plan_lineup(build()).swaps[0].in_player.name
    for _ in range(5):
        assert plan_lineup(build()).swaps[0].in_player.name == first
    assert first == "Alpha RB"   # alphabetical when nothing separates them


# --- applying and verifying ------------------------------------------------
def test_apply_plan_swaps_both_directions_without_mutating_the_original():
    original = roster(
        starter("RB", p("out", "Hurt RB", "RB", PlayerStatus.OUT)),
        bench(p("b", "Fine RB", "RB")),
    )
    plan = plan_lineup(original)
    after = apply_plan(original, plan)

    assert [s.player.name for s in after.starters] == ["Fine RB"]
    assert [s.player.name for s in after.bench] == ["Hurt RB"]
    # The input is untouched.
    assert [s.player.name for s in original.starters] == ["Hurt RB"]


def test_verification_passes_when_the_platform_agrees():
    original = roster(
        starter("RB", p("out", "Hurt RB", "RB", PlayerStatus.OUT)),
        bench(p("b", "Fine RB", "RB")),
    )
    plan = plan_lineup(original)
    ok, problems = verify_plan_applied(apply_plan(original, plan), plan)
    assert ok and problems == []


def test_verification_fails_when_the_platform_silently_dropped_the_change():
    """The exact failure the action gate exists to catch."""
    original = roster(
        starter("RB", p("out", "Hurt RB", "RB", PlayerStatus.OUT)),
        bench(p("b", "Fine RB", "RB")),
    )
    plan = plan_lineup(original)
    ok, problems = verify_plan_applied(original, plan)   # nothing changed
    assert not ok
    assert any("still starting" in x for x in problems)
    assert any("not starting" in x for x in problems)


def test_verification_catches_any_unavailable_starter_even_one_we_never_planned_for():
    after = roster(
        starter("RB", p("x", "Surprise IR", "RB", PlayerStatus.INJURED_RESERVE)),
    )
    from fcc.engines.lineup import LineupPlan

    ok, problems = verify_plan_applied(after, LineupPlan())
    assert not ok
    assert "Surprise IR" in problems[0]


def test_unavailable_starters_lists_the_dashboard_red_rows():
    r = roster(
        starter("RB", p("1", "Out RB", "RB", PlayerStatus.OUT)),
        starter("WR", p("2", "Fine WR", "WR")),
        starter("TE", p("3", "Bye TE", "TE", bye=5)),
        week=5,
    )
    assert {s.player.name for s in unavailable_starters(r)} == {"Out RB", "Bye TE"}
