"""Web: register výluk – vytvorenie, geometria v /api/vyluky (podľa času), overenie, úprava, zrušenie."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from test_web_spolocne import csrf, login_as, make_train

pytest_plugins = ["test_web_spolocne"]


def _local(dt) -> str:
    from vlaky.cas import local

    return local(dt).strftime("%Y-%m-%dT%H:%M")


def test_vyluka_vytvorenie_a_geometria(client):
    from vlaky.cas import now_utc
    from vlaky.db import session_scope
    from vlaky.models import AuditLog, Closure

    login_as(client, "dispecer")
    token = csrf(client, "/vyluky")
    now = now_utc()
    r = client.post("/vyluky/nova", follow_redirects=False, data={
        "csrf_token": token, "from_station": "Poprad-Tatry", "to_station": "liptovsky mikulas", "kind": "uplna",
        "start_utc": _local(now - timedelta(hours=1)), "end_utc": _local(now + timedelta(hours=5)),
        "status": "navrh", "external_ref": "FIKTÍVNE-1"})
    assert r.status_code == 303
    with session_scope() as s:
        c = s.scalar(select(Closure).order_by(Closure.id.desc()))
        cid = c.id
        assert c.to_station == "Liptovský Mikuláš" and c.status == "navrh" and c.source == "manual"
        assert s.scalar(select(AuditLog).where(AuditLog.action == "closure_create", AuditLog.entity_id == str(cid)))
    fc = client.get("/api/vyluky").json()
    feat = next(f for f in fc["features"] if f["id"] == cid)
    assert feat["geometry"]["type"] == "MultiLineString" and len(feat["geometry"]["coordinates"]) > 5
    lon, lat = feat["geometry"]["coordinates"][0][0]
    assert 48.9 < lat < 49.2 and 19.4 < lon < 20.4
    assert feat["properties"]["aktivna"] is True and feat["properties"]["stav"] == "navrh"
    # o 24 h už neplatí, v okne teraz…+6 h áno
    later = (now + timedelta(hours=24)).isoformat()
    assert all(f["id"] != cid for f in client.get("/api/vyluky", params={"od": later, "do": later}).json()["features"])
    window = client.get("/api/vyluky", params={"od": now.isoformat(), "do": (now + timedelta(hours=6)).isoformat()})
    assert any(f["id"] == cid for f in window.json()["features"])
    # stránka registra
    page = client.get("/vyluky").text
    assert "Poprad-Tatry – Liptovský Mikuláš" in page and "platí teraz" in page
    # overiť a zrušiť
    assert client.post(f"/vyluky/{cid}/overit", data={"csrf_token": token}, follow_redirects=False).status_code == 303
    assert client.post(f"/vyluky/{cid}/zrusit", data={"csrf_token": token, "dovod": "zrušená ŽSR"},
                       follow_redirects=False).status_code == 303
    with session_scope() as s:
        c = s.get(Closure, cid)
        assert c.status == "zrusene" and c.version == 3 and "zrušená ŽSR" in c.note
    assert all(f["id"] != cid for f in client.get("/api/vyluky").json()["features"])


def test_vyluka_denne_okno_a_pomala_jazda(client, fixed_now):
    from vlaky.db import session_scope
    from vlaky.models import Closure

    login_as(client, "dispecer")
    token = csrf(client, "/vyluky")
    # teraz je 05:00 miestneho času; denne 08:00–14:00 → teraz neplatí, v okne +6 h áno
    r = client.post("/vyluky/nova", follow_redirects=False, data={
        "csrf_token": token, "from_station": "Žilina", "to_station": "Vrútky", "kind": "pomala_jazda",
        "speed_kmh": "40", "start_utc": "2026-11-01T00:00", "end_utc": "2026-11-10T23:00", "daily_from": "08:00",
        "daily_to": "14:00"})
    assert r.status_code == 303
    with session_scope() as s:
        cid = s.scalar(select(Closure.id).order_by(Closure.id.desc()))
    assert all(f["id"] != cid for f in client.get("/api/vyluky").json()["features"])
    six = client.get("/api/vyluky", params={"od": fixed_now.isoformat(),
                                             "do": (fixed_now + timedelta(hours=6)).isoformat()}).json()
    feat = next(f for f in six["features"] if f["id"] == cid)
    assert feat["properties"]["pomala"] is True and feat["properties"]["aktivna"] is False
    assert feat["properties"]["najblizsie_od"].startswith("2026-11-02T07:00")  # 08:00 CET
    # pomalá jazda bez rýchlosti a rovnaké stanice → chyby
    bad = client.post("/vyluky/nova", data={"csrf_token": token, "from_station": "Žilina", "to_station": "Žilina",
                                            "kind": "pomala_jazda", "start_utc": "2026-11-02T08:00",
                                            "end_utc": "2026-11-02T07:00"})
    assert bad.status_code == 422
    assert "Koniec musí byť po začiatku" in bad.text and "obmedzenú rýchlosť" in bad.text


def test_vyluka_neznama_stanica_a_uprava(client):
    from vlaky.db import session_scope
    from vlaky.models import AuditLog, Closure

    login_as(client, "dispecer")
    token = csrf(client, "/vyluky")
    bad = client.post("/vyluky/nova", data={"csrf_token": token, "from_station": "Atlantída", "to_station": "Košice",
                                            "kind": "uplna", "start_utc": "2026-11-02T08:00",
                                            "end_utc": "2026-11-02T12:00"})
    assert bad.status_code == 422 and "nie je v registri siete" in bad.text
    data = {"csrf_token": token, "from_station": "Kúty", "to_station": "Devínska Nová Ves", "kind": "trakcne_vedenie",
            "start_utc": "2026-11-02T08:00", "end_utc": "2026-11-02T12:00", "status": "overene"}
    assert client.post("/vyluky/nova", data=data, follow_redirects=False).status_code == 303
    with session_scope() as s:
        c = s.scalar(select(Closure).order_by(Closure.id.desc()))
        cid = c.id
        assert c.electric_only is True  # vypnuté trakčné vedenie obmedzuje len elektrickú trakciu
    form = client.get(f"/vyluky/{cid}/upravit").text
    assert 'value="2026-11-02T08:00"' in form
    r = client.post(f"/vyluky/{cid}/upravit", data={**data, "end_utc": "2026-11-02T16:00"}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        c = s.get(Closure, cid)
        assert c.version == 2 and c.end_utc.replace(tzinfo=None).isoformat() == "2026-11-02T15:00:00"
        a = s.scalar(select(AuditLog).where(AuditLog.action == "closure_update", AuditLog.entity_id == str(cid)))
        assert "end_utc" in a.detail["changes"]


def test_vyluka_na_trase_vlaku_je_riziko(client):
    from vlaky.cas import now_utc

    train_id = make_train(number="48001", origin="Čadca", destination="Žilina")
    login_as(client, "dispecer")
    token = csrf(client, "/vyluky")
    now = now_utc()
    client.post("/vyluky/nova", data={"csrf_token": token, "from_station": "Krásno nad Kysucou",
                                      "to_station": "Kysucké Nové Mesto", "kind": "uplna",
                                      "start_utc": _local(now + timedelta(hours=2)),
                                      "end_utc": _local(now + timedelta(hours=6))})
    t = next(x for x in client.get("/api/vlaky").json()["vlaky"] if x["id"] == train_id)
    assert t["route_closures"] and t["route_closures"][0]["od_stanice"] == "Krásno nad Kysucou"
    assert "Krásno nad Kysucou" in t["risk"]
