"""Spracovanie pošty Agenta dispečera: príjem → uloženie → extrakcia → validácia → registre (M4, M5, M6).

Verejné funkcie (volá ich slučka agenta a webová aplikácia; signatúry sú záväzné):

    fetch_and_store(session) -> list[Email]
    process_email(session, email) -> list[ExtractedEvent]
    apply_event(session, event, actor="agent") -> dict
    confirm_event(session, event_id, actor, edits=None) -> ExtractedEvent
    reject_event(session, event_id, actor, reason="") -> ExtractedEvent
    process_mailbox(session) -> dict

Transakcie: ``fetch_and_store`` a ``process_mailbox`` potvrdzujú (commit) po každom e-maile, aby sa pri
reštarte nič nestratilo a chyba jedného e-mailu neovplyvnila ostatné. ``process_email``, ``apply_event``,
``confirm_event`` a ``reject_event`` len zapisujú do relácie (flush) – commit robí volajúci.

Bezpečnosť: e-mail v karanténe sa nikdy neextrahuje; udalosť, ktorá potrebuje potvrdenie, sa do registrov
nedostane bez potvrdenia dispečerom; udalosti z podozrivého e-mailu sa nikdy neaplikujú automaticky.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import DateTime, select
from sqlalchemy.orm import Session

from ..audit import audit
from ..cas import fmt, local, now_utc
from ..config import settings
from ..models import Attachment, Closure, Email, ExtractedEvent, Handover, Order, Path as TrainPath, Train
from .extrakcia import extract
from .overenie import KARANTENA, OK, PODOZRIVY, verify
from .parser import parse_message
from .stanice import stations
from .text import fold
from .udalosti import KIND_LABELS, MailDocument
from .validacia import find_train, parse_number, parse_utc, resolve_border, train_digits, validate_event
from .zdroje import MailSourceError, RawMail, get_source

OPEN_END_DAYS = 30            # výluka „do odvolania“ – technický koniec, kým ŽSR neoznámi skutočný
MATCH_TOLERANCE = timedelta(hours=1)


class ApplyError(Exception):
    """Udalosť nemožno zapísať do registra (správa je pre dispečera)."""


# ====================================================================== príjem
def fetch_and_store(session: Session) -> list[Email]:
    """Načíta nové e-maily zo zdroja a uloží ich (idempotentne podľa Message-ID).

    E-mail v karanténe sa uloží s verdiktom ``karantena`` a ďalej sa nespracúva.
    """
    return _fetch_and_store(session, [])


def _fetch_and_store(session: Session, errors: list[str]) -> list[Email]:
    try:
        src = get_source(session)
        raws = src.fetch_new()
    except MailSourceError as exc:
        errors.append(str(exc))
        return []
    except Exception as exc:  # zdroj pošty nesmie zastaviť agenta
        errors.append(f"Zdroj pošty zlyhal: {type(exc).__name__}: {exc}")
        return []
    stored: list[Email] = []
    for raw in raws:
        try:
            email = _store_one(session, raw)
            session.commit()
        except Exception as exc:
            session.rollback()
            errors.append(f"E-mail {raw.uid} sa nepodarilo uložiť ({type(exc).__name__}: {exc}).")
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
    return stored


def _archive(raw: RawMail, sha: str) -> str:
    """Kópia originálu (IMAP/Graph) do mail_dir/archiv/RRRR/MM/ – audit (sekcia M5)."""
    d = local(raw.received_at or now_utc())
    folder = Path(settings().mail_dir) / "archiv" / f"{d:%Y}" / f"{d:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sha[:24]}.eml"
    if not path.exists():
        path.write_bytes(raw.data)
    return str(path)


def _store_one(session: Session, raw: RawMail) -> Optional[Email]:
    head = parse_message(raw.data, extract_attachments=False)
    if session.scalar(select(Email.id).where(Email.message_id == head.message_id)) is not None:
        return None                                   # už spracované (idempotencia)
    ver = verify(head)
    pm = head
    if ver.verdict != KARANTENA:
        pm = parse_message(raw.data)                  # prílohy len od povoleného a overeného odosielateľa
        ver = verify(pm)
    errors = list(pm.errors)
    if raw.truncated:
        errors.append("E-mail prekročil limit veľkosti – načítaná len časť.")
    for a in pm.attachments:
        if a.error:
            errors.append(f"Príloha „{a.filename}“: {a.error}")
    raw_path = raw.path if raw.source == "directory" else _archive(raw, pm.sha256)
    email = Email(
        message_id=pm.message_id, source=raw.source, received_at=raw.received_at or pm.date or now_utc(),
        sender=(pm.sender or pm.from_header)[:300], subject=pm.subject[:500], body_text=pm.body_text,
        auth_results="\n".join(pm.auth_results), verdict=ver.verdict, verdict_reason=ver.reason_text,
        status="spracovany" if ver.verdict == KARANTENA else "prijaty", error="\n".join(errors), raw_path=raw_path)
    for a in pm.attachments:
        email.attachments.append(Attachment(filename=a.filename[:300], content_type=a.content_type[:120], size=a.size,
                                            text=a.text))
    session.add(email)
    session.flush()
    audit(session, "agent", "email_prijaty", "Email", email.id, message_id=email.message_id, sender=email.sender,
          subject=email.subject, verdict=email.verdict, dovod=email.verdict_reason, zdroj=raw.source)
    return email


# ====================================================================== extrakcia
def process_email(session: Session, email: Email, *, reprocess: bool = False) -> list[ExtractedEvent]:
    """Vytiahne z e-mailu udalosti, overí ich a uloží ako návrhy (``status = navrh``).

    Opakované volanie vráti už existujúce udalosti (``reprocess=True`` nespracované návrhy vytvorí znova).
    Chyby v e-maile zapíše do ``email.status = "chyba"`` a ``email.error`` – výnimku nevyhodí.
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
                session.delete(ev)
        session.flush()
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
            validation = {**vr.as_dict(), "notes": notes, "field_confidence": it.field_confidence,
                          "evidence": it.evidence, "original_data": original, "extraction": result.meta}
            prepared.append(ExtractedEvent(
                email_id=email.id, kind=it.kind, data=vr.data, confidence=round(conf, 2), extractor=it.extractor,
                status="navrh", needs_confirmation=vr.needs_confirmation or email.verdict != OK,
                validation=validation))
    except Exception as exc:  # zvláštny e-mail nesmie zastaviť agenta
        email.status = "chyba"
        email.error = ((email.error + "\n") if email.error else "") + \
            f"Extrakcia zlyhala: {type(exc).__name__}: {exc}"
        session.flush()
        return []
    for ev in prepared:
        session.add(ev)
    email.status = "spracovany"
    session.flush()
    audit(session, "agent", "email_extrakcia", "Email", email.id,
          udalosti=[{"id": e.id, "kind": e.kind, "istota": e.confidence, "na_potvrdenie": e.needs_confirmation}
                    for e in prepared], **result.meta)
    return prepared


# ====================================================================== zápis do registrov
def apply_event(session: Session, event: ExtractedEvent, actor: str = "agent") -> dict:
    """Zapíše udalosť do registrov (výluky, trasy, preberania, objednávky).

    Nepotvrdená udalosť, ktorá potvrdenie potrebuje, sa nezapíše (vráti ``{"ok": False, ...}``).
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
    except ApplyError as exc:
        val = dict(event.validation or {})
        val["apply_error"] = str(exc)
        event.validation = val
        session.flush()
        return {"ok": False, "error": str(exc)}
    result["ok"] = True
    result["kind"] = event.kind
    val = dict(event.validation or {})
    val.pop("apply_error", None)
    val["applied"] = {**result, "at": now_utc().isoformat(), "by": actor, "confirmed": confirmed}
    event.validation = val
    event.status = "aplikovane"
    session.flush()
    audit(session, actor, "udalost_aplikovana", "ExtractedEvent", event.id, druh=event.kind,
          popis=describe_event(event), vysledok={k: v for k, v in result.items() if k not in ("before", "train_before")})
    return result


def _apply_nothing(session: Session, ev: ExtractedEvent, confirmed: bool) -> dict:
    return {"entity": "", "id": None, "action": "nic"}


# ---------------------------------------------------------------------- výluky
_CLOSURE_FIELDS = ("kind", "from_station", "to_station", "line_ref", "track", "start_utc", "end_utc", "daily_from",
                   "daily_to", "electric_only", "speed_kmh", "status", "external_ref", "note", "version",
                   "source_event_id")


def ref_key(ref: Any) -> str:
    """Porovnávací kľúč čísla výluky/trasy („ROV č. 1142/2026“ = „1142/2026“)."""
    k = "".join(ch for ch in fold(str(ref or "")) if ch.isalnum())
    for prefix in ("srov", "rov", "tlg", "potr", "c"):
        if k.startswith(prefix) and len(k) > len(prefix) and k[len(prefix)].isdigit():
            k = k[len(prefix):]
            break
    return k


def _station_key(name: Any) -> str:
    m = stations().match(str(name or "")) if name else None
    return fold(m.name) if m else " ".join(fold(str(name or "")).split())


def _same_section(c: Closure, a: Any, b: Any) -> bool:
    return {_station_key(c.from_station), _station_key(c.to_station)} == {_station_key(a), _station_key(b or a)}


def _find_closure(session: Session, ev: ExtractedEvent, d: dict, start: Optional[datetime],
                  loose: bool = False) -> tuple[Optional[Closure], str]:
    c = session.scalar(select(Closure).where(Closure.source_event_id == ev.id))
    if c is not None:
        return c, "rovnaká udalosť"
    active = list(session.scalars(select(Closure).where(Closure.status != "zrusene")))
    if d.get("external_ref"):
        key = ref_key(d["external_ref"])
        hits = [c for c in active if c.external_ref and ref_key(c.external_ref) == key]
        if len(hits) == 1:
            return hits[0], "číslo výluky"
    if d.get("from_station") and start is not None:
        hits = [c for c in active if _same_section(c, d["from_station"], d.get("to_station"))
                and abs(_utc(c.start_utc) - start) <= MATCH_TOLERANCE]
        if len(hits) == 1:
            return hits[0], "úsek a začiatok"
    if loose and d.get("from_station"):
        hits = [c for c in active if _same_section(c, d["from_station"], d.get("to_station"))]
        if start is not None:
            hits = [c for c in hits if abs(_utc(c.start_utc) - start) <= timedelta(days=3)] or hits
        if len(hits) == 1:
            return hits[0], "úsek"
    return None, ""


def _utc(dt: datetime) -> datetime:
    from ..cas import ensure_utc

    return ensure_utc(dt)


def _apply_closure(session: Session, ev: ExtractedEvent, confirmed: bool) -> dict:
    d = ev.data or {}
    start = parse_utc(d.get("start"))
    end = parse_utc(d.get("end"))
    if start is None:
        raise ApplyError("Chýba začiatok platnosti.")
    if not d.get("from_station"):
        raise ApplyError("Chýba úsek (dopravňa od).")
    notes = []
    if end is None:
        if d.get("until_revoked") or ev.kind == "mimoriadnost":
            end = start + timedelta(days=OPEN_END_DAYS)
            notes.append(f"Koniec neurčený (do odvolania) – technicky nastavený na +{OPEN_END_DAYS} dní, "
                         "aktualizujte po oznámení ukončenia.")
        else:
            raise ApplyError("Chýba koniec platnosti.")
    if ev.kind == "mimoriadnost":
        restriction = "mimoriadnost"
        if d.get("description"):
            notes.insert(0, str(d["description"]))
    elif ev.kind == "pomala_jazda":
        restriction = "pomala_jazda"
    else:
        restriction = d.get("kind") or "uplna"
    if d.get("note"):
        notes.insert(0, str(d["note"]))
    email = session.get(Email, ev.email_id) if ev.email_id else None
    source_line = f"Zdroj: e-mail #{ev.email_id}" + (f" od {email.sender}, „{email.subject[:120]}“" if email else "")
    fields = {
        "kind": restriction, "from_station": d["from_station"], "to_station": d.get("to_station") or d["from_station"],
        "line_ref": str(d.get("line_ref") or "")[:40], "track": str(d.get("track") or "")[:40],
        "start_utc": start, "end_utc": end, "daily_from": d.get("daily_from") or "", "daily_to": d.get("daily_to") or "",
        "electric_only": restriction == "trakcne_vedenie",
        "speed_kmh": parse_number(d.get("speed_kmh")),
        "external_ref": str(d.get("external_ref") or "")[:120],
        "status": "overene" if confirmed else "navrh", "source": "email", "source_event_id": ev.id,
    }
    existing, how = _find_closure(session, ev, d, start, loose=ev.kind == "vyluka_zmena")
    if existing is None:
        if ev.kind == "vyluka_zmena":
            notes.append("Zmena sa týka výluky, ktorá v registri nie je – založená ako nová.")
        c = Closure(note="\n".join(notes + [source_line]), **fields)
        session.add(c)
        session.flush()
        return {"entity": "Closure", "id": c.id, "action": "vytvorene", "status": c.status, "before": None}
    before = _snapshot(existing, _CLOSURE_FIELDS)
    changed = False
    for k, v in fields.items():
        if k in ("status", "source", "source_event_id"):
            continue
        if k == "external_ref" and not v:
            continue
        cur = getattr(existing, k)
        if isinstance(cur, datetime) and isinstance(v, datetime):
            same = _utc(cur) == v
        else:
            same = cur == v
        if not same:
            setattr(existing, k, v)
            changed = True
    existing.status = fields["status"]
    existing.source = "email"
    existing.source_event_id = ev.id
    if changed:
        existing.version = (existing.version or 1) + 1
    extra = [n for n in notes if n not in (existing.note or "")]
    if extra or changed:
        existing.note = "\n".join(filter(None, [existing.note or ""] + extra +
                                         [f"{'Zmenené' if changed else 'Potvrdené'} podľa udalosti #{ev.id} "
                                          f"({how}). {source_line}"]))
    session.flush()
    return {"entity": "Closure", "id": existing.id, "action": "aktualizovane" if changed else "potvrdene",
            "status": existing.status, "matched_by": how, "before": before}


def _apply_closure_cancel(session: Session, ev: ExtractedEvent, confirmed: bool) -> dict:
    d = ev.data or {}
    c = session.scalar(select(Closure).where(Closure.source_event_id == ev.id))
    how = "rovnaká udalosť"
    if c is None:
        c, how = _find_closure(session, ev, d, parse_utc(d.get("start")), loose=True)
    if c is None:
        raise ApplyError("Výluka na zrušenie sa v registri nenašla (podľa čísla ani úseku).")
    before = _snapshot(c, _CLOSURE_FIELDS)
    c.status = "zrusene"
    c.version = (c.version or 1) + 1
    c.source_event_id = ev.id
    c.note = "\n".join(filter(None, [c.note or "", f"Zrušené podľa udalosti #{ev.id} (e-mail #{ev.email_id}, {how})."]))
    session.flush()
    return {"entity": "Closure", "id": c.id, "action": "zrusene", "status": c.status, "matched_by": how,
            "before": before}


# ---------------------------------------------------------------------- trasy
_PATH_FIELDS = ("train_id", "train_number", "kind", "status", "departure_utc", "points", "note", "source_event_id")


def _path_day(session: Session, p: TrainPath) -> Optional[str]:
    if p.departure_utc is not None:
        return local(p.departure_utc).date().isoformat()
    if p.train_id:
        tr = session.get(Train, p.train_id)
        return tr.run_date if tr else None
    return None


def _find_paths(session: Session, ev: ExtractedEvent, number: str, day: Optional[str]) -> list[TrainPath]:
    rows = session.scalars(select(TrainPath).where(TrainPath.train_number == number,
                                                   TrainPath.status != "zrusena")).all()
    if day:
        same = [p for p in rows if _path_day(session, p) == day]
        if same:
            return same
        return [p for p in rows if _path_day(session, p) is None]
    return list(rows)


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
    if existing is None:
        cands = _find_paths(session, ev, number, d.get("date"))
        if ev.kind == "trasa_zmena":
            existing = cands[0] if len(cands) == 1 else None
        else:
            existing = next((p for p in cands if p.status in ("ponuka", "ziadana", "navrh", "pridelena")), None)
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
    for k, v in fields.items():
        if k == "train_id" and v is None:
            continue
        setattr(existing, k, v)
    existing.note = "\n".join(filter(None, [existing.note or ""] + notes))
    session.flush()
    return {"entity": "Path", "id": existing.id, "action": "aktualizovane", "status": existing.status,
            "train_id": existing.train_id, "before": before}


def _apply_path_cancel(session: Session, ev: ExtractedEvent, confirmed: bool) -> dict:
    d = ev.data or {}
    number = train_digits(d.get("train_number"))
    rows = list(session.scalars(select(TrainPath).where(TrainPath.source_event_id == ev.id)))
    if not rows:
        rows = _find_paths(session, ev, number, d.get("date"))
    if not rows:
        raise ApplyError(f"Trasa vlaku {number} na deň {d.get('date') or '?'} sa v registri nenašla.")
    befores = []
    for p in rows:
        befores.append({"id": p.id, **_snapshot(p, _PATH_FIELDS)})
        p.status = "zrusena"
        p.source_event_id = ev.id
        p.note = "\n".join(filter(None, [p.note or "", f"Zrušené podľa udalosti #{ev.id} (e-mail #{ev.email_id})."]))
    session.flush()
    return {"entity": "Path", "id": rows[0].id, "ids": [p.id for p in rows], "action": "zrusene", "status": "zrusena",
            "before": befores}


# ---------------------------------------------------------------------- avízo
_HANDOVER_FIELDS = ("train_id", "border_id", "direction", "avizo_eta_utc", "status", "source_event_id")


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
    if h is None:
        h = session.scalar(select(Handover).where(Handover.train_id == train.id, Handover.direction == "vstup",
                                                  Handover.status.in_(("ocakavany", "prisiel")))
                           .order_by(Handover.id.desc()))
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
def confirm_event(session: Session, event_id: int, actor: str, edits: Optional[dict] = None) -> ExtractedEvent:
    """Dispečer potvrdí (a prípadne opraví) udalosť; potom sa zapíše do registrov.

    ``edits`` – opravy polí dát (napr. ``{"to_station": "Ružomberok"}``); kľúč ``event_kind`` zmení druh
    udalosti. Opravy sa uložia (pôvodná extrakcia zostáva v ``validation.original_data`` – trénovacie príklady).
    Výsledok zápisu je v ``event.validation["apply_result"]``; ak zápis zlyhal, stav ostane ``potvrdene``.
    """
    ev = session.get(ExtractedEvent, event_id)
    if ev is None:
        raise ValueError(f"Udalosť #{event_id} neexistuje.")
    if ev.status == "zamietnute":
        raise ValueError(f"Udalosť #{event_id} bola zamietnutá – nemožno ju potvrdiť.")
    val = copy.deepcopy(ev.validation or {})
    data = copy.deepcopy(ev.data or {})
    if edits:
        edits = dict(edits)
        new_kind = edits.pop("event_kind", None)
        changes = {}
        if new_kind and new_kind != ev.kind:
            if new_kind not in KIND_LABELS:
                raise ValueError(f"Neznámy druh udalosti „{new_kind}“.")
            changes["event_kind"] = {"pred": ev.kind, "po": new_kind}
            ev.kind = new_kind
        for k, v in edits.items():
            if data.get(k) != v:
                changes[k] = {"pred": data.get(k), "po": v}
                data[k] = v
        if changes:
            val.setdefault("edits", []).append({"by": actor, "at": now_utc().isoformat(), "changes": changes})
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
          opravy=(val.get("edits") or [])[-1:] if edits else [])
    result = apply_event(session, ev, actor)
    val = dict(ev.validation or {})
    val["apply_result"] = {k: v for k, v in result.items() if k not in ("before", "train_before")}
    ev.validation = val
    session.flush()
    return ev


def reject_event(session: Session, event_id: int, actor: str, reason: str = "") -> ExtractedEvent:
    """Dispečer udalosť zamietne. Ak už bola zapísaná, jej zápis sa vráti späť (záznam sa zruší/obnoví)."""
    ev = session.get(ExtractedEvent, event_id)
    if ev is None:
        raise ValueError(f"Udalosť #{event_id} neexistuje.")
    if ev.status == "zamietnute":
        return ev
    val = copy.deepcopy(ev.validation or {})
    rollback = None
    if ev.status == "aplikovane" and val.get("applied"):
        rollback = _rollback(session, ev, val["applied"])
    val["rejection"] = {"by": actor, "at": now_utc().isoformat(), "reason": reason, "rollback": rollback}
    ev.status = "zamietnute"
    ev.validation = val
    session.flush()
    audit(session, actor, "udalost_zamietnuta", "ExtractedEvent", ev.id, druh=ev.kind, dovod=reason,
          popis=describe_event(ev), navrat=rollback)
    return ev


def _rollback(session: Session, ev: ExtractedEvent, applied: dict) -> Optional[str]:
    entity, action, before = applied.get("entity"), applied.get("action"), applied.get("before")
    model = {"Closure": Closure, "Path": TrainPath, "Handover": Handover, "Order": Order}.get(entity or "")
    if model is None:
        return None
    if entity == "Path" and action == "zrusene":
        for b in before or []:
            p = session.get(TrainPath, b.get("id"))
            if p is not None:
                _restore(p, {k: v for k, v in b.items() if k != "id"})
        return "zrušenie trasy vrátené"
    obj = session.get(model, applied.get("id"))
    if obj is None:
        return None
    if action == "vytvorene":
        if entity == "Closure":
            obj.status = "zrusene"
            obj.note = "\n".join(filter(None, [obj.note or "", f"Zamietnuté dispečerom (udalosť #{ev.id})."]))
            return "výluka zrušená"
        if entity == "Path":
            obj.status = "zrusena"
            return "trasa zrušená"
        if entity == "Order":
            obj.status = "zamietnuta"
            return "objednávka zamietnutá"
        if entity == "Handover":
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
    if before:
        _restore(obj, before)
        if entity == "Handover":
            tb = applied.get("train_before") or {}
            tr = session.get(Train, obj.train_id)
            if tr is not None and tb.get("status") == "planovany" and tr.status == "na_ceste_k_hranici":
                tr.status = "planovany"
        return "pôvodný stav obnovený"
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
def process_mailbox(session: Session) -> dict:
    """Jeden cyklus: načítaj a ulož poštu, extrahuj udalosti, automaticky zapíš bezpečné, prepočítaj plán.

    Nikdy nevyhodí výnimku – chyby sú v ``summary["errors"]``.
    """
    summary: dict[str, Any] = {"fetched": 0, "quarantined": 0, "suspicious": 0, "events": 0, "auto_applied": 0,
                               "waiting_confirmation": 0, "errors": [], "email_ids": [], "event_ids": [],
                               "applied_event_ids": [], "waiting_event_ids": [], "quarantined_email_ids": [],
                               "planning": None}
    errors: list[str] = summary["errors"]
    try:
        new = _fetch_and_store(session, errors)
    except Exception as exc:  # pragma: no cover - posledná poistka
        session.rollback()
        errors.append(f"Príjem pošty zlyhal: {type(exc).__name__}: {exc}")
        new = []
    summary["fetched"] = len(new)
    summary["email_ids"] = [e.id for e in new]
    summary["quarantined_email_ids"] = [e.id for e in new if e.verdict == KARANTENA]
    summary["quarantined"] = len(summary["quarantined_email_ids"])
    summary["suspicious"] = sum(1 for e in new if e.verdict == PODOZRIVY)
    # aj staršie neprijaté e-maily (napr. po reštarte medzi uložením a extrakciou)
    pending = list(session.scalars(select(Email).where(Email.status == "prijaty", Email.verdict != KARANTENA)
                                   .order_by(Email.id)))
    applied_descr: list[str] = []
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
            errors.append(f"E-mail #{email_id}: spracovanie zlyhalo ({type(exc).__name__}: {exc}).")
            _mark_error(session, email_id, f"Spracovanie zlyhalo: {type(exc).__name__}: {exc}")
            continue
        summary["events"] += len(events)
        summary["event_ids"] += [ev.id for ev in events]
        for ev in events:
            if ev.status != "navrh":
                continue
            if ev.needs_confirmation or ev.kind == "ine" or email.verdict != OK:
                summary["waiting_confirmation"] += 1
                summary["waiting_event_ids"].append(ev.id)
                continue
            try:
                res = apply_event(session, ev, actor="agent")
                session.commit()
            except Exception as exc:
                session.rollback()
                errors.append(f"Udalosť #{ev.id}: zápis zlyhal ({type(exc).__name__}: {exc}).")
                continue
            if res.get("ok"):
                summary["auto_applied"] += 1
                summary["applied_event_ids"].append(ev.id)
                applied_descr.append(describe_event(ev))
            else:
                summary["waiting_confirmation"] += 1
                summary["waiting_event_ids"].append(ev.id)
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
            summary["planning"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            errors.append(f"Prepočet plánu po e-mailoch zlyhal ({type(exc).__name__}: {exc}).")
    try:
        if summary["fetched"] or summary["events"] or errors:
            audit(session, "agent", "posta_cyklus", "", "", **{k: v for k, v in summary.items()
                                                                if k not in ("email_ids", "event_ids")})
        session.commit()
    except Exception as exc:  # pragma: no cover
        session.rollback()
        errors.append(f"Zápis auditu zlyhal ({type(exc).__name__}).")
    return summary


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
