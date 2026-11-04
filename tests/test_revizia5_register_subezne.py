"""Piata revízia – súbežné potvrdenia udalostí z e-mailov (oblasť B, nález R5L4).

Dvaja dispečeri v tej istej chvíli (dve relácie, dve vlákna) potvrdia dve zmeny tej istej výluky alebo dvakrát tú
istú udalosť. Bez zámku obe potvrdenia vychádzajú zo starého stavu registra: novšia zmena termínu sa potichu stratí
(zapíše sa staršia) a tá istá udalosť sa potvrdí dvakrát. Zápisy idú po jednom (PostgreSQL poradný zámok, SQLite
zámok zápisu) – druhé potvrdenie vidí, čo zapísalo prvé.

Poradie v teste vynúti háčik: obe potvrdenia počkajú na seba (bariéra s limitom) a staršie zapíše ako posledné.
Testy bežia na SQLite aj na PostgreSQL (VLAKY_TEST_DATABASE_URL). Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

import re
import threading
import time

from sqlalchemy import select

from vlaky.db import session_scope
from vlaky.models import AuditLog, Closure, ExtractedEvent
from test_revizia3_terminy import as_utc, drop, events, raw_mail, utc
from test_revizia4_pravidla import ROV_KE, _process, env  # noqa: F401 – fixture env
from test_web_spolocne import HESLO, make_user, web_app  # noqa: F401 – fixture web_app

ORIG = ("Dobrý deň,\n\núplná výluka oboch traťových koľají v úseku Kysak – Košice dňa 12.11.2026 od 08:00 do 14:00 "
        "hod. (ROV č. 1142/2026).\n")


def _change(day: int) -> str:
    return (f"Dobrý deň,\n\nZmena ROV č. 1142/2026: úplná výluka v úseku Kysak – Košice sa presúva na {day}.11.2026 od "
            "08:00 do 14:00 hod.\n")


def _registered_with_two_changes(env, session) -> tuple[int, int]:
    """ROV 1142/2026 (12.11.) potvrdený; čakajú dve zmeny: staršia (14.11.) a novšia (16.11.)."""
    from vlaky.posta.spracovanie import confirm_event

    drop(env, "rov.eml", raw_mail("ROV c. 1142/2026 Kysak - Kosice", ORIG, "rov1142", sender=ROV_KE,
                                  date="Mon, 02 Nov 2026 09:00:00 +0100"))
    _process(session, utc(2026, 11, 2, 8, 2))
    (rov,) = events(session)
    confirm_event(session, rov.id, "dispecer1")
    session.commit()
    drop(env, "a.eml", raw_mail("Zmena ROV c. 1142/2026", _change(14), "zm1142a", sender=ROV_KE,
                                date="Thu, 05 Nov 2026 09:00:00 +0100"))
    drop(env, "b.eml", raw_mail("Zmena ROV c. 1142/2026", _change(16), "zm1142b", sender=ROV_KE,
                                date="Thu, 05 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 5, 9, 2))
    older, newer = [e for e in events(session) if e.kind == "vyluka_zmena"]
    assert older.id < newer.id and older.status == newer.status == "navrh"
    return older.id, newer.id


def test_r5l4_dve_zmeny_tej_istej_vyluky_naraz_novsia_sa_nestrati(env, session, monkeypatch):
    """R5L4: dispečer 1 potvrdí staršiu zmenu (14.11.), dispečer 2 v tej istej chvíli novšiu (16.11.). Register musí
    skončiť na novšom termíne ŽSR – buď sa novšia zapíše na staršiu (verzia 3), alebo sa staršia označí ako
    nahradená (verzia 2); nikdy nie staršia zmena s verziou 2 a novšia „potvrdená“ do prázdna."""
    from vlaky.posta import spracovanie

    older, newer = _registered_with_two_changes(env, session)
    barrier = threading.Barrier(2, timeout=3)
    real = spracovanie._closure_changes

    def hooked(c, fields):
        out = real(c, fields)
        name = threading.current_thread().name
        if name in ("starsia", "novsia"):
            try:
                barrier.wait()          # bez zámku tu obe potvrdenia majú prečítaný starý stav výluky
            except threading.BrokenBarrierError:
                pass
            if name == "starsia":
                time.sleep(0.5)         # staršia zmena by sa zapísala ako posledná
        return out

    monkeypatch.setattr(spracovanie, "_closure_changes", hooked)
    errors: list = []

    def run(ev_id: int) -> None:
        try:
            with session_scope() as s:
                spracovanie.confirm_event(s, ev_id, threading.current_thread().name)
        except Exception as exc:  # noqa: BLE001 – test ich vypíše
            errors.append(repr(exc))

    threads = [threading.Thread(target=run, args=(older,), name="starsia"),
               threading.Thread(target=run, args=(newer,), name="novsia")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    with session_scope() as s:
        rows = [c for c in s.scalars(select(Closure).order_by(Closure.id))
                if c.status != "zrusene" and c.proposed_change_of is None]
        assert [(as_utc(c.start_utc), c.source_event_id) for c in rows] == [(utc(2026, 11, 16, 7), newer)]
        a, b = s.get(ExtractedEvent, older), s.get(ExtractedEvent, newer)
        assert b.status == "aplikovane" and b.validation["apply_result"]["ok"] is True
        if a.validation["apply_result"]["action"] == "nahradene":
            assert rows[0].version == 2
        else:
            assert a.validation["apply_result"]["action"] == "aktualizovane" and rows[0].version == 3


def test_r5l4_ta_ista_udalost_potvrdena_dvakrat_naraz(env, session, web_app, monkeypatch):
    """R5L4: dvaja dispečeri potvrdia tú istú udalosť v tej istej chvíli (dva prehliadače). Zapíše sa raz: jeden dostane
    „Udalosť potvrdená“, druhý informáciu, že ju medzitým potvrdil kolega – nie dvakrát „Udalosť potvrdená“ a dva
    záznamy potvrdenia v audite."""
    from fastapi.testclient import TestClient

    from vlaky.posta import spracovanie

    _older, newer = _registered_with_two_changes(env, session)
    for name in ("disp1", "disp2"):
        make_user(name, "dispecer")
    clients = []
    for name in ("disp1", "disp2"):
        c = TestClient(web_app)
        c.__enter__()
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)
        assert c.post("/login", data={"username": name, "password": HESLO, "csrf_token": tok, "next": "/"},
                      follow_redirects=False).status_code == 303
        csrf = re.search(r'<meta name="csrf-token" content="([^"]*)"', c.get("/fronta").text).group(1)
        clients.append((c, csrf))
    barrier = threading.Barrier(2, timeout=3)
    real = spracovanie.validate_event
    calls = []

    def hooked(*args, **kw):
        calls.append(1)
        if len(calls) <= 2:
            try:
                barrier.wait()          # bez zámku tu obe potvrdenia prešli kontrolou „čaká na potvrdenie“
            except threading.BrokenBarrierError:
                pass
        return real(*args, **kw)

    monkeypatch.setattr(spracovanie, "validate_event", hooked)
    flashes: list = []

    def post(client_csrf) -> None:
        c, token = client_csrf
        page = c.post(f"/fronta/{newer}/potvrdit", data={"csrf_token": token, "akcia": "potvrdit"},
                      follow_redirects=True).text
        flashes.append(" ".join(re.findall(r'class="flash flash-[a-z]+"[^>]*>(.*?)</', page, re.S)))

    try:
        threads = [threading.Thread(target=post, args=(cc,)) for cc in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    finally:
        for c, _tok in clients:
            c.__exit__(None, None, None)
    assert len(flashes) == 2, flashes
    assert sum("Udalosť potvrdená" in f for f in flashes) == 1, flashes
    assert sum(f"Udalosť #{newer} už bola spracovaná – potvrdená a zapísaná do registra (disp" in f
               for f in flashes) == 1, flashes
    with session_scope() as s:
        audits = s.scalars(select(AuditLog).where(AuditLog.action == "udalost_potvrdena",
                                                  AuditLog.entity_id == str(newer))).all()
        assert len(audits) == 1, [(a.actor, a.ts) for a in audits]
