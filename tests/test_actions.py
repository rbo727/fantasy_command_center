"""The action gate.

Everything the app does to a live league passes through here, so these tests
are the ones that matter most: they pin the money-tier lock, the no-double-submit
guarantee, and the refusal to call an unconfirmed write a success.
"""

import pytest

from ffm.core.actions import ActionGate, Proposal, Verification
from ffm.core.models import ActionKind, ActionStatus, Tier, tier_for


def proposal(key="k1", kind=ActionKind.PICKEM_SUBMIT, **kw) -> Proposal:
    return Proposal(
        kind=kind,
        idempotency_key=key,
        payload=kw.pop("payload", {"picks": [{"prop": "p1", "option": "o1"}]}),
        rationale=kw.pop("rationale", "staff pick"),
        **kw,
    )


@pytest.fixture
def gate():
    return ActionGate()


# --- tiering ---------------------------------------------------------------
def test_tier_policy_matches_the_agreed_autonomy_split():
    assert tier_for(ActionKind.PICKEM_SUBMIT) is Tier.LOW
    assert tier_for(ActionKind.LINEUP_SWAP) is Tier.LOW
    for kind in (
        ActionKind.WAIVER_CLAIM,
        ActionKind.SURVIVOR_PICK,
        ActionKind.DROP_PLAYER,
        ActionKind.TRADE,
        ActionKind.FREE_AGENT_ADD,
    ):
        assert tier_for(kind) is Tier.MONEY


def test_low_tier_auto_approves(gate, session):
    action = gate.propose(session, proposal())
    assert action.status is ActionStatus.APPROVED


def test_money_tier_waits_for_approval(gate, session):
    action = gate.propose(session, proposal(kind=ActionKind.WAIVER_CLAIM))
    assert action.status is ActionStatus.PROPOSED
    assert action.tier is Tier.MONEY


def test_money_tier_will_not_execute_without_approval(gate, session):
    """The core money guarantee: no approval, no spend — and no exception either."""
    action = gate.propose(session, proposal(kind=ActionKind.WAIVER_CLAIM))
    called = []
    gate.execute(session, action, submit=lambda a: called.append(a) or {})
    assert called == []
    assert action.status is ActionStatus.PROPOSED


def test_approved_money_action_executes(gate, session):
    action = gate.propose(session, proposal(kind=ActionKind.WAIVER_CLAIM))
    gate.approve(session, action)
    gate.execute(
        session,
        action,
        submit=lambda a: {"ok": True},
        verify=lambda a: Verification(ok=True, detail={"claimed": True}),
    )
    assert action.status is ActionStatus.VERIFIED


def test_rejected_action_never_runs(gate, session):
    action = gate.propose(session, proposal(kind=ActionKind.DROP_PLAYER))
    gate.reject(session, action, "changed my mind")
    gate.execute(session, action, submit=lambda a: pytest.fail("must not submit"))
    assert action.status is ActionStatus.REJECTED
    assert "changed my mind" in action.rationale


# --- idempotency -----------------------------------------------------------
def test_reproposing_the_same_key_does_not_duplicate(gate, session):
    a = gate.propose(session, proposal(key="same"))
    b = gate.propose(session, proposal(key="same"))
    assert a.id == b.id


def test_reproposing_after_submission_does_not_resurrect(gate, session):
    """A scheduler misfire must not re-send a claim we already sent."""
    action = gate.propose(session, proposal(key="same"))
    gate.execute(session, action, submit=lambda a: {"ok": True})
    assert action.status is ActionStatus.UNVERIFIED  # submitted, no verifier

    calls = []
    again = gate.propose(session, proposal(key="same"))
    gate.execute(session, again, submit=lambda a: calls.append(1) or {})
    assert again.id == action.id
    assert calls == []


def test_pending_proposal_is_refreshed_not_duplicated(gate, session):
    a = gate.propose(session, proposal(key="same", kind=ActionKind.WAIVER_CLAIM))
    b = gate.propose(
        session,
        proposal(key="same", kind=ActionKind.WAIVER_CLAIM, payload={"bid": 22}),
    )
    assert a.id == b.id
    assert b.payload == {"bid": 22}


# --- execution and verification -------------------------------------------
def test_verification_failure_is_reported_not_swallowed(gate, session):
    action = gate.propose(session, proposal())
    gate.execute(
        session,
        action,
        submit=lambda a: {"ok": True},
        verify=lambda a: Verification(ok=False, message="ESPN shows no picks"),
    )
    assert action.status is ActionStatus.UNVERIFIED
    assert "ESPN shows no picks" in action.error


def test_verifier_that_raises_leaves_action_unverified(gate, session):
    action = gate.propose(session, proposal())
    gate.execute(
        session,
        action,
        submit=lambda a: {"ok": True},
        verify=lambda a: (_ for _ in ()).throw(RuntimeError("network down")),
    )
    assert action.status is ActionStatus.UNVERIFIED
    assert "network down" in str(action.verification)


def test_missing_verifier_is_recorded_as_unverified_not_verified(gate, session):
    """No read-back means no claim of success."""
    action = gate.propose(session, proposal())
    gate.execute(session, action, submit=lambda a: {"ok": True})
    assert action.status is ActionStatus.UNVERIFIED
    assert action.verification == {"checked": False, "reason": "no verifier supplied"}


def test_submit_failure_is_captured(gate, session):
    action = gate.propose(session, proposal())
    gate.execute(session, action, submit=lambda a: (_ for _ in ()).throw(ValueError("boom")))
    assert action.status is ActionStatus.FAILED
    assert "boom" in action.error


def test_expired_action_is_skipped(gate, session):
    from datetime import timedelta

    from ffm.core.models import utcnow

    action = gate.propose(session, proposal(expires_at=utcnow() - timedelta(minutes=1)))
    gate.execute(session, action, submit=lambda a: pytest.fail("must not submit after lock"))
    assert action.status is ActionStatus.SKIPPED


def test_action_not_yet_due_is_left_alone(gate, session):
    from datetime import timedelta

    from ffm.core.models import utcnow

    action = gate.propose(session, proposal(execute_after=utcnow() + timedelta(hours=1)))
    gate.execute(session, action, submit=lambda a: pytest.fail("must not submit early"))
    assert action.status is ActionStatus.APPROVED


# --- dry run ---------------------------------------------------------------
def test_dry_run_records_the_payload_without_sending(gate, session, monkeypatch):
    from ffm.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("FFM_DRY_RUN", "true")
    dry_gate = ActionGate()

    action = dry_gate.propose(session, proposal())
    dry_gate.execute(session, action, submit=lambda a: pytest.fail("dry run must not send"))
    assert action.status is ActionStatus.DRY_RUN
    assert action.response["would_send"] == action.payload


def test_due_lists_only_ready_actions(gate, session):
    from datetime import timedelta

    from ffm.core.models import utcnow

    ready = gate.propose(session, proposal(key="ready"))
    gate.propose(session, proposal(key="later", execute_after=utcnow() + timedelta(hours=2)))
    gate.propose(session, proposal(key="waiting", kind=ActionKind.WAIVER_CLAIM))
    assert [a.id for a in gate.due(session)] == [ready.id]
