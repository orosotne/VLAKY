"""Používatelia a heslá (bcrypt)."""
from __future__ import annotations

import re

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import audit
from .models import User

ROLES = ("dispecer", "veduci", "planovac", "obchod", "admin")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")  # rovnako ako formulár vo webe (Používatelia)


def normalize_username(username: str) -> str:
    """Prihlasovacie meno ako vo webe: malé písmená, 3 – 80 znakov (písmená, číslice, . _ -)."""
    name = (username or "").strip().lower()
    if not USERNAME_RE.match(name):
        raise ValueError("Prihlasovacie meno: 3–80 znakov (malé písmená, číslice, . _ -), začína písmenom alebo "
                         "číslicou.")
    return name


def find_user(session: Session, username: str) -> User | None:
    return session.scalar(select(User).where(User.username == username))


def _check_lengths(full_name: str | None, email: str | None, phone: str | None) -> None:
    for label, value, limit in (("Meno a priezvisko", full_name, 160), ("E-mail", email, 200),
                                ("Telefón", phone, 40)):
        if value is not None and len(value) > limit:
            raise ValueError(f"{label}: najviac {limit} znakov.")


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Heslo musí mať aspoň 10 znakov.")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        return False


def create_user(session: Session, username: str, password: str, role: str = "dispecer",
                full_name: str = "", email: str = "", phone: str = "", actor: str = "admin") -> User:
    """Nový používateľ. Existujúci účet sa nikdy potichu neprepíše (heslo, rola, kontakty ani zrušený účet) –
    zmena existujúceho účtu je update_user."""
    if role not in ROLES:
        raise ValueError(f"Neznáma rola {role}")
    _check_lengths(full_name, email, phone)
    if find_user(session, username) is not None:
        raise ValueError(f"Používateľ {username} už existuje.")
    user = User(username=username, password_hash=hash_password(password), role=role, full_name=full_name,
                email=email, phone=phone, active=True)
    session.add(user)
    session.flush()
    audit(session, actor, "create_user", "user", user.id, username=username, role=role)
    return user


def update_user(session: Session, username: str, *, password: str | None = None, role: str | None = None,
                full_name: str | None = None, email: str | None = None, phone: str | None = None,
                activate: bool = False, actor: str = "admin") -> tuple:
    """Zmena existujúceho účtu – len zadané údaje (None = bez zmeny); zrušený účet povolí len activate=True.
    Vráti (používateľ, zoznam zmenených údajov); zmena sa zapíše do auditu (heslo bez hodnoty)."""
    user = find_user(session, username)
    if user is None:
        raise ValueError(f"Používateľ {username} neexistuje.")
    if role is not None and role not in ROLES:
        raise ValueError(f"Neznáma rola {role}")
    _check_lengths(full_name, email, phone)
    changes: dict = {}
    if password is not None:
        user.password_hash = hash_password(password)
        changes["password"] = "zmenené"
    for field, value in (("role", role), ("full_name", full_name), ("email", email), ("phone", phone)):
        if value is not None and getattr(user, field) != value:
            changes[field] = [getattr(user, field), value]
            setattr(user, field, value)
    if activate and not user.active:
        changes["active"] = [False, True]
        user.active = True
    if changes:
        session.flush()
        audit(session, actor, "user_update", "user", user.id, username=username, changes=changes)
    return user, list(changes)


_DUMMY_HASH: list = []


def _dummy_hash() -> str:
    if not _DUMMY_HASH:
        _DUMMY_HASH.append(bcrypt.hashpw(b"neexistujuci-pouzivatel", bcrypt.gensalt()).decode("ascii"))
    return _DUMMY_HASH[0]


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username, User.active.is_(True)))
    if user is None:
        verify_password(password, _dummy_hash())  # rovnaký čas odpovede – neprezradí, či účet existuje
        return None
    if verify_password(password, user.password_hash):
        return user
    return None
