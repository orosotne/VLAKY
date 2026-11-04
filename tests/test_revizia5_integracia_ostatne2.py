"""5. revízia – integrácia, oprava po kontrole (strana registra): úsek z registra výluk podľa čísla ROV.

- e-mail len s číslom ROV, ktorý uvádza iné miesto (dopravňu na začiatku vety či riadku, v predmete, veľkými
  písmenami, v zátvorke, „Miesto: …“, „tr. 140“, „TÚ 2401“, mesto s viacerými dopravňami, skrátený a skloňovaný
  názov) – úsek sa z registra nedoplní (ostane poistka pre celý vozový park a upozornenie), inak by vlak v mieste
  z e-mailu dostal VYPRAVIŤ ZELENÁ; pozdrav, podpis, meno za titulom, „traťová koľaj“ či „dopravné opatrenia“
  doplnenie nebránia;
- overenie miesta je lineárne v dĺžke e-mailu a ohraničené (dlhší e-mail či neprečítaná príloha – úsek sa nedoplní);
- staršia zmena len podľa čísla (úsek z registra, dočasný záznam pre plánovač) nebráni rozdeleniu výluky novšou
  zmenou: zmena toho istého dňa je potom nahradená, zmena iného dňa sa zapíše; zrušenie časti výluky na ňu počká;
- druh dokumentu: „TLG 5310/2026“ nie je „ROV 5310/2026“; výluka len s číslom bez druhu dokumentu úsek nedá.

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from vlaky.models import Closure
from test_revizia3_terminy import drop, events, raw_mail, utc
from test_revizia4_pravidla import ROV_KE, ROV_ZA, _decide, _process, env  # noqa: F401 – fixture env

ROV_BA = "ZSR OR Bratislava <vyluky.orba@zsr.example>"
TZ = ZoneInfo("Europe/Bratislava")


def loc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=TZ).astimezone(timezone.utc)


def _confirm(session, ev_id: int, **edits) -> dict:
    """Potvrdenie dispečerom; vráti výsledok zápisu (aj odmietnutý zápis)."""
    from vlaky.posta.spracovanie import confirm_event

    ev = confirm_event(session, ev_id, "dispecer1", edits=edits or None)
    session.commit()
    return (ev.validation or {}).get("apply_result") or {}


def _register(env, session, name: str, subject: str, body: str, sender: str, date: str, now) -> None:
    before = {e.id for e in events(session)}
    drop(env, name, raw_mail(subject, body, name.replace(".", "-"), sender=sender, date=date))
    _process(session, now)
    for e in events(session):
        if e.id not in before:
            _confirm(session, e.id)


def _new_events(env, session, name: str, subject: str, body: str, sender: str, date: str, now) -> list:
    before = {e.id for e in events(session)}
    drop(env, name, raw_mail(subject, body, name.replace(".", "-"), sender=sender, date=date))
    _process(session, now)
    return [e for e in events(session) if e.id not in before]


def _warnings(ev) -> str:
    return " | ".join((ev.validation or {}).get("warnings") or [])


def _closed_at(session, when: datetime) -> bool:
    from datetime import timedelta

    from vlaky.planovanie.planovac import closure_windows

    return any(w.start <= when < w.end for w in closure_windows(session, when - timedelta(hours=1),
                                                                 when + timedelta(hours=1)))


_ROV4120 = ("Dobrý deň,\n\núplná výluka v úseku Senec – Galanta dňa 04.12.2026 od 08:00 do 14:00 hod. "
            "(ROV 4120/2026).\n\nOR Bratislava\n")


# ====================================================================== (1) iné miesto v e-maile – úsek sa nedoplní
@pytest.mark.parametrize("subject,body", [
    ("ROV 4120/2026", "Šurany: výluka podľa ROV 4120/2026 bude dňa 11.12.2026 od 8:00 do 12:00 hod."),
    ("ROV 4120/2026 NOVE ZAMKY", "Výluka podľa ROV 4120/2026 sa presúva na 11.12.2026 od 8:00 do 12:00 hod."),
    ("ROV 4120/2026", "Výluka podľa ROV 4120/2026 sa presúva na 11.12.2026 od 8:00 do 12:00 hod. (Šurany)"),
    ("ROV 4120/2026", "Výluka podľa ROV 4120/2026 sa presúva na 11.12.2026 od 8:00 do 12:00 hod.\nMiesto: Šurany"),
    ("ROV 4120/2026", "Výluka podľa ROV 4120/2026 sa presúva na 11.12.2026 od 8:00 do 12:00 hod.\n\nNové Zámky\n"),
    ("Šurany – zmena ROV 4120/2026", "Výluka podľa ROV 4120/2026 bude dňa 11.12.2026 od 8:00 do 12:00 hod."),
    ("ROV 4120/2026", "Výluka podľa ROV 4120/2026 bude dňa 11.12.2026 od 8:00 do 12:00 hod. v NOVÝCH ZÁMKOCH."),
    ("ROV 4120/2026", "Výluka podľa ROV 4120/2026 na tr. 140 sa presúva na 11.12.2026 od 8:00 do 12:00 hod."),
    ("ROV 4120/2026", "Nové Zámky – výluka podľa ROV 4120/2026 sa presúva na 11.12.2026 od 8:00 do 12:00 hod."),
    ("ROV 4120/2026", "Výluka podľa ROV 4120/2026 sa presúva na 11.12.2026 od 8:00 do 12:00 hod. Šurany – výhybka "
                      "č. 5."),
], ids=["stanica_na_zaciatku", "predmet_velkymi", "zatvorka_po_hod", "miesto_dvojbodka", "samostatny_riadok",
        "predmet_stanica_prva", "velke_pismena_lokativ", "skratka_trate", "riadok_stanica_pomlcka", "veta_za_hod"])
def test_iny_miesto_v_emaile_usek_z_registra_nedoplni(env, session, subject, body):
    """Kontrola I2, problém 1: e-mail od OR Bratislava len s číslom ROV 4120/2026 (v registri Senec – Galanta) uvádza
    iné miesto, ktoré pravidlá neprečítali. Úsek sa z registra nedoplní – upozornenie vo fronte miesto vymenuje;
    ostáva poistka pre celý vozový park: vlak Nové Zámky → Šurany 11.12. o 10:00 (v termíne z e-mailu) nie je
    VYPRAVIŤ ZELENÁ (v 8bfb48d dostal úsek Senec – Galanta a VYPRAVIŤ ZELENÁ)."""
    _register(env, session, "r.eml", "ROV c. 4120/2026 Senec - Galanta", _ROV4120, ROV_BA,
              "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    (ev,) = _new_events(env, session, "z.eml", subject, body + "\n\nOR Bratislava\n", ROV_BA,
                        "Thu, 26 Nov 2026 09:40:00 +0100", utc(2026, 11, 26, 8, 42))
    assert not ev.data.get("from_station") and not ev.data.get("to_station"), ev.data
    text = _warnings(ev)
    assert "nepodarilo prečítať (" in text and "Senec – Galanta" in text, text
    r = _decide(session, "72101", "Nové Zámky", "Šurany", utc(2026, 12, 11, 9), utc(2026, 12, 11, 6))
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), r.summary


_NOFILL = [
    "Výluka podľa ROV 4120/2026 sa presúva do Popradu.",
    "Výluka podľa ROV 4120/2026 bude v Bratislave dňa 11.12.2026.",
    "Výluka podľa ROV 4120/2026 sa presúva do Spišskej Novej Vsi.",
    "Výluka podľa ROV 4120/2026 sa presúva do L. Mikuláša.",
    "Výluka podľa ROV 4120/2026 sa presúva do Zvolena.",
    "Výluka podľa ROV 4120/2026 sa presúva, viď TÚ 2401.",
    "Výluka podľa ROV 4120/2026 sa rozširuje na Šurany-Komárno dňa 11.12.2026.",
    "Výluka podľa ROV 4120/2026 („Šurany“) sa presúva.",
    "Výluka podľa ROV 4120/2026 sa presúva do dopravne Šurany.",
    "Výluka podľa ROV 4120/2026 sa presúva na odbočku Kopčany.",
    "Výluka podľa ROV 4120/2026, Leopoldov, sa presúva na 11.12.2026.",
    "Výluka podľa ROV 4120/2026 sa presúva\nŠurany 11.12.2026 od 8:00 do 12:00",
    "ŠURANY – NOVÉ ZÁMKY: výluka podľa ROV 4120/2026 dňa 11.12.2026 od 8:00 do 12:00 hod.",
    "vyluka podla rov 4120/2026 sa presuva na 11.12.2026, nove zamky.",
    "Ďakujem za informáciu, výluka podľa ROV 4120/2026 sa presúva do Šurian.",
]
_FILL = [
    "Výluka podľa ROV 4120/2026 sa odkladá o týždeň.",
    "Dobrý deň,\n\nvýluka podľa ROV 4120/2026 sa odkladá o týždeň.\n\nS pozdravom\nIng. Ján Novák\nOR Bratislava\n",
    "Výluka podľa ROV 4120/2026 sa odkladá, v Senci v rovnakom čase.",
    "Výluka podľa ROV 4120/2026 sa odkladá o týždeň, Galanta – Senec v rovnakom čase.",
    "VÝLUKA PODĽA ROV 4120/2026 SA ODKLADÁ O TÝŽDEŇ.",
    "Dobrý večer,\nvýluka podľa ROV 4120/2026 (1. traťová koľaj) sa odkladá o týždeň.",
    "Výluka podľa ROV 4120/2026 sa presúva na 11.12.2026. Kontakt: Ing. Martin Kováč, tel. 0900 000 000",
    "Výluka podľa ROV 4120/2026 sa nekoná. Dopravné opatrenia sa rušia.",
    "Vážení kolegovia,\nvýluka podľa ROV 4120/2026 sa odkladá na 18.12.2026, čas bez zmeny.\nDispečing OR Bratislava",
    "Výluka podľa ROV 4120/2026 sa odkladá, posunová čata počká na pokyn dispečera.",
    "vyluka podla rov 4120/2026 sa odklada o tyzden, v senci rovnako.",
]


@pytest.mark.parametrize("body,filled", [(b, False) for b in _NOFILL] + [(b, True) for b in _FILL],
                         ids=[f"inde{i}" for i in range(len(_NOFILL))] + [f"dopln{i}" for i in range(len(_FILL))])
def test_doplnenie_useku_podla_formulacie(session, body, filled):
    """Formulácie e-mailu len s číslom ROV: miesto kdekoľvek v texte (mesto s viacerými dopravňami – Poprad, Bratislava,
    Zvolen; „Spišskej Novej Vsi“, „L. Mikuláša“, „TÚ 2401“, spojovník, úvodzovky, dopravňa, odbočka, nový riadok, názov
    stanice malými písmenami bez diakritiky; aj v e-maile, ktorý začína „Ďakujem“) úsek z registra nedá; pozdrav,
    podpis, meno za titulom („Ing. Martin Kováč“), dopravňa úseku z registra, „1. traťová koľaj“, „dopravné
    opatrenia“, „posunová čata“ a „Dispečing OR Bratislava“ doplneniu nebránia."""
    from vlaky.posta.spracovanie import _register_place

    session.add(Closure(kind="uplna", from_station="Senec", to_station="Galanta", start_utc=utc(2026, 12, 4, 7),
                        end_utc=utc(2026, 12, 4, 13), status="overene", source="email", external_ref="ROV 4120/2026"))
    session.commit()
    mail = SimpleNamespace(sender="vyluky.orba@zsr.example", subject="ROV 4120/2026", body_text=body, attachments=[])
    data, reasons, warnings = _register_place(session, "vyluka_zmena", {"external_ref": "4120/2026", "kind": "uplna"},
                                              mail)
    if filled:
        assert (data.get("from_station"), data.get("to_station")) == ("Senec", "Galanta") and reasons, warnings
    else:
        assert not data.get("from_station") and not reasons, (data, warnings)
        assert warnings and "Senec – Galanta" in warnings[0], warnings


def test_neprecitana_priloha_usek_z_registra_nedoplni(session):
    """Príloha (PDF, sken), ktorej text sa nedal prečítať, môže uvádzať iný úsek – úsek sa z registra nedoplní
    a upozornenie prílohu uvedie; logo v podpise (malý obrázok) a kalendár (.ics) doplneniu nebránia."""
    from vlaky.posta.spracovanie import _register_place

    session.add(Closure(kind="uplna", from_station="Senec", to_station="Galanta", start_utc=utc(2026, 12, 4, 7),
                        end_utc=utc(2026, 12, 4, 13), status="overene", source="email", external_ref="ROV 4120/2026"))
    session.commit()

    def place(*attachments):
        mail = SimpleNamespace(sender="vyluky.orba@zsr.example", subject="Zmena ROV 4120/2026",
                               body_text="Zasielame zmenu ROV 4120/2026, podrobnosti sú v prílohe.",
                               attachments=list(attachments))
        return _register_place(session, "vyluka_zmena", {"external_ref": "4120/2026", "kind": "uplna"}, mail)

    pdf = SimpleNamespace(filename="ROV_4120_zmena.pdf", content_type="application/pdf", size=48_000, text="")
    scan = SimpleNamespace(filename="sken.jpg", content_type="image/jpeg", size=800_000, text="")
    for att in (pdf, scan):
        data, reasons, warnings = place(att)
        assert not data.get("from_station") and not reasons, data
        assert warnings and f"príloha „{att.filename}“ sa nedala prečítať" in warnings[0], warnings
    logo = SimpleNamespace(filename="logo.png", content_type="image/png", size=6_000, text="")
    ics = SimpleNamespace(filename="termin.ics", content_type="text/calendar", size=700, text="")
    data, reasons, warnings = place(logo, ics)
    assert data.get("from_station") == "Senec" and reasons, warnings


# ====================================================================== (2) overenie je lineárne a ohraničené
def test_overenie_miesta_je_linearne_a_ohranicene(session):
    """Kontrola I2, problém 2: overenie miesta prechádza text raz (v 8bfb48d pre každú pomlčku tokenizovalo celý text
    pred ňou a za ňou – 60 KB 12,6 s, pri zámku registra). Príloha ~55 KB s časovými rozsahmi sa overí do zlomku
    sekundy; dlhší e-mail sa neoverí – úsek sa nedoplní (vysvetlenie vo fronte), nič sa nepredĺži."""
    from vlaky.posta import spracovanie
    from vlaky.posta.spracovanie import _mail_names_place, _register_place, _station_key

    limit = getattr(spracovanie, "PLACE_SCAN_CHARS", 60_000)
    keys = {_station_key("Senec"), _station_key("Galanta")}
    row = "dňa 14.12.2026 od 22:00 – 04:00 hod. podľa harmonogramu prác č. 12 – 15 bez zmeny\n"
    text = row * (55_000 // len(row))
    assert len(text) < limit
    mail = SimpleNamespace(sender="vyluky.orba@zsr.example", subject="ROV 4120/2026",
                           body_text="Výluka podľa ROV 4120/2026 sa odkladá o týždeň, podrobnosti v prílohe.",
                           attachments=[SimpleNamespace(filename="harmonogram.txt", content_type="text/plain",
                                                        size=len(text), text=text)])
    t = time.perf_counter()
    assert not _mail_names_place(mail, keys)
    assert time.perf_counter() - t < 1.5
    # dlhší e-mail sa neoverí – úsek z registra sa nedoplní (inak by miesto v jeho zvyšku ostalo neprečítané)
    session.add(Closure(kind="uplna", from_station="Senec", to_station="Galanta", start_utc=utc(2026, 12, 4, 7),
                        end_utc=utc(2026, 12, 4, 13), status="overene", source="email", external_ref="ROV 4120/2026"))
    session.commit()
    long_text = "práce podľa harmonogramu bez zmeny v rovnakom čase " * (limit // 40)
    mail.attachments = [SimpleNamespace(filename="harmonogram.txt", content_type="text/plain", size=len(long_text),
                                        text=long_text)]
    t = time.perf_counter()
    data, reasons, warnings = _register_place(session, "vyluka_zmena", {"external_ref": "4120/2026", "kind": "uplna"},
                                              mail)
    assert time.perf_counter() - t < 1.5
    assert not data.get("from_station") and not reasons, data
    assert warnings and "nedá celý overiť" in warnings[0] and "príliš dlhý" in warnings[0], warnings


# ====================================================================== (3) staršia zmena len podľa čísla a rozdelenie
_ROV1315 = ("Dobrý deň,\n\núplná výluka v úseku Ružomberok – Liptovský Mikuláš v dňoch 23.11. – 27.11.2026 denne od "
            "08:00 do 14:00 hod. (ROV č. 1315/2026).\n\nOR Žilina\n")
_NOVSIA_ZMENA = ("Zmena ROV č. 1315/2026: výluka v úseku Ružomberok – Liptovský Mikuláš dňa 24.11.2026 od 08:00 do "
                 "18:00 hod.")


@pytest.mark.parametrize("older,newer,expect", [
    ("Zmena ROV č. 1315/2026: výluka dňa 24.11.2026 bude od 06:00 do 14:00 hod.", _NOVSIA_ZMENA,
     ("rozdelene", "nahradene", None)),
    ("Zmena ROV č. 1315/2026: výluka dňa 26.11.2026 bude od 06:00 do 14:00 hod.", _NOVSIA_ZMENA,
     ("rozdelene", "rozdelene", None)),
    ("Zmena ROV č. 1315/2026: výluka dňa 24.11.2026 bude od 06:00 do 14:00 hod.",
     "Výluka podľa ROV č. 1315/2026 v úseku Ružomberok – Liptovský Mikuláš dňa 24.11.2026 sa ruší.",
     (None, "rozdelene", "zrusene")),
], ids=["zmena_toho_isteho_dna", "zmena_ineho_dna", "zrusenie_toho_isteho_dna"])
def test_starsia_zmena_len_podla_cisla_nebrani_rozdeleniu(env, session, older, newer, expect):
    """Kontrola I2, problém 3: ROV 1315/2026 (z e-mailu OR Žilina) 23. – 27.11. denne 08:00 – 14:00. Staršia zmena
    len podľa čísla dostane úsek z registra a dočasný záznam pre plánovač, no rozdeleniu výluky novšou zmenou s úsekom
    nebráni (v 8bfb48d sa novšia zmena zapísala ako ďalšia výluka a staršia potom zmenila 24.11. – 06:00 – 08:00
    ostalo uzavreté, hoci novšia správa začína o 08:00). Zmena toho istého dňa je potom nahradená, zmena iného dňa sa
    zapíše. Zrušenie dňa na čakajúcu zmenu počká (najprv dispečer vybaví zmenu, potom zrušenie) – zrušený deň sa
    staršou zmenou znova nezaloží."""
    _register(env, session, "r.eml", "ROV c. 1315/2026 Ruzomberok - Liptovsky Mikulas", _ROV1315, ROV_ZA,
              "Mon, 09 Nov 2026 09:00:00 +0100", utc(2026, 11, 9, 8, 2))
    (old,) = _new_events(env, session, "b.eml", "ROV c. 1315/2026", "Dobrý deň,\n\n" + older + "\n\nOR Žilina\n",
                         ROV_ZA, "Mon, 16 Nov 2026 09:00:00 +0100", utc(2026, 11, 16, 8, 2))
    assert (old.data.get("from_station"), old.data.get("to_station")) == ("Ružomberok", "Liptovský Mikuláš"), old.data
    temp = session.scalar(select(Closure).where(Closure.source_event_id == old.id))
    assert temp is not None and temp.proposed_change_of is not None           # plánovač s ňou počíta
    (new,) = _new_events(env, session, "a.eml", "ROV c. 1315/2026", "Dobrý deň,\n\n" + newer + "\n\nOR Žilina\n",
                         ROV_ZA, "Mon, 16 Nov 2026 10:00:00 +0100", utc(2026, 11, 16, 9, 2))
    first, second, third = expect
    res = _confirm(session, new.id)
    if first is None:
        assert res.get("ok") is False and "čaká aj iná zmena" in str(res.get("error")), res
    else:
        assert res.get("ok") is True and res.get("action") == first, res
    res = _confirm(session, old.id)
    assert res.get("ok") is True and res.get("action") == second, res
    if third:
        res = _confirm(session, new.id)
        assert res.get("ok") is True and res.get("action") == third, res
    for d in (23, 25, 27):
        assert _closed_at(session, loc(2026, 11, d, 10, 30)), f"{d}.11. 10:30 má byť uzavreté"
    if third:                                                                 # 24.11. ŽSR zrušili
        assert not any(_closed_at(session, loc(2026, 11, 24, h, 30)) for h in (6, 10, 16))
        return
    assert _closed_at(session, loc(2026, 11, 24, 10, 30)) and _closed_at(session, loc(2026, 11, 24, 16, 30))
    assert not _closed_at(session, loc(2026, 11, 24, 6, 30)), "24.11. 06:00 – 08:00 novšia zmena neuvádza"
    assert _closed_at(session, loc(2026, 11, 26, 6, 30)) is (older.find("26.11.") >= 0)


# ====================================================================== (4) druh dokumentu (ROV, telegram)
_ROV5310 = ("Dobrý deň,\n\nnočná výluka v úseku Kysak – Prešov v noci 07./08.12.2026 od 22:00 do 04:00 hod. "
            "(ROV 5310/2026).\n\nOR Košice\n")


def test_telegram_s_rovnakym_cislom_ako_rov_usek_nedostane(env, session):
    """Kontrola I2, problém 4: „TLG 5310/2026 / Telegram č. 5310/2026: výluka sa odkladá o týždeň.“ – v registri je
    ROV 5310/2026 (Kysak – Prešov); čísla telegramov a ROV sú samostatné rady, úsek sa nedoplní (v 8bfb48d sa doplnil
    a poistka pre celý vozový park zmizla). Vlak Senec → Galanta preto nie je ZELENÝ."""
    _register(env, session, "r.eml", "ROV c. 5310/2026 Kysak - Presov", _ROV5310, ROV_KE,
              "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    (ev,) = _new_events(env, session, "z.eml", "TLG 5310/2026", "Telegram č. 5310/2026: výluka sa odkladá o týždeň.\n\n"
                        "OR Košice\n", ROV_KE, "Thu, 26 Nov 2026 09:40:00 +0100", utc(2026, 11, 26, 8, 42))
    assert not ev.data.get("from_station"), ev.data
    text = _warnings(ev)
    assert "TLG 5310/2026" in text and "ROV 5310/2026" in text and "iný dokument" in text, text
    r = _decide(session, "73101", "Senec", "Galanta", utc(2026, 12, 14, 22), utc(2026, 12, 14, 19))
    assert r.color != "ZELENÁ", r.summary


@pytest.mark.parametrize("ref,subject,body,filled", [
    ("5310/2026", "ROV 5310/2026 - odklad", "Výluka podľa ROV 5310/2026 sa odkladá o týždeň.", False),
    ("ROV č. 5310/2026", "ROV 5310/2026 - odklad", "Výluka podľa ROV 5310/2026 sa odkladá o týždeň.", True),
    ("ROV č. 5310/2026", "Odklad výluky", "Výluka č. 5310/2026 sa odkladá o týždeň.", True),
    ("TLG 5310/2026", "ROV 5310/2026 - odklad", "Výluka podľa ROV 5310/2026 sa odkladá o týždeň.", False),
], ids=["register_bez_druhu", "rov_rov", "email_bez_druhu", "tlg_rov"])
def test_druh_dokumentu_vyluky_v_registri(session, ref, subject, body, filled):
    """Výluka zadaná ručne len s číslom (bez druhu dokumentu) úsek nedá – nedá sa overiť, že ide o ten istý dokument;
    s druhom v čísle („ROV č. …“) áno, aj keď e-mail druh pri čísle neuvádza; telegram v registri ROV v e-maile nie je."""
    from vlaky.posta.spracovanie import _register_place

    session.add(Closure(kind="uplna", from_station="Kysak", to_station="Prešov", start_utc=utc(2026, 12, 7, 21),
                        end_utc=utc(2026, 12, 8, 3), status="overene", source="email", external_ref=ref))
    session.commit()
    mail = SimpleNamespace(sender="vyluky.orke@zsr.example", subject=subject, body_text=body, attachments=[])
    data, reasons, warnings = _register_place(session, "vyluka_zmena", {"external_ref": "5310/2026", "kind": "uplna"},
                                              mail)
    if filled:
        assert data.get("from_station") == "Kysak" and reasons, warnings
    else:
        assert not data.get("from_station") and not reasons, data
        assert warnings and "Úsek sa z registra nedoplnil" in warnings[0], warnings
        assert ("bez druhu dokumentu" if ref == "5310/2026" else "iný dokument") in warnings[0], warnings
