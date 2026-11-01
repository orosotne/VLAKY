"""Príjem polôh vlakov (GPS brána, aplikácia rušňovodiča) a udalostí jazdy (ŽSR / RNE TIS / TAF TSI).

Každá poloha sa priradí k najbližšej koľaji (map-matching, prednosť majú hrany plánovanej trasy vlaku),
doplní sa najbližšia dopravňa a vlak sa nájde podľa čísla (posledný aktívny). Import je idempotentný:
opakované doručenie tej istej správy (vlak, čas, zdroj, súradnice) nevytvorí nový záznam.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Annotated, Literal, Optional

from pydantic import AfterValidator, BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..cas import ensure_utc, now_utc
from ..models import Handover, PositionReport, Train
from .common import DONE_TRAIN_STATES, iso, log
from .data import get_network, route_info

SOURCES = ("gps", "zsr", "tis", "taf", "manual", "driver", "sim")
_NUMBER = re.compile(r"^[0-9A-Za-z][0-9A-Za-z \-/.]{0,39}$")
MAX_FUTURE = timedelta(minutes=10)
MAX_PAST = timedelta(days=7)


def _check_number(v: str) -> str:
    v = (v or "").strip()
    if not _NUMBER.match(v):
        raise ValueError("neplatné číslo vlaku")
    return v


def _check_ts(v: Optional[datetime]) -> Optional[datetime]:
    if v is None:
        return None
    v = ensure_utc(v)
    now = now_utc()
    if v > now + MAX_FUTURE:
        raise ValueError("čas polohy je v budúcnosti")
    if v < now - MAX_PAST:
        raise ValueError("čas polohy je starší ako 7 dní")
    return v


TrainNumber = Annotated[str, Field(min_length=1, max_length=40), AfterValidator(_check_number)]
OptionalTs = Annotated[Optional[datetime], AfterValidator(_check_ts)]
RequiredTs = Annotated[datetime, AfterValidator(_check_ts)]


class PositionIn(BaseModel):
    train_number: TrainNumber
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    ts: OptionalTs = None
    speed_kmh: Optional[float] = Field(default=None, ge=0, le=400)
    accuracy_m: Optional[float] = Field(default=None, ge=0, le=100000)
    source: str = "gps"

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        v = (v or "gps").strip().lower()
        if v not in SOURCES:
            raise ValueError(f"neznámy zdroj (povolené: {', '.join(SOURCES)})")
        return v


class DriverPositionIn(BaseModel):
    t: str = Field(min_length=10, max_length=600)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    ts: OptionalTs = None
    speed_kmh: Optional[float] = Field(default=None, ge=0, le=400)
    accuracy_m: Optional[float] = Field(default=None, ge=0, le=100000)


class RunningEventIn(BaseModel):
    train_number: TrainNumber
    station: str = Field(min_length=1, max_length=120)
    event: Literal["prichod", "odchod", "prejazd"]
    ts: RequiredTs
    delay_min: Optional[float] = Field(default=None, ge=-1440, le=10080)
    source: Literal["zsr", "tis", "taf"] = "zsr"


def find_train(s: Session, number: str, train_id: Optional[int] = None) -> Optional[Train]:
    """Vlak podľa čísla: posledný aktívny, inak posledný vôbec."""
    if train_id is not None:
        t = s.get(Train, train_id)
        if t is not None and t.number == number:
            return t
    base = select(Train).where(Train.number == number)
    t = s.scalar(base.where(Train.status.notin_(DONE_TRAIN_STATES))
                 .order_by(Train.run_date.desc(), Train.id.desc()))
    return t or s.scalar(base.order_by(Train.run_date.desc(), Train.id.desc()))


def _existing(s: Session, number: str, ts: datetime, source: str, lat: float, lon: float) -> Optional[PositionReport]:
    for p in s.scalars(select(PositionReport).where(PositionReport.train_number == number,
                                                    PositionReport.ts_utc == ts, PositionReport.source == source)):
        if abs(p.lat - lat) < 1e-7 and abs(p.lon - lon) < 1e-7:
            return p
    return None


def _result(p: PositionReport, duplicate: bool = False) -> dict:
    return {"id": p.id, "train_id": p.train_id, "train_number": p.train_number, "ts": iso(p.ts_utc),
            "source": p.source, "snapped": p.snapped_lat is not None,
            "lat": p.snapped_lat if p.snapped_lat is not None else p.lat,
            "lon": p.snapped_lon if p.snapped_lon is not None else p.lon,
            "matched_edge": p.matched_edge, "off_track_m": p.off_track_m, "nearest_station": p.nearest_station,
            "duplicate": duplicate}


def ingest_position(s: Session, number: str, lat: float, lon: float, ts: Optional[datetime] = None,
                    speed_kmh: Optional[float] = None, accuracy_m: Optional[float] = None, source: str = "gps",
                    train_id: Optional[int] = None) -> dict:
    ts = ensure_utc(ts) if ts else now_utc()
    dup = _existing(s, number, ts, source, lat, lon)
    if dup is not None:
        return _result(dup, duplicate=True)
    train = find_train(s, number, train_id)
    net = get_network()
    snapped_lat = snapped_lon = None
    matched_edge = None
    off_track = None
    nearest = ""
    if net is not None:
        prefer = None
        if train is not None:
            info = route_info(train)
            prefer = set(info.edges) if info is not None else None
        try:
            m = net.match(lat, lon, 300.0, prefer_edges=prefer)
            if m is not None:
                snapped_lat, snapped_lon, matched_edge, off_track = m["lat"], m["lon"], m["edge"], m["dist_m"]
            else:
                far = net.match(lat, lon, 2000.0)
                off_track = far["dist_m"] if far is not None else None
            st = net.nearest_station(lat, lon)
            nearest = st.name if st is not None else ""
        except Exception:  # noqa: BLE001 – poloha sa uloží aj bez priradenia k trati
            log.exception("Priradenie polohy k trati zlyhalo")
    p = PositionReport(train_id=train.id if train else None, train_number=number, ts_utc=ts, lat=lat, lon=lon,
                       speed_kmh=speed_kmh, source=source, accuracy_m=accuracy_m, matched_edge=matched_edge,
                       snapped_lat=snapped_lat, snapped_lon=snapped_lon, nearest_station=nearest,
                       off_track_m=off_track, received_utc=now_utc())
    s.add(p)
    s.flush()
    return _result(p)


class UnknownStation(ValueError):
    pass


def _same_station(net, name: str, candidates: list) -> bool:
    from ..siet.graf import norm

    target = norm(name)
    for cand in candidates:
        if not cand:
            continue
        if norm(cand) == target:
            return True
        st = net.find_station(cand)
        if st is not None and norm(st.name) == target:
            return True
    return False


def ingest_running_event(s: Session, ev: RunningEventIn) -> dict:
    """Udalosť jazdy (príchod / odchod / prejazd dopravňou) → poloha v dopravni (+ príchod na hranicu)."""
    net = get_network()
    st = net.find_station(ev.station) if net is not None else None
    if st is None:
        raise UnknownStation(f"Dopravňa „{ev.station}“ nie je v registri siete.")
    ts = ensure_utc(ev.ts)
    train = find_train(s, ev.train_number)
    lat, lon = net.nodes.get(st.node, (st.lat, st.lon))
    dup = _existing(s, ev.train_number, ts, ev.source, lat, lon)
    if dup is not None:
        result = _result(dup, duplicate=True)
    else:
        matched = None
        try:
            prefer = None
            if train is not None:
                info = route_info(train)
                prefer = set(info.edges) if info is not None else None
            m = net.match(lat, lon, 100.0, prefer_edges=prefer)
            matched = m["edge"] if m else None
        except Exception:  # noqa: BLE001
            log.exception("Priradenie dopravne k trati zlyhalo")
        p = PositionReport(train_id=train.id if train else None, train_number=ev.train_number, ts_utc=ts,
                           lat=lat, lon=lon, speed_kmh=None, source=ev.source, accuracy_m=None,
                           matched_edge=matched, snapped_lat=lat, snapped_lon=lon, nearest_station=st.name,
                           off_track_m=0.0, received_utc=now_utc())
        s.add(p)
        s.flush()
        result = _result(p)
        audit(s, f"api:{ev.source}", "udalost_jazdy", "train", train.id if train else "",
              train_number=ev.train_number, station=st.name, event=ev.event, ts=ts, delay_min=ev.delay_min,
              position_id=p.id)
    result.update(station=st.name, event=ev.event, delay_min=ev.delay_min, handover_id=None)

    if ev.event == "prichod" and train is not None:
        ho = s.scalar(select(Handover).where(Handover.train_id == train.id, Handover.direction == "vstup",
                                             Handover.status == "ocakavany").order_by(Handover.id.desc()))
        if ho is not None:
            border = ho.border or train.entry_border
            names = [border.sk_station, border.handover_station] if border is not None else []
            if border is not None and _same_station(net, st.name, names):
                ho.arrived_utc = ts
                ho.status = "prisiel"
                from ..planovanie import stav  # začiatok státia na koľajach ŽSR rovnako ako pri zápise vo webe

                stav.record_arrival(train, ho, ts)
                audit(s, f"api:{ev.source}", "handover_arrived", "handover", ho.id, train_id=train.id,
                      station=st.name, ts=ts)
                result["handover_id"] = ho.id
    return result
