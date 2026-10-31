"""Regresné testy k nálezom bezpečnostnej kontroly webu (prihlásenie, relácie, odkaz rušňovodiča, hlavičky)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from test_web_spolocne import HESLO, csrf, login, make_train, make_user

pytest_plugins = ["test_web_spolocne"]


# ------------------------------------------------------------------ prihlásenie
def test_uspesne_prihlasenie_nenuluje_pocitadlo_ip(client):
    make_user("veduci1", "veduci")
    make_user("obchod1", "obchod")
    for _ in range(4):
        assert login(client, "veduci1", "zle-heslo-12345").status_code == 401
    assert login(client, "obchod1").status_code == 303  # vlastný účet útočníka
    client.cookies.clear()
    assert login(client, "veduci1", "zle-heslo-12345").status_code == 401  # 5. neúspech z tej istej IP
    assert login(client, "veduci1").status_code == 429


def test_ucet_sa_zablokuje_aj_pri_pokusoch_z_roznych_ip(client, web_app):
    make_user("veduci2", "veduci")
    for _ in range(10):
        web_app.state.account_limiter.fail("u:veduci2")
    assert login(client, "veduci2").status_code == 429


def test_limiter_nemaze_blokovane_a_nevytvara_zaznamy():
    from vlaky.web.auth import LoginLimiter

    lim = LoginLimiter(max_failures=5, window_s=900, max_keys=100)
    for _ in range(5):
        lim.fail("ip:utocnik")
    for i in range(1000):
        lim.retry_after(f"ip:dotaz{i}")  # len dotaz – nesmie vytvoriť záznam
        lim.fail(f"ip:iny{i}")
    assert lim.retry_after("ip:utocnik") > 0
    assert len(lim._fails) <= 101


def test_zablokovane_pokusy_nezahltia_audit(client):
    from vlaky.db import session_scope
    from vlaky.models import AuditLog

    make_user("dispecer7")
    for _ in range(5):
        login(client, "dispecer7", "zle-heslo-12345")
    for _ in range(20):
        assert login(client, "dispecer7", "zle-heslo-12345").status_code == 429
    with session_scope() as s:
        n = s.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "login_blocked"))
    assert n <= 1


def test_neexistujuci_ucet_overuje_heslo_rovnako_dlho(cfg, monkeypatch):
    from vlaky import pouzivatelia
    from vlaky.db import session_scope

    calls = []
    real = pouzivatelia.verify_password
    monkeypatch.setattr(pouzivatelia, "verify_password", lambda p, h: calls.append(h) or real(p, h))
    with session_scope() as s:
        assert pouzivatelia.authenticate(s, "nikto-taky", "nejake-heslo-123") is None
    assert len(calls) == 1


def test_odhlasenie_zneplatni_reláciu_na_serveri(client):
    make_user("dispecer8")
    assert login(client, "dispecer8").status_code == 303
    old_cookie = client.cookies.get("vlaky_session")
    assert client.get("/api/vlaky").status_code == 200
    out = client.post("/logout", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert out.status_code == 303
    client.cookies.set("vlaky_session", old_cookie)
    assert client.get("/api/vlaky").status_code == 401


# ------------------------------------------------------------------ produkčné nastavenia, hlavičky, export
def test_produkcia_vyzaduje_silny_kluc_a_https(cfg):
    from vlaky.config import override_settings
    from vlaky.web.app import production_problems

    weak = override_settings(env="prod", secret_key="", public_url="")
    assert len(production_problems(weak)) == 2
    weak2 = override_settings(env="prod", secret_key="a" * 40, public_url="http://dispecing.example")
    assert len(production_problems(weak2)) == 2  # jeden znak opakovaný + http
    ok = override_settings(env="prod", secret_key="3f9c2b7e1d8a4c6f0b5e9d2a7c1f8e3b6d0a4c9e",
                           public_url="https://dispecing.example")
    assert production_problems(ok) == []


def test_csp_povoli_len_maplibre(client):
    csp = client.get("/login").headers["content-security-policy"]
    assert "https://unpkg.com/maplibre-gl@" in csp
    assert "https://unpkg.com;" not in csp and not csp.rstrip().endswith("https://unpkg.com")


def test_csv_neutralizuje_vzorce():
    from vlaky.web.views_reporty import _cell

    for bad in ("=1+1", " =1+1", "\n=cmd", "＝1+1", "+421900", "@SUM(A1)"):
        assert _cell(bad).startswith("'"), bad
    assert _cell("Čadca") == "Čadca"


def test_teams_bez_maskovanych_odkazov():
    from vlaky.notifikacie.odosielanie import plain_markdown

    out = plain_markdown("[ŽSR dispečing](https://podvrh.example/login)")
    assert "](" not in out and "https://podvrh.example/login" in out


def test_migracia_doplni_novy_stlpec(tmp_path):
    import sqlite3

    from vlaky.db import dispose_all, init_db

    path = tmp_path / "stara.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(80) UNIQUE, full_name VARCHAR(160), "
                "password_hash VARCHAR(200), role VARCHAR(20), email VARCHAR(200), phone VARCHAR(40), "
                "active BOOLEAN, created_at DATETIME)")
    con.execute("INSERT INTO users (username, password_hash, role, active) VALUES ('x', 'h', 'dispecer', 1)")
    con.commit()
    con.close()
    try:
        init_db(f"sqlite:///{path}")
    finally:
        dispose_all()
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    val = con.execute("SELECT session_epoch FROM users").fetchone()[0]
    con.close()
    assert "session_epoch" in cols and val == 0


# ------------------------------------------------------------------ odkaz rušňovodiča
def _driver(client, **train_kw):
    from vlaky.db import session_scope
    from vlaky.models import Train
    from vlaky.web.rusnovodic import make_driver_token

    train_id = make_train(**train_kw)
    with session_scope() as s:
        return train_id, make_driver_token(s.get(Train, train_id))


def test_nastup_nevynuluje_beziacu_zmenu(client):
    from vlaky.cas import now_utc
    from vlaky.db import session_scope
    from vlaky.models import Driver, DriverShift

    train_id, token = _driver(client, status="planovany")
    start = now_utc() - timedelta(hours=12, minutes=40)
    with session_scope() as s:
        d = Driver(name="R. Unavený")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=train_id, start_utc=start, status="prebieha"))
    r = client.post("/api/hlasenie", json={"t": token, "typ": "nastup"})
    assert r.status_code == 409
    with session_scope() as s:
        sh = s.scalar(select(DriverShift).where(DriverShift.train_id == train_id))
        assert abs((sh.start_utc.replace(tzinfo=start.tzinfo) - start).total_seconds()) < 1


def test_odchod_sa_neda_zopakovat(client):
    _train_id, token = _driver(client, status="planovany")
    assert client.post("/api/hlasenie", json={"t": token, "typ": "odchod"}).status_code == 200
    assert client.post("/api/hlasenie", json={"t": token, "typ": "odchod"}).status_code == 409
    assert client.post("/api/hlasenie", json={"t": token, "typ": "pripraveny"}).status_code == 409


def test_limit_hlaseni_a_jedno_upozornenie_na_problem(client):
    from vlaky.db import session_scope
    from vlaky.models import Notification

    _train_id, token = _driver(client, status="planovany")
    codes = [client.post("/api/hlasenie", json={"t": token, "typ": "problem", "text": f"problém {i} "
                                                "[odkaz](https://podvrh.example)"}).status_code for i in range(8)]
    assert codes[:6] == [200] * 6 and codes[6:] == [429, 429]
    with session_scope() as s:
        notes = list(s.scalars(select(Notification).where(Notification.subject.contains("problém"),
                                                          Notification.status != "potlacena")))
    assert notes and len({n.dedup_key for n in notes}) == 1


def test_poloha_z_mobilu_stara_rychla_a_prilis_skoro(client):
    from vlaky.cas import now_utc

    _train_id, token = _driver(client, status="v_jazde")
    now = now_utc()
    old = client.post("/api/pozicie/rusnovodic", json={"t": token, "lat": 49.4385, "lon": 18.7892,
                                                       "ts": (now - timedelta(hours=1)).isoformat()})
    assert old.status_code == 422
    first = client.post("/api/pozicie/rusnovodic", json={"t": token, "lat": 49.4385, "lon": 18.7892,
                                                         "ts": (now - timedelta(seconds=60)).isoformat()})
    assert first.status_code == 200 and not first.json().get("skipped")
    soon = client.post("/api/pozicie/rusnovodic", json={"t": token, "lat": 49.4386, "lon": 18.7893,
                                                        "ts": (now - timedelta(seconds=55)).isoformat()})
    assert soon.status_code == 200 and soon.json().get("skipped")
    jump = client.post("/api/pozicie/rusnovodic", json={"t": token, "lat": 48.7206, "lon": 21.2581,
                                                        "ts": now.isoformat()})  # Košice o minútu neskôr
    assert jump.status_code == 422
