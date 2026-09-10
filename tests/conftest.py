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
    monkeypatch.setenv("FFM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FFM_LEAGUES_FILE", str(tmp_path / "leagues.yml"))
    monkeypatch.setenv("FFM_DRY_RUN", "false")  # tests exercise the real paths
    monkeypatch.setenv("FFM_NTFY_TOPIC", "")
    monkeypatch.setenv("FFM_DISCORD_WEBHOOK_URL", "")
    # Settings and the engine are cached process-wide; reset both per test.
    from ffm.core import db
    from ffm.core.config import get_settings

    get_settings.cache_clear()
    db.reset_engine()
    yield
    get_settings.cache_clear()
    db.reset_engine()


@pytest.fixture
def session():
    from ffm.core.db import init_db, session_scope

    init_db()
    with session_scope() as s:
        yield s


@pytest.fixture
def master_key(monkeypatch) -> str:
    from ffm.core.secrets import SecretStore

    key = SecretStore.generate_key()
    monkeypatch.setenv("FFM_MASTER_KEY", key)
    for var in list(os.environ):
        if var.startswith("FFM_SECRET_"):
            monkeypatch.delenv(var, raising=False)
    from ffm.core.config import get_settings

    get_settings.cache_clear()
    return key
