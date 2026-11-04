"""Regresné testy k 3. revízii – stabilita odporúčaní (oblasť P2): vlastný odtlačok záznamu o chybe výpočtu,
uplynuté avízo nie je príchod vlaku, text ODSTAVIŤ na vlastnú pridelenú trasu, bod zlomu čakať/odstaviť bez zmeny
údajov, pevná mriežka odchodov (semafor bez kolísania), prijatá nová trasa v lehote P5, poistky ZELENEJ v návrhu
k objednávke a text limitu 18 h, ktorý ešte neuplynul."""
from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select

from test_web_spolocne import csrf, login_as
from vlaky import rozhodovanie as R
from vlaky.agent.beh import propose_new_orders, run_cycle
from vlaky.cas import fmt, local, set_fake_now
from vlaky.db import session_scope
from vlaky.hranice import find_border, seed_borders
from vlaky.models import Closure, Handover, KeyValue, Notification, Order, Path, Recommendation, Train
from vlaky.notifikacie.texty import recommendation_body
from vlaky.planovanie import planovac, stav
from vlaky.planovanie.planovac import evaluate_train, plan_order

pytest_plugins = ["test_web_spolocne"]


def _train(session, now, path_at=None, **kw):
    data = dict(number="73000", run_date=now.date().isoformat(), origin="Čadca", destination="Košice",
                traction="E", gauge="1435", vmax_kmh=100, status="planovany", ready_utc=now,
                deadline_utc=now + timedelta(hours=30))
    data.update(kw)
    t = Train(**data)
    session.add(t)
    session.flush()
    if path_at is not None:
        session.add(Path(train_id=t.id, train_number=t.number, status="pridelena",
                         departure_utc=now + timedelta(minutes=path_at)))
        session.flush()
    return t


def _closure(session, now, start_h, end_h, a="Liptovský Mikuláš", b="Poprad-Tatry"):
    c = Closure(kind="uplna", from_station=a, to_station=b, start_utc=now + timedelta(hours=start_h),
                end_utc=now + timedelta(hours=end_h), status="overene")
    session.add(c)
    session.flush()
    return c


def _recs(session, train_id) -> list:
    return list(session.scalars(select(Recommendation).where(Recommendation.train_id == train_id)
                                .order_by(Recommendation.id)))


def _depart(rec) -> datetime:
    return datetime.fromisoformat(rec.chosen["depart"])


def _accept_web(client, rec_id: int) -> None:
    token = csrf(client, f"/odporucania/{rec_id}")
    r = client.post(f"/odporucania/{rec_id}/rozhodnutie", follow_redirects=False,
                    data={"csrf_token": token, "akcia": "prijat", "poznamka": ""})
    assert r.status_code == 303


def _sent(session, kind="odporucanie") -> list:
    return [n for n in session.scalars(select(Notification).where(Notification.kind == kind).order_by(Notification.id))
            if n.status in ("odoslana", "outbox")]


# ====================================================================== PS3 odtlačok záznamu o chybe výpočtu
def test_chyba_vypoctu_sa_nezluci_so_skutocnou_eskalaciou(cfg, fixed_now, monkeypatch):
    """[PS3] Po jednom cykle s chybou výpočtu musí skutočná eskalácia bez varianty (limit 18 h, žiadna trasa)
    vzniknúť ako KRITICKÉ odporúčanie so SMS – nie potichu zlúčená do záznamu o chybe (DÔLEŽITÉ). A naopak:
    chyba po skutočnej eskalácii je nové odporúčanie (dispečer vie, že agent prestal počítať)."""
    with session_scope() as s:
        seed_borders(s)
        t = _train(s, fixed_now, number="73001", status="odstaveny", ready_utc=fixed_now - timedelta(hours=17),
                   standing_since_utc=fixed_now - timedelta(hours=17))
        tid = t.id
    real = planovac.evaluate_train

    def broken(session, train_id, now=None, trigger=""):
        raise RuntimeError("dočasná chyba (FIKTÍVNY výpadok spojenia s DB)")

    monkeypatch.setattr(planovac, "evaluate_train", broken)
    run_cycle(fixed_now, force_evaluate=True)
    monkeypatch.setattr(planovac, "evaluate_train", real)
    t1 = fixed_now + timedelta(minutes=15)
    set_fake_now(t1)
    run_cycle(t1, force_evaluate=True)
    with session_scope() as s:
        rows = _recs(s, tid)
        assert [(r.decision, r.priority) for r in rows] == [("ESKALOVAŤ", "DÔLEŽITÉ"), ("ESKALOVAŤ", "KRITICKÉ")]
        assert rows[0].state == "zastarane" and rows[1].state == "nove"
        assert "nedokázal prepočítať" in rows[0].summary and "limitu 18 h" in rows[1].summary
        assert rows[0].fingerprint != rows[1].fingerprint
        notes = Counter((n.channel, n.priority) for n in _sent(s) if n.recommendation_id == rows[1].id)
        assert notes[("sms", "KRITICKÉ")] == 1 and notes[("email", "KRITICKÉ")] == 2
    # chyba po skutočnej eskalácii: nový záznam o chybe, nie tiché prepísanie eskalácie
    monkeypatch.setattr(planovac, "evaluate_train", broken)
    t2 = fixed_now + timedelta(minutes=30)
    set_fake_now(t2)
    run_cycle(t2, force_evaluate=True)
    with session_scope() as s:
        rows = _recs(s, tid)
        assert len(rows) == 3 and rows[2].priority == "DÔLEŽITÉ" and "nedokázal prepočítať" in rows[2].summary
        assert rows[1].state == "zastarane"
        assert len([n for n in _sent(s) if n.recommendation_id == rows[2].id and n.channel == "email"]) == 2


def test_starsi_zaznam_chyby_neprehltne_kriticku_eskalaciu(session, fixed_now):
    """[PS3] Záznam o chybe uložený pred opravou (odtlačok ESKALOVAŤ|ČERVENÁ|, DÔLEŽITÉ): skutočná eskalácia s rovnakým
    odtlačkom a vyššou prioritou je nové odporúčanie (KRITICKÉ), nie tichá aktualizácia."""
    seed_borders(session)
    t = _train(session, fixed_now, number="73002", status="odstaveny", ready_utc=fixed_now - timedelta(hours=17),
               standing_since_utc=fixed_now - timedelta(hours=17))
    old = Recommendation(train_id=t.id, created_at=fixed_now - timedelta(minutes=15), decision="ESKALOVAŤ",
                         color="ČERVENÁ", priority="DÔLEŽITÉ", summary="agent ho nedokázal prepočítať (FIKTÍVNE)",
                         chosen={}, crew={}, state="nove",
                         fingerprint=hashlib.sha1("ESKALOVAŤ|ČERVENÁ|".encode("utf-8")).hexdigest())
    session.add(old)
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "pravidelný prepočet")
    assert rec.id != old.id and rec.priority == "KRITICKÉ" and rec.decision == "ESKALOVAŤ"
    assert old.state == "zastarane"


# ====================================================================== PR2 uplynuté avízo nie je príchod
def _abroad(session, now, avizo_h=-2.0, path_at=30, **kw):
    bc = find_border(session, "Čadca")
    t = _train(session, now, number=kw.pop("number", "73010"), path_at=path_at, status="na_ceste_k_hranici",
               entry_border_id=bc.id, ready_utc=None, deadline_utc=now + timedelta(hours=12), **kw)
    ho = Handover(train_id=t.id, border_id=bc.id, direction="vstup", status="ocakavany",
                  avizo_eta_utc=now + timedelta(hours=avizo_h) if avizo_h is not None else None)
    session.add(ho)
    session.flush()
    return t, ho, bc


def test_uplynute_avizo_nie_je_prichod_vlaku(session, fixed_now):
    """[PR2] Vlak ešte v zahraničí (preberanie očakávané, príchod nezaznamenaný), avízo pred 2 h: nesmie dostať
    VYPRAVIŤ ZELENÁ na pridelenú trasu, akoby už prišiel. Príchod je neznámy (najskôr teraz), trasa sa nestihne."""
    seed_borders(session)
    t, ho, bc = _abroad(session, fixed_now)
    rec = evaluate_train(session, t.id, fixed_now, "test")
    dwell = max(bc.dwell_p50_min, bc.dwell_p90_min)
    assert rec.color != "ZELENÁ" and not (rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ")
    assert any("Vlak ešte neprišiel na hranicu" in r and "meškanie 120 min" in r for r in rec.reasons)
    assert not any(f"príchod {fmt(ho.avizo_eta_utc)}" in r for r in rec.reasons)  # avízo sa nevydáva za príchod
    assert any("Pridelenú trasu" in r and "nestihne" in r for r in rec.reasons)
    assert _depart(rec) >= fixed_now + timedelta(minutes=dwell) - timedelta(seconds=1)
    assert rec.chosen.get("needs_new_path") is True


def test_avizo_v_buducnosti_a_zapisany_prichod(client, session, fixed_now):
    """[PR2] Avízo v budúcnosti (aj krátko po ňom – príchod sa zapisuje s oneskorením) je plán príchodu (text
    „avízo príchodu“); uplynuté o viac ako 30 min je meškanie. Po zapísaní príchodu cez web sa počíta so skutočným
    príchodom a obmedzenie na ORANŽOVÚ zmizne."""
    seed_borders(session)
    t, ho, bc = _abroad(session, fixed_now, avizo_h=-0.75, path_at=150, number="73011")
    first = evaluate_train(session, t.id, fixed_now, "test")
    assert first.color != "ZELENÁ" and any("meškanie 45 min" in r for r in first.reasons)
    session.commit()
    login_as(client, "disp1", "dispecer")
    token = csrf(client, "/hranice")
    arrival = fixed_now - timedelta(minutes=5)
    assert client.post(f"/hranice/preberanie/{ho.id}/prisiel", follow_redirects=False,
                       data={"csrf_token": token, "cas": local(arrival).strftime("%Y-%m-%dT%H:%M")}
                       ).status_code == 303
    session.expire_all()
    after = evaluate_train(session, t.id, fixed_now + timedelta(minutes=1), "príchod na hranicu")
    assert any(f"Preberanie v PPS {bc.handover_station}: príchod {fmt(arrival)}" in r for r in after.reasons)
    assert not any("Vlak ešte neprišiel" in r for r in after.reasons)
    assert after.decision == "VYPRAVIŤ" and after.color == "ZELENÁ"
    # druhý vlak s avízom v budúcnosti: avízo je plán, nie príchod
    t2, ho2, _ = _abroad(session, fixed_now, avizo_h=2, path_at=None, number="73012")
    future = evaluate_train(session, t2.id, fixed_now, "test")
    assert any(f"avízo príchodu {fmt(ho2.avizo_eta_utc)} + preberanie" in r for r in future.reasons)
    assert not any("Vlak ešte neprišiel" in r for r in future.reasons)
    # tretí vlak 10 min po avíze: príchod sa ešte len zapisuje – bez obmedzenia, ale nie ako „príchod“
    t3, ho3, _ = _abroad(session, fixed_now, avizo_h=-1 / 6, path_at=None, number="73014")
    just = evaluate_train(session, t3.id, fixed_now, "test")
    assert any(f"avízo príchodu {fmt(ho3.avizo_eta_utc)} (príchod zatiaľ nie je zaznamenaný)" in r
               for r in just.reasons)
    assert not any("Vlak ešte neprišiel" in r for r in just.reasons)


def test_preberanie_bez_aviza_a_bez_pripravenosti_nie_je_zelene(session, fixed_now):
    """[PR2] Očakávané preberanie bez avíza a bez času pripravenosti: vlak je ešte v zahraničí – pripravenosť nie
    je známa, nie „teraz“ a nikdy ZELENÁ."""
    seed_borders(session)
    t, _ho, bc = _abroad(session, fixed_now, avizo_h=None, path_at=None, number="73013")
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.color != "ZELENÁ"
    assert any("Vlak ešte neprišiel na hranicu" in r and "bez avíza" in r for r in rec.reasons)
    assert _depart(rec) >= fixed_now + timedelta(minutes=max(bc.dwell_p50_min, bc.dwell_p90_min)) - \
        timedelta(seconds=1)


# ====================================================================== PR5 ODSTAVIŤ na vlastnú pridelenú trasu
def test_odstavit_na_pridelenu_trasu_neodrieka_trasu(session, fixed_now):
    """[PR5] ODSTAVIŤ s odchodom na vlastnú pridelenú trasu (v tolerancii, bez novej trasy) nesmie znieť „pôvodnú
    trasu odrieknuť, nová trasa na …“ – ani v e-maile, ani v odovzdávke služby, ani v koncepte pre zákazníka."""
    from vlaky.agent.odovzdavka import build_report

    seed_borders(session)
    t = _train(session, fixed_now, number="73020", path_at=330)
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision == "ODSTAVIŤ" and rec.chosen["needs_new_path"] is False
    dep = fmt(_depart(rec))
    assert f"vypraviť na pridelenú trasu o {dep}" in rec.summary
    assert "odrieknuť" not in rec.summary and "nová trasa" not in rec.summary
    body = recommendation_body(rec)
    assert "odrieknuť" not in body and "nová trasa na" not in body
    assert f"vyrazí podľa pridelenej trasy o {dep}" in rec.path_proposal["drafts"]["customer"]
    # odovzdávka služby (súhrn ORANŽOVÝCH vlakov – bez kontroly schránky nie je ZELENÁ)
    session.execute(delete(KeyValue).where(KeyValue.key == "mail_last_ok"))
    later = evaluate_train(session, t.id, fixed_now + timedelta(minutes=1), "test")
    assert later.color == "ORANŽOVÁ"
    report, _data = build_report(session, fixed_now + timedelta(minutes=2))
    assert "vypraviť na pridelenú trasu" in report and "odrieknuť" not in report


def test_odstavit_s_novou_trasou_a_bez_trasy(session, fixed_now):
    """[PR5] Kontrola: ODSTAVIŤ s novou trasou (výluka blokuje pridelenú trasu) zostáva „pôvodnú trasu odrieknuť, nová
    trasa na …“; vlak bez pridelenej trasy dostane „objednať trasu na …“ (nemá čo odrieknuť)."""
    seed_borders(session)
    t = _train(session, fixed_now, number="73021", path_at=30)
    _closure(session, fixed_now, -1, 8)
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision == "ODSTAVIŤ" and rec.chosen["needs_new_path"] is True
    assert f"pôvodnú trasu odrieknuť, nová trasa na {fmt(_depart(rec))}" in rec.summary
    t2 = _train(session, fixed_now, number="73022", path_at=None)
    rec2 = evaluate_train(session, t2.id, fixed_now, "test")
    assert rec2.chosen["needs_new_path"] is True
    if rec2.decision == "ODSTAVIŤ":
        assert f"objednať trasu na {fmt(_depart(rec2))}" in rec2.summary and "odrieknuť" not in rec2.summary
    else:
        assert "odrieknuť" not in rec2.summary


# ====================================================================== PR6 bod zlomu čakať/odstaviť
def test_prijate_odstavenie_sa_neprepne_na_pockat(cfg, client, fixed_now):
    """[PR6] Dispečer prijme ODSTAVIŤ (vlak čaká 5,5 h na svoju trasu) a vlak odstaví: ďalšie cykly agenta bez zmeny
    údajov nesmú poslať „POČKAŤ“ (držať pripravený s rušňovodičom) pre ten istý odchod – odstavenie je zaplatené."""
    with session_scope() as s:
        seed_borders(s)
        tid = _train(s, fixed_now, number="73030", path_at=330).id
    run_cycle(fixed_now)
    with session_scope() as s:
        (first,) = _recs(s, tid)
        assert first.decision == "ODSTAVIŤ"
        rec_id = first.id
    login_as(client, "disp1", "dispecer")
    _accept_web(client, rec_id)
    with session_scope() as s:
        stav.set_status(s, s.get(Train, tid), "odstaveny", fixed_now)
    for k in range(1, 9):  # 2 h cyklov po 15 min
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        run_cycle(now)
    with session_scope() as s:
        rows = _recs(s, tid)
        assert [(r.id, r.decision, r.state) for r in rows] == [(rec_id, "ODSTAVIŤ", "prijate")]
        assert not any("POČKAŤ" in n.subject for n in _sent(s))


def test_prijate_odstavenie_bez_zmeny_stavu_vlaku(session, fixed_now):
    """[PR6] Prijaté ODSTAVIŤ, stav vlaku ešte nezmenený: dispečer sa rozhodol odstaviť, náklad odstavenia sa už
    neporovnáva znova – žiadne nové odporúčanie POČKAŤ ani v bode zlomu (5 h)."""
    seed_borders(session)
    t = _train(session, fixed_now, number="73031", path_at=330)
    first = evaluate_train(session, t.id, fixed_now, "test")
    assert first.decision == "ODSTAVIŤ"
    first.state = "prijate"
    session.flush()
    for k in (2, 4, 8, 12):  # 30 min … 3 h
        evaluate_train(session, t.id, fixed_now + timedelta(minutes=15 * k), "pravidelný prepočet")
    rows = _recs(session, t.id)
    assert [(r.decision, r.state) for r in rows] == [("ODSTAVIŤ", "prijate")]


def test_bod_zlomu_bez_zmeny_udajov_nemeni_volbu_kym_rozdiel_nie_je_podstatny(session, fixed_now):
    """[PR6] Neprijaté ODSTAVIŤ: v bode zlomu (rozdiel nákladov pár eur) sa voľba bez zmeny údajov nemení; až keď je
    čakanie výrazne lacnejšie (hodinu pred odchodom), odporúčanie sa zmení na POČKAŤ – stabilita nie je trvalá."""
    seed_borders(session)
    t = _train(session, fixed_now, number="73032", path_at=330)
    first = evaluate_train(session, t.id, fixed_now, "test")
    assert first.decision == "ODSTAVIŤ"
    for k in range(1, 6):  # do 75 min (zostáva 4 h 5 min čakania – bez stability by už bolo POČKAŤ)
        rec = evaluate_train(session, t.id, fixed_now + timedelta(minutes=15 * k), "pravidelný prepočet")
        assert rec.id == first.id and rec.decision == "ODSTAVIŤ"
    late = evaluate_train(session, t.id, _depart(first) - timedelta(hours=1), "pravidelný prepočet")
    assert late.decision == "POČKAŤ" and late.id != first.id


def test_odstaveny_vlak_bez_jednorazoveho_nakladu_odstavenia():
    """[PR6] Rozhodovací modul: vlak, ktorý už stojí odstavený, neplatí jednorazový náklad odstavenia znova."""
    p = R.Params(samples=200)
    route = R.Route("A – B", (R.Segment("A – B", 120, 2, 8),))
    base = R.evaluate(route, 300, 0, 2000, p, stabled=True)
    paid = R.evaluate(route, 300, 0, 2000, p, stabled=True, already_stabled=True)
    assert base.kind == paid.kind == R.ODSTAVIT
    assert base.expected_cost - paid.expected_cost == pytest.approx(p.cost_stable_fixed)
    rec = R.recommend([route], now=0, ready=0, deadline=2000, p=p, horizon_min=360, step_min=10,
                      allocated_departures=[240], already_stabled=True)
    assert rec.decision == R.ODSTAVIT  # 4 h čakania: odstavený vlak ostáva odstavený (bez nákladu za odstavenie)
    plain = R.recommend([route], now=0, ready=0, deadline=2000, p=p, horizon_min=360, step_min=10,
                        allocated_departures=[240])
    assert plain.decision == R.POCKAT  # bez príznaku pôvodné správanie (bod zlomu 5 h)


# ====================================================================== PR7 pevná mriežka odchodov
def test_semafor_bez_zmeny_udajov_nekolise(cfg, fixed_now):
    """[PR7] Vlak pripravený (čas pripravenosti v minulosti), trasa o 5 h, rezerva voči termínu tesne pri prahu
    60 min: cykly agenta každých 15 min bez zmeny údajov nesmú striedať ZELENÁ/ORANŽOVÁ ani nahrádzať prijaté
    odporúčanie (mriežka odchodov je pevná, nie od teraz)."""
    with session_scope() as s:
        seed_borders(s)
        t = _train(s, fixed_now, number="73040", path_at=300, ready_utc=fixed_now - timedelta(hours=1),
                   deadline_utc=datetime(2026, 11, 2, 13, 0, tzinfo=fixed_now.tzinfo))
        tid = t.id
    for k in range(9):  # 2 h
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        run_cycle(now)
        if k == 2:
            with session_scope() as s:
                rows = _recs(s, tid)
                assert len(rows) == 1, [(r.decision, r.color) for r in rows]
                rows[0].state = "prijate"
    with session_scope() as s:
        rows = _recs(s, tid)
        assert len(rows) == 1 and rows[0].state == "prijate"
        assert len(_sent(s)) <= 2  # jedno odporúčanie dvom dispečerom


def test_mriezka_odchodov_rovnaka_v_kazdom_cykle():
    """[PR7] Rozhodovací modul s pevnou mriežkou: posúdenie o 15 min neskôr (pripravenosť = teraz) dá ten istý
    absolútny odchod aj semafor; bez mriežky (pôvodne) sa odchod posúva s časom posúdenia."""
    p = R.Params(samples=300)
    clo = R.Closure(start=120, end=200)  # absolútne minúty
    assert R._departures(7.5, 30, 10, -7.5) == [7.5, 12.5, 22.5, 32.5]
    assert R._departures(30, 30, 10) == [30, 40, 50, 60]
    assert R._departures(30, 30, 10, 0.0) == [30, 40, 50, 60]
    picks = []
    for t0 in (0.0, 15.0, 30.0):
        seg = (R.Segment("A – B", 60, 2, 8), R.Segment("B – C", 60, 2, 8, (R.Closure(clo.start - t0, clo.end - t0),)))
        rec = R.recommend([R.Route("A – C", seg)], now=0, ready=0, deadline=900 - t0, p=p, horizon_min=600,
                          step_min=10, allocated_departures=[300 - t0], grid_origin=-(t0 % 10))
        picks.append((rec.chosen.depart + t0, rec.chosen.status, rec.decision))
    assert len(set(picks)) == 1


# ====================================================================== PR8 prijatá nová trasa v lehote P5
def test_prijata_nova_trasa_sa_v_lehote_p5_nenahradi(cfg, client, fixed_now):
    """[PR8] Prijaté „ODSTAVIŤ …, nová trasa na 12:20“ (dispečer trasu objednal v lehote): keď čas trasy vojde do
    lehoty 6 h, agent ho nesmie nahradiť neskoršou plávajúcou trasou. Skutočná zmena (predĺžená výluka) a uplynutie
    času trasy bez odchodu sú nové odporúčanie."""
    with session_scope() as s:
        seed_borders(s)
        tid = _train(s, fixed_now, number="73050", path_at=30).id
        cid = _closure(s, fixed_now, -1, 8).id
    run_cycle(fixed_now)
    with session_scope() as s:
        (first,) = _recs(s, tid)
        assert first.decision == "ODSTAVIŤ" and first.chosen["needs_new_path"] is True
        dep, rec_id = _depart(first), first.id
    assert dep - fixed_now > timedelta(hours=6)  # pevný čas, nie najskoršia trasa po lehote
    login_as(client, "disp1", "dispecer")
    _accept_web(client, rec_id)
    for minutes in (90, 120, 150, 240, 360, 420):  # čas trasy je už v lehote 6 h
        now = fixed_now + timedelta(minutes=minutes)
        set_fake_now(now)
        run_cycle(now, force_evaluate=True)
    with session_scope() as s:
        rows = _recs(s, tid)
        assert [(r.id, r.state) for r in rows] == [(rec_id, "prijate")]
        assert _depart(rows[0]) == dep
        assert [n.recommendation_id for n in _sent(s)] == [rec_id, rec_id]
        # skutočná zmena: výluka sa predĺži za čas objednanej trasy → nové odporúčanie (neskorší odchod)
        s.get(Closure, cid).end_utc = fixed_now + timedelta(hours=12)
    now = fixed_now + timedelta(minutes=435)
    set_fake_now(now)
    with session_scope() as s:
        rec = evaluate_train(s, tid, now, "predĺžená výluka")
        assert rec.id != rec_id and _depart(rec) > dep + timedelta(minutes=30)


def test_prijata_nova_trasa_po_uplynuti_casu_bez_odchodu(session, fixed_now):
    """[PR8] Objednaná trasa uplynula a vlak nevyšiel: prijaté odporúčanie sa už nedrží (nové odporúčanie)."""
    seed_borders(session)
    t = _train(session, fixed_now, number="73051", path_at=30)
    _closure(session, fixed_now, -1, 8)
    first = evaluate_train(session, t.id, fixed_now, "test")
    first.state = "prijate"
    session.flush()
    kept = evaluate_train(session, t.id, _depart(first) - timedelta(minutes=10), "pravidelný prepočet")
    assert kept.id == first.id
    missed = evaluate_train(session, t.id, _depart(first) + timedelta(minutes=30), "pravidelný prepočet")
    assert missed.id != first.id


def test_objednana_trasa_v_lehote_len_s_poziadavkou():
    """[PR8] Rozhodovací modul: odchod na novú trasu skôr ako o 6 h sa ponúkne len pri trase objednanej v lehote."""
    p = R.Params(samples=200)
    route = R.Route("A – B", (R.Segment("A – B", 120, 2, 8),))
    plain = R.recommend([route], now=0, ready=0, deadline=2000, p=p, horizon_min=720, step_min=10,
                        allocated_departures=[-600])
    assert plain.chosen.depart >= p.new_path_lead_min
    req = R.recommend([route], now=0, ready=0, deadline=2000, p=p, horizon_min=720, step_min=10,
                      allocated_departures=[-600], requested_departures={"A – B": [200]})
    assert req.chosen.depart == pytest.approx(190) and req.chosen.needs_new_path
    other = R.recommend([route], now=0, ready=0, deadline=2000, p=p, horizon_min=720, step_min=10,
                        allocated_departures=[-600], requested_departures={"iná trasa": [200]})
    assert other.chosen.depart >= p.new_path_lead_min


# ====================================================================== PR9 poistky ZELENEJ v návrhu k objednávke
def test_navrh_objednavky_bez_kontroly_schranky_a_s_neznamou_vylukou(cfg, fixed_now):
    """[PR9] Návrh k objednávke (e-mail dispečerom, /objednavky): bez úspešnej kontroly schránky a s výlukou, ktorú
    sa nepodarilo umiestniť na sieť pri trase, nesmie byť žiadna varianta ZELENÁ – ako pri vlaku. S čerstvými
    údajmi ZELENÁ ostáva (kontrola)."""
    with session_scope() as s:
        seed_borders(s)
        o = Order(customer="Zákazník FIKTÍVNY", origin="Čadca", destination="Košice", status="nova",
                  earliest_departure_utc=fixed_now + timedelta(hours=10), deadline_utc=fixed_now + timedelta(hours=30))
        s.add(o)
        s.flush()
        fresh = plan_order(s, o.id, fixed_now)
        assert any(v["status"] == "ZELENÁ" for v in fresh["variants"])
        assert not any(e.startswith("ORANŽOVÁ") for e in fresh["explanation"])
        s.execute(delete(KeyValue).where(KeyValue.key == "mail_last_ok"))
        _closure(s, fixed_now, 8, 20, a="Kraľovany", b="Neznáma Dopravňa XY")
        plan = plan_order(s, o.id, fixed_now)
        assert plan["variants"] and not any(v["status"] == "ZELENÁ" for v in plan["variants"])
        amber = [e for e in plan["explanation"] if e.startswith("ORANŽOVÁ")]
        assert amber and "schránku" in amber[0] and "nepodarilo umiestniť" in amber[0]
    assert propose_new_orders(fixed_now) == 1
    with session_scope() as s:
        (mail, *_rest) = _sent(s, "objednavka")
        assert "ORANŽOVÁ – vyžaduje potvrdenie dispečera" in mail.body and "schránku" in mail.body


# ====================================================================== OD3 text limitu 18 h
def test_limit_18h_uplynie_pred_pripravenostou_nie_uz_uplynul(session, fixed_now):
    """[OD3] Vlak stojí 17,5 h, pripravený bude o 1 h: limit uplynie skôr, ako bude pripravený – text nesmie tvrdiť,
    že už stojí dlhšie ako 18 h a že ho PIS zrušil; povie, kedy limit uplynie."""
    seed_borders(session)
    bc = find_border(session, "Čadca")
    arrival = fixed_now - timedelta(hours=17, minutes=30)
    t = _train(session, fixed_now, number="73060", destination="Žilina", status="prevzaty",
               ready_utc=fixed_now + timedelta(hours=1))
    session.add(Handover(train_id=t.id, border_id=bc.id, direction="vstup", status="prevzaty", arrived_utc=arrival,
                         taken_over_utc=fixed_now - timedelta(hours=16)))
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.decision == "ESKALOVAŤ" and rec.color == "ČERVENÁ"
    limit = arrival + timedelta(hours=18)
    assert any(r.startswith(f"Vlak stojí od {fmt(arrival)} – limit 18 h státia (PPŽS) uplynie {fmt(limit)}, skôr "
                            f"ako bude vlak pripravený") for r in rec.reasons)
    assert not any("dlhšie ako 18 h" in r for r in rec.reasons)
    assert "zostáva 0,0 h" not in rec.summary and "na odchod už nezostáva čas" in rec.summary
