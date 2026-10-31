"""Web: dopravne (M3) – vyhľadanie a ručná úprava údajov dispečingu (LocationInfo) s auditom."""
from __future__ import annotations

from sqlalchemy import select

from test_web_spolocne import csrf, login_as

pytest_plugins = ["test_web_spolocne"]


def test_hladanie_a_uprava_dopravne(client):
    from vlaky.db import session_scope
    from vlaky.models import AuditLog, LocationInfo

    login_as(client, "dispecer")
    page = client.get("/dopravne", params={"q": "Žil"}).text
    assert "Žilina" in page and "/dopravne/upravit?stanica=" in page
    form = client.get("/dopravne/upravit", params={"stanica": "Žilina"})
    assert form.status_code == 200 and 'name="stanica" value="Žilina"' in form.text
    token = csrf(client, "/dopravne")
    r = client.post("/dopravne/upravit", follow_redirects=False, data={
        "csrf_token": token, "stanica": "zilina", "can_wait": "1", "can_stable": "1", "max_train_length_m": "650",
        "note": "odstavná koľaj 12 (FIKTÍVNE)"})
    assert r.status_code == 303
    with session_scope() as s:
        li = s.scalar(select(LocationInfo).where(LocationInfo.station == "Žilina"))
        assert li.can_wait and li.can_stable and not li.can_change_crew and li.max_train_length_m == 650.0
        a = s.scalar(select(AuditLog).where(AuditLog.action == "location_info_update"))
        assert a.detail["station"] == "Žilina" and a.detail["created"] is True
    places = {f["properties"]["name"]: f["properties"] for f in client.get("/api/siet/miesta").json()["features"]}
    assert places["Žilina"]["note"] == "odstavná koľaj 12 (FIKTÍVNE)"
    same = client.post("/dopravne/upravit", follow_redirects=True, data={
        "csrf_token": token, "stanica": "Žilina", "can_wait": "1", "can_stable": "1", "max_train_length_m": "650",
        "note": "odstavná koľaj 12 (FIKTÍVNE)"})
    assert "Bez zmien" in same.text
    assert "Žilina" in client.get("/dopravne").text  # v evidencii


def test_validacia_a_opravnenia(client):
    login_as(client, "dispecer")
    token = csrf(client, "/dopravne")
    bad = client.post("/dopravne/upravit", data={"csrf_token": token, "stanica": "Žilina", "max_train_length_m": "10"})
    assert bad.status_code == 422 and "najmenej" in bad.text
    unknown = client.post("/dopravne/upravit", data={"csrf_token": token, "stanica": "Atlantída"})
    assert unknown.status_code == 404
    client.cookies.clear()
    login_as(client, "obchod", "obchod")
    assert client.get("/dopravne", params={"q": "Žil"}).status_code == 200
    assert "/dopravne/upravit" not in client.get("/dopravne", params={"q": "Žil"}).text
    assert client.post("/dopravne/upravit", data={"csrf_token": csrf(client, "/dopravne"), "stanica": "Žilina"},
                       follow_redirects=False).status_code == 403
