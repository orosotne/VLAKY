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
import logging
import math
import re
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import case, select, text as sql_text
from sqlalchemy.orm import Session

from ..audit import audit
from ..cas import ensure_utc, fmt, local, now_utc, plus_minutes, tz
from ..config import settings
from ..db import prepare_savepoints
from ..models import (Closure, DriverShift, KeyValue, LocationInfo, Order, ParameterSet, PositionReport,
                      Recommendation, Train)
from ..notifikacie.texty import crew_relief_text, dec, eur, hours, number, relief_overdue
from ..rozhodovanie import (CERVENA, ESKALOVAT, ODSTAVIT, ORANZOVA, POCKAT, PRESMEROVAT, VYPRAVIT, ZELENA,
                            Closure as DClosure, Evaluation, Params, Recommendation as DRecommendation,
                            Route as DRoute, Segment, _normals, evaluate, recommend, select_best)
from ..siet.graf import WAIT_TYPES, Network, Route as GRoute, TrainSpec, network
from .stav import allocated_paths, border_time, entry_handover, hold_anchors  # noqa: F401 (aj pre iné moduly)

POKRACOVAT = "POKRAČOVAŤ"
DONE_STATES = {"ukonceny", "zruseny", "odovzdany"}
RUN_MARGIN = 0.04            # prirážka k vypočítanej jazdnej dobe (UIC 451-1 pre nákladné vlaky ~4 %);
                             # pri časoch z pridelenej trasy ŽSR sa nepridáva (prirážky už obsahuje)
NO_DEADLINE_MIN = 7 * 24 * 60
NO_PATH_SENTINEL = -10 ** 9  # „žiadna pridelená trasa“ → každý odchod vyžaduje novú trasu
BLOCKING = {"uplna", "mimoriadnost", "trakcne_vedenie"}
KNOWN_KINDS = BLOCKING | {"kolaj", "pomala_jazda", "obmedzenie"}   # druhy výluk, ktoré plánovač pozná
AVIZO_GRACE_MIN = 30.0       # avízo uplynulo dlhšie, príchod nie je zaznamenaný → vlak mešká, pripravenosť nie je istá


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
    note: str = ""
    source_event_id: Optional[int] = None  # výluka z e-mailu (udalosť, ktorá záznam zapísala); ručná výluka None
    track: str = ""                        # koľaj výluky koľaje („1“, „2“, „obe“; prázdne = neuvedená)
    # hrany siete, na ktoré sa okno obmedzuje (úplná výluka z výluk oboch koľají na spoločnej časti úsekov –
    # _both_tracks_windows); None = úsek from_station – to_station (closure_edges)
    edges: Optional[frozenset] = None


def _parse_hhmm(text: str) -> Optional[tuple]:
    """'HH:MM' (aj 'H.MM') → (h, m); '24:00' = koniec dňa. Neplatný údaj → None."""
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if (h == 24 and mi == 0) or (0 <= h <= 23 and 0 <= mi <= 59):
        return h, mi
    return None


def _local_at(day, hm: tuple) -> datetime:
    h, mi = hm
    extra = 0
    if h == 24:
        h, extra = 0, 1
    d = day + timedelta(days=extra)
    return datetime(d.year, d.month, d.day, h, mi, tzinfo=tz())


ORPHAN_SOURCE = "email_poistka"     # Closure.source záznamu poistky zrušenia bez cieľa (posta.spracovanie)
ORPHAN_NOTE = ("zrušenie z e-mailu nemá v registri čo zrušiť – môže ísť o novú výluku, overte vo fronte; kým ho "
               "dispečer nevybaví, počíta sa s výlukou v úseku a čase zrušenia")


def closure_windows(session: Session, start: datetime, end: datetime) -> list:
    """Konkrétne časové okná výluk v intervale (rozbalí denné opakovanie v miestnom čase).

    Chybný záznam nesmie zastaviť plánovanie ostatných vlakov: pri neplatnom dennom okne sa výluka
    započíta konzervatívne na celé obdobie (s poznámkou pre dispečera).
    """
    out = []
    rows = session.scalars(select(Closure).where(Closure.status.in_(("navrh", "overene")),
                                                 Closure.end_utc >= start, Closure.start_utc <= end))
    for c in rows:
        cs, ce = ensure_utc(c.start_utc), ensure_utc(c.end_utc)
        electric = c.electric_only or c.kind == "trakcne_vedenie"
        # neznámy druh (napr. voľný text zo staršieho formulára) – úplná výluka, inak by ju plánovač vynechal (FE0)
        kind = c.kind if c.kind in KNOWN_KINDS else "uplna"
        odd = "" if kind == c.kind else (f"neznámy druh obmedzenia „{c.kind}“ – počíta sa ako úplná výluka (overte "
                                         "druh v registri výluk)")

        def whole(note: str = "") -> Window:
            return Window(c.id, kind, cs, ce, electric, c.speed_kmh, c.from_station, c.to_station,
                          c.status, "; ".join(filter(None, [odd, note])), source_event_id=c.source_event_id,
                          track=c.track or "")

        if c.source == ORPHAN_SOURCE:
            # poistka zrušenia bez cieľa (posta.spracovanie): zrušenie z e-mailu nemalo v registri čo zrušiť – môže ísť
            # o novú výluku, kým ju dispečer nevybaví, počíta sa s výlukou v úseku a čase zrušenia
            out.append(whole(ORPHAN_NOTE))
            continue
        if not (c.daily_from or c.daily_to):
            out.append(whole())
            continue
        t1, t2 = _parse_hhmm(c.daily_from), _parse_hhmm(c.daily_to)
        if t1 is None or t2 is None or t1 == (24, 0):
            out.append(whole(f"neplatné denné okno „{c.daily_from}–{c.daily_to}“ – počíta sa celé obdobie"))
            continue
        try:
            day = local(max(cs, start)).date() - timedelta(days=1)
            last = local(min(ce, end)).date() + timedelta(days=1)
            while day <= last:
                ws, we = _local_at(day, t1), _local_at(day, t2)
                if we <= ws:
                    we += timedelta(days=1)
                ws, we = ensure_utc(ws), ensure_utc(we)
                ws, we = max(ws, cs), min(we, ce)
                if we > ws and we >= start and ws <= end:
                    out.append(Window(c.id, kind, ws, we, electric, c.speed_kmh, c.from_station,
                                      c.to_station, c.status, odd, source_event_id=c.source_event_id,
                                      track=c.track or ""))
                day += timedelta(days=1)
        except (ValueError, OverflowError) as exc:  # pragma: no cover - posledná poistka
            out.append(whole(f"denné okno sa nedalo rozbaliť ({exc}) – počíta sa celé obdobie"))
    return out + _both_tracks_windows(out, _closure_line_refs(session, [w for w in out if w.kind == "kolaj"]))


ALL_TRACKS = "*"            # výluka koľaje „obe“, „všetky“ – všetky traťové koľaje úseku


def _track_numbers(track: str) -> set:
    """Traťové koľaje výluky koľaje (označenia na porovnanie): „1“, „1. TK“ → {"1"}; „1 a 2“, „1, 2“ → {"1", "2"};
    „obe“, „oboch“, „všetky“ → {"*"} (všetky koľaje úseku); „ľavá“, „pravá“ (koľaj) → {"L"}, {"P"}. Neuvedená koľaj
    a označenie bez čísla či strany („TK“, „–“) → prázdne (koľaj nie je známa – nekombinuje sa)."""
    from ..siet.graf import norm

    t = norm(track or "").strip(" .,;-")
    if not t:
        return set()
    if t.startswith("ob") or re.search(r"\b(?:vsetk\w*|kazd\w*|all)\b", t):
        return {ALL_TRACKS}
    nums = set(re.findall(r"\d+", t))
    if nums:
        return nums
    # slovom či rímskou číslicou („prvá“, „druhej TK“, „II. TK“, „I. a II.“) – inak by sa výluka nekombinovala (FE1)
    words = {str(n) for w, n in (("prv", 1), ("druh", 2), ("tret", 3), ("stvrt", 4))
             if re.search(rf"\b{w}\w*", t)}
    roman = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}
    words |= {roman[m] for m in re.findall(r"\b(iv|i{1,3})\b(?=\s*(?:\.|tk\b|kol|a\b|,|$))", t)}
    if words:
        return words
    return ({"L"} if re.search(r"\b(?:lav\w*|l)\b", t) else set()) | \
        ({"P"} if re.search(r"\b(?:prav\w*|p)\b", t) else set())


def _all_tracks_closed(tracks: set) -> bool:
    """Vylúčené sú všetky traťové koľaje úseku: „obe“/„všetky“, alebo aspoň dve rôzne koľaje (1. a 2. traťová koľaj
    dvojkoľajnej trate, ľavá a pravá koľaj; iné dve rôzne čísla – prísnejší prípad)."""
    return ALL_TRACKS in tracks or len(tracks) >= 2


def _both_tracks_windows(windows: list, line_refs: Optional[dict] = None) -> list:
    """Výluky koľají, ktoré v rovnakom čase vylučujú 1. aj 2. traťovú koľaj tej istej časti trate (dve výluky koľaje
    s rôznymi neprázdnymi koľajami – napr. dva rozkazy, aj s rôznym číslom ROV či trate, alebo dva riadky súborného
    rozkazu –, alebo jedna výluka koľaje s oboma či všetkými koľajami): trať tam vtedy nie je zjazdná. Výluka koľaje je
    pre simuláciu len zdržanie, preto pre čas prekrytia pribudne okno úplnej výluky (s poznámkou pre dispečera) – inak
    by vlak dostal ZELENÚ cez úplne zatvorený úsek. Nepotvrdená ktorákoľvek z výluk = nepotvrdené okno (počíta sa
    s ním). Číslo trate sa neporovnáva: úsek môže patriť viacerým tratiam (Kysak – Košice je na trati 180 aj 188) –
    1. TK podľa jednej a 2. TK podľa druhej sú tie isté koľaje.

    Porovnáva sa na úrovni hrán siete, nie len rovnaký úsek: výluky s rôznymi úsekmi (2. TK len na časti úseku 1. TK,
    2. TK po medzistaničných úsekoch) sa prekrývajú na spoločných hranách (closure_edges oboch úsekov) – tie sú v čase
    prekrytia úplnou výlukou (okno s ``edges``). Len medzi dvoma dopravňami: koľaje v obvode koncových dopravní úseky
    nezahŕňajú (v stanici je koľají viac) a výluka v jednej dopravni sa nekombinuje."""
    from ..siet.graf import norm, norm_line_ref

    tracked: list = []
    for w in windows:
        if w.kind != "kolaj" or not _track_numbers(w.track):
            continue
        a, b = norm(w.from_station or ""), norm(w.to_station or "")
        if not a or not b or a == b:
            continue
        tracked.append((w, frozenset((a, b))))
    refs = line_refs or {}
    cache: dict = {}
    holder: dict = {}

    def edges(w: Window) -> frozenset:
        """Koľaje úseku výluky medzi dvoma dopravňami (prázdne – v obvode dopravne alebo mimo siete)."""
        ref = norm_line_ref(refs.get(w.closure_id, "") or "")
        key = (w.from_station, w.to_station, ref)
        if key not in cache:
            if "net" not in holder:
                try:
                    holder["net"] = network()
                except Exception:  # noqa: BLE001 – bez siete len rovnaký úsek (ako doteraz)
                    holder["net"] = None
            net = holder["net"]
            if net is None or net.closure_area_stations(w.from_station, w.to_station) is not None:
                cache[key] = frozenset()
            else:
                cache[key] = frozenset(net.closure_edges(w.from_station, w.to_station, ref))
        return cache[key]

    out: list = []
    seen: set = set()
    for i, (x, px) in enumerate(tracked):
        for y, py in tracked[i:]:
            tracks = _track_numbers(x.track) | _track_numbers(y.track)
            if not _all_tracks_closed(tracks):
                continue
            s, e = max(x.start, y.start), min(x.end, y.end)
            if e <= s:
                continue
            ids = sorted({x.closure_id, y.closure_id})
            which = f"výluka #{ids[0]}" if len(ids) == 1 else f"výluky #{ids[0]} a #{ids[1]}"
            both = len(tracks - {ALL_TRACKS}) == 2 or any(norm(w.track or "").startswith("ob") for w in (x, y))
            what = "obe traťové koľaje" if both else "všetky traťové koľaje"
            status = "overene" if x.status == y.status == "overene" else "navrh"
            if px == py:
                key = (min(x.closure_id, y.closure_id), s, e)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Window(ids[0], "uplna", s, e, False, None, x.from_station, x.to_station, status,
                                  f"v čase {fmt(s)} – {fmt(e)} sú vylúčené {what} ({which}) – úsek nie je zjazdný, "
                                  f"počíta sa ako úplná výluka"))
                continue
            ex, ey = edges(x), edges(y)
            common = ex & ey
            key = ("hrany", tuple(ids), s, e)
            if not common or key in seen:
                continue
            seen.add(key)
            inner = y if (ey <= ex or (not ex <= ey and len(ey) <= len(ex))) else x
            out.append(Window(ids[0], "uplna", s, e, False, None, inner.from_station, inner.to_station, status,
                              f"v čase {fmt(s)} – {fmt(e)} sú v spoločnej časti úsekov {x.from_station} – "
                              f"{x.to_station} a {y.from_station} – {y.to_station} vylúčené {what} ({which}) – trať "
                              f"tam nie je zjazdná, počíta sa ako úplná výluka", edges=common))
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


def _route_waypoints(groute: GRoute, start_km: float = 0.0) -> list:
    """Body úsekov trasy pre rozhodovací modul (build_segments) od polohy ``start_km``: idúci vlak má ako prvý bod
    svoju aktuálnu polohu („aktuálna poloha“), potom dopravne pred ním."""
    all_wps = _waypoints(groute)
    wps = [w for w in all_wps if w.km >= start_km - 1e-6]
    if start_km > 0:
        # aktuálna poloha idúceho vlaku ako prvý bod
        mins_at = _interp_minutes(all_wps, start_km)
        wps = [Waypoint("aktuálna poloha", "position", start_km, mins_at)] + \
            [w for w in all_wps if w.km > start_km + 0.05]
    if len(wps) < 2:
        wps = all_wps[:1] + all_wps[-1:] if len(all_wps) >= 2 else all_wps
    return wps


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


def affected_route_edges(net: Network, groute: GRoute, wps: list, a_name: str, b_name: str) -> set:
    """Hrany trasy v úseku a_name – b_name (body z build_segments) – napr. na zákaz pri hľadaní obchádzky.

    Pokrýva aj výluku priradenú poistkou podľa dopravní (_station_waypoint_spans, _section_waypoint_spans),
    ktorej koľaje v modeli siete trasu nepretínajú, takže samotné closure_edges by obchádzku od úseku neodklonili.
    """
    out: set = set()
    for (a, b), edges in zip(zip(wps, wps[1:]), _segment_edges(net, groute, wps)):
        if a.name == a_name and b.name == b_name:
            out |= edges
    return out


def _name_keys(name: str) -> set:
    from ..siet.graf import expand_aliases, norm

    n = norm(name or "")
    return {n, expand_aliases(n)} - {""}


def _station_keys(net: Network, name: str) -> set:
    """Kľúče názvu dopravne z výluky: zapísaný tvar aj názov, ktorý našlo vyhľadávanie v sieti
    (skratky, napr. „Zvolen“ → „Zvolen osobná stanica“, „Výh. Paludza“ → „výhybňa Paludza“)."""
    keys = _name_keys(name)
    st = net.find_station(name) if keys else None
    if st is not None:
        keys |= _name_keys(st.name)
    return keys


def _route_edge_km(net: Network, groute: GRoute) -> list:
    """[(hrana, km začiatku, km stredu, km konca)] hrán trasy v poradí jazdy (km ako v _segment_edges)."""
    cum, out = 0.0, []
    for ei in groute.edges:
        L = net.edges[ei].len_m
        out.append((ei, cum / 1000.0, (cum + L / 2) / 1000.0, (cum + L) / 1000.0))
        cum += L
    return out


def _span_parts(wps: list, spans: list) -> dict:
    """Úseky medzi body wps, ktoré zasahujú časti trasy spans [(km od, km do)] → {index úseku: (km výluky
    v úseku, km od, km do)}. Časti sa najprv zjednotia; bodová časť (od = do) patrí úsekom, na ktorých leží."""
    merged: list = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    out: dict = {}
    for i, (a, b) in enumerate(zip(wps, wps[1:])):
        for lo, hi in merged:
            if hi - lo < 1e-6:
                if not a.km - 1e-6 <= lo <= b.km + 1e-6:
                    continue
                f, t = lo, lo
            elif a.km < hi - 1e-6 and b.km > lo + 1e-6:
                f, t = max(a.km, lo), min(b.km, hi)
            else:
                continue
            length, first, last = out.get(i, (0.0, f, t))
            out[i] = (length + t - f, min(first, f), max(last, t))
    return out


def _station_waypoint_spans(net: Network, wp_keys: list, stations: dict, keys: set, spec: TrainSpec,
                            line_ref: str) -> list:
    """Poistka výluky v jednej dopravni (od = do): dopravňa je bodom trasy → obvod dopravne okolo tohto bodu,
    aj keď sa koľaje jej obvodu v modeli siete s trasou nepretnú. Len ak koľaje obvodu majú rozchod vlaku
    (area_gauges – výluka v stanici Ruskov sa vlaku na súbežnej ŠRT netýka, mimoriadnosť v stanici Slivník ŠRT
    vlaku na trati 190). wp_keys = [(bod celej trasy, kľúče jeho názvu)], stations = {názov: Station trasy}."""
    out = []
    for wp, wk in wp_keys:
        if not (wk & keys):
            continue
        st = stations.get(wp.name)
        if st is not None and spec.gauge not in net.area_gauges(st, line_ref):
            continue
        r = (net.station_radius(st) if st is not None else 600.0) / 1000.0
        out.append((max(0.0, wp.km - r), wp.km + r))
    return out


def _section_waypoint_spans(net: Network, wp_keys: list, stations: dict, from_keys: set, to_keys: set,
                            spec: TrainSpec, sec: set, line_ref: str, route_km: list) -> list:
    """Poistka výluky medzi dvoma dopravňami, ktoré sú obe bodmi trasy: časť trasy medzi nimi, ak úsek výluky
    v sieti trasu medzi nimi nezasiahol (napr. dopravne ležia na koľajach zriaďovacej stanice vedľa hlavnej
    trate – Žilina-Teplička – Varín). Výnimka: úsek výluky vedie po trati s číslom z výluky a trasa medzi
    dopravňami toto číslo nemá – výluka je na inej trati (Slivník ŠRT – Budkovce ŠRT, trať 190, a vlak na ŠRT).
    Ak má číslo trate z výluky aj trasa (Žilina-Teplička – Varín, trať 180), poistka platí. Dopravňa sa počíta,
    len ak obsluhuje rozchod vlaku (station_gauges)."""
    if not from_keys or not to_keys:
        return []

    def positions(k: set) -> list:
        out = []
        for wp, wk in wp_keys:
            if not (wk & k):
                continue
            st = stations.get(wp.name)
            if st is not None and spec.gauge not in net.station_gauges(st):
                continue
            out.append(wp.km)
        return out

    pa, pb = positions(from_keys), positions(to_keys)
    if not pa or not pb:
        return []
    x, y = min(((p, q) for p in pa for q in pb), key=lambda pq: abs(pq[0] - pq[1]))
    lo, hi = min(x, y), max(x, y)
    between = {ei for ei, s, m, e in route_km
               if lo - 1e-6 <= m <= hi + 1e-6 or (hi - lo < 1e-6 and s - 1e-6 <= lo <= e + 1e-6)}
    if sec & between:
        return []
    if line_ref and any(line_ref in net.line_refs(e) for e in sec) and \
            not any(line_ref in net.line_refs(e) for e in between):
        return []
    return [(lo, hi)]


def _closure_line_refs(session: Session, windows: list) -> dict:
    """Čísla tratí výluk (Closure.line_ref) – spresnia úsek, ak by mohol viesť po oboch rozchodoch."""
    ids = {w.closure_id for w in windows if w.closure_id is not None}
    if not ids:
        return {}
    return {cid: ref or "" for cid, ref in session.execute(select(Closure.id, Closure.line_ref)
                                                           .where(Closure.id.in_(ids)))}


def build_segments(net: Network, groute: GRoute, windows: list, t0: datetime, spec: TrainSpec,
                   session: Session, start_km: float = 0.0, run_scale: float = 1.0) -> tuple:
    """Rozdelí trasu na úseky medzi dopravňami a priradí im výluky (s typom a časom).

    Výluka medzi dvoma dopravňami patrí úseku, ktorého koľaje zasahuje (closure_edges), a poistkou aj podľa
    dopravní na trase (_section_waypoint_spans). Výluka v jednej dopravni (od = do) patrí len časti trasy
    v obvode dopravne (closure_route_spans, poistka _station_waypoint_spans): vlak, ktorý stanicu už prešiel,
    sa jej netýka, hoci ide ešte po tej istej dlhej koľaji, a pomalá jazda sa počíta len v obvode stanice.
    Blokujúca výluka je časové okno (Closure). Výluka koľaje a pomalá jazda sú časovo obmedzené predĺženie
    jazdy (Slowdown): rozhodovací modul ich započíta len vtedy, keď vlak ide úsekom počas ich platnosti,
    a každú výluku najviac raz (nie raz za každé denné opakovanie v prehľadávanom období).
    """
    from ..rozhodovanie import Slowdown
    from ..siet.graf import norm_line_ref

    all_wps = _waypoints(groute)
    wps = _route_waypoints(groute, start_km)
    seg_edges = _segment_edges(net, groute, wps)
    route_km = _route_edge_km(net, groute)
    line_refs = _closure_line_refs(session, windows)
    wp_keys = [(wp, _name_keys(wp.name)) for wp in all_wps]
    stations = {st.name: st for st, _km, _mins in groute.stations}
    key_cache: dict = {}

    def keys(name: str) -> set:
        if name not in key_cache:
            key_cache[name] = _station_keys(net, name)
        return key_cache[name]

    # (okno, koľaje úseku výluky, {index úseku: (km výluky v úseku, km od, km do) alebo None = podľa koľají})
    mapped = []
    by_closure: dict = {}  # okná tej istej výluky (denné opakovanie) majú rovnaké úseky
    for w in windows:
        ref = norm_line_ref(line_refs.get(w.closure_id, ""))
        ck = (w.from_station, w.to_station, ref, w.edges)
        if ck not in by_closure:
            # okno obmedzené na hrany (úplná výluka zo spoločnej časti výluk oboch koľají) – len tieto hrany
            sec = set(w.edges) if w.edges is not None else net.closure_edges(w.from_station, w.to_station, ref)
            in_station = w.edges is None and net.closure_area_stations(w.from_station, w.to_station) is not None
            if in_station:
                spans = net.closure_route_spans(groute, w.from_station, w.to_station, ref)
                spans += _station_waypoint_spans(net, wp_keys, stations, keys(w.from_station) | keys(w.to_station),
                                                 spec, ref)
                parts = _span_parts(wps, spans) if spans else \
                    {i: None for i, edges in enumerate(seg_edges) if sec & edges}
            else:
                parts = {i: None for i, edges in enumerate(seg_edges) if sec & edges}
                parts.update(_span_parts(wps, _section_waypoint_spans(
                    net, wp_keys, stations, keys(w.from_station), keys(w.to_station), spec, sec, ref, route_km)))
            by_closure[ck] = (sec, parts, in_station)
        sec, parts, in_station = by_closure[ck]
        if parts and w.kind == "kolaj" and in_station and None not in parts.values():
            # výluka koľaje v stanici: zdržanie raz (úsek s väčšinou obvodu), nie v oboch susedných úsekoch
            best = max(parts, key=lambda k: parts[k][0])
            parts = {best: parts[best]}
        if parts:
            mapped.append((w, sec, parts))
    info = {li.station: li for li in session.scalars(select(LocationInfo))}
    from .kalibracia import delay_defaults

    d50, d90 = delay_defaults(session)  # kalibrované z histórie (ak vedúci schválil), inak predvolené
    segments, affecting = [], []
    for i, (a, b) in enumerate(zip(wps, wps[1:])):
        km = max(0.0, b.km - a.km)
        run = max(0.5, (b.minutes - a.minutes) * (run_scale if run_scale != 1.0 else 1 + RUN_MARGIN))
        p50 = max(0.3, d50 * km / 100.0)
        p90 = max(p50 * 1.5 + 0.5, d90 * km / 100.0)
        closures, slowdowns = [], []
        edges = seg_edges[i] if i < len(seg_edges) else set()
        for w, sec, parts in mapped:
            if i not in parts:
                continue
            part = parts[i]
            affecting.append((w, a.name, b.name))
            ws, we = _min(t0, w.start), _min(t0, w.end)
            # poloha obmedzenia v úseku (podiel dĺžky ≈ podiel jazdnej doby); podľa koľají celý úsek
            f0, f1 = (0.0, 1.0) if part is None or km <= 0 else \
                (min(1.0, max(0.0, (part[1] - a.km) / km)), min(1.0, max(0.0, (part[2] - a.km) / km)))
            if w.kind in BLOCKING:
                closures.append(DClosure(start=ws, end=we, electric_only=w.electric_only,
                                         label=f"výluka #{w.closure_id}"))
            elif w.kind == "kolaj":
                slowdowns.append(Slowdown(ws, we, extra_p50=3.0, extra_p90=15.0, group=w.closure_id,
                                          label=f"výluka koľaje #{w.closure_id}", part_from=f0, part_to=f1))
            elif w.kind == "pomala_jazda":
                v_seg = km / (run / 60.0) if run > 0 else 60.0
                if part is not None:  # časť trasy v obvode dopravne alebo medzi dopravňami trasy (poistka)
                    sec_km = min(km, part[0])
                else:
                    sec_km = min(km, sum(net.edges[e].len_m for e in (sec & edges)) / 1000.0)
                if w.speed_kmh and w.speed_kmh < v_seg and sec_km > 0:
                    extra = sec_km * (60.0 / w.speed_kmh - 60.0 / v_seg)
                else:
                    extra = 5.0
                slowdowns.append(Slowdown(ws, we, extra_run=round(extra, 1), group=w.closure_id,
                                          label=f"pomalá jazda #{w.closure_id}", part_from=f0, part_to=f1))
        li = info.get(a.name)
        can_wait = (a.type in WAIT_TYPES or a.type == "position") if li is None else li.can_wait
        segments.append(Segment(f"{a.name} – {b.name}", round(run, 1), round(p50, 1), round(p90, 1),
                                tuple(closures), can_wait, tuple(slowdowns)))
    return merge_segments(segments), wps, affecting


def _merge_slowdowns(a: Segment, b: Segment) -> tuple:
    """Obmedzenia zlúčeného úseku a+b; každé si ponechá polohu svojej časti (podiel jazdnej doby)."""
    total = a.run_min + b.run_min
    fa = a.run_min / total if total > 0 else 0.5
    return tuple([replace(s, part_from=s.part_from * fa, part_to=s.part_to * fa) for s in a.slowdowns] +
                 [replace(s, part_from=fa + s.part_from * (1 - fa), part_to=fa + s.part_to * (1 - fa))
                  for s in b.slowdowns])


def _combine(a: Segment, b: Segment, closures: tuple, can_wait: bool) -> Segment:
    p50 = a.delay_p50 + b.delay_p50
    spread = math.sqrt((a.delay_p90 - a.delay_p50) ** 2 + (b.delay_p90 - b.delay_p50) ** 2)
    name = a.name.split(" – ")[0] + " – " + b.name.split(" – ")[-1]
    return Segment(name, round(a.run_min + b.run_min, 1), round(p50, 1), round(p50 + spread, 1), closures,
                   can_wait, _merge_slowdowns(a, b))


def merge_segments(segments: list) -> list:
    """Zlúči susedné úseky s rovnakými výlukami (menej úsekov = rýchlejšia simulácia).

    Úsek s výlukou, pred ktorým sa nedá čakať, sa predĺži dozadu až po dopravňu, kde čakať možno
    (bod, odkiaľ niet návratu – sekcia 10.2 PROMPT.md). Časovo obmedzené predĺženie jazdy (pomalá jazda,
    výluka koľaje) zostane viazané na svoju časť zlúčeného úseku (podiel jazdnej doby), takže obmedzenie
    mimo času jazdy výsledok nezmení.
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


def active_shift(session: Session, train_id: int, at: Optional[datetime] = None) -> Optional[DriverShift]:
    """Zmena rušňovodiča vlaku: prebiehajúca (rušňovodič je v službe – jeho 13 h limit), inak naplánovaná – s ``at``
    tá, ktorá je na rade (posledná s nástupom najneskôr ``at``), inak najbližšia neskoršia.

    Naplánované vystriedanie (zmena s neskorším nástupom) nesmie zakryť zmenu, ktorá prebieha – inak by sa
    idúci vlak tváril ako bez záznamu o nástupe a skutočný limit rušňovodiča v službe by sa nestrážil –, ani
    naplánovanú zmenu, ktorá je na rade (vlak pred odchodom by dostal rušňovodiča vystriedania s nástupom až po
    odchode).
    """
    if at is None:
        return session.scalar(select(DriverShift).where(DriverShift.train_id == train_id,
                                                        DriverShift.status.in_(("planovana", "prebieha")))
                              .order_by(case((DriverShift.status == "prebieha", 0), else_=1),
                                        DriverShift.start_utc.desc()))
    running = session.scalar(select(DriverShift).where(DriverShift.train_id == train_id,
                                                       DriverShift.status == "prebieha")
                             .order_by(DriverShift.start_utc.desc(), DriverShift.id.desc()))
    planned = select(DriverShift).where(DriverShift.train_id == train_id, DriverShift.status == "planovana")
    return running or session.scalar(planned.where(DriverShift.start_utc <= at).order_by(
        DriverShift.start_utc.desc(), DriverShift.id.desc())) or session.scalar(
        planned.order_by(DriverShift.start_utc, DriverShift.id))


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
                return path_min / graph_min, f"Jazdné časy podľa pridelenej trasy ŽSR (pomer {dec(path_min / graph_min, 2)}×)."
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


def _mailbox_state(session: Session, now: datetime) -> tuple:
    """(vek úspešnej kontroly schránky v min alebo None, dôvod prečo nesmie byť ZELENÁ alebo None).

    Dôvod: schránka nebola úplne skontrolovaná v posledných 30 min. Kým agent dočítava nahromadenú poštu
    (``mail_last_ok`` s ``partial`` – zdroje.mailbox_check), kontrola nie je úspešná, ale text to povie pravdivo."""
    kv = session.scalar(select(KeyValue).where(KeyValue.key == "mail_last_ok"))
    val = kv.value if kv is not None and isinstance(kv.value, dict) else {}
    if val.get("partial"):
        try:
            last_full = datetime.fromisoformat(val.get("last_full")) if val.get("last_full") else None
        except (TypeError, ValueError):
            last_full = None
        age = _min(last_full, now) if last_full else None
        when = f"posledná úplná kontrola schránky pred {age:.0f} min" if age is not None else \
            "úplná kontrola schránky zatiaľ neprebehla"
        # vek sa nevráti – texty „schránka skontrolovaná pred X min“ by počas dočítavania klamali
        return None, f"Agent ešte dočítava nahromadenú poštu ({when}) – výluky nemusia byť aktuálne."
    age = mailbox_age_minutes(session, now)
    if age is None:
        return None, "Agent ešte úspešne neskontroloval svoju e-mailovú schránku – výluky nemusia byť aktuálne."
    if age > 30:
        return age, f"E-mailová schránka agenta sa nekontrolovala {age:.0f} min – výluky nemusia byť aktuálne."
    return age, None


def _mailbox_reason(session: Session, now: datetime) -> Optional[str]:
    """Dôvod, prečo nesmie byť ZELENÁ (schránka agenta nebola úplne skontrolovaná v posledných 30 min), inak None."""
    return _mailbox_state(session, now)[1]


def _unmapped_windows(net: Network, windows: list, groute: GRoute, ready: datetime, horizon_min: float) -> list:
    """Výluky v čase možnej jazdy, ktoré sa nepodarilo umiestniť na sieť a môžu ležať na trase vlaku.

    Takú výluku nemožno ticho ignorovať (napr. neznámy názov dopravne): ak aspoň jedna jej dopravňa leží
    do 3 km od trasy, alebo ak sa nenašla ani jedna, vlak nesmie byť ZELENÝ.
    """
    from ..siet.graf import _densify, haversine

    t_from = ready - timedelta(hours=2)
    t_to = ready + timedelta(minutes=horizon_min + groute.minutes * 2 + 120)
    out, seen = [], set()
    pts: Optional[list] = None
    for w in windows:
        if w.closure_id in seen or w.end < t_from or w.start > t_to:
            continue
        if w.edges or net.closure_edges(w.from_station, w.to_station):
            continue
        seen.add(w.closure_id)
        near: Optional[bool] = None
        for name in (w.from_station, w.to_station):
            st = net.find_station(name)
            if st is None:
                continue
            if pts is None:
                pts = [p for ei in groute.edges for p in _densify(net.edges[ei].geom, 500.0)]
            close = any(abs(la - st.lat) < 0.04 and abs(lo - st.lon) < 0.06 and
                        haversine(la, lo, st.lat, st.lon) <= 3000.0 for la, lo in pts)
            near = close if near is None else (near or close)
        if near is False:
            continue
        out.append(w)
    return out


def _unverified_email(affecting: list, lo: datetime, hi: datetime, first: bool = True) -> list:
    """Výluky z e-mailu na trase (``affecting`` z build_segments), ktoré dispečer ešte neoveril – záznam „navrh“ so
    zdrojovou udalosťou (čaká vo fronte na potvrdenie alebo ju agent zapísal automaticky) – a platia v období možnej
    jazdy vlaku [lo, hi]. Údaje prečítané z e-mailu (druh, termín, úsek) môžu byť nesprávne – kým ich dispečer
    neoverí, vlak nesmie byť ZELENÝ. Ručne zadaná výluka (bez udalosti) ani overená výluka sem nepatria.
    Vracia jedno okno za výluku (prvé v čase); ``first=False`` – všetky jej okná v období (denná výluka má okno
    na každý deň: dnešné mohlo skončiť a zajtrajšie zasahuje plán vlaku)."""
    out: dict = {}
    for w, _a, _b in affecting:
        if w.status != "navrh" or w.source_event_id is None or w.end < lo or w.start > hi:
            continue
        if first:
            if w.closure_id not in out or w.start < out[w.closure_id].start:
                out[w.closure_id] = w
        else:
            out.setdefault((w.closure_id, w.start, w.end), w)
    return sorted(out.values(), key=lambda w: (w.start, w.closure_id))


RESTRICTION_CAP = "obmedzenie na trase – overte parametre vlaku"
_TECH_NOTE = ("NÁVRH", "Zdroj:", "Potvrdené podľa", "Zmenené podľa", "Rozdelené podľa", "Platí do odvolania",
              "Poznámka zo záznamu", "Zrušené podľa", "Zamietnuté")


def _route_restrictions(affecting: list, lo: datetime, hi: datetime, electric: bool) -> list:
    """Obmedzenia (druh výluky „obmedzenie“ – nápravová hmotnosť, dĺžka či hmotnosť vlaku a pod.) na trase
    (``affecting`` z build_segments), ktoré platia v období [lo, hi] (jazda vlaku). Obmedzenie vlak nezastaví, ale
    plánovač parametre vlaku voči nemu neoveruje – vlak nesmie byť ZELENÝ, kým ich neoverí dispečer. Jedno okno za
    obmedzenie (prvé v období); obmedzenie len pre elektrickú trakciu sa motorového vlaku netýka."""
    out: dict = {}
    for w, _a, _b in affecting:
        if w.kind != "obmedzenie" or (w.electric_only and not electric) or w.end < lo or w.start > hi:
            continue
        if w.closure_id not in out or w.start < out[w.closure_id].start:
            out[w.closure_id] = w
    return sorted(out.values(), key=lambda w: (w.start, w.closure_id))


def _variant_restrictions(v: dict, affecting: list, electric: bool) -> list:
    """Obmedzenia na trase varianty v čase jej jazdy (odchod – príchod P90 varianty, ± 30 min)."""
    try:
        lo = datetime.fromisoformat(str(v["depart"])) - timedelta(minutes=30)
        hi = datetime.fromisoformat(str(v.get("arrival_p90") or v["depart"])) + timedelta(minutes=30)
    except (KeyError, TypeError, ValueError):
        return []
    return _route_restrictions(affecting, ensure_utc(lo), ensure_utc(hi), electric)


def _restriction_reason(session: Session, w: Window, what: str = "vlak nemôže byť ZELENÝ") -> str:
    """Dôvod poistky pri obmedzení na trase (s popisom obmedzenia z poznámky výluky, ak ho má)."""
    c = session.get(Closure, w.closure_id) if w.closure_id is not None else None
    ref = f" (č. {c.external_ref})" if c is not None and c.external_ref else ""
    desc = ""
    for line in str(getattr(c, "note", "") or "").splitlines():
        line = " ".join(line.split())
        if line and not line.startswith(_TECH_NOTE):
            desc = f": {line[:160]}"
            break
    return (f"Obmedzenie na trase – overte parametre vlaku: {w.from_station} – {w.to_station} {fmt(w.start)} – "
            f"{fmt(w.end)}, výluka #{w.closure_id}{ref}{desc} (napr. nápravová hmotnosť, dĺžka či hmotnosť vlaku). "
            f"Plánovač parametre vlaku voči obmedzeniu neoveruje – kým ich neoverí dispečer, {what}.")


def _unverified_reason(w: Window, what: str = "vlak nemôže byť ZELENÝ", later: bool = False) -> str:
    """Dôvod poistky pri neoverenej výluke z e-mailu; ``later`` – podľa prečítaného termínu leží mimo plánu vlaku
    (začína až po jeho príchode, alebo skončila pred jeho možným odchodom)."""
    why = ("plán vlaku sa jej podľa prečítaného termínu netýka, termín však mohol byť prečítaný nesprávne" if later
           else "údaje (druh, termín, úsek) mohli byť prečítané nesprávne")
    return (f"{_kind_label(w.kind)} {w.from_station} – {w.to_station} {fmt(w.start)} – {fmt(w.end)} z e-mailu "
            f"(udalosť #{w.source_event_id}) ešte nie je overená dispečerom – {why}; overte ju vo fronte na "
            f"potvrdenie, dovtedy {what}.")


def _route_guards(unmapped: list, pending: list, unverified: list, what: str = "vlak nemôže byť ZELENÝ") -> list:
    """Dôvody poistiek ZELENEJ pri jednej trase (výluka mimo siete, čakajúca udalosť z e-mailu, neoverená výluka
    z e-mailu) – pre varianty v porovnaní (pri zvolenej trase sú dôvody priamo v odporúčaní)."""
    out = [f"{_kind_label(w.kind)} {w.from_station} – {w.to_station} {fmt(w.start)} – {fmt(w.end)} sa nepodarilo "
           f"umiestniť na sieť (neznámy úsek) – kým sa to neoverí, {what}" for w in unmapped]
    out += [n.rstrip(".") for n in pending]
    out += [_unverified_reason(w, what).rstrip(".") for w in unverified]
    return out


NEAR_DEST_KM = 2.0       # idúci vlak bližšie k cieľu: len dokončiť jazdu alebo počkať pred vjazdom (žiadne odstavenie
                         # na trati ani nová ad hoc trasa pre posledný kilometer)
NEAR_CREW_START = math.inf  # ... simulácia pracovný čas rušňovodiča neráta (nástup „v nekonečne“ – limit 13 h sa nikdy
                            # neprekročí): posudzuje sa zvlášť a vystriedanie počas čakania pred vjazdom príchod nezdrží
                            # (vymyslený rušňovodič od odchodu by pri dlhom čakaní pridal 2 h a príchod by skákal)
POSITION_HOLD_MIN = 30.0  # zhoršenie ZELENÁ → ORANŽOVÁ len pre starnúcu polohu je novým odporúčaním (upozornením) až
                          # vtedy, keď trvá aspoň toľko minút (poloha staršia ako limit čerstvosti + 30 min)


def _official_progress(net: Network, groute: GRoute, pos: PositionReport) -> Optional[dict]:
    """Udalosť jazdy ŽSR/TIS/TAF je hlásenie z dopravne (bod dopravne, nie GPS): jej poloha na trase vlaku je km tejto
    dopravne podľa názvu – bod dopravne môže ležať mimo hrán trasy (východisková stanica býva 180 – 370 m od začiatku
    trasy), takže priradenie k trati by ju odmietlo ako „mimo plánovanej trasy“. Pri dopravni na trase viackrát platí
    prvý výskyt (vlak nie je ďalej, než ukazuje hlásenie). Inak None."""
    if (pos.source or "") not in OFFICIAL_SOURCES or not pos.nearest_station:
        return None
    keys = _station_keys(net, pos.nearest_station)
    kms = [km for st, km, _mins in groute.stations if _name_keys(st.name) & keys]
    if not kms:
        return None
    done = min(kms) * 1000.0
    return {"done_m": done, "remaining_m": max(0.0, groute.length_m - done), "dopravna": pos.nearest_station}


# ------------------------------------------------------------------ vyhodnotenie vlaku
_log = logging.getLogger("vlaky.planovanie")
EVAL_LOCK_KEY = 0x564C4B59      # „VLKY“ – prvý kľúč poradného zámku prepočtu vlaku (PostgreSQL; druhý je id vlaku)
_EVAL_STARTED = "vlaky_eval_started"  # session.info: {id vlaku: skutočný začiatok prebiehajúceho prepočtu}
EVAL_CLOCK_TOLERANCE = timedelta(seconds=5)  # uložený začiatok prepočtu neskorší ako „teraz“ + tolerancia = hodiny
                                             # servera sa posunuli späť (_store poradiu prepočtov neverí)


def _wall_clock() -> datetime:
    """Skutočný čas (UTC, s mikrosekundami) – začiatok prepočtu, podľa ktorého sa zoradia súbežné prepočty toho
    istého vlaku (_store). Nie now_utc(): ten testy a simulácia posúvajú (čas plánu), poradie prepočtov však určuje
    skutočný čas, keď prepočet začal čítať údaje."""
    return datetime.now(timezone.utc)


def evaluate_train(session: Session, train_id: int, now: Optional[datetime] = None,
                   trigger: str = "") -> Optional[Recommendation]:
    """Prepočíta vlak a uloží odporúčanie (_store). Skutočný začiatok prepočtu sa zaznamená ešte pred čítaním údajov
    vlaku: ak medzitým uloží výsledok prepočet, ktorý začal neskôr (napr. po potvrdení avíza, kým tento prepočet –
    „nový vlak“ – ešte bežal), výsledok tohto staršieho prepočtu sa neuloží a vráti sa novšie odporúčanie."""
    started = _wall_clock()
    starts = session.info.setdefault(_EVAL_STARTED, {})
    starts[train_id] = started
    try:
        return _evaluate_train(session, train_id, now, trigger)
    finally:
        if starts.get(train_id) == started:
            starts.pop(train_id, None)


def _evaluate_train(session: Session, train_id: int, now: Optional[datetime], trigger: str) -> Optional[Recommendation]:
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
    position_unusable = False  # idúci vlak bez čerstvej polohy na trase → nikdy ZELENÁ
    position_soft = False      # ... len preto, že poloha na trase práve zostarla (menej ako POSITION_HOLD_MIN) – zmena
                               # farby len z tohto dôvodu nie je nové odporúčanie (_store, fingerprint_color)
    arrival_floating = False   # príchod idúceho vlaku sa posúva s časom (bez polohy aj času odchodu, stará poloha)
    run_since = None           # od kedy je vlak v jazde (aj ručná zmena stavu bez skutočného času odchodu)
    near_km: Optional[float] = None  # idúci vlak tesne pred cieľom: zostávajúce km (NEAR_DEST_KM)
    near_estimated = False           # ... len podľa odhadu z plánu (bez polohy na trase)
    pos = latest_position(session, train.id)
    if pos is not None:
        data_age["position_min"] = round(_min(pos.ts_utc, now), 1)
    if running:
        from .stav import running_since

        run_since = running_since(session, train)
        fresh = pos is not None and data_age["position_min"] <= cfg.position_stale_minutes
        prog = net.progress_on_route(primary, pos.lat, pos.lon) if pos is not None else None
        if prog is None and pos is not None:
            # hlásenie ŽSR/TIS z dopravne na trase (bod dopravne mimo hrán trasy) nie je poloha mimo trasy
            prog = _official_progress(net, primary, pos)
        end_km = _km_at_minutes(primary, float("inf"))  # km cieľovej dopravne (posledný bod trasy)
        # vlak v cieľovej dopravni (GPS, hlásenie TIS/ŽSR) – aj podľa poslednej známej polohy: trasa sa
        # neprepočítava od východiska, len informácia „ukončite jazdu“
        arrived = _arrival_time(session, net, train, primary, pos, prog, end_km, now, params)
        if arrived is not None:
            return _arrived(session, train, now, trigger, params, arrived, run_since, reasons, data_age)
        if fresh and prog:
            start_km = prog["done_m"] / 1000.0
        elif fresh:
            position_unusable = True
            reasons.append("Poloha vlaku nie je na plánovanej trase – overte trasu (výpočet vychádza "
                           "z plánu).")
        elif prog:
            # stará poloha: vlak je aspoň tam, kde bol naposledy – nikdy nie ďalej podľa plánu (výluka pred meškajúcim
            # vlakom nesmie zmiznúť); príchod a koniec pracovného času z nej sa posúvajú s časom. Ak by však vlak od
            # nej podľa plánu (aj s čakaním pred výlukami) už bol v cieli, stará poloha nič nehovorí – overiť polohu,
            # nie plánovať jazdu od miesta, kde bol pred hodinami (výluky za ním, falošná eskalácia)
            fix_km = prog["done_m"] / 1000.0
            _km, due = _run_walk(session, net, primary, spec, fix_km, ensure_utc(pos.ts_utc), now)
            if due is not None:
                return _arrived(session, train, now, trigger, params, None, run_since, reasons, data_age,
                                presumed=(due, f"poslednej známej polohy ({fmt(pos.ts_utc)}, "
                                               f"pred {data_age['position_min']:.0f} min)"))
            start_km = fix_km
            position_unusable = True
            # poloha práve zostarla (napr. GPS každých 30 min, hlásenia TIS z hlavných staníc): nikdy ZELENÁ, ale
            # zhoršenie len z tohto dôvodu je novým odporúčaním až keď trvá POSITION_HOLD_MIN (búrka upozornení)
            position_soft = data_age["position_min"] < cfg.position_stale_minutes + POSITION_HOLD_MIN
            arrival_floating = True
            reasons.append(f"Chýbajú čerstvé dáta o polohe vlaku (posledná poloha pred {data_age['position_min']:.0f} "
                           f"min) – výpočet vychádza z poslednej známej polohy, vlak mohol medzitým pokračovať.")
        else:
            position_unusable = True
            reasons.append("Chýbajú čerstvé dáta o polohe vlaku – výpočet vychádza z plánu.")
        if not prog:
            # bez polohy na trase: odhad od najneskoršieho možného odchodu – skutočného (hlásenie rušňovodiča), inak
            # od času, keď vlak prešiel do jazdy (vlak išiel najneskôr vtedy – oneskorený vlak nie je hneď „v cieli“),
            # inak od minulého plánovaného; cez výluku, ktorá platila, keď by k nej vlak prišiel, neprejde
            dep_ref = _departure_estimate(train, now, run_since, earliest=False)
            if dep_ref is not None:
                start_km, due = _run_walk(session, net, primary, spec, 0.0, dep_ref, now)
                if due is not None:
                    # podľa plánu by už mal byť v cieli – poloha nie je známa: overiť, nie plánovať trasu od východiska
                    return _arrived(session, train, now, trigger, params, None, run_since, reasons, data_age,
                                    presumed=(due, f"odchodu {fmt(dep_ref)}"))
            else:
                arrival_floating = True
        left_km = max(0.0, min(end_km, primary.length_m / 1000.0) - start_km)
        if left_km <= NEAR_DEST_KM:
            # vlak stojí pred vjazdom do cieľa (alebo pred cieľom čaká na koniec výluky): zostáva dokončiť jazdu –
            # odstavenie na trati a nová ad hoc trasa pre zvyšok jazdy nemajú zmysel (ani KRITICKÁ SMS o nich).
            # Bez polohy na trase je to len odhad z plánu – text to povie (poloha sa overuje, vlak nie je ZELENÝ)
            near_km, near_estimated = left_km, not prog
            where = (f"Podľa odhadu z plánu je vlak asi {dec(left_km)} km pred cieľom ({train.destination}; poloha "
                     f"vlaku na trase nie je známa)" if near_estimated else
                     f"Vlak je {dec(left_km)} km pred cieľom ({train.destination})")
            reasons.append(f"{where} – dokončí jazdu, prípadne počká pred vjazdom; odstavenie na trati ani nová "
                           f"(ad hoc) trasa pre zvyšok jazdy sa neponúkajú.")

    # čas pripravenosti (preberanie na hranici: plán počíta s dobou preberania P90 – pravidlo 10.2.8)
    ho = entry_handover(session, train.id)
    bc = ho.border if ho else train.entry_border
    paths = allocated_paths(session, train, border_time(ho), now)
    from .stav import STANDING

    # vlak ešte v zahraničí (vstupné preberanie očakávané, príchod nezaznamenaný; alebo „na ceste k hranici“ bez
    # preberania) – nie vlak, ktorý podľa svojho stavu už stojí na koľajach ŽSR (prevzatý, odstavený: nedopísané
    # preberanie je len evidencia, limit 18 h mu beží – ako v stav._since_applies). Avízo v budúcnosti je plán
    # príchodu; uplynuté avízo NIE JE príchod – vlak mešká alebo príchod nie je zapísaný (ready_unknown: nikdy
    # ZELENÁ). Čas pripravenosti zapísaný až po avíze (hlásenie rušňovodiča „pripravený“, dispečer) znamená, že
    # vlak je zrejme na hranici a nezapísaný je len príchod – počíta sa s ním; inak vlak prinajlepšom príde teraz.
    # Bez avíza platí budúci čas pripravenosti od dispečera.
    ready_unknown = False
    abroad = not running and train.status not in STANDING and (
        (ho is not None and ho.status == "ocakavany" and not ho.arrived_utc) or
        (ho is None and train.status == "na_ceste_k_hranici"))
    planned_ready = ensure_utc(train.ready_utc) if train.ready_utc else None
    avizo = ensure_utc(ho.avizo_eta_utc) if ho is not None and ho.avizo_eta_utc else None
    if running:
        ready = now
    elif (ho and ho.status in ("ocakavany", "prisiel") and (ho.arrived_utc or ho.avizo_eta_utc)) or \
            (abroad and (planned_ready is None or planned_ready <= now)):
        p50 = bc.dwell_p50_min if bc and bc.dwell_p50_min is not None else 60.0
        p90 = bc.dwell_p90_min if bc and bc.dwell_p90_min is not None else max(p50 * 1.5, p50 + 30.0)
        dwell = max(p50, p90)
        # „Preberanie v PPS Čadca“ (pohraničná prechodová stanica), bez priechodu „na hranici“ – nie „na Čadca“
        place = f"v PPS {bc.handover_station}" if bc else "na hranici"
        if not abroad:
            arrival = ensure_utc(ho.arrived_utc or ho.avizo_eta_utc)
            what = "príchod" if ho.arrived_utc else "príchod (čas podľa avíza)"
            reasons.append(f"Preberanie {place}: {what} {fmt(arrival)} + preberanie {dwell:.0f} min "
                           f"(P90; bežne {p50:.0f} min).")
            ready = max(now, plus_minutes(arrival, dwell))
        elif avizo is not None and avizo >= plus_minutes(now, -AVIZO_GRACE_MIN):
            # avízo v budúcnosti, alebo len krátko po ňom (príchod sa zapisuje s oneskorením) – plán príchodu
            note = "" if avizo >= now else " (príchod zatiaľ nie je zaznamenaný)"
            reasons.append(f"Preberanie {place}: avízo príchodu {fmt(avizo)}{note} + preberanie {dwell:.0f} min "
                           f"(P90; bežne {p50:.0f} min).")
            ready = max(now, plus_minutes(avizo, dwell))
        else:
            ready_unknown = True
            late = (f"{bc.handover_station}; " if bc else "") + (
                f"avízo {fmt(avizo)}, meškanie {_min(avizo, now):.0f} min" if avizo is not None else "bez avíza")
            earliest = plus_minutes(now, dwell)  # vlak ešte neprišiel: príchod najskôr teraz + preberanie (P90)
            recorded = planned_ready is not None and (avizo is None or planned_ready >= avizo)
            if recorded:
                # pripravenosť zapísaná po avíze: nie „vlak neprišiel“ – nezapísaný je len príchod (prevzatie); nikdy
                # však skôr, ako by bol pripravený pri príchode podľa avíza
                ready = max(now, planned_ready, plus_minutes(avizo, dwell) if avizo is not None else now)
                reasons.append(f"Príchod na hranicu ({late}) nie je zapísaný; počíta sa s pripravenosťou "
                               f"podľa záznamu vlaku {fmt(planned_ready)} (hlásenie rušňovodiča „pripravený“ alebo "
                               f"dispečer). Ak vlak prišiel, zapíšte príchod a prevzatie v Hraniciach, inak nové "
                               f"avízo alebo čas pripravenosti; dovtedy vlak nemôže byť ZELENÝ.")
            else:
                ready = earliest
                old = f"; zapísaná pripravenosť {fmt(planned_ready)} je staršia ako avízo" if planned_ready else ""
                reasons.append(f"Vlak ešte neprišiel na hranicu ({late}{old}) – príchod nie je známy, počíta "
                               f"sa najskôr s príchodom teraz + preberanie {dwell:.0f} min (P90). Zapíšte príchod "
                               f"alebo nové avízo; dovtedy vlak nemôže byť ZELENÝ.")
            last_path = max((ensure_utc(p.departure_utc) for p in paths), default=None)
            if last_path is not None and last_path < plus_minutes(ready, -params.path_tolerance_min):
                reasons.append(f"Pridelenú trasu ({fmt(last_path)}) vlak{'' if recorded else ' pri neskoršom príchode'}"
                               f" nestihne – počíta sa s novou trasou.")
            elif last_path is not None and last_path < plus_minutes(earliest, -params.path_tolerance_min):
                reasons.append(f"Ak vlak ešte neprišiel, pridelenú trasu ({fmt(last_path)}) nestihne (príchod "
                               f"najskôr teraz + preberanie {dwell:.0f} min) – príchod alebo nové avízo zapíšte "
                               f"čo najskôr.")
    else:
        ready = max(now, planned_ready or now)
    # od kedy vlak už stojí na koľajach ŽSR (limit 18 h – PPŽS): začiatok státia, príchod na hranicu, zmeškaná
    # trasa tejto jazdy (pravidlá a náhradné kotvy starších záznamov vo vlaky.planovanie.stav)
    hold_since: list = hold_anchors(train, ho, paths, now)

    order = session.get(Order, train.order_id) if train.order_id else None
    deadline = train.deadline_utc or (order.deadline_utc if order else None)
    deadline_min = _min(now, deadline) if deadline else _min(now, ready) + NO_DEADLINE_MIN
    if not deadline:
        reasons.append("Vlak nemá zadaný termín zákazníka – posudzujú sa len výluky a pracovný čas.")

    # limit 18 h (PIS ruší neodídený vlak, odstavenie najviac 18 h) sa ráta od začiatku státia, nie od teraz
    horizon_min = float(cfg.horizon_hours * 60)
    hold_exceeded = False  # limit uplynie skôr, ako je vlak pripravený – žiadny odchod nie je prípustný
    if hold_since:
        since = min(hold_since)
        left = params.max_hold_h * 60.0 - _min(since, ready)
        horizon_min = max(0.0, min(horizon_min, left))
        hold_exceeded = left <= 0
        limit_at = plus_minutes(since, params.max_hold_h * 60.0)
        if left <= 0 and limit_at <= now:
            reasons.append(f"Vlak stojí od {fmt(since)} dlhšie ako {hours(params.max_hold_h)} h – podľa PPŽS ho ŽSR "
                           f"(PIS) zruší a odstavenie je najviac {hours(params.max_hold_h)} h. Neskorší odchod nie je "
                           f"prípustný – dohodnúť so ŽSR novú trasu.")
        elif left <= 0:
            # limit ešte neuplynul, ale uplynie skôr, ako bude vlak pripravený (nie „už stojí dlhšie ako 18 h“)
            reasons.append(f"Vlak stojí od {fmt(since)} – limit {hours(params.max_hold_h)} h státia (PPŽS) uplynie "
                           f"{fmt(limit_at)}, skôr ako bude vlak pripravený ({fmt(ready)}); potom ho ŽSR (PIS) "
                           f"zruší a odstavenie je najviac {hours(params.max_hold_h)} h. Neskorší odchod nie je "
                           f"prípustný – pripraviť vlak skôr alebo dohodnúť so ŽSR novú trasu.")
        elif left < params.max_hold_h * 60.0:
            reasons.append(f"Vlak stojí od {fmt(since)} – do limitu {hours(params.max_hold_h)} h (PPŽS) zostáva "
                           f"{dec(left / 60)} h; neskorší odchod sa neponúka.")

    # výluky v celom prehľadávanom období (od pripravenosti, nie od teraz – vlaky a objednávky na neskôr)
    windows = closure_windows(session, min(now, ready) - timedelta(hours=2),
                              ready + timedelta(minutes=horizon_min) + timedelta(hours=30))
    run_scale, scale_note = path_run_scale(paths, primary)
    if scale_note:
        reasons.append(scale_note)
    segs, wps, affecting = build_segments(net, primary, windows, now, spec, session, start_km, run_scale)
    routes = [DRoute(f"{train.origin} – {train.destination}", tuple(segs), electric=spec.traction == "E")]
    for w in {(w.closure_id, w.kind, w.note): w for w, _a, _b in affecting if w.note}.values():
        reasons.append(f"{_kind_label(w.kind)} {w.from_station} – {w.to_station}: {w.note}.")

    # obchádzky okolo blokujúcich výluk na hlavnej trase. Výluka priradená poistkou podľa dopravní (jej koľaje
    # v modeli siete hlavnú trasu nepretínajú) zakáže aj dotknutý úsek hlavnej trasy – inak by „obchádzka“
    # bola tá istá trasa s tou istou výlukou.
    from ..siet.graf import norm_line_ref

    blocked: set = set()
    route_set = set(primary.edges)
    line_refs = _closure_line_refs(session, [w for w, _a, _b in affecting])
    for w, a_name, b_name in affecting:
        if w.kind in BLOCKING and not (w.electric_only and spec.traction != "E"):
            sec = set(w.edges) if w.edges is not None else \
                net.closure_edges(w.from_station, w.to_station, norm_line_ref(line_refs.get(w.closure_id, "")))
            blocked |= sec
            if not (sec & route_set):
                blocked |= affected_route_edges(net, primary, wps, a_name, b_name)
    alt_groutes = []
    affecting_of = {routes[0].name: affecting}  # výluky na trase (z build_segments) podľa názvu trasy
    if blocked and not running:
        ra, rb = net.resolve(train.origin, spec), net.resolve(train.destination, spec)
        if ra and rb:
            alt = net.shortest(ra[1], rb[1], spec, banned=blocked)
            if alt and alt.minutes <= primary.minutes * 2.5:
                alt_groutes.append(alt)
                via = [s.name for s, _, _ in alt.stations if s.type == "station"]
                mid = via[len(via) // 2] if via else "?"
                asegs, _, aaff = build_segments(net, alt, windows, now, spec, session)
                routes.append(DRoute(f"obchádzka cez {mid}", tuple(asegs), electric=spec.traction == "E",
                                     is_primary=False))
                affecting_of[routes[-1].name] = aaff
    groute_of = {routes[0].name: primary, **{d.name: g for d, g in zip(routes[1:], alt_groutes)}}

    # poistky ZELENEJ pri každej trase (hlavnej aj obchádzke – odporučená obchádzka nesmie byť ZELENÁ cez výluku, ktorú
    # nemožno umiestniť na sieť, cez čakajúcu udalosť z e-mailu ani cez neoverenú výluku z e-mailu):
    #  - výluka mimo siete pri trase,
    #  - čakajúca udalosť z e-mailu, s ktorou plánovač nevie počítať (posta.spracovanie.pending_uncertainty),
    #  - výluka z e-mailu, ktorú dispečer ešte neoveril, v období možnej jazdy vlaku
    period_lo = min(now, ready) - timedelta(hours=2)
    period_hi_of = {name: ready + timedelta(minutes=horizon_min + g.minutes * 2 + 120) for name, g in groute_of.items()}
    unmapped_of = {name: _unmapped_windows(net, windows, g, ready, horizon_min) for name, g in groute_of.items()}
    unverified_of = {name: _unverified_email(affecting_of.get(name, []), period_lo, period_hi_of[name])
                     for name in groute_of}
    from ..posta.spracovanie import pending_uncertainty_routes

    path_notes, pending_of = pending_uncertainty_routes(
        session, train, groute_of, paths,
        window=(period_lo, ready + timedelta(minutes=horizon_min) + timedelta(hours=30)))

    # pridelené trasy (idúci vlak je na svojej trase; odstavenie ruší trasu → nová trasa s lehotou)
    if running:
        allocated = []
    elif paths:
        allocated = [_min(now, p.departure_utc) for p in paths]
    else:
        allocated = [NO_PATH_SENTINEL]
    no_path_at = len(reasons)  # sem patrí dôvod „vlak nemá pridelenú trasu“ (text závisí od prijatého odporúčania)

    # rušňovodič (zmena v službe, inak naplánovaná zmena, ktorá je na rade k pripravenosti vlaku)
    shift = active_shift(session, train.id, max(now, ready))
    crew_start = None
    if shift and ensure_utc(shift.start_utc) <= max(now, ready):
        crew_start = _min(now, shift.start_utc)
    crew_unknown = running and crew_start is None
    if crew_unknown:
        reasons.append(CREW_UNKNOWN_REASON)
    elif _mobile_driver(shift):
        reasons.append(_mobile_reason(shift))

    ready_min = _min(now, ready)
    # doterajšie platné odporúčanie: stabilita voľby POČKAŤ/ODSTAVIŤ; rozhodnutie dispečera (posledné prijaté
    # odporúčanie, aj keď po ňom vzniklo iné): už zaplatené odstavenie, objednaná nová trasa
    prev = None if running else _previous_choice(session, train, now)
    accepted = None if running else _accepted_choice(session, train, now)
    requested = _requested_paths(accepted, params, paths)
    near = near_km is not None
    # tesne pred cieľom: bez odstavenia (a novej trasy); rušňovodič sa posudzuje zvlášť – vystriedanie je až v cieli,
    # nie dvojhodinové zdržanie jazdy posledného kilometra (simulácia by inak každú variantu označila ČERVENOU)
    rec = two_stage_recommend(routes, ready_min, deadline_min, params, allocated,
                              NEAR_CREW_START if near else crew_start,
                              horizon_min=horizon_min, stable_needs_new_path=running, anchor=now, prev=prev,
                              already_stabled=_already_stabled(train, accepted), requested=requested,
                              allow_stabling=not near)

    decision = rec.decision
    chosen: Optional[Evaluation] = rec.chosen
    color = chosen.status if chosen else CERVENA
    if ready_unknown and color == ZELENA:
        color = ORANZOVA  # vlak ešte neprišiel na hranicu a avízo uplynulo (chýba) – pripravenosť nie je istá
    if hold_exceeded:
        decision = ESKALOVAT  # aj odchod hneď po pripravenosti by bol po limite 18 h (PPŽS) – len informácia
    least_bad_near = False  # idúci vlak pred cieľom bez bezpečnej možnosti – najmenej zlá možnosť (ako pri eskalácii)
    until_entry = False     # ... čaká pred vjazdom dlhšie, ako siaha prehľadávané obdobie (čas odchodu nič nehovorí)
    if decision == ESKALOVAT:
        color = CERVENA
        if near and chosen is not None:
            # vlak stojí pred cieľom: aj keď nič nie je bez rizika (napr. termín zákazníka), ostáva dokončiť jazdu
            # alebo počkať pred vjazdom – eskalácia s odstavením a novou trasou pre posledný kilometer nemá zmysel
            waits = chosen.depart > ready_min + 1e-6
            decision = POCKAT if waits else VYPRAVIT
            least_bad_near = True
            # cieľ zatvorený dlhšie, ako siaha prehľadávané obdobie: najmenej zlý odchod je jeho koniec – posúva sa
            # s každým prepočtom a nič nehovorí (vlak čaká pred vjazdom do konca výluky); platí príchod
            last_dep = ready_min + min(horizon_min, params.max_hold_h * 60.0) - cfg.step_minutes - 1e-6
            until_entry = waits and chosen.depart >= last_dep
            what = ("počkať pred cieľom, kým sa neuvoľní vjazd" if until_entry else
                    f"počkať pred cieľom do {fmt(_dt(now, chosen.depart))}" if waits else "pokračovať do cieľa")
            reasons.append(f"Žiadna možnosť nie je bez rizika – najmenej zlá je {what} (príchod "
                           f"{'podľa výpočtu ' if until_entry else ''}{fmt(_dt(now, chosen.arrival_p50))}); "
                           f"informujte zákazníka a podľa potreby dohodnite postup so ŽSR.")
    if running and decision == VYPRAVIT:
        decision = POKRACOVAT
    ordered = _ordered(chosen, requested, params)  # nová trasa objednaná podľa prijatého odporúčania
    if not running and not paths:
        # vlak bez pridelenej trasy: výzva objednať trasu, nie však trasu, ktorú dispečer podľa prijatého
        # odporúčania už objednal (druhá objednávka tej istej ad hoc trasy)
        if ordered:
            no_path = (f"Vlak zatiaľ nemá pridelenú trasu – novú trasu na {fmt(_dt(now, chosen.depart))} dispečer "
                       f"objednal podľa prijatého odporúčania; overte jej pridelenie v IS KIS.")
        else:
            no_path = "Vlak zatiaľ nemá pridelenú trasu – treba ju objednať."
        reasons.insert(no_path_at, no_path)
    if ordered and decision == VYPRAVIT:
        # odchod teraz na trasu, ktorú ŽSR (podľa údajov) ešte nepridelila – nikdy ZELENÁ
        asked = min(requested[chosen.route], key=lambda r: abs(r - chosen.depart))
        reasons.append(f"Trasa podľa prijatého odporúčania ({fmt(_dt(now, asked))}) nie je zapísaná ako pridelená – "
                       f"vypraviť až po overení pridelenia v IS KIS.")
        if color == ZELENA:
            color = ORANZOVA

    # poistky ZELENEJ pri trase, ktorú agent odporúča (aj obchádzke); ostatné trasy obmedzia len svoje varianty
    route_name = chosen.route if chosen is not None and chosen.route in groute_of else routes[0].name
    unmapped = unmapped_of[route_name]
    for w in unmapped:
        reasons.append(f"{_kind_label(w.kind)} {w.from_station} – {w.to_station} {fmt(w.start)} – {fmt(w.end)} "
                       f"sa nepodarilo umiestniť na sieť (neznámy úsek) – overte názvy dopravní vo výluke; "
                       f"kým sa to neopraví, vlak nemôže byť ZELENÝ.")
    # čakajúce udalosti z e-mailov, s ktorými plánovač nevie počítať (zrušenie/zmena pridelenej trasy, výluka bez
    # záznamu pre plánovač) – vlak, ktorého sa môžu týkať, nedostane ZELENÚ a dispečer vidí dôvod. Výnimkou je len
    # zrušenie pridelenej trasy, ktoré nariadilo prijaté odporúčanie („pôvodnú trasu odrieknuť, nová trasa na …“) pri
    # odchode na túto objednanú novú trasu – potvrdenie zrušenia z KIS je očakávané a plán platí, nech ŽSR trasu zruší
    # alebo nie. Zmena pridelenej trasy obmedzí aj plán na novú trasu: zmenená trasa môže vlaku vyhovovať.
    def expected_cancel(n: str) -> bool:
        return ordered and getattr(n, "kind", "") == "trasa_zrusena"

    pending_notes = [n for n in path_notes if not expected_cancel(n)] + pending_of.get(route_name, [])
    for n in path_notes:
        if expected_cancel(n):
            reasons.append(f"Zrušenie pridelenej trasy z e-mailu čaká na potvrdenie (udalosť #{n.event_id}) – pôvodnú "
                           f"trasu odriekol dispečer podľa prijatého odporúčania (nová trasa na "
                           f"{fmt(_dt(now, chosen.depart))}); zrušenie potvrďte vo fronte na potvrdenie.")
    # udalosť bez známeho miesta (môže sa týkať ktoréhokoľvek vlaku): ZELENÁ nie, ale zmena farby len z tohto dôvodu
    # nie je nové odporúčanie pri každom vlaku – dispečer o udalosti dostane jedno upozornenie DÔLEŽITÉ (fronta)
    fleet_notes = [n for n in pending_notes if getattr(n, "fleet", False)]
    # neoverená výluka z e-mailu: ZELENÁ nie (termín mohol byť prečítaný nesprávne); ak podľa prečítaného termínu
    # leží mimo plánu vlaku – každé jej okno začína viac ako 2 h po jeho príchode, alebo skončilo viac ako 2 h pred
    # jeho možným odchodom (pripravenosť; idúci vlak teraz) –, zmena farby len z tohto dôvodu nie je nové
    # odporúčanie: jedna výluka by inak poslala správu o každom vlaku na trati (aj o vlakoch na ďalšie dni). Denná
    # výluka, ktorej dnešné okno skončilo, ale zajtrajšie zasahuje plán vlaku, je v pláne (tvrdá poistka; v dôvode
    # okno, ktoré plán zasahuje). Výluka, ktorá skončila pred min(teraz, pripravenosť) − 2 h, vlak neobmedzí vôbec
    # (_unverified_email).
    plan_end = _dt(now, chosen.arrival_p90) + timedelta(hours=2) if chosen is not None else None
    plan_start = ready - timedelta(hours=2)

    def outside_plan(w: Window) -> bool:
        return (plan_end is not None and w.start > plan_end) or w.end < plan_start

    unverified_all = _unverified_email(affecting_of.get(route_name, []), period_lo, period_hi_of[route_name],
                                       first=False)
    in_plan = {}  # výluka → prvé jej okno, ktoré plán vlaku zasahuje
    for w in unverified_all:
        if not outside_plan(w):
            in_plan.setdefault(w.closure_id, w)
    unverified = [in_plan.get(w.closure_id, w) for w in unverified_of[route_name]]
    unverified_later = [w for w in unverified if w.closure_id not in in_plan]
    unverified_plan = [w for w in unverified if w.closure_id in in_plan]

    # bez čerstvých a použiteľných dát nikdy zelená (10.3); poloha, ktorá len práve zostarla, je mäkká poistka
    stale = (position_unusable and not position_soft) or crew_unknown or bool(unmapped)
    if pending_notes:
        stale = stale or len(fleet_notes) < len(pending_notes)
        reasons.extend(str(n) for n in pending_notes)
    if unverified:
        stale = stale or bool(unverified_plan)
        reasons.extend(_unverified_reason(w, later=w in unverified_later) for w in unverified)
    mb_age, mb_reason = _mailbox_state(session, now)
    if mb_age is not None:
        data_age["mailbox_min"] = round(mb_age, 1)
    if mb_reason:
        stale = True
        reasons.append(mb_reason)
    if stale and color == ZELENA:
        color = ORANZOVA
    affecting_chosen = affecting_of.get(route_name, affecting)
    # obmedzenie (nápravová hmotnosť, dĺžka či hmotnosť vlaku …) na zvolenej trase v čase jazdy vlaku: vlak nezastaví,
    # ale ZELENÁ nie je dovolená – plánovač parametre vlaku voči obmedzeniu neoveruje, overí ich dispečer
    electric = spec.traction == "E"
    restrictions = [] if chosen is None else _route_restrictions(
        affecting_chosen, _dt(now, chosen.depart) - timedelta(minutes=30),
        _dt(now, chosen.arrival_p90) + timedelta(minutes=30), electric)
    reasons.extend(_restriction_reason(session, w) for w in restrictions)
    if restrictions and color == ZELENA:
        color = ORANZOVA
    # nepotvrdená výluka bez poistky (ručne zadaná ako návrh, alebo z e-mailu mimo obdobia možnej jazdy) – len informácia
    # (nie okno, ktoré skončilo viac ako 2 h pred možným odchodom vlaku – plán vlaku sa ho netýka)
    unconfirmed = {w.closure_id for w, _, _ in affecting_chosen if w.status == "navrh" and w.end >= plan_start} - \
        {w.closure_id for w in unverified}
    if unconfirmed:
        n_unc = len(unconfirmed)
        if n_unc == 1:
            what = "1 výluka na trase ešte nie je potvrdená dispečerom – počíta sa s ňou"
        elif n_unc < 5:
            what = f"{n_unc} výluky na trase ešte nie sú potvrdené dispečerom – počíta sa s nimi"
        else:
            what = f"{n_unc} výluk na trase ešte nie je potvrdených dispečerom – počíta sa s nimi"
        reasons.append(f"{what} (bezpečnejší variant); po potvrdení alebo zamietnutí sa vlak prepočíta.")
    seen_w: set = set()
    listed = affecting if route_name == routes[0].name else affecting + affecting_chosen
    for w, _a, _b in listed:
        key = (w.closure_id, w.start, w.kind)  # aj úplná výluka z výluk oboch koľají (_both_tracks_windows)
        if key in seen_w:
            continue
        seen_w.add(key)
        if w.end < plan_start:
            # okno skončilo viac ako 2 h pred možným odchodom vlaku (napr. dnešná výluka a vlak zajtra): plán vlaku sa
            # ho netýka – nie „na trase vlaku“ (neoverenú výluku z e-mailu uvádza poistka vyššie)
            continue
        state = "" if w.status == "overene" else " – NEPOTVRDENÁ"
        reasons.append(f"{_kind_label(w.kind)} {w.from_station} – {w.to_station} "
                       f"{fmt(w.start)} – {fmt(w.end)} na trase vlaku{state}.")
    if chosen:
        reasons.extend(chosen.reasons)

    # rušňovodič už v službe: posúdiť jeho pracovný čas pri pokračovaní hneď (aj keď odporúčanie
    # je odstaviť – dispečer musí vedieť, dokedy ho treba vystriedať)
    current_ref = None
    if (shift is not None and ensure_utc(shift.start_utc) <= now) or crew_unknown:
        current_ref = next((e for e in rec.candidates if e.route == routes[0].name
                            and abs(e.depart - ready_min) < 1e-6 and e.kind == VYPRAVIT), None)
        if near and chosen is not None and chosen.kind == POCKAT:
            current_ref = chosen  # pred cieľom čaká ten istý rušňovodič – jeho služba trvá až do príchodu po čakaní
    if crew_unknown:
        # nástup nie je zadaný: len konzervatívny ODHAD (nikdy nie fakt) – od odchodu vlaku mínus príprava
        crew = _crew_estimate(train, shift, current_ref or chosen, paths, now, params, run_since)
    else:
        crew = _crew_info(shift, current_ref or chosen, now, params, current=current_ref is not None)
    # plánovaný (ešte nezačatý) nástup rušňovodiča zbytočne skoro → navrhnúť posun (krok C.11 príbehu vlaku)
    if shift is not None and chosen is not None and chosen.kind != ODSTAVIT and \
            ensure_utc(shift.start_utc) > now:
        ideal = _dt(now, chosen.depart - params.crew_prep_min)
        idle = (ideal - ensure_utc(shift.start_utc)).total_seconds() / 60.0
        if idle >= 30:
            reasons.append(f"Posunúť nástup rušňovodiča{(' ' + shift.driver.name) if shift.driver else ''} "
                           f"z {fmt(shift.start_utc)} na {fmt(ideal)} – inak by {dec(idle / 60)} h čakal a míňal "
                           f"pracovný čas (limit {hours(params.crew_max_duty_min / 60)} h).")
            crew["suggested_start"] = ideal.isoformat()
        elif crew.get("late_start"):
            # zaznamenaná zmena začína až po odchode: rušňovodič pri odchode nie je v službe – nástup sa nevymýšľa
            reasons.append(f"Rušňovodič{(' ' + shift.driver.name) if shift.driver else ''} nastupuje až o "
                           f"{fmt(shift.start_utc)} (zaznamenaná zmena) – na odchod o {fmt(_dt(now, chosen.depart))} "
                           f"treba posunúť nástup najneskôr na {fmt(ideal)} (príprava "
                           f"{params.crew_prep_min:.0f} min) alebo zvoliť neskorší odchod.")
            crew["suggested_start"] = ideal.isoformat()
    if crew.get("status") == CERVENA:
        if crew.get("unknown_start"):
            reasons.append(f"Odhad: rušňovodič by prekročil {hours(params.crew_max_duty_min / 60)} h – "
                           f"{crew_relief_text(crew)}.")
        elif crew.get("relieve_by"):
            late = relief_overdue(crew)
            reasons.append(f"Rušňovodič by prekročil {hours(params.crew_max_duty_min / 60)} h – " + (
                f"vystriedať ihneď ({late})." if late else
                f"naplánujte výmenu najneskôr o {fmt(datetime.fromisoformat(crew['relieve_by']))}."))

    # semafor 10.3: ZELENÁ len ak má rušňovodič rezervu do limitu; inak treba potvrdenie dispečera
    if crew.get("status") in (ORANZOVA, CERVENA) and color == ZELENA:
        color = ORANZOVA
    # mäkké poistky – vlak by inak bol ZELENÝ: poloha idúceho vlaku práve zostarla (nie mimo trasy, nie dlhšie ako
    # POSITION_HOLD_MIN), čakajúca udalosť bez známeho miesta (jedno upozornenie o udalosti, nie o každom vlaku),
    # neoverená výluka z e-mailu mimo plánu vlaku. Odporúčanie nie je ZELENÉ, ale odtlačok (a priorita) sa počítajú
    # zo ZELENEJ – zmena farby len z tohto dôvodu aktualizuje doterajšie nové odporúčanie bez nového odporúčania
    # a upozornenia (_store); zhoršenie starnúcou polohou, ktoré trvá dlhšie, je nové odporúčanie s upozornením
    fp_color = None
    if (position_soft or fleet_notes or unverified_later) and color == ZELENA:
        color, fp_color = ORANZOVA, ZELENA
    # prečo ZELENÁ nie je dovolená (bez ohľadu na farbu zvolenej varianty) – web aj e-mail podľa tohto zoznamu
    # neukážu ZELENÉ varianty ani okná odchodu ako odporúčanie (jedno pravidlo namiesto hľadania v textoch dôvodov)
    caps = [txt for flag, txt in (
        (ready_unknown, "pripravenosť vlaku nie je istá (avízo uplynulo, vlak ešte neprišiel na hranicu)"),
        (bool(ordered), "trasa podľa prijatého odporúčania nie je zapísaná ako pridelená"),
        (position_unusable, "poloha idúceho vlaku nie je čerstvá alebo je mimo plánovanej trasy"),
        (crew_unknown, "chýba záznam o nástupe rušňovodiča"),
        (crew.get("status") in (ORANZOVA, CERVENA), "rušňovodič nemá rezervu do limitu pracovného času"),
        (bool(unmapped), "výluku sa nepodarilo umiestniť na sieť"),
        (bool(pending_notes), "čakajúca udalosť z e-mailu, s ktorou plánovač nevie počítať"),
        (bool(unverified), "výluka z e-mailu na trase ešte nie je overená dispečerom"),
        (bool(mb_reason), "schránka agenta nebola úplne skontrolovaná v posledných 30 min"),
        (bool(restrictions), RESTRICTION_CAP),
    ) if flag]
    if caps:
        data_age["obmedzenie"] = caps

    # návrh trasy na objednanie: nie pre zvyšok jazdy tesne pred cieľom (jediná dopravňa – nedá sa objednať); pri
    # zadržaní pred blokujúcou výlukou s čakaním na dopravni pred ňou (nie časy vnútri výluky)
    path_proposal = {} if near else _path_proposal(
        net, groute_of.get(route_name, primary), chosen, now, start_km if running else 0.0, rec,
        affecting=affecting_of.get(route_name, affecting), electric=spec.traction == "E", params=params,
        pace=run_scale if route_name == routes[0].name and run_scale != 1.0 else 1 + RUN_MARGIN)
    if path_proposal:
        path_proposal["drafts"] = _drafts(train, decision, chosen, path_proposal, now, deadline)
    # poistky ZELENEJ pri každej trase: jej varianty nie sú ZELENÉ a okná nezvolenej trasy nie sú okná bezpečného
    # odchodu; čakajúce zrušenie či zmena pridelenej trasy obmedzí len varianty na pridelenej trase
    guards_of = {name: _route_guards(unmapped_of[name], pending_of.get(name, []), unverified_of[name],
                                     what="varianta nemôže byť ZELENÁ")
                 for name in groute_of}
    path_guard = [n.rstrip(".") for n in path_notes]
    windows_iso = {name: [[_dt(now, a).isoformat(), _dt(now, b).isoformat()] for a, b in wins]
                   for name, wins in rec.green_windows.items()
                   if name == route_name or not guards_of.get(name)}
    variants = _variants(rec, now)
    for v in variants:
        extra = guards_of.get(v.get("route", ""), []) + ([] if v.get("needs_new_path") else path_guard)
        extra += [_restriction_reason(session, w, what="varianta nemôže byť ZELENÁ").rstrip(".")
                  for w in _variant_restrictions(v, affecting_of.get(v.get("route", ""), []), electric)]
        if extra and v.get("status") == ZELENA:
            v["status"] = ORANZOVA
            v["reasons"] = list(v.get("reasons") or []) + extra
    priority = _priority(session, train, decision, fp_color or color, chosen, crew, running, now)
    summary = _summary(train, decision, chosen, now, crew, rec, horizon_min, has_path=bool(paths), ordered=ordered,
                       near_km=near_km, near_estimated=near_estimated, until_entry=until_entry)
    chosen_d = _ev_dict(chosen, now) if chosen else {}
    if chosen is not None and (decision in (ESKALOVAT, POKRACOVAT) or least_bad_near or (
            chosen.needs_new_path and abs(chosen.depart - params.new_path_lead_min) <= cfg.step_minutes + 1e-6)):
        # odchod „čo najskôr“ (idúci vlak, najskoršia nová trasa po lehote P5, najmenej zlá možnosť pri
        # eskalácii aj pred cieľom) sa s časom posúva sám – posun nie je zmena situácie (_store)
        chosen_d["floating"] = True
    if least_bad_near:
        # vlak pred cieľom bez bezpečnej možnosti: o zmene rozhoduje predpokladaný príchod (vjazd po konci výluky),
        # nie najmenej zlý čas odchodu (_store) – posun o 30 min a viac je nové odporúčanie
        chosen_d["compare_arrival"] = True
        if until_entry:
            chosen_d["until_entry"] = True  # texty bez času odchodu – „po uvoľnení vjazdu“
    if chosen is not None and running:
        chosen_d["running"] = True  # idúci vlak – texty: „pokračovať v jazde“, nie odchod „teraz“
        if decision == POCKAT:
            # „POČKAŤ, pokračovať v jazde o …“: čas pokračovania sa ráta od aktuálnej polohy a s každou novou polohou
            # idúceho vlaku sa posúva, hoci sa situácia nemení (vlak sa blíži k výluke a čaká pred ňou do jej konca)
            # – o zmene rozhoduje predpokladaný príchod, ktorý určuje koniec výluky (posun o 30 min a viac je nové
            # odporúčanie), ako pri POKRAČOVAŤ a pri vlaku pred cieľom (_store). Odstavenie sa takto neporovnáva:
            # najskoršia nová trasa (lehota P5) sa s časom posúva aj s príchodom – plávajúci odchod sa neporovnáva
            # a pevný (napr. po konci výluky) sa porovnáva s odchodom, ktorý dispečer dostal.
            chosen_d["floating"] = True
            chosen_d["compare_arrival"] = True
    # bez polohy a času odchodu (alebo zo starej polohy) sa predpokladaný príchod posúva s časom – pri pokračovaní
    # hneď, nie však pri čakaní pred výlukou (POČKAŤ, pokračovať o …): čas pokračovania aj príchod vtedy určuje koniec
    # výluky (pevný bod mriežky odchodov), nie „teraz“, a príchod sa porovnáva aj pri starej polohe (vlak stojí pred
    # výlukou a hlásenia TIS neposiela) – predĺženie výluky je nové odporúčanie, aj keď dispečer doterajšie POČKAŤ
    # prijal. Výnimkou je čakanie pred zatvoreným cieľom po hranicu výpočtu (until_entry), ktorá sa posúva s časom.
    holds = decision == POCKAT and not until_entry
    if chosen is not None and running and arrival_floating and not holds:
        chosen_d["arrival_floating"] = True
    if chosen is not None and chosen.needs_new_path and not running:
        # pridelené trasy v čase odporúčania: po prijatí je nová pridelená (zmenená) trasa odpoveďou ŽSR na objednávku
        chosen_d["paths"] = sorted(ensure_utc(p.departure_utc).isoformat() for p in paths)
    return _store(session, train, now, trigger, decision=decision, color=color, priority=priority,
                  summary=summary, chosen=chosen_d, variants=variants,
                  windows=windows_iso, path_proposal=path_proposal, crew=crew,
                  reasons=_dedupe(reasons), data_age=data_age, fingerprint_color=fp_color)


def _grid_origin(anchor: datetime, step_min: float) -> float:
    """Minúty od `anchor` k bodu pevnej mriežky odchodov (násobky kroku v absolútnom čase), ≤ 0."""
    return -((ensure_utc(anchor).timestamp() % (step_min * 60.0)) / 60.0)  # v sekundách – presne pre celé sekundy


def two_stage_recommend(routes: list, ready_min: float, deadline_min: float, params: Params,
                        allocated: list, crew_start: Optional[float], horizon_min: Optional[float] = None,
                        stable_needs_new_path: bool = False, anchor: Optional[datetime] = None,
                        prev: Optional[dict] = None, already_stabled: bool = False,
                        requested: Optional[dict] = None, allow_stabling: bool = True) -> DRecommendation:
    """1. hrubý prechod všetkých odchodov s menej vzorkami, 2. spresnenie najlepších kandidátov.

    Výsledok je rovnaký ako pri recommend() s plným počtom vzoriek pre vybrané varianty, ale
    výpočet je niekoľkonásobne rýchlejší (sekcia 13: prepočet do 5 s).
    horizon_min – ako ďaleko od pripravenosti hľadať odchod (napr. skrátené limitom 18 h státia).
    stable_needs_new_path – odstavenie ruší trasu (idúci vlak): ODSTAVIŤ len s novou trasou a lehotou.
    anchor – čas posúdenia (t = 0): odchody sa hľadajú v pevnej mriežke (násobky kroku v absolútnom čase), nie
      od teraz – bez zmeny údajov dá ďalší cyklus agenta ten istý odchod aj semafor.
    prev – doterajšie platné odporúčanie (_previous_choice): voľba POČKAŤ/ODSTAVIŤ pri tom istom odchode a trase
      sa nemení, kým je rozdiel nákladov v rámci robustness_premium (bod zlomu čakať/odstaviť bez zmeny údajov).
    already_stabled – vlak už stojí odstavený: jednorazový náklad odstavenia je zaplatený.
    requested – {trasa: [odchody]} nových trás, ktoré dispečer už objednal v lehote (prijaté odporúčanie).
    allow_stabling – False: bez varianty ODSTAVIŤ (idúci vlak tesne pred cieľom).
    """
    cfg = settings()
    fast = replace(params, samples=min(params.samples, 600))
    rec = recommend(routes, now=0.0, ready=ready_min, deadline=deadline_min, p=fast,
                    horizon_min=cfg.horizon_hours * 60 if horizon_min is None else horizon_min,
                    step_min=cfg.step_minutes, allocated_departures=allocated, crew_start=crew_start,
                    stable_needs_new_path=stable_needs_new_path,
                    grid_origin=_grid_origin(anchor, cfg.step_minutes) if anchor is not None else None,
                    already_stabled=already_stabled, requested_departures=requested,
                    allow_stabling=allow_stabling)
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
                       ev.needs_new_path, None if stabled else crew_start, already_stabled=already_stabled)
        if not route.is_primary:
            new.kind = PRESMEROVAT
        refined.append(new)
    decision, chosen, explanation = select_best(refined, params)
    kept = _keep_previous_kind(prev, decision, chosen, by_name, ready_min, deadline_min, params, crew_start,
                               stable_needs_new_path, already_stabled)
    if kept is not None:
        decision, chosen = kept.kind, kept
        explanation = select_best([kept], params)[2] + [
            f"Ponechaná doterajšia voľba {kept.kind} – rozdiel nákladov oproti druhej možnosti je v rámci "
            f"{params.robustness_premium:.0%}."]
    return DRecommendation(decision, chosen, explanation, rec.green_windows, rec.candidates)


def _keep_previous_kind(prev: Optional[dict], decision: str, chosen: Optional[Evaluation], routes: dict,
                        ready_min: float, deadline_min: float, params: Params, crew_start: Optional[float],
                        stable_needs_new_path: bool, already_stabled: bool) -> Optional[Evaluation]:
    """Stabilita voľby POČKAŤ ↔ ODSTAVIŤ: pri tom istom odchode (± tolerancia trasy) a trase bez zmeny semaforu sa
    doterajšia voľba nemení, kým je jej náklad v rámci robustness_premium od lacnejšej možnosti – inak by sa
    s plynúcim časom (bod zlomu čakať/odstaviť) odporúčanie zmenilo bez zmeny údajov. Vracia ponechanú variantu."""
    if prev is None or chosen is None or stable_needs_new_path or decision not in (POCKAT, ODSTAVIT) or \
            prev.get("decision") not in (POCKAT, ODSTAVIT) or prev["decision"] == decision or \
            prev.get("route") != chosen.route:
        return None
    if abs(prev["depart"] - chosen.depart) > params.path_tolerance_min + 1e-6 or chosen.route not in routes:
        return None
    route = routes[chosen.route]
    stabled = prev["decision"] == ODSTAVIT
    normals = _normals(params.samples, len(route.segments), params.seed)
    alt = evaluate(route, chosen.depart, ready_min, deadline_min, params, normals, stabled, chosen.needs_new_path,
                   None if stabled else crew_start, already_stabled=already_stabled)
    if not route.is_primary or alt.kind != prev["decision"] or alt.status != chosen.status:
        return None
    if alt.expected_cost > chosen.expected_cost * (1 + params.robustness_premium) + 1e-6:
        return None
    return alt


def _previous_choice(session: Session, train: Train, now: datetime) -> Optional[dict]:
    """Posledné odporúčanie vlaku, ak ešte platí (nové, prijaté alebo upravené dispečerom) a má odchod:
    {decision, route, depart (min od teraz), needs_new_path, state}. Inak None."""
    last = session.scalar(select(Recommendation).where(Recommendation.train_id == train.id)
                          .order_by(Recommendation.id.desc()))
    if last is None or last.state not in ("nove", "prijate", "upravene"):
        return None
    return _choice(last, now)


def _accepted_choice(session: Session, train: Train, now: datetime) -> Optional[dict]:
    """Rozhodnutie dispečera: posledné prijaté (aj upravené) odporúčanie vlaku s odchodom, ako _previous_choice.

    Platí, kým dispečer neprijme iné – aj keď po ňom vzniklo ďalšie odporúčanie (napr. ORANŽOVÁ pri výpadku
    schránky): prijaté odporúčanie ostáva prijaté (_store označí za zastarané len nové), inak by sa objednaná
    nová trasa či prijaté odstavenie po prvom takom odporúčaní zabudli. Nie rozhodnutie spred posledného
    skutočného odchodu vlaku (to patrilo predchádzajúcemu státiu)."""
    last = session.scalar(select(Recommendation).where(Recommendation.train_id == train.id,
                                                       Recommendation.state.in_(("prijate", "upravene")))
                          .order_by(Recommendation.id.desc()))
    if last is None:
        return None
    if train.departed_utc and last.created_at and ensure_utc(last.created_at) <= ensure_utc(train.departed_utc):
        return None
    return _choice(last, now)


def _choice(rec: Recommendation, now: datetime) -> Optional[dict]:
    ch = rec.chosen or {}
    if not ch.get("depart") or not ch.get("route"):
        return None
    try:
        depart = _min(now, datetime.fromisoformat(ch["depart"]))
    except (TypeError, ValueError):
        return None
    return {"decision": rec.decision, "route": ch["route"], "depart": depart,
            "needs_new_path": bool(ch.get("needs_new_path")), "state": rec.state, "paths": ch.get("paths")}


def _already_stabled(train: Train, accepted: Optional[dict]) -> bool:
    """Vlak už stojí odstavený (alebo dispečer prijal odporúčanie ODSTAVIŤ – _accepted_choice): náklad odstavenia je
    zaplatený – ďalšie posúdenie ho nesmie prepnúť na „POČKAŤ“ (držať pripravený s rušňovodičom) len preto, že čas
    plynie."""
    return train.status == "odstaveny" or bool(accepted and accepted["state"] in ("prijate", "upravene")
                                                and accepted["decision"] == ODSTAVIT)


def _requested_paths(accepted: Optional[dict], params: Params, paths: Optional[list] = None) -> Optional[dict]:
    """Nová trasa z prijatého odporúčania (_accepted_choice – dispečer ju objednal v lehote P5): jej čas sa ponúka aj
    po tom, čo vojde do lehoty new_path_lead_min – kým neuplynie, prijaté odporúčanie sa len preto nenahradí
    neskoršou trasou. Len kým ŽSR neodpovie: pridelená (alebo zmenená) trasa, ktorú odporúčanie ešte nepoznalo
    (``paths`` – pridelené trasy vlaku teraz), je odpoveď na objednávku – ďalej platia pridelené trasy."""
    if accepted is None or accepted["state"] not in ("prijate", "upravene") or not accepted["needs_new_path"] or \
            accepted["decision"] not in (VYPRAVIT, POCKAT, ODSTAVIT, PRESMEROVAT) or \
            accepted["depart"] < -params.path_tolerance_min:
        return None
    known = accepted.get("paths")
    if known is not None and any(ensure_utc(p.departure_utc).isoformat() not in known for p in paths or ()):
        return None
    return {accepted["route"]: [accepted["depart"]]}


def _ordered(chosen: Optional[Evaluation], requested: Optional[dict], params: Params) -> bool:
    """Zvolený odchod je nová trasa, ktorú dispečer už objednal (prijaté odporúčanie – _requested_paths)."""
    return bool(chosen is not None and chosen.needs_new_path and requested and
                any(abs(chosen.depart - r) <= params.path_tolerance_min + 1e-6
                    for r in requested.get(chosen.route, ())))


def evaluate_all(session: Session, now: Optional[datetime] = None, trigger: str = "") -> list:
    """Prepočet všetkých aktívnych vlakov. Chyby sú v ``session.info["evaluate_errors"]``.

    Jeden chybný vlak nesmie zastaviť ostatné. Pri chybe sa staré odporúčanie nesmie tváriť ako platné:
    vlak dostane odporúčanie ESKALOVAŤ s popisom chyby (dispečer rozhodne bez agenta). Každý vlak sa počíta
    vo vlastnej vnorenej transakcii (SAVEPOINT) – aj chyba databázy (napr. pri zápise) vráti späť len jeho zmeny
    a relácia ostane použiteľná pre ďalšie vlaky.

    Vlaky sa prepočítajú v poradí podľa id: zámky vlakov (_store) tak súbežné prepočty (agent, web) berú v rovnakom
    poradí a nečakajú na seba navzájom.
    """
    out, errors = [], []
    session.info["evaluate_errors"] = errors
    ids = list(session.scalars(select(Train.id).where(Train.status.notin_(DONE_STATES)).order_by(Train.id)))
    if ids:
        prepare_savepoints(session)
    for tid in ids:
        started = _wall_clock()   # záznam o chybe sa riadi rovnakým poradím prepočtov ako odporúčanie (_store)
        try:
            with session.begin_nested():
                rec = evaluate_train(session, tid, now, trigger)
            if rec is not None:
                out.append(rec)
        except Exception as exc:  # jeden chybný vlak nesmie zastaviť ostatné
            errors.append({"train_id": tid, "error": repr(exc)[:300]})
            try:
                with session.begin_nested():
                    audit(session, "agent", "evaluate_error", "train", tid, error=repr(exc))
                    rec = _store_failure(session, tid, ensure_utc(now) if now else now_utc(), trigger, exc,
                                         started=started)
                if rec is not None:
                    out.append(rec)
            except Exception:  # noqa: BLE001 - napr. stratené spojenie s DB; chyba je v errors
                pass
    return out


def _store_failure(session: Session, train_id: int, now: datetime, trigger: str, exc: Exception,
                   started: Optional[datetime] = None):
    """Záznam o chybe výpočtu (ESKALOVAŤ – posúďte vlak bez agenta). ``started`` – skutočný začiatok zlyhaného
    prepočtu: ak medzitým uložil výsledok prepočet, ktorý začal neskôr, záznam o chybe sa neuloží (_store)."""
    train = session.get(Train, train_id)
    if train is None:
        return None
    # vlastný odtlačok (failure=True): chyba výpočtu sa nesmie zlúčiť so skutočnou eskaláciou (ani naopak)
    return _store(session, train, now, trigger, decision=ESKALOVAT, color=CERVENA, priority="DÔLEŽITÉ",
                  summary=f"Vlak {train.number} ({train.origin} → {train.destination}): agent ho nedokázal "
                          f"prepočítať – predchádzajúce odporúčanie neplatí. Posúďte vlak bez agenta.",
                  reasons=[f"Chyba výpočtu: {type(exc).__name__}: {str(exc)[:200]}",
                           "Skontrolujte údaje vlaku a výluk (napr. názvy dopravní, časy) a nahláste chybu "
                           "správcovi."], failure=True, started=started)


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
    ready = max(now, ensure_utc(order.earliest_departure_utc) if order.earliest_departure_utc else now)
    # výluky v období možného odchodu (objednávky bývajú na dni až týždne dopredu)
    windows = closure_windows(session, min(now, ready) - timedelta(hours=2),
                              ready + timedelta(hours=cfg.horizon_hours + 48))
    droutes = []
    affecting_by_route: dict = {}
    for i, g in enumerate(groutes):
        segs, _, aff = build_segments(net, g, windows, now, spec, session)
        via = [s.name for s, _, _ in g.stations if s.type == "station"]
        name = "hlavná trasa" if i == 0 else f"alternatíva cez {via[len(via) // 2] if via else '?'}"
        droutes.append(DRoute(name, tuple(segs), electric=spec.traction == "E", is_primary=i == 0))
        affecting_by_route[name] = aff
    # rovnaké poistky ZELENEJ ako pri vlaku (evaluate_train): výluka, ktorú sa nepodarilo umiestniť na sieť pri
    # trase, a schránka agenta neskontrolovaná v posledných 30 min → varianta nanajvýš ORANŽOVÁ
    horizon_min = float(cfg.horizon_hours * 60)
    unmapped_by_route = {d.name: _unmapped_windows(net, windows, g, ready, horizon_min)
                         for g, d in zip(groutes, droutes)}
    unmapped = list({w.closure_id: w for ws in unmapped_by_route.values() for w in ws}.values())
    # výluka z e-mailu na trase variantu, ktorú dispečer ešte neoveril (ako pri vlaku)
    unverified_by_route = {d.name: _unverified_email(affecting_by_route[d.name], min(now, ready) - timedelta(hours=2),
                                                     ready + timedelta(minutes=horizon_min + g.minutes * 2 + 120))
                           for g, d in zip(groutes, droutes)}
    mailbox_reason = _mailbox_reason(session, now)
    # čakajúce výluky z e-mailov bez záznamu pre plánovač, ktorých úsek leží na trase variantu (ako pri vlaku)
    from types import SimpleNamespace

    from ..posta.spracovanie import pending_uncertainty_routes

    no_train = SimpleNamespace(id=None, number="")
    pending_window = (min(now, ready) - timedelta(hours=2), ready + timedelta(hours=cfg.horizon_hours + 48))
    _path_notes, pending_by_route = pending_uncertainty_routes(
        session, no_train, {d.name: g for g, d in zip(groutes, droutes)}, (), window=pending_window)
    deadline_min = _min(now, order.deadline_utc) if order.deadline_utc else _min(now, ready) + NO_DEADLINE_MIN
    rec = two_stage_recommend(droutes, _min(now, ready), deadline_min, params, [NO_PATH_SENTINEL], None,
                              anchor=now)

    def guards(route: str) -> list:
        out_ = _route_guards(unmapped_by_route.get(route, []), pending_by_route.get(route, []),
                             unverified_by_route.get(route, []), what="varianta nemôže byť ZELENÁ")
        return out_ + ([mailbox_reason.rstrip(".")] if mailbox_reason else [])

    electric = spec.traction == "E"

    def restricted(route: str, v: dict) -> list:
        """Obmedzenie (nápravová hmotnosť, dĺžka, hmotnosť …) na trase varianty v čase jej jazdy (ako pri vlaku)."""
        return [_restriction_reason(session, w, what="varianta nemôže byť ZELENÁ").rstrip(".")
                for w in _variant_restrictions(v, affecting_by_route.get(route, []), electric)]

    explanation = []
    ch = rec.chosen
    if ch is not None and rec.decision != ESKALOVAT:
        explanation.append(f"{rec.decision} po trase „{ch.route}“ s odchodom {fmt(_dt(now, ch.depart))}: "
                           f"P(včas) {ch.p_on_time:.0%}, príchod {fmt(_dt(now, ch.arrival_p50))} "
                           f"(90 % do {fmt(_dt(now, ch.arrival_p90))}), očakávaný náklad {eur(ch.expected_cost)}.")
        explanation.append("Trasu treba objednať v IS KIS (ad hoc trasa P5 najneskôr 6 h pred odchodom).")
        amber = (list(ch.reasons) if ch.status != ZELENA else []) + guards(ch.route) + \
            restricted(ch.route, _ev_dict(ch, now))
        if amber:
            explanation.append("ORANŽOVÁ – vyžaduje potvrdenie dispečera: " + "; ".join(amber))
    else:
        explanation.append("Žiadna bezpečná varianta v horizonte – dohodnúť so zákazníkom iný termín "
                           "alebo so ŽSR iný čas či trasu.")
    for w in unmapped:
        explanation.append(f"POZOR: {_kind_label(w.kind)} {w.from_station} – {w.to_station} {fmt(w.start)} – "
                           f"{fmt(w.end)} sa nepodarilo umiestniť na sieť – overte, či sa prepravy netýka.")
    variants = _variants(rec, now)
    for v in variants:
        extra = guards(v.get("route", "")) + restricted(v.get("route", ""), v)
        if extra and v.get("status") == ZELENA:
            v["status"] = ORANZOVA
            v["reasons"] = list(v.get("reasons") or []) + extra
    out = {"order_id": order.id, "decision": rec.decision, "explanation": explanation,
           "variants": variants, "routes": []}
    for g, d in zip(groutes, droutes):
        out["routes"].append({"name": d.name, "km": round(g.length_m / 1000, 1), "minutes": round(g.minutes),
                              "stations": [s.name for s, _, _ in g.stations if s.type == "station"]})
    if rec.chosen:
        idx = [d.name for d in droutes].index(rec.chosen.route)
        out["path_proposal"] = _path_proposal(net, groutes[idx], rec.chosen, now, 0.0, rec,
                                              affecting=affecting_by_route.get(rec.chosen.route, []),
                                              electric=spec.traction == "E", params=params)
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


def _proposal_blocks(groute: GRoute, start_km: float, affecting, electric: bool, p: Params) -> list:
    """Blokujúce výluky na trase návrhu ako [(min. od, min. do, okno od, okno do)] – úsek v jazdných minútach trasy
    (``groute.stations``) a čas výluky s rezervami pred a po nej (rovnaké pravidlo ako simulácia: vlak nesmie byť
    v úseku v čase [začiatok − rezerva, koniec + rezerva]). ``affecting`` = výluky trasy z build_segments
    [(okno, bod od, bod do)] – úsek medzi bodmi tak, ako ho počítala simulácia (od polohy idúceho vlaku)."""
    wps = _route_waypoints(groute, start_km)
    all_wps = _waypoints(groute)
    spans: dict = {}
    for a, b in zip(wps, wps[1:]):
        spans.setdefault((a.name, b.name), []).append((_interp_minutes(all_wps, a.km), _interp_minutes(all_wps, b.km)))
    out = []
    for w, a_name, b_name in affecting or ():
        if w.kind not in BLOCKING or (w.electric_only and not electric):
            continue
        ws = w.start - timedelta(minutes=p.buffer_before_closure_min)
        we = w.end + timedelta(minutes=p.buffer_after_closure_min)
        out += [(lo, hi, ws, we) for lo, hi in spans.get((a_name, b_name), [])]
    return out


def _proposal_schedule(rows_min: list, m0: float, m_end: float, depart: datetime, arrival: datetime,
                       blocks: list, pace: float) -> Optional[list]:
    """Časy dopravní návrhu trasy [(príchod, odchod)] s čakaním pred blokujúcou výlukou, alebo None (stačí rovnomerné
    rozloženie medzi odchodom a predpokladaným príchodom).

    Rovnomerné rozloženie by pri najmenej zlej možnosti, ktorá ráta so zadržaním pred výlukou (napr. výluka dlhšia
    ako prehľadávané obdobie), uviedlo časy dopravní vnútri zatvoreného úseku počas výluky. Vtedy: jazda jazdnou dobou
    trasy (``pace`` – násobok jazdných minút) s čakaním na dopravni pred úsekom výluky, kým sa okno výluky
    (s rezervami) neskončí; zvyšok jazdy za poslednou výlukou sa natiahne na predpokladaný príchod (nikdy nie
    rýchlejšie ako jazdná doba). Prekrývajúce sa úseky výluk sa posudzujú spolu."""
    total = max(1e-6, m_end - m0)
    lin = (arrival - depart).total_seconds() / 60.0 / total

    def lin_t(m: float) -> datetime:
        return plus_minutes(depart, (m - m0) * lin)

    ahead = sorted((max(lo, m0), hi, ws, we) for lo, hi, ws, we in blocks if hi > m0 + 1e-6)
    if not any(lin_t(lo) < we and lin_t(hi) > ws for lo, hi, ws, we in ahead):
        return None
    zones: list = []  # spojené (prekrývajúce sa) úseky: [od, do, [(okno od, okno do)]]
    for lo, hi, ws, we in ahead:
        if zones and lo <= zones[-1][1] + 1e-6:
            zones[-1][1] = max(zones[-1][1], hi)
            zones[-1][2].append((ws, we))
        else:
            zones.append([lo, hi, [(ws, we)]])
    anchors = [(m0, depart, depart)]  # (minúta trasy, príchod, odchod) – medzi nimi rovnomerná jazda
    t, m = depart, m0
    for lo, hi, wins in zones:
        reach = plus_minutes(t, (lo - m) * pace)
        travel = (hi - lo) * pace
        go, moved = reach, True
        while moved:  # po čakaní znova skontrolovať všetky výluky úseku
            moved = False
            for ws, we in wins:
                if go < we and plus_minutes(go, travel) > ws:
                    go, moved = we, True
        anchors.append((lo, reach, go))
        t, m = plus_minutes(go, travel), hi
        anchors.append((hi, t, t))
    rest = m_end - m
    tail = pace if rest <= 1e-6 else max(pace, (arrival - t).total_seconds() / 60.0 / rest)
    anchors.append((m_end, plus_minutes(t, max(0.0, rest) * tail), None))
    out = []
    for x in rows_min:
        arr = dep = None
        for (ma, _aa, da), (mb, ab, _db) in zip(anchors, anchors[1:]):
            if ma - 1e-6 <= x <= mb + 1e-6:
                f = 0.0 if mb - ma < 1e-9 else min(1.0, max(0.0, (x - ma) / (mb - ma)))
                arr = da + (ab - da) * f
                break
        if arr is None:
            arr = anchors[-1][1]
        dep = arr
        for ma, aa, da in anchors[1:-1]:
            if abs(ma - x) <= 1e-6 and da is not None and da > aa:
                arr, dep = aa, da  # čakanie pred výlukou na tejto dopravni
        out.append((arr, dep))
    return out


def _path_proposal(net: Network, groute: GRoute, chosen: Optional[Evaluation], now: datetime,
                   start_km: float, rec, affecting=(), electric: bool = True, params: Optional[Params] = None,
                   pace: Optional[float] = None) -> dict:
    """Návrh trasy (dopravne a časy) pre zvolenú variantu. Časy sú rovnomerne rozložené medzi odchod a predpokladaný
    príchod; ak by tak vlak išiel úsekom blokujúcej výluky (``affecting`` z build_segments) počas nej, návrh ukáže
    čakanie na dopravni pred výlukou do jej konca (_proposal_schedule) – nikdy nie časy vnútri výluky."""
    if chosen is None:
        return {}
    depart = _dt(now, chosen.depart)
    total = max(1e-6, chosen.arrival_p50 - chosen.depart)
    m0 = _interp_minutes(_waypoints(groute), start_km)
    base_total = groute.stations[-1][2] - m0 if groute.stations else 1
    factor = total / max(1e-6, base_total)
    rows = [(st, km, mins) for st, km, mins in groute.stations
            if km >= start_km - 1e-6 and st.type in ("station", "yard", "border", "junction")]
    times = None
    if rows and groute.stations and affecting:
        times = _proposal_schedule([mins for _st, _km, mins in rows], m0, groute.stations[-1][2], depart,
                                   _dt(now, chosen.arrival_p50),
                                   _proposal_blocks(groute, start_km, affecting, electric, params or Params()),
                                   pace or (1 + RUN_MARGIN))
    stations = []
    for i, (st, km, mins) in enumerate(rows):
        if times is not None:
            arr, dep = times[i]
        else:
            arr = dep = plus_minutes(depart, (mins - m0) * factor)
        item = {"name": st.name, "type": st.type, "km": round(km - start_km, 1),
                "uic": st.uic_ref, "ref": st.railway_ref, "arr": arr.isoformat(), "dep": dep.isoformat()}
        if dep > arr:
            item["hold"] = True  # čakanie pred výlukou (do konca výluky s rezervou)
        stations.append(item)
    if len(stations) < 2:
        # jediná dopravňa (zvyšok jazdy idúceho vlaku pred cieľom) nie je trasa, ktorú by sa dalo objednať
        return {}
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
        f"Hmotnosť vlaku: {number(train.weight_t) if train.weight_t else '[doplniť]'} t   "
        f"Dĺžka vlaku: {number(train.length_m) if train.length_m else '[doplniť]'} m   "
        f"Nebezpečný tovar (RID): {'áno' if train.rid else 'nie'}",
        "Pobyty z dopravných dôvodov: [doplniť – napr. výmena rušňovodiča, skúška bŕzd]",
        f"Požadovaný odchod v okne: {_iso_fmt(win[0])} – {_iso_fmt(win[1])}" if win else
        "Požadovaný odchod: podľa návrhu vyššie",
    ])
    arr50 = _dt(now, chosen.arrival_p50)
    arr90 = _dt(now, chosen.arrival_p90)
    dep = _dt(now, chosen.depart)
    # odchod na vlastnú pridelenú trasu (bez novej trasy) nie je kvôli obmedzeniu na trati
    later = f"pre obmedzenie na trati vyrazí o {fmt(dep)}" if chosen.needs_new_path else \
        f"vyrazí podľa pridelenej trasy o {fmt(dep)}"
    phrase = {VYPRAVIT: f"odchádza o {fmt(dep)}", POKRACOVAT: "je na ceste", POCKAT: later, ODSTAVIT: later,
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
            lines.append(f"Oproti dohodnutému termínu {fmt(deadline)} očakávame meškanie približne {dec(late_h)} h.")
    lines += ["", "S pozdravom", f"dispečing {cfg.carrier_name}"]
    drafts = {"customer": "\n".join(lines)}
    if chosen.needs_new_path:
        # objednávka ad hoc trasy len pri odchode, ktorý novú trasu potrebuje (ako v e-maile) – nie pri odchode na
        # vlastnú pridelenú trasu (web by ju inak ponúkal aj pri ODSTAVIŤ či VYPRAVIŤ na pridelenú trasu)
        drafts["zsr_path_request"] = zsr
    return drafts


CREW_LATE_TOLERANCE_MIN = 15.0  # nástup naplánovaný najviac o toľko neskôr ako odchod mínus príprava sa toleruje


def _crew_info(shift: Optional[DriverShift], chosen: Optional[Evaluation], now: datetime, p: Params,
               current: bool = False) -> dict:
    """Pracovný čas rušňovodiča pri variante ``chosen``.

    Zaznamenaná zmena (aj naplánovaná) má vždy svoj nástup – k menu známeho rušňovodiča sa nikdy nepripíše vymyslený
    nástup. Ak naplánovaná zmena začína až po odchode mínus príprava (rušňovodič pri odchode ešte nie je v službe),
    stav je aspoň ORANŽOVÁ (``late_start`` – posunúť nástup alebo neskorší odchod). Pri odstavení nastúpi nový
    rušňovodič (odchod mínus príprava).
    """
    if chosen is None:
        return {}
    depart = _dt(now, chosen.depart)
    fresh_crew = chosen.kind == ODSTAVIT and not current  # pri odstavení nastúpi nový rušňovodič
    late = False
    if not fresh_crew and shift is not None:
        start = ensure_utc(shift.start_utc)
        driver = shift.driver.name if shift.driver else ""
        late = start > now and start > depart - timedelta(minutes=p.crew_prep_min - CREW_LATE_TOLERANCE_MIN)
    else:
        start = _dt(now, chosen.depart - p.crew_prep_min)
        driver = "nový rušňovodič (po odstavení)" if fresh_crew else ""
    limit = plus_minutes(start, p.crew_max_duty_min)
    end50 = _dt(now, chosen.arrival_p50 + p.crew_post_min)
    end90 = _dt(now, chosen.arrival_p90 + p.crew_post_min)
    if end50 > limit:
        status = CERVENA
    elif end90 > limit - timedelta(minutes=p.crew_buffer_min) or late:
        status = ORANZOVA
    else:
        status = ZELENA
    out = {"driver": driver, "start": start.isoformat(), "limit": limit.isoformat(),
           "end_p50": end50.isoformat(), "end_p90": end90.isoformat(),
           "left_min": round((limit - now).total_seconds() / 60.0, 0), "status": status,
           "relieve_by": (limit - timedelta(minutes=p.crew_post_min)).isoformat(),
           "max_duty_h": round(p.crew_max_duty_min / 60.0, 1)}
    if late:
        out["late_start"] = True
    return out


UNKNOWN_DRIVER = "neznámy (chýba záznam o nástupe)"
MOBILE_DRIVER = "neznámy (nástup z mobilu)"  # zmena založená hlásením „Nástup na zmenu“ bez naplánovanej zmeny
NASTUP_EARLY_MIN = 30.0  # zmena je na rade najviac toľko pred naplánovaným nástupom (hlásenie „Nástup“ z odkazu)


CREW_UNKNOWN_REASON = ("Pri idúcom vlaku chýba záznam o nástupe rušňovodiča – limit 13 h sa nedá overiť. "
                       "Zadajte zmenu rušňovodiča.")


def _mobile_driver(shift: Optional[DriverShift]) -> bool:
    return shift is not None and shift.driver is not None and shift.driver.name == MOBILE_DRIVER


def _mobile_reason(shift: DriverShift) -> str:
    return (f"Rušňovodič nahlásil nástup z mobilu o {fmt(shift.start_utc)} bez naplánovanej zmeny – limit 13 h beží "
            f"od tohto času; doplňte v zmene meno rušňovodiča (Rušňovodiči).")


def due_shift(session: Session, train_id: int, now: datetime) -> Optional[DriverShift]:
    """Naplánovaná zmena vlaku, ktorá je na rade (nástup najneskôr o NASTUP_EARLY_MIN), z viacerých tá s nástupom
    najbližšie k teraz – nie neskoršie vystriedanie. Inak None."""
    return session.scalar(select(DriverShift).where(
        DriverShift.train_id == train_id, DriverShift.status == "planovana",
        DriverShift.start_utc <= plus_minutes(now, NASTUP_EARLY_MIN))
        .order_by(DriverShift.start_utc.desc(), DriverShift.id.desc()))


def link_shift(session: Session, train_id: int, now: datetime) -> Optional[DriverShift]:
    """Zmena pre odkaz rušňovodiča (hlavička aplikácie): prebiehajúca (najnovšia), inak tá, ktorá je na rade
    (``due_shift``), inak najbližšia naplánovaná."""
    running = session.scalar(select(DriverShift).where(DriverShift.train_id == train_id,
                                                       DriverShift.status == "prebieha")
                             .order_by(DriverShift.start_utc.desc(), DriverShift.id.desc()))
    return running or due_shift(session, train_id, now) or session.scalar(
        select(DriverShift).where(DriverShift.train_id == train_id, DriverShift.status == "planovana")
        .order_by(DriverShift.start_utc, DriverShift.id))


def _departure_estimate(train: Train, now: datetime, run_since: Optional[datetime] = None,
                        paths: Optional[list] = None, earliest: bool = True) -> Optional[datetime]:
    """Odchod idúceho vlaku – stály, nikdy nie „teraz“. Skutočný (hlásenie „odchod“) platí vždy.

    ``earliest`` (odhad nástupu rušňovodiča – limit 13 h sa radšej skráti): najskorší možný odchod – plánovaný, ak nie
    je neskorší ako čas, keď vlak prešiel do jazdy (``run_since``, ručná zmena stavu; budúci plánovaný odchod nie je
    odchod, vlak išiel skôr), inak posledná minulá pridelená trasa (``paths``), inak čas prechodu do jazdy.
    ``earliest=False`` (odhad polohy – výluky pred vlakom nesmú zmiznúť): najneskorší možný odchod – čas prechodu do
    jazdy (vlak išiel najneskôr vtedy; oneskorený vlak, ktorý dispečer označil „v jazde“ pri odchode, nie je hneď
    „v cieli“), bez neho minulý plánovaný odchod (vlak zadaný rovno ako idúci). Bez toho None (odchod neznámy).
    """
    if train.departed_utc:
        return ensure_utc(train.departed_utc)
    planned = ensure_utc(train.planned_departure_utc) if train.planned_departure_utc else None
    if not earliest:
        if run_since is not None:
            return run_since
        return planned if planned is not None and planned <= now else None
    bound = run_since or now
    if planned is not None and planned <= bound:
        return planned
    past = [ensure_utc(x.departure_utc) for x in paths or ()
            if x.departure_utc and ensure_utc(x.departure_utc) <= bound]
    if past:
        return max(past)
    return run_since


def nastup_start(session: Session, train: Train, shift: Optional[DriverShift], now: datetime) -> datetime:
    """Začiatok pracovného času pri hlásení „Nástup na zmenu“ z odkazu rušňovodiča (nie vystriedanie bežiacej zmeny).

    Teraz – no ak je vlak už v jazde (nástup nahlásený až dodatočne: rušňovodič viedol vlak od odchodu), najneskôr
    odchod mínus príprava a nie neskôr ako naplánovaný nástup tejto zmeny, ktorý už nastal: hlásenie nesmie posunúť
    limit 13 h (ani upozornenia strážnika) neskôr, než ukazujú údaje. Naplánovaná zmena s nástupom až po odchode je
    vystriedanie (predchádzajúci rušňovodič nie je zaznamenaný) – začína teraz.
    """
    if train.status != "v_jazde":
        return now
    from .stav import running_since

    paths = allocated_paths(session, train, border_time(entry_handover(session, train.id)), now)
    dep = _departure_estimate(train, now, running_since(session, train), paths)
    if dep is None or dep > now:
        return now
    planned = ensure_utc(shift.start_utc) if shift is not None else None
    if planned is not None and planned > dep + timedelta(minutes=CREW_LATE_TOLERANCE_MIN):
        return now
    start = min(now, dep - timedelta(minutes=current_params(session).crew_prep_min))
    if planned is not None and planned <= now:
        start = min(start, planned)
    return start


def _crew_estimate(train: Train, shift: Optional[DriverShift], current: Optional[Evaluation], paths: list,
                   now: datetime, p: Params, run_since: Optional[datetime] = None) -> dict:
    """Idúci vlak bez záznamu o nástupe rušňovodiča: KONZERVATÍVNY ODHAD, nie fakt (``unknown_start``).

    Predpokladá sa nástup ``crew_prep_min`` pred najskorším možným odchodom vlaku – skutočným, ak je známy (hlásenie
    „odchod“), inak plánovaným (plánovaný odchod, pridelená trasa), ak nie je neskorší ako čas, keď vlak prešiel do
    jazdy, inak týmto časom (``_departure_estimate``) – stály odhad, ktorý sa s časom neposúva. Bez času odchodu sa
    časy neuvádzajú (None). Stav je aspoň ORANŽOVÁ (limit 13 h sa nedá overiť), ČERVENÁ ak by ho aj odhad prekročil.
    """
    name = shift.driver.name if shift is not None and shift.driver else ""
    crew = {"driver": f"{name} (chýba záznam o nástupe)" if name else UNKNOWN_DRIVER, "unknown_start": True,
            "estimated": True, "status": ORANZOVA, "max_duty_h": round(p.crew_max_duty_min / 60.0, 1),
            "start": None, "limit": None, "relieve_by": None, "left_min": None, "end_p50": None, "end_p90": None}
    dep = _departure_estimate(train, now, run_since, paths)
    if dep is None:
        return crew
    start = min(dep, now) - timedelta(minutes=p.crew_prep_min)
    limit = plus_minutes(start, p.crew_max_duty_min)
    relieve_by = limit - timedelta(minutes=p.crew_post_min)
    crew.update(start=start.isoformat(), limit=limit.isoformat(), relieve_by=relieve_by.isoformat(),
                left_min=round((limit - now).total_seconds() / 60.0, 0))
    over = relieve_by <= now
    if current is not None:
        end50 = _dt(now, current.arrival_p50 + p.crew_post_min)
        crew.update(end_p50=end50.isoformat(), end_p90=_dt(now, current.arrival_p90 + p.crew_post_min).isoformat())
        over = over or end50 > limit
    if over:
        crew["status"] = CERVENA
    return crew


def _destination_blocked(session: Session, net: Network, train: Train, groute: GRoute, now: datetime,
                         p: Params) -> bool:
    """Platí teraz (s rezervami) blokujúca výluka či mimoriadnosť v cieľovej dopravni vlaku alebo na úseku jeho trasy,
    ktorý do nej vedie? Výluka trate za cieľom (jej druhá dopravňa nie je na trase vlaku) vlak v cieli nezdrží."""
    dest = _station_keys(net, train.destination)
    on_route: set = set()
    for st, _km, _mins in groute.stations:
        on_route |= _name_keys(st.name)
    for w in closure_windows(session, now - timedelta(minutes=p.buffer_after_closure_min),
                             now + timedelta(minutes=p.buffer_before_closure_min)):
        if w.kind not in BLOCKING or (w.electric_only and train.traction != "E"):
            continue
        a, b = _station_keys(net, w.from_station), _station_keys(net, w.to_station)
        if (dest & a and (dest & b or b & on_route)) or (dest & b and a & on_route):
            return True
    return False


OFFICIAL_SOURCES = ("zsr", "tis", "taf")  # udalosti jazdy ŽSR/TIS: poloha je bod dopravne (nie GPS)
ARRIVAL_SCAN = 2000  # najviac toľko posledných polôh sa prezrie pre čas príchodu (staršie: príchod skôr – konzervatívne)


def _arrival_time(session: Session, net: Network, train: Train, groute: GRoute, pos: Optional[PositionReport],
                  prog: Optional[dict], end_km: float, now: datetime, p: Params) -> Optional[datetime]:
    """Čas príchodu idúceho vlaku do cieľovej dopravne podľa polohy (aj poslednej známej), inak None.

    V cieli je vlak, ktorý je na konci trasy (posledných 50 m – pred ním už nie je žiadny úsek), ktorého príchod do
    cieľovej dopravne hlási ŽSR/TIS (udalosť jazdy – dopravňa je istá, hoci jej bod leží mimo konca trasy), alebo
    ktorého poloha je v obvode cieľovej dopravne (do polomeru obvodu od jej bodu; trasa končí na okraji obvodu, takže
    vlak čakajúci pred vjazdom, napr. 900 m pred koncom trasy, v cieli nie je – rušňovodič je ešte v službe). V obvode,
    no nie na konci trasy ani podľa hlásenia ŽSR/TIS, sa príchod neuzná, kým v cieľovej dopravni alebo na úseku trasy,
    ktorý do nej vedie, platí blokujúca výluka (vlak môže stáť pred stanicou – počíta sa s výlukou).
    Príchod = prvá z nepretržitého radu posledných polôh v cieli (rovnaké pravidlo) – ďalšie hlásenia z cieľa ho
    neposúvajú (ani koniec pracovného času rušňovodiča).
    """
    if pos is None or not groute.nodes:
        return None
    from ..siet.graf import STATION_AREA_M, haversine

    dest = net.find_station(train.destination)
    radius = net.station_radius(dest) if dest is not None else STATION_AREA_M
    end_lat, end_lon = net.nodes[groute.nodes[-1]]
    centers = [] if dest is None else [(dest.lat, dest.lon)] + (
        [net.nodes[dest.node]] if dest.node in net.nodes else [])
    dest_keys = _station_keys(net, train.destination)
    known: dict = {}

    def sure(lat: float, lon: float, source: Optional[str], station: Optional[str]) -> bool:
        """Na konci trasy, alebo hlásenie ŽSR/TIS z cieľovej dopravne."""
        if haversine(lat, lon, end_lat, end_lon) <= 50.0:
            return True
        if (source or "") not in OFFICIAL_SOURCES or not station:
            return False
        if station not in known:
            known[station] = bool(_station_keys(net, station) & dest_keys)
        return known[station]

    def area(lat: float, lon: float) -> bool:
        return any(haversine(lat, lon, la, lo) <= radius for la, lo in centers)

    at_end = prog is not None and prog["done_m"] / 1000.0 >= min(end_km, groute.length_m / 1000.0) - 0.05
    strict = None  # platí v cieli blokujúca výluka? (zisťuje sa len raz a len keď treba)
    if not (at_end or sure(pos.lat, pos.lon, pos.source, pos.nearest_station)):
        if not area(pos.lat, pos.lon):
            return None
        strict = _destination_blocked(session, net, train, groute, now, p)
        if strict:
            return None
    arrival = ensure_utc(pos.ts_utc)
    rows = session.execute(select(PositionReport.id, PositionReport.ts_utc, PositionReport.lat, PositionReport.lon,
                                  PositionReport.source, PositionReport.nearest_station)
                           .where(PositionReport.train_id == train.id, PositionReport.ts_utc <= pos.ts_utc)
                           .order_by(PositionReport.ts_utc.desc(), PositionReport.id.desc()).limit(ARRIVAL_SCAN))
    for pid, ts, lat, lon, source, station in rows:
        if pid != pos.id and not sure(lat, lon, source, station):
            if not area(lat, lon):
                break
            if strict is None:
                strict = _destination_blocked(session, net, train, groute, now, p)
            if strict:
                break  # pri výluke v cieli sa státie v obvode pred koncom trasy za príchod nepočíta
        arrival = ensure_utc(ts)
    return min(arrival, now)


def _run_walk(session: Session, net: Network, groute: GRoute, spec: TrainSpec, ref_km: float, ref_ts: datetime,
              now: datetime) -> tuple:
    """Kam vlak od (``ref_km``, ``ref_ts``) podľa plánu došiel do ``now``: jazda podľa jazdnej doby (s prirážkou
    RUN_MARGIN); pred blokujúcou výlukou, ktorá platila, keď by k nej vlak prišiel, čaká do jej konca (cez platnú výluku
    prejsť nemohol). Vracia (km teraz, čas príchodu do cieľa podľa plánu – None, ak tam do ``now`` nedôjde)."""
    wps = _waypoints(groute)
    if not wps:
        return ref_km, None
    end_km = wps[-1].km
    scale = 1 + RUN_MARGIN
    base = _interp_minutes(wps, ref_km)

    def minutes_to(km: float) -> float:
        return (_interp_minutes(wps, km) - base) * scale

    holds: list = []
    if now > ref_ts:
        windows = [w for w in closure_windows(session, ref_ts, now)
                   if w.kind in BLOCKING and not (w.electric_only and spec.traction != "E")]
        if windows:
            _segs, seg_wps, affecting = build_segments(net, groute, windows, ref_ts, spec, session, ref_km)
            km_of: dict = {}
            for wp in seg_wps:
                km_of.setdefault(wp.name, wp.km)
            holds = sorted((max(ref_km, km_of.get(a, ref_km)), w.start, w.end) for w, a, _b in affecting)
    wait = 0.0
    for hkm, ws, we in holds:
        reach = plus_minutes(ref_ts, minutes_to(hkm) + wait)
        if reach >= now:
            break
        if ws <= reach < we:
            if we >= now:
                return hkm, None  # stojí pred výlukou
            wait += _min(reach, we)
    total = minutes_to(end_km)
    elapsed = _min(ref_ts, now) - wait
    if elapsed >= total:
        return end_km, plus_minutes(ref_ts, total + wait)
    km = _km_at_minutes(groute, base + max(0.0, elapsed) / scale)
    # nie celkom na konci (build_segments potrebuje úsek pred vlakom) – príchod rieši až čas príchodu
    return min(max(ref_km, km), max(0.0, end_km - 0.06)), None


def _arrived(session: Session, train: Train, now: datetime, trigger: str, p: Params, arrival: Optional[datetime],
             run_since: Optional[datetime], reasons: list, data_age: dict,
             presumed: Optional[tuple] = None) -> Recommendation:
    """Idúci vlak v cieli (``arrival`` – čas príchodu podľa polohy) alebo podľa plánu už v cieli, no bez aktuálnej
    polohy (``arrival`` None, ``presumed`` = (príchod podľa plánu, z čoho sa odhaduje – odchod, posledná poloha)).

    POKRAČOVAŤ s informáciou „ukončite jazdu“, resp. „overte polohu“ – trasa sa neprepočítava od východiska: žiadne
    výluky za vlakom, žiadny návrh novej trasy, žiadny limit 13 h z opakovanej jazdy. Rušňovodič sa posudzuje ku
    koncu jazdy (príchod + ukončenie; bez polohy ako keby prišiel teraz). Bez polohy, so starou kontrolou schránky
    alebo pri neznámom nástupe rušňovodiča nie je ZELENÁ.
    """
    due, basis = presumed if presumed else (None, "")
    arr = now if arrival is None else min(arrival, now)
    order = session.get(Order, train.order_id) if train.order_id else None
    deadline = train.deadline_utc or (order.deadline_utc if order else None)
    on_time = deadline is None or (due or arr) <= ensure_utc(deadline)
    ev = Evaluation(route=f"{train.origin} – {train.destination}", depart=0.0, kind=VYPRAVIT,
                    p_on_time=1.0 if on_time else 0.0, p_hold=0.0, p_stuck=0.0, p_crew_relief=0.0,
                    arrival_p50=_min(now, arr), arrival_p90=_min(now, arr), duty_p90=0.0, expected_cost=0.0)
    stale = arrival is None
    shift = active_shift(session, train.id, now)
    if shift is not None and ensure_utc(shift.start_utc) <= now:
        crew = _crew_info(shift, ev, now, p, current=True)
        if _mobile_driver(shift):
            reasons.append(_mobile_reason(shift))
    else:
        stale = True
        reasons.append(CREW_UNKNOWN_REASON)
        paths = allocated_paths(session, train, border_time(entry_handover(session, train.id)), now)
        crew = _crew_estimate(train, shift, ev, paths, now, p, run_since)
    if arrival is None:
        reasons.append(f"Podľa {basis} a jazdnej doby mal byť vlak v cieli o {fmt(due)} – overte polohu vlaku "
                       f"(rušňovodič, ŽSR) a po príchode ukončite jazdu.")
        head = (f"podľa plánu by už mal byť v cieli ({train.destination}, príchod podľa plánu {fmt(due)}), "
                f"aktuálna poloha nie je známa – overte ju a po príchode ukončite jazdu")
    else:
        age = data_age.get("position_min")
        stale_fix = age is not None and age > settings().position_stale_minutes
        old = f" (posledná poloha pred {age:.0f} min)" if stale_fix else ""
        reasons.append(f"Vlak je v cieľovej dopravni {train.destination} od {fmt(arr)}{old} – trasa sa už "
                       f"neprepočítava; ukončite jazdu vlaku (stav „ukončený“).")
        head = f"vlak je v cieli ({train.destination}, príchod {fmt(arr)}) – ukončite jazdu"
    mb_age, mb_reason = _mailbox_state(session, now)
    if mb_age is not None:
        data_age["mailbox_min"] = round(mb_age, 1)
    if mb_reason:
        stale = True
        reasons.append(mb_reason)
    if crew.get("status") == CERVENA:
        if crew.get("unknown_start"):
            reasons.append(f"Odhad: rušňovodič by prekročil {hours(p.crew_max_duty_min / 60)} h – "
                           f"{crew_relief_text(crew)}.")
        elif crew.get("limit"):
            reasons.append(f"Rušňovodič by prekročil {hours(p.crew_max_duty_min / 60)} h – ukončenie jazdy v cieli "
                           f"(príchod + {p.crew_post_min:.0f} min) je po limite "
                           f"{fmt(datetime.fromisoformat(crew['limit']))}.")
    color = ORANZOVA if stale or crew.get("status") in (ORANZOVA, CERVENA) else ZELENA
    ev.status = color
    summary = f"Vlak {train.number} ({train.origin} → {train.destination}): {POKRACOVAT} – {head}."
    if crew.get("status") in (ORANZOVA, CERVENA) and (crew.get("relieve_by") or crew.get("unknown_start")):
        summary += f" Rušňovodič: {crew_relief_text(crew)}."
    chosen = _ev_dict(ev, now)
    chosen["floating"] = True
    # vlak v cieli: texty správ neuvádzajú „odchod/príchod teraz“, ale príchod do cieľa (alebo príchod podľa plánu)
    chosen["v_cieli"] = True
    chosen["prichod_v_cieli"] = arr.isoformat() if arrival is not None else None
    chosen["prichod_podla_planu"] = due.isoformat() if due is not None else None
    if arrival is None:
        chosen["arrival_floating"] = True  # skutočný príchod nie je známy – „teraz“ sa posúva s časom
    priority = _priority(session, train, POKRACOVAT, color, None, crew, True, now)
    return _store(session, train, now, trigger, decision=POKRACOVAT, color=color, priority=priority,
                  summary=summary, chosen=chosen, variants=[], windows={}, path_proposal={}, crew=crew,
                  reasons=_dedupe(reasons), data_age=data_age)


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


def _summary(train: Train, decision: str, chosen: Optional[Evaluation], now: datetime, crew: dict, rec,
             horizon_min: Optional[float] = None, has_path: Optional[bool] = None, ordered: bool = False,
             near_km: Optional[float] = None, near_estimated: bool = False, until_entry: bool = False) -> str:
    """Súhrn odporúčania (prvý riadok e-mailu, súhrn, odovzdávka služby). has_path – vlak má pridelenú trasu;
    ordered – novú trasu už dispečer objednal (prijaté odporúčanie), nie „treba objednať“ ani „objednať trasu“;
    near_km – idúci vlak tesne pred cieľom (zostávajúce km): dokončiť jazdu, prípadne počkať pred vjazdom
    (near_estimated – len podľa odhadu z plánu, poloha nie je známa; until_entry – čaká, kým sa neuvoľní vjazd,
    dlhšie, ako siaha prehľadávané obdobie: bez času odchodu, ten je len hranicou výpočtu)."""
    rel = f"{train.origin} → {train.destination}"
    if chosen is None or decision == ESKALOVAT:
        full = settings().horizon_hours * 60
        if horizon_min is not None and horizon_min <= 0:
            span = "do limitu 18 h státia (na odchod už nezostáva čas)"
        elif horizon_min is not None and horizon_min < full:
            span = f"do limitu 18 h státia (zostáva {dec(horizon_min / 60)} h)"
        else:
            span = f"v najbližších {settings().horizon_hours} h"
        txt = (f"Vlak {train.number} ({rel}): ESKALOVAŤ – žiadna bezpečná varianta {span}. Kontaktovať ŽSR "
               f"(iná trasa alebo čas) a zákazníka (nový termín).")
        if chosen is not None:
            if chosen.kind == VYPRAVIT and train.status == "v_jazde":
                what = "pokračovať v jazde"
            else:
                what = f"{chosen.kind} s odchodom {fmt(_dt(now, chosen.depart))}"
            txt += (f" Najmenej zlá možnosť: {what}, príchod {fmt(_dt(now, chosen.arrival_p50))} "
                    f"(90 % do {fmt(_dt(now, chosen.arrival_p90))}).")
        return txt
    dep = fmt(_dt(now, chosen.depart))
    arr50, arr90 = fmt(_dt(now, chosen.arrival_p50)), fmt(_dt(now, chosen.arrival_p90))
    # ODSTAVIŤ: pôvodnú trasu odrieknuť len vtedy, keď odchod naozaj potrebuje novú trasu – odchod na vlastnú
    # pridelenú trasu (v tolerancii) sa neodrieka (poplatok, novú trasu by ŽSR v lehote P5 aj tak nepridelila)
    stabled = "ODSTAVIŤ – vlak ostáva odstavený (najviac 18 h)" if train.status == "odstaveny" else \
        "ODSTAVIŤ (koľaj určí ŽSR, najviac 18 h)"
    running = train.status == "v_jazde"
    if not chosen.needs_new_path:
        stable_head = f"{stabled}, vypraviť na pridelenú trasu o {dep}"
    elif ordered:
        # trasu dispečer objednal podľa prijatého odporúčania – nie výzva objednať ju znova (druhá objednávka)
        stable_head = (f"{stabled}, " + ("pôvodnú trasu odrieknuť, " if has_path else "") +
                       f"nová trasa na {dep} (objednaná podľa prijatého odporúčania)")
    elif has_path is False and not running:
        stable_head = f"{stabled}, objednať trasu na {dep}"
    else:
        stable_head = f"{stabled}, pôvodnú trasu odrieknuť, nová trasa na {dep}"
    wait_head = f"POČKAŤ, pokračovať v jazde o {dep}" if running else f"POČKAŤ, vypraviť o {dep}"
    go_head = "POKRAČOVAŤ v jazde"
    if near_km is not None:
        about = "podľa odhadu asi " if near_estimated else ""
        wait_head = (f"POČKAŤ pred cieľom ({train.destination}, {about}{dec(near_km)} km pred koncom trasy), dokončiť "
                     + ("jazdu po uvoľnení vjazdu" if until_entry else f"jazdu o {dep}"))
        go_head = (f"POKRAČOVAŤ – vlak je {about}{dec(near_km)} km pred cieľom ({train.destination}), dokončiť jazdu "
                   f"(čakať na vjazd)")
    head = {VYPRAVIT: f"VYPRAVIŤ o {dep}", POKRACOVAT: go_head,
            POCKAT: wait_head, ODSTAVIT: stable_head,
            PRESMEROVAT: f"PRESMEROVAŤ ({chosen.route}), odchod {dep}"}.get(decision, f"{decision} {dep}")
    txt = (f"Vlak {train.number} ({rel}): {head}. P(včas) {chosen.p_on_time:.0%}, príchod {arr50} "
           f"(90 % do {arr90}).")
    if chosen.needs_new_path and ordered:
        # čas v lehote P5 je len pre trasu objednanú podľa prijatého odporúčania – nie výzva objednať ju znova
        txt += " Nová trasa podľa prijatého odporúčania (objednaná v lehote P5) – overte jej pridelenie v IS KIS."
    elif chosen.needs_new_path:
        txt += (" Treba objednať ad hoc trasu v IS KIS (produkt P5 najneskôr 6 h pred odchodom; neskôr len "
                "výnimočne cez dispečera ŽSR).")
    if crew.get("status") in (ORANZOVA, CERVENA) and (crew.get("relieve_by") or crew.get("unknown_start")):
        txt += f" Rušňovodič: {crew_relief_text(crew)}."
    return txt


def _dedupe(items: list) -> list:
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _shift_min(new_iso: Optional[str], base_iso: Optional[str]) -> float:
    if not new_iso or not base_iso:
        return 0.0
    return abs((datetime.fromisoformat(new_iso) - datetime.fromisoformat(base_iso)).total_seconds()) / 60.0


def _baseline_floating(last: Recommendation) -> bool:
    """Bol odchod oznámený dispečerovi plávajúci? (Odporúčania spred zavedenia príznaku sa odvodia.)"""
    ch = last.chosen or {}
    if "baseline_floating" in ch:
        return bool(ch["baseline_floating"])
    if "floating" in ch:
        return bool(ch["floating"])
    if not ch.get("depart"):
        return False
    if last.decision in (ESKALOVAT, POKRACOVAT):
        return True
    if ch.get("needs_new_path") and last.created_at is not None:
        lead = _min(last.created_at, datetime.fromisoformat(ch.get("baseline_depart") or ch["depart"]))
        return abs(lead - Params().new_path_lead_min) <= settings().step_minutes + 1e-6
    return False


_PRIORITY_RANK = {"INFO": 0, "DÔLEŽITÉ": 1, "KRITICKÉ": 2}


def _serialize_train(session: Session, train_id: int) -> None:
    """Výsledky prepočtov toho istého vlaku sa ukladajú po jednom – zámok platí do konca transakcie (commit, rollback).

    PostgreSQL: transakčný poradný zámok podľa id vlaku (pg_advisory_xact_lock). SQLite: zámok zápisu do databázy
    (BEGIN IMMEDIATE, ak relácia transakciu ešte nemá; inak ho transakcia drží od prvého zápisu). Čítanie po získaní
    zámku vidí všetko, čo súbežný prepočet uložil a potvrdil – kým zámok drží, ďalší prepočet toho vlaku čaká."""
    if session.connection().dialect.name == "postgresql":
        session.execute(sql_text("SELECT pg_advisory_xact_lock(CAST(:k AS integer), CAST(:t AS integer))"),
                        {"k": EVAL_LOCK_KEY, "t": int(train_id)})
    else:
        prepare_savepoints(session)


def _eval_started_of(rec: Optional[Recommendation]) -> Optional[datetime]:
    """Skutočný začiatok prepočtu, ktorý odporúčanie uložil alebo naposledy potvrdil (``data_age["eval_started"]``);
    None – odporúčania spred zavedenia údaja (tie poradie prepočtov neobmedzia)."""
    age = rec.data_age if rec is not None and isinstance(rec.data_age, dict) else {}
    try:
        dt = datetime.fromisoformat(str(age["eval_started"]))
    except (KeyError, TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _store(session: Session, train: Train, now: datetime, trigger: str, decision: str, color: str,
           priority: str, summary: str, chosen: Optional[dict] = None, variants: Optional[list] = None,
           windows: Optional[dict] = None, path_proposal: Optional[dict] = None, crew: Optional[dict] = None,
           reasons: Optional[list] = None, data_age: Optional[dict] = None,
           failure: bool = False, fingerprint_color: Optional[str] = None,
           started: Optional[datetime] = None) -> Recommendation:
    """Uloží odporúčanie len ak sa zmenilo (fingerprint); staré nové odporúčania označí za zastarané.

    Poradie súbežných prepočtov vlaku (web „nový vlak“ a prepočet po potvrdení avíza, agent a web): ukladá sa pod
    zámkom vlaku (_serialize_train) a ``started`` – skutočný začiatok tohto prepočtu (evaluate_train ho zapíše do
    ``session.info`` ešte pred čítaním údajov; _store_failure ho dostane priamo) – sa uloží do
    ``data_age["eval_started"]`` nového aj aktualizovaného odporúčania (aj nezmeneného prijatého – potvrdil ho novší
    prepočet). Ak posledné odporúčanie vlaku uložil prepočet, ktorý začal neskôr, tento prepočet vychádzal zo
    starších údajov: neuloží nič, nič neoznačí za zastarané a vráti novšie odporúčanie. Uložený začiatok neskorší
    ako aktuálny čas (s toleranciou EVAL_CLOCK_TOLERANCE) nie je novší prepočet, ale posun hodín servera späť –
    výsledok sa vtedy uloží (inak by odporúčania vlaku zamrzli, kým hodiny nedobehnú uložený čas).

    Odchod sa porovnáva s tým, čo dispečer dostal (baseline), posun o 30 min a viac je nové odporúčanie.
    Plávajúci odchod („čo najskôr“ – idúci vlak, najskoršia nová trasa, eskalácia; ``chosen["floating"]``) sa
    s časom posúva sám: ak je plávajúci baseline aj nový odchod, posun nie je zmena (pri idúcom vlaku sa namiesto
    odchodu porovnáva predpokladaný príchod – nie však príchod idúceho vlaku, ktorý pokračuje hneď zo starej polohy
    alebo bez polohy aj času odchodu ``chosen["arrival_floating"]``, ten sa posúva s časom; prvý skutočný odhad
    príchodu je nový základ porovnania. Pri čakaní pred výlukou určuje príchod jej koniec – porovnáva sa vždy).
    ``failure`` – záznam o chybe výpočtu (_store_failure) má vlastný odtlačok: skutočná eskalácia bez varianty
    (rovnaké rozhodnutie, farba aj prázdny rušňovodič) sa s ním nezlúči a naopak. Vyššia priorita (napr. KRITICKÉ
    oproti doterajšiemu DÔLEŽITÉ) je vždy nové odporúčanie – tiché zlúčenie by nové upozornenie (SMS) nevyvolalo.
    ``fingerprint_color`` – farba pre odtlačok, ak sa farba odporúčania líši len pre mäkkú poistku (poloha idúceho
    vlaku práve zostarla, čakajúca udalosť z e-mailu bez známeho miesta, neoverená výluka z e-mailu mimo plánu
    vlaku): zmena farby len z tohto dôvodu aktualizuje doterajšie nové odporúčanie aj s farbou (nikdy nie ZELENÁ)
    bez nového odporúčania a upozornenia; pri prijatom či zamietnutom odporúčaní (to sa nemení) vznikne nové
    odporúčanie s prioritou podľa odtlačku (zvyčajne INFO – bez e-mailu).
    Vlak pred cieľom bez bezpečnej možnosti (``chosen["compare_arrival"]``) sa pri plávajúcom odchode porovnáva
    podľa predpokladaného príchodu, ako idúci vlak.
    """
    chosen = chosen or {}
    tag = "|chyba" if failure else ""
    full_src = "|".join([decision, fingerprint_color or color, chosen.get("route", ""),
                         (crew or {}).get("status", ""), str(bool(chosen.get("needs_new_path")))]) + tag
    # pri eskalácii je najmenej zlá možnosť len informácia – jej trasa sa môže s časom striedať a to nie je
    # dôvod na nové (KRITICKÉ) upozornenie
    fp_src = "|".join([decision, color, (crew or {}).get("status", "")]) + tag if decision == ESKALOVAT else full_src
    fingerprint = hashlib.sha1(fp_src.encode("utf-8")).hexdigest()
    same = {fingerprint, hashlib.sha1(full_src.encode("utf-8")).hexdigest()}  # aj eskalácia uložená skôr
    started = started or (session.info.get(_EVAL_STARTED) or {}).get(train.id)
    _serialize_train(session, train.id)
    # posledné odporúčanie priamo z databázy (populate_existing): relácia ho mohla načítať ešte pred zámkom (_priority,
    # predchádzajúca voľba) a súbežný prepočet ho medzitým aktualizoval
    last = session.scalar(select(Recommendation).where(Recommendation.train_id == train.id)
                          .order_by(Recommendation.id.desc()).execution_options(populate_existing=True))
    newer = _eval_started_of(last) if started is not None else None
    if newer is not None and newer > started:
        wall = _wall_clock()
        if newer <= wall + EVAL_CLOCK_TOLERANCE:
            _log.info("Prepočet vlaku %s (začal %s) je starší ako prepočet, ktorý uložil odporúčanie #%s (začal %s) – "
                      "jeho výsledok sa neukladá, platí novšie odporúčanie.", train.number, started.isoformat(),
                      last.id, newer.isoformat())
            return last
        # uložený začiatok je neskorší ako aktuálny čas: hodiny servera sa posunuli späť (NTP po obnovení virtuálneho
        # servera, zlé hodiny pri štarte). Súbežný novší prepočet by začal pred „teraz“ – poradie sa nedá určiť a
        # výsledok sa uloží; inak by sa odporúčania vlaku nezmenili (ani pri novej výluke), kým hodiny nedobehnú
        _log.warning("Odporúčanie #%s vlaku %s uložil prepočet so začiatkom %s, neskorším ako aktuálny čas servera %s – "
                     "hodiny servera sa zrejme posunuli späť; výsledok prepočtu sa uloží.", last.id, train.number,
                     newer.isoformat(), wall.isoformat())
    stamp = {"eval_started": started.isoformat()} if started is not None else {}
    if last is not None and last.fingerprint in same and \
            last.state in ("nove", "prijate", "upravene", "zamietnute") and \
            _PRIORITY_RANK.get(priority, 0) <= _PRIORITY_RANK.get(last.priority or "", 0):
        # porovnáva sa s tým, čo dispečer dostal (pôvodný odchod), nie s poslednou tichou aktualizáciou –
        # inak by sa odchod mohol po malých krokoch posunúť o hodiny bez nového upozornenia
        last_ch = last.chosen or {}
        baseline = last_ch.get("baseline_depart") or last_ch.get("depart")
        base_arrival = last_ch.get("baseline_arrival_p50") or last_ch.get("arrival_p50")
        base_arr_floating = bool(last_ch.get("baseline_arrival_floating", last_ch.get("arrival_floating")))
        base_floating = _baseline_floating(last)
        if base_floating and chosen.get("floating"):
            arrival_known = not (chosen.get("arrival_floating") or base_arr_floating)
            by_arrival = decision == POKRACOVAT or bool(chosen.get("compare_arrival"))
            shift_min = _shift_min(chosen.get("arrival_p50"), base_arrival) if by_arrival and arrival_known else 0.0
        else:
            shift_min = _shift_min(chosen.get("depart"), baseline)
        if shift_min < 30:
            if last.state == "nove":
                # nepodstatná zmena: aktualizovať čísla, nevytvárať nové odporúčanie ani notifikáciu
                chosen = dict(chosen)
                if base_arr_floating and not chosen.get("arrival_floating"):
                    # doterajší príchod bol len posúvajúci sa odhad (bez polohy a času odchodu) – prvý skutočný
                    # odhad príchodu je nový základ porovnania
                    base_arrival, base_arr_floating = chosen.get("arrival_p50"), False
                if baseline:
                    chosen["baseline_depart"] = baseline
                if base_arrival:
                    chosen["baseline_arrival_p50"] = base_arrival
                chosen["baseline_arrival_floating"] = base_arr_floating
                chosen["baseline_floating"] = base_floating
                last.fingerprint = fingerprint
                last.color = color  # pri rovnakom odtlačku sa líši len pre mäkkú poistku (stará poloha)
                last.summary, last.chosen, last.variants = summary, chosen, variants or []
                last.windows = windows or {}
                last.path_proposal, last.crew, last.reasons = path_proposal or {}, crew or {}, reasons or []
                last.data_age = {**(data_age or {}), "computed_at": now.isoformat(), **stamp}
                return last
            if last.color == color:
                # prijaté/upravené/zamietnuté odporúčanie sa nemení – je to záznam toho, čo dispečer rozhodol; zapíše
                # sa len začiatok prepočtu, ktorý ho potvrdil (starší súbežný prepočet ho potom nenahradí)
                if stamp:
                    last.data_age = {**(last.data_age or {}), **stamp}
                return last
            # iná farba len pre mäkkú poistku (napr. ZELENÉ prijaté odporúčanie a poloha práve zostarla): záznam
            # dispečera sa nemení, ale aktuálne odporúčanie nesmie ostať ZELENÉ – nové s prioritou podľa odtlačku
    for old in session.scalars(select(Recommendation).where(Recommendation.train_id == train.id,
                                                            Recommendation.state == "nove")):
        old.state = "zastarane"
    rec = Recommendation(train_id=train.id, created_at=now, decision=decision, color=color, priority=priority,
                         summary=summary, chosen=chosen, variants=variants or [], windows=windows or {},
                         path_proposal=path_proposal or {}, crew=crew or {}, reasons=reasons or [],
                         trigger=trigger, data_age={**(data_age or {}), "computed_at": now.isoformat(), **stamp},
                         fingerprint=fingerprint, state="nove")
    session.add(rec)
    session.flush()
    audit(session, "agent", "recommendation", "train", train.id, decision=decision, color=color,
          priority=priority, trigger=trigger, recommendation_id=rec.id)
    return rec
