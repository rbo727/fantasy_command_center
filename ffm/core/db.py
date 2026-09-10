"""Database engine and session handling."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ffm.core.config import get_settings
from ffm.core.models import Base

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_conn, _record) -> None:
    """WAL + foreign keys.

    WAL matters here: the 3am waiver job and the API server read and write the
    same file concurrently, and rollback-journal mode would have them blocking
    each other at exactly the wrong moment.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def get_engine() -> Engine:
    global _engine, _Session
    if _engine is None:
        url = get_settings().db_url
        _engine = create_engine(url, future=True)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _configure_sqlite)
        _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db() -> None:
    """Create tables. Alembic owns migrations once the schema stabilises."""
    Base.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session. Commits on clean exit, rolls back on exception."""
    get_engine()
    assert _Session is not None
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop cached engine/session factory. Used by tests switching databases."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None
