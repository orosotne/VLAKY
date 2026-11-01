"""Notifikácie: len interný zoznam (technicky vynútené), deduplikácia, tieňová prevádzka."""
from datetime import timedelta

from sqlalchemy import select

from vlaky import config as config_mod
from vlaky.hranice import seed_borders
from vlaky.models import AuditLog, Train
from vlaky.notifikacie import odosielanie
from vlaky.notifikacie.odosielanie import (_deliver, allowed_recipients, notify_recommendation,
                                           send_internal)
from vlaky.planovanie.planovac import evaluate_train


def test_mimo_zoznamu_sa_neodosle(session, cfg):
    n = _deliver(session, "email", "zakaznik@zakaznik.example", "x", "y", "KRITICKÉ", "test", "", None)
    assert n.status == "zablokovana"
    assert session.scalar(select(AuditLog).where(AuditLog.action == "notification_blocked")) is not None
    n2 = _deliver(session, "sms", "+421999999999", "x", "y", "KRITICKÉ", "test", "", None)
    assert n2.status == "zablokovana"


def test_interny_zoznam_outbox(session, cfg):
    out = send_internal(session, "test", "Predmet", "Telo", "INFO", channels=["email"])
    assert {n.recipient for n in out} == {"dispecer1@dopravca.example", "dispecer2@dopravca.example"}
    assert all(n.status == "outbox" for n in out)  # SMTP nie je nastavené → kópia v outboxe
    assert len(list(cfg.outbox_dir.iterdir())) == 2


def test_tienova_prevadzka(session, cfg):
    s = config_mod.override_settings(**{**cfg.model_dump(), "shadow_mode": True})
    assert allowed_recipients() == {"email": {"veduci@dopravca.example"}, "sms": set(), "teams": set()}
    out = send_internal(session, "test", "P", "T", "KRITICKÉ")
    assert {n.recipient for n in out} == {"veduci@dopravca.example"}
    n = _deliver(session, "email", "dispecer1@dopravca.example", "x", "y", "INFO", "test", "", None)
    assert n.status == "zablokovana"
    config_mod.override_settings(**{**s.model_dump(), "shadow_mode": False})


def test_odporucanie_podla_priority_a_dedup(session, cfg, fixed_now):
    seed_borders(session)
    t = Train(number="77", origin="Čadca", destination="Košice", traction="E", status="planovany",
              ready_utc=fixed_now + timedelta(minutes=30), deadline_utc=fixed_now + timedelta(hours=12))
    session.add(t)
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")  # bez trasy → DÔLEŽITÉ (treba objednať trasu)
    assert rec.priority in ("DÔLEŽITÉ", "KRITICKÉ")
    sent = notify_recommendation(session, rec)
    emails = [n for n in sent if n.channel == "email"]
    assert len(emails) == 2 and all(n.status == "outbox" for n in emails)
    assert "NÁVRH TRASY" in emails[0].body and "Čadca" in emails[0].body
    again = notify_recommendation(session, rec)
    assert all(n.status == "potlacena" for n in again if n.channel == "email")


def test_smtp_jeden_prijemca(monkeypatch, session, cfg):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = u

        def send_message(self, msg, to_addrs=None):
            sent["to"] = to_addrs
            sent["cc"] = msg.get("Cc")
            return {}

    config_mod.override_settings(**{**cfg.model_dump(), "smtp_host": "smtp.example", "smtp_user": "agent"})
    monkeypatch.setattr(odosielanie.smtplib, "SMTP", FakeSMTP)
    n = _deliver(session, "email", "dispecer1@dopravca.example", "S", "B", "INFO", "test", "", None)
    assert n.status == "odoslana" and sent["to"] == ["dispecer1@dopravca.example"] and sent["cc"] is None
