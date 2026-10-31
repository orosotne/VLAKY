"""JSON API: vlaky, sieť, výluky, hranice, upozornenia, živé udalosti (SSE), príjem polôh a udalostí jazdy."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import secrets
import threading
import time
from datetime import timedelta
from typing import Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import func, select, text

from ..cas import now_utc
from ..config import settings
from ..db import session_scope
from ..models import Handover, Train
from .auth import require
from .common import iso, log, parse_iso
from .data import (alerts, borders_list, closures_geojson, get_network, max_ids, route_info, train_detail,
                   train_rows)
from .pozicie import PositionIn, RunningEventIn, UnknownStation, ingest_position, ingest_running_event

router = APIRouter()
view = require("view")


# ---------------------------------------------------------------- monitoring
@router.api_route("/health", methods=["GET", "HEAD"], include_in_schema=False)
def health():
    try:
        with session_scope() as s:
            s.execute(text("SELECT 1"))
        return JSONResponse({"status": "ok", "db": "ok", "time": iso(now_utc())},
                            headers={"Cache-Control": "no-store"})
    except Exception as exc:  # noqa: BLE001
        log.exception("Kontrola databázy zlyhala")
        return JSONResponse({"status": "error", "db": type(exc).__name__}, status_code=503,
                            headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- vlaky
@router.get("/api/vlaky")
def api_trains(vsetky: bool = False, user=Depends(view)):
    with session_scope() as s:
        rows = train_rows(s, include_done=vsetky)
    return {"ts": iso(now_utc()), "stale_minutes": settings().position_stale_minutes, "vlaky": rows}


@router.get("/api/vlaky/{train_id}")
def api_train(train_id: int, user=Depends(view)):
    with session_scope() as s:
        row = train_detail(s, train_id)
    if row is None:
        raise HTTPException(404, "Vlak neexistuje.")
    return row


@router.get("/api/vlaky/{train_id}/trasa")
def api_train_route(train_id: int, user=Depends(view)):
    with session_scope() as s:
        train = s.get(Train, train_id)
        if train is None:
            raise HTTPException(404, "Vlak neexistuje.")
        info = route_info(train)
        number = train.number
    if info is None:
        raise HTTPException(404, "Trasa sa nenašla (dopravňa nie je v registri siete alebo trasa nie je "
                                 "uskutočniteľná pre trakciu a rozchod vlaku).")
    return JSONResponse({"type": "Feature", "geometry": {"type": "LineString", "coordinates": info.coords},
                         "properties": {"train_id": train_id, "number": number, "length_km": info.length_km,
                                        "minutes": info.minutes, "stations": info.stations}},
                        media_type="application/geo+json", headers={"Cache-Control": "private, max-age=300"})


# ---------------------------------------------------------------- sieť
_LINES_CACHE: dict = {}
_LINES_LOCK = threading.Lock()


def _lines_blob() -> tuple:
    path = settings().lines_path
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _LINES_LOCK:
        if _LINES_CACHE.get("key") != key:
            data = path.read_bytes()
            _LINES_CACHE.update(key=key, data=data, etag='"' + hashlib.sha256(data).hexdigest()[:32] + '"')
        return _LINES_CACHE["data"], _LINES_CACHE["etag"]


@router.get("/api/siet/trate")
def api_lines(request: Request, user=Depends(view)):
    try:
        data, etag = _lines_blob()
    except OSError:
        raise HTTPException(503, "Geometria tratí nie je k dispozícii (data/siet/trate.geojson.gz).") from None
    headers = {"ETag": etag, "Cache-Control": "private, max-age=86400", "Vary": "Accept-Encoding"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(data, media_type="application/geo+json", headers=headers)
    return Response(gzip.decompress(data), media_type="application/geo+json", headers=headers)


_STATION_TYPES_MAP = ("station", "yard", "border")


@router.get("/api/siet/stanice")
def api_stations(q: str = "", vsetky: bool = False, limit: int = 15, user=Depends(view)):
    net = get_network()
    if net is None:
        raise HTTPException(503, "Register siete nie je k dispozícii.")
    if vsetky:
        items = [s for s in net.stations if s.type in _STATION_TYPES_MAP and s.name]
        return JSONResponse([{"name": s.name, "type": s.type, "lat": round(s.lat, 6), "lon": round(s.lon, 6)}
                             for s in items], headers={"Cache-Control": "private, max-age=3600"})
    q = q.strip()
    if len(q) < 2:
        return []
    out, seen = [], set()
    for s in net.search_stations(q, limit=max(1, min(limit, 30)) * 2):
        if s.name in seen:
            continue
        seen.add(s.name)
        out.append({"name": s.name, "type": s.type, "lat": round(s.lat, 6), "lon": round(s.lon, 6),
                    "uic": s.uic_ref, "ref": s.railway_ref})
        if len(out) >= max(1, min(limit, 30)):
            break
    return out


@router.get("/api/hranice")
def api_borders(user=Depends(view)):
    with session_scope() as s:
        items = borders_list(s)
        counts = dict(s.execute(select(Handover.border_id, func.count(Handover.id))
                                .where(Handover.status.in_(("ocakavany", "prisiel")))
                                .group_by(Handover.border_id)).all())
    for b in items:
        b["expected"] = counts.get(b["id"], 0)
    return items


@router.get("/api/vyluky")
def api_closures(od: Optional[str] = None, do: Optional[str] = None, user=Depends(view)):
    now = now_utc()
    start = parse_iso(od) if od else now
    end = parse_iso(do) if do else start
    if start is None or end is None:
        raise HTTPException(422, "Parametre od/do musia byť čas v tvare ISO 8601.")
    if end < start:
        start, end = end, start
    end = min(end, start + timedelta(days=31))
    with session_scope() as s:
        fc = closures_geojson(s, start, end, now)
    fc["od"], fc["do"] = iso(start), iso(end)
    return JSONResponse(fc, media_type="application/geo+json")


@router.get("/api/upozornenia")
def api_alerts(user=Depends(view)):
    with session_scope() as s:
        return {"ts": iso(now_utc()), "upozornenia": alerts(s)}


# ---------------------------------------------------------------- živé udalosti (SSE)
_IDS_CACHE: dict = {}
_IDS_LOCK = threading.Lock()


def _cached_max_ids() -> dict:
    """Najvyššie id záznamov (zdieľaná cache 1,5 s pre všetkých klientov; agent beží v inom procese)."""
    url = settings().database_url
    with _IDS_LOCK:
        hit = _IDS_CACHE.get(url)
        if hit and time.monotonic() - hit[0] < 1.5:
            return hit[1]
    with session_scope() as s:
        ids = max_ids(s)
    with _IDS_LOCK:
        _IDS_CACHE[url] = (time.monotonic(), ids)
    return ids


POLL_S = 2.0
HEARTBEAT_S = 15.0


@router.get("/api/stream")
async def api_stream(request: Request, max_s: int = 55, user=Depends(view)):
    """Server-Sent Events: {type, id} pri novej polohe, odporúčaní, udalosti z e-mailu, notifikácii, výluke.

    Spojenie sa po `max_s` sekundách ukončí a prehliadač sa sám znovu pripojí (EventSource) – vďaka tomu
    reštart servera nečaká na otvorené spojenia.
    """
    max_s = max(1, min(int(max_s), 300))

    async def events():
        try:
            ids = await run_in_threadpool(_cached_max_ids)
        except Exception:  # noqa: BLE001
            log.exception("SSE: databáza nie je dostupná")
            ids = {}
        yield "retry: 3000\n\n"
        yield f"data: {json.dumps({'type': 'hello', 'ids': ids})}\n\n"
        start = last_beat = time.monotonic()
        while True:
            left = max_s - (time.monotonic() - start)
            if left <= 0:
                break
            await asyncio.sleep(min(POLL_S, left))
            if await request.is_disconnected():
                break
            try:
                current = await run_in_threadpool(_cached_max_ids)
            except Exception:  # noqa: BLE001
                log.exception("SSE: dotaz na zmeny zlyhal")
                continue
            for key, value in current.items():
                if ids.get(key) != value:
                    yield f"data: {json.dumps({'type': key, 'id': value})}\n\n"
            ids = current
            if time.monotonic() - last_beat >= HEARTBEAT_S:
                last_beat = time.monotonic()
                yield ": heartbeat\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- príjem polôh (GPS brána) a udalostí jazdy
def bearer_auth(request: Request) -> None:
    expected = settings().positions_token
    if not expected:
        raise HTTPException(503, "Príjem polôh nie je nakonfigurovaný (VLAKY_POSITIONS_TOKEN).")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip() or not secrets.compare_digest(token.strip(), expected):
        raise HTTPException(401, "Neplatný alebo chýbajúci token.", headers={"WWW-Authenticate": "Bearer"})


MAX_BATCH = 1000


@router.post("/api/pozicie", dependencies=[Depends(bearer_auth)])
def api_positions(body: Union[PositionIn, list[PositionIn]] = Body(...)):
    items = body if isinstance(body, list) else [body]
    if len(items) > MAX_BATCH:
        raise HTTPException(413, f"Najviac {MAX_BATCH} polôh v jednej dávke.")
    with session_scope() as s:
        results = [ingest_position(s, p.train_number, p.lat, p.lon, p.ts, p.speed_kmh, p.accuracy_m, p.source)
                   for p in items]
    if isinstance(body, list):
        return {"ok": True, "stored": sum(1 for r in results if not r["duplicate"]), "items": results}
    return {"ok": True, **results[0]}


@router.post("/api/udalosti-jazdy", dependencies=[Depends(bearer_auth)])
def api_running_events(body: RunningEventIn):
    try:
        with session_scope() as s:
            result = ingest_running_event(s, body)
    except UnknownStation as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **result}
