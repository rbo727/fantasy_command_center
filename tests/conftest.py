"""Test fixtures.

Every test runs against a throwaway data dir and its own SQLite file, so no test
can read or write the real secret store or action history.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("FCC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FCC_LEAGUES_FILE", str(tmp_path / "leagues.yml"))
    monkeypatch.setenv("FCC_DRY_RUN", "false")  # tests exercise the real paths
    monkeypatch.setenv("FCC_NTFY_TOPIC", "")
    monkeypatch.setenv("FCC_DISCORD_WEBHOOK_URL", "")
    # Settings and the engine are cached process-wide; reset both per test.
    from fcc.core import db
    from fcc.core.config import get_settings

    get_settings.cache_clear()
    db.reset_engine()
    yield
    get_settings.cache_clear()
    db.reset_engine()


@pytest.fixture
def session():
    from fcc.core.db import init_db, session_scope

    init_db()
    with session_scope() as s:
        yield s


@pytest.fixture
def master_key(monkeypatch) -> str:
    from fcc.core.secrets import SecretStore

    key = SecretStore.generate_key()
    monkeypatch.setenv("FCC_MASTER_KEY", key)
    for var in list(os.environ):
        if var.startswith("FCC_SECRET_"):
            monkeypatch.delenv(var, raising=False)
    from fcc.core.config import get_settings

    get_settings.cache_clear()
    return key
