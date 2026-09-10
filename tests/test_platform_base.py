"""Normalized platform vocabulary.

`normalize_status` is the single place three platforms' injury spellings meet.
Stage 3 benches players based on its answer, so its failure mode matters more
than its coverage: an unrecognised status must never read as healthy.
"""

import pytest

from fcc.platforms.base import (
    LeagueSummary,
    Player,
    PlayerStatus,
    Roster,
    RosterSlot,
    normalize_status,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Sleeper
        ("Out", PlayerStatus.OUT),
        ("IR", PlayerStatus.INJURED_RESERVE),
        ("Sus", PlayerStatus.SUSPENDED),
        ("PUP", PlayerStatus.PUP),
        ("Questionable", PlayerStatus.QUESTIONABLE),
        ("NA", PlayerStatus.INACTIVE),
        # Yahoo's single letters
        ("O", PlayerStatus.OUT),
        ("Q", PlayerStatus.QUESTIONABLE),
        ("D", PlayerStatus.DOUBTFUL),
        # ESPN's shouted underscores
        ("INJURY_RESERVE", PlayerStatus.INJURED_RESERVE),
        ("Injury Reserve", PlayerStatus.INJURED_RESERVE),
        ("SUSPENSION", PlayerStatus.SUSPENDED),
        # healthy
        ("Active", PlayerStatus.ACTIVE),
        ("", PlayerStatus.ACTIVE),
        (None, PlayerStatus.ACTIVE),
    ],
)
def test_normalize_status(raw, expected):
    assert normalize_status(raw) is expected


def test_unknown_status_is_never_treated_as_healthy():
    """The asymmetry that protects the lineup: unknown != active."""
    status = normalize_status("day-to-day-ish")
    assert status is PlayerStatus.UNKNOWN
    assert not status.is_unavailable   # we don't claim to know they're out
    assert status.is_doubtful          # ...but we do flag it for a human


@pytest.mark.parametrize(
    "status",
    [
        PlayerStatus.OUT,
        PlayerStatus.INJURED_RESERVE,
        PlayerStatus.SUSPENDED,
        PlayerStatus.PUP,
        PlayerStatus.INACTIVE,
        PlayerStatus.BYE,
        PlayerStatus.NOT_ON_ROSTER,
    ],
)
def test_definitely_unavailable_statuses(status):
    assert status.is_unavailable


@pytest.mark.parametrize("status", [PlayerStatus.ACTIVE, PlayerStatus.QUESTIONABLE])
def test_playable_statuses_are_not_auto_benched(status):
    assert not status.is_unavailable


def test_player_normalizes_its_team_code():
    """A connector must not be able to leak a platform-specific abbreviation."""
    assert Player(player_id="1", name="X", team="WSH").team == "WAS"
    assert Player(player_id="2", name="Y", team="JAC").team == "JAX"


def test_roster_partitions_starters_bench_and_problems():
    roster = Roster(
        league_key="sleeper_main",
        team_id="1",
        slots=[
            RosterSlot("QB", Player("1", "Healthy QB"), starter=True),
            RosterSlot(
                "RB",
                Player("2", "Hurt RB", status=PlayerStatus.OUT),
                starter=True,
            ),
            RosterSlot(
                "WR",
                Player("3", "Maybe WR", status=PlayerStatus.QUESTIONABLE),
                starter=True,
            ),
            RosterSlot("BN", Player("4", "Bench RB")),
            RosterSlot("IR", Player("5", "IR guy", status=PlayerStatus.INJURED_RESERVE)),
        ],
    )
    assert len(roster.starters) == 3
    assert [s.player.name for s in roster.bench] == ["Bench RB"]   # IR is not bench
    assert [s.player.name for s in roster.problem_starters()] == ["Hurt RB"]
    assert [s.player.name for s in roster.questionable_starters()] == ["Maybe WR"]


def test_record_formatting():
    assert LeagueSummary(key="k", platform="p", wins=7, losses=3).record == "7-3"
    assert LeagueSummary(key="k", platform="p", wins=7, losses=3, ties=1).record == "7-3-1"
    assert LeagueSummary(key="k", platform="p").record == ""
