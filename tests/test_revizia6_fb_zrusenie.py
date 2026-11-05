"""6. revízia (strana FB – register): poistky pri zrušení výluky.

- Zrušenie bez cieľa (FC0 a podobné): zrušenie z e-mailu, ktoré v registri nemá čo zrušiť (ani čakajúcu výluku) alebo
  zodpovedá len výluke v inom termíne, môže byť nesprávne prečítaným oznámením novej výluky – je čakajúcou udalosťou
  poistky: s úsekom a začiatkom dostane záznam „navrh“ (vlaky v úseku a čase sú obmedzené ako pri neoverenej výluke
  z e-mailu), bez úseku poistku pre celý vozový park; dôvod „zrušenie z e-mailu nemá v registri čo zrušiť – môže ísť
  o novú výluku, overte vo fronte“, upozornenie DÔLEŽITÉ; nikdy sa nezapíše automaticky.
- Zrušenie len niektorých dní v týždni či hodín bez dátumov (FC3) a noci „na“ deň (FC5) sa nikdy nezapíše ako zrušenie
  celej výluky či inej noci; termín z časti vety, ktorá ostáva („…, ale až od 10:00 do 14:00“, „okrem 25.11.“), sa
  nezruší (FC4).

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from vlaky.models import Closure, ExtractedEvent, Notification
from test_revizia3_terminy import as_utc, drop, events, raw_mail, utc
from test_revizia4_pravidla import _decide, _process, env  # noqa: F401 – fixture env

ROV_ZA = "ZSR OR Zilina <vyluky.orza@zsr.example>"
ROV_KE = "ZSR OR Kosice <vyluky.orke@zsr.example>"
ROV_TT = "ZSR OR Trnava <vyluky.ortt@zsr.example>"
TZ = ZoneInfo("Europe/Bratislava")


def loc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=TZ).astimezone(timezone.utc)


def _confirm(session, ev_id: int, **edits) -> dict:
    from vlaky.posta.spracovanie import confirm_event

    ev = confirm_event(session, ev_id, "dispecer1", edits=edits or None)
    session.commit()
    return (ev.validation or {}).get("apply_result") or {}


def _reject(session, ev_id: int) -> None:
    from vlaky.posta.spracovanie import reject_event

    reject_event(session, ev_id, "dispecer1", reason="test")
    session.commit()


def _mails(env, session, name: str, subject: str, body: str, sender: str, date: str, now) -> list:
    before = {e.id for e in events(session)}
    drop(env, name, raw_mail(subject, body, name.replace(".", "-"), sender=sender, date=date))
    _process(session, now)
    return [e for e in events(session) if e.id not in before]


def _register(env, session, name: str, subject: str, body: str, sender: str, date: str, now) -> list:
    new = _mails(env, session, name, subject, body, sender, date, now)
    for e in new:
        _confirm(session, e.id)
    return new


def _alerts(session) -> list[str]:
    return [f"{s}\n{b}" for s, b in session.execute(select(Notification.subject, Notification.body)
                                                    .where(Notification.subject.contains("Na potvrdenie čaká")))]


def _green(r) -> bool:
    return r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"


def _active(session) -> list[Closure]:
    return [c for c in session.scalars(select(Closure).order_by(Closure.id))
            if c.status != "zrusene" and c.proposed_change_of is None]


# ====================================================================== zrušenie bez cieľa (poistka)
def test_zrusenie_bez_ciela_je_poistkou_pre_usek_a_cas(env, session):
    """Zrušenie „Výluka v úseku Kraľovany – Ružomberok dňa 24.11.2026 od 08:00 do 14:00 hod. sa ruší (ROV č.
    2799/2026)“, ktoré v registri nemá čo zrušiť: záznam „navrh“ poistky pre úsek a čas, vlak 24.11. o 10:00 v úseku
    nedostane VYPRAVIŤ ZELENÁ, dispečer dostane DÔLEŽITÉ upozornenie s dôvodom; Potvrdiť nič nezruší a poistka ostáva,
    Zamietnuť ju zruší (v 631f185 nič – vlak ZELENÝ)."""
    (ev,) = _mails(env, session, "z.eml", "Zrušenie výluky – ROV č. 2799/2026",
                   "Dobrý deň,\n\nvýluka v úseku Kraľovany – Ružomberok dňa 24.11.2026 od 08:00 do 14:00 hod. sa ruší "
                   "(ROV č. 2799/2026).\n\nOR Žilina\n", ROV_ZA, "Fri, 20 Nov 2026 09:00:00 +0100",
                   utc(2026, 11, 20, 8, 2))
    assert ev.kind == "vyluka_zrusenie" and ev.status == "navrh" and ev.needs_confirmation
    text = " | ".join(ev.validation.get("warnings") or []) + " | ".join(ev.validation.get("confirmation_reasons") or [])
    assert "nemá v registri čo zrušiť – môže ísť o novú výluku, overte vo fronte" in text, text
    [row] = [c for c in session.scalars(select(Closure)) if c.source_event_id == ev.id]
    assert row.status == "navrh" and row.kind == "uplna" and row.from_station == "Kraľovany", row
    assert (as_utc(row.start_utc), as_utc(row.end_utc)) == (loc(2026, 11, 24, 8), loc(2026, 11, 24, 14))
    alerts = _alerts(session)
    assert alerts and any("nemá v registri čo zrušiť" in a for a in alerts), alerts
    r = _decide(session, "76101", "Kraľovany", "Ružomberok", loc(2026, 11, 24, 10), loc(2026, 11, 24, 7))
    assert not _green(r), r.summary
    assert any("nemá v registri čo zrušiť" in x for x in r.reasons), r.reasons
    res = _confirm(session, ev.id)
    assert res.get("ok") is False and "nemá v registri čo zrušiť" in str(res.get("error")), res
    session.expire_all()
    assert session.get(Closure, row.id).status == "navrh"
    r = _decide(session, "76102", "Kraľovany", "Ružomberok", loc(2026, 11, 24, 10), loc(2026, 11, 24, 7))
    assert not _green(r), r.summary
    _reject(session, ev.id)
    session.expire_all()
    assert session.get(Closure, row.id).status == "zrusene"
    r = _decide(session, "76103", "Kraľovany", "Ružomberok", loc(2026, 11, 24, 10), loc(2026, 11, 24, 7))
    assert _green(r), r.summary


def test_zrusenie_bez_ciela_bez_konca_je_poistkou_do_odvolania(env, session):
    """Zrušenie bez cieľa s neurčeným koncom („… od 23.11.2026 08:00 hod. až do odvolania …“, ak ho pravidlá prečítajú
    ako zrušenie): poistka do odvolania (technický koniec) – vlak 27.11. nie je ZELENÝ."""
    from vlaky.posta.spracovanie import _write_orphan_pending

    (ev,) = _mails(env, session, "z.eml", "Zrušenie výluky – ROV č. 2795/2026",
                   "Dobrý deň,\n\nvýluka v úseku Kraľovany – Ružomberok dňa 24.11.2026 od 08:00 do 14:00 hod. sa ruší "
                   "(ROV č. 2795/2026).\n\nOR Žilina\n", ROV_ZA, "Fri, 20 Nov 2026 09:00:00 +0100",
                   utc(2026, 11, 20, 8, 2))
    ev.data = {**ev.data, "start": loc(2026, 11, 23, 8).isoformat(), "end": None}
    session.flush()
    row = _write_orphan_pending(session, ev)
    session.commit()
    assert row is not None and row.until_revoked and as_utc(row.end_utc) > loc(2026, 12, 15, 0), row
    r = _decide(session, "76104", "Kraľovany", "Ružomberok", loc(2026, 11, 27, 10), loc(2026, 11, 27, 7))
    assert not _green(r), r.summary


def test_fc0_do_odvolania_pred_vylukou_nedostane_zelenu(env, session):
    """FC0 (a): „… je od 23.11.2026 08:00 hod. až do odvolania úplná výluka trate v úseku Kraľovany – Ružomberok
    (ROV č. 2701/2026)“ – či už pravidlá oznámenie prečítajú ako novú výluku, alebo ako zrušenie (poistka zrušenia bez
    cieľa), vlak 24.11. o 10:00 v úseku nedostane VYPRAVIŤ ZELENÁ ani pred potvrdením, ani po Potvrdiť (v 631f185
    VYPRAVIŤ ZELENÁ)."""
    (ev,) = _mails(env, session, "n.eml", "ROV č. 2701/2026 Kraľovany – Ružomberok",
                   "Dobrý deň,\n\nz dôvodu poškodenia mosta je od 23.11.2026 08:00 hod. až do odvolania úplná výluka "
                   "trate v úseku Kraľovany – Ružomberok (ROV č. 2701/2026).\n\nOR Žilina\n", ROV_ZA,
                   "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    r = _decide(session, "76111", "Kraľovany", "Ružomberok", loc(2026, 11, 24, 10), loc(2026, 11, 24, 7))
    assert not _green(r), r.summary
    _confirm(session, ev.id)
    r = _decide(session, "76112", "Kraľovany", "Ružomberok", loc(2026, 11, 24, 10), loc(2026, 11, 24, 7))
    assert not _green(r), r.summary


@pytest.mark.parametrize("subject,body,sender,sec", [
    ("ROV č. 2703/2026 – zosuv svahu",
     "z dôvodu zosuvu svahu bude do odvolania výluka oboch traťových koľají v úseku Kraľovany – Ružomberok, začiatok "
     "23.11.2026 o 08:00 hod. ROV č. 2703/2026.", ROV_ZA, ("Kraľovany", "Ružomberok")),
    ("ROV č. 4707/2026 – Kysak – Košice",
     "trať v úseku Kysak – Košice je od 23.11.2026 08:00 hod. uzavretá, do odvolania úplná výluka oboch traťových "
     "koľají (ROV č. 4707/2026).", ROV_KE, ("Kysak", "Košice")),
], ids=["H03_bude_do_odvolania", "H07_uzavreta_do_odvolania"])
def test_fc0_do_odvolania_pred_vylukou_nie_je_jeden_den(env, session, subject, body, sender, sec):
    """FC0 H03, H07: oznámenie výluky do odvolania prečítané ako zrušenie jedného dňa (23.11.) bez cieľa – poistka
    zrušenia je do odvolania (e-mail hovorí „do odvolania“), nie len 23.11.: vlaky 24.11. a 27.11. nedostanú VYPRAVIŤ
    ZELENÁ ani po Potvrdiť (v 631f185 VYPRAVIŤ ZELENÁ)."""
    new = _mails(env, session, "n.eml", subject, f"Dobrý deň,\n\n{body}\n\nOR\n", sender,
                 "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    for day in (24, 27):
        r = _decide(session, f"7615{day}", *sec, loc(2026, 11, day, 10), loc(2026, 11, day, 7))
        assert not _green(r), r.summary
    for e in new:
        if e.status == "navrh":
            _confirm(session, e.id)
    for day in (24, 27):
        r = _decide(session, f"7616{day}", *sec, loc(2026, 11, day, 10), loc(2026, 11, day, 7))
        assert not _green(r), r.summary


def test_fc0_do_odvolania_nezrusi_vyluku_s_pevnym_koncom(env, session):
    """FC0 H08: výluka 23.11. 08:00 – 27.11. 18:00 (ROV č. 2708/2026); aktualizácia „od 23.11.2026 08:00 hod. do
    odvolania výluka 1. a 2. traťovej koľaje …, predpokladané ukončenie 27.11.2026 o 18:00 hod.“ prečítaná ako
    zrušenie – Potvrdiť výluku nezruší (správa pre dispečera), vlaky 24.11. a 26.11. nedostanú VYPRAVIŤ ZELENÁ
    (v 631f185 sa výluka zrušila a vlaky dostali VYPRAVIŤ ZELENÁ)."""
    _register(env, session, "r.eml", "ROV č. 2708/2026 – Kraľovany – Ružomberok",
              "Dobrý deň,\n\núplná výluka trate v úseku Kraľovany – Ružomberok od 23.11.2026 08:00 hod. do 27.11.2026 "
              "18:00 hod. (ROV č. 2708/2026).\n\nOR Žilina\n", ROV_ZA, "Fri, 20 Nov 2026 09:00:00 +0100",
              utc(2026, 11, 20, 8, 2))
    new = _mails(env, session, "a.eml", "ROV č. 2708/2026 – aktualizácia",
                 "Dobrý deň,\n\nod 23.11.2026 08:00 hod. do odvolania výluka 1. a 2. traťovej koľaje v úseku Kraľovany – "
                 "Ružomberok, predpokladané ukončenie 27.11.2026 o 18:00 hod. (ROV č. 2708/2026).\n\nOR Žilina\n",
                 ROV_ZA, "Sat, 21 Nov 2026 09:00:00 +0100", utc(2026, 11, 21, 8, 2))
    for e in new:
        if e.kind == "vyluka_zrusenie":
            warn = " | ".join(e.validation.get("warnings") or [])
            assert "do odvolania" in warn, warn
        if e.status == "navrh":
            res = _confirm(session, e.id)
            if e.kind == "vyluka_zrusenie":
                assert res.get("ok") is False and res.get("retry") is False, res
    assert [(as_utc(c.start_utc), as_utc(c.end_utc)) for c in _active(session) if c.source != "email_poistka"][:1] \
        == [(loc(2026, 11, 23, 8), loc(2026, 11, 27, 18))]
    for day in (24, 26):
        r = _decide(session, f"7617{day}", "Kraľovany", "Ružomberok", loc(2026, 11, day, 10), loc(2026, 11, day, 7))
        assert not _green(r), r.summary


def test_zrusenie_vyluky_do_odvolania_sa_zapise(env, session):
    """Kontrola: výluka vyhlásená do odvolania sa „do odvolania“ v e-maile zrušiť smie – Potvrdiť ju zruší."""
    _register(env, session, "r.eml", "ROV č. 2711/2026 – Kraľovany – Ružomberok",
              "Dobrý deň,\n\núplná výluka trate v úseku Kraľovany – Ružomberok od 23.11.2026 08:00 hod. do odvolania "
              "(ROV č. 2711/2026).\n\nOR Žilina\n", ROV_ZA, "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    (c,) = _active(session)
    assert c.until_revoked, c
    (ev,) = _mails(env, session, "z.eml", "Zrušenie výluky – ROV č. 2711/2026",
                   "Dobrý deň,\n\núplná výluka trate v úseku Kraľovany – Ružomberok vyhlásená do odvolania podľa ROV č. "
                   "2711/2026 sa ruší.\n\nOR Žilina\n", ROV_ZA, "Mon, 23 Nov 2026 12:00:00 +0100",
                   utc(2026, 11, 23, 11, 2))
    assert ev.kind == "vyluka_zrusenie", ev.kind
    res = _confirm(session, ev.id)
    assert res.get("ok") is True, res
    assert not _active(session)


def test_zrusenie_bez_useku_a_ciela_je_poistkou_pre_cely_vozovy_park(env, session):
    """„STORNO – ROV č. 5555/2026“ bez úseku, výluka s týmto číslom v registri nie je: poistka pre celý vozový park
    (vlak kdekoľvek nie je ZELENÝ) a jedno upozornenie DÔLEŽITÉ; po zamietnutí je vlak ZELENÝ."""
    (ev,) = _mails(env, session, "s.eml", "STORNO - ROV č. 5555/2026",
                   "Dobrý deň,\n\nvýluku v predmete stornujeme.\n\nOR Košice\n", ROV_KE,
                   "Thu, 26 Nov 2026 09:40:00 +0100", utc(2026, 11, 26, 8, 42))
    assert ev.kind == "vyluka_zrusenie" and not ev.data.get("from_station"), ev.data
    assert ev.validation.get("zrusenie_bez_ciela"), ev.validation
    assert not [c for c in session.scalars(select(Closure)) if c.source_event_id == ev.id]
    assert _alerts(session)
    r = _decide(session, "76121", "Senec", "Galanta", loc(2026, 12, 4, 10), loc(2026, 12, 4, 7))
    assert not _green(r), r.summary
    assert any("nemá v registri čo zrušiť" in x for x in r.reasons), r.reasons
    _reject(session, ev.id)
    r = _decide(session, "76122", "Senec", "Galanta", loc(2026, 12, 4, 10), loc(2026, 12, 4, 7))
    assert _green(r), r.summary


def test_zrusenie_v_inom_termine_je_poistkou(env, session):
    """Výluka Trnava – Leopoldov 10.12.; zrušenie „dňa 15.12.2026“ toho istého úseku a čísla sa s ňou neprekrýva – môže
    ísť o nový termín: poistka pre 15.12. (vlak 15.12. o 10:00 nie je ZELENÝ), výluka 10.12. ostáva."""
    _register(env, session, "r.eml", "ROV c. 3113/2026 Trnava - Leopoldov",
              "Dobrý deň,\n\núplná výluka v úseku Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 14:00 hod. "
              "(ROV č. 3113/2026).\n", ROV_TT, "Fri, 27 Nov 2026 09:00:00 +0100", utc(2026, 11, 27, 8, 2))
    (ev,) = _mails(env, session, "z.eml", "ROV c. 3113/2026",
                   "Dobrý deň,\n\nvýluka v úseku Trnava – Leopoldov dňa 15.12.2026 od 08:00 do 14:00 hod. sa ruší "
                   "(ROV č. 3113/2026).\n", ROV_TT, "Tue, 01 Dec 2026 09:00:00 +0100", utc(2026, 12, 1, 8, 2))
    assert ev.kind == "vyluka_zrusenie" and ev.validation.get("zrusenie_bez_ciela"), ev.validation
    r = _decide(session, "76131", "Trnava", "Leopoldov", loc(2026, 12, 15, 10), loc(2026, 12, 15, 7))
    assert not _green(r), r.summary
    res = _confirm(session, ev.id)
    assert res.get("ok") is False, res
    assert [as_utc(c.start_utc) for c in _active(session) if c.source != "email_poistka"] == [loc(2026, 12, 10, 8)]


def test_poistka_nie_je_cielom_inej_udalosti(env, session):
    """Dve rovnaké zrušenia bez cieľa (napr. znova poslaný e-mail): druhé nezruší záznam poistky prvého – obe ostávajú
    poistkami; nová výluka v tom istom úseku a čase sa zapíše ako samostatná výluka."""
    body = ("Dobrý deň,\n\nvýluka v úseku Kraľovany – Ružomberok dňa 24.11.2026 od 08:00 do 14:00 hod. sa ruší "
            "(ROV č. 2798/2026).\n\nOR Žilina\n")
    (a,) = _mails(env, session, "a.eml", "Zrušenie výluky", body, ROV_ZA, "Fri, 20 Nov 2026 09:00:00 +0100",
                  utc(2026, 11, 20, 8, 2))
    (b,) = _mails(env, session, "b.eml", "Zrušenie výluky (opakované)", body, ROV_ZA,
                  "Fri, 20 Nov 2026 09:10:00 +0100", utc(2026, 11, 20, 8, 12))
    assert a.validation.get("zrusenie_bez_ciela") and b.validation.get("zrusenie_bez_ciela"), b.validation
    nets = [c for c in session.scalars(select(Closure)) if c.source == "email_poistka"]
    assert sorted(c.source_event_id for c in nets) == [a.id, b.id]
    (n,) = _mails(env, session, "n.eml", "ROV č. 2798/2026 Kraľovany – Ružomberok",
                  "Dobrý deň,\n\núplná výluka v úseku Kraľovany – Ružomberok dňa 24.11.2026 od 08:00 do 14:00 hod. "
                  "(ROV č. 2798/2026).\n\nOR Žilina\n", ROV_ZA, "Fri, 20 Nov 2026 10:00:00 +0100",
                  utc(2026, 11, 20, 9, 2))
    _confirm(session, n.id)
    session.expire_all()
    assert all(session.get(Closure, c.id).source == "email_poistka" for c in nets)
    assert [c.source for c in _active(session) if c.source_event_id == n.id] == ["email"]


def test_zrusenie_bez_ciela_preklasifikovane_na_novu_vyluku(env, session):
    """Dispečer zmení druh zrušenia bez cieľa na novú výluku (Upraviť a potvrdiť): z poistky je overená výluka."""
    (ev,) = _mails(env, session, "z.eml", "ROV č. 2797/2026",
                   "Dobrý deň,\n\nvýluka v úseku Kraľovany – Ružomberok dňa 24.11.2026 od 08:00 do 14:00 hod. sa ruší "
                   "(ROV č. 2797/2026).\n\nOR Žilina\n", ROV_ZA, "Fri, 20 Nov 2026 09:00:00 +0100",
                   utc(2026, 11, 20, 8, 2))
    res = _confirm(session, ev.id, event_kind="vyluka_nova")
    assert res.get("ok") is True, res
    [c] = _active(session)
    assert (c.status, c.source, c.kind, c.from_station) == ("overene", "email", "uplna", "Kraľovany"), c


def test_skutocne_zrusenie_bez_poistky(env, session):
    """Kontrola: zrušenie výluky, ktorá v registri je, poistku nemá (zruší sa po potvrdení)."""
    _register(env, session, "r.eml", "ROV c. 2796/2026 Kralovany - Ruzomberok",
              "Dobrý deň,\n\núplná výluka v úseku Kraľovany – Ružomberok dňa 24.11.2026 od 08:00 do 14:00 hod. "
              "(ROV č. 2796/2026).\n", ROV_ZA, "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    (ev,) = _mails(env, session, "z.eml", "Zrušenie výluky – ROV č. 2796/2026",
                   "Dobrý deň,\n\nvýluka v úseku Kraľovany – Ružomberok dňa 24.11.2026 sa ruší (ROV č. 2796/2026).\n",
                   ROV_ZA, "Sat, 21 Nov 2026 09:00:00 +0100", utc(2026, 11, 21, 8, 2))
    assert ev.kind == "vyluka_zrusenie" and not ev.validation.get("zrusenie_bez_ciela"), ev.validation
    assert not [c for c in session.scalars(select(Closure)) if c.source == "email_poistka"]
    assert _confirm(session, ev.id).get("ok") is True
    assert not _active(session)


# ====================================================================== zrušenie len niektorých dní či hodín (FC3)
_ROV2501 = ("Dobrý deň,\n\núplná výluka v úseku Ružomberok – Liptovský Mikuláš v dňoch 23.11. – 29.11.2026 denne od "
            "08:00 do 14:00 hod. (ROV č. 2501/2026).\n\nOR Žilina\n")


@pytest.mark.parametrize("subject,body", [
    ("ROV č. 2501/2026", "výluka podľa ROV č. 2501/2026 v úseku Ružomberok – Liptovský Mikuláš sa v sobotu a v nedeľu "
                         "nevykoná. Od pondelka do piatku bez zmeny."),
    ("ROV č. 2501/2026", "výluka podľa ROV č. 2501/2026 v úseku Ružomberok – Liptovský Mikuláš sa cez víkend nevykoná, "
                         "v pracovné dni sa vykoná podľa ROV."),
    ("Zrušenie výluky v piatok – ROV č. 2501/2026", "výluka v úseku Ružomberok – Liptovský Mikuláš sa v piatok ruší, "
                                                    "od pondelka do štvrtka platí."),
    ("ROV č. 2501/2026", "vo všetkých dňoch výluky Ružomberok – Liptovský Mikuláš (ROV č. 2501/2026) sa ruší čas od "
                         "12:00 do 14:00 hod.; výluka bude denne len od 08:00 do 12:00 hod."),
    ("Zrušenie výluky – ROV č. 2501/2026", "výluka Ružomberok – Liptovský Mikuláš v dňoch 23.11. – 29.11.2026 sa "
                                           "v stredu ruší."),
], ids=["sobota_nedela", "vikend", "piatok", "cas_bez_dna", "streda_s_obdobim"])
def test_fc3_zrusenie_len_niektorych_dni_nezrusi_celu_vyluku(env, session, subject, body):
    """FC3: denná výluka Po 23.11. – Ne 29.11.; zrušenie len niektorých dní v týždni či hodín bez dátumov sa nikdy
    nezapíše ako zrušenie celej výluky – vlak v utorok 24.11. o 10:00 ostáva zdržaný (v 631f185 sa po Potvrdiť zrušila
    celá výluka a vlak dostal VYPRAVIŤ ZELENÁ)."""
    _register(env, session, "r.eml", "ROV c. 2501/2026 Ruzomberok - Liptovsky Mikulas", _ROV2501, ROV_ZA,
              "Mon, 16 Nov 2026 09:00:00 +0100", utc(2026, 11, 16, 8, 2))
    new = _mails(env, session, "z.eml", subject, "Dobrý deň,\n\n" + body + "\n\nOR Žilina\n", ROV_ZA,
                 "Thu, 19 Nov 2026 09:00:00 +0100", utc(2026, 11, 19, 8, 2))
    for e in new:
        if e.status == "navrh":
            _confirm(session, e.id)
    r = _decide(session, "76201", "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 24, 10), loc(2026, 11, 24, 7))
    assert not _green(r), r.summary


def _closure(daily: bool = True, **kw) -> Closure:
    base = dict(id=7, kind="uplna", from_station="Ružomberok", to_station="Liptovský Mikuláš",
                start_utc=loc(2026, 11, 23, 8), end_utc=loc(2026, 11, 29, 14), daily_from="08:00" if daily else "",
                daily_to="14:00" if daily else "", status="overene", external_ref="ROV č. 2501/2026")
    base.update(kw)
    return Closure(**base)


def _mail(body: str, subject: str = "ROV č. 2501/2026") -> SimpleNamespace:
    return SimpleNamespace(subject=subject, body_text=body, attachments=[])


@pytest.mark.parametrize("body,refused", [
    ("Výluka sa v sobotu a v nedeľu nevykoná.", True),
    ("Výluka sa cez víkend nevykoná, v pracovné dni sa vykoná podľa ROV.", True),
    ("Výluka sa v noci z piatka na sobotu ruší, ostatné noci platia.", True),
    ("Výluka podľa ROV sa ruší čas od 12:00 do 14:00 hod.", True),
    ("Výluka sa ruší počas sviatkov.", True),
    ("Výluka podľa ROV č. 2501/2026 sa ruší.", False),                         # celá výluka
    ("Výluka od pondelka do nedele sa ruší.", False),                         # všetky dni
    ("Výluka denne od 08:00 do 14:00 hod. sa ruší.", False),                  # celé denné okno
    ("Výluka sa ruší. Náhradná doprava bude v sobotu.", False),               # deň v inej vete
    ("Výluka v úseku Dunajská Streda – Komárno sa ruší.", False),             # názov dopravne
    ("Výluka sa v stredu neruší.", False),                                    # zápor zrušenia
], ids=["so_ne", "vikend_pracovne", "noc_pi_so", "cas", "sviatky", "cela", "vsetky_dni", "cele_okno", "ina_veta",
        "streda_stanica", "nerusi"])
def test_fc3_obmedzenie_zrusenia_bez_datumov(body, refused):
    """Zrušenie celej výluky (bez termínu) s dňami v týždni, víkendom, sviatkami či časom bez dátumu sa odmietne;
    zrušenie celej výluky, všetkých dní, celého denného okna či deň len v inej vete áno."""
    from vlaky.posta.spracovanie import _cancel_restriction

    out = _cancel_restriction(_mail(body), _closure(), {"start": None, "end": None})
    assert bool(out) is refused, out
    if refused:
        assert "Celá výluka sa nezruší" in out, out


def test_fc3_obdobie_ktore_len_opisuje_vyluku_a_den_v_tyzdni():
    """G08: termín zrušenia je obdobie, ktoré výluku len opisuje (23.11. – 29.11.), e-mail ruší len stredu – odmietne
    sa; s dátumom („v stredu 25.11.“) je termín len ten deň (rieši rozdelenie, nie táto poistka)."""
    from vlaky.posta.spracovanie import _cancel_restriction

    d = {"start": loc(2026, 11, 23, 0).isoformat(), "end": loc(2026, 11, 30, 0).isoformat()}
    assert _cancel_restriction(_mail("Výluka v dňoch 23.11. – 29.11.2026 sa v stredu ruší."), _closure(), d)
    assert not _cancel_restriction(_mail("Výluka sa v sobotu 28.11.2026 ruší."),
                                   _closure(start_utc=loc(2026, 11, 28, 8), end_utc=loc(2026, 11, 28, 14)),
                                   {"start": loc(2026, 11, 28, 0).isoformat(), "end": loc(2026, 11, 29, 0).isoformat()})


# ====================================================================== noc „na“ deň (FC5)
_NIGHTS = dict(id=8, kind="uplna", from_station="Kysak", to_station="Košice", start_utc=loc(2026, 11, 23, 22),
               end_utc=loc(2026, 11, 29, 4), daily_from="22:00", daily_to="04:00", status="overene",
               external_ref="ROV č. 4503/2026")


@pytest.mark.parametrize("body,day,refused", [
    ("V noci na 26.11.2026 sa výluka Kysak – Košice nekoná.", 26, True),
    ("Výluka sa v noci na piatok 27.11.2026 nekoná. Ostatné noci bez zmeny.", 27, True),
    ("Výluka sa v noci z 25.11. na 26.11.2026 nekoná.", 25, False),
    ("V noci na 26.11.2026 sa výluka Kysak – Košice nekoná.", 25, False),       # termín už je noc 25./26.
], ids=["noc_na_den", "noc_na_piatok", "noc_z_na", "termin_noci_zaciatku"])
def test_fc5_noc_na_den_nezrusi_nasledujucu_noc(body, day, refused):
    """FC5: „v noci na 26.11.“ je noc, ktorá 26.11. končí (25./26.); termín zrušenia celý deň 26.11. by zrušil noc
    26./27. – zrušenie sa odmietne (opraví ho dispečer). Termín noci, ktorá začína 25.11., je v poriadku."""
    from vlaky.posta.spracovanie import _cancel_restriction

    d = {"start": loc(2026, 11, day, 0).isoformat(), "end": loc(2026, 11, day + 1, 0).isoformat()}
    out = _cancel_restriction(_mail(body, "ROV č. 4503/2026"), Closure(**_NIGHTS), d, whole=False)
    assert bool(out) is refused, out


def test_fc5_end_to_end_noc_na_den(env, session):
    """F03 cez skutočný reťazec: nočná výluka Kysak – Košice 23./24. – 28./29.11.; „v noci na 26.11.2026 sa výluka …
    nekoná“ – noc 26./27. ostáva uzavretá (vlak 26.11. o 23:00 nie je ZELENÝ; v 631f185 Potvrdiť zrušil noc 26./27.)."""
    _register(env, session, "r.eml", "ROV c. 4503/2026 Kysak - Kosice",
              "Dobrý deň,\n\nnočná výluka v úseku Kysak – Košice v nociach 23./24.11. – 28./29.11.2026 vždy od 22:00 "
              "do 04:00 hod. (ROV č. 4503/2026).\n\nOR Košice\n", ROV_KE, "Mon, 16 Nov 2026 09:00:00 +0100",
              utc(2026, 11, 16, 8, 2))
    new = _mails(env, session, "z.eml", "ROV č. 4503/2026",
                 "Dobrý deň,\n\nv noci na 26.11.2026 sa výluka Kysak – Košice podľa ROV č. 4503/2026 nekoná.\n\n"
                 "OR Košice\n", ROV_KE, "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    for e in new:
        if e.status == "navrh":
            _confirm(session, e.id)
    r = _decide(session, "76301", "Kysak", "Košice", loc(2026, 11, 26, 23), loc(2026, 11, 26, 20))
    assert not _green(r), r.summary


# ====================================================================== termín z časti vety, ktorá ostáva (FC4)
@pytest.mark.parametrize("body,term,refused", [
    ("výluka Trnava – Leopoldov dňa 10.12.2026 sa nekoná v čase od 08:00 do 10:00 hod., ale až od 10:00 do 14:00 hod. "
     "(ROV č. 3206/2026).", (10, 14), True),
    ("výluka Trnava – Leopoldov dňa 10.12.2026 v čase od 08:00 do 10:00 hod. sa ruší, ale od 10:00 do 14:00 hod. "
     "bude. ROV č. 3206/2026.", (10, 14), True),
    ("výluka Trnava – Leopoldov dňa 10.12.2026 sa nekoná v čase od 08:00 do 10:00 hod., ale až od 10:00 do 14:00 hod. "
     "(ROV č. 3206/2026).", (8, 10), False),
], ids=["ale_az", "ale_bude", "spravny_termin"])
def test_fc4_cas_za_ale_ostava(body, term, refused):
    """FC4: časť vety za „ale (až)“ s časom za zrušenou časťou ostáva – zrušenie jej času (10:00 – 14:00) sa nezapíše;
    zrušenie 08:00 – 10:00 áno."""
    from vlaky.posta.spracovanie import _stays_text

    c = Closure(id=9, kind="uplna", from_station="Trnava", to_station="Leopoldov", start_utc=loc(2026, 12, 10, 8),
                end_utc=loc(2026, 12, 10, 14), status="overene", external_ref="ROV č. 3206/2026")
    d = {"start": loc(2026, 12, 10, term[0]).isoformat(), "end": loc(2026, 12, 10, term[1]).isoformat()}
    out = _stays_text(_mail(body, "ROV č. 3206/2026"), c, d)
    assert bool(out) is refused, out


def test_fc4_den_okrem_ostava():
    """Y07: „v dňoch 23.11. až 27.11.2026 sa ruší okrem 25.11.2026“ – zrušenie s termínom 25.11. (deň, ktorý ostáva) sa
    nezapíše; zrušenie 24.11. áno."""
    from vlaky.posta.spracovanie import _stays_text

    c = _closure(start_utc=loc(2026, 11, 23, 8), end_utc=loc(2026, 11, 27, 14))
    body = ("výluka v úseku Ružomberok – Liptovský Mikuláš v dňoch 23.11. až 27.11.2026 sa ruší okrem 25.11.2026, kedy "
            "sa vykoná v pôvodnom čase.")
    bad = {"start": loc(2026, 11, 25, 0).isoformat(), "end": loc(2026, 11, 26, 0).isoformat()}
    ok = {"start": loc(2026, 11, 24, 0).isoformat(), "end": loc(2026, 11, 25, 0).isoformat()}
    assert _stays_text(_mail(body), c, bad)
    assert not _stays_text(_mail(body), c, ok)


def test_fc4_end_to_end_ale_az(env, session):
    """B06 cez skutočný reťazec: po Potvrdiť nesmie vlak 10.12. o 11:00 (v čase, ktorý sa koná) dostať VYPRAVIŤ ZELENÁ."""
    _register(env, session, "r.eml", "ROV c. 3206/2026 Trnava - Leopoldov",
              "Dobrý deň,\n\núplná výluka v úseku Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 14:00 hod. "
              "(ROV č. 3206/2026).\n", ROV_TT, "Fri, 27 Nov 2026 09:00:00 +0100", utc(2026, 11, 27, 8, 2))
    new = _mails(env, session, "z.eml", "ROV č. 3206/2026",
                 "Dobrý deň,\n\nvýluka Trnava – Leopoldov dňa 10.12.2026 sa nekoná v čase od 08:00 do 10:00 hod., ale "
                 "až od 10:00 do 14:00 hod. (ROV č. 3206/2026).\n", ROV_TT, "Tue, 01 Dec 2026 09:00:00 +0100",
                 utc(2026, 12, 1, 8, 2))
    for e in new:
        if e.status == "navrh":
            _confirm(session, e.id)
    r = _decide(session, "76401", "Trnava", "Leopoldov", loc(2026, 12, 10, 11), loc(2026, 12, 10, 7))
    assert not _green(r), r.summary
