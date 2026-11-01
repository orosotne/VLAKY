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
MAX_TURN_DEG = 100.0      # ostrejšia zmena smeru = úvrať (obeh rušňa)
REVERSAL_MIN = 20.0       # úvrať je možná len v stanici a stojí ~20 min (obeh rušňa, skúška bŕzd)
ROUTE_GAUGES = frozenset({"1435", "1520"})  # rozchody vlakov (TrainSpec.gauge)
STATION_AREA_M = 600.0    # obvod dopravne pre výluku v jednej dopravni (od = do)
JUNCTION_AREA_M = 200.0   # odbočka, výhybka č., vlečka – len skupina výhybiek, nie obvod stanice
JUNCTION_TYPES = frozenset({"junction", "spur_junction"})


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


def _bearing(a: tuple, b: tuple) -> float:
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _densify(geom: list, step_m: float) -> list:
    """Body geometrie doplnené tak, aby boli od seba najviac ~step_m (dlhé priame hrany majú len 2 body)."""
    if len(geom) < 2:
        return list(geom)
    out = [tuple(geom[0])]
    for p, q in zip(geom, geom[1:]):
        d = haversine(p[0], p[1], q[0], q[1])
        n = int(d // step_m)
        for k in range(1, n + 1):
            f = k / (n + 1)
            out.append((p[0] + f * (q[0] - p[0]), p[1] + f * (q[1] - p[1])))
        out.append(tuple(q))
    return out


def edge_gauges(e: "Edge") -> frozenset:
    """Rozchody koľaje (napr. „1435;1520“ = koľaj so štyrmi koľajnicami pre oba rozchody)."""
    return frozenset(g.strip() for g in e.gauge.replace(",", ";").split(";") if g.strip()) or frozenset({"1435"})


def norm_line_ref(text: str) -> str:
    """Číslo trate na porovnanie s OSM (napr. „trať 190“ → „190“, „101a“ → „101A“); iný tvar → ""."""
    import re

    m = re.search(r"(?<![0-9A-Za-z])(\d{2,3}[A-Za-z]?)(?![0-9A-Za-z])", text or "")
    return m.group(1).upper() if m else ""


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
    reversals: int = 0               # počet úvratí (obeh rušňa v stanici)


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
        # kľúče s rozvinutými skratkami („vyh. paludza“ → „vyhybna paludza“), aby sa našli aj názvy,
        # ktoré e-mailová časť uložila v skrátenom kanonickom tvare (napr. „Výh. Paludza“, „Odb. Letisko“)
        self.by_alias: dict[str, list] = defaultdict(list)
        self.by_node: dict[int, list] = defaultdict(list)
        self.by_ref: dict[str, Station] = {}
        for st in self.stations:
            if st.name:
                self.by_norm[norm(st.name)].append(st)
                self.by_alias[expand_aliases(norm(st.name))].append(st)
            self.by_node[st.node].append(st)
            for ref in (st.uic_ref, st.railway_ref):
                if ref:
                    self.by_ref[ref.lower()] = st
        self._grid: Optional[dict] = None
        self._closure_cache: dict = {}
        self._area_cache: dict = {}
        self._gauge_cache: dict = {}
        # smer odchodu z uzla po hrane (prvý segment geometrie) – na kontrolu úvratí
        self._dir: dict = {}
        for e in self.edges:
            g = e.geom
            if len(g) >= 2:
                self._dir[(e.idx, e.u)] = _bearing(g[0], g[1])
                self._dir[(e.idx, e.v)] = _bearing(g[-1], g[-2])
        # úvrať je možná v obvode stanice (uzly do 600 m od stanice alebo zriaďovacej stanice)
        node_cells: dict = defaultdict(list)
        for nid, (la, lo) in self.nodes.items():
            node_cells[(int(la / 0.01), int(lo / 0.01))].append((nid, la, lo))
        self.reverse_nodes = set()
        for st in self.stations:
            if st.type not in ("station", "yard"):
                continue
            ci, cj = int(st.lat / 0.01), int(st.lon / 0.01)
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for nid, la, lo in node_cells.get((ci + di, cj + dj), ()):
                        if haversine(st.lat, st.lon, la, lo) <= 600.0:
                            self.reverse_nodes.add(nid)

    # ------------------------------------------------------------------ stanice
    def find_station(self, query: str) -> Optional[Station]:
        """Nájde dopravňu podľa názvu, kódu UIC alebo skratky ŽSR (bez ohľadu na diakritiku)."""
        if not query:
            return None
        q = query.strip()
        if q.lower() in self.by_ref:
            return self.by_ref[q.lower()]
        n = expand_aliases(norm(q))
        cands = list(self.by_norm.get(n, [])) or list(self.by_alias.get(n, []))
        if not cands:
            cands = [s for k, lst in self.by_alias.items() if k.startswith(n + " ") or k == n for s in lst]
        if not cands:
            close = difflib.get_close_matches(n, list(self.by_alias.keys()), n=3, cutoff=0.88)
            cands = [s for k in close for s in self.by_alias[k]]
        cands = list({id(s): s for s in cands}.values())
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
    def turn_penalty(self, prev_edge: Optional[int], node: int, next_edge: int) -> Optional[float]:
        """Penalizácia prechodu prev_edge → node → next_edge (None = zakázané).

        Ostrá zmena smeru (> MAX_TURN_DEG) znamená úvrať: v stanici je možná za REVERSAL_MIN minút,
        inde (výhybka na trati, odbočka) je zakázaná – vlak nemôže „zatočiť“ do protismeru.
        """
        if prev_edge is None:
            return 0.0
        d_in = self._dir.get((prev_edge, node))
        d_out = self._dir.get((next_edge, node))
        if d_in is None or d_out is None:
            return 0.0
        arrive = (d_in + 180.0) % 360.0          # smer príchodu do uzla
        turn = abs((d_out - arrive + 180.0) % 360.0 - 180.0)
        if turn <= MAX_TURN_DEG:
            return 0.0
        return REVERSAL_MIN if node in self.reverse_nodes else None

    def shortest(self, origin, dest, train: TrainSpec,
                 penalty: Optional[dict] = None, banned: Iterable[int] = ()) -> Optional[Route]:
        """Dijkstra nad hranami (stav = uzol + hrana príchodu) s kontrolou úvratí.

        origin/dest môžu byť uzol alebo množina uzlov (obvod stanice). Cena = jazdný čas
        (vedľajšie koľaje penalizované) + úvrate v staniciach; bez obratov na trati.
        """
        banned = set(banned)
        penalty = penalty or {}
        origins = set(origin) if isinstance(origin, (set, frozenset, list, tuple)) else {origin}
        dests = set(dest) if isinstance(dest, (set, frozenset, list, tuple)) else {dest}
        dist: dict = {}
        prev: dict = {}
        heap = []
        for o in origins:
            dist[(o, -1)] = 0.0
            heap.append((0.0, o, -1))
        heapq.heapify(heap)
        found = None
        while heap:
            d, x, via = heapq.heappop(heap)
            if x in dests:
                found = (x, via)
                break
            if d > dist.get((x, via), math.inf):
                continue
            for ei, y in self.adj.get(x, ()):
                if ei in banned or ei == via:
                    continue
                e = self.edges[ei]
                if not self.edge_allowed(e, train):
                    continue
                tp = self.turn_penalty(None if via < 0 else via, x, ei)
                if tp is None:
                    continue
                nd = d + self.edge_cost(e, train) * penalty.get(ei, 1.0) + tp
                key = (y, ei)
                if nd < dist.get(key, math.inf):
                    dist[key] = nd
                    prev[key] = (x, via, ei)
                    heapq.heappush(heap, (nd, y, ei))
        if found is None:
            return None
        nodes, edges = [found[0]], []
        state = found
        reversals = 0
        while state[1] != -1:
            x, via, ei = prev[state]
            nodes.append(x)
            edges.append(ei)
            state = (x, via)
        nodes.reverse()
        edges.reverse()
        minutes = sum(self.edge_minutes(self.edges[i], train) for i in edges)
        for k in range(1, len(edges)):
            if self.turn_penalty(edges[k - 1], nodes[k], edges[k]):
                reversals += 1
        r = Route(nodes, edges, sum(self.edges[i].len_m for i in edges),
                  minutes + reversals * REVERSAL_MIN, dist[found])
        r.reversals = reversals
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

    def area_nodes(self, station: Station, train: TrainSpec, radius_m: float = 400.0) -> set:
        """Uzly v obvode stanice, z ktorých vie vlak ísť (trasa môže začať/skončiť kdekoľvek v stanici)."""
        out = set()
        for nid, (la, lo) in self.nodes.items():
            if abs(la - station.lat) > 0.005 or abs(lo - station.lon) > 0.008:
                continue
            if haversine(station.lat, station.lon, la, lo) <= radius_m and any(
                    self.edge_allowed(self.edges[ei], train) for ei, _ in self.adj.get(nid, ())):
                out.add(nid)
        node = self.node_for(station, train)
        if node is not None:
            out.add(node)
        return out

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
        start: set = self.area_nodes(res[0][0], train)
        for k in range(1, len(res)):
            target = self.area_nodes(res[k][0], train)
            part = self.shortest(start, target, train)
            if part is None:
                return None
            total = part if total is None else self._join(total, part, train)
            start = {part.nodes[-1]}
        if total is not None:
            total.stations = self._route_stations(total, train, first=res[0][0], last=res[-1][0])
        return total

    def alternatives(self, origin: str, dest: str, train: TrainSpec = TrainSpec(), k: int = 3,
                     max_detour: float = 1.8) -> list:
        """Až k odlišných trás (penalizácia už použitých hrán). Prvá je najrýchlejšia."""
        ra, rb = self.resolve(origin, train), self.resolve(dest, train)
        if not ra or not rb:
            return []
        a, b = self.area_nodes(ra[0], train), self.area_nodes(rb[0], train)
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
        for r in out:
            r.stations = self._route_stations(r, train, first=ra[0], last=rb[0])
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
                  r1.minutes + r2.minutes, r1.cost + r2.cost, reversals=r1.reversals + r2.reversals)
        r.stations = self._route_stations(r, train)
        return r

    def _route_stations(self, r: Route, train: TrainSpec, max_m: float = 250.0,
                        first: Optional[Station] = None, last: Optional[Station] = None) -> list:
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
        first = first or self.station_at_node(r.nodes[0])
        last = last or self.station_at_node(r.nodes[-1])
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
        if first:
            found.pop(first.name, None)
        if last:
            found.pop(last.name, None)
        items = sorted(found.values(), key=lambda x: x[1])
        if first:
            items.insert(0, (first, 0.0, 0.0))
        if last:
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
    def closure_edges(self, from_station: str, to_station: str, line_ref: str = "") -> set:
        """Všetky koľaje úseku medzi dvoma dopravňami (úsek výluky).

        Základ je najkratšia cesta medzi dopravňami (bez ohľadu na trakciu) – medzi obvodmi staníc aj medzi
        ich hlavnými uzlami – po rozchode, ktorý obsluhujú obe dopravne (station_gauges); ak spoločný rozchod
        nemajú, po oboch. Koľaje v obvode koncových dopravní sa z úseku vynechajú (zhlavia spoločné s inými
        traťami – výluka odbočnej trate nesmie zastaviť vlak na hlavnej trati). Pridajú sa súbežné koľaje
        rovnakého rozchodu (napr. druhá koľaj dvojkoľajnej trate) a koľaje dopravní vnútri úseku, ktoré ležia
        do ~40 m od základnej cesty. Vďaka tomu výluka zasiahne vlak na ktorejkoľvek koľaji a obchádzka nevedie
        cez ten istý úsek; súbežná trať iného rozchodu (ŠRT popri trati 190) do úseku nepatrí. Výluka v jednej
        dopravni (od = do) zahŕňa všetky koľaje v jej obvode (station_area_edges).
        line_ref – číslo trate z výluky (ak je): ak by úsek viedol po oboch rozchodoch, ponechá sa ten, ktorého
        trať má toto číslo; v jednej dopravni pridá koľaje trate s týmto číslom aj iného rozchodu (area_gauges).
        Prázdna množina = úsek sa v sieti nenašiel (volajúci to musí ohlásiť).
        """
        ref = norm_line_ref(line_ref)
        key = (norm(from_station), norm(to_station), ref)
        if key in self._closure_cache:
            return self._closure_cache[key]
        result: set = set()
        area = self.closure_area_stations(from_station, to_station)
        if area is not None:
            for st in area:
                result |= self.station_area_edges(st, line_ref=ref)
        else:
            pair = self._closure_stations(from_station, to_station)
            if pair is not None:
                for a in pair[0]:
                    for b in pair[1]:
                        result |= self._section_edges(a, b, ref)
        self._closure_cache[key] = result
        self._closure_cache[(key[1], key[0], ref)] = result
        return result

    def closure_area_stations(self, from_station: str, to_station: str) -> Optional[list]:
        """Dopravne výluky v obvode dopravne (od = do, alebo dve dopravne do 300 m) – inak None (úsek medzi
        dvoma dopravňami alebo neznáma dopravňa)."""
        key = (norm(from_station), norm(to_station))
        if key in self._area_cache:
            return self._area_cache[key]
        out = None
        pair = self._closure_stations(from_station, to_station)
        if pair is not None:
            group_a, group_b = pair
            if any(a.node == b.node or haversine(a.lat, a.lon, b.lat, b.lon) < 300.0
                   for a in group_a for b in group_b):
                out = list({id(s): s for s in group_a + group_b}.values())
        self._area_cache[key] = out
        return out

    def closure_route_spans(self, r: Route, from_station: str, to_station: str, line_ref: str = "") -> list:
        """Časti trasy r (km od začiatku trasy, ako Route.stations) v obvode dopravne pri výluke v jednej dopravni.

        Bod trasy patrí do výluky, ak leží na koľaji jej obvodu (closure_edges) do polomeru obvodu od bodu
        dopravne alebo jej uzla (station_radius). Dlhá hlavná koľaj, ktorá stanicou len prechádza (Beluša 8,4 km),
        tak do výluky patrí len pri stanici – vlak, ktorý stanicu už prešiel, sa výluky netýka a pomalá jazda sa
        počíta len v obvode stanice. Prázdny zoznam = výluka nie je v jednej dopravni alebo trasa jej obvodom
        neprechádza.
        """
        stations = self.closure_area_stations(from_station, to_station)
        if not stations:
            return []
        area = self.closure_edges(from_station, to_station, line_ref)
        centers = []  # (lat, lon, polomer v m): bod a uzol každej dopravne výluky
        for st in stations:
            radius = self.station_radius(st) + 15.0  # + polovica kroku zhustenia geometrie
            centers.append((st.lat, st.lon, radius))
            if st.node in self.nodes:
                centers.append((self.nodes[st.node][0], self.nodes[st.node][1], radius))
        kms: list = []
        cum = 0.0
        for i, ei in enumerate(r.edges):
            e = self.edges[ei]
            if ei in area:
                g = e.geom if e.u == r.nodes[i] else list(reversed(e.geom))
                along, prev = 0.0, None
                for p in _densify(g, 30.0):
                    if prev is not None:
                        along += haversine(prev[0], prev[1], p[0], p[1])
                    prev = p
                    if any(abs(p[0] - c_la) < 0.01 and haversine(p[0], p[1], c_la, c_lo) <= c_r
                           for c_la, c_lo, c_r in centers):
                        kms.append((cum + min(along, e.len_m)) / 1000.0)
            cum += e.len_m
        spans: list = []
        for km in sorted(kms):
            if spans and km - spans[-1][1] <= 1.0:
                spans[-1][1] = km
            else:
                spans.append([km, km])
        return [(round(lo, 3), round(hi, 3)) for lo, hi in spans]

    def _same_name(self, st: Station) -> list:
        return list({id(s): s for s in self.by_norm.get(norm(st.name), [])}.values()) or [st]

    def _same_place(self, st: Station, radius_m: float = 2000.0) -> list:
        """Záznamy s rovnakým názvom na tom istom mieste (napr. stanica a zriaďovacia stanica Žilina-Teplička)."""
        return [st] + [s for s in self._same_name(st)
                       if s is not st and haversine(st.lat, st.lon, s.lat, s.lon) <= radius_m]

    def _closure_stations(self, from_station: str, to_station: str) -> Optional[tuple]:
        """Dopravne úseku ako skupiny záznamov s rovnakým názvom na tom istom mieste (do 2 km).

        Úsek sa počíta od každého záznamu skupiny (stanica aj zriaďovacia stanica Žilina-Teplička), aby
        zasiahol hlavnú trať aj koľaje zriaďovacej stanice. Výber find_station (stanica pred zriaďovacou
        stanicou) sa zmení, len ak je rovnako nazvaná dopravňa na inom mieste k druhej dopravni oveľa bližšie
        (menej ako polovica vzdialenosti – napr. dve odbočky s rovnakým názvom v rôznych mestách).
        """
        a, b = self.find_station(from_station), self.find_station(to_station)
        if a is None or b is None:
            return None
        d0 = haversine(a.lat, a.lon, b.lat, b.lon)
        best, bd = (a, b), d0
        for x in self._same_name(a):
            for y in self._same_name(b):
                d = haversine(x.lat, x.lon, y.lat, y.lon)
                if d < bd and d < 0.5 * d0:
                    best, bd = (x, y), d
        return self._same_place(best[0]), self._same_place(best[1])

    def _section_edges(self, a: Station, b: Station, line_ref: str = "") -> set:
        # rozchod, ktorý obsluhujú obe dopravne; bez spoločného rozchodu (napr. Ruskov – Slančík ŠRT) oba
        ga = self.station_gauges(a) & ROUTE_GAUGES
        gb = self.station_gauges(b) & ROUTE_GAUGES
        gauges = (ga & gb) or (ga | gb) or frozenset({"1435"})
        per_gauge: dict = {}
        # motorová trakcia (všetky koľaje) aj elektrická (hlavná elektrifikovaná trať, ak má iné vedenie
        # než staršia súbežná koľaj, napr. modernizované úseky) – výluka platí pre všetky koľaje úseku
        for gauge in sorted(gauges):
            for traction in ("M", "E"):
                spec = TrainSpec(traction=traction, gauge=gauge, vmax_kmh=200)
                sa, sb = self.area_nodes(a, spec), self.area_nodes(b, spec)
                if not sa or not sb:
                    continue
                found = []
                r = self.shortest(sa - sb or sa, sb - sa or sb, spec)
                if r and r.edges:
                    found.append(r)
                na, nb = self.node_for(a, spec), self.node_for(b, spec)
                if na is not None and nb is not None and na != nb:
                    r2 = self.shortest(na, nb, spec)
                    if r2 and r2.edges:
                        found.append(r2)
                if found:
                    shortest_m = min(r.length_m for r in found)
                    for r in found:
                        if r.length_m <= shortest_m * 1.5:  # obe spojenia (obvod aj hlavný uzol), ak sú porovnateľné
                            per_gauge.setdefault(gauge, set()).update(r.edges)
        if line_ref and len(per_gauge) > 1:
            # úsek po oboch rozchodoch (dopravne s oboma rozchodmi, napr. Slivník ŠRT – Budkovce ŠRT, alebo bez
            # spoločného rozchodu): číslo trate z výluky určí, ktorej trate sa týka
            with_ref = {g for g, eis in per_gauge.items() if any(line_ref in self.line_refs(ei) for ei in eis)}
            if with_ref:
                per_gauge = {g: eis for g, eis in per_gauge.items() if g in with_ref}
        base: set = set().union(*per_gauge.values()) if per_gauge else set()
        if not base:
            return set()
        # bez koľají v obvode koncových dopravní (zhlavia a koľaje spoločné s inými traťami)
        trim = min(1500.0, 0.3 * haversine(a.lat, a.lon, b.lat, b.lon))

        def inside(ei: int) -> bool:
            return all(haversine(p[0], p[1], a.lat, a.lon) <= trim or haversine(p[0], p[1], b.lat, b.lon) <= trim
                       for p in _densify(self.edges[ei].geom, 100.0))

        core = {ei for ei in base if not inside(ei)}
        if not core:  # veľmi krátky úsek – ponechať celý
            core, inside = set(base), (lambda _ei: False)
        result = set(core)
        core_len = sum(self.edges[ei].len_m for ei in core)
        # súbežné koľaje: väčšina hrany do 40 m od základnej cesty (celej, aj v obvode staníc),
        # ale hrany celé v obvode koncových dopravní sa nepridajú
        pts = [p for ei in base for p in self.edges[ei].geom]
        la0, la1 = min(p[0] for p in pts) - 0.003, max(p[0] for p in pts) + 0.003
        lo0, lo1 = min(p[1] for p in pts) - 0.004, max(p[1] for p in pts) + 0.004
        # súbežná koľaj musí mať rozchod blízkej základnej koľaje – ŠRT (1520) popri trati 190 nie je jej druhou
        # koľajou. Dlhá hrana, ktorá pri úseku leží len čiastočne, sa pridá len ako tá istá trať (ak majú obe
        # číslo trate, musia mať spoločné) – iná trať vedená kus vedľa úseku k nemu nepatrí. Dopravne bez
        # spoločného rozchodu (Ruskov – Slančík ŠRT): nejasné, ktorej trate sa výluka týka → bez obmedzenia.
        strict = bool(ga & gb)
        groups: dict = defaultdict(set)
        for ei in base:
            groups[(edge_gauges(self.edges[ei]), self.line_refs(ei))].add(ei)
        compat: dict = {}
        for e in self.edges:
            if e.idx in result:
                continue
            g = e.geom
            if not any(la0 <= p[0] <= la1 and lo0 <= p[1] <= lo1 for p in g):
                continue
            if inside(e.idx):
                continue
            sig = (edge_gauges(e), self.line_refs(e.idx))
            if sig not in compat:
                same_gauge = {ei for (gs, _r), eis in groups.items() if not strict or gs & sig[0] for ei in eis}
                same_line = {ei for (gs, refs), eis in groups.items()
                             if not strict or (gs & sig[0] and (not refs or not sig[1] or refs & sig[1]))
                             for ei in eis}
                compat[sig] = (same_gauge, same_line)
            near_base, same_line = compat[sig]
            if not near_base:
                continue
            samples = _densify(g, 150.0)
            near = near_line = 0
            for la, lo in samples:
                m = self.match(la, lo, 40.0, prefer_edges=near_base)
                if m and m["edge"] in near_base:
                    near += 1
                    near_line += m["edge"] in same_line
            if not samples:
                continue
            # väčšina hrany je pri úseku, alebo dlhá súbežná koľaj tej istej trate presahuje úsek (napr. hrana
            # 6 km, z ktorej polovica leží pozdĺž úseku výluky)
            if near / len(samples) >= 0.7 or near_line / len(samples) * e.len_m >= max(300.0, 0.5 * core_len):
                result.add(e.idx)
        return result

    def line_refs(self, ei: int) -> frozenset:
        """Čísla tratí hrany (veľkými písmenami, na porovnanie s norm_line_ref)."""
        return frozenset(r.strip().upper() for r in self.edges[ei].line_ref if r.strip())

    def edges_near(self, lat: float, lon: float, radius_m: float) -> dict:
        """Hrany, ktorých geometria prechádza do radius_m od bodu → {hrana: najmenšia vzdialenosť v m}."""
        if self._grid is None:
            self._grid = self._build_grid()
        cell = 0.01
        kx = EARTH_R * math.cos(math.radians(lat)) * math.pi / 180
        ky = EARTH_R * math.pi / 180
        dla, dlo = radius_m / ky, radius_m / kx
        out: dict = {}
        for ci in range(int((lat - dla) / cell), int((lat + dla) / cell) + 1):
            for cj in range(int((lon - dlo) / cell), int((lon + dlo) / cell) + 1):
                for ei, j in self._grid.get((ci, cj), ()):
                    g = self.edges[ei].geom
                    (la1, lo1), (la2, lo2) = g[j], g[j + 1]
                    ax, ay = (lo1 - lon) * kx, (la1 - lat) * ky
                    bx, by = (lo2 - lon) * kx, (la2 - lat) * ky
                    dx, dy = bx - ax, by - ay
                    L2 = dx * dx + dy * dy
                    t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
                    d = math.hypot(ax + t * dx, ay + t * dy)
                    if d <= radius_m and d < out.get(ei, math.inf):
                        out[ei] = d
        return out

    def station_gauges(self, station: Station, radius_m: float = 400.0) -> frozenset:
        """Rozchody, ktoré dopravňa obsluhuje.

        Rozchod koľají pri uzle dopravne a každý rozchod, ktorý má do radius_m od dopravne staničné koľaje
        (vedľajšie, zriaďovacie, vlečky, spojky) alebo výhybky. Súbežná trať iného rozchodu bez koľají v stanici
        (ŠRT popri staniciach Ruskov, Slanec, Čeľovce či Trebišov) do rozchodov dopravne nepatrí; Haniska pri
        Košiciach či Čierna nad Tisou obsluhujú oba rozchody.
        """
        key = (station.node, station.name, station.lat, station.lon)
        cached = self._gauge_cache.get(key)
        if cached is not None:
            return cached
        out: set = set()
        for ei, _ in self.adj.get(station.node, ()):
            out |= edge_gauges(self.edges[ei])
        for ei in self.edges_near(station.lat, station.lon, radius_m):
            e = self.edges[ei]
            if e.service in ("siding", "yard", "spur", "crossover"):
                out |= edge_gauges(e)
            for n in (e.u, e.v):
                adj = self.adj.get(n, ())
                if len(adj) >= 3 and haversine(station.lat, station.lon, *self.nodes[n]) <= radius_m:
                    out |= frozenset.intersection(*(edge_gauges(self.edges[x]) for x, _ in adj))
        res = frozenset(out) or frozenset({"1435"})
        self._gauge_cache[key] = res
        return res

    def station_radius(self, station: Station, radius_m: float = STATION_AREA_M) -> float:
        """Polomer obvodu dopravne pre výluku v jednej dopravni. Odbočka, výhybka č. či vlečka je len skupina
        výhybiek (JUNCTION_AREA_M) – jej výluka nesmie zasiahnuť inú trať vedenú okolím (Budatín odbočka
        400 m od trate 120)."""
        if station.type in JUNCTION_TYPES or "odbocka" in expand_aliases(norm(station.name)):
            return min(radius_m, JUNCTION_AREA_M)
        return radius_m

    def _area_near(self, station: Station, radius_m: float) -> dict:
        """Hrany do radius_m od bodu dopravne alebo od jej uzla → {hrana: najmenšia vzdialenosť v m}."""
        near = self.edges_near(station.lat, station.lon, radius_m)
        if station.node in self.nodes:
            la, lo = self.nodes[station.node]
            for ei, d in self.edges_near(la, lo, radius_m).items():
                if d < near.get(ei, math.inf):
                    near[ei] = d
        return near

    def area_gauges(self, station: Station, line_ref: str = "") -> frozenset:
        """Rozchody koľají v obvode dopravne – pre výluku v jednej dopravni aj poistku podľa dopravní na trase.

        Ako station_gauges, ale dopravňa, ktorej vlastné koľaje (pri jej uzle) majú jeden rozchod a v jej obvode
        nie je koľaj pre oba rozchody ani bod, kde sa stretávajú koľaje rôznych rozchodov, obsluhuje len svoj
        rozchod: staničné koľaje iného rozchodu v okolí patria susednej dopravni (Slivník ŠRT pri výhybni
        Slivník na trati 190, Maťovce ŠRT pri stanici Maťovce). Haniska pri Košiciach či Čierna nad Tisou majú
        v obvode stretnutie oboch rozchodov – obsluhujú oba. Číslo trate z výluky (line_ref) pridá rozchod
        koľají s týmto číslom v obvode – výslovne uvedená trať.
        """
        ref = norm_line_ref(line_ref)
        key = ("obvod", station.node, station.name, station.lat, station.lon, ref)
        cached = self._gauge_cache.get(key)
        if cached is not None:
            return cached
        loose = self.station_gauges(station)
        radius = self.station_radius(station)
        near = self._area_near(station, radius)
        own: frozenset = frozenset()
        for ei, _ in self.adj.get(station.node, ()):
            own |= edge_gauges(self.edges[ei])
        out = loose
        if own and own != loose:
            refs = [(station.lat, station.lon)] + ([self.nodes[station.node]] if station.node in self.nodes else [])
            mixed = any(len(edge_gauges(self.edges[ei])) > 1 for ei in near)
            if not mixed:
                for n in {x for ei in near for x in (self.edges[ei].u, self.edges[ei].v)}:
                    adj = self.adj.get(n, ())
                    if len(adj) >= 2 and len(frozenset().union(*(edge_gauges(self.edges[x]) for x, _ in adj))) > 1 \
                            and any(haversine(la, lo, *self.nodes[n]) <= radius for la, lo in refs):
                        mixed = True
                        break
            if not mixed:
                out = own & loose or own
        if ref:
            for ei in near:
                if ref in self.line_refs(ei):
                    out = out | edge_gauges(self.edges[ei])
        self._gauge_cache[key] = out
        return out

    def station_area_edges(self, station: Station, radius_m: float = STATION_AREA_M, connect_m: float = 1500.0,
                           line_ref: str = "") -> set:
        """Koľaje v obvode dopravne – pre výluku alebo mimoriadnosť v jednej dopravni (od = do).

        Hrana patrí do obvodu, ak niektorá jej časť leží do polomeru obvodu od dopravne (od jej bodu aj od jej
        uzla na koľaji – bod dlhej stanice nemusí byť v jej strede; odbočka len 200 m, station_radius) a má rozchod
        koľají obvodu (area_gauges). Patria sem teda aj hlavné koľaje, ktoré stanicou len prechádzajú a ich uzly
        sú ďaleko za stanicou (dlhé hrany na trati, napr. Beluša, Ilava, Bytča), nie však súbežná trať iného
        rozchodu bez koľají v stanici (ŠRT popri stanici Ruskov, trať 190 pri stanici Slivník ŠRT) ani iná trať,
        ktorá vedie len okolím a s koľajami pri dopravni nie je spojená ani do connect_m (zhlavia dlhej stanice
        môžu byť ďaleko od jej bodu). line_ref – číslo trate z výluky (area_gauges).
        """
        radius_m = self.station_radius(station, radius_m)
        gauges = self.area_gauges(station, line_ref)
        near = self._area_near(station, radius_m)
        area = {ei for ei in near if edge_gauges(self.edges[ei]) & gauges}
        if not area:  # dopravňa bez koľají v okolí (chyba dát) – aspoň hrany pri jej uzle
            return {ei for ei, _ in self.adj.get(station.node, ())}
        # spojitosť v širšom okolí (union-find nad uzlami koľají rovnakého rozchodu)
        parent: dict = {}

        def find(x: int) -> int:
            root = x
            while parent.get(root, root) != root:
                root = parent[root]
            while parent.get(x, x) != root:
                parent[x], x = root, parent[x]
            return root

        wide = {ei for ei in self.edges_near(station.lat, station.lon, connect_m)
                if edge_gauges(self.edges[ei]) & gauges}
        for ei in wide | area:
            ra, rb = find(self.edges[ei].u), find(self.edges[ei].v)
            if ra != rb:
                parent[ra] = rb
        closest = min(near[ei] for ei in area)
        seeds = {find(station.node)} | {find(self.edges[ei].u) for ei in area
                                        if near[ei] <= max(150.0, closest + 100.0)}
        return {ei for ei in area if find(self.edges[ei].u) in seeds}

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
