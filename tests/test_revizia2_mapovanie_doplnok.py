"""Regresné testy k doplneniu 2. revízie – umiestnenie výluk na trasu (nálezy kontroly prvej opravy).

Číslo trate z výluky nevypne poistku medzi dvoma dopravňami trasy, ak ho má aj trasa medzi nimi (Žilina-Teplička
– Varín, trať 180); výluka v jednej dopravni patrí len časti trasy v obvode stanice (idúci vlak, ktorý stanicu
už prešiel, pomalá jazda v stanici, výluka koľaje v stanici raz); koľaje iného rozchodu susednej dopravne do obvodu
dopravne nepatria (Slivník ŠRT pri trati 190); odbočka má obvod len 200 m (Budatín odbočka pri trati 120).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from vlaky.hranice import seed_borders
from vlaky.models import Closure, Driver, DriverShift, Path, PositionReport, Recommendation, Train
from vlaky.planovanie.planovac import build_segments, closure_windows, evaluate_train
from vlaky.rozhodovanie import Params
from vlaky.siet.graf import TrainSpec, haversine, network

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


def _mentions(rec, text: str) -> bool:
    return any(text in r and "na trase vlaku" in r for r in rec.reasons)


# ------------------------------------------------------------------ poistka medzi dopravňami a číslo trate
@pytest.mark.parametrize("ref", ["180", "trať 180"])
def test_cislo_trate_trasy_nevypne_poistku_medzi_dopravnami(session, fixed_now, ref):
    """Úsek Žilina-Teplička – Varín vedie v sieti po koľajach zriaďovacej stanice (dve z nich majú číslo 180), trasu
    nezasiahne. Trasa medzi dopravňami je tiež trať 180 – číslo trate výluku od trasy neoddeľuje, poistka platí."""
    seed_borders(session)
    t = _train(session, fixed_now, origin="Žilina", destination="Poprad-Tatry")
    _closure(session, "Žilina-Teplička", "Varín", fixed_now - timedelta(minutes=30), fixed_now + timedelta(hours=6),
             line_ref=ref)
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert _held(rec)
    assert _mentions(rec, "Žilina-Teplička – Varín")


@pytest.mark.parametrize("od,do,a,b", [("Bratislava hlavná stanica", "Žilina", "Ladce", "Beluša"),
                                       ("Bratislava hlavná stanica", "Poprad-Tatry", "Lisková", "Liptovská Teplá")])
def test_vyluka_medzi_dopravnami_trasy_patri_useku_medzi_nimi(session, fixed_now, od, do, a, b):
    """Úsek výluky zasahuje trasu len hranami, ktorých stred leží za koncovou dopravňou (dlhé hrany) – výluka sa
    aj tak priradí úseku trasy medzi oboma dopravňami, nielen susednému úseku za nimi."""
    net = network()
    spec = TrainSpec()
    r = net.route(od, do, spec)
    km = {s.name: k for s, k, _ in r.stations}
    lo, hi = sorted((km[a], km[b]))
    c = _closure(session, a, b, fixed_now - timedelta(hours=1), fixed_now + timedelta(hours=10))
    wins = closure_windows(session, fixed_now - timedelta(hours=2), fixed_now + timedelta(hours=30))
    _segs, wps, aff = build_segments(net, r, wins, fixed_now, spec, session)
    seg_km = {(x.name, y.name): (x.km, y.km) for x, y in zip(wps, wps[1:])}
    spans = [seg_km[(sa, sb)] for w, sa, sb in aff if w.closure_id == c.id]
    assert any(x < hi - 1e-6 and y > lo + 1e-6 for x, y in spans), (spans, lo, hi)


def test_vyluka_z_emailu_s_riadkom_trat_zastavi_vlak(cfg, fixed_now):
    """Bežný e-mail ŽSR s riadkom „Trať: 180 Žilina – Košice“ (výluka dostane číslo trate) – vlak sa nevypraví."""
    from vlaky.agent.beh import run_cycle
    from vlaky.cas import local
    from vlaky.db import session_scope

    with session_scope() as s:
        seed_borders(s)
        tid = _train(s, fixed_now, number="70150", origin="Žilina", destination="Poprad-Tatry",
                     deadline_utc=fixed_now + timedelta(hours=12)).id
    st, en = local(fixed_now - timedelta(minutes=30)), local(fixed_now + timedelta(hours=6))
    msg = EmailMessage()
    msg["From"] = "vyluky.orza@zsr.example"
    msg["To"] = "agent.dispecing@dopravca.example"
    msg["Subject"] = "ROV č. 1199/2026 – výluka Žilina-Teplička – Varín"
    msg["Message-ID"] = "<rov1199@zsr.example>"
    msg["Date"] = "Mon, 02 Nov 2026 05:00:00 +0100"
    msg.set_content("Vážení dopravcovia,\n\noznamujeme Vám, že na základe Rozkazu o výluke (ROV) č. 1199/2026 bude "
                    "vykonaná výluka:\n\nTrať: 180 Žilina – Košice\n"
                    "Úsek: Žilina-Teplička – Varín, obe traťové koľaje\n"
                    "Druh výluky: úplná výluka, doprava prerušená\n"
                    f"Termín: od {st:%d.%m.%Y %H:%M} do {en:%d.%m.%Y %H:%M}\n"
                    "Dôvod výluky: výmena výhybiek\n\nS pozdravom\nOR Žilina\n")
    (cfg.mail_dir / "rov1199.eml").write_bytes(bytes(msg))
    run_cycle(fixed_now, force_evaluate=True)
    with session_scope() as s:
        c = s.scalar(select(Closure).where(Closure.to_station == "Varín"))
        assert c is not None and "180" in (c.line_ref or "")
        rec = s.scalar(select(Recommendation).where(Recommendation.train_id == tid)
                       .order_by(Recommendation.id.desc()))
        assert _held(rec)
        assert any("Žilina-Teplička – Varín" in r for r in rec.reasons)


# ------------------------------------------------------------------ výluka v jednej dopravni: len obvod stanice
def _point_at(net, r, km):
    """Bod na trase r vo vzdialenosti km od jej začiatku."""
    cum = 0.0
    for i, ei in enumerate(r.edges):
        e = net.edges[ei]
        g = e.geom if e.u == r.nodes[i] else list(reversed(e.geom))
        for a, b in zip(g, g[1:]):
            d = haversine(*a, *b) / 1000.0
            if cum + d >= km:
                f = (km - cum) / d if d else 0.0
                return a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])
            cum += d
    raise AssertionError("bod mimo trasy")


def _running_train(session, now, number, od, do, lat, lon):
    """Idúci vlak s rušňovodičom v službe a čerstvou polohou (2 min)."""
    t = _train(session, now, number=number, origin=od, destination=do, status="v_jazde", path_at=None,
               planned_departure_utc=now - timedelta(minutes=30))
    drv = Driver(name=f"Rušňovodič {number}")
    session.add(drv)
    session.flush()
    session.add(DriverShift(driver_id=drv.id, train_id=t.id, start_utc=now - timedelta(hours=1), status="prebieha"))
    session.add(PositionReport(train_id=t.id, train_number=t.number, ts_utc=now - timedelta(minutes=2),
                               lat=lat, lon=lon))
    session.flush()
    return t


@pytest.mark.parametrize("stanica,od,do,za_km", [
    ("Ilava", "Púchov", "Trenčín", 1.0),         # dlhá hrana 5,4 km pokračuje za stanicou
    ("Kraľovany", "Žilina", "Poprad-Tatry", 2.0),  # pred prvou opravou fungovalo, prvá oprava zhoršila
])
def test_iduci_vlak_za_stanicou_vyluka_v_stanici_netyka(session, fixed_now, stanica, od, do, za_km):
    """Idúci vlak s čerstvou polohou: mimoriadnosť v stanici, ktorú už prešiel, ho nezastaví (ani keď ide ešte po
    tej istej dlhej koľaji, ktorá stanicou prechádza); vlak 1 km pred stanicou áno."""
    seed_borders(session)
    net = network()
    r = net.route(od, do, TrainSpec())
    km_st = next(km for s, km, _ in r.stations if s.name == stanica)
    _closure(session, stanica, stanica, fixed_now - timedelta(minutes=30), fixed_now + timedelta(hours=6),
             kind="mimoriadnost")
    past = _running_train(session, fixed_now, "70180", od, do, *_point_at(net, r, km_st + za_km))
    rec = evaluate_train(session, past.id, fixed_now, "test")
    assert (rec.decision, rec.color) == ("POKRAČOVAŤ", "ZELENÁ"), rec.reasons
    assert not _mentions(rec, f"{stanica} – {stanica}")
    before = _running_train(session, fixed_now, "70181", od, do, *_point_at(net, r, km_st - 1.0))
    rec = evaluate_train(session, before.id, fixed_now, "test")
    assert _held(rec)
    assert _mentions(rec, f"{stanica} – {stanica}")


def _extra_run(session, now, stanica, od, do, kind="pomala_jazda", **kw):
    """Predĺženie jazdy (súčet Slowdown.extra_run) a zvýšenie mediánu zdržania (extra_p50) od výluky v stanici."""
    net = network()
    spec = TrainSpec()
    r = net.route(od, do, spec)
    _closure(session, stanica, stanica, now - timedelta(hours=1), now + timedelta(hours=10), kind=kind, **kw)
    wins = closure_windows(session, now - timedelta(hours=2), now + timedelta(hours=30))
    segs, _wps, _aff = build_segments(net, r, wins, now, spec, session)
    slows = [s for seg in segs for s in seg.slowdowns]
    return sum(s.extra_run for s in slows), sum(s.extra_p50 for s in slows), r


@pytest.mark.parametrize("stanica,od,do", [("Beluša", "Púchov", "Trenčín"), ("Ilava", "Púchov", "Trenčín"),
                                           ("Kraľovany", "Žilina", "Poprad-Tatry")])
def test_pomala_jazda_v_stanici_len_v_obvode(session, fixed_now, stanica, od, do):
    """POTR 30 km/h v stanici (od = do): počíta sa len časť trasy v obvode stanice (do 600 m od nej, ~1,2 km),
    nie celé dlhé hrany, ktoré stanicou prechádzajú (Beluša 8,4 km), a bez 5 min navyše v susednom úseku."""
    extra, _e50, r = _extra_run(session, fixed_now, stanica, od, do, speed_kmh=30)
    assert 0.5 <= extra <= 3.0, extra
    net = network()
    spans = net.closure_route_spans(r, stanica, stanica)
    km_st = next(km for s, km, _ in r.stations if s.name == stanica)
    assert spans and all(lo >= km_st - 0.7 and hi <= km_st + 0.7 for lo, hi in spans), (spans, km_st)


def test_pomala_jazda_v_stanici_nevyvola_falosnu_oranzovu(session, fixed_now):
    """Vlak Púchov → Trenčín s rezervou 68 min voči termínu: POTR 30 km/h v stanici Beluša ho zdrží ~1,6 min, takže
    rezerva zostane nad 60 min (ZELENÁ). Prvá oprava počítala 12,7 min (celá 8,4 km hrana + 5 min) → ORANŽOVÁ."""
    seed_borders(session)
    t0 = _train(session, fixed_now, number="70170", origin="Púchov", destination="Trenčín",
                deadline_utc=fixed_now + timedelta(hours=12))
    r0 = evaluate_train(session, t0.id, fixed_now, "test")
    arr0 = datetime.fromisoformat(r0.chosen["arrival_p50"])
    _closure(session, "Beluša", "Beluša", fixed_now - timedelta(hours=1), fixed_now + timedelta(hours=10),
             kind="pomala_jazda", speed_kmh=30)
    t1 = _train(session, fixed_now, number="70171", origin="Púchov", destination="Trenčín",
                deadline_utc=arr0 + timedelta(minutes=Params().deadline_buffer_min + 8))
    r1 = evaluate_train(session, t1.id, fixed_now, "test")
    assert (r1.decision, r1.color) == ("VYPRAVIŤ", "ZELENÁ"), r1.reasons
    assert _mentions(r1, "Beluša – Beluša")
    delta = (datetime.fromisoformat(r1.chosen["arrival_p50"]) - arr0).total_seconds() / 60.0
    assert 0.5 <= delta <= 3.0, delta


def test_vyluka_kolaje_v_stanici_sa_pocita_raz(session, fixed_now):
    """Výluka koľaje v stanici (od = do) zvýši zdržanie raz (+3 min medián), nie v oboch susedných úsekoch."""
    _extra, e50, _r = _extra_run(session, fixed_now, "Beluša", "Púchov", "Trenčín", kind="kolaj")
    assert e50 == pytest.approx(3.0)


# ------------------------------------------------------------------ rozchod obvodu dopravne (Slivník ŠRT)
@pytest.mark.parametrize("ref", ["102A", ""])
def test_mimoriadnost_v_stanici_srt_nezastavi_vlak_na_trati_190(session, fixed_now, ref):
    """Trať 190 (1435) vedie 15 m od bodu stanice Slivník ŠRT, ktorej vlastné koľaje sú len 1520; staničné koľaje
    1435 v okolí patria výhybni Slivník. Mimoriadnosť v stanici Slivník ŠRT vlak 1435 nezastaví, vlak na ŠRT áno."""
    seed_borders(session)
    t = _train(session, fixed_now, number="70160", origin="Košice", destination="Čierna nad Tisou",
               deadline_utc=fixed_now + timedelta(hours=12))
    _closure(session, "Slivník ŠRT", "Slivník ŠRT", fixed_now - timedelta(minutes=30),
             fixed_now + timedelta(hours=6), kind="mimoriadnost", line_ref=ref)
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert (rec.decision, rec.color) == ("VYPRAVIŤ", "ZELENÁ"), rec.reasons
    assert not any("Slivník" in r for r in rec.reasons)
    srt = _train(session, fixed_now, number="88010", origin="Maťovce ŠRT", destination="Haniska pri Košiciach",
                 traction="M", gauge="1520", vmax_kmh=80)
    rec = evaluate_train(session, srt.id, fixed_now, "test")
    assert _held(rec)
    assert _mentions(rec, "Slivník ŠRT – Slivník ŠRT")


def test_obvod_dopravne_podla_vlastnych_kolaji_a_cisla_trate():
    """Obvod stanice ŠRT bez koľaje pre oba rozchody je len 1520; výslovné číslo trate 190 pridá koľaje trate 190.
    Haniska pri Košiciach a Čierna nad Tisou (stretnutie rozchodov v obvode) zostávajú pre oba rozchody."""
    net = network()
    srt = set(net.route("Maťovce ŠRT", "Haniska pri Košiciach", SRT).edges)
    main = set(net.route("Košice", "Čierna nad Tisou", TrainSpec()).edges)
    sec = net.closure_edges("Slivník ŠRT", "Slivník ŠRT")
    assert sec & srt and not sec & main
    assert all(net.edges[e].gauge == "1520" for e in sec)
    with_190 = net.closure_edges("Slivník ŠRT", "Slivník ŠRT", "trať 190")
    assert with_190 & main and with_190 & srt
    for name in ("Haniska pri Košiciach", "Čierna nad Tisou"):
        assert net.area_gauges(net.find_station(name)) >= {"1435", "1520"}, name
    assert net.closure_edges("Haniska pri Košiciach", "Haniska pri Košiciach") & srt
    assert net.closure_edges("Čierna nad Tisou", "Čierna nad Tisou") & main


# ------------------------------------------------------------------ odbočka: obvod 200 m
@pytest.mark.parametrize("odbocka,cez_od,cez_do", [
    ("Budatín odbočka", "Žilina", "Čadca"),
    ("Nové Mesto nad Váhom výh.č. 53/54", "Nové Mesto nad Váhom", "Myjava"),
])
def test_vyluka_v_odbocke_nezasiahne_inu_trat(session, fixed_now, odbocka, cez_od, cez_do):
    """Odbočka je len skupina výhybiek: jej výluka (od = do) nezastaví vlak Bratislava → Žilina na trati 120, ktorá
    vedie 400 – 570 m od nej, ale trasu cez odbočku zasiahne."""
    net = network()
    through = net.route(cez_od, cez_do, TrainSpec(traction="M"))
    assert odbocka in [s.name for s, _, _ in through.stations]
    assert net.closure_edges(odbocka, odbocka) & set(through.edges)
    seed_borders(session)
    t = _train(session, fixed_now, number="70190", origin="Bratislava hlavná stanica", destination="Žilina")
    _closure(session, odbocka, odbocka, fixed_now - timedelta(minutes=30), fixed_now + timedelta(hours=6))
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert (rec.decision, rec.color) == ("VYPRAVIŤ", "ZELENÁ"), rec.reasons
    assert not any(odbocka in r for r in rec.reasons)
