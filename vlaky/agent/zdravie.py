"""Zdravie agenta: heartbeat, stav komponentov a kontrola pre monitoring (`python -m vlaky health`)."""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, or_, select, text

from ..cas import ensure_utc, now_utc
from ..config import settings
from ..db import session_scope
from ..models import AgentHealth, KeyValue, Notification, Order, Recommendation, Train

MAILBOX_MAX_AGE_MIN = 30  # bez úspešnej (úplnej) kontroly schránky za posledných 30 min nie sú údaje čerstvé

# položky stavu agenta (health_status) po slovensky – web /zdravie aj denná správa (bez surových kľúčov)
HEALTH_DETAIL_LABELS = {"db": "Databáza", "cyklus": "Posledný cyklus agenta",
                        "schranka": "Posledná kontrola schránky", "konfiguracia": "Nastavenie odosielania správ",
                        "dorucenie": "Doručovanie správ", "stav": "Stav"}


def detail_lines(detail: dict) -> list:
    """Riadky stavu agenta po slovensky („Posledná kontrola schránky: pred 3 min“, „Databáza: v poriadku“)."""
    return [f"{HEALTH_DETAIL_LABELS.get(k, k)}: {'v poriadku' if v == 'ok' else v}" for k, v in detail.items()]


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

    Jedna položka = jedna správa a jeden príjemca na kanáli: posledný neúspešný pokus (stav „chyba“) sa počíta, kým
    tomuto príjemcovi neprišla tá istá správa (odoslaná alebo potlačená ako duplicita) a kým sa ešte opakuje:
      - odporúčanie (podľa odporúčania, nie podľa kľúča – kľúč sa pri posune odchodu mení, opakuje sa však to isté
        odporúčanie), kým je aktuálne (nové, nie INFO, vlak nie je ukončený, najviac 48 h – RETRY_HOURS),
      - návrh k objednávke, kým objednávka čaká na návrh,
      - jednorazové upozornenie, ktoré agent opakuje (hlásenie rušňovodiča, pripomienka výluky do odvolania –
        retry_one_shot), najviac RETRY_HOURS od prvého pokusu a kým platí (pripomienka zrušenej výluky nie); iná
        doručená správa ho nevybaví,
      - ostatné (súhrn, denná správa, odovzdávka, upozornenia na pracovný čas a státie, ktoré sa posielajú znova
        v každom cykle) najviac 90 min po poslednom pokuse (opakujú sa najneskôr každých 60 min) a len kým tomuto
        príjemcovi neprišla iná správa (kanál opäť funguje).
    Príjemcovia, ktorí už nie sú na internom zozname, sa nepočítajú.
    """
    from ..notifikacie.odosielanie import (RETRY_HOURS, RETRY_MAX_MIN, SENT_STATES, allowed_recipients,
                                           is_one_shot, one_shot_current)
    from ..planovanie.planovac import DONE_STATES

    now = now or now_utc()
    horizon = now - timedelta(hours=RETRY_HOURS)
    is_rec = (Notification.kind == "odporucanie") & Notification.recommendation_id.is_not(None)
    failed = (Notification.status == "chyba", Notification.created_at >= horizon)
    last_ids = [i for (i,) in session.execute(
        select(func.max(Notification.id)).where(*failed, is_rec)
        .group_by(Notification.recommendation_id, Notification.channel, Notification.recipient))]
    last_ids += [i for (i,) in session.execute(
        select(func.max(Notification.id))
        .where(*failed, or_(Notification.kind != "odporucanie", Notification.recommendation_id.is_(None)))
        .group_by(Notification.dedup_key, Notification.channel, Notification.recipient))]
    if not last_ids:
        return []
    fails = session.execute(select(Notification.id, Notification.kind, Notification.dedup_key, Notification.channel,
                                   Notification.recipient, Notification.recommendation_id, Notification.created_at,
                                   Notification.error)
                            .where(Notification.id.in_(last_ids)).order_by(Notification.id.desc())).all()
    first = min(last_ids)
    delivered = Notification.status.in_(SENT_STATES + ("potlacena",))
    same_later = {(k, ch, r): i for k, ch, r, i in session.execute(
        select(Notification.dedup_key, Notification.channel, Notification.recipient, func.max(Notification.id))
        .where(Notification.id > first, delivered)
        .group_by(Notification.dedup_key, Notification.channel, Notification.recipient))}
    pair_later = {(ch, r): i for ch, r, i in session.execute(
        select(Notification.channel, Notification.recipient, func.max(Notification.id))
        .where(Notification.id > first, Notification.status.in_(SENT_STATES))
        .group_by(Notification.channel, Notification.recipient))}
    rec_ids = {f.recommendation_id for f in fails if f.kind == "odporucanie" and f.recommendation_id}
    rec_later = {(rid, ch, r): i for rid, ch, r, i in session.execute(
        select(Notification.recommendation_id, Notification.channel, Notification.recipient, func.max(Notification.id))
        .where(Notification.id > first, Notification.recommendation_id.in_(rec_ids), delivered)
        .group_by(Notification.recommendation_id, Notification.channel, Notification.recipient))} if rec_ids else {}
    live_recs = set(session.scalars(
        select(Recommendation.id).join(Train, Train.id == Recommendation.train_id)
        .where(Recommendation.id.in_(rec_ids), Recommendation.state == "nove", Recommendation.priority != "INFO",
               Recommendation.created_at >= horizon, Train.status.notin_(DONE_STATES)))) if rec_ids else set()
    order_ids = {int(f.dedup_key.split(":")[1]) for f in fails if _order_key(f.dedup_key)}
    open_orders = set(session.scalars(select(Order.id).where(Order.id.in_(order_ids), Order.status == "nova"))) \
        if order_ids else set()
    one_shot = {f.dedup_key for f in fails if is_one_shot(f.dedup_key)}
    first_try = dict(session.execute(
        select(Notification.dedup_key, func.min(Notification.created_at)).where(Notification.dedup_key.in_(one_shot))
        .group_by(Notification.dedup_key)).all()) if one_shot else {}
    allowed = allowed_recipients()
    recent = now - timedelta(minutes=RETRY_MAX_MIN + 30)
    out = []
    for f in fails:
        if f.dedup_key in ignore_keys or \
                f.recipient.lower().replace(" ", "") not in {r.lower() for r in allowed.get(f.channel, set())}:
            continue
        if f.kind == "odporucanie" and f.recommendation_id:
            if rec_later.get((f.recommendation_id, f.channel, f.recipient), 0) > f.id:
                continue  # to isté odporúčanie už tomuto príjemcovi prišlo (aj s iným kľúčom)
            pending = f.recommendation_id in live_recs
        elif same_later.get((f.dedup_key, f.channel, f.recipient), 0) > f.id:
            continue  # tá istá správa už tomuto príjemcovi prišla
        elif _order_key(f.dedup_key):
            pending = int(f.dedup_key.split(":")[1]) in open_orders
        elif f.dedup_key in one_shot:
            started = first_try.get(f.dedup_key)
            pending = started is not None and ensure_utc(started) >= horizon and \
                one_shot_current(session, f.dedup_key)
        else:
            pending = ensure_utc(f.created_at) >= recent and pair_later.get((f.channel, f.recipient), 0) < f.id
        if pending:
            out.append(f)
    return out


def mailbox_status(kv: dict, now: datetime | None = None) -> tuple:
    """(v poriadku, popis) kontroly schránky podľa záznamu ``mail_last_ok``.

    Čiastočné čítanie (v schránke ostali ďalšie e-maily, napr. nahromadená pošta po výpadku agenta) nie je úspešná
    kontrola – plánovač vtedy nedá ZELENÚ (zdroje.mailbox_check). Pre zdravie je v poriadku, kým od poslednej úplnej
    kontroly (alebo od začiatku dočítavania, ak úplná kontrola ešte neprebehla) neuplynulo viac ako 30 min."""
    now = now or now_utc()

    def age(ts) -> float | None:
        try:
            return (now - ensure_utc(datetime.fromisoformat(str(ts)))).total_seconds() / 60
        except (TypeError, ValueError):
            return None

    kv = kv if isinstance(kv, dict) else {}
    full_ts = kv.get("last_full") if kv.get("partial") else kv.get("ts")
    full = age(full_ts) if full_ts else None
    if kv.get("partial"):
        ref = full if full is not None else age(kv.get("since"))
        text = "dočítava sa (v schránke čakajú ďalšie e-maily), posledná úplná kontrola " + \
            (f"pred {max(full, 0):.0f} min" if full is not None else "zatiaľ neprebehla")
        if ref is not None and ref > MAILBOX_MAX_AGE_MIN:
            return False, text + f" (viac ako {MAILBOX_MAX_AGE_MIN} min!)"
        return True, text
    if full is None:
        return False, "zatiaľ neprebehol"
    text = f"pred {full:.0f} min"
    if full > MAILBOX_MAX_AGE_MIN:
        return False, text + f" (viac ako {MAILBOX_MAX_AGE_MIN} min!)"
    return True, text


def _age_minutes(value, now: datetime) -> float | None:
    """Vek ISO času v minútach (None = chýba alebo je neplatný)."""
    if isinstance(value, datetime):
        ts = ensure_utc(value)
    else:
        try:
            ts = ensure_utc(datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")))
        except (TypeError, ValueError):
            return None
    return (now - ts).total_seconds() / 60.0


def data_freshness(session, now: datetime | None = None) -> dict:
    """Vek dát agenta: posledná úspešná (úplná) kontrola schránky a posledný cyklus agenta.

    Semafor odporúčania platí len k času prepočtu. Keď agent stojí (neprepočítava, nečíta schránku), web ani
    odovzdávka služby nesmú ďalej ukazovať jeho poslednú ZELENÚ (PROMPT 12: bez čerstvých dát nikdy zelená) – zníži
    sa na ORANŽOVÚ a dôvod je v „text“. Cyklus je starý po 2 × VLAKY_REEVALUATE_MINUTES, schránka po 30 min.
    Jedno pravidlo pre web (vlaky.web.data.data_freshness) aj odovzdávku služby (vlaky.agent.odovzdavka).
    """
    now = now or now_utc()
    cycle_limit = 2 * settings().reevaluate_minutes
    mail_val = get_kv(session, "mail_last_ok")
    mailbox = _age_minutes(mail_val.get("ts"), now) if mail_val.get("ts") else None
    cycle_val = get_kv(session, "agent_last_cycle")
    cycle = _age_minutes(cycle_val.get("ts"), now) if cycle_val.get("ts") else None
    reasons = []
    if cycle is None:
        reasons.append("agent zatiaľ neprebehol")
    elif cycle > cycle_limit:
        reasons.append(f"posledný cyklus agenta pred {cycle:.0f} min")
    if mail_val.get("partial"):
        # agent dočítava nahromadenú poštu – kontrola nie je úplná (žiadna ZELENÁ), text to povie pravdivo
        last_full = _age_minutes(mail_val.get("last_full"), now) if mail_val.get("last_full") else None
        if last_full is not None:
            mailbox = last_full
            reasons.append(f"schránka agenta sa dočítava (posledná úplná kontrola pred {mailbox:.0f} min)")
        else:
            reasons.append("schránka agenta sa dočítava (úplná kontrola zatiaľ neprebehla)")
    elif mailbox is None:
        reasons.append("schránka agenta ešte nebola úspešne skontrolovaná")
    elif mailbox > MAILBOX_MAX_AGE_MIN:
        reasons.append(f"schránka agenta sa nekontrolovala {mailbox:.0f} min")
    return {"mailbox_min": round(mailbox, 1) if mailbox is not None else None,
            "agent_cycle_min": round(cycle, 1) if cycle is not None else None,
            "stale": bool(reasons),
            "text": f"Údaje agenta nie sú čerstvé ({', '.join(reasons)})." if reasons else ""}


def queue_counts(session) -> dict:
    """Fronta na potvrdenie tak, ako ju vidí dispečer na webe (/fronta, počítadlo v hlavičke a upozornenie na
    nástenke) – jedno počítanie pre súhrn, odovzdávku, dennú správu aj stránku /zdravie:
      - waiting: udalosti z e-mailov, ktoré čakajú na potvrdenie,
      - auto: automaticky zapísané výluky, ktoré dispečer ešte neoveril (výluka je stále „návrh“),
      - failed: potvrdené udalosti, ktorých zápis zlyhal (napr. avízo pre vlak, ktorý ešte nie je v systéme),
      - total: súčet."""
    from ..models import Closure, ExtractedEvent
    from ..posta.spracovanie import failed_confirmations

    waiting = session.scalar(select(func.count(ExtractedEvent.id)).where(
        ExtractedEvent.needs_confirmation.is_(True), ExtractedEvent.status == "navrh")) or 0
    auto = session.scalar(select(func.count(func.distinct(ExtractedEvent.id)))
                          .select_from(ExtractedEvent).join(Closure, Closure.source_event_id == ExtractedEvent.id)
                          .where(ExtractedEvent.status == "aplikovane", ExtractedEvent.confirmed_at.is_(None),
                                 Closure.status == "navrh")) or 0
    failed = len(failed_confirmations(session))
    return {"waiting": waiting, "auto": auto, "failed": failed, "total": waiting + auto + failed}


def _order_key(key: str) -> bool:
    parts = (key or "").split(":")
    return len(parts) == 2 and parts[0] == "order" and parts[1].isdigit()


def config_status() -> tuple:
    """(v poriadku, popis) nastavenia odosielania: kanál s príjemcami bez servera správ (chýbajúce nastavenie alebo
    preklep v názve premennej) by správy len odkladal do outboxu. V ostrej prevádzke je to chyba (KRITICKÉ SMS
    by nikdy neodišla), vo vývoji, v testoch a v ukážke stačí outbox. Neznáme premenné VLAKY_* sa len uvedú."""
    from ..notifikacie.odosielanie import outbox_counts_as_sent, transport_problems, unknown_env_vars

    problems = transport_problems()
    unknown = unknown_env_vars()
    cfg = settings()
    # bez interného zoznamu príjemcov agent neodošle nič (ani KRITICKÉ) – v ostrej prevádzke je to chyba
    if not cfg.csv(cfg.notify_emails) and not cfg.csv(cfg.notify_phones) and not cfg.teams_webhook_url:
        problems = list(problems) + ["nie je nastavený žiadny príjemca upozornení (VLAKY_NOTIFY_EMAILS, "
                                     "VLAKY_NOTIFY_PHONES, VLAKY_TEAMS_WEBHOOK_URL) – agent neodošle nič"]
    elif not cfg.csv(cfg.notify_emails):
        problems = list(problems) + ["nie je nastavený e-mail dispečerov (VLAKY_NOTIFY_EMAILS) – odporúčania, súhrn "
                                     "ani odovzdávka služby neodídu e-mailom"]
    parts = list(problems)
    if unknown:
        parts.append(f"neznáme premenné prostredia (preklep?): {', '.join(unknown[:10])}")
    if problems and outbox_counts_as_sent():
        parts.append(f"prostredie {cfg.env}: stačí outbox")
    return not (problems and not outbox_counts_as_sent()), "; ".join(parts) or "ok"


def health_status(ignore_keys: tuple = ()) -> tuple:
    """(ok, detail) – DB dostupná, posledný cyklus agenta a kontrola schránky nie sú staré, správy sa doručujú,
    kanály s príjemcami majú nastavený server správ.

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
    ts = cycle.get("ts")
    if not ts:
        detail["cyklus"] = "zatiaľ neprebehol"
        ok = False
    else:
        age = (now - datetime.fromisoformat(ts)).total_seconds() / 60
        detail["cyklus"] = f"pred {age:.0f} min"
        if age > 10:
            ok = False
            detail["cyklus"] += " (viac ako 10 min!)"
    mail_ok, detail["schranka"] = mailbox_status(mail, now)
    ok = ok and mail_ok
    try:
        conf_ok, detail["konfiguracia"] = config_status()
        ok = ok and conf_ok
    except Exception as exc:  # noqa: BLE001
        detail["konfiguracia"] = f"kontrola zlyhala: {exc!r}"[:300]
    # trvalý výpadok doručovania (SMTP, Teams, SMS) musí byť vidieť aj tu, nielen v cykle, ktorý práve opakuje
    try:
        with session_scope() as s:
            und = undelivered(s, now, ignore_keys)
        if und:
            ok = False
            # najnovšia skutočná chyba (správa odložená ističom bez pokusu o spojenie by zakryla príčinu)
            from ..notifikacie.odosielanie import DEFERRED

            last = next((u for u in und if not (u.error or "").startswith(DEFERRED)), und[0])
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
