"""Zdravie agenta: heartbeat, stav komponentov a kontrola pre monitoring (`python -m vlaky health`)."""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from sqlalchemy import select, text

from ..cas import now_utc
from ..config import settings
from ..db import session_scope
from ..models import AgentHealth, KeyValue


def record(session, component: str, status: str, **detail) -> None:
    session.add(AgentHealth(component=component, status=status, detail=detail, ts=now_utc()))


def set_kv(session, key: str, value: dict) -> None:
    kv = session.scalar(select(KeyValue).where(KeyValue.key == key))
    if kv is None:
        session.add(KeyValue(key=key, value=value))
    else:
        kv.value = value


def get_kv(session, key: str) -> dict:
    kv = session.scalar(select(KeyValue).where(KeyValue.key == key))
    return dict(kv.value) if kv and isinstance(kv.value, dict) else {}


def ping_heartbeat(ok: bool, message: str = "") -> bool:
    """Ping na externý monitoring (napr. healthchecks.io). Pri chybe /fail."""
    url = settings().heartbeat_url
    if not url:
        return False
    try:
        target = url if ok else url.rstrip("/") + "/fail"
        httpx.post(target, content=message.encode("utf-8")[:10000], timeout=10)
        return True
    except httpx.HTTPError:
        return False


def health_status() -> tuple:
    """(ok, detail) – DB dostupná, posledný cyklus agenta a kontrola schránky nie sú staré."""
    detail: dict = {}
    ok = True
    try:
        with session_scope() as s:
            s.execute(text("SELECT 1"))
            detail["db"] = "ok"
            cycle = get_kv(s, "agent_last_cycle")
            mail = get_kv(s, "mail_last_ok")
    except Exception as exc:  # noqa: BLE001
        return False, {"db": f"chyba: {exc!r}"}
    now = now_utc()
    for name, kv, limit in (("cyklus", cycle, 10), ("schranka", mail, 30)):
        ts = kv.get("ts")
        if not ts:
            detail[name] = "zatiaľ neprebehol"
            ok = False
            continue
        age = (now - datetime.fromisoformat(ts)).total_seconds() / 60
        detail[name] = f"pred {age:.0f} min"
        if age > limit:
            ok = False
            detail[name] += f" (viac ako {limit} min!)"
    detail["stav"] = "OK" if ok else "PROBLÉM"
    return ok, detail


def components_overview(session) -> list:
    """Posledný stav každého komponentu (pre web /zdravie)."""
    out = []
    comps = [c for (c,) in session.execute(select(AgentHealth.component).distinct())]
    for c in comps:
        last = session.scalar(select(AgentHealth).where(AgentHealth.component == c)
                              .order_by(AgentHealth.ts.desc(), AgentHealth.id.desc()))
        out.append(last)
    return out


def purge_old(session, days: int = 30) -> None:
    cutoff = now_utc() - timedelta(days=days)
    for row in session.scalars(select(AgentHealth).where(AgentHealth.ts < cutoff)).all():
        session.delete(row)
