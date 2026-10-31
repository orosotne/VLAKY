"""Celá cesta: e-mail o výluke → agent → register → prepočet → odporúčanie dispečerovi (len interne).

Spúšťa sa až keď existuje modul vlaky.posta (inak sa preskočí).
"""
from datetime import timedelta
from email.message import EmailMessage

import pytest
from sqlalchemy import select

posta = pytest.importorskip("vlaky.posta.spracovanie")

from vlaky.agent.beh import run_cycle  # noqa: E402
from vlaky.cas import local, set_fake_now  # noqa: E402
from vlaky.db import session_scope  # noqa: E402
from vlaky.hranice import seed_borders  # noqa: E402
from vlaky.models import (Closure, Email, ExtractedEvent, Notification, Path, Recommendation,  # noqa: E402
                          Train)


def _write_mail(cfg, name, sender, subject, body):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "agent.dispecing@dopravca.example"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{name}@zsr.example>"
    msg["Date"] = "Mon, 02 Nov 2026 05:00:00 +0100"
    msg.set_content(body)
    (cfg.mail_dir / f"{name}.eml").write_bytes(bytes(msg))


def test_vyluka_emailom_az_po_odporucanie(cfg, fixed_now):
    with session_scope() as s:
        seed_borders(s)
        t = Train(number="54321", run_date=fixed_now.date().isoformat(), origin="Čadca", destination="Košice",
                  traction="E", status="planovany", ready_utc=fixed_now + timedelta(minutes=30),
                  deadline_utc=fixed_now + timedelta(hours=12))
        s.add(t)
        s.flush()
        s.add(Path(train_id=t.id, train_number="54321", status="pridelena",
                   departure_utc=fixed_now + timedelta(minutes=30)))
    start = local(fixed_now + timedelta(hours=3))
    end = local(fixed_now + timedelta(hours=7))
    body = (f"FIKTÍVNE – ukážka\nOznamujeme výluku koľají v úseku Liptovský Mikuláš – Poprad-Tatry "
            f"dňa {start:%d.%m.%Y} od {start:%H:%M} do {end:%d.%m.%Y} {end:%H:%M}.\nDruh: úplná výluka.\n")
    _write_mail(cfg, "vyluka1", "vyluky@zsr.example", "FIKTÍVNE – Oznámenie o výluke", body)
    # nepovolený odosielateľ → karanténa, nič sa nepoužije
    _write_mail(cfg, "cudzi", "niekto@utocnik.example", "Výluka", body.replace("Liptovský", "Liptovský"))

    summary = run_cycle(fixed_now, force_evaluate=True)
    assert not [e for e in summary["errors"] if "schránka" in e], summary
    with session_scope() as s:
        emails = list(s.scalars(select(Email)))
        assert len(emails) == 2
        assert {e.verdict for e in emails} >= {"karantena"}
        ev = s.scalar(select(ExtractedEvent).where(ExtractedEvent.kind.like("vyluka%")))
        assert ev is not None
        # udalosť v horizonte 72 h ide na potvrdenie – potvrdí ju dispečer
        if ev.status == "navrh":
            posta.confirm_event(s, ev.id, "dispecer")
        cl = s.scalar(select(Closure))
        assert cl is not None and cl.from_station == "Liptovský Mikuláš"

    set_fake_now(fixed_now + timedelta(minutes=1))
    run_cycle(fixed_now + timedelta(minutes=1), force_evaluate=True)
    with session_scope() as s:
        rec = s.scalar(select(Recommendation).order_by(Recommendation.id.desc()))
        assert rec is not None
        assert rec.decision in ("POČKAŤ", "ODSTAVIŤ", "PRESMEROVAŤ")
        assert any("Liptovský Mikuláš – Poprad-Tatry" in r for r in rec.reasons)
        notes = list(s.scalars(select(Notification)))
        assert notes, "dispečer musí dostať odporúčanie"
        allowed = {"dispecer1@dopravca.example", "dispecer2@dopravca.example", "+421900000001", "webhook"}
        assert all(n.recipient in allowed for n in notes if n.status != "zablokovana")
        assert all(n.status in ("outbox", "odoslana", "potlacena") for n in notes if n.recipient in allowed)
