"""6. revízia (strana FB – hromadná pošta): distribučné zoznamy nepotlačia prevádzkové oznámenie (FR3).

Hlavičky distribučného zoznamu (Precedence, List-Unsubscribe, List-Id), názov zoznamu (Bulletin, Spravodaj,
Newsletter) ani odosielateľ prepísaný zoznamom nikdy nepotlačia e-mail, ktorý obsahuje výluku s číslom
prevádzkového dokumentu ŽSR (ROV, SROV, telegram, POTR) a dátum či úsek: spracuje sa ako prevádzkové oznámenie
s poznámkou „overte“ a čaká na dispečera. Newsletter bez prevádzkového obsahu (bez čísla dokumentu) ostáva
neprevádzkovým e-mailom.

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from vlaky.models import Closure
from test_revizia3_terminy import events, utc
from test_revizia4_pravidla import _decide, _drop_at, _mail, _process, env  # noqa: F401 – fixture env

BODY = ("Dobrý deň,\n\núplná výluka v úseku Kysak – Prešov dňa 10.12.2026 od 08:00 do 14:00 hod. "
        "(ROV č. 5212/2026).\n\nOR Košice, oddelenie výluk\n")
LIST = {"Precedence": "list", "List-Id": "<{n}.lists.zsr.example>",
        "List-Unsubscribe": "<mailto:{n}-leave@lists.zsr.example>", "Reply-To": "vyluky.orke@zsr.example",
        "X-Original-From": "ZSR OR Kosice <vyluky.orke@zsr.example>"}
CASES = {
    "bulletin": ("ZSR OR Kosice via Bulletin <bulletin@lists.zsr.example>", "[Bulletin] ROV c. 5212/2026 Kysak - Presov",
                 "bulletin", LIST),
    "spravodaj": ("ZSR OR Kosice via Spravodaj <spravodaj@lists.zsr.example>",
                  "[Spravodaj] ROV c. 5212/2026 Kysak - Presov", "spravodaj", LIST),
    "newsletter": ("ZSR OR Kosice via Newsletter <newsletter@lists.zsr.example>",
                   "[Newsletter] Vyluka Kysak - Presov 10.12.2026 (ROV c. 5212/2026)", "newsletter", LIST),
    # prepísaný odosielateľ bez Reply-To a X-Original-From – rozhoduje obsah
    "bez_povodneho_odosielatela": ("ZSR Bulletin <bulletin@lists.zsr.example>",
                                   "[Bulletin] ROV c. 5212/2026 Kysak - Presov", "bulletin",
                                   {"Precedence": "bulk", "List-Unsubscribe": "<mailto:bulletin-off@lists.zsr.example>"}),
}


@pytest.mark.parametrize("tag", list(CASES))
def test_fr3_rov_cez_distribucny_zoznam_sa_spracuje(env, session, tag):
    """FR3: ROV OR Košice rozoslaný zoznamom (Bulletin, Spravodaj, Newsletter; odosielateľ prepísaný zoznamom) – výluka
    čaká na dispečera s poznámkou „overte“, vlak Kysak → Prešov 10.12. o 10:00 nedostane VYPRAVIŤ ZELENÁ (v 631f185
    „iné – newsletter“, žiadna výluka, ZELENÁ)."""
    sender, subject, name, headers = CASES[tag]
    headers = {k: v.format(n=name) for k, v in headers.items()}
    _drop_at(env, "l.eml", _mail(subject, BODY, f"list6-{tag}", sender, "Fri, 20 Nov 2026 07:00:00 +0100",
                                 headers=headers), utc(2026, 11, 20, 6))
    _process(session, utc(2026, 11, 20, 6, 2))
    evs = events(session)
    assert [e.kind for e in evs] == ["vyluka_nova"], [(e.kind, e.validation.get("notes")) for e in evs]
    (ev,) = evs
    assert ev.status == "navrh" and ev.needs_confirmation, ev.validation.get("confirmation_reasons")
    notes = " | ".join(ev.validation.get("notes") or [])
    assert "overte" in notes and "prevádzkov" in notes, notes
    assert session.scalar(select(Closure).where(Closure.source_event_id == ev.id)) is not None
    r = _decide(session, "75161", "Kysak", "Prešov", utc(2026, 12, 10, 9), utc(2026, 12, 10, 6))
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), r.summary


_NEWS = ("ŽSR Spravodaj 11/2026\n\nModernizácia pokračuje\nV decembri pokračujú práce v úseku Kysak – Prešov "
         "(ROV 5211/2026), výluky budú v dňoch 7.12. až 11.12.2026 vždy od 8:00 do 14:00 hod.\n\n"
         "Personálne zmeny\nNa OR Košice nastúpil nový vedúci oddelenia výluk.\n\n"
         "Odhlásiť odber: newsletter-off@zsr.example\n")
_NEWS_HEADERS = {"Precedence": "bulk", "List-Unsubscribe": "<mailto:newsletter-off@zsr.example>"}


def test_fr3_spravodaj_s_cislom_rov_a_terminom_sa_nepotlaci(env, session):
    """Spravodaj (newsletter@, Precedence: bulk, List-Unsubscribe) s výlukou „v úseku Kysak – Prešov (ROV 5211/2026)
    … v dňoch 7.12. až 11.12.2026“: e-mail s výlukou, číslom ROV a termínom sa nikdy nepotlačí – výluka čaká na
    dispečera (overte), vlak Kysak → Prešov 08.12. o 10:00 nie je VYPRAVIŤ ZELENÁ (v 631f185 „iné“ a ZELENÁ)."""
    _drop_at(env, "n.eml", _mail("ŽSR Spravodaj 11/2026", _NEWS, "news6-1", "ŽSR Spravodaj <newsletter@zsr.example>",
                                 "Thu, 19 Nov 2026 07:00:00 +0100", headers=_NEWS_HEADERS), utc(2026, 11, 19, 6))
    _process(session, utc(2026, 11, 19, 6, 2))
    evs = events(session)
    assert evs and all(e.kind == "vyluka_nova" and e.needs_confirmation for e in evs), [(e.kind, e.data) for e in evs]
    r = _decide(session, "75162", "Kysak", "Prešov", utc(2026, 12, 8, 9), utc(2026, 12, 8, 6))
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), r.summary


def test_fr3_spravodaj_bez_cisla_dokumentu_ostava_neprevadzkovy(env, session):
    """Kontrola: spravodaj bez čísla ROV, telegramu či POTR (len článok o modernizácii) ostáva hromadným e-mailom
    („iné“, žiadna výluka) – poistka platí len pre prevádzkový obsah."""
    body = ("ŽSR Spravodaj 12/2026\n\nModernizácia pokračuje\nV decembri pokračujú práce v úseku Kysak – Prešov, "
            "podrobnosti zverejníme.\n\nOdhlásiť odber: newsletter-off@zsr.example\n")
    _drop_at(env, "n.eml", _mail("ŽSR Spravodaj 12/2026", body, "news6-2", "ŽSR Spravodaj <newsletter@zsr.example>",
                                 "Thu, 19 Nov 2026 07:00:00 +0100", headers=_NEWS_HEADERS), utc(2026, 11, 19, 6))
    _process(session, utc(2026, 11, 19, 6, 2))
    assert [e.kind for e in events(session)] == ["ine"], [(e.kind, e.data) for e in events(session)]
    assert not list(session.scalars(select(Closure)))


@pytest.mark.parametrize("text,expected", [
    ("úplná výluka v úseku Kysak – Prešov dňa 10.12.2026 (ROV č. 5212/2026)", True),
    ("Telegram č. 88/2026: výluka 1. traťovej koľaje Kraľovany – Ružomberok od 23.11.2026", True),
    ("POTR 12/2026 – pomalá jazda v žst. Čierna nad Tisou", True),
    ("Výluky v decembri (ROV 5211/2026) budú zverejnené neskôr.", False),        # bez dátumu a úseku
    ("Spravodaj 11/2026: výluky v regióne, viac na webe 10.12.2026", False),    # bez čísla dokumentu
    ("Rozkaz o výluke č. 1301/2026 – Kysak – Košice", True),
], ids=["rov", "telegram", "potr", "bez_terminu", "bez_cisla", "rozkaz"])
def test_fr3_prevadzkovy_obsah(text, expected):
    from types import SimpleNamespace

    from vlaky.posta.spracovanie import _operational_content

    assert _operational_content(SimpleNamespace(subject="", body_text=text, attachments=[])) is expected
