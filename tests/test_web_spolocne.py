"""Spoločné pomôcky pre testy webovej aplikácie (fixtures sa importujú do testovacích modulov)."""
from __future__ import annotations

import re
from datetime import timedelta

import pytest

HESLO = "tajne-heslo-123"


@pytest.fixture()
def web_app(cfg):
    from vlaky.web.app import create_app

    return create_app(auto_evaluate=False)


@pytest.fixture()
def client(web_app):
    from fastapi.testclient import TestClient

    with TestClient(web_app) as c:
        yield c


def make_user(username: str, role: str = "dispecer", password: str = HESLO) -> None:
    from vlaky.db import session_scope
    from vlaky.pouzivatelia import create_user

    with session_scope() as s:
        create_user(s, username, password, role, full_name=username.capitalize())


def login(client, username: str, password: str = HESLO, follow: bool = False):
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    return client.post("/login", data={"username": username, "password": password, "csrf_token": token,
                                       "next": "/"}, follow_redirects=follow)


def login_as(client, username: str, role: str = "dispecer") -> None:
    make_user(username, role)
    r = login(client, username)
    assert r.status_code == 303, r.text[:500]


def csrf(client, path: str = "/vlaky") -> str:
    r = client.get(path)
    m = re.search(r'<meta name="csrf-token" content="([^"]*)"', r.text)
    assert m and m.group(1), "CSRF token v stránke chýba"
    return m.group(1)


def make_train(**kw):
    """Vlak v DB (predvolene Čadca → Žilina, elektrická trakcia)."""
    from vlaky.cas import now_utc
    from vlaky.db import session_scope
    from vlaky.models import Train

    now = now_utc()
    values = dict(number="12345", run_date=now.date().isoformat(), customer="FIKTÍVNY zákazník", origin="Čadca",
                  destination="Žilina", traction="E", gauge="1435", vmax_kmh=100.0, status="v_jazde",
                  ready_utc=now, deadline_utc=now + timedelta(hours=8))
    values.update(kw)
    with session_scope() as s:
        t = Train(**values)
        s.add(t)
        s.flush()
        return t.id


def make_recommendation(train_id: int, **kw) -> int:
    from vlaky.cas import now_utc
    from vlaky.db import session_scope
    from vlaky.models import Recommendation

    now = now_utc()
    values = dict(
        train_id=train_id, created_at=now, decision="POČKAŤ", color="ORANŽOVÁ", priority="DÔLEŽITÉ",
        summary="Vlak 12345: POČKAŤ, vypraviť o 12:30 (FIKTÍVNE).",
        chosen={"route": "hlavná", "depart": (now + timedelta(hours=1)).isoformat(), "kind": "POČKAŤ",
                "status": "ORANŽOVÁ", "p_on_time": 0.82, "p_hold": 0.01,
                "arrival_p50": (now + timedelta(hours=4)).isoformat(),
                "arrival_p90": (now + timedelta(hours=5)).isoformat(), "expected_cost": 1234,
                "reasons": ["P(včas) 82 % < 90 %"], "needs_new_path": True},
        variants=[{"route": "hlavná", "depart": (now + timedelta(hours=1)).isoformat(), "kind": "POČKAŤ",
                   "status": "ORANŽOVÁ", "p_on_time": 0.82, "p_hold": 0.01,
                   "arrival_p50": (now + timedelta(hours=4)).isoformat(),
                   "arrival_p90": (now + timedelta(hours=5)).isoformat(), "expected_cost": 1234,
                   "reasons": ["P(včas) 82 % < 90 %"], "needs_new_path": True}],
        windows={"hlavná": [[(now + timedelta(hours=1)).isoformat(), (now + timedelta(hours=2)).isoformat()]]},
        path_proposal={"route": "hlavná", "km": 30.1, "needs_new_path": True,
                       "request_window": [(now + timedelta(hours=1)).isoformat(),
                                          (now + timedelta(hours=2)).isoformat()],
                       "stations": [{"name": "Čadca", "type": "station", "km": 0, "arr": None,
                                     "dep": (now + timedelta(hours=1)).isoformat()},
                                    {"name": "Žilina", "type": "station", "km": 30.1,
                                     "arr": (now + timedelta(hours=2)).isoformat(), "dep": None}]},
        crew={"driver": "Ján FIKTÍVNY", "start": now.isoformat(), "limit": (now + timedelta(hours=13)).isoformat(),
              "end_p50": (now + timedelta(hours=4)).isoformat(), "end_p90": (now + timedelta(hours=5)).isoformat(),
              "left_min": 780, "status": "ORANŽOVÁ", "relieve_by": (now + timedelta(hours=12, minutes=30)).isoformat()},
        reasons=["Výluka Krásno nad Kysucou – Čadca (FIKTÍVNE)"], trigger="test", data_age={"position_min": 3.0},
        state="nove")
    values.update(kw)
    with session_scope() as s:
        r = Recommendation(**values)
        s.add(r)
        s.flush()
        return r.id
