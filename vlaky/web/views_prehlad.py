"""Stránky: živá mapa (dashboard), fronta na potvrdenie, odporúčania, odovzdávka služby."""
from __future__ import annotations

import importlib
import json
import re
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..audit import audit
from ..cas import ensure_utc, local, local_naive_to_utc, now_utc
from ..config import settings
from ..db import session_scope
from ..models import (Email, ExtractedEvent, Recommendation, ShiftHandoverReport, Train)
from .auth import csrf_form, require
from .common import (DONE_TRAIN_STATES, EVENT_KIND, FormReader, REC_STATE, TRAIN_STATUS, current_params, log,
                     parse_iso, redirect, render)
from .data import (alerts, auto_applied_unconfirmed, borders_list, capped_variants, crew_live, data_freshness,
                   display_color, is_current, latest_positions, semafor_cap, train_rows)
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


def confirmed_register_edits(val: dict) -> list:
    """Polia, ktoré pri potvrdení ostali podľa úprav dispečera v registri výluk (nie podľa e-mailu)."""
    return [FIELD_LABELS.get(k, k).lower() for k in (val.get("register_kept") or [])]


def _apply_failed(ev: ExtractedEvent) -> bool:
    """Potvrdená udalosť, ktorej zápis do registra zlyhal (napr. avízo pre vlak, ktorý ešte nie je v systéme)."""
    result = (ev.validation or {}).get("apply_result") if isinstance(ev.validation, dict) else None
    return ev.status == "potvrdene" and isinstance(result, dict) and result.get("ok") is False


def _failed_events(s, limit: int = 50) -> list:
    rows = s.scalars(select(ExtractedEvent).where(ExtractedEvent.status == "potvrdene")
                     .order_by(ExtractedEvent.created_at, ExtractedEvent.id).limit(500))
    return [e for e in rows if _apply_failed(e)][:limit]


def _register_notes(s, ev: ExtractedEvent) -> list:
    """Úpravy záznamu výluky, ktoré dispečer urobil v registri výluk – potvrdenie ich zachová (nie údaje z e-mailu)."""
    try:
        edits = _posta().register_edits(s, ev)
    except Exception:  # noqa: BLE001 – len informácia, fronta sa zobrazí aj bez nej
        log.exception("Úpravy v registri pre udalosť %s sa nepodarilo zistiť", ev.id)
        return []
    return [{"label": FIELD_LABELS.get(col, col), "mail": _shown(mail_value), "register": _shown(row_value)}
            for col, (mail_value, row_value) in edits.items()]


def _shown(value: Any) -> str:
    if isinstance(value, datetime):
        return local(value).strftime("%d.%m.%Y %H:%M")
    if isinstance(value, bool):
        return "áno" if value else "nie"
    if value is None or value == "":
        return "—"
    return str(value)


def _queue_items(s) -> list:
    events = list(s.scalars(select(ExtractedEvent).where(ExtractedEvent.needs_confirmation.is_(True),
                                                         ExtractedEvent.status == "navrh")
                            .order_by(ExtractedEvent.created_at, ExtractedEvent.id).limit(50)))
    events += _failed_events(s)
    email_ids = {e.email_id for e in events if e.email_id}
    emails = {m.id: m for m in s.scalars(select(Email).options(selectinload(Email.attachments))
                                         .where(Email.id.in_(email_ids)))} if email_ids else {}
    return [{"event": e, "email": emails.get(e.email_id), "fields": event_fields(e.data or {}),
             "failed": _apply_failed(e), "register": _register_notes(s, e)} for e in events]


def _auto_items(s) -> list:
    """Udalosti zapísané automaticky, ktorých výluka je stále „návrh“ – dispečer ich má overiť."""
    rows = auto_applied_unconfirmed(s)
    email_ids = {ev.email_id for ev, _c in rows if ev.email_id}
    emails = {m.id: m for m in s.scalars(select(Email).where(Email.id.in_(email_ids)))} if email_ids else {}
    return [{"event": ev, "closure": c, "email": emails.get(ev.email_id), "register": _register_notes(s, ev)}
            for ev, c in rows]


def _confirmable(ev: ExtractedEvent) -> bool:
    return ev.status == "navrh" or (ev.status == "aplikovane" and ev.confirmed_at is None) or _apply_failed(ev)


@router.get("/fronta", response_class=HTMLResponse)
def queue_page(request: Request, user=Depends(view)):
    with session_scope() as s:
        items = _queue_items(s)
        auto_items = _auto_items(s)
        total = s.scalar(select(func.count(ExtractedEvent.id)).where(ExtractedEvent.needs_confirmation.is_(True),
                                                                      ExtractedEvent.status == "navrh")) or 0
        total += sum(1 for it in items if it["failed"])
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
            ev = posta.confirm_event(s, event_id, user.username, edits=edits or None)
            val = ev.validation if isinstance(ev.validation, dict) else {}
            result = val.get("apply_result") or {}
            kept = confirmed_register_edits(val)
    except (ImportError, NotImplementedError, AttributeError):
        log.warning("Modul posta.spracovanie nie je k dispozícii")
        return redirect("/fronta", "Spracovanie e-mailov (modul posta) zatiaľ nie je k dispozícii – udalosť "
                                   "zostáva vo fronte.", "chyba")
    except Exception as exc:  # noqa: BLE001 – chyba spracovania sa ukáže dispečerovi, udalosť ostáva vo fronte
        log.exception("Potvrdenie udalosti %s zlyhalo", event_id)
        return redirect(f"/fronta#udalost-{event_id}", f"Udalosť sa nepodarilo potvrdiť: {str(exc)[:300]}", "chyba")
    if request.app.state.auto_evaluate:
        background.add_task(evaluate_all_bg, f"potvrdená udalosť z e-mailu #{event_id}")
    if result.get("ok") is False:
        # zápis zlyhal (napr. vlak ešte nie je v systéme) – udalosť ostáva vo fronte, po náprave sa potvrdí znova
        return redirect(f"/fronta#udalost-{event_id}", "Udalosť je potvrdená, ale do registra sa nezapísala: "
                        f"{str(result.get('error') or 'neznáma chyba')[:300]} Po odstránení príčiny ju potvrďte znova.",
                        "chyba")
    if result.get("action") == "nahradene":
        # staršia udalosť – záznam medzitým zmenila (napr. zrušila) novšia udalosť, ktorá má prednosť
        return redirect("/fronta", "Udalosť potvrdená, ale nič sa nezapísalo: "
                        f"{str(result.get('note') or 'záznam medzitým zmenila novšia udalosť.')[:300]}", "varovanie")
    msg = "Udalosť potvrdená" + (f" s úpravami ({', '.join(edits)})" if edits else "")
    if kept:
        msg += f"; ponechané úpravy z registra výluk ({', '.join(kept)})"
    return redirect("/fronta", msg + ".")


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
        # bez čerstvých dát agenta sa aktuálne (posledné) odporúčanie idúceho vlaku neukáže ako ZELENÁ – staršie
        # odporúčania si farbu ponechajú (záznam o tom, čo sa vtedy odporučilo)
        fresh = data_freshness(s)
        current: set = set()
        if fresh["stale"]:
            train_ids = {r.train_id for r in recs if r.train is not None and r.train.status not in DONE_TRAIN_STATES}
            if train_ids:
                current = set(s.scalars(select(func.max(Recommendation.id)).where(
                    Recommendation.train_id.in_(train_ids)).group_by(Recommendation.train_id)))
        return render(request, "odporucania.html", {"recs": recs, "stav": stav, "farba": farba, "vlak": vlak,
                                                    "strana": strana, "pages": max(1, -(-total // per_page)),
                                                    "total": total, "fresh": fresh, "current_ids": current},
                      user=user)


def _next_steps(rec: Recommendation, color: Optional[str] = None) -> list:
    """Kroky pre dispečera; color – farba, ako ju web ukazuje (bez čerstvých dát agenta nie ZELENÁ)."""
    color = color or rec.color
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
    if crew.get("unknown_start"):
        steps.append("Zadať skutočný nástup rušňovodiča (Rušňovodiči) – limit 13 h je zatiaľ len odhad.")
    if crew.get("status") in ("ORANŽOVÁ", "ČERVENÁ"):
        steps.append("Pripraviť výmenu rušňovodiča (vystriedať najneskôr v uvedenom čase).")
    if rec.decision == "ODSTAVIŤ":
        steps.append("Zabezpečiť odstavnú koľaj a uvoľniť rušňovodiča.")
    if rec.decision == "POČKAŤ" and chosen.get("until_entry"):
        # idúci vlak pred zatvoreným cieľom: čaká pred vjazdom, kým sa neuvoľní (bez času odchodu)
        steps.append("Počkať pred vjazdom do cieľa, kým sa neuvoľní vjazd; rušňovodiča vystriedať podľa limitu 13 h.")
    if rec.decision in ("POČKAŤ", "ODSTAVIŤ", "PRESMEROVAŤ", "ESKALOVAŤ"):
        steps.append("Informovať zákazníka o novom predpokladanom príchode.")
    running = rec.train is not None and rec.train.status == "v_jazde"
    if rec.decision == "POKRAČOVAŤ":
        # idúci vlak sa nevypravuje – pri vlaku v cieli (aj len podľa plánu) treba ukončiť jazdu, resp. overiť polohu
        if chosen.get("v_cieli") and chosen.get("prichod_v_cieli"):
            steps.append("Ukončiť jazdu vlaku (stav „ukončený“) – vlak je v cieli.")
        elif chosen.get("v_cieli"):
            steps.append("Overiť polohu vlaku (rušňovodič, ŽSR); po príchode do cieľa ukončiť jazdu (stav "
                         "„ukončený“).")
        if color == "ORANŽOVÁ":
            steps.append("ORANŽOVÁ: vlak je v jazde – preveriť dôvody nižšie (poloha, rušňovodič, výluky na trase).")
    elif color == "ORANŽOVÁ" and running:
        # idúci vlak sa nevypravuje (POČKAŤ pred cieľom, odstavenie či odklon počas jazdy)
        steps.append("ORANŽOVÁ: vlak je v jazde – odporúčanie vykonať až po výslovnom potvrdení dispečera "
                     "(zvážiť dôvody nižšie).")
    elif color == "ORANŽOVÁ":
        steps.append("ORANŽOVÁ: vypraviť len po výslovnom potvrdení dispečera (zvážiť dôvody nižšie).")
    return steps


@router.get("/odporucania/{rec_id}", response_class=HTMLResponse)
def recommendation_detail(request: Request, rec_id: int, user=Depends(view)):
    cfg = settings()
    now = now_utc()
    with session_scope() as s:
        rec = s.scalar(select(Recommendation).options(selectinload(Recommendation.train))
                       .where(Recommendation.id == rec_id))
        if rec is None:
            raise HTTPException(404, "Odporúčanie neexistuje.")
        latest = s.scalar(select(Recommendation.id).where(Recommendation.train_id == rec.train_id)
                          .order_by(Recommendation.id.desc()))
        history = list(s.scalars(select(Recommendation).where(Recommendation.train_id == rec.train_id)
                                 .order_by(Recommendation.id.desc()).limit(10)))
        fresh = data_freshness(s, now)
        current = is_current(rec, latest)
        # aktuálne odporúčanie vlaku bez čerstvých dát agenta: ZELENÁ sa ukáže ako ORANŽOVÁ (PROMPT 12); varianty
        # a okná obmedzeného semaforu (plánovač alebo web) sa nikde neukážu ako ZELENÉ
        color = display_color(rec, fresh, current)
        cap = semafor_cap(rec, fresh, current, cfg.position_stale_minutes)
        newest = next((h for h in history if h.id == latest), None)
        newest_color = display_color(newest, fresh, is_current(newest, latest))
        history_colors = {h.id: newest_color if h.id == latest else h.color for h in history}
        # aktuálne odporúčanie: pracovný čas rušňovodiča a vek dát k teraz, nie zmrazené čísla z prepočtu
        crew_now = crew_live(rec.crew, current_params(s, now), now) if current and rec.crew else None
        # rušňovodič z odporúčania ešte nenastúpil (naplánovaná zmena, nový rušňovodič po odstavení) alebo nie je
        # pridelený (potrebný nástup podľa plánu): limit 13 h mu ešte nebeží – žiadny odpočet (napr. „63 h 17 min“)
        crew = rec.crew or {}
        crew_start = parse_iso(crew.get("start"))
        crew_ref = now if current else (ensure_utc(rec.created_at) or now)  # staršie odporúčanie: čas prepočtu
        crew_waiting = bool(crew) and not crew.get("unknown_start") and (
            not crew.get("driver") or (crew_start is not None and crew_start > crew_ref))
        position_age = None
        if current:
            rows = latest_positions(s, [rec.train_id]).get(rec.train_id)
            if rows:
                position_age = round((now - ensure_utc(rows[0].ts_utc)).total_seconds() / 60.0, 1)
        return render(request, "odporucanie.html", {
            "rec": rec, "train": rec.train, "latest_id": latest, "history": history,
            "history_colors": history_colors, "steps": _next_steps(rec, color),
            "stale_minutes": cfg.position_stale_minutes, "fresh": fresh, "current": current,
            "agent_stale": bool(current and fresh["stale"]), "color": color, "downgraded": color != rec.color,
            "cap": cap, "variants": capped_variants(rec, cap), "crew_now": crew_now, "crew_waiting": crew_waiting,
            "position_age": position_age}, user=user)


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
            # bez odoslania (odovzdávku z webu dispečer doplní a potvrdí) – chyba pri zostavení sa nesmie zmeniť
            # na druhý pokus, ktorý by ju poslal všetkým dispečerom
            rep = mod.create_report(s, notify=False)
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
