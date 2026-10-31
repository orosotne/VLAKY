"""Testy plánovača na reálnej sieti SR (OSM) s fiktívnymi vlakmi a výlukami."""
from datetime import timedelta

import pytest

from vlaky.hranice import find_border, seed_borders
from vlaky.models import Closure, Driver, DriverShift, Handover, ParameterSet, Path, PositionReport, Train
from vlaky.planovanie.planovac import (POKRACOVAT, closure_windows, current_params, evaluate_all,
                                       evaluate_train, merge_segments)
from vlaky.rozhodovanie import Closure as DClosure, Segment
from vlaky.siet.graf import TrainSpec, network


def _train(session, now, **kw):
    data = dict(number="54321", run_date=now.date().isoformat(), origin="Čadca", destination="Košice",
                traction="E", gauge="1435", vmax_kmh=100, status="planovany",
                ready_utc=now + timedelta(minutes=30), deadline_utc=now + timedelta(hours=12))
    data.update(kw)
    t = Train(**data)
    session.add(t)
    session.flush()
    return t


def test_trasa_cadca_kosice_cez_zilinu_a_poprad():
    r = network().route("Čadca", "Košice", TrainSpec())
    names = [s.name for s, _, _ in r.stations]
    assert 260 < r.length_m / 1000 < 290
    for st in ("Žilina", "Vrútky", "Poprad-Tatry", "Spišská Nová Ves", "Košice"):
        assert st in names
    assert names.index("Žilina") < names.index("Poprad-Tatry") < names.index("Košice")


def test_vyluka_pokryva_obe_kolaje_a_elektricky_vlak_nema_obchadzku():
    net = network()
    sec = net.closure_edges("Liptovský Mikuláš", "Poprad-Tatry")
    ra, rb = net.resolve("Čadca", TrainSpec()), net.resolve("Košice", TrainSpec())
    assert net.shortest(ra[1], rb[1], TrainSpec(), banned=sec) is None
    rm, rk = net.resolve("Čadca", TrainSpec(traction="M")), net.resolve("Košice", TrainSpec(traction="M"))
    alt = net.shortest(rm[1], rk[1], TrainSpec(traction="M"), banned=sec)
    assert alt is not None and "Zvolen" in " ".join(s.name for s, _, _ in alt.stations)


def test_siroky_rozchod_srt():
    r = network().route("Maťovce ŠRT", "Haniska pri Košiciach", TrainSpec(gauge="1520"))
    assert r is not None and 70 < r.length_m / 1000 < 100


def test_bez_vyluky_vypravit_zelena(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now)
    session.add(Path(train_id=t.id, train_number=t.number, status="pridelena",
                     departure_utc=fixed_now + timedelta(minutes=30)))
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ"
    assert rec.path_proposal["stations"][0]["name"] == "Čadca"
    assert rec.path_proposal["stations"][-1]["name"] == "Košice"
    assert not rec.path_proposal["needs_new_path"]


def test_vyluka_na_trase_vynuti_cakanie_alebo_odstavenie(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now)
    session.add(Path(train_id=t.id, train_number=t.number, status="pridelena",
                     departure_utc=fixed_now + timedelta(minutes=30)))
    session.add(Closure(kind="uplna", from_station="Liptovský Mikuláš", to_station="Poprad-Tatry",
                        start_utc=fixed_now + timedelta(hours=3), end_utc=fixed_now + timedelta(hours=7),
                        status="overene"))
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision in ("POČKAŤ", "ODSTAVIŤ")
    assert rec.path_proposal["needs_new_path"]
    from datetime import datetime

    depart = datetime.fromisoformat(rec.chosen["depart"])
    # úsek Liptovský Mikuláš – Poprad-Tatry smie byť obsadený až po konci výluky + 30 min
    assert depart >= fixed_now + timedelta(hours=7, minutes=30) - timedelta(hours=3)
    assert any("Liptovský Mikuláš – Poprad-Tatry" in r for r in rec.reasons)


def test_vypnute_trakcne_vedenie_neobmedzuje_motorovy(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now, number="111", origin="Plaveč", destination="Košice", traction="M")
    session.add(Path(train_id=t.id, train_number=t.number, status="pridelena",
                     departure_utc=fixed_now + timedelta(minutes=30)))
    session.add(Closure(kind="trakcne_vedenie", from_station="Prešov", to_station="Kysak",
                        start_utc=fixed_now, end_utc=fixed_now + timedelta(hours=10), status="overene",
                        electric_only=True))
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision == "VYPRAVIŤ"


def test_bez_pridelenej_trasy_treba_objednat(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now)
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.path_proposal["needs_new_path"] is True
    assert any("pridelenú trasu" in r for r in rec.reasons)


def test_rusnovodic_blizko_13h(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now, status="v_jazde", planned_departure_utc=fixed_now - timedelta(minutes=10))
    d = Driver(name="R")
    session.add(d)
    session.flush()
    session.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=fixed_now - timedelta(hours=11),
                            status="prebieha"))
    net = network()
    r = net.route("Čadca", "Košice", TrainSpec())
    lon, lat = net.geometry(r)[3]
    session.add(PositionReport(train_id=t.id, train_number=t.number, ts_utc=fixed_now, lat=lat, lon=lon,
                               source="gps"))
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision in (POKRACOVAT, "ESKALOVAŤ", "POČKAŤ", "ODSTAVIŤ")
    assert rec.crew["status"] == "ČERVENÁ"
    assert rec.priority == "KRITICKÉ"
    assert rec.color != "ZELENÁ"


def test_idúci_vlak_bez_polohy_nie_je_zeleny(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now, status="v_jazde", planned_departure_utc=fixed_now - timedelta(minutes=30))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.color != "ZELENÁ"
    assert any("čerstvé dáta o polohe" in r for r in rec.reasons)


def test_preberanie_na_hranici_posunie_pripravenost(session, fixed_now):
    seed_borders(session)
    bc = find_border(session, "Čadca")
    t = _train(session, fixed_now, entry_border_id=bc.id, status="na_ceste_k_hranici")
    session.add(Handover(train_id=t.id, border_id=bc.id, direction="vstup", status="ocakavany",
                         avizo_eta_utc=fixed_now + timedelta(hours=2)))
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    from datetime import datetime

    depart = datetime.fromisoformat(rec.chosen["depart"])
    assert depart >= fixed_now + timedelta(hours=2, minutes=bc.dwell_p50_min) - timedelta(minutes=1)
    assert any("Preberanie" in r for r in rec.reasons)


def test_neznama_stanica_eskalovat(session, fixed_now):
    t = _train(session, fixed_now, origin="Neexistujúca stanica XYZ")
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision == "ESKALOVAŤ" and rec.color == "ČERVENÁ"


def test_fingerprint_neduplikuje(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now)
    r1 = evaluate_train(session, t.id, fixed_now, "a")
    r2 = evaluate_train(session, t.id, fixed_now + timedelta(minutes=1), "b")
    assert r1.id == r2.id


def test_parametre_z_db(session, fixed_now):
    session.add(ParameterSet(values={"crew_max_duty_min": 720, "buffer_before_closure_min": "45"},
                             valid_from=fixed_now - timedelta(minutes=1)))
    session.flush()
    p = current_params(session)
    assert p.crew_max_duty_min == 720 and p.buffer_before_closure_min == 45


def test_denne_opakovana_vyluka_okna(session, fixed_now):
    session.add(Closure(kind="uplna", from_station="Žilina", to_station="Púchov",
                        start_utc=fixed_now, end_utc=fixed_now + timedelta(days=3), daily_from="08:00",
                        daily_to="14:00", status="overene"))
    session.flush()
    wins = closure_windows(session, fixed_now, fixed_now + timedelta(days=2))
    assert len(wins) >= 2
    for w in wins:
        assert (w.end - w.start) == timedelta(hours=6)


def test_zlucenie_useku_bod_bez_navratu():
    c = DClosure(100, 200)
    segs = [Segment("A – B", 10, 1, 3, (), True), Segment("B – C", 10, 1, 3, (), True),
            Segment("C – D", 10, 1, 3, (c,), False), Segment("D – E", 10, 1, 3, (), True)]
    out = merge_segments(segs)
    # úsek s výlukou sa predĺži dozadu po dopravňu, kde sa dá čakať (A)
    assert out[0].closures == (c,) and out[0].can_wait_before
    assert out[0].name.startswith("A") and len(out) == 2


def test_evaluate_all(session, fixed_now):
    seed_borders(session)
    _train(session, fixed_now, number="1")
    _train(session, fixed_now, number="2", origin="Kúty", destination="Štúrovo")
    recs = evaluate_all(session, fixed_now, "test")
    assert len(recs) == 2


def test_casy_z_pridelenej_trasy(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now)
    dep = fixed_now + timedelta(minutes=30)
    session.add(Path(train_id=t.id, train_number=t.number, status="pridelena", departure_utc=dep,
                     points=[{"station": "Čadca", "dep": dep.isoformat()},
                             {"station": "Žilina", "arr": (dep + timedelta(minutes=40)).isoformat(),
                              "dep": (dep + timedelta(minutes=45)).isoformat()},
                             {"station": "Košice", "arr": (dep + timedelta(minutes=300)).isoformat()}]))
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    from datetime import datetime

    arr = datetime.fromisoformat(rec.chosen["arrival_p50"])
    # podľa trasy ŽSR 300 min (graf odhaduje ~180 min) → príchod približne o 5 h po odchode
    assert timedelta(minutes=280) <= arr - dep <= timedelta(minutes=340)
    assert any("pridelenej trasy ŽSR" in r for r in rec.reasons)


def test_navrh_posunu_nastupu_rusnovodica(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now, ready_utc=fixed_now + timedelta(hours=1))
    d = Driver(name="R2")
    session.add(d)
    session.flush()
    # nástup naplánovaný o 30 min, ale vlak bez trasy odíde najskôr o 6 h (lehota P5)
    session.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=fixed_now + timedelta(minutes=30),
                            status="planovana"))
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert any("Posunúť nástup rušňovodiča" in r for r in rec.reasons)
    assert "suggested_start" in rec.crew


def test_koncepty_sprav(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now, customer="Zákazník Z (fiktívny)", weight_t=1500, length_m=500)
    rec = evaluate_train(session, t.id, fixed_now, "test")
    drafts = rec.path_proposal["drafts"]
    assert "objednávka ad hoc trasy" in drafts["zsr_path_request"] and "54321" in drafts["zsr_path_request"]
    assert "Čadca" in drafts["zsr_path_request"] and "Košice" in drafts["zsr_path_request"]
    assert "Zákazník Z" in drafts["customer"] and "NEODOSIELA" in drafts["customer"]
