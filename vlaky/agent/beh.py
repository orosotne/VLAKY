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

from sqlalchemy import func, select

from ..audit import audit
from ..cas import ensure_utc, local, now_utc
from ..config import settings
from ..db import init_db, session_scope
from ..models import BorderCrossing, Notification, Recommendation
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
    cycle_started = now_utc() - timedelta(seconds=1)
    summary: dict = {"ts": now.isoformat(), "mail": None, "evaluated": 0, "notified": 0, "errors": []}
    with session_scope() as s:
        if not acquire_lock(s):
            summary["errors"].append("Beží iná inštancia agenta – tento cyklus sa preskočil.")
            return summary
        # odporúčania vzniknuté v tomto cykle (aj pri prepočte po e-maile) sa na konci pošlú dispečerom
        start_rec_id = s.scalar(select(Recommendation.id).order_by(Recommendation.id.desc())) or 0

    # 1) schránka
    applied = False
    try:
        from ..posta.spracovanie import process_mailbox

        with session_scope() as s:
            mail = _process_mailbox(process_mailbox, s, now)
            summary["mail"] = mail
            applied = bool(mail.get("auto_applied"))
            fetch_ok = mail.get("fetch_ok")
            if fetch_ok is None:  # spracovanie pošty bez príznaku – zlyhanie zdroja podľa chybových správ
                fetch_ok = not any(_is_source_error(e) for e in mail.get("errors") or [])
            if fetch_ok:
                # čas poslednej ÚSPEŠNEJ kontroly schránky (plánovač bez neho nedá ZELENÚ, zdravie ho stráži)
                set_kv(s, "mail_last_ok", {"ts": now.isoformat(), "fetched": mail.get("fetched", 0)})
            else:
                src_err = next((e for e in mail.get("errors") or [] if _is_source_error(e)),
                               (mail.get("errors") or ["neznáma chyba"])[0])
                summary["errors"].append(f"schránka sa nedá čítať: {src_err}")
            record(s, "schranka", "error" if not fetch_ok else ("warn" if mail.get("errors") else "ok"),
                   **{k: v for k, v in mail.items() if k != "errors"},
                   errors=(mail.get("errors") or [])[:5])
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"schránka: {exc!r}")
        with session_scope() as s:
            record(s, "schranka", "error", error=repr(exc)[:500])

    # 2) prepočet vlakov
    try:
        with session_scope() as s:
            last = get_kv(s, "last_evaluate")
            mail_planned = bool(((summary.get("mail") or {}).get("planning") or {}).get("ok"))
            due = force_evaluate or (applied and not mail_planned) or not last or \
                (now - datetime.fromisoformat(last["ts"])).total_seconds() >= cfg.reevaluate_minutes * 60
            if due:
                from ..planovanie.planovac import evaluate_all

                trigger = "nové údaje z e-mailu" if applied else "pravidelný prepočet"
                recs = evaluate_all(s, now, trigger)
                summary["evaluated"] = len(recs)
                set_kv(s, "last_evaluate", {"ts": now.isoformat(), "count": len(recs)})
                ev_errors = list(s.info.get("evaluate_errors") or [])
                if ev_errors:
                    summary["errors"].append(f"plánovanie: {len(ev_errors)} vlak(ov) sa nepodarilo prepočítať "
                                             f"({ev_errors[0].get('error', '')[:200]})")
                record(s, "planovanie", "warn" if ev_errors else "ok", vlaky=len(recs), chyby=ev_errors[:5])
        # upozornenia: všetky nové (zmenené) odporúčania z tohto cyklu – z prepočtu po e-maile aj pravidelného
        from ..notifikacie.odosielanie import notify_recommendation

        with session_scope() as s:
            # každé aktuálne odporúčanie, ktoré ešte nebolo doručené (bez ohľadu na to, kto ho vytvoril:
            # cyklus agenta, prepočet z webu, import); zlyhané doručenie sa zopakuje najskôr po 5 min –
            # deduplikácia zabráni dvojitému odoslaniu tým, ktorým už správa prišla
            since = now - timedelta(hours=6)
            q = (select(Recommendation)
                 .where(Recommendation.state == "nove", Recommendation.priority != "INFO",
                        (Recommendation.id > start_rec_id) | (Recommendation.created_at >= since))
                 .order_by(Recommendation.id))
            for rec in s.scalars(q):
                if _needs_delivery(s, rec.id, now):
                    summary["notified"] += len(notify_recommendation(s, rec))
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"plánovanie: {exc!r}")
        with session_scope() as s:
            record(s, "planovanie", "error", error=repr(exc)[:500], trace=traceback.format_exc()[-2000:])

    # 2b) nové objednávky zákazníkov → návrh prepravy dispečerovi
    try:
        summary["orders"] = propose_new_orders(now)
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"objednávky: {exc!r}")

    # 2c) stráženie: pracovný čas rušňovodičov (90/60/30 min), neplánované státie vlakov
    try:
        from .strazenie import crew_alerts, standing_alerts

        with session_scope() as s:
            summary["crew_alerts"] = crew_alerts(s, now)
            summary["standing_alerts"] = standing_alerts(s, now)
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"stráženie: {exc!r}")

    # 2d) zlyhané doručenia v tomto cykle → chyba cyklu (heartbeat /fail, zdravie)
    try:
        with session_scope() as s:
            failed = s.scalar(select(func.count(Notification.id)).where(Notification.created_at >= cycle_started,
                                                                        Notification.status == "chyba")) or 0
            if failed:
                summary["errors"].append(f"doručenie: {failed} správ(a) sa nepodarilo odoslať – zopakuje sa")
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"kontrola doručenia: {exc!r}")

    # 3) súhrn, odovzdávka, denná správa (stav cyklu sa zapíše vopred, aby denná správa videla tento cyklus)
    with session_scope() as s:
        set_kv(s, "agent_last_cycle", {"ts": now.isoformat(), "errors": len(summary["errors"])})
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

    from ..notifikacie.odosielanie import SENT_STATES

    with session_scope() as s:
        ids = list(s.scalars(select(Order.id).where(Order.status == "nova").order_by(Order.id)))
    n = 0
    for oid in ids:  # každá objednávka vo vlastnej transakcii – chybná objednávka nezastaví ostatné
        with session_scope() as s:
            order = s.get(Order, oid)
            if order is None or order.status != "nova":
                continue
            last_fail = s.scalar(select(func.max(Notification.created_at)).where(
                Notification.dedup_key == f"order:{oid}", Notification.status == "chyba"))
            if last_fail is not None and (now_utc() - ensure_utc(last_fail)).total_seconds() < 300:
                continue  # zlyhané doručenie sa zopakuje najskôr po 5 min
            plan = plan_order(s, order.id, now)
            subject, body = order_proposal_text(order, plan)
            out = send_internal(s, "objednavka", subject, body, "DÔLEŽITÉ", dedup_key=f"order:{order.id}",
                                channels=["email", "teams"])
            delivered = any(x.status in SENT_STATES or x.status == "potlacena" for x in out)
            if delivered or not out:
                order.status = "navrhnuta" if not plan.get("error") else "chyba_planu"
            audit(s, "agent", "order_proposal", "order", order.id, decision=plan.get("decision"),
                  error=plan.get("error", ""), delivered=delivered)
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
            out = send_digest(s, datetime.fromisoformat(dg["ts"]))
            if not out or _delivered(out):  # pri zlyhaní sa súhrn zopakuje v ďalšom cykle
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
            out = send_internal(s, "zdravie", f"[AGENT] Denná správa – {'OK' if ok else 'PROBLÉM'}", body,
                                "INFO", dedup_key=f"daily:{dkey}", channels=["email"])
            if not out or _delivered(out):
                set_kv(s, "daily_report", {"date": dkey})
        if loc.hour >= cfg.daily_report_hour and get_kv(s, "retention_done").get("date") != dkey:
            purge_old(s)
            from .strazenie import retention_purge

            summary["retention"] = retention_purge(s, now)
            set_kv(s, "retention_done", {"date": dkey})


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
    failures = 0
    try:
        while True:
            started = time.monotonic()
            try:
                summary = run_cycle()
                failures = 0
                if summary["errors"]:
                    print("Agent – chyby v cykle:", summary["errors"], flush=True)
            except Exception:  # noqa: BLE001 – napr. krátky výpadok databázy: agent nesmie spadnúť
                failures += 1
                print("Agent – cyklus zlyhal:\n" + traceback.format_exc(), flush=True)
                if once:
                    raise
            if once or _STOP["flag"]:
                break
            sleep = max(5.0, cfg.mail_poll_seconds - (time.monotonic() - started))
            if failures:
                sleep = max(sleep, min(300.0, 10.0 * 2 ** min(failures, 5)))  # postupne dlhšia pauza
            end = time.monotonic() + sleep
            while time.monotonic() < end and not _STOP["flag"]:
                time.sleep(1.0)
            if _STOP["flag"]:
                break
    finally:
        _release_lock()
    return 0


def _release_lock() -> None:
    """Uvoľní zámok len vtedy, ak patrí tomuto procesu."""
    me = f"{socket.gethostname()}:{os.getpid()}"
    try:
        with session_scope() as s:
            audit(s, "agent", "stop", "agent", "")
            if get_kv(s, LOCK_KEY).get("owner") == me:
                set_kv(s, LOCK_KEY, {"owner": "", "ts": (now_utc() - timedelta(hours=1)).isoformat()})
    except Exception:  # noqa: BLE001 – pri nedostupnej DB zámok vyprší sám (stale_minutes)
        print("Agent – zámok sa nepodarilo uvoľniť:\n" + traceback.format_exc(), flush=True)


_SOURCE_ERROR_PREFIXES = ("Zdroj pošty zlyhal", "Príjem pošty zlyhal", "IMAP", "Prihlásenie do IMAP",
                          "Priečinok", "Vyhľadávanie v IMAP", "Microsoft Graph", "Získanie tokenu",
                          "Odpoveď Microsoft Graph", "Delta odkaz", "Čítanie schránky", "Neznámy zdroj pošty")


def _is_source_error(message: str) -> bool:
    return str(message).startswith(_SOURCE_ERROR_PREFIXES)


def _process_mailbox(fn, session, now: datetime) -> dict:
    """process_mailbox s časom cyklu (ak ho rozhranie prijíma)."""
    import inspect

    try:
        if "now" in inspect.signature(fn).parameters:
            return fn(session, now=now)
    except (TypeError, ValueError):
        pass
    return fn(session)


def _delivered(out: list) -> bool:
    from ..notifikacie.odosielanie import SENT_STATES

    return any(n.status in SENT_STATES or n.status == "potlacena" for n in out)


def _needs_delivery(session, rec_id: int, now: datetime) -> bool:
    """Odporúčanie ešte nebolo doručené každému príjemcovi (zlyhané pokusy sa opakujú najskôr po 5 min)."""
    rows = list(session.scalars(select(Notification).where(Notification.recommendation_id == rec_id)
                                .order_by(Notification.id)))
    if not rows:
        return True
    latest: dict = {}
    for n in rows:
        latest[(n.channel, n.recipient)] = n
    pending = [n for n in latest.values() if n.status == "chyba"]
    if not pending:
        return False  # doručené, potlačené ako duplicita alebo zablokované (nie je na zozname)
    newest = max(ensure_utc(n.created_at) for n in pending)
    return (now_utc() - newest).total_seconds() >= 300
