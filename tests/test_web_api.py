"""Web API: geometria tratí, stanice, hranice, príjem polôh (map-matching), udalosti jazdy, SSE, trasa vlaku."""
from __future__ import annotations

from datetime import timedelta

from test_web_spolocne import login_as, make_recommendation, make_train

pytest_plugins = ["test_web_spolocne"]

TOKEN = {"Authorization": "Bearer test-token"}


def test_trate_gzip_geojson(client):
    login_as(client, "d1")
    r = client.get("/api/siet/trate")
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["content-type"].startswith("application/geo+json")
    assert "max-age" in r.headers["cache-control"] and r.headers.get("etag")
    data = r.json()  # httpx rozbalí gzip
    assert data["type"] == "FeatureCollection" and len(data["features"]) > 1000
    assert {"el", "v", "g"} <= set(data["features"][0]["properties"])
    again = client.get("/api/siet/trate", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304


def test_stanice_hladanie(client):
    login_as(client, "d2")
    names = [s["name"] for s in client.get("/api/siet/stanice", params={"q": "Žil"}).json()]
    assert "Žilina" in names
    assert client.get("/api/siet/stanice", params={"q": "z"}).json() == []
    allst = client.get("/api/siet/stanice", params={"vsetky": 1}).json()
    assert len(allst) > 300 and all({"name", "lat", "lon"} <= set(s) for s in allst[:5])


def test_hranice_23_priechodov(client):
    login_as(client, "d3")
    items = client.get("/api/hranice").json()
    assert len(items) == 23
    cadca = next(b for b in items if b["code"] == "CADCA")
    assert cadca["handover_station"] == "Čadca" and cadca["lat"] and cadca["lon"]


def test_pozicie_token_a_map_matching(client):
    train_id = make_train(number="44001")
    body = {"train_number": "44001", "lat": 49.2265, "lon": 18.7395, "speed_kmh": 42.5, "accuracy_m": 12}
    assert client.post("/api/pozicie", json=body).status_code == 401
    assert client.post("/api/pozicie", json=body, headers={"Authorization": "Bearer zly"}).status_code == 401
    r = client.post("/api/pozicie", json=body, headers=TOKEN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["train_id"] == train_id and out["snapped"] is True
    assert out["off_track_m"] is not None and out["off_track_m"] <= 300
    assert abs(out["lat"] - 49.2265) < 0.01 and abs(out["lon"] - 18.7395) < 0.01
    assert (out["lat"], out["lon"]) != (49.2265, 18.7395)
    assert out["nearest_station"] == "Žilina"
    from vlaky.db import session_scope
    from vlaky.models import PositionReport

    with session_scope() as s:
        p = s.get(PositionReport, out["id"])
        assert p.source == "gps" and p.matched_edge is not None and p.snapped_lat is not None
        assert p.speed_kmh == 42.5
    # idempotencia: rovnaká správa (rovnaký čas) sa neuloží druhýkrát
    first_ts = out["ts"]
    dup = client.post("/api/pozicie", json={**body, "ts": first_ts}, headers=TOKEN).json()
    assert dup["duplicate"] is True and dup["id"] == out["id"]


def test_pozicie_validacia_a_davka(client):
    bad = client.post("/api/pozicie", json={"train_number": "1", "lat": 123, "lon": 18}, headers=TOKEN)
    assert bad.status_code == 422
    bad_src = client.post("/api/pozicie", json={"train_number": "1", "lat": 48, "lon": 18, "source": "x"},
                          headers=TOKEN)
    assert bad_src.status_code == 422
    future = client.post("/api/pozicie", json={"train_number": "1", "lat": 48, "lon": 18,
                                               "ts": "2099-01-01T00:00:00Z"}, headers=TOKEN)
    assert future.status_code == 422
    batch = client.post("/api/pozicie", headers=TOKEN, json=[
        {"train_number": "55001", "lat": 48.95, "lon": 21.24, "source": "sim"},
        {"train_number": "55002", "lat": 48.15, "lon": 17.11, "source": "zsr"}])
    assert batch.status_code == 200 and batch.json()["stored"] == 2
    assert batch.json()["items"][0]["train_id"] is None  # neznámy vlak – poloha sa uloží bez väzby


def test_api_vlaky_s_polohou_a_odporucanim(client):
    train_id = make_train(number="44002")
    rec_id = make_recommendation(train_id, color="ČERVENÁ", decision="ODSTAVIŤ", priority="KRITICKÉ")
    other = make_train(number="44003", origin="Bratislava hl.st.", destination="Štúrovo")
    client.post("/api/pozicie", json={"train_number": "44002", "lat": 49.2265, "lon": 18.7395}, headers=TOKEN)
    login_as(client, "d4")
    data = client.get("/api/vlaky").json()
    rows = {t["id"]: t for t in data["vlaky"]}
    t = rows[train_id]
    assert t["position"]["snapped"] is True and t["position"]["stale"] is False
    assert t["recommendation"]["id"] == rec_id and t["recommendation"]["color"] == "ČERVENÁ"
    assert t["display_color"] == "ČERVENÁ" and t["driver"]["name"] == "Ján FIKTÍVNY"
    assert t["eta_p50"] and t["next_station"]["name"] == "Čadca"  # odchod z Čadce je ešte len o 1 h
    assert data["vlaky"][0]["id"] == train_id  # ČERVENÁ je prvá
    assert rows[other]["display_color"] == "SIVÁ" and rows[other]["position"] is None
    assert rows[other]["anchor"] is not None
    detail = client.get(f"/api/vlaky/{train_id}").json()
    assert detail["number"] == "44002"
    assert client.get("/api/vlaky/999999").status_code == 404


def test_stara_poloha_je_siva(client, fixed_now):
    from vlaky.db import session_scope
    from vlaky.models import PositionReport

    train_id = make_train(number="44004")
    make_recommendation(train_id, color="ZELENÁ", decision="VYPRAVIŤ")
    with session_scope() as s:
        s.add(PositionReport(train_id=train_id, train_number="44004", ts_utc=fixed_now - timedelta(minutes=40),
                             lat=49.2265, lon=18.7395, source="gps"))
    login_as(client, "d5")
    t = next(x for x in client.get("/api/vlaky").json()["vlaky"] if x["id"] == train_id)
    assert t["position"]["stale"] is True and t["color"] == "ZELENÁ" and t["display_color"] == "SIVÁ"


def test_udalosti_jazdy_a_prichod_na_hranicu(client):
    from vlaky.db import session_scope
    from vlaky.models import BorderCrossing, Handover, PositionReport
    from sqlalchemy import select

    with session_scope() as s:
        cadca = s.scalar(select(BorderCrossing).where(BorderCrossing.code == "CADCA"))
        border_id = cadca.id
    train_id = make_train(number="44005", status="na_ceste_k_hranici", entry_border_id=border_id,
                          origin="Čadca", destination="Žilina")
    with session_scope() as s:
        h = Handover(train_id=train_id, border_id=border_id, direction="vstup", status="ocakavany")
        s.add(h)
        s.flush()
        handover_id = h.id
    body = {"train_number": "44005", "station": "Cadca", "event": "prichod", "ts": "2026-10-31T10:15:00+01:00",
            "delay_min": 25}
    assert client.post("/api/udalosti-jazdy", json=body).status_code == 401
    unknown = client.post("/api/udalosti-jazdy", json={**body, "station": "Neexistujúca stanica XY"}, headers=TOKEN)
    assert unknown.status_code == 400
    r = client.post("/api/udalosti-jazdy", json=body, headers=TOKEN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["station"] == "Čadca" and out["train_id"] == train_id and out["handover_id"] == handover_id
    with session_scope() as s:
        p = s.get(PositionReport, out["id"])
        assert p.source == "zsr" and p.nearest_station == "Čadca" and p.speed_kmh is None
        h = s.get(Handover, handover_id)
        assert h.status == "prisiel"
        assert h.arrived_utc.replace(tzinfo=None).isoformat() == "2026-10-31T09:15:00"
    # odchod z inej stanice nemení preberanie; zdroj TAF TSI
    r2 = client.post("/api/udalosti-jazdy", headers=TOKEN, json={"train_number": "44005", "station": "Žilina",
                                                                 "event": "odchod", "ts": "2026-10-31T10:30:00Z",
                                                                 "source": "taf"})
    assert r2.status_code == 200 and r2.json()["handover_id"] is None
    bad = client.post("/api/udalosti-jazdy", headers=TOKEN, json={**body, "event": "stoji"})
    assert bad.status_code == 422


def test_sse_hlavicky_a_prva_sprava(client):
    login_as(client, "d6")
    with client.stream("GET", "/api/stream", params={"max_s": 1}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"].startswith("no-cache")
        first = next(r.iter_text())
    assert "retry:" in first or "data:" in first
    full = client.get("/api/stream", params={"max_s": 1}).text
    assert '"type": "hello"' in full and '"pozicia"' in full


def test_trasa_vlaku_geojson(client):
    train_id = make_train(number="44006", origin="Čadca", destination="Žilina")
    login_as(client, "d7")
    r = client.get(f"/api/vlaky/{train_id}/trasa")
    assert r.status_code == 200
    f = r.json()
    assert f["geometry"]["type"] == "LineString" and len(f["geometry"]["coordinates"]) > 10
    assert 20 < f["properties"]["length_km"] < 60
    lon, lat = f["geometry"]["coordinates"][0]
    assert 49.3 < lat < 49.6 and 18.6 < lon < 18.9  # začína v Čadci


def test_vyluky_api_validacia(client):
    login_as(client, "d8")
    assert client.get("/api/vyluky", params={"od": "nie-je-cas"}).status_code == 422
    fc = client.get("/api/vyluky").json()
    assert fc["type"] == "FeatureCollection" and fc["features"] == []


def test_upozornenia(client, fixed_now):
    from vlaky.db import session_scope
    from vlaky.models import Driver, DriverShift, ExtractedEvent, Notification

    train_id = make_train(number="44010", status="v_jazde")
    make_recommendation(train_id, color="ČERVENÁ", priority="KRITICKÉ", summary="Vlak 44010: ESKALOVAŤ (FIKTÍVNE).")
    with session_scope() as s:
        d = Driver(name="Ján FIKTÍVNY")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=train_id, start_utc=fixed_now - timedelta(hours=12, minutes=40),
                          status="prebieha"))
        s.add(ExtractedEvent(kind="vyluka_nova", status="navrh", needs_confirmation=True, data={}))
        s.add(Notification(channel="email", recipient="dispecer1@dopravca.example", subject="[AGENT][INFO] Súhrn",
                           priority="INFO", dedup_key="suhrn:1", created_at=fixed_now))
    login_as(client, "d9")
    items = client.get("/api/upozornenia").json()["upozornenia"]
    texts = " | ".join(a["text"] for a in items)
    assert "zostáva 20 min" in texts and "Na potvrdenie čaká 1 udalosť" in texts
    assert "chýbajú čerstvé dáta o polohe" in texts and "ESKALOVAŤ" in texts and "Súhrn" in texts
    assert items[0]["priority"] == "KRITICKÉ"
    assert client.get("/").status_code == 200  # dashboard s upozorneniami sa vykreslí
