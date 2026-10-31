"""Zdroje pošty: adresár, IMAP (podvrhnutý server) a Microsoft Graph (httpx.MockTransport)."""
from __future__ import annotations

import imaplib
import shutil
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from vlaky.models import Email
from vlaky.posta import zdroje
from vlaky.posta.spracovanie import fetch_and_store
from vlaky.posta.zdroje import (DirectorySource, GraphSource, ImapSource, MailSourceError, get_source, kv_get,
                                kv_set)

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "ukazkove_emaily"
MAIL1 = (SAMPLES / "01_rov_vyluka_kolaje_poprad_lm.eml").read_bytes()
MAIL2 = (SAMPLES / "07_pridelenie_trasy_nex54321.eml").read_bytes()
MAIL3 = (SAMPLES / "13_objednavka_pdf_cz.eml").read_bytes()


# ---------------------------------------------------------------------- adresár
def test_directory_source_reads_only_eml_and_moves_processed(cfg):
    inbox = Path(cfg.mail_dir)
    (inbox / "b.eml").write_bytes(MAIL2)
    (inbox / "a.eml").write_bytes(MAIL1)
    (inbox / "poznamka.txt").write_text("nie je e-mail")
    (inbox / "podadresar").mkdir()
    (inbox / "podadresar" / "c.eml").write_bytes(MAIL3)
    src = DirectorySource()
    raws = src.fetch_new()
    assert [r.uid for r in raws] == ["a.eml", "b.eml"] and raws[0].data == MAIL1 and raws[0].source == "directory"
    (inbox / "spracovane").mkdir()
    (inbox / "spracovane" / "a.eml").write_bytes(b"stary")
    new = src.mark_done(raws[0])
    assert new.endswith("spracovane/a_1.eml") and not (inbox / "a.eml").exists() and raws[0].path == new
    assert [r.uid for r in src.fetch_new()] == ["b.eml"]


def test_directory_source_truncates_huge_files(cfg, monkeypatch):
    monkeypatch.setattr(zdroje, "max_raw_bytes", lambda: 100)
    (Path(cfg.mail_dir) / "velky.eml").write_bytes(MAIL1)
    (raw,) = DirectorySource().fetch_new()
    assert raw.truncated and len(raw.data) == 100


def test_get_source_selection(cfg, monkeypatch):
    assert isinstance(get_source(), DirectorySource)
    monkeypatch.setattr(cfg, "mail_source", "imap")
    assert isinstance(get_source(), ImapSource)
    monkeypatch.setattr(cfg, "mail_source", "graph")
    assert isinstance(get_source(), GraphSource)
    monkeypatch.setattr(cfg, "mail_source", "pop3")
    with pytest.raises(MailSourceError):
        get_source()


def test_key_value_state(session):
    assert kv_get(session, "x", 7) == 7
    kv_set(session, "x", {"a": 1})
    kv_set(session, "x", {"a": 2})
    assert kv_get(session, "x") == {"a": 2}


# ---------------------------------------------------------------------- IMAP
class FakeImap:
    """Minimálny IMAP server: len príkazy, ktoré zdroj smie použiť (nič nemaže, nič neoznačuje)."""

    messages: dict = {}
    uidvalidity = 1001
    log: list = []

    def __init__(self, host, port, ssl_context=None, timeout=None):  # noqa: ANN001
        FakeImap.log.append(("connect", host, port))

    def login(self, user, password):  # noqa: ANN001
        if password != "heslo":
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        FakeImap.log.append(("login", user))
        return "OK", [b"ok"]

    def select(self, folder, readonly=False):  # noqa: ANN001
        FakeImap.log.append(("select", folder, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):  # noqa: ANN001
        return code, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):  # noqa: ANN001
        FakeImap.log.append(("uid", command, args))
        if command == "SEARCH":
            start = int(args[1].split()[1].split(":")[0])
            found = [u for u in sorted(self.messages) if u >= start]
            if not found and self.messages:          # vlastnosť IMAP: „n:*“ vráti aj najvyššie UID
                found = [max(self.messages)]
            return "OK", [" ".join(map(str, found)).encode()]
        if command == "FETCH":
            assert "BODY.PEEK[]" in args[1]           # čítanie bez označenia ako prečítané
            u = int(args[0])
            data = self.messages[u]
            meta = f'{u} (UID {u} INTERNALDATE "02-Nov-2026 07:12:00 +0100" BODY[] {{{len(data)}}}'.encode()
            return "OK", [(meta, data), b")"]
        raise AssertionError(f"nepovolený IMAP príkaz {command}")

    def logout(self):
        FakeImap.log.append(("logout",))


@pytest.fixture()
def imap(cfg, monkeypatch):
    FakeImap.messages = {1: MAIL1, 2: MAIL2}
    FakeImap.uidvalidity = 1001
    FakeImap.log = []
    for k, v in {"mail_source": "imap", "imap_host": "imap.dopravca.example", "imap_user": "agent",
                 "imap_password": "heslo", "imap_folder": "Doručená pošta"}.items():
        monkeypatch.setattr(cfg, k, v)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    return FakeImap


def test_imap_is_incremental_by_uid_and_readonly(imap, session):
    src = ImapSource(session)
    raws = src.fetch_new()
    assert [r.uid for r in raws] == ["1", "2"] and raws[0].data == MAIL1
    assert raws[0].received_at.isoformat() == "2026-11-02T06:12:00+00:00"
    assert ("select", '"Doručená pošta"', True) in imap.log and ("logout",) in imap.log
    for r in raws:
        src.mark_done(r)
    assert kv_get(session, "imap_last_uid") == {"uidvalidity": "1001", "last_uid": 2}
    assert ImapSource(session).fetch_new() == []              # „3:*“ vráti 2 → odfiltruje sa
    imap.messages[3] = MAIL3
    assert [r.uid for r in ImapSource(session).fetch_new()] == ["3"]


def test_imap_uidvalidity_change_rereads_mailbox(imap, session):
    kv_set(session, "imap_last_uid", {"uidvalidity": "999", "last_uid": 50})
    assert [r.uid for r in ImapSource(session).fetch_new()] == ["1", "2"]


def test_imap_errors_are_reported(imap, monkeypatch, cfg, session):
    monkeypatch.setattr(cfg, "imap_password", "zle")
    with pytest.raises(MailSourceError, match="Prihlásenie"):
        ImapSource(session).fetch_new()
    monkeypatch.setattr(cfg, "imap_host", "")
    with pytest.raises(MailSourceError, match="nakonfigurované"):
        ImapSource(session).fetch_new()


def test_fetch_and_store_from_imap_archives_original(imap, session, cfg):
    emails = fetch_and_store(session)
    assert [e.source for e in emails] == ["imap", "imap"]
    assert all(Path(e.raw_path).exists() and "archiv" in e.raw_path for e in emails)
    assert Path(emails[0].raw_path).read_bytes() == MAIL1
    assert kv_get(session, "imap_last_uid")["last_uid"] == 2
    assert fetch_and_store(session) == [] and session.query(Email).count() == 2


# ---------------------------------------------------------------------- Microsoft Graph
GRAPH = "https://graph.microsoft.com/v1.0"
DELTA = f"{GRAPH}/users/agent@dopravca.example/mailFolders/inbox/messages/delta?$deltatoken=AAA"
NEXT = f"{GRAPH}/users/agent@dopravca.example/mailFolders/inbox/messages/delta?$skiptoken=BBB"


class FakeGraph:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.delta_status = 200
        self.second_delta: dict = {"value": [], "@odata.deltaLink": DELTA + "2"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith("https://login.microsoftonline.com/"):
            assert b"grant_type=client_credentials" in request.content
            assert b"scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in request.content
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer tok-123"
        if "/messages/delta" in url and "$deltatoken=AAA" in url and not url.endswith("AAA2"):
            if self.delta_status != 200:
                return httpx.Response(self.delta_status, json={"error": {"code": "SyncStateNotFound"}})
            return httpx.Response(200, json=self.second_delta)
        if "/messages/delta" in url and "skiptoken" in url:
            return httpx.Response(200, json={"value": [{"id": "m2", "internetMessageId": "<ukazka-07.fiktivne@zsr.example>",
                                                        "receivedDateTime": "2026-11-02T10:02:00Z"}],
                                             "@odata.deltaLink": DELTA})
        if "/messages/delta" in url:
            q = unquote(url)
            assert "$filter=receivedDateTime ge " in q and request.headers["prefer"] == "odata.maxpagesize=50"
            return httpx.Response(200, json={"value": [
                {"id": "m1", "internetMessageId": "<ukazka-01.fiktivne@zsr.example>",
                 "receivedDateTime": "2026-11-02T06:12:00Z"},
                {"id": "old", "@removed": {"reason": "deleted"}}], "@odata.nextLink": NEXT})
        if url.endswith("/messages/m1/$value"):
            return httpx.Response(200, content=MAIL1)
        if url.endswith("/messages/m2/$value"):
            return httpx.Response(200, content=MAIL2)
        return httpx.Response(404)


@pytest.fixture()
def graph(cfg, monkeypatch):
    for k, v in {"mail_source": "graph", "graph_tenant_id": "tenant-fiktivny", "graph_client_id": "klient",
                 "graph_client_secret": "tajne", "graph_mailbox": "agent@dopravca.example"}.items():
        monkeypatch.setattr(cfg, k, v)
    return FakeGraph()


def test_graph_delta_query_and_state(graph, session):
    src = GraphSource(session, client=httpx.Client(transport=httpx.MockTransport(graph)))
    raws = src.fetch_new()
    assert [(r.uid, r.data) for r in raws] == [("m1", MAIL1), ("m2", MAIL2)]
    assert raws[0].received_at.isoformat() == "2026-11-02T06:12:00+00:00"
    assert kv_get(session, "graph_delta_link") is None           # stav sa posúva až po spracovaní
    src.mark_done(raws[0])
    assert kv_get(session, "graph_delta_link") is None
    src.mark_done(raws[1])
    assert kv_get(session, "graph_delta_link") == DELTA
    token_calls = [r for r in graph.requests if "login.microsoftonline.com" in str(r.url)]
    assert len(token_calls) == 1 and "/tenant-fiktivny/oauth2/v2.0/token" in str(token_calls[0].url)
    # ďalší beh pokračuje deltaLinkom; prázdna odpoveď hneď uloží nový odkaz
    src2 = GraphSource(session, client=httpx.Client(transport=httpx.MockTransport(graph)))
    assert src2.fetch_new() == [] and kv_get(session, "graph_delta_link") == DELTA + "2"


def test_graph_skips_known_messages(graph, session):
    src = GraphSource(session, client=httpx.Client(transport=httpx.MockTransport(graph)), known=lambda mid: True)
    assert src.fetch_new() == []
    assert not [r for r in graph.requests if str(r.url).endswith("$value")]


def test_graph_expired_delta_link_is_reset(graph, session):
    kv_set(session, "graph_delta_link", DELTA)
    graph.delta_status = 410
    with pytest.raises(MailSourceError, match="vypršal"):
        GraphSource(session, client=httpx.Client(transport=httpx.MockTransport(graph))).fetch_new()
    assert kv_get(session, "graph_delta_link") is None


def test_graph_not_configured(cfg, session):
    with pytest.raises(MailSourceError, match="nie je nakonfigurovaný"):
        GraphSource(session, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))) \
            .fetch_new()


def test_fetch_and_store_from_graph(graph, session, monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr(zdroje.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(graph)))
    emails = fetch_and_store(session)
    assert [e.message_id for e in emails] == ["<ukazka-01.fiktivne@zsr.example>", "<ukazka-07.fiktivne@zsr.example>"]
    assert all(e.source == "graph" and Path(e.raw_path).exists() for e in emails)
    assert kv_get(session, "graph_delta_link") == DELTA


def test_fetch_and_store_survives_bad_file(cfg, session):
    shutil.copy(SAMPLES / "01_rov_vyluka_kolaje_poprad_lm.eml", cfg.mail_dir)
    (Path(cfg.mail_dir) / "poskodeny.eml").write_bytes(b"\x00\xff\xfe nic rozumne \x00")
    emails = fetch_and_store(session)
    assert len(emails) == 2
    bad = next(e for e in emails if e.message_id.startswith("sha256:"))
    assert bad.verdict == "karantena"                      # bez odosielateľa
