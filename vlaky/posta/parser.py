"""Rozbor e-mailu (RFC 822 / MIME): hlavičky, text tela a prílohy prevedené na text.

Zásady: e-mail je nedôveryhodný vstup – nič sa nespúšťa, neotvárajú sa odkazy, nenačítavajú sa
vzdialené obrázky. Prílohy nad limit ``max_attachment_mb`` sa nespracujú (zapíše sa dôvod).
Funkcia :func:`parse_message` nikdy nevyhodí výnimku – chyby zapíše do ``ParsedMail.errors``.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Optional

from ..config import settings
from . import prilohy
from .text import clean_text

MAX_BODY_CHARS = 200_000
MAX_NESTING = 3


@dataclass
class ParsedAttachment:
    filename: str
    content_type: str
    size: int
    text: str = ""
    kind: str = ""
    error: str = ""          # prečo sa text nezískal (limit, nepodporovaný typ, chyba)


@dataclass
class ParsedMail:
    message_id: str
    sender: str = ""          # adresa odosielateľa (malými písmenami)
    sender_name: str = ""
    from_header: str = ""
    from_count: int = 0       # počet adries v hlavičke From (viac = podozrivé)
    subject: str = ""
    date: Optional[datetime] = None   # z hlavičky Date (UTC)
    auth_results: list = field(default_factory=list)   # hlavičky Authentication-Results (zhora nadol)
    body_text: str = ""
    attachments: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    sha256: str = ""
    size: int = 0


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_raw_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _header(msg: Message, name: str) -> str:
    try:
        v = msg.get(name)
        return "" if v is None else str(v)
    except Exception:
        try:
            raw = msg.get_all(name, failobj=[]) if hasattr(msg, "get_all") else []
            return _decode_raw_header(str(raw[0])) if raw else ""
        except Exception:
            return ""


def _header_all(msg: Message, name: str) -> list[str]:
    try:
        return [" ".join(str(v).split()) for v in (msg.get_all(name) or [])]
    except Exception:
        return []


def _norm_message_id(raw_id: str, data: bytes) -> str:
    mid = " ".join((raw_id or "").split())
    if not mid:
        return "sha256:" + _sha(data)
    if len(mid) > 290:
        return "sha256:" + _sha(mid.encode("utf-8", "replace"))
    return mid


def _part_bytes(part: Message) -> bytes:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if payload is None:
        try:
            raw = part.get_payload()
            payload = raw.encode("utf-8", "replace") if isinstance(raw, str) else b""
        except Exception:
            payload = b""
    return payload


def _part_text(part: Message) -> str:
    try:
        if isinstance(part, EmailMessage):
            content = part.get_content()
            if isinstance(content, str):
                return content
    except Exception:
        pass
    return prilohy.decode_bytes(_part_bytes(part), part.get_content_charset())


def _filename(part: Message) -> str:
    try:
        name = part.get_filename()
    except Exception:
        name = None
    return clean_text(name or "")[:250]


def _disposition(part: Message) -> str:
    try:
        return part.get_content_disposition() or ""
    except Exception:
        return ""


class _Walker:
    def __init__(self, extract_attachments: bool, limit_bytes: int, sandbox: bool) -> None:
        self.texts: list[str] = []
        self.attachments: list[ParsedAttachment] = []
        self.errors: list[str] = []
        self.extract = extract_attachments
        self.limit = limit_bytes
        self.sandbox = sandbox

    def walk(self, part: Message, depth: int = 0) -> None:
        try:
            ctype = part.get_content_type()
        except Exception:
            ctype = "application/octet-stream"
        if ctype == "message/rfc822" or (ctype.startswith("message/") and part.is_multipart()):
            if depth >= MAX_NESTING:
                self.errors.append("Príliš hlboko vnorená preposlaná správa – vynechaná.")
                return
            try:
                inner = part.get_payload(0)
            except Exception:
                inner = None
            if isinstance(inner, Message):
                head = ["--- Preposlaná správa (príloha) ---",
                        f"Od: {_header(inner, 'From')}", f"Predmet: {_header(inner, 'Subject')}",
                        f"Dátum: {_header(inner, 'Date')}"]
                self.texts.append("\n".join(head))
                self.walk(inner, depth + 1)
            return
        if part.is_multipart():
            try:
                children = part.get_payload()
            except Exception:
                children = []
            if not isinstance(children, list):
                return
            if ctype == "multipart/alternative":
                chosen = None
                for pref in ("text/plain", "text/html"):
                    for ch in children:
                        if ch.get_content_type() == pref and _disposition(ch) != "attachment":
                            chosen = ch
                            break
                    if chosen is not None:
                        break
                if chosen is None and children:
                    chosen = children[-1]
                for ch in children:
                    if ch is chosen or _filename(ch) or _disposition(ch) == "attachment":
                        self.walk(ch, depth)
                return
            for ch in children:
                self.walk(ch, depth)
            return
        filename = _filename(part)
        disp = _disposition(part)
        if not filename and disp != "attachment" and ctype in ("text/plain", "text/html"):
            text = _part_text(part)
            self.texts.append(prilohy.html_to_text(text) if ctype == "text/html" else text)
            return
        if not filename and ctype.startswith(("image/", "application/pkcs7", "application/x-pkcs7")) \
                and disp != "attachment":
            return      # vložené obrázky (logo v podpise) a elektronický podpis ignorujeme
        self._attachment(part, filename or f"bez_nazvu.{ctype.split('/')[-1][:10]}", ctype)

    def _attachment(self, part: Message, filename: str, ctype: str) -> None:
        data = _part_bytes(part)
        att = ParsedAttachment(filename=filename, content_type=ctype[:120], size=len(data))
        if not self.extract:
            att.error = "príloha nebola analyzovaná (e-mail je v karanténe)"
        elif len(data) > self.limit:
            att.error = (f"príloha má {len(data) / 1048576:.1f} MB, limit je "
                         f"{self.limit / 1048576:.0f} MB – nespracovaná")
        else:
            text, kind, err = prilohy.convert(filename, ctype, data, sandbox=self.sandbox)
            att.text, att.kind, att.error = clean_text(text), kind, err
        self.attachments.append(att)


def parse_message(data: bytes, *, extract_attachments: bool = True, sandbox: Optional[bool] = None) -> ParsedMail:
    """Rozoberie e-mail. ``extract_attachments=False`` = prílohy sa len zaevidujú (karanténa)."""
    s = settings()
    limit = int(s.max_attachment_mb) * 1024 * 1024
    if sandbox is None:
        sandbox = bool(getattr(s, "attachment_sandbox", True))
    raw = data or b""
    pm = ParsedMail(message_id="", sha256=_sha(raw), size=len(raw))
    max_total = 4 * limit + 5 * 1024 * 1024
    if len(raw) > max_total:
        pm.errors.append(f"E-mail má {len(raw) / 1048576:.1f} MB a prekračuje limit – spracované len hlavičky.")
        head_end = raw.find(b"\r\n\r\n")
        head_end = raw.find(b"\n\n") if head_end == -1 else head_end
        raw_for_parse = raw[: head_end + 4] if head_end != -1 else raw[:65536]
        extract_attachments = False
    else:
        raw_for_parse = raw
    try:
        msg = message_from_bytes(raw_for_parse, policy=policy.default)
    except Exception as exc:  # veľmi poškodená správa
        pm.message_id = "sha256:" + pm.sha256
        pm.errors.append(f"E-mail sa nepodarilo rozobrať ({type(exc).__name__}).")
        pm.body_text = clean_text(prilohy.decode_bytes(raw[:MAX_BODY_CHARS]))
        return pm

    pm.message_id = _norm_message_id(_header(msg, "Message-ID"), raw)
    pm.subject = " ".join(clean_text(_header(msg, "Subject")).split())[:500]
    pm.from_header = " ".join(clean_text(_header(msg, "From")).split())[:300]
    try:
        addrs = [(n, a) for n, a in getaddresses(_header_all(msg, "From")) if a]
    except Exception:
        addrs = []
    if not addrs:
        m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", pm.from_header)
        addrs = [("", m.group(0))] if m else []
    pm.from_count = len(addrs)
    if addrs:
        pm.sender_name = clean_text(addrs[0][0])[:200]
        pm.sender = addrs[0][1].strip().lower()[:300]
    try:
        d = _header(msg, "Date")
        if d:
            dt = parsedate_to_datetime(d)
            pm.date = (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc))
    except Exception:
        pm.errors.append("Neplatná hlavička Date.")
    pm.auth_results = _header_all(msg, "Authentication-Results")

    walker = _Walker(extract_attachments, limit, sandbox)
    try:
        walker.walk(msg)
    except Exception as exc:
        walker.errors.append(f"Časť e-mailu sa nepodarilo spracovať ({type(exc).__name__}).")
    body = "\n\n".join(t.strip() for t in walker.texts if t and t.strip())
    body = clean_text(body)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[… text skrátený …]"
        pm.errors.append("Text e-mailu bol skrátený.")
    pm.body_text = body
    pm.attachments = walker.attachments
    pm.errors += walker.errors
    return pm
