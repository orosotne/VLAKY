"""Ôsma revízia, piate kolo – register výluk po permutačnom skene finálneho stavu.

* Zamietnutie novšej zmeny vráti výluku do stavu pred ňou, no výluky a termíny, ktoré po nej zapísali staršie správy
  (samostatne zapísané predĺženie, presun iného dňa), ostanú – o ich čase rozhodujú tie správy.

Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

from test_revizia7_odobratie_casu import env, loc, utc  # noqa: F401 – env
from test_revizia8_register import SEC, SUBJ, ZSUBJ, _closed, _confirm, _one  # noqa: F401
from test_revizia8b_register import _reject
from test_revizia8d_register import ROV_A, _cycle, _kryte, _main, _rollback_text


def test_zamietnutie_novsej_zmeny_ponecha_vyluky_starsich_sprav_zapisane_po_nej(env, session):
    """Overený ROV 23.11. 08–14; čakajú presun 24.11. → 26.11., predĺženie do 24.11. 14:00 a (najnovšia) zmena
    „dňa 23.11. bude od 10:00 do 16:00“. Potvrdí sa zmena, potom predĺženie a presun (zapíšu sa ako ďalšie výluky).
    Dispečer zmenu zamietne: platí ROV, presun a predĺženie – 23.11. 15:00 a 20:00 (predĺženie) ostávajú zatvorené."""
    _confirm(session, _one(env, session, SUBJ, ROV_A))
    from test_revizia8_register import _body
    presun = _one(env, session, ZSUBJ, _body(f"{SEC} sa presúva z 24.11.2026 na 26.11.2026, od 08:00 do 14:00 hod."))
    predl = _one(env, session, ZSUBJ, _body(f"{SEC} sa predlžuje do 24.11.2026 14:00 hod."))
    zmena = _one(env, session, ZSUBJ, _body(f"{SEC} sa mení: dňa 23.11.2026 bude od 10:00 do 16:00 hod."))
    assert _confirm(session, zmena).get("action") == "aktualizovane"
    _cycle(env, session)
    r_predl = _confirm(session, predl)
    _cycle(env, session)
    r_presun = _confirm(session, presun)
    _cycle(env, session)
    before = [(c.id, c.start_utc, c.end_utc) for c in _main(session)]
    rej = _reject(session, zmena)
    _cycle(env, session)
    after = [(c.id, c.start_utc, c.end_utc) for c in _main(session)]
    info = (r_predl, r_presun, before, after, _rollback_text(rej))
    assert _kryte(session, 2026, 11, 23, 9), info
    assert _kryte(session, 2026, 11, 23, 15) and _kryte(session, 2026, 11, 23, 20), info
    assert _closed(session, 2026, 11, 23, 15) and _closed(session, 2026, 11, 23, 20), info
    assert _closed(session, 2026, 11, 24, 10) and _closed(session, 2026, 11, 26, 10), info
    assert f"udalosť #{predl.id} zapísaná znova" in _rollback_text(rej), info


def test_zamietnutie_novsej_zmeny_ponecha_samostatnu_vyluku_upravenu_dispecerom(env, session):
    """Ako vyššie, samostatnú výluku predĺženia (24.11.) dispečer upravil v registri výluk: zamietnutie zmeny ju
    nemení (úprava je novšia) – predĺženie sa znova nezapíše, upravená výluka ostáva."""
    from test_revizia8d_register import _uprav
    from vlaky.models import Closure

    _confirm(session, _one(env, session, SUBJ, ROV_A))
    from test_revizia8_register import _body
    predl = _one(env, session, ZSUBJ, _body(f"{SEC} sa predlžuje do 24.11.2026 14:00 hod."))
    zmena = _one(env, session, ZSUBJ, _body(f"{SEC} sa mení: dňa 23.11.2026 bude od 10:00 do 16:00 hod."))
    assert _confirm(session, zmena).get("action") == "aktualizovane"
    _cycle(env, session)
    r = _confirm(session, predl)
    assert r.get("action") == "vytvorene", r
    sep = session.get(Closure, r["id"])
    _uprav(session, sep, end_utc=utc(2026, 11, 24, 16))
    rej = _reject(session, zmena)
    _cycle(env, session)
    session.refresh(sep)
    assert sep.status != "zrusene" and sep.end_utc.replace(tzinfo=None) == utc(2026, 11, 24, 16).replace(tzinfo=None)
    assert f"udalosť #{predl.id} zapísaná znova" not in _rollback_text(rej)
    assert _closed(session, 2026, 11, 24, 15) and _closed(session, 2026, 11, 23, 10)

