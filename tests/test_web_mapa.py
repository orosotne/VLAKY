"""Web – doplnky živej mapy (M1): podklad z konfigurácie, odhad polohy, stopa 24 h, vrstvy, obchádzka,
nepotvrdené výluky z e-mailov."""
from __future__ import annotations

import json
import re
import sys
import types
from datetime import timedelta

from sqlalchemy import select

from test_web_spolocne import csrf, login_as, make_train

pytest_plugins = ["test_web_spolocne"]


def _route_point(client, train_id: int, frac: float) -> tuple:
    coords = client.get(f"/api/vlaky/{train_id}/trasa").json()["geometry"]["coordinates"]
    lon, lat = coords[int(len(coords) * frac)]
    return lat, lon


def _add_position(train_id: int, number: str, lat: float, lon: float, minutes_ago: float, speed=None):
    from vlaky.cas import now_utc
    from vlaky.db import session_scope
    from vlaky.models import PositionReport

    with session_scope() as s:
        s.add(PositionReport(train_id=train_id, train_number=number, ts_utc=now_utc() - timedelta(minutes=minutes_ago),
                             lat=lat, lon=lon, snapped_lat=lat, snapped_lon=lon, speed_kmh=speed, source="gps"))


def _row(client, train_id: int) -> dict:
    return next(t for t in client.get("/api/vlaky").json()["vlaky"] if t["id"] == train_id)


def test_podklad_mapy_z_konfiguracie(cfg):
    from fastapi.testclient import TestClient

    from vlaky import config as config_mod
    from vlaky.web.app import create_app

    config_mod._OVERRIDE["s"] = cfg.model_copy(update={
        "map_tile_url": "https://{s}.dlazdice.example.org/{z}/{x}/{y}.png",
        "map_tile_attribution": "© FIKTÍVNY poskytovateľ <b>máp</b>"})
    with TestClient(create_app(auto_evaluate=False)) as c:
        login_as(c, "dispecer")
        r = c.get("/")
        csp = r.headers["content-security-policy"]
        assert "img-src 'self' data: blob: https://*.dlazdice.example.org" in csp
        assert "connect-src 'self' https://*.dlazdice.example.org" in csp
        assert "tile.openstreetmap.org" not in csp
        conf = json.loads(re.search(r'id="dash-config">(.*?)</script>', r.text, re.S).group(1))
        assert conf["tileUrl"] == "https://{s}.dlazdice.example.org/{z}/{x}/{y}.png"
        assert conf["tileAttribution"] == "© FIKTÍVNY poskytovateľ <b>máp</b>"  # JS ju vloží ako text
        assert "<b>máp</b>" not in r.text  # v HTML len escapované
    config_mod._OVERRIDE["s"] = cfg.model_copy(update={"map_tile_url": "/dlazdice/{z}/{x}/{y}.png"})
    with TestClient(create_app(auto_evaluate=False)) as c:
        csp = c.get("/login").headers["content-security-policy"]
        assert "img-src 'self' data: blob:;" in csp  # relatívna URL = vlastný server


def test_odhad_polohy_medzi_spravami(client):
    from vlaky.cas import now_utc

    train_id = make_train(number="61001", origin="Žilina", destination="Košice", traction="M", status="v_jazde")
    login_as(client, "dispecer")
    lat, lon = _route_point(client, train_id, 0.3)
    _add_position(train_id, "61001", lat, lon, minutes_ago=5, speed=60)
    est = _row(client, train_id)["estimate"]
    assert est is not None and est["method"] == "rýchlosť" and est["speed_kmh"] == 60
    assert 0.03 < est["km"] <= 5.05  # 60 km/h × 5 min = 5 km (alebo menej po najbližšiu dopravňu)
    assert (est["lat"], est["lon"]) != (round(lat, 6), round(lon, 6)) and est["from"] == [round(lon, 6), round(lat, 6)]

    # podľa časov v odporúčaní (návrh trasy): 50 km/h
    from test_web_spolocne import make_recommendation

    route = client.get(f"/api/vlaky/{train_id}/trasa").json()["properties"]
    first, last = route["stations"][0], route["stations"][-1]
    t0 = now_utc() - timedelta(hours=1)
    hours = (last["km"] - first["km"]) / 50.0
    make_recommendation(train_id, path_proposal={
        "route": "Žilina – Košice", "stations": [
            {"name": first["name"], "type": first["type"], "km": 0, "arr": None, "dep": t0.isoformat()},
            {"name": last["name"], "type": last["type"], "km": last["km"],
             "arr": (t0 + timedelta(hours=hours)).isoformat(), "dep": None}]},
        chosen={"route": "Žilina – Košice"})
    est = _row(client, train_id)["estimate"]
    assert est["method"] == "plán" and est["speed_kmh"] == 50 and est["km"] <= 4.2


def test_bez_odhadu_pri_starej_sprave_alebo_statí(client):
    train_id = make_train(number="61002", origin="Žilina", destination="Košice", traction="M", status="v_jazde")
    other = make_train(number="61003", origin="Žilina", destination="Košice", traction="M", status="v_jazde")
    planned = make_train(number="61004", origin="Žilina", destination="Košice", traction="M", status="planovany")
    login_as(client, "dispecer")
    lat, lon = _route_point(client, train_id, 0.3)
    _add_position(train_id, "61002", lat, lon, minutes_ago=20, speed=60)    # staršia ako 15 min
    _add_position(other, "61003", lat, lon, minutes_ago=5, speed=0)          # stojí
    _add_position(planned, "61004", lat, lon, minutes_ago=5, speed=60)       # nie je v jazde
    assert _row(client, train_id)["estimate"] is None
    assert _row(client, other)["estimate"] is None
    assert _row(client, planned)["estimate"] is None


def test_stopa_24_h(client):
    train_id = make_train(number="61010", origin="Žilina", destination="Košice", traction="M")
    login_as(client, "dispecer")
    empty = client.get(f"/api/vlaky/{train_id}/stopa").json()
    assert empty["geometry"] is None and empty["properties"]["points"] == 0
    for frac, ago in ((0.1, 300), (0.2, 200), (0.3, 100), (0.05, 60 * 30)):  # posledná je staršia ako 24 h
        lat, lon = _route_point(client, train_id, frac)
        _add_position(train_id, "61010", lat, lon, minutes_ago=ago)
    f = client.get(f"/api/vlaky/{train_id}/stopa", params={"hodiny": 24}).json()
    assert f["geometry"]["type"] == "LineString" and len(f["geometry"]["coordinates"]) == 3
    assert f["properties"]["points"] == 3
    assert len(client.get(f"/api/vlaky/{train_id}/stopa", params={"hodiny": 72}).json()["geometry"]["coordinates"]) == 4
    assert client.get("/api/vlaky/999999/stopa").status_code == 404


def test_vrstvy_miesta_a_vlecky(client):
    from vlaky.db import session_scope
    from vlaky.models import LocationInfo

    with session_scope() as s:
        s.add(LocationInfo(station="Žilina", can_wait=True, can_stable=True, max_train_length_m=650,
                           note="koľaj 12 (FIKTÍVNE)"))
        s.add(LocationInfo(station="Vrútky", can_wait=False, can_stable=False, note="nie"))
    login_as(client, "obchod", "obchod")
    places = client.get("/api/siet/miesta").json()["features"]
    by_name = {f["properties"]["name"]: f["properties"] for f in places}
    assert by_name["Žilina"]["max_train_length_m"] == 650 and by_name["Žilina"]["overene"] is True
    assert "Vrútky" not in by_name  # bez možnosti čakať/odstaviť sa nezobrazí
    assert any(p["druh"] == "zriaďovacia stanica" for p in by_name.values())
    sidings = client.get("/api/siet/vlecky").json()["features"]
    assert len(sidings) > 10 and sidings[0]["geometry"]["type"] == "Point"
    page = client.get("/").text
    assert 'id="v-miesta"' in page and 'id="v-vlecky"' in page
    assert 'id="f-zakaznik"' in page and 'id="f-relacia"' in page


def test_trasa_zvolenej_varianty_obchadzka(client):
    from test_web_spolocne import make_recommendation
    from vlaky.siet.graf import TrainSpec, network

    train_id = make_train(number="61020", origin="Žilina", destination="Košice", traction="M", status="planovany")
    login_as(client, "dispecer")
    primary = client.get(f"/api/vlaky/{train_id}/trasa").json()
    assert primary["properties"]["variant"] == "hlavna"
    alt = network().route("Žilina", "Košice", TrainSpec("M", "1435", 100.0), via=["Banská Bystrica"])
    stations = [{"name": st.name, "type": st.type, "km": km, "arr": None, "dep": None} for st, km, _m in alt.stations]
    make_recommendation(train_id, decision="PRESMEROVAŤ", chosen={"route": "obchádzka cez Banská Bystrica"},
                        path_proposal={"route": "obchádzka cez Banská Bystrica", "stations": stations})
    f = client.get(f"/api/vlaky/{train_id}/trasa").json()
    assert f["properties"]["variant"] == "obchadzka" and f["properties"]["name"] == "obchádzka cez Banská Bystrica"
    assert f["properties"]["length_km"] > primary["properties"]["length_km"] + 50
    near_bb = [c for c in f["geometry"]["coordinates"] if abs(c[1] - 48.735) < 0.02 and abs(c[0] - 19.164) < 0.03]
    assert near_bb, "obchádzka má viesť cez Banskú Bystricu"


def _auto_applied_closure(status: str = "aplikovane"):
    from vlaky.cas import now_utc
    from vlaky.db import session_scope
    from vlaky.models import Closure, Email, ExtractedEvent

    now = now_utc()
    with session_scope() as s:
        m = Email(message_id=f"<auto-{now.timestamp()}@zsr.example>", sender="vyluky@zsr.example",
                  subject="Výluka Poprad – L. Mikuláš (FIKTÍVNE)", body_text="Výluka 1. koľaje (FIKTÍVNE)", verdict="ok")
        s.add(m)
        s.flush()
        start, end = now + timedelta(hours=3), now + timedelta(hours=9)
        ev = ExtractedEvent(email_id=m.id, kind="vyluka_nova", confidence=0.97, status=status, needs_confirmation=False,
                            data={"from_station": "Poprad-Tatry", "to_station": "Liptovský Mikuláš",
                                  "start": start.isoformat(), "end": end.isoformat()})
        s.add(ev)
        s.flush()
        c = Closure(kind="uplna", from_station="Poprad-Tatry", to_station="Liptovský Mikuláš", start_utc=start,
                    end_utc=end, status="navrh", source="email", source_event_id=ev.id)
        s.add(c)
        s.flush()
        return ev.id, c.id


def test_nepotvrdena_vyluka_z_emailu_vo_fronte_a_na_mape(client, monkeypatch):
    from vlaky.db import session_scope
    from vlaky.models import Closure, ExtractedEvent

    ev_id, cid = _auto_applied_closure()
    login_as(client, "dispecer")
    queue = client.get("/fronta").text
    assert "Zapísané automaticky" in queue and f'id="udalost-{ev_id}"' in queue and "nepotvrdená" in queue
    assert '<span class="nav-badge"' in queue
    page = client.get("/vyluky").text
    assert "nepotvrdená" in page and f"udalosť #{ev_id}" in page
    feat = next(f for f in client.get("/api/vyluky", params={"do": "2099-01-01T00:00:00Z"}).json()["features"]
                if f["id"] == cid)
    assert feat["properties"]["nepotvrdena"] is True and feat["properties"]["udalost_id"] == ev_id

    calls = []
    mod = types.ModuleType("vlaky.posta.spracovanie")

    def confirm_event(session, event_id, actor, edits=None):
        calls.append((event_id, actor))
        ev = session.get(ExtractedEvent, event_id)
        ev.confirmed_by, ev.status = actor, "aplikovane"
        from vlaky.cas import now_utc

        ev.confirmed_at = now_utc()
        session.scalar(select(Closure).where(Closure.source_event_id == event_id)).status = "overene"
        return ev

    mod.confirm_event = confirm_event
    monkeypatch.setitem(sys.modules, "vlaky.posta.spracovanie", mod)
    r = client.post(f"/vyluky/{cid}/overit", data={"csrf_token": csrf(client), "spat": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert calls == [(ev_id, "dispecer")]
    with session_scope() as s:
        assert s.get(Closure, cid).status == "overene"
    assert "Zapísané automaticky" not in client.get("/fronta").text


def test_potvrdenie_automaticky_zapisanej_udalosti_realnym_modulom(client):
    import pytest

    pytest.importorskip("vlaky.posta.spracovanie")
    from vlaky.db import session_scope
    from vlaky.models import Closure, ExtractedEvent

    ev_id, cid = _auto_applied_closure()
    login_as(client, "dispecer")
    r = client.post(f"/fronta/{ev_id}/potvrdit", data={"csrf_token": csrf(client, "/fronta"), "akcia": "potvrdit"},
                    follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        ev = s.get(ExtractedEvent, ev_id)
        assert ev.confirmed_by == "dispecer" and ev.confirmed_at is not None
        assert s.get(Closure, cid).status == "overene"


def test_rucna_vyluka_v_navrhu_sa_overi_priamo(client):
    from vlaky.cas import now_utc
    from vlaky.db import session_scope
    from vlaky.models import Closure

    with session_scope() as s:
        c = Closure(kind="uplna", from_station="Žilina", to_station="Vrútky", start_utc=now_utc(),
                    end_utc=now_utc() + timedelta(hours=2), status="navrh", source="manual")
        s.add(c)
        s.flush()
        cid = c.id
    login_as(client, "dispecer")
    r = client.post(f"/vyluky/{cid}/overit", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/vyluky"
    with session_scope() as s:
        assert s.get(Closure, cid).status == "overene"
