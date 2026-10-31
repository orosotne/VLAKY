"""Reporty (M13) a vyhodnotenie tieňovej prevádzky (2.4, 2.7): odporúčania a reakcie dispečera, upozornenia,
e-maily a udalosti, preberanie na hraniciach, pracovný čas; export CSV; kalibrácia z histórie (vedúci)."""
from __future__ import annotations

import csv
import importlib
import io
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..cas import ensure_utc, local, local_naive_to_utc, now_utc
from ..db import session_scope
from ..models import BorderCrossing, Email, ExtractedEvent, Handover, Notification, Recommendation, Train
from .auth import csrf_form, require
from .common import EVENT_KIND, REC_STATE, log, redirect, render

router = APIRouter()
reports = require("reports")
params_perm = require("params")
EXPORTS = ("odporucania", "preberania", "udalosti")


# ---------------------------------------------------------------- obdobie a štatistika
def period(od: str = "", do: str = "") -> tuple:
    """Obdobie v miestnych dňoch (predvolene posledných 7 dní vrátane dneška) → (od, do, start_utc, end_utc)."""
    today = local(now_utc()).date()

    def parse(v: str) -> Optional[date]:
        try:
            return date.fromisoformat(v) if v else None
        except ValueError:
            return None

    d_do = parse(do) or today
    d_od = parse(od) or d_do - timedelta(days=6)
    if d_od > d_do:
        d_od, d_do = d_do, d_od
    d_od = max(d_od, d_do - timedelta(days=366))
    start = local_naive_to_utc(datetime(d_od.year, d_od.month, d_od.day))
    end = local_naive_to_utc(datetime(d_do.year, d_do.month, d_do.day) + timedelta(days=1))
    return d_od, d_do, start, end


def quantile(values: list, q: float) -> Optional[float]:
    s = sorted(values)
    if not s:
        return None
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def _share(a: int, b: int) -> Optional[float]:
    return a / b if b else None


def build_report(s, start: datetime, end: datetime) -> dict:
    recs = list(s.scalars(select(Recommendation).options(selectinload(Recommendation.train))
                          .where(Recommendation.created_at >= start, Recommendation.created_at < end)
                          .order_by(Recommendation.created_at)))
    states = Counter(r.state for r in recs)
    decided = states["prijate"] + states["upravene"] + states["zamietnute"]
    crew_status = Counter((r.crew or {}).get("status") for r in recs)
    crew_trains = {k: len({r.train_id for r in recs if (r.crew or {}).get("status") == k})
                   for k in ("ČERVENÁ", "ORANŽOVÁ")}
    rejections = [{"at": r.decided_at or r.created_at, "train": r.train.number if r.train else "",
                   "decision": r.decision, "color": r.color, "note": r.dispatcher_note, "id": r.id,
                   "by": r.decided_by}
                  for r in recs if r.state == "zamietnute"][-50:][::-1]

    notif = defaultdict(Counter)
    for channel, status, n in s.execute(select(Notification.channel, Notification.status, func.count(Notification.id))
                                        .where(Notification.created_at >= start, Notification.created_at < end)
                                        .group_by(Notification.channel, Notification.status)):
        notif[channel][status] += n
    notif_statuses = sorted({st for c in notif.values() for st in c})

    emails = list(s.execute(select(Email.verdict, Email.status).where(Email.received_at >= start,
                                                                     Email.received_at < end)).all())
    events = list(s.scalars(select(ExtractedEvent).where(ExtractedEvent.created_at >= start,
                                                         ExtractedEvent.created_at < end)))
    auto = [e for e in events if e.status == "aplikovane" and e.confirmed_at is None]
    confirmed = [e for e in events if e.confirmed_at is not None and e.status != "zamietnute"]
    with_edits = [e for e in confirmed if (e.validation or {}).get("edits")]

    per_border: dict = defaultdict(list)
    handover_count = 0
    for h in s.scalars(select(Handover).where(Handover.arrived_utc >= start, Handover.arrived_utc < end)):
        handover_count += 1
        if h.taken_over_utc is not None:
            mins = (ensure_utc(h.taken_over_utc) - ensure_utc(h.arrived_utc)).total_seconds() / 60.0
            if 0 <= mins <= 48 * 60:
                per_border[h.border_id].append(mins)
    borders = {b.id: b for b in s.scalars(select(BorderCrossing).where(BorderCrossing.id.in_(list(per_border))))} \
        if per_border else {}
    handovers = sorted(({"border": borders[bid].name if bid in borders else "?", "n": len(v),
                         "p50": quantile(v, 0.5), "p90": quantile(v, 0.9),
                         "plan_p50": borders[bid].dwell_p50_min if bid in borders else None,
                         "plan_p90": borders[bid].dwell_p90_min if bid in borders else None}
                        for bid, v in per_border.items()), key=lambda x: -x["n"])

    return {
        "recs_total": len(recs), "by_decision": Counter(r.decision for r in recs).most_common(),
        "by_color": Counter(r.color for r in recs).most_common(),
        "by_priority": Counter(r.priority for r in recs).most_common(), "states": states, "decided": decided,
        "acceptance": _share(states["prijate"] + states["upravene"], decided),
        "accepted_unchanged": _share(states["prijate"], decided), "rejections": rejections,
        "crew_red": crew_status["ČERVENÁ"], "crew_orange": crew_status["ORANŽOVÁ"], "crew_trains": crew_trains,
        "notif": {k: dict(v) for k, v in sorted(notif.items())}, "notif_statuses": notif_statuses,
        "notif_total": sum(sum(c.values()) for c in notif.values()),
        "notif_blocked": sum(c.get("zablokovana", 0) for c in notif.values()),
        "emails_total": len(emails), "emails_verdict": Counter(v for v, _s in emails).most_common(),
        "emails_status": Counter(st for _v, st in emails).most_common(),
        "events_total": len(events), "events_auto": len(auto), "events_confirmed": len(confirmed),
        "events_rejected": sum(1 for e in events if e.status == "zamietnute"),
        "events_waiting": sum(1 for e in events if e.status == "navrh"), "events_with_edits": len(with_edits),
        "edits_share": _share(len(with_edits), len(confirmed)),
        "events_by_kind": Counter(e.kind for e in events).most_common(),
        "handover_count": handover_count, "handovers": handovers,
    }


def _calibration(s) -> tuple:
    try:
        mod = importlib.import_module("vlaky.planovanie.kalibracia")
        return mod.propose_calibration(s), ""
    except (ImportError, AttributeError, NotImplementedError):
        return None, "Modul kalibrácie zatiaľ nie je k dispozícii."
    except Exception as exc:  # noqa: BLE001
        log.exception("Návrh kalibrácie zlyhal")
        return None, f"Návrh kalibrácie sa nepodarilo vypočítať: {type(exc).__name__}: {str(exc)[:200]}"


@router.get("/reporty", response_class=HTMLResponse)
def reports_page(request: Request, od: str = "", do: str = "", user=Depends(reports)):
    d_od, d_do, start, end = period(od, do)
    with session_scope() as s:
        report = build_report(s, start, end)
        calibration, calibration_error = _calibration(s) if user.can("params") else (None, "")
        return render(request, "reporty.html", {"r": report, "od": d_od.isoformat(), "do": d_do.isoformat(),
                                                "calibration": calibration, "calibration_error": calibration_error,
                                                "exports": EXPORTS}, user=user)


@router.post("/reporty/kalibracia")
def calibration_apply(request: Request, user=Depends(params_perm), form=Depends(csrf_form)):
    """Použije návrh kalibrácie – návrh sa prepočíta na serveri (údajom z prehliadača sa neverí)."""
    include_small = str(form.get("aj_male") or "") in ("1", "on")
    try:
        mod = importlib.import_module("vlaky.planovanie.kalibracia")
        with session_scope() as s:
            proposal = mod.propose_calibration(s)
            applied = mod.apply_calibration(s, proposal, actor=user.username, include_small=include_small)
    except (ImportError, AttributeError, NotImplementedError):
        return redirect("/reporty#kalibracia", "Modul kalibrácie zatiaľ nie je k dispozícii.", "chyba")
    except Exception as exc:  # noqa: BLE001
        log.exception("Kalibrácia zlyhala")
        return redirect("/reporty#kalibracia", f"Kalibráciu sa nepodarilo použiť: {str(exc)[:300]}", "chyba")
    borders = len((applied or {}).get("borders") or [])
    delay = (applied or {}).get("delay")
    if not borders and not delay:
        return redirect("/reporty#kalibracia", "Nič sa nezmenilo – žiadna položka nemá dosť vzoriek.", "info")
    return redirect("/reporty#kalibracia", f"Kalibrácia použitá: doby preberania na {borders} priechodoch"
                                           f"{', zdržania na 100 km' if delay else ''}. Zmena je v audite.")


# ---------------------------------------------------------------- export CSV (Excel: UTF-8 s BOM, oddeľovač ;)
def _cell(value) -> str:
    """Hodnota bunky; text začínajúci znakom vzorca sa neutralizuje (ochrana pred CSV injection)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return local(value).strftime("%Y-%m-%d %H:%M")
    text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _csv(name: str, header: list, rows: list, d_od: date, d_do: date) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(header)
    for row in rows:
        w.writerow([_cell(v) for v in row])
    filename = f"{name}_{d_od.isoformat()}_{d_do.isoformat()}.csv"
    return Response(("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


def _pct(v) -> str:
    try:
        return str(round(float(v) * 100))
    except (TypeError, ValueError):
        return ""


@router.get("/reporty/export/{typ}.csv")
def export_csv(typ: str, od: str = "", do: str = "", user=Depends(reports)):
    if typ not in EXPORTS:
        raise HTTPException(404, "Neznámy export.")
    d_od, d_do, start, end = period(od, do)
    from .common import parse_iso

    with session_scope() as s:
        if typ == "odporucania":
            header = ["id", "vytvorené", "vlak", "relácia", "rozhodnutie", "semafor", "priorita", "stav", "rozhodol",
                      "rozhodnuté", "poznámka dispečera", "P(včas) %", "P(zadržania) %", "príchod P50", "príchod P90",
                      "očakávaný náklad €", "rušňovodič", "podnet", "zhrnutie"]
            rows = []
            for r in s.scalars(select(Recommendation).options(selectinload(Recommendation.train))
                               .where(Recommendation.created_at >= start, Recommendation.created_at < end)
                               .order_by(Recommendation.created_at)):
                ch = r.chosen or {}
                rows.append([r.id, r.created_at, r.train.number if r.train else "",
                             f"{r.train.origin} → {r.train.destination}" if r.train else "", r.decision, r.color,
                             r.priority, REC_STATE.get(r.state, r.state), r.decided_by, r.decided_at,
                             r.dispatcher_note, _pct(ch.get("p_on_time")), _pct(ch.get("p_hold")),
                             parse_iso(ch.get("arrival_p50")), parse_iso(ch.get("arrival_p90")),
                             ch.get("expected_cost", ""), (r.crew or {}).get("status", ""), r.trigger, r.summary])
        elif typ == "preberania":
            header = ["id", "vlak", "priechod", "smer", "avízo (príchod)", "príchod", "prevzatie", "trvanie min",
                      "stav", "zistenia"]
            rows = []
            q = (select(Handover, Train.number, BorderCrossing.name)
                 .join(Train, Train.id == Handover.train_id)
                 .outerjoin(BorderCrossing, BorderCrossing.id == Handover.border_id)
                 .where(((Handover.arrived_utc >= start) & (Handover.arrived_utc < end)) |
                        ((Handover.avizo_eta_utc >= start) & (Handover.avizo_eta_utc < end)))
                 .order_by(Handover.id))
            for h, number, border in s.execute(q):
                dur = ""
                if h.arrived_utc and h.taken_over_utc:
                    dur = round((ensure_utc(h.taken_over_utc) - ensure_utc(h.arrived_utc)).total_seconds() / 60.0)
                rows.append([h.id, number, border or "", h.direction, h.avizo_eta_utc, h.arrived_utc, h.taken_over_utc,
                             dur, h.status, h.findings])
        else:
            header = ["id", "vytvorené", "druh", "istota %", "extraktor", "stav", "potvrdil", "potvrdené",
                      "počet úprav dispečera", "odosielateľ", "predmet e-mailu"]
            rows = []
            q = (select(ExtractedEvent, Email.sender, Email.subject)
                 .outerjoin(Email, Email.id == ExtractedEvent.email_id)
                 .where(ExtractedEvent.created_at >= start, ExtractedEvent.created_at < end)
                 .order_by(ExtractedEvent.id))
            for e, sender, subject in s.execute(q):
                rows.append([e.id, e.created_at, EVENT_KIND.get(e.kind, e.kind), _pct(e.confidence), e.extractor,
                             e.status, e.confirmed_by, e.confirmed_at, len((e.validation or {}).get("edits") or []),
                             sender or "", subject or ""])
    return _csv(typ, header, rows, d_od, d_do)
