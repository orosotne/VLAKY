"""Druhá revízia pošty – doplnenie opráv po kontrole (oblasť „posta“): cieľ potvrdenia s opravou čísla či druhu
([12]), skrátené zoznamy dní a nocí ([16]), falošné upozornenia na nepriradený dátum a príznak „do odvolania“ pri
zastaranom formulári výluky.

Každý test opisuje scenár v docstringu. Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from test_revizia2_posta import (_rules, _terms, active, as_utc, drop_raw, env, events, flash, mail,  # noqa: F401
                                 planner, register_form, registered, strict, utc)
from test_web_spolocne import csrf, login_as
from vlaky.models import Closure, ExtractedEvent
from vlaky.posta.spracovanie import confirm_event, process_mailbox

pytest_plugins = ["test_web_spolocne"]


SECTIONS = {"pt": ("Poprad-Tatry", "Liptovský Mikuláš", "Poprad-Tatry - Liptovsky Mikulas"),
            "zv": ("Žilina", "Vrútky", "Zilina - Vrutky")}


def _change_pinned(env, session, day: int, mid: str, sec: str = "pt") -> ExtractedEvent:
    """Zmena termínu bez čísla výluky (nový termín ``day``.11. 08:00 – 14:00) – čaká na potvrdenie."""
    a, b, ascii_sec = SECTIONS[sec]
    drop_raw(env, f"{mid}.eml", mail(mid, f"Zmena terminu vyluky {ascii_sec}",
                                     f"Oznamujeme zmenu termínu výluky 1. TK v úseku {a} – {b}.\n"
                                     f"Nový termín: od {day}.11.2026 08:00 do {day}.11.2026 14:00\n"))
    process_mailbox(session)
    return events(session)[-1]


def _manual(session, start, ref: str = "", sec: str = "pt") -> Closure:
    a, b, _ascii = SECTIONS[sec]
    c = Closure(kind="kolaj", from_station=a, to_station=b, track="1", start_utc=start,
                end_utc=start + timedelta(hours=6), status="overene", source="manual", external_ref=ref)
    session.add(c)
    session.commit()
    return c


def _section_rows(session, sec: str) -> list:
    return [(c.id, c.external_ref, as_utc(c.start_utc)) for c in active(session) if c.from_station == SECTIONS[sec][0]]


# ====================================================================== [12] cieľ potvrdenia s opravou
def test_r12_ciel_bez_cisla_a_doplnene_cislo_inej_vyluky(env, session):
    """Zmena bez čísla sa voľne priradila k ručne zadanej výluke X (20.11., bez čísla). Dispečer pri potvrdení
    doplní číslo 1601/2026, ktoré má výluka Y (30.11.) v tom istom úseku – zmena patrí Y; X (20.11.) ostane.

    Číslo, ktoré v registri nemá žiadna iná výluka, priradenie nemení (zmena sa zapíše do priradenej výluky)."""
    x = _manual(session, utc(2026, 11, 20, 7))
    y = registered(session, utc(2026, 11, 30, 7), "1601/2026")
    ev = _change_pinned(env, session, 22, "zmena-x")
    assert ev.validation["pending_closure"]["proposed_change_of"] == x.id
    confirm_event(session, ev.id, "dispecer1", edits={"external_ref": "1601/2026"})
    session.commit()
    session.refresh(x)
    session.refresh(y)
    assert (as_utc(x.start_utc), x.external_ref, x.status) == (utc(2026, 11, 20, 7), "", "overene")
    assert (as_utc(y.start_utc), y.external_ref, y.status) == (utc(2026, 11, 22, 7), "1601/2026", "overene")
    assert ev.validation["apply_result"]["id"] == y.id and len(active(session)) == 2
    # iný úsek: doplnené nové číslo (žiadna iná výluka ho nemá) – priradenie z fronty platí
    z = _manual(session, utc(2026, 11, 20, 7), sec="zv")
    ev2 = _change_pinned(env, session, 22, "zmena-z", sec="zv")
    assert ev2.validation["pending_closure"]["proposed_change_of"] == z.id
    confirm_event(session, ev2.id, "dispecer1", edits={"external_ref": "1777/2026"})
    session.commit()
    assert _section_rows(session, "zv") == [(z.id, "1777/2026", utc(2026, 11, 22, 7))]


def test_r12_zmena_preklasifikovana_na_novu_vyluku_prida_termin(env, session):
    """Zmena bez čísla sa voľne (± 3 dni) priradila k 1600/2026 (20.11.). Dispečer ju pri potvrdení preklasifikuje
    na novú výluku (ďalší termín 22.11., nie presun) – 1600/2026 na 20.11. ostane, 22.11. je nový záznam.

    Preklasifikácia s rovnakým číslom je podľa pravidiel tá istá výluka – priradenie platí (bez duplicitného čísla)."""
    x = registered(session, utc(2026, 11, 20, 7), "1600/2026")
    ev = _change_pinned(env, session, 22, "zmena-n")
    assert ev.validation["pending_closure"]["proposed_change_of"] == x.id
    confirm_event(session, ev.id, "dispecer1", edits={"event_kind": "vyluka_nova"})
    session.commit()
    session.refresh(x)
    assert (as_utc(x.start_utc), x.external_ref, x.status) == (utc(2026, 11, 20, 7), "1600/2026", "overene")
    rows = active(session)
    assert sorted(as_utc(c.start_utc) for c in rows) == [utc(2026, 11, 20, 7), utc(2026, 11, 22, 7)]
    new = next(c for c in rows if c.id != x.id)
    assert new.proposed_change_of is None and new.status == "overene" and new.source_event_id == ev.id
    # iný úsek: preklasifikácia na novú výluku s číslom priradenej výluky – zmení sa tá istá výluka
    z = _manual(session, utc(2026, 11, 20, 7), "1650/2026", sec="zv")
    ev2 = _change_pinned(env, session, 22, "zmena-r", sec="zv")
    confirm_event(session, ev2.id, "dispecer1", edits={"event_kind": "vyluka_nova", "external_ref": "1650/2026"})
    session.commit()
    assert _section_rows(session, "zv") == [(z.id, "1650/2026", utc(2026, 11, 22, 7))]


# ====================================================================== [16] skrátené zoznamy dní a nocí
def _day(d: int, h0: int = 7, h1: int = 13, month: int = 11) -> tuple:
    return (utc(2026, month, d, h0).isoformat(), utc(2026, month, d, h1).isoformat())


def _night(d: int) -> tuple:
    return (utc(2026, 11, d, 21).isoformat(), utc(2026, 11, d + 1, 4).isoformat())


@pytest.mark.parametrize("text,expected", [
    ("v dňoch 03. a 04.11.2026 od 08:00 do 14:00", [_day(3), _day(4)]),
    ("v dňoch 03., 04. a 05.11.2026 od 08:00 do 14:00", [_day(3), _day(4), _day(5)]),
    ("12. a 13.11.2026 od 8:00 do 14:00", [_day(12), _day(13)]),
    ("v nociach 03./04. a 04./05.11.2026 od 22:00 do 05:00", [_night(3), _night(4)]),
    ("v nociach 12./13., 13./14. a 14./15.11.2026 od 22:00 do 05:00", [_night(12), _night(13), _night(14)]),
    ("v noci z 12. na 13. a z 13. na 14.11.2026 od 22:00 do 05:00", [_night(12), _night(13)]),
    ("3. a 4. novembra 2026 od 8:00 do 14:00", [_day(3), _day(4)]),
    ("v termíne 30. a 1.12.2026 od 8:00 do 14:00", [_day(30), _day(1, month=12)]),
    ("15. a 16. 11. 2026 od 8.00 do 14.00 hod.", [_day(15), _day(16)]),
])
def test_r16_skrateny_zoznam_dni_a_noci(cfg, text, expected):
    """[16] Skrátený zoznam („03. a 04.11.2026“, „03./04. a 04./05.11.“) – každý deň či noc dostane obdobie
    s mesiacom a rokom z posledného dátumu (aj cez prelom mesiaca); istota sa neznižuje."""
    items = _rules(f"Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš {text}.")
    assert _terms(items) == expected
    assert all(i.confidence == 0.85 for i in items), [i.notes for i in items]


def test_r16_skrateny_zaciatok_obdobia_a_zoznam_pred_obdobim(cfg):
    """[16] „od 3. do 5.11.2026“ je obdobie 3. – 5.11. (nie len 5.11.); dni zoznamu pred obdobím („3., 4. a
    10. – 12.11.“) majú to isté denné okno."""
    (it,) = _rules("Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš od 3. do 5.11.2026 denne od 8:00 do 14:00.")
    assert (it.data["start"], it.data["end"], it.data["daily_from"], it.data["daily_to"]) == (
        utc(2026, 11, 3, 7).isoformat(), utc(2026, 11, 5, 13).isoformat(), "08:00", "14:00")
    items = _rules("Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš v dňoch 3., 4. a 10. – 12.11.2026 "
                   "v čase 8:00 – 14:00.")
    assert _terms(items) == [_day(3), _day(4), (utc(2026, 11, 10, 7).isoformat(), utc(2026, 11, 12, 13).isoformat())]


@pytest.mark.parametrize("text", ["v dňoch 3, 4 a 5.11.2026 od 8:00 do 14:00",
                                  "v dňoch 5. a 3.11.2026 od 8:00 do 14:00"])
def test_r16_neprecitany_zoznam_dni_znizi_istotu(cfg, text):
    """[16] Holé čísla dní, ktoré sa nedajú bezpečne prečítať (bez bodky, zlé poradie), sa nevymýšľajú – dispečer
    dostane upozornenie „overte termín“ a istota klesne."""
    items = _rules(f"Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš {text}.")
    assert items and all(i.confidence < 0.8 and any("overte termín" in n for n in i.notes) for i in items)


@pytest.mark.parametrize("term,expected", [
    ("v dňoch 03. a 04.11.2026 od 08:00 do 14:00", [utc(2026, 11, 3, 7), utc(2026, 11, 4, 7)]),
    ("v dňoch 03., 04. a 05.11.2026 od 08:00 do 14:00", [utc(2026, 11, 3, 7), utc(2026, 11, 4, 7),
                                                         utc(2026, 11, 5, 7)]),
    ("v nociach 03./04. a 04./05.11.2026 od 22:00 do 05:00", [utc(2026, 11, 3, 21), utc(2026, 11, 4, 21)]),
])
def test_r16_skrateny_zoznam_end_to_end_kazdy_den_pre_planovac(env, session, fixed_now, term, expected):
    """[16] E-mail so skráteným zoznamom: každý deň (noc) má záznam „navrh“, s ktorým plánovač počíta, kým dispečer
    výluku nepotvrdí – žiadny deň sa potichu nestratí."""
    drop_raw(env, "n.eml", mail(f"zoznam{len(term)}", "ROV c. 2400/2026 Poprad-Tatry - Liptovsky Mikulas",
                                "Rozkaz o výluke č. 2400/2026: výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš "
                                f"{term}.\n"))
    process_mailbox(session)
    assert sorted(as_utc(c.start_utc) for c in active(session)) == expected
    assert all(c.status == "navrh" for c in active(session))


# ====================================================================== [16] bez falošných upozornení
@pytest.mark.parametrize("tail", [
    "Vybavuje: Ing. Novák, tel. 0900 000 000, Bratislava 30.10.2026",
    "Dátum vyhotovenia: 30.10.2026.",
    "Termín platí namiesto 12.11.2026.",
    "Platnosť ROV od 1.11.2026.",
])
def test_r16_datum_dokumentu_nie_je_upozornenie(cfg, tail):
    """Dátum vyhotovenia, podpisu, pôvodný termín („namiesto“) či platnosť rozkazu v tom istom odseku nie je
    nepriradený termín – bez falošného upozornenia „overte termín“ a bez zníženia istoty."""
    (it,) = _rules(f"Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš 13.11.2026 od 8:00 do 14:00. {tail}")
    assert it.confidence == 0.85 and not any("overte termín" in n for n in it.notes), it.notes
    # skutočný ďalší dátum v odseku sa naďalej hlási
    (warn,) = _rules("Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš 13.11.2026 od 8:00 do 14:00. "
                     "Náhradná doprava aj 14.11.2026.")
    assert warn.confidence < 0.8 and any("overte termín" in n for n in warn.notes)


@pytest.mark.parametrize("head", ["Rozkaz zo dňa 30.10.2026,", "Oznámenie z 5.11.2026,"])
def test_r16_datum_vydania_pred_terminom_nie_je_dalsi_den(cfg, head):
    """Dátum vydania tesne pred termínom („Rozkaz zo dňa 30.10.2026, 12.11.2026 od 8:00“) nie je ďalší deň
    zoznamu – nevznikne falošná výluka na deň vydania."""
    items = _rules(f"{head} 12.11.2026 od 8:00 do 14:00. Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš.")
    assert _terms(items) == [_day(12)]


# ====================================================================== výluka „do odvolania“ a formulár
def _open_ended(session, fixed_now) -> int:
    c = Closure(kind="kolaj", from_station="Poprad-Tatry", to_station="Liptovský Mikuláš", track="1",
                start_utc=fixed_now - timedelta(days=20), end_utc=fixed_now + timedelta(days=6), status="overene",
                source="manual", until_revoked=True, external_ref="POTR 5/2026")
    session.add(c)
    session.commit()
    return c.id


def _shown_form(client, session, cid: int, **changes) -> tuple[dict, str]:
    """Formulár úpravy tak, ako ho stránka práve zobrazila (skryté pole so zobrazeným koncom, ak ho stránka má)."""
    import re

    page = client.get(f"/vyluky/{cid}/upravit").text
    form = register_form(session.get(Closure, cid), **changes)
    shown = re.search(r'name="zobrazeny_koniec" value="([^"]*)"', page)
    form.pop("zobrazeny_koniec", None)
    if shown:
        form["zobrazeny_koniec"] = shown.group(1)
    return form, page


def test_r39_zastaraly_formular_nezrusi_priznak_do_odvolania(strict, session, client, fixed_now, planner):
    """Dispečer otvorí úpravu overenej výluky „do odvolania“ (technický koniec o 6 dní), medzitým agent technický
    koniec posunie (o 30 dní). Dispečer uloží len zmenu koľaje – príznak „do odvolania“ ostane a koniec sa
    neskráti na zastaraný (inak by výluka o 6 dní potichu skončila a plánovač by úsek považoval za voľný).

    Starší formulár bez skrytého poľa (otvorený pred aktualizáciou) príznak tiež ponechá – nedá sa zistiť, či
    dispečer koniec menil, a výluka sa nesmie skončiť potichu."""
    cid = _open_ended(session, fixed_now)
    login_as(client, "dispecer")
    form, page = _shown_form(client, session, cid, track="2")
    process_mailbox(session, now=fixed_now)                      # agent posunie technický koniec
    session.expire_all()
    extended = as_utc(session.get(Closure, cid).end_utc)
    assert extended > fixed_now + timedelta(days=29)
    r = client.post(f"/vyluky/{cid}/upravit", data={"csrf_token": csrf(client, "/vyluky"), **form},
                    follow_redirects=True)
    assert "Výluka uložená." in flash(r) and "do odvolania" not in flash(r)
    session.expire_all()
    c = session.get(Closure, cid)
    assert (c.until_revoked, c.track, as_utc(c.end_utc)) == (True, "2", extended)
    assert "Výluka platí do odvolania" in page and form["zobrazeny_koniec"] == form["end_utc"]   # skryté pole
    # starší formulár bez zobrazeného konca (koniec zmenený na zastaraný)
    old = register_form(c, track="1", end_utc=form["end_utc"])
    old.pop("zobrazeny_koniec", None)
    client.post(f"/vyluky/{cid}/upravit", data={"csrf_token": csrf(client, "/vyluky"), **old})
    session.expire_all()
    c = session.get(Closure, cid)
    assert (c.until_revoked, c.track) == (True, "1")


def test_r39_skutocny_koniec_vyluky_do_odvolania_zrusi_priznak(strict, session, client, fixed_now, planner):
    """Dispečer v aktuálnom formulári zmení koniec výluky „do odvolania“ (ŽSR oznámila skutočný koniec) – výluka
    platí do jeho konca, agent ju už neposúva a hláška to povie."""
    cid = _open_ended(session, fixed_now)
    login_as(client, "dispecer")
    form, _page = _shown_form(client, session, cid, end_utc="2026-11-05T18:00")
    r = client.post(f"/vyluky/{cid}/upravit", data={"csrf_token": csrf(client, "/vyluky"), **form},
                    follow_redirects=True)
    assert "už neplatí „do odvolania“" in flash(r)
    session.expire_all()
    c = session.get(Closure, cid)
    assert (c.until_revoked, as_utc(c.end_utc)) == (False, utc(2026, 11, 5, 17))
    process_mailbox(session, now=fixed_now + timedelta(days=2))   # posun technického konca by tu zasiahol
    session.expire_all()
    assert as_utc(session.get(Closure, cid).end_utc) == utc(2026, 11, 5, 17)
