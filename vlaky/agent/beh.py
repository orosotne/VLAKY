"""Hlavný beh Agenta dispečera (sekcia 2.1 PROMPT.md).

Jeden cyklus:
  1. schránka agenta → uloženie, overenie odosielateľa, extrakcia, automatické použitie istých udalostí,
  2. prepočet vlakov (po zmene z e-mailu hneď, inak každých VLAKY_REEVALUATE_MINUTES),
  3. odporúčania dispečerom podľa priority (len interný zoznam),
  4. súhrn (INFO), odovzdávka služby, denná správa o stave, heartbeat na externý monitoring.

Agent beží v jednej inštancii (zámok v databáze). Chyba jedného kroku nezastaví ostatné. Nedostupný server
správ (SMTP, Teams, SMS) sa v cykle skúsi len raz a zlyhané správy sa opakujú s rastúcim odstupom – cyklus
(čítanie schránky) sa tým nezdrží; kým niečo ostáva nedoručené, cyklus hlási chybu (heartbeat /fail, zdravie).
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
from ..cas import local, now_utc
from ..config import settings
from ..db import init_db, session_scope
from ..models import BorderCrossing, Notification, Recommendation, Train
from .zdravie import get_kv, ping_heartbeat, purge_old, record, set_kv, undelivered

_STOP = {"flag": False}
LOCK_KEY = "agent_lock"
ORDER_FAILURES_KEY = "objednavky_chyby"  # objednávky, ktorých návrh opakovane zlyháva: {id: {n, ts, error}}
ORDER_MAX_FAILURES = 3                   # potom dispečeri dostanú upozornenie a objednávka „chyba plánu“


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
    from ..notifikacie.odosielanie import delivery_scope

    with delivery_scope():  # istič: nedostupný kanál sa v tomto cykle skúsi len raz
        return _run_cycle(now, force_evaluate)


def _run_cycle(now: datetime | None, force_evaluate: bool) -> dict:
    cfg = settings()
    now = now or now_utc()
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
        from ..notifikacie.odosielanie import RETRY_HOURS, notify_recommendation, recommendation_attempts
        from ..planovanie.planovac import DONE_STATES

        with session_scope() as s:
            # každé aktuálne odporúčanie, ktoré ešte nebolo doručené (bez ohľadu na to, kto ho vytvoril:
            # cyklus agenta, prepočet z webu, import) – aj po dlhom výpadku servera, najviac však 48 h
            # (RETRY_HOURS) a nie pre ukončený vlak; zlyhané doručenie sa zopakuje s odstupom
            # (5, 10, 20, 40, 60 min; KRITICKÉ pri výpadku kanála každých 5 min) a len tým príjemcom, ktorým správa
            # neprišla; chyba jedného odporúčania nezastaví ostatné ani nevráti späť záznamy o odoslaných správach
            since = now - timedelta(hours=RETRY_HOURS)
            q = (select(Recommendation).join(Train, Train.id == Recommendation.train_id)
                 .where(Recommendation.state == "nove", Recommendation.priority != "INFO",
                        Train.status.notin_(DONE_STATES),
                        (Recommendation.id > start_rec_id) | (Recommendation.created_at >= since))
                 .order_by(Recommendation.id))
            recs = list(s.scalars(q))
            attempts = recommendation_attempts(s, [r.id for r in recs])
            for rec in recs:
                rec_id = rec.id
                try:
                    pairs = _pending_pairs(s, rec_id, attempts.get(rec_id, []))
                    if pairs is None or pairs:
                        summary["notified"] += len(notify_recommendation(s, rec, only=pairs))
                        s.commit()  # záznamy o odoslaných správach sa pri chybe ďalšieho odporúčania nevrátia späť
                except Exception as exc:  # noqa: BLE001 – chybné odporúčanie nezastaví doručenie ostatných
                    s.rollback()
                    summary["errors"].append(f"odporúčanie #{rec_id}: {_short_error(exc)}")
                    record(s, "planovanie", "error", rec=rec_id, error=repr(exc)[:500],
                           trace=traceback.format_exc()[-2000:])
                    s.commit()
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"plánovanie: {exc!r}")
        with session_scope() as s:
            record(s, "planovanie", "error", error=repr(exc)[:500], trace=traceback.format_exc()[-2000:])

    # 2b) nové objednávky zákazníkov → návrh prepravy dispečerovi (chybná objednávka nezastaví ostatné)
    try:
        order_errors: list = []
        summary["orders"] = propose_new_orders(now, order_errors)
        summary["errors"] += [f"objednávky: {e}" for e in order_errors]
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"objednávky: {exc!r}")

    # 2c) stráženie: pracovný čas rušňovodičov (90/60/30 min), neplánované státie vlakov
    try:
        from .strazenie import crew_alerts, standing_alerts

        with session_scope() as s:
            # chyba pri jednom vlaku/zmene nezastaví ostatné ani nevráti už odoslané upozornenia (SAVEPOINT na každé)
            summary["crew_alerts"] = crew_alerts(s, now, summary["errors"])
            summary["standing_alerts"] = standing_alerts(s, now, summary["errors"])
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"stráženie: {exc!r}")

    # 3) súhrn, odovzdávka, denná správa (stav cyklu sa zapíše vopred, aby denná správa videla tento cyklus)
    with session_scope() as s:
        set_kv(s, "agent_last_cycle", {"ts": now.isoformat(), "errors": len(summary["errors"])})
    try:
        _periodic(now, summary)
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"periodické úlohy: {exc!r}")

    # 3b) nedoručené správy → chyba KAŽDÉHO cyklu, kým čakajú (nielen cyklu s pokusom – opakuje sa s odstupom),
    # aby externý monitoring (heartbeat /fail) a zdravie trvalý výpadok doručovania spoľahlivo videli.
    # Bez adries príjemcov (heartbeat ide mimo firmy) – podrobnosti sú na stránke /zdravie.
    try:
        with session_scope() as s:
            und = undelivered(s, now_utc())
            if und:
                channels = ", ".join(sorted({u.channel for u in und}))
                summary["errors"].append(f"doručenie: {len(und)} správ(a) sa nepodarilo doručiť ({channels}) – "
                                         f"opakuje sa s odstupom")
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"kontrola doručenia: {exc!r}")

    # 4) heartbeat
    with session_scope() as s:
        set_kv(s, "agent_last_cycle", {"ts": now.isoformat(), "errors": len(summary["errors"])})
        record(s, "cyklus", "ok" if not summary["errors"] else "warn", errors=summary["errors"][:5])
        hb = get_kv(s, "heartbeat")
        if not hb or (now - datetime.fromisoformat(hb["ts"])).total_seconds() >= cfg.heartbeat_minutes * 60:
            ping_heartbeat(not summary["errors"], "; ".join(summary["errors"])[:1000])
            set_kv(s, "heartbeat", {"ts": now.isoformat()})
    return summary


def propose_new_orders(now: datetime, errors: list | None = None) -> int:
    """Pre každú novú objednávku (status „nova“) pripraví návrh prepravy a pošle ho dispečerom.

    Každá objednávka má vlastnú transakciu aj ošetrenie chýb – chybná objednávka nezastaví ostatné. Chyba sa
    zapíše do zdravia a do `errors` (chyba cyklu, kým trvá), ďalší pokus príde s odstupom; po
    ORDER_MAX_FAILURES pokusoch dostanú dispečeri upozornenie a objednávka prejde do stavu „chyba plánu“.
    """
    from ..models import Order
    from ..notifikacie.odosielanie import retry_delay_minutes, retry_due, send_internal
    from ..notifikacie.texty import order_proposal_text
    from ..planovanie import planovac

    errors = errors if errors is not None else []
    with session_scope() as s:
        ids = list(s.scalars(select(Order.id).where(Order.status == "nova").order_by(Order.id)))
        stored = get_kv(s, ORDER_FAILURES_KEY)
    failing = {k: v for k, v in stored.items() if k.isdigit() and int(k) in ids}
    n = 0
    for oid in ids:
        fail = failing.get(str(oid))
        if fail and now_utc() < datetime.fromisoformat(fail["ts"]) + \
                timedelta(minutes=retry_delay_minutes(int(fail.get("n", 1)))):
            errors.append(f"objednávka #{oid}: návrh sa nedá pripraviť ({fail.get('error', '')[:150]}) – "
                          f"ďalší pokus s odstupom")
            continue
        try:
            with session_scope() as s:
                order = s.get(Order, oid)
                if order is None or order.status != "nova":
                    continue
                if not retry_due(s, f"order:{oid}"):
                    continue  # zlyhané doručenie sa zopakuje s odstupom (5, 10, 20, 40, 60 min)
                plan = planovac.plan_order(s, order.id, now)
                subject, body = order_proposal_text(order, plan)
                out = send_internal(s, "objednavka", subject, body, "DÔLEŽITÉ", dedup_key=f"order:{order.id}",
                                    channels=["email", "teams"])
                delivered = _delivered(out)
                if delivered or not out:
                    order.status = "navrhnuta" if not plan.get("error") else "chyba_planu"
                audit(s, "agent", "order_proposal", "order", order.id, decision=plan.get("decision"),
                      error=plan.get("error", ""), delivered=delivered)
                n += 1
            failing.pop(str(oid), None)
        except Exception as exc:  # noqa: BLE001 – chyba jednej objednávky nezastaví ostatné
            k = int((fail or {}).get("n", 0)) + 1
            failing[str(oid)] = {"n": k, "ts": now_utc().isoformat(), "error": repr(exc)[:300]}
            errors.append(f"objednávka #{oid}: {exc!r}"[:300])
            with session_scope() as s:
                record(s, "objednavky", "error", order=oid, attempts=k, error=repr(exc)[:500],
                       trace=traceback.format_exc()[-2000:])
            if k >= ORDER_MAX_FAILURES and _order_failed(oid, exc):
                failing.pop(str(oid), None)
    if failing != stored:
        with session_scope() as s:
            set_kv(s, ORDER_FAILURES_KEY, failing)
    return n


def _order_failed(oid: int, exc: Exception) -> bool:
    """Návrh pre objednávku sa opakovane nedá pripraviť → upozornenie dispečerom, stav „chyba plánu“."""
    from ..models import Order
    from ..notifikacie.odosielanie import send_internal

    try:
        with session_scope() as s:
            order = s.get(Order, oid)
            if order is None or order.status != "nova":
                return True
            subject = (f"[AGENT][DÔLEŽITÉ] Objednávka #{oid} {order.origin} → {order.destination}: návrh prepravy "
                       f"sa nepodarilo pripraviť")
            body = (f"Agent opakovane nedokázal pripraviť návrh prepravy pre objednávku #{oid} "
                    f"({order.customer or 'zákazník'}, {order.origin} → {order.destination}).\n"
                    f"Chyba: {type(exc).__name__}: {str(exc)[:300]}\n\n"
                    f"Naplánujte prepravu bez agenta a skontrolujte údaje objednávky; chybu nahláste správcovi.\n"
                    f"Detail: {settings().public_url.rstrip('/')}/objednavky/{oid}")
            out = send_internal(s, "objednavka", subject, body, "DÔLEŽITÉ", dedup_key=f"order:{oid}:chyba",
                                channels=["email", "teams"])
            if out and not _delivered(out):
                return False  # upozornenie sa zopakuje pri ďalšom pokuse
            order.status = "chyba_planu"
            audit(s, "agent", "order_proposal", "order", oid, decision="", error=repr(exc)[:300],
                  delivered=_delivered(out))
        return True
    except Exception:  # noqa: BLE001
        print("Agent – upozornenie k objednávke sa nepodarilo pripraviť:\n" + traceback.format_exc(), flush=True)
        return False


def _periodic(now: datetime, summary: dict) -> None:
    """Súhrn, odovzdávka, denná správa a uchovávanie – každá úloha vo vlastnej transakcii: chyba jednej nezastaví
    ostatné a denná správa už vidí, čo sa v tomto cykle doručilo. Správa je vybavená, až keď prišla každému
    príjemcovi; tým, ktorým neprišla, sa tá istá správa (rovnaký kľúč) opakuje s odstupom 5, 10, 20, 40, 60 min."""
    cfg = settings()
    from ..models import ShiftHandoverReport
    from ..notifikacie.odosielanie import delivered_to_all, retry_targets, send_digest, send_internal
    from .odovzdavka import create_report, send_report

    loc = local(now)
    dkey = loc.date().isoformat()

    def digest(s) -> None:
        # súhrn INFO; kto ho nedostal, dostane ten istý súhrn (od toho istého času) s odstupom – najdlhšie do
        # ďalšieho súhrnu, aby trvalá chyba jedného príjemcu nezastavila súhrny ostatným
        dg = get_kv(s, "digest")
        if not dg:
            set_kv(s, "digest", {"ts": now.isoformat()})
            return
        since = datetime.fromisoformat(dg["ts"])
        key = f"digest:{since.isoformat()}"
        sent_at = dg.get("sent_at")  # súhrn už niekomu prišiel (odvtedy beží ďalší súhrn ostatným)
        if sent_at and (now - datetime.fromisoformat(sent_at)).total_seconds() >= cfg.digest_minutes * 60:
            set_kv(s, "digest", {"ts": sent_at})
            return
        if not sent_at and (now - since).total_seconds() < cfg.digest_minutes * 60:
            return
        targets = retry_targets(s, key)  # None = zatiaľ nikomu; inak len tým, ktorým neprišiel (po odstupe)
        if targets is not None and not targets:
            if delivered_to_all(s, key):  # už prišiel všetkým (napr. stav súhrnu sa nestihol uložiť)
                set_kv(s, "digest", {"ts": sent_at or now.isoformat()})
            return
        out = send_digest(s, since, only=targets)
        if not out or delivered_to_all(s, key):
            set_kv(s, "digest", {"ts": sent_at or now.isoformat()})
        elif not sent_at and _delivered(out):
            set_kv(s, "digest", {"ts": dg["ts"], "sent_at": now.isoformat()})

    def handover(s) -> None:
        # odovzdávka služby v hodinách výmeny; kto ju nedostal, dostane tú istú správu (rovnaký kľúč) s odstupom
        hours = [int(h) for h in cfg.csv(cfg.shift_change_hours)]
        key = f"{loc.date().isoformat()}-{loc.hour:02d}"
        done = get_kv(s, "handover_done")
        if loc.hour in hours and done.get("key") != key and loc.minute < 30:
            rep = create_report(s, now, notify=False)
            send_report(s, rep)
            set_kv(s, "handover_done", {"key": key, "report_id": rep.id,
                                        "sent": delivered_to_all(s, f"odovzdavka:{rep.id}")})
            summary["handover_report"] = True
        elif done.get("report_id") and done.get("sent") is False:
            hkey = f"odovzdavka:{done['report_id']}"
            rep = s.get(ShiftHandoverReport, done["report_id"])
            targets = retry_targets(s, hkey)
            if rep is not None and (targets is None or targets):
                send_report(s, rep, only=targets)
            if rep is None or delivered_to_all(s, hkey):
                set_kv(s, "handover_done", {**done, "sent": True})

    def daily(s) -> None:
        # denná správa o stave agenta; kto ju nedostal, dostane ju (s aktuálnym stavom) s odstupom
        if loc.hour >= cfg.daily_report_hour and get_kv(s, "daily_report").get("date") != dkey:
            targets = retry_targets(s, f"daily:{dkey}")
            if targets is not None and not targets:
                if delivered_to_all(s, f"daily:{dkey}"):
                    set_kv(s, "daily_report", {"date": dkey})
                return
            from .zdravie import health_status

            ok, detail = health_status(ignore_keys=(f"daily:{dkey}",))  # vlastný zlyhaný pokus sa nepočíta
            borders = s.scalar(select(BorderCrossing.id).limit(1)) is not None
            from ..models import AgentHealth, Email, ExtractedEvent

            day_ago = now - timedelta(hours=24)
            mails = s.scalar(select(func.count(Email.id)).where(Email.received_at >= day_ago)) or 0
            quarantined = s.scalar(select(func.count(Email.id)).where(Email.received_at >= day_ago,
                                                                      Email.verdict == "karantena")) or 0
            from ..posta.spracovanie import failed_confirmations  # potvrdené, ale zápis zlyhal – čakajú vo fronte

            waiting = (s.scalar(select(func.count(ExtractedEvent.id)).where(
                ExtractedEvent.status == "navrh", ExtractedEvent.needs_confirmation.is_(True))) or 0) + \
                len(failed_confirmations(s))
            last_mail = s.scalar(select(AgentHealth).where(AgentHealth.component == "schranka")
                                 .order_by(AgentHealth.id.desc()))
            failed = s.scalar(select(func.count(Notification.id)).where(Notification.created_at >= day_ago,
                                                                        Notification.status == "chyba")) or 0
            body = "\n".join([f"Denná správa Agenta dispečera ({dkey})", "",
                              *(f"{k}: {v}" for k, v in detail.items()),
                              f"posledný stav čítania schránky: {last_mail.status if last_mail else 'neznámy'}",
                              f"e-maily za 24 h: {mails} (v karanténe {quarantined})",
                              f"udalosti čakajúce na potvrdenie: {waiting}",
                              f"nedoručené správy za 24 h: {failed}",
                              f"hraničné priechody v databáze: {'áno' if borders else 'NIE'}",
                              f"tieňová prevádzka: {'áno' if cfg.shadow_mode else 'nie'}"])
            send_internal(s, "zdravie", f"[AGENT] Denná správa – {'OK' if ok else 'PROBLÉM'}", body, "INFO",
                          dedup_key=f"daily:{dkey}", channels=["email"], only=targets)
            if delivered_to_all(s, f"daily:{dkey}"):
                set_kv(s, "daily_report", {"date": dkey})

    def retention(s) -> None:
        if loc.hour >= cfg.daily_report_hour and get_kv(s, "retention_done").get("date") != dkey:
            purge_old(s)
            from .strazenie import retention_purge

            summary["retention"] = retention_purge(s, now)
            set_kv(s, "retention_done", {"date": dkey})

    for name, task in (("súhrn", digest), ("odovzdávka", handover), ("denná správa", daily),
                       ("uchovávanie", retention)):
        try:
            with session_scope() as s:
                task(s)
        except Exception as exc:  # noqa: BLE001 – chyba jednej úlohy nezastaví ostatné
            summary["errors"].append(f"periodické úlohy ({name}): {exc!r}")


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


def _short_error(exc: Exception) -> str:
    """Chyba pre súhrn cyklu a heartbeat (ide mimo firmy): len typ a prvý riadok – bez parametrov SQL príkazu
    (napr. adries príjemcov); úplný popis s výpisom je v zdraví agenta."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text.splitlines()[0] if text else ''}"[:200]


def _pending_pairs(session, rec_id: int, rows: list | None = None) -> set | None:
    """Komu treba odporúčanie poslať: None = ešte nikomu (všetkým podľa priority); inak len dvojice (kanál,
    príjemca), ktorým sa ho nepodarilo doručiť a uplynul odstup (5, 10, 20, 40, 60 min) – ostatní ho už majú
    (doručené, potlačené ako duplicita alebo zablokované)."""
    from ..notifikacie.odosielanie import retry_pairs

    return retry_pairs(session, rec_id, rows)
