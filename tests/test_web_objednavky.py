"""Web: objednávky zákazníkov (M2) – zoznam, návrh agenta, vlak z objednávky, zamietnutie."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from test_web_spolocne import csrf, login_as

pytest_plugins = ["test_web_spolocne"]


def _order(**kw) -> int:
    from vlaky.cas import now_utc
    from vlaky.db import session_scope
    from vlaky.models import Order

    now = now_utc()
    values = dict(customer="FIKTÍVNY Cement a.s.", origin="Žilina", destination="Košice",
                  earliest_departure_utc=now + timedelta(hours=8), deadline_utc=now + timedelta(hours=30),
                  cargo="cement", rid=True, wagons=20, weight_t=1200.0, length_m=400.0, status="navrhnuta")
    values.update(kw)
    with session_scope() as s:
        o = Order(**values)
        s.add(o)
        s.flush()
        return o.id


FAKE_PLAN = {
    "decision": "VYPRAVIŤ", "explanation": ["VYPRAVIŤ po trase „hlavná trasa“ (FIKTÍVNE)."],
    "routes": [{"name": "hlavná trasa", "km": 241.0, "minutes": 190, "stations": ["Žilina", "Poprad-Tatry", "Košice"]}],
    "variants": [{"route": "hlavná trasa", "depart": "2026-11-02T10:00:00+00:00", "kind": "VYPRAVIŤ",
                  "status": "ZELENÁ", "p_on_time": 0.95, "p_hold": 0.0, "arrival_p50": "2026-11-02T14:00:00+00:00",
                  "arrival_p90": "2026-11-02T14:40:00+00:00", "expected_cost": 1500, "reasons": []}],
    "path_proposal": {"route": "hlavná trasa", "km": 241.0, "stations": [
        {"name": "Žilina", "type": "station", "km": 0, "arr": None, "dep": "2026-11-02T10:00:00+00:00"},
        {"name": "Kraľovany", "type": "station", "km": 38.0, "arr": "2026-11-02T10:40:00+00:00",
         "dep": "2026-11-02T10:40:00+00:00"},
        {"name": "Košice", "type": "station", "km": 241.0, "arr": "2026-11-02T14:00:00+00:00", "dep": None}],
        "request_window": ["2026-11-02T09:30:00+00:00", "2026-11-02T11:00:00+00:00"], "needs_new_path": True},
}


def test_zoznam_a_detail_s_navrhom_agenta(client, monkeypatch):
    import vlaky.planovanie.planovac as planovac

    oid = _order()
    _order(customer="Zamietnutý FIKTÍVNY", status="zamietnuta")
    calls = []
    monkeypatch.setattr(planovac, "plan_order", lambda session, order_id, now=None: calls.append(order_id) or FAKE_PLAN)
    login_as(client, "dispecer")
    listing = client.get("/objednavky").text
    assert "FIKTÍVNY Cement a.s." in listing and "Zamietnutý FIKTÍVNY" not in listing  # zamietnuté len vo filtri
    assert "Zamietnutý FIKTÍVNY" in client.get("/objednavky?stav=zamietnuta").text
    assert '<span class="nav-badge"' in listing  # navrhnutá objednávka v navigácii
    detail = client.get(f"/objednavky/{oid}").text
    assert calls == [oid]
    assert "VYPRAVIŤ" in detail and "hlavná trasa" in detail and "Kraľovany" in detail
    assert "Okno na objednanie odchodu" in detail and "RID" in detail
    assert f'href="/vlaky/novy?objednavka={oid}"' in detail


def test_detail_s_realnym_planovacom(client):
    oid = _order()
    login_as(client, "obchod", "obchod")
    page = client.get(f"/objednavky/{oid}")
    assert page.status_code == 200 and "Návrh prepravy agenta" in page.text
    assert "Vytvoriť vlak z objednávky" not in page.text  # obchod nič nemení


def test_vlak_z_objednavky(client, monkeypatch):
    import vlaky.planovanie.planovac as planovac
    from vlaky.db import session_scope
    from vlaky.models import Train

    monkeypatch.setattr(planovac, "plan_order", lambda session, order_id, now=None: FAKE_PLAN)
    oid = _order()
    login_as(client, "dispecer")
    form = client.get(f"/vlaky/novy?objednavka={oid}").text
    assert f'name="order_id" value="{oid}"' in form
    assert 'name="origin" type="text" value="Žilina"' in form and "FIKTÍVNY Cement a.s." in form
    assert 'name="rid" value="1" checked' in form
    r = client.post("/vlaky/novy", follow_redirects=False, data={
        "csrf_token": csrf(client), "order_id": str(oid), "number": "71001", "run_date": "2026-11-02",
        "customer": "FIKTÍVNY Cement a.s.", "origin": "Žilina", "destination": "Košice", "gauge": "1435",
        "traction": "E", "vmax_kmh": "100", "status": "planovany", "rid": "1"})
    assert r.status_code == 303
    with session_scope() as s:
        t = s.scalar(select(Train).where(Train.number == "71001"))
        assert t.order_id == oid and t.rid is True
    assert "71001" in client.get(f"/objednavky/{oid}").text
    assert client.get("/vlaky/novy?objednavka=999999").status_code == 404


def test_zamietnutie_objednavky(client, monkeypatch):
    import vlaky.planovanie.planovac as planovac
    from vlaky.db import session_scope
    from vlaky.models import AuditLog, Order

    monkeypatch.setattr(planovac, "plan_order", lambda session, order_id, now=None: FAKE_PLAN)
    oid = _order()
    login_as(client, "dispecer")
    token = csrf(client)
    r = client.post(f"/objednavky/{oid}/zamietnut", data={"csrf_token": token, "dovod": ""}, follow_redirects=True)
    assert "uveďte dôvod" in r.text
    r = client.post(f"/objednavky/{oid}/zamietnut", data={"csrf_token": token, "dovod": "termín nestihneme"},
                    follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        o = s.get(Order, oid)
        assert o.status == "zamietnuta" and "termín nestihneme" in o.notes
        a = s.scalar(select(AuditLog).where(AuditLog.action == "order_reject"))
        assert a.detail["reason"] == "termín nestihneme" and a.detail["previous_status"] == "navrhnuta"
    client.cookies.clear()
    login_as(client, "obchod1", "obchod")
    assert client.post(f"/objednavky/{_order()}/zamietnut", data={"csrf_token": csrf(client), "dovod": "x"},
                       follow_redirects=False).status_code == 403
