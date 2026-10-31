"""Spoločné pomôcky webovej aplikácie: šablóny, flash správy, formuláre, popisky v slovenčine."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import fields as dc_fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..cas import ensure_utc, fmt, local, local_naive_to_utc, now_utc
from ..config import settings

log = logging.getLogger("vlaky.web")

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# MapLibre GL JS – pevná verzia z unpkg.com (s kontrolou integrity SRI).
MAPLIBRE_VERSION = "4.7.1"
MAPLIBRE_JS = f"https://unpkg.com/maplibre-gl@{MAPLIBRE_VERSION}/dist/maplibre-gl.js"
MAPLIBRE_CSS = f"https://unpkg.com/maplibre-gl@{MAPLIBRE_VERSION}/dist/maplibre-gl.css"
MAPLIBRE_JS_SRI = "sha384-SYKAG6cglRMN0RVvhNeBY0r3FYKNOJtznwA0v7B5Vp9tr31xAHsZC0DqkQ/pZDmj"
MAPLIBRE_CSS_SRI = "sha384-MinO0mNliZ3vwppuPOUnGa+iq619pfMhLVUXfC4LHwSCvF9H+6P/KO4Q7qBOYV5V"


def tile_csp_source(url: str) -> str:
    """Zdroj pre CSP (img-src, connect-src) z URL šablóny dlaždíc; relatívna URL = 'self' (vráti "").

    „https://{s}.tile.example.org/{z}/{x}/{y}.png“ → „https://*.tile.example.org“. Nepodporovaný tvar → "",
    dlaždice potom CSP zablokuje a mapa sa zobrazí bez podkladu (ako offline).
    """
    from urllib.parse import urlsplit

    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if not parts.scheme and not parts.netloc:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
        return ""
    host = parts.netloc
    if "{" in host or "}" in host:
        labels = host.split(".")
        if len(labels) >= 3 and "{" in labels[0] and not any("{" in x or "}" in x for x in labels[1:]):
            host = "*." + ".".join(labels[1:])
        else:
            return ""
    if not re.fullmatch(r"[A-Za-z0-9*.\-]+(:\d{1,5})?", host):
        return ""
    return f"{parts.scheme}://{host}"

DONE_TRAIN_STATES = ("ukonceny", "zruseny", "odovzdany")

# ---------------------------------------------------------------- popisky (UI v slovenčine)
TRAIN_STATUS = {
    "planovany": "plánovaný", "na_ceste_k_hranici": "na ceste k hranici", "prevzaty": "prevzatý",
    "v_jazde": "v jazde", "odstaveny": "odstavený", "odovzdany": "odovzdaný", "ukonceny": "ukončený",
    "zruseny": "zrušený",
}
CLOSURE_KIND = {
    "uplna": "úplná výluka úseku", "kolaj": "výluka koľaje", "trakcne_vedenie": "vypnuté trakčné vedenie",
    "pomala_jazda": "pomalá jazda", "obmedzenie": "obmedzenie (dĺžka, hmotnosť)", "mimoriadnost": "mimoriadnosť",
}
CLOSURE_STATUS = {"navrh": "návrh", "overene": "overené", "zrusene": "zrušené"}
EVENT_KIND = {
    "vyluka_nova": "nová výluka", "vyluka_zmena": "zmena výluky", "vyluka_zrusenie": "zrušenie výluky",
    "pomala_jazda": "pomalá jazda", "trasa_pridelena": "pridelenie trasy", "trasa_zmena": "zmena trasy",
    "trasa_zrusena": "zrušenie trasy", "mimoriadnost": "mimoriadnosť", "avizo": "avízo príchodu na hranicu",
    "objednavka": "objednávka", "ine": "iné",
}
REC_STATE = {"nove": "nové", "prijate": "prijaté", "upravene": "upravené", "zamietnute": "zamietnuté",
             "zastarane": "zastarané"}
SHIFT_STATUS = {"planovana": "plánovaná", "prebieha": "prebieha", "ukoncena": "ukončená"}
HANDOVER_STATUS = {"ocakavany": "očakávaný", "prisiel": "prišiel na hranicu", "prevzaty": "prevzatý",
                   "odovzdany": "odovzdaný"}
POSITION_SOURCE = {"gps": "GPS", "zsr": "ŽSR", "tis": "RNE TIS", "manual": "ručne", "driver": "rušňovodič",
                   "sim": "simulácia"}
ROLE_LABEL = {"dispecer": "dispečer", "veduci": "vedúci dispečingu", "planovac": "plánovač rušňovodičov a rušňov",
              "obchod": "obchod / zákaznícky servis", "admin": "administrátor"}
ORDER_STATUS = {"nova": "nová", "navrhnuta": "navrhnutá (agent pripravil návrh)", "chyba_planu": "chyba plánu",
                "zamietnuta": "zamietnutá"}
DEADLINE_TYPE = {"pevny": "pevný", "pokuta": "s pokutou", "orientacny": "orientačný"}
COLOR_CLASS = {"ZELENÁ": "zelena", "ORANŽOVÁ": "oranzova", "ČERVENÁ": "cervena"}
PRIORITY_CLASS = {"KRITICKÉ": "kriticke", "DÔLEŽITÉ": "dolezite", "INFO": "info"}
DECISIONS = ("VYPRAVIŤ", "POČKAŤ", "ODSTAVIŤ", "PRESMEROVAŤ", "ESKALOVAŤ", "POKRAČOVAŤ")
LOCO_SYSTEMS = ("25kV50Hz", "3kV", "15kV16.7Hz", "1.5kV")

# Parametre rozhodovania (sekcia 10.1): popis, jednotka, skupina.
PARAM_META: dict[str, tuple[str, str, str]] = {
    "buffer_before_closure_min": (
        "Rezerva pred výlukou (úsek opustiť aspoň toľko pred začiatkom)", "min", "Časové rezervy"),
    "buffer_after_closure_min": (
        "Rezerva po výluke (vstúpiť najskôr toľko po plánovanom konci)", "min", "Časové rezervy"),
    "deadline_buffer_min": ("Rezerva voči termínu zákazníka (medián príchodu)", "min", "Časové rezervy"),
    "crew_buffer_min": ("Rezerva do limitu pracovného času pri zlom priebehu (P90)", "min", "Časové rezervy"),
    "path_tolerance_min": ("Tolerancia odchýlky od pridelenej trasy", "min", "Trasy"),
    "new_path_lead_min": ("Najkratšia lehota na novú (ad hoc) trasu", "min", "Trasy"),
    "p_on_time_green": ("P(včas) pre ZELENÚ – najmenej", "0–1", "Prahy rozhodovania"),
    "p_on_time_amber": ("P(včas) – pod touto hodnotou je ČERVENÁ", "0–1", "Prahy rozhodovania"),
    "p_hold_green": ("P(zadržania pred výlukou) pre ZELENÚ – najviac", "0–1", "Prahy rozhodovania"),
    "p_hold_red": ("P(zadržania pred výlukou) – nad touto hodnotou je ČERVENÁ", "0–1", "Prahy rozhodovania"),
    "p_crew_relief_red": (
        "P(prekročenia pracovného času) – nad touto hodnotou je ČERVENÁ", "0–1", "Prahy rozhodovania"),
    "robustness_premium": (
        "Prémia za robustnosť (ZELENÁ smie byť drahšia najviac o tento podiel)", "0–1", "Prahy rozhodovania"),
    "cost_wait_ready_per_h": ("Pripravený vlak čaká (rušeň, vozne, koľaj)", "€/h", "Ekonomika (VZOROVÉ hodnoty)"),
    "cost_running_per_h": ("Vlak ide alebo stojí na trati s rušňovodičom", "€/h", "Ekonomika (VZOROVÉ hodnoty)"),
    "cost_stabled_per_h": ("Odstavený vlak (vozne, koľaj) bez rušňovodiča", "€/h", "Ekonomika (VZOROVÉ hodnoty)"),
    "cost_stable_fixed": (
        "Odstavenie – jednorazovo (posun, skúška bŕzd, nová trasa)", "€", "Ekonomika (VZOROVÉ hodnoty)"),
    "cost_new_path": ("Poplatok za novú trasu podľa cenníka ŽSR", "€", "Ekonomika (VZOROVÉ hodnoty)"),
    "cost_late_fixed": ("Zmluvná pokuta za nedodržanie termínu", "€", "Ekonomika (VZOROVÉ hodnoty)"),
    "cost_late_per_h": ("Každá hodina meškania", "€/h", "Ekonomika (VZOROVÉ hodnoty)"),
    "cost_unplanned_hold": ("Neplánované zadržanie vlaku pred výlukou", "€", "Ekonomika (VZOROVÉ hodnoty)"),
    "cost_stuck": ("Zadržanie tam, kde nie je kde čakať (blokovanie trate)", "€", "Ekonomika (VZOROVÉ hodnoty)"),
    "cost_crew_relief": ("Mimoriadna výmena rušňovodiča", "€", "Ekonomika (VZOROVÉ hodnoty)"),
    "crew_max_duty_min": ("Max. pracovný čas rušňovodiča (780 min = 13 h)", "min", "Rušňovodič"),
    "crew_prep_min": ("Nástup: prevzatie rušňa, skúška bŕzd", "min", "Rušňovodič"),
    "crew_post_min": ("Po príchode: odstavenie a odovzdanie rušňa", "min", "Rušňovodič"),
    "crew_relief_delay_min": ("Zdržanie pri mimoriadnej výmene rušňovodiča", "min", "Rušňovodič"),
    "samples": ("Počet vzoriek simulácie Monte Carlo", "ks", "Výpočet"),
    "seed": ("Počiatočná hodnota generátora náhodných čísel", "", "Výpočet"),
}
INT_PARAMS = {"samples", "seed"}
PROB_PARAMS = {"p_on_time_green", "p_on_time_amber", "p_hold_green", "p_hold_red", "p_crew_relief_red",
               "robustness_premium"}


# ---------------------------------------------------------------- čas
def parse_iso(value: Any) -> Optional[datetime]:
    """ISO reťazec (alebo datetime) → datetime v UTC; neznámy formát → None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return ensure_utc(dt)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return ensure_utc(dt).isoformat() if dt else None


def minutes_until(dt: Optional[datetime], now: Optional[datetime] = None) -> Optional[float]:
    if dt is None:
        return None
    return (ensure_utc(dt) - ensure_utc(now or now_utc())).total_seconds() / 60.0


def input_dt(dt: Any) -> str:
    """Hodnota pre <input type="datetime-local"> v miestnom čase."""
    d = parse_iso(dt)
    return local(d).strftime("%Y-%m-%dT%H:%M") if d else ""


def fmt_any(value: Any, with_date: bool = True) -> str:
    d = parse_iso(value)
    return fmt(d, with_date) if d else "—"


def fmt_full(value: Any) -> str:
    d = parse_iso(value)
    return local(d).strftime("%d.%m.%Y %H:%M") if d else "—"


def fmt_minutes(value: Any) -> str:
    """Minúty → „3 h 05 min“ (záporné so znamienkom mínus)."""
    if value is None or value == "":
        return "—"
    try:
        m = int(round(float(value)))
    except (TypeError, ValueError):
        return "—"
    sign = "−" if m < 0 else ""
    m = abs(m)
    if m >= 60:
        return f"{sign}{m // 60} h {m % 60:02d} min"
    return f"{sign}{m} min"


def fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f} %"
    except (TypeError, ValueError):
        return "—"


def fmt_eur(value: Any) -> str:
    try:
        return f"{float(value):,.0f} €".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def fmt_num(value: Any, digits: int = 0) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):,.{digits}f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def ago(value: Any) -> str:
    d = parse_iso(value)
    if not d:
        return "—"
    m = -minutes_until(d)
    if m < 1:
        return "práve teraz"
    if m < 60:
        return f"pred {int(m)} min"
    if m < 48 * 60:
        return f"pred {int(m // 60)} h {int(m % 60):02d} min"
    return f"pred {int(m // 1440)} dňami"


# ---------------------------------------------------------------- parametre (sekcia 10)
def param_defaults() -> dict:
    from ..rozhodovanie import Params

    return {f.name: getattr(Params(), f.name) for f in dc_fields(Params)}


def current_param_set(session: Session, now: Optional[datetime] = None):
    from ..models import ParameterSet

    # now_utc() je zaokrúhlený na sekundy – sada uložená „teraz“ (s mikrosekundami) má platiť hneď
    limit = (now or now_utc()) + timedelta(seconds=1)
    return session.scalar(select(ParameterSet).where(ParameterSet.valid_from <= limit)
                          .order_by(ParameterSet.valid_from.desc(), ParameterSet.id.desc()))


def current_params(session: Session, now: Optional[datetime] = None) -> dict:
    """Platné hodnoty parametrov: posledná ParameterSet (valid_from ≤ teraz) zlúčená s predvolenými."""
    values = param_defaults()
    ps = current_param_set(session, now)
    if ps and ps.values:
        for k, v in ps.values.items():
            if k in values:
                try:
                    values[k] = int(v) if k in INT_PARAMS else float(v)
                except (TypeError, ValueError):
                    continue
    return values


def crew_color(left_min: Optional[float], buffer_min: float) -> str:
    """Farba zostávajúceho pracovného času: zelená nad rezervou, oranžová, červená pod polovicou rezervy."""
    if left_min is None:
        return "seda"
    if left_min > buffer_min:
        return "zelena"
    if left_min > buffer_min / 2:
        return "oranzova"
    return "cervena"


# ---------------------------------------------------------------- šablóny
def _color_class(color: Any) -> str:
    return COLOR_CLASS.get(str(color or ""), "seda")


def _json_pretty(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _label(mapping: Mapping, key: Any) -> str:
    return mapping.get(key, key or "—")


def build_templates() -> Jinja2Templates:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)),
                      autoescape=select_autoescape(["html", "xml"], default_for_string=True, default=True),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters.update({
        "t": fmt_any, "tt": lambda v: fmt_any(v, False), "tfull": fmt_full, "dtinput": input_dt,
        "minutes": fmt_minutes, "pct": fmt_pct, "eur": fmt_eur, "num": fmt_num, "ago": ago,
        "color_class": _color_class, "jsonpretty": _json_pretty, "iso": lambda v: iso(parse_iso(v)) or "",
        "priority_class": lambda v: PRIORITY_CLASS.get(str(v or ""), "info"),
        "future": lambda v: bool(parse_iso(v) and parse_iso(v) > now_utc()),
    })
    env.globals.update({
        "TRAIN_STATUS": TRAIN_STATUS, "CLOSURE_KIND": CLOSURE_KIND, "CLOSURE_STATUS": CLOSURE_STATUS,
        "EVENT_KIND": EVENT_KIND, "REC_STATE": REC_STATE, "SHIFT_STATUS": SHIFT_STATUS,
        "HANDOVER_STATUS": HANDOVER_STATUS, "POSITION_SOURCE": POSITION_SOURCE, "ROLE_LABEL": ROLE_LABEL,
        "ORDER_STATUS": ORDER_STATUS, "DEADLINE_TYPE": DEADLINE_TYPE,
        "label": _label, "MAPLIBRE_JS": MAPLIBRE_JS, "MAPLIBRE_CSS": MAPLIBRE_CSS,
        "MAPLIBRE_JS_SRI": MAPLIBRE_JS_SRI, "MAPLIBRE_CSS_SRI": MAPLIBRE_CSS_SRI,
    })
    return Jinja2Templates(env=env)


TEMPLATES = build_templates()

# ---------------------------------------------------------------- flash správy (podpísaný cookie)
FLASH_COOKIE = "vlaky_flash"


def _flash_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings().secret_key, salt="vlaky.flash.v1")


def secure_cookies() -> bool:
    return settings().public_url.lower().startswith("https://")


def redirect(url: str, message: str = "", kind: str = "ok") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if message:
        resp.set_cookie(FLASH_COOKIE, _flash_serializer().dumps([kind, message]), max_age=120, httponly=True,
                        samesite="lax", secure=secure_cookies(), path="/")
    return resp


def read_flash(request: Request) -> Optional[dict]:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    try:
        kind, text = _flash_serializer().loads(raw, max_age=300)
    except (BadSignature, ValueError, TypeError):
        return None
    return {"kind": kind if kind in ("ok", "chyba", "info", "varovanie") else "info", "text": str(text)}


def nav_counts(user) -> dict:
    """Počty pre navigáciu (fronta na potvrdenie, nové kritické odporúčania)."""
    if user is None:
        return {}
    from sqlalchemy import func

    from ..db import session_scope
    from ..models import Order, Recommendation

    from .data import pending_queue_count

    try:
        with session_scope() as s:
            queue = pending_queue_count(s)
            critical = s.scalar(select(func.count(Recommendation.id)).where(
                Recommendation.state == "nove", Recommendation.priority == "KRITICKÉ")) or 0
            orders = s.scalar(select(func.count(Order.id)).where(Order.status.in_(("nova", "navrhnuta")))) or 0
        return {"fronta": queue, "kriticke": critical, "objednavky": orders}
    except Exception:  # noqa: BLE001 – navigácia nesmie zhodiť stránku
        log.exception("Počty pre navigáciu sa nepodarilo načítať")
        return {}


_ASSET_VERSION: dict = {}


def asset_version() -> str:
    """Verzia statických súborov (cache busting) – podľa času zmeny súborov pri štarte."""
    if "v" not in _ASSET_VERSION:
        import hashlib

        h = hashlib.sha1()
        for f in sorted(STATIC_DIR.rglob("*")):
            if f.is_file():
                h.update(f"{f.relative_to(STATIC_DIR)}:{f.stat().st_mtime_ns}".encode())
        _ASSET_VERSION["v"] = h.hexdigest()[:10]
    return _ASSET_VERSION["v"]


def render(request: Request, template: str, ctx: Optional[dict] = None, user=None,
           status_code: int = 200) -> HTMLResponse:
    flash = read_flash(request)
    cfg = settings()
    base = {
        "request": request, "user": user, "csrf_token": getattr(user, "csrf", ""), "flash": flash,
        "path": request.url.path, "carrier": cfg.carrier_name, "env": cfg.env, "shadow_mode": cfg.shadow_mode,
        "now": now_utc(), "tz": cfg.timezone, "counts": nav_counts(user), "asset_version": asset_version(),
    }
    base.update(ctx or {})
    resp = TEMPLATES.TemplateResponse(request, template, base, status_code=status_code)
    if flash:
        resp.delete_cookie(FLASH_COOKIE, path="/")
    return resp


# ---------------------------------------------------------------- formuláre
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class FormReader:
    """Načítanie a kontrola polí formulára; chyby sa zbierajú do self.errors (pole → text)."""

    def __init__(self, form: Mapping[str, Any]) -> None:
        self.form = form
        self.errors: dict[str, str] = {}

    def raw(self, name: str) -> str:
        v = self.form.get(name)
        return "" if v is None else str(v).strip()

    def values(self) -> dict:
        return {k: (str(v) if not hasattr(v, "filename") else "") for k, v in self.form.items()}

    def text(self, name: str, label: str, required: bool = False, maxlen: int = 200) -> str:
        v = self.raw(name)
        if required and not v:
            self.errors[name] = f"{label}: povinné pole."
        elif len(v) > maxlen:
            self.errors[name] = f"{label}: najviac {maxlen} znakov."
            v = v[:maxlen]
        return v

    def number(self, name: str, label: str, required: bool = False, minimum: Optional[float] = None,
               maximum: Optional[float] = None, integer: bool = False) -> Optional[float]:
        v = self.raw(name).replace(",", ".").replace(" ", "")
        if not v:
            if required:
                self.errors[name] = f"{label}: povinné pole."
            return None
        try:
            num = float(v)
            if num != num or num in (float("inf"), float("-inf")):
                raise ValueError
            if integer:
                if num != int(num):
                    raise ValueError
                num = int(num)
        except ValueError:
            self.errors[name] = f"{label}: zadajte {'celé ' if integer else ''}číslo."
            return None
        if minimum is not None and num < minimum:
            self.errors[name] = f"{label}: najmenej {fmt_num(minimum, 2 if minimum < 1 else 0)}."
        elif maximum is not None and num > maximum:
            self.errors[name] = f"{label}: najviac {fmt_num(maximum, 2 if maximum <= 1 else 0)}."
        return num

    def flag(self, name: str) -> bool:
        return self.raw(name).lower() in ("1", "on", "true", "ano", "áno", "yes")

    def choice(self, name: str, label: str, choices: Iterable[str], default: Optional[str] = None) -> str:
        v = self.raw(name)
        allowed = list(choices)
        if not v and default is not None:
            return default
        if v not in allowed:
            self.errors[name] = f"{label}: neplatná hodnota."
            return default if default is not None else (allowed[0] if allowed else "")
        return v

    def local_dt(self, name: str, label: str, required: bool = False) -> Optional[datetime]:
        """Miestny čas z <input type="datetime-local"> → UTC."""
        v = self.raw(name)
        if not v:
            if required:
                self.errors[name] = f"{label}: povinné pole."
            return None
        for pattern in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
            try:
                return local_naive_to_utc(datetime.strptime(v, pattern))
            except ValueError:
                continue
        self.errors[name] = f"{label}: neplatný dátum a čas."
        return None

    def day(self, name: str, label: str, required: bool = False) -> str:
        v = self.raw(name)
        if not v:
            if required:
                self.errors[name] = f"{label}: povinné pole."
            return ""
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError:
            self.errors[name] = f"{label}: dátum v tvare RRRR-MM-DD."
            return ""

    def hhmm(self, name: str, label: str) -> str:
        v = self.raw(name)
        if v and not _HHMM.match(v):
            self.errors[name] = f"{label}: čas v tvare HH:MM."
            return ""
        return v

    def csv_list(self, name: str, maxlen: int = 500) -> list:
        v = self.raw(name)[:maxlen]
        return [x.strip() for x in re.split(r"[,;\n]", v) if x.strip()]

    @property
    def ok(self) -> bool:
        return not self.errors


class NetworkUnavailable(RuntimeError):
    pass


def resolve_station(value: str) -> Optional[str]:
    """Oficiálny názov dopravne z registra siete (alebo None, ak neexistuje)."""
    try:
        from ..siet.graf import network

        st = network().find_station(value)
    except Exception as exc:  # noqa: BLE001 – sieť nemusí byť k dispozícii
        log.exception("Register siete nie je k dispozícii")
        raise NetworkUnavailable(str(exc)) from exc
    return st.name if st is not None else None


def station_or_error(reader: FormReader, name: str, label: str, required: bool = True) -> str:
    """Názov dopravne overený proti registru siete (pravidlo 3: žiadne vymyslené dáta)."""
    value = reader.text(name, label, required=required, maxlen=120)
    if not value:
        return ""
    try:
        canonical = resolve_station(value)
    except NetworkUnavailable:
        reader.errors[name] = f"{label}: register siete nie je k dispozícii."
        return value
    if canonical is None:
        reader.errors[name] = f"{label}: dopravňa „{value}“ nie je v registri siete."
        return value
    return canonical


def safe_next(target: Optional[str], default: str = "/") -> str:
    """Iba relatívne cesty v rámci aplikácie (ochrana pred otvoreným presmerovaním)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return default
    return target


def plus_hours(dt: datetime, hours: float) -> datetime:
    return dt + timedelta(hours=hours)

