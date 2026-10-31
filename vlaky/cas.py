"""Práca s časom: ukladáme v UTC, zobrazujeme v Europe/Bratislava (sekcia 11 v PROMPT.md)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import settings

UTC = timezone.utc
_FAKE_NOW: dict = {}


def tz() -> ZoneInfo:
    return ZoneInfo(settings().timezone)


def now_utc() -> datetime:
    """Aktuálny čas (v testoch a simulácii sa dá posunúť cez set_fake_now)."""
    return _FAKE_NOW.get("now") or datetime.now(UTC).replace(microsecond=0)


def set_fake_now(dt: datetime | None) -> None:
    if dt is None:
        _FAKE_NOW.pop("now", None)
    else:
        _FAKE_NOW["now"] = ensure_utc(dt)


def ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def local(dt: datetime | None) -> datetime | None:
    return None if dt is None else ensure_utc(dt).astimezone(tz())


def local_naive_to_utc(dt: datetime) -> datetime:
    """Miestny čas bez zóny (napr. z e-mailu) → UTC (správne cez letný/zimný čas)."""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC)
    return dt.replace(tzinfo=tz()).astimezone(UTC)


def fmt(dt: datetime | None, with_date: bool = True) -> str:
    if dt is None:
        return "—"
    d = local(dt)
    return d.strftime("%d.%m. %H:%M") if with_date else d.strftime("%H:%M")


def minutes_between(a: datetime, b: datetime) -> float:
    return (ensure_utc(b) - ensure_utc(a)).total_seconds() / 60.0


def plus_minutes(dt: datetime, minutes: float) -> datetime:
    return ensure_utc(dt) + timedelta(minutes=minutes)
