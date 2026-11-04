"""Regresné testy k 5. revízii (oblasť C – plánovač): idúci vlak pred výlukou, neoverená výluka mimo plánu vlaku,
návrh trasy so zadržaním pred výlukou a texty správ o idúcom vlaku.

R5P2 – idúci vlak, ktorý ide k výluke (a počká pred ňou do jej konca), dostával pri každej novej polohe nové
odporúčanie „POČKAŤ, pokračovať v jazde o …“ s neskorším časom, hoci sa predpokladaný príchod nemenil.
R5P3 – neoverená výluka z e-mailu, ktorá skončila pred možnou jazdou vlaku (napr. dnes popoludní a vlak ide zajtra),
dávala tvrdú poistku: nové odporúčanie ORANŽOVÁ a e-mail o každom vlaku na úseku.
R5U4 – návrh trasy najmenej zlej možnosti idúceho vlaku uvádzal časy dopravní vnútri úplnej výluky.
R5U5/R5U6 – porovnanie variantov idúceho vlaku v e-maile (pokračovanie ako „VYPRAVIŤ“ v čase prepočtu, hranica výpočtu
ako odchod), dátum dennej správy v tvare ISO.

Všetky vlaky, výluky a osoby sú fiktívne.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from vlaky.cas import fmt, set_fake_now
from vlaky.db import session_scope
from vlaky.hranice import seed_borders
from vlaky.models import (Closure, Driver, DriverShift, ExtractedEvent, Notification, Path, PositionReport,
                          Recommendation, Train)
from vlaky.planovanie import planovac
from vlaky.planovanie.planovac import _km_at_minutes
from vlaky.siet.graf import TrainSpec, haversine, network


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


def _running_train(number: str, origin: str, dest: str, departed: datetime, deadline: datetime) -> int:
    with session_scope() as s:
        seed_borders(s)
        t = Train(number=number, origin=origin, destination=dest, traction="E", gauge="1435", vmax_kmh=100,
                  status="v_jazde", planned_departure_utc=departed, departed_utc=departed, deadline_utc=deadline,
                  length_m=500, weight_t=1500)
        s.add(t)
        s.flush()
        d = Driver(name=f"R. {number} FIKTÍVNY")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=departed - timedelta(minutes=60),
                          status="prebieha"))
        return t.id


def _position(tid: int, lat: float, lon: float, ts: datetime) -> None:
    with session_scope() as s:
        s.add(PositionReport(train_id=tid, train_number="", ts_utc=ts, lat=lat, lon=lon, source="gps"))


# ------------------------------------------------------------------ R5P2
def test_r5p2_iduci_vlak_k_vyluke_nedostava_pri_kazdej_polohe_nove_odporucanie(cfg, fixed_now):
    """[R5P2] Vlak Žilina → Košice ide k výluke Kysak – Košice a počká pred ňou do jej konca. Poloha prichádza každých
    30 min; čas „pokračovať v jazde o …“ sa s každou polohou posúva o 30 – 40 min, predpokladaný príchod nie. Zostáva
    jedno odporúčanie POČKAŤ (bez nového odporúčania a upozornenia), aktualizované na mieste."""
    t0 = fixed_now
    route = network().route("Žilina", "Košice", TrainSpec())
    stations = {st.name: (km, mins) for st, km, mins in route.stations if st.type == "station"}
    kysak_km, kysak_min = stations["Kysak"]
    dep = t0 - timedelta(minutes=10)
    closure_end = t0 + timedelta(hours=6)
    tid = _running_train("79611", "Žilina", "Košice", dep, t0 + timedelta(hours=10))
    with session_scope() as s:
        s.add(Closure(kind="uplna", from_station="Kysak", to_station="Košice", start_utc=t0 + timedelta(hours=3),
                      end_utc=closure_end, status="overene", source="manual", note="FIKTÍVNA"))
    rows = []
    for k in range(6):
        now = t0 + timedelta(minutes=30 * k)
        set_fake_now(now)
        run = (now - dep).total_seconds() / 60.0
        km = min(_km_at_minutes(route, run), kysak_km)  # jazda podľa plánu, pred výlukou čaká v Kysaku
        _position(tid, *_point(route, km), now - timedelta(minutes=1))
        with session_scope() as s:
            rec = planovac.evaluate_train(s, tid, now, "nová poloha vlaku")
            rows.append((fmt(now), rec.id, rec.decision, (rec.chosen or {}).get("depart"),
                         (rec.chosen or {}).get("arrival_p50")))
    waits = [r for r in rows if r[2] == "POČKAŤ"]
    assert len(waits) >= 4, rows  # predpoklad testu: vlak väčšinu času „POČKAŤ, pokračovať v jazde o …“
    departs = [datetime.fromisoformat(r[3]) for r in waits]
    assert max(departs) - min(departs) >= timedelta(minutes=60), rows  # čas pokračovania sa posúva
    assert len({r[1] for r in waits}) == 1, rows  # … ale odporúčanie je stále to isté
    with session_scope() as s:
        rec = s.get(Recommendation, waits[-1][1])
        assert rec.chosen.get("floating") and rec.chosen.get("compare_arrival") and rec.chosen.get("running")


def test_r5p2_posun_prichodu_o_30_min_je_nove_odporucanie(cfg, fixed_now):
    """[R5P2] Porovnáva sa predpokladaný príchod: predĺženie výluky o 45 min (príchod neskôr o 45 min) pri tom istom
    rozhodnutí POČKAŤ je nové odporúčanie, malý posun (10 min) nie."""
    t0 = fixed_now
    route = network().route("Žilina", "Košice", TrainSpec())
    dep = t0 - timedelta(minutes=10)
    tid = _running_train("79612", "Žilina", "Košice", dep, t0 + timedelta(hours=10))
    with session_scope() as s:
        s.add(Closure(kind="uplna", from_station="Kysak", to_station="Košice", start_utc=t0 + timedelta(hours=3),
                      end_utc=t0 + timedelta(hours=5), status="overene", source="manual", note="FIKTÍVNA"))
    _position(tid, *_point(route, _km_at_minutes(route, 10.0)), t0 - timedelta(minutes=1))
    with session_scope() as s:
        first = planovac.evaluate_train(s, tid, t0, "nová poloha vlaku")
        assert first.decision == "POČKAŤ", first.summary
        first_id, first_arr = first.id, datetime.fromisoformat(first.chosen["arrival_p50"])

    def extend(minutes: int) -> None:
        with session_scope() as s:
            c = s.scalar(select(Closure))
            c.end_utc, c.version = c.end_utc + timedelta(minutes=minutes), c.version + 1

    extend(10)
    with session_scope() as s:
        small = planovac.evaluate_train(s, tid, t0 + timedelta(minutes=2), "zmena výluky")
        assert small.decision == "POČKAŤ" and small.id == first_id, small.summary
    extend(35)
    with session_scope() as s:
        later = planovac.evaluate_train(s, tid, t0 + timedelta(minutes=4), "zmena výluky")
        assert later.decision == "POČKAŤ" and later.id != first_id, later.summary
        assert datetime.fromisoformat(later.chosen["arrival_p50"]) - first_arr >= timedelta(minutes=30)


# ------------------------------------------------------------------ R5P3
def _email_closure(a: str, b: str, start: datetime, end: datetime) -> None:
    """Výluka z e-mailu, ktorú dispečer ešte neoveril (udalosť čaká vo fronte, záznam „navrh“)."""
    with session_scope() as s:
        ev = ExtractedEvent(kind="vyluka_nova", status="navrh", needs_confirmation=True, extractor="pravidla",
                            data={"from_station": a, "to_station": b, "start": start.isoformat(),
                                  "end": end.isoformat(), "kind": "uplna"}, confidence=0.9)
        s.add(ev)
        s.flush()
        s.add(Closure(kind="uplna", from_station=a, to_station=b, start_utc=start, end_utc=end, status="navrh",
                      source="email", source_event_id=ev.id, note="FIKTÍVNA"))


def _planned(number: str, ready: datetime) -> int:
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


def test_r5p3_neoverena_vyluka_pred_jazdou_vlaku_je_len_makka_poistka(cfg, fixed_now):
    """[R5P3] Neoverená výluka z e-mailu dnes 12:00 – 16:00 a vlak zajtra 09:00 na tom istom úseku: plán vlaku sa
    jej netýka – vlak nie je ZELENÝ (termín mohol byť prečítaný nesprávne), ale nevznikne nové odporúčanie ani
    upozornenie (mäkká poistka). Vlak, ktorého plán výluka zasahuje, má tvrdú poistku (nové odporúčanie)."""
    t0 = fixed_now  # 05:00 miestneho času
    tomorrow = _planned("95902", t0 + timedelta(hours=28))     # zajtra 09:00
    overlap = _planned("95903", t0 + timedelta(hours=7, minutes=30))  # dnes 12:30 – vo výluke
    first = {}
    with session_scope() as s:
        for tid in (tomorrow, overlap):
            rec = planovac.evaluate_train(s, tid, t0, "pravidelný prepočet")
            assert rec.color == "ZELENÁ", rec.reasons  # predpoklad testu
            first[tid] = rec.id
    _email_closure("Kysak", "Košice", t0 + timedelta(hours=7), t0 + timedelta(hours=11))
    with session_scope() as s:
        rec = planovac.evaluate_train(s, tomorrow, t0 + timedelta(minutes=5), "nová výluka z e-mailu")
        assert rec.id == first[tomorrow], rec.summary  # žiadne nové odporúčanie (a teda ani e-mail)
        assert rec.color == "ORANŽOVÁ"  # nikdy ZELENÁ s neoverenou výlukou na úseku
        assert any("plán vlaku sa jej podľa prečítaného termínu netýka" in r for r in rec.reasons), rec.reasons
        hit = planovac.evaluate_train(s, overlap, t0 + timedelta(minutes=5), "nová výluka z e-mailu")
        assert hit.id != first[overlap] and hit.color != "ZELENÁ", hit.summary  # tvrdá poistka ostáva
        assert any("údaje (druh, termín, úsek) mohli byť prečítané nesprávne" in r for r in hit.reasons)


def test_r5p3_neoverena_vyluka_davno_skoncena_vlak_neobmedzi(cfg, fixed_now):
    """[R5P3] Neoverená výluka, ktorá skončila pred min(teraz, pripravenosť) − 2 h, vlak vôbec neobmedzí."""
    t0 = fixed_now
    tid = _planned("95904", t0 + timedelta(hours=2))
    _email_closure("Kysak", "Košice", t0 - timedelta(hours=8), t0 - timedelta(hours=3))
    with session_scope() as s:
        rec = planovac.evaluate_train(s, tid, t0, "pravidelný prepočet")
        assert rec.color == "ZELENÁ", rec.reasons
        assert not any("nie je overená dispečerom" in r for r in rec.reasons)


# ------------------------------------------------------------------ R5L1 (časť plánovača): výluky oboch koľají
def test_r5l1_vyluky_oboch_tratovych_kolaji_naraz_su_uplna_vyluka(cfg, fixed_now):
    """[R5L1 – plánovač] Dve výluky koľaje toho istého úseku (1. TK a 2. TK, napr. dva rozkazy) v rovnakom čase:
    úsek nie je zjazdný – vlak cez neho nedostane VYPRAVIŤ počas výluky ani ZELENÚ; dôvod to povie. Výluka len jednej
    koľaje ostáva zdržaním (vlak ide), rovnako ako výluky oboch koľají v rôznom čase."""
    t0 = fixed_now  # 05:00 miestneho času
    ready = t0 + timedelta(hours=1)

    def closures(rows: list) -> None:
        with session_scope() as s:
            for c in s.scalars(select(Closure)):
                s.delete(c)
            for track, a, b in rows:
                s.add(Closure(kind="kolaj", from_station="Kysak", to_station="Košice", track=track,
                              start_utc=t0 + timedelta(hours=a), end_utc=t0 + timedelta(hours=b), status="overene",
                              source="manual", note="FIKTÍVNA"))

    def evaluate(number: str):
        tid = _planned(number, ready)
        with session_scope() as s:
            rec = planovac.evaluate_train(s, tid, t0, "test")
            return rec.decision, rec.color, (rec.chosen or {}).get("depart"), list(rec.reasons)

    closures([("1", 0, 6), ("2", 0, 6)])
    decision, color, depart, reasons = evaluate("95910")
    assert not (decision == "VYPRAVIŤ" and color == "ZELENÁ"), (decision, color, reasons)
    assert datetime.fromisoformat(depart) >= t0 + timedelta(hours=6), (decision, depart)  # až po výluke
    assert any("vylúčené obe traťové koľaje" in r and "počíta sa ako úplná výluka" in r for r in reasons), reasons
    closures([("1", 0, 6)])
    decision, color, depart, _r = evaluate("95911")
    assert decision == "VYPRAVIŤ" and datetime.fromisoformat(depart) < t0 + timedelta(hours=2), (decision, depart)
    closures([("1", 0, 2), ("2", 3, 6)])  # obe koľaje, ale nikdy naraz
    decision, color, depart, _r = evaluate("95912")
    assert decision == "VYPRAVIŤ" and datetime.fromisoformat(depart) < t0 + timedelta(hours=3), (decision, depart)
    closures([("obe", 0, 6)])  # ručne zadaná výluka koľaje s oboma koľajami
    decision, color, depart, _r = evaluate("95913")
    assert datetime.fromisoformat(depart) >= t0 + timedelta(hours=6), (decision, depart)


# ------------------------------------------------------------------ R5U4 + R5U5
def _running_before_long_closure(t0: datetime) -> tuple:
    """Idúci vlak Poprad-Tatry → Žilina pri Liptovskom Mikuláši a úplná výluka Ľubochňa – Kraľovany na 26 h (dlhšie,
    ako siaha prehľadávané obdobie) – každá možnosť ráta so zadržaním pred výlukou."""
    route = network().route("Poprad-Tatry", "Žilina", TrainSpec())
    stations = {st.name: km for st, km, _m in route.stations if st.type == "station"}
    tid = _running_train("95904", "Poprad-Tatry", "Žilina", t0 - timedelta(minutes=40), t0 + timedelta(hours=3))
    end = t0 + timedelta(hours=26)
    with session_scope() as s:
        s.add(Closure(kind="uplna", from_station="Ľubochňa", to_station="Kraľovany",
                      start_utc=t0 + timedelta(minutes=5), end_utc=end, status="overene", source="manual",
                      note="FIKTÍVNA"))
    _position(tid, *_point(route, stations["Liptovský Mikuláš"] + 3.0), t0 - timedelta(minutes=1))
    return tid, end


def test_r5u4_navrh_trasy_neuvadza_casy_vnutri_vyluky(cfg, fixed_now):
    """[R5U4] Najmenej zlá možnosť idúceho vlaku ráta so zadržaním pred výlukou: návrh trasy ukáže čakanie v Ľubochni
    do konca výluky (s rezervou) a úsek Ľubochňa – Kraľovany až po nej – nie časy rovnomerne rozložené cez výluku.
    Príchod do cieľa ostáva predpokladaný príchod odporúčania."""
    t0 = fixed_now
    tid, end = _running_before_long_closure(t0)
    with session_scope() as s:
        rec = planovac.evaluate_train(s, tid, t0, "nová výluka")
        assert rec.decision == "ESKALOVAŤ" and rec.chosen.get("p_hold", 0) > 0.5, rec.summary  # predpoklad testu
        st = {x["name"]: x for x in rec.path_proposal["stations"]}
        depart, arrival = rec.chosen["depart"], rec.chosen["arrival_p50"]
    leave = datetime.fromisoformat(st["Ľubochňa"]["dep"])
    reach = datetime.fromisoformat(st["Kraľovany"]["arr"])
    assert st["Ľubochňa"].get("hold") is True
    assert leave >= end + timedelta(minutes=30), (fmt(leave), fmt(end))  # koniec výluky + rezerva po výluke
    assert reach > leave
    # do Ľubochne (asi 40 km) podľa jazdnej doby, nie rozložené cez čakanie
    assert datetime.fromisoformat(st["Ľubochňa"]["arr"]) - datetime.fromisoformat(depart) < timedelta(hours=1)
    names = [x["name"] for x in rec.path_proposal["stations"]]
    after = rec.path_proposal["stations"][names.index("Ľubochňa") + 1:]
    assert after and all(datetime.fromisoformat(x["arr"]) >= leave for x in after), after  # nikto vo výluke
    assert datetime.fromisoformat(rec.path_proposal["stations"][-1]["arr"]) == datetime.fromisoformat(arrival)


def test_r5u4_email_navrhu_trasy_uvadza_cakanie_pred_vylukou(cfg, fixed_now):
    """[R5U4] E-mail s návrhom trasy idúceho vlaku pred dlhou výlukou: dopravňa pred výlukou má príchod aj odchod po
    výluke a poznámku „čakať pred výlukou“."""
    from vlaky.notifikacie.texty import recommendation_body

    tid, end = _running_before_long_closure(fixed_now)
    with session_scope() as s:
        rec = planovac.evaluate_train(s, tid, fixed_now, "nová výluka")
        proposal = recommendation_body(rec).split("NÁVRH TRASY")[1].split("\n\n")[0]
    row = next(line for line in proposal.splitlines() if "Ľubochňa" in line)
    assert row.endswith("– čakať pred výlukou"), proposal


def test_r5u5_porovnanie_variantov_iduceho_vlaku_v_emaile(cfg, fixed_now):
    """[R5U5] E-mail o idúcom vlaku: pokračovanie bez čakania je „POKRAČOVAŤ“ „ihneď“ (nie VYPRAVIŤ s časom prepočtu)
    a riadok odchodu znie „Pokračovať v jazde: ihneď“ (nie „Odchod: <čas prepočtu>“)."""
    from vlaky.notifikacie.texty import recommendation_body

    t0 = fixed_now
    route = network().route("Poprad-Tatry", "Žilina", TrainSpec())
    tid = _running_train("95905", "Poprad-Tatry", "Žilina", t0 - timedelta(minutes=40), t0 + timedelta(hours=8))
    _position(tid, *_point(route, 40.0), t0 - timedelta(minutes=1))
    with session_scope() as s:
        rec = planovac.evaluate_train(s, tid, t0, "nová poloha vlaku")
        assert rec.decision == "POKRAČOVAŤ" and rec.variants, rec.summary  # predpoklad testu
        body = recommendation_body(rec)
    now_txt = fmt(t0)
    variants = body.split("Porovnanie variantov:")[1].split("\n\n")[0]
    assert "VYPRAVIŤ" not in variants and "POKRAČOVAŤ" in variants and "ihneď" in variants, variants
    assert now_txt not in variants, variants  # čas prepočtu nie je odchod idúceho vlaku
    assert f"Odchod: {now_txt}" not in body and "Pokračovať v jazde: ihneď" in body, body


def test_r5u5_cakanie_pred_zatvorenym_cielom_bez_hranice_vypoctu(cfg, fixed_now):
    """[R5U5] Vlak čaká pred cieľom zatvoreným dlhšie, ako siaha prehľadávané obdobie (``until_entry``): ani riadok
    zvolenej varianty v porovnaní variantov neuvádza hranicu výpočtu ako odchod, ale „po uvoľnení vjazdu“."""
    from vlaky.notifikacie.texty import recommendation_body

    set_fake_now(fixed_now)
    with session_scope() as s:
        t = Train(number="95911", origin="Púchov", destination="Žilina", status="v_jazde", traction="E")
        s.add(t)
        s.flush()
        boundary = (fixed_now + timedelta(hours=23, minutes=50)).isoformat()
        ch = {"route": "Púchov – Žilina", "depart": boundary, "kind": "POČKAŤ", "status": "ČERVENÁ",
              "arrival_p50": (fixed_now + timedelta(hours=40)).isoformat(),
              "arrival_p90": (fixed_now + timedelta(hours=40, minutes=10)).isoformat(),
              "p_on_time": 0.0, "p_hold": 0.0, "expected_cost": 5000, "floating": True, "compare_arrival": True,
              "until_entry": True, "running": True}
        rec = Recommendation(train_id=t.id, decision="POČKAŤ", color="ČERVENÁ", priority="KRITICKÉ",
                             summary="Vlak 95911: POČKAŤ pred cieľom (FIKTÍVNE).", chosen=ch,
                             variants=[dict(ch), {**ch, "depart": fixed_now.isoformat(), "kind": "VYPRAVIŤ"}],
                             state="nove")
        s.add(rec)
        s.flush()
        body = recommendation_body(rec)
    variants = body.split("Porovnanie variantov:")[1].split("\n\n")[0]
    assert fmt(datetime.fromisoformat(boundary)) not in body
    assert "po uvoľnení vjazdu" in variants and "VYPRAVIŤ" not in variants, variants
    assert "Odchod: po uvoľnení vjazdu do cieľa" in body


def test_r5u6_denna_sprava_s_datumom_po_slovensky(cfg, fixed_now, monkeypatch):
    """[R5U6] Denná správa uvádza dátum po slovensky (02.11.2026), nie v tvare ISO (2026-11-02)."""
    from vlaky import config as config_mod
    from vlaky.agent import beh

    config_mod.override_settings(**{**cfg.model_dump(), "daily_report_hour": 5, "shift_change_hours": "3"})
    beh.run_cycle(fixed_now)
    with session_scope() as s:
        bodies = [n.body for n in s.scalars(select(Notification).where(Notification.kind == "zdravie"))]
    assert bodies and all(b.startswith("Denná správa Agenta dispečera (02.11.2026)") for b in bodies), bodies
    assert not any("2026-11-02" in b for b in bodies)
