"""The dashboard API.

Two behaviours carry real weight: a failing league must degrade to one bad card
rather than a blank page, and the approve/reject endpoints must respect the same
money-tier rules as the gate — the HTTP layer is not a way around them.
"""

import pytest
from fastapi.testclient import TestClient

from fcc.api.app import app
from fcc.core.actions import ActionGate, Proposal
from fcc.core.config import LeagueConfig
from fcc.core.db import init_db, session_scope
from fcc.core.models import Action, ActionKind, ActionStatus
from fcc.platforms.base import LeagueSummary
from fcc.platforms.registry import LeagueResult


@pytest.fixture
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def _propose(kind=ActionKind.WAIVER_CLAIM, key="k1", payload=None):
    with session_scope() as s:
        action = ActionGate().propose(
            s,
            Proposal(
                kind=kind,
                idempotency_key=key,
                payload=payload or {"player": "Test RB", "bid": 22},
                rationale="waiver target",
            ),
        )
        return action.id


# --- health / overview -----------------------------------------------------
def test_health_reports_the_safety_switch(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert "dry_run" in body


def test_overview_surfaces_what_needs_a_decision(client):
    action_id = _propose()
    body = client.get("/api/overview").json()
    assert [a["id"] for a in body["needs_approval"]] == [action_id]


def test_overview_surfaces_unverified_writes(client):
    """A submitted-but-unconfirmed write is the dangerous state; show it."""
    with session_scope() as s:
        gate = ActionGate()
        action = gate.propose(
            s, Proposal(ActionKind.PICKEM_SUBMIT, "pk", {}, "picks")
        )
        gate.execute(s, action, submit=lambda a: {"ok": True})  # no verifier
        assert action.status is ActionStatus.UNVERIFIED

    body = client.get("/api/overview").json()
    assert any(a["kind"] == "pickem_submit" for a in body["needs_attention"])


# --- league failure isolation ---------------------------------------------
def test_one_broken_league_does_not_blank_the_page(client, monkeypatch):
    good = LeagueResult(
        key="sleeper_main",
        platform="sleeper",
        summary=LeagueSummary(
            key="sleeper_main", platform="sleeper", name="Main", wins=7, losses=3
        ),
    )
    broken = LeagueResult(
        key="espn_main", platform="espn", error="401 Unauthorized", supported=True
    )
    monkeypatch.setattr("fcc.api.app.load_summaries", lambda: [good, broken])

    rows = client.get("/api/leagues").json()
    assert client.get("/api/leagues").status_code == 200
    by_key = {r["key"]: r for r in rows}
    assert by_key["sleeper_main"]["record"] == "7-3"
    assert by_key["espn_main"]["error"] == "401 Unauthorized"
    assert by_key["espn_main"].get("record") is None


def test_unsupported_platform_is_distinguished_from_a_failure(client, monkeypatch):
    """'Not built yet' and 'broken' must not look the same on the dashboard."""
    monkeypatch.setattr(
        "fcc.api.app.load_summaries",
        lambda: [
            LeagueResult(
                key="yahoo_main",
                platform="yahoo",
                error="yahoo reads land in Stage 3",
                supported=False,
            )
        ],
    )
    rows = client.get("/api/leagues").json()
    assert rows[0]["supported"] is False
    # Unsupported leagues are not "errors needing attention".
    assert client.get("/api/overview").json()["league_errors"] == []


def test_unknown_league_roster_is_404(client):
    assert client.get("/api/leagues/nope/roster").status_code == 404


def test_roster_failure_is_502_not_500(client, monkeypatch):
    monkeypatch.setattr(
        "fcc.core.config.Settings.league",
        lambda self, key: LeagueConfig(
            key=key, platform="sleeper", league_id="L1", season=2026
        ),
    )

    def boom(_league, store=None):
        raise RuntimeError("sleeper is down")

    monkeypatch.setattr("fcc.api.app.connector_for", boom)
    resp = client.get("/api/leagues/sleeper_main/roster")
    assert resp.status_code == 502
    assert "sleeper is down" in resp.json()["detail"]


# --- the action queue ------------------------------------------------------
def test_pending_lists_only_money_tier_work(client):
    _propose(kind=ActionKind.WAIVER_CLAIM, key="money")
    _propose(kind=ActionKind.PICKEM_SUBMIT, key="low")   # auto-approves
    pending = client.get("/api/actions/pending").json()
    assert [a["kind"] for a in pending] == ["waiver_claim"]
    assert pending[0]["needs_approval"] is True


def test_approve_moves_the_action_forward(client):
    action_id = _propose(key="approve-me")
    body = client.post(f"/api/actions/{action_id}/approve").json()
    assert body["status"] == "approved"
    with session_scope() as s:
        assert s.get(Action, action_id).status is ActionStatus.APPROVED


def test_reject_records_the_reason(client):
    action_id = _propose(key="reject-me")
    body = client.post(
        f"/api/actions/{action_id}/reject", json={"reason": "too expensive"}
    ).json()
    assert body["status"] == "rejected"
    assert "too expensive" in body["rationale"]


def test_cannot_approve_an_already_settled_action(client):
    action_id = _propose(key="settled")
    client.post(f"/api/actions/{action_id}/reject", json={"reason": "no"})
    again = client.post(f"/api/actions/{action_id}/approve")
    assert again.status_code == 409


def test_approving_a_missing_action_is_404(client):
    assert client.post("/api/actions/999999/approve").status_code == 404


def test_actions_endpoint_rejects_an_unknown_status_filter(client):
    assert client.get("/api/actions", params={"status": "banana"}).status_code == 400


def test_action_payload_and_rationale_reach_the_client(client):
    """You can't approve what you can't see: both must be in the response."""
    _propose(key="visible", payload={"player": "Bench Guy", "bid": 41})
    action = client.get("/api/actions/pending").json()[0]
    assert action["payload"] == {"player": "Bench Guy", "bid": 41}
    assert action["rationale"] == "waiver target"


# --- lineup guardian -------------------------------------------------------
def test_lineup_endpoint_reports_swaps_and_warnings(client, monkeypatch):
    from fcc.platforms.base import Player, PlayerStatus, Roster, RosterSlot

    def fake_connector(_league, store=None):
        class C:
            def roster(self, week=None):
                return Roster(
                    league_key="sleeper_main",
                    team_id="1",
                    week=5,
                    slots=[
                        RosterSlot(
                            "RB",
                            Player("o", "Hurt RB", position="RB", status=PlayerStatus.OUT),
                            starter=True,
                        ),
                        RosterSlot(
                            "WR",
                            Player(
                                "q", "Maybe WR", position="WR", status=PlayerStatus.QUESTIONABLE
                            ),
                            starter=True,
                        ),
                        RosterSlot("BN", Player("b", "Fine RB", position="RB")),
                    ],
                )

        return C()

    monkeypatch.setattr(
        "fcc.core.config.Settings.league",
        lambda self, key: LeagueConfig(
            key=key, platform="sleeper", league_id="L1", season=2026
        ),
    )
    monkeypatch.setattr("fcc.api.app.connector_for", fake_connector)

    body = client.get("/api/leagues/sleeper_main/lineup").json()
    assert body["clean"] is False
    assert body["swaps"] == [
        {
            "slot": "RB",
            "out": "Hurt RB",
            "out_status": "out",
            "in": "Fine RB",
            "reason": "Hurt RB is out",
        }
    ]
    # Questionable is surfaced as a warning, never swapped.
    assert [w["kind"] for w in body["warnings"]] == ["questionable"]


def test_lineup_endpoint_404s_for_an_unknown_league(client):
    assert client.get("/api/leagues/nope/lineup").status_code == 404


# --- FAAB / waivers --------------------------------------------------------
def _stub_sleeper_for_faab():
    from fcc.platforms.base import LeagueSummary

    TX = []
    for pid, bids in {
        "te_a": [(4, "complete"), (2, "failed")],
        "te_b": [(16, "complete"), (14, "failed")],
        "te_c": [(9, "complete")],
        "te_d": [(31, "complete"), (12, "failed")],
    }.items():
        for bid, status in bids:
            TX.append({
                "type": "waiver", "status": status, "leg": 2,
                "adds": {pid: 1}, "settings": {"waiver_bid": bid},
            })

    class C:
        def league(self):
            return {"settings": {"waiver_budget": 1000}}

        def league_summary(self):
            return LeagueSummary(key="g", platform="sleeper", faab_remaining=640, week=3)

        def transaction_history(self, through_week=None):
            return TX

        def players(self):
            return {f"te_{x}": {"full_name": f"TE {x}", "position": "TE"} for x in "abcd"}

        def league_budgets(self):
            return [
                {"roster_id": 1, "is_me": True, "budget_remaining": 640,
                 "likely_chopped": False, "team_name": "Me"},
                {"roster_id": 2, "is_me": False, "budget_remaining": 420,
                 "likely_chopped": False, "team_name": "Rival"},
                {"roster_id": 3, "is_me": False, "budget_remaining": 990,
                 "likely_chopped": True, "team_name": "Chopped"},
            ]

    return C()


def _faab_league(monkeypatch, fmt="guillotine"):
    monkeypatch.setattr(
        "fcc.core.config.Settings.league",
        lambda self, key: LeagueConfig(
            key=key, platform="sleeper", league_id="L1", season=2026, format=fmt
        ),
    )
    monkeypatch.setattr(
        "fcc.api.app.connector_for", lambda lg, store=None: _stub_sleeper_for_faab()
    )


def test_faab_endpoint_returns_market_and_ladder(client, monkeypatch):
    _faab_league(monkeypatch, fmt="redraft")
    body = client.get("/api/leagues/sleeper_main/faab").json()
    assert body["budget"] == {"total": 1000, "remaining": 640}
    assert body["market"]["resolved"] == 4
    assert [row["probability"] for row in body["ladder"]][0] == 0.5
    assert body["top_claims"][0]["winning_bid"] == 31
    # No guillotine block for a redraft league - its advice would be wrong there.
    assert body["guillotine"] is None


def test_faab_endpoint_adds_guillotine_state_for_that_format(client, monkeypatch):
    _faab_league(monkeypatch)
    g = client.get("/api/leagues/sleeper_main/faab").json()["guillotine"]
    # The chopped team's $990 must not count against you.
    assert g["max_rival_budget"] == 420
    assert g["can_outbid_anyone"] is True
    assert g["price_to_guarantee"] == 421
    assert g["teams_alive"] == 2
    assert any("wins any player outright" in line for line in g["advice"])


def test_faab_endpoint_can_segment_by_position(client, monkeypatch):
    _faab_league(monkeypatch)
    body = client.get("/api/leagues/sleeper_main/faab", params={"position": "TE"}).json()
    assert "TE" in body["market"]["segment"]


def test_faab_endpoint_rejects_a_non_sleeper_league(client, monkeypatch):
    monkeypatch.setattr(
        "fcc.core.config.Settings.league",
        lambda self, key: LeagueConfig(
            key=key, platform="yahoo", league_id="449.l.1", season=2026
        ),
    )
    resp = client.get("/api/leagues/yahoo_main/faab")
    assert resp.status_code == 400
    assert "Sleeper-only" in resp.json()["detail"]


def test_faab_endpoint_404s_for_unknown_league(client):
    assert client.get("/api/leagues/nope/faab").status_code == 404
