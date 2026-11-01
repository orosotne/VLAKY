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
from .text import clean_text, fold, header_text

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
class ForwardedMessage:
    """Preposlaný obsah v e-maile – pôvodného odosielateľa treba overiť rovnako ako pri priamom e-maile."""

    sender: str = ""          # adresa pôvodného odosielateľa (prázdna = nedá sa zistiť)
    from_count: int = 0       # počet adries v pôvodnej hlavičke From
    auth_results: list = field(default_factory=list)   # pôvodné hlavičky Authentication-Results
    message_id: str = ""
    subject: str = ""
    inline: bool = False      # vložené preposlanie v texte – hlavičky na overenie nie sú k dispozícii
    how: str = ""             # príloha message/rfc822 | priložený .eml | text


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
    forwarded: list = field(default_factory=list)      # ForwardedMessage – preposlané správy v e-maile


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
        return [header_text(v) for v in (msg.get_all(name) or [])]
    except Exception:
        return []


def _norm_message_id(raw_id: str, data: bytes) -> str:
    mid = header_text(raw_id)
    if not mid:
        return "sha256:" + _sha(data)
    if len(mid) > 290:
        return "sha256:" + _sha(mid.encode("utf-8", "replace"))
    return mid


_ADDRESS_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _from_addresses(msg: Message) -> tuple[str, str, int]:
    """(adresa, meno, počet adries) z hlavičiek From.

    Počítajú sa aj neplatné a prázdne položky: „From: vyluky@zsr.example <x@utocnik.example>“ knižnica rozoberie
    ako „vyluky@zsr.example, <>“ – skutočnú adresu zahodí. Taká hlavička má dve položky a ide do karantény.
    """
    headers = _header_all(msg, "From")
    try:
        entries = getaddresses(headers)
    except Exception:
        entries = []
    addrs = [(n, a) for n, a in entries if a and a.strip()]
    if not addrs:
        m = _ADDRESS_RE.search(" ".join(headers))
        addrs = [("", m.group(0))] if m else []
    count = max(len(addrs), len(entries))
    if not addrs:
        return "", "", count
    return header_text(addrs[0][1]).lower()[:300], header_text(addrs[0][0])[:200], count


def _forwarded_from_message(inner: Message, how: str) -> ForwardedMessage:
    sender, _name, count = _from_addresses(inner)
    return ForwardedMessage(sender=sender, from_count=count, auth_results=_header_all(inner, "Authentication-Results"),
                            message_id=header_text(_header(inner, "Message-ID"))[:300],
                            subject=header_text(_header(inner, "Subject"))[:500], how=how)


_HEADER_LINE = re.compile(rb"^[A-Za-z][A-Za-z0-9-]{1,40}:(?:[ \t]|$)")
# hlavičky, ktoré má skutočný e-mail (okrem From) – „Vlak:/Dátum:/From: Bratislava/To: Kúty“ e-mail nie je
_MAIL_HEADERS = (b"date:", b"subject:", b"message-id:", b"received:", b"mime-version:")


def _embedded_message(filename: str, ctype: str, data: bytes) -> Optional[Message]:
    """Priložený e-mail, ktorého text sa spracuje (textová príloha začínajúca hlavičkami e-mailu, napr. .eml
    poslaný ako text/plain) – inak None. Overuje sa rovnako ako príloha message/rfc822.

    E-mail je to len vtedy, keď text začína skutočným blokom hlavičiek (až po prvý prázdny riadok sú len
    hlavičky a ich pokračovania): hlavička From s adresou (s „@“) a aspoň jedna z hlavičiek Date, Subject,
    Message-ID, Received, MIME-Version. Textový výpis trasy s riadkami „From: Bratislava“ e-mailom nie je."""
    if prilohy.detect_kind(filename, ctype, data[:8]) != "txt":
        return None                   # iné typy sa na text neprevádzajú (napr. .eml ako application/octet-stream)
    text = data[:16384].replace(b"\r\n", b"\n").lstrip(b"\n")
    block = text.split(b"\n\n", 1)[0].split(b"\n")
    if block and block[0].startswith(b"From ") and not _HEADER_LINE.match(block[0]):
        block = block[1:]             # oddeľovač schránky mbox („From adresa dátum“)
    heads: list[bytes] = []
    for ln in block:
        if ln[:1] in (b" ", b"\t") and heads:
            heads[-1] += b" " + ln.strip().lower()     # pokračovanie predchádzajúcej hlavičky
            continue
        if not _HEADER_LINE.match(ln):
            return None               # v bloku je riadok, ktorý nie je hlavičkou – nejde o e-mail
        heads.append(ln.lower())
    from_lines = [ln for ln in heads if ln.startswith(b"from:")]
    if not from_lines or not any(ln.startswith(h) for ln in heads for h in _MAIL_HEADERS):
        return None
    if not any(_ADDRESS_RE.search(ln[5:].decode("utf-8", "replace")) for ln in from_lines):
        return None
    try:
        return message_from_bytes(data, policy=policy.default)
    except Exception:
        return None


# vložené preposlanie: značka v texte (bez ohľadu na diakritiku) a hlavička pôvodného odosielateľa pod ňou
_FWD_MARKER = re.compile(
    r"^[ \t>]*[-_=*]*[ \t]*(?:forwarded message|begin forwarded message|preposlan\w* (?:sprav\w*|e-?mail\w*|zprav\w*)"
    r"|zaciatok preposlanej spravy|zacatek preposlane zpravy|weitergeleitete nachricht|tovabbitott (?:uzenet|level)"
    r"|wiadomosc przekazana|przekazana wiadomosc)[ \t]*:?[ \t]*[-_=*]*[ \t]*$", re.M)
_REPLY_MARKER = re.compile(r"^[ \t>]*[-_=*]*[ \t]*(?:original message|povodn\w* sprav\w*|puvodni zprav\w*"
                           r"|ursprungliche nachricht)[ \t]*:?[ \t]*[-_=*]*[ \t]*$|^[ \t>]*_{10,}[ \t]*$", re.M)
_FWD_SUBJECT = re.compile(r"^\s*(?:\[[^\]]{0,40}\]\s*)?(?:fwd?|tr|wg|pd|preposl\w*)\s*:", re.I)
_FROM_LINE = re.compile(r"^[ \t>*]*(?:from|od|odosielatel|odesilatel|von|de|felado|nadawca)[ \t]*:[ \t]*(.+)$", re.M)
_HEAD_LINE = re.compile(r"^[ \t>*]*(?:from|od|to|komu|pre|date|datum|sent|odoslane|odeslano|subject|predmet"
                        r"|cc|kopia|kopie|von|an|gesendet|betreff|de|envoye|objet)[ \t]*:", re.M)


def inline_forwards(subject: str, body: str) -> list[ForwardedMessage]:
    """Vložené preposlania v texte e-mailu („---------- Forwarded message ---------“, „Fwd:“ s hlavičkou Od:)."""
    f = fold(body or "")
    starts = [m.end() for m in _FWD_MARKER.finditer(f)]
    if _FWD_SUBJECT.match(subject or ""):
        starts += [m.end() for m in _REPLY_MARKER.finditer(f)]
        if not starts:
            # preposlanie bez značky (Outlook): blok hlavičiek s pôvodným odosielateľom
            for m in _FROM_LINE.finditer(f):
                block = f[m.start():m.start() + 400]
                if len(_HEAD_LINE.findall(block)) >= 2:
                    starts.append(m.start())
                    break
    out: list[ForwardedMessage] = []
    for pos in sorted(set(starts)):
        window = f[pos:pos + 600]
        m = _FROM_LINE.search(window)
        sender = ""
        if m and len(_HEAD_LINE.findall(window[m.start():m.start() + 400])) >= 2:
            a = _ADDRESS_RE.search(body[pos + m.start(1):pos + m.end(1)])
            sender = a.group(0).lower() if a else ""
        out.append(ForwardedMessage(sender=header_text(sender)[:300], from_count=1 if sender else 0, inline=True,
                                    how="text"))
    return out


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
    return header_text(name or "")[:250]


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
        self.forwarded: list[ForwardedMessage] = []
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
                # pôvodného odosielateľa a jeho overenie (Authentication-Results) kontroluje overenie.verify
                self.forwarded.append(_forwarded_from_message(inner, "príloha message/rfc822"))
                head = ["--- Preposlaná správa (príloha) ---",
                        f"Od: {header_text(_header(inner, 'From'))}", f"Predmet: {header_text(_header(inner, 'Subject'))}",
                        f"Dátum: {header_text(_header(inner, 'Date'))}"]
                self.texts.append("\n".join(head))
                self.walk(inner, depth + 1)
            else:
                self.forwarded.append(ForwardedMessage(how="príloha message/rfc822 (nečitateľná)"))
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
        att = ParsedAttachment(filename=filename, content_type=header_text(ctype)[:120], size=len(data))
        inner = _embedded_message(filename, ctype, data)
        if inner is not None:       # priložený e-mail (.eml) – overuje sa ako preposlaná správa
            self.forwarded.append(_forwarded_from_message(inner, f"priložený e-mail {filename}"))
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

    # hlavičky sú nedôveryhodné: bez NUL a riadiacich znakov (inak by ich databáza odmietla a e-mail by
    # zablokoval príjem celej schránky)
    pm.message_id = _norm_message_id(_header(msg, "Message-ID"), raw)
    pm.subject = header_text(_header(msg, "Subject"))[:500]
    pm.from_header = header_text(_header(msg, "From"))[:300]
    pm.sender, pm.sender_name, pm.from_count = _from_addresses(msg)
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
    pm.forwarded = walker.forwarded + inline_forwards(pm.subject, body)
    return pm
