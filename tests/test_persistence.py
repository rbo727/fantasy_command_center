"""Storage-level guarantees.

The timezone test here is a regression guard: SQLite discards tzinfo, which made
every deadline comparison raise TypeError. Those comparisons gate the waiver and
lineup windows, so the failure surfaced at 3am or not at all.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from fcc.core.actions import ActionGate, Proposal
from fcc.core.db import init_db, session_scope
from fcc.core.models import Action, ActionKind, ActionStatus, Tier, utcnow
from fcc.core.secrets import Keys, SecretsError, SecretStore


def test_datetimes_round_trip_timezone_aware():
    init_db()
    deadline = utcnow() + timedelta(hours=3)
    with session_scope() as s:
        s.add(
            Action(
                kind=ActionKind.WAIVER_CLAIM,
                tier=Tier.MONEY,
                idempotency_key="tz-test",
                payload={},
                expires_at=deadline,
            )
        )

    # Fresh session forces a real load from disk rather than the identity map.
    with session_scope() as s:
        loaded = s.scalar(select(Action).where(Action.idempotency_key == "tz-test"))
        assert loaded.expires_at.tzinfo is not None
        # The comparison that used to explode.
        assert loaded.expires_at > utcnow()
        assert loaded.created_at <= datetime.now(UTC)


def test_expiry_is_enforced_after_a_reload():
    """The gate must still honour a deadline on an action loaded from disk."""
    init_db()
    gate = ActionGate()
    with session_scope() as s:
        gate.propose(
            s,
            Proposal(
                kind=ActionKind.PICKEM_SUBMIT,
                idempotency_key="expired",
                payload={},
                rationale="",
                expires_at=utcnow() - timedelta(minutes=5),
            ),
        )

    with session_scope() as s:
        action = s.scalar(select(Action).where(Action.idempotency_key == "expired"))
        gate.execute(s, action, submit=lambda a: pytest.fail("expired action must not submit"))
        assert action.status is ActionStatus.SKIPPED


# --- secrets ---------------------------------------------------------------
def test_secret_round_trip(master_key):
    store = SecretStore()
    store.set(Keys.ESPN_S2, "cookie-value")
    assert SecretStore().get(Keys.ESPN_S2) == "cookie-value"


def test_secret_file_is_encrypted_at_rest(master_key):
    store = SecretStore()
    store.set(Keys.SLEEPER_TOKEN, "super-secret-token")
    blob = store.path.read_bytes()
    assert b"super-secret-token" not in blob
    assert b"sleeper_token" not in blob


def test_names_never_leak_values(master_key):
    store = SecretStore()
    store.set(Keys.FANTASYGURU_PASSWORD, "hunter2")
    assert store.names() == [Keys.FANTASYGURU_PASSWORD]
    assert "hunter2" not in repr(store.names())


def test_wrong_master_key_fails_loudly(master_key, monkeypatch):
    SecretStore().set(Keys.ESPN_SWID, "abc")
    monkeypatch.setenv("FCC_MASTER_KEY", SecretStore.generate_key())
    from fcc.core.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(SecretsError, match="wrong FCC_MASTER_KEY"):
        SecretStore().get(Keys.ESPN_SWID)


def test_missing_master_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("FCC_MASTER_KEY", raising=False)
    from fcc.core.config import get_settings

    get_settings.cache_clear()
    store = SecretStore()
    with pytest.raises(SecretsError, match="FCC_MASTER_KEY is not set"):
        store.set("x", "y")


def test_require_names_the_missing_secret(master_key):
    with pytest.raises(SecretsError, match="espn_s2"):
        SecretStore().require(Keys.ESPN_S2)


def test_env_var_overrides_stored_secret(master_key, monkeypatch):
    store = SecretStore()
    store.set(Keys.ODDS_API_KEY, "from-file")
    monkeypatch.setenv("FCC_SECRET_ODDS_API_KEY", "from-env")
    assert SecretStore().get(Keys.ODDS_API_KEY) == "from-env"
