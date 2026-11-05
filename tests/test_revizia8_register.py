"""Ôsma revízia – register výluk po treťom nezávislom overení (zrušenie a rozdelenie, vrátenie zamietnutím, poradie
potvrdení).

* Potvrdené zrušenie len časti automaticky zapísanej (neoverenej) výluky ponechá ostatné dni: časti po rozdelení majú
  stav pôvodnej výluky („navrh“) a zápis zrušenia ich nesmie zrušiť ako svoje návrhy.
* Čakajúce oznámenie, ktoré opakovalo výluku („rovnaká výluka … už v registri je“ – bez záznamu pre plánovač), dostane
  záznam, keď výluku zmení či rozdelí staršia udalosť alebo dispečer zamietne zapísanú udalosť; dovtedy poistka
  čakajúcej udalosti. Novšia udalosť, ktorá výluku zmenila, má pred ním prednosť.
* Novšie zrušenie dňa odstráni dočasný záznam staršej čakajúcej zmeny len vtedy, keď pokrýva celý jej termín; zvyšok
  (predĺženie do ďalšieho dňa, doplnenie cez dva dni) ostáva pre plánovač a po potvrdení sa zapíše.
* Zmena, ktorej výluku zrušila staršia udalosť, sa nepriradí inej výluke s rovnakým číslom (iný termín ROV, doplnený
  termín) – tá by stratila svoj termín.
* Potvrdenie, ktoré by výluke odobralo čas pridaný medzitým inou udalosťou (fronta o ňom pri udalosti nehovorila), sa
  nezapíše bez nového upozornenia; druhé potvrdenie už áno (rozhodnutie dispečera).
* Ten istý termín sa nezapíše dvakrát; overenie výluky po rovnakom opakovanom oznámení výluku overí; prísnejšie
  obmedzenie v registri nie je „miernejšie“.

Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import timedelta
from email.utils import format_datetime

import pytest

from vlaky.cas import set_fake_now
from vlaky.models import ExtractedEvent
from test_revizia7_odobratie_casu import _decide, _green, _live, _mail, env, loc, utc  # noqa: F401 – env

SUBJ = "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas"
ZSUBJ = "Zmena ROV č. 2331/2026"
CSUBJ = "Zrusenie ROV c. 2331/2026"
SEC = "výluka v úseku Ružomberok – Liptovský Mikuláš podľa ROV č. 2331/2026"


def _body(text: str) -> str:
    return f"Dobrý deň,\n\n{text}\n\nOR Žilina\n"


ROV1 = _body("úplná výluka v úseku Ružomberok – Liptovský Mikuláš dňa 23.11.2026 od 08:00 do 14:00 hod. "
             "(ROV č. 2331/2026).")
ROV3 = _body("úplná výluka v úseku Ružomberok – Liptovský Mikuláš v dňoch 23.11. – 25.11.2026 denne od 08:00 do "
             "14:00 hod. (ROV č. 2331/2026).")
ROV2T = _body("úplná výluka v úseku Ružomberok – Liptovský Mikuláš dňa 23.11.2026 od 08:00 do 14:00 hod. a dňa "
              "25.11.2026 od 08:00 do 14:00 hod. (ROV č. 2331/2026).")
NIGHTS = _body("úplná výluka v úseku Ružomberok – Liptovský Mikuláš v nociach 23./24.11. – 26./27.11.2026 vždy od "
               "22:00 do 04:00 hod. (ROV č. 2331/2026).")
CANCEL_23 = _body(f"{SEC} dňa 23.11.2026 sa ruší.")
CANCEL_24 = _body(f"{SEC} dňa 24.11.2026 sa ruší.")
CANCEL_NIGHT = _body(f"{SEC} v noci z 24.11. na 25.11.2026 sa ruší.")
MOVE_23_25 = _body(f"{SEC} sa presúva z 23.11.2026 na 25.11.2026, od 08:00 do 14:00 hod.")
MOVE_23_26 = _body(f"{SEC} sa presúva z 23.11.2026 na 26.11.2026, od 08:00 do 14:00 hod.")
EXTEND_24 = _body(f"{SEC} sa predlžuje do 24.11.2026 14:00 hod.")
ADD_26 = _body(f"{SEC} sa dopĺňa o ďalší termín 26.11.2026 od 08:00 do 14:00 hod.")
SHORT_23 = _body(f"{SEC} sa skracuje, dňa 23.11.2026 bude od 08:00 do 10:00 hod.")

_T = [utc(2026, 11, 16, 8, 2)]


def _next() -> object:
    _T[0] = _T[0] + timedelta(minutes=30)
    return _T[0]


@pytest.fixture(autouse=True)
def _clock():
    _T[0] = utc(2026, 11, 16, 8, 2)
    yield


def _send(env, session, subject: str, body: str) -> list[ExtractedEvent]:
    t = _next()
    name = f"m{int(t.timestamp())}.eml"
    _s, evs = _mail(env, session, name, subject, body, t, format_datetime(t))
    return evs


def _one(env, session, subject: str, body: str) -> ExtractedEvent:
    evs = _send(env, session, subject, body)
    assert len(evs) == 1, [(e.kind, e.data) for e in evs]
    return evs[0]


def _confirm(session, ev: ExtractedEvent, **kw) -> dict:
    from vlaky.posta.spracovanie import confirm_event

    set_fake_now(_next())
    ev = confirm_event(session, ev.id, "dispecer1", **kw)
    session.commit()
    return (ev.validation or {}).get("apply_result") or {}


def _closed(session, *when: int) -> bool:
    """Plánovač nepustí vlak s odchodom v miestnom čase ``when`` (rok, mesiac, deň, hodina) s VYPRAVIŤ ZELENÁ."""
    dep = loc(*when)
    return not _green(_decide(session, dep, dep - timedelta(minutes=15)))


def _waiting(ev: ExtractedEvent) -> bool:
    return ev.status == "navrh" and bool(ev.needs_confirmation)


# ====================================================================== zrušenie časti neoverenej výluky
@pytest.mark.parametrize("verified", [False, True], ids=["automaticky", "overeny"])
def test_zrusenie_dna_neoverenej_vyluky_ponecha_ostatne_dni(env, session, verified):
    """ROV 23. – 25.11. denne 08 – 14 zapísaný automaticky (neoverený); dispečer potvrdí zrušenie 24.11. Výluka sa
    rozdelí – 23.11. a 25.11. ostanú (aj ako nepotvrdené „navrh“), vlak v nich nedostane VYPRAVIŤ ZELENÁ."""
    rov = _one(env, session, SUBJ, ROV3)
    assert rov.status == "aplikovane"
    if verified:
        _confirm(session, rov)
    cancel = _one(env, session, CSUBJ, CANCEL_24)
    res = _confirm(session, cancel)
    assert res.get("action") == "rozdelene", res
    parts = [c for c in _live(session) if c.proposed_change_of is None]
    assert len(parts) == 2 and {c.status for c in parts} == {"overene" if verified else "navrh"}, \
        [(c.id, c.status, c.start_utc) for c in parts]
    assert _closed(session, 2026, 11, 23, 10) and _closed(session, 2026, 11, 25, 10)
    day24 = loc(2026, 11, 24, 10)
    assert not [c.id for c in parts if c.start_utc.replace(tzinfo=None) <= day24.replace(tzinfo=None)
                <= c.end_utc.replace(tzinfo=None)], "zrušený deň v registri ostal"


def test_zrusenie_jednej_noci_neoverenej_vyluky_ponecha_ostatne_noci(env, session):
    _one(env, session, SUBJ, NIGHTS)
    res = _confirm(session, _one(env, session, CSUBJ, CANCEL_NIGHT))
    assert res.get("action") == "rozdelene", res
    assert _closed(session, 2026, 11, 26, 23) and _closed(session, 2026, 11, 23, 23)


def test_ukoncenie_neoverenej_vyluky_do_odvolania_ponecha_cas_pred_koncom(env, session):
    """Neoverená výluka do odvolania od 20.11.; potvrdené „ruší sa od 25.11.2026 12:00“ – čas pred 25.11. 12:00 ostáva."""
    _s, (rov,) = _mail(env, session, "ur.eml", "ROV c. 2400/2026 Ruzomberok - Liptovsky Mikulas",
                       _body("úplná výluka v úseku Ružomberok – Liptovský Mikuláš od 20.11.2026 08:00 hod. do odvolania "
                             "(ROV č. 2400/2026)."), utc(2026, 11, 10, 8, 2), "Tue, 10 Nov 2026 09:00:00 +0100")
    _T[0] = utc(2026, 11, 21, 8, 2)
    cancel = _one(env, session, "Zrusenie ROV c. 2400/2026",
                  _body("výluka v úseku Ružomberok – Liptovský Mikuláš podľa ROV č. 2400/2026 vyhlásená do odvolania "
                        "sa ruší od 25.11.2026 12:00 hod."))
    res = _confirm(session, cancel)
    assert res.get("ok") is not False, res
    assert _closed(session, 2026, 11, 23, 10)


# ====================================================================== čakajúce „rovnaké“ oznámenie
def _verified(env, session, body: str = ROV3) -> ExtractedEvent:
    rov = _one(env, session, SUBJ, body)
    _confirm(session, rov)
    return rov


def _same_waiting(env, session, body: str = ROV3) -> ExtractedEvent:
    """Ten istý ROV znova do 72 h pred začiatkom výluky – čaká na dispečera bez záznamu (výluka ho obsahuje)."""
    _T[0] = max(_T[0], utc(2026, 11, 21, 8, 2))
    again = _one(env, session, SUBJ, body)
    info = (again.validation or {}).get("pending_closure") or {}
    assert _waiting(again) and info.get("id") is None and "rovnaká výluka" in str(info.get("note")), info
    return again


def test_rozdelenie_starsim_zrusenim_neskryje_novsie_rovnake_oznamenie(env, session):
    """Overený ROV 23. – 25.11.; čaká zrušenie 24.11.; potom príde ten istý ROV znova (čaká, bez záznamu – výluka ho
    obsahuje). Potvrdené (staršie) zrušenie výluku rozdelí – novšie oznámenie celého ROV dostane záznam, 24.11. nie je
    ZELENÁ."""
    _verified(env, session)
    cancel = _one(env, session, CSUBJ, CANCEL_24)
    again = _same_waiting(env, session)
    assert _confirm(session, cancel).get("action") == "rozdelene"
    session.refresh(again)
    assert (again.validation or {}).get("pending_closure", {}).get("id"), again.validation.get("pending_closure")
    assert _closed(session, 2026, 11, 24, 10)


def test_zmena_jedneho_dna_neskryje_novsie_rovnake_oznamenie(env, session):
    """Overený ROV 23. – 25.11.; čaká zmena času 24.11. (08 – 10); potom príde ten istý ROV znova (čaká bez záznamu).
    Potvrdená (staršia) zmena výluku rozdelí – novšie oznámenie celého ROV dostane záznam, 24.11. 12:00 nie je ZELENÁ."""
    _verified(env, session)
    change = _one(env, session, ZSUBJ, _body(f"{SEC} sa mení: dňa 24.11.2026 bude od 08:00 do 10:00 hod."))
    _same_waiting(env, session)
    assert _confirm(session, change).get("action") == "rozdelene"
    assert _closed(session, 2026, 11, 24, 12)


def test_zamietnutie_potvrdenej_udalosti_neskryje_novsie_rovnake_oznamenie(env, session):
    """Overený ROV 08 – 14; potvrdený dlhší ROV 08 – 16; ten istý dlhší ROV príde znova (čaká bez záznamu); dispečer
    zamietne potvrdený dlhší ROV – výluka sa vráti na 08 – 14, novšie oznámenie 08 – 16 platí pre plánovač."""
    longer = ROV1.replace("od 08:00 do 14:00", "od 08:00 do 16:00")
    _verified(env, session, ROV1)
    lw = _one(env, session, SUBJ, longer)
    _confirm(session, lw)
    _same_waiting(env, session, longer)
    from vlaky.posta.spracovanie import reject_event

    reject_event(session, lw.id, "dispecer1", reason="test")
    session.commit()
    assert _closed(session, 2026, 11, 23, 15)


def test_zmena_v_registri_vyluk_neskryje_rovnake_oznamenie(env, session):
    """Dispečer výluku skráti priamo v registri výluk, kým čaká novšie oznámenie celého ROV: plánovač hneď použije
    poistku čakajúcej udalosti a najbližší cyklus agenta oznámeniu doplní záznam."""
    from vlaky.posta.spracovanie import _blind_reason, _event_closures, process_mailbox

    _verified(env, session)
    again = _same_waiting(env, session)
    c = next(x for x in _live(session) if x.proposed_change_of is None)
    c.end_utc = loc(2026, 11, 24, 14)
    c.version = (c.version or 1) + 1
    session.commit()
    assert _blind_reason(session, again)
    set_fake_now(_next())
    process_mailbox(session, now=_T[0])
    session.commit()
    session.refresh(again)
    assert any(x is not None for x in _event_closures(session, again.id))
    assert not _blind_reason(session, again)
    assert _closed(session, 2026, 11, 25, 10)


def test_novsie_zrusenie_ma_prednost_pred_starsim_rovnakym_oznamenim(env, session):
    """Staršie čakajúce rovnaké oznámenie a novšie potvrdené zrušenie dňa: oznámenie záznam nedostane (novšia správa
    ŽSR platí) – zrušený deň ostáva zrušený."""
    from vlaky.posta.spracovanie import _blind_reason, _event_closures

    _verified(env, session)
    again = _same_waiting(env, session)
    assert _confirm(session, _one(env, session, CSUBJ, CANCEL_24)).get("action") == "rozdelene"
    session.refresh(again)
    assert _event_closures(session, again.id) == (None, None)
    assert not _blind_reason(session, again)
    assert not _closed(session, 2026, 11, 24, 10)


# ====================================================================== zrušenie a staršia čakajúca zmena
@pytest.mark.parametrize("body,when", [
    (EXTEND_24, (2026, 11, 24, 10)),
    (_body(f"{SEC} sa predlžuje do 24.11.2026 06:00 hod."), (2026, 11, 24, 3)),
    (_body(f"{SEC} sa dopĺňa o ďalší termín od 23.11.2026 20:00 do 24.11.2026 12:00 hod."), (2026, 11, 24, 10)),
], ids=["predlzenie", "predlzenie_noc", "doplnenie_dva_dni"])
def test_zrusenie_dna_nezahodi_zvysok_starsej_zmeny(env, session, body, when):
    """ROV 23.11. 08 – 14; čaká zmena, ktorej termín siaha do 24.11.; novšie zrušenie 23.11. sa potvrdí. Zvyšok zmeny
    (24.11.) ostáva pre plánovač a po potvrdení zmeny sa zapíše – zrušený 23.11. sa neobnoví."""
    _one(env, session, SUBJ, ROV1)
    change = _one(env, session, ZSUBJ, body)
    assert _waiting(change)
    assert _confirm(session, _one(env, session, CSUBJ, CANCEL_23)).get("action") == "zrusene"
    session.refresh(change)
    assert (change.validation or {}).get("pending_closure", {}).get("id")
    assert any("pokrýva len časť" in w for w in (change.validation or {}).get("warnings") or [])
    assert _closed(session, *when)
    res = _confirm(session, change)
    assert res.get("action") == "vytvorene", res
    assert _closed(session, *when)
    assert not _closed(session, 2026, 11, 23, 10)


def test_zrusenie_dna_odstrani_zmenu_toho_isteho_dna(env, session):
    """Doplnenie večera 23.11. a novšie zrušenie celého 23.11.: zrušenie pokrýva celý termín doplnenia – dočasný záznam
    zanikne (novšia správa o tom dni platí)."""
    _one(env, session, SUBJ, ROV1)
    change = _one(env, session, ZSUBJ, _body(f"{SEC} sa dopĺňa o ďalší termín 23.11.2026 od 18:00 do 22:00 hod."))
    _confirm(session, _one(env, session, CSUBJ, CANCEL_23))
    session.refresh(change)
    assert (change.validation or {}).get("pending_closure", {}).get("id") is None
    assert _confirm(session, change).get("action") == "nahradene"


# ====================================================================== zmena po staršom zrušení
@pytest.mark.parametrize("cancel_first", [True, False], ids=["zrusenie_skor", "zmena_skor"])
def test_zmena_zrusenej_vyluky_neprepise_iny_termin_rov(env, session, cancel_first):
    """ROV s dvoma termínmi (23.11. a 25.11.); čaká zrušenie 23.11. a presun z 23.11. na 26.11. Po potvrdení oboch
    ostáva 25.11. (iný termín ROV) aj 26.11. (nový termín presunu)."""
    assert len(_send(env, session, SUBJ, ROV2T)) >= 1
    cancel = _one(env, session, CSUBJ, CANCEL_23)
    move = _one(env, session, ZSUBJ, MOVE_23_26)
    for ev in ((cancel, move) if cancel_first else (move, cancel)):
        _confirm(session, ev)
    assert _closed(session, 2026, 11, 25, 10) and _closed(session, 2026, 11, 26, 10)
    assert not _closed(session, 2026, 11, 23, 10)


def test_presun_po_starsom_zruseni_neprepise_doplneny_termin(env, session):
    """ROV 23.11.; čaká doplnenie 26.11., zrušenie 23.11. a presun z 23.11. na 25.11. (v tomto poradí). Potvrdené
    v poradí príchodu: doplnený termín 26.11. ostáva, presun zapíše 25.11. ako samostatnú výluku."""
    _one(env, session, SUBJ, ROV1)
    evs = [_one(env, session, ZSUBJ, ADD_26), _one(env, session, CSUBJ, CANCEL_23),
           _one(env, session, ZSUBJ, MOVE_23_25)]
    results = [_confirm(session, ev) for ev in evs]
    assert results[2].get("action") == "vytvorene", results
    assert _closed(session, 2026, 11, 25, 10) and _closed(session, 2026, 11, 26, 10)


# ====================================================================== zastarané upozornenie pri potvrdení
def test_potvrdenie_neodoberie_cas_pridany_inou_udalostou_bez_upozornenia(env, session):
    """Overený ROV 23.11. 08 – 14; čaká predĺženie do 24.11. 14:00 a ROV znova s 08 – 16 (fronta: „mení výluku
    23.11. 08:00 – 14:00“). Dispečer potvrdí predĺženie, potom ROV: zápis by odobral 24.11. – nezapíše sa, udalosť
    dostane nové upozornenie; druhé potvrdenie už zapíše (rozhodnutie dispečera)."""
    _verified(env, session, ROV1)
    extend = _one(env, session, ZSUBJ, EXTEND_24)
    longer = _one(env, session, SUBJ, ROV1.replace("od 08:00 do 14:00", "od 08:00 do 16:00"))
    assert _waiting(extend) and _waiting(longer)
    _confirm(session, extend)
    res = _confirm(session, longer)
    assert res.get("ok") is False and "sa od spracovania e-mailu zmenila" in str(res.get("error")), res
    session.refresh(longer)
    assert any("sa od spracovania e-mailu zmenila" in w for w in (longer.validation or {}).get("warnings") or [])
    assert _closed(session, 2026, 11, 24, 10)
    res = _confirm(session, longer)
    assert res.get("action") == "aktualizovane", res


def test_skratenie_po_presune_neodoberie_novy_termin_bez_upozornenia(env, session):
    """ROV 23.11.; čaká presun na 25.11. a skrátenie „dňa 23.11. bude 08 – 10“. Po potvrdení presunu sa skrátenie
    výluky na 25.11. netýka – bez nového upozornenia ju nevráti na 23.11."""
    _one(env, session, SUBJ, ROV1)
    move = _one(env, session, ZSUBJ, MOVE_23_25)
    short = _one(env, session, ZSUBJ, SHORT_23)
    _confirm(session, move)
    res = _confirm(session, short)
    assert res.get("ok") is False, res
    assert _closed(session, 2026, 11, 25, 10)


def test_dva_presuny_za_sebou_sa_zapisu_bez_noveho_potvrdenia(env, session):
    """Staršia zmena presunie výluku, novšia ju presunie znova – novšia zmena určuje termín celej výluky (bez nového
    upozornenia)."""
    _verified(env, session, ROV1)
    first = _one(env, session, ZSUBJ, MOVE_23_25)
    second = _one(env, session, ZSUBJ, _body(f"{SEC} sa presúva na 26.11.2026, od 08:00 do 14:00 hod."))
    _confirm(session, first)
    assert _confirm(session, second).get("action") == "aktualizovane"
    assert _closed(session, 2026, 11, 26, 10) and not _closed(session, 2026, 11, 25, 10)


# ====================================================================== duplicity, overenie, prísnosť obmedzenia
def test_ten_isty_presun_dvakrat_sa_nezapise_dvakrat(env, session):
    """ROV, ten istý presun z dvoch zoznamov adries, potom dlhší ROV (novší zapisovateľ): potvrdenie oboch presunov
    zapíše termín 25.11. raz."""
    _one(env, session, SUBJ, ROV1)
    p1, p2 = _one(env, session, ZSUBJ, MOVE_23_25), _one(env, session, ZSUBJ, MOVE_23_25)
    _one(env, session, SUBJ, ROV1.replace("od 08:00 do 14:00", "od 08:00 do 16:00"))
    results = [_confirm(session, p1), _confirm(session, p2)]
    assert [r.get("action") for r in results] == ["vytvorene", "bez_zmeny"], results
    terms = [(c.start_utc, c.end_utc) for c in _live(session) if c.proposed_change_of is None]
    assert len(terms) == len(set(terms)), terms
    assert _closed(session, 2026, 11, 25, 10)


def test_overenie_rov_po_rovnakom_opakovanom_oznameni(env, session):
    """Automaticky zapísaný ROV a rovnaký ROV znova (zapíše sa bez zmeny): dispečer overí pôvodný ROV – výluka je
    overená (nie „nahradené“); zamietnutie ROV overenie vráti."""
    from vlaky.posta.spracovanie import reject_event

    rov = _one(env, session, SUBJ, ROV1)
    _one(env, session, SUBJ, ROV1)
    res = _confirm(session, rov, pending_only=True)
    assert res.get("action") == "potvrdene", res
    (c,) = [x for x in _live(session) if x.proposed_change_of is None]
    assert c.status == "overene"
    reject_event(session, rov.id, "dispecer1", reason="test")
    session.commit()
    session.refresh(c)
    assert c.status == "navrh" and _closed(session, 2026, 11, 23, 10)


def test_prisnejsie_obmedzenie_v_registri_nie_je_miernejsie(env, session):
    """Úplná výluka v registri, staršia zmena na výluku 1. TK (dispečer ju potvrdí s koľajou) a novší rovnaký ROV:
    úplná výluka ostáva, nevznikne nadbytočná samostatná výluka s poznámkou o „miernejšom“ obmedzení."""
    _one(env, session, SUBJ, ROV1)
    change = _one(env, session, ZSUBJ, _body("zmena ROV č. 2331/2026: v úseku Ružomberok – Liptovský Mikuláš dňa "
                                             "23.11.2026 od 08:00 do 14:00 hod. výluka 1. TK namiesto úplnej výluky."))
    _one(env, session, SUBJ, ROV1)
    res = _confirm(session, change, edits={"kind": "kolaj", "track": "1"})
    assert res.get("action") == "nahradene", res
    assert "miernejšie" not in str(res.get("note"))
    rows = [c for c in _live(session) if c.proposed_change_of is None]
    assert [c.kind for c in rows] == ["uplna"], [(c.id, c.kind, c.track) for c in rows]
    assert _closed(session, 2026, 11, 23, 10)


def test_poradie_prisnosti_obmedzeni():
    from vlaky.posta.spracovanie import _kind_covers

    assert _kind_covers("uplna", "kolaj") and _kind_covers("mimoriadnost", "pomala_jazda")
    assert _kind_covers("kolaj", "kolaj")
    assert not _kind_covers("kolaj", "uplna") and not _kind_covers("kolaj", "pomala_jazda")
    assert not _kind_covers("trakcne_vedenie", "kolaj")

