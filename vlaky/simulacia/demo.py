"""Ukážkové (FIKTÍVNE) dáta a simulátor polohy vlakov.

Všetky vlaky, výluky, rušňovodiči a časy sú vymyslené – slúžia na predvedenie a testovanie.
Stanice a trate sú skutočné (sieť SR z OpenStreetMap).

  python -m vlaky demo --reset   # vymaže a naplní ukážkovú databázu (nie v produkcii)
  python -m vlaky simulate        # posúva vlaky v jazde po trati (zdroj polohy "sim")
"""
from __future__ import annotations

import math
import os
import random
import secrets
import shutil
import time
from datetime import datetime, timedelta

from sqlalchemy import select

from ..cas import now_utc
from ..config import ROOT, settings
from ..db import Base, engine, init_db, session_scope
from ..hranice import find_border, seed_borders
from ..models import (Closure, Driver, DriverShift, Handover, Locomotive, Path, PositionReport, Train, User)
from ..pouzivatelia import create_user
from ..siet.graf import TrainSpec, haversine, network

DEMO_TRAINS = [
    # číslo, odkiaľ, kam, vstupný priechod, trakcia, rozchod, stav, pripravený (+min), termín (+h), trasa (+min)
    ("95001", "Čadca", "Košice", "CADCA", "E", "1435", "na_ceste_k_hranici", 90, 12, 120),
    ("95002", "Kúty", "Štúrovo", "KUTY", "E", "1435", "v_jazde", -60, 5, -60),
    ("95003", "Rusovce", "Žilina", "RUSOVCE", "E", "1435", "planovany", 180, 10, None),
    ("95004", "Čierna nad Tisou", "Žilina", "CIERNA", "E", "1435", "v_jazde", -150, 8, -150),
    ("95005", "Plaveč", "Košice", "PLAVEC", "M", "1435", "planovany", 60, 6, 60),
    ("95006", "Maťovce ŠRT", "Haniska pri Košiciach", "MATOVCE", "E", "1520", "planovany", 45, 6, 45),
]


def _reset_db() -> None:
    if settings().env == "prod":
        raise RuntimeError("Reset ukážkových dát nie je povolený v produkcii (VLAKY_ENV=prod).")
    from .. import models  # noqa: F401

    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())


DEMO_ENVS = ("dev", "test", "demo")


def seed_demo(reset: bool = False, now: datetime | None = None, copy_emails: bool = True) -> str:
    if settings().env not in DEMO_ENVS:
        raise RuntimeError(f"Ukážkové dáta sa dajú pripraviť len vo vývojovom prostredí (VLAKY_ENV = "
                           f"{', '.join(DEMO_ENVS)}), nie v „{settings().env}“ – fiktívne vlaky a e-maily by sa "
                           f"dostali k skutočným dispečerom.")
    init_db()
    if reset:
        _reset_db()
    now = now or now_utc()
    pw = os.environ.get("VLAKY_DEMO_PASSWORD") or settings().demo_password or secrets.token_urlsafe(12)
    lines = ["UKÁŽKOVÉ DÁTA (FIKTÍVNE) pripravené."]
    with session_scope() as s:
        seed_borders(s)
        for username, role, name, email in (("dispecer", "dispecer", "Dispečer (ukážka)", "dispecer1@dopravca.example"),
                                            ("veduci", "veduci", "Vedúci dispečingu (ukážka)",
                                             "veduci@dopravca.example")):
            if s.scalar(select(User.id).where(User.username == username)) is None:
                create_user(s, username, pw, role, name, email)
                lines.append(f"Prihlásenie: {username} / {pw}")
            else:  # existujúci účet sa nemení (heslo, rola)
                lines.append(f"Účet {username} už existuje – heslo sa nezmenilo.")
        if s.scalar(select(Train.id).limit(1)) is not None and not reset:
            lines.append("Vlaky už existujú – nové sa nepridali (použite --reset).")
            return "\n".join(lines)
        loco_e = Locomotive(number="DEMO-E-001", type="viacsystémový elektrický rušeň (ukážka)", traction="E",
                            systems=["25kV", "3kV", "15kV"])
        loco_m = Locomotive(number="DEMO-M-001", type="motorový rušeň (ukážka)", traction="M")
        s.add_all([loco_e, loco_m])
        s.flush()
        for i, (num, a, b, border, tr, gauge, status, ready, dl, path) in enumerate(DEMO_TRAINS):
            bc = find_border(s, border)
            t = Train(number=num, run_date=now.date().isoformat(), origin=a, destination=b,
                      customer=f"Zákazník {chr(65 + i)} (fiktívny)", entry_border_id=bc.id if bc else None,
                      traction=tr, gauge=gauge, vmax_kmh=100 if tr == "E" else 80, length_m=500 + 30 * i,
                      weight_t=1500 + 150 * i, status=status, ready_utc=now + timedelta(minutes=ready),
                      deadline_utc=now + timedelta(hours=dl),
                      locomotive_id=(loco_e.id if tr == "E" else loco_m.id),
                      notes="FIKTÍVNY ukážkový vlak")
            if status == "v_jazde":
                t.planned_departure_utc = now + timedelta(minutes=ready)
            s.add(t)
            s.flush()
            d = Driver(name=f"Rušňovodič {i + 1} (fiktívny)", phone="")
            s.add(d)
            s.flush()
            shift_start = now + timedelta(minutes=ready - 60)
            if num == "95004":
                shift_start = now - timedelta(hours=10, minutes=30)  # blízko limitu 13 h
            s.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=shift_start,
                              status="prebieha" if shift_start <= now else "planovana"))
            if path is not None:
                s.add(Path(train_id=t.id, train_number=num, kind="ad_hoc", status="pridelena",
                           departure_utc=now + timedelta(minutes=path), note="FIKTÍVNA trasa"))
            if status == "na_ceste_k_hranici" and bc:
                s.add(Handover(train_id=t.id, border_id=bc.id, direction="vstup", status="ocakavany",
                               avizo_eta_utc=now + timedelta(minutes=ready - 60)))
        s.add_all([
            Closure(kind="uplna", from_station="Liptovský Mikuláš", to_station="Poprad-Tatry",
                    start_utc=now + timedelta(hours=3), end_utc=now + timedelta(hours=7), status="overene",
                    source="demo", note="FIKTÍVNA výluka (ukážka)"),
            Closure(kind="trakcne_vedenie", from_station="Nové Zámky", to_station="Štúrovo",
                    start_utc=now + timedelta(minutes=90), end_utc=now + timedelta(hours=4), status="navrh",
                    source="demo", electric_only=True, note="FIKTÍVNE vypnutie trakčného vedenia (nepotvrdené)"),
            Closure(kind="pomala_jazda", from_station="Žilina", to_station="Púchov",
                    start_utc=now - timedelta(days=1), end_utc=now + timedelta(days=5), status="overene",
                    source="demo", speed_kmh=40, note="FIKTÍVNA pomalá jazda 40 km/h"),
        ])
        s.flush()
        # počiatočné polohy idúcich vlakov
        for t in s.scalars(select(Train).where(Train.status == "v_jazde")):
            _sim_position(s, t, now)
    if copy_emails:
        src = ROOT / "data" / "ukazkove_emaily"
        dst = settings().mail_dir
        if src.is_dir():
            os.makedirs(dst, exist_ok=True)
            n = 0
            for f in sorted(src.glob("*.eml")):
                shutil.copy(f, dst / f.name)
                n += 1
            lines.append(f"Do schránky agenta ({dst}) skopírovaných ukážkových e-mailov: {n}")
    try:
        from ..planovanie.planovac import evaluate_all

        with session_scope() as s:
            recs = evaluate_all(s, now, "ukážkové dáta")
            lines.append(f"Vyhodnotených vlakov: {len(recs)}")
            for r in recs:
                lines.append(f"  {r.color:<9} {r.summary}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Vyhodnotenie zlyhalo: {exc!r}")
    return "\n".join(lines)


def _position_at(train: Train, minutes_since_departure: float):
    net = network()
    spec = TrainSpec(traction=train.traction, gauge=train.gauge, vmax_kmh=train.vmax_kmh)
    r = net.route(train.origin, train.destination, spec)
    if r is None:
        return None, None
    frac = max(0.0, min(1.0, minutes_since_departure / max(1.0, r.minutes * 1.1)))
    coords = net.geometry(r)
    target = frac * r.length_m
    acc = 0.0
    for (lo1, la1), (lo2, la2) in zip(coords, coords[1:]):
        d = haversine(la1, lo1, la2, lo2)
        if acc + d >= target:
            f = 0.0 if d == 0 else (target - acc) / d
            return (la1 + f * (la2 - la1), lo1 + f * (lo2 - lo1)), frac
        acc += d
    lo, la = coords[-1]
    return (la, lo), 1.0


def _sim_position(session, train: Train, now: datetime) -> bool:
    if not train.planned_departure_utc:
        return False
    from ..cas import ensure_utc

    elapsed = (now - ensure_utc(train.planned_departure_utc)).total_seconds() / 60.0
    pos, frac = _position_at(train, elapsed)
    if pos is None:
        return False
    lat, lon = pos[0] + random.uniform(-0.00008, 0.00008), pos[1] + random.uniform(-0.00008, 0.00008)
    net = network()
    m = net.match(lat, lon, 300)
    st = net.nearest_station(lat, lon)
    session.add(PositionReport(train_id=train.id, train_number=train.number, ts_utc=now, lat=lat, lon=lon,
                               speed_kmh=0.0 if frac >= 1.0 else round(random.uniform(45, 85), 0),
                               source="sim", accuracy_m=8.0,
                               matched_edge=m["edge"] if m else None, snapped_lat=m["lat"] if m else None,
                               snapped_lon=m["lon"] if m else None, off_track_m=m["dist_m"] if m else None,
                               nearest_station=st.name if st else ""))
    if frac >= 1.0:
        from ..planovanie.stav import set_status

        set_status(session, train, "ukonceny", now)
    return True


def run_simulator(interval_s: float = 10.0, max_cycles: int | None = None) -> int:
    """Posúva vlaky v jazde po trati (polohy so zdrojom „sim“). Ctrl+C ukončí."""
    if settings().env not in DEMO_ENVS:
        raise RuntimeError("Simulátor polohy beží len vo vývojovom prostredí – v produkcii by vytváral "
                           "fiktívne polohy skutočných vlakov.")
    init_db()
    cycles = 0
    print("Simulátor polohy beží (FIKTÍVNE polohy). Ctrl+C ukončí.", flush=True)
    try:
        while max_cycles is None or cycles < max_cycles:
            now = now_utc()
            with session_scope() as s:
                n = 0
                for t in s.scalars(select(Train).where(Train.status == "v_jazde")):
                    n += _sim_position(s, t, now)
            print(f"{now.isoformat()} – polohy: {n}", flush=True)
            cycles += 1
            time.sleep(interval_s)
    except KeyboardInterrupt:
        pass
    return 0


__all__ = ["seed_demo", "run_simulator", "math"]
