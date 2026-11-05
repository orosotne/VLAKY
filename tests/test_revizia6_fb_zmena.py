"""6. revízia (strana FB – register): zámer zmeny výluky a nemenný čas výluky v registri (FC2, FC6, FC7).

Zápis zmeny výluky (vyluka_zmena) nikdy neodstráni čas výluky v registri, okrem výslovného presunu, skrátenia či zmeny
času (``data["zamer"]`` = presun, skratenie, zmena_casu – určujú pravidlá) s celým novým termínom: doplnenie pridá
termín (pôvodná výluka ostáva), predĺženie len posunie koniec, neznámy zámer sa zapíše prídavne s poznámkou vo fronte.
Bez zámeru (staršie udalosti) doterajšie správanie, ale zmena do odvolania od konca výluky ju predĺži (nepresunie) a
viacdenná výluka sa neskráti na menej dní, ako e-mail vymenúva. Zmena výluky koľaje, ktorá koľaj neuvádza, ponechá
koľaj pôvodnej výluky (inak by sa výluky oboch koľají nespojili do úplnej výluky).

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from vlaky.models import Closure, ExtractedEvent
from test_revizia3_terminy import as_utc, drop, events, raw_mail, utc
from test_revizia4_pravidla import _decide, _process, env  # noqa: F401 – fixture env

ROV_ZA = "ZSR OR Zilina <vyluky.orza@zsr.example>"
ROV_KE = "ZSR OR Kosice <vyluky.orke@zsr.example>"
TZ = ZoneInfo("Europe/Bratislava")


def loc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=TZ).astimezone(timezone.utc)


def _closure(**kw) -> Closure:
    base = dict(id=11, kind="uplna", from_station="Ružomberok", to_station="Liptovský Mikuláš",
                start_utc=loc(2026, 11, 23, 8), end_utc=loc(2026, 11, 27, 14), daily_from="08:00", daily_to="14:00",
                status="overene", external_ref="ROV č. 2331/2026", track="", line_ref="", electric_only=False,
                speed_kmh=None, until_revoked=False)
    base.update(kw)
    return Closure(**base)


def _fields(start, end, daily=("08:00", "14:00"), **kw) -> dict:
    out = {"kind": "uplna", "from_station": "Ružomberok", "to_station": "Liptovský Mikuláš", "line_ref": "",
           "track": "", "start_utc": start, "end_utc": end, "daily_from": daily[0], "daily_to": daily[1],
           "electric_only": False, "speed_kmh": None, "external_ref": "ROV č. 2331/2026", "until_revoked": False}
    out.update(kw)
    return out


@pytest.mark.parametrize("zamer,term,expect", [
    ("doplnenie", ((2026, 11, 30, 8), (2026, 12, 4, 14)), "pridat"),           # D21 – pôvodné dni ostávajú
    ("doplnenie", ((2026, 11, 24, 8), (2026, 11, 24, 14)), "bez_zmeny"),
    ("predlzenie", ((2026, 11, 29, 8), (2026, 11, 29, 14)), "predlzit"),       # G09 – koniec sa posunie
    ("predlzenie", ((2026, 11, 24, 8), (2026, 11, 25, 14)), "bez_zmeny"),      # neskracuje
    ("predlzenie", ((2026, 11, 20, 8), (2026, 11, 25, 14)), "pridat"),         # skorší začiatok – pridá, neskráti
    ("niečo_iné", ((2026, 11, 30, 8), (2026, 12, 4, 14)), "pridat"),           # neznámy zámer – prídavne
    ("presun", ((2026, 11, 30, 8), (2026, 12, 4, 14)), None),                  # výslovný presun – nahradí
    ("zmena_casu", ((2026, 11, 23, 6), (2026, 11, 27, 16)), None),             # zmena času všetkých dní – nahradí
], ids=["doplnenie", "doplnenie_obsiahnute", "predlzenie", "predlzenie_nie_kratsie", "predlzenie_skor",
        "nezname", "presun", "zmena_casu"])
def test_zamer_zmeny(session, zamer, term, expect):
    from vlaky.posta.spracovanie import _change_plan

    c = _closure()
    daily = ("06:00", "16:00") if zamer == "zmena_casu" else ("08:00", "14:00")
    plan = _change_plan(session, c, _fields(loc(*term[0]), loc(*term[1]), daily), None, None, zamer=zamer)
    assert (plan.kind if plan else None) == expect, plan and plan.text


def test_predlzenie_s_inym_casom_zapise_aj_novy_termin(session):
    """Predĺženie „do 29.11., denne od 06:00 do 16:00“: koniec výluky sa posunie a nový termín (iné denné okno) sa
    zapíše aj ako ďalšia výluka – nič sa neodstráni."""
    from vlaky.posta.spracovanie import _change_plan

    plan = _change_plan(session, _closure(), _fields(loc(2026, 11, 28, 6), loc(2026, 11, 29, 16), ("06:00", "16:00")),
                        None, None, zamer="predlzenie")
    assert plan.kind == "predlzit" and plan.parts == [True], plan


def test_presun_bez_celeho_terminu_je_pridanie(session):
    """Presun do odvolania (bez konca) nie je celý nový termín – pôvodný termín ostáva, nový sa pridá."""
    from vlaky.posta.spracovanie import _change_plan

    plan = _change_plan(session, _closure(), _fields(loc(2026, 11, 30, 8), loc(2026, 12, 30, 8), ("", ""),
                                                     until_revoked=True), None, None, zamer="presun")
    assert plan.kind == "pridat", plan


def test_bez_zameru_zmena_do_odvolania_od_konca_vyluky_je_predlzenie(session):
    """FC6 H09 (bez zámeru): „ROV … sa mení: od 27.11.2026 18:00 hod. do odvolania“ pri výluke 23.11. 08:00 –
    27.11. 18:00 – pokračovanie (predĺženie), nie presun: pôvodné dni ostávajú."""
    from vlaky.posta.spracovanie import _change_plan

    c = _closure(start_utc=loc(2026, 11, 23, 8), end_utc=loc(2026, 11, 27, 18), daily_from="", daily_to="")
    plan = _change_plan(session, c, _fields(loc(2026, 11, 27, 18), loc(2026, 12, 27, 18), ("", ""),
                                            until_revoked=True), None, None, zamer=None)
    assert plan is not None and plan.kind == "predlzit", plan


def test_bez_zameru_sa_viacdenna_vyluka_neskrati_na_menej_dni_ako_email_vymenuva(session):
    """Staršia udalosť bez zámeru: e-mail „výluka sa skracuje, bude 23.11., 24.11. a 26.11.2026“, nový termín (zle
    prečítaný) len 23. – 24.11. – výluka sa neskráti (pridá sa nový termín); s výslovným skrátením áno."""
    from vlaky.posta.spracovanie import _change_plan

    mail = SimpleNamespace(subject="Zmena ROV č. 2331/2026", body_text="Výluka podľa ROV č. 2331/2026 sa skracuje, "
                           "bude 23.11., 24.11. a 26.11.2026 denne od 08:00 do 14:00 hod.", attachments=[])
    new = _fields(loc(2026, 11, 23, 8), loc(2026, 11, 24, 14))
    plan = _change_plan(session, _closure(), new, None, mail, zamer=None)
    assert plan is not None and plan.kind == "pridat", plan
    assert _change_plan(session, _closure(), new, None, mail, zamer="skratenie") is None


# ====================================================================== cez skutočný reťazec
def _confirm(session, ev_id: int, **edits) -> dict:
    from vlaky.posta.spracovanie import confirm_event

    ev = confirm_event(session, ev_id, "dispecer1", edits=edits or None)
    session.commit()
    return (ev.validation or {}).get("apply_result") or {}


def _mails(env, session, name: str, subject: str, body: str, sender: str, date: str, now) -> list:
    before = {e.id for e in events(session)}
    drop(env, name, raw_mail(subject, body, name.replace(".", "-"), sender=sender, date=date))
    _process(session, now)
    return [e for e in events(session) if e.id not in before]


def _register(env, session, *a) -> list:
    new = _mails(env, session, *a)
    for e in new:
        _confirm(session, e.id)
    return new


def _green(r) -> bool:
    return r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"


@pytest.fixture()
def zamer_schema(monkeypatch):
    """Zámer zmeny v dátach udalosti (schéma vyluka_zmena s kľúčom ``zamer`` – ako ho dopĺňajú pravidlá)."""
    from vlaky.posta import udalosti

    monkeypatch.setitem(udalosti.SCHEMAS, "vyluka_zmena", list(udalosti.SCHEMAS["vyluka_zmena"]) + ["zamer"])


_ROV2331 = ("Dobrý deň,\n\núplná výluka v úseku Ružomberok – Liptovský Mikuláš v dňoch 23.11. – 27.11.2026 denne od "
            "08:00 do 14:00 hod. (ROV č. 2331/2026).\n\nOR Žilina\n")


def test_doplnenie_terminu_zachova_povodne_dni(env, session, zamer_schema):
    """FC2 D21: „ROV č. 2331/2026 sa dopĺňa o ďalší termín: výluka … bude aj v dňoch 30.11.2026 – 04.12.2026 denne od
    08:00 do 14:00 hod.“ so zámerom doplnenie – po Potvrdiť ostáva 23. – 27.11. a pribudne 30.11. – 04.12.; vlak 24.11.
    o 10:00 ostáva zdržaný (v 631f185 sa výluka presunula a vlak dostal VYPRAVIŤ ZELENÁ)."""
    _register(env, session, "r.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", _ROV2331, ROV_ZA,
              "Mon, 16 Nov 2026 09:00:00 +0100", utc(2026, 11, 16, 8, 2))
    new = _mails(env, session, "z.eml", "Zmena ROV č. 2331/2026 – doplnenie termínu",
                 "Dobrý deň,\n\nROV č. 2331/2026 sa dopĺňa o ďalší termín: výluka v úseku Ružomberok – Liptovský "
                 "Mikuláš bude aj v dňoch 30.11.2026 – 04.12.2026 denne od 08:00 do 14:00 hod.\n\nOR Žilina\n", ROV_ZA,
                 "Thu, 19 Nov 2026 09:00:00 +0100", utc(2026, 11, 19, 8, 2))
    for e in new:
        if e.status == "navrh":
            _confirm(session, e.id, **({"zamer": "doplnenie"} if e.kind == "vyluka_zmena" else {}))
    r = _decide(session, "77101", "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 24, 10), loc(2026, 11, 24, 7))
    assert not _green(r), r.summary
    r = _decide(session, "77102", "Ružomberok", "Liptovský Mikuláš", loc(2026, 12, 2, 10), loc(2026, 12, 2, 7))
    assert not _green(r), r.summary


def test_predlzenie_viacdennej_vyluky_pokryje_medzidni(env, session, zamer_schema):
    """FC6 G09: „výluka … podľa ROV č. 2331/2026 sa predlžuje do 29.11.2026, denne od 08:00 do 14:00 hod.“ so zámerom
    predĺženie (nový termín prečítaný len ako 29.11.) – koniec výluky sa posunie, 28.11. je uzavretý (v 631f185 len
    29.11. a vlak 28.11. o 10:00 VYPRAVIŤ ZELENÁ)."""
    _register(env, session, "r.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", _ROV2331, ROV_ZA,
              "Mon, 16 Nov 2026 09:00:00 +0100", utc(2026, 11, 16, 8, 2))
    new = _mails(env, session, "z.eml", "Zmena ROV č. 2331/2026 – predĺženie",
                 "Dobrý deň,\n\nvýluka v úseku Ružomberok – Liptovský Mikuláš podľa ROV č. 2331/2026 sa predlžuje do "
                 "29.11.2026, denne od 08:00 do 14:00 hod.\n\nOR Žilina\n", ROV_ZA, "Thu, 19 Nov 2026 09:00:00 +0100",
                 utc(2026, 11, 19, 8, 2))
    ev = next(e for e in new if e.kind == "vyluka_zmena")
    if not ev.data.get("start"):
        pytest.skip("pravidlá termín predĺženia neprečítali – zmena bez termínu sa nezapíše (poistka)")
    res = _confirm(session, ev.id, zamer="predlzenie")
    assert res.get("ok") is True, res
    r = _decide(session, "77103", "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 28, 10), loc(2026, 11, 28, 7))
    assert not _green(r), r.summary
    r = _decide(session, "77104", "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 24, 10), loc(2026, 11, 24, 7))
    assert not _green(r), r.summary


@pytest.mark.parametrize("zamer,term,daily,expect", [
    ("predlzenie", ((2026, 11, 29, 8), (2026, 11, 29, 14)), ("", ""), ((2026, 11, 27, 14), ("08:00", "14:00"))),
    ("predlzenie", ((2026, 11, 29, 6), (2026, 11, 29, 16)), ("", ""), ((2026, 11, 27, 14), ("", ""))),
    ("predlzenie", ((2026, 11, 28, 8), (2026, 11, 29, 14)), ("08:00", "14:00"),
     ((2026, 11, 27, 14), ("08:00", "14:00"))),
    ("presun", ((2026, 11, 29, 8), (2026, 11, 29, 14)), ("", ""), ((2026, 11, 29, 8), ("", ""))),
    ("doplnenie", ((2026, 11, 29, 8), (2026, 11, 29, 14)), ("", ""), ((2026, 11, 29, 8), ("", ""))),
    (None, ((2026, 11, 29, 8), (2026, 11, 29, 14)), ("", ""), ((2026, 11, 29, 8), ("", ""))),
], ids=["v_dennom_okne", "mimo_denneho_okna_suvislo", "s_dennym_oknom", "presun", "doplnenie", "bez_zameru"])
def test_cakajuce_predlzenie_pokryje_medzidni(zamer, term, daily, expect):
    """Dočasný záznam čakajúceho predĺženia (nový termín prečítaný len ako 29.11.) začne koncom výluky – 28.11. nie je
    do potvrdenia voľný; denné okno výluky len ak sa doň nový termín celý zmestí (inak súvislo, nikdy menej ako nový
    termín). Presun, doplnenie a zmena bez zámeru a bez slova o predĺžení ostávajú pri novom termíne."""
    from vlaky.posta.spracovanie import _pending_extension

    f = _fields(loc(*term[0]), loc(*term[1]), daily)
    _pending_extension(f, _closure(), {"zamer": zamer} if zamer else {}, None)
    assert (f["start_utc"], (f["daily_from"], f["daily_to"])) == (loc(*expect[0]), expect[1])
    assert f["end_utc"] == loc(*term[1])


def test_cakajuce_predlzenie_bez_zameru_drzi_medzidni_cez_retazec(env, session):
    """FC6 G09 pred potvrdením: „výluka … podľa ROV č. 2331/2026 sa predlžuje do 29.11.2026, denne od 08:00 do 14:00
    hod.“ – kým dispečer zmenu nepotvrdí, vlak 28.11. o 10:00 nedostane VYPRAVIŤ ZELENÁ (v 631f185 dočasný záznam len
    29.11.)."""
    _register(env, session, "r.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", _ROV2331, ROV_ZA,
              "Mon, 16 Nov 2026 09:00:00 +0100", utc(2026, 11, 16, 8, 2))
    new = _mails(env, session, "z.eml", "Zmena ROV č. 2331/2026 – predĺženie",
                 "Dobrý deň,\n\nvýluka v úseku Ružomberok – Liptovský Mikuláš podľa ROV č. 2331/2026 sa predlžuje do "
                 "29.11.2026, denne od 08:00 do 14:00 hod.\n\nOR Žilina\n", ROV_ZA, "Thu, 19 Nov 2026 09:00:00 +0100",
                 utc(2026, 11, 19, 8, 2))
    ev = next(e for e in new if e.kind == "vyluka_zmena")
    assert ev.status == "navrh", ev.status
    if not ev.data.get("start"):
        pytest.skip("pravidlá termín predĺženia neprečítali – zmena bez termínu sa nezapíše (poistka)")
    r = _decide(session, "77107", "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 28, 10), loc(2026, 11, 28, 7))
    assert not _green(r), r.summary
    temp = [c for c in session.scalars(select(Closure)) if c.proposed_change_of is not None]
    assert temp and as_utc(temp[0].start_utc) <= loc(2026, 11, 27, 14), [(c.start_utc, c.end_utc) for c in temp]


def test_uprava_agenta_v_docasnom_zazname_nie_je_upravou_dispecera(env, session):
    """Dočasný záznam čakajúceho predĺženia začína koncom výluky – je to úprava agenta, nie dispečera: fronta ju
    neukáže ako úpravu v registri výluk a potvrdenie ju neprenesie ako jeho opravu. Skutočná úprava dispečera
    v registri sa ukáže ďalej."""
    from vlaky.posta.spracovanie import register_edits

    _register(env, session, "r.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", _ROV2331, ROV_ZA,
              "Mon, 16 Nov 2026 09:00:00 +0100", utc(2026, 11, 16, 8, 2))
    new = _mails(env, session, "z.eml", "Zmena ROV č. 2331/2026 – predĺženie",
                 "Dobrý deň,\n\nvýluka v úseku Ružomberok – Liptovský Mikuláš podľa ROV č. 2331/2026 sa predlžuje do "
                 "29.11.2026, denne od 08:00 do 14:00 hod.\n\nOR Žilina\n", ROV_ZA, "Thu, 19 Nov 2026 09:00:00 +0100",
                 utc(2026, 11, 19, 8, 2))
    ev = next(e for e in new if e.kind == "vyluka_zmena")
    if not ev.data.get("start"):
        pytest.skip("pravidlá termín predĺženia neprečítali")
    (temp,) = [c for c in session.scalars(select(Closure)) if c.proposed_change_of is not None]
    # dočasný záznam pokrýva medzidni: začína najneskôr koncom výluky (pravidlá bez začiatku predĺženia – začiatok podľa
    # registra, teda začiatkom výluky; s novým termínom len 29.11. – posunutý na koniec výluky)
    assert as_utc(temp.start_utc) <= loc(2026, 11, 27, 14) and as_utc(temp.end_utc) >= loc(2026, 11, 29, 14), \
        (temp.start_utc, temp.end_utc)
    assert register_edits(session, ev) == {}
    temp.to_station = "Liptovský Hrádok"
    session.flush()
    assert set(register_edits(session, ev)) == {"to_station"}


def test_zmena_do_odvolania_od_konca_vyluky_ju_nepresunie(env, session):
    """FC6 H09 cez skutočný reťazec (bez zámeru): po Potvrdiť ostáva výluka od 23.11. 08:00 – vlak 24.11. o 10:00 je
    zdržaný (v 631f185 sa výluka presunula na 27.11. 18:00 – do odvolania a vlak dostal VYPRAVIŤ ZELENÁ)."""
    _register(env, session, "r.eml", "ROV c. 2709/2026 Kralovany - Ruzomberok",
              "Dobrý deň,\n\núplná výluka trate v úseku Kraľovany – Ružomberok od 23.11.2026 08:00 hod. do 27.11.2026 "
              "18:00 hod. (ROV č. 2709/2026).\n\nOR Žilina\n", ROV_ZA, "Mon, 16 Nov 2026 09:00:00 +0100",
              utc(2026, 11, 16, 8, 2))
    new = _mails(env, session, "z.eml", "Zmena ROV č. 2709/2026",
                 "Dobrý deň,\n\nz dôvodu nedokončenia prác sa ROV č. 2709/2026 mení: od 27.11.2026 18:00 hod. do "
                 "odvolania úplná výluka trate v úseku Kraľovany – Ružomberok.\n\nOR Žilina\n", ROV_ZA,
                 "Sat, 21 Nov 2026 09:00:00 +0100", utc(2026, 11, 21, 8, 2))
    for e in new:
        if e.status == "navrh":
            _confirm(session, e.id)
    r = _decide(session, "77105", "Kraľovany", "Ružomberok", loc(2026, 11, 24, 10), loc(2026, 11, 24, 7))
    assert not _green(r), r.summary
    r = _decide(session, "77106", "Kraľovany", "Ružomberok", loc(2026, 11, 29, 10), loc(2026, 11, 29, 7))
    assert not _green(r), r.summary


def test_zamietnutie_predlzenia_vrati_povodny_termin(env, session, zamer_schema):
    """Predĺženie zapísané s ďalšou výlukou (iné denné okno): zamietnutie vráti pôvodný koniec a ďalšiu výluku zruší."""
    from vlaky.posta.spracovanie import reject_event

    _register(env, session, "r.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", _ROV2331, ROV_ZA,
              "Mon, 16 Nov 2026 09:00:00 +0100", utc(2026, 11, 16, 8, 2))
    new = _mails(env, session, "z.eml", "Zmena ROV č. 2331/2026",
                 "Dobrý deň,\n\nZmena ROV č. 2331/2026: výluka v úseku Ružomberok – Liptovský Mikuláš dňa 29.11.2026 "
                 "od 06:00 do 16:00 hod.\n\nOR Žilina\n", ROV_ZA, "Thu, 19 Nov 2026 09:00:00 +0100",
                 utc(2026, 11, 19, 8, 2))
    ev = next(e for e in new if e.kind == "vyluka_zmena")
    res = _confirm(session, ev.id, zamer="predlzenie")
    assert res.get("ok") is True and res.get("added"), res
    reject_event(session, ev.id, "dispecer1", reason="test")
    session.commit()
    live = [c for c in session.scalars(select(Closure).order_by(Closure.id))
            if c.status != "zrusene" and c.proposed_change_of is None]
    assert [(as_utc(c.start_utc), as_utc(c.end_utc)) for c in live] == [(loc(2026, 11, 23, 8), loc(2026, 11, 27, 14))]


def test_zmena_vyluky_kolaje_bez_kolaje_ponecha_kolaj(env, session):
    """FE (strata koľaje): 2. TK Kysak – Košice 01:00 – 02:00 (ROV 1301) a 1. TK 03:00 – 07:00 (ROV 1302); odklad ROV
    1301 len podľa čísla na 03:00 – 07:00, dispečer doplní úsek a druh „výluka koľaje“ (koľaj neuvedie) – koľaj 2. TK
    ostane, výluky oboch koľají sa spoja do úplnej výluky a vlak Kysak → Košice o 04:00 nedostane VYPRAVIŤ ZELENÁ."""
    for n, (track, a, b) in enumerate((("2", 1, 2), ("1", 3, 7))):
        _register(env, session, f"r{n}.eml", f"Rozkaz o výluke (ROV) č. 130{n + 1}/2026 – výluka {track}. TK",
                  f"Na základe ROV č. 130{n + 1}/2026 bude vykonaná výluka koľaje:\n\nÚsek: Kysak – Košice, "
                  f"{track}. traťová koľaj ({track}. TK)\nDruh výluky: výluka koľaje\nTermín: 05.11.2026 od "
                  f"{a:02d}:00 do {b:02d}:00\n\nOR Košice\n", ROV_KE, "Thu, 05 Nov 2026 00:30:00 +0100",
                  utc(2026, 11, 4, 23, 32))
    live = {c.external_ref: c.track for c in session.scalars(select(Closure))}
    assert sorted(live.values()) == ["1", "2"], live
    (ev,) = _mails(env, session, "o.eml", "ROV č. 1301/2026 sa odkladá",
                   "Oznamujeme, že výluka podľa ROV č. 1301/2026 sa odkladá.\nNový termín: 05.11.2026 od 03:00 do "
                   "07:00.\n\nOR Košice\n", ROV_KE, "Thu, 05 Nov 2026 00:40:00 +0100", utc(2026, 11, 4, 23, 42))
    assert ev.kind == "vyluka_zmena", ev.kind
    res = _confirm(session, ev.id, from_station="Kysak", to_station="Košice", kind="kolaj")
    assert res.get("ok") is True, res
    tracks = sorted(c.track for c in session.scalars(select(Closure))
                    if c.status != "zrusene" and c.proposed_change_of is None)
    assert tracks == ["1", "2"], tracks
    r = _decide(session, "77201", "Kysak", "Košice", utc(2026, 11, 5, 3), utc(2026, 11, 5, 0))
    assert not _green(r), r.summary
