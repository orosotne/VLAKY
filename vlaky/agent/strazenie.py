"""Priebežné stráženie: pracovný čas rušňovodičov, státie vlakov, uchovávanie údajov (GDPR).

Volá sa v každom cykle agenta (sekcie 7 – M7, M9 a 12 PROMPT.md).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..cas import ensure_utc, fmt
from ..config import settings
from ..models import Attachment, DriverShift, Email, PositionReport, Train
from ..notifikacie.odosielanie import send_internal
from ..planovanie.planovac import DONE_STATES, current_params
from ..siet.graf import haversine
from .zdravie import get_kv, set_kv


def crew_alerts(session: Session, now: datetime) -> int:
    """Upozornenia pri 90/60/30 min do limitu pracovného času (13 h) a pri jeho prekročení."""
    p = current_params(session)
    thresholds = sorted({int(x) for x in settings().csv(settings().crew_alert_minutes)}, reverse=True)
    sent = 0
    shifts = session.scalars(select(DriverShift).where(DriverShift.status.in_(("planovana", "prebieha")),
                                                       DriverShift.start_utc <= now))
    for sh in shifts:
        train = session.get(Train, sh.train_id) if sh.train_id else None
        if train is not None and train.status in DONE_STATES:
            continue
        limit = ensure_utc(sh.start_utc) + timedelta(minutes=p.crew_max_duty_min)
        left = (limit - now).total_seconds() / 60.0
        name = sh.driver.name if sh.driver else f"#{sh.driver_id}"
        vlak = f"vlak {train.number}" if train else "bez vlaku"
        if left <= 0:
            level, priority = "prekroceny", "KRITICKÉ"
            head = f"PREKROČENÝ limit {p.crew_max_duty_min / 60:.0f} h"
        else:
            hit = [t for t in thresholds if left <= t]
            if not hit:
                continue
            level = str(min(hit))
            priority = "KRITICKÉ" if min(hit) <= 30 else "DÔLEŽITÉ"
            head = f"do limitu {p.crew_max_duty_min / 60:.0f} h zostáva {left:.0f} min"
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


def standing_alerts(session: Session, now: datetime) -> int:
    """Vlak v jazde, ktorý sa ≥ N minút takmer nepohol (< 300 m), → upozornenie (M9)."""
    minutes = settings().standing_alert_minutes
    sent = 0
    for t in session.scalars(select(Train).where(Train.status == "v_jazde")):
        reports = list(session.scalars(select(PositionReport).where(
            PositionReport.train_id == t.id, PositionReport.ts_utc >= now - timedelta(minutes=minutes * 3))
            .order_by(PositionReport.ts_utc.desc())))
        if len(reports) < 2:
            continue
        last = reports[0]
        if (now - ensure_utc(last.ts_utc)).total_seconds() > settings().position_stale_minutes * 60:
            continue  # bez čerstvých dát sa státie neposudzuje (rieši to semafor)
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
            continue
        # epizóda státia má stály začiatok (okno polôh je len 3×N min, takže still_since sa posúva)
        episode = get_kv(session, kv_key)
        if episode.get("since") and haversine(episode["lat"], episode["lon"], lat0, lon0) <= 300:
            still_since = min(still_since, datetime.fromisoformat(episode["since"]))
        else:
            set_kv(session, kv_key, {"since": still_since.isoformat(), "lat": lat0, "lon": lon0})
        standing = (ensure_utc(last.ts_utc) - still_since).total_seconds() / 60.0
        where = last.nearest_station or f"{lat0:.4f}, {lon0:.4f}"
        bucket = still_since.strftime("%Y%m%d%H%M")  # rovnaká epizóda = rovnaký kľúč (pripomenutie po dedup okne)
        subject = f"[AGENT][DÔLEŽITÉ] Vlak {t.number} stojí {standing:.0f} min pri {where}"
        body = (f"Vlak {t.number} ({t.origin} → {t.destination}) sa od {fmt(still_since)} pohol menej ako 300 m.\n"
                f"Posledná poloha: {fmt(last.ts_utc)} ({last.source}), pri {where}.\n\n"
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
