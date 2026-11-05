"""Spracovanie pošty Agenta dispečera: príjem → uloženie → extrakcia → validácia → registre (M4, M5, M6).

Verejné funkcie (volá ich slučka agenta a webová aplikácia; signatúry sú záväzné):

    fetch_and_store(session) -> list[Email]
    process_email(session, email) -> list[ExtractedEvent]
    apply_event(session, event, actor="agent") -> dict
    confirm_event(session, event_id, actor, edits=None) -> ExtractedEvent
    reject_event(session, event_id, actor, reason="") -> ExtractedEvent
    process_mailbox(session, now=None) -> dict

Transakcie: ``fetch_and_store`` a ``process_mailbox`` potvrdzujú (commit) po každom e-maile, aby sa pri
reštarte nič nestratilo a chyba jedného e-mailu neovplyvnila ostatné. ``process_email``, ``apply_event``,
``confirm_event`` a ``reject_event`` len zapisujú do relácie (flush) – commit robí volajúci.

Bezpečnosť: e-mail v karanténe sa nikdy neextrahuje; udalosť, ktorá potrebuje potvrdenie, sa do registrov
nedostane bez potvrdenia dispečerom; udalosti z podozrivého alebo preposlaného e-mailu sa nikdy neaplikujú
automaticky; výluky a trasy sa automaticky zapisujú len od ŽSR.

Nepotvrdené výluky (sekcia 2.1 – kým zmena nie je potvrdená, počíta sa s oboma možnosťami): čakajúca nová
výluka, pomalá jazda či mimoriadnosť má hneď záznam v stave „navrh“ (plánovač s ním počíta a označí ho ako
nepotvrdený); čakajúca zmena výluky pôvodný záznam nemení a nový termín pridá ako dočasný záznam „navrh“
(``proposed_change_of``); čakajúce zrušenie výluku nemení. Potvrdenie zapíše zmenu do pôvodného záznamu a
dočasný odstráni, zamietnutie dočasný záznam odstráni (vlastný návrh zruší).

Poradie udalostí: záznam v registri patrí udalosti, ktorá ho naposledy zapísala (``source_event_id``).
Staršia udalosť stav zapísaný novšou udalosťou neprepíše (pri potvrdení sa označí ako nahradená) a jej
zamietnutie mení záznam, len ak jej ešte patrí.

Čiastočná zmena či zrušenie: zmena alebo zrušenie nikdy neodstráni ani nepresunie dni či hodiny výluky, ktoré
e-mail výslovne neuvádza. Týka sa len časti výluky (jeden deň viacdennej výluky, časť dňa či noci) – výluka sa
rozdelí: nedotknuté časti ostanú ako samostatné záznamy (rovnaké číslo, úsek a obmedzenie), zmena či zrušenie sa
zapíše len pre uvedenú časť. Kde rozdelenie nie je bezpečné (výluka do odvolania, čakajúca zmena tej istej výluky,
súvislá viacdenná výluka), zmena sa zapíše ako ďalšia výluka a pôvodná ostane celá; zrušenie sa nezapíše a
dispečer dostane vysvetlenie (udalosť ostáva vo fronte).

Súbežnosť: potvrdenie, zamietnutie aj automatický zápis udalosti idú po jednom (:func:`lock_register` –
PostgreSQL poradný zámok, SQLite zámok zápisu); po získaní zámku sa stav registra číta znova.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email import policy as _mail_policy
from email.parser import BytesHeaderParser
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import DateTime, and_, or_, select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session, object_session

from ..audit import audit
from ..cas import fmt, local, now_utc
from ..config import settings
from ..models import Attachment, Closure, Email, ExtractedEvent, Handover, Order, Path as TrainPath, Train
from .datumy import parse_hhmm
from .extrakcia import extract
from .overenie import KARANTENA, OK, PODOZRIVY, verify, zsr_sender
from .parser import parse_message
from .stanice import stations
from .text import clean_text, fold, header_text
from .udalosti import KIND_LABELS, SCHEMAS, MailDocument
from .validacia import (find_train, parse_number, parse_utc, resolve_border, train_digits, train_mismatch,
                        validate_event)
from .zdroje import MailSourceError, RawMail, get_source, kv_get, kv_set

OPEN_END_DAYS = 30            # výluka „do odvolania“ – technický koniec, kým ŽSR neoznámi skutočný (posúva sa)
OPEN_END_REMIND_DAYS = 7      # keď je technický koniec bližšie, posunie sa a dispečer dostane pripomienku
MATCH_TOLERANCE = timedelta(hours=1)
CHANGE_MATCH_WINDOW = timedelta(days=3)    # zmena bez čísla výluky: pôvodný termín najviac ± 3 dni
# zrušenie s termínom: časť výluky mimo rušeného termínu kratšia ako toto je nepresnosť termínu (napr. 07:55 oproti
# 08:00), nie časť výluky, ktorá ostáva
SPLIT_TOLERANCE = timedelta(minutes=15)
SPLIT_MAX_WINDOWS = 400       # dennú výluku s viac oknami zápis nerozdelí (zmena sa zapíše ako ďalšia výluka)
REGISTER_LOCK_KEY = 0x564C5247  # „VLRG“ – poradný zámok zápisov udalostí z e-mailov do registrov (PostgreSQL)
STORE_RETRIES = 3             # po toľkých neúspešných pokusoch o uloženie sa e-mail odloží do karantény
_RETRY_KEY = "posta_neulozene"
_LEGACY_OPEN_END_NOTE = "Koniec neurčený (do odvolania)"     # poznámka starších záznamov bez príznaku
# udalosti, ktoré majú (aj nepotvrdené) záznam v registri výluk – plánovač s nimi počíta
CLOSURE_EVENT_KINDS = ("vyluka_nova", "vyluka_zmena", "pomala_jazda", "mimoriadnost")
# udalosti, ktoré ako správca infraštruktúry vydáva len ŽSR (od iného odosielateľa vždy na potvrdenie)
ZSR_KINDS = ("vyluka_nova", "vyluka_zmena", "vyluka_zrusenie", "pomala_jazda", "mimoriadnost", "trasa_pridelena",
             "trasa_zmena", "trasa_zrusena")
# čakajúce zrušenie či zmena pridelenej trasy: plánovač počíta s trasou v registri, ktorá nemusí platiť
UNCERTAIN_PATH_KINDS = ("trasa_zrusena", "trasa_zmena")
# hlavička každej správy, ktorú agent odošle (vlaky.notifikacie.odosielanie) – kópia späť v schránke sa nespracúva
AGENT_HEADER = "X-Agent-Dispecer"


class ApplyError(Exception):
    """Udalosť nemožno zapísať do registra (správa je pre dispečera).

    ``retry`` – False: správa sama hovorí, čo má dispečer urobiť (napr. upraviť register a udalosť zamietnuť);
    všeobecná rada „po odstránení príčiny ju potvrďte znova“ k nej nepatrí."""

    def __init__(self, message: str, retry: bool = True) -> None:
        super().__init__(message)
        self.retry = retry


class EventProcessed(ValueError):
    """Udalosť medzitým spracoval niekto iný (súbežné potvrdenie či zamietnutie) – nič sa nezmenilo."""


class NeedsConfirmation(Exception):
    """Udalosť sa nesmie zapísať automaticky – zmenila by overený alebo blízky stav (dôvody pre dispečera)."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _short(exc: BaseException, limit: int = 300) -> str:
    return header_text(str(exc))[:limit]


def lock_register(session: Session) -> None:
    """Zápisy udalostí z e-mailov do registrov (potvrdenie, zamietnutie, automatický zápis) idú po jednom.

    PostgreSQL: transakčný poradný zámok (platí do konca transakcie – commit, rollback); SQLite: zámok zápisu
    (BEGIN IMMEDIATE, ak relácia transakciu ešte nemá). Po získaní zámku sa záznamy registrov a udalosti v relácii
    načítajú znova: dve súbežné potvrdenia zmien tej istej výluky (dvaja dispečeri, dispečer a agent) sa tak
    zapíšu jedno po druhom a druhé vidí, čo zapísalo prvé – novšia zmena sa nestratí."""
    from ..db import prepare_savepoints

    if session.connection().dialect.name == "postgresql":
        session.execute(sql_text("SELECT pg_advisory_xact_lock(CAST(:k AS integer), 0)"), {"k": REGISTER_LOCK_KEY})
    else:
        prepare_savepoints(session)
    session.flush()
    for obj in list(session.identity_map.values()):
        if isinstance(obj, (Closure, ExtractedEvent, TrainPath, Handover, Order)):
            session.expire(obj)


def confirmable(ev: ExtractedEvent) -> bool:
    """Udalosť čaká na dispečera: vo fronte, zapísaná automaticky bez overenia, alebo potvrdená so zlyhaným zápisom."""
    return ev.status == "navrh" or (ev.status == "aplikovane" and ev.confirmed_at is None) or apply_failed(ev)


def _who(by: Any, at: Any) -> str:
    when = parse_utc(at) if isinstance(at, str) else at
    parts = [str(by)] if by else []
    if when is not None:
        parts.append(fmt(when))
    return f" ({', '.join(parts)})" if parts else ""


def processed_state(ev: ExtractedEvent) -> str:
    """Ako bola udalosť spracovaná – text pre dispečera (odkaz z upozornenia, súbežné potvrdenie): „zamietnutá
    (disp2, 16.11. 10:30)“, „potvrdená a zapísaná do registra (disp1, …)“, „potvrdená (disp2, …), ale nič sa
    nezapísalo – záznam medzitým zmenila novšia udalosť #5“ …"""
    val = ev.validation if isinstance(ev.validation, dict) else {}
    if ev.status == "zamietnute":
        rej = val.get("rejection") if isinstance(val.get("rejection"), dict) else {}
        return "zamietnutá" + _who(rej.get("by"), rej.get("at"))
    who = _who(ev.confirmed_by, ev.confirmed_at) if ev.confirmed_at is not None else ""
    if ev.status == "aplikovane":
        result = val.get("apply_result") or val.get("applied") or {}
        if ev.confirmed_at is None:
            return "zapísaná automaticky, čaká na overenie dispečerom"
        if isinstance(result, dict) and result.get("action") == "nahradene":
            newer = f" #{result['superseded_by']}" if result.get("superseded_by") else ""
            return f"potvrdená{who}, ale nič sa nezapísalo – záznam medzitým zmenila novšia udalosť{newer}"
        if isinstance(result, dict) and result.get("action") == "bez_zmeny":
            return f"potvrdená{who} – register sa nezmenil (termín už v registri bol)"
        return f"potvrdená a zapísaná do registra{who}"
    if ev.status == "potvrdene":
        return f"potvrdená{who}, ale zápis do registra zlyhal"
    if ev.status == "navrh" and not ev.needs_confirmation:
        return "informačná, nevyžaduje potvrdenie"
    return "čaká na potvrdenie"


# ====================================================================== príjem
def fetch_and_store(session: Session) -> list[Email]:
    """Načíta nové e-maily zo zdroja a uloží ich (idempotentne podľa Message-ID).

    E-mail v karanténe sa uloží s verdiktom ``karantena`` a ďalej sa nespracúva.
    """
    return _fetch_and_store(session, [])[0]


class _Retries:
    """Počítadlo neúspešných pokusov o uloženie e-mailu (v ``KeyValue``, prežije reštart)."""

    def __init__(self, session: Session) -> None:
        self.session = session
        try:
            val = kv_get(session, _RETRY_KEY, {}) or {}
        except Exception:
            val = {}
        self.counts: dict = dict(val) if isinstance(val, dict) else {}

    @staticmethod
    def key(raw: RawMail) -> str:
        return f"{raw.source}:{raw.uid}"[:200]

    def forget(self, raw: RawMail) -> None:
        if self.key(raw) in self.counts:
            self.counts.pop(self.key(raw))
            kv_set(self.session, _RETRY_KEY, dict(self.counts))

    def bump(self, raw: RawMail) -> int:
        n = int(self.counts.get(self.key(raw), 0)) + 1
        self.counts[self.key(raw)] = n
        kv_set(self.session, _RETRY_KEY, dict(self.counts))
        self.session.commit()
        return n


def _fetch_and_store(session: Session, errors: list[str]) -> tuple[list[Email], bool, bool]:
    """Vráti (uložené e-maily, či sa zdroj pošty podarilo prečítať, či je schránka dočítaná).

    Schránka je dočítaná, keď zdroj nehlási ďalšie e-maily nad dávku (BATCH) a za chybným e-mailom neostali
    neprečítané (zdroj s poradím – IMAP – sa pri chybe zastaví). Inak je čítanie len čiastočné (napr. nahromadená
    pošta po výpadku agenta) a nesmie sa počítať ako úspešná kontrola schránky."""
    try:
        src = get_source(session)
        raws = src.fetch_new()
    except MailSourceError as exc:
        errors.append(str(exc))
        return [], False, False
    except Exception as exc:  # zdroj pošty nesmie zastaviť agenta
        errors.append(f"Zdroj pošty zlyhal: {type(exc).__name__}: {_short(exc)}")
        return [], False, False
    stored: list[Email] = []
    retries = _Retries(session)
    unread_left = False                  # zdroj s poradím sa zastavil – za chybným e-mailom ostali ďalšie
    for i, raw in enumerate(raws):
        try:
            email = _store_one(session, raw)
            retries.forget(raw)
            session.commit()
        except Exception as exc:
            session.rollback()
            errors.append(f"E-mail {raw.uid} sa nepodarilo uložiť ({type(exc).__name__}: {_short(exc)}).")
            email = _store_failed(session, retries, raw, exc, errors)
            if email is None:            # skúsi sa znova pri ďalšom cykle (zdroj sa neposunie)
                if src.sequential:
                    unread_left = i < len(raws) - 1
                    break
                continue
        try:
            new_path = src.mark_done(raw)
            if email is not None and raw.source == "directory" and new_path:
                email.raw_path = new_path
            session.commit()
        except Exception as exc:
            session.rollback()
            errors.append(f"E-mail {raw.uid} sa nepodarilo označiť ako spracovaný ({type(exc).__name__}).")
            if src.sequential:
                unread_left = i < len(raws) - 1
                break
        if email is not None:
            stored.append(email)
    return stored, True, not unread_left and not bool(getattr(src, "more", False))


def _store_failed(session: Session, retries: _Retries, raw: RawMail, exc: Exception,
                  errors: list[str]) -> Optional[Email]:
    """Neuložiteľný e-mail: najprv sa skúša znova, po ``STORE_RETRIES`` pokusoch sa odloží (karanténa).

    Jeden „jedovatý“ e-mail tak nikdy natrvalo nezablokuje schránku (IMAP číta v poradí UID) a nič sa
    nestratí – originál je v archíve a záznam v karanténe vidí dispečer v súhrne.
    """
    try:
        n = retries.bump(raw)
    except Exception:
        session.rollback()
        return None
    if n < STORE_RETRIES:
        return None
    try:
        email = _store_dead_letter(session, raw, exc, n)
        retries.forget(raw)
        session.commit()
    except Exception as exc2:
        session.rollback()
        errors.append(f"E-mail {raw.uid} sa nepodarilo odložiť do karantény ({type(exc2).__name__}).")
        return None
    errors.append(f"E-mail {raw.uid} sa nepodarilo uložiť ani na {n}. pokus – odložený do karantény "
                  f"(#{email.id}), originál je v archíve.")
    return email


def _store_dead_letter(session: Session, raw: RawMail, exc: Exception, attempts: int) -> Email:
    sha = hashlib.sha256(raw.data or b"").hexdigest()
    mid = "sha256:" + sha
    existing = session.scalar(select(Email).where(Email.message_id == mid))
    if existing is not None:
        return existing
    try:
        head = parse_message(raw.data, extract_attachments=False)
        sender, subject = head.sender or head.from_header, head.subject
    except Exception:  # pragma: no cover - parse_message výnimky nevyhadzuje
        sender, subject = "", ""
    raw_path = raw.path if raw.source == "directory" else _archive(raw, sha)
    email = Email(message_id=mid, source=raw.source, received_at=raw.received_at or now_utc(),
                  sender=header_text(sender)[:300], subject=header_text(subject)[:500], body_text="",
                  auth_results="", verdict=KARANTENA,
                  verdict_reason=f"e-mail sa nepodarilo uložiť ani na {attempts}. pokus – nespracovaný, originál je "
                                 "v archíve (skontrolujte ho ručne)",
                  status="chyba", error=f"Uloženie zlyhalo: {type(exc).__name__}: {_short(exc)}", raw_path=raw_path,
                  forwarded_from="")
    session.add(email)
    session.flush()
    audit(session, "agent", "email_neulozeny", "Email", email.id, uid=raw.uid, zdroj=raw.source, pokusy=attempts,
          chyba=f"{type(exc).__name__}: {_short(exc)}")
    return email


def _archive(raw: RawMail, sha: str) -> str:
    """Kópia originálu (IMAP/Graph) do mail_dir/archiv/RRRR/MM/ – audit (sekcia M5)."""
    d = local(raw.received_at or now_utc())
    folder = Path(settings().mail_dir) / "archiv" / f"{d:%Y}" / f"{d:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sha[:24]}.eml"
    if not path.exists():
        path.write_bytes(raw.data)
    return str(path)


def _sender_key(pm: Any) -> str:
    return header_text(pm.sender or pm.from_header)[:300].lower()


def _collision_id(pm: Any) -> str:
    """Message-ID kópie s iným obsahom (rovnaké Message-ID od iného odosielateľa, alebo od toho istého odosielateľa
    s iným textom) – jednoznačné, do 300 znakov."""
    return f"{pm.message_id[:280]}#{pm.sha256[:16]}"


def _own_addresses() -> set[str]:
    """Adresy, z ktorých agent posiela svoje správy (SMTP odosielateľ, pri odosielaní cez Graph jeho schránka)."""
    s = settings()
    values = [s.smtp_from]
    if (s.notify_transport or "").lower() == "graph":
        values.append(s.graph_mailbox)
    out = set()
    for value in values:
        addr = parseaddr(str(value or ""))[1].strip().lower()
        if "@" in addr:
            out.add(addr)
    return out


def _own_message(raw: RawMail, head: Any) -> str:
    """Dôvod, prečo je e-mail vlastnou správou agenta („“ = nie je).

    Kópia odporúčania, súhrnu, odovzdávky či dennej správy, ktorá sa vráti do schránky agenta (napr. pravidlo
    toku pošty kopíruje dispečerskú schránku agentovi a tá je medzi príjemcami notifikácií), nie je prevádzková
    správa – inak by z každej vznikla udalosť vo fronte (alebo záznam v karanténe) a súhrn by sa sám udržiaval.
    Spozná sa podľa adresy odosielateľa agenta alebo podľa hlavičky ``X-Agent-Dispecer`` (zostane aj vtedy, keď
    distribučný zoznam prepíše odosielateľa). Hlavička ``Auto-Submitted`` sama nestačí – posielajú ju aj
    automatické systémy ŽSR (KIS).
    """
    if head.sender and head.sender in _own_addresses():
        return f"odosielateľ {head.sender} je adresa agenta"
    try:
        headers = BytesHeaderParser(policy=_mail_policy.compat32).parsebytes((raw.data or b"")[:65536])
        if headers.get(AGENT_HEADER) is not None:
            return f"správa má hlavičku {AGENT_HEADER} (odoslal ju agent)"
    except Exception:  # noqa: BLE001 – poškodená hlavička: e-mail sa spracuje bežným spôsobom
        pass
    return ""


def _content_key(subject: Any, body: Any, attachments: Iterable[tuple]) -> str:
    """Odtlačok obsahu e-mailu: predmet, text a prílohy (názov, veľkosť).

    Rovnaký odtlačok má tá istá správa doručená znova (aj s inými hlavičkami Received či inou cestou cez
    distribučný zoznam); iná správa s rovnakým Message-ID má iný predmet, text alebo prílohy."""
    h = hashlib.sha256()
    h.update(header_text(subject)[:500].encode("utf-8", "replace"))
    h.update(b"\0")
    h.update(str(body or "").encode("utf-8", "replace"))
    for name, size in sorted((header_text(n)[:300], int(sz or 0)) for n, sz in attachments):
        h.update(b"\0" + name.encode("utf-8", "replace") + b"\0" + str(size).encode())
    return h.hexdigest()


def _known_copy(session: Session, head: Any) -> bool:
    """E-mail s rovnakým Message-ID od toho istého odosielateľa a s rovnakým obsahom už je uložený (aj ako kópia
    s jednoznačným Message-ID) – opakované doručenie sa preskočí. Kópie v karanténe rieši volajúci."""
    mid = head.message_id
    rows = session.scalars(select(Email).where(Email.verdict != KARANTENA, or_(
        Email.message_id == mid, Email.message_id.startswith(mid[:280] + "#", autoescape=True))))
    key, sender = None, _sender_key(head)
    for e in rows:
        if (e.sender or "").lower() != sender:
            continue
        if key is None:
            key = _content_key(head.subject, head.body_text, [(a.filename, a.size) for a in head.attachments])
        if _content_key(e.subject, e.body_text, [(a.filename, a.size) for a in e.attachments]) == key:
            return True
    return False


def _store_one(session: Session, raw: RawMail) -> Optional[Email]:
    head = parse_message(raw.data, extract_attachments=False)
    own = _own_message(raw, head)
    if own:
        # vlastná správa agenta (kópia notifikácie) – bez uloženia a extrakcie, len záznam v audite (s výsledkom
        # overenia odosielateľa: podvrh s adresou agenta tam prezradí neúspešné overenie)
        ver = verify(head)
        audit(session, "agent", "email_vlastny_preskoceny", "Email", "", message_id=head.message_id,
              sender=header_text(head.sender or head.from_header)[:300], subject=header_text(head.subject)[:300],
              zdroj=raw.source, dovod=own, overenie=ver.verdict, overenie_dovod=ver.reason_text[:300])
        return None
    existing = session.scalar(select(Email).where(Email.message_id == head.message_id))
    collision = None
    if existing is not None and existing.verdict != KARANTENA:
        if _known_copy(session, head) or \
                session.scalar(select(Email.id).where(Email.message_id == _collision_id(head))) is not None:
            return None                               # už spracované (idempotencia), aj kópia v karanténe
        # rovnaké Message-ID s iným obsahom – od iného odosielateľa (napr. pravý ROV od ŽSR, ktorého Message-ID
        # použil skôr iný povolený odosielateľ) alebo od toho istého (KIS či poštový server Message-ID zopakoval):
        # nesmie potichu „zhltnúť“ e-mail – uloží sa ako samostatný e-mail
        collision, existing = existing, None
    same_sender = collision is not None and (collision.sender or "").lower() == _sender_key(head)
    ver = verify(head)
    pm = head
    if ver.verdict != KARANTENA:
        pm = parse_message(raw.data)                  # prílohy len od povoleného a overeného odosielateľa
        ver = verify(pm)
    if existing is not None and ver.verdict == KARANTENA:
        return None                                   # ďalšia kópia v karanténe – nič nové
    if same_sender and ver.verdict == KARANTENA:
        # neoverená (napr. podvrhnutá) správa s Message-ID už uloženého e-mailu toho istého odosielateľa – nič
        # nové sa neukladá ani nespracúva, ale nezmizne potichu: zostane záznam v audite
        audit(session, "agent", "email_message_id_kolizia", "Email", collision.id, message_id=head.message_id,
              odosielatel=_sender_key(head), verdikt=KARANTENA, dovod=ver.reason_text, skorsi_email=collision.id,
              rovnaky_odosielatel=True, ulozeny=False, zdroj=raw.source)
        return None
    errors = list(pm.errors)
    if raw.truncated:
        errors.append("E-mail prekročil limit veľkosti – načítaná len časť.")
    for a in pm.attachments:
        if a.error:
            errors.append(f"Príloha „{a.filename}“: {a.error}")
    raw_path = raw.path if raw.source == "directory" else _archive(raw, pm.sha256)
    # čas doručenia podľa zdroja (IMAP INTERNALDATE, Graph, súbor); hlavičku Date nastavuje odosielateľ –
    # je len informatívna (inak by spätne datovaný e-mail obišiel súhrny a uchovávanie)
    received = raw.received_at or now_utc()
    fields = dict(source=raw.source, received_at=received, sender=header_text(pm.sender or pm.from_header)[:300],
                  subject=header_text(pm.subject)[:500], body_text=pm.body_text,
                  auth_results="\n".join(pm.auth_results),
                  verdict=ver.verdict, verdict_reason=ver.reason_text,
                  status="spracovany" if ver.verdict == KARANTENA else "prijaty", raw_path=raw_path,
                  forwarded_from=",".join(ver.forwarded_from))
    if existing is not None:
        return _replace_quarantined(session, existing, pm, fields, errors)
    message_id = pm.message_id
    if collision is not None:
        message_id = _collision_id(pm)
        if same_sender:
            errors.append(f"Rovnaké Message-ID {pm.message_id[:200]} už má e-mail #{collision.id} od toho istého "
                          "odosielateľa s iným obsahom – uložený ako samostatný e-mail (odosielateľ alebo poštový "
                          "server Message-ID zopakoval), skontrolujte oba.")
        else:
            errors.append(f"Rovnaké Message-ID {pm.message_id[:200]} už má e-mail #{collision.id} od iného "
                          f"odosielateľa ({collision.sender}) – uložený ako samostatný e-mail, overte pravosť oboch.")
    email = Email(message_id=message_id, error="\n".join(errors), **fields)
    for a in pm.attachments:
        email.attachments.append(Attachment(filename=header_text(a.filename)[:300],
                                            content_type=header_text(a.content_type)[:120], size=a.size, text=a.text))
    session.add(email)
    session.flush()
    audit(session, "agent", "email_prijaty", "Email", email.id, message_id=email.message_id, sender=email.sender,
          subject=email.subject, verdict=email.verdict, dovod=email.verdict_reason, zdroj=raw.source,
          datum_hlavicky=pm.date.isoformat() if pm.date else None,
          preposlane_od=email.forwarded_from or None)
    if collision is not None:
        audit(session, "agent", "email_message_id_kolizia", "Email", email.id, message_id=pm.message_id,
              odosielatel=email.sender, verdikt=email.verdict, skorsi_email=collision.id,
              skorsi_odosielatel=collision.sender, skorsi_verdikt=collision.verdict, rovnaky_odosielatel=same_sender)
    return email


def _replace_quarantined(session: Session, email: Email, pm: Any, fields: dict, errors: list[str]) -> Email:
    """Overená kópia s rovnakým Message-ID nahradí kópiu v karanténe (napr. z distribučného zoznamu).

    Inak by kópia v karanténe (alebo podvrh s uhádnutým Message-ID) navždy „zhltla“ pravý e-mail.
    """
    old = {"sender": email.sender, "verdict_reason": email.verdict_reason, "raw_path": email.raw_path,
           "received_at": email.received_at.isoformat() if email.received_at else None}
    for k, v in fields.items():
        setattr(email, k, v)
    email.error = "\n".join(errors + [f"Nahradil kópiu z karantény ({old['verdict_reason']}; originál "
                                      f"{old['raw_path']})."])
    email.attachments.clear()
    for a in pm.attachments:
        email.attachments.append(Attachment(filename=header_text(a.filename)[:300],
                                            content_type=header_text(a.content_type)[:120], size=a.size, text=a.text))
    session.flush()
    audit(session, "agent", "email_nahradeny_overenou_kopiou", "Email", email.id, message_id=email.message_id,
          verdict=email.verdict, dovod=email.verdict_reason, povodna_kopia=old)
    return email


# ====================================================================== extrakcia
def process_email(session: Session, email: Email, *, reprocess: bool = False) -> list[ExtractedEvent]:
    """Vytiahne z e-mailu udalosti, overí ich a uloží ako návrhy (``status = navrh``).

    Opakované volanie vráti už existujúce udalosti (``reprocess=True`` nespracované návrhy vytvorí znova).
    Chyby v e-maile zapíše do ``email.status = "chyba"`` a ``email.error`` – výnimku nevyhodí.
    Výluka, ktorá čaká na potvrdenie, dostane hneď záznam „navrh“ (plánovač s ňou počíta).
    """
    if email.verdict == KARANTENA:
        return []
    existing = list(session.scalars(select(ExtractedEvent).where(ExtractedEvent.email_id == email.id)
                                    .order_by(ExtractedEvent.id)))
    if existing and not reprocess:
        return existing
    if reprocess:
        for ev in existing:
            if ev.status == "navrh":
                _drop_pending(session, ev, "", delete_own=True)
                for c in session.scalars(select(Closure).where(Closure.source_event_id == ev.id)):
                    c.source_event_id = None          # napr. ručne zrušený návrh – záznam ostane, odkaz nie
                session.delete(ev)
        session.flush()
    now = now_utc()
    try:
        doc = MailDocument(subject=email.subject or "", body=email.body_text or "",
                           attachments=[(a.filename, a.text) for a in email.attachments if a.text],
                           sender=email.sender or "", received_at=email.received_at,
                           auto_reason=_auto_reason(email))
        result = extract(doc, use_llm=email.verdict == OK)
    except Exception as exc:  # zvláštny e-mail nesmie zastaviť agenta
        email.status = "chyba"
        email.error = ((email.error + "\n") if email.error else "") + \
            f"Extrakcia zlyhala: {type(exc).__name__}: {_short(exc)}"
        session.flush()
        return []
    # priradenie k výlukam v registri a záznamy „navrh“ idú po jednom s potvrdeniami dispečera (súbežné rozdelenie
    # výluky by inak ostalo nepovšimnuté – dočasný záznam zmeny by odkazoval na výluku, ktorá už je len jej časťou)
    bulk_note = _bulk_operational_note(email)
    lock_register(session)
    try:
        prepared: list[ExtractedEvent] = []
        for it in result.items:
            conf = it.confidence
            notes = list(it.notes)
            if email.verdict == PODOZRIVY:
                conf = min(conf, 0.5)
                notes.append("Podozrivý e-mail (možný pokus o prompt injection) – spracované len ako dáta, "
                             "istota znížená, vždy vyžaduje potvrdenie.")
            bulk = bool(bulk_note) and it.kind != "ine"
            if bulk and not any("prevádzkového dokumentu" in n for n in notes):
                notes.append(bulk_note)
            original = copy.deepcopy(it.data)
            # zmena, zrušenie či nový termín len s číslom ROV (bez úseku) sa z registra výluk nedopĺňa: ostáva udalosťou
            # bez miesta (poistka pre celý vozový park, upozornenie DÔLEŽITÉ) – výluky s týmto číslom ukáže fronta ako
            # návrh (:func:`register_candidates`) a úsek vyberie či opraví dispečer
            # predĺženie či skrátenie bez začiatku („sa predĺži do 29.11.“ – začiatok ostáva podľa registra): začiatok
            # podľa výluky, ktorej sa zmena týka (číslo a úsek) – inak by sa zmena nedala zapísať ani dočasne
            data_in = _register_start(session, it.kind, it.data, email, notes)
            vr = validate_event(session, it.kind, data_in, confidence=conf, verdict=email.verdict)
            extra = _source_reasons(email, it) + ([_BULK_REASON] if bulk else [])
            target_reasons, target_warnings = _register_check(session, it.kind, vr.data, now, email=email)
            orphan = _orphan_cancel(session, it.kind, vr.data, email) if it.kind == "vyluka_zrusenie" else ""
            if orphan:
                target_reasons.append(ORPHAN_CANCEL_REASON)
                target_warnings = [_orphan_warning(orphan)] + [w for w in target_warnings if w != orphan]
            reasons = list(vr.reasons) + [r for r in extra + target_reasons if r not in vr.reasons]
            validation = {**vr.as_dict(), "notes": notes, "field_confidence": it.field_confidence,
                          "evidence": it.evidence, "original_data": original, "extraction": result.meta,
                          "field_source": dict(it.field_source), "unverified_fields": list(it.unverified)}
            if orphan:
                validation[_ORPHAN_KEY] = {"why": orphan}
            validation["confirmation_reasons"] = reasons
            validation["warnings"] = list(vr.warnings) + [w for w in target_warnings if w not in vr.warnings]
            prepared.append(ExtractedEvent(
                email_id=email.id, kind=it.kind, data=vr.data, confidence=round(conf, 2), extractor=it.extractor,
                status="navrh", needs_confirmation=bool(reasons) or email.verdict != OK, validation=validation))
    except Exception as exc:  # zvláštny e-mail nesmie zastaviť agenta
        email.status = "chyba"
        email.error = ((email.error + "\n") if email.error else "") + \
            f"Extrakcia zlyhala: {type(exc).__name__}: {_short(exc)}"
        session.flush()
        return []
    for ev in prepared:
        session.add(ev)
    email.status = "spracovany"
    session.flush()
    for ev in prepared:
        if ev.needs_confirmation:
            _write_pending_safe(session, ev)
    audit(session, "agent", "email_extrakcia", "Email", email.id,
          udalosti=[{"id": e.id, "kind": e.kind, "istota": e.confidence, "na_potvrdenie": e.needs_confirmation}
                    for e in prepared], **result.meta)
    return prepared


def _auto_reason(email: Email) -> str:
    """E-mail nie je prevádzkovým oznámením podľa hlavičiek a príloh (RFC 3834 automatická odpoveď – neprítomnosť,
    potvrdenie doručenia či prečítania; pozvánka do kalendára text/calendar; hromadný informačný e-mail – newsletter,
    :func:`_bulk_reason`). Automaticky generované správy systémov (KIS – „Auto-Submitted: auto-generated“)
    prevádzkovými oznámeniami sú. „“ = nič také.

    Kalendárová príloha (.ics) s textom bez pozvánky (bez METHOD:REQUEST, organizátora či účastníkov) – napr. termín
    výluky na uloženie do kalendára pri ROV – sama nerozhoduje: posúdia ju pravidlá spolu s obsahom e-mailu
    (:func:`vlaky.posta.pravidla.non_operational`)."""
    from .odpovede import _raw_headers
    from .pravidla import _ICS_INVITE

    h = _raw_headers(email)
    if h is not None:
        auto = " ".join(str(h.get("Auto-Submitted") or "").split()).lower()
        if auto.startswith(("auto-replied", "auto-notified")):
            return f"automatická odpoveď (Auto-Submitted: {auto[:30]})"
        for name in ("X-Autoreply", "X-Autorespond", "X-Auto-Reply"):
            if h.get(name) is not None:
                return f"automatická odpoveď (hlavička {name})"
        if str(h.get("Precedence") or "").strip().lower() == "auto_reply":
            return "automatická odpoveď (Precedence: auto_reply)"
        ctype = str(h.get("Content-Type") or "").strip().lower()
        if ctype.startswith("multipart/report"):
            return "správa o doručení alebo prečítaní"
        if ctype.startswith("text/calendar"):
            return "pozvánka do kalendára (text/calendar)"
        bulk = _bulk_reason(email, h)
        if bulk:
            return bulk
    for a in email.attachments:
        if (str(a.content_type or "").lower().startswith("text/calendar") or
                str(a.filename or "").lower().endswith((".ics", ".calendar"))) and \
                (not (a.text or "").strip() or _ICS_INVITE.search(a.text or "")):
            return "pozvánka do kalendára (text/calendar)"
    return ""


# hromadná pošta: hlavičky distribučného zoznamu (RFC 2076 Precedence, RFC 2369 List-Unsubscribe) sú len slabým
# signálom – prevádzkové oznámenia OR (ROV, telegramy, mimoriadnosti) chodia aj cez distribučné zoznamy
_BULK_PRECEDENCE = ("bulk", "list", "junk")
# adresa odosielateľa newslettera, spravodaja či tlačového oddelenia (časť pred @, bez diakritiky)
_NEWSLETTER_TOKENS = ("newsletter", "spravodaj", "zpravodaj", "bulletin", "novinky", "marketing", "tlacov", "komunikac")
_NEWSLETTER_WORDS = {"news", "press", "media", "pr", "tlac"}
# adresa výlukového, dispečerského či prevádzkového pracoviska (vyluky@, dispecing@, kis@, potr@ …)
_OPERATIONAL_TOKENS = ("vyluk", "dispec", "potr", "telegram", "prevadz", "dopravn", "kontrol", "mimoriad")
_OPERATIONAL_WORDS = {"kis", "tlg", "rov", "srov", "kd"}


def _raw_header(h: Any, name: str) -> str:
    """Hodnota hlavičky z originálu (aj zakódovaná podľa RFC 2047) ako text."""
    from email.header import decode_header, make_header

    raw = h.get(name)
    if raw is None:
        return ""
    try:
        return header_text(str(make_header(decode_header(str(raw)))))
    except Exception:  # noqa: BLE001 – poškodená hlavička: surový text
        return header_text(str(raw))


def _sender_tokens(addr: str) -> list[str]:
    local_part = fold(addr.split("@")[0]) if "@" in addr else ""
    return [t for t in re.split(r"[^a-z0-9]+", local_part) if t]


def _operational_sender(addr: str) -> bool:
    """Adresa výlukového, dispečerského či prevádzkového pracoviska alebo OR („vyluky.orke@“, „kis@“, „…@ortt.…“)."""
    toks = _sender_tokens(addr)
    return bool(_sender_issuer(addr)) or any(t.startswith(_OPERATIONAL_TOKENS) or t in _OPERATIONAL_WORDS
                                             for t in toks)


def _newsletter_sender(addr: str) -> bool:
    return any(t.startswith(_NEWSLETTER_TOKENS) or t in _NEWSLETTER_WORDS for t in _sender_tokens(addr))


def _bulk_signals(email: Any, h: Any) -> tuple[list[str], list[str], str]:
    """Znaky hromadného e-mailu: (hlavičky distribučného zoznamu – Precedence: bulk/list/junk, List-Unsubscribe, List-Id;
    vzhľad newslettera – predmet bulletinu, spravodaja, newslettera či tlačovej správy, adresa typu newsletter@ alebo
    spravodaj@, ktorá nie je adresou výlukového, dispečerského či prevádzkového pracoviska; adresa odosielateľa)."""
    from .parser import is_forward_subject, is_reply_subject
    from .pravidla import _BULK_SUBJECT

    heads = []
    prec = " ".join(_raw_header(h, "Precedence").split()).lower()
    if prec in _BULK_PRECEDENCE:
        heads.append(f"Precedence: {prec}")
    if h.get("List-Unsubscribe") is not None:
        heads.append("List-Unsubscribe")
    if h.get("List-Id") is not None:
        heads.append("List-Id")
    addr = (parseaddr(_raw_header(h, "From"))[1] or str(getattr(email, "sender", "") or "")).strip().lower()
    subject = _raw_header(h, "Subject") or str(getattr(email, "subject", "") or "")
    looks = []
    if _BULK_SUBJECT.search(fold(subject)) and not is_reply_subject(subject) and not is_forward_subject(subject):
        looks.append("predmet")
    if _newsletter_sender(addr) and not _operational_sender(addr):
        looks.append(f"odosielateľ {addr}")
    return heads, looks, addr


def _bulk_reason(email: Any, h: Any) -> str:
    """Hromadný informačný e-mail (newsletter, spravodaj) podľa hlavičiek distribučného zoznamu – „“ = nie.

    Hlavička „Precedence: bulk/list/junk“ či „List-Unsubscribe“ je len slabý signál (prevádzkové oznámenia OR chodia
    aj cez distribučné zoznamy, mimoriadnosť často bez čísla ROV): newsletter je to až spolu s ostatnými signálmi –
    predmet newslettera, spravodaja, bulletinu či tlačovej správy A adresa odosielateľa typu newsletter@, spravodaj@ –
    a nikdy nie od výlukového, dispečerského či prevádzkového pracoviska alebo OR (vyluky.orke@, kis@ …), ani keď je
    takou adresou Reply-To, X-Original-From či Sender (distribučný zoznam prepísal odosielateľa).

    Hlavičky zoznamu, názov zoznamu (Bulletin, Spravodaj, Newsletter) ani prepísaný odosielateľ nikdy nepotlačia e-mail,
    ktorý obsahuje výluku s číslom prevádzkového dokumentu ŽSR (ROV, SROV, telegram, POTR) a s dátumom či úsekom
    (:func:`_operational_content`) – spracuje sa ako prevádzkové oznámenie s poznámkou „overte“ a dispečer ho potvrdí
    (stratená výluka by vlaku dala falošnú ZELENÚ). Pravidlá pre neznámych odosielateľov (karanténa) ostávajú."""
    heads, looks, addr = _bulk_signals(email, h)
    if not [x for x in heads if x != "List-Id"] or _operational_sender(addr):
        return ""
    for name in ("Reply-To", "X-Original-From", "Sender"):
        other = parseaddr(_raw_header(h, name))[1].strip().lower()
        if other and _operational_sender(other):
            return ""
    if len(looks) < 2 or _operational_content(email):
        return ""
    return "hromadný informačný e-mail – newsletter (" + ", ".join(heads + looks) + ")"


def _source_reasons(email: Email, item: Any) -> list[str]:
    """Dôvody na potvrdenie podľa pôvodu údajov: preposlaný obsah, odosielateľ nie je ŽSR, polia len z LLM."""
    reasons: list[str] = []
    if email.forwarded_from:
        who = ", ".join(x if x != "?" else "neznámeho odosielateľa" for x in email.forwarded_from.split(","))
        reasons.append(f"preposlaný e-mail (pôvodne od {who}) – obsah nemožno overiť u zdroja, vždy potvrdzuje "
                       "dispečer")
    if item.kind in ZSR_KINDS and not zsr_sender(email.sender or ""):
        reasons.append(f"{KIND_LABELS.get(item.kind, item.kind)} od odosielateľa {email.sender or '?'}, ktorý nie "
                       "je ŽSR – automaticky sa zapisuje len od ŽSR")
    if getattr(item, "unverified", None):
        reasons.append("kľúčové polia " + ", ".join(item.unverified) + " našiel len LLM – pravidlá ich neoverili")
    return reasons


_PLACE_FROM_REGISTER = ("vyluka_nova", "vyluka_zmena", "vyluka_zrusenie")


def register_candidates(session: Session, ev: ExtractedEvent, limit: int = 12) -> list[dict]:
    """Výluky v registri s číslom, ktoré uvádza udalosť bez úseku („STORNO – ROV 5330/2026“, „Výluka podľa ROV
    5310/2026 sa odkladá o týždeň …“) – len návrh pre dispečera vo fronte, nikdy automatické doplnenie úseku.

    Úsek sa z registra nedopĺňa: e-mail môže uvádzať iné miesto v tvare, ktorý pravidlá neprečítali (skratka, km poloha,
    citovaný či preposlaný text, príloha), číslo ROV má každá OR vlastné a výluka zadaná ručne nemá OR – úsek z registra
    by obmedzil iné vlaky a vlak v skutočnom mieste by dostal ZELENÚ. Udalosť preto ostáva udalosťou bez miesta (poistka
    pre celý vozový park a upozornenie DÔLEŽITÉ) a fronta ukáže výluky s týmto číslom (úsek, termín, OR, druh
    dokumentu) – dispečer jednu vyberie alebo úsek a termín opraví vo formulári potvrdenia.

    Vráti [{id, from_station, to_station, line_ref, track, kind, status, term, or, doc, flags, text}] – len čítanie
    registra (bez prehľadávania textu e-mailu); počíta sa pri zobrazení fronty, nie pod zámkom registra."""
    d = ev.data if isinstance(ev.data, dict) else {}
    ref = str(d.get("external_ref") or "").strip()
    if ev.kind not in _PLACE_FROM_REGISTER or not ref or d.get("from_station") or d.get("to_station"):
        return []
    fam = _event_family(ev.kind, d)
    email = session.get(Email, ev.email_id) if ev.email_id else None
    issuer = mail_issuer(email)
    mail_docs = _mail_doc_types(email, ref)
    out: list[dict] = []
    for c in _active_closures(session, ev.id):
        if not c.external_ref or not refs_match(c.external_ref, ref) or (fam is not None and _closure_family(c) != fam):
            continue
        theirs = _closure_issuer(session, c)
        doc = _closure_doc(session, c)
        flags = []
        if issuer and theirs and theirs != issuer:
            flags.append(f"výluka je od {_OR_LABELS[theirs]}, e-mail od {_OR_LABELS[issuer]} – číslo ROV má každá OR "
                         "vlastné")
        elif not theirs:
            flags.append("OR výluky sa nedá určiť (napr. výluka zadaná ručne)")
        elif not issuer:
            flags.append("OR e-mailu sa nedá určiť (napr. preposlaný e-mail)")
        if doc and mail_docs and doc not in mail_docs:
            named = ", ".join(sorted(_DOC_LABELS[x] for x in mail_docs))
            flags.append(f"e-mail uvádza {named}, výluka je podľa {_DOC_LABELS[doc]} – iný dokument")
        elif not doc:
            flags.append("druh dokumentu výluky (ROV, telegram …) sa nedá určiť")
        term = _term_desc(c.start_utc, c.end_utc, c.daily_from, c.daily_to)
        label = _OR_LABELS.get(theirs or "", "OR neznáma")
        doc_label = _DOC_LABELS.get(doc, "druh dokumentu neznámy")
        out.append({"id": c.id, "from_station": c.from_station, "to_station": c.to_station,
                    "line_ref": c.line_ref or "", "track": c.track or "", "kind": c.kind, "status": c.status,
                    "term": term, "or": label, "doc": doc_label, "flags": flags,
                    "text": f"#{c.id} {c.from_station} – {c.to_station}, {_restriction_text(c.kind, c.track)}, {term} "
                            f"({label}, {doc_label}{', nepotvrdená' if c.status == 'navrh' else ''})"})
        if len(out) >= limit:
            break
    return out


def candidate_edits(session: Session, ev: ExtractedEvent, closure_id: Any) -> dict:
    """Opravy dát udalosti podľa výluky z registra, ktorú dispečer vo fronte vybral (:func:`register_candidates`): úsek
    a číslo trate (ak ho udalosť nemá). Termín nie – register má pôvodný termín, nie termín zmeny či nový termín.
    Výluka, ktorá medzi návrhmi nie je (medzitým zrušená, iné číslo), nič nedoplní."""
    try:
        cid = int(str(closure_id).strip())
    except (TypeError, ValueError):
        return {}
    c = next((x for x in register_candidates(session, ev, limit=1000) if x["id"] == cid), None)
    if c is None:
        return {}
    d = ev.data if isinstance(ev.data, dict) else {}
    out = {"from_station": c["from_station"], "to_station": c["to_station"]}
    if c["line_ref"] and not d.get("line_ref") and "line_ref" in d:
        out["line_ref"] = c["line_ref"]
    return out


# zrušenie z e-mailu, ktoré v registri nemá čo zrušiť: môže ísť o nesprávne prečítané oznámenie novej výluky
# („od 23.11. 08:00 hod. až do odvolania úplná výluka …“) – poistka, nie zrušenie
ORPHAN_CANCEL_REASON = "zrušenie z e-mailu nemá v registri čo zrušiť – môže ísť o novú výluku, overte vo fronte"
_ORPHAN_KEY = "zrusenie_bez_ciela"           # kľúč validácie udalosti zrušenia, ktorá v registri nemá cieľ
ORPHAN_SOURCE = "email_poistka"              # Closure.source záznamu „navrh“ poistky zrušenia bez cieľa


def _orphan_cancel(session: Session, kind: str, d: dict, email: Optional[Email]) -> str:
    """Zrušenie výluky, ktoré nezodpovedá žiadnej výluke v registri (ani čakajúcej, ani dočasnému záznamu čakajúcej
    zmeny) – alebo zodpovedá len výluke v inom termíne (termíny sa neprekrývajú). Môže ísť o oznámenie novej výluky, ktoré
    pravidlá prečítali ako zrušenie: vráti vysvetlenie (prečo nemá cieľ), „“ = cieľ má (zrušenie je zrušením)."""
    if kind != "vyluka_zrusenie":
        return ""
    issuer = mail_issuer(email) if email is not None else None
    try:
        target, _how = _find_closure(session, None, kind, d, parse_utc(d.get("start")), loose=True, issuer=issuer)
    except ApplyError:
        target = None
    if target is not None:
        plan = _cancel_plan(session, target, d, None)
        if _other_term(plan):
            if _repeat_cancel(session, None, d, issuer) is not None:
                return ""                      # opakované zrušenie termínu, ktorý je už zrušený
            return (f"zodpovedá len výluke #{target.id} {target.from_station} – {target.to_station} v inom termíne "
                    f"({_term_desc(target.start_utc, target.end_utc, target.daily_from, target.daily_to)}).")
        return ""
    if _pending_change_matches(session, d):
        return ""
    if _repeat_cancel(session, None, d, issuer) is not None or _quoted_reply_only(email):
        # výluka (či rušený termín rozdelenej výluky) je už zrušená – znova poslané, preposlané zrušenie, zrušenie po
        # zrušení v registri výluk – alebo zrušenie je len v citácii odpovede („Ďakujeme za informáciu“ s citovaným
        # zrušením): nie nová výluka
        return ""
    why, _specific = _no_target(session, None, kind, d, issuer)
    # viac výluk s číslom (nejednoznačné priradenie) – cieľ má, len ho treba vybrať
    return "" if "jednoznačne" in why else why


def _other_term(plan: Optional["_Plan"]) -> bool:
    """Plán zrušenia odmietnutý preto, že sa termín zrušenia s výlukou neprekrýva (zrušenie iného termínu)."""
    return plan is not None and plan.kind == "odmietnut" and "neprekrývajú" in plan.text


def _already_cancelled(session: Session, ev_id: Optional[int], d: dict, issuer: Optional[str]) -> Optional[Closure]:
    """Výluka, ktorej sa zrušenie týka, je v registri už zrušená – znova poslané zrušenie, zrušenie preposlané
    kontrolným dispečerom, zrušenie po zrušení výluky dispečerom v registri výluk: rovnaké číslo a úsek (zrušenie bez
    úseku len číslo, a ak sa OR dá určiť, rovnaká OR), bez čísla rovnaký úsek; termín zrušenia sa s ňou prekrýva (bez
    termínu stačí číslo). Zrušenie s iným termínom (môže ísť o nesprávne prečítaný nový termín výluky s rovnakým
    číslom) opakovaným zrušením nie je – ostáva poistkou. None – taká zrušená výluka nie je."""
    a, b, ref = d.get("from_station"), d.get("to_station"), d.get("external_ref")
    start, end = parse_utc(d.get("start")), parse_utc(d.get("end"))
    if not ref and not (a and start is not None):
        return None
    fam = _event_family("vyluka_zrusenie", d)
    for c in sorted(_active_closures(session, ev_id, cancelled=True), key=lambda x: -(x.id or 0)):
        if ref and not (c.external_ref and refs_match(c.external_ref, ref)):
            continue
        if a and not _same_section(c, a, b):
            continue
        if fam is not None and _closure_family(c) != fam:
            continue
        if start is not None and not _overlaps(c, start, end if end is not None and end > start else None):
            continue
        if not a and issuer and _closure_issuer(session, c) not in (None, issuer):
            continue
        return c
    return None


def _cancelled_text(session: Session, c: Closure) -> str:
    """Upozornenie vo fronte pri opakovanom zrušení už zrušenej výluky."""
    who = ""
    ev = session.get(ExtractedEvent, c.source_event_id) if c.source_event_id else None
    if ev is not None and ev.kind == "vyluka_zrusenie" and ev.status == "aplikovane":
        who = f" (udalosťou #{ev.id})"
    return (f"Výluka #{c.id} {c.from_station} – {c.to_station} "
            f"({_term_desc(c.start_utc, c.end_utc, c.daily_from, c.daily_to)}) je v registri už zrušená{who} – "
            "zrušenie je opakované (znova poslané či preposlané), potvrdením sa register nezmení.")


@dataclass
class _Repeat:
    """Opakované zrušenie: termín, ktorý zrušenie ruší, je v registri už zrušený (celá výluka alebo jej časť)."""
    closure_id: int
    status: str
    text: str


def _repeat_cancel(session: Session, ev_id: Optional[int], d: dict, issuer: Optional[str]) -> Optional[_Repeat]:
    """Zrušenie opakuje skoršie zrušenie: výluka je v registri už zrušená (:func:`_already_cancelled`), alebo termín
    zrušenia už zrušilo skoršie zapísané zrušenie časti tej istej výluky (výluka je rozdelená – zrušený deň v registri
    nie je, ostali len ostatné dni). Znova poslané či preposlané zrušenie takého termínu nie je „zrušením bez cieľa“ –
    žiadna poistka (vlaky v zrušenom termíne by inak znova čakali), vo fronte len upozornenie a potvrdenie register
    nezmení. None – zrušenie nič neopakuje."""
    c = _already_cancelled(session, ev_id, d, issuer)
    if c is not None:
        return _Repeat(c.id, c.status, _cancelled_text(session, c))
    hit = _cancelled_part(session, ev_id, d, issuer)
    if hit is None:
        return None
    prev, c = hit
    start, end = parse_utc(d.get("start")), parse_utc(d.get("end"))
    return _Repeat(c.id, c.status, f"Termín {_cancel_term_text(start, end)} výluky #{c.id} {c.from_station} – "
                                   f"{c.to_station} je v registri už zrušený (udalosťou #{prev.id} – výluka je "
                                   "rozdelená, ostali len ostatné dni) – zrušenie je opakované (znova poslané či "
                                   "preposlané), potvrdením sa register nezmení.")


def _cancel_term_text(start: Optional[datetime], end: Optional[datetime]) -> str:
    """Termín zrušenia na čítanie („10.12.2026“, „10.12. 08:00 – 10.12. 14:00“, „od 25.11. 12:00“)."""
    if start is None:
        return "bez termínu"
    open_end = end is None or end <= start
    if _local_midnight(start) and (open_end or _local_midnight(end)):
        return _days_text(start, end if not open_end else start + timedelta(days=1), open_end)
    return f"od {fmt(start)}" if open_end else f"{fmt(start)} – {fmt(end)}"


def _cancelled_part(session: Session, ev_id: Optional[int], d: dict,
                    issuer: Optional[str]) -> Optional[tuple[ExtractedEvent, Closure]]:
    """Skoršie zapísané zrušenie časti výluky (udalosť „aplikovane“, ktorá výluku rozdelila), ktorého termín obsahuje
    termín zrušenia ``d``, a jeho výluka (časť rozdelenej výluky) s rovnakým číslom a úsekom (bez úseku číslo a OR, bez
    čísla úsek). Termín zrušenia musí ležať v zrušenom termíne – iný termín s rovnakým číslom opakovaním nie je."""
    a, b, ref = d.get("from_station"), d.get("to_station"), d.get("external_ref")
    start, end = parse_utc(d.get("start")), parse_utc(d.get("end"))
    if start is None or not (ref or a):
        return None
    last = end if end is not None and end > start else start
    fam = _event_family("vyluka_zrusenie", d)
    skip = _sibling_events(session, ev_id) | ({ev_id} if ev_id is not None else set())
    rows = session.scalars(select(ExtractedEvent).where(ExtractedEvent.kind == "vyluka_zrusenie",
                                                        ExtractedEvent.status == "aplikovane")
                           .order_by(ExtractedEvent.id.desc()).limit(500))
    for prev in rows:
        val = prev.validation if isinstance(prev.validation, dict) else {}
        applied = val.get("applied") if isinstance(val.get("applied"), dict) else {}
        if prev.id in skip or applied.get("action") != "rozdelene":
            continue
        p = prev.data if isinstance(prev.data, dict) else {}
        ps, pe = parse_utc(p.get("start")), parse_utc(p.get("end"))
        if ps is None or start < ps - MATCH_TOLERANCE:
            continue
        if pe is not None and pe > ps and last > pe + MATCH_TOLERANCE:
            continue
        ids = [applied.get("id")] + [t.get("id") for t in applied.get("terms") or [] if isinstance(t, dict)]
        for cid in dict.fromkeys(i for i in ids if isinstance(i, int)):
            c = session.get(Closure, cid)
            if c is None or (ref and not (c.external_ref and refs_match(c.external_ref, ref))):
                continue
            if (a and not _same_section(c, a, b)) or (fam is not None and _closure_family(c) != fam):
                continue
            if not a and issuer and _closure_issuer(session, c) not in (None, issuer):
                continue
            return prev, c
    return None


def _quoted_reply_only(email: Optional[Email]) -> bool:
    """Odpoveď („Re:“, „Odp:“), ktorej vlastný text (bez citácie) nič neruší ani neohlasuje – zrušenie je len v citovanej
    pôvodnej správe a v predmete (napr. „Ďakujeme za informáciu.“). Taká odpoveď nie je novým oznámením – pôvodná správa
    sa spracovala, keď prišla."""
    from .parser import is_reply_subject

    if email is None or not is_reply_subject(str(email.subject or "")) or getattr(email, "forwarded_from", None):
        return False
    own = fold(_unquoted(email.body_text or ""))
    if _CANCEL_WORDS.search(own) or _CLOSURE_VOCAB.search(own) or _CLOSURE_DOC.search(own) or _DATE_RE.search(own):
        return False
    return not any(str(getattr(a, "text", "") or "").strip() for a in getattr(email, "attachments", None) or [])


def _pending_change_matches(session: Session, d: dict) -> bool:
    """Zrušenie sa týka nového termínu čakajúcej zmeny výluky (jej dočasného záznamu „navrh“) – úsek či číslo a termín."""
    a, b, ref = d.get("from_station"), d.get("to_station"), d.get("external_ref")
    if not (a or ref):
        return False
    start, end = parse_utc(d.get("start")), parse_utc(d.get("end"))
    for t in session.scalars(select(Closure).where(Closure.proposed_change_of.is_not(None),
                                                   Closure.status != "zrusene")):
        if a and not _same_section(t, a, b):
            continue
        if not a and not (t.external_ref and refs_match(t.external_ref, ref)):
            continue
        if start is None or _overlaps(t, start, end):
            return True
    return False


def _orphan(ev: ExtractedEvent) -> str:
    """Vysvetlenie poistky zrušenia bez cieľa („“ – udalosť ju nemá)."""
    if ev.kind != "vyluka_zrusenie" or not isinstance(ev.validation, dict):
        return ""
    mark = ev.validation.get(_ORPHAN_KEY)
    return str(mark.get("why") or ORPHAN_CANCEL_REASON) if isinstance(mark, dict) else ""


def _orphan_warning(why: str) -> str:
    """Upozornenie vo fronte pri zrušení bez cieľa (poistka – nikdy sa nezapíše automaticky)."""
    return (f"{ORPHAN_CANCEL_REASON[:1].upper()}{ORPHAN_CANCEL_REASON[1:]}: {why} Kým ho dispečer nevybaví, plánovač "
            "s ním počíta ako s nepotvrdenou výlukou – vlaky v úseku a čase, ktoré zrušenie uvádza (bez úseku "
            "ktorýkoľvek vlak), nedostanú ZELENÚ. Ak e-mail ohlasuje novú výluku, zmeňte druh udalosti na novú výluku "
            "(Upraviť a potvrdiť); ak naozaj ruší výluku, ktorá v registri nie je, udalosť zamietnite.")


# hromadná pošta s prevádzkovým obsahom (výluka, číslo dokumentu ŽSR a dátum či úsek): spracuje sa, dispečer ju overí
_BULK_REASON = "e-mail prišiel cez distribučný zoznam alebo vyzerá ako hromadný – overte, že ide o prevádzkové oznámenie"
_CLOSURE_VOCAB = re.compile(r"\b(?:vyluk\w*|uzavret\w*|uzavier\w*|uzavre\w*|uzatvor\w*|neprejazdn\w*|"
                            r"vypnut\w*\s+(?:\w+\s+)?(?:trakcn|tv\b)\w*|pomal\w*\s+jazd\w*|prerusen\w*\s+(?:\w+\s+)?"
                            r"(?:prevadzk|doprav|jazd)\w*)")
_PLACE_SAID = re.compile(r"\b(?:usek\w*|zst\b|stanic\w*|medzi\b|dopravn\w*|trat(?!ov)\w*)")


def _operational_content(email: Any) -> bool:
    """E-mail obsahuje prevádzkové oznámenie: slovník výluk spolu s číslom prevádzkového dokumentu ŽSR (ROV, SROV,
    rozkaz o výluke, telegram, POTR) a s dátumom alebo úsekom – v predmete, tele či prílohe (okrem kalendára). Taký
    e-mail sa nikdy nepotlačí ako hromadný (stratená výluka by vlaku dala falošnú ZELENÚ)."""
    from .pravidla import _DOC_NUMBER, _calendar_attachment

    texts = [str(getattr(email, "subject", "") or ""), str(getattr(email, "body_text", "") or "")]
    texts += [str(getattr(a, "text", "") or "") for a in getattr(email, "attachments", None) or []
              if not _calendar_attachment(getattr(a, "filename", ""), getattr(a, "text", ""))]
    texts = [t for t in texts if t.strip()]
    whole = "\n".join(fold(t) for t in texts)
    if not _CLOSURE_VOCAB.search(whole) or not _DOC_NUMBER.search(whole):
        return False
    return bool(_DATE_RE.search(whole) or _PLACE_SAID.search(whole) or any(_SECTION_PAIR.search(t) for t in texts))


def _bulk_operational_note(email: Any) -> str:
    """Poznámka k e-mailu, ktorý má hlavičky distribučného zoznamu a vyzerá ako hromadný (predmet či adresa bulletinu,
    spravodaja, newslettera; odosielateľa prepísal zoznam), ale pre prevádzkový obsah sa spracuje ako prevádzkové
    oznámenie (:func:`_operational_content`) – „“ = nie je taký."""
    from .odpovede import _raw_headers

    try:
        h = _raw_headers(email)
        if h is None:
            return ""
        heads, looks, _addr = _bulk_signals(email, h)
    except Exception:  # noqa: BLE001 – len poznámka
        return ""
    if not heads or not looks or not _operational_content(email):
        return ""
    return ("E-mail prišiel cez distribučný zoznam a vyzerá ako hromadný (" + ", ".join(heads + looks) + "), ale "
            "obsahuje výluku s číslom prevádzkového dokumentu ŽSR (ROV, SROV, telegram, POTR) a termín či úsek – "
            "spracovaný ako prevádzkové oznámenie; overte ho (odosielateľa môže prepísať distribučný zoznam).")


def _register_check(session: Session, kind: str, data: dict, now: datetime,
                    email: Optional[Email] = None) -> tuple[list[str], list[str]]:
    """Čo by udalosť zmenila v registri: dôvody na potvrdenie (M5, 72 h) a informácie pre dispečera."""
    reasons: list[str] = []
    warnings: list[str] = []
    issuer = mail_issuer(email) if email is not None else None
    try:
        if kind in CLOSURE_EVENT_KINDS:
            fields, _notes = _closure_values(kind, data, now)
            target, how = _find_closure(session, None, kind, data, fields["start_utc"], loose=kind == "vyluka_zmena",
                                        issuer=issuer)
            if target is not None and kind == "vyluka_zmena":
                _inherit_track(fields, target)
            plan = _change_plan(session, target, fields, None, email, zamer=_intent(data)) \
                if target is not None and kind == "vyluka_zmena" else None
            if target is None and kind == "vyluka_zmena":
                why, specific = _no_target(session, None, kind, data, issuer)
                reasons.append("zmena sa nedá priradiť k výluke v registri (podľa čísla ani úseku a termínu) – "
                               "overte, ktorej výluky sa týka")
                if specific:
                    warnings.append(why)
            elif plan is not None:
                # zmena sa týka len časti výluky – nikdy sa nezapíše automaticky
                reasons.append(plan.text)
                warnings.append(f"{plan.text} (Priradená podľa: {how}.)")
            elif target is not None and _closure_changes(target, fields):
                reasons += _closure_guard(session, target, now, None)
                if kind != "vyluka_zmena":
                    # opakovaný ROV, týždenný prehľad s časťou výluky … – zmenu výluky potvrdzuje dispečer vždy
                    removal = _removal_reason(target, fields)
                    if removal:
                        reasons.append(removal)
                warnings.append(f"Mení výluku #{target.id} {target.from_station} – {target.to_station} "
                                f"{fmt(target.start_utc)} – {fmt(target.end_utc)} (priradená podľa: {how}).")
            elif target is not None and kind == "vyluka_zmena":
                # nový termín už výluka v registri má (napr. opakovane poslaná zmena po potvrdení) – čaká na dispečera
                # ako každá zmena výluky, fronta však povie, že register nezmení
                warnings.append(f"Výluka #{target.id} {target.from_station} – {target.to_station} už má nový termín "
                                f"({_term_desc(target.start_utc, target.end_utc, target.daily_from, target.daily_to)}) – "
                                "zmena register nezmení; potvrďte alebo zamietnite.")
            if kind != "vyluka_zmena" and data.get("external_ref"):
                for c in _active_closures(session):
                    if (target is not None and c.id == target.id) or not c.external_ref or \
                            not refs_match(c.external_ref, data["external_ref"]):
                        continue
                    if not _same_section(c, data.get("from_station"), data.get("to_station")):
                        warnings.append(f"Výluka s rovnakým číslom {data['external_ref']} je v registri na inom úseku "
                                        f"(#{c.id} {c.from_station} – {c.to_station}) – zapíše sa ako samostatná "
                                        "výluka (číslo môže patriť inej OR).")
                    elif target is None and _closure_family(c) == _event_family(kind, data) and \
                            _same_term(c, fields["start_utc"]) and not _same_restriction(c, kind, data):
                        # iné obmedzenie toho istého úseku a času (napr. vypnuté trakčné vedenie a výluka 1. TK,
                        # 1. TK a 2. TK oznámené zvlášť) – samostatná výluka, nie zmena prvej
                        warnings.append(f"Výluka s rovnakým číslom {data['external_ref']} a termínom je v registri "
                                        f"s iným obmedzením (#{c.id} {_restriction_text(c.kind, c.track)}) – zapíše sa "
                                        "ako samostatná výluka, platia obe obmedzenia.")
                    elif target is None and _closure_family(c) == _event_family(kind, data):
                        warnings.append(f"Výluka s rovnakým číslom {data['external_ref']} je v registri v inom termíne "
                                        f"(#{c.id} {fmt(c.start_utc)} – {fmt(c.end_utc)}) – zapíše sa ako ďalší termín "
                                        "(ROV môže mať viac termínov). Ak ide o presun termínu, pôvodný termín "
                                        "v registri zrušte alebo upravte.")
        elif kind == "vyluka_zrusenie":
            target, how = _find_closure(session, None, kind, data, parse_utc(data.get("start")), loose=True,
                                        issuer=issuer)
            gone = _repeat_cancel(session, None, data, issuer)
            if target is None:
                warnings.append(gone.text if gone is not None else _no_target(session, None, kind, data, issuer)[0])
                if gone is None and _quoted_reply_only(email):
                    warnings.append("Zrušenie je len v citovanej pôvodnej správe odpovede (vlastný text odpovede nič "
                                    "neruší ani neohlasuje) – nie je to nové oznámenie; ak sa týka výluky v registri, "
                                    "overte ju v registri výluk, inak udalosť zamietnite.")
            elif gone is not None and _other_term(_cancel_plan(session, target, data, None)):
                warnings.append(gone.text)
            else:
                stays = _stays_text(email, target, data) if email is not None else ""
                plan = _cancel_plan(session, target, data, None) if not stays else None
                limit = _cancel_restriction(email, target, data, whole=plan is None) \
                    if not stays and (plan is None or plan.kind != "odmietnut") else ""
                if not stays and not limit:
                    limit = _until_revoked_cancel(email, target)
                if stays:
                    warnings.append(stays)
                elif limit:
                    warnings.append(limit)
                elif plan is not None:
                    warnings.append(plan.text)
                else:
                    warnings.append(f"Zruší výluku #{target.id} {target.from_station} – {target.to_station} "
                                    f"{fmt(target.start_utc)} – {fmt(target.end_utc)} (priradená podľa: {how}).")
                theirs = _closure_issuer(session, target) if not data.get("from_station") and issuer is None else None
                if theirs:
                    # priradené len podľa čísla, OR e-mailu sa nedá určiť – číslo ROV má každá OR vlastné
                    warnings.append(f"Výluka #{target.id} je od {_OR_LABELS[theirs]}, e-mail OR neuvádza – overte, "
                                    "že ide o tú istú výluku (číslo ROV má každá OR vlastné).")
        elif kind == "trasa_zmena":
            cands = _find_paths(session, None, train_digits(data.get("train_number")), data.get("date"))
            if len(cands) == 1:
                reasons += _path_guard(session, cands[0], now, None)
        elif kind == "avizo":
            eta = parse_utc(data.get("eta"))
            day = data.get("date") or (local(eta).date().isoformat() if eta else None)
            train = find_train(session, data.get("train_number"), day, avizo=True)
            h = _train_handover(session, train.id) if train is not None else None
            if h is not None:
                reasons += _handover_guard(session, h, now, None)
    except ApplyError:
        pass
    return reasons, warnings


# ====================================================================== zápis do registrov
def apply_event(session: Session, event: ExtractedEvent, actor: str = "agent") -> dict:
    """Zapíše udalosť do registrov (výluky, trasy, preberania, objednávky).

    Nepotvrdená udalosť, ktorá potvrdenie potrebuje, sa nezapíše (vráti ``{"ok": False, ...}``).
    Ak automatický zápis nie je bezpečný (mení overený alebo blízky stav) alebo zlyhá, udalosť sa presunie
    do fronty na potvrdenie (``needs_confirmation``) a nepotvrdená výluka dostane záznam „navrh“.
    Opakované volanie je idempotentné (záznam sa nájde podľa ``source_event_id``).
    """
    lock_register(session)            # súbežné zápisy (dispečer, agent) idú po jednom a vidia výsledok druhého
    if event.status == "zamietnute":
        return {"ok": False, "error": "Udalosť bola zamietnutá – nedá sa aplikovať."}
    confirmed = event.confirmed_at is not None
    if event.needs_confirmation and not confirmed:
        return {"ok": False, "error": "Udalosť čaká na potvrdenie dispečerom."}
    email = session.get(Email, event.email_id) if event.email_id else None
    if email is not None and email.verdict == KARANTENA:
        return {"ok": False, "error": "E-mail je v karanténe – udalosť sa nesmie aplikovať."}
    if email is not None and email.verdict == PODOZRIVY and not confirmed:
        return {"ok": False, "error": "Podozrivý e-mail – udalosť možno aplikovať len po potvrdení dispečerom."}
    if email is not None and email.forwarded_from and not confirmed:
        return {"ok": False, "error": "Preposlaný e-mail – udalosť možno aplikovať len po potvrdení dispečerom."}
    blocking = (event.validation or {}).get("blocking") or []
    if blocking:
        return {"ok": False, "error": "Udalosť nemožno aplikovať: " + ", ".join(blocking) + "."}
    handlers = {"vyluka_nova": _apply_closure, "vyluka_zmena": _apply_closure, "pomala_jazda": _apply_closure,
                "mimoriadnost": _apply_closure, "vyluka_zrusenie": _apply_closure_cancel,
                "trasa_pridelena": _apply_path, "trasa_zmena": _apply_path, "trasa_zrusena": _apply_path_cancel,
                "avizo": _apply_avizo, "objednavka": _apply_order, "ine": _apply_nothing}
    handler = handlers.get(event.kind)
    if handler is None:
        return {"ok": False, "error": f"Neznámy druh udalosti {event.kind}."}
    try:
        result = handler(session, event, confirmed)
    except NeedsConfirmation as exc:
        _require_confirmation(session, event, exc.reasons)
        return {"ok": False, "error": "Automatický zápis nie je bezpečný: " + "; ".join(exc.reasons) + ".",
                "needs_confirmation": True}
    except ApplyError as exc:
        val = dict(event.validation or {})
        val["apply_error"] = str(exc)
        event.validation = val
        session.flush()
        if not confirmed:          # automatický zápis zlyhal → do fronty, aby sa udalosť nestratila
            _require_confirmation(session, event, [f"automatický zápis zlyhal: {exc}"])
        elif event.kind in CLOSURE_EVENT_KINDS + ("vyluka_zrusenie",):
            # odmietnutý zápis potvrdenej výluky (napr. zrušenie len časti výluky do odvolania) – v audite aj s dôvodom
            audit(session, actor, "vyluka_navrh_zlyhal", "ExtractedEvent", event.id, druh=event.kind,
                  popis=describe_event(event), chyba=_short(exc, 600))
        out = {"ok": False, "error": str(exc)}
        if not exc.retry:
            out["retry"] = False
        return out
    result["ok"] = True
    result["kind"] = event.kind
    val = dict(event.validation or {})
    val.pop("apply_error", None)
    val["applied"] = {**result, "at": now_utc().isoformat(), "by": actor, "confirmed": confirmed}
    event.validation = val
    event.status = "aplikovane"
    session.flush()
    if event.kind not in CLOSURE_EVENT_KINDS:
        _drop_pending(session, event, f"Udalosť #{event.id} zapísaná ako {KIND_LABELS.get(event.kind, event.kind)}.")
    audit(session, actor, "udalost_aplikovana", "ExtractedEvent", event.id, druh=event.kind,
          popis=describe_event(event),
          vysledok={k: v for k, v in result.items() if k not in ("before", "train_before")})
    return result


def _require_confirmation(session: Session, ev: ExtractedEvent, reasons: list[str]) -> None:
    val = dict(ev.validation or {})
    rs = list(val.get("confirmation_reasons") or [])
    rs += [r for r in reasons if r not in rs]
    val["confirmation_reasons"] = rs
    ev.validation = val
    ev.needs_confirmation = True
    session.flush()
    _write_pending_safe(session, ev)


def _apply_nothing(session: Session, ev: ExtractedEvent, confirmed: bool) -> dict:
    return {"entity": "", "id": None, "action": "nic"}


def _content_writer(session: Session, c: Closure) -> Optional[int]:
    """Udalosť, ktorá zapísala termín a obmedzenie záznamu výluky. Nedotknutú časť výluky, ktorú udalosť len
    rozdelila (zmenou či zrušením iného dňa), zapísal pôvodný zapisovateľ výluky – nie udalosť, ktorá ju rozdelila;
    tá zapísala len nový termín a skrátené časti."""
    oid, seen = c.source_event_id, set()
    while oid and oid not in seen:
        seen.add(oid)
        writer = session.get(ExtractedEvent, oid)
        applied = (writer.validation or {}).get("applied") if writer is not None and \
            isinstance(writer.validation, dict) else None
        if not (writer is not None and writer.status == "aplikovane" and isinstance(applied, dict)
                and applied.get("action") == "rozdelene"
                and any(isinstance(t, dict) and t.get("id") == c.id and t.get("part") == "povodna"
                        for t in applied.get("terms") or [])):
            return oid
        before = applied.get("before") if isinstance(applied.get("before"), dict) else {}
        oid = before.get("source_event_id")
    return oid


def _newer_owner(session: Session, obj: Any, ev: ExtractedEvent) -> Optional[ExtractedEvent]:
    """Novšia zapísaná udalosť, ktorej záznam už patrí (staršia ho nesmie prepísať). Pri časti rozdelenej výluky
    rozhoduje, kto zapísal jej termín (:func:`_content_writer`) – novšia zmena iného dňa staršiu zmenu tohto dňa
    nenahrádza."""
    oid = _content_writer(session, obj) if isinstance(obj, Closure) else getattr(obj, "source_event_id", None)
    if not oid or oid == ev.id or (ev.id is not None and oid < ev.id):
        return None
    owner = session.get(ExtractedEvent, oid)
    return owner if owner is not None and owner.status == "aplikovane" else None


def _pending_owner(session: Session, obj: Any, ev_id: Optional[int]) -> Optional[ExtractedEvent]:
    """Iná udalosť, ktorá záznam vlastní a ešte čaká na potvrdenie dispečerom."""
    oid = getattr(obj, "source_event_id", None)
    if not oid or oid == ev_id:
        return None
    owner = session.get(ExtractedEvent, oid)
    return owner if owner is not None and owner.status == "navrh" and owner.needs_confirmation else None


def _superseded(entity: str, obj: Any, how: str, owner: ExtractedEvent, **extra: Any) -> dict:
    return {"entity": entity, "id": obj.id, "action": "nahradene", "status": getattr(obj, "status", None),
            "matched_by": how, "superseded_by": owner.id, "before": None,
            "note": f"Záznam medzitým zmenila novšia udalosť #{owner.id} – údaje tejto staršej udalosti sa "
                    "nezapísali.", **extra}


# ---------------------------------------------------------------------- výluky
_CLOSURE_FIELDS = ("kind", "from_station", "to_station", "line_ref", "track", "start_utc", "end_utc", "daily_from",
                   "daily_to", "electric_only", "speed_kmh", "status", "external_ref", "note", "version",
                   "source_event_id", "until_revoked", "proposed_change_of")
_DOC_TYPES = (("suborny rov", "srov"), ("srov", "srov"), ("rozkaz o vyluke", "rov"), ("rov", "rov"),
              ("telegram", "tlg"), ("tlg", "tlg"), ("potr", "potr"))


def _ref_parts(ref: Any) -> tuple[str, str]:
    """(druh dokumentu, číslo): „ROV č. 1142/2026“ → („rov“, „1142/2026“), „1142/2026“ → („“, „1142/2026“)."""
    f = " ".join(fold(str(ref or "")).replace("(", " ").replace(")", " ").split())
    doc = ""
    for prefix, code in _DOC_TYPES:
        if f.startswith(prefix) and (len(f) == len(prefix) or not f[len(prefix)].isalpha()):
            doc, f = code, f[len(prefix):]
            break
    f = f.strip(" .:-")
    for word in ("cislo", "c."):
        if f.startswith(word):
            f = f[len(word):].strip(" .:-")
            break
    num = "".join(ch if ch.isalnum() else ("/" if ch in "/-." else "") for ch in f.replace(" ", ""))
    return doc, num.strip("/")


def ref_key(ref: Any) -> str:
    """Porovnávací kľúč čísla výluky/trasy („ROV č. 1142/2026“ = „1142/2026“ = „č. 1142/2026“)."""
    return _ref_parts(ref)[1]


def refs_match(a: Any, b: Any) -> bool:
    """Rovnaké číslo a nie rôzny druh dokumentu („TLG 15/2026“ ≠ „ROV 15/2026“, „ROV 15/2026“ = „15/2026“)."""
    da, na = _ref_parts(a)
    db, nb = _ref_parts(b)
    if not na or na != nb:
        return False
    return not da or not db or da == db


def _station_key(name: Any) -> str:
    m = stations().match(str(name or "")) if name else None
    return fold(m.name) if m else " ".join(fold(str(name or "")).split())


def _same_section(c: Closure, a: Any, b: Any) -> bool:
    if not a:
        return False
    return {_station_key(c.from_station), _station_key(c.to_station)} == {_station_key(a), _station_key(b or a)}


def _closure_family(c: Closure) -> str:
    return c.kind if c.kind in ("pomala_jazda", "mimoriadnost") else "vyluka"


def _event_family(kind: str, d: dict) -> Optional[str]:
    if kind == "vyluka_zrusenie":
        # zrušenie pomalej jazdy (POTR) ruší len pomalú jazdu – nie výluku s rovnakým (holým) číslom inej OR
        return "pomala_jazda" if d.get("kind") == "pomala_jazda" else None
    if kind == "mimoriadnost":
        return "mimoriadnost"
    if kind == "pomala_jazda" or d.get("kind") == "pomala_jazda":
        return "pomala_jazda"
    return "vyluka"


def _track_ok(c: Closure, track: Any) -> bool:
    a, b = str(c.track or "").strip(), str(track or "").strip()
    return not a or not b or a == b


def _restriction_of(kind: str, d: dict) -> str:
    """Druh obmedzenia, ktorý by udalosť zapísala do registra (ako :func:`_closure_values`)."""
    if kind == "mimoriadnost":
        return "mimoriadnost"
    if kind == "pomala_jazda":
        return "pomala_jazda"
    return str(d.get("kind") or "uplna")[:30]


def _same_restriction(c: Closure, kind: str, d: dict) -> bool:
    """To isté obmedzenie: rovnaký druh (úplná výluka, výluka koľaje, vypnuté trakčné vedenie …) a koľaj (neuvedená
    koľaj sa zhoduje s každou). Vypnuté trakčné vedenie a výluka 1. TK (či 1. TK a 2. TK) toho istého úseku a času sú
    dve obmedzenia – dve výluky v registri, nie zmena jednej druhou."""
    return (c.kind or "uplna") == _restriction_of(kind, d) and _track_ok(c, d.get("track"))


_RESTRICTION_TEXT = {"uplna": "úplná výluka", "kolaj": "výluka koľaje", "trakcne_vedenie": "vypnuté trakčné vedenie",
                     "obmedzenie": "obmedzenie", "pomala_jazda": "pomalá jazda", "mimoriadnost": "mimoriadnosť"}


def _restriction_text(kind: Any, track: Any = "") -> str:
    out = _RESTRICTION_TEXT.get(str(kind or "uplna"), str(kind or "uplna"))
    return out + (f", koľaj {track}" if str(track or "").strip() else "")


# ---------------------------------------------------------------------- oblastné riaditeľstvo (OR) ŽSR
# číslo ROV má každá OR vlastné – rovnaké číslo od inej OR nie je tá istá výluka
_OR_LABELS = {"BA": "OR Bratislava", "TT": "OR Trnava", "TN": "OR Trenčín", "ZA": "OR Žilina", "ZV": "OR Zvolen",
              "KE": "OR Košice"}
_OR_STEMS = {"bratislav": "BA", "trnav": "TT", "trencin": "TN", "zilin": "ZA", "zvolen": "ZV", "kosic": "KE"}
_OR_CODES = {"ba", "tt", "tn", "za", "zv", "ke"}
_OR_TOKEN = re.compile(r"or(ba|tt|tn|za|zv|ke|bratislav\w*|trnav\w*|trencin\w*|zilin\w*|zvolen\w*|kosic\w*)")
_OR_NAME = (r"(?:or|oblastne\s+riaditelstvo|oblastneho\s+riaditelstva|oblastnym\s+riaditelstvom|"
            r"oblastnom\s+riaditelstve)")
# „OR Košice“, „Oblastné riaditeľstvo Košice“; skratka „OR KE“, „OR-KE“, „ORKE“ (aj v podpise „ŽSR – ORKE“)
_OR_TEXT = re.compile(rf"\b{_OR_NAME}\s+(bratislav|trnav|trencin|zilin|zvolen|kosic)\w*"
                      rf"|\b{_OR_NAME}\s*[-.]?\s*(ba|tt|tn|za|zv|ke)\b")


def _or_code(token: str) -> Optional[str]:
    """Kód OR z kúska adresy či názvu: „orke“, „orkosice“, „ke“ → KE (None – nie je to OR)."""
    m = _OR_TOKEN.fullmatch(token)
    token = m.group(1) if m else token
    if token in _OR_CODES:
        return token.upper()
    return next((code for stem, code in _OR_STEMS.items() if token.startswith(stem)), None) if m else None
_QUOTE_LINE = re.compile(r"^\s*(?:-{2,}\s*(?:povodna|original|forwarded|preposlan)|_{5,}|(?:od|from)\s*:.*@)")


def _unquoted(body: str) -> str:
    """Vlastný text e-mailu bez citácie (riadky „>“, „-----Pôvodná správa-----“, „Od: … @“ a všetko za nimi)."""
    out = []
    for line in str(body or "").splitlines():
        if _QUOTE_LINE.match(fold(line)):
            break
        if not line.lstrip().startswith(">"):
            out.append(line)
    return "\n".join(out)


def _quoted(body: str) -> str:
    """Citovaný či preposlaný text e-mailu (riadky „>“ a všetko od „-----Pôvodná správa-----“, „Od: … @“) – doplnok
    :func:`_unquoted`."""
    out, started = [], False
    for line in str(body or "").splitlines():
        started = started or bool(_QUOTE_LINE.match(fold(line)))
        if started or line.lstrip().startswith(">"):
            out.append(line)
    return "\n".join(out)


def _sender_issuer(addr: str) -> Optional[str]:
    """OR podľa adresy odosielateľa: „vyluky.orke@“, „or.kosice@“, „…@ortt.…“; pri adrese ŽSR aj samotný kód OR
    („ke.vyluky@zsr…“, „…@ke.zsr…“). Viac rôznych OR v adrese – None."""
    if addr.count("@") != 1:
        return None
    local_part, domain = addr.split("@")
    toks = [t for t in re.split(r"[^a-z0-9]+", local_part) if t]
    labels = [t for t in domain.split(".")[:-1] if t]          # bez domény najvyššej úrovne („.ke“ je Keňa)
    found = {code for t in toks + labels if _OR_TOKEN.fullmatch(t) and (code := _or_code(t))}
    found |= {code for a, b in zip(toks, toks[1:]) if a == "or" and (code := _or_code(b) or _or_code("or" + b))}
    if not found and zsr_sender(addr) and (len(toks) > 1 or labels):
        # samotný kód OR v adrese ŽSR (nie meno osoby – tá má aspoň dve písmená mena a priezviska)
        found = {t.upper() for t in toks + labels if t in _OR_CODES} if len(toks) > 1 else \
            {t.upper() for t in labels if t in _OR_CODES}
    return found.pop() if len(found) == 1 else None


def _text_issuers(text: str) -> set:
    """OR uvedené v texte („OR Košice“, „Oblastné riaditeľstvo Košice“, „OR KE“, „ORKE“ v podpise)."""
    return {_OR_STEMS[m.group(1)] if m.group(1) else m.group(2).upper() for m in _OR_TEXT.finditer(fold(text))}


def mail_issuer(email: Any) -> Optional[str]:
    """Oblastné riaditeľstvo ŽSR, ktoré e-mail vydalo (kód BA, TT, TN, ZA, ZV, KE), ak sa dá určiť: podľa adresy
    odosielateľa (vyluky.orke@…, ke.vyluky@zsr…, …@ortt.…), inak podľa predmetu a vlastného textu e-mailu („OR Košice“,
    „OR KE“, „ORKE“ v podpise). Viac rôznych OR – nedá sa určiť (None).

    Preposlaný e-mail (``forwarded_from`` – preposlaná príloha message/rfc822 či vložené preposlanie) vydala OR
    preposlaného obsahu – adresa pôvodného odosielateľa, inak OR v preposlanom texte (podpis pôvodnej OR) –, nie OR
    preposielateľa: kontrolný dispečer OR Žilina (kd.orza@…) prepošle aj oznámenie OR Košice, a číslo ROV má každá OR
    vlastné (zrušenie ROV 15/2026 OR Košice nesmie zrušiť ROV 15/2026 OR Žilina – vlak by dostal ZELENÚ cez platnú
    výluku). Keď sa OR preposlaného obsahu určiť nedá, OR preposielateľa sa nepoužije (None – fronta upozorní, že OR
    e-mailu sa nedá určiť); OR z vlastného textu preposielateľa platí, len keď nič nepreposlal."""
    if email is None:
        return None
    code = _sender_issuer(str(getattr(email, "sender", "") or "").strip().lower())
    body = str(getattr(email, "body_text", "") or "")
    if getattr(email, "forwarded_from", None):
        forwarded = [x.strip().lower() for x in str(getattr(email, "forwarded_from", "") or "").split(",")
                     if "@" in x]
        found = {c for c in (_sender_issuer(a) for a in forwarded) if c}
        if not found:
            found = _text_issuers(_quoted(body))   # podpis pôvodnej OR v preposlanom texte
        if len(found) == 1:
            return found.pop()
        if found:
            return None
        # OR preposlaného obsahu sa určiť nedá: preposielateľ s adresou OR mohol preposlať oznámenie inej OR
        return None if code else _single(_text_issuers(str(getattr(email, "subject", "") or "")))
    if code:
        return code
    for text in (str(getattr(email, "subject", "") or ""), _unquoted(body)):
        found = _text_issuers(text)
        if len(found) == 1:
            return found.pop()
        if found:
            return None
    return None


def _single(codes: set) -> Optional[str]:
    return next(iter(codes)) if len(codes) == 1 else None


def _event_issuer(session: Session, ev: Optional[ExtractedEvent]) -> Optional[str]:
    if ev is None or not ev.email_id:
        return None
    return mail_issuer(session.get(Email, ev.email_id))


def _closure_emails(session: Session, c: Closure) -> list[int]:
    """E-maily, ktoré výluku v registri zapísali: e-mail udalosti, ktorá ju naposledy zapísala, a e-maily z poznámky
    („Zdroj: e-mail #N“) – bez opakovania."""
    ids: list[int] = []
    if c.source_event_id:
        ev = session.get(ExtractedEvent, c.source_event_id)
        if ev is not None and ev.email_id:
            ids.append(ev.email_id)
    ids += [int(x) for x in re.findall(r"Zdroj: e-mail #(\d+)", c.note or "")]
    return list(dict.fromkeys(ids))


def _closure_issuer(session: Session, c: Closure) -> Optional[str]:
    """OR, ktorej výluka v registri je (podľa e-mailu, ktorý ju zapísal, alebo e-mailu v poznámke „Zdroj: e-mail #N“);
    None – nedá sa určiť (napr. výluka zadaná ručne)."""
    for email_id in _closure_emails(session, c):
        code = mail_issuer(session.get(Email, email_id))
        if code:
            return code
    return None


# druh dokumentu pred číslom („ROV č. 5310/2026“, „Telegram č. 5310/2026“, „TLG 5310/2026“, „POTR 12/2026“) – v texte
# bez diakritiky, koniec úseku tesne pred číslom
_DOC_TAIL = re.compile(r"\b(suborn\w*\s+rov\w*|s?rov|rozkaz\w*\s+o\s+vyluk\w*|tlg|telegram\w*|potr)"
                       r"\s*[(\[]?\s*(?:c\.|cis\.|cislo|c)?\s*[:.\-–]?\s*$")
_DOC_LABELS = {"rov": "ROV", "srov": "SROV", "tlg": "TLG", "potr": "POTR"}


def _doc_types(text: str, num: str) -> set:
    """Druhy dokumentov (rov, srov, tlg, potr), ktoré text uvádza tesne pred číslom ``num``."""
    out: set = set()
    if not num or not text:
        return out
    f = fold(text)
    for m in re.finditer(rf"(?<![\d/]){re.escape(num)}(?![\d/])", f):
        tail = _DOC_TAIL.search(f, max(0, m.start() - 40), m.start())
        if tail is None:
            continue
        word = tail.group(1)
        out.add("srov" if word.startswith(("suborn", "srov")) else "tlg" if word.startswith(("tlg", "telegram"))
                else "potr" if word == "potr" else "rov")
    return out


def _mail_doc_types(email: Any, ref: Any) -> set:
    """Druhy dokumentov, ktoré e-mail (predmet a vlastný text) uvádza pri čísle ``ref``; prázdna množina – neuvádza."""
    doc, num = _ref_parts(ref)
    if doc:
        return {doc}
    if email is None:
        return set()
    return _doc_types(str(getattr(email, "subject", "") or ""), num) | \
        _doc_types(_unquoted(getattr(email, "body_text", "") or ""), num)


def _closure_doc(session: Session, c: Closure) -> str:
    """Druh dokumentu výluky v registri (rov, srov, tlg, potr): z čísla („ROV č. 1142/2026“), inak z e-mailov, ktoré
    ju zapísali; „“ – nedá sa určiť (výluka zadaná ručne len s číslom, e-mail druh pri čísle neuvádza či ich je viac)."""
    doc, num = _ref_parts(c.external_ref)
    if doc or not num:
        return doc
    found: set = set()
    for email_id in _closure_emails(session, c):
        e = session.get(Email, email_id)
        if e is not None:
            found |= _mail_doc_types(e, num)
    return found.pop() if len(found) == 1 else ""


def _sibling_events(session: Session, ev_id: Optional[int]) -> set[int]:
    """Ostatné udalosti toho istého e-mailu – nikdy nie sú zmenou ani zrušením výluky, ktorú zapísala sesterská
    udalosť (ROV s dvoma termínmi dá dve udalosti s rovnakým číslom; potvrdenie druhej nesmie presunúť prvú)."""
    if ev_id is None:
        return set()
    ev = session.get(ExtractedEvent, ev_id)
    if ev is None or ev.email_id is None:
        return set()
    return set(session.scalars(select(ExtractedEvent.id).where(ExtractedEvent.email_id == ev.email_id,
                                                               ExtractedEvent.id != ev_id)))


def _active_closures(session: Session, exclude_event: Optional[int] = None, cancelled: bool = False) -> list:
    """Platné záznamy výluk bez dočasných záznamov navrhovaných zmien (``cancelled`` = len zrušené); bez záznamov
    udalosti ``exclude_event`` a jej sesterských udalostí z toho istého e-mailu. Záznam poistky zrušenia bez cieľa
    (``ORPHAN_SOURCE``) výlukou v registri nie je – iné udalosti ho nemenia ani nerušia."""
    q = select(Closure).where(Closure.proposed_change_of.is_(None),
                              or_(Closure.source.is_(None), Closure.source != ORPHAN_SOURCE))
    q = q.where(Closure.status == "zrusene") if cancelled else q.where(Closure.status != "zrusene")
    skip = _sibling_events(session, exclude_event) | ({exclude_event} if exclude_event is not None else set())
    return [c for c in session.scalars(q) if c.source_event_id is None or c.source_event_id not in skip]


def _newer_writer(session: Session, c: Closure, ev_id: Optional[int]) -> bool:
    """Záznam naposledy zapísala novšia udalosť ako ``ev_id`` (napr. zmena termínu staršieho oznámenia); pri časti
    rozdelenej výluky tá, ktorá zapísala jej termín (:func:`_content_writer`)."""
    if ev_id is None or c.source_event_id is None or c.source_event_id <= ev_id:
        return False
    writer = _content_writer(session, c)
    return writer is not None and writer > ev_id


def _same_term(c: Closure, start: Optional[datetime]) -> bool:
    """Rovnaký termín: začiatok ± ``MATCH_TOLERANCE``."""
    return start is not None and c.start_utc is not None and abs(_utc(c.start_utc) - start) <= MATCH_TOLERANCE


def _overlaps(c: Closure, start: Optional[datetime], end: Optional[datetime]) -> bool:
    """Výluka ``c`` sa prekrýva s termínom ``start`` – ``end`` (bez konca: obsahuje ``start``)."""
    if start is None or c.start_utc is None or c.end_utc is None:
        return False
    cs, ce = _utc(c.start_utc), _utc(c.end_utc)
    if end is None:
        return cs - MATCH_TOLERANCE <= start <= ce
    return cs < end and ce > start


def _find_closure(session: Session, ev_id: Optional[int], kind: str, d: dict, start: Optional[datetime],
                  loose: bool = False, cancelled: bool = False, skip_own: bool = False,
                  issuer: Optional[str] = None) -> tuple[Optional[Closure], str]:
    """Záznam výluky, ktorého sa udalosť týka.

    Podľa čísla len pri rovnakom úseku (číslo ROV môže mať aj iná OR) a rovnakom druhu (výluka / pomalá jazda
    / mimoriadnosť); bez čísla podľa úseku a začiatku (± 1 h) s rovnakou koľajou; zmena a zrušenie aj podľa
    úseku a blízkeho termínu (± 3 dni). Viac kandidátov = nejednoznačné → nič. ``skip_own`` – vlastné
    záznamy udalosti (napr. jej návrh „navrh“) sa nevracajú, hľadá sa len výluka v registri.

    ROV môže mať viac termínov s rovnakým číslom: nová výluka, pomalá jazda či mimoriadnosť je tá istá výluka len
    s rovnakým termínom (začiatok ± 1 h) a rovnakým obmedzením (druh a koľaj – vypnuté trakčné vedenie a výluka
    1. TK toho istého času sú dve výluky); iný termín s rovnakým číslom je ďalší termín, nie presun. Zmena a
    zrušenie s termínom patria termínu (aj časti rozdelenej výluky), s ktorým sa prekrývajú. Len podľa čísla (bez
    úseku) sa výluka priradí, len ak je od tej istej OR ako e-mail (``issuer``), keď sa OR oboch dá určiť. Záznamy
    sesterských udalostí z toho istého e-mailu sa nehľadajú.
    """
    if ev_id is not None and not cancelled and not skip_own:
        own = session.scalar(select(Closure).where(Closure.source_event_id == ev_id, Closure.status != "zrusene",
                                                   Closure.proposed_change_of.is_(None)))
        if own is not None:
            return own, "rovnaká udalosť"
    active = _active_closures(session, ev_id, cancelled)
    fam = _event_family(kind, d)
    a, b, ref, track = d.get("from_station"), d.get("to_station"), d.get("external_ref"), d.get("track")
    change = kind in ("vyluka_zmena", "vyluka_zrusenie")

    def fits(c: Closure) -> bool:
        return fam is None or _closure_family(c) == fam

    def ref_ok(c: Closure) -> bool:
        return not ref or not c.external_ref or refs_match(c.external_ref, ref)

    if ref:
        hits = [c for c in active if c.external_ref and refs_match(c.external_ref, ref) and fits(c)
                and (not a or _same_section(c, a, b))]
        if not a and issuer:
            # len podľa čísla: výluka inej OR s rovnakým číslom nie je tá istá výluka
            hits = [c for c in hits if _closure_issuer(session, c) in (None, issuer)]
        if not change:
            # iný termín či iné obmedzenie s rovnakým číslom je ďalšia výluka – okrem záznamu, ktorý medzitým zapísala
            # novšia udalosť (staršie oznámenie toho istého ROV je ňou nahradené)
            hits = [c for c in hits if (_same_term(c, start) and _same_restriction(c, kind, d))
                    or _newer_writer(session, c, ev_id)]
        elif len(hits) > 1 and start is not None:
            near = [c for c in hits if _overlaps(c, start, parse_utc(d.get("end")))]
            if len(near) > 1 and kind == "vyluka_zmena":
                same = [c for c in near if _same_restriction(c, kind, d)]
                near = same if len(same) == 1 else near
            hits = near if len(near) == 1 else hits
        if len(hits) == 1:
            return hits[0], "číslo výluky a úsek" if a else "číslo výluky"
        if len(hits) > 1:
            return None, ""
    if a and start is not None:
        hits = [c for c in active if _same_section(c, a, b) and abs(_utc(c.start_utc) - start) <= MATCH_TOLERANCE
                and fits(c) and _track_ok(c, track) and ref_ok(c) and (change or _same_restriction(c, kind, d))]
        if len(hits) == 1:
            return hits[0], "úsek a začiatok"
    if loose and a:
        on_sec = [c for c in active if _same_section(c, a, b) and fits(c) and _track_ok(c, track) and ref_ok(c)]
        hits = on_sec
        if start is not None:
            hits = [c for c in hits if abs(_utc(c.start_utc) - start) <= CHANGE_MATCH_WINDOW]
        if len(hits) == 1:
            return hits[0], "úsek a blízky termín" if start is not None else "úsek"
        if kind == "vyluka_zrusenie" and start is not None:
            # zrušenie jedného dňa či časti dlhej výluky bez čísla („výluka v úseku … dňa 15.12. sa nekoná“ pri výluke
            # 01. – 20.12.): začiatok výluky je ďaleko, ale termín zrušenia leží v nej – jediná taká výluka v úseku je
            # cieľom (plán zrušenia zruší len uvedenú časť); inak by zrušenie bolo „bez cieľa“ (poistka s výzvou, že
            # môže ísť o novú výluku)
            end = parse_utc(d.get("end"))
            over = [c for c in on_sec if _overlaps(c, start, end if end is not None and end > start else None)]
            if len(over) == 1:
                return over[0], "úsek a termín v období výluky"
    return None, ""


def _utc(dt: datetime) -> datetime:
    from ..cas import ensure_utc

    return ensure_utc(dt)


# druhy obmedzenia v registri výluk (Closure.kind) – plánovač iné nepozná
RESTRICTION_CODES = ("uplna", "kolaj", "trakcne_vedenie", "pomala_jazda", "obmedzenie", "mimoriadnost")


def restriction_code(value: Any) -> Optional[str]:
    """Kód druhu obmedzenia z kódu alebo jeho slovenského názvu, aj s veľkými písmenami či bez diakritiky („úplná
    výluka“, „Uplna“, „výluka koľaje“, „koľaj“, „vypnuté trakčné vedenie“, „pomalá jazda“, „obmedzenie“) – None:
    neznámy druh (zapísaný by ho plánovač nepoznal)."""
    raw = str(value or "").strip()
    if raw in RESTRICTION_CODES:
        return raw
    f = re.sub(r"[\s_\-–—.,;:()/]+", " ", fold(raw)).strip()
    if not f:
        return None
    if f.replace(" ", "_") in RESTRICTION_CODES:
        return f.replace(" ", "_")
    if re.search(r"\btrakcn\w*|\btv\b", f):
        return "trakcne_vedenie"
    if re.search(r"\bpomal\w*|\brychlost\w*", f):
        return "pomala_jazda"
    if re.search(r"\bobmedzen\w*", f):
        return "obmedzenie"
    if re.search(r"\bmimoriad\w*", f):
        return "mimoriadnost"
    if re.search(r"\bkolaj\w*|\btk\b", f) and not re.search(r"\bob(?:e|i|och|om)\b|\bvsetk\w*", f):
        return "kolaj"
    if re.search(r"\buplne?\w*|\bcel\w*|\bvyluk\w*|\buzavret\w*|\buzaver\w*", f):
        return "uplna"
    return None


def _closure_values(kind: str, d: dict, now: datetime, strict: bool = True) -> tuple[dict, list[str]]:
    """Polia záznamu výluky z dát udalosti (bez stavu a pôvodu) a poznámky k nim.

    Neznámy druh obmedzenia (napr. voľný text z formulára fronty) sa nezapíše (``strict``: ApplyError – dispečer ho
    opraví); dočasný záznam čakajúcej udalosti (``strict=False``) ho počíta ako úplnú výluku (prísnejší prípad)."""
    start = parse_utc(d.get("start"))
    end = parse_utc(d.get("end"))
    if start is None:
        raise ApplyError("Chýba začiatok platnosti.")
    if not d.get("from_station"):
        raise ApplyError("Chýba úsek (dopravňa od).")
    notes = []
    open_ended = False
    if end is None:
        if d.get("until_revoked") or kind == "mimoriadnost":
            open_ended = True
            end = max(start, now) + timedelta(days=OPEN_END_DAYS)
            notes.append(f"Platí do odvolania – koniec nie je určený. Technický koniec sa priebežne posúva "
                         f"({OPEN_END_DAYS} dní dopredu) a agent pripomína overenie, kým ŽSR obmedzenie neodvolá.")
        else:
            raise ApplyError("Chýba koniec platnosti.")
    if kind == "mimoriadnost":
        restriction = "mimoriadnost"
    elif kind == "pomala_jazda":
        restriction = "pomala_jazda"
    else:
        restriction = restriction_code(d.get("kind") or "uplna")
        if restriction is None:
            if strict:
                raise ApplyError(f"Neznámy druh obmedzenia „{str(d.get('kind'))[:60]}“ – výluka sa nezapísala (plánovač "
                                 "by ju nepoznal). V časti „Upraviť a potvrdiť“ vyberte druh zo zoznamu (úplná výluka, "
                                 "výluka koľaje, …) a potvrďte znova.", retry=False)
            restriction = "uplna"
            notes.append(f"Neznámy druh obmedzenia „{str(d.get('kind'))[:60]}“ – do potvrdenia sa počíta ako úplná "
                         "výluka (prísnejší prípad).")
    # dĺžky podľa stĺpcov registra (PostgreSQL dlhšiu hodnotu odmietne a s ňou celý zápis); neplatný denný čas
    # (validácia ho hlási) sa nezapíše – výluka potom platí celý deň (prísnejší prípad)
    daily = [(parse_hhmm(str(d[k])) or "") if d.get(k) else "" for k in ("daily_from", "daily_to")]
    if not all(daily):
        daily = ["", ""]
    fields = {
        "kind": restriction, "from_station": str(d["from_station"])[:120],
        "to_station": str(d.get("to_station") or d["from_station"])[:120],
        "line_ref": str(d.get("line_ref") or "")[:40], "track": str(d.get("track") or "")[:40],
        "start_utc": start, "end_utc": end, "daily_from": daily[0], "daily_to": daily[1],
        "electric_only": restriction == "trakcne_vedenie", "speed_kmh": parse_number(d.get("speed_kmh")),
        "external_ref": str(d.get("external_ref") or "")[:120], "until_revoked": open_ended,
    }
    return fields, notes


def _closure_changes(c: Closure, fields: dict) -> list[str]:
    """Polia, ktoré by zápis zmenil (technický koniec výluky do odvolania sa za zmenu nepovažuje)."""
    changed = []
    for k, v in fields.items():
        if k == "external_ref" and not v:
            continue
        if k == "end_utc" and fields.get("until_revoked") and c.until_revoked:
            continue
        cur = getattr(c, k)
        if k == "until_revoked":
            same = bool(cur) == bool(v)
        elif isinstance(cur, datetime) and isinstance(v, datetime):
            same = _utc(cur) == v
        else:
            same = cur == v
        if not same:
            changed.append(k)
    return changed


def _closure_fields_of(c: Closure) -> dict:
    """Polia záznamu výluky v tvare ``fields`` (:func:`_closure_values`) – na porovnanie dvoch záznamov."""
    return {"kind": c.kind, "from_station": c.from_station, "to_station": c.to_station, "line_ref": c.line_ref,
            "track": c.track, "start_utc": _utc(c.start_utc) if c.start_utc is not None else None,
            "end_utc": _utc(c.end_utc) if c.end_utc is not None else None, "daily_from": c.daily_from,
            "daily_to": c.daily_to, "electric_only": c.electric_only, "speed_kmh": c.speed_kmh,
            "external_ref": c.external_ref, "until_revoked": c.until_revoked}


def _term_contained(c: Closure, fields: dict) -> bool:
    """Nový termín (``fields``) obsahuje všetky okná výluky ``c`` (tolerancia 1 min). Pri termíne do odvolania je jeho
    koniec technický (posúva sa) – nový termín do odvolania sa považuje za otvorený (koniec ≥ koniec výluky)."""
    slack = timedelta(minutes=1)
    ns, ne = fields.get("start_utc"), fields.get("end_utc")
    if ns is None or ne is None or c.start_utc is None or c.end_utc is None:
        return False
    cs, ce = _utc(c.start_utc), _utc(c.end_utc)
    if fields.get("until_revoked"):
        ne = max(ne, ce)
    old = _windows(cs, ce, c.daily_from, c.daily_to)
    new = _windows(ns, ne, fields.get("daily_from"), fields.get("daily_to"))
    if old is not None and new is not None:
        return all(any(n[0] <= w[0] + slack and w[1] <= n[1] + slack for n in new) for w in old)
    # priveľa okien (dlhá denná výluka) alebo okno, ktoré sa nedá prečítať: celé obdobie a denné okno, ktoré pôvodné
    # okno obsahuje (rovnaký tvar – cez deň či cez polnoc), alebo žiadne denné okno
    if not (ns <= cs + slack and ce <= ne + slack):
        return False
    nf, nt = fields.get("daily_from"), fields.get("daily_to")
    if not nf and not nt:
        return True
    o1, o2, n1, n2 = _hm(c.daily_from), _hm(c.daily_to), _hm(nf), _hm(nt)
    if None in (o1, o2, n1, n2):
        return str(nf or "") == str(c.daily_from or "") and str(nt or "") == str(c.daily_to or "")
    if (o1 < o2) != (n1 < n2):
        return False
    return n1 <= o1 and o2 <= n2


def _tracks_narrowed(c: Closure, fields: dict) -> bool:
    """Výluka koľaje by po zápise uzatvárala menej koľají (rovnako ako ich číta plánovač – ``_track_numbers``): koľaj
    neuvedená či všetky koľaje → konkrétna koľaj, iná koľaj. Pri iných druhoch plánovač koľaj nepoužíva."""
    if str(c.kind or "") != "kolaj" or str(fields.get("kind") or "") != "kolaj":
        return False
    from ..planovanie.planovac import ALL_TRACKS, _track_numbers

    old, new = _track_numbers(str(c.track or "")), _track_numbers(str(fields.get("track") or ""))
    if ALL_TRACKS in new or len(new) >= 2:
        return False                              # všetky koľaje úseku – prísnejšie či rovnaké
    if not new:
        # koľaj neuvedená: plánovač nepustí vlak ZELENÝ, ale nezastaví ho – miernejšie len oproti všetkým koľajam
        # (obe koľaje sú úplná výluka)
        return ALL_TRACKS in old or len(old) >= 2
    if ALL_TRACKS in old or len(old) >= 2 or not old:
        return True                               # všetky či neznáma koľaj → jedna koľaj
    return not new >= old


def _section_narrowed(c: Closure, fields: dict) -> bool:
    """Iná trať (číslo) či úsek by výluke ubrali koľaje siete (napr. úsek oboch rozchodov s doplneným číslom trate ŽSR
    ponechá len jeden rozchod). Ak úsek nemožno v sieti overiť, zmena sa považuje za zúženie (prísnejší prípad)."""
    from ..siet.graf import network, norm, norm_line_ref

    same = (norm(str(fields.get("from_station") or c.from_station or "")) == norm(str(c.from_station or ""))
            and norm(str(fields.get("to_station") or c.to_station or "")) == norm(str(c.to_station or ""))
            and norm_line_ref(str(fields.get("line_ref") or "")) == norm_line_ref(str(c.line_ref or "")))
    if same:
        return False
    try:
        net = network()
        only_ref = (norm(str(fields.get("from_station") or c.from_station or "")) == norm(str(c.from_station or ""))
                    and norm(str(fields.get("to_station") or c.to_station or "")) == norm(str(c.to_station or "")))
        if only_ref and norm(str(c.from_station or "")) != norm(str(c.to_station or "")):
            # číslo trate rozhoduje len na úseku oboch rozchodov (graf._section_edges) – inak nič nezúži
            from ..siet.graf import ROUTE_GAUGES

            sa, sb = net.find_station(str(c.from_station or "")), net.find_station(str(c.to_station or ""))
            if sa is not None and sb is not None:
                ga, gb = net.station_gauges(sa) & ROUTE_GAUGES, net.station_gauges(sb) & ROUTE_GAUGES
                if len((ga & gb) or (ga | gb)) <= 1:
                    return False
        old = net.closure_edges(str(c.from_station or ""), str(c.to_station or ""),
                                norm_line_ref(str(c.line_ref or "")))
        new = net.closure_edges(str(fields.get("from_station") or c.from_station or ""),
                                str(fields.get("to_station") or c.to_station or ""),
                                norm_line_ref(str(fields.get("line_ref") or "")))
    except Exception:
        return True
    return bool(old) and not set(new) >= set(old)


def _removal_parts(c: Closure, fields: dict) -> tuple[bool, list[str]]:
    """(odobral by sa čas, zoznam zmiernení obmedzenia) pri zápise ``fields`` do výluky ``c``
    (:func:`_removal_reason`)."""
    time_lost = not _term_contained(c, fields) or (bool(c.until_revoked) and not fields.get("until_revoked"))
    softer = []
    if str(fields.get("kind") or "uplna") != str(c.kind or "uplna"):
        softer.append(f"druh {_restriction_text(c.kind)} → {_restriction_text(fields.get('kind'))}")
    if _tracks_narrowed(c, fields):
        softer.append(f"koľaj {str(c.track or '').strip() or 'neuvedená'} → "
                      f"{str(fields.get('track') or '').strip() or 'neuvedená'}")
    if fields.get("electric_only") and not c.electric_only:
        softer.append("len pre vlaky na elektrickú trakciu")
    a, b = c.speed_kmh, fields.get("speed_kmh")
    if a is not None and (b is None or float(b) > float(a)):
        new_speed = "neuvedená" if b is None else f"{format(float(b), 'g')} km/h"
        softer.append(f"rýchlosť {format(float(a), 'g')} km/h → {new_speed}")
    if _section_narrowed(c, fields):
        def sec(a_: Any, b_: Any, ref: Any) -> str:
            return f"{a_} – {b_}" + (f" (trať {ref})" if ref else "")
        to_sec = sec(fields.get("from_station") or c.from_station, fields.get("to_station") or c.to_station,
                     fields.get("line_ref"))
        softer.append(f"úsek či trať {sec(c.from_station, c.to_station, c.line_ref)} → {to_sec}")
    return time_lost, softer


def _removal_reason(c: Closure, fields: dict) -> str:
    """Prečo by zápis ``fields`` do výluky ``c`` výluke odobral zatvorený čas alebo zmiernil obmedzenie („“ – nič
    neodoberie: nový termín obsahuje všetky okná výluky a obmedzenie je rovnaké či prísnejšie).

    Taký zápis bez dispečera nesmie prebehnúť – napr. opakovane poslaný ROV s kratším termínom či neskorším začiatkom,
    týždenný prehľad povolených výluk, ktorý uvádza len časť (týždeň) viacdennej výluky, alebo zle prečítaný termín:
    plánovač by inak skutočný čas výluky považoval za voľný (falošná ZELENÁ). Do potvrdenia ostáva výluka celá
    a nový termín je dočasný záznam „navrh“ – plánovač počíta s oboma."""
    time_lost, softer = _removal_parts(c, fields)
    if not time_lost and not softer:
        return ""
    old_desc = _term_desc(c.start_utc, c.end_utc, c.daily_from, c.daily_to) + \
        ("" if not c.until_revoked else ", do odvolania")
    new_desc = _term_desc(fields.get("start_utc"), fields.get("end_utc"), fields.get("daily_from"),
                          fields.get("daily_to")) + ("" if not fields.get("until_revoked") else ", do odvolania")
    where = f"#{c.id} {c.from_station} – {c.to_station}"
    head = (f"zápis by výluke {where} odobral zatvorený čas ({old_desc} → {new_desc})" if time_lost
            else f"zápis by zmiernil obmedzenie výluky {where}")
    if softer:
        head += (" a zmiernil obmedzenie" if time_lost else "") + f" ({'; '.join(softer)})"
    return (head + " – napr. opakovaný ROV či týždenný prehľad s časťou výluky; kým to nepotvrdí dispečer, výluka "
            "ostáva celá")


def _snapshot_fields(snap: dict) -> Optional[dict]:
    """Polia výluky zo snímky ``applied.before`` (:func:`_snapshot`) v tvare ``fields``; None – snímka bez termínu."""
    start, end = parse_utc(snap.get("start_utc")), parse_utc(snap.get("end_utc"))
    if start is None or end is None:
        return None
    out = {k: snap.get(k) for k in ("kind", "from_station", "to_station", "line_ref", "track", "daily_from",
                                    "daily_to", "electric_only", "speed_kmh", "external_ref", "until_revoked")}
    out.update(start_utc=start, end_utc=end)
    return out


def _day_pieces(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Súvislý čas rozdelený na kusy po miestnych dňoch (polnoc miestneho času)."""
    out, s = [], start
    while s < end:
        nxt = _utc(_local_at(local(s).date() + timedelta(days=1), (0, 0)))
        e = min(end, nxt)
        out.append((s, e))
        s = e
    return out


def _term_days(start: Any, end: Any, daily_from: Any, daily_to: Any) -> tuple[list, set]:
    """(kusy času, miestne dni): denná výluka po oknách (deň = deň začiatku okna), súvislá po dňoch."""
    if start is None or end is None:
        return [], set()
    s, e = _utc(start), _utc(end)
    wins = _windows(s, e, daily_from, daily_to) if (daily_from and daily_to) else None
    pieces = wins if wins is not None else _day_pieces(s, e)
    return pieces, {local(p[0]).date() for p in pieces}


def _terms_outside(target: Closure, fields: dict) -> list[dict]:
    """Termíny z ``fields`` v dňoch, ktoré výluka ``target`` nemá (po súvislých úsekoch dní): o dňoch výluky rozhodla
    jej novšia udalosť (aj o čase, ktorý ten deň predtým nemal – napr. novšia zmena toho istého dňa), iné dni sú
    čas, ktorý staršia udalosť pridáva (presun na iný deň, doplnenie, predĺžený deň)."""
    _tp, tdays = _term_days(target.start_utc, target.end_utc, target.daily_from, target.daily_to)
    daily = bool(fields.get("daily_from") and fields.get("daily_to"))
    pieces, _days = _term_days(fields.get("start_utc"), fields.get("end_utc"), fields.get("daily_from") if daily
                               else "", fields.get("daily_to") if daily else "")
    kept = [p for p in pieces if local(p[0]).date() not in tdays]
    runs: list[list] = []
    for p in kept:
        prev = runs[-1][-1] if runs else None
        joined = prev is not None and ((not daily and p[0] <= prev[1]) or
                                       (daily and local(p[0]).date() == local(prev[0]).date() + timedelta(days=1)))
        if joined:
            runs[-1].append(p)
        else:
            runs.append([p])
    out = []
    for run in runs:
        f = dict(fields)
        f["start_utc"], f["end_utc"] = run[0][0], run[-1][1]
        if not daily:
            f["daily_from"] = f["daily_to"] = ""
        out.append(f)
    return out


def _removed_by_newer(session: Session, target: Closure, ev: ExtractedEvent, trial: Closure) -> bool:
    """Termín ``trial`` staršej udalosti ``ev`` výluka ``target`` už mala a odobrala jej ho novšia udalosť (napr.
    pôvodný ROV a potom potvrdený presun či zmena času) – staršia udalosť ho neobnoví. Prechádza zapisovateľov výluky
    novších ako ``ev`` a ich snímky stavu pred zápisom (``applied.before``)."""
    oid, seen = target.source_event_id, set()
    while oid and oid not in seen and (ev.id is None or oid > ev.id):
        seen.add(oid)
        writer = session.get(ExtractedEvent, oid)
        applied = (writer.validation or {}).get("applied") if writer is not None and \
            isinstance(writer.validation, dict) else None
        before = applied.get("before") if isinstance(applied, dict) else None
        if isinstance(before, list):         # rozdelenie výluky: snímka pôvodného záznamu (podľa čísla, inak prvá)
            before = next((b for b in before if isinstance(b, dict) and b.get("id") == target.id),
                          next((b for b in before if isinstance(b, dict)), None))
        if not isinstance(before, dict):
            return False                     # záznam založil novší zapisovateľ – staršia udalosť ho v ňom nemala
        snap = _snapshot_fields(before)
        if snap is not None and not any(_removal_parts(trial, snap)):
            return True
        oid = before.get("source_event_id")
    return False


def _keep_extra_term(session: Session, ev: ExtractedEvent, target: Closure, row: Optional[Closure],
                     owner: ExtractedEvent, how: str, confirmed: bool, fields: dict, *,
                     cancelled: bool = False) -> Optional[dict]:
    """Výluku, ktorej sa udalosť týka, medzitým zapísala (zmenila či zrušila) novšia udalosť ``owner`` – staršia udalosť
    jej termín neprepíše. Novšia správa však nemusí vyvracať čas, ktorý staršia udalosť výluke pridáva (presun na iný
    deň, doplnenie, predĺženie): ak termín udalosti (``fields`` – aj s opravami dispečera) drží zatvorený čas či
    prísnejšie obmedzenie, ktoré výluka v registri nemá, nesmie sa zahodiť – plánovač s ním počítal (dočasný záznam).

    Potvrdená udalosť: termín sa zapíše ako samostatná výluka (prísnejší prípad – dispečer upraví či zruší, čo e-mail
    nahrádza); použije sa záznam udalosti ``row`` (dočasný záznam či vlastný návrh), inak nový. Automatický zápis:
    udalosť ostane čakať na dispečera. Zrušená výluka (``cancelled``): len ak udalosti ostal záznam – zrušenie, ktoré
    termín zmeny pokrýva, ho odstránilo (:func:`_unpin_older_changes`). None – nič navyše (udalosť sa nahradí)."""
    if fields.get("start_utc") is None or fields.get("end_utc") is None:
        return None
    if cancelled:
        if row is None or row.status == "zrusene":
            return None
        term = _term_desc(fields["start_utc"], fields["end_utc"], fields.get("daily_from"), fields.get("daily_to"))
        why = (f"výluku #{target.id} zrušila novšia udalosť #{owner.id}, termín tejto udalosti ({term}) však zrušenie "
               "nepokrýva")
    else:
        trial = Closure(id=row.id if row is not None else None,
                        **{k: fields.get(k) for k in _closure_fields_of(target)})
        time_extra, stricter = _removal_parts(trial, _closure_fields_of(target))
        if not time_extra and not stricter:
            return None
        if _removed_by_newer(session, target, ev, trial):
            return None              # novšia udalosť tento čas výluke zámerne odobrala (presun, iný čas) – neobnoví sa
        terms = _terms_outside(target, fields)
        if not terms:
            return None              # o dňoch udalosti rozhodla novšia udalosť (napr. zmena toho istého dňa)
        term = "; ".join(_term_desc(t["start_utc"], t["end_utc"], t.get("daily_from"), t.get("daily_to"))
                         for t in terms)
        what = (f"termín tejto udalosti ({term}) však výluka v registri nemá" if time_extra else
                f"výluka v registri však má miernejšie obmedzenie ako táto udalosť ({'; '.join(stricter)})")
        why = f"výluku #{target.id} medzitým zapísala novšia udalosť #{owner.id} (aj opakované oznámenie), {what}"
    if not confirmed:
        raise NeedsConfirmation([why + " – overte, ktorý termín platí"])
    note = (f"Potvrdené dispečerom (udalosť #{ev.id}): {why} – zapísaný ako samostatná výluka (prísnejší prípad). Ak "
            f"nahrádza termín výluky #{target.id}, upravte ju v registri výluk.")
    if cancelled:
        terms = [fields]
    if row is None or row.status == "zrusene":
        row = Closure(status="overene", source="email", source_event_id=ev.id)
        session.add(row)
        keep: list[str] = []
    else:
        keep = [ln for ln in (row.note or "").splitlines()
                if ln.strip() and not ln.startswith("NÁVRH – čaká na potvrdenie")]
    rows = [row]
    for t in terms[1:]:
        extra = Closure(status="overene", source="email", source_event_id=ev.id)
        session.add(extra)
        rows.append(extra)
    for r, t in zip(rows, terms):
        for k, v in t.items():
            setattr(r, k, v)
        r.proposed_change_of = None
        r.status = "overene"
        r.source = "email"
        r.source_event_id = ev.id
        r.note = "\n".join((keep if r is row else []) + [note])
    session.flush()
    for r in rows:
        audit(session, "agent", "vyluka_zmena_samostatne", "Closure", r.id, udalost=ev.id, vyluka=target.id,
              novsia_udalost=owner.id)
    out = {"entity": "Closure", "id": row.id, "action": "vytvorene", "status": row.status, "matched_by": how,
           "before": None, "superseded_by": owner.id, "note": note}
    if len(rows) > 1:
        out["added"] = [r.id for r in rows[1:]]
    return out


def _closure_guard(session: Session, c: Closure, now: datetime, ev_id: Optional[int]) -> list[str]:
    """Prečo sa výluka nesmie zmeniť automaticky (overená, platí alebo začne do 72 h, čaká na potvrdenie)."""
    h = settings().confirm_horizon_h
    reasons = []
    if c.status == "overene":
        reasons.append(f"mení sa výluka #{c.id} overená dispečerom")
    if _utc(c.start_utc) <= now + timedelta(hours=h) and _utc(c.end_utc) >= now:
        reasons.append(f"mení sa výluka #{c.id}, ktorá platí alebo začne do {h} h "
                       f"({fmt(c.start_utc)} – {fmt(c.end_utc)})")
    owner = _pending_owner(session, c, ev_id)
    if owner is not None:
        reasons.append(f"výluka #{c.id} ešte čaká na potvrdenie udalosti #{owner.id}")
    return reasons


def _event_closures(session: Session, ev_id: int) -> tuple[Optional[Closure], Optional[Closure]]:
    """(vlastný záznam udalosti, dočasný záznam jej navrhovanej zmeny) – len platné."""
    rows = list(session.scalars(select(Closure).where(Closure.source_event_id == ev_id, Closure.status != "zrusene")
                                .order_by(Closure.id)))
    own = next((c for c in rows if c.proposed_change_of is None), None)
    temp = next((c for c in rows if c.proposed_change_of is not None), None)
    return own, temp


def _agrees(session: Session, t: Closure, ev: ExtractedEvent, d: dict, start: datetime) -> bool:
    """Výluka ``t`` (priradenie zistené pri spracovaní e-mailu) zodpovedá potvrdeným dátam udalosti.

    Dispečer pri potvrdení môže opraviť číslo, druh alebo úsek – priradenie (``proposed_change_of``) potom už
    nemusí platiť a výluka sa hľadá znova. Neplatí, keď nesedí úsek, druh obmedzenia alebo číslo výluky; keď
    ``t`` je bez čísla a doplnené číslo má v tom istom úseku iná výluka (zmena patrí jej); a keď udalosť už nie
    je zmena výluky a ``t`` by podľa pravidiel jej druhu nesedela (nová výluka v inom termíne je ďalší termín, nie
    presun – aj s rovnakým číslom, ROV môže mať viac termínov; rovnaká výluka je len so začiatkom ± 1 h); a keď
    ``t`` naposledy zapísala sesterská udalosť z toho istého e-mailu.
    """
    ref = d.get("external_ref")
    a, b = d.get("from_station"), d.get("to_station")
    fam = _event_family(ev.kind, d)
    if not (_same_section(t, a, b) and _closure_family(t) == fam
            and (not ref or not t.external_ref or refs_match(t.external_ref, ref))):
        return False
    if t.source_event_id is not None and t.source_event_id in _sibling_events(session, ev.id):
        return False
    if ref and not t.external_ref:
        for c in _active_closures(session, ev.id):
            if c.id != t.id and c.external_ref and refs_match(c.external_ref, ref) and _closure_family(c) == fam \
                    and _same_section(c, a, b):
                return False
    if ev.kind != "vyluka_zmena":
        return (_same_term(t, start) and _same_restriction(t, ev.kind, d)) or _newer_writer(session, t, ev.id)
    return True


def _split_parts(session: Session, c: Closure) -> list[Closure]:
    """Platné časti výluky, ktorú rozdelila udalosť, čo naposledy zapísala záznam ``c`` (``c`` je jednou z nich);
    [] – ``c`` nie je časťou rozdelenej výluky."""
    writer = session.get(ExtractedEvent, c.source_event_id) if c.source_event_id else None
    applied = (writer.validation or {}).get("applied") if writer is not None and \
        isinstance(writer.validation, dict) else None
    if not (isinstance(applied, dict) and applied.get("action") == "rozdelene"):
        return []
    ids = [t.get("id") for t in applied.get("terms") or [] if isinstance(t, dict)]
    if c.id not in ids:
        return []
    return [p for p in (session.get(Closure, i) for i in ids if i)
            if p is not None and p.status != "zrusene" and p.proposed_change_of is None]


def _carry_note(target: Closure, row: Closure) -> None:
    """Poznámku, ktorú dispečer dopísal do odstraňovaného záznamu „navrh“, prenesie do výsledného záznamu."""
    own = [ln for ln in (row.note or "").splitlines()
           if ln.strip() and not ln.startswith("NÁVRH – čaká na potvrdenie") and ln not in (target.note or "")]
    if own:
        target.note = "\n".join(filter(None, [target.note or "", f"Poznámka zo záznamu #{row.id}: " + " ".join(own)]))


def _apply_closure(session: Session, ev: ExtractedEvent, confirmed: bool) -> dict:
    d = ev.data or {}
    now = now_utc()
    fields, notes = _closure_values(ev.kind, d, now)
    if confirmed:
        _overlay_register_fields(ev, fields)
    if ev.kind == "mimoriadnost" and d.get("description"):
        notes.insert(0, str(d["description"]))
    if d.get("note"):
        notes.insert(0, str(d["note"]))
    email = session.get(Email, ev.email_id) if ev.email_id else None
    source_line = f"Zdroj: e-mail #{ev.email_id}" + (f" od {email.sender}, „{email.subject[:120]}“" if email else "")
    was_applied = bool((ev.validation or {}).get("applied"))
    previous = (ev.validation or {}).get("applied") or {}
    if was_applied and previous.get("action") in ("rozdelene", "bez_zmeny"):
        # udalosť už výluku rozdelila (alebo register nezmenila) – opakovaný zápis nič nemení
        return {k: v for k, v in previous.items() if k not in ("at", "by", "confirmed", "ok", "kind")}
    own, temp = _event_closures(session, ev.id)
    # vlastný záznam nezapísanej udalosti je len jej návrh „navrh“ (nie výluka v registri); pri zmene výluky
    # s číslom (napr. doplneným dispečerom) sa najprv hľadá výluka, ktorej sa zmena týka
    placeholder = own is not None and not was_applied
    rebind = placeholder and ev.kind == "vyluka_zmena" and bool(d.get("external_ref"))
    target, how, promote = None, "", False
    pinned = temp.proposed_change_of if temp is not None else None
    mark = (ev.validation or {}).get(_CANCELLED_TARGET)
    if not pinned and isinstance(mark, dict):
        pinned = mark.get("closure")            # dočasný záznam odstránilo novšie zrušenie výluky
    if pinned:
        t = session.get(Closure, pinned)
        owner = _newer_owner(session, t, ev) if t is not None and t.status == "zrusene" else None
        if owner is not None and _agrees(session, t, ev, d, fields["start_utc"]):
            # výluku, ktorej sa zmena týka, medzitým zrušila novšia udalosť – staršia zmena ju neobnoví (dočasný
            # záznam s novým termínom sa odstráni, inak by výluka vznikla znova ako nová) – okrem termínu, ktorý
            # zrušenie nepokrýva (dočasný záznam ostal – :func:`_unpin_older_changes`)
            kept = _keep_extra_term(session, ev, t, temp, owner, "navrhovaná zmena výluky", confirmed, fields,
                                    cancelled=True)
            if kept is not None:
                return kept
            _drop_pending(session, ev, "", own=False)
            return _superseded("Closure", t, "navrhovaná zmena výluky", owner)
        if temp is not None and t is not None and t.status != "zrusene" and \
                _agrees(session, t, ev, d, fields["start_utc"]):
            parts = _split_parts(session, t) if t.source_event_id != ev.id else []
            if parts and not _overlaps(t, fields["start_utc"], fields["end_utc"]):
                # výluku po zápise dočasného záznamu rozdelila iná udalosť (napr. súbežné potvrdenie) – zmena patrí
                # časti, s ktorou sa jej nový termín prekrýva; inak sa výluka hľadá znova (iná časť sa nepresunie)
                near = [p for p in parts if _overlaps(p, fields["start_utc"], fields["end_utc"])]
                if len(near) == 1:
                    target, how = near[0], "časť rozdelenej výluky"
            else:
                target, how = t, "navrhovaná zmena výluky"
    if target is None and own is not None and not rebind:
        target, how = own, "rovnaká udalosť"
    if target is None:
        target, how = _find_closure(session, ev.id, ev.kind, d, fields["start_utc"], loose=ev.kind == "vyluka_zmena",
                                    skip_own=True, issuer=_event_issuer(session, ev))
    if target is None and own is not None:
        target, how = own, "rovnaká udalosť"                       # výluka v registri nie je – návrh sa stáva záznamom
    if target is None and temp is not None and confirmed:
        target, how, promote = temp, "dočasný záznam", True        # pôvodná výluka medzitým zanikla alebo nesedí
        if ev.kind == "vyluka_zmena":
            notes.append("Zmena sa týka výluky, ktorá v registri nie je – založená ako nová.")
    if ev.kind == "vyluka_zmena" and target is not None and target is not own and target is not temp:
        _inherit_track(fields, target)
    plan = _change_plan(session, target, fields, ev.id, email, zamer=_intent(d)) \
        if ev.kind == "vyluka_zmena" and target is not None and target is not own and target is not temp else None
    if plan is not None:
        # zmena sa týka len časti výluky (napr. jedného dňa viacdennej výluky) – nikdy nie celej výluky
        owner = _newer_owner(session, target, ev)
        if owner is not None:
            kept = _keep_extra_term(session, ev, target, temp or (own if placeholder else None), owner, how,
                                    confirmed, fields)
            if kept is not None:
                return kept
            _drop_pending(session, ev, f"Udalosť #{ev.id} nahradila novšia udalosť #{owner.id}.", own=rebind,
                          delete_own=True)
            return _superseded("Closure", target, how, owner)
        if not confirmed:
            raise NeedsConfirmation([plan.text])
        target = session.get(Closure, target.id, with_for_update=True, populate_existing=True) or target
        if plan.kind == "rozdelit":
            return _split_closure(session, ev, target, plan, how, fields=fields, notes=notes + [source_line],
                                  drop=[x for x in (temp, own if placeholder else None) if x is not None])
        if plan.kind == "bez_zmeny":
            return _unchanged_closure(session, ev, target, plan, how,
                                      drop=[x for x in (temp, own if placeholder else None) if x is not None])
        if plan.kind == "predlzit":
            return _extend_closure(session, ev, target, plan, how, fields=fields, notes=notes + [source_line],
                                   drop=[x for x in (temp, own if placeholder else None) if x is not None])
        # „pridat“: pôvodná výluka ostáva celá, nový termín je ďalšia výluka (prísnejší prípad) – dispečer upraví
        # termín pôvodnej výluky, ak ho e-mail mení
        notes.append(plan.text)
        if temp is not None:
            target, how, promote = temp, "dočasný záznam", True
        elif own is not None:
            target, how = own, "rovnaká udalosť"
        else:
            target = None
    if target is None:
        gone, how = _find_closure(session, ev.id, ev.kind, d, fields["start_utc"], cancelled=True)
        owner = _newer_owner(session, gone, ev) if gone is not None else None
        if owner is not None:     # výluku medzitým zrušila novšia udalosť – staršia ju neobnoví
            return _superseded("Closure", gone, how, owner)
        if not confirmed and ev.kind == "vyluka_zmena":
            raise NeedsConfirmation(["zmena sa nedá priradiť k výluke v registri – overte, ktorej výluky sa týka"])
        if ev.kind == "vyluka_zmena" and not any("ďalšia výluka" in n for n in notes):
            notes.append("Zmena sa týka výluky, ktorá v registri nie je – založená ako nová.")
        c = Closure(note="\n".join(notes + [source_line]), status="overene" if confirmed else "navrh", source="email",
                    source_event_id=ev.id, **fields)
        session.add(c)
        session.flush()
        return {"entity": "Closure", "id": c.id, "action": "vytvorene", "status": c.status, "before": None}
    # zámok riadku (PostgreSQL): súbežná úprava v registri výluk (/vyluky) sa nestratí ani neprepíše
    target = session.get(Closure, target.id, with_for_update=True, populate_existing=True) or target
    owner = _newer_owner(session, target, ev)
    if owner is not None:
        mine = temp if temp is not None and temp is not target else \
            (own if placeholder and own is not None and own is not target else None)
        kept = _keep_extra_term(session, ev, target, mine, owner, how, confirmed, fields)
        if kept is not None:
            return kept
        _drop_pending(session, ev, f"Udalosť #{ev.id} nahradila novšia udalosť #{owner.id}.", own=rebind,
                      delete_own=True)
        return _superseded("Closure", target, how, owner)
    changes = _closure_changes(target, fields)
    if fields.get("until_revoked") and "end_utc" in changes and _utc(target.end_utc) > fields["end_utc"]:
        # termín do odvolania: jeho koniec je technický (posúva sa) – neskorší koniec výluky sa nezmení (inak by sa
        # výluka potichu skrátila na technický koniec)
        changes = [k for k in changes if k != "end_utc"]
    first_write = promote or (target is own and not was_applied)    # návrh tejto udalosti sa stáva záznamom
    if not confirmed and target is not own and changes:
        reasons = _closure_guard(session, target, now, ev.id)
        # automatický zápis nikdy neodoberie výluke zatvorený čas ani nezmierni obmedzenie (každý druh udalosti – aj
        # zmena výluky prečítaná staršou verziou agenta bez nového pravidla potvrdzovania)
        removal = _removal_reason(target, fields)
        if removal:
            reasons.append(removal)
        if reasons:
            raise NeedsConfirmation(reasons)
    before = None if first_write else _snapshot(target, _CLOSURE_FIELDS)
    for k in changes:
        setattr(target, k, fields[k])
    if fields["until_revoked"] and "end_utc" not in changes and _utc(target.end_utc) < fields["end_utc"]:
        target.end_utc = fields["end_utc"]                           # technický koniec len dopredu
    if promote:
        target.proposed_change_of = None
    if confirmed:
        target.status = "overene"
    elif changes:
        target.status = "navrh"          # overenú výluku sem bez potvrdenia nepustí _closure_guard
    target.source = "email"
    target.source_event_id = ev.id
    if changes and not first_write:
        target.version = (target.version or 1) + 1
        session.flush()
        _repin_newer_same(session, target, ev)
    extra = [n for n in notes if n not in (target.note or "")]
    if extra or changes or first_write:
        verb = "Potvrdené" if first_write and confirmed else ("Zmenené" if changes else "Potvrdené")
        target.note = "\n".join(filter(None, [target.note or ""] + extra +
                                         [f"{verb} podľa udalosti #{ev.id} ({how}). {source_line}"]))
    removed = None
    if temp is not None and temp is not target:
        _carry_note(target, temp)
        removed = temp.id
        session.delete(temp)
    own_removed = None
    if placeholder and own is not target:      # zmena patrí výluke v registri – návrh „navrh“ už netreba
        _carry_note(target, own)
        own_removed = own.id
        session.delete(own)
    session.flush()
    result = {"entity": "Closure", "id": target.id, "action": "vytvorene" if first_write else
              ("aktualizovane" if changes else "potvrdene"), "status": target.status, "matched_by": how,
              "before": before}
    if removed:
        result["temp_removed"] = removed
    if own_removed:
        result["own_removed"] = own_removed
    return result


def _apply_closure_cancel(session: Session, ev: ExtractedEvent, confirmed: bool) -> dict:
    d = ev.data or {}
    val = ev.validation or {}
    applied = bool(val.get("applied"))
    previous = val.get("applied") or {}
    if applied and previous.get("action") == "rozdelene":
        # udalosť už zrušila časť výluky (výluku rozdelila) – opakovaný zápis nič nemení
        return {k: v for k, v in previous.items() if k not in ("at", "by", "confirmed", "ok", "kind")}
    c = None
    if applied:              # opakovaný zápis – výluka, ktorú táto udalosť už zrušila
        c = session.scalar(select(Closure).where(Closure.source_event_id == ev.id,
                                                 Closure.proposed_change_of.is_(None)))
    how = "rovnaká udalosť"
    issuer = _event_issuer(session, ev)
    if c is None:
        # vlastný záznam „navrh“ nezapísanej udalosti ostal z jej pôvodného druhu (dispečer ju pri potvrdení
        # preklasifikoval na zrušenie) – nie je to rušená výluka; ruší sa výluka v registri
        c, how = _find_closure(session, ev.id, ev.kind, d, parse_utc(d.get("start")), loose=True, skip_own=True,
                               issuer=issuer)
    gone = _repeat_cancel(session, ev.id, d, issuer) if not applied else None
    if c is not None and gone is not None and not _other_term(_cancel_plan(session, c, d, ev.id)):
        gone = None
    if gone is not None and not _orphan(ev):
        # výluka (či rušený termín rozdelenej výluky) je už zrušená – znova poslané či preposlané zrušenie, zrušenie po
        # zrušení v registri výluk: opakované zrušenie register nemení a nie je chybou
        if not confirmed:
            raise NeedsConfirmation(["zrušenie výluky vždy potvrdzuje dispečer"])
        return {"entity": "Closure", "id": gone.closure_id, "action": "bez_zmeny", "status": gone.status,
                "matched_by": "zrušená výluka či termín s rovnakým číslom či úsekom", "before": None,
                "note": gone.text}
    if c is None:
        text, specific = _no_target(session, ev.id, ev.kind, d, issuer)
        if _orphan(ev):
            # poistka zrušenia bez cieľa ostáva (záznam „navrh“ či poistka pre celý vozový park), kým dispečer udalosť
            # nezamietne alebo nezmení jej druh – zrušenie, ktoré nič neruší, sa nikdy nezapíše
            raise ApplyError(f"{text} Zrušenie z e-mailu nemá v registri čo zrušiť – môže ísť o novú výluku: ak e-mail "
                             "ohlasuje novú výluku, zmeňte druh udalosti na novú výluku (Upraviť a potvrdiť); ak ruší "
                             "výluku, ktorá v registri nie je, udalosť zamietnite. Dovtedy plánovač počíta s výlukou "
                             "v úseku a čase zrušenia.", retry=False)
        raise ApplyError(text, retry=not specific)
    plan = None
    if not applied:
        email = session.get(Email, ev.email_id) if ev.email_id else None
        stays = "" if _edited_place(ev) else _stays_text(email, c, d)
        if stays:
            # e-mail hovorí, že výluka v tomto úseku ostáva – zrušenie patrí inému úseku (R5C1)
            raise ApplyError(stays, retry=False)
        plan = _cancel_plan(session, c, d, ev.id)
        if plan is not None and plan.kind == "odmietnut":
            # zrušenie len časti výluky, ktorú nemožno bezpečne rozdeliť – celá výluka sa nezruší
            hint = (" Zrušenie môže byť nesprávne prečítaným oznámením novej výluky – dovtedy plánovač počíta s výlukou "
                    "v úseku a čase zrušenia." if _orphan(ev) else "")
            raise ApplyError(plan.text + hint, retry=False)
        # zrušenie len niektorých dní v týždni či hodín bez dátumov, noc „na“ deň: nikdy nie celá výluka či iná noc
        limit = "" if _edited_term(ev) else _cancel_restriction(email, c, d, whole=plan is None)
        if not limit and not _edited_term(ev):
            limit = _until_revoked_cancel(email, c)
        if limit:
            raise ApplyError(limit, retry=False)
    if not applied and confirmed:
        _drop_pending(session, ev, "", delete_own=True)
    c = session.get(Closure, c.id, with_for_update=True, populate_existing=True) or c
    owner = _newer_owner(session, c, ev)
    if owner is not None:        # výluku medzitým zmenila novšia udalosť – ostáva platná (bezpečnejšie)
        return _superseded("Closure", c, how, owner)
    if not confirmed:
        raise NeedsConfirmation(["zrušenie výluky vždy potvrdzuje dispečer"])
    if plan is not None and plan.kind == "rozdelit":
        # zrušenie len časti výluky (jeden deň viacdennej výluky, časť dňa či noci) – ostatné časti ostanú
        return _split_closure(session, ev, c, plan, how)
    before = _snapshot(c, _CLOSURE_FIELDS)
    c.status = "zrusene"
    c.version = (c.version or 1) + 1
    c.source_event_id = ev.id
    c.note = "\n".join(filter(None, [c.note or "", f"Zrušené podľa udalosti #{ev.id} (e-mail #{ev.email_id}, {how})."]))
    session.flush()
    stale = _unpin_older_changes(session, c, ev)
    _repin_newer_same(session, c, ev)        # novšie čakajúce oznámenie tej istej výluky ostáva pre plánovač
    result = {"entity": "Closure", "id": c.id, "action": "zrusene", "status": c.status, "matched_by": how,
              "before": before}
    if stale:
        result["older_changes"] = stale
    return result


def _edited_place(ev: ExtractedEvent) -> bool:
    """Dispečer pri potvrdení výslovne opravil úsek alebo číslo výluky (vie, ktorej výluky sa udalosť týka)."""
    for e in (ev.validation or {}).get("edits") or []:
        changes = e.get("changes") if isinstance(e, dict) else None
        if isinstance(changes, dict) and any(k in changes for k in ("from_station", "to_station", "external_ref")):
            return True
    return False


# ---------------------------------------------------------------------- čiastočná zmena a zrušenie (rozdelenie výluky)
@dataclass
class _Plan:
    """Ako zapísať zmenu či zrušenie, ktoré sa týka len časti výluky.

    ``kind``: „rozdelit“ – výluka sa rozdelí (``parts`` – ostávajúce časti pôvodnej výluky: (začiatok, koniec, či
    ponechať denné opakovanie)); „pridat“ – pôvodná výluka ostane celá, nový termín je ďalšia výluka (prísnejší
    prípad); „bez_zmeny“ – nový termín už výluka obsahuje; „odmietnut“ – zrušenie nemožno zapísať automaticky.
    ``text`` – vysvetlenie pre dispečera (fronta, poznámka v registri, chyba zápisu)."""

    kind: str
    text: str
    parts: list = field(default_factory=list)


def _local_days(start: datetime, end: datetime) -> int:
    """Počet kalendárnych dní (miestny čas), ktoré termín zasahuje."""
    last = end - timedelta(microseconds=1) if end > start else start
    return (local(last).date() - local(start).date()).days + 1


def _hm(text: Any) -> Optional[tuple[int, int]]:
    """„HH:MM“ (aj „H.MM“) → (h, m); „24:00“ = koniec dňa. Neplatný údaj → None (ako plánovač)."""
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", str(text or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if (h == 24 and mi == 0) or (0 <= h <= 23 and 0 <= mi <= 59):
        return h, mi
    return None


def _local_at(day: date, hm: tuple[int, int]) -> datetime:
    from ..cas import tz

    h, mi = hm
    extra = 0
    if h == 24:
        h, extra = 0, 1
    d = day + timedelta(days=extra)
    return datetime(d.year, d.month, d.day, h, mi, tzinfo=tz())


def _windows(start: Any, end: Any, daily_from: Any, daily_to: Any) -> Optional[list[tuple[datetime, datetime]]]:
    """Časové okná záznamu výluky (UTC) rovnako, ako ich počíta plánovač: denné opakovanie (miestny čas) po dňoch,
    orezané na začiatok a koniec výluky, inak celé obdobie. None – denné okno sa nedá prečítať alebo je okien
    priveľa (záznam sa nerozdelí)."""
    if start is None or end is None:
        return None
    cs, ce = _utc(start), _utc(end)
    if ce <= cs:
        return None
    if not (daily_from or daily_to):
        return [(cs, ce)]
    t1, t2 = _hm(daily_from), _hm(daily_to)
    if t1 is None or t2 is None or t1 == (24, 0):
        return None
    out: list[tuple[datetime, datetime]] = []
    day = local(cs).date() - timedelta(days=1)
    last = local(ce).date() + timedelta(days=1)
    while day <= last:
        ws, we = _local_at(day, t1), _local_at(day, t2)
        if we <= ws:
            we += timedelta(days=1)
        ws, we = max(_utc(ws), cs), min(_utc(we), ce)
        if we > ws:
            out.append((ws, we))
            if len(out) > SPLIT_MAX_WINDOWS:
                return None
        day += timedelta(days=1)
    return out


def _minus(w: tuple[datetime, datetime], ps: datetime, pe: datetime) -> list[tuple[datetime, datetime]]:
    """Okno ``w`` bez rušeného termínu ps – pe (zvyšok kratší ako ``SPLIT_TOLERANCE`` je nepresnosť termínu)."""
    ws, we = w
    if pe <= ws or ps >= we:
        return [w]
    return [(s, e) for s, e in ((ws, ps), (pe, we)) if e - s > SPLIT_TOLERANCE]


def _compact(wins: list, keep: list) -> list[tuple[datetime, datetime, bool]]:
    """Ostávajúce časti výluky ako záznamy: nedotknuté okná idúce za sebou → jeden záznam s denným opakovaním
    pôvodnej výluky (True), skrátené okno → samostatný záznam bez opakovania (False). ``keep[i]`` – časti okna i,
    ktoré ostávajú (``[okno]`` = nedotknuté, ``[]`` = celé okno zaniká)."""
    out: list[tuple[datetime, datetime, bool]] = []
    run: Optional[list] = None
    for w, rest in zip(wins, keep):
        if rest == [w]:
            run = [w[0], w[1]] if run is None else [run[0], w[1]]
            continue
        if run is not None:
            out.append((run[0], run[1], True))
            run = None
        out += [(s, e, False) for s, e in rest]
    if run is not None:
        out.append((run[0], run[1], True))
    return out


def _term_desc(start: Any, end: Any, daily_from: Any = "", daily_to: Any = "") -> str:
    out = f"{fmt(start)} – {fmt(end)}"
    return out + (f", denne {daily_from}–{daily_to}" if daily_from and daily_to else "")


def _parts_text(c: Closure, parts: list) -> str:
    return "; ".join(_term_desc(s, e, c.daily_from if keep else "", c.daily_to if keep else "")
                     for s, e, keep in parts)


def _same_fields(c: Closure, fields: dict) -> bool:
    """Nový termín má to isté obmedzenie ako výluka (druh, koľaj – neuvedená koľaj sa zhoduje s každou, trakčné
    vedenie, rýchlosť, číslo, trať)."""
    if str(c.kind or "") != str(fields.get("kind") or "") or not _track_ok(c, fields.get("track")):
        return False
    if bool(c.electric_only) != bool(fields.get("electric_only")):
        return False
    a, b = c.speed_kmh, fields.get("speed_kmh")
    if (a is None) != (b is None) or (a is not None and float(a) != float(b)):
        return False
    if fields.get("line_ref") and str(c.line_ref or "") != str(fields["line_ref"]):
        return False
    return not fields.get("external_ref") or not c.external_ref or refs_match(c.external_ref, fields["external_ref"])


def _placed_by_number(ev: Optional[ExtractedEvent]) -> bool:
    """Čakajúca udalosť, ktorej úsek doplnil register výluk podľa čísla (staršia verzia agenta; teraz je výluka z registra
    len návrhom vo fronte – :func:`register_candidates`): k výluke ju viaže len číslo a priradí sa až pri potvrdení."""
    if ev is None or ev.status != "navrh" or not isinstance(ev.validation, dict):
        return False
    src = ev.validation.get("field_source")
    return isinstance(src, dict) and src.get("from_station") == "register"


def _waiting_changes(session: Session, c: Closure, ev_id: Optional[int]) -> list[int]:
    """Iné čakajúce zmeny tej istej výluky (ich dočasný záznam „navrh“ odkazuje na výluku ``c``)."""
    ids = session.scalars(select(Closure.source_event_id).where(Closure.proposed_change_of == c.id,
                                                                Closure.status != "zrusene"))
    return sorted({i for i in ids if i and i != ev_id})


def _unsafe_split(session: Session, c: Closure, ev_id: Optional[int], change: bool = False,
                  ending: bool = False) -> str:
    """Prečo výluku ``c`` nemožno bezpečne rozdeliť („“ = možno).

    Zmenu (``change``) nezastaví čakajúca zmena, ktorej úsek doplnil register podľa čísla (e-mail úsek neuvádza):
    jej dočasný záznam je len pre plánovač (vlaky v úseku nedostanú ZELENÚ) a pri potvrdení sa priradí k časti
    výluky, s ktorou sa jej termín prekrýva – novšia zapísaná zmena toho istého dňa ju nahradí, zmena iného dňa sa
    zapíše (ako zmena bez úseku, ktorá dočasný záznam nemá). Zrušenie časti výluky taká zmena zastaví (najprv ju
    vybaví dispečer – inak by staršia zmena zrušený deň znova založila).

    Výluku do odvolania (technický koniec) rozdeliť nemožno – okrem jej ukončenia (``ending``: zrušenie od času T bez
    konca, „výluka vyhlásená do odvolania sa ruší od 25.11. 12:00“): ostane časť pred T s pevným koncom T."""
    if c.until_revoked and not ending:
        return "výluka platí do odvolania (jej koniec je len technický)"
    owner = _pending_owner(session, c, ev_id)
    if owner is not None:
        return f"výluka ešte čaká na potvrdenie udalosti #{owner.id}"
    waiting = _waiting_changes(session, c, ev_id)
    if change:
        waiting = [i for i in waiting if not _placed_by_number(session.get(ExtractedEvent, i))]
    if waiting:
        return "na výluku čaká aj iná zmena (udalosť " + ", ".join(f"#{i}" for i in waiting) + ")"
    return ""


def _partial_move_text(c: Closure, fields: dict) -> str:
    return (f"Zmena môže presúvať len časť viacdennej výluky #{c.id} {c.from_station} – {c.to_station} "
            f"({fmt(c.start_utc)} – {fmt(c.end_utc)}) na {fmt(fields['start_utc'])} – {fmt(fields['end_utc'])} – "
            "pôvodná výluka ostáva v registri bez zmeny a nový termín je ďalšia výluka. Ak sa niektoré dni pôvodnej "
            "výluky rušia, upravte jej termín v registri výluk.")


def _union_text(c: Closure, fields: dict, why: str) -> str:
    return (f"Zmena sa týka len časti výluky #{c.id} {c.from_station} – {c.to_station} "
            f"({_term_desc(c.start_utc, c.end_utc, c.daily_from, c.daily_to)}): nový termín "
            f"{_term_desc(fields['start_utc'], fields['end_utc'], fields.get('daily_from'), fields.get('daily_to'))}. "
            f"Výluku nemožno automaticky rozdeliť, lebo {why} – pôvodná výluka ostáva v registri celá a nový termín "
            "je ďalšia výluka (prísnejší prípad). Ak e-mail niektoré dni či hodiny pôvodnej výluky ruší, upravte jej "
            "termín v registri výluk.")


_SHORTEN = re.compile(r"\b(?:skracuj\w*|skrat(?:i|ia|ime|ena|eny|ene|eni|enu|ila|ili|ilo)|ukonc\w*|skonc\w*|"
                      r"predcasn\w*)\b")


def _shortens(c: Closure, fields: dict, email: Optional[Email]) -> bool:
    """Zmena skracuje či ukončuje CELÚ výluku, nie mení jeden jej deň: nový termín má to isté obmedzenie a denné okno,
    trvá viac dní (okien), začína so začiatkom výluky alebo končí s jej koncom a e-mail výluku výslovne skracuje či
    ukončuje („sa skracuje“, „ukončí sa“, „predčasne“). Výluke do odvolania dáva nový termín s rovnakým začiatkom
    koniec aj bez týchto slov (pri dennom okne len pre viac dní)."""
    if fields.get("until_revoked") or not _same_fields(c, fields):
        return False
    cs, ce = _utc(c.start_utc), _utc(c.end_utc)
    ns, ne = fields["start_utc"], fields["end_utc"]
    if ns < cs - MATCH_TOLERANCE or ne > ce + MATCH_TOLERANCE:
        return False
    daily = bool(c.daily_from and c.daily_to)
    if daily != bool(fields.get("daily_from") and fields.get("daily_to")):
        return False
    if daily and (_hm(c.daily_from), _hm(c.daily_to)) != (_hm(fields.get("daily_from")), _hm(fields.get("daily_to"))):
        return False
    many = len(_windows(ns, ne, fields.get("daily_from"), fields.get("daily_to")) or []) > 1 if daily else \
        _local_days(ns, ne) > 1
    same_start = abs(ns - cs) <= MATCH_TOLERANCE
    if c.until_revoked:
        return same_start and (many or not daily)
    if not many or not (same_start or abs(ne - ce) <= MATCH_TOLERANCE) or email is None:
        return False
    return bool(_SHORTEN.search(fold((email.subject or "") + "\n" + _unquoted(email.body_text or ""))))


# zámer zmeny výluky (``data["zamer"]`` z pravidiel): presun, skrátenie a zmena času nahrádzajú termín (s celým novým
# termínom), doplnenie pridáva termíny, predĺženie len posúva koniec; neznámy zámer sa zapíše prídavne
_REPLACING_INTENTS = ("presun", "skratenie", "zmena_casu")
_KNOWN_INTENTS = _REPLACING_INTENTS + ("doplnenie", "predlzenie")
_INTENT_LABELS = {"presun": "presun", "skratenie": "skrátenie", "zmena_casu": "zmena času", "doplnenie": "doplnenie",
                  "predlzenie": "predĺženie"}


def _intent(d: Optional[dict]) -> Optional[str]:
    """Zámer zmeny výluky z dát udalosti (``zamer``): None – udalosť ho neuvádza (staršie udalosti)."""
    raw = d.get("zamer") if isinstance(d, dict) else None
    if raw is None or not str(raw).strip():
        return None
    return re.sub(r"[\s\-]+", "_", fold(str(raw)).strip())


def _contains(c: Closure, fields: dict) -> bool:
    """Výluka ``c`` už obsahuje celý nový termín (všetky jeho okná) s tým istým obmedzením."""
    if not _same_fields(c, fields):
        return False
    wins = _windows(c.start_utc, c.end_utc, c.daily_from, c.daily_to)
    new = _windows(fields["start_utc"], fields["end_utc"], fields.get("daily_from"), fields.get("daily_to"))
    if not wins or not new:
        return False
    return all(any(w[0] <= n[0] + timedelta(minutes=1) and n[1] <= w[1] + timedelta(minutes=1) for w in wins)
               for n in new)


_START_FROM_REGISTER = ("predlzenie", "skratenie")


def _register_start(session: Session, kind: str, d: dict, email: Optional[Email], notes: list,
                    ev_id: Optional[int] = None) -> dict:
    """Dáta zmeny výluky so začiatkom podľa registra, keď ho e-mail neuvádza: predĺženie či skrátenie („výluka … sa
    predĺži do 29.11.2026“, „skončí už 25.11.“ – pravidlá: začiatok ostáva podľa registra) s úsekom a koncom, ktoré sa
    jednoznačne priradí k výluke v registri (číslo a úsek, alebo úsek). Bez začiatku by bola zmena blokovaná („chýba
    začiatok“), nezapísala by sa ani po potvrdení a dni predĺženia by v registri chýbali.

    * Začiatok je začiatok výluky; predĺženie času jedného dňa vnútri výluky („dňa 25.11. sa predlžuje do 16:00“) začína
      oknom výluky v ten deň.
    * Denná výluka (okno 08:00 – 14:00) a zmena bez denného okna: okno ostáva podľa výluky („v rovnakom čase“), ak koniec
      je len dátum (koniec dňa – výluka končí koncom okna, ktoré v posledný deň začína) alebo leží v okne – inak by sa
      z dennej výluky stala súvislá výluka cez noci (všetky vlaky dni a noci stoja). Koniec mimo okna („predĺži sa do
      29.11. 18:00“) okno neprevezme: predĺženie potom začína koncom výluky (súvislý čas len od konca výluky – prísnejší
      prípad, nič sa nestratí).

    Úsek z registra sa nedopĺňa nikdy (udalosť bez úseku ostáva bez miesta – úsek vyberie dispečer vo fronte, potom sa
    začiatok doplní pri potvrdení; ``ev_id`` – udalosť, ktorej záznamy sa nehľadajú)."""
    if kind != "vyluka_zmena" or not isinstance(d, dict) or d.get("start") or not d.get("from_station"):
        return d
    zamer = _intent(d)
    end = parse_utc(d.get("end"))
    if zamer not in _START_FROM_REGISTER or end is None:
        return d
    try:
        target, how = _find_closure(session, ev_id, kind, d, None, loose=True, skip_own=ev_id is not None,
                                    issuer=mail_issuer(email))
    except ApplyError:
        return d
    if target is None or target.start_utc is None or target.end_utc is None:
        return d
    cs, ce = _utc(target.start_utc), _utc(target.end_utc)
    out = dict(d)
    start = cs
    window = ""
    if zamer == "predlzenie" and end <= ce:
        day = local(end - timedelta(microseconds=1)).date()
        wins = [w for w in _windows(cs, ce, target.daily_from, target.daily_to) or []
                if w[0] < end and local(w[0]).date() == day]
        if wins:
            start = wins[-1][0]
    elif target.daily_from and target.daily_to and not (d.get("daily_from") or d.get("daily_to")):
        # denná výluka, zmena bez denného okna
        wins = [w for w in _windows(cs, end + timedelta(days=1), target.daily_from, target.daily_to) or []
                if w[0] < end]
        last = wins[-1] if wins else None
        if last is not None and (_local_midnight(end) or last[0] <= end <= last[1]):
            out["daily_from"], out["daily_to"] = target.daily_from, target.daily_to
            if _local_midnight(end) and last[1] != end:
                out["end"] = last[1].isoformat()
                end = last[1]
            window = f", denne {target.daily_from} – {target.daily_to} ako výluka, koniec {fmt(end)}"
        elif zamer == "predlzenie":
            start = ce
    if start >= end:
        return d
    out["start"] = start.isoformat()
    notes.append(f"Začiatok zmeny e-mail neuvádza – podľa výluky #{target.id} {target.from_station} – "
                 f"{target.to_station} v registri ({how}): {fmt(start)}{window}. Overte termín.")
    return out


def _extension_plan(c: Closure, fields: dict, why: str) -> _Plan:
    """Predĺženie výluky ``c`` (koniec sa len posúva, začiatok a dni výluky ostávajú): „predlzit“ – koniec výluky sa
    posunie na koniec nového termínu (pri zmene do odvolania aj príznak); keď nový termín výluka ani po posune
    neobsahuje (iné denné okno, skorší začiatok, iné obmedzenie), zapíše sa aj ako ďalšia výluka (``parts`` = [True]).
    Nový termín, ktorý výluka obsahuje, register nezmení („bez_zmeny“). Predĺženie nikdy nič neodstráni."""
    cs, ce = _utc(c.start_utc), _utc(c.end_utc)
    ns, ne = fields["start_utc"], fields["end_utc"]
    sec = f"#{c.id} {c.from_station} – {c.to_station}"
    whole = _term_desc(cs, ce, c.daily_from, c.daily_to)
    new = _term_desc(ns, ne, fields.get("daily_from"), fields.get("daily_to"))
    open_end = bool(fields.get("until_revoked"))
    end = max(ce, ne)
    if end <= ce + MATCH_TOLERANCE and not (open_end and not c.until_revoked):
        if _contains(c, fields):
            return _Plan("bez_zmeny", f"{why}: výluka {sec} ({whole}) už obsahuje nový termín {new} – register sa "
                                      "nezmení (predĺženie výluku neskracuje).")
        inside = cs - MATCH_TOLERANCE <= ns and ne <= ce + MATCH_TOLERANCE
        return _Plan("pridat", _additive_text(c, fields, f"{why} – " + (
            "predĺženie času v rámci obdobia výluky (napr. jedného dňa)" if inside else
            "nový termín výluku nepredlžuje")))
    trial = Closure(kind=c.kind, track=c.track, electric_only=c.electric_only, speed_kmh=c.speed_kmh,
                    line_ref=c.line_ref, external_ref=c.external_ref, start_utc=cs, end_utc=end,
                    daily_from=c.daily_from, daily_to=c.daily_to)
    extra = not _contains(trial, fields)
    till = "do odvolania" if open_end else f"do {fmt(end)}"
    text = (f"{why}: výluka {sec} ({whole}) sa predĺži {till} – začiatok a dni výluky ostávajú, nič sa neodstráni"
            + (f"; nový termín {new} sa zapíše aj ako ďalšia výluka (iný čas či obmedzenie)." if extra else "."))
    return _Plan("predlzit", text, [extra])


def _continuation(c: Closure, fields: dict) -> bool:
    """Zmena do odvolania, ktorá začína koncom výluky („ROV … sa mení: od 27.11. 18:00 hod. do odvolania“) – výluka
    pokračuje (predĺženie), nie presun: pôvodné dni výluky ostávajú."""
    return bool(fields.get("until_revoked")) and not c.until_revoked and \
        abs(fields["start_utc"] - _utc(c.end_utc)) <= MATCH_TOLERANCE


_RANGE_RE = re.compile(r"(?<![\d.])(\d{1,2})\.\s?(?:(\d{1,2})\.)?\s?(?:(\d{4})\s*)?(?:-+|–|—|az|do)\s*"
                       r"(\d{1,2})\.\s?(\d{1,2})\.(?:\s?(\d{4}))?")


def _named_days(email: Optional[Email], c: Closure) -> set:
    """Dni výluky ``c`` (v jej období), ktoré e-mail zmeny vymenúva (dátumy aj rozsahy „23. – 27.11.“, „od 23.11. do
    27.11.“) – vo vlastnom texte a predmete."""
    if email is None:
        return set()
    first, last = _closure_days(c)
    out: set = set()
    for text in (str(email.subject or ""), _unquoted(email.body_text or "")):
        f = fold(text)
        for m in _RANGE_RE.finditer(f):
            d1, m1, y1, d2, m2, y2 = m.groups()
            year = int(y2 or y1 or last.year)
            try:
                a = date(int(y1 or year), int(m1 or m2), int(d1))
                b = date(year, int(m2), int(d2))
            except ValueError:
                continue
            if a <= b and (b - a).days <= 62:
                out |= {a + timedelta(days=i) for i in range((b - a).days + 1)}
        for d_, m_, y_ in _DATE_RE.findall(f):
            try:
                out.add(date(int(y_) if y_ else last.year, int(m_), int(d_)))
            except ValueError:
                continue
    return {x for x in out if first <= x <= last}


def _change_plan(session: Session, c: Closure, fields: dict, ev_id: Optional[int],
                 email: Optional[Email] = None, zamer: Optional[str] = None) -> Optional[_Plan]:
    """Ako zapísať zmenu výluky ``c`` podľa zámeru zmeny (``zamer`` – :func:`_intent`). Zmena nikdy neodstráni čas
    výluky v registri, okrem výslovného presunu, skrátenia či zmeny času (``presun``, ``skratenie``, ``zmena_casu``)
    s celým novým termínom:

    * presun, skrátenie, zmena času s celým novým termínom – termín výluky sa nahradí (časť viacdennej výluky sa
      rozdelí – :func:`_term_change_plan`); bez celého termínu (do odvolania) – prídavne;
    * doplnenie – nový termín je ďalšia výluka („pridat“), pôvodná ostáva celá;
    * predĺženie – koniec výluky sa len posunie („predlzit“, :func:`_extension_plan`);
    * neznámy zámer – prídavne („pridat“) s vysvetlením vo fronte;
    * bez zámeru (staršie udalosti) – doterajšie správanie (:func:`_term_change_plan`), ale zmena do odvolania od
      konca výluky ju predĺži a viacdenná výluka sa nikdy neskráti na menej dní, ako e-mail vymenúva.

    None – zmena celej výluky (nahradí termín)."""
    if c is None or c.start_utc is None or c.end_utc is None:
        return None
    label = _INTENT_LABELS.get(zamer or "", zamer or "")
    if zamer == "doplnenie":
        if _contains(c, fields):
            return _Plan("bez_zmeny", f"Doplnenie termínu: výluka #{c.id} {c.from_station} – {c.to_station} už "
                                      "obsahuje nový termín – register sa nezmení.")
        return _Plan("pridat", _additive_text(c, fields, "E-mail termín výluky dopĺňa (doplnenie), nepresúva ho"))
    if zamer == "predlzenie" or (zamer not in _REPLACING_INTENTS and _continuation(c, fields)):
        return _extension_plan(c, fields, "Predĺženie výluky" if zamer == "predlzenie" else
                               "Zmena do odvolania začína koncom výluky (pokračovanie, nie presun)")
    if zamer is None and _says_extension(email) and fields["end_utc"] > _utc(c.end_utc) + MATCH_TOLERANCE:
        # udalosť bez zámeru, e-mail výluku predlžuje („sa predĺži do 29.11.“) a nový termín končí po výluke: koniec sa
        # len posunie – nahradenie termínu by stratilo dni medzi koncom výluky a novým termínom (vlak by dostal ZELENÚ)
        return _extension_plan(c, fields, "E-mail výluku predlžuje (predĺženie, nie presun)")
    if zamer is not None and zamer not in _KNOWN_INTENTS:
        return _Plan("pridat", _additive_text(c, fields, f"Zámer zmeny „{label}“ nie je známy – overte ho podľa "
                                                         "e-mailu"))
    if zamer in _REPLACING_INTENTS and fields.get("until_revoked"):
        return _Plan("pridat", _additive_text(c, fields, f"Zmena ({label}) neuvádza celý nový termín (chýba koniec)"))
    plan = _term_change_plan(session, c, fields, ev_id, email)
    if plan is None and zamer is None and _local_days(_utc(c.start_utc), _utc(c.end_utc)) > 1:
        # staršia udalosť bez zámeru: viacdenná výluka sa nikdy neskráti na menej dní, ako e-mail vymenúva
        named = _named_days(email, c)
        new_days = _local_days(fields["start_utc"], fields["end_utc"])
        if len(named) > new_days:
            return _Plan("pridat", _additive_text(c, fields, f"E-mail uvádza {len(named)} dní výluky, nový termín má "
                                                             f"len {new_days} – výluka sa neskráti"))
    return plan


def _additive_text(c: Closure, fields: dict, why: str) -> str:
    new = _term_desc(fields["start_utc"], fields["end_utc"], fields.get("daily_from"), fields.get("daily_to"))
    return (f"{why}: pôvodná výluka #{c.id} {c.from_station} – {c.to_station} "
            f"({_term_desc(c.start_utc, c.end_utc, c.daily_from, c.daily_to)}) ostáva v registri celá a nový termín "
            f"{new} sa zapíše ako ďalšia výluka (prísnejší prípad). Ak e-mail niektoré dni či hodiny pôvodnej výluky "
            "ruší, upravte jej termín v registri výluk.")


def _term_change_plan(session: Session, c: Closure, fields: dict, ev_id: Optional[int],
                      email: Optional[Email] = None) -> Optional[_Plan]:
    """Zmena, ktorá sa týka len časti výluky ``c`` – nový termín zasahuje menej dní ako viacdenná výluka (napr.
    „dňa 24.11. od 08:00 do 18:00“ pri výluke 23. – 27.11. denne 08:00 – 14:00). None – zmena celej výluky
    (presun, predĺženie, skrátenie či iný čas jednodňovej výluky, iný čas všetkých dní, skrátenie či ukončenie celej
    viacdennej výluky – :func:`_shortens`).

    Denná výluka sa rozdelí („rozdelit“): nový termín nahradí len okná výluky, s ktorými sa prekrýva (inak okno
    v jeho deň); ostatné dni ostanú ako samostatné výluky – zmena nikdy neodstráni ani nepresunie dni, ktoré e-mail
    neuvádza. Keď rozdelenie nie je bezpečné alebo zmysluplné (súvislá viacdenná výluka, nový termín mimo dní
    výluky, výluka do odvolania, čakajúca iná zmena), pôvodná výluka ostane celá a nový termín bude ďalšia výluka
    („pridat“). Nový termín, ktorý výluka už presne obsahuje, register nezmení („bez_zmeny“)."""
    if c is None or c.start_utc is None or c.end_utc is None:
        return None
    cs, ce = _utc(c.start_utc), _utc(c.end_utc)
    ns, ne = fields["start_utc"], fields["end_utc"]
    if _local_days(ns, ne) >= _local_days(cs, ce):
        return None
    wins = _windows(cs, ce, c.daily_from, c.daily_to)
    daily = bool(c.daily_from and c.daily_to) and wins is not None and len(wins) > 1
    if not daily and ce - cs <= timedelta(hours=24):
        return None                      # jednodňová (aj nočná) výluka – zmena jej termínu mení celú výluku
    if _shortens(c, fields, email):
        return None                      # „výluka sa skracuje, bude 23. – 25.11.“, koniec výluky do odvolania
    sec = f"#{c.id} {c.from_station} – {c.to_station}"
    new = _term_desc(ns, ne, fields.get("daily_from"), fields.get("daily_to"))
    same = _same_fields(c, fields)
    if not (ns < ce and ne > cs):
        return _Plan("pridat", _partial_move_text(c, fields))
    if not daily:
        if same and cs <= ns and ne <= ce:
            return _Plan("bez_zmeny", f"Mení len časť výluky {sec} ({_term_desc(cs, ce)}): výluka už obsahuje celý "
                                      f"nový termín {new} – register sa nezmení (súvislú výluku nemožno po dňoch "
                                      "rozdeliť). "
                                      "Ak e-mail výluku skracuje, upravte jej termín v registri výluk.")
        return _Plan("pridat", _union_text(c, fields, "výluka je súvislá, nie po dňoch, a deň z nej nemožno vyčleniť"))
    nwins = _windows(ns, ne, fields.get("daily_from"), fields.get("daily_to")) or [(ns, ne)]
    if any(sum(1 for w in wins if w[0] < n1 and n0 < w[1]) > 1 for n0, n1 in nwins):
        # jeden nový termín zasahuje viac okien výluky (napr. celý deň pri nočnej výluke) – nie je jasné, ktoré okno
        # nahrádza; nahradenie oboch by stratilo ich časti mimo nového termínu
        return _Plan("pridat", _union_text(c, fields, "nový termín zasahuje časti viacerých okien výluky a nie je "
                                                      "jasné, ktoré nahrádza"))
    named = [i for i, w in enumerate(wins) if any(w[0] < n1 and n0 < w[1] for n0, n1 in nwins)]
    if not named:
        day = local(ns).date()
        named = [i for i, w in enumerate(wins) if local(w[0]).date() == day]
    if not named:
        return _Plan("pridat", _partial_move_text(c, fields))
    if len(named) == len(wins):
        return _Plan("pridat", _union_text(c, fields, "nový termín zasahuje všetky okná výluky, ale menej dní"))
    why = _unsafe_split(session, c, ev_id, change=True)
    if why:
        return _Plan("pridat", _union_text(c, fields, why))
    old = "; ".join(_term_desc(*wins[i]) for i in named)
    if same and len(nwins) == len(named) and all(
            abs(n[0] - wins[i][0]) <= timedelta(minutes=1) and abs(n[1] - wins[i][1]) <= timedelta(minutes=1)
            for n, i in zip(nwins, named)):
        whole = _term_desc(cs, ce, c.daily_from, c.daily_to)
        return _Plan("bez_zmeny", f"Mení len časť výluky {sec} ({whole}): výluka už obsahuje nový termín {new} – "
                                  "register sa nezmení. Ak e-mail presúva iný deň výluky (napr. „dňa … sa presúva "
                                  "na …“) alebo výluku skracuje, pôvodný termín ostáva v registri – upravte ho "
                                  "v registri výluk.")
    keep = [[] if i in named else [w] for i, w in enumerate(wins)]
    parts = _compact(wins, keep)
    whole = _term_desc(cs, ce, c.daily_from, c.daily_to)
    return _Plan("rozdelit", f"Mení len časť výluky {sec} ({whole}): {old} nahradí nový termín {new}. Po potvrdení "
                             f"sa výluka rozdelí – ostatné dni ostanú bez zmeny ({_parts_text(c, parts)}), e-mail ich "
                             "neuvádza.", parts)


def _local_midnight(dt: datetime) -> bool:
    t = local(dt)
    return (t.hour, t.minute, t.second, t.microsecond) == (0, 0, 0, 0)


def _night(w: tuple[datetime, datetime]) -> bool:
    """Okno cez polnoc najviac na 24 h (noc) – podľa zápisu ŽSR patrí dňu, ktorým začína."""
    return w[1] - w[0] <= timedelta(hours=24) and \
        local(w[0]).date() != local(w[1] - timedelta(microseconds=1)).date()


def _days_text(ps: datetime, pe: datetime, open_end: bool) -> str:
    first, last = local(ps).date(), local(pe - timedelta(microseconds=1)).date()
    if open_end:
        return f"od {first:%d.%m.%Y}"
    return f"deň {first:%d.%m.%Y}" if first == last else f"dni {first:%d.%m.} – {last:%d.%m.%Y}"


def _cancel_plan(session: Session, c: Closure, d: dict, ev_id: Optional[int]) -> Optional[_Plan]:
    """Ako zrušiť výluku ``c`` podľa rušeného termínu z e-mailu (``start``/``end``; bez konca – od začiatku ďalej).
    None – zruší sa celá (zrušenie bez termínu alebo termín pokrýva celú výluku). „rozdelit“ – zruší sa len uvedená
    časť (jeden deň viacdennej výluky, časť dňa či noci), ostatné časti ostanú ako samostatné výluky. „odmietnut“ –
    automaticky nemožno (termín sa s výlukou neprekrýva, zasahuje len časť viacerých okien, výluka do odvolania,
    čakajúca zmena tej istej výluky …); v ``text`` je dôvod a čo má dispečer urobiť.

    Zrušenie celých dní („dňa 25.11.2026 sa ruší“ = 25.11. 00:00 – 26.11. 00:00) noc nikdy nereže o polnoci: noc patrí
    dňu, ktorým začína (ako pri zápise výluky) – zruší sa celá noc, ktorá začína v rušenom dni; noc, ktorá v ňom len
    končí, ostáva celá."""
    start, end = parse_utc(d.get("start")), parse_utc(d.get("end"))
    if (start is None and end is None) or c.start_utc is None or c.end_utc is None:
        return None
    cs, ce = _utc(c.start_utc), _utc(c.end_utc)
    ps = start if start is not None else cs
    open_end = end is None or end <= ps
    pe = max(ce, ps) + timedelta(minutes=1) if open_end else end
    sec = f"#{c.id} {c.from_station} – {c.to_station}"
    days = start is not None and _local_midnight(ps) and (open_end or _local_midnight(pe))
    term = _days_text(ps, pe, open_end) if days else (f"od {fmt(ps)}" if open_end else f"{fmt(ps)} – {fmt(pe)}")
    whole = _term_desc(cs, ce, c.daily_from, c.daily_to)
    wins = _windows(cs, ce, c.daily_from, c.daily_to)
    if wins is None:
        if ps <= cs + SPLIT_TOLERANCE and pe >= ce - SPLIT_TOLERANCE:
            return None
        return _Plan("odmietnut", f"Zrušenie sa týka len časti výluky {sec} ({term}), výluka platí {whole} – jej denné "
                                  "okno sa nedá prečítať, preto ju nemožno automaticky rozdeliť. Upravte termín výluky "
                                  "v registri výluk a túto udalosť potom zamietnite.")
    if not any(w[0] < pe and ps < w[1] for w in wins):
        return _Plan("odmietnut", f"Zrušenie uvádza termín {term}, výluka {sec} platí {whole} – termíny sa "
                                  "neprekrývajú, výluka sa nezruší. Overte, ktorý termín e-mail ruší: opravte termín "
                                  "zrušenia (Upraviť a potvrdiť), alebo udalosť zamietnite.")
    rests: list[list] = []
    gone: list = []
    kept: list = []
    for w in wins:
        if days and _night(w) and w[0] < pe and ps < w[1]:
            if ps <= _utc(_local_at(local(w[0]).date(), (0, 0))) < pe:
                rests.append([])
                gone.append(w)
            else:
                rests.append([w])
                kept.append(w)
        else:
            rests.append(_minus(w, ps, pe))
    touched = [i for i, (w, r) in enumerate(zip(wins, rests)) if r != [w]]
    if not touched:
        night = "; ".join(f"{fmt(a)} – {fmt(b)} patrí k {local(a):%d.%m.}" for a, b in kept[:3])
        return _Plan("odmietnut", f"Zrušenie uvádza {term}, výluka {sec} platí {whole} – v uvedený deň žiadna noc "
                                  f"výluky nezačína (noc patrí dňu, ktorým začína: {night}); výluka sa nezruší. "
                                  "Overte, ktorú noc e-mail ruší: opravte termín zrušenia (Upraviť a potvrdiť), alebo "
                                  "udalosť zamietnite.")
    if sum(1 for i in touched if rests[i]) > 1:
        return _Plan("odmietnut", f"Zrušenie {term} zasahuje len časť viacerých okien výluky {sec} ({whole}) – nie je "
                                  "jasné, ktoré časti sa rušia; výluka sa nezruší. Overte termín zrušenia: opravte ho "
                                  "(Upraviť a potvrdiť), alebo upravte výluku v registri výluk a udalosť zamietnite.")
    parts = _compact(wins, rests)
    if not parts:
        return None
    # výluka do odvolania sa zrušením od času T bez konca ukončí (ostane časť pred T s pevným koncom) – to nie je
    # rozdelenie s technickým koncom; zrušenie jej vnútornej časti ostáva odmietnuté
    ending = bool(c.until_revoked) and start is not None and open_end and all(e <= ps + SPLIT_TOLERANCE
                                                                             for _s, e, _k in parts)
    why = _unsafe_split(session, c, ev_id, ending=ending)
    if why:
        what = ("Najprv vybavte čakajúcu udalosť, potom potvrďte toto zrušenie znova." if "čaká" in why else
                "Upravte termín výluky v registri výluk a túto udalosť potom zamietnite.")
        return _Plan("odmietnut", f"Zrušenie sa týka len časti výluky {sec} ({term}), výluka platí {whole} – celá "
                                  f"výluka sa nezruší a automaticky ju nemožno rozdeliť, lebo {why}. {what}")
    if ending:
        return _Plan("rozdelit", f"Ukončí výluku {sec} vyhlásenú do odvolania (platí od {fmt(cs)}): e-mail ju ruší "
                                 f"{term}. Po potvrdení ostane {_parts_text(c, parts)} – už nie do odvolania.", parts)
    nights = ""
    if gone:
        nights = (" – ruší sa " + ("noc " if len(gone) == 1 else "noci ") +
                  "; ".join(f"{fmt(a)} – {fmt(b)}" for a, b in gone[:5]) + " (noc patrí dňu, ktorým začína)")
    return _Plan("rozdelit", f"Zruší len časť výluky {sec} ({whole}): {term}{nights}. Po potvrdení sa výluka rozdelí – "
                             f"ostane {_parts_text(c, parts)} (e-mail ruší len uvedený termín).", parts)


def _split_closure(session: Session, ev: ExtractedEvent, c: Closure, plan: _Plan, how: str, *,
                   fields: Optional[dict] = None, notes: Iterable[str] = (), drop: Iterable[Closure] = ()) -> dict:
    """Rozdelí výluku ``c``: nedotknuté časti (``plan.parts``) ostanú ako samostatné záznamy s rovnakým číslom, úsekom
    a obmedzením; pri zmene pribudne nový termín (``fields``), pri zrušení uvedená časť zanikne. Prvý záznam v čase
    ponechá číslo záznamu ``c`` (odkazy naň platia ďalej). Všetky časti zapísala udalosť ``ev`` (``source_event_id``);
    nový termín potvrdil dispečer (overený), nedotknuté časti majú stav pôvodnej výluky (nepotvrdená ostáva
    nepotvrdená). Pôvodný stav je v ``before`` (zamietnutie udalosti ho obnoví)."""
    before = _snapshot(c, _CLOSURE_FIELDS)
    status = c.status if c.status in ("navrh", "overene") else "overene"
    base = {k: getattr(c, k) for k in ("kind", "from_station", "to_station", "line_ref", "track", "electric_only",
                                       "speed_kmh", "external_ref")}
    specs = [("povodna" if keep else "skratena", s, e, {**base, "daily_from": c.daily_from if keep else "",
                                                        "daily_to": c.daily_to if keep else ""})
             for s, e, keep in plan.parts]
    if fields is not None:
        vals = {k: v for k, v in fields.items() if k not in ("start_utc", "end_utc", "until_revoked")}
        vals["external_ref"] = vals.get("external_ref") or c.external_ref
        if not vals.get("track") and vals.get("kind") == c.kind:
            vals["track"] = c.track or ""          # zmena koľaj neuvádza – ostáva koľaj pôvodnej výluky
        specs.append(("novy", fields["start_utc"], fields["end_utc"], vals))
    specs.sort(key=lambda x: x[1])
    old_note = c.note or ""
    head = f"Rozdelené podľa udalosti #{ev.id} (e-mail #{ev.email_id}, {how}): {plan.text}"
    rows: list[tuple[str, Closure]] = []
    for i, (tag, s, e, vals) in enumerate(specs):
        row = c if i == 0 else Closure(source="email")
        for k, v in vals.items():
            setattr(row, k, v)
        row.start_utc, row.end_utc = s, e
        row.until_revoked = False
        row.status = "overene" if tag == "novy" else status
        row.source = "email"
        row.source_event_id = ev.id
        row.proposed_change_of = None
        what = {"novy": "nový termín podľa e-mailu", "povodna": "nedotknutá časť pôvodnej výluky"}.get(
            tag, "ostávajúca (skrátená) časť pôvodnej výluky")
        row.note = "\n".join(filter(None, [old_note] + (list(notes) if tag == "novy" else []) +
                                    [f"{head} Tento záznam: {what}."]))
        if i == 0:
            c.version = (c.version or 1) + 1
        else:
            session.add(row)
        rows.append((tag, row))
    new_row = next((r for t, r in rows if t == "novy"), c)
    for x in drop:
        if x is not None and x is not c and x not in [r for _t, r in rows]:
            _carry_note(new_row, x)
            session.delete(x)
    session.flush()
    terms = [{"id": r.id, "start": _utc(r.start_utc).isoformat(), "end": _utc(r.end_utc).isoformat(),
              "daily": f"{r.daily_from}–{r.daily_to}" if r.daily_from else "", "status": r.status,
              "part": t} for t, r in rows]
    return {"entity": "Closure", "id": c.id, "action": "rozdelene", "status": c.status, "matched_by": how,
            "before": before, "parts": [r.id for _t, r in rows if r is not c], "terms": terms, "note": plan.text}


def _unchanged_closure(session: Session, ev: ExtractedEvent, c: Closure, plan: _Plan, how: str,
                       drop: Iterable[Closure] = ()) -> dict:
    """Zmena, ktorej nový termín výluka už obsahuje – register sa nemení (len poznámka); dočasné záznamy udalosti
    sa odstránia."""
    c.note = "\n".join(filter(None, [c.note or "", f"Udalosť #{ev.id} (e-mail #{ev.email_id}, {how}): {plan.text}"]))
    for x in drop:
        if x is not None and x is not c:
            _carry_note(c, x)
            session.delete(x)
    session.flush()
    return {"entity": "Closure", "id": c.id, "action": "bez_zmeny", "status": c.status, "matched_by": how,
            "before": None, "note": plan.text}


def _extend_closure(session: Session, ev: ExtractedEvent, c: Closure, plan: _Plan, how: str, *, fields: dict,
                    notes: Iterable[str] = (), drop: Iterable[Closure] = ()) -> dict:
    """Predĺženie výluky ``c`` (:func:`_extension_plan`): koniec sa posunie (zmena do odvolania – aj príznak „do
    odvolania“), začiatok, dni, denné okno a obmedzenie ostávajú – nič sa neodstráni. Nový termín, ktorý výluka ani po
    posune neobsahuje, sa zapíše aj ako ďalšia výluka (``added``). Pôvodný stav je v ``before`` – zamietnutie udalosti ho
    obnoví a ďalšiu výluku zruší."""
    before = _snapshot(c, _CLOSURE_FIELDS)
    c.end_utc = max(_utc(c.end_utc), fields["end_utc"])
    if fields.get("until_revoked"):
        c.until_revoked = True
    c.status = "overene"
    c.source = "email"
    c.source_event_id = ev.id
    c.version = (c.version or 1) + 1
    c.note = "\n".join(filter(None, [c.note or "", f"Predĺžené podľa udalosti #{ev.id} (e-mail #{ev.email_id}, "
                                                   f"{how}): {plan.text}"]))
    added: list[Closure] = []
    if plan.parts and plan.parts[0]:
        row = Closure(note="\n".join(list(notes) + [f"Nový termín predĺženia výluky #{c.id} (udalosť #{ev.id})."]),
                      status="overene", source="email", source_event_id=ev.id, **fields)
        session.add(row)
        added.append(row)
    for x in drop:
        if x is not None and x is not c and x not in added:
            _carry_note(added[0] if added else c, x)
            session.delete(x)
    session.flush()
    out = {"entity": "Closure", "id": c.id, "action": "aktualizovane", "status": c.status, "matched_by": how,
           "before": before, "note": plan.text}
    if added:
        out["added"] = [r.id for r in added]
    return out


_EXTEND_WORD = re.compile(r"\bpredlz\w*")


def _says_extension(email: Optional[Email]) -> bool:
    """Vlastný text e-mailu (bez citácie) alebo predmet hovorí o predĺžení výluky („predlžuje“, „predĺži“, „predĺženie“)."""
    return email is not None and bool(_EXTEND_WORD.search(
        fold(str(email.subject or "") + "\n" + _unquoted(email.body_text or ""))))


def _pending_extension(fields: dict, c: Closure, d: dict, email: Optional[Email] = None) -> None:
    """Dočasný záznam čakajúceho predĺženia výluky ``c`` pokryje aj čas medzi koncom výluky a novým termínom
    („predlžuje sa do 29.11.“ prečítané len ako 29.11. – 28.11. by do potvrdenia ostal voľný): zámer predĺženie, zmena
    do odvolania od konca výluky a bez zámeru aj e-mail, ktorý výluku predlžuje. Denné okno výluky dostane záznam len
    vtedy, keď nový termín žiadne neuvádza a celý sa doň zmestí – inak súvislý čas (dočasný záznam pokryje radšej viac,
    nikdy menej ako nový termín). Register sa nemení – po potvrdení platí plán zmeny (:func:`_change_plan`)."""
    if c.start_utc is None or c.end_utc is None:
        return
    zamer = _intent(d)
    if zamer in _REPLACING_INTENTS or zamer == "doplnenie":
        return
    said = zamer is None and _says_extension(email)
    if not (zamer == "predlzenie" or _continuation(c, fields) or said):
        return
    ce = _utc(c.end_utc)
    ns, ne = fields["start_utc"], fields["end_utc"]
    if ne <= ce or ns <= ce:
        return
    own = _windows(ns, ne, fields.get("daily_from"), fields.get("daily_to"))
    fields["start_utc"] = ce
    if c.daily_from and c.daily_to and own and not (fields.get("daily_from") or fields.get("daily_to")):
        wins = _windows(ce, ne, c.daily_from, c.daily_to)
        if wins and all(any(w[0] <= n[0] and n[1] <= w[1] for w in wins) for n in own):
            fields["daily_from"], fields["daily_to"] = c.daily_from, c.daily_to


def _inherit_track(fields: dict, c: Closure) -> None:
    """Zmena výluky koľaje, ktorá koľaj neuvádza („výluka podľa ROV … sa odkladá“, druh opravený dispečerom na výluku
    koľaje): koľaj ostáva koľajou pôvodnej výluky – inak by sa stratila a výluky oboch koľají by sa nespojili do úplnej
    výluky (vlak by dostal ZELENÚ cez úsek s oboma koľajami vylúčenými)."""
    if not str(fields.get("track") or "").strip() and fields.get("kind") == c.kind and str(c.track or "").strip():
        fields["track"] = c.track


def _undo_split(session: Session, ev: ExtractedEvent, applied: dict) -> str:
    """Zamietnutá udalosť výluku rozdelila: pôvodná výluka sa obnoví a jej ďalšie časti zrušia – len ak všetky časti
    ešte patria tejto udalosti (inak ostáva register bez zmeny a dispečer dostane upozornenie)."""
    c = session.get(Closure, applied.get("id"))
    parts = [p for p in (session.get(Closure, i) for i in applied.get("parts") or []) if p is not None]
    if c is None:
        return "rozdelená výluka sa v registri nenašla – nie je čo vrátiť"
    foreign = [x for x in [c] + parts if x.source_event_id != ev.id]
    if foreign:
        return "rozdelenie výluky sa nevrátilo – " + "; ".join(_foreign_owner("Closure", x, ev) for x in foreign)
    snap = _effective_before(session, applied.get("before"), "Closure", c.id)
    _restore(c, snap if isinstance(snap, dict) else applied.get("before") or {})
    for p in parts:
        p.status = "zrusene"
        p.note = "\n".join(filter(None, [p.note or "", f"Zamietnuté dispečerom (udalosť #{ev.id}) – časť rozdelenej "
                                                       f"výluky #{c.id} zlúčená späť do nej."]))
    session.flush()
    if c.status != "zrusene":
        _repin_older_changes(session, c)
    return f"rozdelenie výluky vrátené – výluka #{c.id} má pôvodný termín"


def _plural(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {one if n == 1 else few if 2 <= n <= 4 else many}"


def _no_target(session: Session, ev_id: Optional[int], kind: str, d: dict,
               issuer: Optional[str]) -> tuple[str, bool]:
    """Prečo sa výluka, ktorej sa zmena či zrušenie týka, v registri nenašla. Vráti (text pre dispečera, či je
    konkrétny – nejednoznačné číslo, výluka inej OR, iný úsek; konkrétny text hovorí aj, čo robiť)."""
    cancel = kind == "vyluka_zrusenie"
    generic = (("Výluka na zrušenie" if cancel else "Výluka, ktorej sa zmena týka,") +
               " sa v registri nenašla (podľa čísla ani úseku).", False)
    ref, a, b = d.get("external_ref"), d.get("from_station"), d.get("to_station")
    if not ref:
        return generic
    fam = _event_family(kind, d)
    same = [c for c in _active_closures(session, ev_id) if c.external_ref and refs_match(c.external_ref, ref)
            and (fam is None or _closure_family(c) == fam)]
    if not same:
        return generic
    act = "zrušenie" if cancel else "zmena"
    on_sec = [c for c in same if not a or _same_section(c, a, b)]
    foreign: list[Closure] = []
    if not a and issuer:
        foreign = [c for c in on_sec if (_closure_issuer(session, c) or issuer) != issuer]
        on_sec = [c for c in on_sec if c not in foreign]
    if len(on_sec) > 1:
        sec = f" v úseku {a} – {b or a}" if a else ""
        listed = "; ".join(f"#{c.id} {c.from_station} – {c.to_station} {_term_desc(c.start_utc, c.end_utc)}"
                           for c in on_sec[:6])
        verb = "sú" if 2 <= len(on_sec) <= 4 else "je"
        return (f"{act.capitalize()} nemožno priradiť jednoznačne – v registri {verb} "
                f"{_plural(len(on_sec), 'výluka', 'výluky', 'výluk')} s číslom {ref}{sec} ({listed}). Ak sa týka "
                "všetkých, upravte ich v registri výluk a udalosť zamietnite; ak len jednej, doplňte termín či úsek "
                "(Upraviť a potvrdiť).", True)
    if foreign and not on_sec:
        c = foreign[0]
        theirs = _OR_LABELS.get(_closure_issuer(session, c) or "", "inej OR")
        return (f"Výluka s číslom {ref} v registri (#{c.id} {c.from_station} – {c.to_station}) je od {theirs}, e-mail "
                f"je od {_OR_LABELS.get(issuer or '', 'inej OR')} – číslo ROV má každá OR vlastné, nejde o tú istú "
                f"výluku. Overte, ktorej výluky sa {act} týka: doplňte úsek (Upraviť a potvrdiť), alebo udalosť "
                "zamietnite.", True)
    if a:
        others = [c for c in same if not _same_section(c, a, b)]
        if others:
            c = others[0]
            return (f"Výluka s číslom {ref} je v registri len na inom úseku (#{c.id} {c.from_station} – "
                    f"{c.to_station}), {act} uvádza úsek {a} – {b or a} – automaticky sa nezapíše. Ak ide o časť "
                    "úseku tejto výluky, upravte jej úsek v registri výluk (napr. ju rozdeľte) a udalosť zamietnite; "
                    "inak opravte úsek (Upraviť a potvrdiť).", True)
    return generic


# ---------------------------------------------------------------------- úsek, ktorý podľa e-mailu ostáva (R5C1)
# Poistka za pravidlami: e-mail o viacúsekovom ROV ruší jeden úsek a o inom píše, že výluka v ňom ostáva – zrušenie
# toho úseku (napr. chybne vytiahnuté z e-mailu) sa nezapíše. Posudzuje sa stavba viet (ktorý úsek je vo vete či jej
# časti so zrušením, ktorý vo vete, podľa ktorej výluka ostáva), nie len slovník: zápor zrušenia („nie je zrušená“,
# „neruší sa“) je „ostáva“; veta o inom obmedzení (pomalá jazda, POTR, náhradná doprava), o inom ROV či o inom dni
# výluky sa nepočíta; „ostáva“ s časom („od 08:00 do 10:00 platí“) bráni len zrušeniu toho času.
_CANCEL_NEG = re.compile(r"(?:\bnie\s+(?:je|su|bola|boli|bolo|bude|budu)|\bne(?:bol[aoi]?|bude|budu))\s+(?:\w+\s+)?"
                         r"(?:zrusen|odvolan|stornovan)\w*|\bne(?:zrus\w*|rus(?:i|ia|ime|ite)\b|odvolav\w*|storn\w*)"
                         r"|\b(?:zrus\w*|rusenie)\b.*\b(?:nevztahuj|netyk)\w*|\b(?:nevztahuj|netyk)\w*.*\bzrus\w*")
_CANCEL_WORDS = re.compile(r"\b(?:rus(?:i|ia|ime|ite)\b|zrus\w*|nekona\w*|odpada\w*|storn\w*|odvolav\w*|"
                           r"odvolan[aeyi]\b|neuskutocn\w*|nevykona\w*|nerealiz\w*|neplat(?:i|ia)\b|"
                           r"nebud\w*(?:\s+\S+){0,3}?\s+(?:vykona|realizova|uskutocn|kona)\w*)")
_STAY_WORDS = re.compile(r"\b(?:ostava\w*|ostan\w*|zostava\w*|zostan\w*|bez\s+zmeny|nemen\w*|nezmenen\w*|"
                         r"uskutocn\w*|vykona\w*|kona\w*|realiz\w*|(?:po)?trva\w*|pretrvava\w*|prebehn\w*|"
                         r"prebieha\w*|pokracuj\w*|pokracova\w*|podla\s+planu|nadalej|v\s+platnosti|ponecha\w*|"
                         r"zachova\w*|uzavret\w*|uzatvoren\w*|zatvoren\w*|nedotknut\w*|nie\s+(?:je|su)\s+dotknut\w*|"
                         r"plat(?:i|ia)\b)")
_EXCEPT = re.compile(r"\b(?:okrem|s\s+vynimkou|vynimajuc)\b")
_RELATIVE = re.compile(r"\s*(?:ktor\w*|kde|kedy|ako)\b")             # vedľajšia veta výluku len opisuje
_REMAINDER = re.compile(r"\b(?:ostatn\w*|zvysn\w*|zvysok\w*|dalsi\w*|inak)\b")   # „v ostatných dňoch platí“
_OTHER_RESTRICTION = re.compile(r"\b(?:pomal\w*|rychlost\w*|potr|nahradn\w*|autobus\w*|odklon\w*|obchadz\w*)\b")
_CLOSURE_WORDS = re.compile(r"\b(?:vyluk\w*|uzavret\w*|uzavre\w*|uzatvor\w*|zatvor\w*|vypnut\w*|s?rov|zrus\w*)\b")
_PLACE_WORDS = re.compile(r"\b(?:usek\w*|zst|stanic\w*|medzi|dopravn\w*|obvod\w*|trat\w*)\b")
_LIST_START = re.compile(r"(?:[-•*–]|\d{1,2}[.)]|[a-z]\))\s")
_UPPER = "A-ZÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ"
_SECTION_PAIR = re.compile(rf"[{_UPPER}][\w.]*\s*[-–—]\s*[{_UPPER}]")    # „Senica – Kúty“ (v pôvodnom texte)


_REF_RE = re.compile(r"(?<![\d/])(\d{1,6}/\d{4})(?![\d/])")
_DATE_RE = re.compile(r"(?<![\d.])(\d{1,2})\.\s?(\d{1,2})\.(?:\s?(\d{4}))?")
_TIME_RE = re.compile(r"(?<![\d.:])([01]?\d|2[0-4])(?::([0-5]\d)|\.([0-5]\d)(?=\s*(?:hod|h\b|-|do\b|az\b|[,;)]|$)))")
# skratky, za ktorými bodka vetu nekončí („žst. Trnava“, „č. 15“, „hl. st.“); „hod.“ a „min.“ vetu končiť môžu
_NO_END = {"c", "cis", "zst", "st", "hl", "os", "zast", "napr", "resp", "tzv", "tj", "cca", "ing", "mgr", "ul", "zel",
           "odb", "vyh", "nakl", "obr", "tab", "kol", "tr", "km", "sp", "spol", "pozn"}
_LIST_ITEM = re.compile(r"[ \t]*(?:\n|[-•*–]\s|\d{1,2}[.)]\s|[a-z]\)\s)")


def _word_re(word: str) -> str:
    """Slovo názvu dopravne aj v inom páde („Trnava“ – „Trnavou“, „Ružomberok“ – „Ružomberku“, „hl.“ – „hlavná“)."""
    if word == "hl":
        return r"hl(?:\.|avn\w*)?"
    if word == "st":
        return r"st(?:\.|anic\w*)?"
    if len(word) <= 3:
        return re.escape(word) + r"\.?"
    stems = {word}
    if word[-1] in "aeiouy":
        stems.add(word[:-1])
    m = re.match(r"^(.*[^aeiouy])(?:ie|e|o)([kcnlr])$", word)        # vypadávajúca samohláska
    if m:
        stems.add(m.group(1) + m.group(2))
    return "(?:" + "|".join(re.escape(s) for s in sorted(stems, key=len, reverse=True)) + r")\w{0,4}"


def _name_re(name: Any) -> Optional[re.Pattern]:
    """Názov dopravne v texte (bez diakritiky): aj skloňovaný, so skratkami s bodkou či bez nej („hl. st.“, „hl.st.“)
    a s pomlčkou v názve („Poprad-Tatry“)."""
    words = [w.strip(".") for w in re.split(r"[\s\-]+", fold(str(name or ""))) if w.strip(".")]
    if not words:
        return None
    return re.compile(r"(?<!\w)" + r"[\s.\-]*".join(_word_re(w) for w in words) + r"(?!\w)")


def _sentences(text: str) -> list[tuple[str, str]]:
    """Vety textu ako (pôvodný text, bez diakritiky malými písmenami) – oba s rovnakou dĺžkou a medzerami.

    Bodka končí vetu, len ak za ňou nasleduje veľké písmeno či koniec riadku a nie je za skratkou („žst. Trnava“,
    „č. 15“, „hl. st.“) – „hod.“ či „min.“ na konci vety ju končia, bodka v dátume, čase či čísle nie. Prázdny riadok,
    odrážka zoznamu a riadok končiaci „;“ alebo „:“ začínajú novú vetu; zalomený riadok vetu nekončí."""
    t = clean_text(text)
    f = fold(t)
    out: list[tuple[str, str]] = []
    start, n = 0, len(t)
    for i, ch in enumerate(t):
        end = False
        if ch in "!?":
            end = True
        elif ch == ".":
            j = i + 1
            while j < n and t[j] in " \t":
                j += 1
            nxt = t[j] if j < n else ""
            if nxt in ("", "\n", "\r") or nxt.isupper():
                m = re.search(r"(\w+)$", f[start:i])
                word = m.group(1) if m else ""
                end = not (word in _NO_END or (len(word) == 1 and word.isalpha()))
        elif ch == "\n":
            prev = t[start:i].rstrip()
            end = bool(_LIST_ITEM.match(t, i + 1)) or (bool(prev) and prev[-1] in ";:")
        if end:
            out.append((t[start:i + 1], f[start:i + 1]))
            start = i + 1
    out.append((t[start:], f[start:]))
    return [(" ".join(o.split()), " ".join(x.split())) for o, x in out if x.strip()]


def _clause_parts(orig: str, folded: str) -> list[tuple[str, str]]:
    """Časti vety (pôvodný text, bez diakritiky): oddeľuje ich čiarka, bodkočiarka, dvojbodka (nie v čase „07:00“ ani
    v čísle „12,5“) a spojka pred novou vetou („… sa ruší a výluka v úseku … ostáva“); nie vnútri zátvorky."""
    cuts: list[tuple[int, int]] = []
    for m in re.finditer(r"[,;:]|\s(?:a|ale|avsak|vsak|pricom|zatial\s+co)\s+(?=(?:v|na|pre)\s+usek|usek|vyluk|"
                         r"v\s+case|od\s+\d|dna\s+\d|rusi|zrus)", folded):
        i = m.start()
        if m.group(0) in ",:" and 0 < i < len(folded) - 1 and folded[i - 1].isdigit() and folded[i + 1].isdigit():
            continue
        if folded.count("(", 0, i) > folded.count(")", 0, i):
            continue
        cuts.append((m.start(), m.end()))
    parts, last = [], 0
    for a, b in cuts:
        parts.append((orig[last:a], folded[last:a]))
        last = b
    parts.append((orig[last:], folded[last:]))
    return [(o, x) for o, x in parts if x.strip()]


def _predicate(folded: str) -> Optional[str]:
    """„cancel“ – časť vety výluku ruší, „stay“ – výluka ostáva (aj zápor zrušenia), None – nič z toho."""
    if _CANCEL_NEG.search(folded):
        return "stay"
    if _CANCEL_WORDS.search(folded):
        return "cancel"
    if _STAY_WORDS.search(folded):
        return "stay"
    return None


def _closure_days(c: Closure) -> tuple[date, date]:
    cs, ce = _utc(c.start_utc), _utc(c.end_utc)
    return local(cs).date(), local(ce - timedelta(microseconds=1) if ce > cs else cs).date()


def _text_days(folded: str, c: Closure) -> list[date]:
    """Dátumy v časti vety (rok chýba – rok výluky)."""
    first, last = _closure_days(c)
    out = []
    for d_, m_, y_ in _DATE_RE.findall(folded):
        years = [int(y_)] if y_ else sorted({first.year, last.year})
        for y in years:
            try:
                day = date(y, int(m_), int(d_))
            except ValueError:
                continue
            if not y_ and len(years) > 1 and not first <= day <= last:
                continue
            out.append(day)
            break
    return out


def _foreign(folded: str, c: Closure) -> bool:
    """Časť vety hovorí o inej výluke: iné číslo ROV ako výluka ``c`` alebo len dni mimo jej termínu."""
    refs = _REF_RE.findall(folded)
    own = ref_key(c.external_ref) if c.external_ref else ""
    if own and refs and own not in refs:
        return True
    days = _text_days(folded, c)
    first, last = _closure_days(c)
    return bool(days) and all(not first <= x <= last for x in days)


def _stay_spans(folded: str, c: Closure) -> Optional[list[tuple[datetime, datetime]]]:
    """Časť výluky, o ktorej veta hovorí, že ostáva – podľa dní a časov vo vete („dňa 12.11. od 08:00 do 10:00 platí“);
    None – veta čas neuvádza (celá výluka)."""
    times = [(int(h), int(m1 or m2)) for h, m1, m2 in _TIME_RE.findall(folded)]
    days = _text_days(folded, c)
    if not times and not days:
        return None
    if not days:
        first, last = _closure_days(c)
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)][:SPLIT_MAX_WINDOWS]
    spans = []
    for day in days:
        if times:
            s = _local_at(day, times[0]) if times[0] != (24, 0) else _local_at(day, (0, 0))
            e = _local_at(day, times[1]) if len(times) > 1 else _local_at(day + timedelta(days=1), (0, 0))
            if e <= s:
                e += timedelta(days=1)
        else:
            s, e = _local_at(day, (0, 0)), _local_at(day + timedelta(days=1), (0, 0))
        spans.append((_utc(s), _utc(e)))
    return spans


def _stay_conflict(folded: str, c: Closure, ps: datetime, pe: datetime) -> tuple[str, str]:
    """Veta „výluka ostáva“ a rušený termín ps – pe: („hard“, čas) – veta uvádza čas či deň, ktorý sa prekrýva so
    zrušeným (alebo „v ostatných dňoch platí“ pri zrušení celej viacdennej výluky) – zrušenie jej odporuje, aj keď
    e-mail úsek inde ruší; („soft“, „“) – veta čas neuvádza (úsek ostáva celý); („“, „“) – veta hovorí o inom čase
    (napr. „od 08:00 do 10:00 platí“ pri zrušení 10:00 – 14:00) alebo o iných výlukách."""
    cs, ce = _utc(c.start_utc), _utc(c.end_utc)
    if _REMAINDER.search(folded):
        # zvyšok viacdennej výluky ostáva – zrušenie celej výluky by ho stratilo; jednodňová výluka zvyšok nemá
        # („výluky v ďalších dňoch platia“ hovorí o iných výlukách)
        wins = _windows(cs, ce, c.daily_from, c.daily_to) or [(cs, ce)]
        whole = ps <= cs + SPLIT_TOLERANCE and pe >= ce - SPLIT_TOLERANCE
        return ("hard", "ostatné dni") if whole and (len(wins) > 1 or _local_days(cs, ce) > 1) else ("", "")
    spans = _stay_spans(folded, c)
    if spans is None:
        return "soft", ""
    hit = [(a, b) for a, b in spans if min(b, pe) - max(a, ps) > SPLIT_TOLERANCE]
    return ("hard", "; ".join(f"{fmt(a)} – {fmt(b)}" for a, b in hit[:3])) if hit else ("", "")


@dataclass
class _Said:
    """Čo text e-mailu hovorí o úseku výluky: zrušenie, „ostáva“ bez času (soft), „ostáva“ v rušenom čase (hard)."""

    cancelled: bool = False
    soft: bool = False
    hard: list = field(default_factory=list)

    @property
    def any(self) -> bool:
        return self.cancelled or self.soft or bool(self.hard)


def _section_said(text: str, c: Closure, has: Callable[[str], bool], ps: datetime, pe: datetime,
                  any_closure: bool) -> _Said:
    """Výroky textu (predmet alebo vlastný text e-mailu) o úseku výluky ``c`` (``has`` – úsek je v časti vety)."""
    own_ref = ref_key(c.external_ref) if c.external_ref else ""
    out = _Said()
    for sent_o, sent_f in _sentences(text):
        # veta o výluke (nie o vlakoch, ktoré premávajú); položka zoznamu pod nadpisom o výluke tiež
        about = bool(_CLOSURE_WORDS.search(sent_f)) or bool(own_ref and own_ref in sent_f) or \
            (any_closure and bool(_LIST_START.match(sent_o)))
        items = []
        end = 0
        for o, f in _clause_parts(sent_o, sent_f):
            ex = _EXCEPT.search(f)
            pos = sent_f.find(f, end)
            lead, end = (sent_f[end:pos], pos + len(f)) if pos >= 0 else ("", end)
            items.append({
                "lead": lead + f[:6],            # oddeľovač pred časťou vety („, ale “) a jej začiatok
                "f": f, "pred": None if _RELATIVE.match(f) else _predicate(f),
                "sec": has(f[:ex.start()] if ex else f), "exc": bool(ex) and has(f[ex.end():]),
                "place": bool(_PLACE_WORDS.search(f) or _SECTION_PAIR.search(o)
                              or re.search(rf"\S\s+[{_UPPER}][a-záäčďéíĺľňóôŕšťúýž]", o.strip())),
                "when": bool(_TIME_RE.search(f) or _DATE_RE.search(f)),
                "skip": bool(_OTHER_RESTRICTION.search(f) or re.search(r"\bNAD\b", o)) or _foreign(f, c)})
        for i, it in enumerate(items):
            if it["pred"] is None and it["sec"] and i and items[i - 1]["pred"] == "cancel" and \
                    re.match(r"\s*(?:ale\s+|a\s+)?nie\b", it["f"]):
                it["pred"] = "stay"                  # „ruší sa výluka Senica – Kúty, nie Trnava – Senica“
            elif it["pred"] is None and (it["sec"] or it["exc"]) and not it["skip"]:
                # „Výluky v úsekoch A – B, B – C … sa rušia“: predikát nasledujúcej časti vety
                it["pred"] = next((x["pred"] for x in items[i + 1:] if x["pred"] and not x["skip"]), None)
        # zrušenie časti termínu bez miesta („v čase od 10:00 do 14:00 sa ruší“) v tej istej vete patrí úseku vety
        loose_cancel = any(x["pred"] == "cancel" and x["when"] and not (x["sec"] or x["exc"] or x["place"] or x["skip"])
                           for x in items)
        own = any(x["sec"] for x in items) or bool(own_ref and own_ref in sent_f)
        for i, it in enumerate(items):
            if it["skip"] or not about or not own:
                continue
            # „… sa nekoná/ruší od 08:00 do 10:00, ale (až) od 10:00 do 14:00 (bude)“: časť za „ale“ s časom či dňom
            # za zrušenou časťou vety ostáva – zrušenie jej času by zrušilo časť, ktorá sa koná
            contrast = i and it["when"] and it["pred"] in (None, "stay") and re.search(r"\bale\b", it["lead"]) and \
                any(x["pred"] == "cancel" for x in items[:i])
            # „… v dňoch 23. – 27.11. sa ruší okrem 25.11.“: deň za „okrem“ ostáva
            ex = _EXCEPT.search(it["f"]) if it["pred"] == "cancel" else None
            kept = it["f"][ex.end():] if ex is not None and _DATE_RE.search(it["f"][ex.end():]) else ""
            for part in ((it["f"],) if contrast else ()) + ((kept,) if kept else ()):
                how, when = _stay_conflict(part, c, ps, pe)
                if how == "hard":
                    out.hard.append(when)
        for it in items:
            if it["skip"]:
                continue
            if it["sec"] and (it["pred"] == "cancel" or loose_cancel):
                out.cancelled = True
            if it["sec"] and it["pred"] == "stay" and about:
                how, when = _stay_conflict(it["f"], c, ps, pe)
                out.soft |= how == "soft"
                if how == "hard":
                    out.hard.append(when)
            if it["exc"] and it["pred"] == "cancel":
                out.soft = True                          # „… sa ruší okrem úseku Trnava – Senica“
            elif it["exc"] and it["pred"] == "stay":
                out.cancelled = True                     # „všetky ostávajú okrem úseku …“
    return out


def _stays_text(email: Optional[Email], c: Closure, d: Optional[dict] = None) -> str:
    """E-mail o úseku výluky ``c`` hovorí, že výluka ostáva (uskutoční sa, zostane v platnosti, nie je zrušená, platí
    ROV …), a nikde ho neruší – zrušenie sa netýka výluky ``c`` (napr. „Výluka Senica – Kúty sa ruší. Výluka Trnava –
    Senica sa uskutoční.“); alebo e-mail uvádza, že výluka ostáva práve v rušenom čase (``d`` – dáta zrušenia, rušený
    termín). Vráti vysvetlenie pre dispečera, „“ = nie.

    Úsek patrí predikátu vlastnej časti vety; časť vety s úsekom bez predikátu („Výluky v úsekoch A – B, B – C a C – D
    sa rušia“, „Výluka v úseku A – B, ktorá sa mala uskutočniť …, sa ruší“) preberá predikát nasledujúcej časti, a
    zrušenie časti termínu bez miesta v tej istej vete („od 08:00 do 10:00 platí, od 10:00 do 14:00 sa ruší“) platí aj
    pre úsek vety. Rozhoduje vlastný text e-mailu; predmet len vtedy, keď text o úseku nič nehovorí. Citovaný pôvodný
    e-mail sa neposudzuje."""
    if email is None or c.start_utc is None or c.end_utc is None:
        return ""
    pa = _name_re(c.from_station)
    pb = _name_re(c.to_station) if fold(str(c.to_station or "")) != fold(str(c.from_station or "")) else None
    if pa is None:
        return ""

    def has(text: str) -> bool:
        return bool(pa.search(text)) and (pb is None or bool(pb.search(text)))

    cs, ce = _utc(c.start_utc), _utc(c.end_utc)
    start, end = parse_utc((d or {}).get("start")), parse_utc((d or {}).get("end"))
    ps = start or cs
    pe = end if end is not None and end > ps else max(ce, ps + timedelta(minutes=1))
    body, subject = _unquoted(email.body_text or ""), str(email.subject or "")
    any_closure = any(_CLOSURE_WORDS.search(fold(t)) for t in (subject, body))
    said = _section_said(body, c, has, ps, pe, any_closure)
    if not said.any:
        said = _section_said(subject, c, has, ps, pe, any_closure)
    sec = f"{c.from_station} – {c.to_station}"
    if said.hard:
        # e-mail uvádza, že práve rušený čas výluky ostáva (napr. termín zrušenia vzatý z vety o časti, ktorá ostáva)
        return (f"E-mail uvádza, že výluka v úseku {sec} ostáva (koná sa) aj v čase, ktorý zrušenie uvádza "
                f"({', '.join(dict.fromkeys(said.hard))}) – zrušenie výluky #{c.id} sa nezapíše. Overte termín "
                "zrušenia: opravte ho (Upraviť a potvrdiť), alebo udalosť zamietnite.")
    if not said.soft or said.cancelled:
        return ""
    return (f"E-mail uvádza, že výluka v úseku {sec} ostáva (koná sa), a tento úsek neruší – zrušenie sa netýka výluky "
            f"#{c.id} a nezapíše sa. Ak e-mail ruší iný úsek, opravte úsek (Upraviť a potvrdiť); ak naozaj ruší aj "
            "túto výluku, zrušte ju v registri výluk a udalosť zamietnite.")


# ---------------------------------------------------------------------- zrušenie len niektorých dní či hodín (bez dátumov)
# dni v týždni (text bez diakritiky): „v sobotu a v nedeľu“, „v piatok“, „v noci z piatka na sobotu“, „cez víkend“,
# „v pracovné dni“; „Streda“ je aj v názvoch dopraví (Dunajská Streda, Streda nad Bodrogom)
_WEEKDAY_FORMS = (r"pondel(?:ok|ka|ku|kom|ky|kov|koch|kami)", r"utor(?:ok|ka|ku|kom|ky|kov|koch|kami)",
                  r"(?<!dunajska\s)stred(?:a|y|u|e|ou|ach|am|ami)\b(?!\s+nad\b)",
                  r"stvrt(?:ok|ka|ku|kom|ky|kov|koch|kami)", r"piat(?:ok|ka|ku|kom|ky|kov|koch|kami)",
                  r"sobot(?:a|y|u|e|ou|ach|am|ami)\b", r"(?:nedel(?:a|e|u|i|ou|ach|am|ami)\b|nediel\b)")
_WEEKDAY_RE = re.compile(r"\b(?:" + "|".join(f"(?P<d{i}>{p})" for i, p in enumerate(_WEEKDAY_FORMS)) + ")")
_WEEKDAY_RANGE_GAP = re.compile(r"^\s*(?:-+|–|—|az|do)\s*$")
_WEEKEND_RE = re.compile(r"\bvikend\w*")
_WORKDAYS_RE = re.compile(r"\b(?:pracovn|vsedn)\w*\s+(?:dn|dni)\w*")
_HOLIDAY_RE = re.compile(r"\b(?:sviat\w*|dn\w*\s+pracovneho\s+pokoja)")
_WEEKDAY_NAMES = ("pondelok", "utorok", "streda", "štvrtok", "piatok", "sobota", "nedeľa")
# denné okno „od 12:00 do 14:00“, „12.00 – 14.00 hod.“ (nie dátum „12.11.“)
_DAY_WINDOW_RE = re.compile(r"(?<![\d.:])([01]?\d|2[0-4])[:.]([0-5]\d)(?![.\d])\s*(?:hod\.?|h\.?)?\s*(?:-+|–|—|do|az)\s*"
                            r"([01]?\d|2[0-4])[:.]([0-5]\d)(?![.\d])")
# noc „na“ deň („v noci na 26.11.“, „v noci na piatok 27.11.“) – noc, ktorá v ten deň KONČÍ (z 25. na 26.11.)
_NIGHT_TO_RE = re.compile(r"\bnoc\w*\s+na\s+(?:[a-z]+\s+)?(\d{1,2})\.\s?(\d{1,2})\.(?:\s?(\d{4}))?")


_WEEKDAY_STEMS = ("pondel", "utor", "stred", "stvrt", "piat", "sobot", "nedel")
_station_day_names: Optional[re.Pattern] = None


def _station_day_names_re() -> Optional[re.Pattern]:
    """Názvy dopravní s menom dňa v týždni („Rimavská Sobota“, „Poprad-Spišská Sobota“, „Horná Streda“, „Plavecký
    Štvrtok“) aj v skloňovanom tvare („z Rimavskej Soboty“) – nie sú dňom v týždni."""
    global _station_day_names
    if _station_day_names is None:
        pats: set = set()
        try:
            net = stations().net
            names = [s.name for s in net.stations] if net is not None else []
        except Exception:  # noqa: BLE001 – bez siete bez masky
            names = []
        for name in names:
            toks = re.split(r"[\s\-]+", fold(str(name or "")).strip())
            for i in range(1, len(toks)):
                day = next((d for d in _WEEKDAY_STEMS if toks[i].startswith(d)), None)
                if day and _WEEKDAY_RE.fullmatch(toks[i]) and len(toks[i - 1]) >= 4:
                    stem = toks[i - 1][:max(4, len(toks[i - 1]) - 2)]
                    pats.add(rf"\b{re.escape(stem)}\w*[\s\-]+{day}\w*")
        _station_day_names = re.compile("|".join(sorted(pats))) if pats else re.compile(r"(?!x)x")
    return _station_day_names


# „dohody z utorka“, „e-mailu zo stredy“, „porady z piatka“ (dohoda, oznámenie z toho dňa) nie je obmedzením na deň;
# „výluka z pondelka sa ruší“ (výluka toho dňa) ním je – preto len po podstatnom mene dohody či správy, a nie
# „z piatka na sobotu“, „zo stredy do štvrtka“ (noc či obdobie)
_FROM_DAY = re.compile(r"\b(?:dohod|rokovan|porad|stretnut|oznamen|sprav|mail|list|telefon|zapisnic|zasadnut|vyzv|"
                       r"ziadost|poziadav|rozhodnut|pokyn|dopis|komunikac)\w*\s+(?:\w+\s+){0,2}?zo?\s+(?:" +
                       "|".join(_WEEKDAY_FORMS) + r")\b(?!\s+(?:na|do)\s+(?:" + "|".join(_WEEKDAY_FORMS) + r"))")


def _mask_non_days(folded: str) -> str:
    """Text s vymazanými (medzerami) menami dní, ktoré nie sú dňom výluky: názov dopravne a „dohody z utorka“."""
    def blank(m: re.Match) -> str:
        return " " * len(m.group(0))

    pat = _station_day_names_re()
    out = pat.sub(blank, folded) if pat is not None else folded
    return _FROM_DAY.sub(blank, out)


def _weekdays_in(folded: str) -> tuple[set, bool]:
    """Dni v týždni v časti textu (0 = pondelok): mená dní aj rozsahy („od pondelka do piatku“), víkend, pracovné dni;
    (dni, sviatky – dni pracovného pokoja sa určiť nedajú). Názov dopravne s menom dňa („Rimavská Sobota“) a „z utorka“
    (dohoda z utorka) dňom výluky nie sú (:func:`_mask_non_days`)."""
    folded = _mask_non_days(folded)
    out: set = set()
    found = [(m.start(), m.end(), next(int(k[1:]) for k, v in m.groupdict().items() if v))
             for m in _WEEKDAY_RE.finditer(folded)]
    for (s1, e1, d1), (s2, _e2, d2) in zip(found, found[1:]):
        if _WEEKDAY_RANGE_GAP.match(folded[e1:s2]):
            out |= {(d1 + i) % 7 for i in range((d2 - d1) % 7 + 1)}
    out |= {d for _s, _e, d in found}
    if _WEEKEND_RE.search(folded):
        out |= {5, 6}
    if _WORKDAYS_RE.search(folded):
        out |= {0, 1, 2, 3, 4}
    return out, bool(_HOLIDAY_RE.search(folded))


def _closure_window_hm(c: Closure) -> Optional[tuple]:
    """Denné okno výluky ((h, m), (h, m)) – pri dennom opakovaní, inak čas začiatku a konca jednodňovej výluky."""
    if c.daily_from and c.daily_to:
        return _hm(c.daily_from), _hm(c.daily_to)
    cs, ce = local(_utc(c.start_utc)), local(_utc(c.end_utc))
    if ce - cs <= timedelta(hours=24):
        return (cs.hour, cs.minute), (ce.hour, ce.minute)
    return None


def _cancel_restriction(email: Optional[Email], c: Closure, d: dict, whole: bool = True) -> str:
    """Zrušenie výluky ``c``, ktoré nemožno zapísať podľa termínu z e-mailu – vráti vysvetlenie pre dispečera („“ =
    v poriadku):

    * zrušenie celej výluky (``whole``), ktoré e-mail obmedzuje len na niektoré dni v týždni („v sobotu a v nedeľu sa
      nevykoná“, „cez víkend“, „v noci z piatka na sobotu“, „sa v stredu ruší“) či len na časť dňa („sa ruší čas od
      12:00 do 14:00“) bez dátumov – nikdy sa nezapíše ako zrušenie celej výluky;
    * zrušenie noci „na“ deň („v noci na 26.11.“ – noc, ktorá v ten deň končí), hoci termín zrušenia je celý deň (zrušila
      by sa noc, ktorá v ten deň začína, a rušená noc by ostala).

    Rozhodujú vety vlastného textu e-mailu (a predmetu) so slovom zrušenia; dni z častí vety, podľa ktorých výluka
    ostáva („v pracovné dni sa vykoná“), sa nepočítajú. Deň v týždni obmedzuje, len keď výluka zasahuje aj iné dni."""
    if email is None or c.start_utc is None or c.end_utc is None:
        return ""
    days: set = set()
    holiday = False
    windows: list = []
    nights: set = set()
    first, last = _closure_days(c)
    for text in (str(email.subject or ""), _unquoted(email.body_text or "")):
        for sent_o, sent_f in _sentences(text):
            if not _CANCEL_WORDS.search(_CANCEL_NEG.sub(" ", sent_f)):
                continue                       # veta nič neruší (ani „neruší sa“, „nie je zrušená“)
            stay: set = set()
            said: set = set()
            for _o, f in _clause_parts(sent_o, sent_f):
                wd, hol = _weekdays_in(f)
                if _predicate(f) == "stay":
                    stay |= wd
                    continue
                said |= wd
                holiday |= hol
                windows += [((int(m.group(1)), int(m.group(2))), (int(m.group(3)), int(m.group(4))))
                            for m in _DAY_WINDOW_RE.finditer(f)]
                for m in _NIGHT_TO_RE.finditer(f):
                    for y in ([int(m.group(3))] if m.group(3) else sorted({first.year, last.year})):
                        try:
                            nights.add(date(y, int(m.group(2)), int(m.group(1))))
                        except ValueError:
                            continue
            days |= said - stay
    sec = f"#{c.id} {c.from_station} – {c.to_station} ({_term_desc(c.start_utc, c.end_utc, c.daily_from, c.daily_to)})"
    todo = ("Celá výluka sa nezruší. Upravte výluku v registri výluk (zrušte uvedené dni či hodiny) a udalosť potom "
            "zamietnite, alebo doplňte termín zrušenia (Upraviť a potvrdiť).")
    wins = _windows(c.start_utc, c.end_utc, c.daily_from, c.daily_to) or [(_utc(c.start_utc), _utc(c.end_utc))]
    if len(wins) == 1 and _local_days(*wins[0]) > 1:
        cdays = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    else:
        cdays = [local(w[0]).date() for w in wins]
    if whole and (holiday or (days and any(x.weekday() not in days for x in cdays))):
        named = ", ".join(_WEEKDAY_NAMES[i] for i in sorted(days)) or "sviatky"
        return (f"Zrušenie sa podľa e-mailu týka len niektorých dní v týždni ({named}), ale neuvádza ich dátumy – "
                f"výluka {sec} zasahuje aj iné dni. {todo}")
    own = _closure_window_hm(c)
    start, end = parse_utc(d.get("start")), parse_utc(d.get("end"))
    other = [w for w in windows if own is None or (w[0], w[1]) != own]
    if whole and other and start is None and end is None:
        named = ", ".join(f"{a[0]:02d}:{a[1]:02d} – {b[0]:02d}:{b[1]:02d}" for a, b in dict.fromkeys(other))
        return (f"Zrušenie uvádza len čas ({named}) bez dňa, výluka {sec} má iný čas – zrušila by sa celá výluka, nie "
                f"len uvedené hodiny. {todo}")
    if nights and start is not None and _local_midnight(start):
        stop = end if end is not None and end > start else start + timedelta(days=1)
        cancelled = {local(start).date() + timedelta(days=i) for i in range(max(1, _local_days(start, stop)))}
        if any(x in cancelled for x in nights) and any(_night(w) for w in wins):
            named = ", ".join(f"{x:%d.%m.%Y}" for x in sorted(nights))
            return (f"E-mail ruší noc „na“ {named} – noc, ktorá v ten deň končí (z predchádzajúceho dňa), no termín "
                    f"zrušenia je celý deň a zrušila by sa noc, ktorá v ten deň začína (výluka {sec}). Opravte termín "
                    "zrušenia na noc, ktorú e-mail ruší (Upraviť a potvrdiť), alebo upravte výluku v registri výluk a "
                    "udalosť zamietnite.")
    return ""


_UNTIL_REVOKED = re.compile(r"\bdo\s+odvolania\b")
_CLOSURE_DOC = re.compile(r"\b(?:s?rov|tlg|telegram\w*|potr|rozkaz\w*)\b")
_REPLACEMENT_TRAFFIC = re.compile(r"\bnahradn\w*\s+(?:\w+\s+){0,2}?doprav\w*|\bautobus\w*")


def _says_until_revoked(email: Optional[Email], d: Optional[dict] = None) -> bool:
    """Vlastný text e-mailu (bez citácie) alebo predmet hovorí o výluke „do odvolania“: veta s „do odvolania“, ktorá
    hovorí o výluke či uzavretí trate (slovník výluk, číslo dokumentu ŽSR – ROV, telegram, POTR – alebo dopravňa úseku
    udalosti ``d``). Veta o inom – „náhradná autobusová doprava na trati platí do odvolania“ – sa nepočíta: zrušenie
    jedného dňa výluky by sa inak čítalo ako výluka do odvolania (vlaky v úseku by mesiac nedostali ZELENÚ) a skutočné
    zrušenie výluky s pevným koncom by sa odmietlo."""
    if email is None:
        return False
    names = [n[:6] for n in (fold(str(x)) for x in ((d or {}).get("from_station"), (d or {}).get("to_station")) if x)
             if len(n) >= 5]
    for text in (str(email.subject or ""), _unquoted(email.body_text or "")):
        for _o, f in _sentences(text):
            if not _UNTIL_REVOKED.search(f):
                continue
            closure = bool(_CLOSURE_VOCAB.search(f) or _CLOSURE_DOC.search(f))
            if closure or (not _REPLACEMENT_TRAFFIC.search(f) and any(n in f for n in names)):
                return True
    return False


def _until_revoked_cancel(email: Optional[Email], c: Closure) -> str:
    """Zrušenie výluky ``c`` s pevným koncom z e-mailu, ktorý hovorí „do odvolania“ (FC0: „bude do odvolania výluka …“,
    „do odvolania výluka 1. a 2. TK, predpokladané ukončenie …“ prečítané ako zrušenie): také oznámenie býva výlukou do
    odvolania (pokračovaním), nie zrušením – výluka sa nezruší automaticky ani po potvrdení bez opravy termínu. Výluka
    do odvolania sa takto rušiť smie („výluka vyhlásená do odvolania sa ruší“). „“ = nie."""
    if c.until_revoked or not _says_until_revoked(email, {"from_station": c.from_station, "to_station": c.to_station}):
        return ""
    return (f"E-mail hovorí „do odvolania“, ale výluka #{c.id} {c.from_station} – {c.to_station} má v registri pevný "
            f"koniec ({fmt(c.end_utc)}) – „do odvolania“ býva oznámením výluky do odvolania (pokračovania), nie jej "
            "zrušením. Výluka sa nezruší. Ak ju e-mail naozaj ruší, opravte termín zrušenia (Upraviť a potvrdiť) alebo "
            "ju zrušte v registri výluk a udalosť zamietnite; ak výluku predlžuje do odvolania, zmeňte druh udalosti "
            "na zmenu výluky.")


def _edited_term(ev: ExtractedEvent) -> bool:
    """Dispečer pri potvrdení výslovne opravil termín (začiatok či koniec) udalosti."""
    for e in (ev.validation or {}).get("edits") or []:
        changes = e.get("changes") if isinstance(e, dict) else None
        if isinstance(changes, dict) and any(k in changes for k in ("start", "end")):
            return True
    return False


_CANCELLED_TARGET = "zrusena_vyluka"      # kľúč validácie: cieľ čakajúcej zmeny zrušila novšia udalosť


def _cancelled_warning(closure_id: int, ev_id: int) -> str:
    return (f"Výluku #{closure_id}, ktorej sa táto zmena týka, zrušila novšia udalosť #{ev_id} – po potvrdení sa "
            "zmena nezapíše (novšie zrušenie má prednosť); udalosť zamietnite.")


def _cancelled_kept_warning(closure_id: int, ev_id: int) -> str:
    return (f"Výluku #{closure_id}, ktorej sa táto zmena týka, zrušila novšia udalosť #{ev_id}; termín tejto zmeny "
            "zrušenie nepokrýva – plánovač s ním ďalej počíta a po potvrdení sa zapíše ako samostatná výluka (ak ho "
            "ŽSR zrušila tiež, udalosť zamietnite).")


def _cancel_covers(ev: ExtractedEvent, t: Closure) -> bool:
    """Zrušenie ``ev`` pokrýva termín dočasného záznamu ``t``: zrušenie celej výluky (bez termínu) alebo termín zrušenia
    sa s termínom záznamu prekrýva. Termín mimo zrušeného termínu (presun na iný deň, doplnenie) zrušenie nevyvracia."""
    d = ev.data if isinstance(ev.data, dict) else {}
    cs, ce = parse_utc(d.get("start")), parse_utc(d.get("end"))
    if cs is None or t.start_utc is None or t.end_utc is None:
        return True
    if ce is None or ce <= cs:
        ce = cs + timedelta(days=1)
    return _utc(t.start_utc) < ce and _utc(t.end_utc) > cs


def _unpin_older_changes(session: Session, c: Closure, ev: ExtractedEvent) -> list[int]:
    """Zrušená výluka: dočasné záznamy starších čakajúcich zmien tej istej výluky sa odstránia.

    Novšie zrušenie má prednosť pred staršou zmenou – plánovač už nesmie počítať s jej termínom a neskoršie
    potvrdenie zmeny výluku znova nezaloží (udalosť sa označí ako nahradená). Čakajúce zmeny novšie ako
    zrušenie ostávajú (novšia správa ŽSR platí). Vráti čísla dotknutých udalostí."""
    out: list[int] = []
    rows = list(session.scalars(select(Closure).where(Closure.proposed_change_of == c.id,
                                                      Closure.status != "zrusene")))
    for t in rows:
        pev = session.get(ExtractedEvent, t.source_event_id) if t.source_event_id else None
        if pev is None or pev.id >= ev.id or pev.status == "aplikovane":
            continue
        if _cancel_covers(ev, t):
            session.delete(t)
            _set_pending_info(pev, None, c.id, f"výluku #{c.id} zrušila novšia udalosť #{ev.id} – dočasný záznam "
                                               "zmeny odstránený")
            warn = _cancelled_warning(c.id, ev.id)
        else:
            # termín zmeny (presun na iný deň, doplnenie) leží mimo zrušeného termínu – zrušenie ho nevyvracia,
            # plánovač s ním ďalej počíta; po potvrdení sa zapíše ako samostatná výluka (_keep_extra_term)
            _set_pending_info(pev, t.id, c.id, f"výluku #{c.id} zrušila novšia udalosť #{ev.id} – termín zmeny "
                                               "zrušenie nepokrýva, dočasný záznam ostáva")
            warn = _cancelled_kept_warning(c.id, ev.id)
        val = dict(pev.validation or {})
        val["warnings"] = [w for w in (val.get("warnings") or [])
                           if w not in (_cancelled_warning(c.id, ev.id), _cancelled_kept_warning(c.id, ev.id))] + [warn]
        val[_CANCELLED_TARGET] = {"closure": c.id, "event": ev.id}
        pev.validation = val
        out.append(pev.id)
    if out:
        session.flush()
        audit(session, "agent", "zmeny_zrusenej_vyluky_odstranene", "Closure", c.id, udalost=ev.id, zmeny=out)
    return out


def _repin_newer_same(session: Session, c: Closure, ev: ExtractedEvent) -> list[int]:
    """Výluku ``c`` zmenila či zrušila udalosť ``ev``: novšie čakajúce udalosti, ktorým sa pri spracovaní e-mailu
    nezaložil záznam „navrh“, lebo výluka ich termín už obsahovala („rovnaká výluka … už v registri je“ – napr.
    opakovaný ROV do 72 h), by inak plánovaču zmizli – dostanú záznam podľa svojho termínu (novšia správa ŽSR platí,
    kým ju dispečer nevybaví). Vráti čísla udalostí, ktorým záznam pribudol."""
    out: list[int] = []
    rows = list(session.scalars(select(ExtractedEvent).where(
        ExtractedEvent.status == "navrh", ExtractedEvent.needs_confirmation.is_(True),
        ExtractedEvent.kind.in_(CLOSURE_EVENT_KINDS), ExtractedEvent.id > ev.id)))
    for pev in rows:
        info = (pev.validation or {}).get("pending_closure") if isinstance(pev.validation, dict) else None
        if not isinstance(info, dict) or info.get("id") or info.get("proposed_change_of") != c.id:
            continue
        own, temp = _event_closures(session, pev.id)
        if own is not None or temp is not None:
            continue
        val = dict(pev.validation or {})
        val.pop("pending_closure", None)
        pev.validation = val
        session.flush()
        if _write_pending_safe(session, pev) is not None:
            out.append(pev.id)
    return out


def _repin_older_changes(session: Session, c: Closure) -> list[int]:
    """Zrušenie výluky sa vrátilo (dispečer ho zamietol): čakajúce zmeny, ktorým ho zrušenie odobralo, dostanú
    dočasný záznam „navrh“ znova – plánovač opäť počíta s oboma termínmi."""
    out: list[int] = []
    rows = list(session.scalars(select(ExtractedEvent).where(ExtractedEvent.status == "navrh",
                                                             ExtractedEvent.kind.in_(CLOSURE_EVENT_KINDS))))
    for pev in rows:
        val = dict(pev.validation or {})
        mark = val.get(_CANCELLED_TARGET)
        if not isinstance(mark, dict) or mark.get("closure") != c.id:
            continue
        val.pop(_CANCELLED_TARGET, None)
        val.pop("pending_closure", None)
        gone = (_cancelled_warning(c.id, mark.get("event")), _cancelled_kept_warning(c.id, mark.get("event")))
        val["warnings"] = [w for w in (val.get("warnings") or []) if w not in gone]
        pev.validation = val
        session.flush()
        _write_pending_safe(session, pev)
        out.append(pev.id)
    return out


# ---------------------------------------------------------------------- nepotvrdené výluky (záznam „navrh“)
def _set_pending_info(ev: ExtractedEvent, closure_id: Optional[int], target_id: Optional[int], note: str) -> None:
    val = dict(ev.validation or {})
    val["pending_closure"] = {"id": closure_id, "proposed_change_of": target_id, "note": note}
    ev.validation = val


# validation: stĺpce dočasného záznamu „navrh“, ktoré upravil agent (koľaj pôvodnej výluky, čas predĺženia) – nie sú
# úpravami dispečera v registri výluk (:func:`register_edits` ich neukáže ani pri potvrdení neprenesie)
_ADJUSTED = "pending_adjusted"


def _mark_adjusted(ev: ExtractedEvent, mail_fields: dict, fields: dict) -> None:
    adjusted = {k: _json_value(v) for k, v in fields.items() if mail_fields.get(k) != v}
    val = dict(ev.validation or {})
    if adjusted:
        val[_ADJUSTED] = adjusted
    else:
        val.pop(_ADJUSTED, None)
    ev.validation = val


def _agent_value(ev: ExtractedEvent, col: str, cur: Any) -> bool:
    """Hodnota ``cur`` stĺpca záznamu udalosti je úprava agenta (:func:`_mark_adjusted`), nie dispečera."""
    adjusted = (ev.validation or {}).get(_ADJUSTED)
    if not isinstance(adjusted, dict) or col not in adjusted:
        return False
    v = adjusted[col]
    if isinstance(cur, datetime):
        p = parse_utc(v)
        return p is not None and _utc(cur) == p
    return (cur or "") == (v or "") if isinstance(cur, str) or cur is None else cur == v


def _write_pending(session: Session, ev: ExtractedEvent) -> Optional[Closure]:
    """Výluka čakajúca na potvrdenie: záznam „navrh“, aby s ňou plánovač počítal (sekcia 2.1).

    Nová výluka / pomalá jazda / mimoriadnosť → vlastný záznam „navrh“. Zmena existujúcej výluky → pôvodný
    záznam ostane, nový termín je dočasný záznam „navrh“ (``proposed_change_of``). Zrušenie register nemení – okrem
    zrušenia, ktoré v registri nemá čo zrušiť (:func:`_write_orphan_pending`).
    """
    if ev.kind == "vyluka_zrusenie" and ev.status == "navrh":
        return _write_orphan_pending(session, ev)
    if ev.kind not in CLOSURE_EVENT_KINDS or ev.status != "navrh":
        return None
    email = session.get(Email, ev.email_id) if ev.email_id else None
    if email is not None and email.verdict == KARANTENA:
        return None
    if email is not None and email.verdict == PODOZRIVY:
        # podozrivý e-mail (napr. prompt injection) sa nikdy nepoužije automaticky – ani na plánovanie;
        # dispečer ho vidí vo fronte a po potvrdení sa výluka zapíše
        _set_pending_info(ev, None, None, "podozrivý e-mail – do plánovania sa nezapočíta, kým ho dispečer "
                                          "nepotvrdí")
        return None
    if (ev.validation or {}).get("blocking"):
        _set_pending_info(ev, None, None, "údaje nestačia na záznam výluky (úsek alebo čas)")
        return None
    d = ev.data or {}
    try:
        fields, notes = _closure_values(ev.kind, d, now_utc(), strict=False)
    except ApplyError as exc:
        _set_pending_info(ev, None, None, str(exc))
        return None
    own, temp = _event_closures(session, ev.id)
    row = temp or own
    target_id = row.proposed_change_of if row is not None else None
    if row is None:
        target, _how = _find_closure(session, ev.id, ev.kind, d, fields["start_utc"], loose=ev.kind == "vyluka_zmena",
                                     issuer=_event_issuer(session, ev))
    else:
        target = session.get(Closure, target_id) if target_id else None
    mail_fields = dict(fields)
    if target is not None and ev.kind == "vyluka_zmena":
        _inherit_track(fields, target)
        _pending_extension(fields, target, d, email)
    _mark_adjusted(ev, mail_fields, fields)
    if row is None:
        if target is not None and not _closure_changes(target, fields):
            _set_pending_info(ev, None, target.id, f"rovnaká výluka #{target.id} už v registri je")
            return None
        target_id = target.id if target is not None else None
        row = Closure(status="navrh", source="email", source_event_id=ev.id, proposed_change_of=target_id, **fields)
        session.add(row)
    else:
        for k, v in fields.items():
            setattr(row, k, v)
    head = f"NÁVRH – čaká na potvrdenie dispečerom (udalosť #{ev.id}, e-mail #{ev.email_id})."
    if target_id:
        head += (f" Navrhovaná zmena výluky #{target_id}: pôvodná výluka platí, kým zmenu nepotvrdí dispečer – "
                 "plánovač počíta s oboma termínmi.")
    row.note = "\n".join([head] + notes)
    session.flush()
    _set_pending_info(ev, row.id, target_id, "")
    session.flush()
    audit(session, "agent", "vyluka_navrh_na_potvrdenie", "Closure", row.id, udalost=ev.id, zmena_vyluky=target_id,
          popis=describe_event(ev))
    return row


def _write_orphan_pending(session: Session, ev: ExtractedEvent) -> Optional[Closure]:
    """Poistka zrušenia bez cieľa (:func:`_orphan_cancel`): zrušenie, ktoré v registri nemá čo zrušiť, môže byť nesprávne
    prečítaným oznámením novej výluky – s úsekom a začiatkom dostane záznam „navrh“ (úplná výluka, pri zrušení pomalej
    jazdy pomalá jazda) pre úsek a čas, ktoré uvádza (bez konca: do odvolania, technický koniec sa posúva). Plánovač
    s ním počíta ako s nepotvrdenou výlukou z e-mailu (vlaky v úseku a čase nedostanú ZELENÚ). Záznam nie je výlukou
    v registri: iné udalosti ho za cieľ zmeny či zrušenia nepovažujú (``source`` = ``ORPHAN_SOURCE``); zamietnutie ho
    zruší, potvrdené zrušenie, ktoré cieľ nájde, ho odstráni, a zmena druhu na novú výluku z neho urobí výluku.
    Bez úseku či začiatku záznam nevznikne – vlaky obmedzí poistka čakajúcej udalosti (:func:`pending_uncertainty`)."""
    if not _orphan(ev):
        return None
    email = session.get(Email, ev.email_id) if ev.email_id else None
    if email is not None and email.verdict in (KARANTENA, PODOZRIVY):
        _set_pending_info(ev, None, None, "podozrivý e-mail – do plánovania sa nezapočíta, kým ho dispečer nevybaví")
        return None
    d = ev.data or {}
    start, end = parse_utc(d.get("start")), parse_utc(d.get("end"))
    if not d.get("from_station") or start is None:
        _set_pending_info(ev, None, None, "zrušenie neuvádza úsek alebo začiatok – záznam pre plánovač nevznikne")
        return None
    # bez konca, alebo e-mail hovorí „do odvolania“ („bude do odvolania výluka …“ prečítané ako zrušenie jedného dňa) –
    # do odvolania, technický koniec sa posúva (prísnejší prípad)
    open_end = end is None or end <= start or _says_until_revoked(email, d)
    if open_end:
        end = max(start, now_utc()) + timedelta(days=OPEN_END_DAYS)
    fields = {"kind": "pomala_jazda" if d.get("kind") == "pomala_jazda" else "uplna",
              "from_station": str(d["from_station"])[:120], "to_station": str(d.get("to_station") or d["from_station"])[:120],
              "line_ref": "", "track": "", "start_utc": start, "end_utc": end, "daily_from": "", "daily_to": "",
              "electric_only": False, "speed_kmh": None, "external_ref": str(d.get("external_ref") or "")[:120],
              "until_revoked": open_end}
    own, _temp = _event_closures(session, ev.id)
    row = own
    if row is None:
        row = Closure(status="navrh", source=ORPHAN_SOURCE, source_event_id=ev.id, proposed_change_of=None, **fields)
        session.add(row)
    else:
        for k, v in fields.items():
            setattr(row, k, v)
        row.source = ORPHAN_SOURCE
    row.note = (f"NÁVRH – poistka zrušenia bez cieľa (udalosť #{ev.id}, e-mail #{ev.email_id}): "
                f"{ORPHAN_CANCEL_REASON}. Kým udalosť dispečer nevybaví, plánovač počíta s výlukou v úseku a čase, "
                "ktoré zrušenie uvádza." + (" Koniec zrušenie neuvádza alebo e-mail hovorí „do odvolania“ – do odvolania "
                                          "(technický koniec)." if open_end else ""))
    session.flush()
    _set_pending_info(ev, row.id, None, "")
    session.flush()
    audit(session, "agent", "vyluka_poistka_zrusenia", "Closure", row.id, udalost=ev.id, popis=describe_event(ev))
    return row


def _write_pending_safe(session: Session, ev: ExtractedEvent) -> Optional[Closure]:
    """:func:`_write_pending` v samostatnom SAVEPOINT – chyba zápisu záznamu „navrh“ (napr. príliš dlhá hodnota
    pre PostgreSQL) nesmie zahodiť udalosť ani ostatné udalosti e-mailu (sekcia 2.1: nič sa nesmie stratiť).
    Udalosť ostane vo fronte s upozornením, že plánovač s ňou do potvrdenia nepočíta."""
    try:
        from ..db import prepare_savepoints

        prepare_savepoints(session)  # SQLite: SAVEPOINT len vnútri otvorenej transakcie
        with session.begin_nested():
            return _write_pending(session, ev)
    except Exception as exc:  # noqa: BLE001 – udalosť sa zachová, dispečer dostane upozornenie
        msg = f"záznam pre plánovač sa nepodaril: {type(exc).__name__}"
        _set_pending_info(ev, None, None, msg)
        val = dict(ev.validation or {})
        warn = (f"Výluku sa nepodarilo zapísať pre plánovač ({type(exc).__name__}: {_short(exc, 160)}) – plánovač "
                "s ňou nepočíta, kým ju dispečer nepotvrdí.")
        val["warnings"] = list(val.get("warnings") or []) + [warn]
        ev.validation = val
        session.flush()
        audit(session, "agent", "vyluka_navrh_zlyhal", "ExtractedEvent", ev.id, chyba=f"{type(exc).__name__}: "
                                                                                      f"{_short(exc)}")
        return None


def _drop_pending(session: Session, ev: ExtractedEvent, note: str, own: bool = True,
                  delete_own: bool = False) -> Optional[str]:
    """Odstráni záznamy „navrh“ nepotvrdenej udalosti: dočasný záznam zmeny zmaže, vlastný návrh zruší.

    Mení len záznamy, ktoré udalosti stále patria (``source_event_id``).
    """
    msgs = []
    for c in list(session.scalars(select(Closure).where(Closure.source_event_id == ev.id,
                                                        Closure.status != "zrusene"))):
        if c.proposed_change_of is not None:
            msgs.append(f"dočasný záznam výluky #{c.id} odstránený")
            session.delete(c)
        elif own and c.status == "navrh":
            msgs.append(f"návrh výluky #{c.id} zrušený")
            if delete_own:
                session.delete(c)
            else:
                c.status = "zrusene"
                c.version = (c.version or 1) + 1
                c.note = "\n".join(filter(None, [c.note or "", note]))
    if msgs:
        session.flush()
    return ", ".join(msgs) or None


# ---------------------------------------------------------------------- trasy
_PATH_FIELDS = ("train_id", "train_number", "kind", "status", "departure_utc", "points", "note", "source_event_id")


def _path_day(session: Session, p: TrainPath) -> Optional[str]:
    if p.departure_utc is not None:
        return local(p.departure_utc).date().isoformat()
    if p.train_id:
        tr = session.get(Train, p.train_id)
        return tr.run_date if tr else None
    return None


def _find_paths(session: Session, ev: Optional[ExtractedEvent], number: str, day: Optional[str]) -> list[TrainPath]:
    rows = session.scalars(select(TrainPath).where(TrainPath.train_number == number,
                                                   TrainPath.status != "zrusena")).all()
    if day:
        same = [p for p in rows if _path_day(session, p) == day]
        if same:
            return same
        return [p for p in rows if _path_day(session, p) is None]
    return list(rows)


def _path_guard(session: Session, p: TrainPath, now: datetime, ev_id: Optional[int]) -> list[str]:
    """Prečo sa trasa nesmie zmeniť automaticky (odchod do 72 h alebo už prebieha, čaká na potvrdenie)."""
    h = settings().confirm_horizon_h
    reasons = []
    dep = p.departure_utc
    if dep is None:
        for pt in p.points or []:
            dep = parse_utc(pt.get("dep")) or parse_utc(pt.get("arr"))
            if dep:
                break
    if dep is not None and _utc(dep) <= now + timedelta(hours=h):
        reasons.append(f"mení sa trasa vlaku {p.train_number} s odchodom {fmt(dep)} (do {h} h alebo už prebieha)")
    owner = _pending_owner(session, p, ev_id)
    if owner is not None:
        reasons.append(f"trasa #{p.id} ešte čaká na potvrdenie udalosti #{owner.id}")
    return reasons


def _apply_path(session: Session, ev: ExtractedEvent, confirmed: bool) -> dict:
    d = ev.data or {}
    number = train_digits(d.get("train_number"))
    if not number:
        raise ApplyError("Chýba číslo vlaku.")
    points = [{"station": p.get("station"), "arr": p.get("arr"), "dep": p.get("dep")} for p in d.get("points") or []]
    dep = None
    for p in points:
        dep = parse_utc(p.get("dep")) or parse_utc(p.get("arr"))
        if dep:
            break
    train = find_train(session, number, d.get("date"))
    status = "ponuka" if d.get("offer") else ("zmenena" if ev.kind == "trasa_zmena" else "pridelena")
    notes = []
    if d.get("offer"):
        notes.append("Ponuka trasy z KIS – akceptovať do " +
                     (fmt(parse_utc(d.get("accept_by"))) if d.get("accept_by") else "lehoty uvedenej v e-maile") + ".")
    if d.get("external_ref"):
        notes.append(f"Číslo trasy/žiadosti: {d['external_ref']}")
    notes.append(f"Zdroj: e-mail #{ev.email_id}, udalosť #{ev.id}.")
    existing = session.scalar(select(TrainPath).where(TrainPath.source_event_id == ev.id))
    own = existing is not None
    if existing is None:
        cands = _find_paths(session, ev, number, d.get("date"))
        if ev.kind == "trasa_zmena":
            existing = cands[0] if len(cands) == 1 else None
        else:
            existing = next((p for p in cands if p.status in ("ponuka", "ziadana", "navrh", "pridelena")), None)
    if existing is not None and not own:
        owner = _newer_owner(session, existing, ev)
        if owner is not None:
            return _superseded("Path", existing, "číslo vlaku a deň", owner, train_id=existing.train_id)
        if not confirmed:
            reasons = _path_guard(session, existing, now_utc(), ev.id)
            if reasons:
                raise NeedsConfirmation(reasons)
    fields = {"train_id": train.id if train else None, "train_number": number, "kind": d.get("kind") or "ad_hoc",
              "status": status, "departure_utc": dep, "points": points, "source_event_id": ev.id}
    if existing is None:
        if ev.kind == "trasa_zmena":
            notes.append("Zmena trasy, ktorá v registri nie je – založená ako nová.")
        p = TrainPath(note="\n".join(notes), **fields)
        session.add(p)
        session.flush()
        return {"entity": "Path", "id": p.id, "action": "vytvorene", "status": p.status, "train_id": p.train_id,
                "before": None}
    before = _snapshot(existing, _PATH_FIELDS)
    if existing.status in ("pridelena", "zmenena"):
        _pin_standing(session, existing.train_id)
    for k, v in fields.items():
        if k == "train_id" and v is None:
            continue
        setattr(existing, k, v)
    existing.note = "\n".join(filter(None, [existing.note or ""] + notes))
    session.flush()
    return {"entity": "Path", "id": existing.id, "action": "aktualizovane", "status": existing.status,
            "train_id": existing.train_id, "before": before}


def _pin_standing(session: Session, train_id: Optional[int]) -> None:
    """Pred zrušením, zmenou času alebo preradením trasy (aj pri vrátení zápisu) zapíše začiatok státia vlaku
    podľa doterajších kotiev (napr. zmeškaná trasa, na ktorej vlak stál pripravený) – po zmene trasy by sa
    limit 18 h inak počítal od neskoršieho času. Chyba tu nesmie zastaviť spracovanie e-mailu."""
    if not train_id:
        return
    try:
        from ..planovanie import stav

        from ..db import prepare_savepoints

        prepare_savepoints(session)  # SQLite: SAVEPOINT len vnútri otvorenej transakcie (inak by RELEASE potvrdil)
        with session.begin_nested():  # pri chybe sa vráti len tento krok, nie spracovanie e-mailu
            train = session.get(Train, train_id)
            if train is not None:
                stav.pin_standing(session, train)
    except Exception:  # noqa: BLE001
        logging.getLogger("vlaky.posta").exception("Začiatok státia vlaku %s sa nepodarilo zapísať", train_id)


def _apply_path_cancel(session: Session, ev: ExtractedEvent, confirmed: bool) -> dict:
    d = ev.data or {}
    number = train_digits(d.get("train_number"))
    rows = list(session.scalars(select(TrainPath).where(TrainPath.source_event_id == ev.id)))
    if not rows:
        rows = _find_paths(session, ev, number, d.get("date"))
    if not rows:
        raise ApplyError(f"Trasa vlaku {number} na deň {_day_text(d.get('date'))} sa v registri nenašla.")
    newer = [(p, _newer_owner(session, p, ev)) for p in rows]
    keep = [p for p, owner in newer if owner is None]
    if not keep:
        return _superseded("Path", rows[0], "číslo vlaku a deň", newer[0][1], ids=[p.id for p in rows])
    if not confirmed:
        raise NeedsConfirmation(["zrušenie trasy vždy potvrdzuje dispečer"])
    befores = []
    for p in keep:
        befores.append({"id": p.id, **_snapshot(p, _PATH_FIELDS)})
        if p.status in ("pridelena", "zmenena"):
            _pin_standing(session, p.train_id)
        p.status = "zrusena"
        p.source_event_id = ev.id
        p.note = "\n".join(filter(None, [p.note or "", f"Zrušené podľa udalosti #{ev.id} (e-mail #{ev.email_id})."]))
    session.flush()
    return {"entity": "Path", "id": keep[0].id, "ids": [p.id for p in keep], "action": "zrusene", "status": "zrusena",
            "before": befores}


# ---------------------------------------------------------------------- avízo
_HANDOVER_FIELDS = ("train_id", "border_id", "direction", "avizo_eta_utc", "status", "source_event_id")


def _train_handover(session: Session, train_id: int) -> Optional[Handover]:
    return session.scalar(select(Handover).where(Handover.train_id == train_id, Handover.direction == "vstup",
                                                 Handover.status.in_(("ocakavany", "prisiel")))
                          .order_by(Handover.id.desc()))


def _handover_guard(session: Session, h: Handover, now: datetime, ev_id: Optional[int]) -> list[str]:
    """Prečo sa preberanie nesmie zmeniť automaticky (vlak už prišiel, ETA do 72 h, čaká na potvrdenie)."""
    hours = settings().confirm_horizon_h
    reasons = []
    if h.status != "ocakavany":
        reasons.append(f"vlak už je na hranici (preberanie #{h.id}: {h.status}) – avízo ho nemení automaticky")
    if h.avizo_eta_utc is not None and _utc(h.avizo_eta_utc) <= now + timedelta(hours=hours):
        reasons.append(f"mení sa očakávaný príchod do {hours} h (preberanie #{h.id}, ETA {fmt(h.avizo_eta_utc)})")
    owner = _pending_owner(session, h, ev_id)
    if owner is not None:
        reasons.append(f"preberanie #{h.id} ešte čaká na potvrdenie udalosti #{owner.id}")
    return reasons


def _apply_avizo(session: Session, ev: ExtractedEvent, confirmed: bool) -> dict:
    d = ev.data or {}
    eta = parse_utc(d.get("eta"))
    if eta is None:
        raise ApplyError("Chýba predpokladaný čas príchodu (ETA).")
    day = d.get("date") or local(eta).date().isoformat()
    train = find_train(session, d.get("train_number"), day, avizo=True)
    if train is None:
        num = d.get("train_number")
        shown_day = _day_text(day)
        # vlak s týmto číslom v systéme je, len s iným dátumom jazdy, viac vlakov bez dátumu alebo len ukončená
        # jazda (napr. minulý týždeň) – iná rada
        why = train_mismatch(session, num, day)
        val = dict(ev.validation or {})
        msg = (f"vlak nenájdený – avízo je na {shown_day}, ale {why}" if why else
               f"vlak nenájdený – vlak {num} nie je v systéme; založte ho (dátum jazdy {shown_day}) a avízo "
               "potvrďte znova")
        if msg not in (val.get("errors") or []):
            val["errors"] = list(val.get("errors") or []) + [msg]
        reasons = list(val.get("confirmation_reasons") or [])
        if "vlak nenájdený" not in reasons:
            reasons.append("vlak nenájdený")
        val["confirmation_reasons"] = reasons
        ev.validation = val
        ev.needs_confirmation = True
        session.flush()
        if why:
            raise ApplyError(f"Vlak {num} pre avízo na {shown_day} nenájdený – {why}.")
        raise ApplyError(f"Vlak {num} nenájdený – avízo čaká na priradenie dispečerom (založte vlak s dátumom "
                         f"jazdy {shown_day}).")
    bc = resolve_border(session, d)
    h = session.scalar(select(Handover).where(Handover.source_event_id == ev.id))
    own = h is not None
    if h is None:
        h = _train_handover(session, train.id)
    if h is not None and not own:
        owner = _newer_owner(session, h, ev)
        if owner is not None:     # novšie avízo už platí – staršie ho neprepíše
            return _superseded("Handover", h, "vlak", owner, train_id=train.id, train_before=None)
        if not confirmed:
            reasons = _handover_guard(session, h, now_utc(), ev.id)
            if reasons:
                raise NeedsConfirmation(reasons)
    created = h is None
    before = None if created else _snapshot(h, _HANDOVER_FIELDS)
    train_before = {"status": train.status, "entry_border_id": train.entry_border_id}
    if created:
        h = Handover(train_id=train.id, direction="vstup", status="ocakavany")
        session.add(h)
    if bc is not None:
        h.border_id = bc.id
    h.avizo_eta_utc = eta
    h.source_event_id = ev.id
    if h.status not in ("prisiel", "prevzaty", "odovzdany"):
        h.status = "ocakavany"
    if train.status == "planovany":
        train.status = "na_ceste_k_hranici"
    if train.entry_border_id is None and bc is not None:
        train.entry_border_id = bc.id
    session.flush()
    return {"entity": "Handover", "id": h.id, "action": "vytvorene" if created else "aktualizovane",
            "train_id": train.id, "border": bc.code if bc else None, "before": before, "train_before": train_before}


# ---------------------------------------------------------------------- objednávka
_ORDER_FIELDS = ("customer", "origin", "destination", "earliest_departure_utc", "deadline_utc", "cargo", "rid",
                 "wagons", "weight_t", "length_m", "status", "notes", "source_event_id")


def _apply_order(session: Session, ev: ExtractedEvent, confirmed: bool) -> dict:
    d = ev.data or {}
    if not d.get("origin") or not d.get("destination"):
        raise ApplyError("Objednávka nemá odkiaľ a kam.")
    o = session.scalar(select(Order).where(Order.source_event_id == ev.id))
    created = o is None
    before = None if created else _snapshot(o, _ORDER_FIELDS)
    if created:
        o = Order(status="nova", source_event_id=ev.id, origin=d["origin"], destination=d["destination"])
        session.add(o)
    o.customer = str(d.get("customer") or "")[:200]
    o.origin, o.destination = str(d["origin"])[:120], str(d["destination"])[:120]
    o.earliest_departure_utc = parse_utc(d.get("earliest_departure"))
    o.deadline_utc = parse_utc(d.get("deadline"))
    o.cargo = str(d.get("cargo") or "")[:200]
    o.rid = bool(d.get("rid"))
    wagons = parse_number(d.get("wagons"))
    o.wagons = int(wagons) if wagons is not None else None
    o.weight_t = parse_number(d.get("weight_t"))
    o.length_m = parse_number(d.get("length_m"))
    rid_note = "" if d.get("rid") is not None else " RID neuvedené – overte."
    o.notes = f"Zo e-mailu #{ev.email_id} (udalosť #{ev.id}).{rid_note}"
    session.flush()
    return {"entity": "Order", "id": o.id, "action": "vytvorene" if created else "aktualizovane", "status": o.status,
            "before": before}


# ====================================================================== potvrdenie a zamietnutie
# stĺpec záznamu výluky → kľúč dát udalosti (úpravy dispečera v registri výluk sa pri potvrdení prenesú do dát)
_ROW_KEYS = {"from_station": "from_station", "to_station": "to_station", "line_ref": "line_ref", "track": "track",
             "start_utc": "start", "end_utc": "end", "daily_from": "daily_from", "daily_to": "daily_to",
             "speed_kmh": "speed_kmh", "external_ref": "external_ref", "kind": "kind"}
# kľúč dát → stĺpce, ktoré výslovná oprava dispečera vo fronte nastavuje (má prednosť pred úpravou v registri)
_KEY_COLS = {**{v: (k,) for k, v in _ROW_KEYS.items()}, "kind": ("kind", "electric_only"),
             "until_revoked": ("end_utc",)}
_TEXT_COLS = ("from_station", "to_station", "line_ref", "track", "daily_from", "daily_to", "external_ref", "kind")


def _json_value(v: Any) -> Any:
    return _utc(v).isoformat() if isinstance(v, datetime) else v


def register_edits(session: Session, ev: ExtractedEvent) -> dict:
    """Čo dispečer zmenil v registri výluk v zázname nepotvrdenej udalosti po tom, čo ho udalosť zapísala.

    Záznam udalosti je jej návrh „navrh“, dočasný záznam navrhovanej zmeny alebo výluka zapísaná automaticky.
    Vráti ``{stĺpec: (hodnota podľa e-mailu, hodnota v registri)}``. Potvrdenie tieto úpravy zachová (údaje
    z e-mailu ich nikdy potichu neprepíšu) – dispečer ich vidí vo fronte.
    """
    if ev.kind not in CLOSURE_EVENT_KINDS or ev.status == "zamietnute" or \
            (ev.status == "aplikovane" and ev.confirmed_at is not None):
        return {}
    own, temp = _event_closures(session, ev.id)
    row = temp if temp is not None else own
    if row is None:
        return {}
    try:
        fields, _notes = _closure_values(ev.kind, ev.data or {}, now_utc())
    except ApplyError:
        return {}
    applied = (ev.validation or {}).get("applied") or {}
    # výluku, ktorú udalosť len zmenila (nezaložila), zapis neprepisuje prázdnym číslom – to nie je úprava
    updated = row is own and applied.get("action") in ("aktualizovane", "potvrdene")
    out: dict = {}
    for k, v in fields.items():
        if k == "until_revoked" or (k == "end_utc" and fields.get("until_revoked") and row.until_revoked):
            continue                                  # technický koniec výluky do odvolania posúva agent
        cur = getattr(row, k)
        if isinstance(cur, datetime) or isinstance(v, datetime):
            same = cur is not None and v is not None and _utc(cur) == _utc(v)
        elif k in _TEXT_COLS:
            same = (cur or "") == (v or "") or (k == "external_ref" and updated and not v)
        elif k == "speed_kmh":
            same = (cur is None) == (v is None) and (cur is None or float(cur) == float(v))
        else:
            same = bool(cur) == bool(v)
        if not same and not _agent_value(ev, k, cur):
            out[k] = (v, cur)
    return out


def _overlay_register_fields(ev: ExtractedEvent, fields: dict) -> None:
    """Hodnoty, ktoré dispečer nastavil v registri výluk (aj tie, ktoré dáta udalosti nevedia vyjadriť)."""
    for k, v in ((ev.validation or {}).get("register_fields") or {}).items():
        if k not in fields or k == "until_revoked":
            continue
        if k in ("start_utc", "end_utc"):
            v = parse_utc(v)
            if v is None:
                continue
        fields[k] = v


def confirm_event(session: Session, event_id: int, actor: str, edits: Optional[dict] = None, *,
                  pending_only: bool = False) -> ExtractedEvent:
    """Dispečer potvrdí (a prípadne opraví) udalosť; potom sa zapíše do registrov.

    ``edits`` – opravy polí dát (napr. ``{"to_station": "Ružomberok"}``); kľúč ``event_kind`` zmení druh
    udalosti. Opravy sa uložia (pôvodná extrakcia zostáva v ``validation.original_data`` – trénovacie príklady).
    Výsledok zápisu je v ``event.validation["apply_result"]``; ak zápis zlyhal, stav ostane ``potvrdene``
    a udalosť možno po odstránení príčiny potvrdiť znova.
    Záznam „navrh“ čakajúcej výluky sa stane overeným (s opravami dispečera); pri zmene výluky sa zmena
    zapíše do pôvodného záznamu a dočasný záznam sa odstráni. Úpravy, ktoré dispečer urobil v registri výluk
    v zázname udalosti, sa zachovajú (``validation.register_fields``); výslovná oprava vo fronte má prednosť.
    Staršia udalosť stav zapísaný novšou udalosťou neprepíše (``apply_result.action = "nahradene"``).
    Zmena či zrušenie len časti výluky výluku rozdelí (``apply_result.action = "rozdelene"``).

    Potvrdenia idú po jednom (:func:`lock_register`). ``pending_only`` (web): udalosť, ktorú medzitým spracoval
    iný dispečer, sa nepotvrdí znova – :class:`EventProcessed` s popisom, ako bola spracovaná.
    """
    lock_register(session)
    ev = session.get(ExtractedEvent, event_id)
    if ev is None:
        raise ValueError(f"Udalosť #{event_id} neexistuje.")
    if ev.status == "zamietnute":
        if pending_only:
            raise EventProcessed(f"Udalosť #{event_id} už bola spracovaná – {processed_state(ev)}.")
        raise ValueError(f"Udalosť #{event_id} bola zamietnutá – nemožno ju potvrdiť.")
    if pending_only and not confirmable(ev):
        raise EventProcessed(f"Udalosť #{event_id} už bola spracovaná – {processed_state(ev)}. Nič sa nezmenilo.")
    val = copy.deepcopy(ev.validation or {})
    data = copy.deepcopy(ev.data or {})
    kept = register_edits(session, ev)          # pred zmenou druhu – porovnáva sa s tým, čo udalosť zapísala
    edits = dict(edits or {})
    new_kind = edits.pop("event_kind", None)
    changes: dict = {}
    if new_kind and new_kind != ev.kind:
        if new_kind not in KIND_LABELS:
            raise ValueError(f"Neznámy druh udalosti „{new_kind}“.")
        changes["event_kind"] = {"pred": ev.kind, "po": new_kind}
        ev.kind = new_kind
    overridden = {col for k in edits for col in _KEY_COLS.get(k, ())}
    if "event_kind" in changes:
        overridden |= {"kind", "electric_only"}
    if ev.kind not in CLOSURE_EVENT_KINDS:
        kept = {}                               # preklasifikované (napr. na zrušenie) – návrh výluky sa nepoužije
    reg_changes: dict = {}
    reg_fields: dict = {}
    schema = SCHEMAS.get(ev.kind, [])
    for col, (mail_value, row_value) in kept.items():
        if col in overridden:
            continue
        value = _json_value(row_value)
        reg_fields[col] = value
        key = _ROW_KEYS.get(col)
        if key in schema and (key != "kind" or ev.kind in ("vyluka_nova", "vyluka_zmena")):
            data[key] = value
        reg_changes[key or col] = {"pred": _json_value(mail_value), "po": value}
    for k, v in edits.items():
        if data.get(k) != v:
            changes[k] = {"pred": data.get(k), "po": v}
            data[k] = v
    if ev.kind == "vyluka_zmena" and not data.get("start"):
        # predĺženie či skrátenie bez začiatku, ktorému úsek doplnil dispečer (napr. výberom výluky z registra podľa
        # čísla): začiatok podľa výluky, ktorej sa zmena týka – inak by zápis zlyhal („chýba začiatok“)
        extra: list = []
        data = _register_start(session, ev.kind, data, session.get(Email, ev.email_id) if ev.email_id else None,
                               extra, ev_id=ev.id)
        known = list(val.get("notes") or [])
        val["notes"] = known + [n for n in extra if n not in known]
    if "event_kind" in changes and changes["event_kind"]["pred"] == "vyluka_zrusenie" and \
            isinstance(val.get(_ORPHAN_KEY), dict) and ev.kind in ("vyluka_nova", "vyluka_zmena") and \
            not data.get("end") and not data.get("until_revoked"):
        own, _temp = _event_closures(session, ev.id)
        if own is not None and own.source == ORPHAN_SOURCE and own.until_revoked:
            # poistka zrušenia bez konca (do odvolania) zmenená na výluku: výluka platí do odvolania, kým dispečer
            # nezadá koniec – inak by sa nezapísala („chýba koniec“) a ostala by len poistka
            data["until_revoked"] = True
            val.setdefault("notes", []).append("Zrušenie bez cieľa a bez konca zmenené na výluku – platí do odvolania "
                                               "(technický koniec sa posúva), kým ŽSR neoznámi koniec.")
    new_edits = []
    if reg_changes:
        new_edits.append({"by": actor, "at": now_utc().isoformat(), "zdroj": "register výluk",
                          "changes": reg_changes})
    if changes:
        new_edits.append({"by": actor, "at": now_utc().isoformat(), "changes": changes})
    if new_edits:
        val.setdefault("edits", []).extend(new_edits)
    if reg_fields:
        val["register_fields"] = reg_fields
        val["register_kept"] = sorted(reg_changes)
    else:
        val.pop("register_fields", None)
        val.pop("register_kept", None)
    email = session.get(Email, ev.email_id) if ev.email_id else None
    verdict = email.verdict if email is not None else OK
    vr = validate_event(session, ev.kind, data, confidence=ev.confidence, verdict=verdict, previous=val)
    val.update(vr.as_dict())
    ev.data = vr.data
    ev.confirmed_by = actor
    ev.confirmed_at = now_utc()
    ev.status = "potvrdene"
    ev.validation = val
    session.flush()
    audit(session, actor, "udalost_potvrdena", "ExtractedEvent", ev.id, druh=ev.kind, popis=describe_event(ev),
          opravy=new_edits)
    result = apply_event(session, ev, actor)
    val = dict(ev.validation or {})
    val["apply_result"] = {k: v for k, v in result.items() if k not in ("before", "train_before")}
    ev.validation = val
    session.flush()
    return ev


def apply_failed(ev: ExtractedEvent) -> bool:
    """Potvrdená udalosť, ktorej zápis do registrov zlyhal (napr. avízo pre vlak, ktorý ešte nie je v systéme).

    Ostáva vo fronte na potvrdenie – po odstránení príčiny ju dispečer potvrdí znova alebo zamietne."""
    result = (ev.validation or {}).get("apply_result") if isinstance(ev.validation, dict) else None
    return ev.status == "potvrdene" and isinstance(result, dict) and result.get("ok") is False


def failed_confirmations(session: Session, limit: int = 200) -> list[ExtractedEvent]:
    """Potvrdené udalosti s neúspešným zápisom (čakajú na dispečera rovnako ako udalosti vo fronte).

    Filtruje sa v databáze (validation.apply_result.ok = false) – potvrdených udalostí pribúda a výber prvých N
    potvrdených by nové zlyhané zápisy časom vynechal."""
    failed = ExtractedEvent.validation["apply_result"]["ok"].as_boolean().is_(False)
    rows = session.scalars(select(ExtractedEvent).where(ExtractedEvent.status == "potvrdene", failed)
                           .order_by(ExtractedEvent.created_at, ExtractedEvent.id).limit(limit))
    return [e for e in rows if apply_failed(e)]


def reject_event(session: Session, event_id: int, actor: str, reason: str = "", *,
                 pending_only: bool = False) -> ExtractedEvent:
    """Dispečer udalosť zamietne. Ak už bola zapísaná, jej zápis sa vráti späť (záznam sa zruší/obnoví).

    Záznam, ktorý medzitým zmenila iná (novšia) udalosť, sa nemení. Záznamy „navrh“ čakajúcej udalosti
    sa odstránia (dočasný záznam zmeny) alebo zrušia (návrh novej výluky). Zamietnutie idú po jednom
    s potvrdeniami (:func:`lock_register`); ``pending_only`` (web) – udalosť, ktorú medzitým potvrdil alebo
    zamietol iný dispečer, sa nemení (:class:`EventProcessed`).
    """
    lock_register(session)
    ev = session.get(ExtractedEvent, event_id)
    if ev is None:
        raise ValueError(f"Udalosť #{event_id} neexistuje.")
    if pending_only and not confirmable(ev):
        raise EventProcessed(f"Udalosť #{event_id} už bola spracovaná – {processed_state(ev)}. Nič sa nezmenilo.")
    if ev.status == "zamietnute":
        return ev
    val = copy.deepcopy(ev.validation or {})
    parts: list[str] = []
    was_applied = ev.status == "aplikovane" and bool(val.get("applied"))
    if was_applied:
        r = _rollback(session, ev, val["applied"])
        if r:
            parts.append(r)
    dropped = _drop_pending(session, ev, f"Zamietnuté dispečerom (udalosť #{ev.id}).", own=not was_applied)
    if dropped:
        parts.append(dropped)
    rollback = "; ".join(parts) or None
    val["rejection"] = {"by": actor, "at": now_utc().isoformat(), "reason": reason, "rollback": rollback}
    ev.status = "zamietnute"
    ev.validation = val
    session.flush()
    audit(session, actor, "udalost_zamietnuta", "ExtractedEvent", ev.id, druh=ev.kind, dovod=reason,
          popis=describe_event(ev), navrat=rollback)
    return ev


_ENTITY_GEN = {"Closure": "výluky", "Path": "trasy", "Handover": "preberania", "Order": "objednávky"}


def _foreign_owner(entity: str, obj: Any, ev: ExtractedEvent) -> str:
    oid = getattr(obj, "source_event_id", None)
    what = f"Záznam {_ENTITY_GEN.get(entity, entity)} #{obj.id}"
    if oid:
        newer = "novšia " if ev.id is not None and oid > ev.id else ""
        return f"{what} medzitým zmenila {newer}udalosť #{oid} – ponechaný bez zmeny, skontrolujte ho"
    return f"{what} medzitým upravil dispečer – ponechaný bez zmeny, skontrolujte ho"


def _effective_before(session: Session, before: Any, entity: str, obj_id: int) -> Any:
    """Predchádzajúci platný stav záznamu: preskočí stavy zapísané udalosťami, ktoré dispečer zamietol."""
    snap, seen = before, set()
    while isinstance(snap, dict) and snap.get("source_event_id"):
        sid = snap["source_event_id"]
        if sid in seen:
            break
        seen.add(sid)
        prev = session.get(ExtractedEvent, sid)
        if prev is None or prev.status != "zamietnute":
            break
        pa = (prev.validation or {}).get("applied") or {}
        if pa.get("entity") != entity:
            break
        pb = pa.get("before")
        if isinstance(pb, list):
            snap = next(({k: v for k, v in b.items() if k != "id"} for b in pb if b.get("id") == obj_id), None)
        elif pa.get("id") == obj_id:
            snap = pb
        else:
            break
    return snap


def _rollback(session: Session, ev: ExtractedEvent, applied: dict) -> Optional[str]:
    entity, action, before = applied.get("entity"), applied.get("action"), applied.get("before")
    model = {"Closure": Closure, "Path": TrainPath, "Handover": Handover, "Order": Order}.get(entity or "")
    if model is None:
        return None
    if action == "nahradene":
        return "udalosť nič nezapísala (nahradila ju novšia) – nie je čo vrátiť"
    if action == "bez_zmeny":
        return "udalosť register nezmenila – nie je čo vrátiť"
    if entity == "Closure" and action == "rozdelene":
        return _undo_split(session, ev, applied)
    if entity == "Path" and action == "zrusene":
        restored, kept = [], []
        for b in before or []:
            p = session.get(TrainPath, b.get("id"))
            if p is None:
                continue
            if p.source_event_id != ev.id:
                kept.append(_foreign_owner(entity, p, ev))
                continue
            snap = _effective_before(session, {k: v for k, v in b.items() if k != "id"}, entity, p.id)
            if isinstance(snap, dict):
                _pin_standing(session, p.train_id)
                _restore(p, snap)
                restored.append(p.id)
        msg = "zrušenie trasy vrátené" if restored else ""
        return "; ".join(filter(None, [msg] + kept)) or None
    obj = session.get(model, applied.get("id"))
    if obj is None:
        return None
    if obj.source_event_id != ev.id:
        return _foreign_owner(entity, obj, ev)
    if action == "vytvorene":
        return _undo_creation(session, ev, entity, obj, applied)
    if before:
        snap = _effective_before(session, before, entity, obj.id)
        if not isinstance(snap, dict):       # pôvodný záznam vznikol z udalosti, ktorú dispečer zamietol
            return _undo_creation(session, ev, entity, obj, applied)
        if entity in ("Path", "Handover"):
            _pin_standing(session, obj.train_id)
        _restore(obj, snap)
        if entity == "Closure":
            # predĺženie výluky zapísalo aj nový termín ako ďalšiu výluku – zamietnutím zanikne (ak ešte patrí udalosti)
            for extra in (session.get(Closure, i) for i in applied.get("added") or []):
                if extra is not None and extra.source_event_id == ev.id and extra.status != "zrusene":
                    extra.status = "zrusene"
                    extra.note = "\n".join(filter(None, [extra.note or "", f"Zamietnuté dispečerom (udalosť #{ev.id})."]))
        if entity == "Closure" and action == "zrusene" and obj.status != "zrusene":
            session.flush()
            _repin_older_changes(session, obj)      # čakajúce zmeny výluky znova dostanú záznam pre plánovač
        if entity == "Handover":
            tb = applied.get("train_before") or {}
            tr = session.get(Train, obj.train_id)
            if tr is not None and tb.get("status") == "planovany" and tr.status == "na_ceste_k_hranici":
                tr.status = "planovany"
        return "pôvodný stav obnovený"
    return None


def _undo_creation(session: Session, ev: ExtractedEvent, entity: str, obj: Any, applied: dict) -> Optional[str]:
    if entity == "Closure":
        obj.status = "zrusene"
        obj.note = "\n".join(filter(None, [obj.note or "", f"Zamietnuté dispečerom (udalosť #{ev.id})."]))
        # ďalšie termíny tej istej udalosti zapísané ako samostatné výluky (_keep_extra_term) – ak jej ešte patria
        for extra in (session.get(Closure, i) for i in applied.get("added") or []):
            if extra is not None and extra.source_event_id == ev.id and extra.status != "zrusene":
                extra.status = "zrusene"
                extra.note = "\n".join(filter(None, [extra.note or "", f"Zamietnuté dispečerom (udalosť #{ev.id})."]))
        return "výluka zrušená"
    if entity == "Path":
        _pin_standing(session, obj.train_id)
        obj.status = "zrusena"
        return "trasa zrušená"
    if entity == "Order":
        obj.status = "zamietnuta"
        return "objednávka zamietnutá"
    if entity == "Handover":
        _pin_standing(session, obj.train_id)
        tb = applied.get("train_before") or {}
        tr = session.get(Train, obj.train_id)
        if tr is not None and tb:
            if tr.status == "na_ceste_k_hranici" and tb.get("status") == "planovany":
                tr.status = "planovany"
            if tb.get("entry_border_id") is None and tr.entry_border_id == obj.border_id:
                tr.entry_border_id = None
        if obj.status == "ocakavany":
            session.delete(obj)
            return "preberanie odstránené"
        obj.avizo_eta_utc = None
        return "ETA z avíza odstránený"
    return None


def _snapshot(obj: Any, fields: tuple) -> dict:
    """Hodnoty polí pre návrat zmien (časy vždy s UTC zónou – SQLite vracia časy bez zóny)."""
    out = {}
    for f in fields:
        v = getattr(obj, f)
        out[f] = _utc(v).isoformat() if isinstance(v, datetime) else copy.deepcopy(v)
    return out


def _restore(obj: Any, snap: dict) -> None:
    cols = obj.__table__.columns
    for k, v in snap.items():
        if k not in cols:
            continue
        if isinstance(cols[k].type, DateTime) and isinstance(v, str):
            v = parse_utc(v)
        setattr(obj, k, v)


# ====================================================================== celý cyklus
def process_mailbox(session: Session, now: Optional[datetime] = None) -> dict:
    """Jeden cyklus: načítaj a ulož poštu, extrahuj udalosti, automaticky zapíš bezpečné, prepočítaj plán.

    ``now`` – čas cyklu (predvolene teraz). Po úspešnom načítaní schránky sa hneď uloží ``mail_last_ok``
    (plánovač podľa neho posudzuje čerstvosť výluk); ak sa schránku prečítať nepodarilo, nemení sa. Kým v schránke
    ostávajú ďalšie e-maily nad dávku (``summary["drained"]`` je False), záznam hovorí o čiastočnom čítaní – nie
    je to úspešná kontrola schránky (zdroje.mailbox_check).
    Dokončí aj udalosti, ktoré ostali nezapísané po páde, a posunie technický koniec výluk do odvolania.
    Nikdy nevyhodí výnimku – chyby sú v ``summary["errors"]``.
    """
    now = now or now_utc()
    summary: dict[str, Any] = {"fetched": 0, "fetch_ok": False, "quarantined": 0, "suspicious": 0, "events": 0,
                               "auto_applied": 0, "waiting_confirmation": 0, "errors": [], "email_ids": [],
                               "event_ids": [], "applied_event_ids": [], "waiting_event_ids": [],
                               "quarantined_email_ids": [], "recovered_event_ids": [], "planning": None}
    errors: list[str] = summary["errors"]
    try:
        new, fetch_ok, drained = _fetch_and_store(session, errors)
    except Exception as exc:  # pragma: no cover - posledná poistka
        session.rollback()
        errors.append(f"Príjem pošty zlyhal: {type(exc).__name__}: {_short(exc)}")
        new, fetch_ok, drained = [], False, False
    summary["fetch_ok"] = fetch_ok
    summary["drained"] = drained
    summary["fetched"] = len(new)
    summary["email_ids"] = [e.id for e in new]
    summary["quarantined_email_ids"] = [e.id for e in new if e.verdict == KARANTENA]
    summary["quarantined"] = len(summary["quarantined_email_ids"])
    summary["suspicious"] = sum(1 for e in new if e.verdict == PODOZRIVY)
    if fetch_ok:
        try:     # schránka je prečítaná – plánovač v tomto cykle už vidí čerstvý stav (čiastočné čítanie nie)
            from .zdroje import mailbox_check

            kv_set(session, "mail_last_ok", mailbox_check(kv_get(session, "mail_last_ok"), now, len(new), drained))
            session.commit()
        except Exception as exc:
            session.rollback()
            errors.append(f"Zápis času kontroly schránky zlyhal ({type(exc).__name__}).")
    applied_descr: list[str] = []
    _recover_unapplied(session, summary, applied_descr)
    # aj staršie neprijaté e-maily (napr. po reštarte medzi uložením a extrakciou)
    pending = list(session.scalars(select(Email).where(Email.status == "prijaty", Email.verdict != KARANTENA)
                                   .order_by(Email.id)))
    for email in pending:
        email_id = email.id
        try:
            from .odpovede import handle_dispatcher_reply

            if handle_dispatcher_reply(session, email):  # odpoveď dispečera na odporúčanie, nie prevádzková zmena
                session.commit()
                summary.setdefault("replies", 0)
                summary["replies"] += 1
                continue
            events = process_email(session, email)
            session.commit()
        except Exception as exc:
            session.rollback()
            errors.append(f"E-mail #{email_id}: spracovanie zlyhalo ({type(exc).__name__}: {_short(exc)}).")
            _mark_error(session, email_id, f"Spracovanie zlyhalo: {type(exc).__name__}: {_short(exc)}")
            continue
        summary["events"] += len(events)
        summary["event_ids"] += [ev.id for ev in events]
        for ev in events:
            if ev.status != "navrh":
                continue
            if ev.needs_confirmation or ev.kind == "ine" or email.verdict != OK:
                summary["waiting_confirmation"] += 1
                summary["waiting_event_ids"].append(ev.id)
                _note_pending(ev, applied_descr)
                continue
            _auto_apply(session, ev, summary, applied_descr)
    applied_descr += _backfill_pending(session, errors)
    applied_descr += _extend_open_ended(session, now, errors)
    _alert_uncertain(session, summary["waiting_event_ids"], errors)   # zrušenie trasy, výluka bez záznamu …
    # prepočet hneď: zapísané udalosti aj čakajúce výluky so záznamom „navrh“ (výluka do 72 h vždy čaká na
    # potvrdenie – plánovač s ňou už počíta, odporúčania sa nesmú opierať o starý stav)
    if applied_descr:
        trigger = "e-mail: " + "; ".join(applied_descr)
        trigger = trigger if len(trigger) <= 1000 else trigger[:999] + "…"
        try:
            from ..planovanie.planovac import evaluate_all

            recs = evaluate_all(session, trigger=trigger)
            session.commit()
            summary["planning"] = {"ok": True, "recommendations": len(recs) if isinstance(recs, list) else None}
        except NotImplementedError:
            session.rollback()
            summary["planning"] = {"ok": False, "error": "plánovač zatiaľ nie je k dispozícii"}
            errors.append("Plánovač zatiaľ nie je implementovaný – prepočet vlakov sa nespustil.")
        except Exception as exc:
            session.rollback()
            summary["planning"] = {"ok": False, "error": f"{type(exc).__name__}: {_short(exc)}"}
            errors.append(f"Prepočet plánu po e-mailoch zlyhal ({type(exc).__name__}: {_short(exc)}).")
    try:
        if summary["fetched"] or summary["events"] or summary["recovered_event_ids"] or errors:
            audit(session, "agent", "posta_cyklus", "", "", **{k: v for k, v in summary.items()
                                                                if k not in ("email_ids", "event_ids")})
        session.commit()
    except Exception as exc:  # pragma: no cover
        session.rollback()
        errors.append(f"Zápis auditu zlyhal ({type(exc).__name__}).")
    return summary


def _auto_apply(session: Session, ev: ExtractedEvent, summary: dict, applied_descr: list[str]) -> None:
    """Automatický zápis istej udalosti; pri akejkoľvek chybe ide udalosť do fronty (nič sa nestratí)."""
    ev_id = ev.id
    try:
        res = apply_event(session, ev, actor="agent")
        session.commit()
    except Exception as exc:
        session.rollback()
        msg = f"{type(exc).__name__}: {_short(exc)}"
        summary["errors"].append(f"Udalosť #{ev_id}: zápis zlyhal ({msg}).")
        _mark_waiting(session, ev_id, f"automatický zápis zlyhal ({msg}) – skontrolujte a potvrďte")
        summary["waiting_confirmation"] += 1
        summary["waiting_event_ids"].append(ev_id)
        waiting = session.get(ExtractedEvent, ev_id)
        if waiting is not None:
            _note_pending(waiting, applied_descr)
        return
    if res.get("ok"):
        summary["auto_applied"] += 1
        summary["applied_event_ids"].append(ev_id)
        applied_descr.append(describe_event(ev))
    else:
        if not ev.needs_confirmation:     # napr. podozrivý alebo preposlaný e-mail – aj tak patrí do fronty
            _mark_waiting(session, ev_id, f"automatický zápis odmietnutý: {res.get('error')}")
        summary["waiting_confirmation"] += 1
        summary["waiting_event_ids"].append(ev_id)
        _note_pending(ev, applied_descr)


def _note_pending(ev: ExtractedEvent, applied_descr: list[str]) -> None:
    """Čakajúca udalosť, ktorá mení podklady plánovača, spustí prepočet ešte v tomto cykle.

    Výluka so záznamom „navrh“ – plánovač s ňou počíta. Zrušenie či zmena pridelenej trasy a výluka bez záznamu
    (napr. termín sa nepodarilo prečítať) – plánovač s nimi počítať nevie, dotknutý vlak preto nesmie ostať
    ZELENÝ (:func:`pending_uncertainty`) a odporúčania sa nesmú opierať o starý stav."""
    try:
        info = (ev.validation or {}).get("pending_closure") or {}
        session = object_session(ev)
        if info.get("id"):
            applied_descr.append(describe_event(ev) + " – čaká na potvrdenie")
        elif session is not None and _blind_reason(session, ev) and _blind_targets(session, ev):
            applied_descr.append(describe_event(ev) + " – čaká na potvrdenie (do plánu sa zapíše až po potvrdení)")
    except Exception:  # pragma: no cover - udalosť po zlyhanom commite nemusí byť dostupná
        return


# ---------------------------------------------------------------------- čakajúce udalosti mimo plánovača
def _waiting(ev: ExtractedEvent) -> bool:
    """Udalosť čaká na dispečera: vo fronte na potvrdenie alebo potvrdená so zlyhaným zápisom."""
    return (ev.status == "navrh" and bool(ev.needs_confirmation)) or apply_failed(ev)


def _blind_reason(session: Session, ev: ExtractedEvent, with_rows: Optional[set] = None) -> str:
    """Prečo plánovač s čakajúcou udalosťou nevie počítať („“ = počíta s ňou alebo ju nepotrebuje).

    Čakajúce zrušenie či zmena pridelenej trasy: plánovač počíta s trasou v registri, ktorá nemusí platiť.
    Výluka, pomalá jazda či mimoriadnosť bez záznamu pre plánovač (termín sa nepodarilo prečítať, podozrivý
    e-mail, chyba zápisu): plánovač o nej nevie. Zrušenie výluky ani výluka, ktorá už v registri je, sem
    nepatria (plánovač počíta s výlukou v registri – bezpečnejší variant); ani výluka, ktorej záznam prevzala
    novšia zapísaná udalosť. ``with_rows`` – vopred zistené čísla udalostí, ktoré majú platný záznam výluky
    (jeden dotaz pre všetky udalosti namiesto dotazu pre každú)."""
    if not _waiting(ev):
        return ""
    val = ev.validation if isinstance(ev.validation, dict) else {}
    if ev.kind in UNCERTAIN_PATH_KINDS:
        # ponuka trasy z KIS pridelenú trasu nemení, kým ju dopravca neakceptuje
        return "" if (ev.data or {}).get("offer") else "čaká na potvrdenie dispečerom"
    if ev.kind == "vyluka_zrusenie":
        # zrušenie, ktoré v registri nemá čo zrušiť (môže ísť o novú výluku): bez záznamu poistky (chýba úsek či
        # začiatok, podozrivý e-mail) s ním plánovač nevie počítať
        if not _orphan(ev):
            return ""
        if with_rows is not None:
            return "" if ev.id in with_rows else ORPHAN_CANCEL_REASON
        return "" if any(c is not None for c in _event_closures(session, ev.id)) else ORPHAN_CANCEL_REASON
    if ev.kind not in CLOSURE_EVENT_KINDS:
        return ""
    info = val.get("pending_closure") if isinstance(val.get("pending_closure"), dict) else {}
    if info.get("proposed_change_of") or info.get("bez_vplyvu"):
        # výluka je v registri, zrušila ju novšia udalosť, alebo udalosť zámerne nemá záznam a plánovač nemení
        # (napr. oznámenie o ukončení mimoriadnosti – ``pending_closure.bez_vplyvu``)
        return ""
    if with_rows is not None:
        if ev.id in with_rows:
            return ""
    elif any(c is not None for c in _event_closures(session, ev.id)):
        return ""
    row = session.get(Closure, info["id"]) if isinstance(info.get("id"), int) else None
    if row is not None and _newer_owner(session, row, ev) is not None:
        # záznam udalosti medzitým prevzala novšia zapísaná udalosť (potvrdená zmena alebo zrušenie tej istej
        # výluky) – register má jej stav, ktorý má prednosť; staršia udalosť ho po potvrdení nezmení (nahradená)
        return ""
    applied = val.get("apply_result") if isinstance(val.get("apply_result"), dict) else {}
    blocking = ", ".join(str(x) for x in val.get("blocking") or [])
    why = ": ".join(x for x in (str(info.get("note") or ""), blocking) if x) or applied.get("error")
    return str(why or "údaje nestačia na záznam výluky")


def _blind_targets(session: Session, ev: ExtractedEvent) -> bool:
    """Má čakajúca udalosť bez záznamu pre plánovač čoho sa týkať (trasa v registri; výluka, pomalá jazda či
    mimoriadnosť – aj bez známej dopravne: tú plánovač nevie umiestniť, môže sa týkať každého vlaku – dispečer dostane
    upozornenie hneď a vlaky sa prepočítajú)?"""
    d = ev.data or {}
    if ev.kind in UNCERTAIN_PATH_KINDS:
        number = train_digits(d.get("train_number"))
        return bool(number) and any(p.status in ("pridelena", "zmenena")
                                    for p in _find_paths(session, None, number, d.get("date")))
    if ev.kind in CLOSURE_EVENT_KINDS or _orphan(ev):
        return True
    return bool(d.get("from_station") or d.get("to_station"))


def _route_cover(route: Any) -> Callable[[str, str, Any], bool]:
    """Funkcia „leží úsek výluky a – b (výluka v jednej dopravni: a = b) na trase vlaku?“ pre trasu z plánovača
    (``vlaky.siet.graf.Route``; trasa sa pripraví raz na vyhodnotenie vlaku).

    Rovnako ako plánovač umiestňuje výluky z registra (pravidlo 2): koľaje úseku v modeli siete (closure_edges)
    zasahujú hrany trasy – aj keď vlak začína alebo končí vnútri úseku; poistkou ležia na trase obe dopravne
    úseku. Úsek, ktorý sa v sieti nenašiel: dopravňa úseku leží na trase, alebo sa nenašla ani jedna dopravňa –
    vtedy ho nemožno vylúčiť (ako plánovač pri výluke z registra, ktorú nevie umiestniť na sieť)."""
    from ..siet.graf import expand_aliases, network, norm, norm_line_ref

    net = network()
    edges = set(route.edges)
    on_route: set = set()
    for st, _km, _mins in route.stations:
        n = norm(st.name)
        on_route |= {n, expand_aliases(n)}
    cache: dict = {}

    def keys(name: str) -> tuple[set, bool]:
        if name not in cache:
            n = norm(name or "")
            out = {n, expand_aliases(n)} - {""}
            st = net.find_station(name) if out else None
            if st is not None:
                m = norm(st.name)
                out |= {m, expand_aliases(m)}
            cache[name] = (out, st is not None)
        return cache[name]

    def covers(a: str, b: str, line_ref: Any = "") -> bool:
        (ka, found_a), (kb, found_b) = keys(a), keys(b)
        sec = net.closure_edges(a, b, norm_line_ref(str(line_ref or "")))
        if sec:
            return bool(sec & edges) or (bool(ka & on_route) and bool(kb & on_route))
        if not found_a and not found_b:
            return True
        return bool(ka & on_route) or bool(kb & on_route)

    return covers


def _day_text(day: Any) -> str:
    """Deň „RRRR-MM-DD“ ako „DD.MM.RRRR“ (slovenský zápis); bez dňa „?“."""
    try:
        return date.fromisoformat(str(day)).strftime("%d.%m.%Y")
    except ValueError:
        return str(day or "?")


class PendingNote(str):
    """Dôvod čakajúcej udalosti z e-mailu pre plánovač (text) s údajmi o udalosti: ``kind`` – druh udalosti
    (napr. ``trasa_zrusena``, ``trasa_zmena``), ``event_id`` a ``fleet`` – udalosť bez známeho miesta (bez dopravne
    aj trate) sa môže týkať ktoréhokoľvek vlaku. Je to obyčajný text (str) – kto údaje nepotrebuje, nič nemení."""

    kind: str = ""
    event_id: Optional[int] = None
    fleet: bool = False

    def __new__(cls, text: str, kind: str = "", event_id: Optional[int] = None, fleet: bool = False):
        obj = super().__new__(cls, text)
        obj.kind, obj.event_id, obj.fleet = kind, event_id, fleet
        return obj


def pending_uncertainty(session: Session, train: Train, route: Any = None, paths: Iterable[Any] = (),
                        window: Optional[tuple] = None) -> list[str]:
    """Dôvody, prečo vlak nesmie byť ZELENÝ pre čakajúce udalosti z e-mailov, s ktorými plánovač nevie počítať.

    Volá plánovač pri každom vyhodnotení vlaku (sekcia 2.1 – kým zmena nie je potvrdená, počíta sa s oboma
    možnosťami a dispečer to vidí):

    * zrušenie alebo zmena trasy vlaku čaká na potvrdenie a týka sa trasy, s ktorou plánovač počíta (``paths``
      – pridelené trasy vlaku),
    * výluka, pomalá jazda či mimoriadnosť z e-mailu čaká na potvrdenie bez záznamu pre plánovač a jej úsek leží
      na trase vlaku (``route`` – trasa vlaku z plánovača, ``vlaky.siet.graf.Route``; úsek sa umiestni na sieť
      rovnako ako výluka z registra, :func:`_route_cover`; None = trasa neznáma, posúdi sa konzervatívne).
      Bez známej dopravne (miesto sa nepodarilo prečítať, napr. len „trať 180 v km 212,4“) sa s číslom trate týka
      vlaku, ktorého trasa touto traťou vedie, bez neho každého vlaku.
      Známy začiatok či koniec mimo obdobia ``window`` (od, do – UTC) ju vylúči.

    Prázdny zoznam = žiadna taká udalosť. Chyba pri zisťovaní nesmie dať ZELENÚ – vráti všeobecný dôvod.
    Viac trás naraz (obchádzky, trasy objednávky) – :func:`pending_uncertainty_routes`."""
    path_notes, by_route = pending_uncertainty_routes(session, train, {"": route}, paths, window)
    return path_notes + by_route[""]


def pending_uncertainty_routes(session: Session, train: Any, routes: dict, paths: Iterable[Any] = (),
                               window: Optional[tuple] = None) -> tuple[list[str], dict]:
    """Ako :func:`pending_uncertainty` pre viac trás naraz ({názov: trasa} – hlavná trasa a obchádzky vlaku, trasy
    objednávky), jedným dotazom do databázy. Plánovač posudzuje ZELENÚ pri trase, ktorú odporúča (aj obchádzke).

    Vracia (dôvody k pridelenej trase vlaku – nezávisia od trasy v sieti, {názov trasy: dôvody k úsekom na tejto
    trase}); dôvody sú :class:`PendingNote` (druh a číslo udalosti, ``fleet`` – udalosť bez známeho miesta, môže sa
    týkať ktoréhokoľvek vlaku). Chyba pri zisťovaní nesmie dať ZELENÚ – každá trasa dostane všeobecný dôvod."""
    try:
        return _pending_uncertainty(session, train, routes, paths, window)
    except Exception as exc:  # noqa: BLE001 – plánovanie nesmie zastaviť, ale ani dať ZELENÚ naslepo
        logging.getLogger("vlaky.posta").exception("Čakajúce udalosti pre vlak %s sa nepodarilo overiť",
                                                   getattr(train, "id", None))
        note = (f"Čakajúce udalosti z e-mailov sa nepodarilo overiť ({type(exc).__name__}) – kým sa to nevyrieši, "
                "vlak nemôže byť ZELENÝ.")
        return [], {name: [note] for name in routes}


def _route_lines(route: Any) -> set:
    """Čísla tratí, po ktorých vedie trasa (veľkými písmenami, ako ``norm_line_ref``)."""
    from ..siet.graf import network

    net = network()
    out: set = set()
    for ei in route.edges:
        out |= net.line_refs(ei)
    return out


def _term_text(start: Optional[datetime], end: Optional[datetime]) -> str:
    if start is None and end is None:
        return "termín neznámy"
    return f"{fmt(start) if start is not None else '?'} – {fmt(end) if end is not None else '?'}"


def _pending_uncertainty(session: Session, train: Any, routes: dict, paths: Iterable[Any],
                         window: Optional[tuple]) -> tuple[list[str], dict]:
    from ..siet.graf import norm_line_ref

    out: dict = {name: [] for name in routes}
    # len udalosti, ktoré naozaj čakajú na dispečera (vo fronte alebo potvrdené so zlyhaným zápisom) – potvrdených
    # a zapísaných udalostí pribúda bez obmedzenia a plánovač sa volá pre každý vlak v každom cykle
    failed = ExtractedEvent.validation["apply_result"]["ok"].as_boolean().is_(False)
    waiting = or_(and_(ExtractedEvent.status == "navrh", ExtractedEvent.needs_confirmation.is_(True)),
                  and_(ExtractedEvent.status == "potvrdene", failed))
    rows = list(session.scalars(select(ExtractedEvent).where(
        ExtractedEvent.kind.in_(UNCERTAIN_PATH_KINDS + CLOSURE_EVENT_KINDS + ("vyluka_zrusenie",)), waiting)
        .order_by(ExtractedEvent.id)))
    if not rows:
        return [], out
    number = train_digits(train.number)
    path_ids = {p.id for p in paths}
    # poddotaz namiesto zoznamu čísel (bez limitu počtu parametrov databázy)
    waiting_closures = select(ExtractedEvent.id).where(
        ExtractedEvent.kind.in_(CLOSURE_EVENT_KINDS + ("vyluka_zrusenie",)), waiting)
    with_rows = set(session.scalars(select(Closure.source_event_id).where(
        Closure.source_event_id.in_(waiting_closures), Closure.status != "zrusene")))
    covers: dict = {}   # názov trasy → funkcia „leží úsek na trase?“ (pripraví sa raz na trasu)
    lines: dict = {}    # názov trasy → čísla tratí trasy
    path_notes: list[str] = []
    for ev in rows:
        why = _blind_reason(session, ev, with_rows)
        if not why:
            continue
        d = ev.data if isinstance(ev.data, dict) else {}
        label = KIND_LABELS.get(ev.kind, ev.kind)
        if ev.kind in UNCERTAIN_PATH_KINDS:
            if not number or train_digits(d.get("train_number")) != number or not path_ids:
                continue
            if not any(p.id in path_ids for p in _find_paths(session, None, number, d.get("date"))):
                continue
            what = ("pridelená trasa ŽSR nemusí platiť" if ev.kind == "trasa_zrusena"
                    else "časy pridelenej trasy sa môžu zmeniť")
            path_notes.append(PendingNote(
                f"{label.capitalize()} vlaku {number} na deň {_day_text(d.get('date'))} z e-mailu čaká na "
                f"potvrdenie dispečerom (udalosť #{ev.id}) – {what}; kým ju dispečer nepotvrdí alebo nezamietne, "
                "vlak nemôže byť ZELENÝ.", kind=ev.kind, event_id=ev.id))
            continue
        start, end = parse_utc(d.get("start")), parse_utc(d.get("end"))
        if window and ((start is not None and start > window[1]) or (end is not None and end < window[0])):
            continue
        a, b = d.get("from_station"), d.get("to_station")
        if ev.kind == "vyluka_zrusenie":
            # zrušenie bez cieľa bez záznamu poistky (napr. bez úseku či začiatku): môže ísť o novú výluku
            sec = f" {a or b} – {b or a}" if a or b else ""
            ref = f" {d['external_ref']}" if d.get("external_ref") and not sec else ""
            where = "" if sec else " – e-mail neuvádza úsek, môže sa týkať ktoréhokoľvek vlaku"
            note = PendingNote(
                f"Zrušenie výluky{sec}{ref} z e-mailu ({_term_text(start, end)}; udalosť #{ev.id}): {why}{where}; kým "
                "ho dispečer nevybaví, vlak nemôže byť ZELENÝ.", kind=ev.kind, event_id=ev.id, fleet=not sec)
            for name, route in routes.items():
                if sec and route is not None:
                    if name not in covers:
                        covers[name] = _route_cover(route)
                    if not covers[name](str(a or b), str(b or a), ""):
                        continue
                out[name].append(note)
            continue
        if a or b:
            sec = f"{a or b} – {b or a}"
            note = PendingNote(
                f"{label.capitalize()} {sec} z e-mailu čaká na potvrdenie (udalosť #{ev.id}) a plánovač s ňou "
                f"nevie počítať ({why}) – overte ju vo fronte na potvrdenie; kým sa to nevyrieši, vlak nemôže "
                "byť ZELENÝ.", kind=ev.kind, event_id=ev.id)
            for name, route in routes.items():
                if route is not None:
                    if name not in covers:
                        covers[name] = _route_cover(route)
                    if not covers[name](str(a or b), str(b or a), d.get("line_ref")):
                        continue
                out[name].append(note)
            continue
        # miesto sa nepodarilo prečítať (žiadna dopravňa, napr. len „trať 180 v km 212,4“): s číslom trate sa týka
        # vlaku, ktorého trasa touto traťou vedie, inak každého vlaku v jej období – nesmie ostať neviditeľná
        # (bez trate je to poistka pre celý vozový park – ``fleet``: dispečer o udalosti dostane jedno upozornenie)
        line = norm_line_ref(str(d.get("line_ref") or ""))
        where = f"trať {line}, " if line else ""
        who = (f"vlak ide po trati {line} – " if line else "môže sa týkať ktoréhokoľvek vlaku – ")
        note = PendingNote(
            f"{label.capitalize()} z e-mailu ({where}{_term_text(start, end)}; udalosť #{ev.id}) čaká na "
            f"potvrdenie a plánovač s ňou nevie počítať – miesto sa nepodarilo prečítať – overte vo fronte; "
            f"{who}kým sa to nevyrieši, vlak nemôže byť ZELENÝ.", kind=ev.kind, event_id=ev.id, fleet=not line)
        for name, route in routes.items():
            if line and route is not None:
                if name not in lines:
                    lines[name] = _route_lines(route)
                if line not in lines[name]:
                    continue
            out[name].append(note)
    return path_notes, out


def _alert_uncertain(session: Session, event_ids: Iterable[int], errors: list[str]) -> None:
    """Upozornenie dispečerom (DÔLEŽITÉ, hneď – nie až v súhrne) na nové čakajúce udalosti, s ktorými plánovač
    nevie počítať a ktoré sa môžu týkať vlaku (zrušenie/zmena pridelenej trasy, výluka bez záznamu), a na zrušenie,
    ktoré v registri nemá čo zrušiť (môže ísť o novú výluku – aj keď má záznam poistky pre plánovač)."""
    from ..notifikacie.odosielanie import send_internal
    from ..notifikacie.texty import count

    try:
        items = []
        for ev_id in dict.fromkeys(event_ids):
            ev = session.get(ExtractedEvent, ev_id)
            if ev is None:
                continue
            why = _blind_reason(session, ev)
            if why and _blind_targets(session, ev):
                items.append((ev, why))
            elif _orphan(ev) and _waiting(ev):
                items.append((ev, ORPHAN_CANCEL_REASON))
        if not items:
            return
        url = settings().public_url.rstrip("/")
        if len(items) == 1:
            subject = f"[AGENT][DÔLEŽITÉ] Na potvrdenie čaká {describe_event(items[0][0])}"
        else:
            verb = "čakajú" if 2 <= len(items) <= 4 else "čaká"
            subject = (f"[AGENT][DÔLEŽITÉ] Na potvrdenie {verb} {count(len(items), 'udalosť', 'udalosti', 'udalostí')} "
                       "z e-mailov, ktoré ovplyvňujú vlaky")
        lines = ["Tieto udalosti z e-mailov čakajú na potvrdenie a plánovač s nimi nevie počítať – vlaky, ktorých "
                 "sa môžu týkať, nedostanú ZELENÚ, kým ich nepotvrdíte (s opravou) alebo nezamietnete:", ""]
        if any(ev.kind == "trasa_zrusena" for ev, _w in items):
            # odriekol ju sám dispečer podľa prijatého odporúčania („pôvodnú trasu odrieknuť, nová trasa na …“) –
            # odchod na novú, objednanú trasu plánovač neobmedzí, len pripomenie potvrdenie
            lines[0] = lines[0][:-1] + " (pri zrušení trasy okrem odchodu na novú trasu objednanú podľa prijatého " \
                "odporúčania):"
        for ev, why in items:
            lines.append(f"- {describe_event(ev)}: {why}")
            lines.append(f"  {url}/fronta/udalost/{ev.id}")    # odkaz vždy ukáže udalosť (aj na ďalšej strane fronty)
        send_internal(session, "upozornenie", subject[:300], "\n".join(lines), "DÔLEŽITÉ",
                      dedup_key=("neista_udalost:" + ",".join(str(ev.id) for ev, _w in items))[:120])
        session.commit()
    except Exception as exc:  # upozornenie nesmie zastaviť spracovanie pošty
        session.rollback()
        errors.append(f"Upozornenie na čakajúce udalosti sa neodoslalo ({type(exc).__name__}: {_short(exc)}).")


def _mark_waiting(session: Session, ev_id: int, reason: str) -> None:
    """Udalosť, ktorú sa nepodarilo zapísať automaticky, presunie do fronty na potvrdenie (samostatný commit)."""
    try:
        lock_register(session)                     # záznam „navrh“ sa priraďuje k výluke v registri po jednom
        ev = session.get(ExtractedEvent, ev_id)
        if ev is None or ev.status != "navrh":
            return
        val = dict(ev.validation or {})
        val["apply_error"] = reason
        ev.validation = val
        _require_confirmation(session, ev, [reason])
        session.commit()
    except Exception:  # pragma: no cover - databáza nedostupná; udalosť sa dokončí v ďalšom cykle
        session.rollback()


def _recover_unapplied(session: Session, summary: dict, applied_descr: list[str]) -> None:
    """Udalosti, ktoré po páde ostali „navrh“ bez potvrdenia a bez zápisu (pád medzi extrakciou a zápisom)."""
    try:
        rows = list(session.scalars(
            select(ExtractedEvent).join(Email, Email.id == ExtractedEvent.email_id)
            .where(ExtractedEvent.status == "navrh", ExtractedEvent.needs_confirmation.is_(False),
                   ExtractedEvent.confirmed_at.is_(None), Email.status == "spracovany")
            .order_by(ExtractedEvent.id)))
    except Exception as exc:  # pragma: no cover
        session.rollback()
        summary["errors"].append(f"Kontrola nedokončených udalostí zlyhala ({type(exc).__name__}).")
        return
    for ev in rows:
        summary["recovered_event_ids"].append(ev.id)
        email = session.get(Email, ev.email_id)
        if ev.kind == "ine" or email is None or email.verdict != OK:
            _mark_waiting(session, ev.id, "udalosť ostala nedokončená – skontrolujte ju")
            summary["waiting_confirmation"] += 1
            summary["waiting_event_ids"].append(ev.id)
            _note_pending(ev, applied_descr)
            continue
        _auto_apply(session, ev, summary, applied_descr)


def _backfill_pending(session: Session, errors: list[str]) -> list[str]:
    """Čakajúce výluky bez záznamu „navrh“ (napr. z verzie pred touto úpravou) – doplní ich pre plánovač.

    Vráti popisy udalostí, ktorým záznam pribudol (plán sa prepočíta v tom istom cykle)."""
    written: list[str] = []
    try:
        rows = list(session.scalars(select(ExtractedEvent).where(
            ExtractedEvent.status == "navrh", ExtractedEvent.needs_confirmation.is_(True),
            ExtractedEvent.kind.in_(CLOSURE_EVENT_KINDS)).order_by(ExtractedEvent.id)))
        for ev in rows:
            lock_register(session)                 # po jednom so súbežným potvrdením (stav sa číta znova)
            if ev.status != "navrh" or "pending_closure" in (ev.validation or {}):
                continue
            row = _write_pending_safe(session, ev)
            if row is None and "pending_closure" not in (ev.validation or {}):
                _set_pending_info(ev, None, None, "bez záznamu")
            session.commit()
            if row is not None:
                written.append(describe_event(ev) + " – čaká na potvrdenie")
    except Exception as exc:
        session.rollback()
        errors.append(f"Doplnenie nepotvrdených výluk pre plánovač zlyhalo ({type(exc).__name__}: {_short(exc)}).")
    return written


def _extend_open_ended(session: Session, now: datetime, errors: list[str]) -> list[str]:
    """Výluky a obmedzenia „do odvolania“: technický koniec sa posúva, kým ich ŽSR neodvolá, a dispečer
    dostane pripomienku, aby platnosť overil (inak by po 30 dňoch potichu vypadli z plánovania).

    Vráti popisy predĺžených výluk (plán sa prepočíta v tom istom cykle)."""
    extended: list = []
    try:
        lock_register(session)        # po jednom so súbežným potvrdením zmeny výluky (nový koniec sa nestratí)
        # záznamy z predchádzajúcej verzie mali len poznámku (bez príznaku) – príznak sa doplní
        for c in session.scalars(select(Closure).where(Closure.until_revoked.is_(None),
                                                       Closure.note.contains(_LEGACY_OPEN_END_NOTE))):
            c.until_revoked = True
        rows = list(session.scalars(select(Closure).where(
            Closure.until_revoked.is_(True), Closure.status.in_(("navrh", "overene")),
            Closure.end_utc <= now + timedelta(days=OPEN_END_REMIND_DAYS))))
        extended = []
        for c in rows:
            old_end = _utc(c.end_utc)
            new_end = now + timedelta(days=OPEN_END_DAYS)
            if new_end <= old_end:
                continue
            c.end_utc = new_end
            c.note = "\n".join(filter(None, [c.note or "", f"Platí do odvolania – technický koniec posunutý z "
                                                           f"{fmt(old_end)} na {fmt(new_end)} ({fmt(now)})."]))
            audit(session, "agent", "vyluka_do_odvolania_predlzena", "Closure", c.id, povodny_koniec=old_end,
                  novy_koniec=new_end, plati_od=c.start_utc)
            extended.append(c)
        session.commit()
    except Exception as exc:
        session.rollback()
        errors.append(f"Posun výluk do odvolania zlyhal ({type(exc).__name__}: {_short(exc)}).")
        return []
    descr = [f"výluka do odvolania #{c.id} {c.from_station} – {c.to_station} (technický koniec posunutý)"
             for c in extended]
    for c in extended:
        try:
            _remind_open_ended(session, c)
            session.commit()
        except Exception as exc:  # pripomienka nesmie zastaviť spracovanie pošty
            session.rollback()
            errors.append(f"Pripomienka výluky do odvolania #{c.id} sa neodoslala ({type(exc).__name__}).")
    return descr


def _remind_open_ended(session: Session, c: Closure) -> None:
    from ..notifikacie.odosielanie import send_internal

    label = {"pomala_jazda": "Pomalá jazda", "mimoriadnost": "Mimoriadnosť"}.get(c.kind, "Výluka")
    ref = f" (č. {c.external_ref})" if c.external_ref else ""
    speed = f", {c.speed_kmh:g} km/h" if c.speed_kmh else ""
    subject = f"[AGENT][DÔLEŽITÉ] {label} do odvolania {c.from_station} – {c.to_station}{ref} stále platí"
    body = (f"{label} {c.from_station} – {c.to_station}{ref}{speed} platí do odvolania od {fmt(c.start_utc)} "
            f"(v registri: {'overená' if c.status == 'overene' else 'NEPOTVRDENÁ'}).\n"
            "ŽSR ju zatiaľ neodvolala, preto s ňou agent naďalej počíta pri plánovaní; technický koniec v registri "
            f"posunul na {fmt(c.end_utc)}.\n\nOverte, či obmedzenie stále platí. Ak ho ŽSR odvolala, zrušte ho "
            f"v registri výluk: {settings().public_url.rstrip('/')}/vyluky")
    send_internal(session, "upozornenie", subject[:300], body, "DÔLEŽITÉ",
                  dedup_key=f"do_odvolania:{c.id}:{_utc(c.end_utc):%Y%m%d}", channels=["email"])


def _mark_error(session: Session, email_id: int, msg: str) -> None:
    try:
        e = session.get(Email, email_id)
        if e is not None:
            e.status = "chyba"
            e.error = ((e.error + "\n") if e.error else "") + msg
            session.commit()
    except Exception:  # pragma: no cover
        session.rollback()


# ====================================================================== popis udalosti
def describe_event(ev: ExtractedEvent) -> str:
    """Krátky slovenský popis udalosti (pre audit, spúšťač prepočtu a notifikácie)."""
    d = ev.data or {}
    label = KIND_LABELS.get(ev.kind, ev.kind)

    def t(key: str) -> str:
        dt = parse_utc(d.get(key))
        return fmt(dt) if dt else "?"

    if ev.kind in ("vyluka_nova", "vyluka_zmena", "pomala_jazda", "mimoriadnost"):
        if d.get("from_station") or d.get("to_station"):
            sec = f"{d.get('from_station') or '?'} – {d.get('to_station') or '?'}"
        else:  # ani jedna dopravňa – nie „? – ?“, dispečer má vedieť, že miesto treba doplniť
            sec = "(miesto sa nepodarilo prečítať)"
        extra = f", {d['speed_kmh']:g} km/h" if isinstance(d.get("speed_kmh"), (int, float)) else ""
        daily = f", denne {d['daily_from']}–{d['daily_to']}" if d.get("daily_from") else ""
        if not d.get("start") and not d.get("end") and not d.get("until_revoked") and ev.kind != "mimoriadnost":
            return f"{label} {sec} (termín sa nepodarilo prečítať){daily}{extra} (#{ev.id})"
        # chýbajúci koniec je „do odvolania“ len pri výslovnom príznaku a pri mimoriadnosti, inak neznámy termín
        end = t("end") if d.get("end") else ("do odvolania" if d.get("until_revoked") or ev.kind == "mimoriadnost"
                                             else "?")
        return f"{label} {sec} {t('start')} – {end}{daily}{extra} (#{ev.id})"
    if ev.kind == "vyluka_zrusenie":
        what = d.get("external_ref") or f"{d.get('from_station') or '?'} – {d.get('to_station') or '?'}"
        return f"{label} {what} (#{ev.id})"
    if ev.kind in ("trasa_pridelena", "trasa_zmena", "trasa_zrusena"):
        pts = d.get("points") or []
        rel = f" {pts[0].get('station')} → {pts[-1].get('station')}" if len(pts) >= 2 else ""
        offer = " (ponuka KIS)" if d.get("offer") else ""
        return f"{label}{offer} vlak {d.get('train_number') or '?'} dňa {_day_text(d.get('date'))}{rel} (#{ev.id})"
    if ev.kind == "avizo":
        return f"{label}: vlak {d.get('train_number') or '?'}, {d.get('border') or '?'}, ETA {t('eta')} (#{ev.id})"
    if ev.kind == "objednavka":
        return (f"{label} {d.get('customer') or '?'}: {d.get('origin') or '?'} → {d.get('destination') or '?'}, "
                f"dodať do {t('deadline') if d.get('deadline') else '?'} (#{ev.id})")
    return f"{label} (#{ev.id})"
