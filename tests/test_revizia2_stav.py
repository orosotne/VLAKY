"""Regresné testy k 2. revízii – stav vlaku v plánovači: kotva limitu 18 h (PPŽS), opakované odporúčania pri
plávajúcom odchode, neznámy nástup rušňovodiča (len odhad, nie fakt) a izolácia chyby jedného vlaku."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from test_web_spolocne import csrf, login_as
from vlaky.cas import ensure_utc, fmt, local, set_fake_now
from vlaky.hranice import find_border, seed_borders
from vlaky.models import (AgentHealth, AuditLog, Closure, Driver, DriverShift, Handover, Notification, Path,
                          PositionReport, Recommendation, Train)
from vlaky.planovanie import planovac
from vlaky.planovanie.planovac import evaluate_all, evaluate_train
from vlaky.siet.graf import TrainSpec, network

pytest_plugins = ["test_web_spolocne"]

NO_START = "limit 13 h sa nedá overiť – zadajte nástup rušňovodiča"


def _train(session, now, path_at=None, **kw):
    data = dict(number="70100", run_date=now.date().isoformat(), origin="Čadca", destination="Košice",
                traction="E", gauge="1435", vmax_kmh=100, status="planovany", deadline_utc=now + timedelta(hours=30))
    data.update(kw)
    t = Train(**data)
    session.add(t)
    session.flush()
    if path_at is not None:
        session.add(Path(train_id=t.id, train_number=t.number, status="pridelena",
                         departure_utc=now + timedelta(minutes=path_at)))
        session.flush()
    return t


def _closure(session, now, start_h=-1.0, end_h=5.0):
    session.add(Closure(kind="uplna", from_station="Liptovský Mikuláš", to_station="Poprad-Tatry",
                        start_utc=now + timedelta(hours=start_h), end_utc=now + timedelta(hours=end_h),
                        status="overene"))
    session.flush()


def _hold(rec) -> list:
    return [r for r in rec.reasons if "stojí od" in r]


def _depart(rec):
    return datetime.fromisoformat(rec.chosen["depart"]) if rec.chosen.get("depart") else None


def _recs(session, train_id) -> list:
    return list(session.scalars(select(Recommendation).where(Recommendation.train_id == train_id)
                                .order_by(Recommendation.id)))


def _local(dt) -> str:
    return local(dt).strftime("%Y-%m-%dT%H:%M")


def _form(t, **kw) -> dict:
    data = {"number": t.number, "run_date": t.run_date, "origin": t.origin, "destination": t.destination,
            "gauge": t.gauge, "traction": t.traction, "vmax_kmh": str(int(t.vmax_kmh)), "status": t.status,
            "deadline_utc": _local(t.deadline_utc)}
    data.update(kw)
    return data


# ------------------------------------------------------------------ kotva limitu 18 h
def test_prevzatie_cez_web_drzi_kotvu_od_prichodu(client, session, fixed_now):
    """[30] Zaznamenanie prevzatia na hranici nesmie zrušiť kotvu 18 h (príchod na hranicu)."""
    seed_borders(session)
    bc = find_border(session, "Čadca")
    t = _train(session, fixed_now, status="na_ceste_k_hranici", entry_border_id=bc.id,
               deadline_utc=fixed_now + timedelta(hours=40))
    h = Handover(train_id=t.id, border_id=bc.id, direction="vstup", status="ocakavany",
                 avizo_eta_utc=fixed_now - timedelta(hours=17, minutes=30))
    session.add(h)
    _closure(session, fixed_now)
    session.commit()
    login_as(client, "disp1", "dispecer")
    token = csrf(client, "/hranice")
    arrival = fixed_now - timedelta(hours=17)
    assert client.post(f"/hranice/preberanie/{h.id}/prisiel", follow_redirects=False,
                       data={"csrf_token": token, "cas": _local(arrival)}).status_code == 303
    session.expire_all()
    first = evaluate_train(session, t.id, fixed_now, "po príchode")
    assert first.decision == "ESKALOVAŤ"
    session.commit()
    assert client.post(f"/hranice/preberanie/{h.id}/prevzaty", follow_redirects=False,
                       data={"csrf_token": token, "cas": _local(fixed_now - timedelta(hours=15)),
                             "findings": "bez závad"}).status_code == 303
    session.expire_all()
    rec = evaluate_train(session, t.id, fixed_now + timedelta(minutes=1), "po prevzatí")
    assert rec.decision == "ESKALOVAŤ" and rec.color == "ČERVENÁ"
    assert any(f"Vlak stojí od {fmt(arrival)}" in r and "zostáva 1.0 h" in r for r in rec.reasons)
    t2 = session.get(Train, t.id)
    assert t2.status == "prevzaty" and ensure_utc(t2.standing_since_utc) == arrival  # prevzatie kotvu nemení


@pytest.mark.parametrize("status", ["odstaveny", "prevzaty"])
def test_hlasenie_pripraveny_neposunie_kotvu(client, session, fixed_now, status):
    """[29] „Pripravený“ od rušňovodiča stojaceho vlaku nesmie posunúť minulý čas pripravenosti (kotvu 18 h)."""
    from vlaky.web.rusnovodic import make_driver_token

    seed_borders(session)
    since = fixed_now - timedelta(hours=17)
    t = _train(session, fixed_now, status=status, ready_utc=since)
    _closure(session, fixed_now)
    session.commit()
    before = evaluate_train(session, t.id, fixed_now, "pred")
    assert before.decision == "ESKALOVAŤ"
    token = make_driver_token(t)
    session.commit()
    assert client.post("/api/hlasenie", json={"t": token, "typ": "pripraveny"}).status_code == 200
    later = fixed_now + timedelta(minutes=2)
    set_fake_now(later)
    session.expire_all()
    assert ensure_utc(session.get(Train, t.id).ready_utc) == since
    a = session.scalar(select(AuditLog).where(AuditLog.action == "hlasenie_pripraveny"))
    assert a.detail["previous_ready"] and a.detail.get("ready_unchanged") is True
    after = evaluate_train(session, t.id, later, "po hlásení")
    assert after.decision == "ESKALOVAŤ"
    assert after.chosen.get("depart") is None or _depart(after) <= since + timedelta(hours=18)
    assert any(f"Vlak stojí od {fmt(since)}" in r for r in after.reasons)


def test_uprava_pripravenosti_stojaceho_vlaku_neposunie_kotvu(client, session, fixed_now):
    """[29] Ani dispečer úpravou času pripravenosti (napr. na zajtra) nezruší kotvu staršieho záznamu odstaveného
    vlaku – začiatok státia sa pri úprave zapíše z doterajších údajov."""
    seed_borders(session)
    since = fixed_now - timedelta(hours=16)
    t = _train(session, fixed_now, status="odstaveny", ready_utc=since)
    _closure(session, fixed_now, end_h=10)
    session.commit()
    login_as(client, "disp1", "dispecer")
    token = csrf(client, f"/vlaky/{t.id}/upravit")
    assert client.post(f"/vlaky/{t.id}/upravit", follow_redirects=False,
                       data={"csrf_token": token, **_form(t, ready_utc=_local(fixed_now + timedelta(hours=20)),
                                                          notes="pokračuje po výluke")}).status_code == 303
    session.expire_all()
    rec = evaluate_train(session, t.id, fixed_now, "po úprave")
    # stojí 16 h – limit ešte neuplynul, ale uplynie skôr, ako bude vlak pripravený (3. revízia, OD3)
    assert any(f"Vlak stojí od {fmt(since)} – limit 18 h státia (PPŽS) uplynie {fmt(since + timedelta(hours=18))}"
               in r for r in rec.reasons)
    # aj odchod hneď po novej pripravenosti by bol po limite – žiadny odchod sa neponúka, len eskalácia
    assert rec.decision == "ESKALOVAŤ" and rec.color == "ČERVENÁ"
    assert ensure_utc(session.get(Train, t.id).standing_since_utc) == since


def test_prevzaty_vlak_bez_pripravenosti_kotvi_prichod(session, fixed_now):
    """[3] Prevzatý vlak (záznam bez začiatku státia, bez času pripravenosti) – kotva je príchod na hranicu."""
    seed_borders(session)
    bc = find_border(session, "Čadca")
    t = _train(session, fixed_now, path_at=120, status="prevzaty", entry_border_id=bc.id,
               deadline_utc=fixed_now + timedelta(hours=40))
    arrival = fixed_now - timedelta(hours=13)
    session.add(Handover(train_id=t.id, border_id=bc.id, direction="vstup", status="prevzaty", arrived_utc=arrival,
                         taken_over_utc=fixed_now - timedelta(hours=11)))
    _closure(session, fixed_now, end_h=10)
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision == "ESKALOVAŤ"
    assert any(f"Vlak stojí od {fmt(arrival)}" in r and "zostáva 5.0 h" in r for r in rec.reasons)
    assert rec.chosen.get("depart") is None or _depart(rec) <= arrival + timedelta(hours=18)


def test_odstavenie_po_jazde_nekotvi_minulu_trasu(client, session, fixed_now):
    """[5] Vlak išiel po svojej trase a bol odstavený až teraz: 18 h sa ráta od odstavenia, nie od trasy."""
    from vlaky.web.rusnovodic import make_driver_token

    seed_borders(session)
    departed = fixed_now - timedelta(hours=11)
    t = _train(session, fixed_now, path_at=-11 * 60, status="prevzaty", ready_utc=fixed_now - timedelta(hours=12),
               deadline_utc=fixed_now + timedelta(hours=40))
    _closure(session, fixed_now, end_h=10)
    session.commit()
    token = make_driver_token(t)
    session.commit()
    set_fake_now(departed)
    assert client.post("/api/hlasenie", json={"t": token, "typ": "odchod"}).status_code == 200
    session.expire_all()
    t1 = session.get(Train, t.id)
    assert t1.status == "v_jazde"
    set_fake_now(fixed_now)
    login_as(client, "disp1", "dispecer")
    token_web = csrf(client, f"/vlaky/{t.id}/upravit")
    r = client.post(f"/vlaky/{t.id}/upravit", follow_redirects=False,
                    data={"csrf_token": token_web, **_form(t1, status="odstaveny",
                                                           ready_utc=_local(fixed_now - timedelta(hours=12)))})
    assert r.status_code == 303
    session.expire_all()
    rec = evaluate_train(session, t.id, fixed_now, "po odstavení")
    assert rec.decision != "ESKALOVAŤ" and not _hold(rec)
    t2 = session.get(Train, t.id)
    assert t2.status == "odstaveny" and ensure_utc(t2.standing_since_utc) == fixed_now
    assert ensure_utc(t2.departed_utc) == departed  # skutočný odchod z hlásenia rušňovodiča
    # starší záznam bez začiatku státia: odstavený vlak s pripravenosťou teraz a už použitou trasou (bez kotvy)
    old = _train(session, fixed_now, number="70101", path_at=-11 * 60, status="odstaveny", ready_utc=fixed_now,
                 planned_departure_utc=fixed_now - timedelta(hours=11), deadline_utc=fixed_now + timedelta(hours=40))
    rec_old = evaluate_train(session, old.id, fixed_now, "test")
    assert rec_old.decision != "ESKALOVAŤ" and not _hold(rec_old)


def test_odstavenie_cakajuceho_vlaku_neposunie_kotvu(client, session, fixed_now):
    """Vlak čakal pripravený od odchodu svojej trasy a nevyšiel; zmena stavu na odstavený kotvu neposunie."""
    seed_borders(session)
    path_dep = fixed_now - timedelta(hours=10)
    t = _train(session, fixed_now, path_at=-10 * 60, ready_utc=fixed_now - timedelta(hours=11))
    _closure(session, fixed_now, end_h=10)
    session.commit()
    first = evaluate_train(session, t.id, fixed_now, "pred")
    assert any(f"Vlak stojí od {fmt(path_dep)}" in r for r in first.reasons)
    session.commit()
    login_as(client, "disp1", "dispecer")
    token = csrf(client, f"/vlaky/{t.id}/upravit")
    assert client.post(f"/vlaky/{t.id}/upravit", follow_redirects=False,
                       data={"csrf_token": token, **_form(t, status="odstaveny",
                                                          ready_utc=_local(t.ready_utc))}).status_code == 303
    session.expire_all()
    rec = evaluate_train(session, t.id, fixed_now + timedelta(minutes=1), "po odstavení")
    assert any(f"Vlak stojí od {fmt(path_dep)}" in r for r in rec.reasons)
    t2 = session.get(Train, t.id)
    assert t2.status == "odstaveny" and ensure_utc(t2.standing_since_utc) == path_dep


def test_vcerajsia_trasa_rovnakeho_cisla_sa_nepouzije(session, fixed_now):
    """[5] Trasa denného vlaku s tým istým číslom zo včera (bez priradeného vlaku) nie je kotvou ani trasou."""
    seed_borders(session)
    t = _train(session, fixed_now, path_at=60, ready_utc=fixed_now + timedelta(hours=1),
               deadline_utc=fixed_now + timedelta(hours=40))
    yesterday = Path(train_id=None, train_number=t.number, status="pridelena",
                     departure_utc=fixed_now - timedelta(hours=23, minutes=30))
    tomorrow = Path(train_id=None, train_number=t.number, status="pridelena",
                    departure_utc=fixed_now + timedelta(hours=24, minutes=30))
    session.add_all([yesterday, tomorrow])
    _closure(session, fixed_now, start_h=2, end_h=8)
    ids = {p.id for p in planovac.allocated_paths(session, t)}
    assert yesterday.id not in ids and tomorrow.id not in ids
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision != "ESKALOVAŤ" and not _hold(rec)
    # trasa z e-mailu pre deň jazdy, kým vlak nebol zadaný, platí
    other = _train(session, fixed_now, number="70102", ready_utc=fixed_now + timedelta(hours=1))
    same_day = Path(train_id=None, train_number="70102", status="pridelena",
                    departure_utc=fixed_now + timedelta(hours=2))
    session.add(same_day)
    session.flush()
    assert [p.id for p in planovac.allocated_paths(session, other)] == [same_day.id]


def test_vlak_v_zahranici_so_zmeskanou_trasou_nestoji(session, fixed_now):
    """[5] Vlak ešte v zahraničí (avízo +14 h) nestojí na koľajach ŽSR – zmeškaná trasa nie je kotvou 18 h."""
    seed_borders(session)
    bc = find_border(session, "Čadca")
    t = _train(session, fixed_now, path_at=-60, status="na_ceste_k_hranici", entry_border_id=bc.id,
               deadline_utc=fixed_now + timedelta(hours=60))
    session.add(Handover(train_id=t.id, border_id=bc.id, direction="vstup", status="ocakavany",
                         avizo_eta_utc=fixed_now + timedelta(hours=14)))
    _closure(session, fixed_now, start_h=16, end_h=22)
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision != "ESKALOVAŤ" and not _hold(rec)


# ------------------------------------------------------------------ plávajúci odchod (churn)
def test_plavajuci_odchod_novej_trasy_nevytvara_nove_odporucania(session, fixed_now):
    """[2] Bez zmeny situácie (najskoršia nová trasa = teraz + 6 h) žiadne nové odporúčanie každých 30 min."""
    from vlaky.notifikacie.odosielanie import recommendation_dedup_key

    seed_borders(session)
    t = _train(session, fixed_now, ready_utc=fixed_now)
    keys = []
    for k in range(9):
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        rec = evaluate_train(session, t.id, now, "pravidelný prepočet")
        assert rec.chosen.get("floating") or rec.chosen.get("baseline_floating")
        if k == 1:
            rec.state = "prijate"  # dispečer prijal (objednáva trasu)
            session.flush()
        keys.append(recommendation_dedup_key(rec))
    recs = _recs(session, t.id)
    assert len(recs) == 1 and recs[0].state == "prijate" and len(set(keys)) == 1
    # pevný odchod (koniec výluky) sa porovnáva ako doteraz
    c = _train(session, fixed_now, number="70103", path_at=0, ready_utc=fixed_now)
    _closure(session, fixed_now, end_h=10)
    for k in range(5):
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        evaluate_train(session, c.id, now, "pravidelný prepočet")
    assert len(_recs(session, c.id)) == 1


def test_nezmenena_eskalacia_posle_jednu_sms(cfg):
    """[40] Nezmenená eskalácia (najmenej zlá možnosť sa posúva s časom) = jedna KRITICKÁ SMS, nie každých 40 min."""
    from vlaky.agent.beh import run_cycle
    from vlaky.agent.zdravie import set_kv
    from vlaky.db import session_scope

    t0 = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)
    with session_scope() as s:
        s.add(Train(number="77001", origin="Plaveč", destination="Košice", traction="M", gauge="1435",
                    vmax_kmh=80, status="planovany", ready_utc=t0, deadline_utc=t0 + timedelta(hours=5)))
    for i in range(19):  # 3 h, cyklus každých 10 min
        now = t0 + timedelta(minutes=10 * i)
        set_fake_now(now)
        with session_scope() as s:
            set_kv(s, "mail_last_ok", {"ts": now.isoformat(), "fetched": 0})
        run_cycle(now)
    with session_scope() as s:
        recs = list(s.scalars(select(Recommendation)))
        sms = list(s.scalars(select(Notification).where(Notification.channel == "sms",
                                                        Notification.status.in_(("outbox", "odoslana")))))
        assert {(r.decision, r.color) for r in recs} == {("ESKALOVAŤ", "ČERVENÁ")}
        assert len(recs) == 1 and len(sms) == 1


def test_iduci_vlak_podla_planu_bez_opakovania(cfg, fixed_now):
    """[2] Idúci vlak (odchod = teraz): bez zmeny žiadne nové odporúčanie; posun príchodu o 30 min a viac áno."""
    from vlaky.agent.beh import run_cycle
    from vlaky.db import session_scope
    from vlaky.simulacia.demo import _position_at

    with session_scope() as s:
        seed_borders(s)
        d = Driver(name="R. FIKTÍVNY")
        s.add(d)
        s.flush()
        t = Train(number="70104", origin="Čadca", destination="Košice", traction="E", gauge="1435", vmax_kmh=100,
                  status="v_jazde", planned_departure_utc=fixed_now - timedelta(minutes=5),
                  deadline_utc=fixed_now + timedelta(hours=30))
        s.add(t)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=fixed_now - timedelta(hours=1), status="prebieha"))
        tid = t.id

    def cycle(k, minutes_run):
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        with session_scope() as s:
            pos, _ = _position_at(s.get(Train, tid), minutes_run)
            s.add(PositionReport(train_id=tid, train_number="70104", ts_utc=now, lat=pos[0], lon=pos[1], source="gps"))
        run_cycle(now)

    for k in range(9):  # 2 h podľa plánu
        cycle(k, 5 + 15 * k)
    with session_scope() as s:
        recs = _recs(s, tid)
        assert len(recs) == 1 and recs[0].decision == "POKRAČOVAŤ" and recs[0].chosen.get("floating")
    for k in range(9, 12):  # vlak stojí (rovnaká poloha) – príchod sa posúva
        cycle(k, 5 + 15 * 8)
    with session_scope() as s:
        recs = _recs(s, tid)
        assert len(recs) == 2 and recs[0].state == "zastarane" and recs[1].state == "nove"
        shift = datetime.fromisoformat(recs[1].chosen["arrival_p50"]) - datetime.fromisoformat(
            recs[0].chosen.get("baseline_arrival_p50") or recs[0].chosen["arrival_p50"])
        assert shift >= timedelta(minutes=30)


# ------------------------------------------------------------------ neznámy nástup rušňovodiča
def _running(session, now, idx_frac=3, **kw):
    t = _train(session, now, status="v_jazde", **kw)
    net = network()
    geo = net.geometry(net.route("Čadca", "Košice", TrainSpec()))
    lon, lat = geo[len(geo) // idx_frac]
    session.add(PositionReport(train_id=t.id, train_number=t.number, ts_utc=now, lat=lat, lon=lon, source="gps"))
    session.flush()
    return t


def test_neznamy_nastup_je_odhad_od_odchodu(session, fixed_now):
    """[4] Idúci vlak bez záznamu o nástupe: konzervatívny odhad od plánovaného odchodu, vo všetkých textoch
    len ako odhad (nikdy nie vymyslený nástup „teraz − 60 min“)."""
    from vlaky.notifikacie.texty import recommendation_body, sms_text

    seed_borders(session)
    planned = fixed_now - timedelta(hours=12)
    t = _running(session, fixed_now, planned_departure_utc=planned)
    rec = evaluate_train(session, t.id, fixed_now, "test")
    crew = rec.crew
    start = planned - timedelta(minutes=60)
    relieve = start + timedelta(hours=13) - timedelta(minutes=30)
    assert crew["start"] == start.isoformat() and crew["relieve_by"] == relieve.isoformat()
    assert crew["unknown_start"] and crew.get("estimated") and crew["driver"] == "neznámy (chýba záznam o nástupe)"
    assert crew["status"] == "ČERVENÁ" and rec.color != "ZELENÁ"
    text = f"odhad: vystriedať najneskôr o {fmt(relieve)} (nástup rušňovodiča nie je zadaný)"
    body = recommendation_body(rec)
    assert text in sms_text(rec) and text in rec.summary and text in body
    invented = fmt(fixed_now - timedelta(minutes=60))
    assert f"nástup {invented}" not in body and "Vystriedať najneskôr o" not in body
    assert "vystriedať rušňovodiča do" not in sms_text(rec)


def test_neznamy_nastup_bez_casu_odchodu(client, session, fixed_now):
    """[4] Bez známeho času odchodu sa časy rušňovodiča neuvádzajú; všetky výstupy to povedia a nespadnú."""
    from vlaky.notifikacie.texty import recommendation_body, sms_text

    seed_borders(session)
    t = _running(session, fixed_now)
    d = Driver(name="Ján FIKTÍVNY")
    session.add(d)
    session.flush()
    session.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=fixed_now + timedelta(hours=3),
                            status="planovana"))  # naplánovaná zmena, nástup nezaznamenaný
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    crew = rec.crew
    assert all(crew.get(k) is None for k in ("start", "limit", "relieve_by", "left_min"))
    assert crew["unknown_start"] and crew["driver"] == "Ján FIKTÍVNY (chýba záznam o nástupe)"
    assert crew["status"] == "ORANŽOVÁ"
    for txt in (sms_text(rec), rec.summary, recommendation_body(rec)):
        assert NO_START in txt
    session.commit()
    login_as(client, "obchod", "obchod")
    page = client.get(f"/odporucania/{rec.id}")
    assert page.status_code == 200 and NO_START in page.text and "Vystriedať najneskôr o" not in page.text
    drivers = client.get("/rusnovodici")
    assert drivers.status_code == 200 and NO_START in drivers.text
    rows = client.get("/api/vlaky").json()["vlaky"]
    drv = next(r for r in rows if r["id"] == t.id)
    assert drv["driver"]["unknown_start"] and drv["driver"]["left_min"] is None
    assert drv["driver"]["relieve_by"] is None and drv["driver"]["relieve_note"] == NO_START
    assert drv["risk"] == f"Rušňovodič: {NO_START}"
    assert client.get("/").status_code == 200


def test_odchod_hlasenim_je_skutocny_odchod_pre_odhad(client, session, fixed_now):
    """[4] Hlásenie „odchod“ zapíše skutočný čas odchodu – odhad nástupu vychádza z neho, nie zo starého plánu."""
    from vlaky.web.rusnovodic import make_driver_token

    seed_borders(session)
    t = _train(session, fixed_now, status="prevzaty", planned_departure_utc=fixed_now - timedelta(hours=5))
    t.standing_since_utc = fixed_now - timedelta(hours=6)
    session.commit()
    token = make_driver_token(t)
    session.commit()
    departed = fixed_now - timedelta(hours=2)
    set_fake_now(departed)
    assert client.post("/api/hlasenie", json={"t": token, "typ": "odchod"}).status_code == 200
    set_fake_now(fixed_now)
    session.expire_all()
    net = network()
    geo = net.geometry(net.route("Čadca", "Košice", TrainSpec()))
    lon, lat = geo[len(geo) // 4]
    session.add(PositionReport(train_id=t.id, train_number=t.number, ts_utc=fixed_now, lat=lat, lon=lon, source="gps"))
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.crew["start"] == (departed - timedelta(minutes=60)).isoformat()
    t2 = session.get(Train, t.id)
    assert t2.status == "v_jazde" and t2.standing_since_utc is None and ensure_utc(t2.departed_utc) == departed


# ------------------------------------------------------------------ izolácia chyby jedného vlaku
def test_chyba_databazy_jedneho_vlaku_nezastavi_ostatne(cfg, fixed_now, monkeypatch):
    """[6] Chyba zápisu pri jednom vlaku (SAVEPOINT) – ostatné vlaky sa prepočítajú, chybný dostane ESKALOVAŤ."""
    from vlaky.agent.beh import run_cycle
    from vlaky.db import session_scope

    with session_scope() as s:
        seed_borders(s)
        ta = _train(s, fixed_now, number="70105", path_at=30, ready_utc=fixed_now + timedelta(minutes=30)).id
        tb = _train(s, fixed_now, number="70106", path_at=30, ready_utc=fixed_now + timedelta(minutes=30)).id
    run_cycle(fixed_now, force_evaluate=True)
    with session_scope() as s:
        assert all(_recs(s, tid)[-1].decision == "VYPRAVIŤ" for tid in (ta, tb))
        _closure(s, fixed_now, start_h=1, end_h=8)
    real = planovac.evaluate_train

    def broken(session, train_id, now=None, trigger=""):
        if train_id == ta:  # databáza zápis odmietne (napr. NOT NULL) – relácia potrebuje rollback
            session.add(Recommendation(train_id=None, decision="X", color="X"))
            session.flush()
        return real(session, train_id, now, trigger)

    monkeypatch.setattr(planovac, "evaluate_train", broken)
    later = fixed_now + timedelta(minutes=20)
    set_fake_now(later)
    summary = run_cycle(later, force_evaluate=True)
    assert summary["evaluated"] == 2 and not any("PendingRollback" in e for e in summary["errors"])
    with session_scope() as s:
        rb = _recs(s, tb)[-1]
        ra = _recs(s, ta)[-1]
        assert rb.decision != "VYPRAVIŤ" and rb.state == "nove"  # nová výluka sa prejavila
        assert ra.decision == "ESKALOVAŤ" and "nedokázal prepočítať" in ra.summary
        health = s.scalar(select(AgentHealth).where(AgentHealth.component == "planovanie")
                          .order_by(AgentHealth.id.desc()))
        assert health.status == "warn"
        before = s.scalar(select(func.count(AuditLog.id)))
    # SAVEPOINT na SQLite: uvoľnenie vnorenej transakcie nesmie potvrdiť vonkajšiu
    from vlaky.config import settings
    from vlaky.db import _SESSIONS

    fresh = _SESSIONS[settings().database_url]()
    try:
        _closure(fresh, later, start_h=0.5, end_h=12)
        fresh.commit()
        evaluate_all(fresh, later + timedelta(minutes=5), "test")
        fresh.rollback()
    finally:
        fresh.close()
    with session_scope() as s:
        assert s.scalar(select(func.count(AuditLog.id))) == before  # ani audit chyby, ani odporúčania
