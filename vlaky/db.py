"""Databáza: SQLAlchemy 2.0 (SQLite na vývoj a testy, PostgreSQL na serveri)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_ENGINES: dict[str, Engine] = {}
_SESSIONS: dict[str, sessionmaker] = {}


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def engine(url: str | None = None) -> Engine:
    url = url or settings().database_url
    if url not in _ENGINES:
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs["pool_pre_ping"] = True  # po reštarte DB (napr. aktualizácia) nepadne prvý dotaz
        eng = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(eng, "connect")
            def _pragma(dbapi_conn, _rec):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()
        _ENGINES[url] = eng
        _SESSIONS[url] = sessionmaker(bind=eng, expire_on_commit=False, future=True)
    return _ENGINES[url]


def init_db(url: str | None = None) -> None:
    from . import models  # noqa: F401  (registrácia tabuliek)

    eng = engine(url)
    if eng.url.get_backend_name() == "sqlite" and eng.url.database:
        import os

        os.makedirs(os.path.dirname(os.path.abspath(eng.url.database)), exist_ok=True)
    Base.metadata.create_all(eng)


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    engine(url)
    sess = _SESSIONS[url or settings().database_url]()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def dispose_all() -> None:
    for eng in _ENGINES.values():
        eng.dispose()
    _ENGINES.clear()
    _SESSIONS.clear()
