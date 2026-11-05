"""6. revízia (strana FB – plánovač): výluky oboch traťových koľají s rôznym členením úseku (FR2).

Výluky koľaje rôznych koľají, ktoré sa v čase prekrývajú na tých istých hranách siete, sú v čase prekrytia úplnou
výlukou – aj keď ich úseky nie sú rovnaké: 2. TK len na časti úseku 1. TK, 2. TK po medzistaničných úsekoch, 1. TK
Senec – Galanta a 2. TK Senec – Sládkovičovo. Vlak cez úsek, kde sú vylúčené obe koľaje, nesmie dostať VYPRAVIŤ
ZELENÁ s odchodom hneď. Susedné úseky (spoločná je len dopravňa – v stanici je koľají viac) a výluky tej istej koľaje
úplnou výlukou nie sú.

Všetky vlaky, výluky a osoby sú fiktívne.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from vlaky.db import session_scope
from vlaky.hranice import seed_borders
from vlaky.models import Closure, Driver, DriverShift, Path, Train
from vlaky.planovanie import planovac

KK = "Kostoľany nad Hornádom"


def _planned(number: str, ready: datetime, origin: str, destination: str, traction: str = "E") -> int:
    with session_scope() as s:
        seed_borders(s)
        t = Train(number=number, origin=origin, destination=destination, traction=traction, gauge="1435",
                  vmax_kmh=100, status="planovany", ready_utc=ready, deadline_utc=ready + timedelta(hours=10),
                  length_m=500, weight_t=1500)
        s.add(t)
        s.flush()
        s.add(Path(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena", departure_utc=ready))
        d = Driver(name=f"R. {number} FIKTÍVNY")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=ready - timedelta(minutes=80), status="planovana"))
        return t.id


def _closures(t0: datetime, rows: list) -> None:
    """(koľaj, od h, do h, úsek od, úsek do, stav, číslo) – výluky koľaje."""
    with session_scope() as s:
        for c in s.scalars(select(Closure)):
            s.delete(c)
        for track, a, b, frm, to, status, ref in rows:
            s.add(Closure(kind="kolaj", from_station=frm, to_station=to, track=track, start_utc=t0 + timedelta(hours=a),
                          end_utc=t0 + timedelta(hours=b), status=status, source="manual", external_ref=ref,
                          note="FIKTÍVNA"))


def _evaluate(number: str, t0: datetime, ready: datetime, origin: str, destination: str):
    tid = _planned(number, ready, origin, destination)
    with session_scope() as s:
        rec = planovac.evaluate_train(s, tid, t0, "test FB6")
        return rec.decision, rec.color, (rec.chosen or {}).get("depart"), list(rec.reasons)


CASES = {
    # 2. TK len na časti úseku 1. TK (Kysak – Kostoľany nad Hornádom leží v úseku Kysak – Košice)
    "cast_useku": [("1", 0, 6, "Kysak", "Košice", "overene", "ROV 101/2026"),
                   ("2", 0, 6, "Kysak", KK, "overene", "ROV 102/2026")],
    # 2. TK dvoma rozkazmi po medzistaničných úsekoch, 1. TK jedným rozkazom na celý úsek
    "dve_polovice": [("1", 0, 6, "Kysak", "Košice", "overene", "ROV 101/2026"),
                     ("2", 0, 6, "Kysak", KK, "overene", "ROV 102/2026"),
                     ("2", 0, 6, KK, "Košice", "overene", "ROV 103/2026")],
    # 1. TK overená, 2. TK čakajúca na časti úseku (opačné poradie dopraví)
    "cast_useku_navrh": [("1", 0, 6, "Kysak", "Košice", "overene", "ROV 101/2026"),
                         ("2", 0, 6, "Košice", KK, "navrh", "ROV 102/2026")],
    # Senec – Galanta (1. TK) a Senec – Sládkovičovo (2. TK)
    "senec_sladkovicovo": [("1", 0, 6, "Senec", "Galanta", "overene", "ROV 4101/2026"),
                           ("2", 0, 6, "Senec", "Sládkovičovo", "overene", "ROV 4102/2026")],
}
ROUTES = {"senec_sladkovicovo": ("Senec", "Galanta")}


@pytest.mark.parametrize("case", list(CASES))
def test_fr2_obe_koleje_na_spolocnej_casti_usekov_su_uplna_vyluka(cfg, fixed_now, case):
    """FR2: v čase, keď sú na spoločných hranách vylúčené obe koľaje, vlak úsekom nejde – nie VYPRAVIŤ ZELENÁ s odchodom
    hneď; dôvod odporúčania povie, že sa to počíta ako úplná výluka (v 631f185 dve zdržania +3/+15 min a ZELENÁ)."""
    t0 = fixed_now
    ready = t0 + timedelta(hours=1)
    _closures(t0, CASES[case])
    origin, dest = ROUTES.get(case, ("Kysak", "Košice"))
    decision, color, depart, reasons = _evaluate("9760" + str(list(CASES).index(case)), t0, ready, origin, dest)
    assert not (decision == "VYPRAVIŤ" and color == "ZELENÁ"), (case, decision, color, depart, reasons)
    assert datetime.fromisoformat(depart) >= t0 + timedelta(hours=6) - timedelta(minutes=30), (case, depart, reasons)
    assert any("počíta sa ako úplná výluka" in r for r in reasons), reasons


def test_fr2_okno_uplnej_vyluky_je_len_na_spolocnych_hranach(cfg, fixed_now):
    """Okno úplnej výluky z rôznych úsekov sa obmedzí na spoločné hrany (``edges``) a čas prekrytia; vlak na inej trati
    (Kysak → Prešov) ho nemá."""
    t0 = fixed_now
    _closures(t0, [("1", 0, 6, "Kysak", "Košice", "overene", "ROV 101/2026"),
                   ("2", 2, 9, "Kysak", KK, "navrh", "ROV 102/2026")])
    with session_scope() as s:
        wins = planovac.closure_windows(s, t0 - timedelta(hours=1), t0 + timedelta(hours=12))
    full = [w for w in wins if w.kind == "uplna"]
    assert len(full) == 1, [(w.kind, w.from_station, w.to_station, w.edges is not None) for w in wins]
    w = full[0]
    assert (w.start, w.end) == (t0 + timedelta(hours=2), t0 + timedelta(hours=6))
    assert w.edges and w.status == "navrh" and {w.from_station, w.to_station} == {"Kysak", KK}, w
    from vlaky.siet.graf import network

    net = network()
    assert set(w.edges) <= net.closure_edges("Kysak", "Košice") and set(w.edges) <= net.closure_edges("Kysak", KK)
    decision, color, _depart, reasons = _evaluate("97610", t0, t0 + timedelta(hours=3), "Kysak", "Prešov")
    assert not any("úplná výluka" in r for r in reasons), (decision, color, reasons)


@pytest.mark.parametrize("rows", [
    # susedné úseky – spoločná je len dopravňa Kostoľany nad Hornádom (v stanici je koľají viac)
    [("1", 0, 6, "Kysak", KK, "overene", "ROV 101/2026"), ("2", 0, 6, KK, "Košice", "overene", "ROV 102/2026")],
    # tá istá koľaj na časti úseku – stále jedna koľaj zjazdná
    [("1", 0, 6, "Kysak", "Košice", "overene", "ROV 101/2026"), ("1", 0, 6, "Kysak", KK, "overene", "ROV 102/2026")],
    # iný čas – koľaje vylúčené po sebe, nie naraz
    [("1", 0, 3, "Kysak", "Košice", "overene", "ROV 101/2026"), ("2", 3, 6, "Kysak", KK, "overene", "ROV 102/2026")],
], ids=["susedne_useky", "rovnaka_kolaj", "iny_cas"])
def test_fr2_bez_spolocnej_casti_a_casu_nie_je_uplna_vyluka(cfg, fixed_now, rows):
    t0 = fixed_now
    _closures(t0, rows)
    with session_scope() as s:
        wins = planovac.closure_windows(s, t0 - timedelta(hours=1), t0 + timedelta(hours=12))
    assert not [w for w in wins if w.kind == "uplna"], [(w.from_station, w.to_station, w.start, w.end) for w in wins]
