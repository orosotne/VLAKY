"""Geometria trás pre mapu: plánovaná trasa, obchádzka zvolenej varianty, odhad polohy medzi správami, stopa.

Všetko je len zobrazenie – odhad polohy sa vždy označí ako „odhad“ a nikdy sa nepočíta zo starých dát
(sekcia M1: „Medzi dvoma správami môže aplikácia polohu odhadnúť pozdĺž trate, ale musí to byť zreteľne
označené ako odhad.“).
"""
from __future__ import annotations

import bisect
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .common import iso, log, parse_iso

STOP_TYPES = ("station", "yard", "border")          # dopravne, kde môže vlak zastaviť (strop odhadu)
ESTIMATE_CAP_MIN = 15.0                              # odhad najviac 15 min dopredu
ESTIMATE_MAX_OFF_ROUTE_M = 300.0


@dataclass
class RouteInfo:
    edges: frozenset
    coords: list                       # [[lon, lat], ...] v smere jazdy
    cum_m: list                        # kumulatívna vzdialenosť bodov coords [m]
    edge_span: dict                    # hrana → (index prvého, index posledného bodu v coords)
    length_km: float
    minutes: float
    stations: list                     # [{"name", "type", "km", "min"}]
    name: str = ""
    variant: str = "hlavna"            # hlavna | obchadzka
    via: list = field(default_factory=list)
    edge_last: dict = field(default_factory=dict)  # hrana → index posledného bodu jej posledného výskytu v coords


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from ..siet.graf import haversine

    return haversine(lat1, lon1, lat2, lon2)


def build_route_info(net, r, name: str = "", variant: str = "hlavna", via: Optional[list] = None) -> RouteInfo:
    """RouteInfo z trasy grafu (geometria v smere jazdy + kumulatívne vzdialenosti a poloha hrán)."""
    coords: list = []
    spans: dict = {}
    last: dict = {}
    for i, ei in enumerate(r.edges):
        e = net.edges[ei]
        g = e.geom if e.u == r.nodes[i] else list(reversed(e.geom))
        pts = [[p[1], p[0]] for p in g]
        start = len(coords) - 1 if coords else 0
        coords.extend(pts if not coords else pts[1:])
        spans.setdefault(ei, (start, len(coords) - 1))
        last[ei] = len(coords) - 1
    cum = [0.0]
    for a, b in zip(coords, coords[1:], strict=False):
        cum.append(cum[-1] + _haversine(a[1], a[0], b[1], b[0]))
    return RouteInfo(frozenset(r.edges), coords, cum, spans, round(r.length_m / 1000.0, 1), round(r.minutes, 1),
                     [{"name": st.name, "type": st.type, "km": km, "min": mins} for st, km, mins in r.stations],
                     name, variant, list(via or []), last)


# ---------------------------------------------------------------- poloha na trase
def _project(coords: list, cum: list, i0: int, i1: int, lat: float, lon: float) -> Optional[tuple]:
    """Najbližší bod lomenej čiary coords[i0..i1] k (lat, lon): (vzdialenosť pozdĺž trasy [m], odchýlka [m])."""
    kx = 111320.0 * math.cos(math.radians(lat))
    ky = 110540.0
    best = None
    for i in range(max(0, i0), min(i1, len(coords) - 1)):
        (lo1, la1), (lo2, la2) = coords[i], coords[i + 1]
        ax, ay = (lo1 - lon) * kx, (la1 - lat) * ky
        bx, by = (lo2 - lon) * kx, (la2 - lat) * ky
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
        d = math.hypot(ax + t * dx, ay + t * dy)
        if best is None or d < best[1]:
            best = (cum[i] + t * (cum[i + 1] - cum[i]), d)
    return best


def locate(info: RouteInfo, lat: float, lon: float, edge: Optional[int] = None,
           max_m: float = ESTIMATE_MAX_OFF_ROUTE_M) -> Optional[float]:
    """Vzdialenosť polohy od začiatku trasy [m] (None, ak poloha nie je na trase)."""
    if len(info.coords) < 2:
        return None
    hit = None
    if edge is not None and edge in info.edge_span:
        i0, i1 = info.edge_span[edge]
        hit = _project(info.coords, info.cum_m, i0, i1, lat, lon)
    if hit is None or hit[1] > max_m:
        hit = _project(info.coords, info.cum_m, 0, len(info.coords) - 1, lat, lon)
    if hit is None or hit[1] > max_m:
        return None
    return hit[0]


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def point_at(info: RouteInfo, along_m: float) -> tuple:
    """Bod trasy vo vzdialenosti along_m: (lat, lon, smer v stupňoch)."""
    cum = info.cum_m
    along_m = max(0.0, min(along_m, cum[-1]))
    i = max(0, min(bisect.bisect_right(cum, along_m) - 1, len(cum) - 2))
    seg = cum[i + 1] - cum[i]
    t = 0.0 if seg <= 0 else (along_m - cum[i]) / seg
    (lo1, la1), (lo2, la2) = info.coords[i], info.coords[i + 1]
    return la1 + t * (la2 - la1), lo1 + t * (lo2 - lo1), _bearing(la1, lo1, la2, lo2)


def planned_speed_kmh(info: RouteInfo, path_proposal: dict, along_km: float) -> Optional[float]:
    """Rýchlosť podľa časov v návrhu trasy odporúčania na úseku, kde vlak práve je (km/h)."""
    km_by_name: dict = {}
    for st in info.stations:
        km_by_name.setdefault(st["name"], st["km"])
    pts = []
    for st in (path_proposal or {}).get("stations") or []:
        km = km_by_name.get(st.get("name"))
        if km is None:
            continue
        pts.append((km, parse_iso(st.get("arr")), parse_iso(st.get("dep"))))
    pts.sort(key=lambda x: x[0])
    for (ka, arr_a, dep_a), (kb, arr_b, dep_b) in zip(pts, pts[1:], strict=False):
        if kb <= ka or kb <= along_km:
            continue
        ta, tb = dep_a or arr_a, arr_b or dep_b
        if ta is None or tb is None or tb <= ta:
            continue
        speed = (kb - ka) / ((tb - ta).total_seconds() / 3600.0)
        return speed if 5.0 <= speed <= 200.0 else None
    return None


def estimate_position(info: RouteInfo, report, age_min: float, stale_min: float,
                      path_proposal: Optional[dict] = None) -> Optional[dict]:
    """Odhad polohy vlaku pozdĺž trasy od poslednej správy (len pre čerstvé správy, strop: ďalšia dopravňa, 15 min)."""
    if age_min > stale_min or age_min < 0.25:
        return None
    if report.speed_kmh is not None and report.speed_kmh < 2.0:
        return None  # vlak podľa poslednej správy stojí
    lat = report.snapped_lat if report.snapped_lat is not None else report.lat
    lon = report.snapped_lon if report.snapped_lon is not None else report.lon
    along0 = locate(info, lat, lon, report.matched_edge)
    if along0 is None:
        return None
    speed = planned_speed_kmh(info, path_proposal or {}, along0 / 1000.0)
    method = "plán"
    if speed is None and report.speed_kmh:
        speed, method = float(report.speed_kmh), "rýchlosť"
    if not speed or speed < 5.0:
        return None
    minutes = min(age_min, ESTIMATE_CAP_MIN)
    target = along0 + speed * minutes / 60.0 * 1000.0
    next_station = None
    for st in info.stations:
        if st["type"] in STOP_TYPES and st["km"] * 1000.0 > along0 + 30.0:
            next_station = st
            break
    capped = False
    if next_station is not None and target > next_station["km"] * 1000.0:
        target, capped = next_station["km"] * 1000.0, True
    target = min(target, info.cum_m[-1])
    if target - along0 < 30.0:
        return None
    elat, elon, bearing = point_at(info, target)
    return {"lat": round(elat, 6), "lon": round(elon, 6), "bearing": round(bearing, 1), "minutes": round(minutes, 1),
            "km": round((target - along0) / 1000.0, 2), "method": method, "speed_kmh": round(speed),
            "next_station": next_station["name"] if next_station else None, "capped": capped,
            "from": [round(lon, 6), round(lat, 6)]}


# ---------------------------------------------------------------- obchádzka zvolenej varianty (PRESMEROVAŤ)
_DETOURS: "OrderedDict[tuple, Optional[RouteInfo]]" = OrderedDict()
_DETOURS_LOCK = threading.Lock()


def detour_info(net, spec, path_proposal: dict, route_name: str = "") -> Optional[RouteInfo]:
    """Trasa zvolenej varianty z návrhu trasy (dopravne v poradí) – na mape ukáže obchádzku."""
    stations = [s for s in (path_proposal or {}).get("stations") or [] if s.get("name")]
    if len(stations) < 2:
        return None
    names = [s["name"] for s in stations]
    inner = [s["name"] for s in stations[1:-1] if s.get("type") in STOP_TYPES]
    step = max(1, math.ceil(len(inner) / 12))
    via = inner[::step][:12]
    key = (tuple(names), spec)
    with _DETOURS_LOCK:
        if key in _DETOURS:
            _DETOURS.move_to_end(key)
            return _DETOURS[key]
    info = None
    try:
        for attempt in (via, via[::2], []):
            r = net.route(names[0], names[-1], spec, via=attempt)
            if r is not None:
                info = build_route_info(net, r, route_name, "obchadzka", attempt)
                break
    except Exception:  # noqa: BLE001
        log.exception("Trasu obchádzky sa nepodarilo zostaviť")
        info = None
    with _DETOURS_LOCK:
        _DETOURS[key] = info
        while len(_DETOURS) > 256:
            _DETOURS.popitem(last=False)
    return info


# ---------------------------------------------------------------- stopa (história polôh)
def trail_coords(rows: list, min_step_m: float = 25.0) -> list:
    """Body stopy [[lon, lat], ...] z polôh (preriedené – body bližšie ako min_step_m sa vynechajú)."""
    out: list = []
    for i, (lat, lon) in enumerate(rows):
        if out:
            plon, plat = out[-1]
            if i < len(rows) - 1 and _haversine(plat, plon, lat, lon) < min_step_m:
                continue
        out.append([round(lon, 6), round(lat, 6)])
    return out


def trail_feature(train_id: int, number: str, points: list, since: datetime, until: datetime) -> dict:
    rows = [((p.snapped_lat if p.snapped_lat is not None else p.lat),
             (p.snapped_lon if p.snapped_lon is not None else p.lon)) for p in points]
    coords = trail_coords(rows)
    return {"type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords} if len(coords) >= 2 else None,
            "properties": {"train_id": train_id, "number": number, "points": len(points), "od": iso(since),
                           "do": iso(until), "prvy": iso(points[0].ts_utc) if points else None,
                           "posledny": iso(points[-1].ts_utc) if points else None}}


def hours_window(now: datetime, hours: float) -> tuple:
    hours = max(1.0, min(float(hours), 72.0))
    return now - timedelta(hours=hours), now
