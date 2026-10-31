"""Web: odkaz pre rušňovodiča (podpísaný token na 24 h), hlásenia a poloha z mobilu."""
from __future__ import annotations

import re
import time

from sqlalchemy import select

from test_web_spolocne import csrf, login_as, make_train

pytest_plugins = ["test_web_spolocne"]


def _link(client, train_id: int) -> str:
    login_as(client, "dispecer")
    page = client.post(f"/vlaky/{train_id}/odkaz", data={"csrf_token": csrf(client)})
    assert page.status_code == 200
    m = re.search(r'value="(http://localhost:8000/rusnovodic\?t=[^"]+)"', page.text)
    assert m, "odkaz pre rušňovodiča chýba"
    return m.group(1).split("?t=", 1)[1]


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
    page = client.get("/rusnovodic", params={"t": token})
    assert page.status_code == 200
    assert "Vlak 50001" in page.text and 'rel="manifest"' in page.text and "Ján FIKTÍVNY" in page.text
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


def test_neplatny_alebo_cudzi_token(client):
    from vlaky.db import session_scope
    from vlaky.models import Train
    from vlaky.web.rusnovodic import make_driver_token

    train_id = make_train(number="50003")
    assert client.get("/rusnovodic", params={"t": "zly-token-123456"}).status_code == 403
    assert "neplatný alebo vypršal" in client.get("/rusnovodic").text
    assert client.post("/api/hlasenie", json={"t": "zly-token-123456", "typ": "odchod"}).status_code == 403
    bad_pos = client.post("/api/pozicie/rusnovodic", json={"t": "zly-token-123456", "lat": 48, "lon": 18})
    assert bad_pos.status_code == 403
    with session_scope() as s:
        t = s.get(Train, train_id)
        token = make_driver_token(t)
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
