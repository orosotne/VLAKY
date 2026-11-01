"""Regresné testy k zlúčeniu opráv druhej revízie (súvislosti medzi oblasťami).

- príchod vlaku na vstupnú hranicu hlásený cez /api/udalosti-jazdy zapíše začiatok státia na koľajach ŽSR
  (Train.standing_since_utc) rovnako ako zápis príchodu vo webe – limit 18 h beží od skutočného príchodu,
- mapa a nástenka (train_rows) ukážu rušňovodiča v službe, nie neskoršie naplánované vystriedanie
  (rovnaké pravidlo ako planovac.active_shift).
"""
from __future__ import annotations

from datetime import timedelta

from test_web_spolocne import login_as, make_train

pytest_plugins = ["test_web_spolocne"]

TOKEN = {"Authorization": "Bearer test-token"}


def test_prichod_na_hranicu_z_udalosti_jazdy_zapise_zaciatok_statia(client):
    from sqlalchemy import select

    from vlaky.db import session_scope
    from vlaky.models import BorderCrossing, Handover, Train

    with session_scope() as s:
        border_id = s.scalar(select(BorderCrossing).where(BorderCrossing.code == "CADCA")).id
    train_id = make_train(number="44901", status="na_ceste_k_hranici", entry_border_id=border_id,
                          origin="Čadca", destination="Žilina")
    with session_scope() as s:
        s.add(Handover(train_id=train_id, border_id=border_id, direction="vstup", status="ocakavany"))
    body = {"train_number": "44901", "station": "Cadca", "event": "prichod", "ts": "2026-10-31T10:15:00+01:00"}
    r = client.post("/api/udalosti-jazdy", json=body, headers=TOKEN)
    assert r.status_code == 200, r.text
    assert r.json()["handover_id"] is not None
    with session_scope() as s:
        t = s.get(Train, train_id)
        assert t.standing_since_utc is not None
        assert t.standing_since_utc.replace(tzinfo=None).isoformat() == "2026-10-31T09:15:00"


def test_mapa_ukaze_rusnovodica_v_sluzbe_nie_naplanovane_vystriedanie(client, fixed_now):
    from vlaky.db import session_scope
    from vlaky.models import Driver, DriverShift

    train_id = make_train(number="44902", status="v_jazde")
    with session_scope() as s:
        on_duty = Driver(name="Rušňovodič v službe (FIKTÍVNY)")
        relief = Driver(name="Vystriedanie (FIKTÍVNE)")
        s.add_all([on_duty, relief])
        s.flush()
        s.add_all([
            DriverShift(driver_id=on_duty.id, train_id=train_id, start_utc=fixed_now - timedelta(hours=12),
                        status="prebieha"),
            DriverShift(driver_id=relief.id, train_id=train_id, start_utc=fixed_now + timedelta(minutes=40),
                        status="planovana"),
        ])
    login_as(client, "integ1")
    row = next(r for r in client.get("/api/vlaky").json()["vlaky"] if r["id"] == train_id)
    assert row["driver"]["name"] == "Rušňovodič v službe (FIKTÍVNY)"
    assert row["driver"]["left_min"] is not None and row["driver"]["left_min"] <= 60
