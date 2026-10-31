"""Používatelia a heslá (bcrypt)."""
from __future__ import annotations

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import audit
from .models import User

ROLES = ("dispecer", "veduci", "planovac", "obchod", "admin")


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
                full_name: str = "", email: str = "", phone: str = "") -> User:
    if role not in ROLES:
        raise ValueError(f"Neznáma rola {role}")
    user = session.scalar(select(User).where(User.username == username))
    if user is None:
        user = User(username=username)
        session.add(user)
    user.password_hash = hash_password(password)
    user.role, user.full_name, user.email, user.phone, user.active = role, full_name, email, phone, True
    session.flush()
    audit(session, "admin", "create_user", "user", user.id, username=username, role=role)
    return user


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username, User.active.is_(True)))
    if user and verify_password(password, user.password_hash):
        return user
    return None
