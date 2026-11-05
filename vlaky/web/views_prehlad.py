"""Stránky: živá mapa (dashboard), fronta na potvrdenie, odporúčania, odovzdávka služby."""
from __future__ import annotations

import importlib
import json
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..audit import audit
from ..cas import ensure_utc, fmt, local, local_naive_to_utc, now_utc
from ..config import settings
from ..db import session_scope
from ..models import (Email, ExtractedEvent, Recommendation, ShiftHandoverReport, Train)
from .auth import csrf_form, require
from .common import (DONE_TRAIN_STATES, EVENT_KIND, FormReader, REC_STATE, TRAIN_STATUS, current_params, log,
                     parse_iso, redirect, render)
from .data import (alerts, auto_applied_unconfirmed, borders_list, capped_variants, crew_live, data_freshness,
                   display_color, is_current, latest_positions, semafor_cap, train_rows)
from .prepocet import evaluate_all_bg
from .common import iso, page_state, page_summary, state_response, wants_state
from .data import recommendation_status, worse_color

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
    "until_revoked": "Do odvolania", "via": "Cez", "border_code": "Kód priechodu", "zamer": "Zámer zmeny",
    "partial": "Zrušenie len časti výluky (termín neurčený)",
}
# zámer zmeny výluky (``vyluka_zmena.zamer`` z pravidiel) po slovensky – ako ho uvádza register (spracovanie)
ZAMER_LABELS = {"presun": "presun termínu", "skratenie": "skrátenie", "zmena_casu": "zmena času",
                "doplnenie": "doplnenie ďalšieho termínu", "predlzenie": "predĺženie"}
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


_SHOWN_DT = "%d.%m.%Y %H:%M"


def _point_text(p: Any) -> str:
    """Bod trasy na čítanie: „Žilina (príchod 14.11. 08:05, odchod 14.11. 08:10)“ v miestnom čase."""
    if not isinstance(p, dict):
        return str(p)
    times = [f"{label} {local(dt).strftime('%d.%m. %H:%M')}" for label, dt in
             (("príchod", parse_iso(p.get("arr"))), ("odchod", parse_iso(p.get("dep")))) if dt is not None]
    return str(p.get("station") or "?") + (f" ({', '.join(times)})" if times else "")


def _display(key: str, value: Any, kind: str) -> str:
    """Hodnota poľa udalosti na čítanie (rola, ktorá nepotvrdzuje): miestny čas ako vo formulári dispečera,
    slovenské názvy druhov, áno/nie – nie UTC ISO, interné kľúče či „True“."""
    from datetime import date

    from .common import CLOSURE_KIND, PATH_KIND

    if value is None or value == "" or value == []:
        return "—"
    if kind == "bool":
        return "áno" if value else "nie"
    if kind == "datetime_aware":
        dt = parse_iso(value)
        return local(dt).strftime(_SHOWN_DT) if dt else str(value)
    if kind == "datetime_naive":
        try:
            return datetime.strptime(value.strip().replace(" ", "T")[:16], "%Y-%m-%dT%H:%M").strftime(_SHOWN_DT)
        except ValueError:
            return str(value)
    if kind == "date":
        try:
            return date.fromisoformat(value.strip()).strftime("%d.%m.%Y")
        except ValueError:
            return str(value)
    if kind == "number":
        return f"{value:g}".replace(".", ",") if isinstance(value, float) else str(value)
    if key == "kind":
        return CLOSURE_KIND.get(str(value)) or PATH_KIND.get(str(value)) or f"{value} (neznámy druh – overte)"
    if key == "zamer":
        return ZAMER_LABELS.get(str(value), str(value))
    if key == "points" and isinstance(value, list):
        return "; ".join(_point_text(p) for p in value)
    if kind == "json":
        return json.dumps(value, ensure_ascii=False)
    return str(value)


_PATH_EVENTS = ("trasa_pridelena", "trasa_zmena")


def _kind_options(event_kind: str, value: Any) -> list:
    """Voľby poľa „Druh“ formulára fronty (kód, slovenský názov): druh obmedzenia výluky alebo druh trasy – nie voľný
    text (zapísaný neznámy druh by plánovač nepoznal). Neznáma hodnota z e-mailu je prvá voľba s upozornením."""
    from .common import CLOSURE_KIND, PATH_KIND

    known = PATH_KIND if event_kind in _PATH_EVENTS else CLOSURE_KIND
    out = [(k, v) for k, v in known.items()]
    cur = "" if value is None else str(value)
    if event_kind == "vyluka_zrusenie" or cur == "":
        out.insert(0, ("", "— neuvedený —"))
    if cur and cur not in known:
        out.insert(0, (cur, f"{cur} – neznámy druh, vyberte zo zoznamu"))
    return out


def _zamer_options(value: Any) -> list:
    """Voľby poľa „Zámer zmeny“ (zmena výluky): kód zámeru → slovenský názov, prázdna voľba = zámer neurčený;
    neznáma hodnota z e-mailu je prvá voľba s upozornením (ako pri druhu – :func:`_kind_options`)."""
    from ..posta.validacia import change_intent

    out = [("", "— neurčený —")] + list(ZAMER_LABELS.items())
    cur = change_intent({"zamer": value})
    if cur and cur not in ZAMER_LABELS:
        out.insert(0, (str(value), f"{value} – neznámy zámer, vyberte zo zoznamu"))
    return out


def event_fields(data: dict, event_kind: str = "") -> list:
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
        item = {"key": str(key), "label": FIELD_LABELS.get(str(key), str(key)), "kind": kind, "value": shown,
                "original": value, "display": _display(str(key), value, kind)}
        if str(key) == "kind" and kind in ("text", "textarea") or (str(key) == "kind" and value is None):
            item["kind"] = "choice"
            item["value"] = "" if value is None else str(value)
            item["options"] = _kind_options(event_kind, value)
        if str(key) == "zamer" and (kind in ("text", "textarea") or value is None):
            # zámer zmeny výluky – výber s názvami po slovensky (voľný text by register nemusel rozpoznať)
            from ..posta.validacia import change_intent

            cur = change_intent({"zamer": value})
            item["kind"] = "choice"
            item["value"] = cur if cur in ZAMER_LABELS else ("" if value is None else str(value))
            item["options"] = _zamer_options(value)
        out.append(item)
    return out


def _kind_edit(raw: str, event_kind: str) -> tuple:
    """Druh z formulára fronty → (kód, chyba): slovenský názov či variant („úplná výluka“, „Uplna“) sa prevedie na
    kód, neznámy druh sa odmietne (FE0 – zapísaný by ho plánovač nepoznal)."""
    from .common import CLOSURE_KIND, PATH_KIND

    if raw == "":
        return None, ""
    if event_kind in _PATH_EVENTS:
        return (raw, "") if raw in PATH_KIND else (None, f"Druh: neznámy druh trasy „{raw[:40]}“ – vyberte zo zoznamu.")
    try:
        code = _posta().restriction_code(raw)
    except (ImportError, AttributeError):
        code = raw if raw in CLOSURE_KIND else None
    if code is None:
        return None, f"Druh: neznámy druh obmedzenia „{raw[:40]}“ – vyberte zo zoznamu (úplná výluka, výluka koľaje, …)."
    return code, ""


_ISO_DAY = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b(?![T:\d])")


def local_dates(text: Any) -> str:
    """Dátumy „RRRR-MM-DD“ v texte upozornenia ako „DD.MM.RRRR“ (aj v staršie uložených upozorneniach)."""
    return _ISO_DAY.sub(lambda m: f"{m.group(3)}.{m.group(2)}.{m.group(1)}", str(text))


# stav spracovanej udalosti (tabuľka „Naposledy spracované“)
EVENT_STATE = {"aplikovane": "zapísaná do registra", "zamietnute": "zamietnutá", "potvrdene": "potvrdená",
               "navrh": "čaká na potvrdenie"}


def parse_event_edits(form, data: dict, event_kind: str = "") -> tuple:
    """Zmenené polia z formulára (len tie, ktoré sa líšia od extrakcie) + chyby. Druh (``kind``) len známy kód – aj
    zo slovenského názvu (:func:`_kind_edit`)."""
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
            if key == "kind" and isinstance(new, str) and new != (old or ""):
                code, error = _kind_edit(new, event_kind)
                if error:
                    errors[key] = error
                    continue
                new = code if code is not None or old is not None else None
            if key == "zamer":
                from ..posta.validacia import change_intent

                code = change_intent({"zamer": new})
                if code == change_intent({"zamer": old}):
                    continue                     # ten istý zámer (aj zápis „zmena času“ ako „zmena_casu“)
                if code and code not in ZAMER_LABELS:
                    errors[key] = f"{FIELD_LABELS['zamer']}: neznámy zámer „{str(new)[:40]}“ – vyberte zo zoznamu."
                    continue
                new = code or None
        if new != old:
            edits[key] = new
    d = data or {}
    if "track" in d and ("kind" in edits or "track" in edits) and "kind" not in errors:
        # výluka koľaje bez koľaje (FE1): druh zmenený na „výluka koľaje“ či vymazaná koľaj – bez koľaje nie je známe, či
        # ostáva zjazdná iná koľaj (plánovač by mohol pustiť vlak cez úplne uzavretý úsek); dispečer koľaj doplní
        kind_now = edits.get("kind", d.get("kind"))
        track_now = edits.get("track", d.get("track"))
        if kind_now == "kolaj" and not str(track_now or "").strip():
            errors["track"] = ("Koľaj: pri výluke koľaje uveďte koľaj (napr. 1 alebo 2) – bez nej nie je známe, či "
                               "ostáva zjazdná iná koľaj.")
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


QUEUE_PAGE = 50          # udalostí s celým formulárom na stranu fronty
QUEUE_COMPACT_MAX = 1000  # ďalšie čakajúce udalosti v krátkom zozname (kotva #udalost-N, odkaz na celý formulár)


def _pending_ids(s) -> list:
    """Všetky čakajúce udalosti fronty v poradí fronty (najstaršie prvé) a potvrdené so zlyhaným zápisom."""
    ids = list(s.scalars(select(ExtractedEvent.id).where(ExtractedEvent.needs_confirmation.is_(True),
                                                         ExtractedEvent.status == "navrh")
                         .order_by(ExtractedEvent.created_at, ExtractedEvent.id)))
    seen = set(ids)
    return ids + [e.id for e in _failed_events(s, limit=500) if e.id not in seen]


def _full_items(s, ids: list) -> list:
    if not ids:
        return []
    events = {e.id: e for e in s.scalars(select(ExtractedEvent).where(ExtractedEvent.id.in_(ids)))}
    events = [events[i] for i in ids if i in events]
    email_ids = {e.email_id for e in events if e.email_id}
    emails = {m.id: m for m in s.scalars(select(Email).options(selectinload(Email.attachments))
                                         .where(Email.id.in_(email_ids)))} if email_ids else {}
    return [{"event": e, "email": emails.get(e.email_id), "fields": event_fields(e.data or {}, e.kind),
             "failed": _apply_failed(e), "register": _register_notes(s, e), "issues": _issues(e),
             "failure": _failure(e), "candidates": _candidates(s, e)} for e in events]


def _candidates(s, ev: ExtractedEvent) -> list:
    """Výluky v registri s číslom udalosti bez úseku – návrh pre dispečera (úsek sa z registra nedopĺňa automaticky)."""
    try:
        return _posta().register_candidates(s, ev)
    except Exception:  # noqa: BLE001 – len návrh, fronta sa zobrazí aj bez neho
        log.exception("Návrhy výluk z registra pre udalosť %s sa nepodarilo zistiť", ev.id)
        return []


_CONFIDENCE = re.compile(r"\b(istota|prahom|prah)\s+(\d+)\.(\d+)\b")


def _issues(ev: ExtractedEvent) -> dict:
    """Chyby a upozornenia udalosti na zobrazenie – dátumy v slovenskom zápise a istota s desatinnou čiarkou („istota
    0,50 je pod prahom 0,80“), aj v starších uložených textoch."""
    val = ev.validation if isinstance(ev.validation, dict) else {}
    return {key: [_CONFIDENCE.sub(r"\1 \2,\3", local_dates(x)) for x in val.get(key) or []]
            for key in ("errors", "chyby", "blocking", "warnings", "varovania", "issues", "confirmation_reasons")}


def _failure(ev: ExtractedEvent) -> dict:
    """Potvrdená udalosť so zlyhaným zápisom: chyba a rada pre dispečera (odmietnutý zápis čiastočného zrušenia hovorí
    sám, čo robiť – nie „po odstránení príčiny ju potvrďte znova“; „založenie vlaku“ len pri avíze)."""
    if not _apply_failed(ev):
        return {}
    result = (ev.validation or {}).get("apply_result") or {}
    error = local_dates(result.get("error") or "neznáma chyba")
    if result.get("retry") is False:
        advice = ""
    elif ev.kind == "avizo":
        advice = "Po odstránení príčiny (napr. založení vlaku) ju potvrďte znova, alebo ju zamietnite."
    else:
        advice = "Po odstránení príčiny ju potvrďte znova, alebo ju zamietnite."
    return {"error": error, "advice": advice}


def _compact_items(s, ids: list) -> list:
    """Krátke položky ostatných čakajúcich udalostí (popis, e-mail, poznámky) – bez tela e-mailu a formulára polí."""
    if not ids:
        return []
    events = {e.id: e for e in s.scalars(select(ExtractedEvent).where(ExtractedEvent.id.in_(ids)))}
    events = [events[i] for i in ids if i in events]
    email_ids = {e.email_id for e in events if e.email_id}
    emails = {m.id: m for m in s.scalars(select(Email).where(Email.id.in_(email_ids)))} if email_ids else {}
    try:
        describe = _posta().describe_event
    except Exception:  # noqa: BLE001 – bez modulu posta len druh a číslo
        describe = None
    out = []
    for e in events:
        val = e.validation if isinstance(e.validation, dict) else {}
        out.append({"event": e, "email": emails.get(e.email_id), "failed": _apply_failed(e),
                    "summary": describe(e) if describe else f"{EVENT_KIND.get(e.kind, e.kind)} (#{e.id})",
                    "reasons": [_CONFIDENCE.sub(r"\1 \2,\3", local_dates(x))
                                for x in list(val.get("confirmation_reasons") or [])[:3]]})
    return out


def _queue_items(s, strana: int = 1, udalost: Optional[int] = None) -> dict:
    """Fronta po stranách: celé formuláre pre stranu ``strana``, udalosť ``udalost`` (odkaz z upozornenia) vždy
    s celým formulárom navrchu a ostatné čakajúce udalosti v krátkom zozname – každá udalosť fronty je na stránke
    (kotva #udalost-N) a dá sa potvrdiť či zamietnuť, aj keď je vo fronte viac ako jedna strana."""
    ids = _pending_ids(s)
    pages = max(1, -(-len(ids) // QUEUE_PAGE))
    strana = min(max(1, strana), pages)
    page_ids = ids[(strana - 1) * QUEUE_PAGE:strana * QUEUE_PAGE]
    focus = udalost if udalost in ids and udalost not in page_ids else None
    full = _full_items(s, ([focus] if focus else []) + page_ids)
    if focus and full:
        full[0]["focus"] = True
    on_page = set(page_ids) | {focus}
    rest = [i for i in ids if i not in on_page]
    return {"items": full, "compact": _compact_items(s, rest[:QUEUE_COMPACT_MAX]), "compact_more": max(0, len(rest)
            - QUEUE_COMPACT_MAX), "page": strana, "pages": pages, "pending": len(ids), "per_page": QUEUE_PAGE}


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
def queue_page(request: Request, strana: int = 1, udalost: Optional[int] = None, user=Depends(view)):
    with session_scope() as s:
        queue = _queue_items(s, strana, udalost)
        auto_items = _auto_items(s)
        recent = list(s.scalars(select(ExtractedEvent).where(ExtractedEvent.status != "navrh",
                                                             ExtractedEvent.confirmed_at.is_not(None))
                                .order_by(ExtractedEvent.confirmed_at.desc()).limit(15)))
        return render(request, "fronta.html", {**queue, "total": queue["pending"] + len(auto_items), "recent": recent,
                                               "auto_items": auto_items, "event_state": _recent_state,
                                               "threshold": settings().confirm_threshold}, user=user)


def _recent_state(ev: ExtractedEvent) -> str:
    """Stav spracovanej udalosti po slovensky (tabuľka „Naposledy spracované“), aj s rozdelením či nahradením."""
    result = (ev.validation or {}).get("apply_result") if isinstance(ev.validation, dict) else None
    action = result.get("action") if isinstance(result, dict) else None
    if ev.status == "aplikovane" and action == "nahradene":
        return "potvrdená – nič sa nezapísalo (nahradila ju novšia)"
    if ev.status == "aplikovane" and action == "rozdelene":
        return "zapísaná do registra (výluka rozdelená)"
    if ev.status == "aplikovane" and action == "bez_zmeny":
        return "potvrdená – register bez zmeny"
    if _apply_failed(ev):
        return "potvrdená – zápis zlyhal"
    return EVENT_STATE.get(ev.status, ev.status)


@router.get("/fronta/udalost/{event_id}")
def queue_event(event_id: int, user=Depends(view)):
    """Odkaz na jednu udalosť (z upozornenia e-mailom): čakajúca udalosť sa ukáže s celým formulárom navrchu fronty
    (aj keď je vo fronte viac strán), automaticky zapísaná nepotvrdená v časti „Zapísané automaticky“, spracovaná
    udalosť – fronta s informáciou, ako bola spracovaná (kto ju potvrdil či zamietol, či ju nahradila novšia)."""
    with session_scope() as s:
        ev = s.get(ExtractedEvent, event_id)
        if ev is None:
            return redirect("/fronta", f"Udalosť #{event_id} neexistuje.", "chyba")
        if (ev.status == "navrh" and bool(ev.needs_confirmation)) or _apply_failed(ev):
            return redirect(_event_url(event_id))
        if ev.status == "aplikovane" and ev.confirmed_at is None:
            return redirect(f"/fronta#udalost-{event_id}")
        return redirect("/fronta", _processed_text(ev), "info")


def _processed_text(ev: ExtractedEvent) -> str:
    """„Udalosť #N už bola spracovaná – zamietnutá (disp2, 16.11. 10:30).“ – kto a ako ju spracoval."""
    try:
        state = _posta().processed_state(ev)
    except Exception:  # noqa: BLE001 – bez modulu posta len stav
        state = EVENT_STATE.get(ev.status, ev.status)
    return f"Udalosť #{ev.id} už bola spracovaná – {state}."


def _event_url(event_id: int) -> str:
    """Fronta s celým formulárom udalosti (aj keď je udalosť na inej strane fronty) – návrat po chybe potvrdenia."""
    return f"/fronta?udalost={event_id}#udalost-{event_id}"


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
            return redirect("/fronta", _processed_text(ev), "info")
        data = dict(ev.data or {})
        kind = ev.kind
    with_edits = str(form.get("akcia") or "") == "upravit"
    new_kind = str(form.get("event_kind") or "")
    edits, errors = parse_event_edits(form, data, new_kind if with_edits and new_kind in EVENT_KIND else kind) \
        if with_edits else ({}, {})
    if with_edits and new_kind and new_kind != kind:
        if new_kind not in EVENT_KIND:
            errors["event_kind"] = "Neznámy druh udalosti."
        else:
            edits["event_kind"] = new_kind
    if errors:
        return redirect(_event_url(event_id), "Opravte polia: " + "; ".join(errors.values()), "chyba")
    pick = str(form.get("kandidat") or "").strip()
    if pick:
        # dispečer vybral výluku z registra s číslom udalosti (návrh fronty) – úsek a trať podľa nej, ak ich sám
        # neopravil; termín nie (register má pôvodný termín, nie termín zmeny)
        try:
            with session_scope() as s:
                ev = s.get(ExtractedEvent, event_id)
                chosen = _posta().candidate_edits(s, ev, pick) if ev is not None else {}
        except (ImportError, AttributeError):
            chosen = {}
        if not chosen:
            return redirect(_event_url(event_id), "Vybraná výluka z registra už nie je medzi návrhmi (medzitým sa "
                                                  "zmenila alebo zrušila) – vyberte ju znova alebo doplňte úsek.",
                            "chyba")
        for k, v in chosen.items():
            edits.setdefault(k, v)
    try:
        posta = _posta()
        with session_scope() as s:
            ev = posta.confirm_event(s, event_id, user.username, edits=edits or None, pending_only=True)
            val = ev.validation if isinstance(ev.validation, dict) else {}
            result = val.get("apply_result") or {}
            kept = confirmed_register_edits(val)
    except (ImportError, NotImplementedError, AttributeError):
        log.warning("Modul posta.spracovanie nie je k dispozícii")
        return redirect("/fronta", "Spracovanie e-mailov (modul posta) zatiaľ nie je k dispozícii – udalosť "
                                   "zostáva vo fronte.", "chyba")
    except Exception as exc:  # noqa: BLE001 – chyba spracovania sa ukáže dispečerovi, udalosť ostáva vo fronte
        if _processed(exc):
            # súbežne ju potvrdil alebo zamietol iný dispečer – nič sa nezmenilo
            return redirect("/fronta", str(exc)[:400], "info")
        log.exception("Potvrdenie udalosti %s zlyhalo", event_id)
        return redirect(_event_url(event_id), f"Udalosť sa nepodarilo potvrdiť: {str(exc)[:300]}", "chyba")
    if request.app.state.auto_evaluate:
        background.add_task(evaluate_all_bg, f"potvrdená udalosť z e-mailu #{event_id}")
    if result.get("ok") is False:
        # zápis zlyhal (napr. vlak ešte nie je v systéme) – udalosť ostáva vo fronte; odmietnutý zápis (napr. zrušenie
        # len časti výluky, ktorú nemožno rozdeliť) sám hovorí, čo robiť – bez rady „potvrďte znova“
        error = local_dates(str(result.get("error") or "neznáma chyba"))[:600]
        tail = "" if result.get("retry") is False else " Po odstránení príčiny ju potvrďte znova."
        return redirect(_event_url(event_id), f"Udalosť je potvrdená, ale do registra sa nezapísala: {error}{tail}",
                        "chyba")
    if result.get("action") == "nahradene":
        # staršia udalosť – záznam medzitým zmenila (napr. zrušila) novšia udalosť, ktorá má prednosť
        return redirect("/fronta", "Udalosť potvrdená, ale nič sa nezapísalo: "
                        f"{str(result.get('note') or 'záznam medzitým zmenila novšia udalosť.')[:300]}", "varovanie")
    msg = "Udalosť potvrdená" + (f" s úpravami ({', '.join(edits)})" if edits else "")
    if result.get("action") == "rozdelene":
        parts = ", ".join(f"#{t.get('id')}" for t in result.get("terms") or [])
        msg += f"; výluka #{result.get('id')} je rozdelená ({parts}) – zmena či zrušenie sa týka len uvedenej časti"
    elif result.get("action") == "bez_zmeny":
        note = str(result.get("note") or "")[:300]
        msg += f"; register sa nezmenil – {note[:1].lower() + note[1:]}".rstrip(" –")
    if kept:
        msg += f"; ponechané úpravy z registra výluk ({', '.join(kept)})"
    return redirect("/fronta", msg + ("" if msg.endswith(".") else "."))


def _processed(exc: Exception) -> bool:
    """Výnimka hovorí, že udalosť medzitým spracoval niekto iný (posta.spracovanie.EventProcessed)."""
    return type(exc).__name__ == "EventProcessed"


@router.post("/fronta/{event_id}/zamietnut")
def queue_reject(event_id: int, request: Request, background: BackgroundTasks, user=Depends(operate),
                 form=Depends(csrf_form)):
    reason = str(form.get("dovod") or "").strip()[:1000]
    if not reason:
        return redirect(_event_url(event_id), "Pri zamietnutí uveďte dôvod.", "chyba")
    with session_scope() as s:
        ev = s.get(ExtractedEvent, event_id)
        if ev is None:
            raise HTTPException(404, "Udalosť neexistuje.")
        if not _confirmable(ev):
            return redirect("/fronta", _processed_text(ev), "info")
    try:
        posta = _posta()
        with session_scope() as s:
            posta.reject_event(s, event_id, user.username, reason=reason, pending_only=True)
    except (ImportError, NotImplementedError, AttributeError):
        return redirect("/fronta", "Spracovanie e-mailov (modul posta) zatiaľ nie je k dispozícii.", "chyba")
    except Exception as exc:  # noqa: BLE001
        if _processed(exc):
            return redirect("/fronta", str(exc)[:400], "info")
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
        # odtlačok zoznamu pre živú obnovu (static/js/app.js): po výpadku agenta sa zelené aktuálne odporúčania
        # zmenia na ORANŽOVÉ aj v otvorenom zozname, rovnako nové odporúčanie či rozhodnutie iného dispečera
        state = page_state(total, fresh["stale"], *[
            (r.id, r.state, r.color, r.priority, iso(r.decided_at), (r.data_age or {}).get("computed_at"),
             r.id in current) for r in recs])
        if wants_state(request):
            return state_response(state)
        # hlavné údaje zoznamu (rozhodnutie, zobrazená farba, stav) – ich zmenu otvorený zoznam ohlási výrazne
        summary = page_summary(*[(
            f"Vlak {r.train.number if r.train else '?'} ({fmt(r.created_at)})",
            ", ".join([r.decision, "ORANŽOVÁ (bez čerstvých údajov agenta)" if r.id in current and r.color == "ZELENÁ"
                       else r.color, REC_STATE.get(r.state, r.state)])) for r in recs])
        return render(request, "odporucania.html", {"recs": recs, "stav": stav, "farba": farba, "vlak": vlak,
                                                    "strana": strana, "pages": max(1, -(-total // per_page)),
                                                    "total": total, "fresh": fresh, "current_ids": current,
                                                    "page_state": state, "page_summary": summary}, user=user)


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


def _running_rec(rec: Recommendation, current: bool) -> bool:
    """Odporúčanie pre idúci vlak: pokračovanie v jazde nie je vypravenie (POKRAČOVAŤ, nie VYPRAVIŤ) a jeho čas
    „odchodu“ je len čas prepočtu. Plánovač idúcemu vlaku VYPRAVIŤ nedáva (mení ho na POKRAČOVAŤ) – odporúčanie
    VYPRAVIŤ vzniklo pred odchodom; iné rozhodnutie (POČKAŤ, ODSTAVIŤ, ESKALOVAŤ) je pre idúci vlak, ak je aktuálne
    a vlak je v jazde."""
    chosen = rec.chosen or {}
    return rec.decision == "POKRAČOVAŤ" or bool(chosen.get("until_entry")) or (
        current and rec.decision != "VYPRAVIŤ" and rec.train is not None and rec.train.status == "v_jazde")


def _ago_iso(now: datetime, minutes: Any) -> Optional[str]:
    """Čas pred „minutes“ minútami (ISO) – pre živý údaj „pred … min“ na stránke (data-vek, static/js/app.js)."""
    return iso(now - timedelta(minutes=float(minutes))) if isinstance(minutes, (int, float)) else None


def _duty_minutes(crew: dict, params: dict) -> int:
    """Limit pracovného času rušňovodiča v minútach (z odporúčania, inak z parametrov) – horná hranica odpočtu."""
    try:
        return round(float(crew.get("max_duty_h")) * 60)
    except (TypeError, ValueError):
        return int(params["crew_max_duty_min"])


@router.get("/odporucania/{rec_id}", response_class=HTMLResponse)
def recommendation_detail(request: Request, rec_id: int, user=Depends(view)):
    cfg = settings()
    now = now_utc()
    with session_scope() as s:
        rec = s.scalar(select(Recommendation).options(selectinload(Recommendation.train))
                       .where(Recommendation.id == rec_id))
        if rec is None:
            raise HTTPException(404, "Odporúčanie neexistuje.")
        history = list(s.scalars(select(Recommendation).where(Recommendation.train_id == rec.train_id)
                                 .order_by(Recommendation.id.desc()).limit(10)))
        status = recommendation_status(s, rec, now)
        latest, fresh, current = status["latest"], status["fresh"], status["current"]
        # aktuálne odporúčanie vlaku bez čerstvých dát agenta: ZELENÁ sa ukáže ako ORANŽOVÁ (PROMPT 12); varianty
        # a okná obmedzeného semaforu (plánovač alebo web) sa nikde neukážu ako ZELENÉ
        color = status["color"]
        cap = semafor_cap(rec, fresh, current, cfg.position_stale_minutes)
        newest = next((h for h in history if h.id == latest), None)
        newest_color = display_color(newest, fresh, is_current(newest, latest))
        history_colors = {h.id: newest_color if h.id == latest else h.color for h in history}
        # aktuálne odporúčanie: pracovný čas rušňovodiča a vek dát k teraz, nie zmrazené čísla z prepočtu
        params = current_params(s, now)
        crew_now = crew_live(rec.crew, params, now) if current and rec.crew else None
        # rušňovodič z odporúčania ešte nenastúpil (naplánovaná zmena, nový rušňovodič po odstavení) alebo nie je
        # pridelený (potrebný nástup podľa plánu): limit 13 h mu ešte nebeží – žiadny odpočet (napr. „63 h 17 min“)
        crew = rec.crew or {}
        crew_start = parse_iso(crew.get("start"))
        crew_ref = now if current else (ensure_utc(rec.created_at) or now)  # staršie odporúčanie: čas prepočtu
        crew_waiting = bool(crew) and not crew.get("unknown_start") and (
            not crew.get("driver") or (crew_start is not None and crew_start > crew_ref))
        # koniec pracovného času (limit), ku ktorému sa „Zostáva do limitu“ na otvorenej stránke odpočítava – rovnako
        # ako ho počíta crew_live; bez zadaného nástupu sa zostávajúci čas neukazuje ako fakt (žiadny odpočet)
        crew_limit = iso(parse_iso(crew.get("limit"))) \
            if crew_now and not crew.get("unknown_start") and not crew_waiting else None
        position_age = None
        position_at = None
        if current:
            rows = latest_positions(s, [rec.train_id]).get(rec.train_id)
            if rows:
                position_at = iso(rows[0].ts_utc)
                position_age = round((now - ensure_utc(rows[0].ts_utc)).total_seconds() / 60.0, 1)
        position_stale = position_age is not None and position_age > cfg.position_stale_minutes
        # časy, od ktorých stránka počíta „pred N min“ (data-vek); pri každom dopyte na stav sa posielajú znova
        mailbox_at = _ago_iso(now, fresh.get("mailbox_min"))
        cycle_at = _ago_iso(now, fresh.get("agent_cycle_min"))
        ages = {"poloha": position_at, "schranka": mailbox_at, "cyklus": cycle_at} if current else {}
        # odtlačok toho, čo stránka ukazuje: otvorená stránka sa ho každú minútu pýta a pri zmene (agent prestal
        # prepočítavať a ZELENÁ je teraz ORANŽOVÁ, nové odporúčanie, rozhodnutie iného dispečera, tichá aktualizácia
        # odporúčania plánovačom) si obsah obnoví – nikdy neukazuje ZELENÚ bez čerstvých dát agenta
        state = page_state(
            rec.id, rec.state, rec.decision, rec.color, rec.priority, (rec.data_age or {}).get("computed_at"),
            rec.decided_by, iso(rec.decided_at), color, current, latest, fresh["stale"],
            rec.train.status if rec.train else "", (crew_now or {}).get("status"), crew_limit, position_stale)
        if wants_state(request):
            return state_response(state, ages)
        # hlavné údaje: ich zmenu otvorená stránka ohlási výrazne (bežný prepočet s rovnakým výsledkom nie)
        summary = page_summary(
            ("Semafor", f"{color} (bez čerstvých údajov agenta)" if color != rec.color else color),
            ("Rozhodnutie", rec.decision), ("Priorita", rec.priority or "INFO"),
            ("Stav odporúčania", REC_STATE.get(rec.state, rec.state) + (f" ({rec.decided_by})" if rec.decided_by
                                                                        else "")),
            ("Posledné odporúčanie vlaku", "áno" if latest == rec.id else "nie – vlak má novšie odporúčanie"),
            ("Stav vlaku", TRAIN_STATUS.get(rec.train.status, rec.train.status) if rec.train else None),
            ("Údaje agenta", ("nie sú čerstvé" if fresh["stale"] else "čerstvé") if current else None),
            ("Poloha vlaku", (f"neistá – staršia ako {cfg.position_stale_minutes} min" if position_stale else
                              "bez polohy" if position_age is None else "v poriadku") if current else None),
            ("Pracovný čas rušňovodiča", (crew_now or {}).get("status")))
        return render(request, "odporucanie.html", {
            "rec": rec, "train": rec.train, "latest_id": latest, "history": history,
            "history_colors": history_colors, "steps": _next_steps(rec, color),
            "stale_minutes": cfg.position_stale_minutes, "fresh": fresh, "current": current,
            "agent_stale": bool(current and fresh["stale"]), "color": color, "downgraded": color != rec.color,
            "cap": cap, "variants": capped_variants(rec, cap), "crew_now": crew_now, "crew_waiting": crew_waiting,
            "crew_limit": crew_limit, "crew_duty_min": _duty_minutes(crew, params),
            "position_age": position_age, "position_at": position_at, "page_state": state, "page_summary": summary,
            "mailbox_at": mailbox_at, "cycle_at": cycle_at, "running": _running_rec(rec, current)},
            user=user)


DECISION_STATE = {"prijat": "prijate", "upravit": "upravene", "zamietnut": "zamietnute"}


SEMAFOR_COLORS = ("ZELENÁ", "ORANŽOVÁ", "ČERVENÁ")


@router.post("/odporucania/{rec_id}/rozhodnutie")
def recommendation_decide(rec_id: int, request: Request, user=Depends(operate), form=Depends(csrf_form)):
    """Rozhodnutie dispečera. Prijať a Upraviť sa vzťahujú na odporúčanie tak, ako ho web ukazuje teraz: bez čerstvých
    dát agenta nie je ZELENÉ (rozhodnutie vyžaduje výslovné potvrdenie a v audite je zobrazená farba, nie ZELENÁ)
    a ak sa semafor od zobrazenia stránky zhoršil (farba v skrytom poli „farba“), rozhodnutie sa neuloží."""
    reader = FormReader(form)
    action = reader.choice("akcia", "Akcia", DECISION_STATE.keys())
    note = reader.text("poznamka", "Poznámka", maxlen=2000)
    shown = reader.raw("farba")  # farba semaforu, ktorú stránka ukazovala pri odoslaní
    confirmed_stale = reader.flag("potvrdit_neaktualne")
    back = f"/odporucania/{rec_id}"
    if not reader.ok:
        return redirect(back, "Neplatná akcia.", "chyba")
    if action == "zamietnut" and not note:
        return redirect(back, "Pri zamietnutí uveďte dôvod.", "chyba")
    if action == "upravit" and not note:
        return redirect(back, "Pri úprave popíšte, čo meníte (napr. iný čas odchodu).", "chyba")
    with session_scope() as s:
        rec = s.scalar(select(Recommendation).options(selectinload(Recommendation.train))
                       .where(Recommendation.id == rec_id))
        if rec is None:
            raise HTTPException(404, "Odporúčanie neexistuje.")
        if rec.state == "zastarane":
            return redirect(back, "Odporúčanie je zastarané – rozhodnite o aktuálnom odporúčaní pre vlak.", "chyba")
        status = recommendation_status(s, rec)
        color, fresh, downgraded = status["color"], status["fresh"], status["downgraded"]
        if action != "zamietnut":
            if downgraded and not confirmed_stale:
                # agent nemá čerstvé dáta: zelené odporúčanie sa ukazuje ako ORANŽOVÉ – prijať ho možno len vedome
                return redirect(back + "#h-rozhodnutie",
                                f"{fresh['text']} Odporúčanie sa preto ukazuje ako {color} (pri prepočte {rec.color}) "
                                "a rozhodnutie sa neuložilo. Overte aktuálny stav (výluky, polohu, rušňovodiča); ak "
                                "odporúčanie napriek tomu prijímate, potvrďte to políčkom pri tlačidle a odošlite znova.",
                                "varovanie")
            if shown in SEMAFOR_COLORS and worse_color(color, shown) != shown:
                # semafor sa od zobrazenia stránky zhoršil (tichá aktualizácia odporúčania, výpadok agenta)
                return redirect(back + "#h-rozhodnutie",
                                f"Semafor odporúčania sa od zobrazenia stránky zhoršil (bol {shown}, teraz {color}) – "
                                "rozhodnutie sa neuložilo. Skontrolujte aktuálne odporúčanie a rozhodnite znova.",
                                "varovanie")
        previous = rec.state
        rec.state = DECISION_STATE[action]
        rec.decided_by = user.username
        rec.decided_at = now_utc()
        rec.dispatcher_note = note
        detail: dict = {"train_id": rec.train_id, "decision": rec.decision, "color": color,
                        "previous_state": previous, "state": rec.state, "note": note}
        if downgraded:
            # v audite farba, ako ju dispečer videl (nikdy ZELENÁ bez čerstvých dát agenta), a farba pri prepočte;
            # prijať či upraviť sa dalo len s výslovným potvrdením neaktuálnych údajov
            detail["computed_color"] = rec.color
            if action != "zamietnut":
                detail["stale_confirmed"] = True
        if fresh["stale"]:
            detail["data_freshness"] = fresh["text"]
        audit(s, user.username, f"recommendation_{action}", "recommendation", rec.id, **detail)
        computed = rec.color
    text = {"prijat": "Odporúčanie prijaté.", "upravit": "Úprava odporúčania uložená.",
            "zamietnut": "Odporúčanie zamietnuté."}[action]
    if downgraded and action != "zamietnut":
        text += f" Zaznamenané ako {color} – údaje agenta neboli čerstvé (pri prepočte {computed})."
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
