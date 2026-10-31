"""Import železničnej siete SR z OpenStreetMap (súbor .osm.pbf).

Výstup (adresár --out, predvolene data/siet):
  graf.json.gz      – smerovateľný graf (uzly, hrany s atribútmi a geometriou)
  trate.geojson.gz  – zjednodušené trate na zobrazenie na mape
  stanice.json      – dopravné body (stanice, zastávky, odbočky…) s napojením na graf

Dáta © prispievatelia OpenStreetMap, licencia ODbL 1.0 (https://www.openstreetmap.org/copyright).
Odvodená databáza musí zostať pod ODbL a pri zobrazení treba uviesť zdroj.

Spustenie:
  python -m vlaky.siet.osm_import --pbf slovakia.osm.pbf --out data/siet
Zdroj extraktu (overené dostupné): https://download.openstreetmap.fr/extracts/europe/slovakia.osm.pbf
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

import osmium

STATION_TYPES = {"station", "halt", "yard", "junction", "crossover", "spur_junction",
                 "service_station", "border", "site"}
SKIP_USAGE = {"tourism", "military", "test"}
EARTH_R = 6371008.8


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def parse_speed(value: str | None) -> float | None:
    if not value:
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)", value)
    if not m:
        return None
    v = float(m.group(1))
    if "mph" in value:
        v *= 1.609344
    return v if 0 < v < 400 else None


def parse_int(value: str | None) -> int | None:
    if not value:
        return None
    m = re.match(r"\s*(\d+)", value)
    return int(m.group(1)) if m else None


def simplify(points: list, tol_m: float = 8.0) -> list:
    """Douglas-Peucker na (lat, lon) s toleranciou v metroch (lokálna projekcia)."""
    if len(points) <= 2:
        return points
    lat0 = math.radians(points[0][0])
    kx = EARTH_R * math.cos(lat0) * math.pi / 180
    ky = EARTH_R * math.pi / 180
    xy = [(p[1] * kx, p[0] * ky) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy) or 1e-9
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / norm
            if d > best:
                best, idx = d, i
        if best > tol_m:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(points, keep) if k]


class RailHandler(osmium.SimpleHandler):
    def __init__(self) -> None:
        super().__init__()
        self.ways: dict[int, dict] = {}
        self.stations: list[dict] = []
        self.routes: list[dict] = []

    def node(self, n) -> None:
        rw = n.tags.get("railway")
        if rw in STATION_TYPES or n.tags.get("public_transport") == "station" and n.tags.get("train") == "yes":
            name = n.tags.get("name")
            if not name and rw not in ("junction", "crossover", "spur_junction", "border"):
                return
            self.stations.append({
                "osm_id": n.id, "name": name or "", "type": rw or "station",
                "lat": round(n.location.lat, 6), "lon": round(n.location.lon, 6),
                "uic_ref": n.tags.get("uic_ref", ""), "railway_ref": n.tags.get("railway:ref", ""),
                "operator": n.tags.get("operator", ""),
            })

    def way(self, w) -> None:
        t = w.tags
        if t.get("railway") != "rail":
            return
        if t.get("usage") in SKIP_USAGE:
            return
        try:
            coords = [(round(n.lat, 6), round(n.lon, 6)) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        self.ways[w.id] = {
            "nodes": [n.ref for n in w.nodes], "coords": coords,
            "usage": t.get("usage", ""), "service": t.get("service", ""),
            "electrified": t.get("electrified", ""), "voltage": parse_int(t.get("voltage")),
            "frequency": t.get("frequency", ""), "gauge": t.get("gauge", "1435"),
            "maxspeed": parse_speed(t.get("maxspeed")),
            "name": t.get("name", ""), "tracks": parse_int(t.get("tracks")),
        }

    def relation(self, r) -> None:
        if r.tags.get("route") not in ("railway", "tracks"):
            return
        ways = [m.ref for m in r.members if m.type == "w"]
        if ways:
            self.routes.append({"ref": r.tags.get("ref", ""), "name": r.tags.get("name", ""), "ways": ways})


def build(pbf: str, out_dir: str, include_service: bool = True) -> dict:
    h = RailHandler()
    h.apply_file(pbf, locations=True)

    # číslo trate z relácií route=railway
    way_ref: dict[int, set] = defaultdict(set)
    for r in h.routes:
        if r["ref"]:
            for wid in r["ways"]:
                way_ref[wid].add(r["ref"])

    # koľaje: jazdné + (predvolene) aj staničné, spojky a vlečky kvôli súvislosti siete;
    # trasovanie vedľajšie koľaje penalizuje (atribút service)
    ways = {wid: w for wid, w in h.ways.items()
            if include_service or not w["service"] or w["service"] in ("spur", "crossover")}

    # index všetkých vrcholov koľají (plné rozlíšenie) na pripojenie staníc
    vertex_xy: dict[int, tuple] = {}
    for w in ways.values():
        for nid, xy in zip(w["nodes"], w["coords"]):
            vertex_xy[nid] = xy
    vgrid = _Grid(vertex_xy)
    stations, forced = [], set()
    for s in h.stations:
        max_m = 150.0 if s["type"] == "border" else 600.0
        nid, dist = vgrid.nearest(s["lat"], s["lon"], max_m)
        if nid is None:
            continue
        s["node"] = nid
        s["snap_m"] = round(dist, 1)
        stations.append(s)
        forced.add(nid)

    # uzly grafu = koncové body ciest + body zdieľané viacerými cestami + body staníc
    count: dict[int, int] = defaultdict(int)
    for w in ways.values():
        for nid in w["nodes"]:
            count[nid] += 1
    node_xy: dict[int, tuple] = {}
    edges = []
    for wid, w in ways.items():
        ids, coords = w["nodes"], w["coords"]
        start = 0
        for i in range(1, len(ids)):
            if i == len(ids) - 1 or count[ids[i]] > 1 or ids[i] in forced:
                seg_ids = ids[start:i + 1]
                seg_xy = coords[start:i + 1]
                length = sum(haversine(*seg_xy[k], *seg_xy[k + 1]) for k in range(len(seg_xy) - 1))
                if length > 0.5:
                    node_xy[seg_ids[0]] = seg_xy[0]
                    node_xy[seg_ids[-1]] = seg_xy[-1]
                    edges.append({
                        "u": seg_ids[0], "v": seg_ids[-1], "way": wid, "len_m": round(length, 1),
                        "maxspeed": w["maxspeed"], "electrified": w["electrified"],
                        "voltage": w["voltage"], "frequency": w["frequency"], "gauge": w["gauge"],
                        "usage": w["usage"], "service": w["service"], "tracks": w["tracks"],
                        "line_ref": sorted(way_ref.get(wid, [])), "name": w["name"],
                        "geom": simplify(seg_xy),
                    })
                start = i

    # zlúčenie uzlov stupňa 2 (rovnaké atribúty, nie stanice) – menší graf, geometria sa zachová
    edges = _contract(edges, forced)
    used = {e["u"] for e in edges} | {e["v"] for e in edges}
    node_xy = {k: v for k, v in node_xy.items() if k in used}
    stations = [s for s in stations if s["node"] in used]

    # súvislosť siete: najväčší komponent
    comp = _components(edges)
    biggest = max(comp.values(), key=len) if comp else set()
    for s in stations:
        s["main_component"] = s["node"] in biggest

    os.makedirs(out_dir, exist_ok=True)
    meta = {
        "source": "OpenStreetMap (© OpenStreetMap contributors, ODbL 1.0)",
        "pbf": os.path.basename(pbf),
        "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nodes": len(node_xy), "edges": len(edges), "stations": len(stations),
        "km": round(sum(e["len_m"] for e in edges) / 1000, 1),
        "components": len(comp), "main_component_nodes": len(biggest),
        "stations_in_main_component": sum(1 for s in stations if s["main_component"]),
    }
    graph = {
        "meta": meta,
        "nodes": {str(k): [v[0], v[1]] for k, v in node_xy.items()},
        "edges": edges,
    }
    with gzip.open(os.path.join(out_dir, "graf.json.gz"), "wt", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, separators=(",", ":"))
    features = []
    for e in edges:
        if e["service"] in ("siding", "yard"):
            continue  # na prehľadnej mape len jazdné koľaje, spojky a vlečky
        features.append({
            "type": "Feature",
            "properties": {"el": 1 if e["electrified"] in ("contact_line", "yes", "rail") else 0,
                           "v": e["voltage"], "g": e["gauge"], "u": e["usage"], "s": e["service"],
                           "r": ",".join(e["line_ref"]), "max": e["maxspeed"]},
            "geometry": {"type": "LineString",
                         "coordinates": [[round(p[1], 5), round(p[0], 5)] for p in simplify(e["geom"], 25.0)]},
        })
    with gzip.open(os.path.join(out_dir, "trate.geojson.gz"), "wt", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f,
                  ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(out_dir, "stanice.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "stations": stations}, f, ensure_ascii=False, indent=0)
    return meta


def _components(edges: list) -> dict:
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        a, b = find(e["u"]), find(e["v"])
        if a != b:
            parent[a] = b
    comps: dict[int, set] = defaultdict(set)
    for x in list(parent):
        comps[find(x)].add(x)
    return comps


def _attr_key(e: dict) -> tuple:
    return (e["maxspeed"], e["electrified"], e["voltage"], e["gauge"], e["usage"], e["service"],
            tuple(e["line_ref"]))


def _contract(edges: list, keep: set) -> list:
    """Zlúči reťaze hrán cez uzly stupňa 2 s rovnakými atribútmi (okrem uzlov v `keep`)."""
    adj: dict[int, list] = defaultdict(list)
    for idx, e in enumerate(edges):
        adj[e["u"]].append(idx)
        adj[e["v"]].append(idx)
    alive = [True] * len(edges)
    changed = True
    while changed:
        changed = False
        for node, idxs in list(adj.items()):
            if node in keep:
                continue
            live = [i for i in idxs if alive[i]]
            if len(live) != 2:
                continue
            a, b = (edges[i] for i in live)
            if live[0] == live[1] or _attr_key(a) != _attr_key(b):
                continue
            # orientácia: a končí v node, b začína v node
            ga = a["geom"] if a["v"] == node else list(reversed(a["geom"]))
            ua = a["u"] if a["v"] == node else a["v"]
            gb = b["geom"] if b["u"] == node else list(reversed(b["geom"]))
            vb = b["v"] if b["u"] == node else b["u"]
            if ua == vb:
                continue  # slučka
            merged = dict(a)
            merged.update({"u": ua, "v": vb, "len_m": round(a["len_m"] + b["len_m"], 1),
                           "geom": ga + gb[1:], "way": a["way"]})
            alive[live[0]] = alive[live[1]] = False
            edges.append(merged)
            alive.append(True)
            new_idx = len(edges) - 1
            adj[node] = []
            for end in (ua, vb):
                adj[end] = [i for i in adj[end] if alive[i]] + [new_idx]
            changed = True
    return [e for e, ok in zip(edges, alive) if ok]


class _Grid:
    """Jednoduchý priestorový index (mriežka ~1 km) na hľadanie najbližšieho uzla."""

    def __init__(self, node_xy: dict) -> None:
        self.cell = 0.01
        self.cells: dict[tuple, list] = defaultdict(list)
        for nid, (lat, lon) in node_xy.items():
            self.cells[(int(lat / self.cell), int(lon / self.cell))].append((nid, lat, lon))

    def nearest(self, lat: float, lon: float, max_m: float):
        ci, cj = int(lat / self.cell), int(lon / self.cell)
        best, best_d = None, max_m
        r = 1 + int(max_m / 700)
        for di in range(-r, r + 1):
            for dj in range(-r, r + 1):
                for nid, la, lo in self.cells.get((ci + di, cj + dj), ()):
                    d = haversine(lat, lon, la, lo)
                    if d < best_d:
                        best, best_d = nid, d
        return best, best_d


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--out", default="data/siet")
    ap.add_argument("--only-running-lines", action="store_true",
                    help="vynechať staničné a vedľajšie koľaje (siding/yard); predvolene sú zahrnuté")
    args = ap.parse_args()
    meta = build(args.pbf, args.out, not args.only_running_lines)
    print(json.dumps(meta, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
