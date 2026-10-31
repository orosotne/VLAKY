"""Plánovač a vyhodnocovanie vlakov – jadro Agenta dispečera (sekcie 2.1, 9 a 10 v PROMPT.md).

Verejné funkcie (signatúry sú záväzné, volajú ich ostatné moduly):

    evaluate_train(session, train_id, now=None, trigger="") -> Recommendation | None
    evaluate_all(session, now=None, trigger="") -> list[Recommendation]
    plan_order(session, order_id, now=None) -> dict

Postup pre jeden vlak:
 1. trasa po reálnej sieti (OSM) podľa rozchodu, trakcie a rýchlosti vlaku (+ obchádzky okolo výluk),
 2. výluky z registra (aj nepotvrdené – konzervatívne) → časové okná na úsekoch s rezervami,
 3. čas pripravenosti (preberanie na hranici: avízo/príchod + doba preberania P50),
 4. pridelené trasy (odchod mimo trasy = nová trasa s lehotou), pracovný čas rušňovodiča (13 h),
 5. rozhodovací modul (simulácia Monte Carlo, semafor, očakávané náklady),
 6. návrh trasy (dopravne s časmi), okná bezpečného odchodu, priorita a vysvetlenie.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..cas import ensure_utc, fmt, local, now_utc, plus_minutes, tz
from ..config import settings
from ..models import (Closure, DriverShift, Handover, KeyValue, LocationInfo, Order, ParameterSet, Path,
                      PositionReport, Recommendation, Train)
from ..rozhodovanie import (CERVENA, ESKALOVAT, ODSTAVIT, ORANZOVA, POCKAT, PRESMEROVAT, VYPRAVIT, ZELENA,
                            Closure as DClosure, Evaluation, Params, Recommendation as DRecommendation,
                            Route as DRoute, Segment, _normals, evaluate, recommend, select_best)
from ..siet.graf import WAIT_TYPES, Network, Route as GRoute, TrainSpec, network

POKRACOVAT = "POKRAČOVAŤ"
DONE_STATES = {"ukonceny", "zruseny", "odovzdany"}
RUN_MARGIN = 0.04            # prirážka k vypočítanej jazdnej dobe (UIC 451-1 pre nákladné vlaky ~4 %);
                             # pri časoch z pridelenej trasy ŽSR sa nepridáva (prirážky už obsahuje)
NO_DEADLINE_MIN = 7 * 24 * 60
NO_PATH_SENTINEL = -10 ** 9  # „žiadna pridelená trasa“ → každý odchod vyžaduje novú trasu
BLOCKING = {"uplna", "mimoriadnost", "trakcne_vedenie"}


# ------------------------------------------------------------------ parametre
def current_params(session: Session) -> Params:
    """Posledná platná sada parametrov (ParameterSet) zlúčená s predvolenými hodnotami."""
    ps = session.scalar(select(ParameterSet).where(ParameterSet.valid_from <= now_utc())
                        .order_by(ParameterSet.valid_from.desc(), ParameterSet.id.desc()))
    base = Params()
    if not ps or not ps.values:
        return base
    known = {f.name: f.type for f in fields(Params)}
    values = {}
    for k, v in ps.values.items():
        if k in known:
            try:
                values[k] = int(v) if k in ("samples", "seed") else float(v)
            except (TypeError, ValueError):
                continue
    return Params(**{**vars(base), **values})


def train_spec(train: Train) -> TrainSpec:
    systems: tuple = ()
    if train.locomotive and train.locomotive.systems:
        m = {"25": "25kV", "3": "3kV", "15": "15kV"}
        out = []
        for s in train.locomotive.systems:
            key = str(s).lower().replace(" ", "")
            for prefix, val in m.items():
                if key.startswith(prefix):
                    out.append(val)
                    break
        systems = tuple(sorted(set(out)))
    traction = (train.locomotive.traction if train.locomotive else train.traction) or "E"
    return TrainSpec(traction=traction, gauge=train.gauge or "1435", vmax_kmh=train.vmax_kmh or 100.0,
                     systems=systems, length_m=train.length_m)


# ------------------------------------------------------------------ výluky → časové okná
@dataclass
class Window:
    closure_id: int
    kind: str
    start: datetime
    end: datetime
    electric_only: bool
    speed_kmh: Optional[float]
    from_station: str
    to_station: str
    status: str


def closure_windows(session: Session, start: datetime, end: datetime) -> list:
    """Konkrétne časové okná výluk v intervale (rozbalí denné opakovanie v miestnom čase)."""
    out = []
    rows = session.scalars(select(Closure).where(Closure.status.in_(("navrh", "overene")),
                                                 Closure.end_utc >= start, Closure.start_utc <= end))
    for c in rows:
        cs, ce = ensure_utc(c.start_utc), ensure_utc(c.end_utc)
        electric = c.electric_only or c.kind == "trakcne_vedenie"
        if c.daily_from and c.daily_to:
            day = local(max(cs, start)).date() - timedelta(days=1)
            last = local(min(ce, end)).date() + timedelta(days=1)
            while day <= last:
                h1, m1 = (int(x) for x in c.daily_from.split(":"))
                h2, m2 = (int(x) for x in c.daily_to.split(":"))
                ws = datetime(day.year, day.month, day.day, h1, m1, tzinfo=tz())
                we = datetime(day.year, day.month, day.day, h2, m2, tzinfo=tz())
                if we <= ws:
                    we += timedelta(days=1)
                ws, we = ensure_utc(ws), ensure_utc(we)
                ws, we = max(ws, cs), min(we, ce)
                if we > ws and we >= start and ws <= end:
                    out.append(Window(c.id, c.kind, ws, we, electric, c.speed_kmh, c.from_station,
                                      c.to_station, c.status))
                day += timedelta(days=1)
        else:
            out.append(Window(c.id, c.kind, cs, ce, electric, c.speed_kmh, c.from_station, c.to_station,
                              c.status))
    return out


# ------------------------------------------------------------------ úseky pre rozhodovací modul
@dataclass
class Waypoint:
    name: str
    type: str
    km: float
    minutes: float


def _waypoints(groute: GRoute) -> list:
    pts = []
    for st, km, mins in groute.stations:
        if st.type in ("station", "yard", "border", "junction", "spur_junction") or not pts:
            pts.append(Waypoint(st.name, st.type, km, mins))
    last = groute.stations[-1][0] if groute.stations else None
    if last and pts[-1].name != last.name:
        st, km, mins = groute.stations[-1]
        pts.append(Waypoint(st.name, st.type, km, mins))
    return pts


def _segment_edges(net: Network, groute: GRoute, waypoints: list) -> list:
    """Pre každý úsek medzi waypointmi množinu hrán trasy (podľa polohy stredu hrany)."""
    cum = 0.0
    mids = []
    for ei in groute.edges:
        L = net.edges[ei].len_m
        mids.append((cum + L / 2) / 1000.0)
        cum += L
    out = []
    for a, b in zip(waypoints, waypoints[1:]):
        out.append({ei for ei, m in zip(groute.edges, mids) if a.km <= m <= b.km})
    return out


def build_segments(net: Network, groute: GRoute, windows: list, t0: datetime, spec: TrainSpec,
                   session: Session, start_km: float = 0.0, run_scale: float = 1.0) -> tuple:
    """Rozdelí trasu na úseky medzi dopravňami a priradí im výluky (s typom a časom)."""
    cfg = settings()
    wps = [w for w in _waypoints(groute) if w.km >= start_km - 1e-6]
    all_wps = _waypoints(groute)
    if start_km > 0:
        # aktuálna poloha idúceho vlaku ako prvý bod
        mins_at = _interp_minutes(all_wps, start_km)
        wps = [Waypoint("aktuálna poloha", "position", start_km, mins_at)] + \
            [w for w in all_wps if w.km > start_km + 0.05]
    if len(wps) < 2:
        wps = all_wps[:1] + all_wps[-1:] if len(all_wps) >= 2 else all_wps
    seg_edges = _segment_edges(net, groute, wps)
    info = {li.station: li for li in session.scalars(select(LocationInfo))}
    segments, affecting = [], []
    for i, (a, b) in enumerate(zip(wps, wps[1:])):
        km = max(0.0, b.km - a.km)
        run = max(0.5, (b.minutes - a.minutes) * (run_scale if run_scale != 1.0 else 1 + RUN_MARGIN))
        p50 = max(0.3, cfg.default_delay_p50_min_per_100km * km / 100.0)
        p90 = max(p50 * 1.5 + 0.5, cfg.default_delay_p90_min_per_100km * km / 100.0)
        closures = []
        edges = seg_edges[i] if i < len(seg_edges) else set()
        for w in windows:
            sec = net.closure_edges(w.from_station, w.to_station)
            if not sec or not (sec & edges):
                continue
            affecting.append((w, a.name, b.name))
            if w.kind in BLOCKING:
                closures.append(DClosure(start=_min(t0, w.start), end=_min(t0, w.end),
                                         electric_only=w.electric_only, label=f"výluka #{w.closure_id}"))
            elif w.kind == "kolaj":
                p50 += 3.0
                p90 += 15.0
            elif w.kind == "pomala_jazda":
                v_seg = km / (run / 60.0) if run > 0 else 60.0
                if w.speed_kmh and w.speed_kmh < v_seg:
                    sec_km = min(km, sum(net.edges[e].len_m for e in (sec & edges)) / 1000.0)
                    run += sec_km * (60.0 / w.speed_kmh - 60.0 / v_seg)
                else:
                    run += 5.0
        li = info.get(a.name)
        can_wait = (a.type in WAIT_TYPES or a.type == "position") if li is None else li.can_wait
        segments.append(Segment(f"{a.name} – {b.name}", round(run, 1), round(p50, 1), round(p90, 1),
                                tuple(closures), can_wait))
    return merge_segments(segments), wps, affecting


def _combine(a: Segment, b: Segment, closures: tuple, can_wait: bool) -> Segment:
    p50 = a.delay_p50 + b.delay_p50
    spread = math.sqrt((a.delay_p90 - a.delay_p50) ** 2 + (b.delay_p90 - b.delay_p50) ** 2)
    name = a.name.split(" – ")[0] + " – " + b.name.split(" – ")[-1]
    return Segment(name, round(a.run_min + b.run_min, 1), round(p50, 1), round(p50 + spread, 1), closures,
                   can_wait)


def merge_segments(segments: list) -> list:
    """Zlúči susedné úseky s rovnakými výlukami (menej úsekov = rýchlejšia simulácia).

    Úsek s výlukou, pred ktorým sa nedá čakať, sa predĺži dozadu až po dopravňu, kde čakať možno
    (bod, odkiaľ niet návratu – sekcia 10.2 PROMPT.md).
    """
    out: list = []
    for seg in segments:
        if out and out[-1].closures == seg.closures:
            out[-1] = _combine(out[-1], seg, seg.closures, out[-1].can_wait_before)
            continue
        out.append(seg)
    changed = True
    while changed:
        changed = False
        for i in range(1, len(out)):
            seg = out[i]
            if seg.closures and not seg.can_wait_before:
                prev = out[i - 1]
                out[i - 1:i + 1] = [_combine(prev, seg, tuple(prev.closures) + tuple(
                    c for c in seg.closures if c not in prev.closures), prev.can_wait_before)]
                changed = True
                break
    return out


def _interp_minutes(wps: list, km: float) -> float:
    if not wps:
        return 0.0
    if km <= wps[0].km:
        return wps[0].minutes
    for a, b in zip(wps, wps[1:]):
        if a.km <= km <= b.km:
            f = 0.0 if b.km == a.km else (km - a.km) / (b.km - a.km)
            return a.minutes + f * (b.minutes - a.minutes)
    return wps[-1].minutes if wps else 0.0


def _min(t0: datetime, dt: datetime) -> float:
    return (ensure_utc(dt) - ensure_utc(t0)).total_seconds() / 60.0


def _dt(t0: datetime, minutes: float) -> datetime:
    return plus_minutes(t0, minutes)


# ------------------------------------------------------------------ pomocné dotazy
def latest_position(session: Session, train_id: int) -> Optional[PositionReport]:
    return session.scalar(select(PositionReport).where(PositionReport.train_id == train_id)
                          .order_by(PositionReport.ts_utc.desc(), PositionReport.id.desc()))


def active_shift(session: Session, train_id: int) -> Optional[DriverShift]:
    return session.scalar(select(DriverShift).where(DriverShift.train_id == train_id,
                                                    DriverShift.status.in_(("planovana", "prebieha")))
                          .order_by(DriverShift.start_utc.desc()))


def entry_handover(session: Session, train_id: int) -> Optional[Handover]:
    return session.scalar(select(Handover).where(Handover.train_id == train_id, Handover.direction == "vstup")
                          .order_by(Handover.id.desc()))


def allocated_paths(session: Session, train: Train) -> list:
    q = select(Path).where(Path.status.in_(("pridelena", "zmenena")))
    rows = session.scalars(q.where((Path.train_id == train.id) |
                                   ((Path.train_number == train.number) & (Path.train_id.is_(None)))))
    return [p for p in rows if p.departure_utc]


def path_run_scale(paths: list, groute: GRoute) -> tuple:
    """Pomer jazdných časov z pridelenej trasy ŽSR k odhadu z grafu (presnejšie časy z trasy).

    Vracia (pomer, popis). Ak trasa nemá časy aspoň v dvoch dopravniach na trase vlaku, (1.0, "").
    """
    from ..siet.graf import norm

    pos = {norm(st.name): mins for st, _km, mins in groute.stations}
    for p in sorted(paths, key=lambda x: x.id, reverse=True):
        pts = []
        for pt in p.points or []:
            name = norm(str(pt.get("station", "")))
            t = pt.get("dep") or pt.get("arr")
            if name in pos and t:
                try:
                    pts.append((pos[name], datetime.fromisoformat(t)))
                except ValueError:
                    continue
        if len(pts) >= 2:
            (g1, t1), (g2, t2) = pts[0], pts[-1]
            graph_min = g2 - g1
            path_min = (ensure_utc(t2) - ensure_utc(t1)).total_seconds() / 60.0
            if graph_min > 5 and 0.5 <= path_min / graph_min <= 2.5:
                return path_min / graph_min, f"Jazdné časy podľa pridelenej trasy ŽSR (pomer {path_min / graph_min:.2f}×)."
    return 1.0, ""


def mailbox_age_minutes(session: Session, now: datetime) -> Optional[float]:
    kv = session.scalar(select(KeyValue).where(KeyValue.key == "mail_last_ok"))
    if not kv or not kv.value:
        return None
    try:
        last = datetime.fromisoformat(kv.value.get("ts"))
    except (TypeError, ValueError, AttributeError):
        return None
    return _min(last, now)


# ------------------------------------------------------------------ vyhodnotenie vlaku
def evaluate_train(session: Session, train_id: int, now: Optional[datetime] = None,
                   trigger: str = "") -> Optional[Recommendation]:
    train = session.get(Train, train_id)
    if train is None or train.status in DONE_STATES:
        return None
    cfg = settings()
    now = ensure_utc(now) if now else now_utc()
    params = current_params(session)
    spec = train_spec(train)
    net = network()
    reasons: list = []
    data_age: dict = {}

    primary = net.route(train.origin, train.destination, spec, via=train.via or [])
    if primary is None:
        return _store(session, train, now, trigger, decision=ESKALOVAT, color=CERVENA, priority="KRITICKÉ",
                      summary=f"Trasa {train.origin} → {train.destination} sa nenašla (neznáma dopravňa, "
                              f"nekompatibilný rozchod alebo trakcia).",
                      reasons=["Skontrolujte názvy dopravní, trakciu rušňa a rozchod vlaku."])

    running = train.status == "v_jazde"
    start_km = 0.0
    pos = latest_position(session, train.id)
    if pos is not None:
        data_age["position_min"] = round(_min(pos.ts_utc, now), 1)
    if running:
        if pos is not None and data_age["position_min"] <= cfg.position_stale_minutes:
            prog = net.progress_on_route(primary, pos.lat, pos.lon)
            if prog:
                start_km = prog["done_m"] / 1000.0
            else:
                reasons.append("Poloha vlaku nie je na plánovanej trase – overte trasu.")
        else:
            reasons.append("Chýbajú čerstvé dáta o polohe vlaku – výpočet vychádza z plánu.")
            if train.planned_departure_utc:
                elapsed = _min(train.planned_departure_utc, now)
                start_km = _km_at_minutes(primary, max(0.0, elapsed))

    # čas pripravenosti
    ho = entry_handover(session, train.id)
    bc = ho.border if ho else train.entry_border
    if running:
        ready = now
    elif ho and ho.status in ("ocakavany", "prisiel") and (ho.arrived_utc or ho.avizo_eta_utc):
        arrival = ensure_utc(ho.arrived_utc or ho.avizo_eta_utc)
        dwell = bc.dwell_p50_min if bc else 60.0
        ready = max(now, plus_minutes(arrival, dwell))
        reasons.append(f"Preberanie na {bc.handover_station if bc else 'hranici'}: príchod "
                       f"{fmt(arrival)} + preberanie {dwell:.0f} min (P50).")
    else:
        ready = max(now, ensure_utc(train.ready_utc) if train.ready_utc else now)

    order = session.get(Order, train.order_id) if train.order_id else None
    deadline = train.deadline_utc or (order.deadline_utc if order else None)
    deadline_min = _min(now, deadline) if deadline else _min(now, ready) + NO_DEADLINE_MIN
    if not deadline:
        reasons.append("Vlak nemá zadaný termín zákazníka – posudzujú sa len výluky a pracovný čas.")

    windows = closure_windows(session, now - timedelta(hours=2), now + timedelta(hours=cfg.horizon_hours + 30))
    paths = allocated_paths(session, train)
    run_scale, scale_note = path_run_scale(paths, primary)
    if scale_note:
        reasons.append(scale_note)
    segs, wps, affecting = build_segments(net, primary, windows, now, spec, session, start_km, run_scale)
    routes = [DRoute(f"{train.origin} – {train.destination}", tuple(segs), electric=spec.traction == "E")]

    # obchádzky okolo blokujúcich výluk na hlavnej trase
    blocked: set = set()
    for w, _a, _b in affecting:
        if w.kind in BLOCKING and not (w.electric_only and spec.traction != "E"):
            blocked |= net.closure_edges(w.from_station, w.to_station)
    alt_groutes = []
    if blocked and not running:
        ra, rb = net.resolve(train.origin, spec), net.resolve(train.destination, spec)
        if ra and rb:
            alt = net.shortest(ra[1], rb[1], spec, banned=blocked)
            if alt and alt.minutes <= primary.minutes * 2.5:
                alt_groutes.append(alt)
                via = [s.name for s, _, _ in alt.stations if s.type == "station"]
                mid = via[len(via) // 2] if via else "?"
                asegs, _, _ = build_segments(net, alt, windows, now, spec, session)
                routes.append(DRoute(f"obchádzka cez {mid}", tuple(asegs), electric=spec.traction == "E",
                                     is_primary=False))

    # pridelené trasy
    if running:
        allocated = []
    elif paths:
        allocated = [_min(now, p.departure_utc) for p in paths]
    else:
        allocated = [NO_PATH_SENTINEL]
        reasons.append("Vlak zatiaľ nemá pridelenú trasu – treba ju objednať.")

    # rušňovodič
    shift = active_shift(session, train.id)
    crew_start = None
    if shift and ensure_utc(shift.start_utc) <= max(now, ready):
        crew_start = _min(now, shift.start_utc)

    ready_min = _min(now, ready)
    rec = two_stage_recommend(routes, ready_min, deadline_min, params, allocated, crew_start)

    decision = rec.decision
    chosen: Optional[Evaluation] = rec.chosen
    color = chosen.status if chosen else CERVENA
    if decision == ESKALOVAT:
        color = CERVENA
    if running and decision == VYPRAVIT:
        decision = POKRACOVAT

    # bez čerstvých dát nikdy zelená
    stale = running and (pos is None or data_age.get("position_min", 1e9) > cfg.position_stale_minutes)
    mb_age = mailbox_age_minutes(session, now)
    if mb_age is not None:
        data_age["mailbox_min"] = round(mb_age, 1)
    if mb_age is not None and mb_age > 30:
        stale = True
        reasons.append(f"E-mailová schránka agenta sa nekontrolovala {mb_age:.0f} min – výluky nemusia byť "
                       f"aktuálne.")
    if stale and color == ZELENA:
        color = ORANZOVA
    unconfirmed = [w for w, _, _ in affecting if w.status == "navrh"]
    if unconfirmed:
        reasons.append(f"{len(unconfirmed)} výluk(a) na trase ešte nie je potvrdená dispečerom "
                       f"(počíta sa s ňou).")
    seen_w: set = set()
    for w, _a, _b in affecting:
        key = (w.closure_id, w.start)
        if key in seen_w:
            continue
        seen_w.add(key)
        state = "" if w.status == "overene" else " – NEPOTVRDENÁ"
        reasons.append(f"{_kind_label(w.kind)} {w.from_station} – {w.to_station} "
                       f"{fmt(w.start)} – {fmt(w.end)} na trase vlaku{state}.")
    if chosen:
        reasons.extend(chosen.reasons)

    # rušňovodič už v službe: posúdiť jeho pracovný čas pri pokračovaní hneď (aj keď odporúčanie
    # je odstaviť – dispečer musí vedieť, dokedy ho treba vystriedať)
    current_ref = None
    if shift is not None and ensure_utc(shift.start_utc) <= now:
        current_ref = next((e for e in rec.candidates if e.route == routes[0].name
                            and abs(e.depart - ready_min) < 1e-6 and e.kind == VYPRAVIT), None)
    crew = _crew_info(shift, current_ref or chosen, now, params, current=current_ref is not None)
    # plánovaný (ešte nezačatý) nástup rušňovodiča zbytočne skoro → navrhnúť posun (krok C.11 príbehu vlaku)
    if shift is not None and chosen is not None and chosen.kind != ODSTAVIT and \
            ensure_utc(shift.start_utc) > now:
        ideal = _dt(now, chosen.depart - params.crew_prep_min)
        idle = (ideal - ensure_utc(shift.start_utc)).total_seconds() / 60.0
        if idle >= 30:
            reasons.append(f"Posunúť nástup rušňovodiča{(' ' + shift.driver.name) if shift.driver else ''} "
                           f"z {fmt(shift.start_utc)} na {fmt(ideal)} – inak by {idle / 60:.1f} h čakal a míňal "
                           f"pracovný čas (limit {params.crew_max_duty_min / 60:.0f} h).")
            crew["suggested_start"] = ideal.isoformat()
    if crew.get("status") == CERVENA:
        reasons.append(f"Rušňovodič by prekročil {params.crew_max_duty_min / 60:.0f} h – naplánujte výmenu "
                       f"najneskôr o {fmt(datetime.fromisoformat(crew['relieve_by']))}.")

    # semafor 10.3: ZELENÁ len ak má rušňovodič rezervu do limitu; inak treba potvrdenie dispečera
    if crew.get("status") in (ORANZOVA, CERVENA) and color == ZELENA:
        color = ORANZOVA

    path_proposal = _path_proposal(net, primary if not chosen or chosen.route == routes[0].name
                                   else alt_groutes[0], chosen, now, start_km if running else 0.0, rec)
    if path_proposal:
        path_proposal["drafts"] = _drafts(train, decision, chosen, path_proposal, now, deadline)
    windows_iso = {name: [[_dt(now, a).isoformat(), _dt(now, b).isoformat()] for a, b in wins]
                   for name, wins in rec.green_windows.items()}
    variants = _variants(rec, now)
    priority = _priority(session, train, decision, color, chosen, crew, running, now)
    summary = _summary(train, decision, chosen, now, crew, rec)
    return _store(session, train, now, trigger, decision=decision, color=color, priority=priority,
                  summary=summary, chosen=_ev_dict(chosen, now) if chosen else {}, variants=variants,
                  windows=windows_iso, path_proposal=path_proposal, crew=crew,
                  reasons=_dedupe(reasons), data_age=data_age)


def two_stage_recommend(routes: list, ready_min: float, deadline_min: float, params: Params,
                        allocated: list, crew_start: Optional[float]) -> DRecommendation:
    """1. hrubý prechod všetkých odchodov s menej vzorkami, 2. spresnenie najlepších kandidátov.

    Výsledok je rovnaký ako pri recommend() s plným počtom vzoriek pre vybrané varianty, ale
    výpočet je niekoľkonásobne rýchlejší (sekcia 13: prepočet do 5 s).
    """
    cfg = settings()
    fast = replace(params, samples=min(params.samples, 600))
    rec = recommend(routes, now=0.0, ready=ready_min, deadline=deadline_min, p=fast,
                    horizon_min=cfg.horizon_hours * 60, step_min=cfg.step_minutes,
                    allocated_departures=allocated, crew_start=crew_start)
    if not rec.candidates:
        return rec
    rank = {ZELENA: 0, ORANZOVA: 1, CERVENA: 2}
    top = sorted(rec.candidates, key=lambda e: (rank[e.status], e.expected_cost))[:10]
    by_name = {r.name: r for r in routes}
    refined = []
    for ev in top:
        route = by_name[ev.route]
        stabled = ev.kind == ODSTAVIT
        normals = _normals(params.samples, len(route.segments), params.seed)
        new = evaluate(route, ev.depart, ready_min, deadline_min, params, normals, stabled,
                       ev.needs_new_path, None if stabled else crew_start)
        if not route.is_primary:
            new.kind = PRESMEROVAT
        refined.append(new)
    decision, chosen, explanation = select_best(refined, params)
    return DRecommendation(decision, chosen, explanation, rec.green_windows, rec.candidates)


def evaluate_all(session: Session, now: Optional[datetime] = None, trigger: str = "") -> list:
    out = []
    ids = [t.id for t in session.scalars(select(Train).where(Train.status.notin_(DONE_STATES)))]
    for tid in ids:
        try:
            rec = evaluate_train(session, tid, now, trigger)
            if rec is not None:
                out.append(rec)
        except Exception as exc:  # jeden chybný vlak nesmie zastaviť ostatné
            audit(session, "agent", "evaluate_error", "train", tid, error=repr(exc))
    return out


def plan_order(session: Session, order_id: int, now: Optional[datetime] = None) -> dict:
    """Varianty prepravy pre objednávku (bez uloženia): trasy, časy, rezervy, odporúčanie."""
    order = session.get(Order, order_id)
    if order is None:
        return {"error": "Objednávka neexistuje."}
    cfg = settings()
    now = ensure_utc(now) if now else now_utc()
    spec = TrainSpec(traction="E", gauge="1435", vmax_kmh=cfg.freight_vmax_kmh, length_m=order.length_m)
    net = network()
    groutes = net.alternatives(order.origin, order.destination, spec, k=3)
    if not groutes:
        spec = TrainSpec(traction="M", gauge="1435", vmax_kmh=cfg.freight_vmax_kmh)
        groutes = net.alternatives(order.origin, order.destination, spec, k=3)
    if not groutes:
        return {"error": f"Trasa {order.origin} → {order.destination} sa nenašla."}
    params = current_params(session)
    windows = closure_windows(session, now, now + timedelta(hours=cfg.horizon_hours + 48))
    droutes = []
    for i, g in enumerate(groutes):
        segs, _, _ = build_segments(net, g, windows, now, spec, session)
        via = [s.name for s, _, _ in g.stations if s.type == "station"]
        name = "hlavná trasa" if i == 0 else f"alternatíva cez {via[len(via) // 2] if via else '?'}"
        droutes.append(DRoute(name, tuple(segs), electric=spec.traction == "E", is_primary=i == 0))
    ready = max(now, ensure_utc(order.earliest_departure_utc) if order.earliest_departure_utc else now)
    deadline_min = _min(now, order.deadline_utc) if order.deadline_utc else _min(now, ready) + NO_DEADLINE_MIN
    rec = two_stage_recommend(droutes, _min(now, ready), deadline_min, params, [NO_PATH_SENTINEL], None)
    explanation = []
    ch = rec.chosen
    if ch is not None and rec.decision != ESKALOVAT:
        explanation.append(f"{rec.decision} po trase „{ch.route}“ s odchodom {fmt(_dt(now, ch.depart))}: "
                           f"P(včas) {ch.p_on_time:.0%}, príchod {fmt(_dt(now, ch.arrival_p50))} "
                           f"(90 % do {fmt(_dt(now, ch.arrival_p90))}), očakávaný náklad {ch.expected_cost:,.0f} €.")
        explanation.append("Trasu treba objednať v IS KIS (ad hoc trasa P5 najneskôr 6 h pred odchodom).")
        if ch.status != ZELENA:
            explanation.append("ORANŽOVÁ – vyžaduje potvrdenie dispečera: " + "; ".join(ch.reasons))
    else:
        explanation.append("Žiadna bezpečná varianta v horizonte – dohodnúť so zákazníkom iný termín "
                           "alebo so ŽSR iný čas či trasu.")
    out = {"order_id": order.id, "decision": rec.decision, "explanation": explanation,
           "variants": _variants(rec, now), "routes": []}
    for g, d in zip(groutes, droutes):
        out["routes"].append({"name": d.name, "km": round(g.length_m / 1000, 1), "minutes": round(g.minutes),
                              "stations": [s.name for s, _, _ in g.stations if s.type == "station"]})
    if rec.chosen:
        idx = [d.name for d in droutes].index(rec.chosen.route)
        out["path_proposal"] = _path_proposal(net, groutes[idx], rec.chosen, now, 0.0, rec)
    return out


# ------------------------------------------------------------------ pomocné výstupy
def _km_at_minutes(groute: GRoute, minutes: float) -> float:
    wps = _waypoints(groute)
    if not wps:
        return 0.0
    if minutes <= wps[0].minutes:
        return wps[0].km
    for a, b in zip(wps, wps[1:]):
        if a.minutes <= minutes <= b.minutes:
            f = 0.0 if b.minutes == a.minutes else (minutes - a.minutes) / (b.minutes - a.minutes)
            return a.km + f * (b.km - a.km)
    return wps[-1].km if wps else 0.0


def _kind_label(kind: str) -> str:
    return {"uplna": "Výluka", "kolaj": "Výluka koľaje", "trakcne_vedenie": "Vypnuté trakčné vedenie",
            "pomala_jazda": "Pomalá jazda", "obmedzenie": "Obmedzenie", "mimoriadnost": "Mimoriadnosť"}.get(
        kind, "Obmedzenie")


def _ev_dict(ev: Evaluation, now: datetime) -> dict:
    return {"route": ev.route, "depart": _dt(now, ev.depart).isoformat(), "kind": ev.kind,
            "status": ev.status, "p_on_time": round(ev.p_on_time, 3), "p_hold": round(ev.p_hold, 3),
            "p_crew_relief": round(ev.p_crew_relief, 3),
            "arrival_p50": _dt(now, ev.arrival_p50).isoformat(),
            "arrival_p90": _dt(now, ev.arrival_p90).isoformat(),
            "expected_cost": round(ev.expected_cost, 0), "reasons": list(ev.reasons),
            "needs_new_path": ev.needs_new_path}


def _variants(rec, now: datetime) -> list:
    """Najlepšia varianta každej trasy a každého stavu (prehľadná tabuľka pre dispečera)."""
    best: dict = {}
    for ev in rec.candidates:
        key = (ev.route, ev.status)
        if key not in best or ev.expected_cost < best[key].expected_cost:
            best[key] = ev
    if rec.chosen is not None:
        best[("__chosen__", "")] = rec.chosen
    uniq, seen = [], set()
    for ev in sorted(best.values(), key=lambda e: ({ZELENA: 0, ORANZOVA: 1, CERVENA: 2}[e.status],
                                                    e.expected_cost)):
        k = (ev.route, ev.depart, ev.kind)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(_ev_dict(ev, now))
    return uniq[:6]


def _path_proposal(net: Network, groute: GRoute, chosen: Optional[Evaluation], now: datetime,
                   start_km: float, rec) -> dict:
    if chosen is None:
        return {}
    depart = _dt(now, chosen.depart)
    total = max(1e-6, chosen.arrival_p50 - chosen.depart)
    base_total = groute.stations[-1][2] - _interp_minutes(_waypoints(groute), start_km) if groute.stations else 1
    factor = total / max(1e-6, base_total)
    stations = []
    for st, km, mins in groute.stations:
        if km < start_km - 1e-6 or st.type not in ("station", "yard", "border", "junction"):
            continue
        rel = (mins - _interp_minutes(_waypoints(groute), start_km)) * factor
        t = plus_minutes(depart, rel)
        stations.append({"name": st.name, "type": st.type, "km": round(km - start_km, 1),
                         "uic": st.uic_ref, "ref": st.railway_ref, "arr": t.isoformat(), "dep": t.isoformat()})
    if stations:
        stations[0]["arr"] = None
        stations[-1]["dep"] = None
    window = None
    for a, b in rec.green_windows.get(chosen.route, []):
        if a - 1e-6 <= chosen.depart <= b + 1e-6:
            window = [_dt(now, a).isoformat(), _dt(now, b).isoformat()]
    return {"route": chosen.route, "km": round(groute.length_m / 1000.0 - start_km, 1),
            "stations": stations, "needs_new_path": chosen.needs_new_path, "request_window": window,
            "geometry_points": len(groute.nodes)}


def _iso_fmt(iso: Optional[str]) -> str:
    return fmt(datetime.fromisoformat(iso)) if iso else "—"


def _drafts(train: Train, decision: str, chosen: Optional[Evaluation], pp: dict, now: datetime,
            deadline: Optional[datetime]) -> dict:
    """Koncepty správ pre externých adresátov (M11). Agent ich NIKDY neodosiela – len pripraví."""
    st = pp.get("stations") or []
    if not st or chosen is None:
        return {}
    cfg = settings()
    first, last = st[0], st[-1]
    via = ", ".join(x["name"] for x in st[1:-1] if x.get("type") == "station")
    win = pp.get("request_window")
    loco = train.locomotive.type if train.locomotive and train.locomotive.type else "[doplniť]"
    trakcia = "elektrická" if (train.locomotive.traction if train.locomotive else train.traction) == "E" else "motorová"
    zsr = "\n".join([
        "KONCEPT – objednávka ad hoc trasy (IS KIS, produkt P5) – agent ju NEODOSIELA",
        f"Dopravca: {cfg.carrier_name}",
        f"Číslo vlaku: {train.number}   Druh vlaku: [doplniť, napr. Nex / Pn]   "
        f"Dátum jazdy: {local(datetime.fromisoformat(first['dep'])).strftime('%d.%m.%Y') if first.get('dep') else '[doplniť]'}",
        f"Východisková dopravňa: {first['name']}   odchod {_iso_fmt(first.get('dep'))}",
        f"Cieľová dopravňa: {last['name']}   príchod {_iso_fmt(last.get('arr'))}",
        f"Smerovanie cez: {via[:600] or '—'}",
        f"Rušeň (rad HKV): {loco}   Trakcia: {trakcia}   Max. rýchlosť: {train.vmax_kmh:.0f} km/h",
        f"Hmotnosť vlaku: {train.weight_t or '[doplniť]'} t   Dĺžka vlaku: {train.length_m or '[doplniť]'} m   "
        f"Nebezpečný tovar (RID): {'áno' if train.rid else 'nie'}",
        "Pobyty z dopravných dôvodov: [doplniť – napr. výmena rušňovodiča, skúška bŕzd]",
        f"Požadovaný odchod v okne: {_iso_fmt(win[0])} – {_iso_fmt(win[1])}" if win else
        "Požadovaný odchod: podľa návrhu vyššie",
    ])
    arr50 = _dt(now, chosen.arrival_p50)
    arr90 = _dt(now, chosen.arrival_p90)
    dep = _dt(now, chosen.depart)
    phrase = {VYPRAVIT: f"odchádza o {fmt(dep)}", POKRACOVAT: "je na ceste",
              POCKAT: f"pre obmedzenie na trati vyrazí o {fmt(dep)}",
              ODSTAVIT: f"pre obmedzenie na trati vyrazí o {fmt(dep)}",
              PRESMEROVAT: f"pôjde obchádzkovou trasou s odchodom o {fmt(dep)}"}.get(decision, "")
    lines = [f"KONCEPT – informácia pre zákazníka {train.customer or ''} – agent ju NEODOSIELA", "",
             "Dobrý deň,"]
    if decision == ESKALOVAT:
        lines.append(f"vlak č. {train.number} ({train.origin} → {train.destination}) pre obmedzenia na trati "
                     f"nestihne dohodnutý termín. Navrhujeme nový termín: [doplniť].")
    else:
        lines.append(f"vlak č. {train.number} ({train.origin} → {train.destination}) {phrase}. Predpokladaný "
                     f"príchod: {fmt(arr50)} (najneskôr približne {fmt(arr90)}).")
        if deadline and arr50 > ensure_utc(deadline):
            late_h = (arr50 - ensure_utc(deadline)).total_seconds() / 3600.0
            lines.append(f"Oproti dohodnutému termínu {fmt(deadline)} očakávame meškanie približne {late_h:.1f} h.")
    lines += ["", f"S pozdravom", f"dispečing {cfg.carrier_name}"]
    return {"zsr_path_request": zsr, "customer": "\n".join(lines)}


def _crew_info(shift: Optional[DriverShift], chosen: Optional[Evaluation], now: datetime, p: Params,
               current: bool = False) -> dict:
    if chosen is None:
        return {}
    fresh_crew = chosen.kind == ODSTAVIT and not current  # pri odstavení nastúpi nový rušňovodič
    if not fresh_crew and shift is not None and ensure_utc(shift.start_utc) <= _dt(now, chosen.depart):
        start = ensure_utc(shift.start_utc)
        driver = shift.driver.name if shift.driver else ""
    else:
        start = _dt(now, chosen.depart - p.crew_prep_min)
        driver = "nový rušňovodič (po odstavení)" if fresh_crew else \
            (shift.driver.name if shift and shift.driver else "")
    limit = plus_minutes(start, p.crew_max_duty_min)
    end50 = _dt(now, chosen.arrival_p50 + p.crew_post_min)
    end90 = _dt(now, chosen.arrival_p90 + p.crew_post_min)
    if end50 > limit:
        status = CERVENA
    elif end90 > limit - timedelta(minutes=p.crew_buffer_min):
        status = ORANZOVA
    else:
        status = ZELENA
    return {"driver": driver, "start": start.isoformat(), "limit": limit.isoformat(),
            "end_p50": end50.isoformat(), "end_p90": end90.isoformat(),
            "left_min": round((limit - now).total_seconds() / 60.0, 0), "status": status,
            "relieve_by": (limit - timedelta(minutes=p.crew_post_min)).isoformat(),
            "max_duty_h": round(p.crew_max_duty_min / 60.0, 1)}


def _priority(session: Session, train: Train, decision: str, color: str, chosen, crew: dict, running: bool,
              now: datetime) -> str:
    soon = chosen is not None and (datetime.fromisoformat(_dt(now, chosen.depart).isoformat()) - now) <= \
        timedelta(hours=2)
    if decision == ESKALOVAT or crew.get("status") == CERVENA or (color == CERVENA and (running or soon)):
        return "KRITICKÉ"
    last = session.scalar(select(Recommendation).where(Recommendation.train_id == train.id)
                          .order_by(Recommendation.id.desc()))
    changed = last is None or last.decision != decision
    if color == ORANZOVA or crew.get("status") == ORANZOVA or changed or (chosen and chosen.needs_new_path):
        return "DÔLEŽITÉ"
    return "INFO"


def _summary(train: Train, decision: str, chosen: Optional[Evaluation], now: datetime, crew: dict, rec) -> str:
    rel = f"{train.origin} → {train.destination}"
    if chosen is None or decision == ESKALOVAT:
        return (f"Vlak {train.number} ({rel}): ESKALOVAŤ – žiadna bezpečná varianta v najbližších "
                f"{settings().horizon_hours} h. Kontaktovať ŽSR (iná trasa alebo čas) a zákazníka (nový termín).")
    dep = fmt(_dt(now, chosen.depart))
    arr50, arr90 = fmt(_dt(now, chosen.arrival_p50)), fmt(_dt(now, chosen.arrival_p90))
    head = {VYPRAVIT: f"VYPRAVIŤ o {dep}", POKRACOVAT: "POKRAČOVAŤ v jazde",
            POCKAT: f"POČKAŤ, vypraviť o {dep}",
            ODSTAVIT: f"ODSTAVIŤ (koľaj určí ŽSR, najviac 18 h), pôvodnú trasu odrieknuť, nová trasa na {dep}",
            PRESMEROVAT: f"PRESMEROVAŤ ({chosen.route}), odchod {dep}"}.get(decision, f"{decision} {dep}")
    txt = (f"Vlak {train.number} ({rel}): {head}. P(včas) {chosen.p_on_time:.0%}, príchod {arr50} "
           f"(90 % do {arr90}).")
    if chosen.needs_new_path:
        txt += (" Treba objednať ad hoc trasu v IS KIS (produkt P5 najneskôr 6 h pred odchodom; neskôr len "
                "výnimočne cez dispečera ŽSR).")
    if crew.get("status") in (ORANZOVA, CERVENA):
        txt += f" Rušňovodič: vystriedať najneskôr o {fmt(datetime.fromisoformat(crew['relieve_by']))}."
    return txt


def _dedupe(items: list) -> list:
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _store(session: Session, train: Train, now: datetime, trigger: str, decision: str, color: str,
           priority: str, summary: str, chosen: Optional[dict] = None, variants: Optional[list] = None,
           windows: Optional[dict] = None, path_proposal: Optional[dict] = None, crew: Optional[dict] = None,
           reasons: Optional[list] = None, data_age: Optional[dict] = None) -> Recommendation:
    """Uloží odporúčanie len ak sa zmenilo (fingerprint); staré nové odporúčania označí za zastarané."""
    chosen = chosen or {}
    fp_src = "|".join([decision, color, chosen.get("route", ""), (crew or {}).get("status", ""),
                       str(bool(chosen.get("needs_new_path")))])
    fingerprint = hashlib.sha1(fp_src.encode("utf-8")).hexdigest()
    last = session.scalar(select(Recommendation).where(Recommendation.train_id == train.id)
                          .order_by(Recommendation.id.desc()))
    if last is not None and last.fingerprint == fingerprint and \
            last.state in ("nove", "prijate", "upravene", "zamietnute"):
        shift_min = 0.0
        if chosen.get("depart") and (last.chosen or {}).get("depart"):
            shift_min = abs((datetime.fromisoformat(chosen["depart"]) -
                             datetime.fromisoformat(last.chosen["depart"])).total_seconds()) / 60.0
        if shift_min < 30:
            # nepodstatná zmena: aktualizovať čísla, nevytvárať nové odporúčanie ani notifikáciu
            last.summary, last.chosen, last.variants, last.windows = summary, chosen, variants or [], windows or {}
            last.path_proposal, last.crew, last.reasons = path_proposal or {}, crew or {}, reasons or []
            last.data_age = data_age or {}
            return last
    for old in session.scalars(select(Recommendation).where(Recommendation.train_id == train.id,
                                                            Recommendation.state == "nove")):
        old.state = "zastarane"
    rec = Recommendation(train_id=train.id, created_at=now, decision=decision, color=color, priority=priority,
                         summary=summary, chosen=chosen, variants=variants or [], windows=windows or {},
                         path_proposal=path_proposal or {}, crew=crew or {}, reasons=reasons or [],
                         trigger=trigger, data_age=data_age or {}, fingerprint=fingerprint, state="nove")
    session.add(rec)
    session.flush()
    audit(session, "agent", "recommendation", "train", train.id, decision=decision, color=color,
          priority=priority, trigger=trigger, recommendation_id=rec.id)
    return rec
