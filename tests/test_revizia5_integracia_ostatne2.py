"""5. revízia – integrácia, oprava po kontrole (strana registra): e-mail len s číslom ROV (bez úseku).

Od 6. revízie sa úsek z registra výluk podľa čísla ROV automaticky nedopĺňa (kontrola miesta v texte e-mailu nikdy
nepokryje všetky tvary – preposlaný či citovaný text, skratky, km polohy, prílohy): udalosť ostáva udalosťou bez miesta
(poistka pre celý vozový park a upozornenie DÔLEŽITÉ) a fronta ponúkne výluky s týmto číslom ako návrh (úsek, termín,
OR, druh dokumentu – :func:`vlaky.posta.spracovanie.register_candidates`). Testy overujú, že

- e-mail len s číslom ROV, ktorý uvádza iné miesto (aj v tvare, ktorý pravidlá neprečítali), úsek z registra nedostane
  a vlak v mieste z e-mailu nedostane VYPRAVIŤ ZELENÁ;
- staršia zmena len podľa čísla nebráni rozdeleniu výluky novšou zmenou; po výbere úseku dispečerom má novšia správa
  prednosť;
- návrh vo fronte uvádza druh dokumentu („TLG 5310/2026“ nie je „ROV 5310/2026“, výluka len s číslom bez druhu).

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from vlaky.models import Closure, Email, ExtractedEvent
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


def _candidates(session, ev) -> list:
    from vlaky.posta.spracovanie import register_candidates

    return register_candidates(session, ev)


def _closed_at(session, when: datetime) -> bool:
    from datetime import timedelta

    from vlaky.planovanie.planovac import closure_windows

    return any(w.start <= when < w.end for w in closure_windows(session, when - timedelta(hours=1),
                                                                 when + timedelta(hours=1)))


def _not_filled(session, new: list, *section: str) -> None:
    """Žiadna udalosť nedostala úsek z registra; udalosti bez úseku majú výluku z registra ako návrh."""
    for ev in new:
        assert (ev.validation or {}).get("field_source", {}).get("from_station") != "register", ev.validation
        if ev.kind in ("vyluka_nova", "vyluka_zmena", "vyluka_zrusenie") and not ev.data.get("from_station"):
            assert [x["from_station"] for x in _candidates(session, ev)] == [section[0]], ev.data


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
    """E-mail od OR Bratislava len s číslom ROV 4120/2026 (v registri Senec – Galanta) uvádza iné miesto: úsek sa
    z registra nedoplní, fronta ponúkne výluku ako návrh; vlak Nové Zámky → Šurany 11.12. o 10:00 (v termíne z e-mailu)
    nie je VYPRAVIŤ ZELENÁ."""
    _register(env, session, "r.eml", "ROV c. 4120/2026 Senec - Galanta", _ROV4120, ROV_BA,
              "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    new = _new_events(env, session, "z.eml", subject, body + "\n\nOR Bratislava\n", ROV_BA,
                      "Thu, 26 Nov 2026 09:40:00 +0100", utc(2026, 11, 26, 8, 42))
    assert new
    _not_filled(session, new, "Senec")
    r = _decide(session, "72101", "Nové Zámky", "Šurany", utc(2026, 12, 11, 9), utc(2026, 12, 11, 6))
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), r.summary


_PHRASINGS = [
    "Výluka podľa ROV 4120/2026 sa presúva do Popradu.",
    "Výluka podľa ROV 4120/2026 bude v Bratislave dňa 11.12.2026.",
    "Výluka podľa ROV 4120/2026 sa presúva do Spišskej Novej Vsi.",
    "Výluka podľa ROV 4120/2026 sa presúva do L. Mikuláša.",
    "Výluka podľa ROV 4120/2026 sa presúva, viď TÚ 2401.",
    "Výluka podľa ROV 4120/2026 sa rozširuje na Šurany-Komárno dňa 11.12.2026.",
    "Výluka podľa ROV 4120/2026 sa presúva na odbočku Kopčany.",
    "vyluka podla rov 4120/2026 sa presuva na 11.12.2026, nove zamky.",
    "Ďakujem za informáciu, výluka podľa ROV 4120/2026 sa presúva do Šurian.",
    "Výluka podľa ROV 4120/2026 sa odkladá o týždeň.",
    "Dobrý deň,\n\nvýluka podľa ROV 4120/2026 sa odkladá o týždeň.\n\nS pozdravom\nIng. Ján Novák\nOR Bratislava\n",
    "Výluka podľa ROV 4120/2026 sa odkladá, v Senci v rovnakom čase.",
    "VÝLUKA PODĽA ROV 4120/2026 SA ODKLADÁ O TÝŽDEŇ.",
    "Výluka podľa ROV 4120/2026 sa presúva na 11.12.2026. Kontakt: Ing. Martin Kováč, tel. 0900 000 000",
    "Vážení kolegovia,\nvýluka podľa ROV 4120/2026 sa odkladá na 18.12.2026, čas bez zmeny.\nDispečing OR Bratislava",
]


def test_usek_z_registra_sa_nedoplni_v_ziadnej_formulacii(env, session):
    """Formulácie e-mailu len s číslom ROV (iné miesto kdekoľvek v texte, ale aj dopravňa úseku z registra, podpis,
    meno za titulom): úsek z registra sa nedoplní v žiadnej – výluka je len návrhom vo fronte."""
    session.add(Closure(kind="uplna", from_station="Senec", to_station="Galanta", start_utc=utc(2026, 12, 4, 7),
                        end_utc=utc(2026, 12, 4, 13), status="overene", source="email", external_ref="ROV 4120/2026"))
    session.commit()
    for i, body in enumerate(_PHRASINGS):
        new = _new_events(env, session, f"z{i}.eml", "ROV 4120/2026", body, ROV_BA,
                          f"Thu, 26 Nov 2026 09:{i:02d}:00 +0100", utc(2026, 11, 26, 8, 2 + i))
        assert new, body
        _not_filled(session, new, "Senec")


# ====================================================================== (2) staršia zmena len podľa čísla a rozdelenie
_ROV1315 = ("Dobrý deň,\n\núplná výluka v úseku Ružomberok – Liptovský Mikuláš v dňoch 23.11. – 27.11.2026 denne od "
            "08:00 do 14:00 hod. (ROV č. 1315/2026).\n\nOR Žilina\n")
_NOVSIA_ZMENA = ("Zmena ROV č. 1315/2026: výluka v úseku Ružomberok – Liptovský Mikuláš dňa 24.11.2026 od 08:00 do "
                 "18:00 hod.")


@pytest.mark.parametrize("older,newer,expect", [
    ("Zmena ROV č. 1315/2026: výluka dňa 24.11.2026 bude od 06:00 do 14:00 hod.", _NOVSIA_ZMENA,
     ("rozdelene", "nahradene")),
    ("Zmena ROV č. 1315/2026: výluka dňa 26.11.2026 bude od 06:00 do 14:00 hod.", _NOVSIA_ZMENA,
     ("rozdelene", "rozdelene")),
], ids=["zmena_toho_isteho_dna", "zmena_ineho_dna"])
def test_starsia_zmena_len_podla_cisla_nebrani_rozdeleniu(env, session, older, newer, expect):
    """ROV 1315/2026 (z e-mailu OR Žilina) 23. – 27.11. denne 08:00 – 14:00. Staršia zmena len podľa čísla úsek nedostane
    (ani dočasný záznam), takže rozdeleniu výluky novšou zmenou s úsekom nebráni. Keď dispečer staršej zmene vyberie úsek
    z návrhu, zmena toho istého dňa je nahradená (novšia správa má prednosť), zmena iného dňa sa zapíše."""
    _register(env, session, "r.eml", "ROV c. 1315/2026 Ruzomberok - Liptovsky Mikulas", _ROV1315, ROV_ZA,
              "Mon, 09 Nov 2026 09:00:00 +0100", utc(2026, 11, 9, 8, 2))
    (old,) = _new_events(env, session, "b.eml", "ROV c. 1315/2026", "Dobrý deň,\n\n" + older + "\n\nOR Žilina\n",
                         ROV_ZA, "Mon, 16 Nov 2026 09:00:00 +0100", utc(2026, 11, 16, 8, 2))
    assert not old.data.get("from_station"), old.data
    assert session.scalar(select(Closure).where(Closure.source_event_id == old.id)) is None
    [cand] = _candidates(session, old)
    (new,) = _new_events(env, session, "a.eml", "ROV c. 1315/2026", "Dobrý deň,\n\n" + newer + "\n\nOR Žilina\n",
                         ROV_ZA, "Mon, 16 Nov 2026 10:00:00 +0100", utc(2026, 11, 16, 9, 2))
    first, second = expect
    res = _confirm(session, new.id)
    assert res.get("ok") is True and res.get("action") == first, res
    res = _confirm(session, old.id, from_station=cand["from_station"], to_station=cand["to_station"])
    assert res.get("ok") is True and res.get("action") == second, res
    for d in (23, 25, 27):
        assert _closed_at(session, loc(2026, 11, d, 10, 30)), f"{d}.11. 10:30 má byť uzavreté"
    assert _closed_at(session, loc(2026, 11, 24, 10, 30)) and _closed_at(session, loc(2026, 11, 24, 16, 30))
    assert not _closed_at(session, loc(2026, 11, 24, 6, 30)), "24.11. 06:00 – 08:00 novšia zmena neuvádza"
    assert _closed_at(session, loc(2026, 11, 26, 6, 30)) is (older.find("26.11.") >= 0)


# ====================================================================== (3) druh dokumentu (ROV, telegram) v návrhu
_ROV5310 = ("Dobrý deň,\n\nnočná výluka v úseku Kysak – Prešov v noci 07./08.12.2026 od 22:00 do 04:00 hod. "
            "(ROV 5310/2026).\n\nOR Košice\n")


def test_telegram_s_rovnakym_cislom_ako_rov_usek_nedostane(env, session):
    """„TLG 5310/2026 / Telegram č. 5310/2026: výluka sa odkladá o týždeň.“ – v registri je ROV 5310/2026 (Kysak –
    Prešov); čísla telegramov a ROV sú samostatné rady: návrh vo fronte to povie, úsek sa nedoplní a vlak Senec → Galanta
    nie je ZELENÝ."""
    _register(env, session, "r.eml", "ROV c. 5310/2026 Kysak - Presov", _ROV5310, ROV_KE,
              "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    (ev,) = _new_events(env, session, "z.eml", "TLG 5310/2026", "Telegram č. 5310/2026: výluka sa odkladá o týždeň.\n\n"
                        "OR Košice\n", ROV_KE, "Thu, 26 Nov 2026 09:40:00 +0100", utc(2026, 11, 26, 8, 42))
    assert not ev.data.get("from_station"), ev.data
    flags = " | ".join(f for x in _candidates(session, ev) for f in x["flags"])
    assert "TLG" in flags and "ROV" in flags and "iný dokument" in flags, flags
    r = _decide(session, "73101", "Senec", "Galanta", utc(2026, 12, 14, 22), utc(2026, 12, 14, 19))
    assert r.color != "ZELENÁ", r.summary


@pytest.mark.parametrize("ref,subject,body,flag", [
    ("5310/2026", "ROV 5310/2026 - odklad", "Výluka podľa ROV 5310/2026 sa odkladá o týždeň.", "sa nedá určiť"),
    ("ROV č. 5310/2026", "ROV 5310/2026 - odklad", "Výluka podľa ROV 5310/2026 sa odkladá o týždeň.", ""),
    ("ROV č. 5310/2026", "Odklad výluky", "Výluka č. 5310/2026 sa odkladá o týždeň.", ""),
    ("TLG 5310/2026", "ROV 5310/2026 - odklad", "Výluka podľa ROV 5310/2026 sa odkladá o týždeň.", "iný dokument"),
], ids=["register_bez_druhu", "rov_rov", "email_bez_druhu", "tlg_rov"])
def test_druh_dokumentu_vyluky_v_navrhu(session, ref, subject, body, flag):
    """Návrh vo fronte uvádza druh dokumentu: výluka zadaná ručne len s číslom (bez druhu) – druh sa nedá určiť;
    s druhom v čísle („ROV č. …“) bez upozornenia, aj keď e-mail druh pri čísle neuvádza; telegram v registri pri ROV
    v e-maile je iný dokument."""
    session.add(Closure(kind="uplna", from_station="Kysak", to_station="Prešov", start_utc=utc(2026, 12, 7, 21),
                        end_utc=utc(2026, 12, 8, 3), status="overene", source="email", external_ref=ref))
    mail = Email(message_id=f"<dok-{ref.replace(' ', '')}@zsr.example>", sender="vyluky.orke@zsr.example",
                 subject=subject, body_text=body, received_at=utc(2026, 11, 26, 8), verdict="ok", status="spracovany")
    session.add(mail)
    session.flush()
    ev = ExtractedEvent(email_id=mail.id, kind="vyluka_zmena", data={"external_ref": "5310/2026", "kind": "uplna"},
                        confidence=0.8, extractor="pravidla", status="navrh", needs_confirmation=True, validation={})
    session.add(ev)
    session.commit()
    [cand] = _candidates(session, ev)
    doc_flags = [f for f in cand["flags"] if "dokument" in f]
    if flag:
        assert any(flag in f for f in doc_flags), cand["flags"]
    else:
        assert not doc_flags, cand["flags"]
