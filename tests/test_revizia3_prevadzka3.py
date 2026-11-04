"""Regresné testy k 3. revízii prevádzky agenta (oblasť A).

Nahromadená pošta a čerstvosť schránky (MS0), aktuálny čas po dlhom čítaní schránky a zmeškaná odovzdávka (MS1),
jeden prepočet v cykle s e-mailom a náhradný prepočet (MS4), opakovanie jednorazových upozornení – pripomienka
výluky do odvolania a hlásenie rušňovodiča (MS5, WR3), nedoručené správy podľa odporúčania (WR4), rovnaký počet vo
fronte všade (MS6), test-notify a outbox v ostrej prevádzke (OD0), create-user bez prepísania účtu (OD2), náklady
po slovensky (OD5), check.sh bez Dockera (OD6) a skutočná chyba doručenia v zdraví (OD4).

Všetko FIKTÍVNE (adresy *.example); SMTP, Teams, SMS brána, IMAP aj Microsoft Graph sú atrapy v procese.
"""
from __future__ import annotations

import imaplib
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from test_web_spolocne import client, web_app  # noqa: F401  (fixtures)
from vlaky import config as config_mod
from vlaky.agent import beh
from vlaky.agent.zdravie import get_kv, health_status, set_kv, undelivered
from vlaky.cas import ensure_utc, local, now_utc, set_fake_now
from vlaky.db import session_scope
from vlaky.models import (Closure, Driver, DriverShift, Email, ExtractedEvent, Notification, Path as TrainPath,
                          Recommendation, ShiftHandoverReport, Train, User)
from vlaky.notifikacie import odosielanie

ROOT = Path(__file__).resolve().parent.parent
D1, D2 = "dispecer1@dopravca.example", "dispecer2@dopravca.example"
UTC = timezone.utc
FAR = {"ts": "2100-01-01T00:00:00+00:00"}  # pravidelný prepočet sa v teste nespustí sám


# ------------------------------------------------------------------ pomôcky
class Relay:
    """Falošný SMTP server v procese: funguje, alebo odmieta spojenie (výpadok)."""

    def __init__(self) -> None:
        self.down, self.sent = False, []

    def smtp(self):
        relay = self

        class FakeSMTP:
            def __init__(self, host, port, timeout=30):
                if relay.down:
                    raise ConnectionRefusedError(111, "Connection refused")
                self.sock = None

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self, context=None):
                pass

            def login(self, user, password):
                pass

            def send_message(self, msg, to_addrs=None):
                relay.sent.append((to_addrs[0], str(msg["Subject"])))
                return {}

        return FakeSMTP


def _setup(cfg, monkeypatch, relay: Relay | None = None, **kw):
    """Nastavenia testu: bez Teams, bez dennej správy a odovzdávky (ak ich test nezapne); s relay falošný SMTP."""
    base = {**cfg.model_dump(), "teams_webhook_url": "", "daily_report_hour": 23, "shift_change_hours": "3"}
    if relay is not None:
        base.update(smtp_host="relay.example", smtp_starttls=False)
        monkeypatch.setattr(odosielanie.smtplib, "SMTP", relay.smtp())
    base.update(kw)
    return config_mod.override_settings(**base)


def _eml(mid: str, subject: str, body: str, sender: str = "vyluky.orke@zsr.example") -> bytes:
    head = (f"From: {Header('ŽSR OR Košice (FIKTÍVNE)', 'utf-8').encode()} <{sender}>\r\n"
            f"To: agent.dispecing@dopravca.example\r\nSubject: {Header(subject, 'utf-8').encode()}\r\n"
            f"Date: {format_datetime(local(now_utc()))}\r\nMessage-ID: <{mid}>\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=\"utf-8\"\r\nContent-Transfer-Encoding: 8bit\r\n\r\n")
    return head.encode("ascii") + body.replace("\n", "\r\n").encode("utf-8")


def _drop(cfg, name: str, data: bytes) -> None:
    path = Path(cfg.mail_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    ts = now_utc().timestamp()
    os.utime(path, (ts, ts))  # čas doručenia = čas vloženia do schránky


def _info(cfg, i: int) -> None:
    _drop(cfg, f"a_{i:03d}.eml", _eml(f"info-{i}@zsr.example", f"FIKTÍVNE – informácia {i}",
                                      f"FIKTÍVNE – ukážka. Informácia č. {i}: zmena telefónneho kontaktu oddelenia "
                                      f"{i}. Bez vplyvu na prevádzku.\n", sender="info.orke@zsr.example"))


def _rov(ref: str, a: str, b: str, start: datetime, end: datetime) -> tuple:
    def f(dt: datetime) -> str:
        return local(dt).strftime("%d.%m.%Y %H:%M")

    subject = f"FIKTÍVNE – ukážka: Rozkaz o výluke (ROV) č. {ref} – výluka {a} – {b}"
    body = (f"Vážení dopravcovia,\n\nFIKTÍVNE – ukážka (nejde o skutočný rozkaz ŽSR).\n\n"
            f"oznamujeme Vám, že na základe Rozkazu o výluke (ROV) č. {ref} bude vykonaná výluka:\n\n"
            f"Úsek: {a} – {b}\nDruh výluky: úplná výluka trate\nTermín: od {f(start)} do {f(end)}\n"
            f"Dôvod výluky: oprava mosta\n\nS pozdravom\nOR Košice – prevádzka (FIKTÍVNE)\n")
    return subject, body


def _train(s, number: str, origin: str, dest: str, ready: datetime, deadline: datetime, status: str = "planovany",
           path_dep: datetime | None = None, shift_start: datetime | None = None,
           departed: datetime | None = None) -> int:
    t = Train(number=number, run_date=local(ready).date().isoformat(), origin=origin, destination=dest,
              customer="Zákazník (fiktívny)", traction="E", gauge="1435", vmax_kmh=100, length_m=500,
              weight_t=1600, status=status, ready_utc=ready, deadline_utc=deadline, notes="FIKTÍVNY")
    if departed is not None:
        t.planned_departure_utc = departed
        t.departed_utc = departed
    s.add(t)
    s.flush()
    if path_dep is not None:
        s.add(TrainPath(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena",
                        departure_utc=path_dep, note="FIKTÍVNA trasa"))
    if shift_start is not None:
        driver = Driver(name=f"Rušňovodič {number} (fiktívny)", phone="")
        s.add(driver)
        s.flush()
        s.add(DriverShift(driver_id=driver.id, train_id=t.id, start_utc=shift_start,
                          status="prebieha" if departed is not None else "planovana"))
    s.flush()
    return t.id


def _latest(s, train_id: int) -> Recommendation:
    return s.scalar(select(Recommendation).where(Recommendation.train_id == train_id)
                    .order_by(Recommendation.id.desc()))


class _FakeImap:
    """Minimálny IMAP server (len čítanie)."""

    messages: dict = {}

    def __init__(self, host, port, ssl_context=None, timeout=None):
        pass

    def login(self, user, password):
        return "OK", [b"ok"]

    def select(self, folder, readonly=False):
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [b"7"]

    def uid(self, command, *args):
        if command == "SEARCH":
            start = int(args[1].split()[1].split(":")[0])
            return "OK", [" ".join(str(u) for u in sorted(self.messages) if u >= start).encode()]
        if command == "FETCH":
            u = int(args[0])
            data = self.messages[u]
            meta = f'{u} (UID {u} INTERNALDATE "02-Nov-2026 07:12:00 +0100" BODY[] {{{len(data)}}}'.encode()
            return "OK", [(meta, data), b")"]
        raise AssertionError(command)

    def logout(self):
        pass


IMAP = {"mail_source": "imap", "imap_host": "imap.dopravca.example", "imap_user": "agent", "imap_password": "heslo"}


# ------------------------------------------------------------------ MS0 nahromadená pošta
def test_ms0_nahromadena_posta_bez_zelenej_kym_sa_schranka_nedocita(cfg, fixed_now, monkeypatch):
    """[MS0] V schránke čaká viac ako jedna dávka (50) e-mailov – hromadná pošta krátko po poslednej kontrole, výluka
    na trase vlaku je až v druhej dávke (v prvej je výluka inde, ktorá spustí prepočet). Kým sa schránka nedočíta,
    kontrola sa nepočíta ako úspešná: žiadna ZELENÁ, žiadne „schránka skontrolovaná pred 0 min“, krátke dočítavanie
    však nie je poplach; výluka sa započíta v ďalšom cykle."""
    _setup(cfg, monkeypatch)
    t0 = fixed_now
    with session_scope() as s:
        tid = _train(s, "95121", "Žilina", "Košice", ready=t0 + timedelta(minutes=30),
                     deadline=t0 + timedelta(hours=10), path_dep=t0 + timedelta(minutes=40),
                     shift_start=t0 - timedelta(minutes=25))  # nástup s prípravou 60 min pred odchodom
        set_kv(s, "mail_last_ok", {"ts": (t0 - timedelta(minutes=1)).isoformat(), "fetched": 0})  # schránka čerstvá
    for i in range(59):
        _info(cfg, i)
    subject, body = _rov("2700/2026", "Bratislava-Petržalka", "Rusovce", t0 + timedelta(hours=2),
                         t0 + timedelta(hours=6))
    _drop(cfg, "a_0001_rov2700.eml", _eml("rov-2700@zsr.example", subject, body))  # prvá dávka, mimo trasy
    subject, body = _rov("2701/2026", "Kysak", "Košice", t0 + timedelta(hours=2), t0 + timedelta(hours=7))
    _drop(cfg, "z_rov2701.eml", _eml("rov-2701@zsr.example", subject, body))       # až druhá dávka

    first = beh.run_cycle(t0)
    with session_scope() as s:
        rec = _latest(s, tid)
        assert rec is not None  # prepočet po výluke z prvej dávky
        color, reasons, bodies = rec.color, rec.reasons, [n.body for n in s.scalars(select(Notification).where(
            Notification.recommendation_id == rec.id))]
        state = get_kv(s, "mail_last_ok")
    assert color != "ZELENÁ", (rec.decision, color)
    assert any("dočítava nahromadenú poštu" in r for r in reasons), reasons  # pravdivý dôvod počas dočítavania
    assert not any("schránka skontrolovaná" in b for b in bodies)
    assert first["mail"]["fetched"] == 50 and first["mail"]["drained"] is False
    assert state.get("partial") is True and not state.get("ts")      # čiastočné čítanie nie je úspešná kontrola
    _ok, detail = health_status()
    assert "dočítava sa" in detail["schranka"] and "viac ako" not in detail["schranka"], detail["schranka"]
    assert not any("schránka" in e for e in first["errors"]), first["errors"]

    t1 = t0 + timedelta(minutes=1)
    set_fake_now(t1)
    second = beh.run_cycle(t1)
    assert second["mail"]["fetched"] == 11 and second["mail"]["drained"] is True
    with session_scope() as s:
        assert get_kv(s, "mail_last_ok")["ts"] == t1.isoformat()
        latest = _latest(s, tid)
        assert latest.decision != "VYPRAVIŤ" and any("Kysak" in r for r in latest.reasons), latest.reasons


def test_ms0_zdroje_hlasia_dalsie_emaily_nad_davku(cfg, session, monkeypatch):
    """[MS0] Adresár, IMAP aj Microsoft Graph povedia, či po dávke ostali v schránke ďalšie e-maily."""
    from vlaky.posta.zdroje import BATCH, DirectorySource, GraphSource, ImapSource

    folder = Path(cfg.mail_dir)
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(BATCH + 1):
        (folder / f"m{i:03d}.eml").write_bytes(b"Subject: FIKTIVNE\r\n\r\nx")
    src = DirectorySource()
    assert len(src.fetch_new()) == BATCH and src.more is True
    for p in sorted(folder.glob("*.eml"))[:BATCH]:
        p.unlink()
    assert len(src.fetch_new()) == 1 and src.more is False

    _setup(cfg, monkeypatch, **IMAP)
    _FakeImap.messages = {u: _eml(f"imap-{u}@zsr.example", f"FIKTÍVNE {u}", "x") for u in range(1, BATCH + 3)}
    imap = ImapSource(session, factory=_FakeImap)
    raws = imap.fetch_new()
    assert len(raws) == BATCH and imap.more is True
    for raw in raws:
        imap.mark_done(raw)
    rest = ImapSource(session, factory=_FakeImap)
    assert len(rest.fetch_new()) == 2 and rest.more is False

    _setup(cfg, monkeypatch, graph_tenant_id="tenant-fiktivny", graph_client_id="klient", graph_client_secret="tajne",
           graph_mailbox="agent@dopravca.example")

    def graph(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "login.microsoftonline.com" in url:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if url.endswith("/$value"):
            return httpx.Response(200, content=b"Subject: FIKTIVNE\r\n\r\nx")
        if "skiptoken" in url:
            return httpx.Response(200, json={"value": [{"id": "z1", "internetMessageId": "<z1@zsr.example>"}],
                                             "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?$deltatoken=D"})
        if "/messages/delta" in url:
            return httpx.Response(200, json={
                "value": [{"id": f"m{i}", "internetMessageId": f"<m{i}@zsr.example>"} for i in range(BATCH)],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/users/x/mailFolders/inbox/messages/delta"
                                   "?$skiptoken=S"})
        return httpx.Response(404)

    g = GraphSource(session, client=httpx.Client(transport=httpx.MockTransport(graph)))
    raws = g.fetch_new()
    assert len(raws) == BATCH and g.more is True
    for raw in raws:
        g.mark_done(raw)
    g2 = GraphSource(session, client=httpx.Client(transport=httpx.MockTransport(graph)))
    assert [r.uid for r in g2.fetch_new()] == ["z1"] and g2.more is False


def test_ms0_imap_zastaveny_na_chybnom_emaile_nie_je_docitany(cfg, session, fixed_now, monkeypatch):
    """[MS0] IMAP číta v poradí UID: keď sa e-mail nedá uložiť, ďalšie za ním ostanú neprečítané – to nie je úspešná
    kontrola schránky (jeden chybný e-mail sa skúsi znova v ďalšom cykle)."""
    from vlaky.posta import spracovanie

    _setup(cfg, monkeypatch, **IMAP)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeImap)
    _FakeImap.messages = {1: _eml("imap-a@zsr.example", "FIKTÍVNE A", "x"),
                          2: _eml("imap-b@zsr.example", "FIKTÍVNE B", "x")}
    real = spracovanie._store_one

    def flaky(session, raw):
        if raw.uid == "1":
            raise RuntimeError("databáza je zaneprázdnená")
        return real(session, raw)

    monkeypatch.setattr(spracovanie, "_store_one", flaky)
    summary = spracovanie.process_mailbox(session, now=fixed_now)
    assert summary["fetch_ok"] is True and summary["drained"] is False
    assert get_kv(session, "mail_last_ok").get("partial") is True


def test_ms0_dlhe_docitavanie_je_problem_zdravia(cfg, fixed_now, monkeypatch):
    """[MS0] Dočítavanie nahromadenej pošty je v poriadku len krátko: keď od poslednej úplnej kontroly schránky
    uplynulo viac ako 30 min (napr. po výpadku agenta), zdravie aj cyklus to hlásia (heartbeat /fail)."""
    def ago(minutes: int) -> str:
        return (fixed_now - timedelta(minutes=minutes)).isoformat()

    _setup(cfg, monkeypatch)
    with session_scope() as s:
        set_kv(s, "last_evaluate", FAR)
        set_kv(s, "mail_last_ok", {"ts": ago(45), "fetched": 0})  # agent stál 45 min
    for i in range(51):
        _info(cfg, i)
    summary = beh.run_cycle(fixed_now)
    assert any(e.startswith("schránka: dočítava sa") and "viac ako 30 min" in e for e in summary["errors"]), \
        summary["errors"]
    assert summary["mail"]["drained"] is False
    ok, detail = health_status()
    assert not ok and "viac ako 30 min" in detail["schranka"], detail

    from vlaky.agent.zdravie import mailbox_status

    assert mailbox_status({"ts": ago(5)}, fixed_now) == (True, "pred 5 min")
    ok, text = mailbox_status({"partial": True, "last_full": ago(10), "since": ago(2)}, fixed_now)
    assert ok and "dočítava sa" in text and "pred 10 min" in text
    ok, text = mailbox_status({"partial": True, "last_full": ago(45), "since": ago(1)}, fixed_now)
    assert not ok and "viac ako 30 min" in text
    assert mailbox_status({"partial": True, "last_full": None, "since": ago(5)}, fixed_now)[0] is True
    assert mailbox_status({"partial": True, "last_full": None, "since": ago(40)}, fixed_now)[0] is False
    assert mailbox_status({}, fixed_now) == (False, "zatiaľ neprebehol")


def test_ms0_pravidelny_prepocet_pocka_na_docitanie_schranky(cfg, fixed_now, monkeypatch):
    """[MS0] Kým sa nahromadená pošta dočítava (najviac VLAKY_REEVALUATE_MINUTES), pravidelný prepočet počká – inak by
    zelené vlaky dostali ORANŽOVÚ a po dočítaní znova ZELENÚ (dve zbytočné správy o každom vlaku)."""
    from vlaky.planovanie import planovac

    _setup(cfg, monkeypatch)
    with session_scope() as s:  # pravidelný prepočet je na rade
        set_kv(s, "last_evaluate", {"ts": (fixed_now - timedelta(minutes=20)).isoformat()})
    calls: list = []
    monkeypatch.setattr(planovac, "evaluate_all", lambda session, now=None, trigger="": calls.append(trigger) or [])
    for i in range(51):
        _info(cfg, i)
    first = beh.run_cycle(fixed_now)
    assert calls == [] and first["mail"]["drained"] is False
    t1 = fixed_now + timedelta(seconds=10)
    set_fake_now(t1)
    beh.run_cycle(t1)
    assert calls == ["pravidelný prepočet"]


def test_ms0_pri_nahromadenej_poste_pride_dalsi_cyklus_hned(cfg, monkeypatch):
    """[MS0] Kým sa nahromadená pošta dočítava, ďalší cyklus agenta príde o pár sekúnd (každý cyklus ostáva krátky
    a stráženie beží), nie až o VLAKY_MAIL_POLL_SECONDS; pri chybe uloženia e-mailu však s bežným odstupom."""
    clock = [1000.0]
    marks: list = []
    summaries = [{"errors": [], "mail": {"fetch_ok": True, "drained": False, "errors": []}},
                 {"errors": [], "mail": {"fetch_ok": True, "drained": False,
                                         "errors": ["E-mail 7 sa nepodarilo uložiť (RuntimeError: x)."]}},
                 {"errors": [], "mail": {"fetch_ok": True, "drained": True, "errors": []}}]

    def fake_cycle():
        marks.append(clock[0])
        if len(marks) == len(summaries):
            beh._STOP["flag"] = True
        return summaries[len(marks) - 1]

    def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(beh, "run_cycle", fake_cycle)
    monkeypatch.setattr(beh.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(beh.time, "sleep", fake_sleep)
    monkeypatch.setattr(beh.signal, "signal", lambda *a: None)
    try:
        assert beh.run_agent() == 0
    finally:
        beh._STOP["flag"] = False
    gaps = [b - a for a, b in zip(marks, marks[1:])]
    assert gaps[0] <= 6, gaps                          # nahromadená pošta: ďalšia dávka hneď
    assert gaps[1] >= cfg.mail_poll_seconds - 1, gaps  # chyba uloženia: ďalší pokus s bežným odstupom


# ------------------------------------------------------------------ MS1 čas po dlhom čítaní schránky
def test_ms1_po_dlhom_citani_schranky_plati_aktualny_cas(cfg, monkeypatch):
    """[MS1] Čítanie schránky trvalo 40 min (jazykový model pri každom e-maile čakal na časový limit): prepočet,
    upozornenie na pracovný čas aj odovzdávka služby počítajú s časom po čítaní schránky, nie so začiatkom cyklu.
    Okno odovzdávky 06:00 – 06:29 uplynulo počas cyklu – odovzdávka sa doplní."""
    from vlaky.posta import spracovanie

    _setup(cfg, monkeypatch, shift_change_hours="6")
    t0 = datetime(2026, 11, 2, 4, 50, tzinfo=UTC)  # 05:50 miestneho času
    set_fake_now(t0)
    real_extract = spracovanie.extract

    def slow_extract(doc, **kw):
        set_fake_now(now_utc() + timedelta(minutes=8))  # časový limit jazykového modelu
        return real_extract(doc, **kw)

    monkeypatch.setattr(spracovanie, "extract", slow_extract)
    try:
        with session_scope() as s:
            tid = _train(s, "95151", "Žilina", "Košice", ready=t0 + timedelta(minutes=10),
                         deadline=t0 + timedelta(hours=10), path_dep=t0 + timedelta(minutes=20),
                         shift_start=t0 - timedelta(minutes=10))
            # vlak v jazde: rušňovodič dosiahne limit 13 h o 06:35 (5 min po skončení dlhého čítania schránky)
            _train(s, "95152", "Čadca", "Košice", ready=t0 - timedelta(hours=11), deadline=t0 + timedelta(hours=8),
                   status="v_jazde", departed=t0 - timedelta(hours=11),
                   shift_start=t0 + timedelta(minutes=45) - timedelta(hours=13))
        for i in range(5):
            _info(cfg, i)
        beh.run_cycle(t0)
        end = now_utc()
        assert end >= t0 + timedelta(minutes=40)
        with session_scope() as s:
            rec = _latest(s, tid)
            created, depart = ensure_utc(rec.created_at), (rec.chosen or {}).get("depart")
            crew = [n.subject for n in s.scalars(select(Notification).where(
                Notification.kind == "upozornenie", Notification.recipient == D1))]
            reports = s.scalar(select(func.count(ShiftHandoverReport.id)))
            done = get_kv(s, "handover_done")
    finally:
        set_fake_now(None)
    assert created >= end - timedelta(minutes=1), created                    # nie 05:50
    assert depart is None or datetime.fromisoformat(depart) >= end - timedelta(minutes=1), depart
    assert any("zostáva 5 min" in x for x in crew) and not any("zostáva 45 min" in x for x in crew), crew
    assert reports == 1 and done.get("key") == "2026-11-02-06", done


def test_ms1_zmeskana_odovzdavka_sa_doplni_najviac_90_min(cfg, monkeypatch):
    """[MS1] Agent nebežal počas okna odovzdávky (06:00 – 06:29): o 06:45 odovzdávku doplní (raz), po 07:30 už nie."""
    _setup(cfg, monkeypatch, shift_change_hours="6,18")
    with session_scope() as s:
        set_kv(s, "last_evaluate", FAR)
    try:
        for minutes in (45, 50):  # 06:45 a 06:50 miestneho času
            t = datetime(2026, 11, 2, 5, minutes, tzinfo=UTC)
            set_fake_now(t)
            beh.run_cycle(t)
        with session_scope() as s:
            assert s.scalar(select(func.count(ShiftHandoverReport.id))) == 1
            assert get_kv(s, "handover_done")["key"] == "2026-11-02-06"
            set_kv(s, "handover_done", {})
        t = datetime(2026, 11, 2, 6, 40, tzinfo=UTC)  # 07:40 – viac ako 90 min po výmene
        set_fake_now(t)
        beh.run_cycle(t)
        with session_scope() as s:
            assert s.scalar(select(func.count(ShiftHandoverReport.id))) == 1
    finally:
        set_fake_now(None)


# ------------------------------------------------------------------ MS4 prepočet po e-maile
def _ms4_train(fixed_now) -> int:
    with session_scope() as s:
        return _train(s, "95101", "Žilina", "Košice", ready=fixed_now + timedelta(minutes=30),
                      deadline=fixed_now + timedelta(hours=10), path_dep=fixed_now + timedelta(minutes=40),
                      shift_start=fixed_now - timedelta(minutes=25))  # nástup s prípravou 60 min pred odchodom


def test_ms4_v_cykle_s_emailom_sa_vlaky_prepocitaju_len_raz(cfg, fixed_now, monkeypatch):
    """[MS4] Prepočet po e-maile (nepotvrdená výluka) prepočíta všetky vlaky – pravidelný prepočet, ktorý je v tom
    istom cykle na rade, sa už nespustí (celý vozový park by sa počítal dvakrát) a posunie sa."""
    from vlaky.planovanie import planovac

    _setup(cfg, monkeypatch)
    _ms4_train(fixed_now)
    calls: list = []
    real = planovac.evaluate_all

    def counting(session, now=None, trigger=""):
        calls.append(trigger)
        return real(session, now, trigger)

    monkeypatch.setattr(planovac, "evaluate_all", counting)
    beh.run_cycle(fixed_now)
    t1 = fixed_now + timedelta(minutes=3)
    set_fake_now(t1)
    subject, body = _rov("2201/2026", "Kysak", "Košice", fixed_now + timedelta(hours=2),
                         fixed_now + timedelta(hours=7))
    _drop(cfg, "rov2201.eml", _eml("rov-2201@zsr.example", subject, body))
    with session_scope() as s:  # pravidelný prepočet je práve na rade
        set_kv(s, "last_evaluate", {"ts": (t1 - timedelta(minutes=15)).isoformat(), "count": 1})
    before = len(calls)
    out = beh.run_cycle(t1)
    assert len(calls) - before == 1 and calls[-1].startswith("e-mail:"), calls[before:]
    assert out["mail"]["planning"]["ok"] is True and out["evaluated"] == 0
    with session_scope() as s:
        assert get_kv(s, "last_evaluate")["ts"] == t1.isoformat()


def test_ms4_zlyhany_prepocet_po_emaile_agent_zopakuje_hned(cfg, fixed_now, monkeypatch):
    """[MS4] Prepočet po e-maile s nepotvrdenou výlukou zlyhá (napr. databáza je zamknutá): agent prepočíta vlaky hneď
    v tom istom cykle (ZELENÁ nesmie ostať až do pravidelného prepočtu); ak zlyhá aj to, cyklus hlási chybu."""
    from sqlalchemy.exc import OperationalError

    from vlaky.planovanie import planovac

    _setup(cfg, monkeypatch)
    tid = _ms4_train(fixed_now)
    beh.run_cycle(fixed_now)
    with session_scope() as s:
        first = _latest(s, tid)
        first_id = first.id
        assert (first.decision, first.color) == ("VYPRAVIŤ", "ZELENÁ")
    calls: list = []
    real = planovac.evaluate_all

    def flaky(session, now=None, trigger=""):
        calls.append(trigger)
        if trigger.startswith("e-mail:"):
            raise OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))
        return real(session, now, trigger)

    monkeypatch.setattr(planovac, "evaluate_all", flaky)
    t1 = fixed_now + timedelta(minutes=2)
    set_fake_now(t1)
    subject, body = _rov("2601/2026", "Kysak", "Košice", fixed_now + timedelta(hours=2),
                         fixed_now + timedelta(hours=7))
    _drop(cfg, "rov2601.eml", _eml("rov-2601@zsr.example", subject, body))
    out = beh.run_cycle(t1)
    assert out["mail"]["planning"]["ok"] is False
    assert calls[-1] == "nové údaje z e-mailu", calls
    with session_scope() as s:
        latest = _latest(s, tid)
        assert latest.id != first_id and latest.decision != "VYPRAVIŤ", (latest.decision, latest.color)

    def broken(session, now=None, trigger=""):
        raise OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))

    monkeypatch.setattr(planovac, "evaluate_all", broken)
    t2 = t1 + timedelta(minutes=1)
    set_fake_now(t2)
    subject, body = _rov("2602/2026", "Kraľovany", "Ružomberok", fixed_now + timedelta(hours=3),
                         fixed_now + timedelta(hours=6))
    _drop(cfg, "rov2602.eml", _eml("rov-2602@zsr.example", subject, body))
    out = beh.run_cycle(t2)
    assert out["mail"]["planning"]["ok"] is False
    assert any(e.startswith("plánovanie") for e in out["errors"]), out["errors"]


# ------------------------------------------------------------------ MS5 pripomienka výluky do odvolania
def test_ms5_pripomienka_vyluky_do_odvolania_sa_zopakuje(cfg, fixed_now, monkeypatch):
    """[MS5] Pripomienka výluky do odvolania sa pri výpadku servera správ neodošle: agent ju zopakuje, a kým nepríde,
    zdravie ju hlási – aj keď tým istým dispečerom medzitým prišla iná správa."""
    relay = Relay()
    relay.down = True
    _setup(cfg, monkeypatch, relay)
    with session_scope() as s:
        set_kv(s, "last_evaluate", FAR)
        s.add(Closure(kind="pomala_jazda", from_station="Žilina", to_station="Púchov",
                      start_utc=fixed_now - timedelta(days=25), end_utc=fixed_now + timedelta(days=3),
                      status="overene", source="email", speed_kmh=30, until_revoked=True,
                      external_ref="TLG 77/2026", note="Platí do odvolania."))
    beh.run_cycle(fixed_now)  # technický koniec sa posunie, pripomienka sa pri výpadku neodošle
    with session_scope() as s:
        rows = [n.status for n in s.scalars(select(Notification).where(
            Notification.dedup_key.like("do_odvolania:%")))]
    assert rows and set(rows) == {"chyba"}
    relay.down = False
    t1 = fixed_now + timedelta(minutes=2)
    set_fake_now(t1)
    with session_scope() as s:  # iná správa tým istým dispečerom prejde – pripomienka je stále nedoručená
        odosielanie.send_internal(s, "test", "[AGENT] Skúšobná správa", "FIKTÍVNE", "INFO", channels=["email"])
    ok, detail = health_status()
    assert not ok and "nedoručených" in detail["dorucenie"], detail
    for k in range(3, 12):
        t = fixed_now + timedelta(minutes=k)
        set_fake_now(t)
        beh.run_cycle(t)
    reminders = [r for r, subject in relay.sent if "do odvolania" in subject]
    assert sorted(reminders) == [D1, D2]  # každému práve raz
    _ok, detail = health_status()
    assert detail["dorucenie"] == "ok", detail


# ------------------------------------------------------------------ WR3 hlásenie rušňovodiča o probléme
def test_wr3_hlasenie_problemu_pri_vypadku_sa_zopakuje_a_strazi(client, cfg, fixed_now, monkeypatch):
    """[WR3] Rušňovodič nahlási problém počas výpadku servera správ: upozornenie dispečerom agent zopakuje po
    obnovení a zdravie ho hlási ako nedoručené, kým nepríde (iná doručená správa ho nevybaví)."""
    from test_web_spolocne import make_train
    from vlaky.web.rusnovodic import make_driver_token

    relay = Relay()
    relay.down = True
    _setup(cfg, monkeypatch, relay)
    with session_scope() as s:
        set_kv(s, "last_evaluate", FAR)
    tid = make_train(number="61001", status="v_jazde", origin="Žilina", destination="Košice")
    with session_scope() as s:
        token = make_driver_token(s.get(Train, tid))
    r = client.post("/api/hlasenie", json={"t": token, "typ": "problem",
                                           "text": "Horúce ložisko na 3. voze, stojím v Kraľovanoch"})
    assert r.status_code == 200
    with session_scope() as s:
        rows = [n.status for n in s.scalars(select(Notification).where(Notification.dedup_key.like("problem:%")))]
    assert rows and set(rows) == {"chyba"}
    relay.down = False
    t1 = fixed_now + timedelta(minutes=5)
    set_fake_now(t1)
    with session_scope() as s:
        odosielanie.send_internal(s, "test", "[AGENT] Skúšobná správa", "FIKTÍVNE", "INFO", channels=["email"])
    ok, detail = health_status()
    assert not ok and "nedoručených" in detail["dorucenie"], detail
    for k in range(6, 15):
        t = fixed_now + timedelta(minutes=k)
        set_fake_now(t)
        beh.run_cycle(t)
    problem = [r for r, subject in relay.sent if "hlásenie rušňovodiča – problém" in subject]
    assert sorted(problem) == [D1, D2]
    _ok, detail = health_status()
    assert detail["dorucenie"] == "ok", detail


def test_wr3_upozornenie_o_probleme_netvrdi_odoslanie_pri_vypadku(cfg, fixed_now, monkeypatch):
    """[WR3] _notify_problem vráti text pre rušňovodiča, keď sa upozornenie nepodarilo odoslať (nikomu) – aplikácia
    nesmie tvrdiť „Problém odoslaný dispečerovi.“; doručené upozornenie uvádza čas nahlásenia (aj pri opakovaní)."""
    from vlaky.web.rusnovodic import _notify_problem

    relay = Relay()
    relay.down = True
    _setup(cfg, monkeypatch, relay)
    note = _notify_problem("61002", "Žilina → Košice", "Porucha brzdy", fixed_now)
    assert note and "nepodarilo odoslať" in note and "zavolajte" in note.lower()
    relay.down = False
    assert _notify_problem("61003", "Žilina → Košice", "Porucha brzdy", fixed_now) is None
    with session_scope() as s:
        body = s.scalar(select(Notification.body).where(Notification.dedup_key.like("problem:61003:%"),
                                                        Notification.status == "odoslana"))
    assert "Nahlásené: 02.11. 05:00" in body


# ------------------------------------------------------------------ WR4 nedoručené podľa odporúčania
def test_wr4_nedorucene_sa_pocitaju_podla_odporucania_nie_podla_kluca(cfg, fixed_now, monkeypatch):
    """[WR4] Kľúč správy odporúčania sa pri opakovaní posúva s odchodom: odporúčanie doručené po výpadku (s novým
    kľúčom) už nie je nedoručené; nedoručené sa počíta po správe a príjemcovi, nie po pokusoch – aj v dennej správe."""
    _setup(cfg, monkeypatch, daily_report_hour=5)
    with session_scope() as s:
        set_kv(s, "last_evaluate", FAR)
        train = Train(number="66001", origin="Žilina", destination="Košice", traction="E", status="planovany")
        s.add(train)
        s.flush()
        recs = []
        for fp in ("ODSTAVIŤ|ZELENÁ", "POČKAŤ|ORANŽOVÁ"):
            rec = Recommendation(train_id=train.id, decision=fp.split("|")[0], color=fp.split("|")[1],
                                 priority="DÔLEŽITÉ", summary="FIKTÍVNE", state="nove", fingerprint=fp,
                                 chosen={"depart": (fixed_now + timedelta(hours=5)).isoformat(), "route": "A – B"},
                                 created_at=fixed_now - timedelta(hours=2))
            s.add(rec)
            s.flush()
            recs.append(rec.id)

        def attempt(rec_id: int, minutes: int, key: str, status: str, recipient: str) -> None:
            s.add(Notification(recommendation_id=rec_id, kind="odporucanie", channel="email", recipient=recipient,
                               subject="[AGENT][DÔLEŽITÉ] Vlak 66001", body="FIKTÍVNE", priority="DÔLEŽITÉ",
                               dedup_key=key, status=status, created_at=fixed_now - timedelta(minutes=minutes),
                               error="Chyba kanála: ConnectionRefusedError(111)" if status == "chyba" else ""))

        for recipient in (D1, D2):  # výpadok, kľúč sa posúval; po obnovení odporúčanie prišlo s novým kľúčom
            attempt(recs[0], 110, f"rec:{train.id}:a:100", "chyba", recipient)
            attempt(recs[0], 100, f"rec:{train.id}:a:101", "chyba", recipient)
            attempt(recs[0], 90, f"rec:{train.id}:a:102", "odoslana", recipient)
        attempt(recs[1], 80, f"rec:{train.id}:b:200", "chyba", D1)  # druhé odporúčanie D1 zatiaľ neprišlo
        attempt(recs[1], 70, f"rec:{train.id}:b:201", "chyba", D1)
    with session_scope() as s:
        und = undelivered(s, fixed_now)
    assert [(u.recommendation_id, u.recipient) for u in und] == [(recs[1], D1)]  # jedna správa, nie dva pokusy
    beh.run_cycle(fixed_now)  # denná správa (05:00 miestneho času)
    with session_scope() as s:
        daily = s.scalar(select(Notification.body).where(Notification.subject.contains("Denná správa")))
    assert "nedoručené správy za 24 h" not in daily
    assert "neúspešné pokusy o doručenie za 24 h: 6" in daily and "nedoručené správy: " in daily, daily


# ------------------------------------------------------------------ MS6 fronta na potvrdenie
def test_ms6_fronta_rovnaka_v_suhrne_odovzdavke_dennej_sprave_aj_na_webe(cfg, fixed_now, monkeypatch):
    """[MS6] Automaticky zapísaná, dispečerom neoverená výluka je vo fronte (/fronta, počítadlo v hlavičke) – súhrn,
    odovzdávka aj denná správa ju počítajú rovnako a súhrn s odovzdávkou ju uvedú zvlášť („overte“)."""
    from vlaky.agent.odovzdavka import build_report
    from vlaky.notifikacie.odosielanie import send_digest
    from vlaky.web.data import pending_queue_count

    _setup(cfg, monkeypatch, daily_report_hour=5)
    with session_scope() as s:
        set_kv(s, "last_evaluate", FAR)
        em = Email(message_id="<fronta-1@zsr.example>", sender="vyluky.orke@zsr.example", subject="ROV (FIKTÍVNE)",
                   body_text="FIKTÍVNE", status="spracovany", verdict="ok")
        s.add(em)
        s.flush()
        s.add(ExtractedEvent(email_id=em.id, kind="avizo", status="navrh", needs_confirmation=True,
                             data={"train_number": "47251"}, confidence=0.6))
        auto = ExtractedEvent(email_id=em.id, kind="vyluka_nova", status="aplikovane", needs_confirmation=False,
                              data={}, confidence=0.95)
        s.add(auto)
        s.flush()
        s.add(Closure(kind="uplna", from_station="Poprad-Tatry", to_station="Liptovský Mikuláš",
                      start_utc=fixed_now + timedelta(days=10), end_utc=fixed_now + timedelta(days=10, hours=6),
                      status="navrh", source="email", source_event_id=auto.id))
        s.add(ExtractedEvent(email_id=em.id, kind="avizo", status="potvrdene", needs_confirmation=True,
                             data={"train_number": "47250"}, confidence=0.9,
                             validation={"apply_result": {"ok": False, "error": "Vlak 47250 nenájdený"}}))
    with session_scope() as s:
        assert pending_queue_count(s) == 3  # počítadlo v hlavičke webu (/fronta)
        sent = send_digest(s, fixed_now - timedelta(hours=2))
        content, data = build_report(s, fixed_now)
        digests = [n.body for n in sent if n.channel == "email"]
    assert digests and all("Čaká na potvrdenie dispečerom: 3 udalosti" in b for b in digests), digests
    assert all("z toho zapísané automaticky – overte: 1" in b for b in digests)
    assert data["waiting"] == 3 and "Nepotvrdené udalosti z e-mailov: 3" in content
    assert "z toho zapísané automaticky – overte: 1" in content
    beh.run_cycle(fixed_now)  # denná správa (05:00 miestneho času)
    with session_scope() as s:
        daily = s.scalar(select(Notification.body).where(Notification.subject.contains("Denná správa")))
    assert "udalosti čakajúce na potvrdenie: 3 (z toho zapísané automaticky – overte: 1)" in daily, daily
    from vlaky.agent.zdravie import queue_counts  # jedno počítanie pre agenta aj web

    with session_scope() as s:
        assert queue_counts(s) == {"waiting": 1, "auto": 1, "failed": 1, "total": 3}


# ------------------------------------------------------------------ OD0 test-notify a outbox v ostrej prevádzke
def test_od0_test_notify_skusi_kazdy_kanal_a_pri_chybe_vrati_1(cfg, monkeypatch, capsys):
    """[OD0] test-notify pošle skúšobnú správu na e-mail, Teams aj SMS (nielen e-mail) a pri chybe skončí kódom 1."""
    from vlaky.__main__ import main

    relay = Relay()
    _setup(cfg, monkeypatch, relay, teams_webhook_url="https://teams.example/webhook",
           sms_gateway_url="https://sms.example/brana")
    posted: list = []

    def post(url, *a, json=None, **k):
        posted.append(url)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(odosielanie.httpx, "post", post)
    assert main(["test-notify"]) == 0
    out = capsys.readouterr().out
    assert sorted(posted) == ["https://sms.example/brana", "https://teams.example/webhook"]
    assert sorted(r for r, _s in relay.sent) == [D1, D2]
    assert "teams webhook odoslana" in out and "sms +421900000001 odoslana" in out
    relay.down = True
    assert main(["test-notify", "--kanal", "email"]) == 1
    assert "VÝSLEDOK: CHYBA" in capsys.readouterr().err


def test_od0_bez_servera_sprav_v_ostrej_prevadzke_nic_nie_je_dorucene(cfg, fixed_now, monkeypatch):
    """[OD0] Ostrá prevádzka bez servera správ (preklep VLAKY_SMTP_SERVER namiesto VLAKY_SMTP_HOST, čísla bez SMS
    brány): KRITICKÁ správa nie je „doručená do outboxu“, ale chyba doručenia; zdravie aj cyklus to hlásia."""
    _setup(cfg, monkeypatch, env="prod", smtp_host="")
    monkeypatch.setenv("VLAKY_SMTP_SERVER", "relay.dopravca.example")
    with session_scope() as s:
        out = odosielanie.send_internal(s, "upozornenie", "[AGENT][KRITICKÉ] Rušňovodič vlaku 95004 (FIKTÍVNE)",
                                        "FIKTÍVNE", "KRITICKÉ", dedup_key="crew:test:1")
        statuses = {(n.channel, n.status) for n in out}
    assert statuses == {("email", "chyba"), ("sms", "chyba")}
    ok, detail = health_status()
    assert not ok and "nedoručených" in detail["dorucenie"]
    assert "VLAKY_SMTP_SERVER" in detail["konfiguracia"] and "SMS brána" in detail["konfiguracia"]
    with session_scope() as s:
        set_kv(s, "last_evaluate", FAR)
    summary = beh.run_cycle(fixed_now)
    assert any(e.startswith("nastavenie odosielania") for e in summary["errors"]), summary["errors"]


# ------------------------------------------------------------------ OD2 create-user
def test_od2_create_user_neprepise_existujuci_ani_zruseny_ucet(cfg, capsys):
    """[OD2] create-user existujúci účet nezmení (heslo, rola, kontakty, zrušený účet); zmena len s --update a len
    zadaných údajov, zrušený účet povolí až --activate. Meno ako vo webe (malé písmená)."""
    from vlaky.__main__ import main
    from vlaky.pouzivatelia import authenticate

    def row() -> tuple:
        with session_scope() as s:
            u = s.scalar(select(User).where(User.username == "jnovak"))
            return u.id, u.role, u.full_name, u.email, u.phone, u.password_hash, u.active

    assert main(["create-user", "jnovak", "--role", "veduci", "--password", "Povodne-Heslo-2026",
                 "--full-name", "Ján Novák", "--email", "jan.novak@dopravca.example", "--phone", "+421900000002"]) == 0
    with session_scope() as s:
        s.scalar(select(User).where(User.username == "jnovak")).active = False  # odchod zamestnanca
    before = row()
    assert main(["create-user", "jnovak", "--role", "dispecer", "--password", "Nove-Heslo-2026"]) == 2
    assert "už existuje" in capsys.readouterr().err
    assert row() == before
    assert main(["create-user", "jnovak", "--update", "--password", "Nove-Heslo-2026"]) == 0
    after = row()
    assert after[:5] == before[:5] and after[5] != before[5] and after[6] is False
    with session_scope() as s:
        assert authenticate(s, "jnovak", "Nove-Heslo-2026") is None  # zrušený účet sa neprihlási
    assert main(["create-user", "jnovak", "--update", "--activate"]) == 0
    with session_scope() as s:
        assert authenticate(s, "jnovak", "Nove-Heslo-2026") is not None
    assert main(["create-user", "PNovak", "--password", "Heslo-2026-fiktivne"]) == 0
    with session_scope() as s:
        assert s.scalar(select(User.role).where(User.username == "pnovak")) == "dispecer"
    assert main(["create-user", "kratke", "--password", "kratke"]) == 2  # bez výpisu chyby Pythonu
    assert "aspoň 10 znakov" in capsys.readouterr().err
    with session_scope() as s:
        assert s.scalar(select(User.id).where(User.username == "kratke")) is None


# ------------------------------------------------------------------ OD5 náklady po slovensky
def test_od5_naklad_v_emaile_a_teams_po_slovensky(cfg):
    """[OD5] Náklad v správe (e-mail, Teams) ako na webe: „1 593 €“, nie „1,593 €“ (v slovenčine desatinná čiarka)."""
    from vlaky.notifikacie import texty

    rec = Recommendation(id=7, train_id=1, decision="ODSTAVIŤ", color="ORANŽOVÁ", priority="DÔLEŽITÉ",
                         summary="FIKTÍVNE",
                         chosen={"depart": "2026-11-02T10:00:00+00:00", "route": "Žilina – Košice",
                                 "expected_cost": 1593.4},
                         variants=[{"route": "Žilina – Košice", "depart": "2026-11-02T10:00:00+00:00",
                                    "kind": "ODSTAVIŤ", "status": "ORANŽOVÁ", "p_on_time": 0.8,
                                    "expected_cost": 1250.0},
                                   {"route": "cez Zvolen", "depart": "2026-11-02T11:00:00+00:00",
                                    "kind": "PRESMEROVAŤ", "status": "ZELENÁ", "p_on_time": 0.9,
                                    "expected_cost": 980.0}])
    body = texty.recommendation_body(rec)
    assert "Očakávaný náklad: 1 593 €" in body and "1 250 €" in body and "980 €" in body
    assert "1,593" not in body and "1,250" not in body
    assert texty.eur(12.5, 2) == "12,50 €"


# ------------------------------------------------------------------ OD6 check.sh bez Dockera
def _stub(folder: Path, name: str, script: str) -> Path:
    path = folder / name
    path.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash nie je k dispozícii")
def test_od6_check_sh_bez_dockera_kontroluje_zdravie_agenta_aj_https(tmp_path):
    """[OD6] Inštalácia bez Dockera (systemd): check.sh spustí zdravie agenta s konfiguráciou služieb, doménu nájde
    vo VLAKY_PUBLIC_URL (/etc/vlaky/vlaky.env) a čo sa skontrolovať nedá, ohlási ako CHYBU – nie potichu OK."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "systemctl", "exit 0\n")
    _stub(bin_dir, "curl", "echo 200\n")
    _stub(bin_dir, "openssl", 'if [ "$1" = x509 ]; then echo "notAfter=Jan  1 00:00:00 2099 GMT"; '
                              'else cat >/dev/null; fi\n')
    backups = tmp_path / "zalohy"
    (backups / "db").mkdir(parents=True)
    (backups / "db" / "vlaky-db-20261101T000000Z.sql.gz").write_bytes(b"x")
    py_ok = _stub(tmp_path, "py_ok", "echo \"{'stav': 'OK'}\"\nexit 0\n")
    py_bad = _stub(tmp_path, "py_bad", "echo \"dopravca=$VLAKY_CARRIER_NAME {'stav': 'PROBLÉM'}\"\nexit 1\n")
    env_file = tmp_path / "vlaky.env"
    env_file.write_text("VLAKY_PUBLIC_URL=https://agent.dopravca.example\n"
                        "VLAKY_CARRIER_NAME=Dopravca s.r.o. (FIKTÍVNY)\n", encoding="utf-8")

    def run(**extra) -> subprocess.CompletedProcess:
        ops = tmp_path / "ops.env"
        values = {"DEPLOY_MODE": "systemd", "VLAKY_APP_DIR": str(ROOT), "BACKUP_DIR": str(backups),
                  "CHECK_DISK_PATHS": "/", "CHECK_DISK_MAX_PCT": "100",
                  "RESTIC_REPOSITORY": "s3:https://zalohy.example/vlaky", **extra}
        ops.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("VLAKY_")}
        env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", OPS_ENV_FILE=str(ops), NO_COLOR="1")
        return subprocess.run(["bash", str(ROOT / "deploy" / "monitoring" / "check.sh")], env=env,
                              capture_output=True, text=True, timeout=120)

    r = run(CHECK_ENV_FILE=str(tmp_path / "neexistuje.env"), CHECK_PYTHON=str(tmp_path / "neexistuje"))
    assert r.returncode == 1, r.stdout
    assert "HTTPS a certifikát sa NEKONTROLUJÚ" in r.stdout and "zdravie agenta sa nedá overiť" in r.stdout
    r = run(CHECK_ENV_FILE=str(env_file), CHECK_PYTHON=str(py_ok))
    assert r.returncode == 0, r.stdout
    assert "zdravie agenta: OK" in r.stdout
    assert "https://agent.dopravca.example/health odpovedá 200" in r.stdout
    assert "certifikát pre agent.dopravca.example platí" in r.stdout
    r = run(CHECK_ENV_FILE=str(env_file), CHECK_PYTHON=str(py_bad))
    assert r.returncode == 1 and "zdravie agenta: PROBLÉM" in r.stdout, r.stdout
    assert "dopravca=Dopravca s.r.o. (FIKTÍVNY)" in r.stdout  # konfigurácia služieb sa načítala


# ------------------------------------------------------------------ OD4 diagnostika
def test_od4_zdravie_ukaze_skutocnu_chybu_dorucenia(cfg, monkeypatch):
    """[OD4] Zdravie pri výpadku servera správ ukáže skutočnú chybu spojenia („Chyba kanála: …“), nie správu
    odloženú ističom („Neodoslané – kanál …“) – podľa nej sa v kapitole 13 rozlišuje výpadok od odmietnutia
    príjemcu. (Kódy chýb schránky – Graph invalid_client/AADSTS…, IMAP AUTHENTICATIONFAILED – patria do zdrojov
    pošty, mimo tejto oblasti.)"""
    relay = Relay()
    relay.down = True
    _setup(cfg, monkeypatch, relay)
    with session_scope() as s:
        odosielanie.send_internal(s, "upozornenie", "[AGENT][DÔLEŽITÉ] FIKTÍVNE", "FIKTÍVNE", "DÔLEŽITÉ",
                                  dedup_key="test:od4", channels=["email"])
    _ok, detail = health_status()
    assert "Chyba kanála: ConnectionRefusedError" in detail["dorucenie"], detail
    assert "Neodoslané" not in detail["dorucenie"]
