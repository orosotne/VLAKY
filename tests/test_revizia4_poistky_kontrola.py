"""Regresné testy k 4. revízii – nálezy kontroly opráv poistiek plánovača (oblasť P4 „poistky“).

* QU2 – posun hodín servera späť (NTP po obnovení virtuálneho servera): uložený začiatok prepočtu „v budúcnosti“ nie
  je novší prepočet – odporúčania vlaku nezamrznú, nová výluka zmení odporúčanie hneď.
* QP4 – čakajúca zmena pridelenej trasy (KIS) obmedzí aj plán na novú trasu (zmenená trasa môže vlaku vyhovovať);
  čakajúce zrušenie pridelenej trasy bez objednanej novej trasy tiež. Zrušenie, ktoré nariadilo prijaté odporúčanie
  („pôvodnú trasu odrieknuť, nová trasa na …“), je len informácia.
* QP3 – idúci vlak tesne pred cieľom bez bezpečnej možnosti (cieľová stanica zatvorená dlhšie, ako siaha prehľadávané
  obdobie): najmenej zlé čakanie sa s časom neposúva do nových odporúčaní (SMS, e-maily), text neuvádza čas odchodu,
  ktorý je len hranicou výpočtu, a vymyslený rušňovodič od odchodu nepridá k príchodu 2 h; predĺženie výluky (iný
  príchod) je nové odporúčanie.
* QP0 – čakajúca udalosť z e-mailu bez miesta (môže sa týkať ktoréhokoľvek vlaku): vlaky nie sú ZELENÉ, ale nepríde
  správa o každom vlaku – dispečer dostane jedno upozornenie o udalosti (spracovanie pošty).
* QO2 – neoverená výluka z e-mailu, ktorá podľa prečítaného termínu leží mimo plánu vlaku (začína až po jeho
  príchode): vlak nie je ZELENÝ, ale bez správy o každom vlaku na trati; vlak, ktorého plánu sa týka, dostane
  odporúčanie so správou.

Všetky vlaky a adresy sú fiktívne (*.example).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from vlaky.agent.zdravie import set_kv
from vlaky.cas import fmt, local, set_fake_now
from vlaky.db import session_scope
from vlaky.hranice import seed_borders
from vlaky.models import (Closure, Driver, DriverShift, ExtractedEvent, Notification, Path, PositionReport,
                          Recommendation, Train)
from vlaky.notifikacie.odosielanie import SENT_STATES
from vlaky.notifikacie.texty import recommendation_body, recommendation_subject, sms_text
from vlaky.planovanie import planovac
from vlaky.planovanie.planovac import evaluate_train
from vlaky.posta.spracovanie import reject_event
from vlaky.siet.graf import TrainSpec, haversine, network

PENDING_CAP = "čakajúca udalosť z e-mailu, s ktorou plánovač nevie počítať"
NO_PLACE = "miesto sa nepodarilo prečítať – overte vo fronte"
UNVERIFIED = "ešte nie je overená dispečerom"
OUT_OF_PLAN = "plán vlaku sa jej podľa prečítaného termínu netýka"


# ====================================================================== pomocné
def _recs(session, train_id) -> list:
    return list(session.scalars(select(Recommendation).where(Recommendation.train_id == train_id)
                                .order_by(Recommendation.id)))


def _train(session, now, number, origin="Žilina", destination="Košice", ready_min=30, path_min=40,
           shift_min=-80, deadline_h=10) -> Train:
    """Plánovaný vlak s pridelenou trasou o ``path_min`` (None – bez trasy) a rušňovodičom (nástup voči pripravenosti)."""
    ready = now + timedelta(minutes=ready_min)
    t = Train(number=number, run_date=local(ready).date().isoformat(), origin=origin, destination=destination,
              traction="E", gauge="1435", vmax_kmh=100, status="planovany", ready_utc=ready,
              deadline_utc=ready + timedelta(hours=deadline_h), length_m=500, weight_t=1500)
    session.add(t)
    session.flush()
    if path_min is not None:
        session.add(Path(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena",
                         departure_utc=now + timedelta(minutes=path_min)))
    d = Driver(name=f"R. {number} FIKTÍVNY")
    session.add(d)
    session.flush()
    session.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=ready + timedelta(minutes=shift_min),
                            status="planovana"))
    session.flush()
    return t


def _path_event(session, kind, number, day) -> ExtractedEvent:
    """Zrušenie či zmena pridelenej trasy z e-mailu KIS – čaká vo fronte na potvrdenie."""
    ev = ExtractedEvent(kind=kind, status="navrh", needs_confirmation=True, confidence=0.9, extractor="pravidla",
                        data={"train_number": number, "date": day}, validation={"blocking": []})
    session.add(ev)
    session.flush()
    return ev


def _email_closure(session, now, a, b, start_h, end_h, kind="uplna") -> Closure:
    """Výluka z e-mailu, ktorú dispečer ešte neoveril: udalosť vo fronte a jej záznam „navrh“ pre plánovač."""
    start, end = now + timedelta(hours=start_h), now + timedelta(hours=end_h)
    ev = ExtractedEvent(kind="vyluka_nova", status="navrh", needs_confirmation=True, confidence=0.85,
                        extractor="pravidla", data={"from_station": a, "to_station": b, "start": start.isoformat(),
                                                    "end": end.isoformat(), "kind": kind},
                        validation={"blocking": [], "pending_closure": {"id": None, "note": ""}})
    session.add(ev)
    session.flush()
    c = Closure(kind=kind, from_station=a, to_station=b, start_utc=start, end_utc=end, status="navrh",
                source="email", source_event_id=ev.id)
    session.add(c)
    session.flush()
    ev.validation = {"blocking": [], "pending_closure": {"id": c.id, "note": ""}}
    session.flush()
    return c


def _sent(session, after_id: int, kind: str = "odporucanie") -> list:
    return [n for n in session.scalars(select(Notification).where(Notification.id > after_id,
                                                                    Notification.kind == kind))
            if n.status in SENT_STATES]


def _last_notification(session) -> int:
    return max((n.id for n in session.scalars(select(Notification))), default=0)


# ====================================================================== QU2 posun hodín späť
def test_qu2_posun_hodin_spat_nezmrazi_odporucania(cfg, fixed_now, monkeypatch, caplog):
    """[QU2] Odporúčanie uložil prepočet, keď hodiny servera išli o hodinu napred; NTP ich vráti späť. Nový prepočet
    (dispečer zadal overenú úplnú výluku cez celú jazdu) začal „pred“ uloženým začiatkom – nie je to však starší
    súbežný prepočet, ale posun hodín: výsledok sa uloží (nie VYPRAVIŤ ZELENÁ cez výluku) s upozornením v logu
    a ďalšie prepočty sa riadia novým časom."""
    wall = {"t": datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(planovac, "_wall_clock", lambda: wall["t"])
    with session_scope() as s:
        seed_borders(s)
        tid = _train(s, fixed_now, "47290").id
    with session_scope() as s:
        first = evaluate_train(s, tid, fixed_now, "nový vlak")
        first_id = first.id
        assert (first.decision, first.color) == ("VYPRAVIŤ", "ZELENÁ"), first.reasons
    wall["t"] = datetime(2026, 11, 4, 11, 5, tzinfo=timezone.utc)          # NTP: hodiny späť o 55 min
    with session_scope() as s:
        s.add(Closure(kind="uplna", from_station="Kysak", to_station="Košice", status="overene",
                      start_utc=fixed_now, end_utc=fixed_now + timedelta(hours=12)))
    with caplog.at_level(logging.WARNING, logger="vlaky.planovanie"):
        with session_scope() as s:
            rec = evaluate_train(s, tid, fixed_now + timedelta(minutes=2), "nová výluka (zadal dispečer)")
            rec_id, decision, color = rec.id, rec.decision, rec.color
            assert rec.data_age["eval_started"] == wall["t"].isoformat()
    assert rec_id != first_id and (decision, color) != ("VYPRAVIŤ", "ZELENÁ"), (decision, color)
    assert any("posunuli späť" in r.getMessage() for r in caplog.records)
    with session_scope() as s:
        assert [(r.id, r.state) for r in _recs(s, tid)] == [(first_id, "zastarane"), (rec_id, "nove")]
    wall["t"] += timedelta(minutes=15)
    with session_scope() as s:
        planovac.evaluate_all(s, fixed_now + timedelta(minutes=15), "pravidelný prepočet")
    with session_scope() as s:
        cur = _recs(s, tid)[-1]
        assert cur.state == "nove" and cur.decision == decision
        assert cur.data_age["eval_started"] == wall["t"].isoformat()     # novší prepočet uložil (potvrdil) výsledok


# ====================================================================== QP4 čakajúca zmena/zrušenie pridelenej trasy
def _missed_path_train(session, now, number) -> Train:
    """Vlak pripravený o 3 h, jeho pridelená trasa je o 40 min (nestihne ju): plán ide na novú (ad hoc) trasu po
    lehote P5; rušňovodič nastupuje 1 h pred jej odchodom."""
    seed_borders(session)
    t = _train(session, now, number, ready_min=180, path_min=40, shift_min=120, deadline_h=14)
    session.commit()
    return t


def test_qp4_cakajuca_zmena_pridelenej_trasy_obmedzi_aj_plan_na_novu_trasu(session, fixed_now):
    """[QP4] Vlak nestihne pridelenú trasu – agent odporučí novú ad hoc trasu (ZELENÁ). KIS pošle zmenu pridelenej
    trasy (čaká vo fronte): zmenená trasa môže vlaku vyhovovať a nová trasa by bola zbytočná – kým dispečer zmenu
    nepotvrdí alebo nezamietne, plán nie je ZELENÝ a dôvod to povie (ako upozornenie agenta k udalosti)."""
    t = _missed_path_train(session, fixed_now, "47300")
    before = evaluate_train(session, t.id, fixed_now, "nový vlak")
    assert before.chosen["needs_new_path"] and before.color == "ZELENÁ", (before.color, before.reasons)
    ev = _path_event(session, "trasa_zmena", "47300", fixed_now.date().isoformat())
    session.commit()
    rec = evaluate_train(session, t.id, fixed_now + timedelta(minutes=5), "e-mail: zmena trasy")
    assert rec.chosen["needs_new_path"] and rec.color == "ORANŽOVÁ", (rec.color, rec.reasons)
    note = next(r for r in rec.reasons if f"udalosť #{ev.id}" in r)
    assert "Zmena trasy vlaku 47300" in note and "nemôže byť ZELENÝ" in note
    assert PENDING_CAP in rec.data_age["obmedzenie"]


def test_qp4_cakajuce_zrusenie_trasy_bez_objednanej_novej_obmedzi(session, fixed_now):
    """[QP4] Čakajúce zrušenie pridelenej trasy, ktoré prijaté odporúčanie nenariadilo (novú trasu dispečer ešte
    neobjednal): ani plán na novú trasu nie je ZELENÝ – zrušenie je udalosť, s ktorou plánovač nevie počítať."""
    t = _missed_path_train(session, fixed_now, "47301")
    before = evaluate_train(session, t.id, fixed_now, "nový vlak")
    assert before.chosen["needs_new_path"] and before.color == "ZELENÁ"
    ev = _path_event(session, "trasa_zrusena", "47301", fixed_now.date().isoformat())
    session.commit()
    rec = evaluate_train(session, t.id, fixed_now + timedelta(minutes=5), "e-mail: zrušenie trasy")
    assert rec.color == "ORANŽOVÁ" and any(f"udalosť #{ev.id}" in r and "nemôže byť ZELENÝ" in r
                                            for r in rec.reasons), rec.reasons
    assert PENDING_CAP in rec.data_age["obmedzenie"]


def test_qp4_zrusenie_nariadene_prijatym_odporucanim_je_len_informacia(session, fixed_now):
    """[QP4] Prijaté „ODSTAVIŤ …, pôvodnú trasu odrieknuť, nová trasa na D“; KIS potvrdí zrušenie pôvodnej trasy
    (čaká vo fronte) a výpadok schránky vytvorí nové odporúčanie: zrušenie je očakávané – dôvod to povie (potvrdiť
    vo fronte), ale semafor neobmedzí (obmedzuje len schránka)."""
    seed_borders(session)
    t = Train(number="90103", run_date=fixed_now.date().isoformat(), origin="Čadca", destination="Košice",
              traction="E", gauge="1435", vmax_kmh=100, status="planovany", ready_utc=fixed_now,
              deadline_utc=fixed_now + timedelta(hours=30))
    session.add(t)
    session.flush()
    session.add(Path(train_id=t.id, train_number="90103", kind="ad_hoc", status="pridelena",
                     departure_utc=fixed_now + timedelta(minutes=30)))
    session.add(Closure(kind="uplna", from_station="Liptovský Mikuláš", to_station="Poprad-Tatry", status="overene",
                        start_utc=fixed_now - timedelta(hours=1), end_utc=fixed_now + timedelta(hours=8)))
    session.flush()
    first = evaluate_train(session, t.id, fixed_now, "test")
    assert first.decision == "ODSTAVIŤ" and first.chosen["needs_new_path"]
    first.state = "prijate"
    ev = _path_event(session, "trasa_zrusena", "90103", fixed_now.date().isoformat())
    out = fixed_now + timedelta(minutes=45)
    set_kv(session, "mail_last_ok", {"ts": (out - timedelta(minutes=50)).isoformat(), "fetched": 0})
    session.commit()
    rec = evaluate_train(session, t.id, out, "výpadok schránky")
    assert rec.id != first.id and rec.decision == "ODSTAVIŤ" and rec.color == "ORANŽOVÁ"
    dep = fmt(datetime.fromisoformat(first.chosen["depart"]))
    note = next(r for r in rec.reasons if f"udalosť #{ev.id}" in r)
    assert "odriekol dispečer podľa prijatého odporúčania" in note and f"nová trasa na {dep}" in note, note
    assert "nemôže byť ZELENÝ" not in note
    assert "schránka agenta nebola úplne skontrolovaná v posledných 30 min" in rec.data_age["obmedzenie"]
    assert PENDING_CAP not in rec.data_age["obmedzenie"]


# ====================================================================== QP3 vlak pred zatvoreným cieľom
def _point_at(net, r, km):
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


def test_qp3_najmenej_zle_cakanie_pred_cielom_sa_neposuva(cfg, fixed_now):
    """[QP3] Púchov → Trenčín, vlak stojí 800 m pred koncom trasy, Trenčín je zatvorený 30 h (dlhšie, ako siaha
    prehľadávané obdobie). Najmenej zlé je počkať pred vjazdom do konca výluky: prepočet každých 15 min (čerstvá
    rovnaká poloha) počas 2 h je stále to isté odporúčanie – jedna KRITICKÁ SMS a jeden e-mail každému dispečerovi,
    nie nové odporúčanie každých 30 min. Text neuvádza čas odchodu (len hranica výpočtu, posúva sa) – „po uvoľnení
    vjazdu“ – a príchod je koniec výluky + rezerva (nie + 2 h za vymysleného rušňovodiča). Predĺženie výluky o 10 h
    (príchod o 10 h neskôr) je nové odporúčanie."""
    net = network()
    r = net.route("Púchov", "Trenčín", TrainSpec())
    lat, lon = _point_at(net, r, r.length_m / 1000.0 - 0.8)
    closure_end = fixed_now + timedelta(hours=30)
    with session_scope() as s:
        seed_borders(s)
        s.add(Closure(kind="mimoriadnost", from_station="Trenčín", to_station="Trenčín", status="overene",
                      start_utc=fixed_now - timedelta(hours=1), end_utc=closure_end))
        t = Train(number="79945", origin="Púchov", destination="Trenčín", traction="E", gauge="1435", vmax_kmh=100,
                  status="v_jazde", planned_departure_utc=fixed_now - timedelta(minutes=40),
                  departed_utc=fixed_now - timedelta(minutes=40), deadline_utc=fixed_now + timedelta(hours=8))
        s.add(t)
        s.flush()
        d = Driver(name="R. 79945 FIKTÍVNY")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=fixed_now - timedelta(hours=1), status="prebieha"))
        tid = t.id
    from vlaky.agent.beh import run_cycle

    seen = []
    for k in range(9):
        now = fixed_now + timedelta(minutes=15 * k)
        set_fake_now(now)
        with session_scope() as s:
            s.add(PositionReport(train_id=tid, train_number="79945", ts_utc=now, lat=lat, lon=lon, source="gps"))
        run_cycle(now, force_evaluate=True)          # prepočet a upozornenia ako v cykle agenta
        with session_scope() as s:
            rec = _recs(s, tid)[-1]
            seen.append((rec.id, rec.decision, rec.color, rec.chosen.get("arrival_p50")))
    assert {x[0] for x in seen} == {seen[0][0]}, seen
    assert all(x[1:3] == ("POČKAŤ", "ČERVENÁ") for x in seen), seen
    arrival = datetime.fromisoformat(seen[-1][3])
    assert closure_end + timedelta(minutes=30) <= arrival <= closure_end + timedelta(minutes=45), arrival
    with session_scope() as s:
        rec = _recs(s, tid)[-1]
        assert "dokončiť jazdu po uvoľnení vjazdu" in rec.summary and "dokončiť jazdu o" not in rec.summary
        assert any(f"kým sa neuvoľní vjazd (príchod podľa výpočtu {fmt(arrival)})" in x for x in rec.reasons), \
            rec.reasons
        assert "POČKAŤ pred cieľom (ČERVENÁ)" in recommendation_subject(rec)
        sms = sms_text(rec)
        assert "POČKAŤ pred cieľom" in sms and fmt(datetime.fromisoformat(rec.chosen["depart"])) not in sms
        assert "Odchod: po uvoľnení vjazdu do cieľa" in recommendation_body(rec)
        sent = _sent(s, 0)
        assert sorted((n.channel, n.recipient) for n in sent) == [
            ("email", "dispecer1@dopravca.example"), ("email", "dispecer2@dopravca.example"), ("sms", "+421900000001")]
        s.scalars(select(Closure)).one().end_utc = closure_end + timedelta(hours=10)
    now = fixed_now + timedelta(minutes=135)
    set_fake_now(now)
    with session_scope() as s:
        s.add(PositionReport(train_id=tid, train_number="79945", ts_utc=now, lat=lat, lon=lon, source="gps"))
        s.flush()
        longer = evaluate_train(s, tid, now, "výluka predĺžená")
        assert longer.id != seen[0][0] and longer.decision == "POČKAŤ", (longer.id, longer.decision)
        assert datetime.fromisoformat(longer.chosen["arrival_p50"]) >= closure_end + timedelta(hours=10)


# ====================================================================== QP0 udalosť bez miesta – jedno upozornenie
PAIRS = [("Žilina", "Košice"), ("Bratislava hlavná stanica", "Nové Zámky"), ("Púchov", "Trenčín")]


def test_qp0_udalost_bez_miesta_obmedzi_vlaky_bez_spravy_o_kazdom(cfg, fixed_now):
    """[QP0] ŽSR pošle „Zmena ROV …“ bez úseku aj termínu (udalosť bez miesta čaká vo fronte): môže sa týkať
    ktoréhokoľvek vlaku, takže žiadny vlak nie je ZELENÝ – ale o každom vlaku nepríde nové odporúčanie so správou
    (dispečer dostane jedno upozornenie o udalosti). Nové odporúčanie sa len aktualizuje (ORANŽOVÁ s dôvodom),
    prijaté ostane prijaté a aktuálne je nové s prioritou INFO. Po zamietnutí udalosti sú vlaky znova ZELENÉ."""
    from vlaky.agent.beh import run_cycle

    with session_scope() as s:
        seed_borders(s)
        ids = [_train(s, fixed_now, f"9530{k}", origin=a, destination=b, ready_min=30 + 30 * k,
                      path_min=40 + 30 * k).id for k, (a, b) in enumerate(PAIRS)]
    run_cycle(fixed_now)
    with session_scope() as s:
        first = {tid: _recs(s, tid)[-1] for tid in ids}
        assert all((r.decision, r.color) == ("VYPRAVIŤ", "ZELENÁ") for r in first.values()), \
            [(r.decision, r.color, r.reasons) for r in first.values()]
        first[ids[0]].state = "prijate"
        first_ids = {tid: r.id for tid, r in first.items()}
        mark = _last_notification(s)
        ev = ExtractedEvent(kind="vyluka_zmena", status="navrh", needs_confirmation=True, confidence=0.6,
                            extractor="pravidla", data={},
                            validation={"blocking": ["chýba od dopravne", "chýba do dopravne", "chýba začiatok"],
                                        "pending_closure": {"id": None, "proposed_change_of": None,
                                                            "note": "údaje nestačia na záznam výluky (úsek alebo čas)"}})
        s.add(ev)
        s.flush()
        ev_id = ev.id
    later = fixed_now + timedelta(minutes=5)
    set_fake_now(later)
    run_cycle(later, force_evaluate=True)
    with session_scope() as s:
        for tid in ids:
            rec = _recs(s, tid)[-1]
            assert rec.color == "ORANŽOVÁ", (tid, rec.color, rec.reasons)
            note = next(x for x in rec.reasons if f"udalosť #{ev_id}" in x)
            assert NO_PLACE in note and "ktoréhokoľvek vlaku" in note
            assert PENDING_CAP in rec.data_age["obmedzenie"]
            if tid == ids[0]:
                assert rec.id != first_ids[tid] and rec.priority == "INFO"
                assert s.get(Recommendation, first_ids[tid]).state == "prijate"
            else:
                assert rec.id == first_ids[tid] and rec.state == "nove"
        assert _sent(s, mark) == []
        reject_event(s, ev_id, "dispecer1", "administratívna správa – žiadna výluka")
    set_fake_now(later + timedelta(minutes=5))
    run_cycle(later + timedelta(minutes=5), force_evaluate=True)
    with session_scope() as s:
        for tid in ids[1:]:
            rec = _recs(s, tid)[-1]
            assert rec.id == first_ids[tid] and rec.color == "ZELENÁ", (rec.id, rec.color, rec.reasons)
        assert _recs(s, ids[0])[-1].color == "ZELENÁ"
        assert _sent(s, mark) == []


# ====================================================================== QO2 neoverená výluka mimo plánu vlaku
def test_qo2_neoverena_vyluka_mimo_planu_vlaku_bez_spravy_o_kazdom_vlaku(cfg, fixed_now):
    """[QO2] ROV z e-mailu (výluka Kysak – Košice zajtra ráno) čaká na overenie. Vlaky, ktoré idú dnes, ju podľa
    prečítaného termínu nestretnú: nie sú ZELENÉ (termín mohol byť prečítaný nesprávne), ale o každom z nich nepríde
    nové odporúčanie so správou – dôvod povie, že plán vlaku sa výluky netýka. Vlak, ktorý pôjde zajtra cez výluku,
    dostane nové odporúčanie (ORANŽOVÉ) so správou dispečerom."""
    from vlaky.agent.beh import run_cycle

    with session_scope() as s:
        seed_borders(s)
        today = [_train(s, fixed_now, f"9520{k}", ready_min=30 + 60 * k, path_min=40 + 60 * k).id for k in range(3)]
        tomorrow = _train(s, fixed_now, "95209", ready_min=24 * 60 + 30, path_min=24 * 60 + 40).id
    run_cycle(fixed_now)
    with session_scope() as s:
        first = {tid: _recs(s, tid)[-1] for tid in today + [tomorrow]}
        assert all(r.color == "ZELENÁ" for r in first.values()), [(r.color, r.reasons) for r in first.values()]
        first_ids = {tid: r.id for tid, r in first.items()}
        mark = _last_notification(s)
        row = _email_closure(s, fixed_now, "Kysak", "Košice", 25, 29)
        ev_id = row.source_event_id
    later = fixed_now + timedelta(minutes=5)
    set_fake_now(later)
    run_cycle(later, force_evaluate=True)
    with session_scope() as s:
        for tid in today:
            rec = _recs(s, tid)[-1]
            assert rec.id == first_ids[tid] and rec.color == "ORANŽOVÁ", (tid, rec.id, rec.color, rec.reasons)
            note = next(x for x in rec.reasons if f"udalosť #{ev_id}" in x)
            assert UNVERIFIED in note and OUT_OF_PLAN in note and "nemôže byť ZELENÝ" in note
            assert "výluka z e-mailu na trase ešte nie je overená dispečerom" in rec.data_age["obmedzenie"]
        rec = _recs(s, tomorrow)[-1]
        assert rec.id != first_ids[tomorrow] and rec.color == "ORANŽOVÁ", (rec.decision, rec.color, rec.reasons)
        note = next(x for x in rec.reasons if f"udalosť #{ev_id}" in x)
        assert UNVERIFIED in note and OUT_OF_PLAN not in note
        sent = _sent(s, mark)
        assert sent and {n.recommendation_id for n in sent} == {rec.id}, [(n.recommendation_id, n.subject)
                                                                         for n in sent]
