"""Regresné testy k nálezom revízie plánovača (výluky, limity PPŽS, idúce vlaky, deduplikácia)."""
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from vlaky.hranice import find_border, seed_borders
from vlaky.models import (Closure, Driver, DriverShift, Handover, KeyValue, Order, Path, PositionReport,
                          Recommendation, Train)
from vlaky.planovanie import planovac
from vlaky.planovanie.planovac import closure_windows, evaluate_all, evaluate_train, plan_order
from vlaky.siet.graf import TrainSpec, network


def _train(session, now, path_at=30, **kw):
    data = dict(number="54321", run_date=now.date().isoformat(), origin="Čadca", destination="Košice",
                traction="E", gauge="1435", vmax_kmh=100, status="planovany",
                ready_utc=now + timedelta(minutes=30), deadline_utc=now + timedelta(hours=14))
    data.update(kw)
    t = Train(**data)
    session.add(t)
    session.flush()
    if path_at is not None:
        session.add(Path(train_id=t.id, train_number=t.number, status="pridelena",
                         departure_utc=now + timedelta(minutes=path_at)))
        session.flush()
    return t


def _closure(session, a, b, start, end, kind="uplna", **kw):
    c = Closure(kind=kind, from_station=a, to_station=b, start_utc=start, end_utc=end, status="overene", **kw)
    session.add(c)
    session.flush()
    return c


def _depart(rec):
    return datetime.fromisoformat(rec.chosen["depart"])


# ------------------------------------------------------------------ denné okná výluk
def test_denne_okno_do_polnoci_nezastavi_planovanie(session, fixed_now):
    """'22:00–24:00' (validácia ho prijme) nesmie zhodiť prepočet všetkých vlakov."""
    seed_borders(session)
    t1 = _train(session, fixed_now, number="1")
    t2 = _train(session, fixed_now, number="2", origin="Kúty", destination="Štúrovo")
    _closure(session, "Zvolen", "Banská Bystrica", fixed_now, fixed_now + timedelta(days=3),
             daily_from="22:00", daily_to="24:00")
    wins = closure_windows(session, fixed_now, fixed_now + timedelta(days=1))
    assert wins and all((w.end - w.start) == timedelta(hours=2) for w in wins if not w.note)
    recs = evaluate_all(session, fixed_now, "test")
    assert {r.train_id for r in recs} == {t1.id, t2.id}
    assert session.info["evaluate_errors"] == []


def test_neplatne_denne_okno_sa_pocita_cele(session, fixed_now):
    c = _closure(session, "Zvolen", "Banská Bystrica", fixed_now, fixed_now + timedelta(days=1),
                 daily_from="25:00", daily_to="06:00")
    wins = closure_windows(session, fixed_now, fixed_now + timedelta(days=2))
    assert len(wins) == 1 and wins[0].closure_id == c.id and "celé obdobie" in wins[0].note
    assert wins[0].start == fixed_now and wins[0].end == fixed_now + timedelta(days=1)


def test_chyba_jedneho_vlaku_nenecha_stare_zelene(session, fixed_now, monkeypatch):
    seed_borders(session)
    t1 = _train(session, fixed_now, number="1")
    t2 = _train(session, fixed_now, number="2", origin="Kúty", destination="Štúrovo")
    first = {r.train_id: r for r in evaluate_all(session, fixed_now, "test")}
    assert first[t1.id].color == "ZELENÁ"
    real = planovac.evaluate_train

    def broken(session_, tid, now=None, trigger=""):
        if tid == t1.id:
            raise ValueError("pokazený záznam")
        return real(session_, tid, now, trigger)

    monkeypatch.setattr(planovac, "evaluate_train", broken)
    recs = {r.train_id: r for r in evaluate_all(session, fixed_now + timedelta(minutes=5), "test")}
    assert session.info["evaluate_errors"][0]["train_id"] == t1.id
    assert recs[t1.id].decision == "ESKALOVAŤ" and recs[t1.id].color == "ČERVENÁ"
    assert "nedokázal prepočítať" in recs[t1.id].summary
    assert recs[t2.id].decision != "ESKALOVAŤ"
    latest = session.scalar(select(Recommendation).where(Recommendation.train_id == t1.id)
                            .order_by(Recommendation.id.desc()))
    assert latest.decision == "ESKALOVAŤ"


# ------------------------------------------------------------------ umiestnenie výluk na sieť
def test_vyluka_v_jednej_stanici_zastavi_vlak(session, fixed_now):
    seed_borders(session)
    assert network().closure_edges("Kraľovany", "Kraľovany")
    t = _train(session, fixed_now)
    _closure(session, "Kraľovany", "Kraľovany", fixed_now - timedelta(hours=1), fixed_now + timedelta(hours=10),
             kind="mimoriadnost")
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert not (rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ")
    assert any("Kraľovany" in r for r in rec.reasons)


def test_skratka_vyhybne_sa_najde(session, fixed_now):
    """E-mailová časť ukladá kanonický skrátený názov „Výh. Paludza“."""
    net = network()
    assert net.find_station("Výh. Paludza") is not None
    assert net.find_station("výhybňa Paludza") is not None
    seed_borders(session)
    t = _train(session, fixed_now)
    _closure(session, "Liptovská Teplá", "Výh. Paludza", fixed_now - timedelta(hours=1),
             fixed_now + timedelta(hours=10))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert not (rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ")
    assert any("Paludza" in r for r in rec.reasons)


def test_vyluka_na_sirokom_rozchode(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now, number="88001", origin="Maťovce ŠRT", destination="Haniska pri Košiciach",
               traction="M", gauge="1520", vmax_kmh=80)
    assert network().closure_edges("Slančík ŠRT", "Haniska pri Košiciach")
    _closure(session, "Slančík ŠRT", "Haniska pri Košiciach", fixed_now - timedelta(hours=1),
             fixed_now + timedelta(hours=10))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert not (rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ")


def test_neznamy_usek_vyluky_nie_je_zelena(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now)
    _closure(session, "Neexistujúca Dopravňa", "Iná Neexistujúca", fixed_now, fixed_now + timedelta(hours=10))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.color != "ZELENÁ"
    assert any("nepodarilo umiestniť" in r for r in rec.reasons)


def test_vzdialena_neumiestnena_vyluka_vlaku_nevadi(session, fixed_now):
    """Neznámy druhý koniec, ale známa dopravňa ďaleko od trasy → vlak sa to netýka."""
    seed_borders(session)
    t = _train(session, fixed_now)
    _closure(session, "Štúrovo", "Neexistujúca Dopravňa", fixed_now, fixed_now + timedelta(hours=10))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ"


def test_vyluka_odbocnej_trate_nezastavi_hlavnu(session, fixed_now):
    seed_borders(session)
    net = network()
    r = net.route("Čadca", "Košice", TrainSpec())
    for a, b in [("Vrútky", "Martin"), ("Žilina", "Rajec"), ("Kraľovany", "Dolný Kubín"),
                 ("Poprad-Tatry", "Kežmarok"), ("Kysak", "Prešov")]:
        assert not (net.closure_edges(a, b) & set(r.edges)), (a, b)
    t = _train(session, fixed_now)
    _closure(session, "Vrútky", "Martin", fixed_now - timedelta(hours=1), fixed_now + timedelta(hours=8))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision == "VYPRAVIŤ"
    assert not any("Vrútky" in x for x in rec.reasons)


def test_vyluka_na_hlavnej_trati_stale_plati(session, fixed_now):
    net = network()
    r = net.route("Čadca", "Košice", TrainSpec())
    for a, b in [("Liptovský Mikuláš", "Poprad-Tatry"), ("Žilina", "Vrútky"), ("Lisková", "Liptovská Teplá"),
                 ("Kysak", "Košice")]:
        assert net.closure_edges(a, b) & set(r.edges), (a, b)


# ------------------------------------------------------------------ výluky v budúcnosti
def test_vlak_o_tri_dni_vidi_vyluku(session, fixed_now):
    seed_borders(session)
    ready = fixed_now + timedelta(days=3)
    t = _train(session, fixed_now, path_at=3 * 24 * 60, ready_utc=ready, deadline_utc=ready + timedelta(hours=12))
    _closure(session, "Liptovský Mikuláš", "Poprad-Tatry", ready - timedelta(hours=1), ready + timedelta(hours=10))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert not (rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ")
    assert any("Liptovský Mikuláš" in r for r in rec.reasons)


def test_objednavka_o_styri_dni_vidi_vyluku(session, fixed_now):
    seed_borders(session)
    dep = fixed_now + timedelta(days=4)
    o = Order(customer="Z", origin="Čadca", destination="Košice", earliest_departure_utc=dep,
              deadline_utc=dep + timedelta(hours=30))
    session.add(o)
    _closure(session, "Liptovský Mikuláš", "Poprad-Tatry", dep - timedelta(hours=1), dep + timedelta(hours=12))
    plan = plan_order(session, o.id, fixed_now)
    ch = plan["variants"][0]
    through_closure = (ch["route"] == "hlavná trasa" and
                       datetime.fromisoformat(ch["depart"]) < dep + timedelta(hours=8))
    assert not through_closure, plan["explanation"]


# ------------------------------------------------------------------ preberanie na hranici (P90)
def test_preberanie_pocita_s_p90(session, fixed_now):
    seed_borders(session)
    bc = find_border(session, "Čadca")
    bc.dwell_p50_min, bc.dwell_p90_min = 60.0, 150.0
    t = _train(session, fixed_now, path_at=120, status="na_ceste_k_hranici", entry_border_id=bc.id,
               ready_utc=None)
    session.add(Handover(train_id=t.id, border_id=bc.id, direction="vstup", status="ocakavany",
                         avizo_eta_utc=fixed_now + timedelta(hours=1)))
    _closure(session, "Liptovský Mikuláš", "Poprad-Tatry", fixed_now + timedelta(hours=5, minutes=40),
             fixed_now + timedelta(hours=10))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert any("P90" in r and "150" in r for r in rec.reasons)
    assert _depart(rec) >= fixed_now + timedelta(hours=1, minutes=150)
    assert not (rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ")


# ------------------------------------------------------------------ limit 18 h (PPŽS)
def test_limit_18h_sa_rata_od_zaciatku_statia(session, fixed_now):
    seed_borders(session)
    since = fixed_now - timedelta(hours=16)
    t = _train(session, fixed_now, path_at=None, status="odstaveny", ready_utc=since,
               deadline_utc=fixed_now + timedelta(hours=30))
    _closure(session, "Liptovský Mikuláš", "Poprad-Tatry", fixed_now - timedelta(hours=1),
             fixed_now + timedelta(hours=10))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert any("18 h" in r for r in rec.reasons)
    if rec.decision != "ESKALOVAŤ":
        assert _depart(rec) <= since + timedelta(hours=18)


def test_limit_18h_prekroceny_eskalovat(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now, path_at=None, status="odstaveny", ready_utc=fixed_now - timedelta(hours=20),
               deadline_utc=fixed_now + timedelta(hours=30))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision == "ESKALOVAŤ"
    assert any("dlhšie ako 18 h" in r for r in rec.reasons)


# ------------------------------------------------------------------ idúci vlak
def _running(session, now, idx=3, shift=True, **kw):
    data = dict(number="54321", origin="Čadca", destination="Košice", traction="E", gauge="1435", vmax_kmh=100,
                status="v_jazde", planned_departure_utc=now - timedelta(minutes=10),
                deadline_utc=now + timedelta(hours=30))
    data.update(kw)
    t = Train(**data)
    session.add(t)
    session.flush()
    net = network()
    lon, lat = net.geometry(net.route("Čadca", "Košice", TrainSpec()))[idx]
    session.add(PositionReport(train_id=t.id, train_number=t.number, ts_utc=now, lat=lat, lon=lon, source="gps"))
    if shift:
        d = Driver(name="R. Testovací")
        session.add(d)
        session.flush()
        session.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=now - timedelta(hours=1),
                                status="prebieha"))
    session.flush()
    return t


def test_iduci_vlak_odstavit_len_s_novou_trasou(session, fixed_now):
    seed_borders(session)
    t = _running(session, fixed_now)
    _closure(session, "Liptovský Mikuláš", "Poprad-Tatry", fixed_now + timedelta(minutes=40),
             fixed_now + timedelta(hours=6))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    for v in rec.variants + [rec.chosen]:
        if v.get("kind") == "ODSTAVIŤ":
            assert v["needs_new_path"]
            assert datetime.fromisoformat(v["depart"]) >= fixed_now + timedelta(hours=6) - timedelta(seconds=1)


def test_iduci_vlak_bez_zaznamu_rusnovodica_nie_je_zeleny(session, fixed_now):
    seed_borders(session)
    t = _running(session, fixed_now, shift=False)
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.color != "ZELENÁ"
    assert rec.crew.get("unknown_start") and rec.crew["status"] != "ZELENÁ"
    assert any("nástupe rušňovodiča" in r for r in rec.reasons)


def test_poloha_mimo_trasy_nie_je_zelena(session, fixed_now):
    seed_borders(session)
    t = _running(session, fixed_now, number="777", planned_departure_utc=fixed_now - timedelta(hours=2))
    session.execute(delete(PositionReport).where(PositionReport.train_id == t.id))
    session.add(PositionReport(train_id=t.id, train_number=t.number, ts_utc=fixed_now, lat=48.1585, lon=17.1065,
                               source="gps"))  # Bratislava – ďaleko od trasy Čadca – Košice
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.color != "ZELENÁ"
    assert rec.path_proposal["stations"][0]["name"] != "Čadca"  # odhad podľa plánu, nie z východiska


# ------------------------------------------------------------------ čerstvosť schránky
def test_schranka_nikdy_neskontrolovana_nie_je_zelena(session, fixed_now):
    seed_borders(session)
    session.execute(delete(KeyValue).where(KeyValue.key == "mail_last_ok"))
    t = _train(session, fixed_now)
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.color == "ORANŽOVÁ"
    assert any("neskontroloval" in r for r in rec.reasons)


# ------------------------------------------------------------------ deduplikácia odporúčaní
def test_postupny_posun_odchodu_vytvori_nove_odporucanie(session, fixed_now):
    seed_borders(session)
    bc = find_border(session, "Čadca")
    t = _train(session, fixed_now, path_at=None, status="na_ceste_k_hranici", entry_border_id=bc.id,
               ready_utc=None, deadline_utc=fixed_now + timedelta(hours=30))
    ho = Handover(train_id=t.id, border_id=bc.id, direction="vstup", status="ocakavany",
                  avizo_eta_utc=fixed_now + timedelta(hours=7))
    session.add(ho)
    session.flush()
    first = evaluate_train(session, t.id, fixed_now, "avízo")
    first_dep = first.chosen["depart"]
    first.state = "prijate"
    session.flush()
    now = fixed_now
    for k in range(1, 7):  # avízo sa šesťkrát posunie o 25 min (spolu 2,5 h)
        now += timedelta(minutes=15)
        ho.avizo_eta_utc = fixed_now + timedelta(hours=7, minutes=25 * k)
        session.flush()
        evaluate_train(session, t.id, now, "avízo zmena")
    recs = list(session.scalars(select(Recommendation).where(Recommendation.train_id == t.id)
                                .order_by(Recommendation.id)))
    assert len(recs) >= 2
    assert recs[0].state == "prijate" and recs[0].chosen["depart"] == first_dep  # prijaté sa nemení
    drift_last = datetime.fromisoformat(recs[-1].chosen["depart"]) - datetime.fromisoformat(
        recs[-1].chosen.get("baseline_depart") or recs[-1].chosen["depart"])
    assert abs(drift_last) < timedelta(minutes=30)


# ------------------------------------------------------------------ nasadenie rušňovodičov
def test_nasadenie_respektuje_odpocinok_po_ukoncenej_zmene(session, fixed_now):
    from vlaky.planovanie.nasadenie import propose_driver_assignment

    t = Train(number="N3", origin="Čadca", destination="Žilina", status="planovany",
              ready_utc=fixed_now + timedelta(hours=2))
    d = Driver(name="C", home_station="Čadca")
    session.add_all([t, d])
    session.flush()
    session.add(DriverShift(driver_id=d.id, start_utc=fixed_now - timedelta(hours=13),
                            planned_end_utc=fixed_now - timedelta(hours=1),
                            actual_end_utc=fixed_now - timedelta(hours=1), status="ukoncena"))
    session.flush()
    res = propose_driver_assignment(session, fixed_now)
    assert not any(a["driver"] == "C" for a in res["assignments"])
    assert res["unassigned"] and "odpočinku" in res["unassigned"][0]["reason"]


# ------------------------------------------------------------------ rozhodovací modul
def test_odstavenie_s_povinnou_novou_trasou():
    from vlaky.rozhodovanie import ODSTAVIT, Closure as C, Params, Route, Segment, recommend

    seg = (Segment("A – B", 60, 2, 8), Segment("B – C", 60, 2, 8, (C(start=90, end=300),)))
    route = Route("A – C", seg)
    p = Params(samples=300)
    free = recommend([route], now=0, ready=0, deadline=2000, p=p, horizon_min=600, step_min=10)
    strict = recommend([route], now=0, ready=0, deadline=2000, p=p, horizon_min=600, step_min=10,
                       stable_needs_new_path=True)
    assert all(not (e.kind == ODSTAVIT and e.depart < p.new_path_lead_min) for e in strict.candidates)
    assert all(e.needs_new_path for e in strict.candidates if e.kind == ODSTAVIT)
    assert any(e.kind == ODSTAVIT and e.depart < p.new_path_lead_min for e in free.candidates)


def test_poznamka_k_neplatnemu_oknu_len_pri_vyluke_na_trase(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now)
    _closure(session, "Zvolen", "Banská Bystrica", fixed_now, fixed_now + timedelta(days=1),
             daily_from="25:00", daily_to="06:00")  # mimo trasy Čadca – Košice
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert not any("neplatné denné okno" in r for r in rec.reasons)
    _closure(session, "Liptovský Mikuláš", "Poprad-Tatry", fixed_now, fixed_now + timedelta(days=1),
             daily_from="25:00", daily_to="06:00")  # na trase → počíta sa celé obdobie a dispečer to vidí
    rec = evaluate_train(session, t.id, fixed_now + timedelta(minutes=1), "test")
    assert any("neplatné denné okno" in r for r in rec.reasons)
    assert not (rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ")
