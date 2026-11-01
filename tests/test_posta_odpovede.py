"""Odpovede dispečerov na odporúčania (PRIJAŤ / ZAMIETNUŤ)."""
from email.message import EmailMessage

from sqlalchemy import select

from vlaky.models import AuditLog, Recommendation, Train
from vlaky.posta.spracovanie import process_mailbox


def _rec(session, now):
    t = Train(number="1", origin="Čadca", destination="Košice", status="planovany")
    session.add(t)
    session.flush()
    r = Recommendation(train_id=t.id, decision="VYPRAVIŤ", color="ZELENÁ", created_at=now, state="nove")
    session.add(r)
    session.flush()
    return r


def _reply(cfg, name, sender, first_line, rec_id, subject="Re: [AGENT][DÔLEŽITÉ] Vlak 1"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "agent@dopravca.example"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{name}@dopravca.example>"
    msg.set_content(f"{first_line}\n\n> Detail a potvrdenie: http://x/odporucania/{rec_id}\n")
    (cfg.mail_dir / f"{name}.eml").write_bytes(bytes(msg))


def test_prijat_od_dispecera(cfg, session, fixed_now):
    r = _rec(session, fixed_now)
    session.commit()
    _reply(cfg, "a1", "dispecer1@dopravca.example", "PRIJAŤ", r.id)
    out = process_mailbox(session)
    session.commit()
    assert out.get("replies") == 1 and out["events"] == 0
    session.refresh(r)
    assert r.state == "prijate" and r.decided_by == "dispecer1@dopravca.example"


def test_zamietnut_s_dovodom(cfg, session, fixed_now):
    r = _rec(session, fixed_now)
    session.commit()
    _reply(cfg, "a2", "dispecer2@dopravca.example", "ZAMIETNUŤ rušňovodič nie je k dispozícii", r.id)
    process_mailbox(session)
    session.commit()
    session.refresh(r)
    assert r.state == "zamietnute" and "rušňovodič" in r.dispatcher_note


def test_cudzi_odosielatel_nemeni_stav(cfg, session, fixed_now):
    r = _rec(session, fixed_now)
    session.commit()
    # povolená doména zákazníka, ale nie je na zozname dispečerov → bežné spracovanie, nie odpoveď
    _reply(cfg, "a3", "niekto@zakaznik.example", "PRIJAŤ", r.id)
    process_mailbox(session)
    session.commit()
    session.refresh(r)
    assert r.state == "nove"
    assert session.scalar(select(AuditLog).where(AuditLog.action == "recommendation_reply")) is None
