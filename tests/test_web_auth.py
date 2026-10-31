"""Web: prihlásenie, relácia, obmedzenie pokusov, roly, CSRF, bezpečnostné hlavičky, /health."""
from __future__ import annotations

import re

from test_web_spolocne import HESLO, csrf, login, login_as, make_user

pytest_plugins = ["test_web_spolocne"]


def test_login_uspech_a_odhlasenie(client):
    make_user("dispecer1")
    r = login(client, "dispecer1")
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers.get("set-cookie", "")
    assert "vlaky_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    assert "Secure" not in cookie  # public_url je http v testoch
    home = client.get("/")
    assert home.status_code == 200 and 'id="mapa"' in home.text
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', home.text).group(1)
    out = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"] == "/login"
    after = client.get("/", follow_redirects=False)
    assert after.status_code == 303 and after.headers["location"].startswith("/login")


def test_login_zle_heslo(client):
    make_user("dispecer2")
    r = login(client, "dispecer2", "zle-heslo-12345")
    assert r.status_code == 401
    assert "Nesprávne meno alebo heslo" in r.text
    assert "vlaky_session=" not in r.headers.get("set-cookie", "")


def test_login_obmedzenie_pokusov(client):
    make_user("dispecer3")
    for _ in range(5):
        assert login(client, "dispecer3", "zle-heslo-12345").status_code == 401
    blocked = login(client, "dispecer3", HESLO)  # aj správne heslo je teraz zablokované
    assert blocked.status_code == 429
    assert "Príliš veľa neúspešných pokusov" in blocked.text


def test_login_vyzaduje_csrf_token(client):
    make_user("dispecer4")
    client.get("/login")
    r = client.post("/login", data={"username": "dispecer4", "password": HESLO, "csrf_token": "zly"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_bezpecne_presmerovanie_po_prihlaseni(client):
    make_user("dispecer5")
    page = client.get("/login?next=//zly.example/x")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    r = client.post("/login", data={"username": "dispecer5", "password": HESLO, "csrf_token": token,
                                    "next": "https://zly.example/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_neprihlaseny_pouzivatel(client):
    r = client.get("/vlaky", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/vlaky"
    api = client.get("/api/vlaky")
    assert api.status_code == 401 and api.json()["detail"]


def test_csrf_povinne_pri_formulari(client):
    login_as(client, "dispecer6", "dispecer")
    data = {"from_station": "Poprad-Tatry", "to_station": "Liptovský Mikuláš", "kind": "uplna",
            "start_utc": "2026-11-02T08:00", "end_utc": "2026-11-02T14:00"}
    assert client.post("/vyluky/nova", data=data, follow_redirects=False).status_code == 403
    assert client.post("/vyluky/nova", data={**data, "csrf_token": "zly"}, follow_redirects=False).status_code == 403
    ok = client.post("/vyluky/nova", data={**data, "csrf_token": csrf(client)}, follow_redirects=False)
    assert ok.status_code == 303


def test_rola_obchod_nesmie_menit(client):
    login_as(client, "obchod1", "obchod")
    assert client.get("/vlaky").status_code == 200
    assert client.get("/vyluky").status_code == 200
    token = csrf(client)
    r = client.post("/vyluky/nova", data={"csrf_token": token, "from_station": "Poprad-Tatry",
                                          "to_station": "Liptovský Mikuláš", "kind": "uplna",
                                          "start_utc": "2026-11-02T08:00", "end_utc": "2026-11-02T14:00"},
                    follow_redirects=False)
    assert r.status_code == 403
    assert "oprávnenie" in r.text
    assert client.get("/vlaky/novy").status_code == 403
    assert client.get("/nastavenia").status_code == 403
    assert client.get("/audit").status_code == 403
    page = client.get("/vlaky")
    assert "Nový vlak" not in page.text


def test_rola_planovac_a_veduci(client):
    login_as(client, "planovac1", "planovac")
    token = csrf(client, "/rusnovodici")
    r = client.post("/rusnovodici/novy", data={"csrf_token": token, "name": "Peter FIKTÍVNY"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.post("/vyluky/nova", data={"csrf_token": token}, follow_redirects=False).status_code == 403
    assert client.get("/nastavenia").status_code == 403


def test_bezpecnostne_hlavicky(client):
    r = client.get("/login")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "https://unpkg.com" in csp and "https://tile.openstreetmap.org" in csp
    assert "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in r.headers  # len pri https


def test_health_bez_prihlasenia(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["db"] == "ok"
