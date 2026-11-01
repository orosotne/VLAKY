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
    _add_missing_columns(eng)
    _add_missing_indexes(eng)


def _add_missing_columns(eng: Engine) -> None:
    """Jednoduchá migrácia: doplní do existujúcich tabuliek nové stĺpce (create_all ich nepridá).

    Nové stĺpce musia byť nullable alebo mať server_default. Zmeny typov a mazanie stĺpcov sa robia ručne
    (docs/PREVADZKA.md – aktualizácia).
    """
    from sqlalchemy import inspect, text

    insp = inspect(eng)
    existing_tables = set(insp.get_table_names())
    with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = col.type.compile(dialect=eng.dialect)
                default = ""
                if col.server_default is not None and hasattr(col.server_default, "arg"):
                    arg = col.server_default.arg
                    default = f" DEFAULT {arg if isinstance(arg, str) and arg.isdigit() else repr(str(arg))}"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}{default}'))


def _add_missing_indexes(eng: Engine) -> None:
    """Doplní do existujúcich tabuliek indexy pridané v novej verzii (create_all ich do existujúcej tabuľky
    nepridá). Pri veľkej tabuľke môže vytvorenie indexu chvíľu trvať – robí sa raz, pri štarte po aktualizácii."""
    from sqlalchemy import inspect

    insp = inspect(eng)
    existing_tables = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {ix.get("name") for ix in insp.get_indexes(table.name)}
        for index in table.indexes:
            if index.name and index.name not in have:
                index.create(eng, checkfirst=True)


def prepare_savepoints(session: Session) -> None:
    """Pripraví reláciu na vnorené transakcie (``session.begin_nested()`` = SAVEPOINT).

    PostgreSQL ich podporuje priamo. pysqlite (predvolené správanie) pred SAVEPOINT nevydá BEGIN – SAVEPOINT by
    sa stal vonkajšou transakciou (RELEASE by všetko potvrdil, ROLLBACK TO by nevrátil len časť). Preto sa na
    SQLite transakcia otvorí hneď, a to so zámkom zápisu (BEGIN IMMEDIATE): odložený BEGIN by po dlhom čítaní
    pri súbežnom zápise iného procesu (WAL) skončil chybou „database is locked“ na zastaranom snímku. Ak relácia
    už transakciu má (napr. po zápise), nič sa nemení.
    """
    conn = session.connection()
    if conn.dialect.name != "sqlite":
        return
    raw = conn.connection.dbapi_connection
    if raw is not None and not raw.in_transaction:
        conn.exec_driver_sql("BEGIN IMMEDIATE")


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
