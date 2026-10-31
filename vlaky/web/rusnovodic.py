"""Stránka rušňovodiča (mobilná PWA) – podpísaný odkaz na 24 h, hlásenia a zdieľanie polohy.

Odkaz vytvorí dispečer vo webe a rušňovodičovi ho pošle sám (agent rušňovodičom nič neposiela – sekcia 2.2).
"""
from __future__ import annotations

import hashlib
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..audit import audit
from ..cas import fmt, now_utc
from ..config import settings
from ..db import session_scope
from ..models import DriverShift, Train
from .common import DONE_TRAIN_STATES, STATIC_DIR, log, render
from .pozicie import DriverPositionIn, ingest_position

TOKEN_MAX_AGE = 24 * 3600
router = APIRouter()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings().secret_key, salt="vlaky.rusnovodic.v1")


def make_driver_token(train: Train) -> str:
    return _serializer().dumps({"n": train.number, "id": train.id})


def driver_link(train: Train) -> str:
    return f"{settings().public_url.rstrip('/')}/rusnovodic?t={make_driver_token(train)}"


def verify_driver_token(token: str) -> Optional[dict]:
    """Vráti {"n": číslo vlaku, "id": id vlaku} alebo None (neplatný / starší ako 24 h)."""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("n"), str) or not isinstance(data.get("id"), int):
        return None
    return data


def _train_for_token(s, data: dict) -> Optional[Train]:
    t = s.get(Train, data["id"])
    if t is None or t.number != data["n"] or t.status in DONE_TRAIN_STATES:
        return None
    return t


@router.get("/rusnovodic", response_class=HTMLResponse)
def driver_page(request: Request, t: str = ""):
    data = verify_driver_token(t)
    with session_scope() as s:
        train = _train_for_token(s, data) if data else None
        if train is None:
            return render(request, "rusnovodic.html", {"valid": False, "token": t}, status_code=403 if t else 200)
        shift = s.scalar(select(DriverShift).where(DriverShift.train_id == train.id,
                                                   DriverShift.status != "ukoncena")
                         .order_by(DriverShift.start_utc.desc()))
        ctx = {"valid": True, "token": t, "train": train, "shift": shift,
               "driver_name": shift.driver.name if shift and shift.driver else ""}
        return render(request, "rusnovodic.html", ctx)


class ReportIn(BaseModel):
    t: str = Field(min_length=10, max_length=600)
    typ: Literal["nastup", "pripraveny", "odchod", "problem"]
    text: str = Field(default="", max_length=2000)


TYP_TEXT = {"nastup": "Nástup na zmenu", "pripraveny": "Vlak pripravený", "odchod": "Odchod",
            "problem": "Problém"}


def _forbidden() -> JSONResponse:
    return JSONResponse({"ok": False, "detail": "Odkaz je neplatný alebo vypršal. Vyžiadajte si nový od "
                                                "dispečera."}, status_code=403)


@router.post("/api/hlasenie")
def driver_report(body: ReportIn, request: Request, background: BackgroundTasks):
    data = verify_driver_token(body.t)
    if data is None:
        return _forbidden()
    now = now_utc()
    text = body.text.strip()
    if body.typ == "problem" and not text:
        return JSONResponse({"ok": False, "detail": "Popíšte problém."}, status_code=422)
    with session_scope() as s:
        train = _train_for_token(s, data)
        if train is None:
            return _forbidden()
        actor = f"rusnovodic:{train.number}"
        detail: dict = {"typ": body.typ, "text": text}
        if body.typ == "nastup":
            shift = s.scalar(select(DriverShift).where(DriverShift.train_id == train.id,
                                                       DriverShift.status != "ukoncena")
                             .order_by(DriverShift.start_utc.desc(), DriverShift.id.desc()))
            if shift is not None:
                detail["shift_id"] = shift.id
                detail["previous_start"] = shift.start_utc
                shift.start_utc = now
                shift.status = "prebieha"
                message = f"Nástup zaznamenaný o {fmt(now, False)}. Limit pracovného času beží od tohto času."
            else:
                message = (f"Nástup zaznamenaný o {fmt(now, False)}. Zmena pre tento vlak nie je naplánovaná – "
                           f"dispečer ju doplní.")
        elif body.typ == "pripraveny":
            detail["previous_ready"] = train.ready_utc
            train.ready_utc = now
            message = f"Vlak pripravený o {fmt(now, False)}."
        elif body.typ == "odchod":
            detail["previous_status"] = train.status
            train.status = "v_jazde"
            message = f"Odchod zaznamenaný o {fmt(now, False)}."
        else:
            message = "Problém odoslaný dispečerovi."
        audit(s, actor, f"hlasenie_{body.typ}", "train", train.id, **detail)
        train_id, number = train.id, train.number
        relation = f"{train.origin} → {train.destination}"
    if body.typ == "problem":
        _notify_problem(number, relation, text)
    if request.app.state.auto_evaluate:
        background.add_task(evaluate_train_bg, train_id, f"hlásenie rušňovodiča: {TYP_TEXT[body.typ]}")
    return {"ok": True, "message": message, "typ": body.typ, "ts": now.isoformat()}


def _notify_problem(number: str, relation: str, text: str) -> None:
    """Upozornenie dispečerom (iba interný zoznam – odosielacia služba iných príjemcov odmietne)."""
    try:
        from ..notifikacie.odosielanie import send_internal

        with session_scope() as s:
            send_internal(s, "upozornenie", f"[AGENT][DÔLEŽITÉ] Vlak {number}: hlásenie rušňovodiča – problém",
                          f"Vlak {number} ({relation})\nRušňovodič hlási problém:\n\n{text}", "DÔLEŽITÉ",
                          dedup_key=f"problem:{number}:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}")
    except NotImplementedError:
        log.warning("Odosielanie upozornení zatiaľ nie je k dispozícii")
    except Exception:  # noqa: BLE001 – hlásenie je uložené v audite aj tak
        log.exception("Upozornenie o probléme sa nepodarilo odoslať")


def evaluate_train_bg(train_id: int, trigger: str) -> None:
    """Prepočet vlaku na pozadí (po hlásení, úprave vlaku, preberaní…). Chyby sa len zalogujú."""
    import importlib

    try:
        planovac = importlib.import_module("vlaky.planovanie.planovac")
        with session_scope() as s:
            planovac.evaluate_train(s, train_id, trigger=trigger)
    except NotImplementedError:
        log.info("Plánovač zatiaľ nie je k dispozícii")
    except Exception:  # noqa: BLE001
        log.exception("Prepočet vlaku %s zlyhal", train_id)


def evaluate_all_bg(trigger: str) -> None:
    import importlib

    try:
        planovac = importlib.import_module("vlaky.planovanie.planovac")
        with session_scope() as s:
            planovac.evaluate_all(s, trigger=trigger)
    except NotImplementedError:
        log.info("Plánovač zatiaľ nie je k dispozícii")
    except Exception:  # noqa: BLE001
        log.exception("Prepočet všetkých vlakov zlyhal")


@router.post("/api/pozicie/rusnovodic")
def driver_position(body: DriverPositionIn):
    data = verify_driver_token(body.t)
    if data is None:
        return _forbidden()
    with session_scope() as s:
        train = _train_for_token(s, data)
        if train is None:
            return _forbidden()
        out = ingest_position(s, train.number, body.lat, body.lon, body.ts, body.speed_kmh, body.accuracy_m,
                              source="driver", train_id=train.id)
    return {"ok": True, **out}


@router.get("/rusnovodic.webmanifest")
def manifest():
    return Response((STATIC_DIR / "rusnovodic.webmanifest").read_bytes(), media_type="application/manifest+json",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/rusnovodic-sw.js")
def service_worker():
    return Response((STATIC_DIR / "js" / "rusnovodic-sw.js").read_bytes(), media_type="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/rusnovodic"})
