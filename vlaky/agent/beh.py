"""Hlavný beh Agenta dispečera (sekcia 2.1 PROMPT.md).

Jeden cyklus:
  1. schránka agenta → uloženie, overenie odosielateľa, extrakcia, automatické použitie istých udalostí,
  2. prepočet vlakov (po zmene z e-mailu hneď, inak každých VLAKY_REEVALUATE_MINUTES),
  3. odporúčania dispečerom podľa priority (len interný zoznam),
  4. súhrn (INFO), odovzdávka služby, denná správa o stave, heartbeat na externý monitoring.

Agent beží v jednej inštancii (zámok v databáze). Chyba jedného kroku nezastaví ostatné.
"""
from __future__ import annotations

import os
import signal
import socket
import time
import traceback
from datetime import datetime, timedelta

from sqlalchemy import select

from ..audit import audit
from ..cas import local, now_utc
from ..config import settings
from ..db import init_db, session_scope
from ..models import BorderCrossing, Recommendation
from .zdravie import get_kv, ping_heartbeat, purge_old, record, set_kv

_STOP = {"flag": False}
LOCK_KEY = "agent_lock"


def _handle_stop(signum, _frame) -> None:  # noqa: ANN001
    _STOP["flag"] = True


def acquire_lock(session, stale_minutes: int = 3) -> bool:
    """Zabráni dvom súčasne bežiacim agentom (duplicitné správy)."""
    me = f"{socket.gethostname()}:{os.getpid()}"
    lock = get_kv(session, LOCK_KEY)
    now = now_utc()
    if lock and lock.get("owner") != me:
        ts = datetime.fromisoformat(lock.get("ts"))
        if (now - ts).total_seconds() < stale_minutes * 60:
            return False
    set_kv(session, LOCK_KEY, {"owner": me, "ts": now.isoformat()})
    return True


def run_cycle(now: datetime | None = None, force_evaluate: bool = False) -> dict:
    """Jeden cyklus agenta. Vracia súhrn (používa sa aj v testoch a pri `--once`)."""
    cfg = settings()
    now = now or now_utc()
    summary: dict = {"ts": now.isoformat(), "mail": None, "evaluated": 0, "notified": 0, "errors": []}
    with session_scope() as s:
        if not acquire_lock(s):
            summary["errors"].append("Beží iná inštancia agenta – tento cyklus sa preskočil.")
            return summary

    # 1) schránka
    applied = False
    try:
        from ..posta.spracovanie import process_mailbox

        with session_scope() as s:
            mail = process_mailbox(s)
            summary["mail"] = mail
            applied = bool(mail.get("auto_applied"))
            set_kv(s, "mail_last_ok", {"ts": now.isoformat(), "fetched": mail.get("fetched", 0)})
            record(s, "schranka", "ok" if not mail.get("errors") else "warn", **{k: v for k, v in mail.items()
                                                                                   if k != "errors"})
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"schránka: {exc!r}")
        with session_scope() as s:
            record(s, "schranka", "error", error=repr(exc)[:500])

    # 2) prepočet vlakov
    try:
        with session_scope() as s:
            last = get_kv(s, "last_evaluate")
            due = force_evaluate or applied or not last or \
                (now - datetime.fromisoformat(last["ts"])).total_seconds() >= cfg.reevaluate_minutes * 60
            if due:
                from ..planovanie.planovac import evaluate_all
                from ..notifikacie.odosielanie import notify_recommendation

                trigger = "nové údaje z e-mailu" if applied else "pravidelný prepočet"
                before = s.scalar(select(Recommendation.id).order_by(Recommendation.id.desc())) or 0
                recs = evaluate_all(s, now, trigger)
                summary["evaluated"] = len(recs)
                for rec in recs:
                    if rec.id > before:  # len nové (zmenené) odporúčania
                        summary["notified"] += len(notify_recommendation(s, rec))
                set_kv(s, "last_evaluate", {"ts": now.isoformat(), "count": len(recs)})
                record(s, "planovanie", "ok", vlaky=len(recs))
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"plánovanie: {exc!r}")
        with session_scope() as s:
            record(s, "planovanie", "error", error=repr(exc)[:500], trace=traceback.format_exc()[-2000:])

    # 2b) nové objednávky zákazníkov → návrh prepravy dispečerovi
    try:
        summary["orders"] = propose_new_orders(now)
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"objednávky: {exc!r}")

    # 3) súhrn, odovzdávka, denná správa
    try:
        _periodic(now, summary)
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"periodické úlohy: {exc!r}")

    # 4) heartbeat
    with session_scope() as s:
        set_kv(s, "agent_last_cycle", {"ts": now.isoformat(), "errors": len(summary["errors"])})
        record(s, "cyklus", "ok" if not summary["errors"] else "warn", errors=summary["errors"][:5])
        hb = get_kv(s, "heartbeat")
        if not hb or (now - datetime.fromisoformat(hb["ts"])).total_seconds() >= cfg.heartbeat_minutes * 60:
            ping_heartbeat(not summary["errors"], "; ".join(summary["errors"])[:1000])
            set_kv(s, "heartbeat", {"ts": now.isoformat()})
    return summary


def propose_new_orders(now: datetime) -> int:
    """Pre každú novú objednávku (status „nova“) pripraví návrh prepravy a pošle ho dispečerom."""
    from ..models import Order
    from ..notifikacie.odosielanie import send_internal
    from ..notifikacie.texty import order_proposal_text
    from ..planovanie.planovac import plan_order

    n = 0
    with session_scope() as s:
        for order in s.scalars(select(Order).where(Order.status == "nova")).all():
            plan = plan_order(s, order.id, now)
            subject, body = order_proposal_text(order, plan)
            send_internal(s, "objednavka", subject, body, "DÔLEŽITÉ", dedup_key=f"order:{order.id}",
                          channels=["email", "teams"])
            order.status = "navrhnuta" if not plan.get("error") else "chyba_planu"
            audit(s, "agent", "order_proposal", "order", order.id, decision=plan.get("decision"),
                  error=plan.get("error", ""))
            n += 1
    return n


def _periodic(now: datetime, summary: dict) -> None:
    cfg = settings()
    from ..notifikacie.odosielanie import send_digest, send_internal
    from .odovzdavka import create_report

    with session_scope() as s:
        # súhrn INFO
        dg = get_kv(s, "digest")
        if not dg:
            set_kv(s, "digest", {"ts": now.isoformat()})
        elif (now - datetime.fromisoformat(dg["ts"])).total_seconds() >= cfg.digest_minutes * 60:
            send_digest(s, datetime.fromisoformat(dg["ts"]))
            set_kv(s, "digest", {"ts": now.isoformat()})
        loc = local(now)
        # odovzdávka služby v hodinách výmeny
        hours = [int(h) for h in cfg.csv(cfg.shift_change_hours)]
        key = f"{loc.date().isoformat()}-{loc.hour:02d}"
        if loc.hour in hours and get_kv(s, "handover_done").get("key") != key and loc.minute < 30:
            create_report(s, now)
            set_kv(s, "handover_done", {"key": key})
            summary["handover_report"] = True
        # denná správa o stave agenta
        dkey = loc.date().isoformat()
        if loc.hour >= cfg.daily_report_hour and get_kv(s, "daily_report").get("date") != dkey:
            from .zdravie import health_status

            ok, detail = health_status()
            borders = s.scalar(select(BorderCrossing.id).limit(1)) is not None
            body = "\n".join([f"Denná správa Agenta dispečera ({dkey})", "",
                              *(f"{k}: {v}" for k, v in detail.items()),
                              f"hraničné priechody v databáze: {'áno' if borders else 'NIE'}",
                              f"tieňová prevádzka: {'áno' if cfg.shadow_mode else 'nie'}"])
            send_internal(s, "zdravie", f"[AGENT] Denná správa – {'OK' if ok else 'PROBLÉM'}", body, "INFO",
                          dedup_key=f"daily:{dkey}", channels=["email"])
            set_kv(s, "daily_report", {"date": dkey})
            purge_old(s)


def run_agent(once: bool = False) -> int:
    init_db()
    with session_scope() as s:
        from ..hranice import seed_borders

        if s.scalar(select(BorderCrossing.id).limit(1)) is None:
            seed_borders(s)
        audit(s, "agent", "start", "agent", "", once=once, pid=os.getpid())
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    cfg = settings()
    while True:
        started = time.monotonic()
        summary = run_cycle()
        if summary["errors"]:
            print("Agent – chyby v cykle:", summary["errors"], flush=True)
        if once or _STOP["flag"]:
            break
        sleep = max(5.0, cfg.mail_poll_seconds - (time.monotonic() - started))
        end = time.monotonic() + sleep
        while time.monotonic() < end and not _STOP["flag"]:
            time.sleep(1.0)
        if _STOP["flag"]:
            break
    with session_scope() as s:
        audit(s, "agent", "stop", "agent", "")
        set_kv(s, LOCK_KEY, {"owner": "", "ts": (now_utc() - timedelta(hours=1)).isoformat()})
    return 0
