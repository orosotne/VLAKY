"""Spoločné testovacie nastavenia: dočasná databáza, izolované adresáre, pevný čas."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """Izolované nastavenia pre test (vlastná SQLite DB, schránka, outbox).

    VLAKY_TEST_DATABASE_URL (napr. postgresql+psycopg://postgres@127.0.0.1:55432/vlaky_test) – testy pobežia proti
    tejto databáze (ako v ostrej prevádzke); pred každým testom sa jej tabuľky zmažú a vytvoria znova.
    """
    from vlaky import config as config_mod
    from vlaky.db import dispose_all, init_db

    test_db = os.environ.get("VLAKY_TEST_DATABASE_URL", "").strip()
    db_url = test_db or f"sqlite:///{tmp_path / 'test.db'}"
    s = config_mod.override_settings(
        env="test",
        database_url=db_url,
        data_dir=ROOT / "data",
        mail_dir=tmp_path / "schranka",
        outbox_dir=tmp_path / "odoslane",
        allowed_senders="zsr.example,partner.example,zakaznik.example,dopravca.example",
        require_dmarc_pass=False,
        notify_emails="dispecer1@dopravca.example,dispecer2@dopravca.example",
        notify_phones="+421900000001",
        shadow_mode=False,
        shadow_emails="veduci@dopravca.example",
        secret_key="test-secret",
        positions_token="test-token",
        llm_provider="none",
    )
    os.makedirs(s.mail_dir, exist_ok=True)
    os.makedirs(s.outbox_dir, exist_ok=True)
    if test_db:
        from vlaky import models  # noqa: F401  (registrácia tabuliek)
        from vlaky.db import Base, engine

        Base.metadata.drop_all(engine(db_url))
    init_db(db_url)
    # Plánovač bez úspešnej kontroly schránky nikdy nedá ZELENÚ (10.3). V testoch sa schránka považuje za
    # čerstvo skontrolovanú (čas ďaleko v budúcnosti); testy výpadku schránky tento záznam zmažú.
    # Web takisto nezobrazí ZELENÚ bez čerstvého cyklu agenta – aj ten sa považuje za čerstvý (testy výpadku
    # agenta ho prepíšu starým časom alebo zmažú).
    from vlaky.agent.zdravie import set_kv
    from vlaky.db import session_scope

    with session_scope(db_url) as sess:
        set_kv(sess, "mail_last_ok", {"ts": "2100-01-01T00:00:00+00:00", "fetched": 0, "test": True})
        set_kv(sess, "agent_last_cycle", {"ts": "2100-01-01T00:00:00+00:00", "errors": 0, "test": True})
    yield s
    dispose_all()
    config_mod._OVERRIDE.clear()
    from vlaky.cas import set_fake_now

    set_fake_now(None)


@pytest.fixture()
def session(cfg):
    from vlaky.db import session_scope

    with session_scope() as s:
        yield s


@pytest.fixture()
def fixed_now():
    from vlaky.cas import set_fake_now

    dt = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)  # 05:00 miestneho času (CET)
    set_fake_now(dt)
    yield dt
    set_fake_now(None)
