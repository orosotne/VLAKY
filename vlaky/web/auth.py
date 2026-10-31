"""Prihlásenie, relácia (podpísaný cookie), roly a oprávnenia, CSRF a obmedzenie neúspešných prihlásení."""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..audit import audit
from ..config import settings
from ..db import session_scope
from ..models import User
from .common import log, redirect, render, safe_next, secure_cookies

SESSION_COOKIE = "vlaky_session"
LOGIN_CSRF_COOKIE = "vlaky_login"
SESSION_MAX_AGE = 12 * 3600          # 12 h od prihlásenia (absolútna platnosť)

# Oprávnenia rolí (sekcia 6 PROMPT.md).
PERMISSIONS: dict[str, frozenset] = {
    "dispecer": frozenset({"view", "operate", "crew"}),
    "veduci": frozenset({"view", "operate", "crew", "params", "users", "audit", "borders"}),
    "planovac": frozenset({"view", "crew"}),
    "obchod": frozenset({"view"}),
    "admin": frozenset({"view", "users", "audit"}),
}


class NotAuthenticated(Exception):
    """Požiadavka bez platnej relácie."""


class PermissionDenied(Exception):
    def __init__(self, message: str = "Na túto akciu nemáte oprávnenie.") -> None:
        super().__init__(message)
        self.message = message


class CsrfError(Exception):
    """Chýbajúci alebo neplatný CSRF token."""


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    full_name: str
    role: str
    csrf: str

    def can(self, permission: str) -> bool:
        return permission in PERMISSIONS.get(self.role, frozenset())

    @property
    def display(self) -> str:
        return self.full_name or self.username


# ---------------------------------------------------------------- relácia
def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings().secret_key, salt="vlaky.session.v1")


def _pw_fingerprint(password_hash: str) -> str:
    """Zmena hesla zneplatní existujúce relácie."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def issue_session(response, user: User) -> None:
    token = _serializer().dumps({"uid": user.id, "u": user.username, "pw": _pw_fingerprint(user.password_hash),
                                 "csrf": secrets.token_urlsafe(32)})
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
                        secure=secure_cookies(), path="/")


def clear_session(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=secure_cookies())


def _load_user(request: Request) -> Optional[CurrentUser]:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=SESSION_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("uid"), int):
        return None
    with session_scope() as s:
        user = s.get(User, data["uid"])
        if user is None or not user.active or user.username != data.get("u"):
            return None
        if data.get("pw") != _pw_fingerprint(user.password_hash):
            return None
        return CurrentUser(user.id, user.username, user.full_name or "", user.role, str(data.get("csrf", "")))


def optional_user(request: Request) -> Optional[CurrentUser]:
    if not hasattr(request.state, "vlaky_user"):
        request.state.vlaky_user = _load_user(request)
    return request.state.vlaky_user


def current_user(request: Request) -> CurrentUser:
    user = optional_user(request)
    if user is None:
        raise NotAuthenticated()
    return user


def require(permission: str):
    """Závislosť FastAPI: prihlásený používateľ s daným oprávnením."""

    def dependency(request: Request) -> CurrentUser:
        user = current_user(request)
        if not user.can(permission):
            raise PermissionDenied()
        return user

    dependency.__name__ = f"require_{permission}"
    return dependency


async def csrf_form(request: Request):
    """Formulár (alebo hlavička X-CSRF-Token) s platným CSRF tokenom aktuálnej relácie."""
    user = optional_user(request)
    if user is None:
        raise NotAuthenticated()
    form = await request.form()
    token = str(form.get("csrf_token") or request.headers.get("x-csrf-token") or "")
    if not token or not user.csrf or not secrets.compare_digest(token, user.csrf):
        raise CsrfError()
    return form


# ---------------------------------------------------------------- obmedzenie neúspešných prihlásení
class LoginLimiter:
    """Jednoduché obmedzenie v pamäti: po `max_failures` neúspechoch z jednej IP blokovať na `window_s`."""

    def __init__(self, max_failures: int = 5, window_s: int = 900) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque:
        q = self._fails[key]
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            q = self._prune(key, now)
            if len(q) >= self.max_failures:
                return max(1, int(self.window_s - (now - q[0])))
            return 0

    def fail(self, key: str) -> None:
        with self._lock:
            q = self._prune(key, time.monotonic())
            q.append(time.monotonic())
            if len(self._fails) > 10000:  # ochrana pamäte
                for k in list(self._fails)[:5000]:
                    self._fails.pop(k, None)

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "nezname"


# ---------------------------------------------------------------- stránky prihlásenia
router = APIRouter()


def _login_page(request: Request, next_url: str, error: str = "", username: str = "",
                status_code: int = 200) -> HTMLResponse:
    token = secrets.token_urlsafe(24)
    resp = render(request, "login.html", {"login_token": token, "next": next_url, "error": error,
                                          "username": username}, status_code=status_code)
    resp.set_cookie(LOGIN_CSRF_COOKIE, token, max_age=3600, httponly=True, samesite="lax",
                    secure=secure_cookies(), path="/")
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):  # noqa: A002 – názov parametra z URL
    if optional_user(request) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return _login_page(request, safe_next(next))


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username") or "").strip()[:80]
    password = str(form.get("password") or "")[:200]
    next_url = safe_next(str(form.get("next") or "/"))
    limiter: LoginLimiter = request.app.state.login_limiter
    ip = client_ip(request)

    wait = limiter.retry_after(ip)
    if wait:
        with session_scope() as s:
            audit(s, username or "?", "login_blocked", "user", "", ip=ip)
        return _login_page(request, next_url, f"Príliš veľa neúspešných pokusov. Skúste to znova o "
                           f"{max(1, wait // 60)} min.", username, status_code=429)

    cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    form_token = str(form.get("csrf_token") or "")
    if not cookie_token or not form_token or not secrets.compare_digest(cookie_token, form_token):
        return _login_page(request, next_url, "Platnosť formulára vypršala. Prihláste sa znova.", username,
                           status_code=403)

    from ..pouzivatelia import authenticate

    with session_scope() as s:
        user = authenticate(s, username, password) if username and password else None
        if user is None:
            audit(s, username or "?", "login_failed", "user", "", ip=ip)
        else:
            audit(s, user.username, "login", "user", user.id, ip=ip)
    if user is None:
        limiter.fail(ip)
        return _login_page(request, next_url, "Nesprávne meno alebo heslo.", username, status_code=401)
    limiter.reset(ip)
    resp = RedirectResponse(next_url, status_code=303)
    issue_session(resp, user)
    resp.delete_cookie(LOGIN_CSRF_COOKIE, path="/")
    log.info("Prihlásený používateľ %s", user.username)
    return resp


@router.post("/logout")
async def logout(request: Request, form=Depends(csrf_form)):
    user = optional_user(request)
    if user is not None:
        with session_scope() as s:
            audit(s, user.username, "logout", "user", user.id, ip=client_ip(request))
    resp = redirect("/login", "Boli ste odhlásený.", "info")
    clear_session(resp)
    return resp

