"""Regresné testy k 4. revízii – slovenské texty plánovača a správ (QU3, oblasť P4 „poistky“).

Čísla s desatinnou čiarkou („zostáva 0,8 h“, „meškanie približne 1,5 h“, „110,1 km“), limity bez zbytočných
desatinných miest („13 h“, „18 h“), tisíce oddelené medzerou („1 500 t“) a správny pád („Preberanie v PPS Čadca“).
Všetky vlaky, zákazníci a adresy sú fiktívne.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from vlaky.cas import fmt
from vlaky.hranice import find_border, seed_borders
from vlaky.models import Closure, Handover, Order, Path, Train
from vlaky.notifikacie.texty import order_proposal_text, recommendation_body
from vlaky.planovanie import planovac
from vlaky.planovanie.planovac import evaluate_train
from vlaky.rozhodovanie import VYPRAVIT, Evaluation


def test_qu3_limit_18_h_desatinna_ciarka(session, fixed_now):
    """[QU3] Vlak stojí 17,2 h, trasa je zatvorená: dôvod „do limitu 18 h (PPŽS) zostáva 0,8 h“ a eskalácia
    „žiadna bezpečná varianta do limitu 18 h státia (zostáva 0,8 h)“ – nie „0.8 h“ (web, e-mail, SMS)."""
    seed_borders(session)
    since = fixed_now - timedelta(hours=17, minutes=12)
    t = Train(number="79301", run_date=since.date().isoformat(), origin="Čadca", destination="Košice",
              traction="E", gauge="1435", vmax_kmh=100, status="odstaveny", ready_utc=since,
              standing_since_utc=since, deadline_utc=fixed_now + timedelta(hours=30))
    session.add(t)
    session.add(Closure(kind="uplna", from_station="Liptovský Mikuláš", to_station="Poprad-Tatry", status="overene",
                        start_utc=fixed_now - timedelta(hours=1), end_utc=fixed_now + timedelta(hours=5)))
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision == "ESKALOVAŤ", (rec.decision, rec.summary)
    assert "do limitu 18 h státia (zostáva 0,8 h)" in rec.summary, rec.summary
    assert any(f"Vlak stojí od {fmt(since)} – do limitu 18 h (PPŽS) zostáva 0,8 h" in r for r in rec.reasons), \
        rec.reasons
    body = recommendation_body(rec)
    assert "0.8 h" not in body and "zostáva 0,8 h" in body


def test_qu3_preberanie_v_pps(session, fixed_now):
    """[QU3] Dôvod k preberaniu na hranici: „Preberanie v PPS Čadca: avízo príchodu …“ – nie „Preberanie na Čadca“."""
    seed_borders(session)
    bc = find_border(session, "Čadca")
    t = Train(number="79302", run_date=fixed_now.date().isoformat(), origin="Čadca", destination="Košice",
              traction="E", gauge="1435", vmax_kmh=100, status="na_ceste_k_hranici", entry_border_id=bc.id,
              deadline_utc=fixed_now + timedelta(hours=20))
    session.add(t)
    session.flush()
    session.add(Path(train_id=t.id, train_number=t.number, status="pridelena",
                     departure_utc=fixed_now + timedelta(hours=4)))
    avizo = fixed_now + timedelta(hours=2)
    session.add(Handover(train_id=t.id, border_id=bc.id, direction="vstup", status="ocakavany", avizo_eta_utc=avizo))
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "avízo")
    assert any(r.startswith(f"Preberanie v PPS {bc.handover_station}: avízo príchodu {fmt(avizo)}")
               for r in rec.reasons), rec.reasons
    assert not any("Preberanie na " in r for r in rec.reasons)


def test_qu3_cisla_v_navrhu_objednavky_a_koncepte_pre_zakaznika(session, fixed_now):
    """[QU3] Návrh prepravy k objednávke („Hmotnosť: 1 500 t“, „110,1 km“) a koncept pre zákazníka („meškanie
    približne 1,5 h“) – čísla po slovensky ako na webe."""
    order = Order(customer="Zákazník FIKTÍVNY", origin="Žilina", destination="Košice", weight_t=1500.0,
                  length_m=550.0, earliest_departure_utc=fixed_now, deadline_utc=fixed_now + timedelta(hours=8))
    plan = {"decision": "VYPRAVIŤ", "explanation": [],
            "routes": [{"name": "hlavná trasa", "km": 110.1, "minutes": 95, "stations": ["Žilina", "Kraľovany",
                                                                                         "Košice"]}]}
    _subject, text = order_proposal_text(order, plan)
    assert "Hmotnosť: 1 500 t" in text and "Dĺžka: 550 m" in text, text
    assert "110,1 km" in text and "110.1" not in text
    train = SimpleNamespace(number="79303", origin="Žilina", destination="Košice", customer="Zákazník FIKTÍVNY",
                            locomotive=None, traction="E", vmax_kmh=100.0, weight_t=1500.0, length_m=550.0, rid=False)
    deadline = fixed_now + timedelta(hours=3)
    ev = Evaluation(route="Žilina – Košice", depart=30.0, kind=VYPRAVIT, p_on_time=0.0, p_hold=0.0, p_stuck=0.0,
                    p_crew_relief=0.0, arrival_p50=270.0, arrival_p90=290.0, duty_p90=0.0, expected_cost=0.0)
    pp = {"stations": [{"name": "Žilina", "type": "station", "dep": fixed_now.isoformat(), "arr": None},
                       {"name": "Košice", "type": "station", "dep": None, "arr": fixed_now.isoformat()}]}
    drafts = planovac._drafts(train, VYPRAVIT, ev, pp, fixed_now, deadline)
    assert "očakávame meškanie približne 1,5 h" in drafts["customer"], drafts["customer"]


def test_qu3_pomocne_zapisy_cisel():
    """[QU3] Spoločné pomocné funkcie textov: desatinná čiarka, hodiny bez „.0“, tisíce s medzerou."""
    from vlaky.notifikacie.texty import dec, hours, number

    assert dec(0.8) == "0,8" and dec(1.0833, 2) == "1,08" and dec(None) == ""
    assert hours(13.0) == "13" and hours(12.5) == "12,5" and hours(18) == "18"
    assert number(1500.0) == "1 500" and number(12.5) == "12,5" and number(500) == "500"
