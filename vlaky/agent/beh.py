"""Hlavný beh Agenta dispečera (sekcia 2.1 PROMPT.md).

Jeden cyklus:
  1. schránka agenta → uloženie, overenie odosielateľa, extrakcia, automatické použitie istých udalostí,
  2. prepočet vlakov (po zmene z e-mailu hneď, inak každých VLAKY_REEVALUATE_MINUTES),
  3. odporúčania dispečerom podľa priority (len interný zoznam),
  4. súhrn (INFO), odovzdávka služby, denná správa o stave, heartbeat na externý monitoring.

Agent beží v jednej inštancii (zámok v databáze). Chyba jedného kroku nezastaví ostatné. Nedostupný server
správ (SMTP, Teams, SMS) sa v cykle skúsi len raz a zlyhané správy sa opakujú s rastúcim odstupom – cyklus
(čítanie schránky) sa tým nezdrží; kým niečo ostáva nedoručené, cyklus hlási chybu (heartbeat /fail, zdravie).

Čítanie schránky môže trvať dlho (nahromadená pošta, časové limity jazykového modelu) – kroky po ňom (prepočet,
upozornenia, stráženie, odovzdávka) počítajú s aktuálnym časom, nie so začiatkom cyklu. Kým v schránke ostávajú
ďalšie e-maily nad jednu dávku, kontrola schránky nie je úplná (plánovač nedá ZELENÚ) a ďalší cyklus príde hneď;
zhoršenie ZELENÁ → ORANŽOVÁ len kvôli dočítavaniu sa neposiela a po dočítaní sa vlaky hneď prepočítajú.
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
from .zdravie import get_kv, mailbox_status, ping_heartbeat, purge_old, record, set_kv, undelivered

_STOP = {"flag": False}
LOCK_KEY = "agent_lock"
ORDER_FAILURES_KEY = "objednavky_chyby"  # objednávky, ktorých návrh opakovane zlyháva: {id: {n, ts, error}}
ORDER_MAX_FAILURES = 3                   # potom dispečeri dostanú upozornenie a objednávka „chyba plánu“
HANDOVER_LATE_MIN = 90                   # zmeškaná odovzdávka (agent stál, dlhý cyklus) sa doplní do 90 min po výmene
BACKLOG_PAUSE_S = 5.0                    # pauza medzi cyklami, kým sa dočítava nahromadená pošta


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
    mail_eval_errors: list = []
    try:
        from ..posta.spracovanie import process_mailbox

        with session_scope() as s:
            mail = _process_mailbox(process_mailbox, s, now)
            summary["mail"] = mail
            # chyby jednotlivých vlakov pri prepočte, ktorý spustilo spracovanie pošty (evaluate_all ich dáva sem)
            mail_eval_errors = list(s.info.get("evaluate_errors") or []) if mail.get("planning") else []
            applied = bool(mail.get("auto_applied"))
            fetch_ok = mail.get("fetch_ok")
            if fetch_ok is None:  # spracovanie pošty bez príznaku – zlyhanie zdroja podľa chybových správ
                fetch_ok = not any(_is_source_error(e) for e in mail.get("errors") or [])
                if fetch_ok:  # čas kontroly schránky inak zapisuje spracovanie pošty (len úplnú kontrolu)
                    set_kv(s, "mail_last_ok", {"ts": now.isoformat(), "fetched": mail.get("fetched", 0)})
            if not fetch_ok:
                src_err = next((e for e in mail.get("errors") or [] if _is_source_error(e)),
                               (mail.get("errors") or ["neznáma chyba"])[0])
                summary["errors"].append(f"schránka sa nedá čítať: {src_err}")
            elif mail.get("drained") is False:
                # nahromadená pošta sa dočítava po dávkach: bez ZELENEJ (plánovač), chyba až keď to trvá dlho
                mail_ok, mail_text = mailbox_status(get_kv(s, "mail_last_ok"), now)
                if not mail_ok:
                    summary["errors"].append(f"schránka: {mail_text}")
            record(s, "schranka", "error" if not fetch_ok else ("warn" if mail.get("errors") else "ok"),
                   **{k: v for k, v in mail.items() if k != "errors"},
                   errors=(mail.get("errors") or [])[:5])
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"schránka: {exc!r}")
        with session_scope() as s:
            record(s, "schranka", "error", error=repr(exc)[:500])

    # spracovanie pošty mohlo trvať dlho (nahromadená pošta, časový limit jazykového modelu pri každom e-maile):
    # prepočet, upozornenia, stráženie a odovzdávka nesmú počítať so začiatkom cyklu
    now = max(now, now_utc())

    # 2) prepočet vlakov
    try:
        with session_scope() as s:
            last = get_kv(s, "last_evaluate")
            mail = summary.get("mail") or {}
            planning = mail.get("planning")
            # prepočet po e-maile prepočítal všetky vlaky – pravidelný prepočet v tom istom cykle by bol zbytočne
            # druhý. Ak zlyhal celý, agent prepočíta všetky vlaky hneď sám; ak len pri niektorých vlakoch (tie už
            # majú záznam o chybe výpočtu), znova len tie – nie celý vozový park v každom cykle s e-mailom.
            mail_planned = bool((planning or {}).get("ok"))
            mail_failed = planning is not None and not mail_planned
            retry_ids = list(dict.fromkeys(e["train_id"] for e in mail_eval_errors if e.get("train_id"))) \
                if mail_planned else []
            if mail_failed or retry_ids:
                err = (planning or {}).get("error") or \
                    (mail_eval_errors[0].get("error", "") if mail_eval_errors else "")
                record(s, "planovanie", "warn", prepocet_po_emaile="zlyhal", chyba=str(err)[:300],
                       vlaky=retry_ids[:20])
            # schránka sa ešte dočítava (nahromadená pošta): prepočet teraz plánovač obmedzí na ORANŽOVÚ – zapamätá
            # sa to a po dočítaní sa vlaky hneď prepočítajú znova (nie až pravidelným prepočtom o 15 min)
            mail_state = get_kv(s, "mail_last_ok")
            incomplete = bool(mail_state.get("partial"))
            if mail_planned and not force_evaluate:
                set_kv(s, "last_evaluate", {"ts": now.isoformat(), "count": (planning or {}).get("recommendations"),
                                            "z_emailu": True, "neuplna_schranka": incomplete})
            after_backlog = not incomplete and not mail_planned and bool(last.get("neuplna_schranka")) and \
                bool(mail.get("fetch_ok"))
            periodic = not last or \
                (now - datetime.fromisoformat(last["ts"])).total_seconds() >= cfg.reevaluate_minutes * 60
            due = force_evaluate or mail_failed or after_backlog or (applied and not mail_planned) or \
                (not mail_planned and periodic and not _catching_up(mail_state, now))
            if due:
                from ..planovanie.planovac import evaluate_all

                trigger = "nové údaje z e-mailu" if applied or mail_failed else \
                    ("schránka dočítaná" if after_backlog else "pravidelný prepočet")
                recs = evaluate_all(s, now, trigger)
                summary["evaluated"] = len(recs)
                set_kv(s, "last_evaluate", {"ts": now.isoformat(), "count": len(recs), "neuplna_schranka": incomplete})
                ev_errors = list(s.info.get("evaluate_errors") or [])
                if ev_errors:
                    summary["errors"].append(f"plánovanie: {len(ev_errors)} vlak(ov) sa nepodarilo prepočítať "
                                             f"({ev_errors[0].get('error', '')[:200]})")
                record(s, "planovanie", "warn" if ev_errors else "ok", vlaky=len(recs), chyby=ev_errors[:5])
            elif retry_ids:
                ev_errors = _retry_trains(s, retry_ids, now, "nové údaje z e-mailu")
                summary["evaluated"] = len(retry_ids) - len(ev_errors)
                if ev_errors:
                    summary["errors"].append(f"plánovanie: {len(ev_errors)} vlak(ov) sa nepodarilo prepočítať "
                                             f"({ev_errors[0].get('error', '')[:200]})")
                record(s, "planovanie", "warn" if ev_errors else "ok", vlaky=len(retry_ids), chyby=ev_errors[:5],
                       opakovanie_po_emaile=True)
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
            # kým sa dočítava nahromadená pošta, zhoršenie ZELENÁ → ORANŽOVÁ pri rovnakom rozhodnutí počká: plánovač
            # ho dal preto, že schránka nie je dočítaná, a po dočítaní býva vlak znova ZELENÝ – dispečeri by o každom
            # vlaku dostali zbytočnú správu. Ak ostane ORANŽOVÉ, pošle sa po dočítaní (najneskôr o 15 min).
            hold = _catching_up(get_kv(s, "mail_last_ok"), now)
            for rec in recs:
                rec_id = rec.id
                if hold and not attempts.get(rec_id) and _mailbox_downgrade(s, rec):
                    summary["held_back"] = summary.get("held_back", 0) + 1
                    continue
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
                n_und = len(und)
                what = "1 správu" if n_und == 1 else f"{n_und} správy" if n_und < 5 else f"{n_und} správ"
                summary["errors"].append(f"doručenie: {what} sa nepodarilo doručiť ({channels}) – "
                                         f"opakuje sa s odstupom")
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"kontrola doručenia: {exc!r}")
    # ostrá prevádzka: kanál s príjemcami bez servera správ (chýbajúce nastavenie, preklep) hlási každý cyklus,
    # aj keď práve nič neodchádza – inak by sa na to prišlo až pri prvej KRITICKEJ správe
    try:
        from .zdravie import config_status

        conf_ok, conf_text = config_status()
        if not conf_ok:
            summary["errors"].append(f"nastavenie odosielania: {conf_text}")
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"kontrola nastavení: {exc!r}")

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


# stav posledného čítania schránky v dennej správe po slovensky (v databáze sú kódy ok/warn/error)
_MAIL_STATUS = {"ok": "v poriadku", "warn": "upozornenie", "error": "chyba"}


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
        # odovzdávka služby v hodinách výmeny; kto ju nedostal, dostane tú istú správu (rovnaký kľúč) s odstupom.
        # Zmeškaná výmena (agent stál, cyklus s dlhým čítaním schránky prekryl okno) sa doplní do HANDOVER_LATE_MIN.
        shift = _last_shift_change(loc, [int(h) for h in cfg.csv(cfg.shift_change_hours)])
        key = f"{shift.date().isoformat()}-{shift.hour:02d}" if shift is not None else ""
        done = get_kv(s, "handover_done")
        if shift is not None and done.get("key") != key and loc - shift < timedelta(minutes=HANDOVER_LATE_MIN):
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
            from .zdravie import detail_lines, health_status

            ok, detail = health_status(ignore_keys=(f"daily:{dkey}",))  # vlastný zlyhaný pokus sa nepočíta
            borders = s.scalar(select(BorderCrossing.id).limit(1)) is not None
            from ..models import AgentHealth, Email
            from .zdravie import queue_counts

            day_ago = now - timedelta(hours=24)
            mails = s.scalar(select(func.count(Email.id)).where(Email.received_at >= day_ago)) or 0
            quarantined = s.scalar(select(func.count(Email.id)).where(Email.received_at >= day_ago,
                                                                      Email.verdict == "karantena")) or 0
            queue = queue_counts(s)  # rovnaké číslo ako fronta na webe (aj automaticky zapísané, neoverené výluky)
            last_mail = s.scalar(select(AgentHealth).where(AgentHealth.component == "schranka")
                                 .order_by(AgentHealth.id.desc()))
            # nedoručené = správy (príjemca a kanál), ktoré stále čakajú – nie počet neúspešných pokusov
            pending = len(undelivered(s, now, (f"daily:{dkey}",)))
            failed = s.scalar(select(func.count(Notification.id)).where(Notification.created_at >= day_ago,
                                                                        Notification.status == "chyba")) or 0
            body = "\n".join([f"Denná správa Agenta dispečera ({dkey})", "",
                              *detail_lines(detail),
                              f"posledný stav čítania schránky: "
                              f"{_MAIL_STATUS.get(last_mail.status, last_mail.status) if last_mail else 'neznámy'}",
                              f"e-maily za 24 h: {mails} (v karanténe {quarantined})",
                              f"udalosti čakajúce na potvrdenie: {queue['total']}"
                              + (f" (z toho zapísané automaticky – overte: {queue['auto']})" if queue["auto"] else ""),
                              f"nedoručené správy: {pending} (neúspešné pokusy o doručenie za 24 h: {failed})",
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

    def one_shot(s) -> None:
        # jednorazové upozornenia (hlásenie rušňovodiča z webu, pripomienka výluky do odvolania), ktoré neprišli
        from ..notifikacie.odosielanie import retry_one_shot

        out = retry_one_shot(s, now)
        if out:
            summary["one_shot_retried"] = len(out)

    for name, task in (("opakovanie upozornení", one_shot), ("súhrn", digest), ("odovzdávka", handover),
                       ("denná správa", daily), ("uchovávanie", retention)):
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
    _print_config_warnings()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    cfg = settings()
    failures = 0
    last_errors: list = []
    try:
        while True:
            started = time.monotonic()
            summary: dict = {}
            try:
                summary = run_cycle()
                failures = 0
                last_errors = list(summary.get("errors") or [])
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
            if _backlog(summary):
                sleep = BACKLOG_PAUSE_S  # nahromadená pošta: ďalšia dávka hneď (cyklus ostáva krátky)
            if failures:
                sleep = max(sleep, min(300.0, 10.0 * 2 ** min(failures, 5)))  # postupne dlhšia pauza
            end = time.monotonic() + sleep
            while time.monotonic() < end and not _STOP["flag"]:
                time.sleep(1.0)
            if _STOP["flag"]:
                break
    finally:
        _release_lock()
    # jeden cyklus (cron, test): chyby v cykle (napr. schránka sa nedá prečítať) = návratový kód 1
    return 1 if once and last_errors else 0


def _print_config_warnings() -> None:
    """Pri štarte: kanál s príjemcami bez servera správ alebo neznáma premenná VLAKY_* (preklep) – do logu."""
    try:
        from .zdravie import config_status

        ok, text = config_status()
        if text != "ok":
            print(f"Agent – {'CHYBA' if not ok else 'upozornenie'} v nastavení odosielania: {text}", flush=True)
    except Exception:  # noqa: BLE001 – kontrola nastavení nesmie zastaviť agenta
        print("Agent – kontrolu nastavení sa nepodarilo urobiť:\n" + traceback.format_exc(), flush=True)


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


def _last_shift_change(loc: datetime, hours: list) -> datetime | None:
    """Posledná výmena služby (miestny čas, celá hodina z VLAKY_SHIFT_CHANGE_HOURS) najneskôr v čase loc."""
    best = None
    for day in (loc, loc - timedelta(days=1)):
        for h in hours:
            if not 0 <= h <= 23:
                continue
            t = day.replace(hour=h, minute=0, second=0, microsecond=0)
            if t <= loc and (best is None or t > best):
                best = t
    return best


def _retry_trains(session, train_ids: list, now: datetime, trigger: str) -> list:
    """Znova prepočíta len vlaky, ktorých prepočet po e-maile zlyhal (každý vo vlastnom SAVEPOINT, chyba jedného
    nezastaví ostatné). Vráti chyby ako evaluate_all: [{"train_id", "error"}]. Záznam o chybe výpočtu (ESKALOVAŤ –
    posúďte vlak bez agenta) vlak dostal už pri prepočte po e-maile; trvalo chybný vlak sa tak nepočíta celý vozový
    park dvakrát v každom cykle s e-mailom."""
    from ..db import prepare_savepoints
    from ..planovanie.planovac import evaluate_train

    errors: list = []
    prepare_savepoints(session)
    for tid in train_ids:
        try:
            with session.begin_nested():
                evaluate_train(session, tid, now, trigger)
        except Exception as exc:  # noqa: BLE001 – jeden chybný vlak nesmie zastaviť ostatné
            errors.append({"train_id": tid, "error": repr(exc)[:300]})
    return errors


def _mailbox_downgrade(session, rec) -> bool:
    """Odporúčanie len zhoršilo semafor ZELENÁ → ORANŽOVÁ pri rovnakom rozhodnutí ako predchádzajúce odporúčanie
    vlaku (DÔLEŽITÉ). Nie KRITICKÉ, nie zmena rozhodnutia, nie prvé odporúčanie vlaku – tie sa posielajú hneď."""
    if rec.priority != "DÔLEŽITÉ" or rec.color != "ORANŽOVÁ":
        return False
    prev = session.scalar(select(Recommendation).where(Recommendation.train_id == rec.train_id,
                                                        Recommendation.id < rec.id)
                          .order_by(Recommendation.id.desc()).limit(1))
    return prev is not None and prev.decision == rec.decision and prev.color == "ZELENÁ"


def _catching_up(mail_state: dict, now: datetime) -> bool:
    """Nahromadená pošta sa dočítava (ďalší cyklus o pár sekúnd) a netrvá to dlhšie ako VLAKY_REEVALUATE_MINUTES.

    Pravidelný prepočet vtedy počká na dočítanie: plánovač by všetkým vlakom dal ORANŽOVÚ (schránka nie je dočítaná)
    a po dočítaní znova ZELENÚ – dispečeri by dostali dve zbytočné správy o každom vlaku. Prepočet po e-maile (napr.
    nová výluka) beží hneď; jeho zhoršenie ZELENÁ → ORANŽOVÁ pri rovnakom rozhodnutí sa medzitým neposiela
    (_mailbox_downgrade) a po dočítaní sa vlaky hneď prepočítajú (``neuplna_schranka`` v ``last_evaluate``)."""
    if not mail_state.get("partial"):
        return False
    try:
        since = datetime.fromisoformat(str(mail_state.get("since")))
    except (TypeError, ValueError):
        return False
    return (now - since).total_seconds() < settings().reevaluate_minutes * 60


def _backlog(summary: dict) -> bool:
    """Cyklus prečítal schránku len čiastočne (ďalšie e-maily čakajú) – ďalší cyklus má prísť hneď. Nie pri chybe
    (napr. e-mail sa nedal uložiť): jeho ďalší pokus má prísť s bežným odstupom, nie o pár sekúnd."""
    mail = summary.get("mail") or {}
    return bool(mail.get("fetch_ok")) and mail.get("drained") is False and not mail.get("errors")


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
