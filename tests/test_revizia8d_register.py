"""Ôsma revízia, štvrté kolo – register výluk po nezávislom overení prevzatia výluky (držitelia namiesto prevzatia).

Kritérium: o každom čase rozhoduje najnovšia prijatá (nezamietnutá; aj čakajúca) správa, ktorá o ňom hovorí; úprava či
zrušenie výluky dispečerom v registri výluk je najnovšou správou o čase, ktorý zmenila – plánovač nesmie dať VYPRAVIŤ
ZELENÁ, kým ho taká správa drží zatvorený.

* Potvrdená udalosť, ktorej obsah register už má („bez zmeny“, overenie rovnakého obsahu, kópia zmeny z iného zoznamu
  adries), zapisovateľa výluky nemení – výluku len drží. Ak neskorší zápis staršej udalosti či zamietnutie zapisovateľa
  jej termín z registra odoberie, zapíše sa znova ako jej výluka; zamietnutie držiteľa zruší len to, čo sa podľa neho
  zapísalo. Rovnaké opakované oznámenie výluky (ROV znova) je ako doteraz jej novším zapisovateľom.
* Zamietnutie novšej udalosti zapíše nahradenú staršiu udalosť znova s novým plánom (zrušenie jedného dňa ostane
  zrušením jedného dňa, nie celej výluky).
* Úprava dispečera v registri výluk po spracovaní e-mailu (podľa auditu /vyluky) zastaví raz každé potvrdenie, ktoré
  by čas pridaný úpravou odobralo (aj rozdelenie, zrušenie, staršie udalosti); zamietnutie udalosti upravenú výluku
  ponechá.
* Záznam udalosti, ktorú dispečer zamietol, je tiež výlukou s rovnakým číslom (presun iného dňa je ďalšia výluka).
* Texty fronty pri výluke do odvolania uvádzajú „od … do odvolania“; výsledok uvádza skutočnú príčinu zrušenia.

Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from vlaky.cas import set_fake_now
from vlaky.models import Closure, ExtractedEvent
from test_revizia7_odobratie_casu import _live, env, loc, utc  # noqa: F401 – env
from test_revizia8_register import (CSUBJ, SEC, SUBJ, ZSUBJ, _T, _body, _clock, _closed, _confirm, _next, _one,  # noqa
                                    _send)
from test_revizia8b_register import _reject
from test_web_spolocne import client, login_as, web_app  # noqa: F401 – fixtures

STALE = "sa od spracovania e-mailu zmenila"


def _rov(term: str) -> str:
    return _body(f"úplná výluka v úseku Ružomberok – Liptovský Mikuláš {term} (ROV č. 2331/2026).")


ROV_A = _rov("dňa 23.11.2026 od 08:00 do 14:00 hod.")
ROV_B = _rov("v dňoch 23.11. – 25.11.2026 denne od 08:00 do 14:00 hod.")
ROV_E = _rov("dňa 23.11.2026 od 08:00 do 14:00 hod. a dňa 25.11.2026 od 08:00 do 14:00 hod.")
ROV_SUVISLY = _rov("od 23.11.2026 08:00 do 25.11.2026 14:00 hod.")
ROV_NOCI = _rov("v nociach 23./24.11. – 26./27.11.2026 vždy od 22:00 do 04:00 hod.")
ROV_ODVOLANIE = _rov("od 23.11.2026 08:00 hod. do odvolania")
ZRUSENIE_24 = _body(f"{SEC} dňa 24.11.2026 sa ruší.")
DOPLNENIE_25 = _body(f"{SEC} sa dopĺňa o ďalší termín 25.11.2026 od 08:00 do 14:00 hod.")
PRESUN_23_25 = _body(f"{SEC} sa presúva z 23.11.2026 na 25.11.2026, od 08:00 do 14:00 hod.")
SKRATENIE_23 = _body(f"{SEC} sa skracuje, dňa 23.11.2026 bude od 08:00 do 10:00 hod.")
TECH = re.compile(r"\b(?:0[1-9]|[12]\d|3[01])\.12\.")       # technický koniec výluky do odvolania leží v decembri
_NAMED_TECH = re.compile(r"\(?\s*technický koniec [^)]*\)?")  # výslovne označený technický koniec (nie koniec výluky)


def _cycle(env, session) -> None:
    from vlaky.posta.spracovanie import process_mailbox

    t = _next()
    set_fake_now(t)
    process_mailbox(session, now=t)
    session.commit()


def _main(session) -> list[Closure]:
    return [c for c in _live(session) if c.proposed_change_of is None]


def _uprav(session, c: Closure, **values) -> None:
    """Úprava výluky v registri výluk ako POST /vyluky/<id>/upravit: nové hodnoty, verzia + 1 a záznam auditu
    „closure_update“ so zmenami – zapisovateľ výluky ostáva."""
    from vlaky.audit import audit
    from vlaky.web.views_evidencia import _diff

    set_fake_now(_next())
    changes = _diff(c, values)
    for k, v in values.items():
        setattr(c, k, v)
    c.version = (c.version or 1) + 1
    audit(session, "dispecer1", "closure_update", "closure", c.id, version=c.version, changes=changes)
    session.commit()


def _uprav_priamo(session, c: Closure, **values) -> None:
    """Zmena výluky mimo /vyluky (bez záznamu auditu): nové hodnoty a verzia + 1."""
    set_fake_now(_next())
    for k, v in values.items():
        setattr(c, k, v)
    c.version = (c.version or 1) + 1
    session.commit()


def _kryte(session, *when: int) -> bool:
    """Niektorá platná výluka v registri (bez dočasných záznamov) má okno, ktoré obsahuje miestny čas ``when``."""
    from vlaky.posta.spracovanie import _closure_pieces

    t = loc(*when)
    return any(a <= t < b for c in _main(session) for a, b in _closure_pieces(c))


def _rollback_text(ev: ExtractedEvent) -> str:
    return str(((ev.validation or {}).get("rejection") or {}).get("rollback"))


def _applied(session, ev: ExtractedEvent) -> dict:
    return (session.get(ExtractedEvent, ev.id).validation or {}).get("applied") or {}


# ====================================================================== opätovný zápis nahradeného zrušenia
@pytest.mark.parametrize("newer", ["dlhsie_opakovanie", "predlzenie"])
def test_zamietnutie_novsieho_zapisovatela_zrusi_znova_len_jeden_den(env, session, newer):
    """Overený ROV 23. – 25.11. denne; čaká „dňa 24.11. sa ruší“; potvrdí sa novší zápis výluky (dlhší ROV či predĺženie
    do 26.11.) a potom zrušenie („nahradené“). Dispečer novší zápis zamietne: zrušenie sa zapíše znova s novým plánom –
    zruší len 24.11. (nie celú výluku); 23.11. a 25.11. ostávajú zatvorené."""
    _confirm(session, _one(env, session, SUBJ, ROV_B))
    cancel = _one(env, session, CSUBJ, ZRUSENIE_24)
    body = _rov("v dňoch 23.11. – 26.11.2026 denne od 08:00 do 14:00 hod.") if newer == "dlhsie_opakovanie" else \
        _body(f"{SEC} sa predlžuje do 26.11.2026 14:00 hod.")
    writer = _one(env, session, SUBJ if newer == "dlhsie_opakovanie" else ZSUBJ, body)
    assert _confirm(session, writer).get("action") == "aktualizovane"
    _cycle(env, session)
    assert _confirm(session, cancel).get("action") == "nahradene"
    _cycle(env, session)
    rej = _reject(session, writer)
    assert f"udalosť #{cancel.id} zapísaná znova" in _rollback_text(rej), _rollback_text(rej)
    session.refresh(cancel)
    assert ((cancel.validation or {}).get("apply_result") or {}).get("action") == "rozdelene"
    _cycle(env, session)
    assert _closed(session, 2026, 11, 23, 10) and _closed(session, 2026, 11, 25, 10)
    assert not _closed(session, 2026, 11, 24, 10)


def test_zlyhany_opatovny_zapis_zrusenia_sa_po_potvrdeni_zapise_ako_prvy_zapis(env, session, monkeypatch):
    """Ako vyššie, opätovný zápis zrušenia zlyhá (udalosť ostane vo fronte ako potvrdená so zlyhaným zápisom); jej
    ďalšie potvrdenie je prvým zápisom – zruší len 24.11., nie celú výluku."""
    from vlaky.posta import spracovanie as sp

    _confirm(session, _one(env, session, SUBJ, ROV_B))
    cancel = _one(env, session, CSUBJ, ZRUSENIE_24)
    longer = _one(env, session, SUBJ, _rov("v dňoch 23.11. – 26.11.2026 denne od 08:00 do 14:00 hod."))
    _confirm(session, longer)
    _cycle(env, session)
    _confirm(session, cancel)
    real = sp._apply_closure_cancel

    def boom(*a, **k):
        raise RuntimeError("umelá chyba")

    monkeypatch.setattr(sp, "_apply_closure_cancel", boom)
    rej = _reject(session, longer)
    monkeypatch.setattr(sp, "_apply_closure_cancel", real)
    assert f"udalosť #{cancel.id} sa nedala zapísať znova" in _rollback_text(rej)
    session.refresh(cancel)
    assert sp.apply_failed(cancel) and "applied" not in (cancel.validation or {})
    assert _confirm(session, cancel).get("action") == "rozdelene"
    _cycle(env, session)
    assert _closed(session, 2026, 11, 23, 10) and _closed(session, 2026, 11, 25, 10)


@pytest.mark.parametrize("variant", ["noci", "do_odvolania"])
def test_zamietnutie_rovnakeho_opakovania_neodomkne_ostatne_dni(env, session, variant):
    """Overený ROV (noci 23./24. – 26./27.11., alebo od 23.11. 08:00 do odvolania); čaká zrušenie jednej noci, alebo
    „ruší sa od 25.11. 12:00“; rovnaký ROV príde znova (zapíše sa automaticky – novší zapisovateľ). Potvrdí sa zrušenie
    (opakovanie je novšie – jeho termín ostáva, zrušenie sa nahradí), dispečer opakovanie zamietne ako duplicitu: zrušenie
    sa zapíše znova s novým plánom – zrušená je len uvedená noc či čas od 25.11. 12:00, nie celá výluka."""
    if variant == "noci":
        first, cancel_body = ROV_NOCI, _body(f"{SEC} v noci z 24.11. na 25.11.2026 sa ruší.")
        closed, opened = [(2026, 11, 23, 23), (2026, 11, 26, 23)], [(2026, 11, 24, 23)]
    else:
        first, cancel_body = ROV_ODVOLANIE, _body(f"{SEC} vyhlásená do odvolania sa ruší od 25.11.2026 12:00 hod.")
        closed, opened = [(2026, 11, 23, 10), (2026, 11, 24, 10)], [(2026, 11, 26, 10)]
    _confirm(session, _one(env, session, SUBJ, first))
    cancel = _one(env, session, CSUBJ, cancel_body)
    again = _one(env, session, SUBJ, first)
    assert again.status == "aplikovane" and again.confirmed_at is None
    res = _confirm(session, cancel)
    assert res.get("action") in ("nahradene", "rozdelene"), res
    _cycle(env, session)
    assert all(_closed(session, *w) for w in closed + opened)      # opakovanie (novšie) drží celý termín
    rej = _reject(session, again)
    if res.get("action") == "nahradene":
        assert f"udalosť #{cancel.id} zapísaná znova" in _rollback_text(rej), _rollback_text(rej)
    _cycle(env, session)
    assert all(_closed(session, *w) for w in closed)
    assert not any(_closed(session, *w) for w in opened)


def test_vratenie_do_fronty_uvadza_spravny_smer(env, session, monkeypatch):
    """Automaticky zapisovaná udalosť A sa zapíše až po novšej B („nahradená“); dispečer B zamietne: A sa vráti do
    fronty s dôvodom „túto udalosť nahradila udalosť #B, ktorú dispečer zamietol“."""
    from pathlib import Path

    from email.utils import format_datetime

    from vlaky.posta import spracovanie as sp
    from test_revizia7_odobratie_casu import _raw

    _one(env, session, SUBJ, ROV_A)
    t1 = _next()
    (Path(env.mail_dir) / "ma.eml").write_bytes(_raw(SUBJ, _rov("dňa 23.11.2026 od 08:00 do 16:00 hod."), "ma",
                                                     format_datetime(t1)))
    t2 = _next()
    (Path(env.mail_dir) / "mb.eml").write_bytes(_raw(SUBJ, _rov("dňa 23.11.2026 od 08:00 do 18:00 hod."), "mb",
                                                     format_datetime(t2)))
    real, skipped = sp._auto_apply, []

    def flaky(session_, ev, summary, descr):
        if not skipped:
            skipped.append(ev.id)            # agent sa zastavil pred automatickým zápisom A
            return None
        return real(session_, ev, summary, descr)

    monkeypatch.setattr(sp, "_auto_apply", flaky)
    set_fake_now(t2)
    sp.process_mailbox(session, now=t2)
    session.commit()
    monkeypatch.setattr(sp, "_auto_apply", real)
    a = session.get(ExtractedEvent, skipped[0])
    b = [e for e in session.query(ExtractedEvent).order_by(ExtractedEvent.id) if e.id > a.id][0]
    _cycle(env, session)                     # dokončenie A: „nahradené“ (výluku zapísala B)
    session.refresh(a)
    assert ((a.validation or {}).get("applied") or {}).get("action") == "nahradene"
    _reject(session, b)
    session.refresh(a)
    why = [r for r in (a.validation or {}).get("confirmation_reasons") or [] if f"#{b.id}" in r]
    assert why and why[0].startswith(f"túto udalosť nahradila udalosť #{b.id}, ktorú dispečer zamietol"), why
    assert a.status == "navrh" and a.needs_confirmation


def test_text_zamietnutia_nehovori_zapisana_znova_ked_sa_nic_nezapisalo(env, session):
    """ROV 23.11. 08 – 14 (automaticky); čaká kratšie opakovanie 08 – 12; dlhšie 08 – 16 sa zapíše; kratšie potvrdené
    („nahradené“); predĺženie do 24.11. sa potvrdí (najnovší zapisovateľ); dispečer zamietne dlhšie opakovanie: kratšie
    sa znova nezapíše (výluku zapísala novšia udalosť) – text návratu to tak aj hovorí."""
    _one(env, session, SUBJ, ROV_A)
    shorter = _one(env, session, SUBJ, _rov("dňa 23.11.2026 od 08:00 do 12:00 hod."))
    longer = _one(env, session, SUBJ, _rov("dňa 23.11.2026 od 08:00 do 16:00 hod."))
    assert _confirm(session, shorter).get("action") == "nahradene"
    _confirm(session, _one(env, session, ZSUBJ, _body(f"{SEC} sa predlžuje do 24.11.2026 14:00 hod.")))
    rej = _reject(session, longer)
    text = _rollback_text(rej)
    assert f"udalosť #{shorter.id} zapísaná znova" not in text, text
    assert f"udalosť #{shorter.id} sa znova nezapísala" in text, text


# ====================================================================== držitelia namiesto prevzatia
def test_zamietnutie_zrusenia_po_doplneni_casti_vrati_rozdelenie(env, session):
    """Overený ROV 23. – 25.11.; potvrdené zrušenie 24.11. (rozdelenie); potvrdené doplnenie 25.11. („bez zmeny“ – časť
    len drží). Dispečer zamietne zrušenie: rozdelenie sa vráti, 24.11. je znova zatvorený; zamietnutie doplnenia potom
    nič nezruší – platí ROV."""
    _confirm(session, _one(env, session, SUBJ, ROV_B))
    cancel = _one(env, session, CSUBJ, ZRUSENIE_24)
    assert _confirm(session, cancel).get("action") == "rozdelene"
    _cycle(env, session)
    add = _one(env, session, ZSUBJ, DOPLNENIE_25)
    res = _confirm(session, add)
    assert res.get("action") == "bez_zmeny" and res.get("held"), res
    assert all(c.source_event_id != add.id for c in _live(session))           # zapisovateľ sa nemení
    _cycle(env, session)
    assert "rozdelenie výluky vrátené" in _rollback_text(_reject(session, cancel))
    _cycle(env, session)
    assert _closed(session, 2026, 11, 24, 10)
    _reject(session, add)
    _cycle(env, session)
    assert all(_closed(session, 2026, 11, d, 10) for d in (23, 24, 25))


def test_zamietnutie_zmeny_a_jej_kopie_vrati_termin_rov(env, session):
    """Overený ROV 23. – 25.11.; „dňa 24.11. bude 10 – 16“ potvrdená (rozdelenie), jej kópia z iného zoznamu adries
    potvrdená (rovnaký obsah – výluku len drží). Dispečer zamietne zmenu a potom kópiu: platí ROV, 24.11. 09:00 aj
    12:00 sú zatvorené."""
    _confirm(session, _one(env, session, SUBJ, ROV_B))
    change = _body(f"{SEC} sa mení: dňa 24.11.2026 bude od 10:00 do 16:00 hod.")
    t1 = _one(env, session, ZSUBJ, change)
    assert _confirm(session, t1).get("action") == "rozdelene"
    _cycle(env, session)
    t2 = _one(env, session, ZSUBJ, change)
    res = _confirm(session, t2)
    assert res.get("held"), res
    _cycle(env, session)
    _reject(session, t1)
    _cycle(env, session)
    assert _closed(session, 2026, 11, 24, 15)                  # kópia (prijatá) drží 24.11. 10 – 16
    _reject(session, t2)
    _cycle(env, session)
    assert _closed(session, 2026, 11, 24, 9) and _closed(session, 2026, 11, 24, 12)


@pytest.mark.parametrize("older", ["den", "dlhsie_opakovanie", "predlzenie"])
def test_novsia_zmena_jedneho_dna_bez_zmeny_nerozhoduje_o_inych_dnoch(env, session, older):
    """Overený súvislý ROV 23.11. 08:00 – 25.11. 14:00; čaká staršia správa T (zmena 23.11. na 06 – 12, dlhší ROV či
    predĺženie do 26.11. 14:00) a novšia S „dňa 24.11. bude 08 – 14“ (výluka ju má). Potvrdí sa S („bez zmeny“ – výluku
    len drží), potom T: T je najnovšou správou o svojich časoch – zapíše sa."""
    _confirm(session, _one(env, session, SUBJ, ROV_SUVISLY))
    body, subj, when = {
        "den": (_body(f"{SEC} sa mení: dňa 23.11.2026 bude od 06:00 do 12:00 hod."), ZSUBJ, (2026, 11, 23, 6, 30)),
        "dlhsie_opakovanie": (_rov("od 23.11.2026 08:00 do 26.11.2026 14:00 hod."), SUBJ, (2026, 11, 25, 17)),
        "predlzenie": (_body(f"{SEC} sa predlžuje do 26.11.2026 14:00 hod."), ZSUBJ, (2026, 11, 25, 17)),
    }[older]
    t = _one(env, session, subj, body)
    s = _one(env, session, ZSUBJ, _body(f"{SEC} sa mení: dňa 24.11.2026 bude od 08:00 do 14:00 hod."))
    assert _confirm(session, s).get("action") == "bez_zmeny"
    _cycle(env, session)
    assert _confirm(session, t).get("action") != "nahradene"
    _cycle(env, session)
    assert _closed(session, *when)


def test_zamietnutie_presunu_po_doplneni_vrati_den_rov_a_ponecha_doplnenie(env, session):
    """Overený ROV 23.11.; potvrdený presun na 25.11.; potvrdené doplnenie 25.11. („bez zmeny“ – drží). Dispečer
    zamietne presun: 23.11. sa vráti (ROV) a 25.11. ostáva podľa doplnenia (zapíše sa znova ako jeho výluka)."""
    _confirm(session, _one(env, session, SUBJ, ROV_A))
    move = _one(env, session, ZSUBJ, PRESUN_23_25)
    add = _one(env, session, ZSUBJ, DOPLNENIE_25)
    assert _confirm(session, move).get("action") == "aktualizovane"
    _cycle(env, session)
    assert _confirm(session, add).get("action") == "bez_zmeny"
    _cycle(env, session)
    text = _rollback_text(_reject(session, move))
    assert "pôvodný stav obnovený" in text and f"udalosť #{add.id}" in text, text
    _cycle(env, session)
    assert _closed(session, 2026, 11, 23, 10) and _closed(session, 2026, 11, 25, 10)
    assert any(c.source_event_id == add.id for c in _live(session))


def test_starsie_zrusenie_ineho_dna_nezastavi_novsie_doplnenie(env, session):
    """ROV 23. – 25.11. (automaticky); čaká zrušenie 24.11. a doplnenie 25.11. Potvrdí sa doplnenie („bez zmeny“),
    potom zrušenie: doplnenie o 24.11. nehovorí – zrušenie sa zapíše (24.11. je voľný) a text nehovorí o doplnení."""
    _one(env, session, SUBJ, ROV_B)
    cancel = _one(env, session, CSUBJ, ZRUSENIE_24)
    add = _one(env, session, ZSUBJ, DOPLNENIE_25)
    assert _confirm(session, add).get("action") == "bez_zmeny"
    res = _confirm(session, cancel)
    assert res.get("action") == "rozdelene", res
    assert f"#{add.id}" not in str(res.get("note")), res.get("note")
    _cycle(env, session)
    assert not _kryte(session, 2026, 11, 24, 10)
    assert _closed(session, 2026, 11, 23, 10) and _closed(session, 2026, 11, 25, 10)


def test_potvrdenie_bez_zmeny_neskryje_automaticky_zapisany_rov(env, session):
    """ROV 23. – 25.11. zapísaný automaticky (neoverený); potvrdené doplnenie 25.11. („bez zmeny“): ROV ostáva medzi
    automaticky zapísanými na overenie (fronta, počítadlá, odovzdávka) – jeho výluka je stále „návrh“."""
    from vlaky.agent.odovzdavka import build_report
    from vlaky.agent.zdravie import queue_counts
    from vlaky.web.data import auto_applied_unconfirmed

    rov = _one(env, session, SUBJ, ROV_B)
    _confirm(session, _one(env, session, ZSUBJ, DOPLNENIE_25))
    (c,) = _main(session)
    assert c.status == "navrh" and c.source_event_id == rov.id
    assert queue_counts(session)["auto"] == 1
    assert [ev.id for ev, _c in auto_applied_unconfirmed(session)] == [rov.id]
    _content, data = build_report(session, datetime.now(timezone.utc))
    assert data["auto_applied"] == 1


def test_tri_kopie_presunu_zamietnute_v_poradi_drzia_termin(env, session):
    """Overený ROV 23.11.; ten istý presun na 25.11. z troch zoznamov adries – prvý sa zapíše, ďalšie výluku len držia.
    Dispečer ich zamieta postupne: kým je niektorá kópia prijatá, 25.11. je zatvorený; nakoniec platí ROV (23.11.)."""
    _confirm(session, _one(env, session, SUBJ, ROV_A))
    copies = []
    for _i in range(3):
        copies.append(_one(env, session, ZSUBJ, PRESUN_23_25))
    results = []
    for c in copies:
        results.append(_confirm(session, c))
        _cycle(env, session)
    assert all(r.get("held") for r in results[1:]), results
    assert {x.source_event_id for x in _main(session)} == {copies[0].id}       # zapisovateľ ostáva prvá kópia
    for n, c in enumerate(copies):
        _reject(session, c)
        _cycle(env, session)
        if n < 2:
            assert _closed(session, 2026, 11, 25, 10), n
    assert _closed(session, 2026, 11, 23, 10)


def test_zamietnutie_overeneho_rov_ktory_drzi_doplnenie_zapise_termin_doplnenia(env, session):
    """Automaticky zapísaný ROV 23.11. dispečer overí; potvrdené doplnenie 23.11. 08 – 14 („bez zmeny“) výluku drží.
    Dispečer ROV zamietne: výluka zamietnutého ROV sa zruší a termín doplnenia (prijatá správa) sa zapíše znova ako jeho
    výluka – neostane záznam zamietnutej udalosti (jeho neskoršia úprava v registri by sa inak počítala k nej)."""
    rov = _one(env, session, SUBJ, ROV_A)
    _confirm(session, rov)
    add = _one(env, session, ZSUBJ, _body(f"{SEC} sa dopĺňa o ďalší termín 23.11.2026 od 08:00 do 14:00 hod."))
    res = _confirm(session, add)
    assert res.get("action") == "bez_zmeny" and res.get("held"), res
    _cycle(env, session)
    text = _rollback_text(_reject(session, rov))
    assert "zapísala ju zamietnutá udalosť" in text and f"udalosť #{add.id} výluku držala" in text, text
    _cycle(env, session)
    assert [c.source_event_id for c in _main(session)] == [add.id]
    assert _closed(session, 2026, 11, 23, 10)


def test_presun_s_inym_povodnym_dnom_neprepise_termin_vyluky(env, session):
    """Overený ROV 23.11.; „presúva sa z 24.11. na 26.11.“ (deň, ktorý výluka nemá) a „presúva sa z 23.11. na 25.11.“.
    Prvý presun výluke 23.11. termín nenahradí (nový termín je ďalšia výluka), druhý presunie 23.11. na 25.11. –
    26.11. ostáva zatvorený podľa prvého presunu."""
    _confirm(session, _one(env, session, SUBJ, ROV_A))
    other = _one(env, session, ZSUBJ, _body(f"{SEC} sa presúva z 24.11.2026 na 26.11.2026, od 08:00 do 14:00 hod."))
    move = _one(env, session, ZSUBJ, PRESUN_23_25)
    res = _confirm(session, other)
    assert res.get("action") == "vytvorene", res
    _cycle(env, session)
    _confirm(session, move)
    _cycle(env, session)
    assert _closed(session, 2026, 11, 26, 10) and _closed(session, 2026, 11, 25, 10)
    assert not _kryte(session, 2026, 11, 23, 10)


# ====================================================================== záznam zamietnutej udalosti je výluka s číslom
def test_presun_dna_zamietnuteho_terminu_neprepise_iny_termin_rov(env, session):
    """ROV s termínmi 23.11. a 25.11. (dve udalosti); dispečer zamietne termín 25.11.; „presúva sa z 25.11. na 26.11.“
    sa netýka termínu 23.11. – ten ostáva, 26.11. je ďalšia výluka."""
    e23, e25 = _send(env, session, SUBJ, ROV_E)
    _reject(session, e25)
    _cycle(env, session)
    move = _one(env, session, ZSUBJ, _body(f"{SEC} sa presúva z 25.11.2026 na 26.11.2026, od 08:00 do 14:00 hod."))
    res = _confirm(session, move)
    assert res.get("action") == "vytvorene", res
    _cycle(env, session)
    assert _closed(session, 2026, 11, 23, 10) and _closed(session, 2026, 11, 26, 10)


@pytest.mark.parametrize("other", ["druhy_rov", "rucna_vyluka"])
def test_presun_po_zamietnutom_rov_neprepise_inu_vyluku_s_cislom(env, session, other):
    """ROV 23.11. dispečer zamietne; výluku s rovnakým číslom má druhý ROV (27.11.) alebo ju dispečer zadal ručne
    (26.11.). Presun iného dňa („z 24.11. na 28.11.“) ju nenahradí – nový termín je ďalšia výluka."""
    rov = _one(env, session, SUBJ, ROV_A)
    _reject(session, rov)
    _cycle(env, session)
    if other == "druhy_rov":
        _one(env, session, SUBJ, _rov("dňa 27.11.2026 od 08:00 do 14:00 hod."))
        keep = (2026, 11, 27, 10)
    else:
        session.add(Closure(kind="uplna", from_station="Ružomberok", to_station="Liptovský Mikuláš", line_ref="",
                            track="", start_utc=loc(2026, 11, 26, 8), end_utc=loc(2026, 11, 26, 14), daily_from="",
                            daily_to="", electric_only=False, speed_kmh=None, external_ref="ROV č. 2331/2026",
                            until_revoked=False, status="overene", source="manual"))
        session.commit()
        keep = (2026, 11, 26, 10)
    move = _one(env, session, ZSUBJ, _body(f"{SEC} sa presúva z 24.11.2026 na 28.11.2026, od 08:00 do 14:00 hod."))
    _confirm(session, move)
    _cycle(env, session)
    assert _closed(session, *keep) and _closed(session, 2026, 11, 28, 10)


# ====================================================================== úprava dispečera v registri výluk
@pytest.mark.parametrize("edit", ["audit", "priamo"])
def test_uprava_v_registri_po_potvrdeni_bez_zmeny_zastavi_starsi_presun(env, session, edit):
    """Overený ROV 23.11. 08 – 14; čaká doplnenie 23.11. 08 – 12 (bez zmeny) a presun na 25.11.; dispečer v registri
    predĺži výluku do 16:00; doplnenie sa potvrdí (výluku len drží), potom presun: prvé potvrdenie sa nezapíše (nové
    upozornenie), 23.11. 15:00 ostáva zatvorený; druhé potvrdenie sa zapíše."""
    _confirm(session, _one(env, session, SUBJ, ROV_A))
    add = _one(env, session, ZSUBJ, _body(f"{SEC} sa dopĺňa o ďalší termín 23.11.2026 od 08:00 do 12:00 hod."))
    move = _one(env, session, ZSUBJ, PRESUN_23_25)
    (c,) = _main(session)
    (_uprav if edit == "audit" else _uprav_priamo)(session, c, end_utc=loc(2026, 11, 23, 16))
    _cycle(env, session)
    assert _confirm(session, add).get("action") == "bez_zmeny"
    res = _confirm(session, move)
    assert res.get("ok") is False and STALE in str(res.get("error")), res
    assert _closed(session, 2026, 11, 23, 15)
    assert _confirm(session, move).get("ok") is True


def test_uprava_v_registri_zastavi_staru_udalost_bez_verzie_stavu(env, session):
    """Udalosť spracovaná predchádzajúcou verziou (stav výluky vo fronte bez zapisovateľa, verzie a auditu); dispečer
    výluku potom predĺži v registri: staršie skrátenie sa prvýkrát nezapíše."""
    _confirm(session, _one(env, session, SUBJ, ROV_B))
    short = _one(env, session, ZSUBJ, _body(f"{SEC} sa skracuje, bude v dňoch 23.11. – 24.11.2026 denne od 08:00 do "
                                            "14:00 hod."))
    e = session.get(ExtractedEvent, short.id)
    val = dict(e.validation or {})
    val["register_seen"] = {k: v for k, v in (val.get("register_seen") or {}).items() if k in ("closure", "fields")}
    e.validation = val
    session.commit()
    (c,) = _main(session)
    _uprav(session, c, end_utc=loc(2026, 11, 26, 14))
    _cycle(env, session)
    res = _confirm(session, short)
    assert res.get("ok") is False and STALE in str(res.get("error")), res
    assert _closed(session, 2026, 11, 26, 10)


def test_uprava_v_registri_zastavi_zrusenie_bez_stavu_vo_fronte(env, session):
    """Zrušenie celej výluky spracované predchádzajúcou verziou (bez stavu výluky vo fronte); dispečer výluku potom
    predĺži v registri: zrušenie sa prvýkrát nezapíše, 26.11. ostáva."""
    _confirm(session, _one(env, session, SUBJ, ROV_B))
    cancel = _one(env, session, CSUBJ, _body(f"{SEC} sa ruší."))
    e = session.get(ExtractedEvent, cancel.id)
    val = dict(e.validation or {})
    val.pop("register_seen", None)
    e.validation = val
    session.commit()
    (c,) = _main(session)
    _uprav(session, c, end_utc=loc(2026, 11, 26, 14))
    _cycle(env, session)
    res = _confirm(session, cancel)
    assert res.get("ok") is False and STALE in str(res.get("error")), res
    assert _closed(session, 2026, 11, 26, 10)


def test_uprava_v_registri_zastavi_druhu_kopiu_skratenia(env, session):
    """Overený ROV 23.11. 08 – 14; skrátenie „dňa 23.11. bude 08 – 10“ príde dvakrát. Prvá kópia sa potvrdí, dispečer
    potom výluku v registri predĺži do 16:00; druhá kópia (zapisovateľom výluky je už prvá) sa prvýkrát nezapíše."""
    _confirm(session, _one(env, session, SUBJ, ROV_A))
    s1, s2 = _one(env, session, ZSUBJ, SKRATENIE_23), _one(env, session, ZSUBJ, SKRATENIE_23)
    _confirm(session, s1)
    _cycle(env, session)
    (c,) = _main(session)
    _uprav(session, c, end_utc=loc(2026, 11, 23, 16))
    _cycle(env, session)
    res = _confirm(session, s2)
    assert res.get("ok") is False and STALE in str(res.get("error")), res
    assert _closed(session, 2026, 11, 23, 15)


def test_uprava_vyluky_vzniknutej_po_spracovani_zastavi_zrusenie(env, session):
    """ROV 23.11. (do 72 h – čaká), jeho opakovanie a zrušenie čakajú; dispečer ROV zamietne, potvrdí opakovanie (vznikne
    nová výluka) a predĺži ju v registri do 16:00. Zrušenie sa prvýkrát nezapíše – výluku fronta pri ňom neukázala."""
    _T[0] = utc(2026, 11, 21, 7, 32)
    rov = _one(env, session, SUBJ, ROV_A)
    again = _one(env, session, SUBJ, ROV_A)
    cancel = _one(env, session, CSUBJ, _body(f"{SEC} dňa 23.11.2026 sa ruší."))
    _reject(session, rov)
    _cycle(env, session)
    res = _confirm(session, again)
    c = session.get(Closure, res.get("id"))
    _uprav(session, c, end_utc=loc(2026, 11, 23, 16))
    _cycle(env, session)
    res = _confirm(session, cancel)
    assert res.get("ok") is False and STALE in str(res.get("error")), res
    assert _closed(session, 2026, 11, 23, 15)


def test_uprava_v_registri_zastavi_rozdelenie_zmenou_dna(env, session, client):
    """Overený ROV 23. – 25.11. denne 08 – 14; čaká „dňa 24.11. bude 10 – 12“; dispečer v registri výluk (/vyluky)
    zmení denné okno na 08 – 16. Potvrdenie zmeny (rozdelenie výluky) sa prvýkrát nezapíše – inak by 24.11. 14 – 16,
    ktoré drží úprava, ostalo voľné; druhé potvrdenie sa zapíše."""
    from vlaky.db import session_scope
    from vlaky.web.views_evidencia import _closure_values
    from test_web_spolocne import csrf

    login_as(client, "dispecer1")
    _confirm(session, _one(env, session, SUBJ, ROV_B))
    change = _one(env, session, ZSUBJ, _body(f"{SEC} sa mení: dňa 24.11.2026 bude od 10:00 do 12:00 hod."))
    (c,) = _main(session)
    set_fake_now(_next())
    with session_scope() as s:
        vals = {k: ("" if v is None else v) for k, v in _closure_values(s.get(Closure, c.id)).items()}
    vals.update(daily_to="16:00", end_utc="2026-11-25T16:00")
    r = client.post(f"/vyluky/{c.id}/upravit", data={"csrf_token": csrf(client, "/vyluky"), **vals},
                    follow_redirects=False)
    assert r.status_code in (302, 303), r.text[:300]
    session.expire_all()
    _cycle(env, session)
    assert _closed(session, 2026, 11, 24, 15)
    res = _confirm(session, change)
    assert res.get("ok") is False and STALE in str(res.get("error")), res
    assert "úprava pridala" in str(res.get("error")), res
    assert _closed(session, 2026, 11, 24, 15)
    assert _confirm(session, change).get("action") == "rozdelene"


def test_zrusenie_ineho_dna_po_uprave_v_registri_sa_zapise_hned(env, session):
    """Overený ROV 23. – 25.11.; čaká zrušenie 24.11.; dispečer v registri pridá 26.11. Zrušenie sa týka len 24.11. –
    pridaného času sa nedotkne, zapíše sa hneď (bez nepravdivého upozornenia) a 26.11. ostáva."""
    _confirm(session, _one(env, session, SUBJ, ROV_B))
    cancel = _one(env, session, CSUBJ, ZRUSENIE_24)
    (c,) = _main(session)
    _uprav(session, c, end_utc=loc(2026, 11, 26, 14))
    _cycle(env, session)
    res = _confirm(session, cancel)
    assert res.get("ok") is True and res.get("action") == "rozdelene", res
    _cycle(env, session)
    assert _closed(session, 2026, 11, 26, 10) and not _closed(session, 2026, 11, 24, 10)


def test_zamietnutie_zapisovatela_ponecha_upravu_v_registri(env, session):
    """ROV 23.11. 08 – 14 a jeho dlhšie opakovanie 08 – 16 (zapisovateľ) sa zapíšu automaticky; dispečer v registri
    predĺži výluku do 24.11. 16:00 a potom opakovanie zamietne: upravená výluka ostáva (text to hovorí), 24.11. 10:00
    je zatvorený."""
    _one(env, session, SUBJ, ROV_A)
    x = _one(env, session, SUBJ, _rov("dňa 23.11.2026 od 08:00 do 16:00 hod."))
    assert x.status == "aplikovane"
    (c,) = _main(session)
    _uprav(session, c, end_utc=loc(2026, 11, 24, 16))
    text = _rollback_text(_reject(session, x))
    assert f"výluku #{c.id} medzitým upravil dispečer v registri výluk – ponechaná bez zmeny" in text, text
    _cycle(env, session)
    assert _closed(session, 2026, 11, 24, 10)


def test_zamietnutie_zrusenia_dna_po_zruseni_casti_v_registri_obnovi_cas_rov(env, session):
    """ROV 23. – 25.11. denne (automaticky); potvrdené „dňa 24.11. sa ruší“ (rozdelenie); dispečer v registri výluk zruší
    časť 23.11. a potom zamietne zrušenie 24.11.: úprava v registri ostáva v zázname, ale čas ROV, ktorý zamietnutá
    udalosť a zrušená časť už nemajú (23. a 24.11.), sa zapíše znova podľa ROV – zamietnutím sa nič neotvorí."""
    from vlaky.audit import audit

    _one(env, session, SUBJ, ROV_B)
    cancel = _one(env, session, CSUBJ, ZRUSENIE_24)
    assert _confirm(session, cancel).get("action") == "rozdelene"
    _cycle(env, session)
    first = min(_main(session), key=lambda c: c.start_utc)
    set_fake_now(_next())
    first.status = "zrusene"
    first.version = (first.version or 1) + 1
    audit(session, "dispecer1", "closure_cancel", "closure", first.id, reason="test", version=first.version)
    session.commit()
    text = _rollback_text(_reject(session, cancel))
    assert "ponechaná bez zmeny" in text and "zapísaný znova" in text, text
    _cycle(env, session)
    assert _kryte(session, 2026, 11, 23, 10) and _kryte(session, 2026, 11, 24, 10) and _kryte(session, 2026, 11, 25, 10)


# ====================================================================== texty: do odvolania, príčina zrušenia
@pytest.mark.parametrize("body", [
    _body(f"{SEC} sa predlžuje do 30.11.2026 14:00 hod."),
    _body(f"{SEC} sa mení: dňa 24.11.2026 bude od 10:00 do 16:00 hod."),
    _body(f"{SEC} sa skracuje, dňa 24.11.2026 bude od 08:00 do 10:00 hod."),
    PRESUN_23_25,
    ZRUSENIE_24,
], ids=["predlzenie", "zmena_casu", "skratenie", "presun", "zrusenie_dna"])
def test_texty_vyluky_do_odvolania_bez_technickeho_konca(env, session, body):
    """ROV „od 23.11.2026 08:00 do odvolania“ (overený); upozornenia, dôvody a výsledok potvrdenia nadväzujúcej správy
    neuvádzajú technický koniec výluky (december) – len „od … do odvolania“."""
    _confirm(session, _one(env, session, SUBJ, ROV_ODVOLANIE))
    ev = _one(env, session, CSUBJ if body is ZRUSENIE_24 else ZSUBJ, body)
    val = ev.validation or {}
    texts = [str(x) for x in (val.get("warnings") or []) + (val.get("confirmation_reasons") or [])]
    res = _confirm(session, ev)
    texts += [str(res.get("note") or ""), str(res.get("error") or "")]
    bad = [t for t in texts if TECH.search(t)]
    assert not bad, bad[0][:400]


def test_text_samostatneho_terminu_do_odvolania(env, session):
    """Overený ROV 23.11. 08 – 14; čaká jeho opakovanie „od 23.11. 08:00 do odvolania“ a novšie skrátenie 23.11. na
    08 – 10. Potvrdí sa skrátenie, potom opakovanie: jeho termín v iné dni sa zapíše samostatne – text uvádza
    „od … do odvolania“, nie technický koniec."""
    _confirm(session, _one(env, session, SUBJ, ROV_A))
    older = _one(env, session, SUBJ, ROV_ODVOLANIE)
    _confirm(session, _one(env, session, ZSUBJ, SKRATENIE_23))
    _cycle(env, session)
    res = _confirm(session, older)
    note = str(res.get("note") or "")
    assert "do odvolania" in note and not TECH.search(note), note
    assert _closed(session, 2026, 11, 24, 10)


def test_karta_automaticky_zapisanych_a_odovzdavka_vyluky_do_odvolania(env, session, client):
    """Automaticky zapísaný ROV do odvolania: karta „Zapísané automaticky“ vo fronte aj odovzdávka služby uvádzajú
    „od … do odvolania“ – technický koniec nanajvýš výslovne označený („technický koniec …“, ako register výluk), nikdy
    ako koniec výluky."""
    from vlaky.agent.odovzdavka import build_report

    login_as(client, "dispecer1")
    _one(env, session, SUBJ, ROV_ODVOLANIE)
    page = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", client.get("/fronta").text))
    i = page.find("Zapísané automaticky")
    card = _NAMED_TECH.sub("", page[i:i + 900])
    assert i >= 0 and "do odvolania" in card and not TECH.search(card), page[i:i + 900]
    content, _data = build_report(session, datetime.now(timezone.utc))
    part = content[content.find("Nové alebo zmenené výluky"):][:400]
    assert "do odvolania" in part and not TECH.search(_NAMED_TECH.sub("", part)), part


@pytest.mark.parametrize("how", ["zamietnutie_rov", "zrusenie_v_registri"])
def test_vysledok_uvadza_skutocnu_priciny_zrusenia_vyluky(env, session, how):
    """ROV 23.11. (automaticky); čaká doplnenie 26.11.; dispečer ROV zamietne alebo jeho výluku zruší v registri, potom
    potvrdí doplnenie: výsledok nehovorí o „staršej udalosti“, ktorá výluku zrušila – uvádza dispečera."""
    rov = _one(env, session, SUBJ, ROV_A)
    add = _one(env, session, ZSUBJ, _body(f"{SEC} sa dopĺňa o ďalší termín 26.11.2026 od 08:00 do 14:00 hod."))
    if how == "zamietnutie_rov":
        _reject(session, rov)
    else:
        (c,) = _main(session)
        from vlaky.audit import audit

        set_fake_now(_next())
        c.status = "zrusene"
        c.version = (c.version or 1) + 1
        audit(session, "dispecer1", "closure_cancel", "closure", c.id, reason="test", version=c.version)
        session.commit()
    res = _confirm(session, add)
    note = str(res.get("note") or "")
    assert "zrušila staršia udalosť" not in note and "dispečer" in note, note
    assert _closed(session, 2026, 11, 26, 10)


# ====================================================================== výkon
def test_zamietnutie_zapisovatela_neprechadza_vsetky_udalosti(env, session):
    """Pri tisícoch zapísaných udalostí (aj „bez zmeny“ a overení) zamietnutie zapisovateľa výluky nehľadá nahradené
    udalosti ani držiteľov prechodom všetkých udalostí: načíta len niekoľko udalostí (počet nezávisí od rýchlosti
    databázy) a trvá krátko."""
    from sqlalchemy import event as sa_event
    from sqlalchemy import insert

    from vlaky.posta.spracovanie import confirm_event, reject_event

    rows = []
    base = utc(2026, 9, 1, 6)
    for i in range(12000):
        s = base + timedelta(minutes=37 * i)
        act = ("bez_zmeny", "potvrdene", "vytvorene", "nahradene")[i % 4]
        applied = {"entity": "Closure", "id": 1 + i % 50, "action": act, "status": "overene", "at": s.isoformat(),
                   "by": "dispecer1", "confirmed": True, "ok": True, "kind": "vyluka_nova",
                   "superseded_by": 1 + (i + 5) % 12000 if act == "nahradene" else None,
                   "verified_only": act == "potvrdene", "verified": [1 + i % 50] if act == "potvrdene" else [],
                   "held": [1 + (i + 3) % 50] if act == "bez_zmeny" else None}
        rows.append({"email_id": None, "kind": "vyluka_nova", "data": {"start": s.isoformat()}, "confidence": 0.85,
                     "extractor": "pravidla", "status": "aplikovane", "needs_confirmation": False,
                     "validation": {"applied": applied, "warnings": ["x" * 400]}, "created_at": s,
                     "confirmed_by": "dispecer1", "confirmed_at": s})
    for k in range(0, len(rows), 1000):
        session.execute(insert(ExtractedEvent), rows[k:k + 1000])
    session.commit()
    _one(env, session, SUBJ, ROV_A)
    y = _one(env, session, SUBJ, _rov("dňa 23.11.2026 od 08:00 do 12:00 hod."))
    x = _one(env, session, SUBJ, _rov("dňa 23.11.2026 od 08:00 do 16:00 hod."))
    assert x.status == "aplikovane" and y.needs_confirmation
    set_fake_now(_next())
    confirm_event(session, y.id, "dispecer1")
    session.commit()
    set_fake_now(_next())
    loaded: list[int] = []

    def _nacitana(_session, obj) -> None:
        if isinstance(obj, ExtractedEvent):
            loaded.append(obj.id)

    sa_event.listen(session, "loaded_as_persistent", _nacitana)
    try:
        t0 = time.perf_counter()
        reject_event(session, x.id, "dispecer1", reason="výkon")
        session.commit()
        dt = time.perf_counter() - t0
    finally:
        sa_event.remove(session, "loaded_as_persistent", _nacitana)
    assert len(loaded) < 100, f"zamietnutie načítalo {len(loaded)} udalostí"
    assert dt < 2.0, f"zamietnutie trvalo {dt:.2f} s"
    assert _closed(session, 2026, 11, 23, 10)
