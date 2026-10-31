"""Stránky: živá mapa (dashboard), fronta na potvrdenie, odporúčania, odovzdávka služby."""
from __future__ import annotations

import importlib
import json
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..audit import audit
from ..cas import local, local_naive_to_utc, now_utc
from ..config import settings
from ..db import session_scope
from ..models import (Email, ExtractedEvent, Recommendation, ShiftHandoverReport, Train)
from .auth import csrf_form, require
from .common import EVENT_KIND, FormReader, REC_STATE, TRAIN_STATUS, log, parse_iso, redirect, render
from .data import alerts, auto_applied_unconfirmed, borders_list, train_rows
from .prepocet import evaluate_all_bg

router = APIRouter()
view = require("view")
operate = require("operate")


# ---------------------------------------------------------------- dashboard
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(view)):
    cfg = settings()
    with session_scope() as s:
        trains = train_rows(s)
        borders = borders_list(s)
        alert_list = alerts(s)
    config = {"tz": cfg.timezone, "staleMinutes": cfg.position_stale_minutes, "role": user.role,
              "canOperate": user.can("operate"), "borders": borders, "tileUrl": (cfg.map_tile_url or "").strip(),
              "tileAttribution": cfg.map_tile_attribution or "", "statusLabels": TRAIN_STATUS}
    return render(request, "dashboard.html", {"trains": trains, "borders": borders, "alerts": alert_list,
                                              "config": config, "wall": request.query_params.get("nastenka") == "1",
                                              "page_class": "page-dashboard"}, user=user)


# ---------------------------------------------------------------- fronta na potvrdenie (M5)
FIELD_LABELS = {
    "train_number": "Číslo vlaku", "train_numbers": "Čísla vlakov", "number": "Číslo", "from_station": "Od dopravne",
    "to_station": "Po dopravňu", "station": "Dopravňa", "stations": "Dopravne", "line_ref": "Číslo trate",
    "track": "Koľaj", "start_utc": "Začiatok", "end_utc": "Koniec", "start": "Začiatok", "end": "Koniec",
    "daily_from": "Denne od", "daily_to": "Denne do", "kind": "Druh", "closure_kind": "Druh výluky",
    "electric_only": "Len elektrická trakcia (vypnuté TV)", "speed_kmh": "Rýchlosť [km/h]",
    "border": "Hraničný priechod", "eta_utc": "Predpokladaný príchod",
    "arrival_utc": "Príchod", "departure_utc": "Odchod", "customer": "Zákazník", "origin": "Odkiaľ",
    "destination": "Kam", "deadline_utc": "Termín doručenia", "earliest_departure_utc": "Najskorší odchod",
    "cargo": "Tovar", "rid": "Nebezpečný tovar (RID)", "wagons": "Počet vozňov", "weight_t": "Hmotnosť [t]",
    "length_m": "Dĺžka [m]", "points": "Body trasy", "external_ref": "Číslo rozkazu / výluky",
    "reference": "Značka", "reason": "Dôvod", "description": "Popis", "note": "Poznámka", "run_date": "Dátum jazdy",
    "delay_min": "Meškanie [min]", "path_id": "Trasa", "partner": "Partner", "direction": "Smer",
    "eta": "Predpokladaný príchod (avízo)", "earliest_departure": "Najskorší odchod", "deadline": "Termín doručenia",
    "accept_by": "Akceptovať do", "date": "Dátum", "offer": "Ponuka trasy (treba akceptovať)",
    "until_revoked": "Do odvolania", "via": "Cez", "border_code": "Kód priechodu",
}
_ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM = re.compile(r"^\d{2}:\d{2}$")


def _field_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (list, dict)):
        return "json"
    if isinstance(value, str):
        if _ISO_DT.match(value.strip()):
            aware = value.strip().endswith("Z") or bool(re.search(r"[+-]\d{2}:?\d{2}$", value.strip()))
            return "datetime_aware" if aware else "datetime_naive"
        if _ISO_DATE.match(value.strip()):
            return "date"
        if _HHMM.match(value.strip()):
            return "time"
        return "textarea" if len(value) > 80 or "\n" in value else "text"
    return "text"


def event_fields(data: dict) -> list:
    out = []
    for key, value in (data or {}).items():
        if str(key).startswith("_"):
            continue
        kind = _field_kind(value)
        if kind == "datetime_aware":
            shown = local(parse_iso(value)).strftime("%Y-%m-%dT%H:%M") if parse_iso(value) else ""
        elif kind == "datetime_naive":
            shown = value.strip().replace(" ", "T")[:16]
        elif kind == "json":
            shown = json.dumps(value, ensure_ascii=False, indent=1)
        elif value is None:
            shown = ""
        else:
            shown = value
        out.append({"key": str(key), "label": FIELD_LABELS.get(str(key), str(key)), "kind": kind, "value": shown,
                    "original": value})
    return out


def parse_event_edits(form, data: dict) -> tuple:
    """Zmenené polia z formulára (len tie, ktoré sa líšia od extrakcie) + chyby."""
    edits: dict = {}
    errors: dict = {}
    for key, old in (data or {}).items():
        key = str(key)
        if key.startswith("_"):
            continue
        name = f"f__{key}"
        kind = _field_kind(old)
        if kind == "bool":
            if form.get(f"b__{key}") is None:
                continue
            new: Any = form.get(name) is not None
        else:
            if name not in form:
                continue
            raw = str(form.get(name) or "").strip()
            try:
                if kind == "number":
                    new = None if raw == "" else float(raw.replace(",", "."))
                    if isinstance(old, int) and new is not None and float(new).is_integer():
                        new = int(new)
                elif kind == "json":
                    new = json.loads(raw) if raw else ([] if isinstance(old, list) else {})
                elif kind == "datetime_aware":
                    new = local_naive_to_utc(datetime.strptime(raw, "%Y-%m-%dT%H:%M")).isoformat() if raw else None
                    if new is not None and parse_iso(old) == parse_iso(new):
                        continue
                elif kind == "datetime_naive":
                    new = (datetime.strptime(raw, "%Y-%m-%dT%H:%M").strftime(
                        "%Y-%m-%dT%H:%M:%S" if len(old.strip()) > 16 else "%Y-%m-%dT%H:%M") if raw else None)
                else:
                    new = raw if raw != "" or old is not None else None
            except (ValueError, json.JSONDecodeError):
                errors[key] = f"{FIELD_LABELS.get(key, key)}: neplatná hodnota."
                continue
        if new != old:
            edits[key] = new
    return edits, errors


def _queue_items(s) -> list:
    events = list(s.scalars(select(ExtractedEvent).where(ExtractedEvent.needs_confirmation.is_(True),
                                                         ExtractedEvent.status == "navrh")
                            .order_by(ExtractedEvent.created_at, ExtractedEvent.id).limit(50)))
    email_ids = {e.email_id for e in events if e.email_id}
    emails = {m.id: m for m in s.scalars(select(Email).options(selectinload(Email.attachments))
                                         .where(Email.id.in_(email_ids)))} if email_ids else {}
    return [{"event": e, "email": emails.get(e.email_id), "fields": event_fields(e.data or {})} for e in events]


def _auto_items(s) -> list:
    """Udalosti zapísané automaticky, ktorých výluka je stále „návrh“ – dispečer ich má overiť."""
    rows = auto_applied_unconfirmed(s)
    email_ids = {ev.email_id for ev, _c in rows if ev.email_id}
    emails = {m.id: m for m in s.scalars(select(Email).where(Email.id.in_(email_ids)))} if email_ids else {}
    return [{"event": ev, "closure": c, "email": emails.get(ev.email_id)} for ev, c in rows]


def _confirmable(ev: ExtractedEvent) -> bool:
    return ev.status == "navrh" or (ev.status == "aplikovane" and ev.confirmed_at is None)


@router.get("/fronta", response_class=HTMLResponse)
def queue_page(request: Request, user=Depends(view)):
    with session_scope() as s:
        items = _queue_items(s)
        auto_items = _auto_items(s)
        total = s.scalar(select(func.count(ExtractedEvent.id)).where(ExtractedEvent.needs_confirmation.is_(True),
                                                                      ExtractedEvent.status == "navrh")) or 0
        recent = list(s.scalars(select(ExtractedEvent).where(ExtractedEvent.status != "navrh",
                                                             ExtractedEvent.confirmed_at.is_not(None))
                                .order_by(ExtractedEvent.confirmed_at.desc()).limit(15)))
        return render(request, "fronta.html", {"items": items, "total": total + len(auto_items), "recent": recent,
                                               "auto_items": auto_items,
                                               "threshold": settings().confirm_threshold}, user=user)


def _posta():
    return importlib.import_module("vlaky.posta.spracovanie")


@router.post("/fronta/{event_id}/potvrdit")
def queue_confirm(event_id: int, request: Request, background: BackgroundTasks, user=Depends(operate),
                  form=Depends(csrf_form)):
    with session_scope() as s:
        ev = s.get(ExtractedEvent, event_id)
        if ev is None:
            raise HTTPException(404, "Udalosť neexistuje.")
        if not _confirmable(ev):
            return redirect("/fronta", "Udalosť už bola spracovaná.", "info")
        data = dict(ev.data or {})
        kind = ev.kind
    with_edits = str(form.get("akcia") or "") == "upravit"
    edits, errors = parse_event_edits(form, data) if with_edits else ({}, {})
    new_kind = str(form.get("event_kind") or "")
    if with_edits and new_kind and new_kind != kind:
        if new_kind not in EVENT_KIND:
            errors["event_kind"] = "Neznámy druh udalosti."
        else:
            edits["event_kind"] = new_kind
    if errors:
        return redirect(f"/fronta#udalost-{event_id}", "Opravte polia: " + "; ".join(errors.values()), "chyba")
    try:
        posta = _posta()
        with session_scope() as s:
            posta.confirm_event(s, event_id, user.username, edits=edits or None)
    except (ImportError, NotImplementedError, AttributeError):
        log.warning("Modul posta.spracovanie nie je k dispozícii")
        return redirect("/fronta", "Spracovanie e-mailov (modul posta) zatiaľ nie je k dispozícii – udalosť "
                                   "zostáva vo fronte.", "chyba")
    except Exception as exc:  # noqa: BLE001 – chyba spracovania sa ukáže dispečerovi, udalosť ostáva vo fronte
        log.exception("Potvrdenie udalosti %s zlyhalo", event_id)
        return redirect(f"/fronta#udalost-{event_id}", f"Udalosť sa nepodarilo potvrdiť: {str(exc)[:300]}", "chyba")
    if request.app.state.auto_evaluate:
        background.add_task(evaluate_all_bg, f"potvrdená udalosť z e-mailu #{event_id}")
    msg = "Udalosť potvrdená" + (f" s úpravami ({', '.join(edits)})." if edits else ".")
    return redirect("/fronta", msg)


@router.post("/fronta/{event_id}/zamietnut")
def queue_reject(event_id: int, request: Request, background: BackgroundTasks, user=Depends(operate),
                 form=Depends(csrf_form)):
    reason = str(form.get("dovod") or "").strip()[:1000]
    if not reason:
        return redirect(f"/fronta#udalost-{event_id}", "Pri zamietnutí uveďte dôvod.", "chyba")
    with session_scope() as s:
        ev = s.get(ExtractedEvent, event_id)
        if ev is None:
            raise HTTPException(404, "Udalosť neexistuje.")
        if not _confirmable(ev):
            return redirect("/fronta", "Udalosť už bola spracovaná.", "info")
    try:
        posta = _posta()
        with session_scope() as s:
            posta.reject_event(s, event_id, user.username, reason=reason)
    except (ImportError, NotImplementedError, AttributeError):
        return redirect("/fronta", "Spracovanie e-mailov (modul posta) zatiaľ nie je k dispozícii.", "chyba")
    except Exception as exc:  # noqa: BLE001
        log.exception("Zamietnutie udalosti %s zlyhalo", event_id)
        return redirect("/fronta", f"Udalosť sa nepodarilo zamietnuť: {str(exc)[:300]}", "chyba")
    if request.app.state.auto_evaluate:
        background.add_task(evaluate_all_bg, f"zamietnutá udalosť z e-mailu #{event_id}")
    return redirect("/fronta", "Udalosť zamietnutá.")


# ---------------------------------------------------------------- odporúčania (2.3, 2.4, M10)
@router.get("/odporucania", response_class=HTMLResponse)
def recommendations_page(request: Request, stav: str = "", farba: str = "", vlak: str = "", strana: int = 1,
                         user=Depends(view)):
    per_page = 50
    strana = max(1, strana)
    with session_scope() as s:
        q = select(Recommendation).options(selectinload(Recommendation.train))
        if stav in REC_STATE:
            q = q.where(Recommendation.state == stav)
        elif stav != "vsetky":
            q = q.where(Recommendation.state != "zastarane")
        if farba in ("ZELENÁ", "ORANŽOVÁ", "ČERVENÁ"):
            q = q.where(Recommendation.color == farba)
        if vlak.strip():
            q = q.join(Train, Train.id == Recommendation.train_id).where(Train.number.contains(vlak.strip()[:40]))
        total = s.scalar(select(func.count()).select_from(q.subquery())) or 0
        recs = list(s.scalars(q.order_by(Recommendation.created_at.desc(), Recommendation.id.desc())
                              .offset((strana - 1) * per_page).limit(per_page)))
        return render(request, "odporucania.html", {"recs": recs, "stav": stav, "farba": farba, "vlak": vlak,
                                                    "strana": strana, "pages": max(1, -(-total // per_page)),
                                                    "total": total}, user=user)


def _next_steps(rec: Recommendation) -> list:
    steps = []
    chosen = rec.chosen or {}
    proposal = rec.path_proposal or {}
    if rec.decision == "ESKALOVAŤ":
        steps.append("Kontaktovať ŽSR (iná trasa alebo čas) a zákazníka (nový termín).")
    if chosen.get("needs_new_path") or proposal.get("needs_new_path"):
        window = proposal.get("request_window")
        if window:
            steps.append("Objednať ad hoc trasu u ŽSR podľa návrhu (okno odchodu je uvedené pri návrhu trasy).")
        else:
            steps.append("Objednať novú (ad hoc) trasu u ŽSR podľa návrhu.")
    crew = rec.crew or {}
    if crew.get("status") in ("ORANŽOVÁ", "ČERVENÁ"):
        steps.append("Pripraviť výmenu rušňovodiča (vystriedať najneskôr v uvedenom čase).")
    if rec.decision == "ODSTAVIŤ":
        steps.append("Zabezpečiť odstavnú koľaj a uvoľniť rušňovodiča.")
    if rec.decision in ("POČKAŤ", "ODSTAVIŤ", "PRESMEROVAŤ", "ESKALOVAŤ"):
        steps.append("Informovať zákazníka o novom predpokladanom príchode.")
    if rec.color == "ORANŽOVÁ":
        steps.append("ORANŽOVÁ: vypraviť len po výslovnom potvrdení dispečera (zvážiť dôvody nižšie).")
    return steps


@router.get("/odporucania/{rec_id}", response_class=HTMLResponse)
def recommendation_detail(request: Request, rec_id: int, user=Depends(view)):
    with session_scope() as s:
        rec = s.scalar(select(Recommendation).options(selectinload(Recommendation.train))
                       .where(Recommendation.id == rec_id))
        if rec is None:
            raise HTTPException(404, "Odporúčanie neexistuje.")
        latest = s.scalar(select(Recommendation.id).where(Recommendation.train_id == rec.train_id)
                          .order_by(Recommendation.id.desc()))
        history = list(s.scalars(select(Recommendation).where(Recommendation.train_id == rec.train_id)
                                 .order_by(Recommendation.id.desc()).limit(10)))
        return render(request, "odporucanie.html", {
            "rec": rec, "train": rec.train, "latest_id": latest, "history": history, "steps": _next_steps(rec),
            "stale_minutes": settings().position_stale_minutes}, user=user)


DECISION_STATE = {"prijat": "prijate", "upravit": "upravene", "zamietnut": "zamietnute"}


@router.post("/odporucania/{rec_id}/rozhodnutie")
def recommendation_decide(rec_id: int, request: Request, user=Depends(operate), form=Depends(csrf_form)):
    reader = FormReader(form)
    action = reader.choice("akcia", "Akcia", DECISION_STATE.keys())
    note = reader.text("poznamka", "Poznámka", maxlen=2000)
    back = f"/odporucania/{rec_id}"
    if not reader.ok:
        return redirect(back, "Neplatná akcia.", "chyba")
    if action == "zamietnut" and not note:
        return redirect(back, "Pri zamietnutí uveďte dôvod.", "chyba")
    if action == "upravit" and not note:
        return redirect(back, "Pri úprave popíšte, čo meníte (napr. iný čas odchodu).", "chyba")
    with session_scope() as s:
        rec = s.get(Recommendation, rec_id)
        if rec is None:
            raise HTTPException(404, "Odporúčanie neexistuje.")
        if rec.state == "zastarane":
            return redirect(back, "Odporúčanie je zastarané – rozhodnite o aktuálnom odporúčaní pre vlak.", "chyba")
        previous = rec.state
        rec.state = DECISION_STATE[action]
        rec.decided_by = user.username
        rec.decided_at = now_utc()
        rec.dispatcher_note = note
        audit(s, user.username, f"recommendation_{action}", "recommendation", rec.id, train_id=rec.train_id,
              decision=rec.decision, color=rec.color, previous_state=previous, state=rec.state, note=note)
    text = {"prijat": "Odporúčanie prijaté.", "upravit": "Úprava odporúčania uložená.",
            "zamietnut": "Odporúčanie zamietnuté."}[action]
    return redirect(back, text)


# ---------------------------------------------------------------- odovzdávka služby (M12)
@router.get("/odovzdavka", response_class=HTMLResponse)
def handover_reports(request: Request, user=Depends(view)):
    with session_scope() as s:
        reports = list(s.scalars(select(ShiftHandoverReport).order_by(ShiftHandoverReport.created_at.desc())
                                 .limit(60)))
        return render(request, "odovzdavky.html", {"reports": reports}, user=user)


@router.post("/odovzdavka/nova")
def handover_create(request: Request, user=Depends(operate), form=Depends(csrf_form)):
    try:
        mod = importlib.import_module("vlaky.agent.odovzdavka")
        with session_scope() as s:
            try:
                rep = mod.create_report(s, notify=False)
            except TypeError:
                rep = mod.create_report(s)
            audit(s, user.username, "handover_report_create", "shift_handover_report", rep.id)
            rep_id = rep.id
    except (ImportError, NotImplementedError, AttributeError):
        log.warning("Modul odovzdávky nie je k dispozícii")
        return redirect("/odovzdavka", "Modul odovzdávky služby zatiaľ nie je k dispozícii.", "chyba")
    return redirect(f"/odovzdavka/{rep_id}", "Odovzdávka služby vytvorená. Doplňte ju a potvrďte.")


@router.get("/odovzdavka/{rep_id}", response_class=HTMLResponse)
def handover_detail(request: Request, rep_id: int, user=Depends(view)):
    with session_scope() as s:
        rep = s.get(ShiftHandoverReport, rep_id)
        if rep is None:
            raise HTTPException(404, "Odovzdávka neexistuje.")
        return render(request, "odovzdavka.html", {"rep": rep}, user=user)


@router.post("/odovzdavka/{rep_id}/potvrdit")
def handover_confirm(rep_id: int, request: Request, user=Depends(operate), form=Depends(csrf_form)):
    note = str(form.get("poznamka") or "").strip()[:5000]
    with session_scope() as s:
        rep = s.get(ShiftHandoverReport, rep_id)
        if rep is None:
            raise HTTPException(404, "Odovzdávka neexistuje.")
        rep.note = note
        rep.confirmed_by = user.username
        rep.confirmed_at = now_utc()
        audit(s, user.username, "handover_report_confirm", "shift_handover_report", rep.id, note=note)
    return redirect(f"/odovzdavka/{rep_id}", "Odovzdávka potvrdená.")
