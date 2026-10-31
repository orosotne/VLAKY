"""Odosielanie upozornení a odporúčaní – VÝHRADNE internému zoznamu dispečerov.

ZÁVÄZNÉ (sekcia 2.2 PROMPT.md): správy smú ísť len na adresy, čísla a kanály z konfigurácie
(VLAKY_NOTIFY_EMAILS, VLAKY_NOTIFY_PHONES, VLAKY_TEAMS_WEBHOOK_URL; v tieňovej prevádzke len
VLAKY_SHADOW_EMAILS). Kontrola je v kóde (_guard) – nezávisle od jazykového modelu aj od toho,
kto funkciu zavolá. Iný príjemca → stav „zablokovana“ a auditný záznam. Každá správa sa uloží aj
do adresára outbox (kópia na audit).

Verejné funkcie:
    notify_recommendation(session, rec) -> list[Notification]
    send_internal(session, kind, subject, body, priority="INFO", dedup_key="") -> list[Notification]
    send_digest(session, since) -> list[Notification]
    allowed_recipients() -> dict[str, set[str]]
"""
from __future__ import annotations

import os
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..cas import fmt, now_utc
from ..config import settings
from ..models import Email, ExtractedEvent, Notification, Recommendation
from .texty import recommendation_body, recommendation_subject, sms_text

SENT_STATES = ("odoslana", "outbox")


def allowed_recipients() -> dict:
    s = settings()
    if s.shadow_mode:
        # tieňová prevádzka: len vedúci dispečingu / testovací kanál, žiadne SMS ani Teams
        return {"email": {a.lower() for a in s.csv(s.shadow_emails)}, "sms": set(), "teams": set()}
    return {"email": {a.lower() for a in s.csv(s.notify_emails)},
            "sms": {p.replace(" ", "") for p in s.csv(s.notify_phones)},
            "teams": {"webhook"} if s.teams_webhook_url else set()}


def _guard(session: Session, channel: str, recipient: str) -> bool:
    ok = recipient.lower().replace(" ", "") in {r.lower() for r in allowed_recipients().get(channel, set())}
    if not ok:
        audit(session, "agent", "notification_blocked", "notification", "", channel=channel,
              recipient=recipient)
    return ok


def _is_duplicate(session: Session, channel: str, recipient: str, dedup_key: str) -> bool:
    if not dedup_key:
        return False
    since = now_utc() - timedelta(minutes=settings().dedup_minutes)
    q = select(Notification).where(Notification.dedup_key == dedup_key, Notification.channel == channel,
                                   Notification.recipient == recipient, Notification.created_at >= since,
                                   Notification.status.in_(SENT_STATES))
    return session.scalar(q) is not None


def _write_outbox(n: Notification) -> str:
    out = settings().outbox_dir
    os.makedirs(out, exist_ok=True)
    name = f"{now_utc().strftime('%Y%m%dT%H%M%S')}_{n.channel}_{n.id or 'x'}.txt"
    path = os.path.join(out, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"Kanál: {n.channel}\nPríjemca: {n.recipient}\nPriorita: {n.priority}\nPredmet: {n.subject}\n\n"
                f"{n.body}\n")
    return path


def _send_email(recipient: str, subject: str, body: str) -> str:
    s = settings()
    if not s.smtp_host:
        return "outbox"  # SMTP nie je nastavené – správa je len v adresári outbox
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = recipient  # vždy presne jeden príjemca, žiadne CC/BCC
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=s.smtp_from.split("@")[-1] or "localhost")
    msg["X-Agent-Dispecer"] = "odporucanie"
    msg.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
        if s.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        refused = smtp.send_message(msg, to_addrs=[recipient])
        if refused:
            raise RuntimeError(f"SMTP odmietol príjemcu: {refused}")
    return "odoslana"


def _send_sms(phone: str, text: str) -> str:
    s = settings()
    if not s.sms_gateway_url:
        return "outbox"
    headers = {"Authorization": f"Bearer {s.sms_gateway_token}"} if s.sms_gateway_token else {}
    r = httpx.post(s.sms_gateway_url, json={"to": phone, "text": text[:300]}, headers=headers, timeout=20)
    r.raise_for_status()
    return "odoslana"


def _send_teams(subject: str, body: str) -> str:
    s = settings()
    if not s.teams_webhook_url:
        return "outbox"
    r = httpx.post(s.teams_webhook_url, json={"text": f"**{subject}**\n\n" + body.replace("\n", "  \n")[:20000]},
                   timeout=20)
    r.raise_for_status()
    return "odoslana"


def _deliver(session: Session, channel: str, recipient: str, subject: str, body: str, priority: str,
             kind: str, dedup_key: str, rec_id: int | None) -> Notification:
    n = Notification(channel=channel, recipient=recipient, subject=subject, body=body, priority=priority,
                     kind=kind, dedup_key=dedup_key, recommendation_id=rec_id, created_at=now_utc())
    session.add(n)
    session.flush()
    if not _guard(session, channel, recipient):
        n.status, n.error = "zablokovana", "Príjemca nie je na internom zozname – odoslanie zablokované."
        return n
    if _is_duplicate(session, channel, recipient, dedup_key):
        n.status = "potlacena"
        return n
    try:
        if channel == "email":
            n.status = _send_email(recipient, subject, body)
        elif channel == "sms":
            n.status = _send_sms(recipient, body)
        elif channel == "teams":
            n.status = _send_teams(subject, body)
        else:
            n.status, n.error = "chyba", f"Neznámy kanál {channel}"
        n.sent_at = now_utc()
    except Exception as exc:  # výpadok SMTP/brány nesmie zastaviť agenta
        n.status, n.error = "chyba", repr(exc)[:500]
    try:
        _write_outbox(n)
    except OSError:
        pass
    return n


def _channels(priority: str) -> list:
    if priority == "KRITICKÉ":
        return ["email", "teams", "sms"]
    if priority == "DÔLEŽITÉ":
        return ["email", "teams"]
    return []  # INFO ide do súhrnu


def send_internal(session: Session, kind: str, subject: str, body: str, priority: str = "INFO",
                  dedup_key: str = "", rec_id: int | None = None, channels: list | None = None) -> list:
    out = []
    allowed = allowed_recipients()
    for ch in channels or (_channels(priority) or ["email"]):
        if ch == "teams":
            if allowed["teams"]:
                out.append(_deliver(session, "teams", "webhook", subject, body, priority, kind, dedup_key, rec_id))
            continue
        text = body if ch != "sms" else f"{subject}"[:300]
        for r in sorted(allowed.get(ch, set())):
            out.append(_deliver(session, ch, r, subject, text, priority, kind, dedup_key, rec_id))
    return out


def notify_recommendation(session: Session, rec: Recommendation) -> list:
    """Pošle odporúčanie podľa priority (INFO sa neposiela hneď, ide do súhrnu)."""
    channels = _channels(rec.priority)
    if not channels:
        return []
    subject = recommendation_subject(rec)
    body = recommendation_body(rec)
    dedup = f"rec:{rec.train_id}:{rec.fingerprint}"
    out = []
    allowed = allowed_recipients()
    for ch in channels:
        if ch == "email":
            for r in sorted(allowed["email"]):
                out.append(_deliver(session, "email", r, subject, body, rec.priority, "odporucanie", dedup, rec.id))
        elif ch == "teams" and allowed["teams"]:
            out.append(_deliver(session, "teams", "webhook", subject, body, rec.priority, "odporucanie", dedup,
                                rec.id))
        elif ch == "sms":
            for r in sorted(allowed["sms"]):
                out.append(_deliver(session, "sms", r, subject, sms_text(rec), rec.priority, "odporucanie", dedup,
                                    rec.id))
    return out


def send_digest(session: Session, since: datetime) -> list:
    """Súhrn: INFO odporúčania, čakajúce potvrdenia, karanténa (sekcia 2.3 – priorita INFO)."""
    recs = list(session.scalars(select(Recommendation).where(Recommendation.created_at >= since,
                                                             Recommendation.priority == "INFO",
                                                             Recommendation.state == "nove")))
    waiting = list(session.scalars(select(ExtractedEvent).where(ExtractedEvent.status == "navrh",
                                                                ExtractedEvent.needs_confirmation.is_(True))))
    quarantined = list(session.scalars(select(Email).where(Email.received_at >= since,
                                                           Email.verdict == "karantena")))
    if not recs and not waiting and not quarantined:
        return []
    lines = [f"Súhrn Agenta dispečera od {fmt(since)}", ""]
    if recs:
        lines.append("Odporúčania (INFO):")
        lines += [f"  - {r.summary}" for r in recs]
        lines.append("")
    if waiting:
        lines.append(f"Čaká na potvrdenie dispečerom: {len(waiting)} udalostí z e-mailov "
                     f"({settings().public_url.rstrip('/')}/fronta)")
    if quarantined:
        lines.append(f"V karanténe (nepovolený alebo neoverený odosielateľ): {len(quarantined)} e-mailov")
        lines += [f"  - {e.sender}: {e.subject}" for e in quarantined[:10]]
    body = "\n".join(lines)
    return send_internal(session, "suhrn", "[AGENT][INFO] Súhrn", body, "INFO",
                         dedup_key=f"digest:{since.isoformat()}", channels=["email"])
