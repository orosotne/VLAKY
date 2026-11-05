"""Ôsma revízia, druhé kolo – register výluk po štvrtom nezávislom overení.

* Noc zmeny pri zrušení dňa patrí dňu, ktorým začína (ako pri zrušení dňa výluky v registri) – aj pri nočných
  oknách viacerých dní; zvyšky termínu do odvolania sú do odvolania len vtedy, keď končia jeho koncom.
* Zamietnuté zrušenie vráti staršej zmene, ktorá sa po ňom zapísala len so zvyškom, celý termín.
* Potvrdenie udalosti spracovanej pred zavedením údaja o stave fronty (register_seen) neodoberie výluke čas, ktorý
  jej medzitým pridala iná udalosť, bez nového upozornenia.
* Zmena jedného dňa po predĺžení výluky sa zapíše bez nového upozornenia (určuje len deň, o ktorom hovorí).
* Agentovo orezanie dočasného záznamu nie je úpravou dispečera v registri výluk.
* Overenie výluky (potvrdením rovnakého obsahu) neodvolá zamietnutie duplicitného oznámenia; overenie ROV po zrušení
  jedného dňa výluku len overí (zrušený deň ostáva zrušený).
* Úplná výluka pokrýva obmedzenie rýchlosti; zamietnutý chybný termín nie je „iným termínom“ rovnakého ROV.
* Po zamietnutom zrušení nevznikne druhá rovnaká výluka a ďalšie zrušenie sa dá priradiť.

Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

from vlaky.models import ExtractedEvent
from test_revizia7_odobratie_casu import _live, env, utc  # noqa: F401 – env
from test_revizia8_register import (CANCEL_23, CANCEL_24, CSUBJ, EXTEND_24, ROV1, ROV3, SEC, SUBJ, ZSUBJ, _T,
                                    _body, _clock, _closed, _confirm, _one, _send, _waiting)  # noqa: F401 – _clock


def _reject(session, ev, reason: str = "test") -> ExtractedEvent:
    from vlaky.cas import set_fake_now
    from vlaky.posta.spracovanie import reject_event

    from test_revizia8_register import _next

    set_fake_now(_next())
    ev = reject_event(session, ev.id, "dispecer1", reason=reason)
    session.commit()
    return ev


# ====================================================================== noc a zvyšky termínu
def test_nocne_okna_zmeny_po_zruseni_dna_patria_dnu_ktorym_zacinaju(env, session):
    """Doplnenie nocí 21./22. – 23./24.11. 22:00 – 04:00 a novšie zrušenie 23.11.: noc 22./23. (patrí 22.11.) ostáva
    celá – aj 23.11. 00:00 – 04:00; noc 23./24. (patrí 23.11.) zrušenie zruší."""
    _one(env, session, SUBJ, ROV1)
    add = _one(env, session, ZSUBJ, _body(f"{SEC} sa dopĺňa o ďalšie termíny v nociach 21./22.11. – 23./24.11.2026 vždy "
                                          "od 22:00 do 04:00 hod."))
    _confirm(session, _one(env, session, CSUBJ, CANCEL_23))
    assert _confirm(session, add).get("action") == "vytvorene"
    assert _closed(session, 2026, 11, 23, 2) and _closed(session, 2026, 11, 22, 23)


def test_zvysok_terminu_do_odvolania_pred_zrusenym_dnom_nie_je_do_odvolania(env, session):
    """ROV 24.11.; staršia čakajúca zmena „od 23.11.2026 08:00 do odvolania“; novšie zrušenie 24.11. Po potvrdení zmeny
    je časť pred zrušeným dňom s pevným koncom (inak by ju plánovač bral ako otvorenú a zrušený deň zatvoril znova)."""
    _one(env, session, SUBJ, ROV1.replace("23.11.2026", "24.11.2026"))
    change = _one(env, session, ZSUBJ, _body(f"{SEC} sa mení: od 23.11.2026 08:00 hod. do odvolania."))
    _confirm(session, _one(env, session, CSUBJ, CANCEL_24))
    session.refresh(change)
    assert any("plánovač zatiaľ počíta s celým termínom" in w for w in (change.validation or {}).get("warnings") or [])
    _confirm(session, change)
    rows = sorted((c for c in _live(session) if c.source_event_id == change.id), key=lambda c: c.start_utc)
    assert len(rows) == 2, [(c.id, c.start_utc, c.end_utc, c.until_revoked) for c in rows]
    assert not rows[0].until_revoked and rows[1].until_revoked


def test_zamietnute_zrusenie_vrati_zmene_cely_termin(env, session):
    """ROV 23.11. 08 – 14; staršia predĺženie do 24.11. 14:00; novšie zrušenie 23.11. potvrdené, potom predĺženie
    (zapísal sa len zvyšok 24.11.). Dispečer zrušenie zamietne – predĺženie má opäť celý termín (aj 23.11. 18:00)."""
    _one(env, session, SUBJ, ROV1)
    change = _one(env, session, ZSUBJ, EXTEND_24)
    cancel = _one(env, session, CSUBJ, CANCEL_23)
    _confirm(session, cancel)
    assert _confirm(session, change).get("action") == "vytvorene"
    assert not _closed(session, 2026, 11, 23, 18)
    rej = _reject(session, cancel)
    assert "má opäť celý termín" in str((rej.validation or {}).get("rejection", {}).get("rollback"))
    assert _closed(session, 2026, 11, 23, 18) and _closed(session, 2026, 11, 24, 10)


# ====================================================================== zastarané upozornenie
def test_starsia_udalost_bez_udaja_o_fronte_neodoberie_pridany_cas(env, session):
    """Udalosť spracovaná pred zavedením register_seen: ak výluku po spracovaní e-mailu zapísala iná udalosť
    (predĺženie), prvé potvrdenie sa nezapíše a udalosť dostane nové upozornenie; druhé už áno."""
    rov = _one(env, session, SUBJ, ROV1)
    _confirm(session, rov)
    extend = _one(env, session, ZSUBJ, EXTEND_24)
    longer = _one(env, session, SUBJ, ROV1.replace("od 08:00 do 14:00", "od 08:00 do 16:00"))
    val = dict(longer.validation)
    val.pop("register_seen", None)
    longer.validation = val
    session.commit()
    _confirm(session, extend)
    res = _confirm(session, longer)
    assert res.get("ok") is False and "sa od spracovania e-mailu zmenila" in str(res.get("error")), res
    assert _closed(session, 2026, 11, 24, 10)
    assert _confirm(session, longer).get("action") == "aktualizovane"


def test_zmena_dna_po_predlzeni_sa_zapise_bez_noveho_upozornenia(env, session):
    """Overený ROV 23. – 25.11.; potvrdené predĺženie do 26.11.; novšia zmena „dňa 24.11. bude 08 – 10“ sa zapíše
    hneď (rozdelí výluku) – 26.11. ostáva."""
    _confirm(session, _one(env, session, SUBJ, ROV3))
    extend = _one(env, session, ZSUBJ, _body(f"{SEC} sa predlžuje do 26.11.2026 14:00 hod."))
    change = _one(env, session, ZSUBJ, _body(f"{SEC} sa mení: dňa 24.11.2026 bude od 08:00 do 10:00 hod."))
    _confirm(session, extend)
    res = _confirm(session, change)
    assert res.get("action") == "rozdelene", res
    assert _closed(session, 2026, 11, 26, 10) and _closed(session, 2026, 11, 24, 9)


def test_orezanie_zvysku_nie_je_upravou_dispecera(env, session):
    """Predĺženie čaká, novšie zrušenie 23.11. jeho dočasný záznam oreže na 24.11. – fronta to neukáže ako úpravu
    dispečera v registri a potvrdenie neprepíše termín udalosti orezaným termínom."""
    from vlaky.posta.spracovanie import register_edits

    _one(env, session, SUBJ, ROV1)
    change = _one(env, session, ZSUBJ, EXTEND_24)
    start = (change.data or {}).get("start")
    _confirm(session, _one(env, session, CSUBJ, CANCEL_23))
    session.refresh(change)
    assert register_edits(session, change) == {}
    _confirm(session, change)
    session.refresh(change)
    assert (change.data or {}).get("start") == start
    assert not any(e.get("zdroj") == "register výluk" for e in (change.validation or {}).get("edits") or [])


# ====================================================================== overenie
def test_overenie_ostava_po_zamietnuti_duplicitneho_oznamenia(env, session):
    """Automaticky zapísaný ROV A a rovnaký ROV B (zapísaný bez zmeny): dispečer overí A, potom zamietne B ako
    duplicitu – výluka ostáva overená."""
    a = _one(env, session, SUBJ, ROV1)
    b = _one(env, session, SUBJ, ROV1)
    assert _confirm(session, a, pending_only=True).get("action") == "potvrdene"
    _reject(session, b, "duplicitný e-mail")
    (c,) = [x for x in _live(session) if x.proposed_change_of is None]
    assert c.status == "overene"


def test_overenie_rov_po_zruseni_dna_ho_len_overi(env, session):
    """Automaticky zapísaný ROV 23. – 25.11., potvrdené zrušenie 24.11. (výluka rozdelená); dispečer potom ROV overí:
    časti výluky sú overené, zrušený 24.11. ostáva zrušený."""
    rov = _one(env, session, SUBJ, ROV3)
    assert _confirm(session, _one(env, session, CSUBJ, CANCEL_24)).get("action") == "rozdelene"
    res = _confirm(session, rov, pending_only=True)
    assert res.get("action") == "potvrdene" and res.get("verified_only"), res
    rows = [c for c in _live(session) if c.proposed_change_of is None]
    assert len(rows) == 2 and all(c.status == "overene" for c in rows), [(c.id, c.status) for c in rows]
    assert not _closed(session, 2026, 11, 24, 10)
    assert _closed(session, 2026, 11, 23, 10) and _closed(session, 2026, 11, 25, 10)


# ====================================================================== prísnosť, súrodenci, duplicity
def test_uplna_vyluka_pokryva_obmedzenie_rychlosti(env, session):
    """Úplná výluka v registri; staršia zmena potvrdená ako „obmedzenie 60 km/h“; novší rovnaký ROV vlastní výluku:
    úplná výluka je prísnejšia – nič sa nezapíše ako „prísnejšie“."""
    _one(env, session, SUBJ, ROV1)
    change = _one(env, session, ZSUBJ, _body("zmena ROV č. 2331/2026: v úseku Ružomberok – Liptovský Mikuláš dňa "
                                             "23.11.2026 od 08:00 do 14:00 hod. obmedzenie rýchlosti 60 km/h namiesto "
                                             "úplnej výluky."))
    _one(env, session, SUBJ, ROV1)
    res = _confirm(session, change, edits={"kind": "obmedzenie", "speed_kmh": "60"})
    rows = [c for c in _live(session) if c.proposed_change_of is None]
    assert [c.kind for c in rows] == ["uplna"], [(c.id, c.kind, c.speed_kmh) for c in rows]
    assert "miernejšie" not in str(res.get("note"))


def test_zamietnuty_chybny_termin_nebrani_presunu(env, session):
    """ROV 23.11.; chybný termín 27.11. s rovnakým číslom dispečer zamietne; zmena bez čísla „presúva sa z 23.11. na
    30.11.“, číslo doplní dispečer – presun nahradí termín (zamietnutý záznam nie je iným termínom ROV)."""
    _one(env, session, SUBJ, ROV1)
    wrong = _one(env, session, SUBJ, ROV1.replace("23.11.2026", "27.11.2026"))
    _reject(session, wrong, "chybný termín")
    move = _one(env, session, "Zmena výluky Ruzomberok - Liptovsky Mikulas",
                _body("Oznamujeme zmenu: výluka v úseku Ružomberok – Liptovský Mikuláš sa presúva z 23.11.2026 na "
                      "30.11.2026, od 08:00 do 14:00 hod."))
    assert move.kind == "vyluka_zmena" and not (move.data or {}).get("external_ref"), (move.kind, move.data)
    edits = {"external_ref": "2331/2026"}
    if not (move.data or {}).get("zamer"):
        edits["zamer"] = "presun"
    _confirm(session, move, edits=edits)
    assert _closed(session, 2026, 11, 30, 10) and not _closed(session, 2026, 11, 23, 10)


def test_zamietnute_zrusenie_nevytvori_druhu_rovnaku_vyluku(env, session):
    """Overený ROV; staršie zrušenie celého ROV čaká; novší rovnaký ROV čaká bez záznamu. Potvrdené zrušenie dá
    novšiemu oznámeniu záznam; zamietnutím zrušenia sa výluka vráti – potvrdený novší ROV potom nevytvorí druhú rovnakú
    výluku a ďalšie zrušenie celého ROV sa dá priradiť."""
    _confirm(session, _one(env, session, SUBJ, ROV3))
    cancel = _one(env, session, CSUBJ, _body(f"{SEC} sa ruší."))
    _T[0] = max(_T[0], utc(2026, 11, 21, 8, 2))
    again = _one(env, session, SUBJ, ROV3)
    assert _waiting(again)
    _confirm(session, cancel)
    _reject(session, cancel)
    session.refresh(again)
    _confirm(session, again)
    rows = [c for c in _live(session) if c.proposed_change_of is None]
    assert len(rows) == 1, [(c.id, c.start_utc, c.status, c.source_event_id) for c in rows]
    res = _confirm(session, _one(env, session, CSUBJ, _body(f"{SEC} sa ruší.")))
    assert res.get("ok") is not False, res
    assert not [c for c in _live(session) if c.proposed_change_of is None]


def test_texty_novych_upozorneni(env, session):
    """Poistka čakajúcej „rovnakej“ udalosti a upozornenie pri čiastočnom pokrytí zmeny zrušením – zrozumiteľná
    slovenčina."""
    from vlaky.posta.spracovanie import _blind_reason

    _confirm(session, _one(env, session, SUBJ, ROV3))
    _T[0] = max(_T[0], utc(2026, 11, 21, 8, 2))
    again = _one(env, session, SUBJ, ROV3)
    c = next(x for x in _live(session) if x.proposed_change_of is None)
    c.end_utc = c.end_utc.replace(day=24)
    session.commit()
    assert "register nemusí obsahovať termín z tohto e-mailu" in _blind_reason(session, again)
