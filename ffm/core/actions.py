"""The action gate — the single choke point for every platform mutation.

Nothing in this app calls a platform's write API directly. Engines *propose*;
the gate decides whether the proposal may execute, executes it, and then reads
the platform back to confirm it actually took effect.

Three properties this buys us:

* **No double-submits.** Every proposal carries an idempotency key, so a
  scheduler misfire or a manual re-run converges instead of stacking claims.
* **No silent wrongness.** A submission that isn't confirmed by a read-back is
  recorded as ``unverified`` and notified, not quietly counted as success.
* **No accidental spend.** Money-tier actions cannot auto-execute; there is no
  configuration flag that changes this.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ffm.core.config import get_settings
from ffm.core.models import (
    Action,
    ActionKind,
    ActionStatus,
    League,
    Tier,
    tier_for,
    utcnow,
)
from ffm.core.notify import Notifier

log = logging.getLogger(__name__)

#: A submit function performs the platform write and returns its raw response.
SubmitFn = Callable[[Action], dict]
#: A verify function re-reads the platform and reports whether it agrees.
VerifyFn = Callable[[Action], "Verification"]


@dataclass
class Verification:
    """Outcome of reading the platform back after a write."""

    ok: bool
    detail: dict = field(default_factory=dict)
    message: str = ""


@dataclass
class Proposal:
    """An intended mutation, before the gate has ruled on it."""

    kind: ActionKind
    idempotency_key: str
    payload: dict
    rationale: str
    league_key: str | None = None
    season: int | None = None
    week: int | None = None
    execute_after: datetime | None = None
    expires_at: datetime | None = None


class ActionGate:
    def __init__(self, notifier: Notifier | None = None) -> None:
        self.settings = get_settings()
        self.notifier = notifier or Notifier()

    # -- proposing --------------------------------------------------------
    def propose(self, session: Session, proposal: Proposal) -> Action:
        """Record an intent, converging on the idempotency key.

        Re-proposing an already-settled action is a no-op: we hand back the
        existing row untouched rather than resurrecting a decision you already
        made (or a claim we already submitted).
        """
        existing = session.scalar(
            select(Action).where(Action.idempotency_key == proposal.idempotency_key)
        )
        if existing is not None:
            if existing.is_terminal or existing.status == ActionStatus.SUBMITTED:
                log.debug(
                    "action %s already %s; not re-proposing", existing.id, existing.status.value
                )
                return existing
            # Still pending — refresh it with the latest thinking.
            existing.payload = proposal.payload
            existing.rationale = proposal.rationale
            existing.expires_at = proposal.expires_at
            session.flush()
            return existing

        tier = tier_for(proposal.kind)
        action = Action(
            kind=proposal.kind,
            tier=tier,
            status=ActionStatus.PROPOSED,
            idempotency_key=proposal.idempotency_key,
            payload=proposal.payload,
            rationale=proposal.rationale,
            season=proposal.season,
            week=proposal.week,
            execute_after=proposal.execute_after,
            expires_at=proposal.expires_at,
        )
        if proposal.league_key:
            league = session.scalar(select(League).where(League.key == proposal.league_key))
            if league is not None:
                action.league_id = league.id

        # Low-tier work is why this app exists; it clears itself. Money-tier
        # work waits for you, and pings you so it isn't waiting silently.
        if tier is Tier.LOW and self.settings.auto_execute_low_tier:
            action.status = ActionStatus.APPROVED
            action.decided_at = utcnow()
        else:
            self.notifier.needs_approval(action)

        session.add(action)
        session.flush()
        log.info("proposed %s (%s) -> %s", action.kind.value, tier.value, action.status.value)
        return action

    # -- deciding ---------------------------------------------------------
    def approve(self, session: Session, action: Action) -> Action:
        if action.is_terminal:
            raise ValueError(f"action {action.id} is already {action.status.value}")
        action.status = ActionStatus.APPROVED
        action.decided_at = utcnow()
        session.flush()
        return action

    def reject(self, session: Session, action: Action, reason: str = "") -> Action:
        action.status = ActionStatus.REJECTED
        action.decided_at = utcnow()
        if reason:
            action.rationale = f"{action.rationale}\n\nRejected: {reason}".strip()
        session.flush()
        return action

    # -- executing --------------------------------------------------------
    def execute(
        self,
        session: Session,
        action: Action,
        submit: SubmitFn,
        verify: VerifyFn | None = None,
    ) -> Action:
        """Submit an approved action, then confirm it landed.

        Callers pass the platform-specific ``submit``/``verify`` closures; the
        gate owns the state machine so every platform behaves identically.
        """
        if action.status is not ActionStatus.APPROVED:
            log.info("skipping action %s: status is %s", action.id, action.status.value)
            return action

        now = utcnow()
        if action.expires_at and now > action.expires_at:
            action.status = ActionStatus.SKIPPED
            action.error = "expired before execution"
            session.flush()
            log.warning("action %s expired (window closed)", action.id)
            return action
        if action.execute_after and now < action.execute_after:
            log.debug("action %s not due until %s", action.id, action.execute_after)
            return action

        if self.settings.dry_run:
            action.status = ActionStatus.DRY_RUN
            action.submitted_at = now
            action.response = {"dry_run": True, "would_send": action.payload}
            session.flush()
            log.info("[dry-run] %s: %s", action.kind.value, action.payload)
            return action

        try:
            action.response = submit(action)
            action.status = ActionStatus.SUBMITTED
            action.submitted_at = utcnow()
            session.flush()
        except Exception as exc:  # noqa: BLE001 - recorded, notified, re-raised by caller if needed
            action.status = ActionStatus.FAILED
            action.error = f"{type(exc).__name__}: {exc}"
            session.flush()
            log.exception("action %s failed to submit", action.id)
            self.notifier.action_failed(action)
            return action

        if verify is None:
            # No read-back available for this platform/action. Say so plainly
            # rather than claiming a verification we never performed.
            action.status = ActionStatus.UNVERIFIED
            action.verification = {"checked": False, "reason": "no verifier supplied"}
            session.flush()
            return action

        try:
            result = verify(action)
            action.verification = {"checked": True, "ok": result.ok, **result.detail}
            action.verified_at = utcnow()
            action.status = ActionStatus.VERIFIED if result.ok else ActionStatus.UNVERIFIED
            if not result.ok:
                action.error = result.message or "read-back did not match submitted payload"
                self.notifier.action_unverified(action)
            session.flush()
        except Exception as exc:  # noqa: BLE001
            action.status = ActionStatus.UNVERIFIED
            action.verification = {"checked": False, "error": f"{type(exc).__name__}: {exc}"}
            session.flush()
            log.exception("action %s submitted but verification errored", action.id)
            self.notifier.action_unverified(action)

        return action

    def propose_and_execute(
        self,
        session: Session,
        proposal: Proposal,
        submit: SubmitFn,
        verify: VerifyFn | None = None,
    ) -> Action:
        """Convenience path for low-tier work that clears itself."""
        action = self.propose(session, proposal)
        return self.execute(session, action, submit, verify)

    # -- queries ----------------------------------------------------------
    @staticmethod
    def pending_approval(session: Session) -> list[Action]:
        return list(
            session.scalars(
                select(Action)
                .where(Action.status == ActionStatus.PROPOSED)
                .order_by(Action.created_at)
            )
        )

    @staticmethod
    def due(session: Session) -> list[Action]:
        """Approved actions ready to run now."""
        now = utcnow()
        rows = session.scalars(
            select(Action).where(Action.status == ActionStatus.APPROVED).order_by(Action.created_at)
        )
        return [a for a in rows if not a.execute_after or a.execute_after <= now]
