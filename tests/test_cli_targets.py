"""`fcc faab targets` end to end.

Stubbed against a roster shaped like the real problem: starters on bye, an
injured starter, and a chopped team's players sitting unrostered on the wire.
"""

import pytest
from typer.testing import CliRunner

from fcc.cli import app
from fcc.core.config import LeagueConfig
from fcc.engines.value import RankedPlayer
from fcc.platforms.base import LeagueSummary, Player, PlayerStatus, Roster, RosterSlot

WEEK = 7


def rp(pid, name, pos, pts):
    p = RankedPlayer(name=name, position=pos, projected_points=pts)
    p.player_id = pid
    return p


# A full projected universe so replacement level is locatable.
UNIVERSE = (
    [rp(f"rb{i}", f"RB Guy {i}", "RB", 21 - i * 0.45) for i in range(45)]
    + [rp(f"wr{i}", f"WR Guy {i}", "WR", 19 - i * 0.35) for i in range(45)]
    + [rp(f"te{i}", f"TE Guy {i}", "TE", 15 - i * 0.6) for i in range(22)]
)
# The chopped team's RB: strong, and nobody owns him.
CHOPPED_RB = rp("chopped_rb", "Chopped Star RB", "RB", 16.5)
UNIVERSE.append(CHOPPED_RB)

MY_PLAYERS = ["rb0", "wr0", "te0", "rb30"]
OTHERS = [f"rb{i}" for i in range(1, 20)] + [f"wr{i}" for i in range(1, 20)]


class StubSleeper:
    platform = "sleeper"

    def current_week(self):
        return WEEK

    def league(self):
        return {"season": "2026", "settings": {"waiver_budget": 1000}}

    def league_summary(self):
        return LeagueSummary(key="g", platform="sleeper", faab_remaining=600, week=WEEK)

    def roster(self, week=None):
        return Roster(
            league_key="g", team_id="1", week=WEEK,
            slots=[
                # Two RBs unusable: one on bye, one hurt. This is the hole.
                RosterSlot("RB", Player("rb0", "My RB1", position="RB",
                                        projected_points=18, bye_week=WEEK), starter=True),
                RosterSlot("RB", Player("rb30", "My RB2", position="RB",
                                        projected_points=7,
                                        status=PlayerStatus.OUT), starter=True),
                RosterSlot("WR", Player("wr0", "My WR", position="WR",
                                        projected_points=17), starter=True),
                RosterSlot("TE", Player("te0", "My TE", position="TE",
                                        projected_points=13), starter=True),
            ],
        )

    def ranked_players(self, week=None):
        return UNIVERSE

    def rostered_player_ids(self):
        return set(MY_PLAYERS) | set(OTHERS)

    def league_budgets(self):
        return [
            {"roster_id": 1, "is_me": True, "budget_remaining": 600, "likely_chopped": False},
            {"roster_id": 2, "is_me": False, "budget_remaining": 410, "likely_chopped": False},
            {"roster_id": 3, "is_me": False, "budget_remaining": 900, "likely_chopped": True},
        ]

    def transaction_history(self, through_week=None):
        out = []
        for pid, bids in {
            "a": [(40, "complete"), (33, "failed")],
            "b": [(12, "complete")],
            "c": [(85, "complete"), (60, "failed")],
            "d": [(25, "complete"), (20, "failed")],
        }.items():
            for bid, status in bids:
                out.append({"type": "waiver", "status": status, "leg": 3,
                            "adds": {f"rb{pid}": 1}, "settings": {"waiver_bid": bid}})
        return out

    def players(self):
        return {f"rb{k}": {"full_name": f"Past RB {k}", "position": "RB"} for k in "abcd"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(
        "fcc.core.config.Settings.league",
        lambda self, key: LeagueConfig(
            key=key, platform="sleeper", league_id="L1", season=2026,
            format="guillotine", name="The Chopping Block",
        ),
    )
    monkeypatch.setattr(
        "fcc.platforms.registry.connector_for", lambda lg, store=None: StubSleeper()
    )


def run(*args):
    return CliRunner().invoke(app, list(args))


def test_it_names_the_holes_in_the_lineup(stubbed):
    r = run("faab", "targets", "sleeper_main")
    assert r.exit_code == 0, r.output
    assert "Holes in your lineup" in r.output
    assert "bye" in r.output
    assert "out" in r.output


def test_it_surfaces_the_chopped_team_player(stubbed):
    """The whole point: a strong RB nobody owns should come top."""
    r = run("faab", "targets", "sleeper_main", "--position", "RB")
    assert "Chopped Star RB" in r.output


def test_it_distinguishes_a_one_week_rental_from_a_lasting_hole(stubbed):
    r = run("faab", "targets", "sleeper_main")
    assert "1wk" in r.output or "this week" in r.output


def test_it_shows_what_a_bid_typically_costs(stubbed):
    r = run("faab", "targets", "sleeper_main")
    assert "Typical bid" in r.output
    assert "$" in r.output


def test_it_explains_each_recommendation(stubbed):
    r = run("faab", "targets", "sleeper_main")
    assert "Why these" in r.output
    assert "over the next free" in r.output


def test_it_never_claims_to_have_submitted_anything(stubbed):
    r = run("faab", "targets", "sleeper_main")
    assert "nothing here is submitted" in r.output.lower()


def test_a_position_filter_narrows_the_list(stubbed):
    r = run("faab", "targets", "sleeper_main", "--position", "TE")
    assert "Chopped Star RB" not in r.output
