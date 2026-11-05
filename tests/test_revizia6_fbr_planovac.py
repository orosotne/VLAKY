"""6. revízia (strana FB – plánovač, oprava po kontrole): výluky koľají a úplná výluka na spoločnej časti trate,
výluka koľaje bez uvedenej koľaje.

- Výluky koľaje rôznych tratí, ktoré sa stretávajú len v jednej stanici (1. TK Nové Zámky – Šurany a 2. TK Nové Zámky –
  Galanta; 1. TK Bratislava hl. st. – Bratislava-Petržalka a 2. TK Bratislava hl. st. – Pezinok), majú pri stanici
  spoločné len zhlavie, koľajové spojky a súbežné koľaje – nie traťové koľaje: úplnou výlukou nie sú a vlak po trati
  s voľnou koľajou nečaká. Spoločná časť úsekov sa počíta, len ak oba úseky prechádzajú aspoň dvoma tými istými
  dopravňami (Kysak – Košice a Kysak – Kostoľany nad Hornádom ostávajú úplnou výlukou).
- Výluka koľaje bez uvedenej koľaje (napr. druh zmenený vo fronte na „výluka koľaje“ bez koľaje): nie je známe, či ostáva
  zjazdná iná koľaj – vlak nedostane ZELENÚ, dôvod „neuvádza koľaj“; so súbežnou výlukou inej koľaje (aj bez koľaje)
  toho istého úseku či jeho časti je úplnou výlukou (môže ísť o druhú koľaj).

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
BA = "Bratislava hlavná stanica"


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
        rec = planovac.evaluate_train(s, tid, t0, "test FB6r")
        return rec.decision, rec.color, (rec.chosen or {}).get("depart"), list(rec.reasons)


def _full(t0: datetime) -> list:
    with session_scope() as s:
        wins = planovac.closure_windows(s, t0 - timedelta(hours=1), t0 + timedelta(hours=12))
        return [(w.from_station, w.to_station, len(w.edges or ())) for w in wins if w.kind == "uplna"]


def _full_notes(t0: datetime) -> list:
    with session_scope() as s:
        wins = planovac.closure_windows(s, t0 - timedelta(hours=1), t0 + timedelta(hours=12))
        return [w.note for w in wins if w.kind == "uplna"]


JUNCTIONS = {
    # trate sa stretávajú len v stanici Nové Zámky
    "nove_zamky": ([("1", 0, 8, "Nové Zámky", "Šurany", "overene", "ROV 801/2026"),
                    ("2", 0, 8, "Nové Zámky", "Galanta", "overene", "ROV 802/2026")],
                   [("Nové Zámky", "Šurany"), ("Nové Zámky", "Galanta")]),
    # z Bratislavy hl. st. na Petržalku a na Pezinok – spoločné len koľaje pri stanici
    "bratislava": ([("1", 0, 8, BA, "Bratislava-Petržalka", "overene", "ROV 901/2026"),
                    ("2", 0, 8, BA, "Pezinok", "overene", "ROV 902/2026")],
                   [(BA, "Pezinok"), (BA, "Bratislava-Petržalka")]),
}


@pytest.mark.parametrize("case", list(JUNCTIONS))
def test_vyluky_kolaji_roznych_trati_z_jednej_stanice_nie_su_uplnou_vylukou(cfg, fixed_now, case):
    """1. TK jednej trate a 2. TK inej trate z tej istej stanice: žiadne okno úplnej výluky a vlak po každej z tratí
    ide hneď (VYPRAVIŤ) – na svojej trati má voľnú koľaj (v e0a3d0a úplná výluka na spoločných hranách pri stanici –
    vlak Nové Zámky → Šurany ODSTAVIŤ, Bratislava hl. st. → Pezinok ODSTAVIŤ)."""
    t0 = fixed_now
    rows, trains = JUNCTIONS[case]
    _closures(t0, rows)
    assert not _full(t0), _full(t0)
    for n, (a, b) in enumerate(trains):
        decision, color, depart, reasons = _evaluate(f"9790{n}{len(case)}", t0, t0 + timedelta(hours=1), a, b)
        assert decision == "VYPRAVIŤ", (case, a, b, decision, color, depart, reasons)
        assert not any("počíta sa ako úplná výluka" in r for r in reasons), reasons


def test_vyluky_kolaji_na_spolocnom_medzistanicnom_useku_ostavaju_uplnou_vylukou(cfg, fixed_now):
    """Kontrola: 1. TK Kysak – Košice a 2. TK Kysak – Kostoľany nad Hornádom (spoločné dopravne Kysak a Kostoľany) –
    na spoločnom úseku úplná výluka, vlak Kysak → Košice nie je VYPRAVIŤ ZELENÁ."""
    t0 = fixed_now
    _closures(t0, [("1", 0, 6, "Kysak", "Košice", "overene", "ROV 101/2026"),
                   ("2", 0, 6, "Kysak", KK, "overene", "ROV 102/2026")])
    assert _full(t0)
    decision, color, _depart, reasons = _evaluate("97911", t0, t0 + timedelta(hours=1), "Kysak", "Košice")
    assert not (decision == "VYPRAVIŤ" and color == "ZELENÁ"), (decision, color, reasons)


@pytest.mark.parametrize("rows", [
    [("1", 0, 8, "Kysak", "Košice", "overene", "ROV 161/2026"), ("", 0, 8, "Kysak", "Košice", "overene", "ROV 162/2026")],
    [("", 0, 8, "Kysak", "Košice", "overene", "ROV 163/2026")],
], ids=["znama_a_neznama_kolaj", "len_neznama_kolaj"])
def test_vyluka_kolaje_bez_kolaje_nie_je_zelena(cfg, fixed_now, rows):
    """FE1 (zvyšok): výluka koľaje bez uvedenej koľaje (sama, alebo spolu s výlukou 1. TK v tom istom čase a úseku) –
    nie je známe, či ostáva zjazdná iná koľaj: vlak Kysak → Košice nedostane VYPRAVIŤ ZELENÁ, dôvod povie, že výluka
    neuvádza koľaj (v e0a3d0a VYPRAVIŤ ZELENÁ)."""
    t0 = fixed_now
    _closures(t0, rows)
    decision, color, _depart, reasons = _evaluate("9792" + str(len(rows)), t0, t0 + timedelta(hours=1), "Kysak",
                                                  "Košice")
    assert color != "ZELENÁ", (decision, color, reasons)
    assert any("neuvádza koľaj" in r for r in reasons), reasons


def test_vyluka_kolaje_so_znamou_kolajou_ostava_zelena(cfg, fixed_now):
    """Kontrola: samotná výluka 1. TK (druhá koľaj zjazdná) – zdržanie, ZELENÁ dovolená (bez poistky „neuvádza koľaj“)."""
    t0 = fixed_now
    _closures(t0, [("1", 0, 8, "Kysak", "Košice", "overene", "ROV 164/2026")])
    decision, color, _depart, reasons = _evaluate("97930", t0, t0 + timedelta(hours=1), "Kysak", "Košice")
    assert not any("neuvádza koľaj" in r for r in reasons), reasons
    assert decision == "VYPRAVIŤ" and color == "ZELENÁ", (decision, color, reasons)


@pytest.mark.parametrize("rows,a,b", [
    ([("1", 0, 8, "Kysak", "Košice", "overene", "ROV 165/2026"), ("", 0, 8, "Kysak", "Košice", "overene", "ROV 166/2026")],
     "Kysak", "Košice"),
    ([("", 0, 8, "Kysak", "Košice", "overene", "ROV 167/2026"), ("", 0, 6, "Kysak", "Košice", "navrh", "ROV 168/2026")],
     "Kysak", "Košice"),
    ([("2", 0, 8, "Kysak", KK, "overene", "ROV 169/2026"), ("", 0, 8, "Kysak", "Košice", "overene", "ROV 170/2026")],
     "Kysak", KK),
], ids=["znama_a_neznama", "dve_nezname", "cast_useku_a_neznama"])
def test_vyluka_kolaje_bez_kolaje_so_subeznou_vylukou_kolaje_je_uplna(cfg, fixed_now, rows, a, b):
    """FE1 (zvyšok): výluka koľaje bez uvedenej koľaje spolu s inou výlukou koľaje (1. TK, druhá bez koľaje, 2. TK len
    na časti úseku) v tom istom čase – môže ísť o druhú koľaj: v čase prekrytia úplná výluka s poznámkou „neuvádza
    koľaj“ a vlak cez spoločnú časť nie je VYPRAVIŤ (v e0a3d0a sa výluka bez koľaje nekombinovala – VYPRAVIŤ ZELENÁ)."""
    t0 = fixed_now
    _closures(t0, rows)
    notes = _full_notes(t0)
    assert notes and all("neuvádza koľaj" in n for n in notes), notes
    decision, color, depart, reasons = _evaluate("9794" + str(len(a) + len(rows)), t0, t0 + timedelta(hours=1), a, b)
    assert decision != "VYPRAVIŤ", (decision, color, depart, reasons)
    assert any("počíta sa ako úplná výluka" in r for r in reasons), reasons


def test_samotna_vyluka_kolaje_bez_kolaje_nie_je_uplna(cfg, fixed_now):
    """Kontrola: samotná výluka koľaje bez koľaje (bez súbežnej výluky inej koľaje) nie je úplnou výlukou – vlak ide hneď
    (VYPRAVIŤ), len nie ZELENÁ (koľaj treba overiť)."""
    t0 = fixed_now
    _closures(t0, [("", 0, 8, "Kysak", "Košice", "overene", "ROV 171/2026")])
    assert not _full(t0), _full(t0)
    decision, color, _depart, reasons = _evaluate("97950", t0, t0 + timedelta(hours=1), "Kysak", "Košice")
    assert decision == "VYPRAVIŤ" and color != "ZELENÁ", (decision, color, reasons)
