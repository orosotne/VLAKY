"""Model železničnej siete SR: vyhľadávanie dopravní, trasovanie, priradenie polohy k trati.

Graf vzniká z OpenStreetMap (vlaky/siet/osm_import.py). © prispievatelia OpenStreetMap, ODbL 1.0.
"""
from __future__ import annotations

import difflib
import gzip
import heapq
import json
import math
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional, Sequence

EARTH_R = 6371008.8
WAIT_TYPES = {"station", "yard", "border"}          # kde môže vlak predvolene čakať
PROPOSAL_TYPES = {"station", "yard", "border", "junction", "spur_junction"}  # body v návrhu trasy
DEFAULT_SPEED = {"main": 80.0, "branch": 60.0, "industrial": 30.0, "": 60.0}
SERVICE_SPEED = {"siding": 20.0, "yard": 20.0, "spur": 30.0, "crossover": 30.0}
SERVICE_COST = {"siding": 2.0, "yard": 2.0, "spur": 1.5, "crossover": 1.2}


def norm(text: str) -> str:
    """Normalizácia názvu: bez diakritiky, malé písmená, jednotné medzery a pomlčky."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = t.replace("–", "-").replace("—", "-").replace(".", ". ")
    return " ".join(t.replace("-", " - ").split())


_ALIASES = [
    (r"\bhl\. ?st\.?", "hlavna stanica"), (r"\bos\. ?st\.?", "osobna stanica"),
    (r"\bnakl\. ?st\.?", "nakladna stanica"), (r"\bzast\.?(?=\s|$)", "zastavka"),
    (r"\bzst\.?(?=\s|$)", "zastavka"), (r"\bvyh\.?(?=\s|$)", "vyhybna"),
    (r"\bodb\.?(?=\s|$)", "odbocka"), (r"\bzel\. ?st\.?", "zeleznicna stanica"),
]


def expand_aliases(normalized: str) -> str:
    import re

    t = normalized
    for pat, rep in _ALIASES:
        t = re.sub(pat, rep, t)
    return " ".join(t.split())


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(min(1.0, a)))


@dataclass(frozen=True)
class TrainSpec:
    """Parametre vlaku pre filter uskutočniteľnosti (sekcia 9.1 v PROMPT.md)."""

    traction: str = "E"                  # E (elektrická) | M (motorová)
    gauge: str = "1435"
    vmax_kmh: float = 100.0
    systems: tuple = ()                  # napr. ("25kV", "3kV"); prázdne = vie všetky systémy SR
    length_m: Optional[float] = None


@dataclass
class Edge:
    idx: int
    u: int
    v: int
    len_m: float
    maxspeed: Optional[float]
    electrified: bool
    voltage: Optional[int]
    frequency: str
    gauge: str
    usage: str
    service: str
    line_ref: tuple
    geom: list


@dataclass
class Station:
    name: str
    type: str
    lat: float
    lon: float
    node: int
    uic_ref: str = ""
    railway_ref: str = ""
    main_component: bool = True


@dataclass
class Route:
    nodes: list
    edges: list                      # indexy hrán v poradí
    length_m: float
    minutes: float
    cost: float
    stations: list = field(default_factory=list)  # [(Station, cum_km, cum_min)]


class Network:
    def __init__(self, graph_path: Path, stations_path: Path) -> None:
        with gzip.open(graph_path, "rt", encoding="utf-8") as f:
            g = json.load(f)
        self.meta = g.get("meta", {})
        self.nodes: dict[int, tuple] = {int(k): (v[0], v[1]) for k, v in g["nodes"].items()}
        self.edges: list[Edge] = []
        self.adj: dict[int, list] = defaultdict(list)
        for i, e in enumerate(g["edges"]):
            edge = Edge(i, int(e["u"]), int(e["v"]), float(e["len_m"]), e.get("maxspeed"),
                        e.get("electrified") in ("contact_line", "yes", "rail"), e.get("voltage"),
                        str(e.get("frequency") or ""), str(e.get("gauge") or "1435"),
                        e.get("usage") or "", e.get("service") or "", tuple(e.get("line_ref") or ()),
                        [tuple(p) for p in e["geom"]])
            self.edges.append(edge)
            self.adj[edge.u].append((i, edge.v))
            self.adj[edge.v].append((i, edge.u))
        with open(stations_path, encoding="utf-8") as f:
            sdata = json.load(f)
        self.stations: list[Station] = []
        for s in sdata["stations"]:
            if int(s["node"]) not in self.nodes:
                continue
            self.stations.append(Station(s["name"], s["type"], s["lat"], s["lon"], int(s["node"]),
                                         s.get("uic_ref", ""), s.get("railway_ref", ""),
                                         bool(s.get("main_component", True))))
        self.by_norm: dict[str, list] = defaultdict(list)
        self.by_node: dict[int, list] = defaultdict(list)
        self.by_ref: dict[str, Station] = {}
        for st in self.stations:
            if st.name:
                self.by_norm[norm(st.name)].append(st)
            self.by_node[st.node].append(st)
            for ref in (st.uic_ref, st.railway_ref):
                if ref:
                    self.by_ref[ref.lower()] = st
        self._grid: Optional[dict] = None
        self._closure_cache: dict = {}

    # ------------------------------------------------------------------ stanice
    def find_station(self, query: str) -> Optional[Station]:
        """Nájde dopravňu podľa názvu, kódu UIC alebo skratky ŽSR (bez ohľadu na diakritiku)."""
        if not query:
            return None
        q = query.strip()
        if q.lower() in self.by_ref:
            return self.by_ref[q.lower()]
        n = expand_aliases(norm(q))
        cands = list(self.by_norm.get(n, []))
        if not cands:
            cands = [s for k, lst in self.by_norm.items() if k.startswith(n + " ") or k == n for s in lst]
        if not cands:
            close = difflib.get_close_matches(n, list(self.by_norm.keys()), n=3, cutoff=0.88)
            cands = [s for k in close for s in self.by_norm[k]]
        if not cands:
            return None
        rank = {"station": 0, "yard": 1, "border": 2, "junction": 3, "spur_junction": 4, "halt": 5}
        cands.sort(key=lambda s: (not s.main_component, rank.get(s.type, 9), len(s.name)))
        return cands[0]

    def search_stations(self, query: str, limit: int = 10) -> list:
        n = norm(query)
        out = []
        for k, lst in self.by_norm.items():
            if n in k:
                out.extend(lst)
        out.sort(key=lambda s: (not norm(s.name).startswith(n), s.type != "station", len(s.name)))
        return out[:limit]

    def station_at_node(self, node: int) -> Optional[Station]:
        lst = self.by_node.get(node)
        if not lst:
            return None
        rank = {"station": 0, "yard": 1, "border": 2, "junction": 3, "spur_junction": 4, "halt": 5}
        return sorted(lst, key=lambda s: rank.get(s.type, 9))[0]

    # ------------------------------------------------------------------ hrany
    def edge_allowed(self, e: Edge, train: TrainSpec) -> bool:
        gauges = {g.strip() for g in e.gauge.replace(",", ";").split(";") if g.strip()} or {"1435"}
        if train.gauge not in gauges:
            return False
        if train.traction == "E":
            if not e.electrified:
                return False
            if train.systems and e.voltage:
                system = {25000: "25kV", 3000: "3kV", 15000: "15kV"}.get(e.voltage, str(e.voltage))
                if system not in train.systems:
                    return False
        return True

    def edge_minutes(self, e: Edge, train: TrainSpec) -> float:
        v = e.maxspeed or SERVICE_SPEED.get(e.service) or DEFAULT_SPEED.get(e.usage, 60.0)
        v = max(5.0, min(v, train.vmax_kmh))
        return e.len_m / 1000.0 / v * 60.0

    def edge_cost(self, e: Edge, train: TrainSpec) -> float:
        return self.edge_minutes(e, train) * SERVICE_COST.get(e.service, 1.0)

    # ------------------------------------------------------------------ trasovanie
    def shortest(self, origin: int, dest: int, train: TrainSpec,
                 penalty: Optional[dict] = None, banned: Iterable[int] = ()) -> Optional[Route]:
        """Dijkstra podľa jazdného času (vedľajšie koľaje penalizované)."""
        banned = set(banned)
        penalty = penalty or {}
        dist = {origin: 0.0}
        prev: dict[int, tuple] = {}
        heap = [(0.0, origin)]
        while heap:
            d, x = heapq.heappop(heap)
            if x == dest:
                break
            if d > dist.get(x, math.inf):
                continue
            for ei, y in self.adj.get(x, ()):
                if ei in banned:
                    continue
                e = self.edges[ei]
                if not self.edge_allowed(e, train):
                    continue
                nd = d + self.edge_cost(e, train) * penalty.get(ei, 1.0)
                if nd < dist.get(y, math.inf):
                    dist[y] = nd
                    prev[y] = (x, ei)
                    heapq.heappush(heap, (nd, y))
        if dest not in dist:
            return None
        nodes, edges = [dest], []
        while nodes[-1] != origin:
            x, ei = prev[nodes[-1]]
            nodes.append(x)
            edges.append(ei)
        nodes.reverse()
        edges.reverse()
        r = Route(nodes, edges, sum(self.edges[i].len_m for i in edges),
                  sum(self.edge_minutes(self.edges[i], train) for i in edges), dist[dest])
        r.stations = self._route_stations(r, train)
        return r

    def node_for(self, station: Station, train: TrainSpec, max_m: float = 1500.0) -> Optional[int]:
        """Uzol stanice, z ktorého vie vlak ísť (správny rozchod, trakcia); inak najbližší vhodný."""
        def ok(node: int) -> bool:
            return any(self.edge_allowed(self.edges[ei], train) for ei, _ in self.adj.get(node, ()))

        if ok(station.node):
            return station.node
        best, bd = None, max_m
        for nid, (la, lo) in self.nodes.items():
            if abs(la - station.lat) > 0.02 or abs(lo - station.lon) > 0.03:
                continue
            d = haversine(station.lat, station.lon, la, lo)
            if d < bd and ok(nid):
                best, bd = nid, d
        return best

    def resolve(self, name: str, train: TrainSpec) -> Optional[tuple]:
        st = self.find_station(name)
        if not st:
            return None
        node = self.node_for(st, train)
        return (st, node) if node is not None else None

    def route(self, origin: str, dest: str, train: TrainSpec = TrainSpec(),
              via: Sequence[str] = ()) -> Optional[Route]:
        points = [origin, *via, dest]
        res = [self.resolve(p, train) for p in points]
        if any(r is None for r in res):
            return None
        total: Optional[Route] = None
        for (_, a), (_, b) in zip(res, res[1:]):
            part = self.shortest(a, b, train)
            if part is None:
                return None
            total = part if total is None else self._join(total, part, train)
        return total

    def alternatives(self, origin: str, dest: str, train: TrainSpec = TrainSpec(), k: int = 3,
                     max_detour: float = 1.8) -> list:
        """Až k odlišných trás (penalizácia už použitých hrán). Prvá je najrýchlejšia."""
        ra, rb = self.resolve(origin, train), self.resolve(dest, train)
        if not ra or not rb:
            return []
        a, b = ra[1], rb[1]
        best = self.shortest(a, b, train)
        if not best:
            return []
        out, penalty = [best], {}
        by_ref: dict = defaultdict(list)
        for e in self.edges:
            for ref in e.line_ref:
                by_ref[ref].append(e.idx)
        for _ in range(k * 3):
            if len(out) >= k:
                break
            # penalizuje celé koridory (obe koľaje dvojkoľajnej trate), nie len použité hrany
            for r in out:
                refs = {ref for ei in r.edges for ref in self.edges[ei].line_ref}
                for ref in refs:
                    for ei in by_ref[ref]:
                        penalty[ei] = penalty.get(ei, 1.0) * 1.5
                for ei in r.edges:
                    penalty[ei] = penalty.get(ei, 1.0) * 1.5
            cand = self.shortest(a, b, train, penalty)
            if not cand or cand.minutes > best.minutes * max_detour:
                break
            if all(self._overlap(cand, r) < 0.7 for r in out):
                out.append(cand)
        return out

    def _overlap(self, r1: Route, r2: Route) -> float:
        """Podobnosť trás podľa staníc, ktorými prechádzajú (koridor, nie konkrétna koľaj)."""
        s1 = {s.name for s, _, _ in r1.stations if s.type in ("station", "yard")}
        s2 = {s.name for s, _, _ in r2.stations if s.type in ("station", "yard")}
        if not s1 or not s2:
            e2 = set(r2.edges)
            common = sum(self.edges[i].len_m for i in r1.edges if i in e2)
            return common / max(1.0, min(r1.length_m, r2.length_m))
        return len(s1 & s2) / max(1, min(len(s1), len(s2)))

    def _join(self, r1: Route, r2: Route, train: TrainSpec) -> Route:
        r = Route(r1.nodes + r2.nodes[1:], r1.edges + r2.edges, r1.length_m + r2.length_m,
                  r1.minutes + r2.minutes, r1.cost + r2.cost)
        r.stations = self._route_stations(r, train)
        return r

    def _route_stations(self, r: Route, train: TrainSpec, max_m: float = 250.0) -> list:
        """Dopravne na trase (aj keď vlak ide po inej koľaji stanice) s kumul. km a minútami."""
        if not r.edges:
            st = self.station_at_node(r.nodes[0])
            return [(st, 0.0, 0.0)] if st else []
        cum_m, cum_min = [0.0], [0.0]
        for ei in r.edges:
            e = self.edges[ei]
            cum_m.append(cum_m[-1] + e.len_m)
            cum_min.append(cum_min[-1] + self.edge_minutes(e, train))
        edge_pos = {ei: k for k, ei in enumerate(r.edges)}
        lats = [self.nodes[n][0] for n in r.nodes]
        lons = [self.nodes[n][1] for n in r.nodes]
        la0, la1, lo0, lo1 = min(lats) - 0.01, max(lats) + 0.01, min(lons) - 0.015, max(lons) + 0.015
        found: dict = {}
        first, last = self.station_at_node(r.nodes[0]), self.station_at_node(r.nodes[-1])
        for st in self.stations:
            if st.type not in PROPOSAL_TYPES or not (la0 <= st.lat <= la1 and lo0 <= st.lon <= lo1):
                continue
            m = self.match(st.lat, st.lon, max_m, prefer_edges=edge_pos.keys())
            if not m or m["edge"] not in edge_pos:
                continue
            k = edge_pos[m["edge"]]
            e = self.edges[m["edge"]]
            along = m["along_m"] if e.u == r.nodes[k] else e.len_m - m["along_m"]
            frac = along / e.len_m if e.len_m else 0.0
            pos_m = cum_m[k] + along
            pos_min = cum_min[k] + frac * (cum_min[k + 1] - cum_min[k])
            rank = {"station": 0, "yard": 1, "border": 2, "junction": 3, "spur_junction": 4}
            prev = found.get(st.name)
            if prev is None or rank.get(st.type, 9) < rank.get(prev[0].type, 9):
                found[st.name] = (st, pos_m, pos_min)
        items = sorted(found.values(), key=lambda x: x[1])
        if first and (not items or items[0][0].name != first.name):
            items.insert(0, (first, 0.0, 0.0))
        if last and items[-1][0].name != last.name:
            items.append((last, cum_m[-1], cum_min[-1]))
        out = []
        for st, pm, pmin in items:
            if out and out[-1][0].name == st.name:
                continue
            out.append((st, round(pm / 1000.0, 2), round(pmin, 1)))
        return out

    def geometry(self, r: Route) -> list:
        """Súradnice trasy [[lon, lat], ...] v smere jazdy."""
        coords: list = []
        for i, ei in enumerate(r.edges):
            e = self.edges[ei]
            g = e.geom if e.u == r.nodes[i] else list(reversed(e.geom))
            pts = [[p[1], p[0]] for p in g]
            coords.extend(pts if not coords else pts[1:])
        return coords

    # ------------------------------------------------------------------ výluky na trase
    def closure_edges(self, from_station: str, to_station: str) -> set:
        """Hrany medzi dvoma dopravňami (úsek výluky) – najkratšia cesta bez ohľadu na trakciu."""
        key = (norm(from_station), norm(to_station))
        if key in self._closure_cache:
            return self._closure_cache[key]
        a, b = self.find_station(from_station), self.find_station(to_station)
        result: set = set()
        if a and b and a.node != b.node:
            for gauge in ("1435", "1520"):
                r = self.shortest(a.node, b.node, TrainSpec(traction="M", gauge=gauge, vmax_kmh=200))
                if r:
                    result = set(r.edges)
                    break
        self._closure_cache[key] = result
        self._closure_cache[(key[1], key[0])] = result
        return result

    # ------------------------------------------------------------------ priradenie polohy
    def _build_grid(self) -> dict:
        grid: dict = defaultdict(list)
        cell = 0.01
        for e in self.edges:
            g = e.geom
            for j in range(len(g) - 1):
                (la1, lo1), (la2, lo2) = g[j], g[j + 1]
                for ci in range(int(min(la1, la2) / cell), int(max(la1, la2) / cell) + 1):
                    for cj in range(int(min(lo1, lo2) / cell), int(max(lo1, lo2) / cell) + 1):
                        grid[(ci, cj)].append((e.idx, j))
        return grid

    def match(self, lat: float, lon: float, max_m: float = 300.0,
              prefer_edges: Optional[set] = None) -> Optional[dict]:
        """Priradí polohu k najbližšej koľaji. prefer_edges = hrany plánovanej trasy (majú prednosť)."""
        if self._grid is None:
            self._grid = self._build_grid()
        cell = 0.01
        ci, cj = int(lat / cell), int(lon / cell)
        best, best_pref = None, None
        kx = EARTH_R * math.cos(math.radians(lat)) * math.pi / 180
        ky = EARTH_R * math.pi / 180
        seen = set()
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for ei, j in self._grid.get((ci + di, cj + dj), ()):
                    if (ei, j) in seen:
                        continue
                    seen.add((ei, j))
                    e = self.edges[ei]
                    (la1, lo1), (la2, lo2) = e.geom[j], e.geom[j + 1]
                    ax, ay = (lo1 - lon) * kx, (la1 - lat) * ky
                    bx, by = (lo2 - lon) * kx, (la2 - lat) * ky
                    dx, dy = bx - ax, by - ay
                    L2 = dx * dx + dy * dy
                    t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
                    px, py = ax + t * dx, ay + t * dy
                    d = math.hypot(px, py)
                    if d > max_m:
                        continue
                    cand = (d, ei, j, t, lat + py / ky, lon + px / kx)
                    if best is None or d < best[0]:
                        best = cand
                    if prefer_edges and ei in prefer_edges and (best_pref is None or d < best_pref[0]):
                        best_pref = cand
        chosen = best_pref if best_pref and best_pref[0] <= max(best[0] * 3, 60.0) else best
        if not chosen:
            return None
        d, ei, j, t, slat, slon = chosen
        e = self.edges[ei]
        along = sum(haversine(*e.geom[k], *e.geom[k + 1]) for k in range(j))
        along += t * haversine(*e.geom[j], *e.geom[j + 1])
        return {"edge": ei, "dist_m": round(d, 1), "lat": round(slat, 6), "lon": round(slon, 6),
                "along_m": round(along, 1), "edge_len_m": e.len_m}

    def nearest_station(self, lat: float, lon: float, max_m: float = 3000.0) -> Optional[Station]:
        best, bd = None, max_m
        for s in self.stations:
            if s.type not in ("station", "yard", "border", "halt"):
                continue
            if abs(s.lat - lat) > 0.05 or abs(s.lon - lon) > 0.08:
                continue
            d = haversine(lat, lon, s.lat, s.lon)
            if d < bd:
                best, bd = s, d
        return best

    def progress_on_route(self, r: Route, lat: float, lon: float, max_m: float = 300.0) -> Optional[dict]:
        """Poloha vlaku na plánovanej trase: prejdené km a zostávajúci čas podľa grafu."""
        m = self.match(lat, lon, max_m, prefer_edges=set(r.edges))
        if not m or m["edge"] not in r.edges:
            return None
        k = r.edges.index(m["edge"])
        e = self.edges[m["edge"]]
        forward = e.u == r.nodes[k]
        done = sum(self.edges[i].len_m for i in r.edges[:k])
        done += m["along_m"] if forward else e.len_m - m["along_m"]
        return {"done_m": done, "remaining_m": max(0.0, r.length_m - done), "edge_pos": k, **m}


@lru_cache(maxsize=4)
def load_network(graph_path: str, stations_path: str) -> Network:
    return Network(Path(graph_path), Path(stations_path))


def network() -> Network:
    from ..config import settings

    s = settings()
    return load_network(str(s.graph_path), str(s.stations_path))
