"""Web: odkaz pre rušňovodiča (token vo fragmente URL, 24 h, zrušiteľný), hlásenia a poloha z mobilu."""
from __future__ import annotations

import re
import time

from sqlalchemy import select

from test_web_spolocne import csrf, login_as, make_train

pytest_plugins = ["test_web_spolocne"]


def _link(client, train_id: int, login: bool = True) -> str:
    if login:
        login_as(client, "dispecer")
    page = client.post(f"/vlaky/{train_id}/odkaz", data={"csrf_token": csrf(client)})
    assert page.status_code == 200
    m = re.search(r'value="(http://localhost:8000/rusnovodic#t=[^"]+)"', page.text)
    assert m, "odkaz pre rušňovodiča chýba (token má byť vo fragmente #t=…)"
    assert "?t=" not in m.group(1)
    return m.group(1).split("#t=", 1)[1]


def test_odkaz_a_hlasenia(client):
    from vlaky.cas import now_utc
    from vlaky.db import session_scope
    from vlaky.models import AuditLog, Driver, DriverShift, Train

    train_id = make_train(number="50001", status="prevzaty", ready_utc=None)
    with session_scope() as s:
        d = Driver(name="Ján FIKTÍVNY")
        s.add(d)
        s.flush()
        sh = DriverShift(driver_id=d.id, train_id=train_id, start_utc=now_utc().replace(year=2026, month=11, day=1),
                         status="planovana")
        s.add(sh)
        s.flush()
        shift_id = sh.id
    token = _link(client, train_id)
    client.cookies.clear()  # rušňovodič nie je prihlásený
    page = client.get("/rusnovodic")  # token sa serveru v URL neposiela
    assert page.status_code == 200
    assert 'rel="manifest"' in page.text and "/static/js/rusnovodic.js" in page.text
    assert "50001" not in page.text  # údaje vlaku načíta až JS s tokenom
    info = client.post("/api/rusnovodic/info", json={"t": token})
    assert info.status_code == 200
    data = info.json()
    assert data["train"]["number"] == "50001" and data["driver_name"] == "Ján FIKTÍVNY"
    assert data["train"]["relation"] == "Čadca → Žilina" and data["valid_until"]
    assert client.get("/rusnovodic.webmanifest").headers["content-type"].startswith("application/manifest+json")

    before = now_utc()
    r = client.post("/api/hlasenie", json={"t": token, "typ": "nastup"})
    assert r.status_code == 200 and r.json()["ok"] is True
    with session_scope() as s:
        sh = s.get(DriverShift, shift_id)
        assert sh.status == "prebieha"
        assert abs((sh.start_utc.replace(tzinfo=None) - before.replace(tzinfo=None)).total_seconds()) < 60
    assert client.post("/api/hlasenie", json={"t": token, "typ": "pripraveny"}).status_code == 200
    assert client.post("/api/hlasenie", json={"t": token, "typ": "odchod"}).status_code == 200
    no_text = client.post("/api/hlasenie", json={"t": token, "typ": "problem", "text": " "})
    assert no_text.status_code == 422
    assert client.post("/api/hlasenie", json={"t": token, "typ": "problem",
                                              "text": "Porucha brzdy na 5. vozni"}).status_code == 200
    with session_scope() as s:
        t = s.get(Train, train_id)
        assert t.ready_utc is not None and t.status == "v_jazde"
        actions = [a.action for a in s.scalars(select(AuditLog).where(AuditLog.actor == "rusnovodic:50001"))]
        assert {"hlasenie_nastup", "hlasenie_pripraveny", "hlasenie_odchod", "hlasenie_problem"} <= set(actions)
        assert s.scalar(select(AuditLog).where(AuditLog.action == "driver_link")) is not None


def test_poloha_z_mobilu(client):
    from vlaky.db import session_scope
    from vlaky.models import PositionReport

    train_id = make_train(number="50002")
    token = _link(client, train_id)
    r = client.post("/api/pozicie/rusnovodic", json={"t": token, "lat": 49.2265, "lon": 18.7395,
                                                      "accuracy_m": 15, "speed_kmh": 0})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["train_id"] == train_id and out["snapped"] is True
    with session_scope() as s:
        assert s.get(PositionReport, out["id"]).source == "driver"


def test_zrusenie_odkazov(client):
    from vlaky.db import session_scope
    from vlaky.models import AuditLog

    train_id = make_train(number="50005")
    old = _link(client, train_id)
    assert client.post("/api/rusnovodic/info", json={"t": old}).status_code == 200
    token = csrf(client)
    r = client.post(f"/vlaky/{train_id}/odkaz/zrusit", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    for url, body in (("/api/rusnovodic/info", {"t": old}), ("/api/hlasenie", {"t": old, "typ": "odchod"}),
                      ("/api/pozicie/rusnovodic", {"t": old, "lat": 49.2265, "lon": 18.7395})):
        assert client.post(url, json=body).status_code == 403, url
    new = _link(client, train_id, login=False)
    assert new != old
    assert client.post("/api/rusnovodic/info", json={"t": new}).status_code == 200
    with session_scope() as s:
        assert s.scalar(select(AuditLog).where(AuditLog.action == "driver_link_revoke")) is not None
    # obchod nesmie rušiť odkazy
    client.cookies.clear()
    login_as(client, "obchod1", "obchod")
    assert client.post(f"/vlaky/{train_id}/odkaz/zrusit", data={"csrf_token": csrf(client)},
                       follow_redirects=False).status_code == 403


def test_neplatny_alebo_cudzi_token(client):
    from vlaky.db import session_scope
    from vlaky.models import Train
    from vlaky.web.rusnovodic import make_driver_token

    train_id = make_train(number="50003")
    assert client.get("/rusnovodic").status_code == 200
    assert "neplatný alebo vypršal" in client.get("/rusnovodic").text  # sekcia pre neplatný odkaz (zobrazí JS)
    assert client.post("/api/rusnovodic/info", json={"t": "zly-token-123456"}).status_code == 403
    assert client.post("/api/hlasenie", json={"t": "zly-token-123456", "typ": "odchod"}).status_code == 403
    bad_pos = client.post("/api/pozicie/rusnovodic", json={"t": "zly-token-123456", "lat": 48, "lon": 18})
    assert bad_pos.status_code == 403
    with session_scope() as s:
        t = s.get(Train, train_id)
        token = make_driver_token(t)
        assert client.post("/api/rusnovodic/info", json={"t": token}).status_code == 200
        t.status = "ukonceny"  # ukončený vlak – odkaz už neplatí
    assert client.post("/api/hlasenie", json={"t": token, "typ": "odchod"}).status_code == 403


def test_token_vyprsi_po_24_h(client, monkeypatch):
    from vlaky.web import rusnovodic

    train_id = make_train(number="50004")
    token = _link(client, train_id)
    assert rusnovodic.verify_driver_token(token) is not None
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 25 * 3600)
    assert rusnovodic.verify_driver_token(token) is None
