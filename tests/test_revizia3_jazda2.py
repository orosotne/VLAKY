"""Regresné testy k 3. revízii – idúci vlak, druhé kolo opráv: odhad polohy od najneskoršieho možného odchodu a nie cez
platnú výluku, stará poloha pri východisku (vlak podľa plánu v cieli), hlásenie ŽSR/TIS v cieli pri výluke, čakanie
pred vjazdom do cieľa, dodatočný nástup z mobilu po odchode a rozhodnutie dispečera pri návrate z jazdy."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from test_web_spolocne import csrf, login_as
from vlaky.cas import ensure_utc, fmt, local, set_fake_now
from vlaky.hranice import seed_borders
from vlaky.models import AuditLog, Closure, Driver, DriverShift, Notification, PositionReport, Recommendation, Train
from vlaky.planovanie.planovac import evaluate_train
from vlaky.siet.graf import TrainSpec, haversine, network

pytest_plugins = ["test_web_spolocne"]


def _point_at(net, r, km):
    """Bod trasy (lat, lon) vo vzdialenosti km od začiatku."""
    cum, g = 0.0, None
    for i, ei in enumerate(r.edges):
        e = net.edges[ei]
        g = e.geom if e.u == r.nodes[i] else list(reversed(e.geom))
        for (la1, lo1), (la2, lo2) in zip(g, g[1:]):
            d = haversine(la1, lo1, la2, lo2) / 1000.0
            if cum + d >= km:
                f = (km - cum) / d if d else 0.0
                return la1 + f * (la2 - la1), lo1 + f * (lo2 - lo1)
            cum += d
    return g[-1]


def _train(session, now, **kw):
    data = dict(number="73400", run_date=now.date().isoformat(), origin="Čadca", destination="Košice",
                traction="E", gauge="1435", vmax_kmh=100, status="v_jazde", deadline_utc=now + timedelta(hours=30))
    data.update(kw)
    t = Train(**data)
    session.add(t)
    session.flush()
    return t


def _shift(session, train, start, status="prebieha", name="R. FIKTÍVNY"):
    d = Driver(name=name)
    session.add(d)
    session.flush()
    sh = DriverShift(driver_id=d.id, train_id=train.id, start_utc=start, status=status)
    session.add(sh)
    session.flush()
    return sh


def _gps(session, train, ts, lat, lon, source="gps"):
    session.add(PositionReport(train_id=train.id, train_number=train.number, ts_utc=ts, lat=lat, lon=lon,
                               source=source))
    session.flush()


def _last_rec(session, train_id):
    return session.scalar(select(Recommendation).where(Recommendation.train_id == train_id)
                          .order_by(Recommendation.id.desc()))


def _local(dt) -> str:
    return local(dt).strftime("%Y-%m-%dT%H:%M")


def _form(t, **kw) -> dict:
    data = {"number": t.number, "run_date": t.run_date or "", "origin": t.origin, "destination": t.destination,
            "gauge": t.gauge, "traction": t.traction, "vmax_kmh": str(int(t.vmax_kmh)), "status": t.status,
            "deadline_utc": _local(t.deadline_utc) if t.deadline_utc else "",
            "ready_utc": _local(t.ready_utc) if t.ready_utc else "",
            "planned_departure_utc": _local(t.planned_departure_utc) if t.planned_departure_utc else ""}
    data.update(kw)
    return data


# ------------------------------------------------------------------ R2 / odhad polohy: najneskorší odchod, nie cez výluku
def test_oneskoreny_vlak_v_jazde_nie_je_hned_v_cieli(client, session, fixed_now):
    """Dispečer označí oneskorený vlak „v jazde“ vo webe až pri odchode (plánovaný odchod pred 30 min, jazda
    Púchov – Trenčín ~20 min): vlak nie je „podľa plánu už v cieli“ – mimoriadnosť v Ilave, ku ktorej príde o pár
    minút, zostáva pred ním. Po skutočnom odchode (hlásenie) pred 30 min a s mimoriadnosťou, ktorá v Ilave platí od
    hodiny pred odchodom, vlak cez ňu podľa plánu prejsť nemohol – stojí pred ňou, nie „v cieli“."""
    seed_borders(session)
    session.add(Closure(kind="mimoriadnost", from_station="Ilava", to_station="Ilava", status="overene",
                        start_utc=fixed_now + timedelta(minutes=5), end_utc=fixed_now + timedelta(hours=3)))
    web = _train(session, fixed_now, number="73401", origin="Púchov", destination="Trenčín", status="prevzaty",
                 ready_utc=fixed_now - timedelta(hours=1), standing_since_utc=fixed_now - timedelta(hours=1),
                 planned_departure_utc=fixed_now - timedelta(minutes=30), deadline_utc=fixed_now + timedelta(hours=8))
    _shift(session, web, fixed_now - timedelta(hours=1))
    session.commit()
    login_as(client, "disp1", "dispecer")
    token = csrf(client, f"/vlaky/{web.id}/upravit")
    r = client.post(f"/vlaky/{web.id}/upravit", follow_redirects=False,
                    data={"csrf_token": token, **_form(web, status="v_jazde")})
    assert r.status_code == 303
    session.expire_all()
    assert session.get(Train, web.id).departed_utc is None  # čas úpravy nie je skutočný odchod
    rec_web = evaluate_train(session, web.id, fixed_now, "úprava vlaku")

    actual = _train(session, fixed_now, number="73402", origin="Púchov", destination="Trenčín",
                    planned_departure_utc=fixed_now - timedelta(minutes=30),
                    departed_utc=fixed_now - timedelta(minutes=30), deadline_utc=fixed_now + timedelta(hours=8))
    _shift(session, actual, fixed_now - timedelta(hours=1), name="R. 73402")
    session.add(Closure(kind="mimoriadnost", from_station="Ilava", to_station="Ilava", status="overene",
                        start_utc=fixed_now - timedelta(hours=1), end_utc=fixed_now + timedelta(hours=3)))
    session.flush()
    rec_actual = evaluate_train(session, actual.id, fixed_now, "t")
    for rec in (rec_web, rec_actual):
        assert "v cieli" not in rec.summary, rec.summary
        assert any("Ilava – Ilava" in x and "na trase vlaku" in x for x in rec.reasons), rec.reasons
        assert rec.decision != "POKRAČOVAŤ" and rec.path_proposal


# ------------------------------------------------------------------ R1: stará poloha pri východisku
def test_stara_poloha_pri_vychodisku_vlak_podla_planu_v_cieli(cfg, fixed_now):
    """Jediná poloha GPS je pri východisku z času odchodu pred 3 h (jazda Žilina – Poprad-Tatry ~90 min), výluka
    Žilina – Varín začína teraz: vlak sa neplánuje znova od východiska (žiadna výluka za ním, ESKALOVAŤ ani KRITICKÁ
    SMS), ale „podľa plánu by už mal byť v cieli – overte polohu“. Stará poloha pred mimoriadnosťou, ktorá vlak drží,
    zostáva dolnou hranicou aj po čase, keď by bez nej bol v cieli."""
    from vlaky.agent.beh import run_cycle
    from vlaky.db import session_scope

    net = network()
    r = net.route("Žilina", "Poprad-Tatry", TrainSpec())
    with session_scope() as s:
        seed_borders(s)
        t = _train(s, fixed_now, number="73403", origin="Žilina", destination="Poprad-Tatry",
                   planned_departure_utc=fixed_now - timedelta(hours=3), departed_utc=fixed_now - timedelta(hours=3),
                   deadline_utc=fixed_now + timedelta(hours=3))
        _shift(s, t, fixed_now - timedelta(hours=4))
        _gps(s, t, fixed_now - timedelta(hours=3), *_point_at(net, r, 0.2))
        s.add(Closure(kind="uplna", from_station="Žilina", to_station="Varín", status="overene",
                      start_utc=fixed_now, end_utc=fixed_now + timedelta(hours=6)))
        tid = t.id
    run_cycle(fixed_now, force_evaluate=True)
    with session_scope() as s:
        rec = _last_rec(s, tid)
        assert (rec.decision, rec.color) == ("POKRAČOVAŤ", "ORANŽOVÁ") and rec.priority != "KRITICKÉ", rec.summary
        assert "podľa plánu by už mal byť v cieli" in rec.summary and "overte" in rec.summary
        assert not any("Varín" in x for x in rec.reasons) and rec.path_proposal == {}
        assert any("poslednej známej polohy" in x and "pred 180 min" in x for x in rec.reasons), rec.reasons
        assert rec.chosen.get("arrival_floating")
        assert s.scalars(select(Notification).where(Notification.channel == "sms")).all() == []

    with session_scope() as s:
        r2 = net.route("Púchov", "Trenčín", TrainSpec())
        held = _train(s, fixed_now, number="73404", origin="Púchov", destination="Trenčín",
                      planned_departure_utc=fixed_now - timedelta(minutes=45),
                      departed_utc=fixed_now - timedelta(minutes=45), deadline_utc=fixed_now + timedelta(hours=8))
        _shift(s, held, fixed_now - timedelta(hours=2), name="R. 73404")
        _gps(s, held, fixed_now - timedelta(minutes=40), *_point_at(net, r2, 1.0))
        s.add(Closure(kind="mimoriadnost", from_station="Ilava", to_station="Ilava", status="overene",
                      start_utc=fixed_now - timedelta(hours=1), end_utc=fixed_now + timedelta(hours=3)))
        s.flush()
        rec2 = evaluate_train(s, held.id, fixed_now, "pravidelný prepočet")
        assert "v cieli" not in rec2.summary and rec2.decision != "POKRAČOVAŤ", rec2.summary
        assert any("Ilava – Ilava" in x and "na trase vlaku" in x for x in rec2.reasons)
        assert any("posledná poloha pred 40 min" in x for x in rec2.reasons)


# ------------------------------------------------------------------ R4 / R4b: príchod do cieľa pri výluke
def test_prichod_do_ciela_pri_vyluke_v_cieli_aj_za_cielom(client, fixed_now):
    """Hlásenie TIS „príchod“ v cieli (Poprad-Tatry) platí aj vtedy, keď na poslednom úseku trasy (Štrba – Poprad-
    Tatry) o 20 min začína výluka – žiadna výluka za vlakom, ESKALOVAŤ ani KRITICKÁ SMS. Výluka trate za cieľom
    (Poprad-Tatry – Spišská Nová Ves) neznamená, že vlak v obvode cieľovej stanice ešte nie je v cieli."""
    from vlaky.agent.beh import run_cycle
    from vlaky.db import session_scope

    net = network()
    r = net.route("Žilina", "Poprad-Tatry", TrainSpec())
    with session_scope() as s:
        seed_borders(s)
        tis = _train(s, fixed_now, number="73405", origin="Žilina", destination="Poprad-Tatry",
                     planned_departure_utc=fixed_now - timedelta(minutes=85),
                     departed_utc=fixed_now - timedelta(minutes=85), deadline_utc=fixed_now + timedelta(hours=3))
        _shift(s, tis, fixed_now - timedelta(hours=3))
        s.add(Closure(kind="uplna", from_station="Štrba", to_station="Poprad-Tatry", status="overene",
                      start_utc=fixed_now + timedelta(minutes=20), end_utc=fixed_now + timedelta(hours=6)))
        tid = tis.id
    arrival = fixed_now - timedelta(minutes=2)
    resp = client.post("/api/udalosti-jazdy", headers={"Authorization": "Bearer test-token"},
                       json={"train_number": "73405", "station": "Poprad-Tatry", "event": "prichod",
                             "ts": local(arrival).isoformat(), "source": "tis"})
    assert resp.status_code == 200, resp.text
    run_cycle(fixed_now, force_evaluate=True)
    with session_scope() as s:
        rec = _last_rec(s, tid)
        assert rec.decision == "POKRAČOVAŤ" and rec.priority != "KRITICKÉ", rec.summary
        assert f"vlak je v cieli (Poprad-Tatry, príchod {fmt(arrival)})" in rec.summary
        assert not any("Štrba" in x for x in rec.reasons)
        assert s.scalars(select(Notification).where(Notification.channel == "sms")).all() == []

    with session_scope() as s:
        s.query(Closure).delete()
        s.add(Closure(kind="uplna", from_station="Poprad-Tatry", to_station="Spišská Nová Ves", status="overene",
                      start_utc=fixed_now - timedelta(hours=1), end_utc=fixed_now + timedelta(hours=6)))
        gps = _train(s, fixed_now, number="73406", origin="Žilina", destination="Poprad-Tatry",
                     planned_departure_utc=fixed_now - timedelta(minutes=100),
                     departed_utc=fixed_now - timedelta(minutes=100), deadline_utc=fixed_now + timedelta(hours=3))
        _shift(s, gps, fixed_now - timedelta(hours=3), name="R. 73406")
        _gps(s, gps, fixed_now - timedelta(minutes=1), *_point_at(net, r, r.length_m / 1000.0 - 0.3))
        rec2 = evaluate_train(s, gps.id, fixed_now, "t")
        assert "vlak je v cieli (Poprad-Tatry" in rec2.summary, rec2.summary
        assert rec2.path_proposal == {} and rec2.decision == "POKRAČOVAŤ"


# ------------------------------------------------------------------ R7: čakanie pred vjazdom do cieľa
def test_cakanie_pred_vjazdom_do_ciela_nie_je_prichod(session, fixed_now):
    """Vlak hodinu stojí 900 m pred koncom trasy (pred vjazdom do Košíc, mimo obvodu stanice) a rušňovodičovi zostáva
    do 13 h 10 min: nie je „v cieli“ s koncom pracovného času zmrazeným pri prvej polohe – rušňovodič je ešte
    v službe (ČERVENÁ). Keď vlak potom vojde do stanice, príchod je prvá poloha v stanici, nie začiatok čakania pred
    vjazdom (koniec pracovného času sa neskracuje)."""
    seed_borders(session)
    net = network()
    r = net.route("Čadca", "Košice", TrainSpec())
    dep = fixed_now - timedelta(hours=11, minutes=50)
    signal = _point_at(net, r, r.length_m / 1000.0 - 0.9)
    trains = {}
    for number in ("73407", "73408"):
        t = _train(session, fixed_now, number=number, planned_departure_utc=dep, departed_utc=dep,
                   deadline_utc=fixed_now + timedelta(hours=10))
        _shift(session, t, dep - timedelta(minutes=60), name=f"R. {number}")
        for k in range(5):
            _gps(session, t, fixed_now - timedelta(minutes=65 - 15 * k), *signal)
        trains[number] = t
    st = net.find_station("Košice")
    _gps(session, trains["73408"], fixed_now - timedelta(minutes=2), st.lat, st.lon)
    waiting = evaluate_train(session, trains["73407"].id, fixed_now, "t")
    assert "v cieli" not in waiting.summary, waiting.summary
    assert waiting.crew["status"] == "ČERVENÁ"
    inside = evaluate_train(session, trains["73408"].id, fixed_now, "t")
    assert "vlak je v cieli" in inside.summary
    assert inside.chosen["arrival_p50"] == (fixed_now - timedelta(minutes=2)).isoformat()
    assert inside.crew["end_p50"] == (fixed_now + timedelta(minutes=28)).isoformat()


# ------------------------------------------------------------------ R9: dodatočný nástup z mobilu po odchode
def test_dodatocny_nastup_z_mobilu_po_odchode_neposunie_limit(client, cfg, fixed_now):
    """Rušňovodič nahlási „Odchod“ a „Nástup na zmenu“ až 6 h po ňom: limit 13 h (zmena aj upozornenia) beží
    najneskôr od odchodu mínus príprava, nie od ťuknutia – bez naplánovanej zmeny aj s naplánovanou zmenou k odchodu.
    Naplánované vystriedanie neskôr (iný rušňovodič) sa dodatočným nástupom nezačne – vlastnú zmenu začne až rušňovodič
    vystriedania a zmena z mobilu sa ukončí."""
    from vlaky.db import session_scope
    from vlaky.web.rusnovodic import make_driver_token

    dep = fixed_now - timedelta(hours=6)
    with session_scope() as s:
        seed_borders(s)
        bare = _train(s, fixed_now, number="73409", status="prevzaty", ready_utc=dep - timedelta(minutes=30),
                      planned_departure_utc=dep)
        planned = _train(s, fixed_now, number="73410", status="prevzaty", ready_utc=dep - timedelta(minutes=30),
                         planned_departure_utc=dep)
        planned_start = dep - timedelta(minutes=45)
        _shift(s, planned, planned_start, status="planovana", name="Jozef FIKTÍVNY")
        relief = _train(s, fixed_now, number="73414", status="prevzaty", ready_utc=dep - timedelta(minutes=30),
                        planned_departure_utc=dep)
        relief_start = fixed_now + timedelta(hours=5)
        relief_id = _shift(s, relief, relief_start, status="planovana", name="Peter Bystrý FIKTÍVNY").id
        ids = {"bare": bare.id, "planned": planned.id, "relief": relief.id}
        tokens = {key: make_driver_token(s.get(Train, tid)) for key, tid in ids.items()}
    set_fake_now(dep)
    for key in ids:
        assert client.post("/api/hlasenie", json={"t": tokens[key], "typ": "odchod"}).status_code == 200
    set_fake_now(fixed_now)
    with session_scope() as s:
        before = evaluate_train(s, ids["bare"], fixed_now, "pred nástupom")
        assert before.crew["start"] == (dep - timedelta(minutes=60)).isoformat()
    r = client.post("/api/hlasenie", json={"t": tokens["bare"], "typ": "nastup"})
    assert r.status_code == 200 and "Vlak je už v jazde" in r.json()["message"]
    assert fmt(dep - timedelta(minutes=60)) in r.json()["message"]
    r2 = client.post("/api/hlasenie", json={"t": tokens["planned"], "typ": "nastup"})
    assert r2.status_code == 200
    with session_scope() as s:
        bare_shift = s.scalar(select(DriverShift).where(DriverShift.train_id == ids["bare"]))
        assert bare_shift.status == "prebieha" and ensure_utc(bare_shift.start_utc) == dep - timedelta(minutes=60)
        planned_shift = s.scalar(select(DriverShift).where(DriverShift.train_id == ids["planned"]))
        assert planned_shift.status == "prebieha" and ensure_utc(planned_shift.start_utc) == dep - timedelta(minutes=60)
        after = evaluate_train(s, ids["bare"], fixed_now + timedelta(minutes=1), "po nástupe")
        assert after.crew["start"] == (dep - timedelta(minutes=60)).isoformat()
        audit = s.scalar(select(AuditLog).where(AuditLog.action == "hlasenie_nastup",
                                                AuditLog.entity_id == str(ids["bare"])))
        assert audit.detail.get("shift_created") and audit.detail.get("start")
    assert client.post("/api/hlasenie", json={"t": tokens["relief"], "typ": "nastup"}).status_code == 200
    with session_scope() as s:
        shifts = {sh.id: sh for sh in s.scalars(select(DriverShift).where(DriverShift.train_id == ids["relief"]))}
        mobile = next(sh for sid, sh in shifts.items() if sid != relief_id)
        assert mobile.status == "prebieha" and ensure_utc(mobile.start_utc) == dep - timedelta(minutes=60)
        assert shifts[relief_id].status == "planovana" and ensure_utc(shifts[relief_id].start_utc) == relief_start
        mobile_id = mobile.id
    set_fake_now(relief_start)
    assert client.post("/api/hlasenie", json={"t": tokens["relief"], "typ": "nastup"}).status_code == 200
    with session_scope() as s:
        assert s.get(DriverShift, mobile_id).status == "ukoncena"
        sh = s.get(DriverShift, relief_id)
        assert sh.status == "prebieha" and ensure_utc(sh.start_utc) == relief_start


# ------------------------------------------------------------------ R3: návrat z jazdy – rozhodnutie dispečera
def test_navrat_z_jazdy_rozhodne_dispecer(client, session, fixed_now):
    """Vlak stojí 17 h (ESKALOVAŤ), rušňovodič ťukne „Odchod“, no vlak nevyšiel a bez polohy: dispečer pri návrate
    po 45 min zvolí „odchod bol omyl“ – limit 18 h beží ďalej od začiatku státia. Voľba „vlak išiel“ začne nové státie
    aj krátko po odchode. Bez voľby (automaticky) správa ukáže, že limit 18 h začal znova a odkedy bol vlak v jazde."""
    from vlaky.web.rusnovodic import make_driver_token

    seed_borders(session)
    since = fixed_now - timedelta(hours=17)
    session.add(Closure(kind="uplna", from_station="Liptovský Mikuláš", to_station="Poprad-Tatry", status="overene",
                        start_utc=fixed_now - timedelta(hours=1), end_utc=fixed_now + timedelta(hours=5)))
    trains = {}
    for number in ("73411", "73412", "73413"):
        trains[number] = _train(session, fixed_now, number=number, status="odstaveny", ready_utc=since,
                                standing_since_utc=since)
    session.commit()
    set_fake_now(fixed_now)
    for t in trains.values():
        tok = make_driver_token(t)
        session.commit()
        assert client.post("/api/hlasenie", json={"t": tok, "typ": "odchod"}).status_code == 200
    login_as(client, "disp1", "dispecer")

    def revert(number, minutes, choice):
        set_fake_now(fixed_now + timedelta(minutes=minutes))
        tid = trains[number].id
        tok = csrf(client, f"/vlaky/{tid}/upravit")
        session.expire_all()
        data = {"csrf_token": tok, **_form(session.get(Train, tid), status="odstaveny")}
        if choice is not None:
            data["navrat_z_jazdy"] = choice
        resp = client.post(f"/vlaky/{tid}/upravit", follow_redirects=True, data=data)
        assert resp.status_code == 200
        session.expire_all()
        return resp.text, session.get(Train, tid)

    text, t = revert("73411", 45, "omyl")
    assert "Odchod sa považuje za omyl" in text and ensure_utc(t.standing_since_utc) == since
    after = evaluate_train(session, t.id, fixed_now + timedelta(minutes=46), "po oprave")
    assert (after.decision, after.color) == ("ESKALOVAŤ", "ČERVENÁ")
    assert any(f"Vlak stojí od {fmt(since)}" in x for x in after.reasons)
    audit = session.scalar(select(AuditLog).where(AuditLog.action == "train_update", AuditLog.entity_id == str(t.id)))
    assert audit.detail.get("oprava_odchodu") is True and audit.detail.get("navrat_z_jazdy") == "omyl"
    session.commit()  # prepočet zapísal odporúčanie – ďalšia úprava vo webe potrebuje zápis do databázy

    text, t = revert("73412", 3, "jazda")
    assert "Odchod sa považuje za omyl" not in text and "Vlak sa považuje za odídený" in text
    assert ensure_utc(t.standing_since_utc) == fixed_now + timedelta(minutes=3)

    text, t = revert("73413", 45, None)
    restart = fixed_now + timedelta(minutes=45)
    assert ensure_utc(t.standing_since_utc) == restart
    assert f"Vlak sa považuje za odídený (v jazde od {fmt(fixed_now)}) – státie a limit 18 h začínajú " \
           f"{fmt(restart)}." in text
