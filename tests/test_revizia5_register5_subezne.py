"""Piata revízia – oprava oblasti B po kontrole: agent spracúva e-mail so zmenou výluky v tej istej chvíli, keď
dispečer potvrdí inú zmenu tej istej výluky, ktorá výluku rozdelí.

Bez zámku pri spracovaní e-mailu si agent prečíta register (výluka #1 23. – 27.11. je ešte celá), zapíše dočasný
záznam zmeny B s odkazom na #1 a potvrdenie zmeny A (24.11.) medzitým výluku rozdelí – #1 je už len 23.11. Potvrdenie
B (26.11.) potom presunie práve deň 23.11. a ten sa stratí (vlak 23.11. o 10:00 dostane VYPRAVIŤ ZELENÁ). Priradenie
k výluke pri spracovaní e-mailu ide po jednom s potvrdeniami (PostgreSQL poradný zámok, SQLite zámok zápisu).

Test beží na SQLite aj na PostgreSQL (VLAKY_TEST_DATABASE_URL); na PostgreSQL bez zámku zlyhá. Adresy sú fiktívne.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from vlaky.cas import set_fake_now
from vlaky.db import session_scope
from vlaky.models import Closure
from test_revizia3_terminy import drop, events, raw_mail, utc
from test_revizia4_pravidla import ROV_ZA, _decide, _process, env  # noqa: F401 – fixture env

TZ = ZoneInfo("Europe/Bratislava")
_ZMENA = ("Dobrý deň,\n\nZmena ROV č. 1315/2026: výluka v úseku Ružomberok – Liptovský Mikuláš dňa {day}.11.2026 "
          "od {od} do {do} hod.\n")


def loc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=TZ).astimezone(timezone.utc)


def _closed_at(session, when: datetime) -> bool:
    from vlaky.planovanie.planovac import closure_windows

    return any(w.start <= when < w.end for w in closure_windows(session, when - timedelta(hours=1),
                                                                 when + timedelta(hours=1)))


def test_r5r_zmena_spracovana_pocas_rozdelenia_vyluky_nestrati_den(env, session, monkeypatch):
    """Zmena A (24.11. 08:00 – 18:00) čaká vo fronte; agent spracúva e-mail so zmenou B (26.11. 06:00 – 14:00) a
    dispečer práve vtedy potvrdí A. Žiadny deň výluky 23. – 27.11. sa nestratí ani po potvrdení B."""
    from vlaky.posta import spracovanie

    c = Closure(kind="uplna", from_station="Ružomberok", to_station="Liptovský Mikuláš", start_utc=loc(2026, 11, 23, 8),
                end_utc=loc(2026, 11, 27, 14), daily_from="08:00", daily_to="14:00", status="overene", source="email",
                external_ref="1315/2026")
    session.add(c)
    session.commit()
    drop(env, "a.eml", raw_mail("Zmena ROV c. 1315/2026", _ZMENA.format(day=24, od="08:00", do="18:00"), "zmA",
                                sender=ROV_ZA, date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 2))
    (ev_a,) = events(session)
    assert ev_a.status == "navrh"
    drop(env, "b.eml", raw_mail("Zmena ROV c. 1315/2026", _ZMENA.format(day=26, od="06:00", do="14:00"), "zmB",
                                sender=ROV_ZA, date="Mon, 16 Nov 2026 10:20:00 +0100"))
    written, confirmed = threading.Event(), threading.Event()
    real = spracovanie._write_pending

    def hooked(s, ev):
        row = real(s, ev)
        if threading.current_thread().name == "agent" and ev.kind == "vyluka_zmena":
            written.set()            # zmena B prečítala register (#1 je celá) a zapísala dočasný záznam …
            confirmed.wait(2)        # … potvrdenie A by bez zámku prebehlo práve teraz
        return row

    monkeypatch.setattr(spracovanie, "_write_pending", hooked)
    errors: list = []

    def agent() -> None:
        try:
            set_fake_now(utc(2026, 11, 16, 9, 22))
            with session_scope() as s:
                spracovanie.process_mailbox(s, now=utc(2026, 11, 16, 9, 22))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def dispatcher() -> None:
        try:
            written.wait(10)
            with session_scope() as s:
                spracovanie.confirm_event(s, ev_a.id, "dispecer1", pending_only=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
        finally:
            confirmed.set()

    threads = [threading.Thread(target=agent, name="agent"), threading.Thread(target=dispatcher, name="dispecer")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    monkeypatch.setattr(spracovanie, "_write_pending", real)
    session.expire_all()
    ev_b = next(e for e in events(session) if e.id != ev_a.id and e.kind == "vyluka_zmena")
    res = spracovanie.confirm_event(session, ev_b.id, "dispecer2").validation["apply_result"]
    session.commit()
    assert res["ok"] is True, res
    active = [x for x in session.scalars(select(Closure)) if x.status != "zrusene" and x.proposed_change_of is None]
    lost = [d for d in (23, 24, 25, 26, 27) if not _closed_at(session, loc(2026, 11, d, 10))]
    assert not lost, (lost, [(x.id, x.start_utc, x.end_utc, x.daily_from) for x in active])
    assert _closed_at(session, loc(2026, 11, 26, 6, 30)) and _closed_at(session, loc(2026, 11, 24, 17))
    r = _decide(session, "77623", "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 23, 10), loc(2026, 11, 23, 5))
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), r.summary
