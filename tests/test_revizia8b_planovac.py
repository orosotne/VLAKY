"""Plánovač: nezávislá kontrola 8. kola, doplnok (minulé okná, rezervy, nadväzujúce okná výluk, texty).

F1 – úseky simulácie závisia len od okien, ktoré môžu ovplyvniť niektorý odchod: okno, ktoré aj s rezervou po výluke
skončí pred pripravenosťou vlaku (idúci vlak: pred teraz), simulácia nevidí (ostáva v poistkách, poznámkach a textoch).
Inak by minulé prekrytie výluk oboch koľají rozdelilo trasu na iné úseky a rozhodnutie by sa zmenilo podľa toho, ako
ďaleko do minulosti sa okná rozbalili.
F2/F3 – pohľad späť a podrobné okná pred pripravenosťou sa riadia rezervou po výluke z nastavení (nie pevné 2 h a 48 h);
formulár nastavení prijme rezervu pred výlukou a po nej najviac 24 h.
T0 – prekrytie výluky do odvolania s výlukou so skutočným koncom má tento skutočný koniec; texty menujú obe výluky.
T1 – plán, ktorý by po poslednom okne výluk, ktoré simulácia videla, vošiel do ďalšieho okna (nadväzujúce okná za
koncom prehľadávaného obdobia), nemá časy, návrh trasy ani koncept objednávky trasy – termín nie je známy.
T2 – odovzdávka služby a fronta uvádzajú výluku do odvolania „od … do odvolania (technický koniec …)“.
T3 – očakávaný náklad z vymysleného príchodu sa neuvádza („nie je známy“).
T4 – e-mail o objednávke s neznámym termínom neradí „potvrdiť termín a objednať trasu“.

Všetky vlaky, výluky a osoby sú fiktívne.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from vlaky.cas import set_fake_now
from vlaky.hranice import seed_borders
from vlaky.models import (Closure, Driver, DriverShift, Email, ExtractedEvent, Order, ParameterSet,
                          Path as TrainPath, PositionReport, Train)

TZ = ZoneInfo("Europe/Bratislava")
UTC = timezone.utc
A, B = "Ružomberok", "Liptovský Mikuláš"
NOW = datetime(2026, 11, 21, 9, tzinfo=TZ).astimezone(UTC)


def loc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=TZ).astimezone(UTC)


def _l(dt) -> str:
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
    return dt.astimezone(TZ).strftime("%d.%m. %H:%M")


@pytest.fixture()
def env(cfg, session):
    seed_borders(session)
    session.commit()
    return cfg


def _closure(session, kind: str = "uplna", track: str = "", daily: tuple = ("", ""), start=None, end=None,
             until_revoked: bool = True, ref: str = "8100/2026", a: str = A, b: str = B, status: str = "overene",
             source: str = "rucne", source_event_id=None) -> Closure:
    c = Closure(kind=kind, from_station=a, to_station=b, line_ref="", track=track,
                start_utc=start or loc(2026, 11, 23, 8), end_utc=end or loc(2026, 12, 23, 8),
                daily_from=daily[0], daily_to=daily[1], electric_only=False, speed_kmh=None, external_ref=ref,
                until_revoked=until_revoked, status=status, source=source, source_event_id=source_event_id,
                note="FIKTÍVNA")
    session.add(c)
    session.commit()
    return c


def _params(session, **values) -> None:
    session.add(ParameterSet(valid_from=NOW - timedelta(days=1), created_by="veduci", values=values,
                             note="FIKTÍVNE"))
    session.commit()


_N = [88500]


def _train(session, ready: datetime, now: datetime = NOW, origin: str = A, destination: str = B,
           deadline_h: float = 8, path: bool = True) -> Train:
    from vlaky.agent.zdravie import set_kv

    _N[0] += 1
    set_fake_now(now)
    t = Train(number=str(_N[0]), origin=origin, destination=destination, traction="E", gauge="1435", vmax_kmh=80,
              status="planovany", ready_utc=ready, deadline_utc=ready + timedelta(hours=deadline_h))
    session.add(t)
    session.flush()
    if path:
        session.add(TrainPath(train_id=t.id, train_number=t.number, kind="ad_hoc", status="pridelena",
                              departure_utc=ready))
    session.commit()
    set_kv(session, "mail_last_ok", {"ts": (now - timedelta(minutes=2)).isoformat(), "fetched": 0})
    session.commit()
    return t


def _decide(session, ready: datetime, now: datetime = NOW, **kw):
    from vlaky.planovanie.planovac import evaluate_train

    t = _train(session, ready, now, **kw)
    rec = evaluate_train(session, t.id, now, "kontrola 8b")
    session.commit()
    return rec


def _spy_segments(monkeypatch) -> list:
    """Úseky simulácie (názvy) z prvého volania build_segments v každom prepočte (hlavná trasa)."""
    from vlaky.planovanie import planovac

    seen: list = []
    real = planovac.build_segments

    def spy(net, groute, windows, *args, **kwargs):
        out = real(net, groute, windows, *args, **kwargs)
        seen.append([s.name for s in out[0]])
        return out

    monkeypatch.setattr(planovac, "build_segments", spy)
    return seen


def _texts(rec) -> list:
    return [rec.summary or ""] + [str(r) for r in rec.reasons or []]


def _essence(rec) -> tuple:
    ch = rec.chosen or {}
    return rec.decision, rec.color, ch.get("depart"), ch.get("route"), ch.get("arrival_p50"), \
        sorted((v.get("route"), v.get("depart"), v.get("kind"), v.get("status")) for v in rec.variants or [])


# ------------------------------------------------------------------ F1 – úseky simulácie a minulé okná
def test_f1_rozbalenie_okien_do_minulosti_nemeni_useky_ani_rozhodnutie(env, session, monkeypatch):
    """[F1] Vlak Kraľovany → Liptovský Hrádok pripravený 03.12. 10:00; výluka 1. TK Ružomberok – Liptovský Mikuláš
    denne 08:00 – 14:00 (21. – 30.11.) a 2. TK raz 23.11. 09:00 – 12:00 – prekrytie (úplná výluka) 10 dní pred jazdou.
    Plánovač rozbaľuje okná pred pripravenosťou len obmedzene (_plan_windows); keď sa okná rozbalia celé (ako pred
    obmedzením), úseky simulácie aj rozhodnutie musia byť rovnaké – minulé prekrytie vlak nijako neobmedzí."""
    from vlaky.planovanie import planovac

    _closure(session, kind="kolaj", track="1", daily=("08:00", "14:00"), start=loc(2026, 11, 21, 8),
             end=loc(2026, 11, 30, 14), until_revoked=False, ref="8101/2026")
    _closure(session, kind="kolaj", track="2", start=loc(2026, 11, 23, 9), end=loc(2026, 11, 23, 12),
             until_revoked=False, ref="8102/2026")
    seen = _spy_segments(monkeypatch)
    ready = loc(2026, 12, 3, 10)
    bounded = _decide(session, ready, origin="Kraľovany", destination="Liptovský Hrádok", deadline_h=107 / 60)
    segs_bounded = seen[0]

    def full(session_, now, ready_, end, p=None):  # všetky okná od min(teraz, pripravenosť) − 2 h (bez obmedzenia)
        return planovac.closure_windows(session_, min(now, ready_) - timedelta(hours=2), end)

    monkeypatch.setattr(planovac, "_plan_windows", full)
    seen.clear()
    whole = _decide(session, ready, origin="Kraľovany", destination="Liptovský Hrádok", deadline_h=107 / 60)
    assert any("23.11. 09:00 – 23.11. 12:00 sú vylúčené obe traťové koľaje" in r for r in whole.reasons) or \
        any(w for w in planovac.closure_windows(session, loc(2026, 11, 23, 0), loc(2026, 11, 24, 0))
            if w.kind == "uplna"), "prekrytie výluk oboch koľají 23.11. musí existovať"
    assert seen[0] == segs_bounded, (seen[0], segs_bounded)
    assert _essence(whole) == _essence(bounded), (whole.summary, bounded.summary)


@pytest.mark.parametrize("running", [False, True], ids=["planovany", "iduci"])
def test_f1_prekrytie_ktore_skoncilo_pred_pripravenostou_nemeni_useky(env, session, monkeypatch, running):
    """[F1] Prekrytie výluk oboch koľají Ružomberok – Liptovský Mikuláš, ktoré skončilo 1 h pred pripravenosťou vlaku
    (idúci vlak: pred 1 h; rezerva po výluke 30 min): žiadny odchod nezdrží. Vlak s ním a vlak bez neho (bez výluky
    2. TK) majú rovnaké úseky simulácie a rovnaké rozhodnutie; prekrytie ostáva v dôvodoch (poznámka k výluke)."""
    from vlaky.planovanie.planovac import evaluate_train
    from vlaky.siet.graf import TrainSpec, network

    ready = NOW + (timedelta(0) if running else timedelta(hours=3))
    _closure(session, kind="kolaj", track="1", start=ready - timedelta(hours=5), end=ready - timedelta(hours=1),
             until_revoked=False, ref="8111/2026")
    tk2 = _closure(session, kind="kolaj", track="2", start=ready - timedelta(hours=4), end=ready - timedelta(hours=1),
                   until_revoked=False, ref="8112/2026")
    seen = _spy_segments(monkeypatch)

    def run():
        if not running:
            return _decide(session, ready, origin="Kraľovany", destination="Liptovský Hrádok")
        _N[0] += 1
        t = Train(number=str(_N[0]), origin="Kraľovany", destination="Liptovský Hrádok", traction="E", gauge="1435",
                  vmax_kmh=100, status="v_jazde", planned_departure_utc=NOW - timedelta(minutes=5),
                  departed_utc=NOW - timedelta(minutes=5), deadline_utc=NOW + timedelta(hours=6))
        session.add(t)
        session.flush()
        d = Driver(name=f"R. {t.number} FIKTÍVNY")
        session.add(d)
        session.flush()
        session.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=NOW - timedelta(hours=1), status="prebieha"))
        route = network().route("Kraľovany", "Liptovský Hrádok", TrainSpec())
        lon, lat = network().geometry(route)[1]
        session.add(PositionReport(train_id=t.id, train_number=t.number, ts_utc=NOW - timedelta(minutes=1), lat=lat,
                                   lon=lon, source="gps"))
        session.commit()
        from vlaky.agent.zdravie import set_kv

        set_fake_now(NOW)
        set_kv(session, "mail_last_ok", {"ts": (NOW - timedelta(minutes=2)).isoformat(), "fetched": 0})
        session.commit()
        rec = evaluate_train(session, t.id, NOW, "kontrola 8b")
        session.commit()
        return rec

    with_overlap = run()
    segs_with = seen[0]
    assert any("sú vylúčené obe traťové koľaje" in r for r in with_overlap.reasons), with_overlap.reasons
    session.delete(tk2)
    session.commit()
    seen.clear()
    without = run()
    assert seen[0] == segs_with, (segs_with, seen[0])
    assert _essence(with_overlap) == _essence(without), (with_overlap.summary, without.summary)


# ------------------------------------------------------------------ F2/F3 – rezervy po výluke a obdobie výluk
@pytest.mark.parametrize("ended", [150, 170], ids=["pred-150-min", "pred-170-min"])
def test_f3_vyluka_v_rezerve_po_vyluke_dlhsej_ako_2_h(env, session, ended):
    """[F3] Rezerva po výluke 180 min (nastavenia vedúceho), úplná výluka Ružomberok – Liptovský Mikuláš skončila pred
    150 (170) min: vlak pripravený teraz smie vojsť do úseku najskôr koniec + 180 min – nie VYPRAVIŤ teraz (ZELENÁ)."""
    _params(session, buffer_after_closure_min=180.0)
    end = NOW - timedelta(minutes=ended)
    _closure(session, start=end - timedelta(hours=6), end=end, until_revoked=False, ref="8201/2026")
    rec = _decide(session, NOW)
    assert not (rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ"), rec.summary
    dep = datetime.fromisoformat(rec.chosen["depart"]) if (rec.chosen or {}).get("depart") else None
    assert dep is None or dep >= end + timedelta(minutes=180), rec.summary
    assert any("na trase vlaku" in r and _l(end) in r for r in rec.reasons), rec.reasons


def test_f2_vlak_na_neskor_s_rezervou_dlhsou_ako_48_h(env, session):
    """[F2] Uložená sada parametrov (zo staršej verzie formulára) s rezervou po výluke 4 200 min (70 h); denná výluka
    Ružomberok – Liptovský Mikuláš 21. – 25.11. 08:00 – 14:00, vlak pripravený 28.11. 10:00: posledné okno + 70 h =
    28.11. 12:00 – vlak nesmie vyraziť 28.11. 10:00 (okná pred pripravenosťou sa rozbalia podľa rezervy, nie len 48 h)."""
    now = loc(2026, 11, 21, 7)
    _params(session, buffer_after_closure_min=4200.0)
    _closure(session, daily=("08:00", "14:00"), start=loc(2026, 11, 21, 8), end=loc(2026, 11, 25, 14),
             until_revoked=False, ref="8202/2026")
    rec = _decide(session, loc(2026, 11, 28, 10), now=now, deadline_h=12)
    assert not (rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ"), rec.summary
    assert rec.chosen and datetime.fromisoformat(rec.chosen["depart"]) >= loc(2026, 11, 28, 12), rec.summary


def test_f3_neoverena_vyluka_v_rezerve_po_vyluke_je_tvrda_poistka(env, session):
    """[F3] Rezerva po výluke 240 min; neoverená výluka z e-mailu Kraľovany – Ružomberok skončila 3 h pred
    pripravenosťou vlaku Kraľovany → Liptovský Hrádok: patrí do obdobia možnej jazdy (pohľad späť podľa rezervy) – je
    medzi výlukami na trase a vlak nemôže byť ZELENÝ; vlak do úseku nevojde pred koncom + 240 min."""
    _params(session, buffer_after_closure_min=240.0)
    m = Email(message_id="<r8b-neoverena@zsr.example>", sender="vyluky@zsr.example", subject="Výluka FIKTÍVNA")
    session.add(m)
    session.flush()
    ev = ExtractedEvent(email_id=m.id, kind="vyluka_nova", confidence=0.9, status="aplikovane",
                        needs_confirmation=False, data={"from_station": "Kraľovany", "to_station": A})
    session.add(ev)
    session.flush()
    ready = NOW + timedelta(hours=2)
    end = ready - timedelta(hours=3)
    _closure(session, a="Kraľovany", b=A, start=end - timedelta(hours=4), end=end, until_revoked=False,
             ref="8203/2026", status="navrh", source="email", source_event_id=ev.id)
    rec = _decide(session, ready, origin="Kraľovany", destination="Liptovský Hrádok")
    assert rec.color != "ZELENÁ", rec.summary
    assert any("z e-mailu" in r and "ešte nie je overená" in r and "plán vlaku sa jej podľa prečítaného termínu "
               "netýka" not in r for r in rec.reasons), rec.reasons
    dep = datetime.fromisoformat(rec.chosen["depart"]) if (rec.chosen or {}).get("depart") else None
    assert dep is None or dep >= end + timedelta(minutes=240) - timedelta(minutes=40), rec.summary


def test_f2_formular_nastaveni_obmedzi_rezervu_na_24_h(env, session):
    """[F2] Formulár nastavení neprijme rezervu pred výlukou ani po výluke dlhšiu ako 24 h (1 440 min) – zrozumiteľná
    chyba po slovensky, sada parametrov sa neuloží; 1 440 min prijme."""
    from fastapi.testclient import TestClient

    from test_web_spolocne import csrf, login_as
    from vlaky.web.app import create_app

    with TestClient(create_app(auto_evaluate=False)) as client:
        login_as(client, "veduci", "veduci")
        values = dict(re.findall(r'<input id="par-[^"]+" name="([a-z_0-9]+)" type="number" value="([^"]*)"',
                                 client.get("/nastavenia").text))
        assert values.get("buffer_after_closure_min"), values
        for key in ("buffer_after_closure_min", "buffer_before_closure_min"):
            r = client.post("/nastavenia", data={**values, key: "4200", "note": "preklep (test)",
                                                 "csrf_token": csrf(client, "/nastavenia")})
            assert r.status_code == 422, r.text[:300]
            assert "najviac 1 440 min (24 h)" in r.text and "rezerva sa zadáva v minútach" in r.text, r.text[:3000]
        assert not list(session.scalars(select(ParameterSet)))
        ok = client.post("/nastavenia", follow_redirects=False,
                         data={**values, "buffer_after_closure_min": "1440", "note": "dlhá rezerva (test)",
                               "csrf_token": csrf(client, "/nastavenia")})
        assert ok.status_code == 303, ok.text[:500]
    session.expire_all()
    ps = list(session.scalars(select(ParameterSet)))
    assert len(ps) == 1 and ps[0].values["buffer_after_closure_min"] == 1440.0


# ------------------------------------------------------------------ T0 – prekrytie s výlukou do odvolania
def test_t0_prekrytie_s_dennou_vylukou_ma_skutocny_koniec(env, session):
    """[T0] 1. TK celé obdobie do odvolania, 2. TK denne 12:00 – 20:00 do odvolania; koniec prehľadávaného obdobia
    22.01. 16:00 padne do denného okna: úplná výluka z výluk oboch koľají v ten deň trvá do 20:00 (2. TK je potom
    voľná), nie „od 22.01. 12:00 do odvolania“."""
    from vlaky.planovanie.planovac import closure_windows

    _closure(session, kind="kolaj", track="1", ref="8301/2026")
    _closure(session, kind="kolaj", track="2", daily=("12:00", "20:00"), ref="8302/2026")
    both = [w for w in closure_windows(session, loc(2027, 1, 20, 0), loc(2027, 1, 22, 16)) if w.kind == "uplna"]
    last = max(both, key=lambda w: w.start)
    assert (_l(last.start), _l(last.end), last.open_end) == ("22.01. 12:00", "22.01. 20:00", False), \
        [(_l(w.start), _l(w.end), w.open_end) for w in both]
    assert "v čase 22.01. 12:00 – 22.01. 20:00 sú vylúčené obe traťové koľaje" in last.note, last.note
    assert not any(w.open_end for w in both)
    rec = _decide(session, loc(2027, 1, 20, 10))
    assert not any("od 22.01. 12:00 do odvolania" in t for t in _texts(rec)), _texts(rec)


def test_t0_obe_kolaje_do_odvolania_menuju_obe_vyluky(env, session):
    """[T0] Výluka 1. TK aj 2. TK celé obdobie do odvolania: termín jazdy nie je známy a dôvod menuje obe výluky (aj
    obe čísla rozkazov), nie len prvú."""
    _closure(session, kind="kolaj", track="1", ref="8311/2026")
    _closure(session, kind="kolaj", track="2", ref="8312/2026")
    rec = _decide(session, loc(2026, 11, 25, 10))
    assert rec.decision == "ESKALOVAŤ" and "termín jazdy nie je známy" in rec.summary, rec.summary
    assert any("platí od 23.11. 08:00 do odvolania (výluky #" in r and "č. 8311/2026 a 8312/2026)" in r
               for r in rec.reasons), rec.reasons


def test_t0_vyluka_do_odvolania_s_dennou_0_24_druhej_kolaje(env, session):
    """[T0/T1] 1. TK celé obdobie do odvolania a 2. TK denne 00:00 – 24:00 do odvolania (od 23.11. 08:00), vlak
    20.01.2027: termín nie je známy, texty uvádzajú začiatok výluk (23.11. 08:00) a obe výluky – nie „od 22.01. 00:00
    do odvolania“ (začiatok posledného denného prekrytia v prehľadávanom období)."""
    c1 = _closure(session, kind="kolaj", track="1", ref="8321/2026")
    c2 = _closure(session, kind="kolaj", track="2", daily=("00:00", "24:00"), ref="8322/2026")
    rec = _decide(session, loc(2027, 1, 20, 10))
    assert rec.decision == "ESKALOVAŤ" and "termín jazdy nie je známy" in rec.summary, rec.summary
    assert not any("od 22.01. 00:00 do odvolania" in t for t in _texts(rec)), _texts(rec)
    assert f"výluky #{c1.id} a #{c2.id}" in rec.summary and "od 23.11. 08:00 do odvolania" in rec.summary, rec.summary
    assert not rec.chosen and rec.path_proposal == {}, (rec.chosen, rec.path_proposal)


# ------------------------------------------------------------------ T1 – nadväzujúce okná za koncom obdobia
CHAINS = {
    "denne-0-24": [dict(daily=("00:00", "24:00"), start=loc(2026, 11, 1, 0), ref="8401/2026")],
    "denne-08-0750": [dict(daily=("08:00", "07:50"), start=loc(2026, 11, 1, 0), ref="8402/2026")],
    "dve-striedave": [dict(daily=("06:00", "18:00"), start=loc(2026, 11, 1, 0), ref="8403/2026"),
                      dict(daily=("18:00", "06:00"), start=loc(2026, 11, 1, 0), ref="8404/2026")],
    "pevna-denna-0-24": [dict(daily=("00:00", "24:00"), start=loc(2026, 11, 1, 0), end=loc(2027, 3, 1, 0),
                              until_revoked=False, ref="8405/2026")],
}


@pytest.mark.parametrize("shape", list(CHAINS))
def test_t1_nadvazujuce_okna_bez_casov_navrhu_a_konceptu(env, session, shape):
    """[T1] Okná výluk bez času na prejazd medzi nimi (denne 00:00 – 24:00, 08:00 – 07:50, dve striedavé výluky
    06 – 18 a 18 – 06, pevná denná výluka do 1.3.2027) pokračujú aj za koncom prehľadávaného obdobia: najmenej zlá
    možnosť nemá čas odchodu ani príchodu, odporúčanie nemá návrh trasy ani koncept objednávky trasy v IS KIS (odchod
    po poslednom okne, ktoré simulácia videla, by išiel rovno do ďalšieho); varianty nemajú príchod ani náklad."""
    from vlaky.notifikacie.texty import recommendation_body

    rows = [_closure(session, **kw) for kw in CHAINS[shape]]
    rec = _decide(session, loc(2026, 11, 25, 10))
    assert (rec.decision, rec.color) == ("ESKALOVAŤ", "ČERVENÁ"), rec.summary
    assert "termín jazdy nie je známy" in rec.summary and "nenecháva" in rec.summary, rec.summary
    assert "s odchodom" not in rec.summary and "príchod" not in rec.summary.split("Najmenej zlá")[-1], rec.summary
    assert all(f"#{c.id}" in rec.summary for c in rows), rec.summary
    assert not rec.chosen and rec.path_proposal == {}, (rec.chosen, rec.path_proposal)
    assert rec.variants and all(v.get("arrival_p50") is None and v.get("expected_cost") is None
                                for v in rec.variants), rec.variants
    assert not (rec.crew or {}).get("end_p50"), rec.crew
    body = recommendation_body(rec)
    assert "NÁVRH TRASY" not in body and "objednávka ad hoc trasy" not in body and "Odchod:" not in body, body
    assert "28.11. 00:50" not in body and "27.11. 18:50" not in body


def test_t1_objednavka_s_nadvazujucimi_oknami(env, session):
    """[T1/T4] Objednávka cez úsek s dennou výlukou 00:00 – 24:00 do odvolania: žiadny návrh trasy na objednanie,
    termín prepravy nie je známy a e-mail o objednávke neradí potvrdiť termín zákazníkovi a objednať trasu – ďalší krok
    je dohodnúť so zákazníkom, že termín zatiaľ nie je známy. Objednávka bez výluky ostáva pri doterajšom kroku."""
    from vlaky.notifikacie.texty import order_proposal_text
    from vlaky.planovanie.planovac import plan_order

    c = _closure(session, daily=("00:00", "24:00"), start=loc(2026, 11, 1, 0), ref="8411/2026")
    set_fake_now(NOW)
    o = Order(customer="FIKTÍVNY zákazník", origin=A, destination=B, earliest_departure_utc=loc(2026, 11, 25, 10),
              deadline_utc=loc(2026, 11, 26, 10), status="nova")
    free = Order(customer="FIKTÍVNY zákazník", origin="Žilina", destination="Vrútky",
                 earliest_departure_utc=loc(2026, 11, 25, 10), deadline_utc=loc(2026, 11, 26, 10), status="nova")
    session.add_all([o, free])
    session.commit()
    plan = plan_order(session, o.id, NOW)
    assert plan["decision"] == "ESKALOVAŤ" and not plan.get("path_proposal"), plan
    assert plan.get("date_unknown") and f"#{c.id}" in plan["date_unknown"], plan
    assert any("nie je známy" in e and "návrh trasy sa preto nepripravuje" in e for e in plan["explanation"])
    assert all(v.get("arrival_p50") is None and v.get("expected_cost") is None for v in plan["variants"])
    _s, body = order_proposal_text(o, plan)
    assert "NÁVRH TRASY" not in body and "potvrdí termín zákazníkovi a objedná trasu" not in body, body
    assert "dohodne so zákazníkom, že termín zatiaľ nie je známy" in body, body
    plan_free = plan_order(session, free.id, NOW)
    assert (plan_free.get("path_proposal") or {}).get("stations"), plan_free
    _s, body_free = order_proposal_text(free, plan_free)
    assert "potvrdí termín zákazníkovi a objedná trasu v IS KIS" in body_free, body_free



def test_t1_po_dlhom_cakani_dalsie_okno_za_koncom_obdobia(env, session):
    """[T1] Úplná výluka Ružomberok – Liptovský Mikuláš 24.11. – 04.12. 08:00 (vlak Kraľovany → Liptovský Hrádok
    pripravený 25.11. 10:00 by čakal do jej konca) a krátka výluka Liptovský Mikuláš – Liptovský Hrádok 04.12. 08:45 –
    10:00, ktorá začína až za koncom prehľadávaného obdobia: plán po čakaní by išiel rovno do nej – časy, návrh trasy ani
    koncept objednávky trasy sa neuvádzajú a dôvod menuje výluku, do ktorej by vlak vošiel (nie „nenecháva medzi
    oknami čas“ – nejde o nadväzujúce okná)."""
    _closure(session, start=loc(2026, 11, 24, 8), end=loc(2026, 12, 4, 8), until_revoked=False, ref="8431/2026")
    late = _closure(session, a=B, b="Liptovský Hrádok", start=loc(2026, 12, 4, 8, 45), end=loc(2026, 12, 4, 10),
                    until_revoked=False, ref="8432/2026")
    rec = _decide(session, loc(2026, 11, 25, 10), origin="Kraľovany", destination="Liptovský Hrádok")
    assert (rec.decision, rec.color) == ("ESKALOVAŤ", "ČERVENÁ"), rec.summary
    assert "termín jazdy nie je známy" in rec.summary and "s odchodom" not in rec.summary, rec.summary
    assert f"do ďalšieho okna výluky – výluka #{late.id} {B} – Liptovský Hrádok od 04.12. 08:45" in rec.summary, \
        rec.summary
    assert "nenecháva" not in rec.summary, rec.summary
    assert not rec.chosen and rec.path_proposal == {}, (rec.chosen, rec.path_proposal)
    assert any("ktoré agent pri výpočte nevidel" in r and "č. 8432/2026" in r for r in rec.reasons), rec.reasons

def _near_destination_train(session, now: datetime) -> Train:
    """Idúci vlak Púchov → Trenčín 800 m pred koncom trasy s rušňovodičom v službe (ako v test_revizia8_planovac)."""
    from vlaky.siet.graf import TrainSpec, haversine, network

    net = network()
    r = net.route("Púchov", "Trenčín", TrainSpec())
    km, cum, point = r.length_m / 1000.0 - 0.8, 0.0, None
    for i, ei in enumerate(r.edges):
        e = net.edges[ei]
        g = e.geom if e.u == r.nodes[i] else list(reversed(e.geom))
        for (la1, lo1), (la2, lo2) in zip(g, g[1:]):
            d = haversine(la1, lo1, la2, lo2) / 1000.0
            if point is None and cum + d >= km:
                f = (km - cum) / d if d else 0.0
                point = (la1 + f * (la2 - la1), lo1 + f * (lo2 - lo1))
            cum += d
    _N[0] += 1
    t = Train(number=str(_N[0]), origin="Púchov", destination="Trenčín", traction="E", gauge="1435", vmax_kmh=100,
              status="v_jazde", planned_departure_utc=now - timedelta(minutes=30),
              departed_utc=now - timedelta(minutes=30), deadline_utc=now + timedelta(hours=8))
    session.add(t)
    session.flush()
    d = Driver(name=f"R. {t.number} FIKTÍVNY")
    session.add(d)
    session.flush()
    session.add(DriverShift(driver_id=d.id, train_id=t.id, status="prebieha", start_utc=now - timedelta(hours=2)))
    session.add(PositionReport(train_id=t.id, train_number=t.number, ts_utc=now, lat=point[0], lon=point[1],
                               source="gps"))
    session.commit()
    return t


def test_t1_vlak_pred_cielom_s_nadvazujucimi_oknami_caka_bez_casu_prichodu(env, session):
    """[T1/T3] Idúci vlak 800 m pred cieľom (Trenčín), v cieľovej stanici výluka denne 00:00 – 24:00 (30 dní): POČKAŤ
    pred cieľom, kým sa neuvoľní vjazd – príchod ani očakávaný náklad nie sú známe (nie „podľa výpočtu“ čas po
    poslednom okne, ktoré simulácia videla); koniec služby rušňovodiča tiež nie."""
    from vlaky.notifikacie.texty import recommendation_body
    from vlaky.planovanie.planovac import evaluate_train

    set_fake_now(NOW)
    c = _closure(session, a="Trenčín", b="Trenčín", daily=("00:00", "24:00"), start=NOW - timedelta(days=2),
                 end=NOW + timedelta(days=30), until_revoked=False, ref="8421/2026")
    t = _near_destination_train(session, NOW)
    rec = evaluate_train(session, t.id, NOW, "kontrola 8b")
    assert (rec.decision, rec.color) == ("POČKAŤ", "ČERVENÁ"), rec.summary
    assert "dokončiť jazdu po uvoľnení vjazdu" in rec.summary and "príchod nie je známy" in rec.summary, rec.summary
    assert f"#{c.id}" in rec.summary and "nenecháva medzi oknami čas na prejazd" in rec.summary, rec.summary
    assert not any("podľa výpočtu" in x for x in _texts(rec)), _texts(rec)
    ch = rec.chosen
    assert ch["until_entry"] and ch["arrival_p50"] is None and ch["expected_cost"] is None and ch["do_odvolania"]
    assert rec.path_proposal == {} and (rec.crew or {}).get("end_p50") is None
    body = recommendation_body(rec)
    assert "Očakávaný náklad: nie je známy" in body and "Príchod: nie je známy" in body, body


# ------------------------------------------------------------------ T2 – odovzdávka a fronta
def test_t2_odovzdavka_a_fronta_uvadzaju_vyluku_do_odvolania(env, session):
    """[T2] Odovzdávka služby (nové a zmenené výluky) a fronta („Zapísané automaticky“) uvádzajú výluku do odvolania
    „od … do odvolania (technický koniec …)“ – technický koniec nie je koniec výluky; výluka s pevným koncom ostáva
    „od – do“."""
    from fastapi.testclient import TestClient

    from test_web_spolocne import login_as
    from vlaky.agent.odovzdavka import build_report
    from vlaky.web.app import create_app

    m = Email(message_id="<r8b-auto@zsr.example>", sender="vyluky@zsr.example", subject="Výluka FIKTÍVNA",
              body_text="FIKTÍVNY e-mail")
    session.add(m)
    session.flush()
    ev = ExtractedEvent(email_id=m.id, kind="vyluka_nova", confidence=0.95, status="aplikovane",
                        needs_confirmation=False, data={"from_station": A, "to_station": B})
    session.add(ev)
    session.flush()
    _closure(session, ref="8501/2026", status="navrh", source="email", source_event_id=ev.id)
    _closure(session, a="Kraľovany", b=A, until_revoked=False, start=loc(2026, 11, 24, 8), end=loc(2026, 11, 24, 14),
             ref="8502/2026")
    content, _data = build_report(session, datetime.now(UTC) + timedelta(minutes=5))
    line = next(x for x in content.splitlines() if f"{A} – {B}" in x)
    assert "od 23.11. 08:00 do odvolania (technický koniec 23.12. 08:00)" in line, line
    assert "23.11. 08:00 – 23.12. 08:00" not in content
    assert any("Kraľovany – Ružomberok 24.11. 08:00 – 24.11. 14:00" in x for x in content.splitlines()), content
    session.commit()
    with TestClient(create_app(auto_evaluate=False)) as client:
        login_as(client, "dispecer")
        page = client.get("/fronta").text
    assert "Zapísané automaticky" in page, page[:2000]
    part = page[page.index('id="automaticky"'):]
    assert "od 23.11. 08:00 do odvolania" in part and "(technický koniec 23.12. 08:00)" in part, part[:2000]
    assert "23.11. 08:00 – 23.12. 08:00" not in part


# ------------------------------------------------------------------ T3 – náklad z vymysleného príchodu
def test_t3_naklad_pri_neznamom_prichode_nie_je_znamy(env, session):
    """[T3] Vlak pred cieľom zatvoreným mimoriadnosťou do odvolania (POČKAŤ bez času príchodu) a varianty eskalácie
    pri výluke do odvolania: očakávaný náklad (vypočítaný z vymysleného príchodu) sa neuvádza – e-mail aj web
    „nie je známy“."""
    from fastapi.testclient import TestClient

    from test_web_spolocne import login_as
    from vlaky.notifikacie.texty import recommendation_body
    from vlaky.planovanie.planovac import evaluate_train
    from vlaky.web.app import create_app

    set_fake_now(NOW)
    _closure(session, kind="mimoriadnost", a="Trenčín", b="Trenčín", start=NOW - timedelta(hours=1),
             end=NOW + timedelta(days=30), ref="")
    t = _near_destination_train(session, NOW)
    rec = evaluate_train(session, t.id, NOW, "kontrola 8b")
    session.commit()
    assert rec.decision == "POČKAŤ" and rec.chosen["arrival_p50"] is None, rec.summary
    assert rec.chosen["expected_cost"] is None, rec.chosen
    body = recommendation_body(rec)
    assert "Očakávaný náklad: nie je známy" in body and "vzorové sadzby" not in body.split("Očakávaný náklad")[1][:60]
    _closure(session, ref="8601/2026")
    esk = _decide(session, loc(2026, 11, 25, 10))
    assert esk.decision == "ESKALOVAŤ" and esk.variants, esk.summary
    assert all(v.get("expected_cost") is None for v in esk.variants if v.get("do_odvolania")), esk.variants
    assert any(v.get("do_odvolania") for v in esk.variants), esk.variants
    esk_body = recommendation_body(esk)
    rows = esk_body.split("Porovnanie variantov:")[1].split("Dôvody a upozornenia:")[0]
    assert "nie je známy" in rows and "€" not in rows, rows
    with TestClient(create_app(auto_evaluate=False)) as client:
        login_as(client, "dispecer")
        page = client.get(f"/odporucania/{rec.id}").text
        page_esk = client.get(f"/odporucania/{esk.id}").text
    i = page.index("Očakávaný náklad")
    assert "nie je známy" in page[i:i + 200], page[i:i + 200]
    table = page_esk[page_esk.index("Porovnanie variantov"):]
    table = table[:table.index("</table>")]
    assert "nie je známy" in table and "€" not in table, table
