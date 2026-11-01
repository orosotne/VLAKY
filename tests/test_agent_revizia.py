"""Regresné testy k nálezom revízie agenta, upozornení a prevádzky."""
from __future__ import annotations

from datetime import timedelta
from email.message import EmailMessage

import pytest
from sqlalchemy import delete, func, select

from vlaky import config as config_mod
from vlaky.agent import beh
from vlaky.cas import set_fake_now
from vlaky.db import session_scope
from vlaky.models import (Closure, Email, KeyValue, Notification, Order, PositionReport, Recommendation, Train,
                          User)


# ------------------------------------------------------------------ výpadok schránky
def test_vypadok_schranky_je_chyba_a_nie_je_cerstvy(cfg, fixed_now, monkeypatch):
    from vlaky.agent.zdravie import get_kv, health_status

    config_mod.override_settings(**{**cfg.model_dump(), "mail_source": "imap", "imap_host": "",
                                    "heartbeat_url": "https://hc.example/ping/abc"})
    with session_scope() as s:
        s.execute(delete(KeyValue).where(KeyValue.key == "mail_last_ok"))
    pings = []
    monkeypatch.setattr(beh, "ping_heartbeat", lambda ok, msg="": pings.append(ok) or True)
    for k in range(3):
        now = fixed_now + timedelta(minutes=35 * k)
        set_fake_now(now)
        summary = beh.run_cycle(now)
        assert any("schránka sa nedá čítať" in e for e in summary["errors"]), summary["errors"]
    with session_scope() as s:
        assert get_kv(s, "mail_last_ok") == {}
    ok, detail = health_status()
    assert not ok and "zatiaľ neprebehol" in detail["schranka"]
    assert pings and not any(pings)  # externý monitoring dostane /fail


def test_funkcna_schranka_zapise_cas_kontroly(cfg, fixed_now):
    from vlaky.agent.zdravie import get_kv

    set_fake_now(fixed_now)
    summary = beh.run_cycle(fixed_now)
    assert not any("schránka" in e for e in summary["errors"])
    with session_scope() as s:
        assert get_kv(s, "mail_last_ok")["ts"] == fixed_now.isoformat()


# ------------------------------------------------------------------ upozornenia na odporúčania
def _rec(s, train_id, depart, fingerprint="fp1", **kw):
    data = dict(train_id=train_id, decision="ODSTAVIŤ", color="ZELENÁ", priority="DÔLEŽITÉ", summary="x",
                chosen={"depart": depart.isoformat(), "route": "A – B"}, fingerprint=fingerprint, state="nove")
    data.update(kw)
    r = Recommendation(**data)
    s.add(r)
    s.flush()
    return r


def test_posun_odchodu_sa_oznami_ale_kolisanie_nie(cfg, fixed_now):
    from vlaky.notifikacie.odosielanie import notify_recommendation

    set_fake_now(fixed_now)
    with session_scope() as s:
        t = Train(number="95001", origin="Žilina", destination="Košice")
        s.add(t)
        s.flush()
        first = notify_recommendation(s, _rec(s, t.id, fixed_now + timedelta(hours=8)))
        moved = notify_recommendation(s, _rec(s, t.id, fixed_now + timedelta(hours=12)))  # výluka predĺžená
        same = notify_recommendation(s, _rec(s, t.id, fixed_now + timedelta(hours=12, minutes=5)))
    assert all(n.status in ("odoslana", "outbox") for n in first)
    assert all(n.status in ("odoslana", "outbox") for n in moved)
    assert all(n.status == "potlacena" for n in same)


def test_zlyhane_dorucenie_sa_zopakuje(cfg, fixed_now, monkeypatch):
    from vlaky.hranice import seed_borders
    from vlaky.notifikacie import odosielanie

    config_mod.override_settings(**{**cfg.model_dump(), "smtp_host": "smtp.example"})
    state = {"down": True, "delivered": []}

    class FlakySMTP:
        def __init__(self, host, port, timeout):
            if state["down"]:
                raise ConnectionRefusedError(111, "Connection refused")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg, to_addrs=None):
            state["delivered"].append(msg["Subject"])
            return {}

    monkeypatch.setattr(odosielanie.smtplib, "SMTP", FlakySMTP)
    with session_scope() as s:
        seed_borders(s)
        s.add(Train(number="95001", origin="Žilina", destination="Košice", traction="E", status="planovany",
                    ready_utc=fixed_now + timedelta(hours=1), deadline_utc=fixed_now + timedelta(hours=30)))
        s.add(Order(customer="Zákazník", origin="Kúty", destination="Štúrovo",
                    earliest_departure_utc=fixed_now + timedelta(hours=8),
                    deadline_utc=fixed_now + timedelta(hours=20)))
    set_fake_now(fixed_now)
    first = beh.run_cycle(fixed_now, force_evaluate=True)
    assert any("doručenie" in e for e in first["errors"])
    with session_scope() as s:
        assert s.scalar(select(Order.status)) == "nova"  # návrh nedoručený → objednávka čaká
    state["down"] = False
    for k in range(1, 8):
        t = fixed_now + timedelta(minutes=k)
        set_fake_now(t)
        beh.run_cycle(t)
    assert any("Nová objednávka" in subj for subj in state["delivered"])
    assert any("Vlak 95001" in subj for subj in state["delivered"])
    with session_scope() as s:
        assert s.scalar(select(Order.status)) == "navrhnuta"


def test_dlhy_predmet_sa_skrati_na_dlzku_stlpca(cfg):
    from vlaky.notifikacie.odosielanie import send_internal

    with session_scope() as s:
        out = send_internal(s, "objednavka", "[AGENT] " + "X" * 600, "telo", "DÔLEŽITÉ", dedup_key="k" * 200,
                            channels=["email"])
        assert out and all(len(n.subject) <= 300 and len(n.dedup_key) <= 120 for n in out)


# ------------------------------------------------------------------ státie vlaku
def test_stojaci_vlak_neposiela_upozornenie_kazdy_cyklus(cfg, fixed_now):
    from vlaky.agent.strazenie import standing_alerts

    t0 = fixed_now
    with session_scope() as s:
        t = Train(number="95002", origin="Kúty", destination="Štúrovo", traction="E", status="v_jazde")
        s.add(t)
        s.flush()
        for m in range(-30, 0):
            s.add(PositionReport(train_id=t.id, train_number="95002", ts_utc=t0 + timedelta(minutes=m),
                                 lat=48.0 + (m * 0.01), lon=17.0, source="gps"))
        for m in range(0, 181):
            s.add(PositionReport(train_id=t.id, train_number="95002", ts_utc=t0 + timedelta(minutes=m),
                                 lat=48.0, lon=17.0, source="gps", nearest_station="Galanta"))
    alert_cycles = []
    for minute in range(20, 181):
        now = t0 + timedelta(minutes=minute)
        set_fake_now(now)
        with session_scope() as s:
            if standing_alerts(s, now):
                alert_cycles.append(minute)
    assert 1 <= len(alert_cycles) <= 2, alert_cycles  # jedno upozornenie (+ najviac jedno pripomenutie)


# ------------------------------------------------------------------ odovzdávka služby
def test_odovzdavka_ukaze_zmenene_a_zrusene_vyluky(cfg):
    from vlaky.agent.odovzdavka import build_report
    from vlaky.cas import now_utc

    now = now_utc()
    with session_scope() as s:
        for a, b in (("Liptovský Mikuláš", "Poprad-Tatry"), ("Nové Zámky", "Štúrovo")):
            s.add(Closure(kind="uplna", from_station=a, to_station=b, start_utc=now + timedelta(hours=3),
                          end_utc=now + timedelta(hours=7), status="overene", source="email",
                          created_at=now - timedelta(days=2), updated_at=now - timedelta(days=2)))
    with session_scope() as s:
        c1, c2 = s.scalars(select(Closure).order_by(Closure.id)).all()
        c1.end_utc, c1.version = now + timedelta(hours=15), 2
        c2.status, c2.version = "zrusene", 2
    with session_scope() as s:
        text, data = build_report(s, now)
    part = text.split("Nové alebo zmenené výluky")[1].split("\n\n")[0]
    assert data["closures_new"] == 2
    assert "zmenená (verzia 2)" in part and "ZRUŠENÁ" in part


# ------------------------------------------------------------------ GDPR uchovávanie
def _mail(i: int, with_body: bool) -> bytes:
    m = EmailMessage()
    m["From"] = "Objednávky <objednavky@zakaznik.example>"
    m["To"] = "agent.dispecing@dopravca.example"
    m["Subject"] = f"Objednávka {i}"
    m["Date"] = "Mon, 02 Nov 2026 05:00:00 +0100"
    m["Message-ID"] = f"<ret-{i}@zakaznik.example>"
    if with_body:
        m.set_content("Dobrý deň, v prílohe objednávka. Kontakt: Ján Novák, +421 900 123 456.")
    csv = "odkiaľ;kam;vozne\nKúty;Štúrovo;20\nkontakt;Ján Novák;+421900123456\n".encode("utf-8")
    m.add_attachment(csv, maintype="text", subtype="csv", filename="objednavka.csv")
    return m.as_bytes()


def test_retencia_vymaze_aj_email_len_s_prilohou(cfg, fixed_now):
    from vlaky.agent.strazenie import retention_purge
    from vlaky.posta.spracovanie import process_mailbox

    set_fake_now(fixed_now)
    (cfg.mail_dir / "a_body.eml").write_bytes(_mail(1, True))
    (cfg.mail_dir / "b_nobody.eml").write_bytes(_mail(2, False))
    with session_scope() as s:
        process_mailbox(s)
    later = fixed_now + timedelta(days=800)
    set_fake_now(later)
    with session_scope() as s:
        retention_purge(s, later)
    with session_scope() as s:
        for e in s.scalars(select(Email)):
            assert e.body_text == "" and e.raw_path == ""
            assert all(not (a.text or "") for a in e.attachments)


# ------------------------------------------------------------------ ukážkové dáta a beh agenta
def test_ukazka_v_produkcii_odmietne(cfg):
    from vlaky.simulacia.demo import run_simulator, seed_demo

    config_mod.override_settings(**{**cfg.model_dump(), "env": "prod"})
    with pytest.raises(RuntimeError):
        seed_demo(copy_emails=False)
    with pytest.raises(RuntimeError):
        run_simulator(max_cycles=1)
    with session_scope() as s:
        assert s.scalar(select(func.count(Train.id))) == 0
        assert s.scalar(select(func.count(User.id))) == 0


def test_ukazka_nemeni_existujuci_ucet(cfg):
    from vlaky.pouzivatelia import authenticate, create_user
    from vlaky.simulacia.demo import seed_demo

    with session_scope() as s:
        create_user(s, "veduci", "skutocne-heslo-123", "admin")
    seed_demo(copy_emails=False)
    with session_scope() as s:
        user = authenticate(s, "veduci", "skutocne-heslo-123")
        assert user is not None and user.role == "admin"


def test_agent_prezije_chybu_cyklu_a_neuvolni_cudzi_zamok(cfg, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from vlaky.agent.zdravie import get_kv, set_kv

    calls = []

    def fake_cycle():
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("SELECT 1", {}, Exception("databáza nedostupná"))
        beh._STOP["flag"] = True
        return {"errors": []}

    clock = [0.0]

    def fake_monotonic():
        clock[0] += 50.0
        return clock[0]

    monkeypatch.setattr(beh, "run_cycle", fake_cycle)
    monkeypatch.setattr(beh.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(beh.time, "sleep", lambda _s: None)
    monkeypatch.setattr(beh.signal, "signal", lambda *a: None)
    with session_scope() as s:
        set_kv(s, beh.LOCK_KEY, {"owner": "iny-server:1", "ts": "2026-11-02T04:00:00+00:00"})
    try:
        assert beh.run_agent() == 0
    finally:
        beh._STOP["flag"] = False
    assert len(calls) == 2
    with session_scope() as s:
        assert get_kv(s, beh.LOCK_KEY)["owner"] == "iny-server:1"


def test_denna_sprava_obsahuje_stav_schranky(cfg, fixed_now):
    now = fixed_now.replace(hour=cfg.daily_report_hour + 1)  # po hodine dennej správy (miestny čas je o 1 h viac)
    set_fake_now(now)
    beh.run_cycle(now)
    with session_scope() as s:
        n = s.scalar(select(Notification).where(Notification.subject.contains("Denná správa")))
    assert n is not None
    assert "e-maily za 24 h" in n.body and "udalosti čakajúce na potvrdenie" in n.body
    assert "posledný stav čítania schránky: ok" in n.body
