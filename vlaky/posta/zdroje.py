"""Zdroje pošty Agenta dispečera: adresár (.eml), IMAP a Microsoft Graph.

Spoločné rozhranie: ``fetch_new() -> list[RawMail]`` a ``mark_done(raw)``. Schránku nikdy nemažeme:

  * :class:`DirectorySource` – číta ``*.eml`` z ``mail_dir`` (ukážky, testy, záložný „drop folder“);
    spracované súbory presunie do ``mail_dir/spracovane/``,
  * :class:`ImapSource` – IMAP4 over SSL, schránka len na čítanie (``readonly``, ``BODY.PEEK``),
    prírastkovo podľa UID uloženého v ``KeyValue("imap_last_uid")`` (aj s UIDVALIDITY),
  * :class:`GraphSource` – Microsoft Graph (OAuth client credentials), priečinok Doručená pošta schránky
    ``graph_mailbox`` cez delta query (deltaLink v ``KeyValue("graph_delta_link")``), MIME cez ``$value``.

Stav (posledné UID, deltaLink) sa posúva až po ``mark_done`` – e-mail, ktorý sa nepodarilo uložiť,
sa pri ďalšom behu načíta znova (idempotencia podľa Message-ID zabráni duplicitám).
"""
from __future__ import annotations

import imaplib
import re
import shutil
import ssl
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..cas import now_utc
from ..config import settings
from ..models import KeyValue

BATCH = 50                        # najviac e-mailov na jedno načítanie
GRAPH_MAX_PAGES = 20
GRAPH_INITIAL_DAYS = 14           # pri prvom spustení len pošta za posledné dni
GRAPH_API = "https://graph.microsoft.com/v1.0"
GRAPH_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


class MailSourceError(Exception):
    """Zdroj pošty nie je dostupný alebo nakonfigurovaný (správa je pre prevádzku)."""


@dataclass
class RawMail:
    """Surový e-mail zo zdroja."""

    data: bytes
    source: str                   # directory | imap | graph
    uid: str                      # názov súboru / IMAP UID / Graph id
    path: str = ""                # pri adresári: cesta k súboru (po mark_done nová cesta)
    received_at: Optional[datetime] = None   # čas doručenia podľa zdroja (UTC), ak je známy
    truncated: bool = False       # e-mail bol nad limit veľkosti, načítala sa len časť


class MailSource(Protocol):
    name: str
    sequential: bool              # True = stav sa posúva v poradí (pri chybe treba zastaviť dávku)

    def fetch_new(self) -> list[RawMail]: ...

    def mark_done(self, raw: RawMail) -> Optional[str]: ...


def max_raw_bytes() -> int:
    return (4 * int(settings().max_attachment_mb) + 5) * 1024 * 1024


# ---------------------------------------------------------------------- stav v KeyValue
def kv_get(session: Optional[Session], key: str, default: Any = None) -> Any:
    def _get(s: Session) -> Any:
        row = s.scalar(select(KeyValue).where(KeyValue.key == key))
        return default if row is None else row.value

    if session is not None:
        return _get(session)
    from ..db import session_scope

    with session_scope() as s:
        return _get(s)


def kv_set(session: Optional[Session], key: str, value: Any) -> None:
    def _set(s: Session) -> None:
        row = s.scalar(select(KeyValue).where(KeyValue.key == key))
        if row is None:
            s.add(KeyValue(key=key, value=value))
        else:
            row.value = value          # vždy nový objekt (JSON stĺpec nesleduje zmeny vo vnútri)
        s.flush()

    if session is not None:
        _set(session)
        return
    from ..db import session_scope

    with session_scope() as s:
        _set(s)


# ---------------------------------------------------------------------- adresár
class DirectorySource:
    """Súbory ``*.eml`` v adresári ``mail_dir`` (spracované sa presunú do ``spracovane/``)."""

    name = "directory"
    sequential = False

    def __init__(self, directory: Optional[Path] = None) -> None:
        self.dir = Path(directory or settings().mail_dir)

    @property
    def done_dir(self) -> Path:
        return self.dir / "spracovane"

    def fetch_new(self) -> list[RawMail]:
        self.dir.mkdir(parents=True, exist_ok=True)
        files = sorted((p for p in self.dir.glob("*.eml") if p.is_file()), key=lambda p: p.name)[:BATCH]
        out: list[RawMail] = []
        limit = max_raw_bytes()
        for p in files:
            try:
                size = p.stat().st_size
                with open(p, "rb") as f:
                    data = f.read(limit + 1)
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue          # súbor práve niekto zapisuje / presunul – skúsime nabudúce
            out.append(RawMail(data=data[:limit], source=self.name, uid=p.name, path=str(p),
                               received_at=min(mtime, now_utc()), truncated=size > limit))
        return out

    def mark_done(self, raw: RawMail) -> Optional[str]:
        src = Path(raw.path)
        if not src.exists():
            return raw.path
        self.done_dir.mkdir(parents=True, exist_ok=True)
        dest = self.done_dir / src.name
        n = 1
        while dest.exists():
            dest = self.done_dir / f"{src.stem}_{n}{src.suffix}"
            n += 1
        shutil.move(str(src), str(dest))
        raw.path = str(dest)
        return raw.path


# ---------------------------------------------------------------------- IMAP
# RFC 3501: date-day-fixed = (SP DIGIT) / 2DIGIT – dni 1 až 9 môžu mať namiesto nuly medzeru („ 2-Nov-2026“)
_INTERNALDATE = re.compile(rb'INTERNALDATE "[ 0]?(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})"')
_MONTHS = {m: i for i, m in enumerate(
    [b"Jan", b"Feb", b"Mar", b"Apr", b"May", b"Jun", b"Jul", b"Aug", b"Sep", b"Oct", b"Nov", b"Dec"], start=1)}


def _internaldate(meta: bytes) -> Optional[datetime]:
    m = _INTERNALDATE.search(meta or b"")
    if not m:
        return None
    try:
        dt = datetime(int(m.group(3)), _MONTHS[m.group(2).capitalize()], int(m.group(1)), int(m.group(4)),
                      int(m.group(5)), int(m.group(6)))
    except (KeyError, ValueError):
        return None
    off = timedelta(hours=int(m.group(8)), minutes=int(m.group(9)))
    off = -off if m.group(7) == b"-" else off
    return (dt - off).replace(tzinfo=timezone.utc)


class ImapSource:
    """IMAP (SSL), len čítanie. Prírastkovo podľa UID; schránku nemení (readonly + BODY.PEEK)."""

    name = "imap"
    sequential = True
    STATE_KEY = "imap_last_uid"

    def __init__(self, session: Optional[Session] = None,
                 factory: Optional[Callable[..., Any]] = None) -> None:
        self.session = session
        self.factory = factory or imaplib.IMAP4_SSL
        self._uidvalidity: Optional[str] = None

    def _state(self) -> dict:
        val = kv_get(self.session, self.STATE_KEY, {}) or {}
        return val if isinstance(val, dict) else {"last_uid": int(val)}

    def fetch_new(self) -> list[RawMail]:
        s = settings()
        if not s.imap_host or not s.imap_user:
            raise MailSourceError("IMAP nie je nakonfigurované (VLAKY_IMAP_HOST, VLAKY_IMAP_USER, VLAKY_IMAP_PASSWORD).")
        try:
            conn = self.factory(s.imap_host, s.imap_port, ssl_context=ssl.create_default_context(), timeout=60)
        except TypeError:          # staršie rozhranie bez parametra timeout
            conn = self.factory(s.imap_host, s.imap_port, ssl_context=ssl.create_default_context())
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailSourceError(f"IMAP server {s.imap_host} nie je dostupný ({type(exc).__name__}).") from exc
        try:
            try:
                conn.login(s.imap_user, s.imap_password)
            except imaplib.IMAP4.error as exc:
                raise MailSourceError("Prihlásenie do IMAP zlyhalo (skontrolujte používateľa a heslo).") from exc
            folder = s.imap_folder or "INBOX"
            if " " in folder and not folder.startswith('"'):
                folder = f'"{folder}"'
            typ, _ = conn.select(folder, readonly=True)
            if typ != "OK":
                raise MailSourceError(f"Priečinok {s.imap_folder} sa nedá otvoriť.")
            uv = None
            try:
                _, resp = conn.response("UIDVALIDITY")
                uv = resp[0].decode() if resp and resp[0] else None
            except Exception:
                uv = None
            self._uidvalidity = uv
            state = self._state()
            last = int(state.get("last_uid") or 0)
            if uv and state.get("uidvalidity") and str(state.get("uidvalidity")) != str(uv):
                last = 0           # schránka bola prečíslovaná – načítame znova (duplicity zachytí Message-ID)
            typ, data = conn.uid("SEARCH", None, f"UID {last + 1}:*")
            if typ != "OK":
                raise MailSourceError("Vyhľadávanie v IMAP schránke zlyhalo.")
            uids = sorted(int(x) for x in (data[0] or b"").split() if x.isdigit() and int(x) > last)[:BATCH]
            out: list[RawMail] = []
            limit = max_raw_bytes()
            for uid in uids:
                typ, msgdata = conn.uid("FETCH", str(uid), "(INTERNALDATE BODY.PEEK[])")
                if typ != "OK" or not msgdata:
                    raise MailSourceError(f"E-mail UID {uid} sa nepodarilo načítať.")
                meta, raw = b"", b""
                for item in msgdata:
                    if isinstance(item, tuple) and len(item) >= 2:
                        meta, raw = item[0], item[1]
                        break
                out.append(RawMail(data=raw[:limit], source=self.name, uid=str(uid),
                                   received_at=_internaldate(meta), truncated=len(raw) > limit))
            return out
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def mark_done(self, raw: RawMail) -> Optional[str]:
        state = self._state()
        last = int(state.get("last_uid") or 0)
        if self._uidvalidity and str(state.get("uidvalidity") or "") != str(self._uidvalidity):
            last = 0
        uid = int(raw.uid)
        if uid > last:
            kv_set(self.session, self.STATE_KEY, {"uidvalidity": self._uidvalidity, "last_uid": uid})
        return None


# ---------------------------------------------------------------------- Microsoft Graph
class GraphSource:
    """Microsoft Graph: Doručená pošta schránky agenta cez delta query (aplikačné oprávnenie Mail.Read)."""

    name = "graph"
    sequential = False
    STATE_KEY = "graph_delta_link"

    def __init__(self, session: Optional[Session] = None, client: Optional[httpx.Client] = None,
                 known: Optional[Callable[[str], bool]] = None,
                 known_sender: Optional[Callable[[str, str], bool]] = None) -> None:
        self.session = session
        self.client = client or httpx.Client(timeout=60)
        self.known = known                      # vráti True, ak e-mail s daným Message-ID už máme
        # vráti True, ak e-mail s daným Message-ID od toho istého odosielateľa už máme (iný odosielateľ s rovnakým
        # Message-ID sa stiahne – môže to byť pravý e-mail, ktorého Message-ID skôr použil niekto iný)
        self.known_sender = known_sender
        self._token: Optional[str] = None
        self._token_exp = 0.0
        self._pending_link: Optional[str] = None
        self._outstanding: set[str] = set()

    def _auth(self) -> str:
        s = settings()
        if not (s.graph_tenant_id and s.graph_client_id and s.graph_client_secret and s.graph_mailbox):
            raise MailSourceError("Microsoft Graph nie je nakonfigurovaný (VLAKY_GRAPH_TENANT_ID, VLAKY_GRAPH_CLIENT_ID, "
                                  "VLAKY_GRAPH_CLIENT_SECRET, VLAKY_GRAPH_MAILBOX).")
        if self._token and _time.time() < self._token_exp - 60:
            return self._token
        r = self.client.post(GRAPH_TOKEN_URL.format(tenant=quote(s.graph_tenant_id, safe="")),
                             data={"client_id": s.graph_client_id, "client_secret": s.graph_client_secret,
                                   "scope": "https://graph.microsoft.com/.default",
                                   "grant_type": "client_credentials"})
        if r.status_code != 200:
            raise MailSourceError(f"Získanie tokenu Microsoft Graph zlyhalo (HTTP {r.status_code}).")
        j = r.json()
        self._token = j.get("access_token")
        if not self._token:
            raise MailSourceError("Odpoveď Microsoft Graph neobsahuje access_token.")
        self._token_exp = _time.time() + float(j.get("expires_in") or 3600)
        return self._token

    def _get(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> httpx.Response:
        for attempt in range(3):
            h = {"Authorization": f"Bearer {self._auth()}", **(headers or {})}
            r = self.client.get(url, params=params, headers=h)
            if r.status_code in (429, 503, 504) and attempt < 2:
                try:
                    wait = min(30.0, float(r.headers.get("Retry-After", "2")))
                except ValueError:
                    wait = 2.0
                _time.sleep(wait)
                continue
            if r.status_code == 401 and attempt < 2:
                self._token = None
                continue
            return r
        return r

    def fetch_new(self) -> list[RawMail]:
        s = settings()
        mailbox = quote(s.graph_mailbox, safe="@")
        link = kv_get(self.session, self.STATE_KEY)
        if link:
            url, params = str(link), None
        else:
            since = (now_utc() - timedelta(days=GRAPH_INITIAL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
            url = f"{GRAPH_API}/users/{mailbox}/mailFolders/inbox/messages/delta"
            params = {"$select": "id,internetMessageId,receivedDateTime,subject,from",
                      "$filter": f"receivedDateTime ge {since}"}
        messages: list[dict] = []
        next_state: Optional[str] = None
        for _ in range(GRAPH_MAX_PAGES):
            r = self._get(url, params=params, headers={"Prefer": "odata.maxpagesize=50"})
            if r.status_code == 410 and link:     # deltaLink vypršal – začneme odznova
                kv_set(self.session, self.STATE_KEY, None)
                raise MailSourceError("Delta odkaz Microsoft Graph vypršal – pri ďalšom behu sa schránka načíta znova.")
            if r.status_code != 200:
                raise MailSourceError(f"Čítanie schránky cez Microsoft Graph zlyhalo (HTTP {r.status_code}).")
            j = r.json()
            for m in j.get("value") or []:
                if "@removed" in m or not m.get("id"):
                    continue
                messages.append(m)
            params = None
            if j.get("@odata.nextLink"):
                url = j["@odata.nextLink"]
                next_state = url
                if len(messages) >= BATCH:
                    break
                continue
            next_state = j.get("@odata.deltaLink") or next_state
            break
        out: list[RawMail] = []
        limit = max_raw_bytes()
        for m in messages:
            mid = m.get("internetMessageId") or ""
            frm = m.get("from") if isinstance(m.get("from"), dict) else {}
            sender = str((frm.get("emailAddress") or {}).get("address") or "").strip().lower()
            if mid and sender and self.known_sender is not None:
                if self.known_sender(mid, sender):
                    continue
            elif mid and self.known and self.known(mid):
                continue
            r = self._get(f"{GRAPH_API}/users/{mailbox}/messages/{quote(m['id'], safe='')}/$value")
            if r.status_code != 200:
                raise MailSourceError(f"E-mail {m['id'][:12]}… sa nepodarilo stiahnuť (HTTP {r.status_code}).")
            data = r.content
            rec = None
            try:
                rec = datetime.fromisoformat(str(m.get("receivedDateTime")).replace("Z", "+00:00"))
            except ValueError:
                rec = None
            out.append(RawMail(data=data[:limit], source=self.name, uid=m["id"], received_at=rec,
                               truncated=len(data) > limit))
        self._pending_link = next_state
        self._outstanding = {r.uid for r in out}
        if not out and next_state:
            kv_set(self.session, self.STATE_KEY, next_state)
            self._pending_link = None
        return out

    def mark_done(self, raw: RawMail) -> Optional[str]:
        self._outstanding.discard(raw.uid)
        if not self._outstanding and self._pending_link:
            kv_set(self.session, self.STATE_KEY, self._pending_link)
            self._pending_link = None
        return None


def get_source(session: Optional[Session] = None) -> MailSource:
    """Zdroj podľa ``settings().mail_source`` (directory | imap | graph)."""
    kind = (settings().mail_source or "directory").lower()
    if kind == "imap":
        return ImapSource(session)
    if kind == "graph":
        from ..models import Email

        def known(mid: str) -> bool:
            # e-mail v karanténe sa stiahne znova: overená kópia s rovnakým Message-ID ho môže nahradiť
            if session is None:
                return False
            return session.scalar(select(Email.id).where(Email.message_id == mid,
                                                         Email.verdict != "karantena")) is not None

        def known_sender(mid: str, sender: str) -> bool:
            if session is None:
                return False
            rows = session.scalars(select(Email.sender).where(Email.message_id == mid, Email.verdict != "karantena"))
            return any((s or "").strip().lower() == sender for s in rows)

        return GraphSource(session, known=known, known_sender=known_sender)
    if kind == "directory":
        return DirectorySource()
    raise MailSourceError(f"Neznámy zdroj pošty „{kind}“ (directory | imap | graph).")
