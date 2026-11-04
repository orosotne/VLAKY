"""Priebežné stráženie: pracovný čas rušňovodičov, státie vlakov, uchovávanie údajov (GDPR).

Volá sa v každom cykle agenta (sekcie 7 – M7, M9 a 12 PROMPT.md).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..cas import ensure_utc, fmt
from ..config import settings
from ..db import prepare_savepoints
from ..models import Attachment, DriverShift, Email, PositionReport, Train
from ..notifikacie.odosielanie import send_internal
from ..planovanie.planovac import DONE_STATES, current_params
from ..siet.graf import haversine
from .zdravie import get_kv, set_kv

log = logging.getLogger("vlaky.agent")


def _isolated(session: Session, errors: list | None, what: str, fn) -> int:
    """Jedno upozornenie v SAVEPOINT: chyba pri jednom vlaku alebo zmene vráti len jeho kroky, nie záznamy
    upozornení, ktoré už v tomto cykle odišli (inak by sa pri trvalej chybe posielali znova v každom cykle)."""
    try:
        with session.begin_nested():
            return fn()
    except Exception as exc:  # noqa: BLE001
        log.exception("Stráženie zlyhalo: %s", what)
        if errors is not None:
            first = str(exc).splitlines()[0][:160] if str(exc) else ""
            errors.append(f"stráženie ({what}): {type(exc).__name__}: {first}")
        return 0


def crew_alerts(session: Session, now: datetime, errors: list | None = None) -> int:
    """Upozornenia pri 90/60/30 min do limitu pracovného času (13 h) a pri jeho prekročení.

    Rovnaké pravidlo ako plánovač: limit sa ráta od skutočného nástupu zmeny (prebiehajúca, inak naplánovaná, ktorá
    je na rade, s nástupom najneskôr teraz). Idúci vlak, ktorého zmena nástup nemá (žiadna zmena, len neskoršia
    naplánovaná, „prebieha“ s budúcim nástupom), plánovač posudzuje len ODHADOM (nástup = odchod vlaku mínus
    príprava) – strážnik vtedy upozorní podľa toho istého odhadu z odporúčania, aby nemlčal po limite plánovača."""
    p = current_params(session)
    thresholds = sorted({int(x) for x in settings().csv(settings().crew_alert_minutes)}, reverse=True)
    # naplánovaná zmena vlaku, ktorý už má zmenu v službe (vystriedanie ešte nenastúpilo), nestráži limit – inak by
    # upozornenie menovalo rušňovodiča, ktorý nie je v službe
    on_duty = select(DriverShift.train_id).where(DriverShift.status == "prebieha", DriverShift.train_id.is_not(None))
    shifts = list(session.scalars(select(DriverShift).where(
        DriverShift.start_utc <= now,
        (DriverShift.status == "prebieha") |
        ((DriverShift.status == "planovana") &
         (DriverShift.train_id.is_(None) | DriverShift.train_id.not_in(on_duty))))))
    estimates = _unknown_start_trains(session, now)
    if shifts or estimates:
        prepare_savepoints(session)
    sent = sum(_isolated(session, errors, f"zmena rušňovodiča #{sh.id}",
                         lambda sh=sh: _crew_alert(session, now, sh, p, thresholds)) for sh in shifts)
    return sent + sum(_isolated(session, errors, f"odhad pracovného času, vlak {t.number}",
                                lambda t=t, a=start, b=limit, crew=crew:
                                _estimate_alert(session, now, t, a, b, crew, p, thresholds))
                      for t, start, limit, crew in estimates)


def _duty_h(p) -> str:
    """Limit pracovného času v hodinách po slovensky („13“, „12,5“)."""
    return f"{p.crew_max_duty_min / 60:g}".replace(".", ",")


def _iso(value) -> datetime | None:
    try:
        return ensure_utc(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


def _unknown_start_trains(session: Session, now: datetime) -> list:
    """Idúce vlaky bez známeho nástupu rušňovodiča (zmena podľa plánovača – prebiehajúca, inak tá, ktorá je na rade –
    nemá nástup najneskôr teraz) s odhadom plánovača v poslednom odporúčaní: [(vlak, nástup, limit, crew)]."""
    from ..models import Recommendation
    from ..planovanie.planovac import active_shift

    out = []
    for t in session.scalars(select(Train).where(Train.status == "v_jazde").order_by(Train.id)):
        sh = active_shift(session, t.id, now)
        if sh is not None and ensure_utc(sh.start_utc) <= now:
            continue  # nástup je známy – stráži ho upozornenie zmeny (rovnaký limit ako plánovač)
        rec = session.scalar(select(Recommendation).where(Recommendation.train_id == t.id)
                             .order_by(Recommendation.id.desc()))
        crew = (rec.crew or {}) if rec is not None else {}
        start, limit = _iso(crew.get("start")), _iso(crew.get("limit"))
        if crew.get("unknown_start") and start is not None and limit is not None:
            out.append((t, start, limit, crew))
    return out


def _estimate_alert(session: Session, now: datetime, train: Train, start: datetime, limit: datetime, crew: dict,
                    p, thresholds: list) -> int:
    """Upozornenie podľa odhadu plánovača pri idúcom vlaku bez zaznamenaného nástupu – vždy ako ODHAD, nie fakt."""
    left = (limit - now).total_seconds() / 60.0
    duty_h = _duty_h(p)
    if left <= 0:
        level, priority = "prekroceny", "KRITICKÉ"
        head = f"odhad – PREKROČENÝ limit {duty_h} h"
    else:
        hit = [t for t in thresholds if left <= t]
        if not hit:
            return 0
        level = str(min(hit))
        priority = "KRITICKÉ" if min(hit) <= 30 else "DÔLEŽITÉ"
        head = f"odhad – do limitu {duty_h} h zostáva {left:.0f} min"
    name = crew.get("driver") or "neznámy (chýba záznam o nástupe)"
    relieve_by = limit - timedelta(minutes=p.crew_post_min)
    subject = f"[AGENT][{priority}] Rušňovodič {name} (vlak {train.number}): {head}"
    body = (f"Rušňovodič: {name}\nVlak: vlak {train.number} ({train.origin} → {train.destination})\n"
            f"Nástup rušňovodiča nie je zadaný – limit {duty_h} h je len ODHAD plánovača (nástup = odchod vlaku "
            f"mínus príprava): nástup {fmt(start)}, limit {fmt(limit)}.\n"
            f"Odhad: vystriedať najneskôr o {fmt(relieve_by)} (vrátane {p.crew_post_min:.0f} min na odovzdanie rušňa)."
            f"\n\nZadajte skutočný nástup rušňovodiča (Rušňovodiči → Vlaky v jazde bez záznamu o nástupe rušňovodiča)."
            f"\nAgent dispečer iba upozorňuje. Výmenu rušňovodiča zabezpečí dispečer.\n"
            f"Detail: {settings().public_url.rstrip('/')}/rusnovodici")
    key = f"crew:odhad:{train.id}:{start.strftime('%Y%m%d%H%M')}:{level}"
    out = send_internal(session, "upozornenie", subject, body, priority, dedup_key=key)
    if any(n.status in ("odoslana", "outbox") for n in out):
        audit(session, "agent", "crew_alert", "train", train.id, level=level, left_min=round(left), odhad=True)
        return 1
    return 0


def _crew_alert(session: Session, now: datetime, sh: DriverShift, p, thresholds: list) -> int:
    sent = 0
    train = session.get(Train, sh.train_id) if sh.train_id else None
    if train is not None and train.status in DONE_STATES:
        return 0
    limit = ensure_utc(sh.start_utc) + timedelta(minutes=p.crew_max_duty_min)
    left = (limit - now).total_seconds() / 60.0
    name = sh.driver.name if sh.driver else f"#{sh.driver_id}"
    vlak = f"vlak {train.number}" if train else "bez vlaku"
    if left <= 0:
        level, priority = "prekroceny", "KRITICKÉ"
        head = f"PREKROČENÝ limit {_duty_h(p)} h"
    else:
        hit = [t for t in thresholds if left <= t]
        if not hit:
            return 0
        level = str(min(hit))
        priority = "KRITICKÉ" if min(hit) <= 30 else "DÔLEŽITÉ"
        head = f"do limitu {_duty_h(p)} h zostáva {left:.0f} min"
    relieve_by = limit - timedelta(minutes=p.crew_post_min)
    subject = f"[AGENT][{priority}] Rušňovodič {name} ({vlak}): {head}"
    body = (f"Rušňovodič: {name}\nVlak: {vlak}"
            f"{(' (' + train.origin + ' → ' + train.destination + ')') if train else ''}\n"
            f"Nástup: {fmt(sh.start_utc)}   Limit: {fmt(limit)}\n"
            f"Vystriedať najneskôr o: {fmt(relieve_by)} (vrátane {p.crew_post_min:.0f} min na odovzdanie rušňa)\n\n"
            f"Agent dispečer iba upozorňuje. Výmenu rušňovodiča zabezpečí dispečer.\n"
            f"Detail: {settings().public_url.rstrip('/')}/rusnovodici")
    out = send_internal(session, "upozornenie", subject, body, priority, dedup_key=f"crew:{sh.id}:{level}")
    if any(n.status in ("odoslana", "outbox") for n in out):
        sent += 1
        audit(session, "agent", "crew_alert", "driver_shift", sh.id, level=level, left_min=round(left))
    return sent


def standing_alerts(session: Session, now: datetime, errors: list | None = None) -> int:
    """Vlak v jazde, ktorý sa ≥ N minút takmer nepohol (< 300 m), → upozornenie (M9)."""
    minutes = settings().standing_alert_minutes
    trains = list(session.scalars(select(Train).where(Train.status == "v_jazde")))
    if trains:
        prepare_savepoints(session)
    return sum(_isolated(session, errors, f"vlak {t.number}", lambda t=t: _standing_alert(session, now, t, minutes))
               for t in trains)


def _standing_alert(session: Session, now: datetime, t: Train, minutes: int) -> int:
    sent = 0
    reports = list(session.scalars(select(PositionReport).where(
        PositionReport.train_id == t.id, PositionReport.ts_utc >= now - timedelta(minutes=minutes * 3))
        .order_by(PositionReport.ts_utc.desc())))
    if len(reports) < 2:
        return 0
    last = reports[0]
    if (now - ensure_utc(last.ts_utc)).total_seconds() > settings().position_stale_minutes * 60:
        return 0  # bez čerstvých dát sa státie neposudzuje (rieši to semafor)
    lat0, lon0 = last.snapped_lat or last.lat, last.snapped_lon or last.lon
    still_since = ensure_utc(last.ts_utc)
    for r in reports[1:]:
        if haversine(lat0, lon0, r.snapped_lat or r.lat, r.snapped_lon or r.lon) > 300:
            break
        still_since = ensure_utc(r.ts_utc)
    kv_key = f"standing:{t.id}"
    standing = (ensure_utc(last.ts_utc) - still_since).total_seconds() / 60.0
    if standing < minutes:
        if get_kv(session, kv_key):
            set_kv(session, kv_key, {})  # vlak sa pohol – epizóda státia skončila
        return 0
    # epizóda státia má stály začiatok (okno polôh je len 3×N min, takže still_since sa posúva)
    episode = get_kv(session, kv_key)
    if episode.get("since") and haversine(episode["lat"], episode["lon"], lat0, lon0) <= 300:
        still_since = min(still_since, datetime.fromisoformat(episode["since"]))
    else:
        set_kv(session, kv_key, {"since": still_since.isoformat(), "lat": lat0, "lon": lon0})
    standing = (ensure_utc(last.ts_utc) - still_since).total_seconds() / 60.0
    # „pri dopravni Žilina“ (nie „pri Žilina“ – názov dopravne sa neskloňuje), bez dopravne súradnice
    where = (f"pri dopravni {last.nearest_station}" if last.nearest_station
             else f"na súradniciach {lat0:.4f}, {lon0:.4f}")
    bucket = still_since.strftime("%Y%m%d%H%M")  # rovnaká epizóda = rovnaký kľúč (pripomenutie po dedup okne)
    subject = f"[AGENT][DÔLEŽITÉ] Vlak {t.number} stojí {standing:.0f} min {where}"
    body = (f"Vlak {t.number} ({t.origin} → {t.destination}) sa od {fmt(still_since)} pohol menej ako 300 m.\n"
            f"Posledná poloha: {fmt(last.ts_utc)} ({last.source}), {where}.\n\n"
            f"Overte dôvod (plánovaný pobyt, výluka, porucha, čakanie na trasu) a v prípade potreby "
            f"informujte zákazníka. Detail: {settings().public_url.rstrip('/')}/")
    out = send_internal(session, "upozornenie", subject, body, "DÔLEŽITÉ",
                        dedup_key=f"standing:{t.id}:{bucket}")
    if any(n.status in ("odoslana", "outbox") for n in out):
        sent += 1
    return sent


def _purge_outbox(directory, cutoff: datetime) -> int:
    """Zmaže kópie správ v outboxe staršie ako cutoff (čas z názvu súboru, inak čas zmeny súboru)."""
    from datetime import timezone

    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(directory, name)
        if not name.endswith(".txt") or not os.path.isfile(path):
            continue
        try:
            ts = datetime.strptime(name[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                ts = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
            except OSError:
                continue
        if ts < cutoff:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def retention_purge(session: Session, now: datetime) -> dict:
    """GDPR: zmaže staré polohy a obsah starých e-mailov (metadáta a audit ostávajú); správy agenta (záznamy
    aj kópie v outboxe, vrátane zopakovaných pokusov pri výpadku servera správ) po rovnakej dobe ako e-maily."""
    cfg = settings()
    gps_cut = now - timedelta(days=cfg.retention_gps_days)
    res = session.execute(delete(PositionReport).where(PositionReport.ts_utc < gps_cut))
    mail_cut = now - timedelta(days=cfg.retention_email_days)
    # ešte nevymazané: telo, súbor .eml alebo text príloh (aj e-mail len s prílohou, bez textu)
    emails = list(session.scalars(select(Email).where(
        Email.received_at < mail_cut,
        (Email.body_text != "") | (Email.raw_path != "") | Email.attachments.any(Attachment.text != ""))))
    for e in emails:
        e.body_text = ""
        for a in e.attachments:
            a.text = ""
        if e.raw_path and os.path.isfile(e.raw_path):
            try:
                os.remove(e.raw_path)
            except OSError:
                pass
        e.raw_path = ""
    out = {"positions_deleted": res.rowcount or 0, "emails_redacted": len(emails)}
    # správy agenta sú komunikácia ako e-maily (opakovanie a monitoring doručenia potrebujú najviac 48 h)
    from ..models import Notification

    notify_cut = now - timedelta(days=max(3, cfg.retention_email_days))
    deleted = session.execute(delete(Notification).where(Notification.created_at < notify_cut)).rowcount or 0
    files = _purge_outbox(cfg.outbox_dir, ensure_utc(notify_cut))
    if deleted or files:
        out.update(notifications_deleted=deleted, outbox_files_deleted=files)
    if any(out.values()):
        audit(session, "agent", "retention_purge", "", "", **out)
    return out


__all__ = ["crew_alerts", "standing_alerts", "retention_purge", "Attachment"]
