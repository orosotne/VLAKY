"""Zdravie agenta: heartbeat, stav komponentov a kontrola pre monitoring (`python -m vlaky health`)."""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select, text

from ..cas import ensure_utc, now_utc
from ..config import settings
from ..db import session_scope
from ..models import AgentHealth, KeyValue, Notification, Order, Recommendation, Train


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


def undelivered(session, now: datetime | None = None, ignore_keys: tuple = ()) -> list:
    """Správy, ktoré sa nepodarilo doručiť a stále čakajú – pre heartbeat a zdravie (výpadok SMTP, Teams, SMS).

    Posledný neúspešný pokus (stav „chyba“) pre kľúč a príjemcu sa počíta, kým tomuto príjemcovi neprišla tá
    istá správa (odoslaná alebo potlačená ako duplicita) a kým sa ešte opakuje:
      - odporúčanie, kým je aktuálne (nové, nie INFO, vlak nie je ukončený, najviac 48 h – RETRY_HOURS),
      - návrh k objednávke, kým objednávka čaká na návrh,
      - ostatné (súhrn, denná správa, odovzdávka, upozornenia) najviac 90 min po poslednom pokuse (opakujú sa
        najneskôr každých 60 min) a len kým tomuto príjemcovi neprišla iná správa (kanál opäť funguje).
    Príjemcovia, ktorí už nie sú na internom zozname, sa nepočítajú.
    """
    from ..notifikacie.odosielanie import RETRY_HOURS, RETRY_MAX_MIN, SENT_STATES, allowed_recipients
    from ..planovanie.planovac import DONE_STATES

    now = now or now_utc()
    horizon = now - timedelta(hours=RETRY_HOURS)
    last_ids = [i for (i,) in session.execute(
        select(func.max(Notification.id)).where(Notification.status == "chyba", Notification.created_at >= horizon)
        .group_by(Notification.dedup_key, Notification.channel, Notification.recipient))]
    if not last_ids:
        return []
    fails = session.execute(select(Notification.id, Notification.kind, Notification.dedup_key, Notification.channel,
                                   Notification.recipient, Notification.recommendation_id, Notification.created_at,
                                   Notification.error)
                            .where(Notification.id.in_(last_ids)).order_by(Notification.id.desc())).all()
    first = min(last_ids)
    same_later = {(k, ch, r): i for k, ch, r, i in session.execute(
        select(Notification.dedup_key, Notification.channel, Notification.recipient, func.max(Notification.id))
        .where(Notification.id > first, Notification.status.in_(SENT_STATES + ("potlacena",)))
        .group_by(Notification.dedup_key, Notification.channel, Notification.recipient))}
    pair_later = {(ch, r): i for ch, r, i in session.execute(
        select(Notification.channel, Notification.recipient, func.max(Notification.id))
        .where(Notification.id > first, Notification.status.in_(SENT_STATES))
        .group_by(Notification.channel, Notification.recipient))}
    rec_ids = {f.recommendation_id for f in fails if f.kind == "odporucanie" and f.recommendation_id}
    live_recs = set(session.scalars(
        select(Recommendation.id).join(Train, Train.id == Recommendation.train_id)
        .where(Recommendation.id.in_(rec_ids), Recommendation.state == "nove", Recommendation.priority != "INFO",
               Recommendation.created_at >= horizon, Train.status.notin_(DONE_STATES)))) if rec_ids else set()
    order_ids = {int(f.dedup_key.split(":")[1]) for f in fails if _order_key(f.dedup_key)}
    open_orders = set(session.scalars(select(Order.id).where(Order.id.in_(order_ids), Order.status == "nova"))) \
        if order_ids else set()
    allowed = allowed_recipients()
    recent = now - timedelta(minutes=RETRY_MAX_MIN + 30)
    out = []
    for f in fails:
        if f.dedup_key in ignore_keys or \
                f.recipient.lower().replace(" ", "") not in {r.lower() for r in allowed.get(f.channel, set())}:
            continue
        if same_later.get((f.dedup_key, f.channel, f.recipient), 0) > f.id:
            continue  # tá istá správa už tomuto príjemcovi prišla
        if f.kind == "odporucanie" and f.recommendation_id:
            pending = f.recommendation_id in live_recs
        elif _order_key(f.dedup_key):
            pending = int(f.dedup_key.split(":")[1]) in open_orders
        else:
            pending = ensure_utc(f.created_at) >= recent and pair_later.get((f.channel, f.recipient), 0) < f.id
        if pending:
            out.append(f)
    return out


def _order_key(key: str) -> bool:
    parts = (key or "").split(":")
    return len(parts) == 2 and parts[0] == "order" and parts[1].isdigit()


def health_status(ignore_keys: tuple = ()) -> tuple:
    """(ok, detail) – DB dostupná, posledný cyklus agenta a kontrola schránky nie sú staré, správy sa doručujú.

    ignore_keys – kľúče správ, ktoré sa do doručenia nezapočítajú (denná správa pri vlastnom opakovaní).
    """
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
    # trvalý výpadok doručovania (SMTP, Teams, SMS) musí byť vidieť aj tu, nielen v cykle, ktorý práve opakuje
    try:
        with session_scope() as s:
            und = undelivered(s, now, ignore_keys)
        if und:
            ok = False
            last = und[0]
            age = (now - ensure_utc(last.created_at)).total_seconds() / 60
            detail["dorucenie"] = (f"{len(und)} nedoručených správ ({', '.join(sorted({u.channel for u in und}))}),"
                                   f" posledný pokus pred {age:.0f} min: {(last.error or '')[:200]}")
        else:
            detail["dorucenie"] = "ok"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail["dorucenie"] = f"kontrola zlyhala: {exc!r}"[:300]
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
