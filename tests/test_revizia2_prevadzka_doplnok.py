"""Regresné testy k doplnkovej oprave prevádzky agenta (kontrola opráv 2. revízie): jedno odmietnuté číslo
nezablokuje SMS ostatným, čiastočne doručená odovzdávka, denná správa a súhrn prídu aj tomu, komu nepodarilo
doručiť, KRITICKÉ odporúčanie po obnovení servera do 5 min, chybné odporúčanie nezastaví doručenie ostatných
a limit spojenia z nastavenia."""
from __future__ import annotations

import smtplib
import socket
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select

from vlaky import config as config_mod
from vlaky.agent import beh
from vlaky.agent.zdravie import get_kv, set_kv
from vlaky.cas import now_utc, set_fake_now
from vlaky.db import session_scope
from vlaky.models import (AgentHealth, Driver, DriverShift, ExtractedEvent, Notification, Recommendation,
                          ShiftHandoverReport, Train)
from vlaky.notifikacie import odosielanie

D1, D2 = "dispecer1@dopravca.example", "dispecer2@dopravca.example"
BAD, GOOD = "+421900000001", "+421900000002"


class Relay:
    """Falošný SMTP server: funguje, odmieta spojenie (down) alebo ticho zahadzuje (blackhole – spojenie visí do
    časového limitu); príjemca natrvalo odmietnutý (550) alebo dočasne (450 pri prvých n pokusoch)."""

    def __init__(self, mode: str = "ok"):
        self.mode, self.sent, self.connects, self.reject, self.transient = mode, [], [], set(), {}

    def smtp(self):
        relay = self

        class FakeSMTP:
            def __init__(self, host, port, timeout=30):
                relay.connects.append((timeout, now_utc()))
                if relay.mode == "down":
                    raise ConnectionRefusedError(111, "Connection refused")
                if relay.mode == "blackhole":
                    set_fake_now(now_utc() + timedelta(seconds=timeout))
                    raise socket.timeout("timed out")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self, context=None):
                pass

            def login(self, u, p):
                pass

            def send_message(self, msg, to_addrs=None):
                rcpt = to_addrs[0]
                if rcpt in relay.reject:
                    raise smtplib.SMTPRecipientsRefused({rcpt: (550, b"5.1.1 user unknown")})
                if relay.transient.get(rcpt, 0) > 0:
                    relay.transient[rcpt] -= 1
                    raise smtplib.SMTPRecipientsRefused({rcpt: (450, b"4.2.1 mailbox busy, try later")})
                relay.sent.append((rcpt, str(msg["Subject"]), now_utc()))
                return {}

        return FakeSMTP


def _setup(cfg, monkeypatch, relay: Relay | None = None, **kw):
    """Falošný SMTP server (bez neho len outbox – žiadne skutočné spojenie), plánovač sa sám nespúšťa
    (odporúčania zakladá test), bez dennej správy a odovzdávky (ak ich test nezapne)."""
    base = {**cfg.model_dump(), "smtp_host": "relay.example" if relay is not None else "", "smtp_starttls": False,
            "teams_webhook_url": "", "reevaluate_minutes": 100000, "daily_report_hour": 23,
            "shift_change_hours": "3"}
    base.update(kw)
    if relay is not None:
        monkeypatch.setattr(odosielanie.smtplib, "SMTP", relay.smtp())
    s = config_mod.override_settings(**base)
    with session_scope() as sess:
        set_kv(sess, "last_evaluate", {"ts": "2100-01-01T00:00:00+00:00"})
    return s


def _gateway(monkeypatch, bad_status: int) -> list:
    """Falošná SMS brána: číslo BAD odmietne zadaným stavom, ostatné prijme."""
    calls = []

    def post(url, *a, json=None, **k):
        calls.append(json["to"])
        status = bad_status if json["to"] == BAD else 200
        return httpx.Response(status, request=httpx.Request("POST", url), text="x")

    monkeypatch.setattr(odosielanie.httpx, "post", post)
    return calls


def _train(s, number="95001", status="planovany") -> Train:
    t = Train(number=number, origin="Žilina", destination="Košice", traction="E", status=status)
    s.add(t)
    s.flush()
    return t


def _rec(s, train_id, depart, decision="ODSTAVIŤ", color="ZELENÁ", priority="DÔLEŽITÉ", created_at=None,
         **kw) -> Recommendation:
    """Nové odporúčanie (ako plánovač: predchádzajúce nové odporúčanie vlaku je zastarané)."""
    for old in s.scalars(select(Recommendation).where(Recommendation.train_id == train_id,
                                                      Recommendation.state == "nove")):
        old.state = "zastarane"
    r = Recommendation(train_id=train_id, decision=decision, color=color, priority=priority, summary="x",
                       chosen={"depart": depart.isoformat(), "route": "A – B"}, state="nove",
                       fingerprint=f"{decision}|{color}", created_at=created_at or now_utc(), **kw)
    s.add(r)
    s.flush()
    return r


def _rows(s, **where) -> list:
    q = select(Notification).order_by(Notification.id)
    for k, v in where.items():
        q = q.where(getattr(Notification, k) == v)
    return list(s.scalars(q))


def _cycles(start, minutes, step=1, before=None) -> list:
    out = []
    for k in range(0, minutes, step):
        t = start + timedelta(minutes=k)
        if before:
            before(k)
        set_fake_now(t)
        out.append((k, beh.run_cycle(t)))
    return out


# ------------------------------------------------------------------ istič SMS a jedno odmietnuté číslo
def test_jedno_odmietnute_cislo_neblokuje_sms_ostatnym(cfg, fixed_now, monkeypatch):
    """SMS brána odmietne jedno číslo (403): KRITICKÉ odporúčanie aj upozornenie na limit 13 h dostane druhý
    dispečer hneď v prvom cykle. Odmietnuté číslo sa pri odporúčaní opakuje s bežným odstupom, nie každých 5 min."""
    _setup(cfg, monkeypatch, notify_phones=f"{BAD},{GOOD}", sms_gateway_url="https://sms.example/brana")
    _gateway(monkeypatch, 403)
    with session_scope() as s:
        t = _train(s, status="v_jazde")
        _rec(s, t.id, fixed_now + timedelta(hours=7), "ESKALOVAŤ", "ČERVENÁ", "KRITICKÉ", created_at=fixed_now)
        driver = Driver(name="Ján Fiktívny")
        s.add(driver)
        s.flush()
        s.add(DriverShift(driver_id=driver.id, train_id=t.id, status="prebieha",
                          start_utc=fixed_now - timedelta(hours=12, minutes=40)))  # do limitu zostáva 20 min
    set_fake_now(fixed_now)
    beh.run_cycle(fixed_now)
    with session_scope() as s:
        good = {(n.kind, n.status) for n in _rows(s, channel="sms", recipient=GOOD)}
    assert good == {("odporucanie", "odoslana"), ("upozornenie", "odoslana")}, good
    _cycles(fixed_now + timedelta(minutes=5), 4 * 60 - 5, step=5)
    with session_scope() as s:
        bad_tries = _rows(s, channel="sms", recipient=BAD, kind="odporucanie")
        good_sent = [n for n in _rows(s, channel="sms", recipient=GOOD, kind="odporucanie") if n.status == "odoslana"]
    assert len(good_sent) == 1
    assert 4 <= len(bad_tries) <= 9, len(bad_tries)  # 5, 10, 20, 40, 60 min – nie 48 pokusov za 4 h


def test_chyba_servera_pri_jednom_cisle_neblokuje_sms_ostatnym_natrvalo(cfg, fixed_now, monkeypatch):
    """Brána vráti pre jedno číslo 500 (istič kanála sa na cyklus vypne): pri ďalšom pokuse ide SMS najprv
    dispečerovi, ktorému zatiaľ nezlyhala – nefunkčné číslo ho natrvalo neblokuje."""
    _setup(cfg, monkeypatch, notify_phones=f"{BAD},{GOOD}", sms_gateway_url="https://sms.example/brana")
    calls = _gateway(monkeypatch, 500)
    with session_scope() as s:
        t = _train(s)
        _rec(s, t.id, fixed_now + timedelta(hours=7), "ESKALOVAŤ", "ČERVENÁ", "KRITICKÉ", created_at=fixed_now)
    _cycles(fixed_now, 11)
    assert calls[0] == BAD  # prvý pokus: chyba servera vypne istič – GOOD odložené bez volania brány
    with session_scope() as s:
        good = [n for n in _rows(s, channel="sms", recipient=GOOD) if n.status == "odoslana"]
    assert good and good[0].created_at.replace(tzinfo=timezone.utc) <= fixed_now + timedelta(minutes=6), good


# ------------------------------------------------------------------ čiastočne doručené súhrnné správy
def test_ciastocne_dorucena_odovzdavka_pride_aj_druhemu_dispecerovi(cfg, monkeypatch):
    relay = Relay()
    relay.transient = {D2: 1}  # prvý pokus pre dispecer2 dočasne odmietnutý (450)
    _setup(cfg, monkeypatch, relay, shift_change_hours="6")
    t0 = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)  # 06:00 miestneho času
    _cycles(t0, 30)
    got = [x[0] for x in relay.sent if "Odovzdávka" in x[1]]
    assert sorted(got) == [D1, D2], got  # každému práve raz
    with session_scope() as s:
        assert s.scalar(select(func.count(ShiftHandoverReport.id))) == 1  # tá istá správa, žiadna nová
        assert {n.dedup_key for n in _rows(s, kind="odovzdavka")} == {"odovzdavka:1"}
        assert get_kv(s, "handover_done").get("sent") is True


def test_ciastocne_dorucena_denna_sprava_pride_aj_druhemu_dispecerovi(cfg, monkeypatch):
    relay = Relay()
    relay.transient = {D2: 1}
    _setup(cfg, monkeypatch, relay, daily_report_hour=6)
    t0 = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)  # 06:00 miestneho času
    _cycles(t0, 30)
    got = [x[0] for x in relay.sent if "Denná správa" in x[1]]
    assert sorted(got) == [D1, D2], got
    with session_scope() as s:
        assert {n.dedup_key for n in _rows(s, kind="zdravie")} == {"daily:2026-11-02"}
        assert get_kv(s, "daily_report").get("date") == "2026-11-02"


def test_suhrn_ciastocne_doruceny_a_trvalo_odmietnuty_prijemca(cfg, fixed_now, monkeypatch):
    """Súhrn, ktorý dispecer2 dočasne nedostal, mu príde (ten istý, od toho istého času); dispecer1 ho dostane
    raz. Natrvalo odmietnutý príjemca nezastaví súhrny ostatným (opakuje sa mu najdlhšie do ďalšieho súhrnu)."""
    relay = Relay()
    relay.transient = {D2: 1}
    _setup(cfg, monkeypatch, relay, digest_minutes=60)
    with session_scope() as s:
        s.add(ExtractedEvent(kind="vyluka_nova", status="navrh", needs_confirmation=True))  # obsah každého súhrnu
        set_kv(s, "digest", {"ts": (fixed_now - timedelta(minutes=60)).isoformat()})
    _cycles(fixed_now, 30)
    assert sorted(x[0] for x in relay.sent if "Súhrn" in x[1]) == [D1, D2]
    with session_scope() as s:
        assert len({n.dedup_key for n in _rows(s, kind="suhrn")}) == 1
        assert get_kv(s, "digest") == {"ts": fixed_now.isoformat()}  # ďalší súhrn od času prvého odoslania
    relay.reject = {D2}
    relay.sent.clear()
    _cycles(fixed_now + timedelta(minutes=30), 5 * 60, step=2)
    assert len([x for x in relay.sent if x[0] == D1 and "Súhrn" in x[1]]) >= 4
    with session_scope() as s:
        d2_tries = [n for n in _rows(s, kind="suhrn", recipient=D2) if n.created_at.replace(tzinfo=timezone.utc)
                    >= fixed_now + timedelta(minutes=30)]
    assert all(n.status == "chyba" for n in d2_tries) and len(d2_tries) <= 30, len(d2_tries)


# ------------------------------------------------------------------ KRITICKÉ po obnovení servera
def test_kriticke_odporucania_po_obnoveni_servera_do_5_min(cfg, fixed_now, monkeypatch):
    """Server ticho zahadzuje spojenia 60 min; KRITICKÉ odporúčania vznikli v rôznych minútach. Počas výpadku
    najviac jeden pokus o spojenie za 5 min (spoločné okno všetkých KRITICKÝCH správ), po obnovení prídu
    všetkým do 5 min – nie až po hodinovom odstupe."""
    relay = Relay("blackhole")
    _setup(cfg, monkeypatch, relay)
    with session_scope() as s:
        ids = [_train(s, f"9700{i}").id for i in range(3)]

    def before(k):
        if k < 3:
            with session_scope() as s:
                _rec(s, ids[k], fixed_now + timedelta(hours=7), "ESKALOVAŤ", "ČERVENÁ", "KRITICKÉ",
                     created_at=fixed_now + timedelta(minutes=k))
        if k == 60:
            relay.mode = "ok"

    _cycles(fixed_now, 75, before=before)
    during = [ts for _timeout, ts in relay.connects if ts < fixed_now + timedelta(minutes=60)]
    assert len(during) <= 3 + 12, len(during)  # prvé pokusy + jeden za každé 5-min okno
    up = fixed_now + timedelta(minutes=60)
    for number in ("97000", "97001", "97002"):
        got = sorted(r for r, subj, ts in relay.sent if number in subj)
        assert got == [D1, D2], (number, got)
        assert max(ts for r, subj, ts in relay.sent if number in subj) <= up + timedelta(minutes=5)


# ------------------------------------------------------------------ chybné odporúčanie v cykle
def test_chybne_odporucanie_nezastavi_dorucenie_ostatnych(cfg, fixed_now, monkeypatch):
    """Text jedného odporúčania sa nedá pripraviť (neočakávané údaje): ostatné sa doručia raz (záznamy o odoslaní
    sa nevrátia späť), chyba je vidieť v každom cykle a v heartbeate nie sú adresy ani parametre SQL."""
    relay = Relay()
    _setup(cfg, monkeypatch, relay)
    real_body, real_sms = odosielanie.recommendation_body, odosielanie.sms_text

    def body(rec):
        if rec.train.number == "95002":
            raise ValueError("neočakávané údaje\n[parameters: {'recipient': 'dispecer1@dopravca.example'}]")
        return real_body(rec)

    def sms(rec):
        if rec.train.number == "95003":
            raise TypeError("neočakávané údaje pre SMS")
        return real_sms(rec)

    monkeypatch.setattr(odosielanie, "recommendation_body", body)
    monkeypatch.setattr(odosielanie, "sms_text", sms)
    with session_scope() as s:
        a, b, c = _train(s, "95001"), _train(s, "95002"), _train(s, "95003")
        _rec(s, a.id, fixed_now + timedelta(hours=8), created_at=fixed_now)
        bad_id = _rec(s, b.id, fixed_now + timedelta(hours=8), created_at=fixed_now).id
        _rec(s, c.id, fixed_now + timedelta(hours=7), "ESKALOVAŤ", "ČERVENÁ", "KRITICKÉ", created_at=fixed_now)
    out = _cycles(fixed_now, 30)
    assert sorted(x[0] for x in relay.sent if "95001" in x[1]) == [D1, D2]  # každému práve raz
    assert len([x for x in relay.sent if "95003" in x[1]]) <= 2  # e-mail sa neopakuje po chybe pri SMS texte
    for _k, summary in out:
        assert [e for e in summary["errors"] if e.startswith(f"odporúčanie #{bad_id}:")], summary["errors"]
        assert not any("dopravca.example" in e for e in summary["errors"]), summary["errors"]
    with session_scope() as s:
        assert s.scalar(select(AgentHealth).where(AgentHealth.component == "planovanie",
                                                  AgentHealth.status == "error")) is not None


# ------------------------------------------------------------------ limit spojenia z nastavenia
class _Nastavenia:
    """Nastavenia s limitom spojenia notify_connect_timeout_s (ako keď ho konfigurácia obsahuje)."""

    def __init__(self, base, **extra):
        self._base, self._extra = base, extra

    def __getattr__(self, name):
        return self._extra[name] if name in self._extra else getattr(self._base, name)


def test_limit_spojenia_z_nastavenia(cfg, fixed_now, monkeypatch):
    relay = Relay("blackhole")
    s = _setup(cfg, monkeypatch, relay)
    monkeypatch.setattr(odosielanie, "settings", lambda: _Nastavenia(s, notify_connect_timeout_s=4))
    with session_scope() as sess:
        out = odosielanie.send_internal(sess, "upozornenie", "[AGENT][DÔLEŽITÉ] Test", "telo", "DÔLEŽITÉ",
                                        dedup_key="test:limit", channels=["email"])
    assert [timeout for timeout, _ts in relay.connects] == [4.0]
    assert len(out) == 2 and all(n.status == "chyba" for n in out)
    assert odosielanie._timeout(20).connect == 4.0
    monkeypatch.setattr(odosielanie, "settings", lambda: s)
    assert odosielanie._timeout(20).connect == 10.0  # predvolený limit, kým nastavenie nie je zadané
