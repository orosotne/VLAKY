"""Regresné testy k nálezom druhej revízie webu: odkaz rušňovodiča, prihlásenie, staré dáta na mape.

[31]/[42] prepočet po zmene stavu z odkazu sa nezahodí, [32] odmietnuté požiadavky nezahltia audit ani históriu
vlaku, [33] súčasné overenia hesla a IPv6 /64, [34] hlásenie o probléme má vlastný limit, [35]/[38] audit
zablokovaného prihlásenia podľa kľúča, ktorý blokuje, [36] poloha s časom v budúcnosti, [44] bez čerstvých dát
agenta web neukáže ZELENÚ.
"""
from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
from sqlalchemy import func, select

from test_web_spolocne import HESLO, login, login_as, make_recommendation, make_train, make_user

pytest_plugins = ["test_web_spolocne"]

ZILINA = (49.2265, 18.7395)
KOSICE = (48.7206, 21.2581)


def _driver(**train_kw):
    from vlaky.db import session_scope
    from vlaky.models import Train
    from vlaky.web.rusnovodic import make_driver_token

    train_id = make_train(**train_kw)
    with session_scope() as s:
        return train_id, make_driver_token(s.get(Train, train_id))


def _audit_counts(train_id: int) -> dict:
    from vlaky.db import session_scope
    from vlaky.models import AuditLog

    with session_scope() as s:
        return dict(s.execute(select(AuditLog.action, func.count(AuditLog.id))
                              .where(AuditLog.entity == "train", AuditLog.entity_id == str(train_id))
                              .group_by(AuditLog.action)).all())


def _latest_rec(train_id: int):
    from vlaky.db import session_scope
    from vlaky.models import Recommendation

    with session_scope() as s:
        r = s.scalar(select(Recommendation).where(Recommendation.train_id == train_id)
                     .order_by(Recommendation.id.desc()))
        return (r.decision, r.color, r.trigger) if r is not None else None


# ------------------------------------------------------------------ [31]/[42] prepočet po hlásení
def test_odchod_tesne_po_pripravenosti_sa_prepocita(cfg, fixed_now, monkeypatch):
    """Pripravený a o 20 s odchod: prepočíta sa aj odchod – nie až pravidelný prepočet agenta o 15 min."""
    from fastapi.testclient import TestClient

    import vlaky.web.rusnovodic as rus
    from vlaky.cas import set_fake_now
    from vlaky.web.app import create_app

    train_id, token = _driver(number="70003", status="planovany", origin="Čadca", destination="Košice",
                              ready_utc=fixed_now + timedelta(hours=2), deadline_utc=fixed_now + timedelta(hours=30))
    calls = []
    real = rus.evaluate_train_bg
    monkeypatch.setattr(rus, "evaluate_train_bg", lambda tid, trig: (calls.append(trig), real(tid, trig)))
    with TestClient(create_app(auto_evaluate=True)) as c:
        assert c.post("/api/hlasenie", json={"t": token, "typ": "pripraveny"}).status_code == 200
        set_fake_now(fixed_now + timedelta(seconds=20))
        assert c.post("/api/hlasenie", json={"t": token, "typ": "odchod"}).status_code == 200
    assert calls == ["hlásenie rušňovodiča: Vlak pripravený", "hlásenie rušňovodiča: Odchod"]
    decision, _color, trigger = _latest_rec(train_id)
    assert decision == "POKRAČOVAŤ" and trigger == "hlásenie rušňovodiča: Odchod"  # odporúčanie pre idúci vlak


def test_problem_sa_prepocita_najviac_raz_za_30_s_zmena_stavu_vzdy(cfg, fixed_now, monkeypatch):
    from fastapi.testclient import TestClient

    import vlaky.web.rusnovodic as rus
    from vlaky.web.app import create_app

    _train_id, token = _driver(status="planovany")
    calls = []
    monkeypatch.setattr(rus, "evaluate_train_bg", lambda tid, trig: calls.append(trig))
    with TestClient(create_app(auto_evaluate=True)) as c:
        for typ, text in (("nastup", ""), ("problem", "Porucha brzdy (FIKTÍVNE)"), ("pripraveny", ""),
                          ("problem", "Stále stojím"), ("odchod", "")):
            assert c.post("/api/hlasenie", json={"t": token, "typ": typ, "text": text}).status_code == 200, typ
    assert calls == ["hlásenie rušňovodiča: Nástup na zmenu", "hlásenie rušňovodiča: Vlak pripravený",
                     "hlásenie rušňovodiča: Odchod"]


def test_prepocty_jedneho_vlaku_po_hlaseniach_idu_za_sebou(monkeypatch):
    """Starší prepočet (po nástupe) nesmie bežať súčasne s novším (po pripravenosti) a prepísať ho."""
    import vlaky.web.rusnovodic as rus

    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def slow(tid, trig):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.15)
        with lock:
            state["active"] -= 1

    monkeypatch.setattr(rus, "evaluate_train_bg", slow)
    throttle = rus._TrainThrottle()
    threads = [threading.Thread(target=rus._evaluate_after_report, args=(throttle, 7, f"t{i}")) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] == 1


# ------------------------------------------------------------------ [34] vlastný limit pre problém
def test_odmietnute_hlasenia_stavu_neminu_limit_pre_problem(client, fixed_now):
    from vlaky.db import session_scope
    from vlaky.models import Driver, DriverShift, Notification

    train_id, token = _driver(status="planovany", ready_utc=fixed_now)
    with session_scope() as s:
        d = Driver(name="R. FIKTÍVNY")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=train_id, start_utc=fixed_now - timedelta(hours=1),
                          status="prebieha"))
    codes = [client.post("/api/hlasenie", json={"t": token, "typ": typ}).status_code
             for typ in ("nastup", "odchod", "odchod", "pripraveny", "odchod", "nastup")]
    assert codes == [409, 200, 409, 409, 409, 409]
    r = client.post("/api/hlasenie", json={"t": token, "typ": "problem", "text": "Porucha brzdy, stojím (FIKTÍVNE)"})
    assert r.status_code == 200
    with session_scope() as s:
        assert s.scalar(select(func.count(Notification.id)).where(Notification.subject.contains("problém"))) >= 1
    # hlásenia stavu majú stále svoj limit (6 za 10 min) – ďalšie odmietnuté
    assert client.post("/api/hlasenie", json={"t": token, "typ": "odchod"}).status_code == 429


def test_aplikacia_rusnovodica_409_ako_informacia_a_preskocena_poloha():
    from vlaky.web.common import STATIC_DIR

    js = (STATIC_DIR / "js" / "rusnovodic.js").read_text(encoding="utf-8")
    i409 = js.index("err.status === 409")
    assert "Už zaznamenané: " in js[i409:i409 + 200] and "Skúste znova" not in js[i409:i409 + 120]
    assert "data.skipped" in js and js.index("data.skipped") < js.index("'Poloha odoslaná o '")


# ------------------------------------------------------------------ [32] odmietnuté požiadavky a audit
def test_odmietnute_poziadavky_z_odkazu_nezahltia_audit(client, fixed_now):
    train_id, token = _driver(status="v_jazde")
    codes = [client.post("/api/hlasenie", json={"t": token, "typ": "problem", "text": f"problém {i}"}).status_code
             for i in range(60)]
    assert codes.count(200) == 6 and codes.count(429) == 54
    first = client.post("/api/pozicie/rusnovodic", json={"t": token, "lat": ZILINA[0], "lon": ZILINA[1],
                                                         "ts": (fixed_now - timedelta(seconds=60)).isoformat()})
    assert first.status_code == 200 and not first.json().get("skipped")
    jumps = [client.post("/api/pozicie/rusnovodic",
                         json={"t": token, "lat": KOSICE[0], "lon": KOSICE[1],
                               "ts": (fixed_now - timedelta(seconds=30 - i * 0.01)).isoformat()}).status_code
             for i in range(50)]
    assert jumps == [422] * 50
    counts = _audit_counts(train_id)
    assert counts.get("hlasenie_obmedzene") == 1 and counts.get("poloha_odmietnuta") == 1, counts
    assert counts.get("hlasenie_problem") == 6

    from vlaky.db import session_scope
    from vlaky.models import AuditLog

    with session_scope() as s:
        row = s.scalar(select(AuditLog).where(AuditLog.action == "hlasenie_obmedzene"))
        assert row.detail["typ"] == "problem" and row.detail["text"] == "problém 6"  # text prvého odmietnutého


def test_audit_odmietnuti_raz_za_okno_s_poctom_nezapisanych(monkeypatch):
    import vlaky.web.rusnovodic as rus

    clock = [1000.0]
    monkeypatch.setattr(rus, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    th = rus._TrainThrottle()
    assert th.audit_gate(1, "poloha_odmietnuta") == 0
    assert [th.audit_gate(1, "poloha_odmietnuta") for _ in range(30)] == [None] * 30
    assert th.audit_gate(2, "poloha_odmietnuta") == 0          # iný vlak má vlastné okno
    assert th.audit_gate(1, "hlasenie_obmedzene:stav") == 0    # iný druh odmietnutia tiež
    clock[0] += rus.AUDIT_WINDOW_S + 1
    assert th.audit_gate(1, "poloha_odmietnuta") == 30         # nový záznam s počtom nezapísaných


def test_problem_rusnovodica_ostane_v_historii_vlaku(client, fixed_now):
    from vlaky.audit import audit
    from vlaky.db import session_scope

    train_id, token = _driver(status="v_jazde")
    r = client.post("/api/hlasenie", json={"t": token, "typ": "problem",
                                           "text": "Horúce ložisko vozňa 7, stojím v Kraľovanoch (FIKTÍVNE)"})
    assert r.status_code == 200
    with session_scope() as s:
        for i in range(40):  # neskoršie záznamy k vlaku (úpravy dispečera)
            audit(s, "dispecer", "train_update", "train", train_id, changes={"notes": [str(i), str(i + 1)]})
    for i in range(20):  # ďalšie hlásenia a nával odmietnutých
        client.post("/api/hlasenie", json={"t": token, "typ": "problem", "text": f"spam {i}"})
    login_as(client, "disp1", "dispecer")
    page = client.get(f"/vlaky/{train_id}")
    assert page.status_code == 200
    assert "Horúce ložisko vozňa 7" in page.text
    assert "hlasenie_obmedzene" in page.text  # posledné odmietnutie je v histórii (raz)
    assert page.text.count("train_update") == 30 - 5  # 30 riadkov: 5 novších hlásení o probléme + 25 úprav


# ------------------------------------------------------------------ [36] poloha s časom v budúcnosti
def test_poloha_s_casom_v_buducnosti_neblokuje_dalsie(client, fixed_now):
    from vlaky.cas import ensure_utc, set_fake_now
    from vlaky.db import session_scope
    from vlaky.models import PositionReport

    train_id, token = _driver(status="v_jazde")
    skewed = client.post("/api/pozicie/rusnovodic", json={"t": token, "lat": ZILINA[0], "lon": ZILINA[1],
                                                          "ts": (fixed_now + timedelta(minutes=9)).isoformat()})
    assert skewed.status_code == 200 and not skewed.json().get("skipped")
    res = []
    for i in range(1, 4):  # hodiny mobilu sa opravili – polohy každých 30 s
        now = fixed_now + timedelta(seconds=30 * i)
        set_fake_now(now)
        r = client.post("/api/pozicie/rusnovodic", json={"t": token, "lat": ZILINA[0] + 0.0005 * i,
                                                          "lon": ZILINA[1], "ts": now.isoformat()})
        res.append((r.status_code, bool(r.json().get("skipped"))))
    assert res == [(200, False)] * 3
    with session_scope() as s:
        times = [ensure_utc(p.ts_utc) for p in s.scalars(select(PositionReport).where(
            PositionReport.train_id == train_id).order_by(PositionReport.id))]
    assert times[0] == fixed_now  # uložená s časom prijatia, nie +9 min
    assert max(times) <= fixed_now + timedelta(seconds=90)


def test_poloha_z_brany_s_casom_v_buducnosti_neblokuje_mobil(client, fixed_now):
    """Poloha rušňovodiča z inej cesty (brána, staršie záznamy) s časom v budúcnosti sa na odstup nepoužije."""
    _train_id, token = _driver(number="50077", status="v_jazde")
    gw = client.post("/api/pozicie", json={"train_number": "50077", "lat": ZILINA[0], "lon": ZILINA[1],
                                           "ts": (fixed_now + timedelta(minutes=8)).isoformat(), "source": "driver"},
                     headers={"Authorization": "Bearer test-token"})
    assert gw.status_code == 200
    r = client.post("/api/pozicie/rusnovodic", json={"t": token, "lat": ZILINA[0] + 0.001, "lon": ZILINA[1],
                                                      "ts": fixed_now.isoformat()})
    assert r.status_code == 200 and not r.json().get("skipped")


# ------------------------------------------------------------------ [33] súčasné overenia hesla, IPv6 /64
def _login_form(i: int = 0, username: str = "") -> dict:
    return {"username": username or f"nikto{i}", "password": "zle-heslo-12345", "csrf_token": "t" * 32, "next": "/"}


async def _post_login(app, data: dict, client_addr=("127.0.0.1", 50000)):
    transport = httpx.ASGITransport(app=app, client=client_addr)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 cookies={"vlaky_login": "t" * 32}) as ac:
        return await ac.post("/login", data=data)


def _slow_check(state: dict, delay: float):
    lock = threading.Lock()

    def check(username, password, ip):
        with lock:
            state["active"] = state.get("active", 0) + 1
            state["peak"] = max(state.get("peak", 0), state["active"])
        time.sleep(delay)
        with lock:
            state["active"] -= 1
        return None

    return check


def test_overenie_hesla_najviac_tri_naraz(web_app, monkeypatch):
    """Nával prihlásení (rôzne mená) nespustí viac ako 3 bcrypt naraz – zvyšok čaká v slučke udalostí."""
    import vlaky.web.auth as auth

    state: dict = {}
    monkeypatch.setattr(auth, "_check_login", _slow_check(state, 0.15))

    async def flood():  # rôzne mená aj adresy – limit pokusov nezasiahne
        return await asyncio.gather(*[_post_login(web_app, _login_form(i), (f"198.51.100.{i + 1}", 50000))
                                      for i in range(8)])

    codes = [r.status_code for r in asyncio.run(flood())]
    assert state["peak"] <= 3, state
    assert codes == [401] * 8  # 3 vlny po 0,15 s sa zmestia do čakania 2 s


def test_subezne_pokusy_z_jednej_adresy_neobidu_limit(web_app, monkeypatch):
    """Pokusy čakajúce na voľné overenie hesla sa po získaní miesta znova skontrolujú voči limitu (5 na IP)."""
    import vlaky.web.auth as auth

    state: dict = {}
    monkeypatch.setattr(auth, "_check_login", _slow_check(state, 0.15))

    async def burst():
        return await asyncio.gather(*[_post_login(web_app, _login_form(i)) for i in range(12)])

    codes = [r.status_code for r in asyncio.run(burst())]
    assert codes.count(401) <= 5 + 3 - 1, codes  # najviac limit + súbežne bežiace overenia
    assert codes.count(401) + codes.count(429) == 12, codes


def test_zaneprazdneny_server_vrati_503_a_miesto_sa_uvolni(web_app, monkeypatch):
    import vlaky.web.auth as auth

    make_user("dispecer11")
    web_app.state.password_checks = auth.PasswordChecks(limit=1, wait_s=0.2)
    real = auth._check_login
    state: dict = {}

    async def scenario():
        monkeypatch.setattr(auth, "_check_login", _slow_check(state, 0.6))
        burst = await asyncio.gather(*[_post_login(web_app, _login_form(i)) for i in range(3)])
        monkeypatch.setattr(auth, "_check_login", real)
        ok = await _post_login(web_app, {"username": "dispecer11", "password": HESLO, "csrf_token": "t" * 32,
                                         "next": "/"})
        return burst, ok

    burst, ok = asyncio.run(scenario())
    codes = sorted(r.status_code for r in burst)
    assert codes == [401, 503, 503], codes
    busy = next(r for r in burst if r.status_code == 503)
    assert "Server je zaneprázdnený" in busy.text and busy.headers.get("retry-after")
    assert ok.status_code == 303  # semafor sa uvoľnil (aj po odmietnutí)


def test_ipv6_limit_prihlaseni_podla_siete_64(web_app, monkeypatch):
    import vlaky.web.auth as auth

    monkeypatch.setattr(auth, "_check_login", lambda username, password, ip: None)

    async def scenario():
        out = []
        for i in range(1, 6):  # 5 neúspechov z rôznych adries jednej /64
            out.append((await _post_login(web_app, _login_form(i), (f"2001:db8:1:2::{i:x}", 1))).status_code)
        out.append((await _post_login(web_app, _login_form(9), ("2001:db8:1:2:ffff::99", 1))).status_code)
        out.append((await _post_login(web_app, _login_form(9), ("2001:db8:1:3::1", 1))).status_code)
        return out

    assert asyncio.run(scenario()) == [401] * 5 + [429, 401]
    assert auth.limiter_ip("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == "2001:db8:1:2::/64"
    assert auth.limiter_ip("::ffff:192.0.2.7") == "192.0.2.7"
    assert auth.limiter_ip("192.0.2.7") == "192.0.2.7" and auth.limiter_ip("testclient") == "testclient"


# ------------------------------------------------------------------ [35]/[38] audit zablokovaného prihlásenia
def test_zablokovany_ucet_jeden_zaznam_v_audite_aj_z_roznych_adries(client, web_app, monkeypatch):
    import vlaky.web.auth as auth
    from vlaky.db import session_scope
    from vlaky.models import AuditLog

    make_user("veduci9", "veduci")
    for _ in range(10):
        web_app.state.account_limiter.fail("u:veduci9")
    ips = iter(f"198.51.100.{i}" for i in range(1, 250))
    monkeypatch.setattr(auth, "client_ip", lambda request: next(ips))
    assert [login(client, "veduci9").status_code for _ in range(20)] == [429] * 20
    with session_scope() as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action == "login_blocked")))
    assert len(rows) == 1 and rows[0].detail.get("blokovane") == "ucet"


def test_zapis_zablokovaneho_prihlasenia_mimo_slucky_udalosti(client, web_app, monkeypatch):
    import vlaky.web.auth as auth

    on_loop: list = []
    real_scope = auth.session_scope

    @contextmanager
    def scope(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        with real_scope(*args, **kwargs) as s:
            yield s

    make_user("dispecer9")
    for _ in range(10):
        web_app.state.account_limiter.fail("u:dispecer9")
    monkeypatch.setattr(auth, "session_scope", scope)
    assert login(client, "dispecer9").status_code == 429
    assert on_loop and not any(on_loop)  # zápis do auditu v threadpoole, nie v slučke udalostí


def test_zaznamy_zablokovanych_pokusov_maju_obmedzenu_pamat():
    from vlaky.web.auth import LoginLimiter

    lim = LoginLimiter(max_failures=5, window_s=900, max_keys=1000)
    t0 = time.perf_counter()
    assert all(lim.should_log(f"ip:k{i}") for i in range(6000))
    assert time.perf_counter() - t0 < 5  # bez prestavby celého slovníka pri každom volaní
    assert len(lim._logged) <= 1000  # živé záznamy (v okne) sa nehromadia nad max_keys
    assert lim.should_log("ip:k5999") is False  # najnovšie záznamy sa stále deduplikujú


# ------------------------------------------------------------------ [44] bez čerstvých dát agenta nie ZELENÁ
def _set_agent_ages(now: datetime, mailbox_min, cycle_min) -> None:
    from vlaky.agent.zdravie import set_kv
    from vlaky.db import session_scope
    from vlaky.models import KeyValue

    with session_scope() as s:
        for key, age in (("mail_last_ok", mailbox_min), ("agent_last_cycle", cycle_min)):
            if age is None:
                kv = s.scalar(select(KeyValue).where(KeyValue.key == key))
                if kv is not None:
                    s.delete(kv)
            else:
                set_kv(s, key, {"ts": (now - timedelta(minutes=age)).isoformat()})


def _rows(client) -> dict:
    return {t["number"]: t for t in client.get("/api/vlaky").json()["vlaky"]}


def test_bez_cerstvych_dat_agenta_web_neukaze_zelenu(client, fixed_now):
    from vlaky.db import session_scope
    from vlaky.models import PositionReport

    green = make_train(number="44100")
    make_recommendation(green, color="ZELENÁ", decision="POKRAČOVAŤ")
    red = make_train(number="44101")
    make_recommendation(red, color="ČERVENÁ", decision="ESKALOVAŤ", priority="KRITICKÉ")
    with session_scope() as s:
        s.add(PositionReport(train_id=green, train_number="44100", ts_utc=fixed_now - timedelta(minutes=1),
                             lat=ZILINA[0], lon=ZILINA[1], source="gps"))
    login_as(client, "d44")
    _set_agent_ages(fixed_now, 1, 1)  # čerstvé dáta: ZELENÁ ostáva
    row = _rows(client)["44100"]
    assert row["color"] == "ZELENÁ" and row["display_color"] == "ZELENÁ" and not row.get("downgraded")

    _set_agent_ages(fixed_now, 120, 120)  # agent stojí 2 h (GPS stále prichádza)
    rows = _rows(client)
    row = rows["44100"]
    assert row["color"] == "ORANŽOVÁ" and row["display_color"] == "ORANŽOVÁ" and row["downgraded"] is True
    assert row["recommendation"]["color"] == "ZELENÁ"  # samotné odporúčanie sa nemení
    assert row["data_age"] == {"mailbox_min": 120.0, "agent_cycle_min": 120.0, "stale": True}
    assert "120 min" in row["data_note"] and "ZELENÁ" in row["data_note"]
    assert rows["44101"]["color"] == "ČERVENÁ" and not rows["44101"]["downgraded"]
    assert rows["44101"]["data_note"] == ""  # ČERVENÁ/ORANŽOVÁ sa nemení, bez ďalšej poznámky
    assert client.get(f"/api/vlaky/{green}").json()["color"] == "ORANŽOVÁ"
    page = client.get("/").text
    assert "bez čerstvých údajov agenta" in page

    _set_agent_ages(fixed_now, 5, 31)  # len cyklus agenta je starý (> 2 × 15 min)
    assert _rows(client)["44100"]["color"] == "ORANŽOVÁ"
    _set_agent_ages(fixed_now, None, 1)  # schránka ešte nebola skontrolovaná
    row = _rows(client)["44100"]
    assert row["color"] == "ORANŽOVÁ" and row["data_age"]["mailbox_min"] is None
    _set_agent_ages(fixed_now, 29, 29)
    row = _rows(client)["44100"]
    assert row["color"] == "ZELENÁ" and not row["downgraded"]  # v limite – bez falošného poplachu
    assert row["data_age"] == {"mailbox_min": 29.0, "agent_cycle_min": 29.0, "stale": False}


def test_sablona_zoznamu_vlakov_s_vekom_dat_neukaze_zelenu(cfg, fixed_now):
    """Len šablóna vlaky.html (kontext zostavený v teste): keď jej pohľad odovzdá vek dát (fresh), ZELENÁ sa zobrazí
    ako ORANŽOVÁ s vysvetlením. Pohľad trains_page vek dát zatiaľ neodovzdáva – skutočnú stránku /vlaky overuje
    test_revizia2_web_doplnok.py (očakávané zlyhanie do úpravy pohľadu)."""
    from starlette.requests import Request

    from vlaky.db import session_scope
    from vlaky.models import Recommendation, Train
    from vlaky.web.auth import CurrentUser
    from vlaky.web.common import render
    from vlaky.web.data import data_freshness

    train_id = make_train(number="44200")
    make_recommendation(train_id, color="ZELENÁ", decision="VYPRAVIŤ")
    _set_agent_ages(fixed_now, 120, 120)
    request = Request({"type": "http", "method": "GET", "path": "/vlaky", "headers": [], "query_string": b"",
                       "server": ("test", 80), "scheme": "http", "root_path": ""})
    user = CurrentUser(1, "d", "D", "dispecer", "x")
    with session_scope() as s:
        trains = list(s.scalars(select(Train)))
        recs = {r.train_id: r for r in s.scalars(select(Recommendation))}
        stale = render(request, "vlaky.html", {"trains": trains, "recs": recs, "filter": "aktivne", "q": "",
                                               "fresh": data_freshness(s)}, user=user).body.decode()
        _set_agent_ages(fixed_now, 1, 1)
        ok = render(request, "vlaky.html", {"trains": trains, "recs": recs, "filter": "aktivne", "q": "",
                                            "fresh": data_freshness(s)}, user=user).body.decode()
    assert "semafor-oranzova" in stale and "semafor-zelena" not in stale and "bez čerstvých údajov agenta" in stale
    assert "semafor-zelena" in ok and "bez čerstvých údajov agenta" not in ok


def test_zelena_po_zastaveni_agenta_cez_skutocny_cyklus(cfg):
    from fastapi.testclient import TestClient

    from vlaky.agent.beh import run_cycle
    from vlaky.agent.zdravie import set_kv
    from vlaky.cas import set_fake_now
    from vlaky.db import session_scope
    from vlaky.models import Path, Train
    from vlaky.web.app import create_app

    t0 = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)
    set_fake_now(t0)
    make_user("disp44")
    with session_scope() as s:
        t = Train(number="77005", origin="Plaveč", destination="Košice", traction="M", gauge="1435", vmax_kmh=80,
                  status="planovany", ready_utc=t0 + timedelta(hours=3), deadline_utc=t0 + timedelta(hours=12))
        s.add(t)
        s.flush()
        s.add(Path(train_id=t.id, train_number="77005", kind="ad_hoc", status="pridelena",
                   departure_utc=t0 + timedelta(hours=3)))
        set_kv(s, "mail_last_ok", {"ts": t0.isoformat(), "fetched": 0})
    run_cycle(t0)  # agent zapíše cyklus aj kontrolu schránky
    with TestClient(create_app(auto_evaluate=False)) as c:
        assert login(c, "disp44").status_code == 303
        assert _rows(c)["77005"]["color"] == "ZELENÁ"
        set_fake_now(t0 + timedelta(hours=2))  # agent prestal bežať
        row = _rows(c)["77005"]
    assert row["recommendation"]["color"] == "ZELENÁ"
    assert row["color"] == "ORANŽOVÁ" and row["display_color"] != "ZELENÁ" and row["downgraded"] is True
    assert row["data_age"]["agent_cycle_min"] == 120.0 and row["data_age"]["mailbox_min"] == 120.0
