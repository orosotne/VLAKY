"""Regresné testy k 4. revízii – idúci vlak (oblasť P4 „poistky“).

* QP2 + jazda-R5: riedke alebo prerušované hlásenia polohy (GPS každých 30 min, udalosti TIS z hlavných staníc).
  Pri starej polohe nikdy ZELENÁ, ale zmena farby len pre starnúcu polohu aktualizuje doterajšie odporúčanie bez
  nového odporúčania a upozornenia; zhoršenie, ktoré trvá aspoň 30 min, je nové odporúčanie. Hlásenie TIS
  z východiskovej stanice (bod stanice mimo hrán trasy) nie je poloha mimo trasy.
* QP3 + jazda-R7/R8: idúci vlak tesne pred cieľom (čaká pred vjazdom, alebo pred zatvorenou cieľovou stanicou)
  nedostane ODSTAVIŤ ani návrh novej ad hoc trasy pre posledný kilometer – POKRAČOVAŤ alebo POČKAŤ pred cieľom;
  rušňovodič, ktorého čas vystriedania už uplynul, „vystriedať ihneď“; kontrolný zoznam pred odchodom sa idúcemu
  vlaku neposiela.

Všetky vlaky a adresy sú fiktívne.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from sqlalchemy import select

from vlaky.cas import local, set_fake_now
from vlaky.hranice import seed_borders
from vlaky.models import Closure, Driver, DriverShift, Notification, PositionReport, Recommendation, Train
from vlaky.notifikacie.texty import crew_relief_text, recommendation_body, sms_text
from vlaky.planovanie.planovac import _km_at_minutes, evaluate_train
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


def _running(session, now, number, origin="Čadca", destination="Košice", departed=None, shift_start=None,
             deadline_h=10) -> Train:
    dep = departed if departed is not None else now - timedelta(minutes=10)
    t = Train(number=number, run_date=local(dep).date().isoformat(), origin=origin, destination=destination,
              traction="E", gauge="1435", vmax_kmh=100, status="v_jazde", planned_departure_utc=dep,
              departed_utc=dep, deadline_utc=now + timedelta(hours=deadline_h))
    session.add(t)
    session.flush()
    d = Driver(name=f"R. {number} FIKTÍVNY")
    session.add(d)
    session.flush()
    session.add(DriverShift(driver_id=d.id, train_id=t.id, status="prebieha",
                            start_utc=shift_start if shift_start is not None else now - timedelta(hours=1)))
    session.flush()
    return t


def _gps(session, train, ts, lat, lon, source="gps"):
    session.add(PositionReport(train_id=train.id, train_number=train.number, ts_utc=ts, lat=lat, lon=lon,
                               source=source))
    session.flush()


def _recs(session, train_id) -> list:
    return list(session.scalars(select(Recommendation).where(Recommendation.train_id == train_id)
                                .order_by(Recommendation.id)))


def _on_time_gps(session, net, r, t, dep, ts):
    """GPS v bode, kde je vlak podľa plánu (odchod ``dep``) v čase ``ts``."""
    km = _km_at_minutes(r, (ts - dep).total_seconds() / 60.0)
    _gps(session, t, ts, *_point_at(net, r, km))


# ====================================================================== QP2 / jazda-R5 riedke polohy
def test_qp2_gps_kazdych_30_min_jedno_odporucanie_a_nikdy_zelena_pri_starej_polohe(session, fixed_now):
    """[QP2b / jazda-R5] Vlak ide načas, GPS chodí každých 30 min, prepočet každých 15 min (poloha je stará po 15 min).
    Pri starej polohe nie je ZELENÁ, pri čerstvej áno – ale stále je to to isté odporúčanie (zmena farby len pre
    starnúcu polohu), nie deväť nových odporúčaní."""
    seed_borders(session)
    net = network()
    r = net.route("Čadca", "Košice", TrainSpec())
    dep = fixed_now - timedelta(minutes=10)
    t = _running(session, fixed_now, "79601", departed=dep)
    session.commit()
    seen = []
    for k in range(9):
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        if k % 2 == 0:
            _on_time_gps(session, net, r, t, dep, now - timedelta(minutes=1))
            session.commit()
        rec = evaluate_train(session, t.id, now, "pravidelný prepočet")
        session.commit()
        seen.append((k, rec.id, rec.decision, rec.color))
    assert {rid for _k, rid, _d, _c in seen} == {seen[0][1]}, seen          # jedno odporúčanie
    assert all(d == "POKRAČOVAŤ" for _k, _r, d, _c in seen)
    assert all(c == "ZELENÁ" for k, _r, _d, c in seen if k % 2 == 0), seen
    assert all(c == "ORANŽOVÁ" for k, _r, _d, c in seen if k % 2 == 1), seen  # stará poloha – nikdy ZELENÁ
    (rec,) = _recs(session, t.id)
    assert rec.color == "ZELENÁ" and rec.state == "nove"


def test_qp2_trvalo_stara_poloha_je_nove_odporucanie_s_upozornenim(session, fixed_now):
    """[QP2b] Poloha prestane chodiť: kým je stará menej ako 15 + 30 min, farba sa zmení na ORANŽOVÚ v tom istom
    odporúčaní (bez upozornenia); keď zhoršenie trvá aspoň 30 min, vznikne nové odporúčanie ORANŽOVÁ s prioritou
    DÔLEŽITÉ (upozornenie) a dôvodom o chýbajúcej polohe."""
    seed_borders(session)
    net = network()
    r = net.route("Čadca", "Košice", TrainSpec())
    dep = fixed_now - timedelta(minutes=10)
    t = _running(session, fixed_now, "79602", departed=dep)
    _on_time_gps(session, net, r, t, dep, fixed_now - timedelta(minutes=1))
    session.commit()
    first = evaluate_train(session, t.id, fixed_now, "test")
    assert first.color == "ZELENÁ"
    out = []
    for minutes in (16, 31, 46, 61):
        now = fixed_now + timedelta(minutes=minutes)
        set_fake_now(now)
        rec = evaluate_train(session, t.id, now, "pravidelný prepočet")
        session.commit()
        out.append((minutes, rec.id, rec.color, rec.priority))
    assert [(m, rid == first.id, c) for m, rid, c, _p in out[:2]] == [(16, True, "ORANŽOVÁ"), (31, True, "ORANŽOVÁ")]
    m46, rid46, c46, p46 = out[2]
    assert rid46 != first.id and c46 == "ORANŽOVÁ" and p46 == "DÔLEŽITÉ", out
    assert out[3][1] == rid46                                                   # ďalej už bez nového odporúčania
    last = session.get(Recommendation, rid46)
    assert any("Chýbajú čerstvé dáta o polohe vlaku" in x for x in last.reasons)


def test_qp2_prijate_zelene_odporucanie_a_starnuca_poloha(session, fixed_now):
    """[QP2b] Dispečer prijal ZELENÉ POKRAČOVAŤ a poloha práve zostarla: prijaté odporúčanie sa nemení, ale aktuálne
    nesmie ostať ZELENÉ – nové odporúčanie ORANŽOVÁ s prioritou INFO (bez e-mailu); s novou polohou je znova ZELENÉ
    v tom istom odporúčaní."""
    seed_borders(session)
    net = network()
    r = net.route("Čadca", "Košice", TrainSpec())
    dep = fixed_now - timedelta(minutes=10)
    t = _running(session, fixed_now, "79603", departed=dep)
    _on_time_gps(session, net, r, t, dep, fixed_now - timedelta(minutes=1))
    session.commit()
    first = evaluate_train(session, t.id, fixed_now, "test")
    first.state = "prijate"
    session.commit()
    now = fixed_now + timedelta(minutes=16)
    stale = evaluate_train(session, t.id, now, "pravidelný prepočet")
    assert stale.id != first.id and stale.color == "ORANŽOVÁ" and stale.priority == "INFO", \
        (stale.id, stale.color, stale.priority)
    assert session.get(Recommendation, first.id).state == "prijate" and first.color == "ZELENÁ"
    _on_time_gps(session, net, r, t, dep, now + timedelta(minutes=13))
    session.commit()
    back = evaluate_train(session, t.id, now + timedelta(minutes=14), "pravidelný prepočet")
    assert back.id == stale.id and back.color == "ZELENÁ"


def test_qp2_tis_odchod_z_vychodiskovej_stanice_nie_je_mimo_trasy(client, fixed_now):
    """[QP2a] Prvé hlásenie TIS každej jazdy – „odchod“ z východiskovej stanice – je bod stanice 180 – 370 m od
    začiatku trasy plánovača. Nie je to poloha mimo plánovanej trasy: vlak je ZELENÝ, bez dôvodu „nie je na plánovanej
    trase“."""
    from vlaky.db import session_scope

    for origin, dest, number in (("Čadca", "Košice", "88001"), ("Púchov", "Trenčín", "88003")):
        with session_scope() as s:
            seed_borders(s)
            tid = _running(s, fixed_now, number, origin=origin, destination=dest,
                           departed=fixed_now - timedelta(minutes=3)).id
        resp = client.post("/api/udalosti-jazdy", headers={"Authorization": "Bearer test-token"},
                           json={"train_number": number, "station": origin, "event": "odchod",
                                 "ts": local(fixed_now - timedelta(minutes=3)).isoformat(), "source": "tis"})
        assert resp.status_code == 200, resp.text
        with session_scope() as s:
            rec = evaluate_train(s, tid, fixed_now, "udalosť jazdy")
            assert rec.color == "ZELENÁ", (origin, rec.color, rec.reasons)
            assert not any("nie je na plánovanej trase" in x for x in rec.reasons)


def test_qp2_cyklus_agenta_bez_burky_upozorneni(cfg, fixed_now):
    """[jazda-R5] Celý cyklus agenta (prepočet + upozornenia) pri GPS každých 30 min: za 2 h jedno odporúčanie a jedna
    správa každému dispečerovi (nie 9 odporúčaní a 10 e-mailov)."""
    from vlaky.agent.beh import run_cycle
    from vlaky.db import session_scope

    net = network()
    r = net.route("Čadca", "Košice", TrainSpec())
    dep = fixed_now - timedelta(minutes=10)
    with session_scope() as s:
        seed_borders(s)
        tid = _running(s, fixed_now, "79604", departed=dep).id
    for k in range(9):
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        if k % 2 == 0:
            with session_scope() as s:
                _on_time_gps(s, net, r, s.get(Train, tid), dep, now - timedelta(minutes=1))
        run_cycle(now)
    with session_scope() as s:
        recs = _recs(s, tid)
        notes = s.scalars(select(Notification).where(Notification.recommendation_id.in_([x.id for x in recs]))).all()
        assert len(recs) == 1, [(x.id, x.color, x.priority) for x in recs]
        assert Counter(n.recipient for n in notes if n.channel == "email") == {
            "dispecer1@dopravca.example": 1, "dispecer2@dopravca.example": 1}


# ====================================================================== QP3 / jazda-R7, R8 tesne pred cieľom
def test_qp3_vlak_pred_vjazdom_s_rusnovodicom_na_limite(cfg, fixed_now):
    """[QP3 / jazda-R7] Čadca → Košice, vlak hodinu stojí 900 m pred koncom trasy (pred vjazdom), rušňovodičovi
    uplynie 13 h o 10 min. Nie ODSTAVIŤ s novou trasou o 6 h a KRITICKÁ SMS o objednaní trasy, ale POKRAČOVAŤ –
    dokončiť jazdu, „vystriedať ihneď“ (čas vystriedania už uplynul); bez návrhu trasy, konceptu objednávky
    a kontrolného zoznamu pred odchodom."""
    from vlaky.agent.beh import run_cycle
    from vlaky.db import session_scope

    net = network()
    r = net.route("Čadca", "Košice", TrainSpec())
    dep = fixed_now - timedelta(hours=11, minutes=50)
    lat, lon = _point_at(net, r, r.length_m / 1000.0 - 0.9)
    with session_scope() as s:
        seed_borders(s)
        t = _running(s, fixed_now, "79801", departed=dep, shift_start=dep - timedelta(minutes=60))
        for k in range(5):
            _gps(s, t, fixed_now - timedelta(minutes=60 - 15 * k), lat, lon)
        tid = t.id
    run_cycle(fixed_now, force_evaluate=True)
    with session_scope() as s:
        rec = _recs(s, tid)[-1]
        assert rec.decision == "POKRAČOVAŤ", (rec.decision, rec.summary)
        assert rec.path_proposal == {} and "nová trasa" not in rec.summary and "objednať" not in rec.summary
        assert "pred cieľom (Košice)" in rec.summary and "dokončiť jazdu" in rec.summary
        assert rec.crew["status"] == "ČERVENÁ" and "vystriedať ihneď" in rec.summary
        assert any("odstavenie na trati ani nová (ad hoc) trasa" in x for x in rec.reasons)
        assert not any(v["kind"] == "ODSTAVIŤ" for v in rec.variants)
        sms = sms_text(rec)
        assert "ihneď" in sms and "ODSTAVIŤ" not in sms
        body = recommendation_body(rec)
        assert "NÁVRH TRASY" not in body and "Treba objednať" not in body and "KONCEPT" not in body
        assert "Pred odchodom (kontrolný zoznam)" not in body
        sent = s.scalars(select(Notification).where(Notification.recommendation_id == rec.id,
                                                    Notification.channel == "sms")).all()
        assert sent and all("ODSTAVIŤ" not in n.body for n in sent)


def test_qp3_zatvorena_cielova_stanica_pocka_pred_cielom(session, fixed_now):
    """[QP3 / jazda-R8] Púchov → Trenčín, vlak 800 m pred koncom trasy, v Trenčíne mimoriadnosť na 8 h (termín
    zákazníka sa nestihne): nie ESKALOVAŤ „ODSTAVIŤ s odchodom …“ s návrhom trasy 0,8 km len s Trenčínom, ale POČKAŤ
    pred cieľom do konca mimoriadnosti (ČERVENÁ – najmenej zlá možnosť, informovať zákazníka). Pri mimoriadnosti na
    3 h POČKAŤ pred cieľom – ani vtedy nie „vypraviť“ a návrh trasy."""
    seed_borders(session)
    net = network()
    r = net.route("Púchov", "Trenčín", TrainSpec())
    lat, lon = _point_at(net, r, r.length_m / 1000.0 - 0.8)
    out = {}
    for hours in (8, 3):
        session.query(Closure).delete()
        session.add(Closure(kind="mimoriadnost", from_station="Trenčín", to_station="Trenčín", status="overene",
                            start_utc=fixed_now - timedelta(hours=1), end_utc=fixed_now + timedelta(hours=hours)))
        t = _running(session, fixed_now, f"7990{hours}", origin="Púchov", destination="Trenčín",
                     departed=fixed_now - timedelta(minutes=30), shift_start=fixed_now - timedelta(hours=2),
                     deadline_h=8)
        _gps(session, t, fixed_now, lat, lon)
        session.commit()
        out[hours] = evaluate_train(session, t.id, fixed_now, "test")
    long, short = out[8], out[3]
    assert long.decision == "POČKAŤ" and long.color == "ČERVENÁ", (long.decision, long.color, long.summary)
    assert "POČKAŤ pred cieľom (Trenčín" in long.summary and "dokončiť jazdu o" in long.summary
    assert any("najmenej zlá je počkať pred cieľom" in x and "informujte zákazníka" in x for x in long.reasons)
    assert long.path_proposal == {} and not any(v["kind"] == "ODSTAVIŤ" for v in long.variants)
    assert short.decision == "POČKAŤ" and "POČKAŤ pred cieľom (Trenčín" in short.summary
    assert "vypraviť" not in short.summary and short.path_proposal == {}
    assert "NÁVRH TRASY" not in recommendation_body(short)


def test_qp3_vystriedat_ihned_ked_cas_vystriedania_uplynul():
    """[QP3] Pokyn „vystriedať najneskôr o <čas v minulosti>“ sa nedá splniť: pri prepočte už uplynutom čase
    vystriedania text povie „vystriedať ihneď“ s časom limitu (aj pri odhade bez záznamu o nástupe)."""
    crew = {"relieve_by": "2026-11-02T03:40:00+00:00", "limit": "2026-11-02T04:10:00+00:00", "left_min": 10.0,
            "max_duty_h": 13.0, "status": "ČERVENÁ"}
    assert crew_relief_text(crew) == "vystriedať ihneď (limit 13 h uplynie o 02.11. 05:10)"
    assert crew_relief_text(dict(crew, left_min=-5.0)) == "vystriedať ihneď (limit 13 h uplynul o 02.11. 05:10)"
    assert crew_relief_text(dict(crew, unknown_start=True)).startswith("odhad: vystriedať ihneď (limit 13 h")
    assert crew_relief_text(dict(crew, left_min=200.0)) == "vystriedať najneskôr o 02.11. 04:40"
