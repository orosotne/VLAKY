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
"""
from __future__ import annotations

import copy
import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import DateTime, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..cas import fmt, local, now_utc
from ..config import settings
from ..models import Attachment, Closure, Email, ExtractedEvent, Handover, Order, Path as TrainPath, Train
from .datumy import parse_hhmm
from .extrakcia import extract
from .overenie import KARANTENA, OK, PODOZRIVY, verify, zsr_sender
from .parser import parse_message
from .stanice import stations
from .text import fold, header_text
from .udalosti import KIND_LABELS, SCHEMAS, MailDocument
from .validacia import find_train, parse_number, parse_utc, resolve_border, train_digits, validate_event
from .zdroje import MailSourceError, RawMail, get_source, kv_get, kv_set

OPEN_END_DAYS = 30            # výluka „do odvolania“ – technický koniec, kým ŽSR neoznámi skutočný (posúva sa)
OPEN_END_REMIND_DAYS = 7      # keď je technický koniec bližšie, posunie sa a dispečer dostane pripomienku
MATCH_TOLERANCE = timedelta(hours=1)
CHANGE_MATCH_WINDOW = timedelta(days=3)    # zmena bez čísla výluky: pôvodný termín najviac ± 3 dni
STORE_RETRIES = 3             # po toľkých neúspešných pokusoch o uloženie sa e-mail odloží do karantény
_RETRY_KEY = "posta_neulozene"
_LEGACY_OPEN_END_NOTE = "Koniec neurčený (do odvolania)"     # poznámka starších záznamov bez príznaku
# udalosti, ktoré majú (aj nepotvrdené) záznam v registri výluk – plánovač s nimi počíta
CLOSURE_EVENT_KINDS = ("vyluka_nova", "vyluka_zmena", "pomala_jazda", "mimoriadnost")
# udalosti, ktoré ako správca infraštruktúry vydáva len ŽSR (od iného odosielateľa vždy na potvrdenie)
ZSR_KINDS = ("vyluka_nova", "vyluka_zmena", "vyluka_zrusenie", "pomala_jazda", "mimoriadnost", "trasa_pridelena",
             "trasa_zmena", "trasa_zrusena")


class ApplyError(Exception):
    """Udalosť nemožno zapísať do registra (správa je pre dispečera)."""


class NeedsConfirmation(Exception):
    """Udalosť sa nesmie zapísať automaticky – zmenila by overený alebo blízky stav (dôvody pre dispečera)."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _short(exc: BaseException, limit: int = 300) -> str:
    return header_text(str(exc))[:limit]


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


def _fetch_and_store(session: Session, errors: list[str]) -> tuple[list[Email], bool]:
    """Vráti (uložené e-maily, či sa zdroj pošty podarilo prečítať)."""
    try:
        src = get_source(session)
        raws = src.fetch_new()
    except MailSourceError as exc:
        errors.append(str(exc))
        return [], False
    except Exception as exc:  # zdroj pošty nesmie zastaviť agenta
        errors.append(f"Zdroj pošty zlyhal: {type(exc).__name__}: {_short(exc)}")
        return [], False
    stored: list[Email] = []
    retries = _Retries(session)
    for raw in raws:
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
                break
        if email is not None:
            stored.append(email)
    return stored, True


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
    """Message-ID kópie od iného odosielateľa (rovnaké Message-ID, iný obsah) – jednoznačné, do 300 znakov."""
    return f"{pm.message_id[:280]}#{pm.sha256[:16]}"


def _store_one(session: Session, raw: RawMail) -> Optional[Email]:
    head = parse_message(raw.data, extract_attachments=False)
    existing = session.scalar(select(Email).where(Email.message_id == head.message_id))
    collision = None
    if existing is not None and existing.verdict != KARANTENA:
        if (existing.sender or "").lower() == _sender_key(head):
            return None                               # už spracované (idempotencia)
        # rovnaké Message-ID od iného odosielateľa: nesmie potichu „zhltnúť“ e-mail (napr. pravý ROV od ŽSR,
        # ktorého Message-ID použil skôr iný povolený odosielateľ) – uloží sa ako samostatný e-mail
        if session.scalar(select(Email.id).where(Email.message_id == _collision_id(head))) is not None:
            return None                               # túto kópiu už máme
        collision, existing = existing, None
    ver = verify(head)
    pm = head
    if ver.verdict != KARANTENA:
        pm = parse_message(raw.data)                  # prílohy len od povoleného a overeného odosielateľa
        ver = verify(pm)
    if existing is not None and ver.verdict == KARANTENA:
        return None                                   # ďalšia kópia v karanténe – nič nové
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
                  subject=header_text(pm.subject)[:500], body_text=pm.body_text, auth_results="\n".join(pm.auth_results),
                  verdict=ver.verdict, verdict_reason=ver.reason_text,
                  status="spracovany" if ver.verdict == KARANTENA else "prijaty", raw_path=raw_path,
                  forwarded_from=",".join(ver.forwarded_from))
    if existing is not None:
        return _replace_quarantined(session, existing, pm, fields, errors)
    message_id = pm.message_id
    if collision is not None:
        message_id = _collision_id(pm)
        errors.append(f"Rovnaké Message-ID {pm.message_id[:200]} už má e-mail #{collision.id} od iného odosielateľa "
                      f"({collision.sender}) – uložený ako samostatný e-mail, overte pravosť oboch.")
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
              skorsi_odosielatel=collision.sender, skorsi_verdikt=collision.verdict)
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
                           sender=email.sender or "", received_at=email.received_at)
        result = extract(doc, use_llm=email.verdict == OK)
        prepared: list[ExtractedEvent] = []
        for it in result.items:
            conf = it.confidence
            notes = list(it.notes)
            if email.verdict == PODOZRIVY:
                conf = min(conf, 0.5)
                notes.append("Podozrivý e-mail (možný pokus o prompt injection) – spracované len ako dáta, "
                             "istota znížená, vždy vyžaduje potvrdenie.")
            original = copy.deepcopy(it.data)
            vr = validate_event(session, it.kind, it.data, confidence=conf, verdict=email.verdict)
            extra = _source_reasons(email, it)
            target_reasons, target_warnings = _register_check(session, it.kind, vr.data, now)
            reasons = list(vr.reasons) + [r for r in extra + target_reasons if r not in vr.reasons]
            validation = {**vr.as_dict(), "notes": notes, "field_confidence": it.field_confidence,
                          "evidence": it.evidence, "original_data": original, "extraction": result.meta,
                          "field_source": dict(it.field_source), "unverified_fields": list(it.unverified)}
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


def _register_check(session: Session, kind: str, data: dict, now: datetime) -> tuple[list[str], list[str]]:
    """Čo by udalosť zmenila v registri: dôvody na potvrdenie (M5, 72 h) a informácie pre dispečera."""
    reasons: list[str] = []
    warnings: list[str] = []
    try:
        if kind in CLOSURE_EVENT_KINDS:
            fields, _notes = _closure_values(kind, data, now)
            target, how = _find_closure(session, None, kind, data, fields["start_utc"], loose=kind == "vyluka_zmena")
            if target is None and kind == "vyluka_zmena":
                reasons.append("zmena sa nedá priradiť k výluke v registri (podľa čísla ani úseku a termínu) – "
                               "overte, ktorej výluky sa týka")
            elif target is not None and _closure_changes(target, fields):
                reasons += _closure_guard(session, target, now, None)
                warnings.append(f"Mení výluku #{target.id} {target.from_station} – {target.to_station} "
                                f"{fmt(target.start_utc)} – {fmt(target.end_utc)} (priradená podľa: {how}).")
            if kind != "vyluka_zmena" and data.get("external_ref"):
                for c in _active_closures(session):
                    if (target is None or c.id != target.id) and c.external_ref and \
                            refs_match(c.external_ref, data["external_ref"]) and \
                            not _same_section(c, data.get("from_station"), data.get("to_station")):
                        warnings.append(f"Výluka s rovnakým číslom {data['external_ref']} je v registri na inom úseku "
                                        f"(#{c.id} {c.from_station} – {c.to_station}) – zapíše sa ako samostatná "
                                        "výluka (číslo môže patriť inej OR).")
        elif kind == "vyluka_zrusenie":
            target, how = _find_closure(session, None, kind, data, parse_utc(data.get("start")), loose=True)
            if target is None:
                warnings.append("Výluka na zrušenie sa v registri nenašla (podľa čísla ani úseku).")
            else:
                warnings.append(f"Zruší výluku #{target.id} {target.from_station} – {target.to_station} "
                                f"{fmt(target.start_utc)} – {fmt(target.end_utc)} (priradená podľa: {how}).")
        elif kind == "trasa_zmena":
            cands = _find_paths(session, None, train_digits(data.get("train_number")), data.get("date"))
            if len(cands) == 1:
                reasons += _path_guard(session, cands[0], now, None)
        elif kind == "avizo":
            eta = parse_utc(data.get("eta"))
            day = data.get("date") or (local(eta).date().isoformat() if eta else None)
            train = find_train(session, data.get("train_number"), day)
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
        return {"ok": False, "error": str(exc)}
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
          popis=describe_event(event), vysledok={k: v for k, v in result.items() if k not in ("before", "train_before")})
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


def _newer_owner(session: Session, obj: Any, ev: ExtractedEvent) -> Optional[ExtractedEvent]:
    """Novšia zapísaná udalosť, ktorej záznam už patrí (staršia ho nesmie prepísať)."""
    oid = getattr(obj, "source_event_id", None)
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
        return None
    if kind == "mimoriadnost":
        return "mimoriadnost"
    if kind == "pomala_jazda" or d.get("kind") == "pomala_jazda":
        return "pomala_jazda"
    return "vyluka"


def _track_ok(c: Closure, track: Any) -> bool:
    a, b = str(c.track or "").strip(), str(track or "").strip()
    return not a or not b or a == b


def _active_closures(session: Session, exclude_event: Optional[int] = None, cancelled: bool = False) -> list:
    """Platné záznamy výluk bez dočasných záznamov navrhovaných zmien (``cancelled`` = len zrušené)."""
    q = select(Closure).where(Closure.proposed_change_of.is_(None))
    q = q.where(Closure.status == "zrusene") if cancelled else q.where(Closure.status != "zrusene")
    return [c for c in session.scalars(q) if exclude_event is None or c.source_event_id != exclude_event]


def _find_closure(session: Session, ev_id: Optional[int], kind: str, d: dict, start: Optional[datetime],
                  loose: bool = False, cancelled: bool = False,
                  skip_own: bool = False) -> tuple[Optional[Closure], str]:
    """Záznam výluky, ktorého sa udalosť týka.

    Podľa čísla len pri rovnakom úseku (číslo ROV môže mať aj iná OR) a rovnakom druhu (výluka / pomalá jazda
    / mimoriadnosť); bez čísla podľa úseku a začiatku (± 1 h) s rovnakou koľajou; zmena a zrušenie aj podľa
    úseku a blízkeho termínu (± 3 dni). Viac kandidátov = nejednoznačné → nič. ``skip_own`` – vlastné
    záznamy udalosti (napr. jej návrh „navrh“) sa nevracajú, hľadá sa len výluka v registri.
    """
    if ev_id is not None and not cancelled and not skip_own:
        own = session.scalar(select(Closure).where(Closure.source_event_id == ev_id, Closure.status != "zrusene",
                                                   Closure.proposed_change_of.is_(None)))
        if own is not None:
            return own, "rovnaká udalosť"
    active = _active_closures(session, ev_id, cancelled)
    fam = _event_family(kind, d)
    a, b, ref, track = d.get("from_station"), d.get("to_station"), d.get("external_ref"), d.get("track")

    def fits(c: Closure) -> bool:
        return fam is None or _closure_family(c) == fam

    def ref_ok(c: Closure) -> bool:
        return not ref or not c.external_ref or refs_match(c.external_ref, ref)

    if ref:
        hits = [c for c in active if c.external_ref and refs_match(c.external_ref, ref) and fits(c)
                and (not a or _same_section(c, a, b))]
        if len(hits) == 1:
            return hits[0], "číslo výluky a úsek" if a else "číslo výluky"
        if len(hits) > 1:
            return None, ""
    if a and start is not None:
        hits = [c for c in active if _same_section(c, a, b) and abs(_utc(c.start_utc) - start) <= MATCH_TOLERANCE
                and fits(c) and _track_ok(c, track) and ref_ok(c)]
        if len(hits) == 1:
            return hits[0], "úsek a začiatok"
    if loose and a:
        hits = [c for c in active if _same_section(c, a, b) and fits(c) and _track_ok(c, track) and ref_ok(c)]
        if start is not None:
            hits = [c for c in hits if abs(_utc(c.start_utc) - start) <= CHANGE_MATCH_WINDOW]
        if len(hits) == 1:
            return hits[0], "úsek a blízky termín" if start is not None else "úsek"
    return None, ""


def _utc(dt: datetime) -> datetime:
    from ..cas import ensure_utc

    return ensure_utc(dt)


def _closure_values(kind: str, d: dict, now: datetime) -> tuple[dict, list[str]]:
    """Polia záznamu výluky z dát udalosti (bez stavu a pôvodu) a poznámky k nim."""
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
        restriction = str(d.get("kind") or "uplna")[:30]
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
    je zmena výluky a ``t`` by podľa pravidiel jej druhu nesedela (napr. nová výluka v blízkom termíne je ďalší
    termín, nie presun – rovnaká výluka je len s rovnakým číslom alebo začiatkom ± 1 h).
    """
    ref = d.get("external_ref")
    a, b = d.get("from_station"), d.get("to_station")
    fam = _event_family(ev.kind, d)
    if not (_same_section(t, a, b) and _closure_family(t) == fam
            and (not ref or not t.external_ref or refs_match(t.external_ref, ref))):
        return False
    if ref and not t.external_ref:
        for c in _active_closures(session, ev.id):
            if c.id != t.id and c.external_ref and refs_match(c.external_ref, ref) and _closure_family(c) == fam \
                    and _same_section(c, a, b):
                return False
    if ev.kind != "vyluka_zmena":
        return bool(ref and t.external_ref) or abs(_utc(t.start_utc) - start) <= MATCH_TOLERANCE
    return True


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
    own, temp = _event_closures(session, ev.id)
    # vlastný záznam nezapísanej udalosti je len jej návrh „navrh“ (nie výluka v registri); pri zmene výluky
    # s číslom (napr. doplneným dispečerom) sa najprv hľadá výluka, ktorej sa zmena týka
    placeholder = own is not None and not was_applied
    rebind = placeholder and ev.kind == "vyluka_zmena" and bool(d.get("external_ref"))
    target, how, promote = None, "", False
    if temp is not None and temp.proposed_change_of:
        t = session.get(Closure, temp.proposed_change_of)
        if t is not None and t.status != "zrusene" and _agrees(session, t, ev, d, fields["start_utc"]):
            target, how = t, "navrhovaná zmena výluky"
    if target is None and own is not None and not rebind:
        target, how = own, "rovnaká udalosť"
    if target is None:
        target, how = _find_closure(session, ev.id, ev.kind, d, fields["start_utc"], loose=ev.kind == "vyluka_zmena",
                                    skip_own=True)
    if target is None and own is not None:
        target, how = own, "rovnaká udalosť"                       # výluka v registri nie je – návrh sa stáva záznamom
    if target is None and temp is not None and confirmed:
        target, how, promote = temp, "dočasný záznam", True        # pôvodná výluka medzitým zanikla alebo nesedí
        if ev.kind == "vyluka_zmena":
            notes.append("Zmena sa týka výluky, ktorá v registri nie je – založená ako nová.")
    if target is None:
        gone, how = _find_closure(session, ev.id, ev.kind, d, fields["start_utc"], cancelled=True)
        owner = _newer_owner(session, gone, ev) if gone is not None else None
        if owner is not None:     # výluku medzitým zrušila novšia udalosť – staršia ju neobnoví
            return _superseded("Closure", gone, how, owner)
        if not confirmed and ev.kind == "vyluka_zmena":
            raise NeedsConfirmation(["zmena sa nedá priradiť k výluke v registri – overte, ktorej výluky sa týka"])
        if ev.kind == "vyluka_zmena":
            notes.append("Zmena sa týka výluky, ktorá v registri nie je – založená ako nová.")
        c = Closure(note="\n".join(notes + [source_line]), status="overene" if confirmed else "navrh", source="email",
                    source_event_id=ev.id, **fields)
        session.add(c)
        session.flush()
        return {"entity": "Closure", "id": c.id, "action": "vytvorene", "status": c.status, "before": None}
    owner = _newer_owner(session, target, ev)
    if owner is not None:
        _drop_pending(session, ev, f"Udalosť #{ev.id} nahradila novšia udalosť #{owner.id}.", own=rebind,
                      delete_own=True)
        return _superseded("Closure", target, how, owner)
    changes = _closure_changes(target, fields)
    first_write = promote or (target is own and not was_applied)    # návrh tejto udalosti sa stáva záznamom
    if not confirmed and target is not own and changes:
        reasons = _closure_guard(session, target, now, ev.id)
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
    applied = bool((ev.validation or {}).get("applied"))
    c = None
    if applied:              # opakovaný zápis – výluka, ktorú táto udalosť už zrušila
        c = session.scalar(select(Closure).where(Closure.source_event_id == ev.id,
                                                 Closure.proposed_change_of.is_(None)))
    how = "rovnaká udalosť"
    if c is None:
        # vlastný záznam „navrh“ nezapísanej udalosti ostal z jej pôvodného druhu (dispečer ju pri potvrdení
        # preklasifikoval na zrušenie) – nie je to rušená výluka; ruší sa výluka v registri
        c, how = _find_closure(session, ev.id, ev.kind, d, parse_utc(d.get("start")), loose=True, skip_own=True)
    if c is None:
        raise ApplyError("Výluka na zrušenie sa v registri nenašla (podľa čísla ani úseku).")
    if not applied and confirmed:
        _drop_pending(session, ev, "", delete_own=True)
    owner = _newer_owner(session, c, ev)
    if owner is not None:        # výluku medzitým zmenila novšia udalosť – ostáva platná (bezpečnejšie)
        return _superseded("Closure", c, how, owner)
    if not confirmed:
        raise NeedsConfirmation(["zrušenie výluky vždy potvrdzuje dispečer"])
    before = _snapshot(c, _CLOSURE_FIELDS)
    c.status = "zrusene"
    c.version = (c.version or 1) + 1
    c.source_event_id = ev.id
    c.note = "\n".join(filter(None, [c.note or "", f"Zrušené podľa udalosti #{ev.id} (e-mail #{ev.email_id}, {how})."]))
    session.flush()
    return {"entity": "Closure", "id": c.id, "action": "zrusene", "status": c.status, "matched_by": how,
            "before": before}


# ---------------------------------------------------------------------- nepotvrdené výluky (záznam „navrh“)
def _set_pending_info(ev: ExtractedEvent, closure_id: Optional[int], target_id: Optional[int], note: str) -> None:
    val = dict(ev.validation or {})
    val["pending_closure"] = {"id": closure_id, "proposed_change_of": target_id, "note": note}
    ev.validation = val


def _write_pending(session: Session, ev: ExtractedEvent) -> Optional[Closure]:
    """Výluka čakajúca na potvrdenie: záznam „navrh“, aby s ňou plánovač počítal (sekcia 2.1).

    Nová výluka / pomalá jazda / mimoriadnosť → vlastný záznam „navrh“. Zmena existujúcej výluky → pôvodný
    záznam ostane, nový termín je dočasný záznam „navrh“ (``proposed_change_of``). Zrušenie nič nemení.
    """
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
        fields, notes = _closure_values(ev.kind, d, now_utc())
    except ApplyError as exc:
        _set_pending_info(ev, None, None, str(exc))
        return None
    own, temp = _event_closures(session, ev.id)
    row = temp or own
    target_id = row.proposed_change_of if row is not None else None
    if row is None:
        target, _how = _find_closure(session, ev.id, ev.kind, d, fields["start_utc"], loose=ev.kind == "vyluka_zmena")
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


def _write_pending_safe(session: Session, ev: ExtractedEvent) -> Optional[Closure]:
    """:func:`_write_pending` v samostatnom SAVEPOINT – chyba zápisu záznamu „navrh“ (napr. príliš dlhá hodnota
    pre PostgreSQL) nesmie zahodiť udalosť ani ostatné udalosti e-mailu (sekcia 2.1: nič sa nesmie stratiť).
    Udalosť ostane vo fronte s upozornením, že plánovač s ňou do potvrdenia nepočíta."""
    try:
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
    for c in list(session.scalars(select(Closure).where(Closure.source_event_id == ev.id, Closure.status != "zrusene"))):
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
        raise ApplyError(f"Trasa vlaku {number} na deň {d.get('date') or '?'} sa v registri nenašla.")
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
    train = find_train(session, d.get("train_number"), d.get("date") or local(eta).date().isoformat())
    if train is None:
        val = dict(ev.validation or {})
        msg = f"vlak nenájdený – vlak {d.get('train_number')} nie je v systéme; založte ho a avízo potvrďte znova"
        if msg not in (val.get("errors") or []):
            val["errors"] = list(val.get("errors") or []) + [msg]
        reasons = list(val.get("confirmation_reasons") or [])
        if "vlak nenájdený" not in reasons:
            reasons.append("vlak nenájdený")
        val["confirmation_reasons"] = reasons
        ev.validation = val
        ev.needs_confirmation = True
        session.flush()
        raise ApplyError(f"Vlak {d.get('train_number')} nenájdený – avízo čaká na priradenie dispečerom.")
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
        if not same:
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


def confirm_event(session: Session, event_id: int, actor: str, edits: Optional[dict] = None) -> ExtractedEvent:
    """Dispečer potvrdí (a prípadne opraví) udalosť; potom sa zapíše do registrov.

    ``edits`` – opravy polí dát (napr. ``{"to_station": "Ružomberok"}``); kľúč ``event_kind`` zmení druh
    udalosti. Opravy sa uložia (pôvodná extrakcia zostáva v ``validation.original_data`` – trénovacie príklady).
    Výsledok zápisu je v ``event.validation["apply_result"]``; ak zápis zlyhal, stav ostane ``potvrdene``
    a udalosť možno po odstránení príčiny potvrdiť znova.
    Záznam „navrh“ čakajúcej výluky sa stane overeným (s opravami dispečera); pri zmene výluky sa zmena
    zapíše do pôvodného záznamu a dočasný záznam sa odstráni. Úpravy, ktoré dispečer urobil v registri výluk
    v zázname udalosti, sa zachovajú (``validation.register_fields``); výslovná oprava vo fronte má prednosť.
    Staršia udalosť stav zapísaný novšou udalosťou neprepíše (``apply_result.action = "nahradene"``).
    """
    ev = session.get(ExtractedEvent, event_id)
    if ev is None:
        raise ValueError(f"Udalosť #{event_id} neexistuje.")
    if ev.status == "zamietnute":
        raise ValueError(f"Udalosť #{event_id} bola zamietnutá – nemožno ju potvrdiť.")
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


def reject_event(session: Session, event_id: int, actor: str, reason: str = "") -> ExtractedEvent:
    """Dispečer udalosť zamietne. Ak už bola zapísaná, jej zápis sa vráti späť (záznam sa zruší/obnoví).

    Záznam, ktorý medzitým zmenila iná (novšia) udalosť, sa nemení. Záznamy „navrh“ čakajúcej udalosti
    sa odstránia (dočasný záznam zmeny) alebo zrušia (návrh novej výluky).
    """
    ev = session.get(ExtractedEvent, event_id)
    if ev is None:
        raise ValueError(f"Udalosť #{event_id} neexistuje.")
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
    (plánovač podľa neho posudzuje čerstvosť výluk); ak sa schránku prečítať nepodarilo, nemení sa.
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
        new, fetch_ok = _fetch_and_store(session, errors)
    except Exception as exc:  # pragma: no cover - posledná poistka
        session.rollback()
        errors.append(f"Príjem pošty zlyhal: {type(exc).__name__}: {_short(exc)}")
        new, fetch_ok = [], False
    summary["fetch_ok"] = fetch_ok
    summary["fetched"] = len(new)
    summary["email_ids"] = [e.id for e in new]
    summary["quarantined_email_ids"] = [e.id for e in new if e.verdict == KARANTENA]
    summary["quarantined"] = len(summary["quarantined_email_ids"])
    summary["suspicious"] = sum(1 for e in new if e.verdict == PODOZRIVY)
    if fetch_ok:
        try:     # schránka je prečítaná – plánovač v tomto cykle už vidí čerstvý stav
            kv_set(session, "mail_last_ok", {"ts": now.isoformat(), "fetched": len(new)})
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
    """Čakajúca výluka dostala záznam „navrh“ – plánovač s ňou počíta, plán sa prepočíta ešte v tomto cykle."""
    try:
        info = (ev.validation or {}).get("pending_closure") or {}
    except Exception:  # pragma: no cover - udalosť po zlyhanom commite nemusí byť dostupná
        return
    if info.get("id"):
        applied_descr.append(describe_event(ev) + " – čaká na potvrdenie")


def _mark_waiting(session: Session, ev_id: int, reason: str) -> None:
    """Udalosť, ktorú sa nepodarilo zapísať automaticky, presunie do fronty na potvrdenie (samostatný commit)."""
    try:
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
            if "pending_closure" in (ev.validation or {}):
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
        sec = f"{d.get('from_station') or '?'} – {d.get('to_station') or '?'}"
        extra = f", {d['speed_kmh']:g} km/h" if isinstance(d.get("speed_kmh"), (int, float)) else ""
        daily = f", denne {d['daily_from']}–{d['daily_to']}" if d.get("daily_from") else ""
        return f"{label} {sec} {t('start')} – {t('end') if d.get('end') else 'do odvolania'}{daily}{extra} (#{ev.id})"
    if ev.kind == "vyluka_zrusenie":
        what = d.get("external_ref") or f"{d.get('from_station') or '?'} – {d.get('to_station') or '?'}"
        return f"{label} {what} (#{ev.id})"
    if ev.kind in ("trasa_pridelena", "trasa_zmena", "trasa_zrusena"):
        pts = d.get("points") or []
        rel = f" {pts[0].get('station')} → {pts[-1].get('station')}" if len(pts) >= 2 else ""
        offer = " (ponuka KIS)" if d.get("offer") else ""
        return f"{label}{offer} vlak {d.get('train_number') or '?'} dňa {d.get('date') or '?'}{rel} (#{ev.id})"
    if ev.kind == "avizo":
        return f"{label}: vlak {d.get('train_number') or '?'}, {d.get('border') or '?'}, ETA {t('eta')} (#{ev.id})"
    if ev.kind == "objednavka":
        return (f"{label} {d.get('customer') or '?'}: {d.get('origin') or '?'} → {d.get('destination') or '?'}, "
                f"dodať do {t('deadline') if d.get('deadline') else '?'} (#{ev.id})")
    return f"{label} (#{ev.id})"
