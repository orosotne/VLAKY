"""Regresné testy k nálezom 2. revízie prevádzky agenta: doručovanie upozornení, opakovanie, monitoring,
odovzdávka služby a objednávky (nálezy [20] – [26], [46] a dohody o ESKALOVAŤ a neznámom nástupe rušňovodiča)."""
from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select

from vlaky import config as config_mod
from vlaky.agent import beh
from vlaky.cas import now_utc, set_fake_now
from vlaky.db import session_scope
from vlaky.hranice import seed_borders
from vlaky.models import (AgentHealth, BorderCrossing, Closure, Handover, Notification, Order, Recommendation,
                          ShiftHandoverReport, Train)
from vlaky.notifikacie import odosielanie
from vlaky.notifikacie.texty import recommendation_subject

D1, D2 = "dispecer1@dopravca.example", "dispecer2@dopravca.example"


class Relay:
    """Falošný SMTP server: funguje, odmieta spojenie, ticho zahadzuje (blackhole) alebo odmietne príjemcu."""

    def __init__(self, mode: str = "ok"):
        self.mode, self.sent, self.connects, self.reject = mode, [], [], set()

    def smtp(self):
        relay = self

        class FakeSMTP:
            def __init__(self, host, port, timeout=30):
                relay.connects.append(timeout)
                if relay.mode == "down":
                    raise ConnectionRefusedError(111, "Connection refused")
                if relay.mode == "blackhole":  # spojenie visí až do časového limitu
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
                if to_addrs[0] in relay.reject:
                    return {to_addrs[0]: (550, b"5.1.1 user unknown")}
                relay.sent.append((to_addrs[0], str(msg["Subject"])))
                return {}

        return FakeSMTP


def _setup(cfg, monkeypatch, relay: Relay | None = None, **kw):
    """Falošný SMTP server (bez neho len outbox – žiadne skutočné spojenie), plánovač sa sám nespúšťa
    (odporúčania zakladá test), bez dennej správy a odovzdávky."""
    base = {**cfg.model_dump(), "smtp_host": "relay.example" if relay is not None else "", "smtp_starttls": False,
            "teams_webhook_url": "", "reevaluate_minutes": 100000, "daily_report_hour": 23,
            "shift_change_hours": "3"}
    base.update(kw)
    if relay is not None:
        monkeypatch.setattr(odosielanie.smtplib, "SMTP", relay.smtp())
    s = config_mod.override_settings(**base)
    from vlaky.agent.zdravie import set_kv

    with session_scope() as sess:
        set_kv(sess, "last_evaluate", {"ts": "2100-01-01T00:00:00+00:00"})
    return s


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


def _cycles(start, minutes, step=1, relay=None, before=None) -> list:
    out = []
    for k in range(0, minutes, step):
        t = start + timedelta(minutes=k)
        if before:
            before(k)
        set_fake_now(t)
        out.append((k, beh.run_cycle(t)))
    return out


# ------------------------------------------------------------------ [20] A → B → A
def test_navrat_k_skorsiemu_odporucaniu_sa_posle(cfg, fixed_now):
    """Výluka platí → ŽSR ju zruší → zrušenie odvolá: návrat k pôvodnému odporúčaniu dispečeri dostanú."""
    now = fixed_now
    with session_scope() as s:
        seed_borders(s)
        s.add(Train(number="95001", origin="Žilina", destination="Košice", traction="E", status="planovany",
                    ready_utc=now + timedelta(hours=1), deadline_utc=now + timedelta(hours=30)))
        s.add(Closure(kind="uplna", from_station="Liptovský Mikuláš", to_station="Poprad-Tatry",
                      start_utc=now + timedelta(hours=2), end_utc=now + timedelta(hours=8), status="overene",
                      source="test"))
    subjects = []
    for i, st in enumerate(["overene", "zrusene", "overene"]):
        t = now + timedelta(minutes=20 * i)
        with session_scope() as s:
            c = s.scalar(select(Closure))
            c.status = st
            c.version += 1
        set_fake_now(t)
        beh.run_cycle(t, force_evaluate=True)
        with session_scope() as s:
            r = s.scalar(select(Recommendation).order_by(Recommendation.id.desc()))
            subjects.append(recommendation_subject(r))
            statuses = [n.status for n in _rows(s, recommendation_id=r.id)]
        assert statuses and all(x == "outbox" for x in statuses), (st, statuses)
    assert subjects[0] == subjects[2] != subjects[1]
    # ďalšie cykly: nič sa nemení – nič sa neposiela znova
    for k in range(1, 6):
        t = now + timedelta(minutes=40 + k)
        set_fake_now(t)
        beh.run_cycle(t)
    with session_scope() as s:
        cur = s.scalar(select(Recommendation).where(Recommendation.state == "nove"))
        last = s.scalar(select(Notification).where(Notification.kind == "odporucanie", Notification.recipient == D1,
                                                   Notification.status.in_(("odoslana", "outbox")))
                        .order_by(Notification.id.desc()))
        assert last.subject == recommendation_subject(cur)
        assert s.scalar(select(func.count(Notification.id)).where(Notification.kind == "odporucanie")) == 6


def test_opatovna_eskalacia_ide_aj_sms_a_rovnaka_sprava_sa_neopakuje(cfg, fixed_now):
    """ESKALOVAŤ (KRITICKÉ, e-mail + SMS) → vyriešené (e-mail) → znova ESKALOVAŤ: e-mail aj SMS sa pošlú.
    Tá istá správa (aj z iného odporúčania so zhodným obsahom – web a agent súčasne) sa nepošle."""
    from vlaky.notifikacie.odosielanie import notify_recommendation

    set_fake_now(fixed_now)
    with session_scope() as s:
        t = _train(s)
        esk = notify_recommendation(s, _rec(s, t.id, fixed_now + timedelta(hours=7), "ESKALOVAŤ", "ČERVENÁ",
                                            "KRITICKÉ"))
        ok = notify_recommendation(s, _rec(s, t.id, fixed_now + timedelta(hours=3)))
        again = notify_recommendation(s, _rec(s, t.id, fixed_now + timedelta(hours=7), "ESKALOVAŤ", "ČERVENÁ",
                                              "KRITICKÉ"))
        twin = notify_recommendation(s, _rec(s, t.id, fixed_now + timedelta(hours=7), "ESKALOVAŤ", "ČERVENÁ",
                                             "KRITICKÉ"))
    assert {n.channel for n in esk} == {"email", "sms"} and all(n.status == "outbox" for n in esk)
    assert all(n.status == "outbox" for n in ok)
    assert {n.channel for n in again} == {"email", "sms"}
    assert all(n.status == "outbox" for n in again), [(n.channel, n.status) for n in again]
    assert twin and all(n.status == "potlacena" for n in twin)


def test_eskalovat_ma_stabilny_kluc(cfg, fixed_now):
    """Čas „najmenej zlej“ možnosti pri ESKALOVAŤ sa posúva s časom – nové upozornenie z toho nevznikne."""
    from vlaky.notifikacie.odosielanie import notify_recommendation, recommendation_dedup_key

    set_fake_now(fixed_now)
    with session_scope() as s:
        t = _train(s)
        a = _rec(s, t.id, fixed_now + timedelta(hours=6), "ESKALOVAŤ", "ČERVENÁ", "KRITICKÉ")
        first = notify_recommendation(s, a)
        b = _rec(s, t.id, fixed_now + timedelta(hours=7, minutes=10), "ESKALOVAŤ", "ČERVENÁ", "KRITICKÉ")
        assert recommendation_dedup_key(a) == recommendation_dedup_key(b)
        second = notify_recommendation(s, b)
        # iné rozhodnutia ostávajú s časovým košom (posun odchodu o 30 min a viac je nová správa)
        c = _rec(s, t.id, fixed_now + timedelta(hours=2))
        d = _rec(s, t.id, fixed_now + timedelta(hours=3))
        assert recommendation_dedup_key(c) != recommendation_dedup_key(d)
    assert all(n.status == "outbox" for n in first)
    assert second and all(n.status == "potlacena" for n in second)


def test_navrat_po_info_v_suhrne_sa_posle(cfg, fixed_now):
    """A (DÔLEŽITÉ) → B (INFO, dispečer ho dostal v súhrne) → A: návrat sa pošle. Bez súhrnu dispečer stále
    drží A, takže A sa nepošle znova."""
    from vlaky.notifikacie.odosielanie import notify_recommendation, send_digest

    set_fake_now(fixed_now)
    with session_scope() as s:
        t1, t2 = _train(s, "95001"), _train(s, "95002")
        dep = fixed_now + timedelta(hours=5)
        out = {}
        for t in (t1, t2):
            out[t.number] = [notify_recommendation(s, _rec(s, t.id, dep, color="ORANŽOVÁ"))]
        _rec(s, t1.id, dep, color="ZELENÁ", priority="INFO")
        set_fake_now(fixed_now + timedelta(minutes=30))
        digest = send_digest(s, fixed_now - timedelta(minutes=1))
        assert digest and all(n.status == "outbox" for n in digest)
        set_fake_now(fixed_now + timedelta(minutes=40))
        # vlak 95001: INFO bolo v súhrne → návrat k ORANŽOVEJ sa pošle
        back1 = notify_recommendation(s, _rec(s, t1.id, dep, color="ORANŽOVÁ"))
        # vlak 95002: INFO vzniklo až po súhrne → dispečer drží ORANŽOVÚ, nič nové
        _rec(s, t2.id, dep, color="ZELENÁ", priority="INFO")
        back2 = notify_recommendation(s, _rec(s, t2.id, dep, color="ORANŽOVÁ"))
    assert all(n.status == "outbox" for n in out["95001"][0] + out["95002"][0])
    assert back1 and all(n.status == "outbox" for n in back1), [n.status for n in back1]
    assert back2 and all(n.status == "potlacena" for n in back2), [n.status for n in back2]


# ------------------------------------------------------------------ [22] čiastočné zlyhanie
def test_po_ciastocnom_zlyhani_sa_opakuje_len_neuspesnym(cfg, fixed_now, monkeypatch):
    """dispecer2 odmietnutý (550), Teams vracia 404: dispecer1 dostane odporúčanie raz, aj po 4 h; neúspešným
    sa opakuje s odstupom 5, 10, 20, 40, 60 min (nie každých 5 min)."""
    relay = Relay()
    relay.reject = {D2}
    _setup(cfg, monkeypatch, relay, teams_webhook_url="https://teams.example/workflows/x")
    teams = []

    def dead_teams(url, *a, **k):
        teams.append(url)
        return httpx.Response(404, request=httpx.Request("POST", url), text="Not Found")

    monkeypatch.setattr(odosielanie.httpx, "post", dead_teams)
    with session_scope() as s:
        t = _train(s)
        _rec(s, t.id, fixed_now + timedelta(hours=8), created_at=fixed_now)
    _cycles(fixed_now, 4 * 60, step=5)
    d1 = [x for x in relay.sent if x[0] == D1 and "95001" in x[1]]
    assert len(d1) == 1, d1
    with session_scope() as s:
        tries_d2 = len([n for n in _rows(s, kind="odporucanie", recipient=D2)])
        tries_teams = len([n for n in _rows(s, kind="odporucanie", channel="teams")])
    assert 5 <= tries_d2 <= 8 and 5 <= tries_teams <= 8, (tries_d2, tries_teams)
    assert len(teams) == tries_teams


# ------------------------------------------------------------------ [23] istič, odstup, časový limit
def test_nedostupny_server_nezdrzi_cyklus(cfg, fixed_now, monkeypatch):
    """Server ticho zahadzuje spojenia: v cykle len jeden pokus (10 s), ostatné správy bez pripájania; ďalšie
    pokusy s odstupom – cyklus (čítanie schránky) beží ďalej každú minútu."""
    relay = Relay("blackhole")
    _setup(cfg, monkeypatch, relay)
    with session_scope() as s:
        for i in range(4):
            t = _train(s, f"9600{i}")
            _rec(s, t.id, fixed_now + timedelta(hours=8), created_at=fixed_now)
    set_fake_now(fixed_now)
    beh.run_cycle(fixed_now)
    assert relay.connects == [10]  # krátky limit spojenia (predvolených 10 s – CONNECT_TIMEOUT_S)
    assert (now_utc() - fixed_now).total_seconds() <= 15  # cyklus trval ~10 s, nie 8 × 30 s
    with session_scope() as s:
        rows = _rows(s, kind="odporucanie")
        assert len(rows) == 8 and all(n.status == "chyba" for n in rows)
        assert sum("Neodoslané" in n.error for n in rows) == 7
    assert len(os.listdir(cfg.outbox_dir)) == 1  # kópia len skutočného pokusu
    # hodina výpadku: cykly každú minútu, pokusy len po odstupe (5, 15, 35 min) – vždy len jeden
    starts = []
    t = fixed_now + timedelta(minutes=1)
    while t < fixed_now + timedelta(minutes=60):
        set_fake_now(t)
        starts.append(t)
        beh.run_cycle(t)
        t = max(now_utc(), t) + timedelta(seconds=60)
    assert len(relay.connects) <= 4, relay.connects
    gaps = [(b - a).total_seconds() for a, b in zip(starts, starts[1:])]
    assert max(gaps) <= 75
    # aj jedno odoslanie z webu (bez cyklu agenta) sa po zlyhaní spojenia ďalej nepripája
    relay.mode, relay.connects = "down", []
    with session_scope() as s:
        out = odosielanie.send_internal(s, "upozornenie", "[AGENT][DÔLEŽITÉ] Test", "telo", "DÔLEŽITÉ",
                                        dedup_key="web:test", channels=["email"])
    assert len(out) == 2 and all(n.status == "chyba" for n in out) and len(relay.connects) == 1


def test_suhrn_a_denna_sprava_sa_opakuju_s_odstupom(cfg, fixed_now, monkeypatch):
    relay = Relay("down")
    _setup(cfg, monkeypatch, relay, daily_report_hour=5, digest_minutes=1)
    from vlaky.agent.zdravie import set_kv

    with session_scope() as s:
        t = _train(s)
        _rec(s, t.id, fixed_now + timedelta(hours=8), priority="INFO")  # obsah súhrnu
        set_kv(s, "digest", {"ts": (fixed_now - timedelta(hours=3)).isoformat()})
    _cycles(fixed_now, 60)
    with session_scope() as s:
        daily = _rows(s, kind="zdravie", recipient=D1)
        digest = _rows(s, kind="suhrn", recipient=D1)
    assert 2 <= len(daily) <= 4 and 2 <= len(digest) <= 4, (len(daily), len(digest))
    relay.mode = "ok"
    _cycles(fixed_now + timedelta(minutes=60), 30)
    assert any("Denná správa" in x[1] for x in relay.sent) and any("Súhrn" in x[1] for x in relay.sent)


def test_retencia_zmaze_stare_spravy_a_kopie(cfg, fixed_now):
    """Správy agenta (aj zopakované pokusy) sa mažú po rovnakej dobe ako obsah e-mailov (VLAKY_RETENTION_EMAIL_DAYS)."""
    from vlaky.agent.strazenie import retention_purge

    assert cfg.retention_email_days == 365
    with session_scope() as s:
        for days in (500, 400, 1):
            s.add(Notification(channel="email", recipient=D1, subject="x", body="y", kind="upozornenie",
                               status="chyba", created_at=fixed_now - timedelta(days=days)))
    os.makedirs(cfg.outbox_dir, exist_ok=True)
    for days in (500, 400, 1):
        name = (fixed_now - timedelta(days=days)).strftime("%Y%m%dT%H%M%S") + f"_email_{days}.txt"
        (cfg.outbox_dir / name).write_text("x", encoding="utf-8")
    (cfg.outbox_dir / "poznamka.md").write_text("nie je kópia správy", encoding="utf-8")
    with session_scope() as s:
        out = retention_purge(s, fixed_now)
    assert out.get("notifications_deleted") == 2 and out.get("outbox_files_deleted") == 2
    with session_scope() as s:
        assert s.scalar(select(func.count(Notification.id))) == 1
    assert sorted(os.listdir(cfg.outbox_dir))[0].startswith((fixed_now - timedelta(days=1)).strftime("%Y%m%d"))
    assert "poznamka.md" in os.listdir(cfg.outbox_dir)


# ------------------------------------------------------------------ [21] monitoring trvalého výpadku
def test_trvaly_vypadok_dorucovania_vidi_heartbeat_aj_zdravie(cfg, fixed_now, monkeypatch):
    from vlaky.agent.zdravie import health_status, set_kv

    relay = Relay("down")
    _setup(cfg, monkeypatch, relay, heartbeat_url="https://hc.example/ping/x", heartbeat_minutes=5)
    pings = []
    monkeypatch.setattr(beh, "ping_heartbeat", lambda ok, msg="": pings.append((ok, msg)) or True)
    with session_scope() as s:
        t = _train(s)
        _rec(s, t.id, fixed_now + timedelta(hours=8), created_at=fixed_now)
        set_kv(s, "heartbeat", {"ts": (fixed_now - timedelta(minutes=2)).isoformat()})  # iná fáza ako pokusy
    _cycles(fixed_now, 40)
    assert len(pings) >= 7 and not any(ok for ok, _msg in pings), [p[0] for p in pings]
    assert all("dopravca.example" not in msg for _ok, msg in pings)  # adresy nejdú na externý monitoring
    ok, detail = health_status()
    assert not ok and detail["dorucenie"].startswith("2 nedoručených") and detail["stav"] == "PROBLÉM"
    # server opäť funguje: správa sa doručí (každému raz) a monitoring je znova v poriadku
    relay.mode = "ok"
    pings.clear()
    _cycles(fixed_now + timedelta(minutes=40), 40)
    assert sorted(x[0] for x in relay.sent) == [D1, D2]
    assert pings and pings[-1][0] is True
    ok, detail = health_status()
    assert ok and detail["dorucenie"] == "ok"


def test_nahradene_odporucanie_nehlasi_vypadok(cfg, fixed_now, monkeypatch):
    """Nedoručené odporúčanie je výpadok doručovania (zdravie PROBLÉM); keď ho nahradí nové a to sa doručí,
    zdravie je znova OK (nahradené sa už neposiela ani nehlási)."""
    from vlaky.agent.zdravie import health_status

    relay = Relay("down")
    _setup(cfg, monkeypatch, relay)
    with session_scope() as s:
        t = _train(s)
        _rec(s, t.id, fixed_now + timedelta(hours=8), created_at=fixed_now)
    _cycles(fixed_now, 2)
    ok, detail = health_status()
    assert not ok and detail.get("dorucenie", "ok") != "ok", detail
    relay.mode = "ok"
    with session_scope() as s:
        _rec(s, t.id, fixed_now + timedelta(hours=10), decision="POČKAŤ", created_at=fixed_now + timedelta(minutes=2))
    _cycles(fixed_now + timedelta(minutes=2), 2)
    ok, detail = health_status()
    assert ok and detail["dorucenie"] == "ok", detail
    assert len(relay.sent) == 2  # nahradené odporúčanie sa už neposiela


# ------------------------------------------------------------------ [24] výpadok dlhší ako 6 h
def test_aktualne_odporucanie_sa_dorucuje_aj_po_dlhom_vypadku(cfg, fixed_now, monkeypatch):
    relay = Relay("down")
    _setup(cfg, monkeypatch, relay)
    with session_scope() as s:
        t = _train(s)
        _rec(s, t.id, fixed_now + timedelta(hours=20), created_at=fixed_now)
        done = _train(s, "95009", status="ukonceny")
        _rec(s, done.id, fixed_now + timedelta(hours=1), created_at=fixed_now)
    _cycles(fixed_now, 7 * 60, step=10)
    relay.mode = "ok"
    _cycles(fixed_now + timedelta(hours=7), 65, step=5)
    got = sorted(x[0] for x in relay.sent if "95001" in x[1])
    assert got == [D1, D2], got  # každému práve raz
    assert not [x for x in relay.sent if "95009" in x[1]]  # ukončený vlak sa neopakuje
    with session_scope() as s:
        done_tries = len([n for n in _rows(s, kind="odporucanie") if "95009" in n.subject])
    assert done_tries == 0


# ------------------------------------------------------------------ [25] odovzdávka služby
def test_nedorucena_odovzdavka_sa_zopakuje(cfg, monkeypatch):
    relay = Relay("down")
    _setup(cfg, monkeypatch, relay, shift_change_hours="6")
    t0 = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)  # 06:00 miestneho času
    _cycles(t0, 20, before=lambda k: setattr(relay, "mode", "down" if k < 5 else "ok"))
    assert [x[0] for x in relay.sent if "Odovzdávka" in x[1]] == [D1, D2]
    with session_scope() as s:
        assert s.scalar(select(func.count(ShiftHandoverReport.id))) == 1  # tá istá správa, žiadna nová
        keys = {n.dedup_key for n in _rows(s, kind="odovzdavka")}
    assert keys == {"odovzdavka:1"}


# ------------------------------------------------------------------ [26] objednávky
def test_chybna_objednavka_nezastavi_ostatne(cfg, fixed_now, monkeypatch):
    from vlaky.planovanie import planovac

    _setup(cfg, monkeypatch)
    with session_scope() as s:
        seed_borders(s)
        for cust in ("A (chybná)", "B", "C"):
            s.add(Order(customer=cust, origin="Kúty", destination="Štúrovo",
                        earliest_departure_utc=fixed_now + timedelta(hours=8),
                        deadline_utc=fixed_now + timedelta(hours=30)))
    real = planovac.plan_order

    def flaky(session, order_id, now=None):
        if order_id == 1:
            raise ValueError("neočakávané údaje v objednávke")
        return real(session, order_id, now)

    monkeypatch.setattr(planovac, "plan_order", flaky)
    errs = [[e for e in out["errors"] if e.startswith("objednávky")] for _k, out in _cycles(fixed_now, 4)]
    assert all(errs), errs  # chyba je vidieť v každom cykle, kým trvá (heartbeat /fail)
    with session_scope() as s:
        st = dict(s.execute(select(Order.customer, Order.status)).all())
        assert st == {"A (chybná)": "nova", "B": "navrhnuta", "C": "navrhnuta"}
        assert s.scalar(select(AgentHealth).where(AgentHealth.component == "objednavky",
                                                  AgentHealth.status == "error")) is not None
    # po 3 neúspešných pokusoch (s odstupom) dostanú dispečeri upozornenie a objednávka „chyba plánu“
    _cycles(fixed_now + timedelta(minutes=4), 20)
    with session_scope() as s:
        assert s.get(Order, 1).status == "chyba_planu"
        notes = [n for n in _rows(s, kind="objednavka") if n.dedup_key == "order:1:chyba"]
        assert notes and all(n.status == "outbox" for n in notes)
        assert "nepodarilo pripraviť" in notes[0].subject
    last = beh.run_cycle(fixed_now + timedelta(minutes=30))
    assert not [e for e in last["errors"] if e.startswith("objednávky")]


# ------------------------------------------------------------------ [46] a neznámy nástup rušňovodiča
def test_odovzdavka_preberania_cislom_vlaku(cfg, fixed_now):
    from vlaky.agent.odovzdavka import build_report

    with session_scope() as s:
        seed_borders(s)
        for i in range(3):
            _train(s, f"1000{i}")
        t = _train(s, "47250", status="na_ceste_k_hranici")
        border = s.scalar(select(BorderCrossing).where(BorderCrossing.handover_station != "")
                          .order_by(BorderCrossing.id))
        s.add(Handover(train_id=t.id, border_id=border.id, avizo_eta_utc=fixed_now + timedelta(hours=3)))
        train_id, station = t.id, border.handover_station
    with session_scope() as s:
        text, _data = build_report(s, fixed_now)
    part = text.split("Očakávané preberania na hraniciach:")[1].split("\n\n")[0]
    assert f"vlak 47250: {station} avízo" in part
    assert f"vlak {train_id}:" not in part


def test_odovzdavka_neznamy_nastup_rusnovodica_len_ako_odhad(cfg, fixed_now):
    from vlaky.agent.odovzdavka import build_report

    with session_scope() as s:
        relieve = (fixed_now + timedelta(hours=2)).isoformat()
        est = _train(s, "47251", status="v_jazde")
        _rec(s, est.id, fixed_now, decision="POKRAČOVAŤ", color="ORANŽOVÁ",
             crew={"driver": "neznámy (chýba záznam o nástupe)", "unknown_start": True, "estimated": True,
                   "status": "ORANŽOVÁ", "max_duty_h": 13.0, "relieve_by": relieve})
        unknown = _train(s, "47252", status="v_jazde")
        _rec(s, unknown.id, fixed_now, decision="POKRAČOVAŤ", color="ORANŽOVÁ",
             crew={"driver": "neznámy (chýba záznam o nástupe)", "unknown_start": True, "estimated": True,
                   "status": "ČERVENÁ", "max_duty_h": 13.0, "relieve_by": None, "limit": None, "start": None})
        known = _train(s, "47253", status="v_jazde")
        _rec(s, known.id, fixed_now, decision="POKRAČOVAŤ", color="ORANŽOVÁ",
             crew={"driver": "Ján Fiktívny", "status": "ORANŽOVÁ", "max_duty_h": 13.0, "relieve_by": relieve})
    with session_scope() as s:
        text, _data = build_report(s, fixed_now)  # relieve_by = None nesmie zhodiť odovzdávku
    crew_part = text.split("Rušňovodiči blízko limitu 13 h:")[1].split("\n\n")[0]
    assert "vlak 47251: odhad: vystriedať najneskôr o 02.11. 07:00 (nástup rušňovodiča nie je zadaný)" in crew_part
    assert "vlak 47252: limit 13 h sa nedá overiť – zadajte nástup rušňovodiča" in crew_part
    assert "vlak 47253: vystriedať najneskôr o 02.11. 07:00" in crew_part
    assert "47253: odhad" not in crew_part
