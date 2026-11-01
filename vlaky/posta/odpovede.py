"""Odpovede dispečerov na odporúčania e-mailom (sekcia 2.4 PROMPT.md).

Dispečer môže na e-mail agenta odpovedať prvým riadkom:
    PRIJAŤ                      → odporúčanie prijaté
    ZAMIETNUŤ <dôvod>           → odporúčanie zamietnuté (dôvod sa uloží)
Príkaz je len celé prvé slovo riadku („PRIJAŤ“, „Prijať.“, „ZAMIETNUŤ: dôvod“) – veta, ktorá sa ním len začína
(„Prijateľnejšie by bolo…“), príkazom nie je.
Odpoveď sa prijme LEN ak:
  * odosielateľ je na internom zozname dispečerov (VLAKY_NOTIFY_EMAILS / VLAKY_SHADOW_EMAILS),
  * e-mail prešiel overením odosielateľa (verdikt „ok“ – zoznam povolených domén a DMARC),
  * v predmete je značka [AGENT] a v texte odkaz /odporucania/<číslo>,
  * odporúčanie je aktuálne odporúčanie vlaku – o zastaranom (nahradenom novším) sa nerozhoduje, rovnako ako
    vo webe.
Odpoveďou sa mení len stav odporúčania. Údaje o výlukách a trasách sa potvrdzujú iba v aplikácii. Ak sa
príkaz nezapíše (zastarané odporúčanie, zamietnutie bez dôvodu, neznáme odporúčanie, odpoveď bez príkazu), agent
dispečerovi krátko odpíše prečo – len na jeho adresu z interného zoznamu.
"""
from __future__ import annotations

import re
import unicodedata
from email.utils import parseaddr
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import audit
from ..cas import fmt, now_utc
from ..config import settings
from ..models import Email, Recommendation, Train

_LINK = re.compile(r"/odporucania/(\d+)")
_COMMANDS = {"PRIJAT": "prijate", "ZAMIETNUT": "zamietnute"}
# prvé slovo riadku: končí medzerou alebo interpunkciou („PRIJAŤ.“, „ZAMIETNUŤ: dôvod“, „PRIJAŤ – súhlasím“)
_FIRST_WORD = re.compile(r"\s*(\S+?)(?=$|[\s.,;:!?\-–—])")
_DECIDABLE = ("nove", "prijate", "upravene", "zamietnute")


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


def _command(first: str) -> tuple[str, str]:
    """(príkaz PRIJAT/ZAMIETNUT alebo „“, zvyšok riadku) – príkaz len ako celé prvé slovo."""
    m = _FIRST_WORD.match(first or "")
    if not m or _plain(m.group(1)) not in _COMMANDS:
        return "", ""
    return _plain(m.group(1)), first[m.end():].lstrip(" \t.,;:!?-–—").rstrip()


def _answer(session: Session, email: Email, addr: str, subject: str, lines: list[str], dedup_key: str) -> None:
    """Krátka odpoveď dispečerovi, prečo sa jeho príkaz nezapísal (len e-mail, len na jeho adresu zo zoznamu)."""
    from ..notifikacie.odosielanie import send_internal

    try:
        send_internal(session, "odpoved", subject[:300], "\n".join(lines), "DÔLEŽITÉ", dedup_key=dedup_key[:120],
                      channels=["email"], only={("email", addr)})
    except Exception as exc:  # noqa: BLE001 – odpoveď je len informácia, príkaz je už vybavený (zamietnutý)
        email.error = "\n".join(filter(None, [email.error, f"Odpoveď dispečerovi sa neodoslala: "
                                                          f"{type(exc).__name__}"]))


def _current(session: Session, rec: Recommendation) -> Optional[Recommendation]:
    """Aktuálne (posledné) odporúčanie vlaku."""
    return session.scalar(select(Recommendation).where(Recommendation.train_id == rec.train_id)
                          .order_by(Recommendation.id.desc()).limit(1))


def _refuse(session: Session, email: Email, addr: str, error: str, explanation: list[str], subject: str,
            dedup_key: str, **detail) -> bool:
    email.status, email.error = "spracovany", error
    audit(session, "agent", "reply_rejected", "email", email.id, sender=addr, reason=error, **detail)
    first = _first_line(email.body_text)
    _answer(session, email, addr, subject,
            [f"Vaša odpoveď „{first[:120]}“ (e-mail „{(email.subject or '')[:120]}“) sa nezapísala.", ""]
            + explanation + ["", "Odpovedajte prvým riadkom PRIJAŤ alebo ZAMIETNUŤ <dôvod> na e-mail aktuálneho "
                                 "odporúčania, alebo rozhodnite v aplikácii."], dedup_key)
    return True


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
    command, rest = _command(first)
    m = _LINK.search(email.body_text or "")
    base = settings().public_url.rstrip("/")
    if not m or not command:
        email.status, email.error = "spracovany", "Odpoveď dispečera bez platného príkazu (PRIJAŤ / ZAMIETNUŤ)."
        audit(session, "agent", "reply_unparsed", "email", email.id, sender=addr, first_line=first[:200])
        what = "nezačína príkazom PRIJAŤ ani ZAMIETNUŤ" if not command else \
            "neobsahuje odkaz na odporúčanie (/odporucania/<číslo>)"
        # najviac jedna taká odpoveď za interval deduplikácie (automatické odpovede nesmú vyvolať slučku)
        _answer(session, email, addr, "[AGENT][DÔLEŽITÉ] Odpoveď na odporúčanie bez platného príkazu",
                [f"Vaša odpoveď „{first[:120]}“ {what} – stav odporúčania sa nezmenil.", "",
                 "Príkaz je celé prvé slovo prvého riadku: PRIJAŤ alebo ZAMIETNUŤ <dôvod>."],
                f"odpoved_bez_prikazu:{addr}")
        return True
    rec_id = int(m.group(1))
    rec = session.get(Recommendation, rec_id) if rec_id < 2 ** 31 else None   # mimo rozsahu stĺpca = neexistuje
    if rec is None:
        return _refuse(session, email, addr, "Odporúčanie z odpovede neexistuje.",
                       [f"Odporúčanie #{m.group(1)} neexistuje."],
                       "[AGENT][DÔLEŽITÉ] Odpoveď na neexistujúce odporúčanie", f"odpoved:{email.id}")
    current = _current(session, rec)
    if rec.state not in _DECIDABLE or (current is not None and current.id != rec.id):
        # zastarané odporúčanie (nahradilo ho novšie) – rovnako ako web: rozhoduje sa o aktuálnom
        train = session.get(Train, rec.train_id)
        label = f"vlak {train.number}" if train is not None else f"vlak #{rec.train_id}"
        explanation = [f"Odporúčanie #{rec.id} ({label}, {rec.decision}) je zastarané – medzitým ho nahradilo "
                       "novšie odporúčanie."]
        if current is not None and current.id != rec.id:
            explanation = [f"Aktuálne odporúčanie pre {label}: {current.decision} ({current.color}), "
                           f"{fmt(current.created_at)} – {base}/odporucania/{current.id}", ""] + explanation
        return _refuse(session, email, addr, "Odporúčanie je zastarané – rozhodnite o aktuálnom odporúčaní pre vlak.",
                       explanation, f"[AGENT][DÔLEŽITÉ] Odpoveď nezapísaná – odporúčanie #{rec.id} je zastarané",
                       f"odpoved:{email.id}", recommendation=rec.id, state=rec.state,
                       current=current.id if current is not None else None)
    if _COMMANDS[command] == "prijate":
        rec.state, rec.dispatcher_note = "prijate", rec.dispatcher_note
    else:
        if not rest:
            return _refuse(session, email, addr, "Zamietnutie bez dôvodu – použite: ZAMIETNUŤ <dôvod>.",
                           [f"Zamietnutie odporúčania #{rec.id} potrebuje dôvod v tom istom riadku: "
                            "ZAMIETNUŤ <dôvod>."],
                           f"[AGENT][DÔLEŽITÉ] Odpoveď nezapísaná – zamietnutie odporúčania #{rec.id} bez dôvodu",
                           f"odpoved:{email.id}", recommendation=rec.id)
        rec.state, rec.dispatcher_note = "zamietnute", rest[:1000]
    rec.decided_by, rec.decided_at = addr, now_utc()
    email.status = "spracovany"
    audit(session, addr, "recommendation_reply", "recommendation", rec.id, state=rec.state,
          note=rec.dispatcher_note, email_id=email.id)
    return True
