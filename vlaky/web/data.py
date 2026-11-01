"""Zostavenie dát pre mapu, tabuľku vlakov, výluky a upozornenia (čítanie z DB, bez zápisu)."""
from __future__ import annotations

import math
import threading
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..cas import ensure_utc, fmt, local, now_utc, tz
from ..config import settings
from ..models import (AgentHealth, BorderCrossing, Closure, DriverShift, ExtractedEvent, Handover, LocationInfo,
                      Notification, PositionReport, Recommendation, Train)
from .common import (CLOSURE_KIND, CLOSURE_STATUS, DONE_TRAIN_STATES, POSITION_SOURCE, TRAIN_STATUS, crew_color,
                     current_params, iso, log, parse_iso)
from .trasy import RouteInfo, build_route_info, detour_info, estimate_position, hours_window, trail_feature

URGENCY = {"ČERVENÁ": 0, "ORANŽOVÁ": 1, None: 2, "ZELENÁ": 3}
PRIORITY_RANK = {"KRITICKÉ": 0, "DÔLEŽITÉ": 1, "INFO": 2}


# ---------------------------------------------------------------- sieť (bezpečne – nemusí byť k dispozícii)
def get_network():
    try:
        from ..siet.graf import network

        return network()
    except Exception:  # noqa: BLE001
        log.exception("Model siete nie je k dispozícii")
        return None


def train_spec(train: Train):
    from ..siet.graf import TrainSpec

    systems: tuple = ()
    loco = train.locomotive
    if loco is not None and loco.systems:
        out = set()
        for raw in loco.systems:
            key = str(raw).lower().replace(" ", "")
            for prefix, val in (("25", "25kV"), ("3", "3kV"), ("15", "15kV")):
                if key.startswith(prefix):
                    out.add(val)
                    break
        systems = tuple(sorted(out))
    traction = (loco.traction if loco is not None and loco.traction else train.traction) or "E"
    return TrainSpec(traction=traction, gauge=train.gauge or "1435", vmax_kmh=float(train.vmax_kmh or 100.0),
                     systems=systems, length_m=train.length_m)


_ROUTES: "OrderedDict[tuple, Optional[RouteInfo]]" = OrderedDict()
_ROUTES_LOCK = threading.Lock()
_ROUTES_MAX = 512


def _route_key(train: Train) -> tuple:
    spec = train_spec(train)
    return (train.origin or "", train.destination or "", tuple(train.via or ()), spec.traction, spec.gauge,
            spec.vmax_kmh, spec.systems)


def primary_route_name(train: Train) -> str:
    """Názov hlavnej trasy tak, ako ho používa plánovač (vlaky/planovanie/planovac.py)."""
    return f"{train.origin} – {train.destination}"


def chosen_route(train: Train, rec: Optional[Recommendation]) -> Optional[RouteInfo]:
    """Trasa zvolenej varianty: obchádzka z návrhu trasy (PRESMEROVAŤ), inak hlavná trasa."""
    chosen = (rec.chosen if rec is not None else None) or {}
    proposal = (rec.path_proposal if rec is not None else None) or {}
    name = chosen.get("route") or proposal.get("route") or ""
    if name and name != primary_route_name(train) and len(proposal.get("stations") or []) >= 2:
        net = get_network()
        if net is not None:
            info = detour_info(net, train_spec(train), proposal, name)
            if info is not None:
                return info
    return route_info(train)


def route_info(train: Train, compute: bool = True) -> Optional[RouteInfo]:
    """Plánovaná trasa vlaku po sieti (cache podľa relácie a parametrov vlaku)."""
    key = _route_key(train)
    with _ROUTES_LOCK:
        if key in _ROUTES:
            _ROUTES.move_to_end(key)
            return _ROUTES[key]
    if not compute:
        return None
    net = get_network()
    if net is None:
        return None
    info: Optional[RouteInfo] = None
    try:
        r = net.route(train.origin, train.destination, train_spec(train), via=[v for v in (train.via or []) if v])
        if r is not None:
            info = build_route_info(net, r, primary_route_name(train))
    except Exception:  # noqa: BLE001
        log.exception("Trasu vlaku %s sa nepodarilo vypočítať", train.number)
        info = None
    with _ROUTES_LOCK:
        _ROUTES[key] = info
        while len(_ROUTES) > _ROUTES_MAX:
            _ROUTES.popitem(last=False)
    return info


# ---------------------------------------------------------------- výluky
def closure_spans(c: Closure, start: datetime, end: datetime) -> list:
    """Konkrétne časové intervaly výluky v [start, end] (rozbalí denné opakovanie v miestnom čase)."""
    cs, ce = ensure_utc(c.start_utc), ensure_utc(c.end_utc)
    if ce < start or cs > end:
        return []
    if not (c.daily_from and c.daily_to):
        return [(cs, ce)]
    out = []
    try:
        h1, m1 = (int(x) for x in c.daily_from.split(":"))
        h2, m2 = (int(x) for x in c.daily_to.split(":"))
    except ValueError:
        return [(cs, ce)]
    day = local(max(cs, start)).date() - timedelta(days=1)
    last = local(min(ce, end)).date() + timedelta(days=1)
    while day <= last:
        ws = datetime(day.year, day.month, day.day, h1, m1, tzinfo=tz())
        we = datetime(day.year, day.month, day.day, h2, m2, tzinfo=tz())
        if we <= ws:
            we += timedelta(days=1)
        ws, we = max(ensure_utc(ws), cs), min(ensure_utc(we), ce)
        if we > ws and we >= start and ws <= end:
            out.append((ws, we))
        day += timedelta(days=1)
    return out


def closure_edges(c: Closure) -> set:
    net = get_network()
    if net is None:
        return set()
    try:
        return set(net.closure_edges(c.from_station, c.to_station))
    except Exception:  # noqa: BLE001
        log.exception("Úsek výluky %s sa nepodarilo nájsť", c.id)
        return set()


def closure_geometry(c: Closure) -> Optional[dict]:
    """MultiLineString z geometrie hrán úseku výluky ([lat, lon] → [lon, lat])."""
    net = get_network()
    edges = closure_edges(c)
    if net is None or not edges:
        return None
    lines = [[[round(p[1], 6), round(p[0], 6)] for p in net.edges[ei].geom] for ei in sorted(edges)]
    return {"type": "MultiLineString", "coordinates": lines}


def closures_between(s: Session, od: datetime, do: datetime, include_cancelled: bool = False) -> list:
    q = select(Closure).where(Closure.end_utc >= od - timedelta(days=1), Closure.start_utc <= do)
    if not include_cancelled:
        q = q.where(Closure.status != "zrusene")
    out = []
    for c in s.scalars(q.order_by(Closure.start_utc)):
        spans = closure_spans(c, od, do)
        if spans:
            out.append((c, spans))
    return out


def closure_feature(c: Closure, spans: list, now: datetime) -> dict:
    active_now = any(a <= now <= b for a, b in spans)
    nxt = next((a for a, b in spans if b >= now), None)
    return {
        "type": "Feature", "id": c.id,
        "geometry": closure_geometry(c),
        "properties": {
            "id": c.id, "druh": c.kind, "druh_text": CLOSURE_KIND.get(c.kind, c.kind),
            "od_stanice": c.from_station, "do_stanice": c.to_station, "trat": c.line_ref, "kolaj": c.track,
            "zaciatok": iso(c.start_utc), "koniec": iso(c.end_utc), "denne_od": c.daily_from,
            "denne_do": c.daily_to, "stav": c.status, "stav_text": CLOSURE_STATUS.get(c.status, c.status),
            "len_elektricka": bool(c.electric_only or c.kind == "trakcne_vedenie"), "rychlost": c.speed_kmh,
            "zdroj": c.source, "cislo": c.external_ref, "poznamka": c.note,
            "aktivna": active_now, "najblizsie_od": iso(nxt), "pomala": c.kind == "pomala_jazda",
            "nepotvrdena": c.status == "navrh", "udalost_id": c.source_event_id,
            "intervaly": [[iso(a), iso(b)] for a, b in spans[:20]],
        },
    }


def closures_geojson(s: Session, od: datetime, do: datetime, now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    return {"type": "FeatureCollection",
            "features": [closure_feature(c, spans, now) for c, spans in closures_between(s, od, do)]}


# ---------------------------------------------------------------- vlaky
def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from ..siet.graf import haversine

    return haversine(lat1, lon1, lat2, lon2)


def latest_positions(s: Session, train_ids: list) -> dict:
    out: dict = {}
    for tid in train_ids:
        rows = list(s.scalars(select(PositionReport).where(PositionReport.train_id == tid)
                              .order_by(PositionReport.ts_utc.desc(), PositionReport.id.desc()).limit(2)))
        if rows:
            out[tid] = rows
    return out


def latest_recommendations(s: Session, train_ids: list) -> dict:
    if not train_ids:
        return {}
    sub = (select(func.max(Recommendation.id)).where(Recommendation.train_id.in_(train_ids))
           .group_by(Recommendation.train_id))
    return {r.train_id: r for r in s.scalars(select(Recommendation).where(Recommendation.id.in_(sub)))}


def current_shifts(s: Session, train_ids: list) -> dict:
    if not train_ids:
        return {}
    out: dict = {}
    rows = s.scalars(select(DriverShift).options(selectinload(DriverShift.driver))
                     .where(DriverShift.train_id.in_(train_ids), DriverShift.status != "ukoncena")
                     .order_by(DriverShift.start_utc.desc(), DriverShift.id.desc()))
    for sh in rows:
        out.setdefault(sh.train_id, sh)
    return out


def entry_handovers(s: Session, train_ids: list) -> dict:
    if not train_ids:
        return {}
    out: dict = {}
    rows = s.scalars(select(Handover).options(selectinload(Handover.border))
                     .where(Handover.train_id.in_(train_ids), Handover.direction == "vstup")
                     .order_by(Handover.id.desc()))
    for h in rows:
        out.setdefault(h.train_id, h)
    return out


def _position_dict(rows: list, now: datetime, stale_min: float) -> Optional[dict]:
    if not rows:
        return None
    p = rows[0]
    lat = p.snapped_lat if p.snapped_lat is not None else p.lat
    lon = p.snapped_lon if p.snapped_lon is not None else p.lon
    age = (now - ensure_utc(p.ts_utc)).total_seconds() / 60.0
    bearing, moving = None, None
    if p.speed_kmh is not None:
        moving = p.speed_kmh > 2.0
    if len(rows) > 1:
        q = rows[1]
        qlat = q.snapped_lat if q.snapped_lat is not None else q.lat
        qlon = q.snapped_lon if q.snapped_lon is not None else q.lon
        d = _dist_m(qlat, qlon, lat, lon)
        dt_min = (ensure_utc(p.ts_utc) - ensure_utc(q.ts_utc)).total_seconds() / 60.0
        if d >= 15 and 0 < dt_min <= 30:
            bearing = round(_bearing(qlat, qlon, lat, lon), 1)
            if moving is None:
                moving = d / max(dt_min, 0.01) > 30  # > ~2 km/h
        elif moving is None and 0 < dt_min <= 30:
            moving = False
    return {"lat": lat, "lon": lon, "raw_lat": p.lat, "raw_lon": p.lon, "snapped": p.snapped_lat is not None,
            "ts": iso(p.ts_utc), "source": p.source, "source_text": POSITION_SOURCE.get(p.source, p.source),
            "age_min": round(age, 1), "speed": p.speed_kmh, "accuracy_m": p.accuracy_m, "bearing": bearing,
            "moving": moving, "nearest_station": p.nearest_station, "off_track_m": p.off_track_m,
            "stale": age > stale_min}


def _rec_dict(r: Optional[Recommendation]) -> Optional[dict]:
    if r is None:
        return None
    return {"id": r.id, "decision": r.decision, "color": r.color, "priority": r.priority, "summary": r.summary,
            "crew": r.crew or {}, "chosen": r.chosen or {}, "state": r.state, "created_at": iso(r.created_at),
            "reasons": list(r.reasons or [])[:6], "data_age": r.data_age or {}}


def _driver_dict(sh: Optional[DriverShift], rec: Optional[Recommendation], params: dict,
                 now: datetime) -> Optional[dict]:
    crew = (rec.crew if rec is not None else None) or {}
    if sh is not None and sh.status == "planovana" and ensure_utc(sh.start_utc) > now and crew.get("start") and \
            not crew.get("unknown_start") and (parse_iso(crew["start"]) or ensure_utc(sh.start_utc)) <= now:
        sh = None  # naplánované vystriedanie: kým nenastúpi, platí pracovný čas rušňovodiča v službe (odporúčanie)
    if sh is None and not crew:
        return None
    out: dict = {"name": "", "shift_id": None, "start": None, "limit": None, "left_min": None,
                 "relieve_by": None, "color": "seda", "status": None, "end_p90": None, "shift_status": None}
    if sh is not None:
        start = ensure_utc(sh.start_utc)
        limit = start + timedelta(minutes=params["crew_max_duty_min"])
        left = (limit - now).total_seconds() / 60.0
        out.update(name=sh.driver.name if sh.driver else "", shift_id=sh.id, start=iso(start), limit=iso(limit),
                   left_min=round(left), relieve_by=iso(limit - timedelta(minutes=params["crew_post_min"])),
                   color=crew_color(left, params["crew_buffer_min"]), shift_status=sh.status)
    unknown = bool(crew.get("unknown_start")) and not (
        sh is not None and sh.status == "prebieha" and ensure_utc(sh.start_utc) <= now)
    if unknown:
        # nástup rušňovodiča nie je zadaný: časy z odporúčania sú len odhad – nezobrazujú sa ako fakt
        from ..notifikacie.texty import crew_relief_text

        out.update(name=crew.get("driver") or out["name"], start=None, limit=None, left_min=None, relieve_by=None,
                   end_p90=None, status=crew.get("status"), unknown_start=True, estimated=True,
                   relieve_by_estimate=crew.get("relieve_by"), relieve_note=crew_relief_text(crew),
                   color={"ČERVENÁ": "cervena"}.get(crew.get("status"), "oranzova"))
        return out
    if crew:
        out["name"] = out["name"] or crew.get("driver") or ""
        out["status"] = crew.get("status")
        out["end_p90"] = crew.get("end_p90")
        if sh is None:
            crew_limit = parse_iso(crew.get("limit"))
            left = (crew_limit - now).total_seconds() / 60.0 if crew_limit else crew.get("left_min")
            out.update(start=crew.get("start"), limit=crew.get("limit"),
                       left_min=round(left) if isinstance(left, (int, float)) else None,
                       relieve_by=crew.get("relieve_by"),
                       color=crew_color(left if isinstance(left, (int, float)) else None, params["crew_buffer_min"]))
        worse = {"ČERVENÁ": "cervena", "ORANŽOVÁ": "oranzova"}.get(crew.get("status"))
        rank = {"cervena": 0, "oranzova": 1, "zelena": 2, "seda": 3}
        if worse and rank[worse] < rank.get(out["color"], 3):
            out["color"] = worse
        elif out["color"] == "seda" and crew.get("status") == "ZELENÁ":
            out["color"] = "zelena"
    return out


def _next_station(rec: Optional[Recommendation], now: datetime) -> Optional[dict]:
    if rec is None:
        return None
    for st in (rec.path_proposal or {}).get("stations", []) or []:
        t = parse_iso(st.get("arr") or st.get("dep"))
        if t is not None and t > now:
            return {"name": st.get("name", ""), "arr": iso(t)}
    return None


def _risk_text(route_closures: list, driver: Optional[dict], rec: Optional[Recommendation], pos: Optional[dict],
               running: bool) -> Optional[str]:
    if driver and driver.get("unknown_start") and driver.get("relieve_note"):
        return f"Rušňovodič: {driver['relieve_note']}"
    if driver and driver.get("color") in ("cervena", "oranzova") and driver.get("relieve_by"):
        return f"Rušňovodič: vystriedať najneskôr o {fmt(parse_iso(driver['relieve_by']))}"
    if route_closures:
        c = route_closures[0]
        when = "teraz" if c["aktivna"] else f"od {fmt(parse_iso(c['od']))}"
        return f"{c['druh_text'].capitalize()} {c['od_stanice']} – {c['do_stanice']} ({when})"
    if running and (pos is None or pos.get("stale")):
        return "Chýbajú čerstvé dáta o polohe"
    if rec is not None:
        for reason in rec.reasons or []:
            if isinstance(reason, str) and reason.strip():
                return reason.strip()
    return None


def _border_brief(bc: Optional[BorderCrossing]) -> Optional[dict]:
    if bc is None:
        return None
    return {"id": bc.id, "code": bc.code, "name": bc.name, "handover_station": bc.handover_station,
            "lat": bc.lat, "lon": bc.lon}


def _anchor(train: Train, ho: Optional[Handover]) -> Optional[dict]:
    """Kde zobraziť vlak bez polohy: pri preberacej stanici (na ceste k hranici) alebo vo východiskovej."""
    bc = (ho.border if ho is not None and ho.border is not None else None) or train.entry_border
    if bc is not None and bc.lat is not None and train.status in ("planovany", "na_ceste_k_hranici"):
        label = f"pri hranici {bc.name}"
        return {"lat": bc.lat, "lon": bc.lon, "kind": "hranica", "label": label}
    net = get_network()
    if net is None:
        return None
    st = net.find_station(train.origin) if train.origin else None
    if st is None:
        return None
    return {"lat": st.lat, "lon": st.lon, "kind": "vychodzia", "label": f"východisková stanica {st.name}"}


MAILBOX_MAX_AGE_MIN = 30   # bez úspešnej kontroly schránky za 30 min nikdy ZELENÁ (rovnako ako plánovač)


def _kv_age_minutes(s: Session, key: str, now: datetime) -> Optional[float]:
    """Vek záznamu agenta {"ts": ISO} v minútach (None = záznam chýba alebo je neplatný)."""
    from ..models import KeyValue

    kv = s.scalar(select(KeyValue).where(KeyValue.key == key))
    try:
        ts = ensure_utc(datetime.fromisoformat(str((kv.value or {}).get("ts"))))
    except (AttributeError, TypeError, ValueError):
        return None
    return (now - ts).total_seconds() / 60.0


def data_freshness(s: Session, now: Optional[datetime] = None) -> dict:
    """Vek dát agenta: posledná úspešná kontrola schránky a posledný cyklus agenta.

    Semafor odporúčania platí len k času prepočtu. Keď agent stojí (neprepočítava, nečíta schránku), web nesmie
    ďalej ukazovať jeho poslednú ZELENÚ (PROMPT 12: bez čerstvých dát nikdy zelená) – train_rows ju zníži na
    ORANŽOVÚ a dôvod je v „text“. Cyklus je starý po 2 × VLAKY_REEVALUATE_MINUTES, schránka po 30 min.
    """
    now = now or now_utc()
    cycle_limit = 2 * settings().reevaluate_minutes
    mailbox = _kv_age_minutes(s, "mail_last_ok", now)
    cycle = _kv_age_minutes(s, "agent_last_cycle", now)
    reasons = []
    if cycle is None:
        reasons.append("agent zatiaľ neprebehol")
    elif cycle > cycle_limit:
        reasons.append(f"posledný cyklus agenta pred {cycle:.0f} min")
    if mailbox is None:
        reasons.append("schránka agenta ešte nebola úspešne skontrolovaná")
    elif mailbox > MAILBOX_MAX_AGE_MIN:
        reasons.append(f"schránka agenta sa nekontrolovala {mailbox:.0f} min")
    return {"mailbox_min": round(mailbox, 1) if mailbox is not None else None,
            "agent_cycle_min": round(cycle, 1) if cycle is not None else None,
            "stale": bool(reasons),
            "text": f"Údaje agenta nie sú čerstvé ({', '.join(reasons)})." if reasons else ""}


def train_rows(s: Session, now: Optional[datetime] = None, include_done: bool = False,
               train_id: Optional[int] = None) -> list:
    """Vlaky pre mapu a tabuľku (zoradené podľa naliehavosti: ČERVENÁ, ORANŽOVÁ, bez odporúčania, ZELENÁ).

    Bez čerstvých dát agenta (data_freshness) sa ZELENÁ zobrazí ako ORANŽOVÁ („downgraded“, dôvod v „data_note“).
    """
    now = now or now_utc()
    cfg = settings()
    params = current_params(s, now)
    fresh = data_freshness(s, now)
    q = select(Train).options(selectinload(Train.locomotive), selectinload(Train.entry_border),
                              selectinload(Train.exit_border))
    if train_id is not None:
        q = q.where(Train.id == train_id)
    elif not include_done:
        q = q.where(Train.status.notin_(DONE_TRAIN_STATES))
    trains = list(s.scalars(q.order_by(Train.id)))
    ids = [t.id for t in trains]
    positions = latest_positions(s, ids)
    recs = latest_recommendations(s, ids)
    shifts = current_shifts(s, ids)
    handovers = entry_handovers(s, ids)
    window = closures_between(s, now - timedelta(hours=1), now + timedelta(hours=48))
    closure_sets = [(c, spans, closure_edges(c)) for c, spans in window]

    rows = []
    for t in trains:
        rec = recs.get(t.id)
        pos = _position_dict(positions.get(t.id, []), now, cfg.position_stale_minutes)
        sh = shifts.get(t.id)
        ho = handovers.get(t.id)
        driver = _driver_dict(sh, rec, params, now)
        chosen = (rec.chosen if rec is not None else None) or {}
        arr50, arr90 = parse_iso(chosen.get("arrival_p50")), parse_iso(chosen.get("arrival_p90"))
        depart = parse_iso(chosen.get("depart")) or ensure_utc(t.planned_departure_utc) or ensure_utc(t.ready_utc)
        deadline = ensure_utc(t.deadline_utc)
        slack = round((deadline - arr50).total_seconds() / 60.0) if deadline and arr50 else None
        running = t.status in ("v_jazde", "prevzaty", "odstaveny")
        estimate = None
        last = positions.get(t.id, [None])[0]
        if t.status == "v_jazde" and pos is not None and not pos["stale"] and last is not None:
            info = route_info(t)
            if info is not None:
                try:
                    estimate = estimate_position(info, last, pos["age_min"], cfg.position_stale_minutes,
                                                 (rec.path_proposal if rec is not None else None) or {})
                except Exception:  # noqa: BLE001 – odhad je len doplnok
                    log.exception("Odhad polohy vlaku %s zlyhal", t.number)
        route = route_info(t) if closure_sets else None
        route_closures = []
        if route is not None:
            for c, spans, edges in closure_sets:
                if edges and route.edges & edges:
                    nxt = next(((a, b) for a, b in spans if b >= now), None)
                    if nxt is None:
                        continue
                    route_closures.append({
                        "id": c.id, "druh": c.kind, "druh_text": CLOSURE_KIND.get(c.kind, c.kind),
                        "od_stanice": c.from_station, "do_stanice": c.to_station, "od": iso(nxt[0]),
                        "do": iso(nxt[1]), "aktivna": nxt[0] <= now <= nxt[1], "stav": c.status,
                        "intervaly": [[iso(a), iso(b)] for a, b in spans if b >= now][:10]})
            route_closures.sort(key=lambda x: x["od"] or "")
        color = rec.color if rec is not None else None
        downgraded = fresh["stale"] and color == "ZELENÁ"
        if downgraded:  # posledný prepočet už nie je aktuálny – nikdy zelená bez čerstvých dát
            color = "ORANŽOVÁ"
        stale = pos is None or pos["stale"]
        conflict = False
        if depart and route_closures:
            end = arr90 or arr50 or depart
            for rc in route_closures:
                for a, b in rc["intervaly"]:
                    if parse_iso(a) <= end and parse_iso(b) >= depart:
                        conflict = True
        row = {
            "id": t.id, "number": t.number, "run_date": t.run_date, "customer": t.customer,
            "origin": t.origin, "destination": t.destination, "relation": f"{t.origin} → {t.destination}",
            "status": t.status, "status_text": TRAIN_STATUS.get(t.status, t.status),
            "entry_border": _border_brief(t.entry_border), "exit_border": _border_brief(t.exit_border),
            "traction": t.traction, "gauge": t.gauge, "vmax_kmh": t.vmax_kmh, "length_m": t.length_m,
            "weight_t": t.weight_t, "rid": t.rid,
            "locomotive": ({"id": t.locomotive.id, "number": t.locomotive.number, "type": t.locomotive.type,
                            "traction": t.locomotive.traction} if t.locomotive else None),
            "driver": driver, "ready_utc": iso(t.ready_utc), "planned_departure_utc": iso(t.planned_departure_utc),
            "deadline_utc": iso(t.deadline_utc), "position": pos, "estimate": estimate,
            "anchor": None if pos is not None else _anchor(t, ho),
            "recommendation": _rec_dict(rec), "color": color, "display_color": "SIVÁ" if stale else (color or "SIVÁ"),
            "stale": stale, "downgraded": downgraded,
            "data_age": {"mailbox_min": fresh["mailbox_min"], "agent_cycle_min": fresh["agent_cycle_min"],
                         "stale": fresh["stale"]},
            "data_note": (fresh["text"] + " Posledné odporúčanie bolo ZELENÁ – bez čerstvých údajov sa zobrazuje "
                                          "ORANŽOVÁ.") if downgraded else "",
            "eta_p50": iso(arr50), "eta_p90": iso(arr90), "slack_min": slack,
            "delay_min": (-slack if slack is not None and slack < 0 else 0) if slack is not None else None,
            "next_station": _next_station(rec, now), "route_closures": route_closures[:5],
            "conflict": conflict, "risk": _risk_text(route_closures, driver, rec, pos, running),
            "plan": {"depart": iso(depart), "arr_p50": iso(arr50), "arr_p90": iso(arr90)},
            "handover": ({"id": ho.id, "border": ho.border.name if ho.border else "", "status": ho.status,
                          "avizo_eta": iso(ho.avizo_eta_utc), "arrived": iso(ho.arrived_utc),
                          "taken_over": iso(ho.taken_over_utc)} if ho is not None else None),
            "last_message": ({"ts": pos["ts"], "source": pos["source_text"]} if pos else None),
            "urgency": URGENCY.get(color, 2),
            "priority_rank": PRIORITY_RANK.get(rec.priority if rec is not None else None, 3),
        }
        rows.append(row)
    rows.sort(key=lambda r: (r["urgency"], r["priority_rank"], r["deadline_utc"] or "9999", r["number"]))
    return rows


def train_detail(s: Session, train_id: int, now: Optional[datetime] = None) -> Optional[dict]:
    rows = train_rows(s, now, train_id=train_id)
    return rows[0] if rows else None


# ---------------------------------------------------------------- fronta na potvrdenie
def auto_applied_unconfirmed(s: Session, limit: int = 100) -> list:
    """Udalosti zapísané automaticky (bez potvrdenia), ktorých výluka je stále len „návrh“."""
    q = (select(ExtractedEvent, Closure).join(Closure, Closure.source_event_id == ExtractedEvent.id)
         .where(ExtractedEvent.status == "aplikovane", ExtractedEvent.confirmed_at.is_(None),
                Closure.status == "navrh")
         .order_by(ExtractedEvent.created_at, ExtractedEvent.id).limit(limit))
    return list(s.execute(q).all())


def pending_queue_count(s: Session) -> int:
    waiting = s.scalar(select(func.count(ExtractedEvent.id)).where(
        ExtractedEvent.needs_confirmation.is_(True), ExtractedEvent.status == "navrh")) or 0
    auto = s.scalar(select(func.count(func.distinct(ExtractedEvent.id)))
                    .select_from(ExtractedEvent).join(Closure, Closure.source_event_id == ExtractedEvent.id)
                    .where(ExtractedEvent.status == "aplikovane", ExtractedEvent.confirmed_at.is_(None),
                           Closure.status == "navrh")) or 0
    return waiting + auto


# ---------------------------------------------------------------- upozornenia (pre dashboard)
def alerts(s: Session, now: Optional[datetime] = None, limit: int = 30) -> list:
    """Upozornenia: notifikácie agenta (24 h) + odvodené (fronta, stará poloha, pracovný čas, heartbeat)."""
    now = now or now_utc()
    cfg = settings()
    params = current_params(s, now)
    out: list = []

    queue = pending_queue_count(s)
    if queue:
        out.append({"priority": "DÔLEŽITÉ", "ts": iso(now), "text": f"Na potvrdenie čaká {queue} "
                    f"{'udalosť' if queue == 1 else 'udalosti' if queue < 5 else 'udalostí'} z e-mailov.",
                    "href": "/fronta", "kind": "fronta"})

    for r in s.scalars(select(Recommendation).options(selectinload(Recommendation.train))
                       .where(Recommendation.state == "nove", Recommendation.priority == "KRITICKÉ")
                       .order_by(Recommendation.id.desc()).limit(10)):
        out.append({"priority": "KRITICKÉ", "ts": iso(r.created_at), "kind": "odporucanie",
                    "text": r.summary or f"Vlak {r.train.number if r.train else ''}: {r.decision}",
                    "href": f"/odporucania/{r.id}"})

    active = list(s.scalars(select(Train).where(Train.status.in_(("v_jazde", "prevzaty")))))
    pos = latest_positions(s, [t.id for t in active])
    for t in active:
        rows = pos.get(t.id)
        age = (now - ensure_utc(rows[0].ts_utc)).total_seconds() / 60.0 if rows else None
        if age is None or age > cfg.position_stale_minutes:
            txt = (f"Vlak {t.number}: chýbajú čerstvé dáta o polohe"
                   + (f" (posledná pred {int(age)} min)." if age is not None else " (žiadna poloha)."))
            out.append({"priority": "DÔLEŽITÉ", "ts": iso(now), "text": txt, "href": f"/vlaky/{t.id}",
                        "kind": "poloha"})

    for sh in s.scalars(select(DriverShift).options(selectinload(DriverShift.driver))
                        .where(DriverShift.status == "prebieha")):
        duty_end = ensure_utc(sh.start_utc) + timedelta(minutes=params["crew_max_duty_min"])
        left = (duty_end - now).total_seconds() / 60.0
        if left <= 90:
            name = sh.driver.name if sh.driver else "?"
            pr = "KRITICKÉ" if left <= 30 else "DÔLEŽITÉ"
            txt = (f"Rušňovodič {name}: limit pracovného času prekročený o {int(-left)} min!" if left < 0 else
                   f"Rušňovodič {name}: do limitu pracovného času zostáva {int(left)} min "
                   f"(limit o {fmt(duty_end, False)}).")
            out.append({"priority": pr, "ts": iso(now), "text": txt, "href": "/rusnovodici", "kind": "rusnovodic"})

    hb = s.scalar(select(func.max(AgentHealth.ts)))
    if hb is not None:
        age = (now - ensure_utc(hb)).total_seconds() / 60.0
        if age > max(3 * cfg.heartbeat_minutes, 15):
            out.append({"priority": "KRITICKÉ", "ts": iso(hb), "kind": "zdravie", "href": "/zdravie",
                        "text": f"Agent neposlal stav {int(age)} min – skontrolujte, či beží."})

    seen: set = set()
    for n in s.scalars(select(Notification).where(Notification.created_at >= now - timedelta(hours=24),
                                                  Notification.status != "potlacena")
                       .order_by(Notification.id.desc()).limit(200)):
        key = n.dedup_key or n.subject
        if key in seen:
            continue
        seen.add(key)
        out.append({"priority": n.priority or "INFO", "ts": iso(n.created_at), "kind": n.kind,
                    "text": n.subject or (n.body or "")[:160],
                    "href": f"/odporucania/{n.recommendation_id}" if n.recommendation_id else None})
        if len(seen) >= limit:
            break

    out.sort(key=lambda a: (PRIORITY_RANK.get(a["priority"], 3), -(parse_iso(a["ts"]) or now).timestamp()))
    return out[:limit]


def borders_list(s: Session) -> list:
    return [{"id": b.id, "code": b.code, "name": b.name, "country": b.country, "sk_station": b.sk_station,
             "foreign_station": b.foreign_station, "handover_station": b.handover_station,
             "handover_side": b.handover_side, "gauge": b.gauge, "freight": b.freight, "lat": b.lat, "lon": b.lon,
             "dwell_p50_min": b.dwell_p50_min, "dwell_p90_min": b.dwell_p90_min, "active": b.active}
            for b in s.scalars(select(BorderCrossing).order_by(BorderCrossing.name))]


def max_ids(s: Session) -> dict:
    cl = s.execute(select(func.max(Closure.id), func.max(Closure.updated_at), func.count(Closure.id))).one()
    return {
        "pozicia": s.scalar(select(func.max(PositionReport.id))) or 0,
        "odporucanie": s.scalar(select(func.max(Recommendation.id))) or 0,
        "udalost": s.scalar(select(func.max(ExtractedEvent.id))) or 0,
        "notifikacia": s.scalar(select(func.max(Notification.id))) or 0,
        "vyluka": f"{cl[0] or 0}:{cl[2] or 0}:{iso(cl[1]) or ''}",
    }


# ---------------------------------------------------------------- vrstvy mapy: miesta na čakanie/odstavenie, vlečky
def _point(lon: float, lat: float, props: dict) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            "properties": props}


def waiting_places_geojson(s: Session) -> dict:
    """Zriaďovacie stanice (OSM) + dopravne z evidencie dispečingu (LocationInfo) s možnosťou čakať/odstaviť."""
    net = get_network()
    features: dict = {}
    if net is not None:
        for st in net.stations:
            if st.type == "yard" and st.name:
                features.setdefault(st.name, _point(st.lon, st.lat, {
                    "name": st.name, "druh": "zriaďovacia stanica", "zdroj": "OpenStreetMap",
                    "can_wait": None, "can_stable": None, "can_change_crew": None, "max_train_length_m": None,
                    "note": "", "overene": False}))
    for li in s.scalars(select(LocationInfo).where((LocationInfo.can_wait.is_(True)) |
                                                   (LocationInfo.can_stable.is_(True)))):
        st = net.find_station(li.station) if net is not None else None
        if st is None:
            continue
        props = {"name": st.name, "druh": "dopravňa", "zdroj": "evidencia dispečingu", "can_wait": li.can_wait,
                 "can_stable": li.can_stable, "can_change_crew": li.can_change_crew,
                 "max_train_length_m": li.max_train_length_m, "note": li.note or "", "overene": True}
        if st.name in features:
            features[st.name]["properties"].update({k: v for k, v in props.items() if k != "druh"})
        else:
            features[st.name] = _point(st.lon, st.lat, props)
    return {"type": "FeatureCollection", "features": list(features.values())}


def sidings_geojson() -> dict:
    net = get_network()
    if net is None:
        return {"type": "FeatureCollection", "features": []}
    return {"type": "FeatureCollection", "features": [
        _point(st.lon, st.lat, {"name": st.name or "vlečka", "druh": "odbočenie vlečky"})
        for st in net.stations if st.type == "spur_junction"]}


def train_trail(s: Session, train: Train, hours: float, now: Optional[datetime] = None) -> dict:
    since, until = hours_window(now or now_utc(), hours)
    points = list(s.scalars(select(PositionReport).where(PositionReport.train_id == train.id,
                                                         PositionReport.ts_utc >= since,
                                                         PositionReport.ts_utc <= until)
                            .order_by(PositionReport.ts_utc, PositionReport.id)))
    return trail_feature(train.id, train.number, points, since, until)


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value
