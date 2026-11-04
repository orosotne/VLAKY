"""Regresné testy k 5. revízii (oblasť C – plánovač a doručovanie): súbežné odosielanie odporúčaní.

R5P1 – prepočet z webu na pozadí a cyklus agenta (rôzne procesy) poslali to isté odporúčanie dispečerovi dvakrát:
oba zistili „ešte neodoslané“ skôr, ako druhý potvrdil svoje záznamy. Doručovanie odporúčaní vlaku ide teraz po jednom
pod zámkom (PostgreSQL poradný zámok, SQLite zámok zápisu) a stav odporúčania aj doterajšie pokusy sa zisťujú až pod
zámkom.
R5O3 – prepočet z webu posielal upozornenia v tej istej transakcii, ktorá držala zámky prepočtu vlakov: nedostupný
server správ zdržal prepočet agenta aj tlačidlo „Prepočítať“. Prepočet sa teraz najprv potvrdí a upozornenia idú až
potom (každé odporúčanie vo vlastnej transakcii).

Testy bežia na SQLite (súbor) aj na PostgreSQL (VLAKY_TEST_DATABASE_URL). Všetky údaje sú fiktívne.
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from datetime import timedelta

import pytest
from sqlalchemy import select

from vlaky.db import session_scope
from vlaky.hranice import seed_borders
from vlaky.models import Driver, DriverShift, Notification, Path, Recommendation, Train
from vlaky.notifikacie import odosielanie
from vlaky.planovanie import planovac


def _train(now, number: str) -> int:
    """Vlak Žilina → Košice s pridelenou trasou o 40 min, pripravený o 30 min, rušňovodič naplánovaný (fiktívny)."""
    with session_scope() as s:
        seed_borders(s)
        ready = now + timedelta(minutes=30)
        t = Train(number=number, run_date=ready.date().isoformat(), origin="Žilina", destination="Košice",
                  traction="E", gauge="1435", vmax_kmh=100, status="planovany", ready_utc=ready,
                  deadline_utc=ready + timedelta(hours=10), length_m=500, weight_t=1500)
        s.add(t)
        s.flush()
        s.add(Path(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena",
                   departure_utc=now + timedelta(minutes=40)))
        d = Driver(name=f"R. {number} FIKTÍVNY")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=ready - timedelta(minutes=80),
                          status="planovana"))
        s.flush()
        return t.id


def _rec_mails() -> list:
    with session_scope() as s:
        return [(n.recommendation_id, n.channel, n.recipient, n.status)
                for n in s.scalars(select(Notification).where(Notification.kind == "odporucanie")
                                   .order_by(Notification.id))]


def _postgres() -> bool:
    from vlaky.config import settings

    return settings().database_url.startswith("postgresql")


def test_r5p1_web_a_agent_neposlu_to_iste_odporucanie_dvakrat(cfg, fixed_now, monkeypatch):
    """[R5P1] Agent prepočíta vlak a ide ho oznámiť; web (úprava vlaku) prepočíta ten istý vlak súbežne a jeho prepočet
    vráti odporúčanie, ktoré medzitým uložil agent – obaja ho chcú poslať. Oba odosielatelia sa zastavia tesne pred
    odoslaním prvej správy, kým nie sú pri nej obaja (alebo kým neuplynie 5 s). Každý dispečer dostane odporúčanie
    práve raz."""
    from vlaky.agent import beh
    from vlaky.web import prepocet

    tid = _train(fixed_now, "47501")
    web_thread: dict = {}
    real_cw = planovac.closure_windows

    def hooked_cw(session, start, end):
        if threading.get_ident() == web_thread.get("id"):
            # prepočet z webu začal skôr, ale ukladá až po agentovi – vráti odporúčanie, ktoré uložil agent
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                with session_scope() as s2:
                    if s2.scalar(select(Recommendation.id).where(Recommendation.train_id == tid)) is not None:
                        break
                time.sleep(0.05)
        return real_cw(session, start, end)

    monkeypatch.setattr(planovac, "closure_windows", hooked_cw)
    barrier = threading.Barrier(2, timeout=5)
    waited: set = set()
    real_send = odosielanie._send_email

    def send_email(recipient, subject, body):
        me = threading.get_ident()
        if me not in waited:  # prvá správa každého odosielateľa: obaja už zistili, či ju treba poslať
            waited.add(me)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return real_send(recipient, subject, body)

    monkeypatch.setattr(odosielanie, "_send_email", send_email)
    errors: list = []

    def web():
        web_thread["id"] = threading.get_ident()
        try:
            prepocet.evaluate_train_bg(tid, "úprava vlaku (zadal dispecer)")
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def agent():
        try:
            beh.run_cycle(fixed_now, force_evaluate=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    tw = threading.Thread(target=web)
    tw.start()
    time.sleep(0.3)
    ta = threading.Thread(target=agent)
    ta.start()
    ta.join(180)
    tw.join(180)
    assert not ta.is_alive() and not tw.is_alive() and not errors, errors
    rows = _rec_mails()
    with session_scope() as s:
        rec_ids = list(s.scalars(select(Recommendation.id).where(Recommendation.train_id == tid)))
    assert len(rec_ids) == 1, rows  # jediné odporúčanie (web vrátil to, ktoré uložil agent)
    sent = Counter((rid, ch, r) for rid, ch, r, st in rows if st in odosielanie.SENT_STATES)
    assert set(sent) == {(rec_ids[0], "email", "dispecer1@dopravca.example"),
                         (rec_ids[0], "email", "dispecer2@dopravca.example")}, rows
    assert set(sent.values()) == {1}, rows  # nikdy dva rovnaké e-maily jednému dispečerovi


def test_r5p1_r5o3_agent_neposle_odporucanie_ktore_web_prave_posiela(cfg, fixed_now, monkeypatch):
    """[R5P1 + R5O3] Web prepočíta vlak (nové odporúčanie), prepočet potvrdí a až potom posiela upozornenie. Kým ho
    posiela (pomalý server správ), cyklus agenta odporúčanie už vidí (je potvrdené, zatiaľ bez pokusu o doručenie)
    a chce ho tiež oznámiť. Pri odoslaní z webu je odporúčanie viditeľné z inej relácie a každý dispečer ho dostane
    práve raz – agent počká na zámok doručovania vlaku a potom zistí, že odporúčanie už prišlo."""
    from vlaky.agent import beh
    from vlaky.agent.zdravie import set_kv
    from vlaky.web import prepocet

    tid = _train(fixed_now, "47507")
    with session_scope() as s:
        set_kv(s, "last_evaluate", {"ts": "2100-01-01T00:00:00+00:00"})  # agent neprepočítava, len oznamuje
    web_thread: dict = {}
    sending = threading.Event()
    agent_done = threading.Event()
    seen: list = []
    real_send = odosielanie._send_email

    def send_email(recipient, subject, body):
        if threading.get_ident() == web_thread.get("id") and not sending.is_set():
            with session_scope() as other:  # ako agent: iná relácia vidí len potvrdené údaje
                seen.append(other.scalar(select(Recommendation.id).where(Recommendation.train_id == tid)))
            sending.set()
            agent_done.wait(3)  # pomalý server správ – agent medzitým beží (alebo čaká na zámok doručovania)
        return real_send(recipient, subject, body)

    monkeypatch.setattr(odosielanie, "_send_email", send_email)
    errors: list = []

    def web():
        web_thread["id"] = threading.get_ident()
        try:
            prepocet.evaluate_train_bg(tid, "úprava vlaku (zadal dispecer)")
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def agent():
        try:
            beh.run_cycle(fixed_now)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
        finally:
            agent_done.set()

    tw = threading.Thread(target=web)
    tw.start()
    assert sending.wait(60), "web neposielal upozornenie"
    ta = threading.Thread(target=agent)
    ta.start()
    ta.join(180)
    tw.join(180)
    assert not ta.is_alive() and not tw.is_alive() and not errors, errors
    assert seen and seen[0] is not None, seen  # web posiela až po potvrdení prepočtu
    sent = Counter((rid, ch, r) for rid, ch, r, st in _rec_mails() if st in odosielanie.SENT_STATES)
    assert sorted(r for (_rid, _ch, r) in sent) == ["dispecer1@dopravca.example", "dispecer2@dopravca.example"]
    assert set(sent.values()) == {1}, sent


def test_r5p1_medzitym_zastarane_odporucanie_sa_neoznamuje(cfg, fixed_now):
    """[R5P1] Odosielateľ (cyklus agenta) si odporúčanie vlaku načítal, ale kým sa dostal k zámku doručovania, súbežný
    prepočet uložil novšie odporúčanie (staré je zastarané) a oznámil ho. Staré odporúčanie sa už nepošle – dispečer by
    inak mal ako poslednú správu o vlaku neplatné odporúčanie."""
    tid = _train(fixed_now, "47502")
    with session_scope() as s:
        old = planovac.evaluate_train(s, tid, fixed_now, "pravidelný prepočet")
        old_id = old.id
        assert old.priority != "INFO"  # predpoklad testu: odporúčanie sa posiela hneď (nie len v súhrne)
    with session_scope() as s:
        stale = s.get(Recommendation, old_id)  # odosielateľ ho má v ruke (načítané pred zámkom)
        with session_scope() as other:   # súbežný prepočet: novšie odporúčanie, staré je zastarané
            other.get(Recommendation, old_id).state = "zastarane"
            newer = Recommendation(train_id=tid, decision="POČKAŤ", color="ORANŽOVÁ", priority="DÔLEŽITÉ",
                                   summary="Vlak 47502: POČKAŤ (FIKTÍVNE).", state="nove", fingerprint="novsie",
                                   chosen={"depart": (fixed_now + timedelta(hours=3)).isoformat()})
            other.add(newer)
        out = odosielanie.notify_recommendation(s, stale)
    assert out == []
    assert not [r for r in _rec_mails() if r[0] == old_id]


def test_r5o3_prepocet_z_webu_najprv_potvrdi_potom_posiela(cfg, fixed_now, monkeypatch):
    """[R5O3] Prepočet z webu (evaluate_all_bg po potvrdenej udalosti, evaluate_train_bg, „Prepočítať“) posiela
    upozornenie až po potvrdení prepočtu: pri odoslaní správy je odporúčanie viditeľné aj z inej relácie (zámky
    prepočtu vlakov sú uvoľnené)."""
    from vlaky.web import prepocet

    seen: list = []
    real_send = odosielanie._send_email

    def send_email(recipient, subject, body):
        with session_scope() as other:  # iná relácia (ako agent): vidí len potvrdené údaje
            seen.append(other.scalar(select(Recommendation.id).where(Recommendation.train_id.in_(tids))))
        return real_send(recipient, subject, body)

    monkeypatch.setattr(odosielanie, "_send_email", send_email)
    tids = [_train(fixed_now, "47503")]
    prepocet.evaluate_all_bg("potvrdená udalosť z e-mailu #1")
    assert seen and all(x is not None for x in seen), seen
    tids = [_train(fixed_now, "47504")]
    seen.clear()
    prepocet.evaluate_train_bg(tids[0], "nový vlak (zadal dispecer)")
    assert seen and all(x is not None for x in seen), seen
    tids = [_train(fixed_now, "47505")]
    seen.clear()
    out, error = prepocet.evaluate_train_now(tids[0], "dispecer")
    assert out and not error and seen and all(x is not None for x in seen), (out, error, seen)
    sent = Counter((ch, r) for _rid, ch, r, st in _rec_mails() if st in odosielanie.SENT_STATES)
    assert sent == Counter({("email", "dispecer1@dopravca.example"): 3, ("email", "dispecer2@dopravca.example"): 3})


def test_r5o3_pg_prepocet_agenta_neceka_na_odosielanie_z_webu(cfg, fixed_now, monkeypatch):
    """[R5O3] PostgreSQL: kým prepočet z webu posiela upozornenie (server správ odpovedá pomaly), prepočet toho istého
    vlaku agentom nečaká na zámok vlaku – web ho už uvoľnil potvrdením prepočtu."""
    if not _postgres():
        pytest.skip("poradné zámky – len PostgreSQL (VLAKY_TEST_DATABASE_URL)")
    from vlaky.web import prepocet

    tid = _train(fixed_now, "47506")
    sending = threading.Event()
    release = threading.Event()
    real_send = odosielanie._send_email

    def slow_send(recipient, subject, body):
        sending.set()
        release.wait(12)  # pomalý (ticho zahadzujúci) server správ
        return real_send(recipient, subject, body)

    monkeypatch.setattr(odosielanie, "_send_email", slow_send)
    web = threading.Thread(target=prepocet.evaluate_train_bg, args=(tid, "úprava vlaku (zadal dispecer)"))
    web.start()
    try:
        assert sending.wait(60), "web neposlal upozornenie"
        t0 = time.monotonic()
        with session_scope() as s:
            planovac.evaluate_train(s, tid, fixed_now + timedelta(minutes=1), "pravidelný prepočet")
        took = time.monotonic() - t0
    finally:
        release.set()
        web.join(60)
    assert took < 8, f"prepočet agenta čakal {took:.1f} s na odosielanie z webu"
