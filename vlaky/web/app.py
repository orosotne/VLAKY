"""Webová aplikácia Agenta dispečera: živá mapa, fronta na potvrdenie, odporúčania, registre, API.

Spustenie: `python -m vlaky web` (uvicorn, factory `vlaky.web.app:create_app`).
"""
from __future__ import annotations

import logging
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
from .auth import CsrfError, LoginLimiter, NotAuthenticated, PermissionDenied, optional_user
from .common import STATIC_DIR, log, render

DEFAULT_SECRET = "zmen-ma-v-produkcii"

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://unpkg.com",
    "style-src 'self' https://unpkg.com",
    "img-src 'self' data: blob: https://tile.openstreetmap.org",
    "connect-src 'self' https://tile.openstreetmap.org",
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

    def __init__(self, app: ASGIApp, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                extra = [
                    (b"content-security-policy", CSP.encode()),
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


def _startup() -> None:
    from ..db import init_db, session_scope
    from ..models import BorderCrossing

    cfg = settings()
    if cfg.secret_key == DEFAULT_SECRET:
        if cfg.env == "prod":
            raise RuntimeError("VLAKY_SECRET_KEY nie je nastavený – v produkcii je povinný.")
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
    app.state.login_limiter = LoginLimiter()
    app.state.auto_evaluate = auto_evaluate

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings().public_url.lower().startswith("https://"))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from . import api, auth, rusnovodic, views_evidencia, views_prehlad, views_sprava

    app.include_router(auth.router)
    app.include_router(api.router)
    app.include_router(rusnovodic.router)
    app.include_router(views_prehlad.router)
    app.include_router(views_evidencia.router)
    app.include_router(views_sprava.router)

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
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
                  for e in exc.errors()]
        if _is_api(request):
            return JSONResponse({"detail": "Neplatné údaje.", "errors": errors}, status_code=422)
        return render(request, "chyba.html", {"title": "Neplatné údaje",
                                              "message": "; ".join(e["msg"] for e in errors)[:500]},
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


logging.getLogger("vlaky.web").setLevel(logging.INFO)
