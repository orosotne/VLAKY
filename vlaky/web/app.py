"""Webová aplikácia Agenta dispečera: živá mapa, fronta na potvrdenie, odporúčania, registre, API.

Spustenie: `python -m vlaky web` (uvicorn, factory `vlaky.web.app:create_app`).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import settings
from .auth import CsrfError, LoginLimiter, NotAuthenticated, PasswordChecks, PermissionDenied, optional_user
from .common import MAPLIBRE_VERSION, STATIC_DIR, log, render, tile_csp_source

DEFAULT_SECRET = "zmen-ma-v-produkcii"
WEAK_SECRETS = {DEFAULT_SECRET, "changeme", "change-me", "secret", "test-secret", "tajne", "heslo", "password"}


def production_problems(cfg) -> list:
    """Nastavenia, s ktorými web v produkcii nesmie bežať (podvrhnuteľné odkazy rušňovodiča, cookies bez Secure)."""
    problems = []
    key = cfg.secret_key or ""
    if len(key) < 32 or key.strip().lower() in WEAK_SECRETS or len(set(key)) < 8:
        problems.append("VLAKY_SECRET_KEY musí byť náhodný reťazec s aspoň 32 znakmi (openssl rand -hex 32).")
    if not (cfg.public_url or "").lower().startswith("https://"):
        problems.append("VLAKY_PUBLIC_URL musí začínať https:// (napr. https://dispecing.dopravca.example).")
    return problems


def build_csp(tile_source: str = "") -> str:
    """Content-Security-Policy: len vlastný pôvod + MapLibre (presná verzia na unpkg.com) + mapové dlaždice."""
    tiles = f" {tile_source}" if tile_source else ""
    maplibre = f"https://unpkg.com/maplibre-gl@{MAPLIBRE_VERSION}/dist/"  # nie celý unpkg.com (ľubovoľný balík)
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' {maplibre}",
        f"style-src 'self' {maplibre}",
        f"img-src 'self' data: blob:{tiles}",
        f"connect-src 'self'{tiles}",
        "worker-src 'self' blob:",
        "child-src blob:",
        "font-src 'self'",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


class SecurityHeadersMiddleware:
    """Bezpečnostné hlavičky pre každú odpoveď (čisté ASGI – nebráni streamovaniu SSE)."""

    def __init__(self, app: ASGIApp, hsts: bool, csp: str) -> None:
        self.app = app
        self.hsts = hsts
        self.csp = csp.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                extra = [
                    (b"content-security-policy", self.csp),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"permissions-policy", b"geolocation=(self), camera=(), microphone=(), payment=(), usb=()"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                ]
                if self.hsts:
                    extra.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                if b"cache-control" not in names and (ctype.startswith(b"text/html")
                                                      or ctype.startswith(b"application/json")):
                    extra.append((b"cache-control", b"no-store"))
                headers.extend((k, v) for k, v in extra if k not in names)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _is_api(request: Request) -> bool:
    p = request.url.path
    return p.startswith("/api/") or p == "/health"


# chyby overenia údajov (pydantic) po slovensky – text pre dispečera, rušňovodiča (PWA) aj správcu brány polôh
_VALIDATION_TEXT = {
    "missing": "chýba",
    "int_parsing": "musí byť celé číslo", "int_type": "musí byť celé číslo", "int_from_float": "musí byť celé číslo",
    "float_parsing": "musí byť číslo", "float_type": "musí byť číslo", "finite_number": "musí byť konečné číslo",
    "bool_parsing": "musí byť áno alebo nie (true/false)", "bool_type": "musí byť áno alebo nie (true/false)",
    "string_type": "musí byť text", "list_type": "musí byť zoznam", "dict_type": "musí byť objekt (JSON)",
    "model_type": "musí byť objekt (JSON)", "model_attributes_type": "musí byť objekt (JSON)",
    "json_invalid": "nie je platný JSON", "json_type": "nie je platný JSON",
    "datetime_parsing": "musí byť dátum a čas (ISO 8601)", "datetime_type": "musí byť dátum a čas (ISO 8601)",
    "datetime_from_date_parsing": "musí byť dátum a čas (ISO 8601)",
    "date_parsing": "musí byť dátum (RRRR-MM-DD)", "date_type": "musí byť dátum (RRRR-MM-DD)",
    "date_from_datetime_parsing": "musí byť dátum (RRRR-MM-DD)",
    "literal_error": "má nepovolenú hodnotu", "enum": "má nepovolenú hodnotu",
    "extra_forbidden": "nie je povolené",
}
_CHARS = ("znak", "znaky", "znakov")
_ITEMS = ("položku", "položky", "položiek")
_VALIDATION_LIMIT = {
    "string_too_short": ("min_length", "musí mať aspoň {}", _CHARS),
    "string_too_long": ("max_length", "môže mať najviac {}", _CHARS),
    "too_short": ("min_length", "musí mať aspoň {}", _ITEMS),
    "too_long": ("max_length", "môže mať najviac {}", _ITEMS),
    "greater_than_equal": ("ge", "musí byť najmenej {}", None), "less_than_equal": ("le", "musí byť najviac {}", None),
    "greater_than": ("gt", "musí byť viac ako {}", None), "less_than": ("lt", "musí byť menej ako {}", None),
}
_LOCATION = {"query": "parameter adresy", "path": "časť adresy", "body": "pole", "form": "pole formulára",
             "header": "hlavička", "cookie": "cookie"}
_SHAPE_ERRORS = {"list_type", "dict_type", "model_type", "model_attributes_type"}


def _union_tag(part) -> bool:
    """Časť umiestnenia chyby, ktorá je typ alternatívy (napr. „PositionIn“, „list[PositionIn]“), nie názov poľa."""
    return isinstance(part, str) and (part[:1].isupper() or "[" in part or "-" in part)


def validation_errors(errors: list) -> list:
    """Chyby overenia bez šumu alternatív: pri tele „objekt alebo zoznam objektov“ chyba „musí byť zoznam“ nepomôže,
    ak je k dispozícii chyba konkrétneho poľa."""
    specific = [e for e in errors if str(e.get("type") or "") not in _SHAPE_ERRORS]
    return specific or list(errors)


def validation_text(error: dict) -> str:
    """Jedna chyba overenia (RequestValidationError.errors()) po slovensky: „Parameter adresy „strana“: musí byť celé
    číslo.“, „Položka 2, pole „lat“: musí byť najviac 90.“ Vlastné texty validátorov (ValueError v slovenčine) sa
    prevezmú bez anglického „Value error,“."""
    from .common import fmt_num_short

    loc = list(error.get("loc") or [])
    kind = str(error.get("type") or "")
    parts = [x for x in loc[1:] if not _union_tag(x)]
    index = [int(x) + 1 for x in parts if isinstance(x, int) or str(x).isdigit()]
    names = [str(x) for x in parts if not (isinstance(x, int) or str(x).isdigit()) and str(x)]
    where = _LOCATION.get(str(loc[0]), "pole") if loc else "pole"
    field = f"{where} „{'.'.join(names)}“" if names else ""
    items = ", ".join(f"položka {i}" for i in index) if kind != "json_invalid" else ""  # pri JSON je to pozícia znaku
    place = ", ".join(p for p in (items, field) if p) or "údaje"
    ctx = error.get("ctx") or {}
    if kind in ("value_error", "assertion_error") and ctx.get("error"):
        what = str(ctx["error"])
    elif kind in _VALIDATION_LIMIT and _VALIDATION_LIMIT[kind][0] in ctx:
        key, text, words = _VALIDATION_LIMIT[kind]
        limit = fmt_num_short(ctx[key])
        if words:
            n = int(ctx[key]) if str(ctx[key]).isdigit() else 5
            limit += " " + (words[0] if n == 1 else words[1] if 2 <= n <= 4 else words[2])
        what = text.format(limit)
    else:
        what = _VALIDATION_TEXT.get(kind, "má neplatnú hodnotu")
    return f"{place[:1].upper()}{place[1:]}: {what}."


def _startup() -> None:
    from ..db import init_db, session_scope
    from ..models import BorderCrossing

    cfg = settings()
    if cfg.env == "prod":
        problems = production_problems(cfg)
        if problems:
            raise RuntimeError("Web sa v produkcii nespustí: " + " ".join(problems))
    elif cfg.secret_key == DEFAULT_SECRET:
        log.warning("Používa sa predvolený VLAKY_SECRET_KEY – nastavte vlastný (premenná prostredia).")
    init_db()
    with session_scope() as s:
        if not s.scalar(select(func.count(BorderCrossing.id))):
            try:
                from ..hranice import seed_borders

                log.info("Nahraté hraničné priechody: %s", seed_borders(s))
            except Exception:  # noqa: BLE001 – aplikácia beží aj bez registra priechodov
                log.exception("Hraničné priechody sa nepodarilo nahrať")


def create_app(auto_evaluate: bool = True) -> FastAPI:
    """Vytvorí aplikáciu. auto_evaluate=False vypne prepočet vlakov na pozadí (testy)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await run_in_threadpool(_startup)
        yield

    app = FastAPI(title="Agent dispečer", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.login_limiter = LoginLimiter()                          # podľa IP adresy (IPv6 podľa /64)
    app.state.account_limiter = LoginLimiter(max_failures=10)          # podľa účtu (pokusy z viacerých IP)
    app.state.password_checks = PasswordChecks(limit=3, wait_s=2.0)    # súčasné overenia hesla (bcrypt)
    app.state.auto_evaluate = auto_evaluate

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    cfg = settings()
    app.add_middleware(SecurityHeadersMiddleware, hsts=cfg.public_url.lower().startswith("https://"),
                       csp=build_csp(tile_csp_source(cfg.map_tile_url)))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from . import (api, auth, rusnovodic, views_dopravne, views_evidencia, views_objednavky, views_prehlad,
                   views_reporty, views_sprava)

    app.include_router(auth.router)
    app.include_router(api.router)
    app.include_router(rusnovodic.router)
    app.include_router(views_prehlad.router)
    app.include_router(views_evidencia.router)
    app.include_router(views_sprava.router)
    app.include_router(views_objednavky.router)
    app.include_router(views_dopravne.router)
    app.include_router(views_reporty.router)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC_DIR / "img" / "ikona.svg", media_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, exc: NotAuthenticated):
        if _is_api(request):
            return JSONResponse({"detail": "Prihláste sa."}, status_code=401)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)

    @app.exception_handler(PermissionDenied)
    async def _forbidden(request: Request, exc: PermissionDenied):
        if _is_api(request):
            return JSONResponse({"detail": exc.message}, status_code=403)
        return render(request, "chyba.html", {"title": "Prístup zamietnutý", "message": exc.message},
                      user=optional_user(request), status_code=403)

    @app.exception_handler(CsrfError)
    async def _csrf(request: Request, exc: CsrfError):
        msg = "Neplatný bezpečnostný token formulára (CSRF). Obnovte stránku a skúste to znova."
        if _is_api(request):
            return JSONResponse({"detail": msg}, status_code=403)
        return render(request, "chyba.html", {"title": "Formulár sa nepodarilo odoslať", "message": msg},
                      user=optional_user(request), status_code=403)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if _is_api(request) or exc.status_code not in (404, 405):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers=getattr(exc, "headers", None))
        title = "Stránka neexistuje" if exc.status_code == 404 else "Nepovolená metóda"
        message = exc.detail if exc.status_code == 404 and exc.detail not in (None, "Not Found") else \
            "Požadovaná stránka alebo záznam neexistuje."
        return render(request, "chyba.html", {"title": title, "message": message},
                      user=optional_user(request), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # „msg“ ostáva pôvodný (pre vývojárov brány polôh), „text“ a stránka sú po slovensky
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", ""),
                   "text": validation_text(e)} for e in validation_errors(exc.errors())]
        texts = list(dict.fromkeys(e["text"] for e in errors))
        if _is_api(request):
            return JSONResponse({"detail": ("Neplatné údaje – " + " ".join(texts))[:500] if texts else "Neplatné údaje.",
                                 "errors": errors}, status_code=422)
        message = " ".join(texts)[:500] + " Skontrolujte adresu alebo formulár a skúste to znova."
        return render(request, "chyba.html", {"title": "Neplatné údaje", "message": message.strip()},
                      user=optional_user(request), status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        log.exception("Neočakávaná chyba pri %s %s", request.method, request.url.path)
        if _is_api(request):
            return JSONResponse({"detail": "Vnútorná chyba servera."}, status_code=500)
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Chyba</title><h1>Vnútorná chyba servera</h1>"
                            "<p>Skúste to znova. Ak chyba pretrváva, kontaktujte správcu.</p><p><a href='/'>Späť"
                            "</a></p>", status_code=500)

    return app

