"""Ôsma revízia – texty registra pri výluke do odvolania a dátumy ďaleko dopredu z e-mailov.

* Upozornenia fronty a dôvody potvrdenia uvádzajú pri výluke do odvolania „od … do odvolania“, nie jej technický
  koniec (ten sa len posúva – dispečer by ho čítal ako skutočný koniec výluky).
* Objednávka s termínom viac ako rok dopredu (pravdepodobne preklep v roku) a avízo s príchodom ďaleko od dnešného dňa
  čakajú na dispečera – rovnako ako formulár vlaku, ktorý pripravenosť viac ako rok dopredu neprijme.

Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

from vlaky.hranice import seed_borders
from vlaky.posta.validacia import validate_event
from test_revizia7_odobratie_casu import _mail, env, utc  # noqa: F401 – env

ROV_UR = ("Dobrý deň,\n\núplná výluka v úseku Ružomberok – Liptovský Mikuláš od 20.11.2026 08:00 hod. do odvolania "
          "(ROV č. 2400/2026).\n\nOR Žilina\n")


def test_upozornenie_fronty_pri_vyluke_do_odvolania_neuvadza_technicky_koniec(env, session):
    """Výluka do odvolania v registri, potom to isté oznámenie so skorším začiatkom: fronta povie „Mení výluku …
    od 20.11. 08:00 do odvolania“, nie dátum technického konca."""
    from vlaky.models import Closure

    _s, (rov,) = _mail(env, session, "ur.eml", "ROV c. 2400/2026 Ruzomberok - Liptovsky Mikulas", ROV_UR,
                       utc(2026, 11, 10, 8, 2), "Tue, 10 Nov 2026 09:00:00 +0100")
    c = session.query(Closure).one()
    assert c.until_revoked
    technical = c.end_utc
    _s, (again,) = _mail(env, session, "ur2.eml", "ROV c. 2400/2026 Ruzomberok - Liptovsky Mikulas",
                         ROV_UR.replace("od 20.11.2026 08:00", "od 20.11.2026 06:00"), utc(2026, 11, 18, 8, 2),
                         "Wed, 18 Nov 2026 09:00:00 +0100")
    texts = list((again.validation or {}).get("warnings") or []) + list(
        (again.validation or {}).get("confirmation_reasons") or [])
    mine = [t for t in texts if f"#{c.id}" in t]
    assert mine, texts
    assert any("od 20.11. 08:00 do odvolania" in t for t in mine), mine
    tech = f"{technical:%d.%m.}"
    assert not any(tech in t for t in mine), (tech, mine)


def test_objednavka_viac_ako_rok_dopredu_caka_na_dispecera(session, fixed_now):
    order = {"customer": "FIKTÍVNA s.r.o.", "origin": "Kuty", "destination": "Zilina-Teplicka",
             "earliest_departure": "2028-11-20T05:00:00+00:00", "deadline": "2028-11-21T13:00:00+00:00",
             "wagons": "22", "rid": False}
    vr = validate_event(session, "objednavka", dict(order), confidence=0.99)
    assert vr.needs_confirmation and any("viac ako rok dopredu" in r for r in vr.reasons), vr.reasons
    assert any("najmä rok" in w for w in vr.warnings)
    near = {**order, "earliest_departure": "2026-11-20T05:00:00+00:00", "deadline": "2026-11-21T13:00:00+00:00"}
    vr = validate_event(session, "objednavka", near, confidence=0.99)
    assert not any("viac ako rok dopredu" in r for r in vr.reasons), vr.reasons


def test_avizo_s_prichodom_daleko_od_dnes_caka_na_dispecera(session, fixed_now):
    seed_borders(session)
    far = {"train_number": "47250", "border": "Čadca", "eta": "2027-02-03T12:35:00+00:00"}
    vr = validate_event(session, "avizo", dict(far), confidence=0.99)
    assert vr.needs_confirmation and any("ďaleko od dnešného dňa" in r for r in vr.reasons), vr.reasons
