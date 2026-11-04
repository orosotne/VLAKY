"""Regresné testy k 5. revízii (oblasť C – plánovač), doplnok po kontrole opráv R5P2 a R5P3.

R5P2 (doplnok) – idúci vlak, ktorý čaká pred výlukou („POČKAŤ, pokračovať v jazde o …“), porovnáva sa podľa
predpokladaného príchodu. Pri starej polohe (vlak stojí pred výlukou a hlásenia TIS neposiela, GPS vypadla) bol
príchod označený ako „posúvajúci sa s časom“ a každý posun sa bral ako nepodstatný – predĺženie výluky o hodiny
sa stratilo v tichej aktualizácii (bez nového odporúčania a e-mailu, prijaté POČKAŤ ostalo so starým časom).
Pri čakaní pred výlukou určuje čas pokračovania aj príchod koniec výluky, nie „teraz“ – porovnáva sa aj pri starej
polohe; nové odporúčania pri každej polohe (pôvodné R5P2) sa nevracajú.
R5P3 (doplnok) – denná neoverená výluka z e-mailu: posudzujú sa všetky jej okná, nie len prvé (dnešné okno
skončilo, zajtrajšie zasahuje plán vlaku → tvrdá poistka s oknom, ktoré plán zasahuje). Okno, ktoré skončilo viac
ako 2 h pred možným odchodom vlaku, sa medzi výlukami „na trase vlaku“ neuvádza.

Všetky vlaky, výluky, adresy a osoby sú fiktívne.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from vlaky.agent.beh import run_cycle
from vlaky.cas import fmt, set_fake_now
from vlaky.db import session_scope
from vlaky.hranice import seed_borders
from vlaky.models import (Closure, Driver, DriverShift, ExtractedEvent, Notification, Path, PositionReport,
                          Recommendation, Train)
from vlaky.planovanie import planovac
from vlaky.planovanie.planovac import _km_at_minutes
from vlaky.siet.graf import TrainSpec, haversine, network

SENT = ("odoslana", "outbox")


def _point(route, km: float) -> tuple:
    """Bod trasy (lat, lon) vo vzdialenosti km od začiatku."""
    pts = network().geometry(route)
    cum = 0.0
    for (lo1, la1), (lo2, la2) in zip(pts, pts[1:]):
        d = haversine(la1, lo1, la2, lo2) / 1000.0
        if cum + d >= km:
            return la2, lo2
        cum += d
    return pts[-1][1], pts[-1][0]


def _running_train(number: str, departed: datetime, deadline: datetime) -> int:
    """Idúci vlak Žilina → Košice s rušňovodičom v službe."""
    with session_scope() as s:
        seed_borders(s)
        t = Train(number=number, origin="Žilina", destination="Košice", traction="E", gauge="1435", vmax_kmh=100,
                  status="v_jazde", planned_departure_utc=departed, departed_utc=departed, deadline_utc=deadline,
                  length_m=500, weight_t=1500)
        s.add(t)
        s.flush()
        d = Driver(name=f"R. {number} FIKTÍVNY")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=departed - timedelta(minutes=30),
                          status="prebieha"))
        return t.id


def _planned(number: str, ready: datetime) -> int:
    """Plánovaný vlak Kysak → Košice s pridelenou trasou a naplánovaným rušňovodičom."""
    with session_scope() as s:
        seed_borders(s)
        t = Train(number=number, origin="Kysak", destination="Košice", traction="E", gauge="1435", vmax_kmh=100,
                  status="planovany", ready_utc=ready, deadline_utc=ready + timedelta(hours=10), length_m=500,
                  weight_t=1500)
        s.add(t)
        s.flush()
        s.add(Path(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena", departure_utc=ready))
        d = Driver(name=f"R. {number} FIKTÍVNY")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=ready - timedelta(minutes=80), status="planovana"))
        return t.id


def _gps(tid: int, route, km: float, ts: datetime) -> None:
    lat, lon = _point(route, km)
    with session_scope() as s:
        s.add(PositionReport(train_id=tid, train_number="", ts_utc=ts, lat=lat, lon=lon, source="gps"))


def _verified_closure(start: datetime, end: datetime, a: str = "Kysak", b: str = "Košice") -> None:
    with session_scope() as s:
        s.add(Closure(kind="uplna", from_station=a, to_station=b, start_utc=start, end_utc=end, status="overene",
                      source="manual", note="FIKTÍVNA"))


def _email_closure(start: datetime, end: datetime, daily: tuple = ("", "")) -> None:
    """Výluka Kysak – Košice z e-mailu, ktorú dispečer ešte neoveril (udalosť čaká vo fronte, záznam „navrh“);
    ``daily`` – denné okno v miestnom čase („06:00“, „09:00“)."""
    with session_scope() as s:
        ev = ExtractedEvent(kind="vyluka_nova", status="navrh", needs_confirmation=True, extractor="pravidla",
                            data={"from_station": "Kysak", "to_station": "Košice", "start": start.isoformat(),
                                  "end": end.isoformat(), "kind": "uplna", "daily_from": daily[0],
                                  "daily_to": daily[1]}, confidence=0.9)
        s.add(ev)
        s.flush()
        s.add(Closure(kind="uplna", from_station="Kysak", to_station="Košice", start_utc=start, end_utc=end,
                      status="navrh", source="email", source_event_id=ev.id, daily_from=daily[0] or None,
                      daily_to=daily[1] or None, note="FIKTÍVNA"))


def _extend(hours: float) -> None:
    """ŽSR predĺži (jedinú) výluku – zmena overenej výluky."""
    with session_scope() as s:
        c = s.scalar(select(Closure))
        c.end_utc, c.version = c.end_utc + timedelta(hours=hours), c.version + 1


def _mails(after_id: int = 0) -> list:
    """E-maily s odporúčaniami dispečerom (odoslané) po správe ``after_id``: [(id, odporúčanie, príjemca, predmet)]."""
    with session_scope() as s:
        return [(n.id, n.recommendation_id, n.recipient, n.subject) for n in s.scalars(
            select(Notification).where(Notification.kind == "odporucanie", Notification.channel == "email",
                                       Notification.id > after_id, Notification.status.in_(SENT))
            .order_by(Notification.id))]


def _current(tid: int) -> Recommendation:
    with session_scope() as s:
        rec = s.scalar(select(Recommendation).where(Recommendation.train_id == tid)
                       .order_by(Recommendation.id.desc()))
        s.expunge(rec)
        return rec


# ------------------------------------------------------------------ R5P2 – čakanie pred výlukou pri starej polohe
def test_r5p2_tis_vlak_caka_pred_vylukou_predlzenie_vyluky_dispecer_dostane(cfg, fixed_now):
    """[R5P2] Vlak hlásený len cez TIS prišiel do Kysaku a čaká pred výlukou Kysak – Košice (stojaci vlak ďalšie
    hlásenie nepošle, poloha starne). ŽSR výluku predĺži o 2 h: dispečeri dostanú práve jedno nové odporúčanie
    POČKAŤ s novým časom pokračovania (nie tichú aktualizáciu) a potom už nič ďalšie."""
    from vlaky.web.pozicie import RunningEventIn, ingest_running_event

    t0 = fixed_now  # 05:00 miestneho času
    tid = _running_train("79641", t0 - timedelta(hours=3), t0 + timedelta(hours=14))
    _verified_closure(t0 - timedelta(hours=1), t0 + timedelta(hours=3))
    with session_scope() as s:
        # posledné hlásenie TIS: príchod do Kysaku – vlak stojí pred výlukou, ďalšie hlásenie príde až pri odchode
        ingest_running_event(s, RunningEventIn(train_number="79641", station="Kysak", event="prichod",
                                               ts=t0 - timedelta(minutes=1), source="tis"))
    for minutes in (0, 30, 60):
        now = t0 + timedelta(minutes=minutes)
        set_fake_now(now)
        run_cycle(now, force_evaluate=True)
    before = _current(tid)
    assert before.decision == "POČKAŤ", before.summary  # predpoklad testu: vlak čaká pred výlukou
    assert datetime.fromisoformat(before.chosen["depart"]) >= t0 + timedelta(hours=3), before.chosen
    mark = max((m[0] for m in _mails()), default=0)
    _extend(2)  # výluka do 10:00 miestneho času
    for minutes in (65, 80, 95, 110):
        now = t0 + timedelta(minutes=minutes)
        set_fake_now(now)
        run_cycle(now, force_evaluate=True)
    after = _current(tid)
    assert after.id != before.id and after.decision == "POČKAŤ", (before.summary, after.summary)
    new_depart = datetime.fromisoformat(after.chosen["depart"])
    assert new_depart >= t0 + timedelta(hours=5), after.chosen  # pokračovanie až po predĺženej výluke
    new = _mails(mark)
    assert sorted(m[2] for m in new) == ["dispecer1@dopravca.example", "dispecer2@dopravca.example"], new
    assert all(m[1] == after.id and f"POČKAŤ {fmt(new_depart)}" in m[3] for m in new), new


def test_r5p2_prijate_pockat_so_starou_polohou_predlzena_vyluka_je_nove_odporucanie(cfg, fixed_now):
    """[R5P2] Dispečer prijal „POČKAŤ, pokračovať v jazde o …“, GPS vlaku medzitým vypadla (poloha 61 min stará).
    ŽSR výluku predĺži o 3 h: vznikne nové odporúčanie s novým časom – prijaté odporúčanie so starým časom nesmie
    zostať platné."""
    t0 = fixed_now
    route = network().route("Žilina", "Košice", TrainSpec())
    tid = _running_train("79642", t0 - timedelta(minutes=10), t0 + timedelta(hours=14))
    _verified_closure(t0 + timedelta(hours=2), t0 + timedelta(hours=5))
    _gps(tid, route, _km_at_minutes(route, 10.0), t0 - timedelta(minutes=1))
    now = t0 + timedelta(minutes=60)
    set_fake_now(now)
    with session_scope() as s:
        rec = planovac.evaluate_train(s, tid, now, "prepočet")
        assert rec.decision == "POČKAŤ", rec.summary  # predpoklad testu
        rec.state, rec.decided_by, rec.decided_at = "prijate", "dispecer1", now  # dispečer prijal
        accepted_id, accepted_depart = rec.id, datetime.fromisoformat(rec.chosen["depart"])
    _extend(3)
    now = t0 + timedelta(minutes=65)
    set_fake_now(now)
    with session_scope() as s:
        rec = planovac.evaluate_train(s, tid, now, "zmena výluky")
        assert rec.id != accepted_id and rec.state == "nove" and rec.decision == "POČKAŤ", rec.summary
        assert datetime.fromisoformat(rec.chosen["depart"]) - accepted_depart >= timedelta(hours=2), rec.chosen


def test_r5p2_pockat_o_krok_vypoctu_a_potom_predlzena_vyluka(cfg, fixed_now):
    """[R5P2] Dispečer dostal POČKAŤ s čakaním len o krok výpočtu (výluka pred vlakom práve končí, poloha je stará)
    a ŽSR potom výluku predĺži o 2 h – nové odporúčanie (čas pokračovania aj príchod určuje koniec výluky)."""
    t0 = fixed_now
    route = network().route("Žilina", "Košice", TrainSpec())
    kysak_min = next(m for st, _km, m in route.stations if st.name == "Kysak")
    tid = _running_train("79643", t0 - timedelta(hours=3), t0 + timedelta(hours=10))
    # výluka skončila pred 5 min (+ 30 min rezerva); stará poloha asi 20 min jazdy pred Kysakom
    _verified_closure(t0 - timedelta(hours=3), t0 - timedelta(minutes=5))
    _gps(tid, route, _km_at_minutes(route, kysak_min - 20.0), t0 - timedelta(minutes=50))
    with session_scope() as s:
        first = planovac.evaluate_train(s, tid, t0, "prepočet")
        assert first.decision == "POČKAŤ", first.summary  # predpoklad testu
        wait = datetime.fromisoformat(first.chosen["depart"]) - t0
        assert wait <= timedelta(minutes=cfg.step_minutes), first.chosen  # čakanie len o krok výpočtu
        first_id = first.id
    with session_scope() as s:
        same = planovac.evaluate_train(s, tid, t0 + timedelta(minutes=1), "prepočet")
        assert same.id == first_id, same.summary  # bez zmeny údajov nič nové
    _extend(2)
    now = t0 + timedelta(minutes=2)
    set_fake_now(now)
    with session_scope() as s:
        rec = planovac.evaluate_train(s, tid, now, "zmena výluky")
        assert rec.id != first_id and rec.decision == "POČKAŤ", rec.summary
        assert datetime.fromisoformat(rec.chosen["depart"]) >= t0 + timedelta(hours=2), rec.chosen


def test_r5p2_kontrola_poloha_kazdych_30_min_prepocet_kazdych_15_min_bez_novych_odporucani(cfg, fixed_now):
    """[R5P2 – kontrola] Vlak ide k výluke a čaká pred ňou; GPS každých 30 min, prepočet každých 15 min (každý druhý
    prepočet so starnúcou polohou). Čas pokračovania sa posúva, príchod nie – ostáva jedno odporúčanie POČKAŤ."""
    t0 = fixed_now
    route = network().route("Žilina", "Košice", TrainSpec())
    kysak_km = next(km for st, km, _m in route.stations if st.name == "Kysak")
    dep = t0 - timedelta(minutes=10)
    tid = _running_train("79644", dep, t0 + timedelta(hours=10))
    _verified_closure(t0 + timedelta(hours=3), t0 + timedelta(hours=6))
    rows = []
    for k in range(9):
        now = t0 + timedelta(minutes=15 * k)
        set_fake_now(now)
        if k % 2 == 0:
            run = (now - dep).total_seconds() / 60.0
            _gps(tid, route, min(_km_at_minutes(route, run), kysak_km), now - timedelta(minutes=1))
        with session_scope() as s:
            rec = planovac.evaluate_train(s, tid, now, "prepočet")
            rows.append((fmt(now), rec.id, rec.decision, rec.chosen.get("depart"), rec.chosen.get("arrival_p50")))
    waits = [r for r in rows if r[2] == "POČKAŤ"]
    assert len(waits) >= 6, rows  # predpoklad testu
    assert len({r[1] for r in waits}) == 1, rows


# ------------------------------------------------------------------ R5P3 – denná neoverená výluka, texty
def test_r5p3_denna_neoverena_vyluka_zajtrajsie_okno_v_plane_je_tvrda_poistka(cfg, fixed_now):
    """[R5P3] Neoverená denná výluka z e-mailu 06:00 – 09:00 na 02. a 03.11.; vlak odchádza 03.11. 09:40 (40 min po
    konci zajtrajšieho okna). Dnešné okno skončilo dávno pred jazdou vlaku, zajtrajšie však zasahuje jeho plán:
    tvrdá poistka – nové odporúčanie a e-mail dispečerom, dôvod uvádza zajtrajšie okno (nie „netýka sa“)."""
    t0 = fixed_now  # 02.11. 05:00 miestneho času
    tid = _planned("77651", t0 + timedelta(hours=28, minutes=40))  # 03.11. 09:40
    run_cycle(t0, force_evaluate=True)
    first = _current(tid)
    assert first.color == "ZELENÁ", first.reasons  # predpoklad testu
    mark = max((m[0] for m in _mails()), default=0)
    _email_closure(t0 + timedelta(hours=1), t0 + timedelta(hours=29), daily=("06:00", "09:00"))
    now = t0 + timedelta(minutes=5)
    set_fake_now(now)
    run_cycle(now, force_evaluate=True)
    rec = _current(tid)
    assert rec.id != first.id and rec.color != "ZELENÁ", rec.summary
    guard = [r for r in rec.reasons if "nie je overená dispečerom" in r]
    assert guard and all("03.11. 06:00 – 03.11. 09:00" in r and "netýka" not in r for r in guard), rec.reasons
    new = _mails(mark)
    assert sorted(m[2] for m in new) == ["dispecer1@dopravca.example", "dispecer2@dopravca.example"], new


def test_r5p3_kontrola_denna_vyluka_mimo_planu_vlaku_je_makka_poistka(cfg, fixed_now):
    """[R5P3 – kontrola] Tá istá denná výluka a vlak 02.11. 13:00: dnešné okno skončilo 4 h pred jeho odchodom,
    zajtrajšie začína až dlho po jeho príchode – plán vlaku sa výluky netýka (mäkká poistka: ORANŽOVÁ na mieste,
    bez nového odporúčania)."""
    t0 = fixed_now
    tid = _planned("77652", t0 + timedelta(hours=8))  # 02.11. 13:00
    with session_scope() as s:
        first = planovac.evaluate_train(s, tid, t0, "nový vlak")
        assert first.color == "ZELENÁ", first.reasons  # predpoklad testu
        first_id = first.id
    _email_closure(t0 + timedelta(hours=1), t0 + timedelta(hours=29), daily=("06:00", "09:00"))
    with session_scope() as s:
        rec = planovac.evaluate_train(s, tid, t0 + timedelta(minutes=5), "nová výluka z e-mailu")
        assert rec.id == first_id and rec.color == "ORANŽOVÁ", rec.summary
        assert any("plán vlaku sa jej podľa prečítaného termínu netýka" in r for r in rec.reasons), rec.reasons


def test_r5p3_vyluka_skoncena_pred_jazdou_vlaku_sa_neuvadza_na_trase_vlaku(cfg, fixed_now):
    """[R5P3 – text] Vlak zajtra 09:00; neoverená výluka z e-mailu dnes 12:00 – 16:00 a overená výluka dnes
    06:00 – 10:00 na tom istom úseku skončili dávno pred jeho jazdou: nie sú „na trase vlaku“ (poistka neoverenej
    výluky ostáva s textom „netýka sa“). Vlak dnes počas výluky ju medzi výlukami na trase má."""
    t0 = fixed_now  # 02.11. 05:00 miestneho času
    tomorrow = _planned("77653", t0 + timedelta(hours=28))     # 03.11. 09:00
    today = _planned("77654", t0 + timedelta(hours=7, minutes=30))  # 02.11. 12:30 – vo výluke
    _verified_closure(t0 + timedelta(hours=1), t0 + timedelta(hours=5))   # 06:00 – 10:00
    _email_closure(t0 + timedelta(hours=7), t0 + timedelta(hours=11))     # 12:00 – 16:00
    with session_scope() as s:
        rec = planovac.evaluate_train(s, tomorrow, t0, "pravidelný prepočet")
        listed = [r for r in rec.reasons if "na trase vlaku" in r]
        assert not listed, rec.reasons
        assert not any("ešte nie je potvrdená dispečerom" in r for r in rec.reasons), rec.reasons
        assert any("plán vlaku sa jej podľa prečítaného termínu netýka" in r for r in rec.reasons), rec.reasons
        assert rec.color == "ORANŽOVÁ"
        hit = planovac.evaluate_train(s, today, t0, "pravidelný prepočet")
        assert any("02.11. 12:00 – 02.11. 16:00 na trase vlaku – NEPOTVRDENÁ" in r for r in hit.reasons), hit.reasons
