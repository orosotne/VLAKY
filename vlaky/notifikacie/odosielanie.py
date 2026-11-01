"""Odosielanie upozornení a odporúčaní – VÝHRADNE internému zoznamu dispečerov.

ZÁVÄZNÉ (sekcia 2.2 PROMPT.md): správy smú ísť len na adresy, čísla a kanály z konfigurácie
(VLAKY_NOTIFY_EMAILS, VLAKY_NOTIFY_PHONES, VLAKY_TEAMS_WEBHOOK_URL; v tieňovej prevádzke len
VLAKY_SHADOW_EMAILS). Kontrola je v kóde (_guard) – nezávisle od jazykového modelu aj od toho,
kto funkciu zavolá. Iný príjemca → stav „zablokovana“ a auditný záznam. Každá správa sa uloží aj
do adresára outbox (kópia na audit).

Doručovanie: nedostupný server (SMTP, Teams, SMS brána) sa v jednom cykle agenta skúsi len raz (istič, delivery_scope)
s limitom spojenia 10 s, zlyhané správy sa opakujú s rastúcim odstupom (5, 10, 20, 40, potom 60 min – retry_due,
retry_pairs, retry_targets; KRITICKÉ pri výpadku kanála každých 5 min) a len tým príjemcom, ktorým sa ich
nepodarilo doručiť. Jednorazové upozornenia bez vlastného opakovania (hlásenie rušňovodiča o probléme, pripomienka
výluky do odvolania) opakuje agent cez retry_one_shot.

Server správ nie je nastavený (napr. chýba VLAKY_SMTP_HOST alebo je v názve preklep): vo vývoji, v testoch a v ukážke
ostane správa len v adresári outbox (stav „outbox“), v ostrej prevádzke je to chyba doručenia (stav „chyba“) – inak
by sa KRITICKÉ upozornenie tvárilo ako doručené (transport_problems, zdravie agenta „konfiguracia“).

Verejné funkcie:
    notify_recommendation(session, rec, only=None) -> list[Notification]
    send_internal(session, kind, subject, body, priority="INFO", dedup_key="", only=None) -> list[Notification]
    send_digest(session, since, only=None) -> list[Notification]
    send_test(session, channels=None) -> (list[Notification], list[str])
    allowed_recipients() -> dict[str, set[str]]
    transport_problems() -> list[str], unknown_env_vars() -> list[str], outbox_counts_as_sent() -> bool
    delivery_scope(), retry_due(session, dedup_key), retry_pairs(session, rec_id, rows=None),
    retry_targets(session, dedup_key), delivered_to_all(session, dedup_key), retry_one_shot(session, now=None),
    recommendation_attempts(session, rec_ids), retry_delay_minutes(n)
"""
from __future__ import annotations

import os
import smtplib
import ssl
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..cas import ensure_utc, fmt, now_utc
from ..config import settings
from ..models import Email, Notification, Recommendation
from .texty import recommendation_body, recommendation_subject, sms_text

SENT_STATES = ("odoslana", "outbox")
CONNECT_TIMEOUT_S = 10  # spojenie so serverom správ (SMTP, Graph, Teams, SMS brána) – nedostupný nezdrží agenta
RETRY_FIRST_MIN = 5     # zlyhaná správa sa zopakuje po 5 min, potom vždy po dvojnásobku (10, 20, 40 …)
RETRY_MAX_MIN = 60      # … najviac však po 60 min (KRITICKÉ pri výpadku kanála stále každých 5 min)
RETRY_HOURS = 48        # aktuálne odporúčanie sa skúša doručiť najviac 48 h (aj po dlhom výpadku servera)
# správy odložené v tom istom cykle majú o pár sekúnd neskorší čas (čakanie na nedostupný server) – aby sa
# nerozpadli do samostatných kôl (každé s ďalším pokusom o spojenie), zopakujú sa spolu s prvou
RETRY_GRACE_S = 90
DEFERRED = "Neodoslané – kanál"  # začiatok chyby správy odloženej ističom (bez pokusu o spojenie)
CHANNEL_ERROR = "Chyba kanála: "  # začiatok chyby spojenia alebo servera (nie odmietnutia tejto správy)
# jednorazové upozornenia bez vlastného opakovania (hlásenie rušňovodiča, pripomienka výluky do odvolania): zlyhané
# doručenie opakuje agent (retry_one_shot) a kým neprídu, zdravie ich počíta ako nedoručené
ONE_SHOT_PREFIXES = ("problem:", "do_odvolania:")
OUTBOX_ENVS = ("dev", "test", "demo")  # len tu stačí kópia v adresári outbox (bez servera správ)
# premenné VLAKY_* mimo nastavení aplikácie (nasadenie, testy) – nie sú preklepom
KNOWN_ENV_EXTRA = ("VLAKY_DOMAIN", "VLAKY_UPSTREAM", "VLAKY_APP_DIR", "VLAKY_USER", "VLAKY_UID", "VLAKY_STATE_DIR",
                   "VLAKY_TEST_DATABASE_URL", "VLAKY_SCREENSHOT_DIR", "VLAKY_SKIP_BROWSER")

# istič odosielania: {kanál: prvá chyba spojenia} – platí len v rámci delivery_scope (cyklus agenta, jedno volanie)
_ISTIC: ContextVar = ContextVar("vlaky_istic_odosielania", default=None)


@contextmanager
def delivery_scope():
    """Istič pre jeden cyklus agenta (alebo jedno odoslanie z webu).

    Po prvom zlyhaní spojenia na kanáli (SMTP/Graph, Teams, SMS brána) sa ďalšie správy na tom istom kanáli
    v tomto rozsahu už nepripájajú – dostanú stav „chyba“ a zopakujú sa podľa odstupu. Nedostupný (napr. ticho
    zahadzujúci) server tak nezdrží cyklus agenta a čítanie schránky o časový limit za každú správu.
    """
    if _ISTIC.get() is not None:  # vnorené volanie – platí istič vonkajšieho rozsahu
        yield
        return
    token = _ISTIC.set({})
    try:
        yield
    finally:
        _ISTIC.reset(token)


def _connection_error(exc: Exception) -> bool:
    """Chyba spojenia alebo servera (zlyhala by aj ďalšia správa na tom istom kanáli), nie chyba jednej správy."""
    if isinstance(exc, smtplib.SMTPResponseException) and exc.smtp_code == 421:
        return True  # služba nedostupná
    if isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError)):
        return False  # odmietnutý príjemca alebo obsah tejto správy
    if isinstance(exc, httpx.HTTPStatusError):
        # odpoveď servera cyklus nezdrží a môže sa týkať len jedného príjemcu (napr. 403 alebo 404 pre jedno číslo
        # v SMS bráne) – istič len pri výpadku alebo preťažení servera (5xx, 408, 429)
        code = exc.response.status_code
        return code >= 500 or code in (408, 429)
    return isinstance(exc, (OSError, httpx.TransportError))  # aj ostatné smtplib.SMTPException, TLS, časový limit


def _connect_timeout() -> float:
    """Limit spojenia so serverom správ v sekundách: nastavenie notify_connect_timeout_s
    (VLAKY_NOTIFY_CONNECT_TIMEOUT_S), ak ho konfigurácia má, inak 10 s (CONNECT_TIMEOUT_S)."""
    try:
        value = float(getattr(settings(), "notify_connect_timeout_s", 0) or 0)
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else float(CONNECT_TIMEOUT_S)


def _timeout(total: float) -> httpx.Timeout:
    return httpx.Timeout(total, connect=_connect_timeout())


def allowed_recipients() -> dict:
    s = settings()
    if s.shadow_mode:
        # tieňová prevádzka: len vedúci dispečingu / testovací kanál, žiadne SMS ani Teams
        return {"email": {a.lower() for a in s.csv(s.shadow_emails)}, "sms": set(), "teams": set()}
    return {"email": {a.lower() for a in s.csv(s.notify_emails)},
            "sms": {p.replace(" ", "") for p in s.csv(s.notify_phones)},
            "teams": {"webhook"} if s.teams_webhook_url else set()}


def outbox_counts_as_sent() -> bool:
    """Správa len v adresári outbox (server správ nie je nastavený) sa počíta ako odoslaná len vo vývoji, v testoch
    a v ukážke (VLAKY_ENV dev | test | demo) – v ostrej prevádzke je to chyba doručenia."""
    return (settings().env or "").strip().lower() in OUTBOX_ENVS


def _graph_missing(s) -> list:
    return [name for name, value in (("VLAKY_GRAPH_TENANT_ID", s.graph_tenant_id),
                                     ("VLAKY_GRAPH_CLIENT_ID", s.graph_client_id),
                                     ("VLAKY_GRAPH_CLIENT_SECRET", s.graph_client_secret),
                                     ("VLAKY_GRAPH_MAILBOX", s.graph_mailbox)) if not value]


def _email_transport(s) -> str:
    """Ako sa e-mail naozaj pošle (rovnaké pravidlo ako _send_email): graph | smtp | outbox."""
    if s.notify_transport == "graph" and s.graph_mailbox and s.graph_client_id:
        return "graph"
    return "smtp" if s.smtp_host else "outbox"


def transport_problems() -> list:
    """Kanály, ktoré majú príjemcov, ale nemajú nastavený server správ (prípadne s preklepom v nastavení) –
    správy by ostali len v outboxe. Prázdny zoznam = v poriadku. Kontroluje sa bez ohľadu na prostredie; v ostrej
    prevádzke ich zdravie agenta hlási ako chybu (vo vývoji a ukážke stačí outbox)."""
    s = settings()
    allowed = allowed_recipients()
    out = []
    transport = (s.notify_transport or "smtp").strip()
    if allowed["email"]:
        if transport not in ("smtp", "graph"):
            out.append(f"e-mail: neznámy spôsob odosielania VLAKY_NOTIFY_TRANSPORT={transport!r} (smtp | graph)")
        elif transport == "graph" and _graph_missing(s):
            out.append("e-mail: VLAKY_NOTIFY_TRANSPORT=graph, ale chýba " + ", ".join(_graph_missing(s)))
        if _email_transport(s) == "outbox":
            out.append("e-mail: nie je nastavený server správ (VLAKY_SMTP_HOST, alebo VLAKY_NOTIFY_TRANSPORT=graph "
                       "s VLAKY_GRAPH_*) – e-maily sa len ukladajú do outboxu")
    if allowed["sms"] and not s.sms_gateway_url:
        out.append("SMS: čísla sú nastavené (VLAKY_NOTIFY_PHONES), ale chýba SMS brána (VLAKY_SMS_GATEWAY_URL)")
    return out


def unknown_env_vars() -> list:
    """Premenné prostredia VLAKY_*, ktoré aplikácia nepozná (napr. preklep VLAKY_SMTP_SERVER namiesto
    VLAKY_SMTP_HOST) – nastavenia ich potichu ignorujú."""
    from ..config import Settings

    known = {f"VLAKY_{name}".upper() for name in Settings.model_fields} | set(KNOWN_ENV_EXTRA)
    return sorted(k for k in os.environ if k.upper().startswith("VLAKY_") and k.upper() not in known)


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


def _is_rec_duplicate(session: Session, n: Notification, train_id: int) -> bool:
    """Odporúčanie je duplicita, len ak je zhodné s POSLEDNOU správou o vlaku, ktorú tento príjemca dostal.

    Návrat k skoršiemu odporúčaniu (A → B → A) sa pošle – dispečer má v ruke B. Ak posledná zhodná správa patrí
    inému odporúčaniu (napr. súbežné odoslanie z webu a agenta), musí byť zhodná aj posledná správa o vlaku, ktorú
    dostal ktokoľvek z dispečingu (napr. SMS pri opätovnej eskalácii, keď medzitým prišlo „vyriešené“ e-mailom).
    Bez časového okna: rovnaké upozornenie sa neopakuje, kým sa situácia nezmení.
    """
    if not n.dedup_key:
        return False
    base = (select(Notification.dedup_key, Notification.recommendation_id)
            .join(Recommendation, Recommendation.id == Notification.recommendation_id)
            .where(Recommendation.train_id == train_id, Notification.kind == "odporucanie",
                   Notification.status.in_(SENT_STATES), Notification.id != n.id)
            .order_by(Notification.id.desc()).limit(1))
    mine = session.execute(base.where(Notification.channel == n.channel,
                                      Notification.recipient == n.recipient)).first()
    if mine is None or mine.dedup_key != n.dedup_key:
        return False
    if mine.recommendation_id != n.recommendation_id:
        team = session.execute(base.where(Notification.recommendation_id != n.recommendation_id)).first()
        if team is None or team.dedup_key != n.dedup_key:
            return False
    return not (n.channel == "email" and _info_in_digest(session, train_id, n, mine.recommendation_id))


def _info_in_digest(session: Session, train_id: int, n: Notification, after_rec_id: int) -> bool:
    """Medzitým vzniklo iné odporúčanie s prioritou INFO a príjemcovi potom odišiel súhrn – mohol ho tam vidieť
    (A → B v súhrne → A); návrat k A sa preto pošle znova."""
    for b in session.scalars(select(Recommendation).where(Recommendation.train_id == train_id,
                                                          Recommendation.priority == "INFO",
                                                          Recommendation.id > after_rec_id)):
        if recommendation_dedup_key(b) == n.dedup_key:
            continue
        digest = session.scalar(select(Notification.id).where(
            Notification.kind == "suhrn", Notification.channel == n.channel, Notification.recipient == n.recipient,
            Notification.status.in_(SENT_STATES), Notification.created_at >= ensure_utc(b.created_at)).limit(1))
        if digest is not None:
            return True
    return False


def retry_delay_minutes(failures: int) -> float:
    """Odstup ďalšieho pokusu po n-tom neúspešnom pokuse za sebou: 5, 10, 20, 40, potom 60 min."""
    return float(min(RETRY_FIRST_MIN * 2 ** max(0, failures - 1), RETRY_MAX_MIN))


def _allowed_pair(allowed: dict, channel: str, recipient: str) -> bool:
    return recipient.lower().replace(" ", "") in {r.lower() for r in allowed.get(channel, set())}


def _failed_pairs(rows: list) -> dict:
    """rows (vzostupne podľa id) → {(kanál, príjemca): (posledný pokus, počet neúspešných pokusov za sebou)} pre
    príjemcov, ktorým posledný pokus zlyhal a sú stále na internom zozname."""
    per_pair: dict = {}
    for row in rows:
        per_pair.setdefault((row.channel, row.recipient), []).append(row)
    allowed = allowed_recipients()
    out = {}
    for (channel, recipient), items in per_pair.items():
        last = items[-1]
        if last.status != "chyba" or not _allowed_pair(allowed, channel, recipient):
            continue
        fails = 0
        for row in reversed(items):
            if row.status != "chyba":
                break
            fails += 1
        out[(channel, recipient)] = (last, fails)
    return out


def _pending(session: Session, rows: list) -> dict:
    """rows (vzostupne podľa id) → {(kanál, príjemca): smie sa zopakovať} pre príjemcov, ktorým posledný pokus
    zlyhal a sú stále na internom zozname.

    Zopakovanie: po odstupe podľa počtu neúspešných pokusov za sebou. Pri výpadku kanála (chyba spojenia alebo
    servera, správa odložená ističom) aj hneď, keď tomu istému príjemcovi medzitým prišla iná správa (kanál opäť
    funguje), a KRITICKÉ v každom 5-min okne (spoločnom pre všetky KRITICKÉ správy – pri výpadku jeden pokus
    o spojenie na kanál) – po obnovení servera prídu do 5 min, nie až o hodinu. Odmietnutie samotnej správy (napr.
    neznámy príjemca, číslo zamietnuté bránou) sa opakuje len s bežným odstupom. Príjemcovia tej istej správy na
    tom istom kanáli sa opakujú spolu (pri jednom pokuse o spojenie), aj keď ich pokus bol o pár sekúnd neskôr.
    """
    now = now_utc()
    window = RETRY_FIRST_MIN * 60
    out = {}
    for (channel, recipient), (last, fails) in _failed_pairs(rows).items():
        outage = (getattr(last, "error", "") or "").startswith((CHANNEL_ERROR, DEFERRED))
        if outage and getattr(last, "priority", "") == "KRITICKÉ":
            due = now.timestamp() // window > ensure_utc(last.created_at).timestamp() // window
        else:
            due = (now - ensure_utc(last.created_at)) >= \
                timedelta(minutes=retry_delay_minutes(fails)) - timedelta(seconds=RETRY_GRACE_S)
        if not due and outage:
            due = session.scalar(select(Notification.id).where(
                Notification.channel == channel, Notification.recipient == recipient,
                Notification.status.in_(SENT_STATES), Notification.id > last.id).limit(1)) is not None
        out[(channel, recipient)] = due
    due_channels = {channel for (channel, _r), due in out.items() if due}
    return {pair: due or pair[0] in due_channels for pair, due in out.items()}


def _attempts(session: Session, *where) -> list:
    return list(session.execute(select(Notification.id, Notification.channel, Notification.recipient,
                                       Notification.status, Notification.priority, Notification.error,
                                       Notification.created_at)
                                .where(*where).order_by(Notification.id)).all())


def recommendation_attempts(session: Session, rec_ids: list) -> dict:
    """{id odporúčania: pokusy o doručenie (vzostupne)} – jedným dopytom pre všetky odporúčania cyklu."""
    out: dict = {}
    if rec_ids:
        for row in session.execute(select(Notification.recommendation_id, Notification.id, Notification.channel,
                                          Notification.recipient, Notification.status, Notification.priority,
                                          Notification.error, Notification.created_at)
                                   .where(Notification.recommendation_id.in_(rec_ids)).order_by(Notification.id)):
            out.setdefault(row.recommendation_id, []).append(row)
    return out


def retry_pairs(session: Session, rec_id: int, rows: list | None = None) -> set | None:
    """Komu treba odporúčanie (znova) poslať: None = zatiaľ nikomu (všetkým podľa priority), inak dvojice
    (kanál, príjemca), ktorým sa ho nepodarilo doručiť a uplynul odstup – ostatným sa znova neposiela."""
    if rows is None:
        rows = _attempts(session, Notification.recommendation_id == rec_id)
    if not rows:
        return None
    return {pair for pair, due in _pending(session, rows).items() if due}


def retry_due(session: Session, dedup_key: str) -> bool:
    """Smie sa správa s týmto kľúčom (návrh k objednávke) skúsiť poslať teraz?

    Bez neúspešného pokusu áno; po zlyhaní až po odstupe 5, 10, 20, 40, 60 min (alebo keď kanál opäť funguje).
    """
    pending = _pending(session, _attempts(session, Notification.dedup_key == dedup_key[:120]))
    return not pending or any(pending.values())


def retry_targets(session: Session, dedup_key: str) -> set | None:
    """Komu treba správu s týmto kľúčom (súhrn, denná správa, odovzdávka) poslať: None = zatiaľ nikomu (všetkým),
    inak len dvojice (kanál, príjemca), ktorým sa ju nepodarilo doručiť a uplynul odstup (prázdna množina = teraz
    nikomu) – kto ju už dostal, nedostane ju druhý raz."""
    rows = _attempts(session, Notification.dedup_key == dedup_key[:120])
    if not rows:
        return None
    return {pair for pair, due in _pending(session, rows).items() if due}


def delivered_to_all(session: Session, dedup_key: str) -> bool:
    """Správa s týmto kľúčom prišla každému príjemcovi zo zoznamu, ktorému sa posielala (žiadny posledný pokus
    nezlyhal) – až potom je súhrn, denná správa alebo odovzdávka vybavená."""
    return not _failed_pairs(_attempts(session, Notification.dedup_key == dedup_key[:120]))


def _write_outbox(n: Notification) -> str:
    out = settings().outbox_dir
    os.makedirs(out, exist_ok=True)
    name = f"{now_utc().strftime('%Y%m%dT%H%M%S')}_{n.channel}_{n.id or 'x'}.txt"
    path = os.path.join(out, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"Kanál: {n.channel}\nPríjemca: {n.recipient}\nPriorita: {n.priority}\nPredmet: {n.subject}\n\n"
                f"{n.body}\n")
    return path


def _graph_token() -> str:
    s = settings()
    r = httpx.post(f"https://login.microsoftonline.com/{s.graph_tenant_id}/oauth2/v2.0/token",
                   data={"client_id": s.graph_client_id, "client_secret": s.graph_client_secret,
                         "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"},
                   timeout=_timeout(30))
    r.raise_for_status()
    return r.json()["access_token"]


def _send_email_graph(recipient: str, subject: str, body: str) -> str:
    """Odoslanie cez Microsoft Graph (sendMail) z účtu agenta – vždy jeden príjemca, bez kópií."""
    s = settings()
    payload = {"message": {"subject": subject, "body": {"contentType": "Text", "content": body},
                           "toRecipients": [{"emailAddress": {"address": recipient}}]},
               "saveToSentItems": True}
    r = httpx.post(f"https://graph.microsoft.com/v1.0/users/{s.graph_mailbox}/sendMail", json=payload,
                   headers={"Authorization": f"Bearer {_graph_token()}"}, timeout=_timeout(30))
    r.raise_for_status()
    return "odoslana"


def _send_email(recipient: str, subject: str, body: str) -> str:
    s = settings()
    if s.notify_transport == "graph" and s.graph_mailbox and s.graph_client_id:
        return _send_email_graph(recipient, subject, body)
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
    # krátky limit na spojenie (nedostupný server nezdrží agenta), potom bežný limit na odpovede servera
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=_connect_timeout()) as smtp:
        if getattr(smtp, "sock", None) is not None:
            smtp.sock.settimeout(30)
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
    r = httpx.post(s.sms_gateway_url, json={"to": phone, "text": text[:300]}, headers=headers, timeout=_timeout(20))
    r.raise_for_status()
    return "odoslana"


def plain_markdown(text: str) -> str:
    """Text pre Adaptive Card bez maskovaných odkazov.

    Do upozornení sa dostáva aj text zvonka (hlásenie rušňovodiča, e-mail) – odkaz v tvare
    „[ŽSR dispečing](https://podvrh…)“ by v Teams vyzeral dôveryhodne. Medzi „]“ a „(“ sa vloží znak
    nulovej šírky, takže Teams odkaz nevykreslí a adresa ostane viditeľná ako obyčajný text.
    """
    return (text or "").replace("](", "]\u200b(").replace("]\n(", "]\u200b\n(")


def _send_teams(subject: str, body: str) -> str:
    s = settings()
    if not s.teams_webhook_url:
        return "outbox"
    subject, body = plain_markdown(subject), plain_markdown(body[:6000])
    # Adaptive Card (Teams Workflows / Power Automate „webhook“); text skrátený na rozumnú dĺžku
    card = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [{"type": "TextBlock", "text": subject, "weight": "Bolder", "wrap": True},
                             {"type": "TextBlock", "text": body, "wrap": True, "fontType": "Monospace"}]}}]}
    r = httpx.post(s.teams_webhook_url, json=card, timeout=_timeout(20))
    r.raise_for_status()
    return "odoslana"


def _deliver(session: Session, channel: str, recipient: str, subject: str, body: str, priority: str,
             kind: str, dedup_key: str, rec_id: int | None, train_id: int | None = None) -> Notification:
    # dĺžky podľa stĺpcov (PostgreSQL by dlhší text odmietol a zhodil celú transakciu)
    if len(subject) > 300:
        subject = subject[:299] + "…"
    n = Notification(channel=channel[:10], recipient=recipient[:200], subject=subject, body=body,
                     priority=priority[:12], kind=kind[:30], dedup_key=dedup_key[:120], recommendation_id=rec_id,
                     created_at=now_utc())
    session.add(n)
    session.flush()
    if not _guard(session, channel, recipient):
        n.status, n.error = "zablokovana", "Príjemca nie je na internom zozname – odoslanie zablokované."
        return n
    duplicate = _is_rec_duplicate(session, n, train_id) if train_id is not None else \
        _is_duplicate(session, channel, recipient, dedup_key)
    if duplicate:
        n.status = "potlacena"
        return n
    istic = _ISTIC.get()
    if istic is not None and channel in istic:
        # kanál v tomto cykle nefunguje – bez ďalšieho pripájania; zopakuje sa podľa odstupu
        n.status = "chyba"
        n.error = f"{DEFERRED} {channel} je teraz nedostupný ({istic[channel]}); zopakuje sa."[:500]
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
        if n.status == "outbox" and not outbox_counts_as_sent():
            # ostrá prevádzka bez servera správ (chýbajúce nastavenie alebo preklep v názve): nič neodišlo –
            # chyba doručenia (opakuje sa, zdravie a heartbeat ju hlásia), nie „odoslané“
            n.status, n.sent_at = "chyba", None
            hint = {"sms": "VLAKY_SMS_GATEWAY_URL", "teams": "VLAKY_TEAMS_WEBHOOK_URL"}.get(
                channel, "VLAKY_SMTP_HOST alebo VLAKY_GRAPH_*")
            n.error = f"{CHANNEL_ERROR}{channel}: server správ nie je nastavený ({hint}) – správa je len v outboxe"
    except Exception as exc:  # výpadok SMTP/brány nesmie zastaviť agenta
        outage = _connection_error(exc)
        n.status, n.error = "chyba", f"{CHANNEL_ERROR if outage else ''}{exc!r}"[:500]
        if istic is not None and outage:
            istic[channel] = f"{type(exc).__name__}: {exc}"[:200]
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


def _recipients(session: Session, allowed: dict, channel: str, dedup_key: str, only: set | None) -> list:
    """Príjemcovia správy na kanáli (only – len tieto dvojice (kanál, príjemca), napr. pri opakovaní).

    Stále poradie, ale príjemcovia, ktorým posledný skutočný pokus o túto správu zlyhal, idú na koniec: keby ich
    chyba vypla istič kanála, správa aj tak najprv príde ostatným (napr. jedno nefunkčné číslo v SMS bráne
    nezablokuje KRITICKÉ SMS ostatným dispečerom).
    """
    if channel == "teams":
        pool = ["webhook"] if allowed.get("teams") else []
    else:
        pool = sorted(allowed.get(channel, set()))
    pool = [r for r in pool if only is None or (channel, r) in only]
    if len(pool) < 2 or not dedup_key:
        return pool
    failed: set = set()
    for recipient, status, error in session.execute(
            select(Notification.recipient, Notification.status, Notification.error)
            .where(Notification.dedup_key == dedup_key[:120], Notification.channel == channel,
                   Notification.status.in_(SENT_STATES + ("chyba",)))
            .order_by(Notification.id)):
        if status in SENT_STATES:
            failed.discard(recipient)
        elif not (error or "").startswith(DEFERRED):
            failed.add(recipient)
    return sorted(pool, key=lambda r: r in failed)


def send_internal(session: Session, kind: str, subject: str, body: str, priority: str = "INFO",
                  dedup_key: str = "", rec_id: int | None = None, channels: list | None = None,
                  only: set | None = None) -> list:
    """Interná správa podľa priority (alebo na zadané kanály); only – len týmto dvojiciam (kanál, príjemca),
    napr. pri opakovaní súhrnu, dennej správy alebo odovzdávky tým, ktorým neprišla."""
    out = []
    allowed = allowed_recipients()
    with delivery_scope():
        for ch in channels or (_channels(priority) or ["email"]):
            text = body if ch != "sms" else f"{subject}"[:300]
            for r in _recipients(session, allowed, ch, dedup_key, only):
                out.append(_deliver(session, ch, r, subject, text, priority, kind, dedup_key, rec_id))
    return out


def recommendation_dedup_key(rec: Recommendation) -> str:
    """Rovnaký obsah = rovnaký kľúč (rovnaká správa sa nepošle znova), ale posun odchodu o 30 min a viac
    (napr. predĺžená alebo zrušená výluka) je nová správa. ESKALOVAŤ má stály kľúč – čas „najmenej zlej“
    možnosti sa s časom posúva (lehota na trasu), hoci sa situácia nezmenila."""
    dep = (rec.chosen or {}).get("depart")
    bucket = "x"
    if dep and rec.decision != "ESKALOVAŤ":
        try:
            bucket = str(int(datetime.fromisoformat(dep).timestamp() // 1800))
        except ValueError:
            pass
    return f"rec:{rec.train_id}:{rec.fingerprint}:{bucket}"


def notify_recommendation(session: Session, rec: Recommendation, only: set | None = None) -> list:
    """Pošle odporúčanie podľa priority (INFO sa neposiela hneď, ide do súhrnu).

    only – len týmto dvojiciam (kanál, príjemca), napr. pri opakovaní po zlyhaní (retry_pairs); None = všetkým.
    Potlačí sa len správa zhodná s poslednou, ktorú príjemca o vlaku dostal (_is_rec_duplicate).
    """
    channels = _channels(rec.priority)
    if not channels:
        return []
    # všetky texty vopred – chyba pri ich príprave nesmie prísť až po odoslaní časti správ (záznamy o nich by sa
    # s chybou vrátili späť a správy by odišli znova)
    subject = recommendation_subject(rec)
    body = recommendation_body(rec)
    sms = sms_text(rec) if "sms" in channels else ""
    dedup = recommendation_dedup_key(rec)
    out = []
    allowed = allowed_recipients()
    with delivery_scope():
        for ch in channels:
            for r in _recipients(session, allowed, ch, dedup, only):
                out.append(_deliver(session, ch, r, subject, sms if ch == "sms" else body, rec.priority,
                                    "odporucanie", dedup, rec.id, train_id=rec.train_id))
    return out


def send_digest(session: Session, since: datetime, only: set | None = None) -> list:
    """Súhrn: INFO odporúčania, čakajúce potvrdenia, karanténa (sekcia 2.3 – priorita INFO).

    only – len týmto dvojiciam (kanál, príjemca): opakovanie súhrnu tým, ktorým ho nepodarilo doručiť."""
    recs = list(session.scalars(select(Recommendation).where(Recommendation.created_at >= since,
                                                             Recommendation.priority == "INFO",
                                                             Recommendation.state == "nove")))
    # fronta na potvrdenie rovnako ako na webe (/fronta, počítadlo v hlavičke): čakajúce udalosti, automaticky
    # zapísané výluky, ktoré dispečer ešte neoveril, aj potvrdené udalosti so zlyhaným zápisom. Samotné neoverené
    # automatické zápisy súhrn nevyvolajú (plánovač s nimi už počíta; pripomína ich odovzdávka a denná správa).
    from ..agent.zdravie import queue_counts

    queue = queue_counts(session)
    quarantined = list(session.scalars(select(Email).where(Email.received_at >= since,
                                                           Email.verdict == "karantena")))
    if not recs and not (queue["waiting"] or queue["failed"]) and not quarantined:
        return []
    lines = [f"Súhrn Agenta dispečera od {fmt(since)}", ""]
    if recs:
        lines.append("Odporúčania (INFO):")
        lines += [f"  - {r.summary}" for r in recs]
        lines.append("")
    if queue["total"]:
        lines.append(f"Čaká na potvrdenie dispečerom: {queue['total']} udalostí z e-mailov "
                     f"({settings().public_url.rstrip('/')}/fronta)")
        if queue["auto"]:
            lines.append(f"  - z toho zapísané automaticky – overte: {queue['auto']}")
    if quarantined:
        lines.append(f"V karanténe (nepovolený alebo neoverený odosielateľ): {len(quarantined)} e-mailov")
        lines += [f"  - {e.sender}: {e.subject}" for e in quarantined[:10]]
    body = "\n".join(lines)
    return send_internal(session, "suhrn", "[AGENT][INFO] Súhrn", body, "INFO",
                         dedup_key=f"digest:{since.isoformat()}", channels=["email"], only=only)


def is_one_shot(dedup_key: str) -> bool:
    """Jednorazové upozornenie, ktorého zlyhané doručenie opakuje agent (retry_one_shot)."""
    return bool(dedup_key) and dedup_key.startswith(ONE_SHOT_PREFIXES)


def retry_one_shot(session: Session, now: datetime | None = None) -> list:
    """Zopakuje jednorazové upozornenia, ktoré sa nepodarilo doručiť (hlásenie rušňovodiča o probléme odoslané
    z webu, pripomienka výluky do odvolania): tú istú správu (rovnaký kľúč, predmet aj text) len tým príjemcom,
    ktorým neprišla, s odstupom 5, 10, 20, 40, 60 min (pri výpadku kanála hneď, ako tomu istému príjemcovi prišla
    iná správa), najviac RETRY_HOURS od prvého pokusu."""
    now = now or now_utc()
    since = now - timedelta(hours=RETRY_HOURS)
    keys = [k for (k,) in session.execute(
        select(Notification.dedup_key).where(Notification.status == "chyba", Notification.created_at >= since,
                                             or_(*(Notification.dedup_key.startswith(p) for p in ONE_SHOT_PREFIXES)))
        .distinct())]
    out: list = []
    for key in sorted(keys):
        first = session.scalar(select(func.min(Notification.created_at)).where(Notification.dedup_key == key))
        if first is None or ensure_utc(first) < since:
            continue  # opakuje sa najviac RETRY_HOURS od prvého pokusu
        targets = retry_targets(session, key)
        if not targets:
            continue
        rows = list(session.scalars(select(Notification).where(Notification.dedup_key == key)
                                    .order_by(Notification.id.desc())))
        src = next((n for n in rows if n.channel != "sms"), rows[0])  # SMS má len predmet
        channels = [ch for ch in ("email", "teams", "sms") if any(c == ch for c, _r in targets)]
        out += send_internal(session, src.kind, src.subject, src.body, src.priority, dedup_key=key,
                             channels=channels, only=targets)
    return out


def send_test(session: Session, channels: list | None = None) -> tuple:
    """Skúšobná správa (`python -m vlaky test-notify`) na KAŽDÝ kanál interného zoznamu (e-mail, Teams, SMS) – nielen
    na e-mail, ako by ju poslala priorita INFO. Vráti (záznamy správ, poznámky ku kanálom bez príjemcov)."""
    s = settings()
    allowed = allowed_recipients()
    notes = []
    wanted = []
    for ch in dict.fromkeys(channels or ["email", "teams", "sms"]):
        if allowed.get(ch):
            wanted.append(ch)
        elif s.shadow_mode and ch != "email":
            notes.append(f"{ch}: v tieňovej prevádzke vypnuté (VLAKY_SHADOW_MODE=true)")
        else:
            setting = {"email": "VLAKY_SHADOW_EMAILS" if s.shadow_mode else "VLAKY_NOTIFY_EMAILS",
                       "teams": "VLAKY_TEAMS_WEBHOOK_URL", "sms": "VLAKY_NOTIFY_PHONES"}[ch]
            notes.append(f"{ch}: bez príjemcov – nenastavené ({setting})")
    out = send_internal(session, "test", "[AGENT] Skúšobná správa",
                        "Toto je skúšobná správa Agenta dispečera (test-notify). Ak ste ju dostali, kanál funguje.",
                        "INFO", dedup_key="", channels=wanted) if wanted else []
    return out, notes
