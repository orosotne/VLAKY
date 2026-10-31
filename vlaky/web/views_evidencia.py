"""Stránky registrov: vlaky, výluky, hraničné priechody a preberania, rušňovodiči a zmeny, rušne."""
from __future__ import annotations

import importlib
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import case, or_, select
from sqlalchemy.orm import selectinload

from ..audit import audit
from ..cas import ensure_utc, now_utc
from ..db import session_scope
from ..models import (AuditLog, BorderCrossing, Closure, Driver, DriverShift, ExtractedEvent, Handover, Locomotive,
                      Order, Path, PositionReport, Recommendation, Train)
from .auth import csrf_form, require
from .common import (CLOSURE_KIND, CLOSURE_STATUS, DONE_TRAIN_STATES, LOCO_SYSTEMS, TRAIN_STATUS, FormReader,
                     NetworkUnavailable,
                     crew_color, current_params, input_dt, iso, log, redirect, render, resolve_station,
                     safe_next, station_or_error)
from .data import closure_edges, closure_spans, latest_recommendations
from .prepocet import evaluate_all_bg, evaluate_train_bg, evaluate_train_now
from .rusnovodic import driver_link, revoke_driver_links, token_version

router = APIRouter()
view = require("view")
operate = require("operate")
crew = require("crew")
borders_perm = require("borders")


def _diff(obj, values: dict) -> dict:
    changes = {}
    for k, v in values.items():
        old = getattr(obj, k)
        if old is not None and hasattr(old, "tzinfo"):
            old = ensure_utc(old)
        if old != v:
            changes[k] = [iso(old) if hasattr(old, "tzinfo") else old, iso(v) if hasattr(v, "tzinfo") else v]
    return changes


# ================================================================ VLAKY (M2)
def _train_choices(s) -> dict:
    return {"borders": list(s.scalars(select(BorderCrossing).order_by(BorderCrossing.name))),
            "locos": list(s.scalars(select(Locomotive).order_by(Locomotive.number))),
            "statuses": TRAIN_STATUS}


def _read_train_form(reader: FormReader, s, train_id: Optional[int] = None) -> dict:
    v: dict = {}
    v["number"] = reader.text("number", "Číslo vlaku", required=True, maxlen=40)
    v["run_date"] = reader.day("run_date", "Dátum jazdy")
    v["customer"] = reader.text("customer", "Zákazník", maxlen=200)
    v["origin"] = station_or_error(reader, "origin", "Východisková dopravňa")
    v["destination"] = station_or_error(reader, "destination", "Cieľová dopravňa")
    resolved = []
    for name in reader.csv_list("via")[:10]:
        try:
            canonical = resolve_station(name)
        except NetworkUnavailable:
            reader.errors["via"] = "Cez: register siete nie je k dispozícii."
            break
        if canonical is None:
            reader.errors["via"] = f"Cez: dopravňa „{name}“ nie je v registri siete."
        else:
            resolved.append(canonical)
    v["via"] = resolved
    for key, label in (("entry_border_id", "Vstupný hraničný priechod"),
                       ("exit_border_id", "Výstupný hraničný priechod"), ("locomotive_id", "Rušeň")):
        num = reader.number(key, label, integer=True, minimum=1)
        v[key] = int(num) if num else None
    v["length_m"] = reader.number("length_m", "Dĺžka vlaku [m]", minimum=1, maximum=2000)
    v["weight_t"] = reader.number("weight_t", "Hmotnosť [t]", minimum=1, maximum=10000)
    v["gauge"] = reader.choice("gauge", "Rozchod", ("1435", "1520"), "1435")
    v["traction"] = reader.choice("traction", "Trakcia", ("E", "M"), "E")
    vmax = reader.number("vmax_kmh", "Najvyššia rýchlosť [km/h]", minimum=10, maximum=160)
    v["vmax_kmh"] = vmax if vmax is not None else 100.0
    v["rid"] = reader.flag("rid")
    v["ready_utc"] = reader.local_dt("ready_utc", "Pripravený (miestny čas)")
    v["planned_departure_utc"] = reader.local_dt("planned_departure_utc", "Plánovaný odchod (miestny čas)")
    v["deadline_utc"] = reader.local_dt("deadline_utc", "Termín doručenia (miestny čas)")
    v["status"] = reader.choice("status", "Stav", TRAIN_STATUS.keys(), "planovany")
    v["notes"] = reader.text("notes", "Poznámka", maxlen=2000)
    order_id = reader.number("order_id", "Objednávka", integer=True, minimum=1)
    v["order_id"] = int(order_id) if order_id else None
    if v["order_id"] and s.get(Order, v["order_id"]) is None:
        reader.errors["order_id"] = "Objednávka neexistuje."
    if v["ready_utc"] and v["deadline_utc"] and v["deadline_utc"] <= v["ready_utc"]:
        reader.errors["deadline_utc"] = "Termín doručenia musí byť po čase pripravenosti."
    if v["origin"] and v["origin"] == v["destination"]:
        reader.errors["destination"] = "Cieľová dopravňa sa musí líšiť od východiskovej."
    for key, model in (("entry_border_id", BorderCrossing), ("exit_border_id", BorderCrossing),
                       ("locomotive_id", Locomotive)):
        if v[key] and s.get(model, v[key]) is None:
            reader.errors[key] = "Neplatný výber."
    if v["locomotive_id"] and "locomotive_id" not in reader.errors:
        loco = s.get(Locomotive, v["locomotive_id"])
        if loco.traction and loco.traction != v["traction"]:
            reader.errors["traction"] = (f"Trakcia vlaku ({v['traction']}) nesedí s rušňom {loco.number} "
                                         f"({loco.traction}).")
    if v["number"] and v["run_date"]:
        q = select(Train).where(Train.number == v["number"], Train.run_date == v["run_date"],
                                Train.status.notin_(("zruseny",)))
        if train_id:
            q = q.where(Train.id != train_id)
        if s.scalar(q) is not None:
            reader.errors["number"] = "Vlak s týmto číslom a dátumom jazdy už existuje."
    return v


@router.get("/vlaky", response_class=HTMLResponse)
def trains_page(request: Request, filter: str = "aktivne", q: str = "", user=Depends(view)):  # noqa: A002
    with session_scope() as s:
        query = select(Train).options(selectinload(Train.locomotive), selectinload(Train.entry_border),
                                      selectinload(Train.exit_border))
        if filter == "aktivne":
            query = query.where(Train.status.notin_(DONE_TRAIN_STATES))
        elif filter == "ukoncene":
            query = query.where(Train.status.in_(DONE_TRAIN_STATES))
        if q.strip():
            like = f"%{q.strip()[:60]}%"
            query = query.where(or_(Train.number.like(like), Train.customer.like(like), Train.origin.like(like),
                                    Train.destination.like(like)))
        trains = list(s.scalars(query.order_by(Train.run_date.desc(), Train.number).limit(300)))
        ids = [t.id for t in trains]
        recs = latest_recommendations(s, ids) if ids else {}
        return render(request, "vlaky.html", {"trains": trains, "recs": recs, "filter": filter, "q": q}, user=user)


@router.get("/vlaky/novy", response_class=HTMLResponse)
def train_new(request: Request, objednavka: int = 0, user=Depends(operate)):
    values: dict = {"gauge": "1435", "traction": "E", "vmax_kmh": "100", "status": "planovany"}
    order = None
    with session_scope() as s:
        if objednavka:
            order = s.get(Order, objednavka)
            if order is None:
                raise HTTPException(404, "Objednávka neexistuje.")
            values.update({"order_id": order.id, "customer": order.customer, "origin": order.origin,
                           "destination": order.destination, "length_m": order.length_m or "",
                           "weight_t": order.weight_t or "", "rid": "1" if order.rid else "",
                           "deadline_utc": input_dt(order.deadline_utc),
                           "ready_utc": input_dt(order.earliest_departure_utc),
                           "run_date": input_dt(order.earliest_departure_utc)[:10],
                           "notes": f"Z objednávky #{order.id}" + (f": {order.cargo}" if order.cargo else "")})
        return render(request, "vlak_form.html", {"train": None, "values": values, "errors": {}, "order": order,
                                                  **_train_choices(s)}, user=user)


def _train_values_for_form(t: Train) -> dict:
    return {"number": t.number, "run_date": t.run_date, "customer": t.customer, "origin": t.origin,
            "destination": t.destination, "via": ", ".join(t.via or []), "entry_border_id": t.entry_border_id or "",
            "exit_border_id": t.exit_border_id or "", "locomotive_id": t.locomotive_id or "",
            "length_m": t.length_m or "", "weight_t": t.weight_t or "", "gauge": t.gauge, "traction": t.traction,
            "vmax_kmh": t.vmax_kmh, "rid": "1" if t.rid else "", "ready_utc": input_dt(t.ready_utc),
            "planned_departure_utc": input_dt(t.planned_departure_utc), "deadline_utc": input_dt(t.deadline_utc),
            "status": t.status, "notes": t.notes, "order_id": t.order_id or ""}


@router.post("/vlaky/novy")
def train_create(request: Request, background: BackgroundTasks, user=Depends(operate), form=Depends(csrf_form)):
    reader = FormReader(form)
    with session_scope() as s:
        values = _read_train_form(reader, s)
        if not reader.ok:
            return render(request, "vlak_form.html", {"train": None, "values": reader.values(),
                                                      "errors": reader.errors, **_train_choices(s)},
                          user=user, status_code=422)
        train = Train(**values)
        s.add(train)
        s.flush()
        audit(s, user.username, "train_create", "train", train.id,
              **{k: (iso(v) if hasattr(v, "tzinfo") else v) for k, v in values.items()})
        train_id, number = train.id, train.number
    if request.app.state.auto_evaluate:
        background.add_task(evaluate_train_bg, train_id, f"nový vlak (zadal {user.username})")
    return redirect(f"/vlaky/{train_id}", f"Vlak {number} uložený.")


@router.get("/vlaky/{train_id}", response_class=HTMLResponse)
def train_detail_page(request: Request, train_id: int, user=Depends(view)):
    return _train_detail(request, train_id, user)


def _train_detail(request: Request, train_id: int, user, link: str = "") -> HTMLResponse:
    with session_scope() as s:
        t = s.scalar(select(Train).options(selectinload(Train.locomotive), selectinload(Train.entry_border),
                                           selectinload(Train.exit_border)).where(Train.id == train_id))
        if t is None:
            raise HTTPException(404, "Vlak neexistuje.")
        rec = s.scalar(select(Recommendation).where(Recommendation.train_id == t.id)
                       .order_by(Recommendation.id.desc()))
        positions = list(s.scalars(select(PositionReport).where(PositionReport.train_id == t.id)
                                   .order_by(PositionReport.ts_utc.desc(), PositionReport.id.desc()).limit(15)))
        shifts = list(s.scalars(select(DriverShift).options(selectinload(DriverShift.driver))
                                .where(DriverShift.train_id == t.id).order_by(DriverShift.start_utc.desc())))
        handovers = list(s.scalars(select(Handover).options(selectinload(Handover.border))
                                   .where(Handover.train_id == t.id).order_by(Handover.id.desc())))
        paths = list(s.scalars(select(Path).where(Path.train_id == t.id).order_by(Path.id.desc()).limit(10)))
        history = list(s.scalars(select(AuditLog).where(AuditLog.entity == "train", AuditLog.entity_id == str(t.id))
                                 .order_by(AuditLog.ts.desc(), AuditLog.id.desc()).limit(30)))
        params = current_params(s)
        return render(request, "vlak.html", {"t": t, "rec": rec, "positions": positions, "shifts": shifts,
                                             "handovers": handovers, "paths": paths, "history": history,
                                             "driver_link": link, "params": params}, user=user)


@router.get("/vlaky/{train_id}/upravit", response_class=HTMLResponse)
def train_edit(request: Request, train_id: int, user=Depends(operate)):
    with session_scope() as s:
        t = s.get(Train, train_id)
        if t is None:
            raise HTTPException(404, "Vlak neexistuje.")
        return render(request, "vlak_form.html", {"train": t, "values": _train_values_for_form(t), "errors": {},
                                                  **_train_choices(s)}, user=user)


@router.post("/vlaky/{train_id}/upravit")
def train_update(train_id: int, request: Request, background: BackgroundTasks, user=Depends(operate),
                 form=Depends(csrf_form)):
    reader = FormReader(form)
    with session_scope() as s:
        t = s.get(Train, train_id)
        if t is None:
            raise HTTPException(404, "Vlak neexistuje.")
        values = _read_train_form(reader, s, train_id)
        if not reader.ok:
            return render(request, "vlak_form.html", {"train": t, "values": reader.values(), "errors": reader.errors,
                                                      **_train_choices(s)}, user=user, status_code=422)
        changes = _diff(t, values)
        for k, v in values.items():
            setattr(t, k, v)
        if changes:
            audit(s, user.username, "train_update", "train", t.id, changes=changes)
        number = t.number
    if changes and request.app.state.auto_evaluate:
        background.add_task(evaluate_train_bg, train_id, f"úprava vlaku ({', '.join(sorted(changes))})")
    return redirect(f"/vlaky/{train_id}", f"Vlak {number} uložený." if changes else "Bez zmien.")


@router.post("/vlaky/{train_id}/prepocitat")
def train_recompute(train_id: int, request: Request, user=Depends(operate), form=Depends(csrf_form)):
    with session_scope() as s:
        if s.get(Train, train_id) is None:
            raise HTTPException(404, "Vlak neexistuje.")
    rec, error = evaluate_train_now(train_id, user.username)
    if error:
        return redirect(f"/vlaky/{train_id}", error, "chyba")
    if rec is None:
        return redirect(f"/vlaky/{train_id}", "Prepočet prebehol – vlak nemá odporúčanie (napr. je ukončený).",
                        "info")
    return redirect(f"/odporucania/{rec['id']}", f"Prepočítané: {rec['summary'] or rec['decision']}")


@router.post("/vlaky/{train_id}/odkaz", response_class=HTMLResponse)
def train_driver_link(train_id: int, request: Request, user=Depends(operate), form=Depends(csrf_form)):
    with session_scope() as s:
        t = s.get(Train, train_id)
        if t is None:
            raise HTTPException(404, "Vlak neexistuje.")
        if t.status in DONE_TRAIN_STATES:
            return redirect(f"/vlaky/{train_id}", "Pre ukončený alebo zrušený vlak sa odkaz nevytvára.", "chyba")
        link = driver_link(s, t)
        audit(s, user.username, "driver_link", "train", t.id, number=t.number, valid_h=24,
              version=token_version(s, t.id))
    return _train_detail(request, train_id, user, link=link)


@router.post("/vlaky/{train_id}/odkaz/zrusit")
def train_driver_link_revoke(train_id: int, request: Request, user=Depends(operate), form=Depends(csrf_form)):
    """Zruší všetky vydané odkazy pre rušňovodiča tohto vlaku (napr. stratený telefón, iný rušňovodič)."""
    with session_scope() as s:
        t = s.get(Train, train_id)
        if t is None:
            raise HTTPException(404, "Vlak neexistuje.")
        version = revoke_driver_links(s, t.id)
        audit(s, user.username, "driver_link_revoke", "train", t.id, number=t.number, version=version)
    return redirect(f"/vlaky/{train_id}", "Odkazy pre rušňovodiča boli zrušené. Ak treba, vytvorte nový odkaz.")


# ================================================================ VÝLUKY (M4)
def _read_closure_form(reader: FormReader) -> dict:
    v: dict = {}
    v["from_station"] = station_or_error(reader, "from_station", "Od dopravne")
    v["to_station"] = station_or_error(reader, "to_station", "Po dopravňu")
    v["kind"] = reader.choice("kind", "Druh", CLOSURE_KIND.keys(), "uplna")
    v["line_ref"] = reader.text("line_ref", "Číslo trate", maxlen=40)
    v["track"] = reader.text("track", "Koľaj", maxlen=40)
    v["start_utc"] = reader.local_dt("start_utc", "Začiatok", required=True)
    v["end_utc"] = reader.local_dt("end_utc", "Koniec", required=True)
    v["daily_from"] = reader.hhmm("daily_from", "Denne od")
    v["daily_to"] = reader.hhmm("daily_to", "Denne do")
    v["electric_only"] = reader.flag("electric_only") or v["kind"] == "trakcne_vedenie"
    v["speed_kmh"] = reader.number("speed_kmh", "Rýchlosť pri pomalej jazde [km/h]", minimum=5, maximum=160)
    v["status"] = reader.choice("status", "Stav", ("navrh", "overene"), "overene")
    v["external_ref"] = reader.text("external_ref", "Číslo rozkazu / výluky", maxlen=120)
    v["note"] = reader.text("note", "Poznámka", maxlen=2000)
    if v["start_utc"] and v["end_utc"] and v["end_utc"] <= v["start_utc"]:
        reader.errors["end_utc"] = "Koniec musí byť po začiatku."
    if bool(v["daily_from"]) != bool(v["daily_to"]):
        reader.errors["daily_to"] = "Denné opakovanie: zadajte čas od aj do (alebo nechajte obe prázdne)."
    if v["kind"] == "pomala_jazda" and v["speed_kmh"] is None and "speed_kmh" not in reader.errors:
        reader.errors["speed_kmh"] = "Pri pomalej jazde zadajte obmedzenú rýchlosť."
    if v["from_station"] and v["from_station"] == v["to_station"]:
        reader.errors["to_station"] = "Úsek musí mať dve rôzne dopravne."
    if reader.ok:
        probe = Closure(from_station=v["from_station"], to_station=v["to_station"])
        if not closure_edges(probe):
            reader.errors["to_station"] = (f"Úsek {v['from_station']} – {v['to_station']} sa v sieti nenašiel "
                                           f"(skontrolujte dopravne).")
    return v


@router.get("/vyluky", response_class=HTMLResponse)
def closures_page(request: Request, filter: str = "platne", user=Depends(view)):  # noqa: A002
    now = now_utc()
    with session_scope() as s:
        q = select(Closure)
        if filter == "aktivne":
            q = q.where(Closure.status != "zrusene", Closure.start_utc <= now, Closure.end_utc >= now)
        elif filter == "planovane":
            q = q.where(Closure.status != "zrusene", Closure.start_utc > now)
        elif filter == "zrusene":
            q = q.where(Closure.status == "zrusene")
        elif filter == "vsetky":
            pass
        else:
            filter = "platne"
            q = q.where(Closure.status != "zrusene", Closure.end_utc >= now)
        closures = list(s.scalars(q.order_by(Closure.start_utc).limit(500)))
        rows = []
        for c in closures:
            spans = closure_spans(c, now, now + timedelta(days=60)) if c.status != "zrusene" else []
            rows.append({"c": c, "active": any(a <= now <= b for a, b in spans),
                         "next": next((a for a, b in spans if a > now), None)})
        return render(request, "vyluky.html", {"rows": rows, "filter": filter}, user=user)


def _closure_values(c: Closure) -> dict:
    return {"from_station": c.from_station, "to_station": c.to_station, "kind": c.kind, "line_ref": c.line_ref,
            "track": c.track, "start_utc": input_dt(c.start_utc), "end_utc": input_dt(c.end_utc),
            "daily_from": c.daily_from, "daily_to": c.daily_to, "electric_only": "1" if c.electric_only else "",
            "speed_kmh": c.speed_kmh if c.speed_kmh is not None else "", "status": c.status,
            "external_ref": c.external_ref, "note": c.note}


@router.get("/vyluky/nova", response_class=HTMLResponse)
def closure_new(request: Request, user=Depends(operate)):
    return render(request, "vyluka_form.html", {"closure": None, "values": {"status": "overene", "kind": "uplna"},
                                                "errors": {}}, user=user)


@router.post("/vyluky/nova")
def closure_create(request: Request, background: BackgroundTasks, user=Depends(operate), form=Depends(csrf_form)):
    reader = FormReader(form)
    values = _read_closure_form(reader)
    if not reader.ok:
        return render(request, "vyluka_form.html", {"closure": None, "values": reader.values(),
                                                    "errors": reader.errors}, user=user, status_code=422)
    with session_scope() as s:
        c = Closure(**values, source="manual")
        s.add(c)
        s.flush()
        audit(s, user.username, "closure_create", "closure", c.id,
              **{k: (iso(v) if hasattr(v, "tzinfo") else v) for k, v in values.items()})
        cid = c.id
    if request.app.state.auto_evaluate:
        background.add_task(evaluate_all_bg, f"nová výluka #{cid}")
    return redirect("/vyluky", f"Výluka {values['from_station']} – {values['to_station']} uložená.")


@router.get("/vyluky/{closure_id}/upravit", response_class=HTMLResponse)
def closure_edit(request: Request, closure_id: int, user=Depends(operate)):
    with session_scope() as s:
        c = s.get(Closure, closure_id)
        if c is None:
            raise HTTPException(404, "Výluka neexistuje.")
        return render(request, "vyluka_form.html", {"closure": c, "values": _closure_values(c), "errors": {}},
                      user=user)


@router.post("/vyluky/{closure_id}/upravit")
def closure_update(closure_id: int, request: Request, background: BackgroundTasks, user=Depends(operate),
                   form=Depends(csrf_form)):
    reader = FormReader(form)
    values = _read_closure_form(reader)
    with session_scope() as s:
        c = s.get(Closure, closure_id)
        if c is None:
            raise HTTPException(404, "Výluka neexistuje.")
        if not reader.ok:
            return render(request, "vyluka_form.html", {"closure": c, "values": reader.values(),
                                                        "errors": reader.errors}, user=user, status_code=422)
        changes = _diff(c, values)
        if changes:
            for k, v in values.items():
                setattr(c, k, v)
            c.version = (c.version or 1) + 1
            audit(s, user.username, "closure_update", "closure", c.id, version=c.version, changes=changes)
    if changes and request.app.state.auto_evaluate:
        background.add_task(evaluate_all_bg, f"zmena výluky #{closure_id}")
    return redirect("/vyluky", "Výluka uložená." if changes else "Bez zmien.")


@router.post("/vyluky/{closure_id}/zrusit")
def closure_cancel(closure_id: int, request: Request, background: BackgroundTasks, user=Depends(operate),
                   form=Depends(csrf_form)):
    reason = str(form.get("dovod") or "").strip()[:500]
    with session_scope() as s:
        c = s.get(Closure, closure_id)
        if c is None:
            raise HTTPException(404, "Výluka neexistuje.")
        if c.status == "zrusene":
            return redirect("/vyluky", "Výluka už je zrušená.", "info")
        c.status = "zrusene"
        c.version = (c.version or 1) + 1
        if reason:
            c.note = (c.note + "\n" if c.note else "") + f"Zrušená: {reason}"
        audit(s, user.username, "closure_cancel", "closure", c.id, reason=reason, version=c.version)
    if request.app.state.auto_evaluate:
        background.add_task(evaluate_all_bg, f"zrušená výluka #{closure_id}")
    return redirect("/vyluky", "Výluka zrušená.")


@router.post("/vyluky/{closure_id}/overit")
def closure_verify(closure_id: int, request: Request, background: BackgroundTasks, user=Depends(operate),
                   form=Depends(csrf_form)):
    """Overenie výluky v stave „návrh“. Výluka z e-mailu sa potvrdí cez spracovanie pošty (potvrdí aj udalosť)."""
    back = safe_next(str(form.get("spat") or ""), "/vyluky")
    with session_scope() as s:
        c = s.get(Closure, closure_id)
        if c is None:
            raise HTTPException(404, "Výluka neexistuje.")
        if c.status != "navrh":
            return redirect(back, f"Výluka je v stave „{CLOSURE_STATUS.get(c.status, c.status)}“.", "info")
        event = s.get(ExtractedEvent, c.source_event_id) if c.source_event_id else None
        event_id = event.id if event is not None and event.confirmed_at is None and \
            event.status in ("navrh", "aplikovane") else None
        if event_id is None:
            c.status = "overene"
            c.version = (c.version or 1) + 1
            audit(s, user.username, "closure_verify", "closure", c.id, version=c.version)
    if event_id is not None:
        try:
            posta = importlib.import_module("vlaky.posta.spracovanie")
            with session_scope() as s:
                ev = posta.confirm_event(s, event_id, user.username)
                result = (ev.validation or {}).get("apply_result") or {}
                c = s.get(Closure, closure_id)
                if c is not None and c.status != "overene":
                    error = result.get("error") or "výluka zostala v stave návrh"
                    return redirect(f"/fronta#udalost-{event_id}", f"Výluku sa nepodarilo potvrdiť: {error}", "chyba")
                audit(s, user.username, "closure_verify", "closure", closure_id, event_id=event_id)
        except (ImportError, NotImplementedError, AttributeError):
            return redirect(back, "Spracovanie e-mailov (modul posta) zatiaľ nie je k dispozícii.", "chyba")
        except Exception as exc:  # noqa: BLE001
            log.exception("Potvrdenie výluky %s zlyhalo", closure_id)
            return redirect(back, f"Výluku sa nepodarilo potvrdiť: {str(exc)[:300]}", "chyba")
    if request.app.state.auto_evaluate:
        background.add_task(evaluate_all_bg, f"overená výluka #{closure_id}")
    return redirect(back, "Výluka potvrdená (overené).")


# ================================================================ HRANICE A PREBERANIE (M6)
@router.get("/hranice", response_class=HTMLResponse)
def borders_page(request: Request, user=Depends(view)):
    now = now_utc()
    with session_scope() as s:
        borders = list(s.scalars(select(BorderCrossing).order_by(BorderCrossing.name)))
        handovers = list(s.scalars(
            select(Handover).options(selectinload(Handover.border))
            .where(or_(Handover.status.in_(("ocakavany", "prisiel")), Handover.updated_at >= now - timedelta(hours=24)))
            .order_by(Handover.avizo_eta_utc, Handover.id)))
        train_ids = {h.train_id for h in handovers}
        trains_by_id = {t.id: t for t in s.scalars(select(Train).where(Train.id.in_(train_ids)))} if train_ids else {}
        active_trains = list(s.scalars(select(Train).where(Train.status.notin_(DONE_TRAIN_STATES))
                                       .order_by(Train.number)))
        return render(request, "hranice.html", {"borders": borders, "handovers": handovers, "trains": trains_by_id,
                                                "active_trains": active_trains}, user=user)


@router.post("/hranice/{border_id}")
def border_update(border_id: int, request: Request, user=Depends(borders_perm), form=Depends(csrf_form)):
    reader = FormReader(form)
    p50 = reader.number("dwell_p50_min", "Doba preberania P50", required=True, minimum=1, maximum=2880)
    p90 = reader.number("dwell_p90_min", "Doba preberania P90", required=True, minimum=1, maximum=2880)
    contact = reader.text("contact", "Kontakt", maxlen=1000)
    notes = reader.text("notes", "Poznámky", maxlen=4000)
    if p50 is not None and p90 is not None and p90 < p50:
        reader.errors["dwell_p90_min"] = "P90 nesmie byť menšie ako P50."
    if not reader.ok:
        return redirect(f"/hranice#hranica-{border_id}", " ".join(reader.errors.values()), "chyba")
    with session_scope() as s:
        b = s.get(BorderCrossing, border_id)
        if b is None:
            raise HTTPException(404, "Hraničný priechod neexistuje.")
        values = {"dwell_p50_min": float(p50), "dwell_p90_min": float(p90), "contact": contact, "notes": notes}
        changes = _diff(b, values)
        for k, v in values.items():
            setattr(b, k, v)
        if changes:
            audit(s, user.username, "border_update", "border_crossing", b.id, code=b.code, changes=changes)
        name = b.name
    return redirect(f"/hranice#hranica-{border_id}", f"Priechod {name} uložený." if changes else "Bez zmien.")


@router.post("/hranice/preberanie/nove")
def handover_new(request: Request, background: BackgroundTasks, user=Depends(operate), form=Depends(csrf_form)):
    reader = FormReader(form)
    train_id = reader.number("train_id", "Vlak", required=True, integer=True, minimum=1)
    border_id = reader.number("border_id", "Hraničný priechod", required=True, integer=True, minimum=1)
    direction = reader.choice("direction", "Smer", ("vstup", "vystup"), "vstup")
    eta = reader.local_dt("avizo_eta_utc", "Avízo – predpokladaný príchod")
    if not reader.ok:
        return redirect("/hranice#preberania", " ".join(reader.errors.values()), "chyba")
    with session_scope() as s:
        t, b = s.get(Train, int(train_id)), s.get(BorderCrossing, int(border_id))
        if t is None or b is None:
            return redirect("/hranice#preberania", "Neplatný vlak alebo priechod.", "chyba")
        h = Handover(train_id=t.id, border_id=b.id, direction=direction, avizo_eta_utc=eta, status="ocakavany",
                     updated_at=now_utc())
        s.add(h)
        s.flush()
        audit(s, user.username, "handover_create", "handover", h.id, train_id=t.id, border=b.code,
              direction=direction, avizo_eta=eta)
        tid = t.id
    if request.app.state.auto_evaluate:
        background.add_task(evaluate_train_bg, tid, "nové avízo / preberanie")
    return redirect("/hranice#preberania", "Preberanie zaevidované.")


def _handover_action(handover_id: int, user, mutate, message: str, request: Request, background: BackgroundTasks):
    with session_scope() as s:
        h = s.scalar(select(Handover).options(selectinload(Handover.border)).where(Handover.id == handover_id))
        if h is None:
            raise HTTPException(404, "Preberanie neexistuje.")
        error = mutate(s, h)
        if error:
            return redirect("/hranice#preberania", error, "chyba")
        h.updated_at = now_utc()
        tid = h.train_id
    if request.app.state.auto_evaluate:
        background.add_task(evaluate_train_bg, tid, message)
    return redirect("/hranice#preberania", message)


@router.post("/hranice/preberanie/{handover_id}/prisiel")
def handover_arrived(handover_id: int, request: Request, background: BackgroundTasks, user=Depends(operate),
                     form=Depends(csrf_form)):
    reader = FormReader(form)
    ts = reader.local_dt("cas", "Čas príchodu") or now_utc()
    if not reader.ok:
        return redirect("/hranice#preberania", " ".join(reader.errors.values()), "chyba")

    def mutate(s, h):
        if h.status not in ("ocakavany", "prisiel"):
            return "Vlak už bol prevzatý."
        h.arrived_utc, h.status = ts, "prisiel"
        audit(s, user.username, "handover_arrived", "handover", h.id, train_id=h.train_id, ts=ts)
        return ""

    return _handover_action(handover_id, user, mutate, "Príchod vlaku na hranicu zaznamenaný.", request, background)


@router.post("/hranice/preberanie/{handover_id}/prevzaty")
def handover_taken(handover_id: int, request: Request, background: BackgroundTasks, user=Depends(operate),
                   form=Depends(csrf_form)):
    reader = FormReader(form)
    ts = reader.local_dt("cas", "Čas prevzatia") or now_utc()
    findings = reader.text("findings", "Zistenia", maxlen=4000)
    if not reader.ok:
        return redirect("/hranice#preberania", " ".join(reader.errors.values()), "chyba")

    def mutate(s, h):
        if h.status in ("prevzaty", "odovzdany"):
            return "Preberanie už je ukončené."
        if h.arrived_utc is None:
            h.arrived_utc = ts
        h.taken_over_utc = ts
        h.status = "prevzaty" if h.direction == "vstup" else "odovzdany"
        if findings:
            h.findings = findings
        t = s.get(Train, h.train_id)
        if t is not None and t.status not in ("ukonceny", "zruseny"):
            t.status = "prevzaty" if h.direction == "vstup" else "odovzdany"
        audit(s, user.username, "handover_taken_over", "handover", h.id, train_id=h.train_id, ts=ts,
              findings=findings, direction=h.direction)
        return ""

    return _handover_action(handover_id, user, mutate, "Prevzatie vlaku zaznamenané.", request, background)


@router.post("/hranice/preberanie/{handover_id}/upravit")
def handover_update(handover_id: int, request: Request, background: BackgroundTasks, user=Depends(operate),
                    form=Depends(csrf_form)):
    reader = FormReader(form)
    eta = reader.local_dt("avizo_eta_utc", "Avízo – predpokladaný príchod")
    findings = reader.text("findings", "Zistenia", maxlen=4000)
    if not reader.ok:
        return redirect("/hranice#preberania", " ".join(reader.errors.values()), "chyba")

    def mutate(s, h):
        changes = _diff(h, {"avizo_eta_utc": eta, "findings": findings})
        h.avizo_eta_utc, h.findings = eta, findings
        if changes:
            audit(s, user.username, "handover_update", "handover", h.id, train_id=h.train_id, changes=changes)
        return ""

    return _handover_action(handover_id, user, mutate, "Preberanie uložené.", request, background)


# ================================================================ RUŠŇOVODIČI A ZMENY (M7)
def _assignment_proposal(s) -> tuple:
    """Návrh nasadenia rušňovodičov na 24 h (vlaky.planovanie.nasadenie) – len návrh, nič sa nemení."""
    try:
        mod = importlib.import_module("vlaky.planovanie.nasadenie")
        return mod.propose_driver_assignment(s), ""
    except (ImportError, AttributeError, NotImplementedError):
        return None, "Modul nasadenia rušňovodičov zatiaľ nie je k dispozícii."
    except Exception as exc:  # noqa: BLE001
        log.exception("Návrh nasadenia zlyhal")
        return None, f"Návrh nasadenia sa nepodarilo vypočítať: {type(exc).__name__}: {str(exc)[:200]}"


@router.get("/rusnovodici", response_class=HTMLResponse)
def drivers_page(request: Request, navrh: int = 0, user=Depends(view)):
    now = now_utc()
    with session_scope() as s:
        proposal, proposal_error = _assignment_proposal(s) if navrh and user.can("crew") else (None, "")
        params = current_params(s, now)
        drivers = list(s.scalars(select(Driver).order_by(Driver.active.desc(), Driver.name)))
        shifts = list(s.scalars(select(DriverShift).options(selectinload(DriverShift.driver))
                                .where(or_(DriverShift.status != "ukoncena",
                                           DriverShift.start_utc >= now - timedelta(hours=36)))
                                .order_by(case((DriverShift.status == "prebieha", 0),
                                               (DriverShift.status == "planovana", 1), else_=2),
                                          DriverShift.start_utc)))
        trains = {t.id: t for t in s.scalars(select(Train).where(Train.id.in_({sh.train_id for sh in shifts
                                                                               if sh.train_id})))}
        recs = latest_recommendations(s, list(trains)) if trains else {}
        rows = []
        for sh in shifts:
            start = ensure_utc(sh.start_utc)
            limit = start + timedelta(minutes=params["crew_max_duty_min"])
            left = (limit - now).total_seconds() / 60.0 if sh.status != "ukoncena" else None
            color = crew_color(left, params["crew_buffer_min"]) if sh.status == "prebieha" else "seda"
            rec = recs.get(sh.train_id)
            crew_info = (rec.crew or {}) if rec is not None else {}
            if sh.status == "prebieha" and crew_info.get("status") == "ČERVENÁ":
                color = "cervena"
            elif sh.status == "prebieha" and crew_info.get("status") == "ORANŽOVÁ" and color == "zelena":
                color = "oranzova"
            rows.append({"sh": sh, "train": trains.get(sh.train_id), "limit": limit, "left": left, "color": color,
                         "relieve_by": limit - timedelta(minutes=params["crew_post_min"]), "crew": crew_info})
        active_trains = list(s.scalars(select(Train).where(Train.status.notin_(DONE_TRAIN_STATES))
                                       .order_by(Train.number)))
        return render(request, "rusnovodici.html", {"drivers": drivers, "rows": rows, "params": params,
                                                    "active_trains": active_trains, "proposal": proposal,
                                                    "proposal_error": proposal_error,
                                                    "show_contacts": user.role != "obchod"}, user=user)


def _read_driver(reader: FormReader) -> dict:
    return {"name": reader.text("name", "Meno", required=True, maxlen=160),
            "phone": reader.text("phone", "Telefón", maxlen=40),
            "home_station": reader.text("home_station", "Domovská stanica", maxlen=120),
            "route_knowledge": reader.csv_list("route_knowledge"), "languages": reader.csv_list("languages"),
            "active": reader.flag("active")}


@router.post("/rusnovodici/novy")
def driver_create(request: Request, user=Depends(crew), form=Depends(csrf_form)):
    reader = FormReader(form)
    values = _read_driver(reader)
    values["active"] = True
    if not reader.ok:
        return redirect("/rusnovodici", " ".join(reader.errors.values()), "chyba")
    with session_scope() as s:
        d = Driver(**values)
        s.add(d)
        s.flush()
        audit(s, user.username, "driver_create", "driver", d.id, name=d.name)
    return redirect("/rusnovodici", f"Rušňovodič {values['name']} pridaný.")


@router.post("/rusnovodici/{driver_id}/upravit")
def driver_update(driver_id: int, request: Request, user=Depends(crew), form=Depends(csrf_form)):
    reader = FormReader(form)
    values = _read_driver(reader)
    if not reader.ok:
        return redirect("/rusnovodici", " ".join(reader.errors.values()), "chyba")
    with session_scope() as s:
        d = s.get(Driver, driver_id)
        if d is None:
            raise HTTPException(404, "Rušňovodič neexistuje.")
        changes = _diff(d, values)
        for k, v in values.items():
            setattr(d, k, v)
        if changes:
            audit(s, user.username, "driver_update", "driver", d.id, changes=changes)
    return redirect("/rusnovodici", "Rušňovodič uložený." if changes else "Bez zmien.")


def _read_shift(reader: FormReader, s) -> dict:
    driver_id = reader.number("driver_id", "Rušňovodič", required=True, integer=True, minimum=1)
    train_id = reader.number("train_id", "Vlak", integer=True, minimum=1)
    start = reader.local_dt("start_utc", "Nástup", required=True)
    planned_end = reader.local_dt("planned_end_utc", "Plánovaný koniec")
    status = reader.choice("status", "Stav", ("planovana", "prebieha", "ukoncena"), "planovana")
    if driver_id and s.get(Driver, int(driver_id)) is None:
        reader.errors["driver_id"] = "Neplatný rušňovodič."
    if train_id and s.get(Train, int(train_id)) is None:
        reader.errors["train_id"] = "Neplatný vlak."
    if start and planned_end and planned_end <= start:
        reader.errors["planned_end_utc"] = "Plánovaný koniec musí byť po nástupe."
    return {"driver_id": int(driver_id) if driver_id else None, "train_id": int(train_id) if train_id else None,
            "start_utc": start, "planned_end_utc": planned_end, "status": status}


@router.post("/zmeny/nova")
def shift_create(request: Request, background: BackgroundTasks, user=Depends(crew), form=Depends(csrf_form)):
    reader = FormReader(form)
    with session_scope() as s:
        values = _read_shift(reader, s)
        if not reader.ok:
            return redirect("/rusnovodici#zmeny", " ".join(reader.errors.values()), "chyba")
        sh = DriverShift(**values)
        s.add(sh)
        s.flush()
        audit(s, user.username, "shift_create", "driver_shift", sh.id,
              **{k: (iso(v) if hasattr(v, "tzinfo") else v) for k, v in values.items()})
        tid = sh.train_id
    if tid and request.app.state.auto_evaluate:
        background.add_task(evaluate_train_bg, tid, "nová zmena rušňovodiča")
    return redirect("/rusnovodici#zmeny", "Zmena uložená.")


@router.post("/zmeny/z-navrhu")
def shift_from_proposal(request: Request, background: BackgroundTasks, user=Depends(crew), form=Depends(csrf_form)):
    """Zmena podľa návrhu nasadenia (po potvrdení dispečerom/plánovačom). Údaje sa overia na serveri."""
    from .common import parse_iso

    reader = FormReader(form)
    driver_id = reader.number("driver_id", "Rušňovodič", required=True, integer=True, minimum=1)
    train_id = reader.number("train_id", "Vlak", required=True, integer=True, minimum=1)
    start, end = parse_iso(reader.raw("start")), parse_iso(reader.raw("end"))
    if start is None:
        reader.errors["start"] = "Neplatný začiatok zmeny."
    if not reader.ok:
        return redirect("/rusnovodici#zmeny", " ".join(reader.errors.values()), "chyba")
    with session_scope() as s:
        driver, train = s.get(Driver, int(driver_id)), s.get(Train, int(train_id))
        if driver is None or not driver.active or train is None or train.status in DONE_TRAIN_STATES:
            return redirect("/rusnovodici?navrh=1#navrh", "Rušňovodič alebo vlak už nie je k dispozícii.", "chyba")
        if s.scalar(select(DriverShift.id).where(DriverShift.train_id == train.id,
                                                 DriverShift.status.in_(("planovana", "prebieha")))):
            return redirect("/rusnovodici#zmeny", f"Vlak {train.number} už má naplánovanú zmenu.", "chyba")
        sh = DriverShift(driver_id=driver.id, train_id=train.id, start_utc=start,
                         planned_end_utc=end if end and end > start else None, status="planovana")
        s.add(sh)
        s.flush()
        audit(s, user.username, "shift_create_from_proposal", "driver_shift", sh.id, driver=driver.name,
              train=train.number, start=start, end=end)
        msg = f"Zmena pre {driver.name} na vlak {train.number} naplánovaná od {iso(start)}."
        tid = train.id
    if request.app.state.auto_evaluate:
        background.add_task(evaluate_train_bg, tid, "zmena podľa návrhu nasadenia")
    return redirect("/rusnovodici#zmeny", msg)


@router.post("/zmeny/{shift_id}/upravit")
def shift_update(shift_id: int, request: Request, background: BackgroundTasks, user=Depends(crew),
                 form=Depends(csrf_form)):
    reader = FormReader(form)
    with session_scope() as s:
        sh = s.get(DriverShift, shift_id)
        if sh is None:
            raise HTTPException(404, "Zmena neexistuje.")
        values = _read_shift(reader, s)
        if not reader.ok:
            return redirect("/rusnovodici#zmeny", " ".join(reader.errors.values()), "chyba")
        changes = _diff(sh, values)
        for k, v in values.items():
            setattr(sh, k, v)
        if changes:
            audit(s, user.username, "shift_update", "driver_shift", sh.id, changes=changes)
        tid = sh.train_id
    if changes and tid and request.app.state.auto_evaluate:
        background.add_task(evaluate_train_bg, tid, "úprava zmeny rušňovodiča")
    return redirect("/rusnovodici#zmeny", "Zmena uložená." if changes else "Bez zmien.")


@router.post("/zmeny/{shift_id}/nastup")
def shift_start(shift_id: int, request: Request, background: BackgroundTasks, user=Depends(crew),
                form=Depends(csrf_form)):
    with session_scope() as s:
        sh = s.get(DriverShift, shift_id)
        if sh is None:
            raise HTTPException(404, "Zmena neexistuje.")
        if sh.status == "ukoncena":
            return redirect("/rusnovodici#zmeny", "Zmena je už ukončená.", "chyba")
        old = sh.start_utc
        sh.start_utc, sh.status = now_utc(), "prebieha"
        audit(s, user.username, "shift_start", "driver_shift", sh.id, previous_start=old, start=sh.start_utc)
        tid = sh.train_id
    if tid and request.app.state.auto_evaluate:
        background.add_task(evaluate_train_bg, tid, "nástup rušňovodiča")
    return redirect("/rusnovodici#zmeny", "Nástup zaznamenaný – limit pracovného času beží od teraz.")


@router.post("/zmeny/{shift_id}/ukoncit")
def shift_end(shift_id: int, request: Request, user=Depends(crew), form=Depends(csrf_form)):
    with session_scope() as s:
        sh = s.get(DriverShift, shift_id)
        if sh is None:
            raise HTTPException(404, "Zmena neexistuje.")
        sh.actual_end_utc, sh.status = now_utc(), "ukoncena"
        audit(s, user.username, "shift_end", "driver_shift", sh.id, end=sh.actual_end_utc)
    return redirect("/rusnovodici#zmeny", "Zmena ukončená.")


# ================================================================ RUŠNE
@router.get("/rusne", response_class=HTMLResponse)
def locos_page(request: Request, user=Depends(view)):
    with session_scope() as s:
        locos = list(s.scalars(select(Locomotive).order_by(Locomotive.number)))
        return render(request, "rusne.html", {"locos": locos, "systems": LOCO_SYSTEMS}, user=user)


def _read_loco(reader: FormReader) -> dict:
    systems = [x for x in LOCO_SYSTEMS if reader.flag(f"sys_{x}")]
    return {"number": reader.text("number", "Číslo rušňa", required=True, maxlen=40),
            "type": reader.text("type", "Typ / rad", maxlen=80),
            "traction": reader.choice("traction", "Trakcia", ("E", "M"), "E"),
            "systems": systems, "owner": reader.text("owner", "Vlastník", maxlen=120),
            "telematics_id": reader.text("telematics_id", "ID telematiky", maxlen=120)}


@router.post("/rusne/novy")
def loco_create(request: Request, user=Depends(crew), form=Depends(csrf_form)):
    reader = FormReader(form)
    values = _read_loco(reader)
    if values["traction"] == "E" and not values["systems"]:
        reader.errors["systems"] = "Elektrický rušeň: vyberte aspoň jeden napájací systém."
    with session_scope() as s:
        if values["number"] and s.scalar(select(Locomotive).where(Locomotive.number == values["number"])):
            reader.errors["number"] = "Rušeň s týmto číslom už existuje."
        if not reader.ok:
            return redirect("/rusne", " ".join(reader.errors.values()), "chyba")
        loco = Locomotive(**values)
        s.add(loco)
        s.flush()
        audit(s, user.username, "locomotive_create", "locomotive", loco.id, **values)
    return redirect("/rusne", f"Rušeň {values['number']} pridaný.")


@router.post("/rusne/{loco_id}/upravit")
def loco_update(loco_id: int, request: Request, user=Depends(crew), form=Depends(csrf_form)):
    reader = FormReader(form)
    values = _read_loco(reader)
    if values["traction"] == "E" and not values["systems"]:
        reader.errors["systems"] = "Elektrický rušeň: vyberte aspoň jeden napájací systém."
    with session_scope() as s:
        loco = s.get(Locomotive, loco_id)
        if loco is None:
            raise HTTPException(404, "Rušeň neexistuje.")
        other = s.scalar(select(Locomotive).where(Locomotive.number == values["number"], Locomotive.id != loco_id))
        if other is not None:
            reader.errors["number"] = "Rušeň s týmto číslom už existuje."
        if not reader.ok:
            return redirect("/rusne", " ".join(reader.errors.values()), "chyba")
        changes = _diff(loco, values)
        for k, v in values.items():
            setattr(loco, k, v)
        if changes:
            audit(s, user.username, "locomotive_update", "locomotive", loco.id, changes=changes)
    return redirect("/rusne", "Rušeň uložený." if changes else "Bez zmien.")
