"""The /targets endpoint — the dashboard's answer to "who should I add".

Distinct from /waiver-report, which is FantasyGuru's board filtered to
availability. This one starts from the roster's own holes.
"""

import pytest
from fastapi.testclient import TestClient

from fcc.api.app import app
from fcc.core.config import LeagueConfig
from fcc.core.db import init_db
from tests.test_cli_targets import StubSleeper


@pytest.fixture
def client(monkeypatch):
    init_db()
    def _league(self, key):
        # Behave like the real lookup: unknown keys raise, so the 404 path is
        # actually exercised rather than resolved by an over-eager stub.
        if key != "sleeper_main":
            raise KeyError(key)
        return LeagueConfig(
            key=key, platform="sleeper", league_id="L1", season=2026, format="guillotine"
        )

    monkeypatch.setattr("fcc.core.config.Settings.league", _league)
    monkeypatch.setattr("fcc.api.app.connector_for", lambda lg, store=None: StubSleeper())
    with TestClient(app) as c:
        yield c


def test_it_reports_the_roster_holes(client):
    body = client.get("/api/leagues/sleeper_main/targets").json()
    causes = {n["cause"] for n in body["needs"]}
    assert "bye" in causes
    assert "out" in causes


def test_a_bye_is_marked_a_rental_and_an_injury_is_not(client):
    needs = {n["cause"]: n for n in client.get("/api/leagues/sleeper_main/targets").json()["needs"]}
    assert needs["bye"]["is_rental"] is True
    assert needs["out"]["is_rental"] is False
    assert needs["out"]["weeks"] > needs["bye"]["weeks"]


def test_it_surfaces_an_unrostered_player(client):
    """A chopped team's players are simply no longer on any roster."""
    body = client.get("/api/leagues/sleeper_main/targets", params={"position": "RB"}).json()
    assert "Chopped Star RB" in [t["name"] for t in body["targets"]]


def test_each_target_carries_its_reasoning_and_a_price(client):
    top = client.get("/api/leagues/sleeper_main/targets").json()["targets"][0]
    assert top["reasons"]
    assert "value_over_free" in top
    assert "typical_bid" in top
    assert top["fills"] is None or "weeks" in top["fills"]


def test_budget_and_field_context_are_included(client):
    body = client.get("/api/leagues/sleeper_main/targets").json()
    assert body["budget"]["remaining"] == 600
    assert body["teams_alive"] >= 1
    assert body["available_count"] > 0


def test_position_filter_applies(client):
    body = client.get("/api/leagues/sleeper_main/targets", params={"position": "TE"}).json()
    assert all(t["position"] == "TE" for t in body["targets"])


def test_unknown_league_is_404(client):
    assert client.get("/api/leagues/nope/targets").status_code == 404


def test_non_sleeper_league_is_400(client, monkeypatch):
    monkeypatch.setattr(
        "fcc.core.config.Settings.league",
        lambda self, key: LeagueConfig(
            key=key, platform="yahoo", league_id="449.l.1", season=2026
        ),
    )
    assert client.get("/api/leagues/yahoo_main/targets").status_code == 400


def test_a_real_league_still_resolves(client):
    assert client.get("/api/leagues/sleeper_main/targets").status_code == 200
