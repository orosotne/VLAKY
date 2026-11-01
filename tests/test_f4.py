"""Fáza F4: kalibrácia z histórie a návrh nasadenia rušňovodičov (CP-SAT)."""
from datetime import timedelta


from vlaky.hranice import find_border, seed_borders
from vlaky.models import Driver, DriverShift, Handover, ParameterSet, Train
from vlaky.planovanie.kalibracia import apply_calibration, delay_defaults, propose_calibration
from vlaky.planovanie.nasadenie import propose_driver_assignment


def test_kalibracia_doby_preberania(session, fixed_now):
    seed_borders(session)
    bc = find_border(session, "Čadca")
    for i in range(30):
        t = Train(number=f"K{i}", origin="Čadca", destination="Žilina", status="ukonceny")
        session.add(t)
        session.flush()
        arr = fixed_now - timedelta(days=10, hours=i)
        session.add(Handover(train_id=t.id, border_id=bc.id, arrived_utc=arr,
                             taken_over_utc=arr + timedelta(minutes=40 + i), status="prevzaty"))
    session.flush()
    prop = propose_calibration(session, fixed_now)
    b = next(x for x in prop["borders"] if x["code"] == "CADCA")
    assert b["n"] == 30 and b["enough"] and 50 <= b["p50"] <= 60 and b["p90"] > b["p50"]
    applied = apply_calibration(session, prop, "veduci")
    assert applied["borders"][0]["code"] == "CADCA"
    assert session.get(type(bc), bc.id).dwell_p50_min == b["p50"]


def test_kalibracia_bez_historie(session, fixed_now):
    prop = propose_calibration(session, fixed_now)
    assert prop["notes"] and not prop["borders"]


def test_kalibrovane_zdrzania_z_parameterset(session, fixed_now):
    session.add(ParameterSet(values={"delay_p50_min_per_100km": 7, "delay_p90_min_per_100km": 30},
                             valid_from=fixed_now - timedelta(days=1)))
    session.flush()
    assert delay_defaults(session) == (7.0, 30.0)


def test_nasadenie_rusnovodicov(session, fixed_now):
    seed_borders(session)
    t1 = Train(number="N1", origin="Čadca", destination="Žilina", status="planovany",
               ready_utc=fixed_now + timedelta(hours=1))
    t2 = Train(number="N2", origin="Kúty", destination="Štúrovo", status="planovany",
               ready_utc=fixed_now + timedelta(hours=1, minutes=30))
    d1 = Driver(name="A", home_station="Čadca")
    d2 = Driver(name="B", home_station="Kúty")
    session.add_all([t1, t2, d1, d2])
    session.flush()
    res = propose_driver_assignment(session, fixed_now)
    pairs = {(a["train"], a["driver"]) for a in res["assignments"]}
    assert pairs == {("N1", "A"), ("N2", "B")}  # bez zbytočných presunov
    assert all(a["duty_h"] <= 13 for a in res["assignments"])


def test_nasadenie_rešpektuje_odpocinok(session, fixed_now):
    t = Train(number="N3", origin="Čadca", destination="Žilina", status="planovany",
              ready_utc=fixed_now + timedelta(hours=2))
    d = Driver(name="C", home_station="Čadca")
    session.add_all([t, d])
    session.flush()
    # rušňovodič práve skončil zmenu pred 3 h → nemá 11 h odpočinku
    session.add(DriverShift(driver_id=d.id, start_utc=fixed_now - timedelta(hours=12),
                            planned_end_utc=fixed_now - timedelta(hours=3), status="prebieha"))
    session.flush()
    res = propose_driver_assignment(session, fixed_now)
    assert not res["assignments"] and "odpočinku" in res["unassigned"][0]["reason"]
