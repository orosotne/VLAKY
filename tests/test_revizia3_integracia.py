"""Regresné testy k zlúčeniu opráv tretej revízie (súvislosti medzi oblasťami)."""
from __future__ import annotations

from datetime import timedelta

from test_web_spolocne import make_train

pytest_plugins = ["test_web_spolocne"]


def test_hlavicka_odkazu_rusnovodica_ukaze_zmenu_v_sluzbe_nie_vystriedanie(client, fixed_now):
    """Po nástupe prvého rušňovodiča (naplánované vystriedanie o 7 h) ukáže hlavička aplikácie v mobile zmenu,
    ktorá prebieha – nie neskoršie naplánované vystriedanie (planovac.link_shift)."""
    from vlaky.db import session_scope
    from vlaky.models import Driver, DriverShift, Train
    from vlaky.web.rusnovodic import make_driver_token

    train_id = make_train(number="47801", status="v_jazde")
    with session_scope() as s:
        first = Driver(name="Prvý rušňovodič (FIKTÍVNY)")
        relief = Driver(name="Vystriedanie (FIKTÍVNE)")
        s.add_all([first, relief])
        s.flush()
        s.add_all([
            DriverShift(driver_id=first.id, train_id=train_id, start_utc=fixed_now - timedelta(hours=1),
                        status="prebieha"),
            DriverShift(driver_id=relief.id, train_id=train_id, start_utc=fixed_now + timedelta(hours=7),
                        status="planovana"),
        ])
        token = make_driver_token(s.get(Train, train_id))
    info = client.post("/api/rusnovodic/info", json={"t": token}).json()
    assert info["driver_name"] == "Prvý rušňovodič (FIKTÍVNY)" and info["shift_status"] == "prebieha"
