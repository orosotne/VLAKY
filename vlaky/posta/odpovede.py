"""Odpovede dispečerov na odporúčania e-mailom (sekcia 2.4 PROMPT.md).

Dispečer môže na e-mail agenta odpovedať prvým riadkom:
    PRIJAŤ                      → odporúčanie prijaté
    ZAMIETNUŤ <dôvod>           → odporúčanie zamietnuté (dôvod sa uloží)
Odpoveď sa prijme LEN ak:
  * odosielateľ je na internom zozname dispečerov (VLAKY_NOTIFY_EMAILS / VLAKY_SHADOW_EMAILS),
  * e-mail prešiel overením odosielateľa (verdikt „ok“ – zoznam povolených domén a DMARC),
  * v predmete je značka [AGENT] a v texte odkaz /odporucania/<číslo>.
Odpoveďou sa mení len stav odporúčania. Údaje o výlukách a trasách sa potvrdzujú iba v aplikácii.
"""
from __future__ import annotations

import re
import unicodedata
from email.utils import parseaddr

from sqlalchemy.orm import Session

from ..audit import audit
from ..cas import now_utc
from ..config import settings
from ..models import Email, Recommendation

_LINK = re.compile(r"/odporucania/(\d+)")


def _plain(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in t if not unicodedata.combining(c)).upper()


def _dispatchers() -> set:
    s = settings()
    return {a.lower() for a in s.csv(s.notify_emails) + s.csv(s.shadow_emails)}


def _first_line(body: str) -> str:
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or line.startswith(">"):
            continue
        return line
    return ""


def handle_dispatcher_reply(session: Session, email: Email) -> bool:
    """Ak je e-mail odpoveďou dispečera na odporúčanie, spracuje ho a vráti True."""
    addr = parseaddr(email.sender or "")[1].lower()
    if addr not in _dispatchers() or "[AGENT]" not in (email.subject or "").upper():
        return False
    if email.verdict != "ok":
        audit(session, "agent", "reply_rejected", "email", email.id, sender=addr,
              reason="odosielateľ neprešiel overením (DMARC / zoznam povolených)")
        email.status, email.error = "spracovany", "Odpoveď dispečera neprešla overením odosielateľa."
        return True
    first = _first_line(email.body_text)
    head = _plain(first)
    m = _LINK.search(email.body_text or "")
    if not m or not (head.startswith("PRIJAT") or head.startswith("ZAMIETNUT")):
        email.status, email.error = "spracovany", "Odpoveď dispečera bez platného príkazu (PRIJAŤ / ZAMIETNUŤ)."
        audit(session, "agent", "reply_unparsed", "email", email.id, sender=addr, first_line=first[:200])
        return True
    rec = session.get(Recommendation, int(m.group(1)))
    if rec is None:
        email.status, email.error = "spracovany", "Odporúčanie z odpovede neexistuje."
        return True
    if head.startswith("PRIJAT"):
        rec.state, rec.dispatcher_note = "prijate", rec.dispatcher_note
    else:
        reason = first.split(None, 1)[1].strip() if len(first.split(None, 1)) > 1 else ""
        if not reason:
            email.status, email.error = "spracovany", "Zamietnutie bez dôvodu – použite: ZAMIETNUŤ <dôvod>."
            return True
        rec.state, rec.dispatcher_note = "zamietnute", reason[:1000]
    rec.decided_by, rec.decided_at = addr, now_utc()
    email.status = "spracovany"
    audit(session, addr, "recommendation_reply", "recommendation", rec.id, state=rec.state,
          note=rec.dispatcher_note, email_id=email.id)
    return True
