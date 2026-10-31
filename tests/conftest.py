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
    """Izolované nastavenia pre test (vlastná SQLite DB, schránka, outbox)."""
    from vlaky import config as config_mod
    from vlaky.db import dispose_all, init_db

    db_url = f"sqlite:///{tmp_path / 'test.db'}"
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
    init_db(db_url)
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
