"""`fcc faab bid` end to end.

Drives the real command through typer's runner with a stubbed Sleeper client,
using a transaction log shaped like a real season: some uncontested $1 claims,
some genuinely contested ones, and losing bids alongside the winners.
"""

import pytest
from typer.testing import CliRunner

from fcc.cli import app
from fcc.core.config import LeagueConfig
from fcc.platforms.base import LeagueSummary

PLAYERS = {
    "bowers": {"full_name": "Brock Bowers", "position": "TE", "team": "LV"},
    "goedert": {"full_name": "Dallas Goedert", "position": "TE", "team": "PHI"},
    "te_a": {"full_name": "Streamer TE", "position": "TE", "team": "CHI"},
    "te_b": {"full_name": "Other TE", "position": "TE", "team": "NYJ"},
    "te_c": {"full_name": "Third TE", "position": "TE", "team": "MIA"},
    "te_d": {"full_name": "Fourth TE", "position": "TE", "team": "DEN"},
    "rb_a": {"full_name": "Hot RB", "position": "RB", "team": "KC"},
    # Two players sharing a surname, to exercise the ambiguity refusal.
    "amb1": {"full_name": "Mike Williams", "position": "WR", "team": "NYJ"},
    "amb2": {"full_name": "Mike Williams", "position": "WR", "team": "LAC"},
}


def waiver(pid, bid, status="complete", week=1):
    return {
        "type": "waiver",
        "status": status,
        "leg": week,
        "adds": {pid: 1},
        "settings": {"waiver_bid": bid},
    }


TRANSACTIONS = [
    waiver("te_a", 4, week=1), waiver("te_a", 2, status="failed", week=1),
    waiver("te_b", 16, week=2), waiver("te_b", 14, status="failed", week=2),
    waiver("te_c", 9, week=3),
    waiver("te_d", 31, week=3), waiver("te_d", 12, status="failed", week=3),
    waiver("rb_a", 55, week=2), waiver("rb_a", 50, status="failed", week=2),
]


class StubSleeper:
    platform = "sleeper"

    def players(self):
        return PLAYERS

    def league(self):
        return {"name": "Test League", "season": "2026", "settings": {"waiver_budget": 100}}

    def current_week(self):
        return 3

    def transaction_history(self, through_week=None):
        return TRANSACTIONS

    def league_summary(self):
        return LeagueSummary(
            key="sleeper_main", platform="sleeper", faab_remaining=64, week=3
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(
        "fcc.core.config.Settings.league",
        lambda self, key: LeagueConfig(
            key=key, platform="sleeper", league_id="L1", season=2026
        ),
    )
    monkeypatch.setattr(
        "fcc.platforms.registry.connector_for", lambda lg, store=None: StubSleeper()
    )


def run(*args):
    return CliRunner().invoke(app, list(args))


# --- the bid command -------------------------------------------------------
def test_bid_reports_the_market_and_a_price_ladder(stubbed):
    r = run("faab", "bid", "sleeper_main", "--player", "Brock Bowers")
    assert r.exit_code == 0, r.output
    assert "Brock Bowers" in r.output
    assert "TE" in r.output
    # Budget context comes from the live league, not a guess.
    assert "$64 of $100" in r.output
    assert "Would have won" in r.output


def test_the_ladder_prices_come_from_this_league_not_a_generic_chart(stubbed):
    r = run("faab", "bid", "sleeper_main", "--player", "Brock Bowers")
    # TE winners are 4, 16, 9, 31 -> beating all of them costs $32.
    assert "$32" in r.output
    # The $55 RB claim must not leak into a TE segment.
    assert "$56" not in r.output


def test_losing_bids_are_surfaced_as_the_runner_up_signal(stubbed):
    r = run("faab", "market", "sleeper_main")
    assert "Median runner-up" in r.output
    assert "Winner's premium over field" in r.output
    assert "Most expensive contested claims" in r.output


def test_value_ceiling_appears_only_when_a_projection_is_given(stubbed):
    without = run("faab", "bid", "sleeper_main", "--player", "Brock Bowers")
    assert "Value ceiling" not in without.output
    assert "--vor" in without.output

    with_vor = run(
        "faab", "bid", "sleeper_main", "--player", "Brock Bowers",
        "--vor", "5.5", "--need", "replacing_injured_starter",
    )
    assert "Value ceiling" in with_vor.output
    assert "over replacement" in with_vor.output


def test_it_never_claims_to_have_submitted_anything(stubbed):
    r = run("faab", "bid", "sleeper_main", "--player", "Brock Bowers", "--vor", "5")
    assert "nothing here is submitted" in r.output.lower()


# --- refusals --------------------------------------------------------------
def test_an_ambiguous_player_name_is_refused_not_guessed(stubbed):
    r = run("faab", "bid", "sleeper_main", "--player", "Mike Williams")
    assert r.exit_code != 0
    assert "ambiguous" in r.output.lower()


def test_an_unknown_player_is_refused(stubbed):
    r = run("faab", "bid", "sleeper_main", "--player", "Nobody At All")
    assert r.exit_code != 0
    assert "No player matching" in r.output


def test_a_non_sleeper_league_says_so_plainly(monkeypatch):
    monkeypatch.setattr(
        "fcc.core.config.Settings.league",
        lambda self, key: LeagueConfig(
            key=key, platform="yahoo", league_id="449.l.1", season=2026
        ),
    )
    r = run("faab", "bid", "yahoo_main", "--player", "Brock Bowers")
    assert r.exit_code != 0
    assert "only wired" in r.output


def test_market_can_be_narrowed_to_a_position(stubbed):
    r = run("faab", "market", "sleeper_main", "--position", "TE")
    assert "TE claims" in r.output
