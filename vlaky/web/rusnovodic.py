"""Stránka rušňovodiča (mobilná PWA) – podpísaný odkaz na 24 h, hlásenia a zdieľanie polohy.

Odkaz vytvorí dispečer vo webe a rušňovodičovi ho pošle sám (agent rušňovodičom nič neposiela – sekcia 2.2).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..audit import audit
from ..cas import ensure_utc, fmt, now_utc
from ..config import settings
from ..db import session_scope
from ..models import DriverShift, KeyValue, PositionReport, Train
from ..siet.graf import haversine
from .common import DONE_TRAIN_STATES, STATIC_DIR, TRAIN_STATUS, iso, log, render
from .pozicie import DriverPositionIn, ingest_position
from .prepocet import evaluate_train_bg

TOKEN_MAX_AGE = 24 * 3600
REPORTS_PER_WINDOW = 6          # najviac hlásení jedného vlaku za okno (ochrana pred zneužitím odkazu) – hlásenia
REPORT_WINDOW_S = 600           # stavu (nástup, pripravený, odchod) a hlásenia o probléme majú každé vlastný limit
PROBLEM_WINDOW_S = 600          # upozornenia na problém: najviac jedno za 10 min na vlak (aj pri inom texte)
EVALUATE_MIN_GAP_S = 30         # prepočet po hlásení o probléme najviac raz za 30 s (zmena stavu sa prepočíta vždy)
AUDIT_WINDOW_S = 600            # odmietnuté požiadavky z odkazu: do auditu najviac raz za 10 min na vlak a druh
STATE_TYPES = ("nastup", "pripraveny", "odchod")
router = APIRouter()


class _TrainThrottle:
    """Počítadlá v pamäti pre odkazy rušňovodičov (na vlak)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reports: dict[tuple, deque] = {}
        self._last_eval: dict[int, float] = {}
        self._audited: dict[tuple, list] = {}       # (vlak, druh) → [čas posledného zápisu, nezapísané odvtedy]
        self._eval_locks: dict[int, threading.Lock] = {}

    def allow_report(self, train_id: int, problem: bool = False) -> bool:
        """Hlásenie o probléme má vlastný limit – odmietnuté (409) ani opakované hlásenia stavu ho nevyčerpajú."""
        now = time.monotonic()
        with self._lock:
            q = self._reports.setdefault((train_id, problem), deque())
            while q and now - q[0] > REPORT_WINDOW_S:
                q.popleft()
            if len(q) >= REPORTS_PER_WINDOW:
                return False
            q.append(now)
            if len(self._reports) > 5000:
                self._reports = {k: v for k, v in self._reports.items() if v and now - v[-1] <= REPORT_WINDOW_S}
            return True

    def allow_evaluate(self, train_id: int, force: bool = False) -> bool:
        """Prepočet po hlásení. Zmena stavu vlaku (force) sa prepočíta vždy – zahodený prepočet by nechal staré
        odporúčanie až do pravidelného prepočtu agenta; hlásenie o probléme najviac raz za EVALUATE_MIN_GAP_S."""
        now = time.monotonic()
        with self._lock:
            last = self._last_eval.get(train_id)
            if not force and last is not None and now - last < EVALUATE_MIN_GAP_S:
                return False
            self._last_eval[train_id] = now
            if len(self._last_eval) > 5000:
                self._last_eval = {k: v for k, v in self._last_eval.items() if now - v < EVALUATE_MIN_GAP_S}
            return True

    def audit_gate(self, train_id: int, kind: str) -> Optional[int]:
        """Odmietnutá požiadavka sa do auditu zapíše najviac raz za AUDIT_WINDOW_S (na vlak a druh odmietnutia).

        Vráti počet odmietnutí od posledného zápisu, ktoré sa nezapísali, alebo None (teraz sa nezapisuje).
        Nával odmietnutých požiadaviek tak nezahltí audit ani históriu vlaku.
        """
        now = time.monotonic()
        key = (train_id, kind)
        with self._lock:
            entry = self._audited.get(key)
            if entry is not None and now - entry[0] < AUDIT_WINDOW_S:
                entry[1] += 1
                return None
            skipped = entry[1] if entry is not None else 0
            self._audited[key] = [now, 0]
            if len(self._audited) > 5000:
                self._audited = {k: v for k, v in self._audited.items() if now - v[0] < AUDIT_WINDOW_S}
            return skipped

    def eval_lock(self, train_id: int) -> threading.Lock:
        """Prepočty toho istého vlaku po hláseniach idú za sebou (posledný vždy číta aktuálny stav vlaku)."""
        with self._lock:
            lock = self._eval_locks.get(train_id)
            if lock is None:
                if len(self._eval_locks) > 5000:
                    self._eval_locks = {k: v for k, v in self._eval_locks.items() if v.locked()}
                lock = self._eval_locks[train_id] = threading.Lock()
            return lock



def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings().secret_key, salt="vlaky.rusnovodic.v1")


def _version_key(train_id: int) -> str:
    return f"driver_token_ver:{train_id}"


def token_version(s, train_id: int) -> int:
    """Aktuálna verzia odkazov pre vlak (po zrušení odkazov sa zvýši – staršie odkazy prestanú platiť)."""
    kv = s.scalar(select(KeyValue).where(KeyValue.key == _version_key(train_id)))
    try:
        return int((kv.value or {}).get("v", 0)) if kv is not None else 0
    except (TypeError, ValueError, AttributeError):
        return 0


def revoke_driver_links(s, train_id: int) -> int:
    """Zruší všetky doteraz vydané odkazy pre vlak (zvýši verziu). Vráti novú verziu."""
    key = _version_key(train_id)
    version = token_version(s, train_id) + 1
    kv = s.scalar(select(KeyValue).where(KeyValue.key == key))
    if kv is None:
        s.add(KeyValue(key=key, value={"v": version}))
    else:
        kv.value = {"v": version}
    s.flush()
    return version


def make_driver_token(train: Train, version: int = 0) -> str:
    return _serializer().dumps({"n": train.number, "id": train.id, "v": int(version)})


def driver_link(s, train: Train) -> str:
    """Odkaz s tokenom vo fragmente URL (#t=…) – prehliadač ho neposiela serveru, nedostane sa do logov."""
    token = make_driver_token(train, token_version(s, train.id))
    return f"{settings().public_url.rstrip('/')}/rusnovodic#t={token}"


def verify_driver_token(token: str) -> Optional[dict]:
    """Vráti {"n": číslo vlaku, "id": id vlaku, "v": verzia, "ts": vydanie} alebo None (neplatný / > 24 h)."""
    if not token or len(token) > 600:
        return None
    try:
        data, issued = _serializer().loads(token, max_age=TOKEN_MAX_AGE, return_timestamp=True)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("n"), str) or not isinstance(data.get("id"), int):
        return None
    return {**data, "ts": issued}


def _train_for_token(s, data: dict) -> Optional[Train]:
    t = s.get(Train, data["id"])
    if t is None or t.number != data["n"] or t.status in DONE_TRAIN_STATES:
        return None
    if int(data.get("v", 0) or 0) != token_version(s, t.id):
        return None  # odkaz bol zrušený dispečerom
    return t


def _throttle(request: Request) -> "_TrainThrottle":
    th = getattr(request.app.state, "driver_throttle", None)
    if th is None:
        th = request.app.state.driver_throttle = _TrainThrottle()
    return th


@router.get("/rusnovodic", response_class=HTMLResponse)
def driver_page(request: Request):
    """Stránka rušňovodiča. Token sa sem neposiela – JS ho vezme z fragmentu URL (#t=…) a pošle v tele požiadavky."""
    return render(request, "rusnovodic.html", {})


class TokenIn(BaseModel):
    t: str = Field(min_length=10, max_length=600)


@router.post("/api/rusnovodic/info")
def driver_info(body: TokenIn):
    data = verify_driver_token(body.t)
    if data is None:
        return _forbidden()
    with session_scope() as s:
        train = _train_for_token(s, data)
        if train is None:
            return _forbidden()
        shift = s.scalar(select(DriverShift).where(DriverShift.train_id == train.id, DriverShift.status != "ukoncena")
                         .order_by(DriverShift.start_utc.desc(), DriverShift.id.desc()))
        valid_until = data["ts"] + timedelta(seconds=TOKEN_MAX_AGE)
        return {"ok": True, "train": {"number": train.number, "origin": train.origin,
                                      "destination": train.destination,
                                      "relation": f"{train.origin} → {train.destination}",
                                      "status": train.status, "status_text": TRAIN_STATUS.get(train.status, "")},
                "driver_name": shift.driver.name if shift and shift.driver else "",
                "shift_status": shift.status if shift else None,
                "shift_start": iso(shift.start_utc) if shift else None,
                "shift_start_text": fmt(shift.start_utc) if shift else "",
                "valid_until": iso(valid_until), "valid_until_text": fmt(valid_until)}


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
        throttle = _throttle(request)
        problem = body.typ == "problem"
        if not throttle.allow_report(train.id, problem=problem):
            skipped = throttle.audit_gate(train.id, "hlasenie_obmedzene:" + ("problem" if problem else "stav"))
            if skipped is not None:
                audit(s, actor, "hlasenie_obmedzene", "train", train.id, typ=body.typ, nezapisane=skipped,
                      **({"text": text[:500]} if problem else {}))
            return JSONResponse({"ok": False, "detail": "Príliš veľa hlásení za krátky čas. Ak ide o niečo "
                                                        "naliehavé, zavolajte dispečerovi."}, status_code=429)
        detail: dict = {"typ": body.typ, "text": text}
        if body.typ == "nastup":
            from ..models import Driver
            from ..planovanie.planovac import MOBILE_DRIVER, NASTUP_EARLY_MIN, due_shift

            def who(sh) -> str:
                name = sh.driver.name if sh.driver and sh.driver.name != MOBILE_DRIVER else ""
                return f" ({name})" if name else ""

            # začína sa zmena, ktorá je na rade (nie neskoršie vystriedanie); vystriedanie ukončí bežiacu zmenu
            shift = due_shift(s, train.id, now)
            running = list(s.scalars(select(DriverShift).where(DriverShift.train_id == train.id,
                                                               DriverShift.status == "prebieha")
                                     .order_by(DriverShift.start_utc.desc(), DriverShift.id.desc())))
            if shift is not None and running and ensure_utc(shift.start_utc) <= \
                    ensure_utc(running[0].start_utc) + timedelta(minutes=NASTUP_EARLY_MIN):
                shift = None  # naplánovaná na ten istý čas ako bežiaca zmena – nie vystriedanie (13 h sa nevynuluje)
            if shift is None and running:
                # začiatok bežiacej zmeny sa z odkazu nesmie zmeniť (vynulovanie 13 h limitu) – len dispečer
                current = running[0]
                skipped = throttle.audit_gate(train.id, "hlasenie_nastup_odmietnute")
                if skipped is not None:
                    audit(s, actor, "hlasenie_nastup_odmietnute", "train", train.id, shift_id=current.id,
                          start=current.start_utc, nezapisane=skipped)
                relief = s.scalar(select(DriverShift).where(
                    DriverShift.train_id == train.id, DriverShift.status == "planovana",
                    DriverShift.start_utc > ensure_utc(current.start_utc) + timedelta(minutes=NASTUP_EARLY_MIN))
                    .order_by(DriverShift.start_utc, DriverShift.id))
                text_409 = f"Nástup už bol zaznamenaný o {fmt(current.start_utc, False)}{who(current)}."
                if relief is not None:
                    text_409 += (f" Vystriedanie{who(relief)} je naplánované na {fmt(relief.start_utc, False)} – "
                                 f"nástup sa dá zaznamenať najskôr {NASTUP_EARLY_MIN:.0f} min vopred.")
                return JSONResponse({"ok": False, "detail": text_409 + " Zmenu môže upraviť len dispečer."},
                                    status_code=409)
            if shift is None:
                # žiadna zmena ešte nie je na rade: najbližšia naplánovaná (rušňovodič prišiel skôr)
                shift = s.scalar(select(DriverShift).where(DriverShift.train_id == train.id,
                                                           DriverShift.status == "planovana")
                                 .order_by(DriverShift.start_utc, DriverShift.id))
            if shift is None:
                # zmena nie je naplánovaná: založí sa skutočná zmena (rušňovodič neznámy, dispečer doplní meno) –
                # 13 h limit beží od nahláseného nástupu (plánovač aj upozornenia), nie od odhadu
                driver = s.scalar(select(Driver).where(Driver.name == MOBILE_DRIVER).order_by(Driver.id))
                if driver is None:
                    driver = Driver(name=MOBILE_DRIVER, active=False)  # neaktívny – návrh nasadenia ho nepoužije
                    s.add(driver)
                    s.flush()
                shift = DriverShift(driver_id=driver.id, train_id=train.id, start_utc=now, status="prebieha")
                s.add(shift)
                s.flush()
                detail["shift_id"] = shift.id
                detail["shift_created"] = True
                message = (f"Nástup zaznamenaný o {fmt(now, False)}. Limit pracovného času beží od tohto času. "
                           f"Zmena pre tento vlak nebola naplánovaná – dispečer doplní vaše meno.")
            else:
                ended = []
                for current in running:  # vystriedanie: zmena predchádzajúceho rušňovodiča na vlaku končí
                    current.status, current.actual_end_utc = "ukoncena", now
                    audit(s, actor, "shift_end", "driver_shift", current.id, end=now,
                          dovod="vystriedanie – nástup ďalšieho rušňovodiča z odkazu")
                    ended.append(current)
                detail["shift_id"] = shift.id
                detail["previous_start"] = shift.start_utc
                if ended:
                    detail["relieved_shift_ids"] = [x.id for x in ended]
                shift.start_utc = now
                shift.status = "prebieha"
                message = f"Nástup zaznamenaný o {fmt(now, False)}. Limit pracovného času beží od tohto času."
                if ended:
                    message += f" Zmena predchádzajúceho rušňovodiča{who(ended[0])} je ukončená."
        elif body.typ == "pripraveny":
            if train.status == "v_jazde":
                return JSONResponse({"ok": False, "detail": "Vlak je už v jazde."}, status_code=409)
            from ..planovanie import stav

            detail["previous_ready"] = train.ready_utc
            if stav.keep_ready_on_report(s, train, now):
                # vlak už stojí na koľajach ŽSR (odstavený, prevzatý, čaká pripravený od zmeškanej trasy): doterajší
                # čas pripravenosti sa nemení – hlásenie nesmie posunúť limit 18 h státia (PPŽS); ostáva v audite
                detail["ready_unchanged"] = True
            else:
                train.ready_utc = now
            message = f"Vlak pripravený o {fmt(now, False)}."
        elif body.typ == "odchod":
            if train.status == "v_jazde":
                return JSONResponse({"ok": False, "detail": "Odchod už bol zaznamenaný."}, status_code=409)
            from ..planovanie import stav

            detail["previous_status"] = train.status
            # pre opravu omylom nahláseného odchodu (vlak nevyšiel) – stav.set_status si ich zapamätá tiež
            detail["previous_standing_since"] = train.standing_since_utc
            detail["previous_departed"] = train.departed_utc
            stav.set_status(s, train, "v_jazde", now)  # státie končí, zapíše sa skutočný čas odchodu
            message = f"Odchod zaznamenaný o {fmt(now, False)}."
        else:
            message = "Problém odoslaný dispečerovi."
        audit(s, actor, f"hlasenie_{body.typ}", "train", train.id, **detail)
        train_id, number = train.id, train.number
        relation = f"{train.origin} → {train.destination}"
    if body.typ == "problem":
        _notify_problem(number, relation, text, now)
    # zmena stavu (nástup, pripravenosť, odchod) sa prepočíta vždy – počet je obmedzený limitom hlásení a 409
    if request.app.state.auto_evaluate and throttle.allow_evaluate(train_id, force=body.typ in STATE_TYPES):
        background.add_task(_evaluate_after_report, throttle, train_id, f"hlásenie rušňovodiča: {TYP_TEXT[body.typ]}")
    return {"ok": True, "message": message, "typ": body.typ, "ts": now.isoformat()}


def _evaluate_after_report(throttle: _TrainThrottle, train_id: int, trigger: str) -> None:
    """Prepočet vlaku po hlásení (na pozadí). Prepočty jedného vlaku idú za sebou, aby starší prepočet
    (napr. po nástupe) neprepísal novší (po pripravenosti o sekundu neskôr) – každý číta stav z DB až po zámku."""
    with throttle.eval_lock(train_id):
        evaluate_train_bg(train_id, trigger)


def _notify_problem(number: str, relation: str, text: str, now=None) -> None:
    """Upozornenie dispečerom (iba interný zoznam – odosielacia služba iných príjemcov odmietne).

    Najviac jedno upozornenie za 10 min na vlak (deduplikácia podľa časového okna, nie podľa textu) –
    ďalšie hlásenia sú v audite a v detaile vlaku. Text sa posiela ako obyčajný text.
    """
    try:
        from ..notifikacie.odosielanie import send_internal

        bucket = int((now or now_utc()).timestamp() // PROBLEM_WINDOW_S)
        with session_scope() as s:
            send_internal(s, "upozornenie", f"[AGENT][DÔLEŽITÉ] Vlak {number}: hlásenie rušňovodiča – problém",
                          f"Vlak {number} ({relation})\nRušňovodič hlási problém (text od rušňovodiča, "
                          f"neoverený):\n\n{text}\n\nĎalšie hlásenia sú v detaile vlaku a v audite.", "DÔLEŽITÉ",
                          dedup_key=f"problem:{number}:{bucket}")
    except NotImplementedError:
        log.warning("Odosielanie upozornení zatiaľ nie je k dispozícii")
    except Exception:  # noqa: BLE001 – hlásenie je uložené v audite aj tak
        log.exception("Upozornenie o probléme sa nepodarilo odoslať")


DRIVER_MAX_AGE = timedelta(minutes=15)   # poloha z mobilu musí byť aktuálna (nie spätné prepisovanie stopy)
DRIVER_MIN_GAP_S = 10                     # najviac jedna poloha za 10 s
DRIVER_MAX_KMH = 200.0                    # rýchlejší „skok“ medzi polohami je chyba alebo podvrh
DRIVER_CLOCK_HINT_S = 60                  # aplikácia posiela polohu hneď (maximumAge 10 s) – väčší rozdiel = hodiny


@router.post("/api/pozicie/rusnovodic")
def driver_position(body: DriverPositionIn, request: Request):
    data = verify_driver_token(body.t)
    if data is None:
        return _forbidden()
    now = now_utc()
    # hodiny mobilu môžu ísť dopredu (vstupná kontrola pripustí najviac +10 min): poloha z „budúcnosti“ by
    # inak bola najnovšia a ďalšie polohy by sa až 10 min zahadzovali – na kontroly aj uloženie najviac „teraz“
    ts = min(ensure_utc(body.ts), now) if body.ts else now
    if ts < now - DRIVER_MAX_AGE:
        return JSONResponse({"ok": False, "detail": "Poloha je staršia ako 15 min."}, status_code=422)
    with session_scope() as s:
        train = _train_for_token(s, data)
        if train is None:
            return _forbidden()
        # poloha s časom v budúcnosti (napr. staršia z brány) sa na odstup a rýchlosť nepoužije
        # (now_utc() je zaokrúhlený na sekundy – tolerancia 1 s)
        last = s.scalar(select(PositionReport).where(PositionReport.train_id == train.id,
                                                     PositionReport.source == "driver",
                                                     PositionReport.ts_utc <= now + timedelta(seconds=1))
                        .order_by(PositionReport.ts_utc.desc(), PositionReport.id.desc()))
        if last is not None:
            gap = (ts - ensure_utc(last.ts_utc)).total_seconds()
            if gap < 0:
                # staršia ako posledná uložená (iný mobil, oneskorené doručenie) – stopa sa spätne neprepisuje; ak čas
                # polohy výrazne zaostáva za časom servera, hodiny mobilu nesedia (aplikácia čas polohy prepočíta
                # podľa hlavičky Date, staršia verzia nie)
                detail = "Poloha je staršia ako posledná uložená."
                if (now - ts).total_seconds() > DRIVER_CLOCK_HINT_S:
                    detail += " Skontrolujte, či má mobil zapnutý automatický dátum a čas."
                return {"ok": True, "skipped": True, "detail": detail}
            if gap < DRIVER_MIN_GAP_S:
                return {"ok": True, "skipped": True, "detail": "Poloha prišla príliš skoro po predchádzajúcej."}
            km = haversine(last.lat, last.lon, body.lat, body.lon) / 1000.0
            if km > 1.0 and km / (gap / 3600.0) > DRIVER_MAX_KMH:
                skipped = _throttle(request).audit_gate(train.id, "poloha_odmietnuta")
                if skipped is not None:
                    audit(s, f"rusnovodic:{train.number}", "poloha_odmietnuta", "train", train.id,
                          km=round(km, 1), gap_s=round(gap), lat=body.lat, lon=body.lon, nezapisane=skipped)
                return JSONResponse({"ok": False, "detail": "Nepravdepodobný skok polohy – poloha sa neuložila."},
                                    status_code=422)
        out = ingest_position(s, train.number, body.lat, body.lon, ts, body.speed_kmh, body.accuracy_m,
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
