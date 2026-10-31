"""Návrh nasadenia rušňovodičov na vlaky (fáza F4, sekcia 9.5 PROMPT.md) – CP-SAT (Google OR-Tools).

Obmedzenia:
  * pracovná zmena (príprava + jazda + odovzdanie rušňa) najviac crew_max_duty_min (13 h),
  * medzi zmenami odpočinok aspoň crew_min_rest_h (zákon 462/2007 Z. z.: 11 h),
  * prekrývajúce sa vlaky nemôže viesť ten istý rušňovodič,
  * znalosť trate: ak má rušňovodič vyplnený zoznam (čísla tratí), trasa vlaku ho musí obsahovať,
  * už naplánované zmeny rušňovodičov sú obsadený čas.
Cieľ: čo najviac vlakov s rušňovodičom, potom čo najkratšie presuny z domovskej stanice.

Výsledok je NÁVRH pre dispečera/plánovača – agent nič nemení ani neposiela.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..cas import ensure_utc, fmt, now_utc
from ..models import Driver, DriverShift, Recommendation, Train
from ..siet.graf import TrainSpec, network
from .planovac import DONE_STATES, current_params

CREW_MIN_REST_H = 11.0  # zákon 462/2007 Z. z. (denný odpočinok)


def _train_window(session: Session, t: Train, p) -> Optional[tuple]:
    rec = session.scalar(select(Recommendation).where(Recommendation.train_id == t.id)
                         .order_by(Recommendation.id.desc()))
    ch = (rec.chosen or {}) if rec else {}
    if ch.get("depart") and ch.get("arrival_p90"):
        dep = datetime.fromisoformat(ch["depart"])
        arr = datetime.fromisoformat(ch["arrival_p90"])
    else:
        dep = ensure_utc(t.planned_departure_utc or t.ready_utc) if (t.planned_departure_utc or t.ready_utc) \
            else None
        if dep is None:
            return None
        r = network().route(t.origin, t.destination, TrainSpec(traction=t.traction, gauge=t.gauge,
                                                               vmax_kmh=t.vmax_kmh))
        if r is None:
            return None
        arr = dep + timedelta(minutes=r.minutes * 1.3)
    start = dep - timedelta(minutes=p.crew_prep_min)
    end = arr + timedelta(minutes=p.crew_post_min)
    return start, end


def _route_lines(t: Train) -> set:
    r = network().route(t.origin, t.destination, TrainSpec(traction=t.traction, gauge=t.gauge))
    if r is None:
        return set()
    return {ref for ei in r.edges for ref in network().edges[ei].line_ref}


def _deadhead_min(home: str, origin: str) -> int:
    if not home:
        return 0
    if home.strip().lower() == origin.strip().lower():
        return 0
    r = network().route(home, origin, TrainSpec(traction="M", vmax_kmh=120))
    return int(r.minutes) if r else 240


def propose_driver_assignment(session: Session, now: Optional[datetime] = None, horizon_h: int = 24,
                              time_limit_s: float = 10.0) -> dict:
    from ortools.sat.python import cp_model

    now = now or now_utc()
    p = current_params(session)
    max_duty = timedelta(minutes=p.crew_max_duty_min)
    rest = timedelta(hours=CREW_MIN_REST_H)
    trains, windows = [], {}
    for t in session.scalars(select(Train).where(Train.status.notin_(DONE_STATES | {"v_jazde"}))):
        has_shift = session.scalar(select(DriverShift.id).where(DriverShift.train_id == t.id,
                                                                DriverShift.status.in_(("planovana", "prebieha"))))
        if has_shift:
            continue
        w = _train_window(session, t, p)
        if w is None or w[0] > now + timedelta(hours=horizon_h) or w[1] < now:
            continue
        trains.append(t)
        windows[t.id] = w
    drivers = list(session.scalars(select(Driver).where(Driver.active.is_(True))))
    result: dict = {"created": now.isoformat(), "assignments": [], "unassigned": [], "notes": []}
    if not trains:
        result["notes"].append("Žiadne vlaky bez rušňovodiča v horizonte.")
        return result
    if not drivers:
        result["unassigned"] = [{"train": t.number, "reason": "žiadni aktívni rušňovodiči"} for t in trains]
        return result

    busy: dict = {d.id: [] for d in drivers}
    for sh in session.scalars(select(DriverShift).where(DriverShift.status.in_(("planovana", "prebieha")))):
        if sh.driver_id in busy:
            s0 = ensure_utc(sh.start_utc)
            busy[sh.driver_id].append((s0, ensure_utc(sh.planned_end_utc) if sh.planned_end_utc else s0 + max_duty))
    lines = {t.id: _route_lines(t) for t in trains}

    model = cp_model.CpModel()
    x, reasons = {}, {t.id: [] for t in trains}
    for d in drivers:
        knows = {str(k) for k in (d.route_knowledge or [])}
        for t in trains:
            s0, e0 = windows[t.id]
            if e0 - s0 > max_duty:
                reasons[t.id].append(f"jazda dlhšia ako {p.crew_max_duty_min / 60:.0f} h – treba výmenu na trase")
                continue
            if knows and lines[t.id] and not (lines[t.id] <= knows):
                reasons[t.id].append(f"{d.name}: chýba znalosť tratí {', '.join(sorted(lines[t.id] - knows))}")
                continue
            if any(s0 < b1 + rest and b0 < e0 + rest for b0, b1 in busy[d.id]):
                reasons[t.id].append(f"{d.name}: obsadený alebo bez odpočinku {CREW_MIN_REST_H:.0f} h")
                continue
            x[d.id, t.id] = model.NewBoolVar(f"x_{d.id}_{t.id}")
    for t in trains:
        vs = [x[d.id, t.id] for d in drivers if (d.id, t.id) in x]
        if vs:
            model.Add(sum(vs) <= 1)
    for d in drivers:
        mine = [t for t in trains if (d.id, t.id) in x]
        for i, a in enumerate(mine):
            for b in mine[i + 1:]:
                a0, a1 = windows[a.id]
                b0, b1 = windows[b.id]
                if a0 < b1 + rest and b0 < a1 + rest:  # prekryv alebo krátky odpočinok
                    model.Add(x[d.id, a.id] + x[d.id, b.id] <= 1)
    dead = {(d.id, t.id): _deadhead_min(d.home_station, t.origin) for d in drivers for t in trains
            if (d.id, t.id) in x}
    model.Maximize(sum(10000 * v for v in x.values()) - sum(dead[k] * v for k, v in x.items()))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    status = solver.Solve(model)
    assigned = set()
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for (did, tid), v in x.items():
            if solver.Value(v):
                d = next(dr for dr in drivers if dr.id == did)
                t = next(tr for tr in trains if tr.id == tid)
                s0, e0 = windows[tid]
                result["assignments"].append({
                    "train": t.number, "train_id": t.id, "driver": d.name, "driver_id": d.id,
                    "start": s0.isoformat(), "end": e0.isoformat(), "start_txt": fmt(s0), "end_txt": fmt(e0),
                    "duty_h": round((e0 - s0).total_seconds() / 3600, 1), "deadhead_min": dead[(did, tid)]})
                assigned.add(tid)
        result["solver"] = "optimálne" if status == cp_model.OPTIMAL else "prípustné (časový limit)"
    for t in trains:
        if t.id not in assigned:
            why = "; ".join(sorted(set(reasons[t.id]))) or "nedostatok voľných rušňovodičov"
            result["unassigned"].append({"train": t.number, "train_id": t.id, "reason": why})
    result["assignments"].sort(key=lambda a: a["start"])
    return result
