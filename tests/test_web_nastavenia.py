"""Web: nastavenia parametrov – nová ParameterSet s platnosťou, validácia, audit, oprávnenia."""
from __future__ import annotations

import re

from sqlalchemy import func, select

from test_web_spolocne import csrf, login_as

pytest_plugins = ["test_web_spolocne"]


def _form_values(html: str) -> dict:
    return dict(re.findall(r'<input id="par-[^"]+" name="([a-z_0-9]+)" type="number" value="([^"]*)"', html))


def test_ulozenie_parametrov(client):
    from vlaky.db import session_scope
    from vlaky.models import AuditLog, ParameterSet
    from vlaky.web.common import current_params, param_defaults

    login_as(client, "veduci", "veduci")
    page = client.get("/nastavenia").text
    values = _form_values(page)
    assert set(values) == set(param_defaults())  # všetky polia Params
    assert values["crew_max_duty_min"] == "780.0"
    assert "Tieňová prevádzka" in page and "dispecer1@dopravca.example" in page
    token = csrf(client, "/nastavenia")
    data = {**values, "crew_buffer_min": "75", "cost_stuck": "3500", "csrf_token": token, "note": "kalibrácia (test)"}
    r = client.post("/nastavenia", data=data, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        ps = s.scalar(select(ParameterSet).order_by(ParameterSet.id.desc()))
        assert ps.values["crew_buffer_min"] == 75.0 and ps.values["cost_stuck"] == 3500.0
        assert ps.values["samples"] == 4000 and isinstance(ps.values["samples"], int)
        assert ps.created_by == "veduci" and ps.note == "kalibrácia (test)"
        assert current_params(s)["crew_buffer_min"] == 75.0
        a = s.scalar(select(AuditLog).where(AuditLog.action == "parameters_update"))
        assert a.detail["changes"]["crew_buffer_min"] == [60.0, 75.0] and "cost_stuck" in a.detail["changes"]
    # bez zmeny sa nová sada neuloží
    same = client.post("/nastavenia", data={**data, "csrf_token": token}, follow_redirects=True)
    assert "nezmenili" in same.text
    with session_scope() as s:
        assert s.scalar(select(func.count(ParameterSet.id))) == 1


def test_validacia_parametrov(client):
    from vlaky.db import session_scope
    from vlaky.models import ParameterSet

    login_as(client, "veduci", "veduci")
    values = _form_values(client.get("/nastavenia").text)
    token = csrf(client, "/nastavenia")
    r = client.post("/nastavenia", data={**values, "p_on_time_green": "1.5", "p_hold_green": "0.5",
                                         "p_hold_red": "0.1", "csrf_token": token, "note": ""})
    assert r.status_code == 422
    assert "najviac 1" in r.text and "Dôvod zmeny: povinné pole" in r.text
    assert "P(zadržania) nesmie byť vyšší" in r.text
    with session_scope() as s:
        assert s.scalar(select(ParameterSet)) is None


def test_planovana_sada_a_opravnenia(client):
    from vlaky.db import session_scope
    from vlaky.models import ParameterSet
    from vlaky.web.common import current_params

    login_as(client, "veduci", "veduci")
    values = _form_values(client.get("/nastavenia").text)
    token = csrf(client, "/nastavenia")
    r = client.post("/nastavenia", follow_redirects=False, data={**values, "crew_buffer_min": "90",
                                                                  "valid_from": "2099-01-01T00:00",
                                                                  "csrf_token": token, "note": "od budúceho roka"})
    assert r.status_code == 303
    with session_scope() as s:
        valid_from = s.scalar(select(ParameterSet)).valid_from.replace(tzinfo=None)
        assert valid_from.isoformat() == "2098-12-31T23:00:00"  # 1. 1. 2099 00:00 CET
        assert current_params(s)["crew_buffer_min"] == 60.0  # ešte neplatí
    assert "plánovaná" in client.get("/nastavenia").text


def test_dispecer_nema_pristup(client):
    login_as(client, "dispecer")
    assert client.get("/nastavenia").status_code == 403
    token = csrf(client, "/vlaky")
    assert client.post("/nastavenia", data={"csrf_token": token}, follow_redirects=False).status_code == 403
