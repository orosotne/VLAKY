"""Regresné testy k 3. revízii – idúci vlak: stará poloha GPS, vlak v cieli (aj bez polohy podľa plánu), stály odhad
nástupu rušňovodiča, nástup z aplikácie rušňovodiča (bez naplánovanej zmeny, vystriedanie), neskorý nástup
zaznamenanej zmeny, vlaky bez záznamu o nástupe na stránke Rušňovodiči a oprava omylom nahláseného odchodu
(limit 18 h státia)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from test_web_spolocne import csrf, login_as
from vlaky.cas import ensure_utc, fmt, local, set_fake_now
from vlaky.hranice import seed_borders
from vlaky.models import (AuditLog, Closure, Driver, DriverShift, Notification, Path, PositionReport,
                          Recommendation, Train)
from vlaky.planovanie import planovac
from vlaky.planovanie.planovac import evaluate_train
from vlaky.siet.graf import TrainSpec, haversine, network

pytest_plugins = ["test_web_spolocne"]

ESTIMATE = "(nástup rušňovodiča nie je zadaný)"
NO_START = "limit 13 h sa nedá overiť – zadajte nástup rušňovodiča"


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
    data = dict(number="73300", run_date=now.date().isoformat(), origin="Čadca", destination="Košice",
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


def _recs(session, train_id) -> list:
    return list(session.scalars(select(Recommendation).where(Recommendation.train_id == train_id)
                                .order_by(Recommendation.id)))


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


# ------------------------------------------------------------------ PS0: stará poloha GPS
def test_stara_poloha_nezahodi_vyluku_pred_meskajucim_vlakom(session, fixed_now):
    """Meškajúci vlak (posledná poloha pred Ilavou), mimoriadnosť v Ilave: bez novej polohy 16 min sa vlak nesmie
    „posunúť“ podľa plánu za Ilavu – výluka pred ním zostáva, odhad vychádza z poslednej známej polohy."""
    seed_borders(session)
    net = network()
    r = net.route("Púchov", "Trenčín", TrainSpec())
    t = _train(session, fixed_now, number="73301", origin="Púchov", destination="Trenčín",
               planned_departure_utc=fixed_now - timedelta(minutes=3), departed_utc=fixed_now - timedelta(minutes=3),
               deadline_utc=fixed_now + timedelta(hours=8))
    _shift(session, t, fixed_now - timedelta(hours=2))
    _gps(session, t, fixed_now, *_point_at(net, r, 1.0))
    session.add(Closure(kind="mimoriadnost", from_station="Ilava", to_station="Ilava", status="overene",
                        start_utc=fixed_now - timedelta(hours=1), end_utc=fixed_now + timedelta(hours=3)))
    session.flush()
    fresh = evaluate_train(session, t.id, fixed_now, "gps")
    later = fixed_now + timedelta(minutes=16)
    set_fake_now(later)
    stale = evaluate_train(session, t.id, later, "pravidelný prepočet")
    for rec in (fresh, stale):
        assert any("Ilava – Ilava" in x and "na trase vlaku" in x for x in rec.reasons), rec.reasons
        assert rec.decision != "POKRAČOVAŤ"
    assert stale.color != "ZELENÁ"
    assert any("posledná poloha pred 16 min" in x and "poslednej známej polohy" in x for x in stale.reasons)
    assert stale.chosen.get("arrival_floating")  # príchod zo starej polohy sa posúva s časom – nie je zmena situácie


def test_stara_poloha_nezmeni_varovanie_rusnovodica(session, fixed_now):
    """Meškajúci vlak (20 % trasy po 2 h), rušňovodič 10,5 h v službe: varovanie o 13 h (ČERVENÁ) nesmie zmiznúť len
    preto, že 20 min neprišla poloha (predtým odhad podľa plánu: POKRAČOVAŤ, rušňovodič ZELENÁ)."""
    seed_borders(session)
    net = network()
    r = net.route("Čadca", "Košice", TrainSpec())
    t = _train(session, fixed_now, number="73302", planned_departure_utc=fixed_now - timedelta(hours=2),
               departed_utc=fixed_now - timedelta(hours=2), deadline_utc=fixed_now + timedelta(hours=10))
    _shift(session, t, fixed_now - timedelta(hours=10, minutes=30))
    lon, lat = net.geometry(r)[len(net.geometry(r)) // 5]
    _gps(session, t, fixed_now, lat, lon)
    fresh = evaluate_train(session, t.id, fixed_now, "gps")
    later = fixed_now + timedelta(minutes=20)
    set_fake_now(later)
    stale = evaluate_train(session, t.id, later, "pravidelný prepočet")
    assert fresh.crew["status"] == "ČERVENÁ" and stale.crew["status"] == "ČERVENÁ"
    assert stale.decision != "POKRAČOVAŤ" and stale.color != "ZELENÁ"
    assert datetime.fromisoformat(stale.crew["end_p50"]) >= datetime.fromisoformat(fresh.crew["end_p50"])
    assert any("Rušňovodič by prekročil 13 h" in x for x in stale.reasons)


def test_gps_kazdych_30_min_bez_striedania_rad(cfg, fixed_now):
    """GPS každých 30 min, cyklus agenta každých 15 min: rada sa nestrieda (ODSTAVIŤ / POKRAČOVAŤ s rušňovodičom
    ZELENÁ) a nepríde nové upozornenie každý cyklus."""
    from vlaky.agent.beh import run_cycle
    from vlaky.db import session_scope

    net = network()
    geo = net.geometry(net.route("Čadca", "Košice", TrainSpec()))
    with session_scope() as s:
        seed_borders(s)
        t = _train(s, fixed_now, number="73303", planned_departure_utc=fixed_now - timedelta(hours=2),
                   departed_utc=fixed_now - timedelta(hours=2), deadline_utc=fixed_now + timedelta(hours=10))
        _shift(s, t, fixed_now - timedelta(hours=10, minutes=30))
        tid = t.id
    for k in range(8):
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        if k % 2 == 0:
            lon, lat = geo[len(geo) // 5 + k]
            with session_scope() as s:
                _gps(s, s.get(Train, tid), now - timedelta(minutes=1), lat, lon)
        run_cycle(now)
    with session_scope() as s:
        recs = _recs(s, tid)
        assert len(recs) <= 2, [(x.decision, x.color, x.crew.get("status")) for x in recs]
        assert all(x.decision != "POKRAČOVAŤ" and x.crew.get("status") == "ČERVENÁ" for x in recs)


# ------------------------------------------------------------------ PS2 / PR1 / UI5: vlak v cieli
def test_vlak_v_cieli_podla_gps_sa_neprepocitava_od_vychodiska(session, fixed_now):
    """Vlak stojí v cieli (GPS každý cyklus), za ním výluka pri východisku, rušňovodič 11 h 50 min v službe: žiadna
    eskalácia ani výluka za vlakom, žiadny návrh trasy, príchod = prvá poloha v cieli (neposúva sa) – za 2 h jedno
    odporúčanie a rušňovodič neprekročí 13 h len preto, že poloha z cieľa prichádza ďalej."""
    seed_borders(session)
    net = network()
    r = net.route("Žilina", "Poprad-Tatry", TrainSpec())
    t = _train(session, fixed_now, number="73304", origin="Žilina", destination="Poprad-Tatry",
               planned_departure_utc=fixed_now - timedelta(hours=3), departed_utc=fixed_now - timedelta(hours=3),
               deadline_utc=fixed_now + timedelta(hours=3))
    _shift(session, t, fixed_now - timedelta(hours=11, minutes=50))
    session.add(Closure(kind="uplna", from_station="Žilina", to_station="Varín", status="overene",
                        start_utc=fixed_now, end_utc=fixed_now + timedelta(hours=6)))
    session.flush()
    lon, lat = net.geometry(r)[-1]
    for k in range(9):
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        _gps(session, t, now, lat, lon)
        rec = evaluate_train(session, t.id, now, "pravidelný prepočet")
        assert rec.decision == "POKRAČOVAŤ" and rec.priority != "KRITICKÉ", (rec.decision, rec.summary)
        assert not any("Varín" in x for x in rec.reasons)
        assert rec.path_proposal == {} and rec.variants == []
        assert "vlak je v cieli" in rec.summary and "ukončite jazdu" in rec.summary
        assert rec.chosen["arrival_p50"] == fixed_now.isoformat()  # prvá poloha v cieli
        assert rec.crew["status"] != "ČERVENÁ"
        assert rec.crew["end_p50"] == (fixed_now + timedelta(minutes=30)).isoformat()
    assert len(_recs(session, t.id)) == 1


def test_vlak_tesne_pred_cielom_bez_falosnej_eskalacie(session, fixed_now):
    """GPS 30 m pred Košicami (rovnako ako 500 m pred nimi), výluka Liptovský Mikuláš – Poprad-Tatry 150 km za vlakom:
    nie ESKALOVAŤ KRITICKÉ ani falošné prekročenie 13 h z opakovanej jazdy od východiska."""
    seed_borders(session)
    net = network()
    r = net.route("Čadca", "Košice", TrainSpec())
    session.add(Closure(kind="uplna", from_station="Liptovský Mikuláš", to_station="Poprad-Tatry", status="overene",
                        start_utc=fixed_now, end_utc=fixed_now + timedelta(hours=6)))
    for number, before_km in (("73305", 0.03), ("73321", 0.5)):
        t = _train(session, fixed_now, number=number, planned_departure_utc=fixed_now - timedelta(hours=5),
                   departed_utc=fixed_now - timedelta(hours=5), deadline_utc=fixed_now + timedelta(hours=2))
        _shift(session, t, fixed_now - timedelta(hours=6), name=f"R. {number}")
        _gps(session, t, fixed_now - timedelta(minutes=1), *_point_at(net, r, r.length_m / 1000.0 - before_km))
        rec = evaluate_train(session, t.id, fixed_now, "t")
        assert rec.decision == "POKRAČOVAŤ" and rec.priority != "KRITICKÉ", (before_km, rec.summary)
        assert not any("Liptovský Mikuláš" in x or "prekročil" in x for x in rec.reasons)
        assert rec.crew["status"] == "ZELENÁ" and rec.color == "ZELENÁ"


def test_hlasenie_tis_prichod_v_cieli_bez_kritickej_sms(client, fixed_now):
    """Udalosť jazdy ŽSR/TIS „príchod“ v cieľovej stanici (API) a cyklus agenta: žiadne ESKALOVAŤ ani KRITICKÁ SMS."""
    from vlaky.agent.beh import run_cycle
    from vlaky.db import session_scope

    with session_scope() as s:
        seed_borders(s)
        t = _train(s, fixed_now, number="73306", origin="Žilina", destination="Poprad-Tatry",
                   planned_departure_utc=fixed_now - timedelta(hours=2), departed_utc=fixed_now - timedelta(hours=2),
                   deadline_utc=fixed_now + timedelta(hours=3))
        _shift(s, t, fixed_now - timedelta(hours=3))
        s.add(Closure(kind="uplna", from_station="Žilina", to_station="Varín", status="overene",
                      start_utc=fixed_now, end_utc=fixed_now + timedelta(hours=6)))
        tid = t.id
    arrival = fixed_now - timedelta(minutes=5)
    r = client.post("/api/udalosti-jazdy", headers={"Authorization": "Bearer test-token"},
                    json={"train_number": "73306", "station": "Poprad-Tatry", "event": "prichod",
                          "ts": local(arrival).isoformat(), "source": "tis"})
    assert r.status_code == 200, r.text
    run_cycle(fixed_now, force_evaluate=True)
    with session_scope() as s:
        rec = _recs(s, tid)[-1]
        assert rec.decision == "POKRAČOVAŤ" and rec.priority != "KRITICKÉ"
        assert f"príchod {fmt(arrival)}" in rec.summary and not any("Varín" in x for x in rec.reasons)
        sms = s.scalars(select(Notification).where(Notification.channel == "sms")).all()
        assert sms == []
        rec_id = rec.id
    login_as(client, "disp1", "dispecer")
    page = client.get(f"/odporucania/{rec_id}")
    assert page.status_code == 200 and "vlak je v cieli" in page.text
    assert client.get("/").status_code == 200 and client.get("/api/vlaky").status_code == 200


def test_bez_polohy_podla_planu_v_cieli_overit_polohu(cfg, fixed_now):
    """Bez polohy, odchod pred 7 h na ~3 h jazdu: nie ESKALOVAŤ „ODSTAVIŤ s odchodom +7 h“ s návrhom trasy 0 km a
    KRITICKOU SMS, ale POKRAČOVAŤ ORANŽOVÁ „podľa plánu by už mal byť v cieli – overte polohu“; bez opakovania."""
    from vlaky.agent.beh import run_cycle
    from vlaky.db import session_scope

    with session_scope() as s:
        seed_borders(s)
        t = _train(s, fixed_now, number="73307", planned_departure_utc=fixed_now - timedelta(hours=7),
                   departed_utc=fixed_now - timedelta(hours=7), deadline_utc=fixed_now + timedelta(hours=2))
        _shift(s, t, fixed_now - timedelta(hours=8))
        s.add(Closure(kind="uplna", from_station="Liptovský Mikuláš", to_station="Poprad-Tatry", status="overene",
                      start_utc=fixed_now, end_utc=fixed_now + timedelta(hours=6)))
        tid = t.id
    for k in range(5):
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        run_cycle(now)
    with session_scope() as s:
        recs = _recs(s, tid)
        assert len(recs) == 1
        rec = recs[0]
        assert (rec.decision, rec.color, rec.priority) == ("POKRAČOVAŤ", "ORANŽOVÁ", "DÔLEŽITÉ")
        assert "podľa plánu by už mal byť v cieli" in rec.summary and "overte" in rec.summary
        assert rec.path_proposal == {} and not any("Liptovský Mikuláš" in x for x in rec.reasons)
        assert rec.chosen.get("arrival_floating")
        sms = s.scalars(select(Notification).where(Notification.channel == "sms")).all()
        assert sms == []


def test_vyluka_v_cielovej_stanici_vlak_pred_stanicou(session, fixed_now):
    """Vlak 800 m pred koncom trasy (v obvode cieľa), v cieľovej stanici platí mimoriadnosť: ešte nie je „v cieli“ –
    výluka sa započíta (vlak môže stáť pred stanicou). Na konci trasy je v cieli aj vtedy."""
    seed_borders(session)
    net = network()
    r = net.route("Púchov", "Trenčín", TrainSpec())
    session.add(Closure(kind="mimoriadnost", from_station="Trenčín", to_station="Trenčín", status="overene",
                        start_utc=fixed_now - timedelta(hours=1), end_utc=fixed_now + timedelta(hours=3)))
    out = {}
    for number, before in (("73308", 0.8), ("73309", 0.02)):
        t = _train(session, fixed_now, number=number, origin="Púchov", destination="Trenčín",
                   planned_departure_utc=fixed_now - timedelta(minutes=30),
                   departed_utc=fixed_now - timedelta(minutes=30), deadline_utc=fixed_now + timedelta(hours=8))
        _shift(session, t, fixed_now - timedelta(hours=2), name=f"R. {number}")
        _gps(session, t, fixed_now, *_point_at(net, r, r.length_m / 1000.0 - before))
        out[before] = evaluate_train(session, t.id, fixed_now, "t")
    assert "v cieli" not in out[0.8].summary
    assert any("Trenčín – Trenčín" in x and "na trase vlaku" in x for x in out[0.8].reasons)
    assert "vlak je v cieli" in out[0.02].summary


# ------------------------------------------------------------------ PS1: stály odhad nástupu
def test_v_jazde_z_webu_staly_odhad_nastupu(client, session, fixed_now):
    """Dispečer označí prevzatý vlak „v jazde“ vo webe (bez skutočného času odchodu), plánovaný odchod je až o 4 h, bez
    GPS a bez zmeny rušňovodiča: odhad nástupu sa s časom neposúva (čas zmeny stavu mínus príprava) a predpokladaný
    príchod nevytvára nové odporúčanie každých 30 min."""
    from vlaky.notifikacie.texty import recommendation_body, sms_text

    seed_borders(session)
    t = _train(session, fixed_now, number="73310", status="prevzaty", ready_utc=fixed_now - timedelta(minutes=30),
               planned_departure_utc=fixed_now + timedelta(hours=4), deadline_utc=fixed_now + timedelta(hours=40))
    session.commit()
    login_as(client, "disp1", "dispecer")
    token = csrf(client, f"/vlaky/{t.id}/upravit")
    r = client.post(f"/vlaky/{t.id}/upravit", follow_redirects=False,
                    data={"csrf_token": token, **_form(t, status="v_jazde")})
    assert r.status_code == 303
    session.expire_all()
    assert session.get(Train, t.id).departed_utc is None  # čas úpravy nie je skutočný odchod
    start = fixed_now - timedelta(minutes=60)
    relieve = start + timedelta(hours=13) - timedelta(minutes=30)
    starts, arrivals = set(), []
    for k in range(9):
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        rec = evaluate_train(session, t.id, now, "pravidelný prepočet")
        starts.add(rec.crew.get("start"))
        arrivals.append(datetime.fromisoformat(rec.chosen["arrival_p50"]))
    assert starts == {start.isoformat()}
    assert rec.crew["relieve_by"] == relieve.isoformat() and rec.crew["unknown_start"]
    assert max(arrivals) - min(arrivals) < timedelta(minutes=30)
    assert len(_recs(session, t.id)) == 1
    text = f"odhad: vystriedať najneskôr o {fmt(relieve)} {ESTIMATE}"
    assert text in rec.summary and text in sms_text(rec) and text in recommendation_body(rec)


def test_v_jazde_s_buducim_planom_bez_vymysleneho_nastupu(session, fixed_now):
    """Idúci vlak bez času odchodu (plánovaný odchod je v budúcnosti, vlak išiel skôr) a bez kotvy zmeny stavu:
    odchod je neznámy – „limit 13 h sa nedá overiť“, nie odhad od „teraz“, ktorý by sa každý cyklus posúval."""
    seed_borders(session)
    t = _train(session, fixed_now, number="73311", planned_departure_utc=fixed_now + timedelta(hours=3))
    for k in range(5):
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        rec = evaluate_train(session, t.id, now, "pravidelný prepočet")
        assert rec.crew["unknown_start"] and rec.crew["start"] is None and NO_START in rec.summary
        assert rec.chosen.get("arrival_floating")
    assert len(_recs(session, t.id)) == 1


# ------------------------------------------------------------------ WR1: nástup z mobilu bez naplánovanej zmeny
def test_nastup_z_mobilu_bez_zmeny_zalozi_zmenu(client, cfg, fixed_now):
    """„Nástup na zmenu“ v aplikácii bez naplánovanej zmeny založí skutočnú zmenu (rušňovodič neznámy): plánovač
    počíta 13 h od nahláseného nástupu (nie odhad o hodiny neskôr s textom „nástup nie je zadaný“) a upozornenia
    90/60/30 min idú od neho."""
    from vlaky.agent.strazenie import crew_alerts
    from vlaky.db import session_scope
    from vlaky.notifikacie.texty import recommendation_body, sms_text
    from vlaky.web.rusnovodic import make_driver_token

    with session_scope() as s:
        seed_borders(s)
        t = _train(s, fixed_now, number="73312", origin="Žilina", status="prevzaty",
                   ready_utc=fixed_now, planned_departure_utc=fixed_now + timedelta(hours=4))
        tid = t.id
        token = make_driver_token(t)
    r = client.post("/api/hlasenie", json={"t": token, "typ": "nastup"})
    assert r.status_code == 200 and "Limit pracovného času beží od tohto času" in r.json()["message"]
    assert client.post("/api/hlasenie", json={"t": token, "typ": "nastup"}).status_code == 409  # nedá sa zopakovať
    with session_scope() as s:
        sh = s.scalar(select(DriverShift).where(DriverShift.train_id == tid))
        assert sh.status == "prebieha" and ensure_utc(sh.start_utc) == fixed_now
        assert sh.driver.name == planovac.MOBILE_DRIVER and sh.driver.active is False
        assert s.scalar(select(AuditLog).where(AuditLog.action == "hlasenie_nastup")).detail.get("shift_created")
        mobile_id = sh.id
        # dispečer dodatočne naplánuje zmenu na ten istý čas: ďalšie hlásenie „Nástup“ nevynuluje 13 h limit
        _shift(s, s.get(Train, tid), fixed_now, status="planovana", name="Jozef FIKTÍVNY")
    set_fake_now(fixed_now + timedelta(minutes=15))
    assert client.post("/api/hlasenie", json={"t": token, "typ": "nastup"}).status_code == 409
    with session_scope() as s:
        sh = s.get(DriverShift, mobile_id)
        assert sh.status == "prebieha" and ensure_utc(sh.start_utc) == fixed_now
    set_fake_now(fixed_now + timedelta(hours=4))
    assert client.post("/api/hlasenie", json={"t": token, "typ": "odchod"}).status_code == 200
    with session_scope() as s:
        rec = evaluate_train(s, tid, fixed_now + timedelta(hours=4), "po odchode")
        assert not rec.crew.get("unknown_start") and rec.crew["start"] == fixed_now.isoformat()
        assert rec.crew["relieve_by"] == (fixed_now + timedelta(hours=12, minutes=30)).isoformat()
        for txt in (rec.summary, sms_text(rec), recommendation_body(rec)):
            assert "nástup rušňovodiča nie je zadaný" not in txt
        assert any("nahlásil nástup z mobilu" in x for x in rec.reasons)
    with session_scope() as s:
        assert crew_alerts(s, fixed_now + timedelta(hours=12, minutes=30)) >= 1
        mine = s.scalars(select(Notification).where(Notification.dedup_key == f"crew:{mobile_id}:30")).all()
        assert mine and all("zostáva 30 min" in n.subject for n in mine)


# ------------------------------------------------------------------ WR2: nástup začne zmenu, ktorá je na rade
def test_nastup_z_odkazu_zacne_zmenu_na_rade_nie_vystriedanie(client, cfg, fixed_now):
    """Naplánovaná zmena A o 05:00 a vystriedanie B o 12:00: nástup o 05:00 začne zmenu A (nie B); skoré hlásenie
    o 08:00 sa odmietne s informáciou o vystriedaní; B o 12:00 svoju zmenu začne a zmena A sa ukončí."""
    from vlaky.agent.strazenie import crew_alerts
    from vlaky.db import session_scope
    from vlaky.web.rusnovodic import make_driver_token

    with session_scope() as s:
        seed_borders(s)
        t = _train(s, fixed_now, number="73313", origin="Žilina", status="planovany",
                   ready_utc=fixed_now + timedelta(minutes=30), planned_departure_utc=fixed_now + timedelta(minutes=45))
        a, b = Driver(name="Ján Avšak FIKTÍVNY"), Driver(name="Peter Bystrý FIKTÍVNY")
        s.add_all([a, b])
        s.flush()
        tid, a_id, b_id = t.id, a.id, b.id
        token = make_driver_token(t)
    login_as(client, "planovac1", "planovac")
    tok = csrf(client, "/rusnovodici")
    for drv, start in ((a_id, fixed_now), (b_id, fixed_now + timedelta(hours=7))):
        r = client.post("/zmeny/nova", follow_redirects=False,
                        data={"csrf_token": tok, "driver_id": drv, "train_id": tid, "start_utc": _local(start),
                              "status": "planovana"})
        assert r.status_code == 303

    def shifts():
        with session_scope() as s:
            return {sh.driver_id: (sh.status, ensure_utc(sh.start_utc), sh.actual_end_utc)
                    for sh in s.scalars(select(DriverShift).where(DriverShift.train_id == tid))}

    assert client.post("/api/hlasenie", json={"t": token, "typ": "nastup"}).status_code == 200
    st = shifts()
    assert st[a_id][:2] == ("prebieha", fixed_now) and st[b_id][:2] == ("planovana", fixed_now + timedelta(hours=7))
    with session_scope() as s:
        assert planovac.link_shift(s, tid, fixed_now).driver_id == a_id  # hlavička aplikácie: rušňovodič v službe
    set_fake_now(fixed_now + timedelta(hours=3))
    early = client.post("/api/hlasenie", json={"t": token, "typ": "nastup"})
    assert early.status_code == 409 and "Vystriedanie (Peter Bystrý FIKTÍVNY)" in early.json()["detail"]
    relief = fixed_now + timedelta(hours=7)
    set_fake_now(relief)
    r2 = client.post("/api/hlasenie", json={"t": token, "typ": "nastup"})
    assert r2.status_code == 200 and "Ján Avšak FIKTÍVNY" in r2.json()["message"]
    st = shifts()
    assert st[b_id][:2] == ("prebieha", relief)
    assert st[a_id][0] == "ukoncena" and ensure_utc(st[a_id][2]) == relief
    with session_scope() as s:
        assert crew_alerts(s, fixed_now + timedelta(hours=12, minutes=30)) == 0  # A už nie je v službe, B má rezervu


# ------------------------------------------------------------------ PR3: zaznamenaná zmena začína po odchode
def test_neskory_nastup_zaznamenanej_zmeny_nie_je_zelena(session, fixed_now):
    """Vlak pripravený teraz, trasa o 30 min, zaznamenaná zmena rušňovodiča až o 3 h: nie ZELENÁ, nástup sa nevymýšľa
    (limit od zaznamenaného nástupu) a dôvod navrhne posun nástupu alebo neskorší odchod."""
    from vlaky.notifikacie.texty import recommendation_body

    seed_borders(session)
    t = _train(session, fixed_now, number="73314", status="planovany", ready_utc=fixed_now,
               deadline_utc=fixed_now + timedelta(hours=12))
    session.add(Path(train_id=t.id, train_number=t.number, status="pridelena",
                     departure_utc=fixed_now + timedelta(minutes=30)))
    shift_start = fixed_now + timedelta(hours=3)
    _shift(session, t, shift_start, status="planovana", name="Ján FIKTÍVNY")
    rec = evaluate_train(session, t.id, fixed_now, "t")
    depart = datetime.fromisoformat(rec.chosen["depart"])
    assert depart < shift_start and rec.color != "ZELENÁ"
    assert rec.crew["driver"] == "Ján FIKTÍVNY" and rec.crew["start"] == shift_start.isoformat()
    assert rec.crew.get("late_start") and rec.crew["status"] != "ZELENÁ"
    ideal = depart - timedelta(minutes=60)
    assert rec.crew["suggested_start"] == ideal.isoformat()
    assert any("nastupuje až o" in x and fmt(shift_start) in x and fmt(ideal) in x for x in rec.reasons)
    body = recommendation_body(rec)
    assert f"nástup {fmt(shift_start)}" in body and f"nástup {fmt(ideal)}" not in body
    # zmena naplánovaná včas (odchod mínus príprava) zostáva bez výhrad
    t2 = _train(session, fixed_now, number="73315", status="planovany", ready_utc=fixed_now,
                deadline_utc=fixed_now + timedelta(hours=12))
    session.add(Path(train_id=t2.id, train_number=t2.number, status="pridelena",
                     departure_utc=fixed_now + timedelta(minutes=90)))
    _shift(session, t2, fixed_now + timedelta(minutes=20), status="planovana", name="Jozef FIKTÍVNY")
    rec2 = evaluate_train(session, t2.id, fixed_now, "t")
    assert not rec2.crew.get("late_start") and not any("nastupuje až o" in x for x in rec2.reasons)
    # prvá zmena a naplánované vystriedanie: k odchodu patrí prvá zmena (nie vystriedanie s nástupom po odchode)
    t3 = _train(session, fixed_now, number="73322", status="planovany", ready_utc=fixed_now + timedelta(hours=2),
                deadline_utc=fixed_now + timedelta(hours=14))
    session.add(Path(train_id=t3.id, train_number=t3.number, status="pridelena",
                     departure_utc=fixed_now + timedelta(hours=2)))
    first = _shift(session, t3, fixed_now + timedelta(hours=1), status="planovana", name="Adam FIKTÍVNY")
    _shift(session, t3, fixed_now + timedelta(hours=8), status="planovana", name="Boris FIKTÍVNY")
    rec3 = evaluate_train(session, t3.id, fixed_now, "t")
    assert rec3.crew["driver"] == "Adam FIKTÍVNY" and rec3.crew["start"] == ensure_utc(first.start_utc).isoformat()
    assert not rec3.crew.get("late_start") and not any("nastupuje až o" in x for x in rec3.reasons)


# ------------------------------------------------------------------ UI3: Rušňovodiči – vlaky bez záznamu o nástupe
def test_rusnovodici_ukazu_vlaky_v_jazde_bez_zaznamu(client, session, fixed_now):
    """Idúci vlak bez záznamu o nástupe: stránka Rušňovodiči ho uvedie s textom ako v odporúčaní (len odhad) a návrh
    nasadenia netvrdí „Žiadne vlaky bez rušňovodiča v horizonte“."""
    seed_borders(session)
    t = _train(session, fixed_now, number="73316", planned_departure_utc=fixed_now - timedelta(hours=2))
    other = _train(session, fixed_now, number="73317")  # bez času odchodu – limit sa nedá overiť
    rec = evaluate_train(session, t.id, fixed_now, "t")
    session.commit()
    estimate = f"odhad: vystriedať najneskôr o {fmt(datetime.fromisoformat(rec.crew['relieve_by']))} {ESTIMATE}"
    login_as(client, "disp1", "dispecer")
    page = client.get("/rusnovodici").text
    assert "Vlaky v jazde bez záznamu o nástupe rušňovodiča (2)" in page
    assert f">{t.number}</a>" in page and f">{other.number}</a>" in page
    assert estimate in page and NO_START in page
    proposal = client.get("/rusnovodici?navrh=1").text
    assert "Žiadne vlaky bez rušňovodiča v horizonte." not in proposal
    assert "Vlaky v jazde bez záznamu o nástupe rušňovodiča: 2" in proposal
    # so zmenou, ktorá prebieha, sa vlak v zozname neukáže
    _shift(session, t, fixed_now - timedelta(hours=3))
    session.commit()
    assert "Vlaky v jazde bez záznamu o nástupe rušňovodiča (1)" in client.get("/rusnovodici").text


# ------------------------------------------------------------------ WR0: oprava omylom nahláseného odchodu
def _standing_train(session, now, number):
    since = now - timedelta(hours=17)
    t = _train(session, now, number=number, status="odstaveny", ready_utc=since, standing_since_utc=since)
    session.add(Closure(kind="uplna", from_station="Liptovský Mikuláš", to_station="Poprad-Tatry", status="overene",
                        start_utc=now - timedelta(hours=1), end_utc=now + timedelta(hours=5)))
    session.commit()
    return t, since


def test_oprava_omylom_nahlaseneho_odchodu_neresetuje_18h(client, session, fixed_now):
    """Vlak stojí 17 h (ESKALOVAŤ – zostáva 1 h), rušňovodič omylom ťukne „Odchod“ a dispečer o 3 min vráti stav na
    „odstavený“: limit 18 h beží ďalej od začiatku státia (nie ODSTAVIŤ ZELENÁ s novými 18 h)."""
    from vlaky.web.rusnovodic import make_driver_token

    seed_borders(session)
    t, since = _standing_train(session, fixed_now, "73318")
    before = evaluate_train(session, t.id, fixed_now, "pred")
    assert (before.decision, before.color) == ("ESKALOVAŤ", "ČERVENÁ")
    token = make_driver_token(t)
    session.commit()
    assert client.post("/api/hlasenie", json={"t": token, "typ": "odchod"}).status_code == 200
    set_fake_now(fixed_now + timedelta(minutes=3))
    login_as(client, "disp1", "dispecer")
    tok = csrf(client, f"/vlaky/{t.id}/upravit")
    session.expire_all()
    r = client.post(f"/vlaky/{t.id}/upravit", follow_redirects=True,
                    data={"csrf_token": tok, **_form(session.get(Train, t.id), status="odstaveny")})
    assert r.status_code == 200 and "Odchod sa považuje za omyl" in r.text
    session.expire_all()
    t2 = session.get(Train, t.id)
    assert t2.status == "odstaveny" and ensure_utc(t2.standing_since_utc) == since and t2.departed_utc is None
    after = evaluate_train(session, t.id, fixed_now + timedelta(minutes=4), "po oprave")
    assert (after.decision, after.color) == ("ESKALOVAŤ", "ČERVENÁ")
    assert any(f"Vlak stojí od {fmt(since)}" in x for x in after.reasons)
    audit = session.scalar(select(AuditLog).where(AuditLog.action == "train_update", AuditLog.entity_id == str(t.id)))
    assert audit.detail.get("oprava_odchodu") is True


def test_skutocna_jazda_alebo_polohy_rozhodnu_o_oprave(client, session, fixed_now):
    """Odchod pred 2 h a polohy ukazujú jazdu (12 km) → odstavenie je nové státie (od teraz). Odchod pred 2 h a polohy
    z mobilu ukazujú, že vlak stál na mieste → oprava aj po 2 h (limit 18 h beží od pôvodného začiatku)."""
    from vlaky.web.rusnovodic import make_driver_token

    seed_borders(session)
    net = network()
    r = net.route("Čadca", "Košice", TrainSpec())
    start_pt, far_pt = _point_at(net, r, 0.5), _point_at(net, r, 12.5)
    results = {}
    login_as(client, "disp1", "dispecer")
    for number, end_pt in (("73319", far_pt), ("73320", start_pt)):
        t, since = _standing_train(session, fixed_now, number)
        _gps(session, t, fixed_now - timedelta(hours=2, minutes=5), *start_pt, source="driver")
        token = make_driver_token(t)
        session.commit()
        set_fake_now(fixed_now - timedelta(hours=2))
        assert client.post("/api/hlasenie", json={"t": token, "typ": "odchod"}).status_code == 200
        session.expire_all()
        _gps(session, session.get(Train, t.id), fixed_now - timedelta(minutes=5), *end_pt, source="driver")
        session.commit()
        set_fake_now(fixed_now)
        tok = csrf(client, f"/vlaky/{t.id}/upravit")
        session.expire_all()
        resp = client.post(f"/vlaky/{t.id}/upravit", follow_redirects=False,
                           data={"csrf_token": tok, **_form(session.get(Train, t.id), status="odstaveny")})
        assert resp.status_code == 303
        session.expire_all()
        results[number] = (ensure_utc(session.get(Train, t.id).standing_since_utc), since)
    moved_since, _ = results["73319"]
    stayed_since, orig = results["73320"]
    assert moved_since == fixed_now  # po skutočnej jazde státie začína odstavením
    assert stayed_since == orig      # vlak nevyšiel – pôvodný začiatok státia
