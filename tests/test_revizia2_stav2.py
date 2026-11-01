"""Regresné testy k 2. revízii – doplnok (stav vlaku v plánovači): kotva 18 h vlaku, ktorý čaká pripravený od
zmeškanej trasy, opakované odporúčania idúceho vlaku bez polohy, trasa z e-mailu pre vlak bez dátumu jazdy
a naplánované vystriedanie rušňovodiča, ktoré nesmie zakryť zmenu, ktorá prebieha."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from test_web_spolocne import csrf, login_as
from vlaky.cas import ensure_utc, fmt, local, set_fake_now
from vlaky.hranice import seed_borders
from vlaky.models import (AuditLog, Closure, Driver, DriverShift, Notification, Path, PositionReport,
                          Recommendation, Train)
from vlaky.planovanie import planovac
from vlaky.planovanie.planovac import evaluate_train
from vlaky.siet.graf import TrainSpec, network

pytest_plugins = ["test_web_spolocne"]


def _train(session, now, path_at=None, **kw):
    data = dict(number="70200", run_date=now.date().isoformat(), origin="Čadca", destination="Košice",
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


def _stands_since(rec, since) -> bool:
    return any(f"Vlak stojí od {fmt(since)}" in r for r in rec.reasons)


def _within_limit(rec, since) -> bool:
    dep = rec.chosen.get("depart")
    return dep is None or datetime.fromisoformat(dep) <= since + timedelta(hours=18)


def _local(dt) -> str:
    return local(dt).strftime("%Y-%m-%dT%H:%M")


def _form(t, **kw) -> dict:
    data = {"number": t.number, "run_date": t.run_date, "origin": t.origin, "destination": t.destination,
            "gauge": t.gauge, "traction": t.traction, "vmax_kmh": str(int(t.vmax_kmh)), "status": t.status,
            "deadline_utc": _local(t.deadline_utc)}
    data.update(kw)
    return data


def _recs(session, train_id) -> list:
    return list(session.scalars(select(Recommendation).where(Recommendation.train_id == train_id)
                                .order_by(Recommendation.id)))


def _gps(session, t, now, frac=3):
    net = network()
    geo = net.geometry(net.route("Čadca", "Košice", TrainSpec()))
    lon, lat = geo[len(geo) // frac]
    session.add(PositionReport(train_id=t.id, train_number=t.number, ts_utc=now, lat=lat, lon=lon, source="gps"))
    session.flush()


# ------------------------------------------------------------------ kotva 18 h: vlak čaká od zmeškanej trasy
@pytest.mark.parametrize("ready_known", [True, False], ids=["pripravenost_zadana", "pripravenost_neznama"])
def test_hlasenie_pripraveny_vlaku_cakajuceho_od_trasy_neposunie_kotvu(client, session, fixed_now, ready_known):
    """Plánovaný vlak stojí pripravený od odchodu svojej trasy (plánovač: „stojí od …“). Hlásenie „pripravený“ od
    rušňovodiča nesmie čas pripravenosti posunúť na teraz – kotva by zmizla a vlak by dostal ZELENÚ po limite 18 h."""
    from vlaky.web.rusnovodic import make_driver_token

    seed_borders(session)
    dep = fixed_now - timedelta(hours=17)
    t = _train(session, fixed_now, path_at=-17 * 60, ready_utc=dep if ready_known else None)
    _closure(session, fixed_now)
    session.commit()
    before = evaluate_train(session, t.id, fixed_now, "pred")
    assert before.decision == "ESKALOVAŤ" and _stands_since(before, dep)
    token = make_driver_token(t)
    session.commit()
    assert client.post("/api/hlasenie", json={"t": token, "typ": "pripraveny"}).status_code == 200
    later = fixed_now + timedelta(minutes=2)
    set_fake_now(later)
    session.expire_all()
    t2 = session.get(Train, t.id)
    assert (ensure_utc(t2.ready_utc) if t2.ready_utc else None) == (dep if ready_known else None)
    assert t2.standing_since_utc is None  # hlásenie „pripravený“ začiatok státia nemení
    a = session.scalar(select(AuditLog).where(AuditLog.action == "hlasenie_pripraveny"))
    assert a.detail.get("ready_unchanged") is True
    after = evaluate_train(session, t.id, later, "po hlásení")
    assert after.decision == "ESKALOVAŤ" and after.color == "ČERVENÁ"
    assert _stands_since(after, dep) and _within_limit(after, dep)
    # budúci alebo chýbajúci čas pripravenosti vlaku bez kotvy sa naďalej nahradí časom hlásenia
    other = _train(session, later, number="70201", path_at=120, ready_utc=None)
    session.commit()
    token2 = make_driver_token(other)
    session.commit()
    assert client.post("/api/hlasenie", json={"t": token2, "typ": "pripraveny"}).status_code == 200
    session.expire_all()
    assert ensure_utc(session.get(Train, other.id).ready_utc) == later


def test_uprava_pripravenosti_planovaneho_vlaku_drzi_kotvu(client, session, fixed_now):
    """Dispečer upraví čas pripravenosti vlaku, ktorý čaká od zmeškanej trasy (stav ostáva „plánovaný“) – limit 18 h
    sa ráta ďalej od odchodu trasy (začiatok státia sa pred úpravou zapíše)."""
    seed_borders(session)
    dep = fixed_now - timedelta(hours=17)
    t = _train(session, fixed_now, path_at=-17 * 60, ready_utc=dep)
    _closure(session, fixed_now)
    session.commit()
    assert _stands_since(evaluate_train(session, t.id, fixed_now, "pred"), dep)
    session.commit()
    login_as(client, "disp1", "dispecer")
    token = csrf(client, f"/vlaky/{t.id}/upravit")
    assert client.post(f"/vlaky/{t.id}/upravit", follow_redirects=False,
                       data={"csrf_token": token, **_form(t, ready_utc=_local(fixed_now + timedelta(hours=1)))}
                       ).status_code == 303
    session.expire_all()
    t2 = session.get(Train, t.id)
    assert t2.status == "planovany" and ensure_utc(t2.standing_since_utc) == dep
    rec = evaluate_train(session, t.id, fixed_now + timedelta(minutes=1), "po úprave")
    assert rec.decision == "ESKALOVAŤ" and rec.color == "ČERVENÁ"
    assert _stands_since(rec, dep) and _within_limit(rec, dep)


def test_odstavenie_s_novou_pripravenostou_v_jednom_ulozeni(client, session, fixed_now):
    """Stav „odstavený“ a nový čas pripravenosti v jednom uložení formulára: začiatok státia je odchod zmeškanej
    trasy, nie čas uloženia (zmena stavu limit 18 h nepredĺži ani pri súčasnej zmene pripravenosti)."""
    seed_borders(session)
    dep = fixed_now - timedelta(hours=12)
    t = _train(session, fixed_now, path_at=-12 * 60, ready_utc=dep)
    _closure(session, fixed_now)
    session.commit()
    assert _stands_since(evaluate_train(session, t.id, fixed_now, "pred"), dep)
    session.commit()
    login_as(client, "disp1", "dispecer")
    token = csrf(client, f"/vlaky/{t.id}/upravit")
    assert client.post(f"/vlaky/{t.id}/upravit", follow_redirects=False,
                       data={"csrf_token": token, **_form(t, status="odstaveny",
                                                          ready_utc=_local(fixed_now + timedelta(hours=3)))}
                       ).status_code == 303
    session.expire_all()
    t2 = session.get(Train, t.id)
    assert t2.status == "odstaveny" and ensure_utc(t2.standing_since_utc) == dep
    rec = evaluate_train(session, t.id, fixed_now + timedelta(minutes=1), "po odstavení")
    assert rec.decision == "ESKALOVAŤ"
    assert any(f"Vlak stojí od {fmt(dep)}" in r and "zostáva 3.0 h" in r for r in rec.reasons)
    assert _within_limit(rec, dep)


def test_odchod_po_prichode_na_hranicu_zrusi_kotvu_prichodu(client, session, fixed_now):
    """Vlak prišiel na hranicu (prevzatie sa nezaznamenalo), rušňovodič nahlásil odchod a teraz je vlak odstavený:
    limit 18 h sa ráta od odstavenia, nie od dávneho príchodu na hranicu (vlak medzitým išiel)."""
    from vlaky.hranice import find_border
    from vlaky.models import Handover
    from vlaky.web.rusnovodic import make_driver_token

    seed_borders(session)
    bc = find_border(session, "Čadca")
    t = _train(session, fixed_now, number="70204", status="na_ceste_k_hranici", entry_border_id=bc.id,
               deadline_utc=fixed_now + timedelta(hours=40))
    arrival = fixed_now - timedelta(hours=16)
    session.add(Handover(train_id=t.id, border_id=bc.id, direction="vstup", status="prisiel", arrived_utc=arrival))
    _closure(session, fixed_now, end_h=10)
    session.commit()
    token = make_driver_token(t)
    session.commit()
    set_fake_now(fixed_now - timedelta(hours=14))
    assert client.post("/api/hlasenie", json={"t": token, "typ": "odchod"}).status_code == 200
    set_fake_now(fixed_now)
    session.expire_all()
    t1 = session.get(Train, t.id)
    login_as(client, "disp1", "dispecer")
    tok = csrf(client, f"/vlaky/{t.id}/upravit")
    assert client.post(f"/vlaky/{t.id}/upravit", follow_redirects=False,
                       data={"csrf_token": tok, **_form(t1, status="odstaveny", entry_border_id=str(bc.id))}
                       ).status_code == 303
    session.expire_all()
    t2 = session.get(Train, t.id)
    assert t2.status == "odstaveny" and ensure_utc(t2.standing_since_utc) == fixed_now
    rec = evaluate_train(session, t.id, fixed_now, "po odstavení")
    assert not _stands_since(rec, arrival) and rec.decision != "ESKALOVAŤ"


# ------------------------------------------------------------------ idúci vlak bez polohy: bez opakovania
def test_iduci_vlak_bez_polohy_s_hlasenym_odchodom_bez_opakovania(cfg, client, fixed_now):
    """Idúci vlak bez GPS a bez plánovaného odchodu, odchod nahlásil rušňovodič: poloha sa odhaduje od skutočného
    odchodu – bez zmeny situácie jedno odporúčanie a jedno upozornenie za 2 h (nie každých 30 min)."""
    from vlaky.agent.beh import run_cycle
    from vlaky.db import session_scope
    from vlaky.web.rusnovodic import make_driver_token

    with session_scope() as s:
        seed_borders(s)
        d = Driver(name="R. FIKTÍVNY")
        s.add(d)
        s.flush()
        t = _train(s, fixed_now, number="70202", status="prevzaty", ready_utc=fixed_now - timedelta(minutes=30))
        s.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=fixed_now - timedelta(minutes=60),
                          status="prebieha"))
        tid = t.id
        token = make_driver_token(t)
    assert client.post("/api/hlasenie", json={"t": token, "typ": "odchod"}).status_code == 200
    for k in range(9):
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        run_cycle(now, force_evaluate=True)
    with session_scope() as s:
        recs = _recs(s, tid)
        assert len(recs) == 1 and recs[0].decision == "POKRAČOVAŤ" and recs[0].state == "nove"
        assert not recs[0].chosen.get("arrival_floating")  # príchod odhadnutý od skutočného odchodu
        notes = s.scalars(select(Notification).where(Notification.kind == "odporucanie")).all()
        assert len({n.recommendation_id for n in notes}) == 1
        assert Counter(n.channel for n in notes)["email"] == len(notes) <= 2


def test_iduci_vlak_bez_polohy_aj_casu_odchodu_bez_opakovania(session, fixed_now):
    """Idúci vlak bez polohy a bez akéhokoľvek času odchodu: predpokladaný príchod (teraz + celá jazda) sa posúva
    s časom – to nie je zmena situácie, žiadne nové odporúčanie každých 30 min."""
    seed_borders(session)
    t = _train(session, fixed_now, number="70203", status="v_jazde")
    d = Driver(name="S. FIKTÍVNY")
    session.add(d)
    session.flush()
    session.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=fixed_now - timedelta(minutes=60),
                            status="prebieha"))
    session.flush()
    for k in range(9):
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        rec = evaluate_train(session, t.id, now, "pravidelný prepočet")
        assert rec.decision == "POKRAČOVAŤ" and rec.color != "ZELENÁ"
    recs = _recs(session, t.id)
    assert len(recs) == 1 and recs[0].state == "nove"
    assert recs[0].chosen.get("arrival_floating")


# ------------------------------------------------------------------ trasa z e-mailu pre vlak bez dátumu jazdy
def test_trasa_z_emailu_pre_vlak_bez_datumu_jazdy(session, fixed_now):
    """Vlak zadaný bez dátumu jazdy a časov, trasa s jeho číslom prišla e-mailom skôr (bez priradeného vlaku):
    patrí najbližšej jazde – plánovač ju použije a neodporučí ju odrieknuť a objednať novú."""
    seed_borders(session)
    t = _train(session, fixed_now, number="70500", run_date=None, deadline_utc=fixed_now + timedelta(hours=12))
    path = Path(train_id=None, train_number="70500", status="pridelena", departure_utc=fixed_now + timedelta(hours=2))
    yesterday = Path(train_id=None, train_number="70500", status="pridelena",
                     departure_utc=fixed_now - timedelta(hours=22))
    session.add_all([path, yesterday])
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert not rec.chosen.get("needs_new_path")
    assert not any("nemá pridelenú trasu" in r for r in rec.reasons) and "odrieknuť" not in rec.summary
    assert not any("stojí od" in r for r in rec.reasons)  # včerajšia trasa iného behu nie je kotvou
    assert [p.id for p in planovac.allocated_paths(session, t, None, fixed_now)] == [path.id]


# ------------------------------------------------------------------ naplánované vystriedanie rušňovodiča
def test_planovane_vystriedanie_nezakryje_prebiehajucu_zmenu(client, session, fixed_now):
    """Idúci vlak so zmenou, ktorá prebieha (12 h 10 min), a naplánovaným vystriedaním o 3 h: stráži sa skutočný
    limit rušňovodiča v službe (vystriedať do 20 min), nie odhad „chýba záznam o nástupe“ s časom o 9,5 h."""
    seed_borders(session)
    t = _train(session, fixed_now, number="70900", status="v_jazde",
               planned_departure_utc=fixed_now - timedelta(hours=2))
    _gps(session, t, fixed_now)
    a, b = Driver(name="Adam FIKTÍVNY"), Driver(name="Boris FIKTÍVNY")
    session.add_all([a, b])
    session.flush()
    a_start, b_start = fixed_now - timedelta(hours=12, minutes=10), fixed_now + timedelta(hours=3)
    session.add_all([DriverShift(driver_id=a.id, train_id=t.id, start_utc=a_start, status="prebieha"),
                     DriverShift(driver_id=b.id, train_id=t.id, start_utc=b_start, status="planovana")])
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    a_relieve = a_start + timedelta(hours=13) - timedelta(minutes=30)
    crew = rec.crew
    assert not crew.get("unknown_start") and crew["driver"] == "Adam FIKTÍVNY"
    assert crew["relieve_by"] == a_relieve.isoformat() and crew["status"] == "ČERVENÁ"
    assert rec.priority == "KRITICKÉ" and rec.color != "ZELENÁ"
    assert not any("chýba záznam o nástupe" in r for r in rec.reasons)
    assert f"vystriedať najneskôr o {fmt(a_relieve)}" in rec.summary
    session.commit()
    login_as(client, "disp1", "dispecer")
    drv = next(r for r in client.get("/api/vlaky").json()["vlaky"] if r["id"] == t.id)["driver"]
    assert drv["name"] == "Adam FIKTÍVNY" and drv["relieve_by"] == a_relieve.isoformat()
    assert drv["color"] == "cervena" and not drv.get("unknown_start")
    page = client.get("/rusnovodici").text
    rows = {m.group(1): m.group(2) for m in re.finditer(
        r"<td><strong>([^<]+)</strong></td>(.*?)</tr>", page, re.S)}
    b_relieve = b_start + timedelta(hours=13) - timedelta(minutes=30)
    assert f"<strong>{fmt(a_relieve)}</strong>" in rows["Adam FIKTÍVNY"]
    assert f"<strong>{fmt(b_relieve)}</strong>" in rows["Boris FIKTÍVNY"]  # vystriedanie má vlastný limit


def test_zastaraly_odhad_sa_pri_prebiehajucej_zmene_neukaze(client, session, fixed_now):
    """Posledné (prijaté) odporúčanie ešte s odhadom pri neznámom nástupe a medzitým zaznamenaná zmena, ktorá
    prebieha: stránka rušňovodičov ukazuje skutočný limit zmeny, nie odhad ako fakt."""
    seed_borders(session)
    t = _train(session, fixed_now, number="70901", status="v_jazde",
               planned_departure_utc=fixed_now - timedelta(hours=3))
    estimate = (fixed_now + timedelta(hours=8, minutes=30)).isoformat()
    session.add(Recommendation(train_id=t.id, created_at=fixed_now - timedelta(minutes=20), decision="POKRAČOVAŤ",
                               color="ORANŽOVÁ", priority="DÔLEŽITÉ", summary="odhad", state="prijate",
                               fingerprint="x", crew={"driver": "neznámy (chýba záznam o nástupe)",
                                                      "unknown_start": True, "estimated": True,
                                                      "status": "ORANŽOVÁ", "max_duty_h": 13.0,
                                                      "relieve_by": estimate, "end_p90": estimate}))
    d = Driver(name="Cyril FIKTÍVNY")
    session.add(d)
    session.flush()
    start = fixed_now - timedelta(hours=11, minutes=45)
    session.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=start, status="prebieha"))
    session.commit()
    login_as(client, "disp1", "dispecer")
    page = client.get("/rusnovodici").text
    row = {m.group(1): m.group(2) for m in re.finditer(r"<td><strong>([^<]+)</strong></td>(.*?)</tr>", page,
                                                       re.S)}["Cyril FIKTÍVNY"]
    real = start + timedelta(hours=13) - timedelta(minutes=30)
    assert f"<strong>{fmt(real)}</strong>" in row
    assert fmt(datetime.fromisoformat(estimate)) not in row
