"""Regresné testy k 2. revízii – umiestnenie výluk na trasu vlaku.

Výluka v jednej dopravni (od = do) zasiahne aj hlavné koľaje, ktoré stanicou len prechádzajú; poistka podľa
dopravní na trase; rovnako nazvané dopravne (Žilina-Teplička); rozchody 1435/1520 (ŠRT popri trati 190) a číslo
trate z výluky; výluka koľaje a pomalá jazda sa počítajú len pri jazde počas ich platnosti a každá raz; webový
formulár prijme výluku v jednej dopravni.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from test_web_spolocne import csrf, login_as
from vlaky.hranice import seed_borders
from vlaky.models import Closure, Path, Train
from vlaky.planovanie.planovac import evaluate_train, merge_segments
from vlaky.rozhodovanie import Params, Route, Segment, evaluate
from vlaky.siet.graf import TrainSpec, network

pytest_plugins = ["test_web_spolocne"]

SRT = TrainSpec(traction="M", gauge="1520")


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


def _held(rec) -> bool:
    return not (rec.decision in ("VYPRAVIŤ", "POKRAČOVAŤ") and rec.color == "ZELENÁ")


def _arrival(rec) -> datetime:
    return datetime.fromisoformat(rec.chosen["arrival_p50"])


# ------------------------------------------------------------------ výluka v jednej dopravni (od = do)
@pytest.mark.parametrize("stanica,od,do,trakcia", [
    ("Ilava", "Púchov", "Trenčín", "E"), ("Beluša", "Púchov", "Trenčín", "E"),
    ("Bytča", "Púchov", "Žilina", "E"), ("Dolný Hričov", "Púchov", "Žilina", "E"),
    ("Zvolen osobná stanica", "Nové Zámky", "Zvolen", "E"), ("Michaľany", "Humenné", "Michaľany", "M"),
])
def test_vyluka_v_stanici_zasiahne_hlavne_kolaje(stanica, od, do, trakcia):
    """Hlavná koľaj prechádza stanicou, ale jej uzly sú ďaleko za stanicou (dlhá hrana) – patrí do obvodu."""
    net = network()
    r = net.route(od, do, TrainSpec(traction=trakcia))
    assert stanica in [s.name for s, _, _ in r.stations]
    assert net.closure_edges(stanica, stanica) & set(r.edges)


def test_vyluky_v_staniciach_systematicky():
    """Každá dopravňa trás medzi hlavnými stanicami: výluka od = do zasiahne trasu, ktorá ňou prechádza."""
    net = network()
    misses = []
    for od, do, trakcia in [("Bratislava hlavná stanica", "Žilina", "E"), ("Čadca", "Košice", "E"),
                            ("Zvolen", "Košice", "M"), ("Nové Zámky", "Zvolen", "E"), ("Kúty", "Štúrovo", "E"),
                            ("Leopoldov", "Nitra", "M"), ("Žilina", "Zvolen", "E"), ("Lučenec", "Košice", "M")]:
        r = net.route(od, do, TrainSpec(traction=trakcia))
        for st, _km, _mins in r.stations:
            if st.name and st.type in ("station", "yard", "border", "junction", "spur_junction"):
                if not (net.closure_edges(st.name, st.name) & set(r.edges)):
                    misses.append((od, do, st.name))
    assert misses == []


def test_mimoriadnost_v_stanici_na_trase_zastavi_vlak(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now, origin="Púchov", destination="Trenčín")
    _closure(session, "Beluša", "Beluša", fixed_now - timedelta(minutes=30), fixed_now + timedelta(hours=6),
             kind="mimoriadnost")
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert _held(rec)
    assert any("Beluša – Beluša" in r and "na trase vlaku" in r for r in rec.reasons)


def test_poistka_vyluka_v_dopravni_na_trase_bez_prieniku_kolaji(session, fixed_now, monkeypatch):
    """Aj keď sa obvod dopravne v modeli siete s trasou nepretne, dopravňa na trase vlaku výluku nezahodí."""
    seed_borders(session)
    net = network()
    real = net.closure_edges
    far = real("Štúrovo", "Štúrovo")  # neprázdny úsek, ale ďaleko od trasy Púchov – Trenčín
    monkeypatch.setattr(net, "closure_edges",
                        lambda a, b, *args, **kw: far if a == b == "Ilava" else real(a, b, *args, **kw))
    t = _train(session, fixed_now, origin="Púchov", destination="Trenčín")
    _closure(session, "Ilava", "Ilava", fixed_now - timedelta(minutes=30), fixed_now + timedelta(hours=6))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert _held(rec)
    assert any("Ilava – Ilava" in r and "na trase vlaku" in r for r in rec.reasons)


def test_poistka_vyluka_medzi_dopravnami_trasy(session, fixed_now):
    """Žilina-Teplička – Varín: úsek v sieti vedie po koľajach zriaďovacej stanice vedľa hlavnej trate; obe
    dopravne sú na trase vlaku, takže výluka sa priradí úsekom medzi nimi."""
    seed_borders(session)
    names = [s.name for s, _, _ in network().route("Žilina", "Poprad-Tatry", TrainSpec()).stations]
    assert "Žilina-Teplička" in names and "Varín" in names
    t = _train(session, fixed_now, origin="Žilina", destination="Poprad-Tatry")
    _closure(session, "Žilina-Teplička", "Varín", fixed_now - timedelta(minutes=30), fixed_now + timedelta(hours=6))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert _held(rec)
    assert any("Žilina-Teplička – Varín" in r and "na trase vlaku" in r for r in rec.reasons)


def test_vyluka_v_stanici_vedla_srt_nezastavi_vlak_1520(session, fixed_now):
    """ŠRT vedie 18 m od stanice Ruskov, ale stanica rozchod 1520 neobsluhuje – mimoriadnosť v nej vlak na ŠRT
    nezastaví (ani poistkou podľa dopravní na trase)."""
    seed_borders(session)
    t = _train(session, fixed_now, number="88001", origin="Maťovce ŠRT", destination="Haniska pri Košiciach",
               traction="M", gauge="1520", vmax_kmh=80)
    _closure(session, "Ruskov", "Ruskov", fixed_now - timedelta(minutes=30), fixed_now + timedelta(hours=6),
             kind="mimoriadnost")
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ"
    assert not any("Ruskov" in r for r in rec.reasons)


# ------------------------------------------------------------------ rovnako nazvané dopravne
def test_vyluka_zilina_teplicka_zasiahne_hlavnu_trat():
    net = network()
    for od, do in (("Čadca", "Košice"), ("Žilina", "Poprad-Tatry")):
        r = net.route(od, do, TrainSpec())
        for a, b in (("Žilina", "Žilina-Teplička"), ("Žilina-Teplička", "Žilina")):
            assert net.closure_edges(a, b) & set(r.edges), (od, do, a, b)


def test_vyluka_zilina_teplicka_zastavi_vlak(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now, origin="Žilina", destination="Poprad-Tatry")
    _closure(session, "Žilina", "Žilina-Teplička", fixed_now - timedelta(minutes=30), fixed_now + timedelta(hours=6))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert _held(rec)
    assert any("Žilina – Žilina-Teplička" in r and "na trase vlaku" in r for r in rec.reasons)


def test_rovnako_nazvane_dopravne_na_tom_istom_mieste():
    """Všetky skupiny rovnakých názvov: použije sa poradie find_station a všetky záznamy na tom istom mieste
    (stanica a zriaďovacia stanica, stanica a zastávka), nie najbližší záznam o pár desiatok metrov."""
    net = network()
    groups = [g for g in net.by_norm.values() if len(g) > 1]
    assert groups
    for g in groups:
        name = g[0].name
        ga, gb = net._closure_stations(name, "Košice")
        assert ga[0] is net.find_station(name)
        assert {id(s) for s in g} <= {id(s) for s in ga}, name


# ------------------------------------------------------------------ rozchody 1435 / 1520
def test_rozchody_dopravni():
    net = network()

    def g(name):
        return net.station_gauges(net.find_station(name))

    assert g("Ruskov") == {"1435"} and g("Slanec") == {"1435"} and g("Trebišov") == {"1435"}
    assert g("Slančík ŠRT") == {"1520"}
    assert {"1435", "1520"} <= g("Haniska pri Košiciach") and {"1435", "1520"} <= g("Čierna nad Tisou")


@pytest.mark.parametrize("a,b", [("Nižná Myšľa", "Ruskov"), ("Ruskov", "Slanec"), ("Slanec", "Kuzmice")])
def test_vyluka_trate_1435_nezasiahne_srt(a, b):
    net = network()
    srt = net.route("Maťovce ŠRT", "Haniska pri Košiciach", SRT)
    main = net.route("Košice", "Čierna nad Tisou", TrainSpec())
    sec = net.closure_edges(a, b)
    assert sec & set(main.edges)
    assert not (sec & set(srt.edges))


@pytest.mark.parametrize("a,b", [("Slančík ŠRT", "Slivník ŠRT"), ("Slančík ŠRT", "Haniska pri Košiciach")])
def test_vyluka_srt_nezasiahne_trat_1435(a, b):
    net = network()
    srt = net.route("Maťovce ŠRT", "Haniska pri Košiciach", SRT)
    main = net.route("Košice", "Čierna nad Tisou", TrainSpec())
    sec = net.closure_edges(a, b)
    assert sec & set(srt.edges)
    assert not (sec & set(main.edges))


def test_vyluka_1435_nezastavi_vlak_na_srt(session, fixed_now):
    seed_borders(session)
    t = _train(session, fixed_now, number="88001", origin="Maťovce ŠRT", destination="Haniska pri Košiciach",
               traction="M", gauge="1520", vmax_kmh=80)
    _closure(session, "Slanec", "Kuzmice", fixed_now - timedelta(hours=1), fixed_now + timedelta(hours=10))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ"
    assert not any("Slanec" in r for r in rec.reasons)


def test_cislo_trate_rozhodne_o_rozchode():
    """Slivník ŠRT aj Budkovce ŠRT obsluhujú oba rozchody – číslo trate z výluky určí, ktorej trate sa týka."""
    net = network()
    srt = set(net.route("Maťovce ŠRT", "Haniska pri Košiciach", SRT).edges)
    both = net.closure_edges("Slivník ŠRT", "Budkovce ŠRT")
    srt_only = net.closure_edges("Slivník ŠRT", "Budkovce ŠRT", "102A")
    normal = net.closure_edges("Slivník ŠRT", "Budkovce ŠRT", "trať 190")
    assert both & srt and srt_only & srt
    assert all("1520" in net.edges[e].gauge for e in srt_only)
    assert normal and not (normal & srt)
    assert net.closure_edges("Slivník ŠRT", "Budkovce ŠRT", "999") == both  # neznáme číslo trate sa ignoruje


def test_cislo_trate_vo_vyluke_planovac(session, fixed_now):
    seed_borders(session)

    def srt_train(number):
        return _train(session, fixed_now, number=number, origin="Maťovce ŠRT", destination="Haniska pri Košiciach",
                      traction="M", gauge="1520", vmax_kmh=80).id

    c = _closure(session, "Slivník ŠRT", "Budkovce ŠRT", fixed_now - timedelta(hours=1),
                 fixed_now + timedelta(hours=10), line_ref="190")
    rec = evaluate_train(session, srt_train("88002"), fixed_now, "test")
    assert rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ"  # výluka trate 190 (1435) sa ŠRT netýka
    c.line_ref = "102A"
    session.flush()
    rec = evaluate_train(session, srt_train("88003"), fixed_now, "test")
    assert _held(rec)
    assert any("Slivník ŠRT – Budkovce ŠRT" in r for r in rec.reasons)


# ------------------------------------------------------------------ výluka koľaje a pomalá jazda v čase
def test_obmedzenie_len_pocas_platnosti_rozhodovaci_modul():
    from vlaky.rozhodovanie import Slowdown

    p = Params(samples=300)
    plain = Route("A – C", (Segment("A – B", 60, 2, 8), Segment("B – C", 60, 2, 8)))
    slow = Route("A – C", (Segment("A – B", 60, 2, 8),
                           Segment("B – C", 60, 2, 8, (), True, (Slowdown(600, 900, extra_run=60, group=1),))))
    for depart in (0, 400, 900, 1000):  # úsekom B – C ide vlak mimo platnosti obmedzenia
        assert evaluate(slow, depart, 0, 5000, p).arrival_p50 == evaluate(plain, depart, 0, 5000, p).arrival_p50
    assert evaluate(slow, 500, 0, 5000, p).arrival_p50 == pytest.approx(
        evaluate(plain, 500, 0, 5000, p).arrival_p50 + 60, abs=1.0)
    # dve nadväzujúce okná tej istej výluky (denné opakovanie) sa pri jednej jazde počítajú raz
    twice = Route("A – C", (Segment("A – B", 60, 2, 8), Segment("B – C", 60, 2, 8, (), True, (
        Slowdown(0, 590, extra_run=60, group=7), Slowdown(590, 1200, extra_run=60, group=7)))))
    assert evaluate(twice, 500, 0, 5000, p).arrival_p50 == pytest.approx(
        evaluate(plain, 500, 0, 5000, p).arrival_p50 + 60, abs=1.0)


def test_zlucenie_useku_zachova_polohu_obmedzenia():
    from vlaky.rozhodovanie import Slowdown

    p = Params(samples=300)
    segs = [Segment("A – B", 60, 2, 8), Segment("B – C", 30, 1, 5, (), True, (Slowdown(600, 900, extra_run=45),)),
            Segment("C – D", 60, 2, 8)]
    merged = merge_segments(segs)
    plain = merge_segments([Segment("A – B", 60, 2, 8), Segment("B – C", 30, 1, 5), Segment("C – D", 60, 2, 8)])
    assert len(merged) == len(plain) == 1
    (s,) = merged[0].slowdowns
    assert s.part_from == pytest.approx(60 / 150) and s.part_to == pytest.approx(90 / 150)
    for depart in (0, 300, 900):  # úsekom B – C mimo okna → rovnaký výsledok ako bez obmedzenia
        assert evaluate(Route("r", tuple(merged)), depart, 0, 5000, p).arrival_p50 == \
            evaluate(Route("r", tuple(plain)), depart, 0, 5000, p).arrival_p50
    later = evaluate(Route("r", tuple(merged)), 560, 0, 5000, p).arrival_p50
    assert later > evaluate(Route("r", tuple(plain)), 560, 0, 5000, p).arrival_p50 + 30


def _same_train(session, now, number, deadline_h):
    """Vlak Čadca → Košice (úsek Liptovský Mikuláš – Poprad-Tatry ide ráno); každý scenár má vlastný (rovnaký)
    vlak, aby výsledok nezávisel od zlučovania odporúčaní v _store."""
    return _train(session, now, number=number, deadline_utc=now + timedelta(hours=deadline_h)).id


def _slow_zone_case(session, now, deadline_h=6, **closure):
    """Výsledok bez obmedzenia a s obmedzením na úseku Liptovský Mikuláš – Poprad-Tatry."""
    seed_borders(session)
    base = evaluate_train(session, _same_train(session, now, "70001", deadline_h), now, "test")
    base_out = (base.decision, base.color, _arrival(base))
    _closure(session, "Liptovský Mikuláš", "Poprad-Tatry", **closure)
    return base_out, evaluate_train(session, _same_train(session, now, "70002", deadline_h), now, "test")


@pytest.mark.parametrize("kind", ["pomala_jazda", "kolaj"])
def test_nocne_obmedzenie_dennemu_vlaku_nevadi(session, fixed_now, kind):
    """Nočné obmedzenie 22:00–04:00 (denne, 10 dní): vlak ide úsekom ráno – výsledok ako bez obmedzenia."""
    base, rec = _slow_zone_case(session, fixed_now, kind=kind, speed_kmh=20 if kind == "pomala_jazda" else None,
                                start=fixed_now - timedelta(days=1), end=fixed_now + timedelta(days=10),
                                daily_from="22:00", daily_to="04:00")
    assert (rec.decision, rec.color) == base[:2] == ("VYPRAVIŤ", "ZELENÁ")
    assert abs((_arrival(rec) - base[2]).total_seconds()) < 1


@pytest.mark.parametrize("start_h,end_h", [(20, 30), (-10, -1)])
def test_pomala_jazda_mimo_casu_jazdy_sa_nepocita(session, fixed_now, start_h, end_h):
    """Jednorazová pomalá jazda až zajtra alebo už skončená sa vlaku, ktorý ide teraz, nepočíta."""
    base, rec = _slow_zone_case(session, fixed_now, kind="pomala_jazda", speed_kmh=20,
                                start=fixed_now + timedelta(hours=start_h), end=fixed_now + timedelta(hours=end_h))
    assert (rec.decision, rec.color) == base[:2] == ("VYPRAVIŤ", "ZELENÁ")
    assert abs((_arrival(rec) - base[2]).total_seconds()) < 1


def test_pomala_jazda_pocas_jazdy_sa_pocita_raz(session, fixed_now):
    """Pomalá jazda počas jazdy vlaku sa započíta; denne opakovaná (každý deň v prehľadávanom období) rovnako
    ako jednorazová – raz, nie raz za každé denné okno."""
    base, once = _slow_zone_case(session, fixed_now, deadline_h=14, kind="pomala_jazda", speed_kmh=20,
                                 start=fixed_now - timedelta(hours=1), end=fixed_now + timedelta(hours=10))
    once_out = (once.decision, once.color, _arrival(once))
    assert once_out[2] > base[2] + timedelta(minutes=20)
    for c in session.scalars(select(Closure)):
        c.status = "zrusene"
    _closure(session, "Liptovský Mikuláš", "Poprad-Tatry", fixed_now - timedelta(days=1),
             fixed_now + timedelta(days=4), kind="pomala_jazda", speed_kmh=20, daily_from="05:00", daily_to="13:00")
    daily = evaluate_train(session, _same_train(session, fixed_now, "70003", 14), fixed_now, "test")
    assert (daily.decision, daily.color) == once_out[:2]
    assert abs((_arrival(daily) - once_out[2]).total_seconds()) < 1


# ------------------------------------------------------------------ webový formulár: výluka v jednej dopravni
def _local(dt) -> str:
    from vlaky.cas import local

    return local(dt).strftime("%Y-%m-%dT%H:%M")


def test_web_vyluka_v_jednej_dopravni(client):
    from vlaky.cas import now_utc
    from vlaky.db import session_scope

    login_as(client, "dispecer")
    token = csrf(client, "/vyluky")
    now = now_utc()
    form = {"csrf_token": token, "from_station": "Žilina", "to_station": "Žilina", "kind": "mimoriadnost",
            "start_utc": _local(now + timedelta(hours=1)), "end_utc": _local(now + timedelta(hours=5)),
            "status": "overene", "note": "porucha výhybky (telefonicky ŽSR)"}
    r = client.post("/vyluky/nova", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    assert "Výluka v jednej dopravni" in client.get("/vyluky/nova").text  # nápoveda vo formulári
    with session_scope() as s:
        c = s.scalar(select(Closure).order_by(Closure.id.desc()))
        assert (c.from_station, c.to_station, c.kind) == ("Žilina", "Žilina", "mimoriadnost")
        # výluka v jednej dopravni z e-mailu sa dá upraviť (predĺžiť)
        e = Closure(kind="mimoriadnost", from_station="Ilava", to_station="Ilava", start_utc=now,
                    end_utc=now + timedelta(hours=2), status="navrh", source="email")
        s.add(e)
        s.flush()
        eid = e.id
    r = client.post(f"/vyluky/{eid}/upravit", follow_redirects=False, data={
        **form, "from_station": "Ilava", "to_station": "Ilava", "end_utc": _local(now + timedelta(hours=8))})
    assert r.status_code == 303, r.text[:300]
    with session_scope() as s:
        e = s.get(Closure, eid)
        assert e.to_station == "Ilava" and e.version == 2
    # neznáma dopravňa sa stále odmietne
    r = client.post("/vyluky/nova", data={**form, "from_station": "Neexistujúca Dopravňa",
                                          "to_station": "Neexistujúca Dopravňa"}, follow_redirects=False)
    assert r.status_code == 422
