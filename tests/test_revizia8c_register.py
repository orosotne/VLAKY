"""Ôsma revízia, tretie kolo – register výluk po prehľadávaní tisícok poradí udalostí (permutačný sken).

Kritérium: o každom čase rozhoduje najnovšia prijatá (nezamietnutá; aj čakajúca) správa, ktorá o ňom hovorí – plánovač
nesmie dať VYPRAVIŤ ZELENÁ, kým ho niektorá taká správa drží zatvorený.

* Zmena, ktorej nový termín sa výluky nedotýka, výluke termín nenahradí, ak je skrátením či zmenou času („dňa D bude
  od … do …“), alebo ak číslo výluky má aj iná (platná či zrušená) výluka – nový termín je ďalšia výluka.
* Potvrdená udalosť, ktorá register nemení („bez_zmeny“), sa stane zapisovateľom výluky: staršia správa potvrdená
  neskôr ju nenahradí a zamietnutie inej udalosti s tým istým termínom výluku nezruší.
* Zamietnutie zapísanej udalosti znova zapíše staršie udalosti, ktoré nahradila; výluku, ktorú po nej s rovnakým
  obsahom overila iná udalosť, ponechá.
* Novšie predĺženie či doplnenie nerozhoduje o dňoch, o ktorých nehovorí – staršia zmena jedného dňa sa zapíše.
* Úprava dispečera v registri výluk a výluka, ktorá vznikla až po spracovaní e-mailu, sa potvrdením staršej správy
  bez nového upozornenia neprepíšu.

Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

import pytest

from vlaky.cas import set_fake_now
from test_revizia7_odobratie_casu import _live, env, loc, utc  # noqa: F401 – env
from test_revizia8_register import (CSUBJ, SEC, SUBJ, ZSUBJ, _T, _body, _clock, _closed, _confirm, _next, _one,  # noqa
                                    _send)
from test_revizia8b_register import _reject


def _rov(term: str) -> str:
    return _body(f"úplná výluka v úseku Ružomberok – Liptovský Mikuláš {term} (ROV č. 2331/2026).")


def _cycle(env, session) -> None:
    from vlaky.posta.spracovanie import process_mailbox

    t = _next()
    set_fake_now(t)
    process_mailbox(session, now=t)
    session.commit()


ROV_A = _rov("dňa 23.11.2026 od 08:00 do 14:00 hod.")
ROV_B = _rov("v dňoch 23.11. – 25.11.2026 denne od 08:00 do 14:00 hod.")
ROV_E = _rov("dňa 23.11.2026 od 08:00 do 14:00 hod. a dňa 25.11.2026 od 08:00 do 14:00 hod.")
PRESUN_A = _body(f"{SEC} sa presúva z 23.11.2026 na 25.11.2026, od 08:00 do 14:00 hod.")
DOPLNENIE_A = _body(f"{SEC} sa dopĺňa o ďalší termín 26.11.2026 od 08:00 do 14:00 hod.")
PREDLZENIE_A = _body(f"{SEC} sa predlžuje do 24.11.2026 14:00 hod.")
SKRATENIE_A = _body(f"{SEC} sa skracuje, dňa 23.11.2026 bude od 08:00 do 10:00 hod.")
ZMENA_A = _body(f"{SEC} sa mení: dňa 23.11.2026 bude od 10:00 do 16:00 hod.")
ZRUSENIE_23 = _body(f"{SEC} dňa 23.11.2026 sa ruší.")


# ====================================================================== zmena iného dňa
def test_zmena_zruseneho_dna_neprepise_iny_termin_rov(env, session):
    """ROV s termínmi 23.11. a 25.11.; potvrdené zrušenie 25.11.; neskôr „dňa 25.11. bude 10 – 16“: termín 23.11. žiadna
    správa nezmenila – ostáva; nový čas 25.11. je ďalšia výluka."""
    assert len(_send(env, session, SUBJ, ROV_E)) == 2
    _confirm(session, _one(env, session, CSUBJ, _body(f"{SEC} dňa 25.11.2026 sa ruší.")))
    _cycle(env, session)
    change = _one(env, session, ZSUBJ, _body(f"{SEC} sa mení: dňa 25.11.2026 bude od 10:00 do 16:00 hod."))
    assert (change.data or {}).get("zamer") == "zmena_casu"
    res = _confirm(session, change)
    assert "netýka" in str(res.get("note")), res
    assert _closed(session, 2026, 11, 25, 12) and _closed(session, 2026, 11, 23, 10)


def test_zmena_zruseneho_dna_neprepise_doplneny_termin(env, session):
    _one(env, session, SUBJ, ROV_A)
    _confirm(session, _one(env, session, ZSUBJ, DOPLNENIE_A))
    _confirm(session, _one(env, session, CSUBJ, ZRUSENIE_23))
    _cycle(env, session)
    _confirm(session, _one(env, session, ZSUBJ, ZMENA_A))
    assert _closed(session, 2026, 11, 23, 12, 30) and _closed(session, 2026, 11, 26, 10)


def test_skratenie_po_zamietnutom_rov_neprepise_doplnenie(env, session):
    rov = _one(env, session, SUBJ, ROV_A)
    _reject(session, rov)
    _cycle(env, session)
    add = _one(env, session, ZSUBJ, DOPLNENIE_A)
    short = _one(env, session, ZSUBJ, SKRATENIE_A)
    _confirm(session, add)
    _confirm(session, short)
    assert _closed(session, 2026, 11, 26, 10)


def test_zmena_ineho_dna_po_zamietnutom_rov_neprepise_presun(env, session):
    for e in _send(env, session, SUBJ, ROV_E):
        _reject(session, e)
    _cycle(env, session)
    move = _one(env, session, ZSUBJ, _body(f"{SEC} sa presúva z 23.11.2026 na 26.11.2026, od 08:00 do 14:00 hod."))
    change = _one(env, session, ZSUBJ, _body(f"{SEC} sa mení: dňa 25.11.2026 bude od 10:00 do 16:00 hod."))
    _confirm(session, move)
    _cycle(env, session)
    _confirm(session, change)
    assert _closed(session, 2026, 11, 26, 10)


@pytest.mark.parametrize("body,when", [(SKRATENIE_A, (2026, 11, 23, 9)), (ZMENA_A, (2026, 11, 23, 12, 30))],
                         ids=["skratenie", "zmena_casu"])
def test_zmena_dna_po_presune_neprepise_presunuty_termin(env, session, body, when):
    """ROV 23.11.; potvrdený presun na 25.11.; potom skrátenie či zmena času „dňa 23.11. bude …“: hovoria len o 23.11.
    – presunutý termín 25.11. ostáva, nový čas 23.11. je ďalšia výluka."""
    _one(env, session, SUBJ, ROV_A)
    _confirm(session, _one(env, session, ZSUBJ, PRESUN_A))
    _cycle(env, session)
    _confirm(session, _one(env, session, ZSUBJ, body))
    _cycle(env, session)
    assert _closed(session, *when) and _closed(session, 2026, 11, 25, 10)


def test_skratenie_po_povysenom_opakovani_neprepise_presun(env, session):
    """ROV 23. – 25.11. denne; čaká kratšie opakovanie (23. – 24.11.); dispečer ROV zamietne; presun „z 24.11. na
    28.11.“ a skrátenie „bude v dňoch 23. – 24.11.“ čakajú; potvrdené v poradí príchodu: 28.11. ostáva."""
    rov = _one(env, session, SUBJ, ROV_B)
    shorter = _one(env, session, SUBJ, _rov("v dňoch 23.11. – 24.11.2026 denne od 08:00 do 14:00 hod."))
    _reject(session, rov)
    _cycle(env, session)
    move = _one(env, session, ZSUBJ, _body(f"{SEC} sa presúva z 24.11.2026 na 28.11.2026, od 08:00 do 14:00 hod."))
    short = _one(env, session, ZSUBJ, _body(f"{SEC} sa skracuje, bude v dňoch 23.11. – 24.11.2026 denne od 08:00 do "
                                            "14:00 hod."))
    for ev in (shorter, move, short):
        _confirm(session, ev)
        _cycle(env, session)
    assert _closed(session, 2026, 11, 28, 10)


# ====================================================================== úprava v registri, nová výluka
def test_uprava_dispecera_v_registri_sa_neprepise_starsim_skratenim(env, session):
    """Overený ROV 23. – 25.11.; čaká skrátenie „bude v dňoch 23. – 24.11.“; dispečer v registri predĺži výluku do
    26.11.; potvrdenie skrátenia sa prvýkrát nezapíše (nové upozornenie) – 26.11. ostáva."""
    _confirm(session, _one(env, session, SUBJ, ROV_B))
    short = _one(env, session, ZSUBJ, _body(f"{SEC} sa skracuje, bude v dňoch 23.11. – 24.11.2026 denne od 08:00 do "
                                            "14:00 hod."))
    (c,) = [x for x in _live(session) if x.proposed_change_of is None]
    set_fake_now(_next())
    c.end_utc = loc(2026, 11, 26, 14)
    c.version = (c.version or 1) + 1
    session.commit()
    _cycle(env, session)
    res = _confirm(session, short)
    assert res.get("ok") is False and "sa od spracovania e-mailu zmenila" in str(res.get("error")), res
    assert _closed(session, 2026, 11, 26, 10)


def test_opakovany_rov_neprepise_samostatne_predlzenie(env, session):
    """Overený ROV 23.11. 08 – 14; čakajú presun na 25.11., predĺženie do 24.11. a ROV znova s 08 – 16. Po presune je
    predĺženie samostatnou výlukou, ktorá vznikla až po spracovaní opakovaného ROV – ten ju bez nového upozornenia
    neprepíše: 24.11. ostáva."""
    _confirm(session, _one(env, session, SUBJ, ROV_A))
    evs = [_one(env, session, ZSUBJ, PRESUN_A), _one(env, session, ZSUBJ, PREDLZENIE_A),
           _one(env, session, SUBJ, _rov("dňa 23.11.2026 od 08:00 do 16:00 hod."))]
    for ev in evs:
        _confirm(session, ev)
        _cycle(env, session)
    assert _closed(session, 2026, 11, 24, 10)


# ====================================================================== zamietnutie a nahradené udalosti
def test_zamietnutie_prvej_kopie_nezrusi_termin_druhej(env, session):
    """ROV 23.11.; ten istý presun z dvoch zoznamov adries; ROV znova (novší zapisovateľ). Prvý presun sa zapíše,
    druhý „bez zmeny“ (výluku prevezme); dispečer zamietne prvý ako duplicitu – 25.11. ostáva."""
    _one(env, session, SUBJ, ROV_A)
    p1, p2 = _one(env, session, ZSUBJ, PRESUN_A), _one(env, session, ZSUBJ, PRESUN_A)
    _one(env, session, SUBJ, ROV_A)
    assert _confirm(session, p1).get("action") == "vytvorene"
    assert _confirm(session, p2).get("action") == "bez_zmeny"
    _cycle(env, session)
    _reject(session, p1)
    _cycle(env, session)
    assert _closed(session, 2026, 11, 25, 10)


def test_zamietnutie_novsej_kopie_ponecha_overeny_obsah_starsej(env, session):
    _one(env, session, SUBJ, ROV_A)
    p1, p2 = _one(env, session, ZSUBJ, PRESUN_A), _one(env, session, ZSUBJ, PRESUN_A)
    _confirm(session, p2)
    _confirm(session, p1)
    _cycle(env, session)
    rej = _reject(session, p2)
    assert "ostáva podľa udalosti" in str((rej.validation or {}).get("rejection", {}).get("rollback"))
    _cycle(env, session)
    assert _closed(session, 2026, 11, 25, 10)


def test_zamietnutie_dlhsieho_opakovania_znova_zapise_nahradene_rovnake(env, session):
    """ROV 23.11. 08 – 14 (16.11.); do 72 h čakajú presun, ten istý ROV a dlhší ROV 08 – 16. Dlhší sa zapíše, rovnaký
    je „nahradený“; dispečer dlhší zamietne – rovnaký ROV (novší ako presun) sa zapíše znova a potvrdený presun 23.11.
    neodoberie."""
    _one(env, session, SUBJ, ROV_A)
    _T[0] = utc(2026, 11, 21, 8, 2)
    move = _one(env, session, ZSUBJ, PRESUN_A)
    same = _one(env, session, SUBJ, ROV_A)
    longer = _one(env, session, SUBJ, _rov("dňa 23.11.2026 od 08:00 do 16:00 hod."))
    _confirm(session, longer)
    _cycle(env, session)
    _confirm(session, same)
    _cycle(env, session)
    rej = _reject(session, longer)
    assert "zapísaná znova" in str((rej.validation or {}).get("rejection", {}).get("rollback")), rej.validation
    _cycle(env, session)
    _confirm(session, move)
    _cycle(env, session)
    assert _closed(session, 2026, 11, 23, 9)


# ====================================================================== novšie predĺženie, novšia správa bez zmeny
def test_novsie_predlzenie_nerozhoduje_o_dni_starsej_zmeny(env, session):
    """ROV 23. – 25.11. denne; čaká „dňa 24.11. bude 10 – 16“, potom predĺženie do 26.11.; potvrdené predĺženie
    a potom zmena: predĺženie o 24.11. nehovorí – čas 10 – 16 sa zapíše (aj 24.11. 15:00 je zatvorené)."""
    _one(env, session, SUBJ, ROV_B)
    change = _one(env, session, ZSUBJ, _body(f"{SEC} sa mení: dňa 24.11.2026 bude od 10:00 do 16:00 hod."))
    extend = _one(env, session, ZSUBJ, _body(f"{SEC} sa predlžuje do 26.11.2026 14:00 hod."))
    _confirm(session, extend)
    _cycle(env, session)
    _confirm(session, change)
    assert _closed(session, 2026, 11, 24, 15) and _closed(session, 2026, 11, 26, 10)


def test_predlzenie_bez_zmeny_chrani_pred_starsim_prehladom(env, session):
    """ROV 23. – 25.11. denne; dlhšie opakovanie do 26.11. (zapíše sa); čaká týždenný prehľad 23. – 25.11. a predĺženie
    do 26.11.; potvrdené predĺženie („bez zmeny“ – výluka 26.11. má), potom staršie oznámenie: 26.11. ostáva."""
    _one(env, session, SUBJ, ROV_B)
    _one(env, session, SUBJ, _rov("v dňoch 23.11. – 26.11.2026 denne od 08:00 do 14:00 hod."))
    weekly = _one(env, session, "Oznámenie o povolených výlukách – 48. týždeň",
                  _body("v 48. týždni (23.11. – 29.11.2026) sú povolené tieto výluky:\n\n1. Ružomberok – Liptovský "
                        "Mikuláš, úplná výluka, 23.11. – 25.11.2026 denne od 08:00 do 14:00 hod., ROV č. 2331/2026"))
    extend = _one(env, session, ZSUBJ, _body(f"{SEC} sa predlžuje do 26.11.2026 14:00 hod."))
    _confirm(session, extend)
    _cycle(env, session)
    _confirm(session, weekly)
    assert _closed(session, 2026, 11, 26, 10)


def test_skratenie_bez_zmeny_chrani_pred_starsim_zrusenim(env, session):
    """Overený ROV 23.11.; čakajú predĺženie do 24.11., zrušenie 23.11. a skrátenie „dňa 23.11. bude 08 – 10“. Predĺženie
    sa zapíše, zrušenie sa najprv odmietne (čaká iná zmena), skrátenie je „bez zmeny“; zopakované staršie zrušenie
    23.11. 08 – 10 neodoberie (novšie skrátenie)."""
    _confirm(session, _one(env, session, SUBJ, ROV_A))
    extend = _one(env, session, ZSUBJ, PREDLZENIE_A)
    cancel = _one(env, session, CSUBJ, ZRUSENIE_23)
    short = _one(env, session, ZSUBJ, SKRATENIE_A)
    _confirm(session, extend)
    _cycle(env, session)
    _confirm(session, cancel)
    _cycle(env, session)
    _confirm(session, short)
    _cycle(env, session)
    _confirm(session, cancel)
    assert _closed(session, 2026, 11, 23, 9)


def test_zamietnute_zrusenie_vrati_zvysok_predlzenia(env, session):
    _one(env, session, SUBJ, ROV_A)
    extend = _one(env, session, ZSUBJ, PREDLZENIE_A)
    cancel = _one(env, session, CSUBJ, ZRUSENIE_23)
    _confirm(session, cancel)
    _cycle(env, session)
    _confirm(session, extend)
    _cycle(env, session)
    _reject(session, cancel)
    _cycle(env, session)
    assert _closed(session, 2026, 11, 24, 10) and _closed(session, 2026, 11, 23, 20)


# ====================================================================== prevzatie výluky a reťaz zamietnutí
def test_zamietnutie_dvoch_kopii_doplnenia_ponecha_vyluku_rov(env, session):
    """Overený ROV 23. – 25.11. denne; to isté doplnenie 25.11. (deň, ktorý výluka už má) z dvoch zoznamov adries – obe
    potvrdené „bez zmeny“ (výluku prevezmú). Dispečer obe zamietne ako duplicity: výluku drží znova ROV – všetky tri
    dni ostávajú (výluka sa nezruší)."""
    rov = _one(env, session, SUBJ, ROV_B)
    _confirm(session, rov)
    add = _body(f"{SEC} sa dopĺňa o ďalší termín 25.11.2026 od 08:00 do 14:00 hod.")
    d1, d2 = _one(env, session, ZSUBJ, add), _one(env, session, ZSUBJ, add)
    assert _confirm(session, d1).get("action") == "bez_zmeny"
    assert _confirm(session, d2).get("action") == "bez_zmeny"
    _cycle(env, session)
    _reject(session, d1)
    rej = _reject(session, d2)
    text = str((rej.validation or {}).get("rejection", {}).get("rollback"))
    assert f"ostáva podľa udalosti #{rov.id}" in text and "zrušená" not in text, text
    _cycle(env, session)
    assert all(_closed(session, 2026, 11, d, 10) for d in (23, 24, 25))


def test_starsia_kopia_presunu_neprevezme_vyluku_novsej(env, session):
    """ROV 23.11.; presun na 25.11., zrušenie 25.11., ten istý presun znova a ROV znova (novší zapisovateľ 23.11.).
    Potvrdené: novší presun (25.11. ako samostatná výluka), starší presun (rovnaký termín – výluku 25.11. neprevezme,
    zapísala ju novšia správa) a staršie zrušenie 25.11.: o 25.11. rozhoduje novší presun – ostáva zatvorený."""
    from vlaky.models import Closure

    _one(env, session, SUBJ, ROV_A)
    older = _one(env, session, ZSUBJ, PRESUN_A)
    cancel = _one(env, session, CSUBJ, _body(f"{SEC} dňa 25.11.2026 sa ruší."))
    newer = _one(env, session, ZSUBJ, PRESUN_A)
    _one(env, session, SUBJ, ROV_A)
    assert _confirm(session, newer).get("action") == "vytvorene"
    res = _confirm(session, older)
    assert res.get("action") == "bez_zmeny", res
    x = session.get(Closure, res.get("id"))
    assert x.source_event_id == newer.id, (x.source_event_id, newer.id, older.id)
    _confirm(session, cancel)
    _cycle(env, session)
    assert _closed(session, 2026, 11, 25, 10)


def test_zamietnutie_doplnenia_po_rucne_zapisanej_vyluke(env, session):
    """Dispečer zapíše výluku 25.11. ručne v registri výluk; doplnenie 25.11. ju potvrdením prevezme („bez zmeny“).
    Zamietnutím doplnenia ručná výluka ostáva a text návratu nehovorí o neexistujúcej udalosti."""
    from vlaky.models import Closure
    from test_revizia7_odobratie_casu import A, B

    c = Closure(kind="uplna", from_station=A, to_station=B, line_ref="", track="", start_utc=loc(2026, 11, 25, 8),
                end_utc=loc(2026, 11, 25, 14), daily_from="", daily_to="", electric_only=False, speed_kmh=None,
                external_ref="ROV č. 2331/2026", until_revoked=False, status="overene", source="manual")
    session.add(c)
    session.commit()
    add = _one(env, session, ZSUBJ, _body(f"{SEC} sa dopĺňa o ďalší termín 25.11.2026 od 08:00 do 14:00 hod."))
    res = _confirm(session, add)
    assert res.get("action") == "bez_zmeny" and res.get("id") == c.id, res
    rej = _reject(session, add)
    text = str((rej.validation or {}).get("rejection", {}).get("rollback"))
    assert "None" not in text and f"výluka #{c.id} ostáva" in text, text
    session.refresh(c)
    assert c.status == "overene" and c.source_event_id is None
    _cycle(env, session)
    assert _closed(session, 2026, 11, 25, 10)


# ====================================================================== opätovný zápis nahradeného čiastočného zrušenia
@pytest.mark.parametrize("newer", ["opakovanie", "predlzenie"])
def test_zamietnutie_novsej_udalosti_znova_zapise_len_zrusenie_dna(env, session, newer):
    """Overený ROV 23. – 25.11. denne; čaká „dňa 24.11. sa ruší“; potvrdené novšie dlhšie opakovanie (či predĺženie)
    do 26.11. a potom zrušenie („nahradené“). Dispečer novšiu udalosť zamietne: zrušenie sa zapíše znova s novým plánom –
    zruší len 24.11., 23.11. a 25.11. ostávajú zatvorené (nie celá výluka)."""
    _confirm(session, _one(env, session, SUBJ, ROV_B))
    cancel = _one(env, session, CSUBJ, _body(f"{SEC} dňa 24.11.2026 sa ruší."))
    if newer == "opakovanie":
        later = _one(env, session, SUBJ, _rov("v dňoch 23.11. – 26.11.2026 denne od 08:00 do 14:00 hod."))
    else:
        later = _one(env, session, ZSUBJ, _body(f"{SEC} sa predlžuje do 26.11.2026 14:00 hod."))
    assert _confirm(session, later).get("ok")
    _cycle(env, session)
    assert _confirm(session, cancel).get("action") == "nahradene"
    _cycle(env, session)
    rej = _reject(session, later)
    text = str((rej.validation or {}).get("rejection", {}).get("rollback"))
    assert f"udalosť #{cancel.id} zapísaná znova" in text, text
    _cycle(env, session)
    assert _closed(session, 2026, 11, 23, 10) and _closed(session, 2026, 11, 25, 10)
    assert not _closed(session, 2026, 11, 24, 10)
