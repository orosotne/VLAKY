"""5. revízia – integrácia požiadaviek medzi oblasťami (strana plánovača).

- (4) obmedzenie (nápravová hmotnosť, dĺžka či hmotnosť vlaku – druh výluky „obmedzenie“) na trase vlaku v čase jeho
  jazdy: vlak sa nezastaví, ale nedostane ZELENÚ – dôvod „obmedzenie na trase – overte parametre vlaku“;
- (5) výluka koľaje so všetkými koľajami („všetky“) a výluky dvoch rôznych neprázdnych koľají toho istého úseku
  v rovnakom čase (aj „ľavá“ a „pravá“) sú pre plánovač úplnou výlukou – nie zdržanie +3/+15 min.

Všetky vlaky, výluky a osoby sú fiktívne.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from vlaky.db import session_scope
from vlaky.hranice import seed_borders
from vlaky.models import Closure, Driver, DriverShift, Path, Train
from vlaky.planovanie import planovac

PHRASE = "obmedzenie na trase – overte parametre vlaku"


def _planned(number: str, ready: datetime, origin: str = "Kysak", destination: str = "Košice",
             traction: str = "E") -> int:
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
    """Register výluk (všetky overené dispečerom): (druh, koľaj, od, do, úsek od, úsek do) – časy v h od t0."""
    with session_scope() as s:
        for c in s.scalars(select(Closure)):
            s.delete(c)
        for kind, track, a, b, frm, to in rows:
            s.add(Closure(kind=kind, from_station=frm, to_station=to, track=track, start_utc=t0 + timedelta(hours=a),
                          end_utc=t0 + timedelta(hours=b), status="overene", source="manual",
                          note="FIKTÍVNA – nápravová hmotnosť nad 22,5 t" if kind == "obmedzenie" else "FIKTÍVNA"))


def _evaluate(number: str, t0: datetime, ready: datetime, **kw):
    tid = _planned(number, ready, **kw)
    with session_scope() as s:
        rec = planovac.evaluate_train(s, tid, t0, "test")
        return (rec.decision, rec.color, datetime.fromisoformat((rec.chosen or {}).get("depart")), list(rec.reasons),
                dict(rec.data_age or {}))


# ------------------------------------------------------------------ (4) obmedzenie na trase
def test_obmedzenie_na_trase_v_case_jazdy_nie_je_zelena(cfg, fixed_now):
    """Obmedzenie Kysak – Košice (napr. nápravová hmotnosť) počas jazdy vlaku: VYPRAVIŤ (obmedzenie neblokuje), ale
    nie ZELENÁ – dôvod „obmedzenie na trase – overte parametre vlaku“ aj v zozname poistiek (web a e-mail neukážu ZELENÉ
    okná). Obmedzenie mimo času jazdy či na inom úseku vlak neobmedzí."""
    t0 = fixed_now
    ready = t0 + timedelta(hours=1)
    _closures(t0, [("obmedzenie", "", 0, 6, "Kysak", "Košice")])
    decision, color, depart, reasons, age = _evaluate("96401", t0, ready)
    assert decision == "VYPRAVIŤ" and depart < t0 + timedelta(hours=2), (decision, depart)      # neblokuje
    assert color == "ORANŽOVÁ", (color, reasons)
    assert any(PHRASE in r.lower() for r in reasons), reasons
    assert PHRASE in age.get("obmedzenie", []), age
    _closures(t0, [("obmedzenie", "", 30, 40, "Kysak", "Košice")])        # až zajtra – plán vlaku sa ho netýka
    decision, color, _depart, reasons, age = _evaluate("96402", t0, ready)
    assert (decision, color) == ("VYPRAVIŤ", "ZELENÁ"), (decision, color, reasons)
    _closures(t0, [("obmedzenie", "", 0, 6, "Žilina", "Púchov")])         # iný úsek
    decision, color, _depart, reasons, age = _evaluate("96403", t0, ready)
    assert (decision, color) == ("VYPRAVIŤ", "ZELENÁ"), (decision, color, reasons)


# ------------------------------------------------------------------ (5) všetky koľaje, rôzne koľaje naraz
def test_vyluka_vsetkych_kolaji_a_dvoch_roznych_kolaji_je_uplna(cfg, fixed_now):
    """Výluka koľaje „všetky“ a výluky „ľavá“ + „pravá“ koľaj toho istého úseku v rovnakom čase: úsek nie je zjazdný –
    elektrický vlak Kysak → Košice odchádza až po výluke a dôvod povie, že sa počíta úplná výluka. Jedna koľaj ostáva
    zdržaním (vlak ide)."""
    t0 = fixed_now
    ready = t0 + timedelta(hours=1)
    sides = [("kolaj", "ľavá", 0, 6, "Kysak", "Košice"), ("kolaj", "pravá", 0, 6, "Košice", "Kysak")]
    for i, rows in enumerate(([("kolaj", "všetky", 0, 6, "Kysak", "Košice")],
                              [("kolaj", "Všetky koľaje", 0, 6, "Kysak", "Košice")], sides)):
        _closures(t0, rows)
        decision, color, depart, reasons, _age = _evaluate(f"9650{i}", t0, ready)
        assert not (decision == "VYPRAVIŤ" and color == "ZELENÁ"), (rows, decision, color)
        assert depart >= t0 + timedelta(hours=6), (rows, decision, depart)
        assert any("počíta sa ako úplná výluka" in r for r in reasons), (rows, reasons)
    _closures(t0, [("kolaj", "ľavá", 0, 6, "Kysak", "Košice")])
    decision, _color, depart, _r, _age = _evaluate("96509", t0, ready)
    assert decision == "VYPRAVIŤ" and depart < t0 + timedelta(hours=2), (decision, depart)
