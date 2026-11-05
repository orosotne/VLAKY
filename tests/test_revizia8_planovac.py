"""Plánovač: výluka do odvolania pri vlaku na neskôr a jej koniec v textoch (nezávislá kontrola, 8. kolo).

P1 – výluka do odvolania sa v plánovači počíta po koniec prehľadávaného obdobia. Pri vlaku pripravenom o N dní (napr.
preklep v roku vo formulári) dala denná výluka do odvolania N denných okien a všetky dvojice okien výluk koľají sa
porovnávali medzi sebou: prepočet jedného vlaku trval minúty a s ním celý cyklus agenta (pošta, ostatné vlaky).
Pred pripravenosťou vlaku stačí z každej výluky jej prvé okno, výluky oboch koľají sa porovnávajú len v rovnakom čase
a formulár vlaku neprijme čas viac ako rok dopredu.

P2 – koniec okna výluky do odvolania (koniec prehľadávaného obdobia či technický koniec) sa ukazoval ako koniec výluky
a najmenej zlá možnosť, návrh trasy aj koncept objednávky trasy v IS KIS odchádzali hneď po tomto vymyslenom konci –
dispečer by objednal trasu do platnej výluky. Texty uvádzajú „do odvolania“ a termín jazdy, kým ŽSR výluku neodvolá,
nie je známy. Denná výluka do odvolania má skutočné denné konce.

Všetky vlaky, výluky a osoby sú fiktívne.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from vlaky.cas import set_fake_now
from vlaky.hranice import seed_borders
from vlaky.models import Closure, Driver, DriverShift, Order, Path as TrainPath, PositionReport, Train

TZ = ZoneInfo("Europe/Bratislava")
UTC = timezone.utc
A, B = "Ružomberok", "Liptovský Mikuláš"


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
             until_revoked: bool = True, ref: str = "2331/2026", a: str = A, b: str = B, status: str = "overene",
             source: str = "email", source_event_id=None) -> Closure:
    c = Closure(kind=kind, from_station=a, to_station=b, line_ref="", track=track,
                start_utc=start or loc(2026, 11, 23, 8), end_utc=end or loc(2026, 12, 23, 8),
                daily_from=daily[0], daily_to=daily[1], electric_only=False, speed_kmh=None, external_ref=ref,
                until_revoked=until_revoked, status=status, source=source, source_event_id=source_event_id,
                note="FIKTÍVNA")
    session.add(c)
    session.commit()
    return c


_N = [88000]


def _decide(session, ready: datetime, now: datetime, path_at=None, origin: str = A, destination: str = B):
    """Plánovaný vlak s pripravenosťou ``ready`` a pridelenou trasou (predvolene v čase pripravenosti), vyhodnotený
    skutočným plánovačom v čase ``now``."""
    from vlaky.agent.zdravie import set_kv
    from vlaky.planovanie.planovac import evaluate_train

    _N[0] += 1
    set_fake_now(now)
    t = Train(number=str(_N[0]), origin=origin, destination=destination, traction="E", gauge="1435", vmax_kmh=80,
              status="planovany", ready_utc=ready, deadline_utc=ready + timedelta(hours=8))
    session.add(t)
    session.flush()
    session.add(TrainPath(train_id=t.id, train_number=t.number, kind="ad_hoc", status="pridelena",
                          departure_utc=path_at or ready))
    session.commit()
    set_kv(session, "mail_last_ok", {"ts": (now - timedelta(minutes=2)).isoformat(), "fetched": 0})
    session.commit()
    return evaluate_train(session, t.id, now, "výluka do odvolania")


def _texts(rec) -> list:
    return [rec.summary or ""] + [str(r) for r in rec.reasons or []]


# ------------------------------------------------------------------ P1 – výkon
def test_vlak_o_dva_roky_pocita_s_ohranicenym_poctom_okien(env, session, monkeypatch):
    """[P1] Dve denné výluky koľají do odvolania (1. TK 08 – 14, 2. TK 20 – 02 – striedanie pri dlhej stavbe) a vlak
    s preklepom v roku (pripravený o dva roky): plánovač nedostane stovky denných okien – pred pripravenosťou vlaku len
    prvé okno každej výluky. Okná v čase jazdy vlaku ostávajú (výluka do odvolania platí aj o dva roky)."""
    from vlaky.planovanie import planovac

    for i, (track, frm, to) in enumerate((("1", "08:00", "14:00"), ("2", "20:00", "02:00"))):
        _closure(session, kind="kolaj", track=track, daily=(frm, to), start=loc(2026, 11, 1, 8),
                 end=loc(2026, 12, 21, 8), ref=f"{4000 + i}/2026")
    seen: list = []
    real = planovac.build_segments

    def spy(net, groute, windows, *args, **kwargs):
        seen.append(len(windows))
        # pri neohraničenom rozbalení by tu bolo ~1 500 okien a prepočet by trval minúty – zastaviť hneď
        assert len(windows) <= 40, f"{len(windows)} okien výluk pre jeden vlak – rozbalenie nie je ohraničené"
        return real(net, groute, windows, *args, **kwargs)

    monkeypatch.setattr(planovac, "build_segments", spy)
    rec = _decide(session, loc(2028, 11, 20, 10), loc(2026, 11, 21, 9))
    assert seen and max(seen) <= 40, seen
    assert any("Výluka koľaje Ružomberok – Liptovský Mikuláš 20.11. 08:00 – 20.11. 14:00 na trase vlaku" in r
               for r in rec.reasons), rec.reasons
    assert any("20.11. 20:00 – 21.11. 02:00 na trase vlaku" in r for r in rec.reasons), rec.reasons


def test_vyluky_oboch_kolaji_porovnava_len_okna_v_rovnakom_case(env, session, monkeypatch):
    """[P1] Dlhé denné výluky 1. a 2. traťovej koľaje (300 dní, prekrytie každý deň 12:00 – 13:00): úplná výluka
    z výluk oboch koľají vznikne pre každý deň, ale okná sa neporovnávajú všetky so všetkými (pri 600 oknách 180 000
    dvojíc s rozborom koľají) – rozbor označenia koľaje len raz na označenie."""
    from vlaky.planovanie import planovac

    _closure(session, kind="kolaj", track="1", daily=("08:00", "13:00"), start=loc(2026, 11, 1, 8),
             end=loc(2027, 8, 28, 8), until_revoked=False, ref="4100/2026")
    _closure(session, kind="kolaj", track="2", daily=("12:00", "18:00"), start=loc(2026, 11, 1, 8),
             end=loc(2027, 8, 28, 8), until_revoked=False, ref="4101/2026")
    calls: list = []
    real = planovac._track_numbers
    monkeypatch.setattr(planovac, "_track_numbers", lambda track: calls.append(track) or real(track))
    wins = planovac.closure_windows(session, loc(2026, 11, 1, 0), loc(2027, 8, 28, 0))
    days = (date(2027, 8, 27) - date(2026, 11, 1)).days + 1
    both = [w for w in wins if w.kind == "uplna"]
    assert len([w for w in wins if w.kind == "kolaj"]) == 2 * days
    assert len(both) == days, len(both)
    assert all(_l(w.start)[-5:] == "12:00" and _l(w.end)[-5:] == "13:00" and not w.open_end for w in both)
    assert all("sú vylúčené obe traťové koľaje" in w.note and w.status == "overene" for w in both)
    assert len(calls) <= 10, len(calls)


def test_vlak_na_neskor_si_drzi_poistku_neoverenej_vyluky_pred_pripravenostou(env, session, monkeypatch):
    """[P1] Vlak o rok a neoverená výluka z e-mailu, ktorá podľa prečítaného termínu skončí ešte zajtra (dávno pred
    pripravenosťou vlaku): termín mohol byť prečítaný nesprávne – vlak nie je ZELENÝ (mäkká poistka ostáva ako pri
    vlaku na zajtra), hoci sa okná výluk pred jeho pripravenosťou nerozbaľujú všetky (denná výluka do odvolania)."""
    from vlaky.models import Email, ExtractedEvent
    from vlaky.planovanie import planovac

    m = Email(message_id="<r8-neoverena@zsr.example>", sender="vyluky@zsr.example", subject="Výluka FIKTÍVNA")
    session.add(m)
    session.flush()
    ev = ExtractedEvent(email_id=m.id, kind="vyluka_nova", confidence=0.9, status="aplikovane",
                        needs_confirmation=False, data={"from_station": A, "to_station": B})
    session.add(ev)
    session.flush()
    _closure(session, start=loc(2026, 11, 22, 8), end=loc(2026, 11, 22, 14), until_revoked=False, ref="4200/2026",
             status="navrh", source_event_id=ev.id)
    _closure(session, kind="kolaj", track="1", daily=("01:00", "02:00"), start=loc(2026, 11, 1, 1),
             end=loc(2026, 12, 21, 2), ref="4201/2026")
    real = planovac.build_segments

    def spy(net, groute, windows, *args, **kwargs):
        assert len(windows) <= 40, f"{len(windows)} okien výluk pre jeden vlak – rozbalenie nie je ohraničené"
        return real(net, groute, windows, *args, **kwargs)

    monkeypatch.setattr(planovac, "build_segments", spy)
    rec = _decide(session, loc(2027, 11, 20, 10), loc(2026, 11, 21, 9))
    assert not (rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ"), (rec.decision, rec.color, rec.summary)
    assert any("22.11. 08:00 – 22.11. 14:00 z e-mailu" in r and "netýka" in r for r in rec.reasons), rec.reasons


# ------------------------------------------------------------------ P2 – výluka do odvolania v textoch a návrhoch
@pytest.mark.parametrize("ready, fake", [
    (loc(2027, 1, 20, 10), "22.01. 16:00"),    # vlak za technickým koncom – koniec prehľadávaného obdobia
    (loc(2026, 12, 21, 10), "23.12. 16:00"),   # obdobie siaha za technický koniec
    (loc(2026, 11, 25, 10), "23.12. 08:00"),   # vlak pred technickým koncom – technický koniec nie je koniec výluky
], ids=["po-technickom-konci", "cez-technicky-koniec", "pred-technickym-koncom"])
def test_vyluka_do_odvolania_bez_vymysleneho_konca(env, session, ready, fake):
    """[P2] Overená úplná výluka do odvolania od 23.11. 08:00 (technický koniec 23.12. 08:00): dôvody uvádzajú
    „od 23.11. 08:00 do odvolania“, nie koniec prehľadávaného obdobia ani technický koniec. Najmenej zlá možnosť nemá
    čas odchodu ani príchodu (termín jazdy nie je známy, kým ŽSR výluku neodvolá), návrh trasy ani koncept objednávky
    trasy v IS KIS sa nepripraví (odchod po vymyslenom konci = trasa do platnej výluky), e-mail ich neuvádza."""
    from vlaky.notifikacie.texty import recommendation_body

    _closure(session)
    rec = _decide(session, ready, loc(2026, 11, 21, 9))
    assert (rec.decision, rec.color) == ("ESKALOVAŤ", "ČERVENÁ"), rec.summary
    assert "Výluka Ružomberok – Liptovský Mikuláš od 23.11. 08:00 do odvolania na trase vlaku." in rec.reasons
    assert not any(fake in t for t in _texts(rec)), _texts(rec)
    assert "termín jazdy nie je známy" in rec.summary and "platí od 23.11. 08:00 do odvolania" in rec.summary
    assert "s odchodom" not in rec.summary and "príchod" not in rec.summary.split("Najmenej zlá")[-1]
    assert not rec.chosen and rec.path_proposal == {}, (rec.chosen, rec.path_proposal)
    assert rec.variants and all(v.get("arrival_p50") is None and v.get("arrival_p90") is None for v in rec.variants)
    assert all(not any(str(r).startswith("rezerva voči termínu") for r in v.get("reasons") or [])
               for v in rec.variants)
    assert not (rec.crew or {}).get("end_p50")
    body = recommendation_body(rec)
    assert "NÁVRH TRASY" not in body and "objednávka ad hoc trasy" not in body and fake not in body, body


def test_web_odporucanie_pri_vyluke_do_odvolania(env, session):
    """[P2] Web: odporúčanie vlaku, ktorý by čakal na koniec výluky do odvolania – stránka uvádza „do odvolania“, nie
    vymyslený koniec, bez postupnosti dopravní a konceptu žiadosti o trasu; kroky dispečera neponúkajú objednanie trasy
    a hovoria, že nový termín zatiaľ nie je známy."""
    from fastapi.testclient import TestClient

    from test_web_spolocne import login_as
    from vlaky.web.app import create_app
    from vlaky.web.views_prehlad import _next_steps

    _closure(session)
    rec = _decide(session, loc(2027, 1, 20, 10), loc(2026, 11, 21, 9))
    session.commit()  # odporúčanie uložené – web ho číta vo vlastnom spojení
    steps = _next_steps(rec)
    assert any("nový termín zatiaľ nie je známy" in x for x in steps) and not any("Objednať" in x for x in steps)
    with TestClient(create_app(auto_evaluate=False)) as client:
        login_as(client, "dispecer")
        r = client.get(f"/odporucania/{rec.id}")
    assert r.status_code == 200, r.text[:500]
    assert "od 23.11. 08:00 do odvolania" in r.text, r.text[:3000]
    assert "Postupnosť dopravní" not in r.text and "Žiadosť o trasu pre ŽSR" not in r.text
    assert "22.01. 16:00" not in r.text and "22.01. 18:52" not in r.text


def test_objednavka_pri_vyluke_do_odvolania_bez_navrhu_trasy(env, session):
    """[P2] Objednávka s odchodom za technickým koncom výluky do odvolania: žiadna bezpečná varianta, vysvetlenie
    uvádza výluku do odvolania a návrh trasy na objednanie (s odchodom po vymyslenom konci) sa nepripraví."""
    from vlaky.notifikacie.texty import order_proposal_text
    from vlaky.planovanie.planovac import plan_order

    _closure(session)
    now = loc(2026, 11, 21, 9)
    set_fake_now(now)
    o = Order(customer="FIKTÍVNY zákazník", origin=A, destination=B, earliest_departure_utc=loc(2027, 1, 20, 10),
              deadline_utc=loc(2027, 1, 21, 10), status="nova")
    session.add(o)
    session.commit()
    plan = plan_order(session, o.id, now)
    assert plan["decision"] == "ESKALOVAŤ", plan["explanation"]
    assert not plan.get("path_proposal"), plan.get("path_proposal")
    assert any("platí od 23.11. 08:00 do odvolania" in e and "nie je známy" in e for e in plan["explanation"])
    assert all(v.get("arrival_p50") is None for v in plan["variants"]), plan["variants"]
    _subject, body = order_proposal_text(o, plan)
    assert "NÁVRH TRASY" not in body and "22.01." not in body, body


def test_denna_vyluka_do_odvolania_ma_skutocne_denne_konce(env, session):
    """[P2] Denná výluka do odvolania (každý deň 08:00 – 18:00): každé okno má skutočný koniec – aj okno, do ktorého
    padne koniec prehľadávaného obdobia (nie je ním orezané), a žiadne nie je „do odvolania“. Odchod po dennom okne
    je skutočný odchod (VYPRAVIŤ); vlak pripravený počas okna dostane odchod po jeho konci s rezervou – aj ako najmenej
    zlú možnosť s návrhom trasy a konceptom objednávky trasy (termín zákazníka 18:00 už nestihne)."""
    from vlaky.planovanie.planovac import closure_windows

    _closure(session, daily=("08:00", "18:00"))
    wins = closure_windows(session, loc(2027, 1, 18, 0), loc(2027, 1, 20, 12))
    last = max(wins, key=lambda w: w.start)
    assert (_l(last.start), _l(last.end)) == ("20.01. 08:00", "20.01. 18:00"), [(_l(w.start), _l(w.end)) for w in wins]
    assert wins and not any(w.open_end for w in wins)
    now = loc(2026, 11, 21, 9)
    late = _decide(session, loc(2027, 1, 20, 19), now)
    assert (late.decision, late.color) == ("VYPRAVIŤ", "ZELENÁ"), late.summary
    assert not any("do odvolania" in t for t in _texts(late)), _texts(late)
    early = _decide(session, loc(2027, 1, 20, 10), now)
    assert _l(datetime.fromisoformat(early.chosen["depart"])) == "20.01. 18:30", early.summary
    assert "s odchodom 20.01. 18:30" in early.summary or early.decision in ("POČKAŤ", "ODSTAVIŤ"), early.summary
    assert "nie je známy" not in early.summary and not any("do odvolania" in t for t in _texts(early))
    stations = early.path_proposal["stations"]
    assert _l(datetime.fromisoformat(stations[0]["dep"])) == "20.01. 18:30", stations
    assert "odchod 20.01. 18:30" in early.path_proposal["drafts"]["zsr_path_request"]
    assert any("20.01. 08:00 – 20.01. 18:00 na trase vlaku" in r for r in early.reasons), early.reasons
    assert any("22.01. 08:00 – 22.01. 18:00 na trase vlaku" in r for r in early.reasons), early.reasons


def test_vlak_s_pripravenostou_v_minulosti_vidi_vyluku_od_teraz(env, session):
    """[P1/P2] Plánovaný vlak, ktorého pripravenosť je v minulosti (pripravený od 06:00, teraz 09:00): okná výluk sa
    počítajú od teraz – dnešné okno dennej výluky do odvolania (08:00 – 18:00) platí, vlak nesmie ísť hneď. Technický
    koniec výluky je zajtra (agent ho ešte neposunul) – výluka platí ďalej a okno, do ktorého padne koniec
    prehľadávaného obdobia, má skutočný denný koniec (nie koniec obdobia)."""
    _closure(session, daily=("08:00", "18:00"), start=loc(2026, 11, 1, 8), end=loc(2026, 11, 22, 12))
    now = loc(2026, 11, 21, 9)
    rec = _decide(session, now - timedelta(hours=3), now, path_at=now + timedelta(minutes=30))
    assert not (rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ"), rec.summary
    if rec.chosen:
        assert datetime.fromisoformat(rec.chosen["depart"]) >= loc(2026, 11, 21, 18, 30), rec.summary
    assert any("21.11. 08:00 – 21.11. 18:00 na trase vlaku" in r for r in rec.reasons), rec.reasons
    assert any("23.11. 08:00 – 23.11. 18:00 na trase vlaku" in r for r in rec.reasons), rec.reasons
    assert not any("do odvolania" in t for t in _texts(rec)), _texts(rec)


def test_iduci_vlak_vidi_vyluku_do_odvolania_od_teraz(env, session):
    """[P2] Idúci vlak Liptovský Hrádok → Žilina tesne po odchode a pred ním úplná výluka do odvolania Liptovský
    Mikuláš – Ružomberok, ktorá platí od včera: vlak nesmie pokračovať (nikdy POKRAČOVAŤ ZELENÁ), odporúčanie neuvádza
    vymyslený príchod po konci prehľadávaného obdobia ani návrh trasy; rušňovodič v službe má skutočný čas
    vystriedania, koniec jeho služby (príchod) nie je známy."""
    from vlaky.planovanie.planovac import evaluate_train
    from vlaky.siet.graf import TrainSpec, network

    now = loc(2026, 11, 21, 9)
    set_fake_now(now)
    _closure(session, start=loc(2026, 11, 20, 8), end=loc(2026, 12, 20, 8), a=B, b=A)
    t = Train(number="88901", origin="Liptovský Hrádok", destination="Žilina", traction="E", gauge="1435", vmax_kmh=100,
              status="v_jazde", planned_departure_utc=now - timedelta(minutes=10),
              departed_utc=now - timedelta(minutes=10), deadline_utc=now + timedelta(hours=6), length_m=500,
              weight_t=1500)
    session.add(t)
    session.flush()
    d = Driver(name="R. 88901 FIKTÍVNY")
    session.add(d)
    session.flush()
    session.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=now - timedelta(minutes=70), status="prebieha"))
    route = network().route("Liptovský Hrádok", "Žilina", TrainSpec())
    lon, lat = network().geometry(route)[3]
    session.add(PositionReport(train_id=t.id, train_number=t.number, ts_utc=now - timedelta(minutes=1), lat=lat,
                               lon=lon, source="gps"))
    session.commit()
    rec = evaluate_train(session, t.id, now, "výluka do odvolania")
    assert (rec.decision, rec.color) == ("ESKALOVAŤ", "ČERVENÁ"), rec.summary
    assert "Výluka Liptovský Mikuláš – Ružomberok od 20.11. 08:00 do odvolania na trase vlaku." in rec.reasons
    assert "termín jazdy nie je známy" in rec.summary, rec.summary
    assert "príchod" not in rec.summary.split("Najmenej zlá")[-1], rec.summary
    assert rec.path_proposal == {} and not rec.chosen
    crew = rec.crew or {}
    assert crew.get("relieve_by") and crew.get("end_p50") is None, crew
    assert session.scalar(select(Train.status).where(Train.id == t.id)) == "v_jazde"


def test_vlak_pred_cielom_zatvorenym_do_odvolania_caka_bez_casu_prichodu(env, session):
    """[P2] Idúci vlak 800 m pred cieľom (Trenčín), v cieľovej stanici mimoriadnosť do odvolania: POČKAŤ pred cieľom,
    kým sa neuvoľní vjazd – bez vymysleného príchodu (koniec prehľadávaného obdobia či technický koniec) v súhrne,
    v dôvodoch, v e-maile ani v údajoch zvolenej varianty. Ďalší prepočet bez zmeny údajov nové odporúčanie nevytvorí;
    keď ŽSR oznámi koniec mimoriadnosti, odporúčanie so skutočným príchodom je nové."""
    from vlaky.models import Recommendation
    from vlaky.notifikacie.texty import recommendation_body
    from vlaky.planovanie.planovac import evaluate_train
    from vlaky.siet.graf import TrainSpec, haversine, network

    now = loc(2026, 11, 21, 9)
    set_fake_now(now)
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
    c = _closure(session, kind="mimoriadnost", a="Trenčín", b="Trenčín", start=now - timedelta(hours=1),
                 end=now + timedelta(days=30), ref="")
    t = Train(number="88970", origin="Púchov", destination="Trenčín", traction="E", gauge="1435", vmax_kmh=100,
              status="v_jazde", planned_departure_utc=now - timedelta(minutes=30),
              departed_utc=now - timedelta(minutes=30), deadline_utc=now + timedelta(hours=8))
    session.add(t)
    session.flush()
    d = Driver(name="R. 88970 FIKTÍVNY")
    session.add(d)
    session.flush()
    session.add(DriverShift(driver_id=d.id, train_id=t.id, status="prebieha", start_utc=now - timedelta(hours=2)))
    session.add(PositionReport(train_id=t.id, train_number=t.number, ts_utc=now, lat=point[0], lon=point[1],
                               source="gps"))
    session.commit()
    rec = evaluate_train(session, t.id, now, "mimoriadnosť do odvolania")
    assert (rec.decision, rec.color) == ("POČKAŤ", "ČERVENÁ"), rec.summary
    open_text = "mimoriadnosť Trenčín – Trenčín platí od 21.11. 08:00 do odvolania"
    assert "dokončiť jazdu po uvoľnení vjazdu" in rec.summary and f"príchod nie je známy – {open_text}" in rec.summary
    assert any("počkať pred cieľom, kým sa neuvoľní vjazd (príchod nie je známy" in x for x in rec.reasons)
    assert not any("podľa výpočtu" in x for x in _texts(rec)), _texts(rec)
    ch = rec.chosen
    assert ch["until_entry"] and ch["arrival_p50"] is None and ch["arrival_p90"] is None and ch["do_odvolania"]
    assert rec.path_proposal == {} and (rec.crew or {}).get("end_p50") is None
    body = recommendation_body(rec)
    assert f"Príchod: nie je známy – {open_text}" in body and "Odchod: po uvoľnení vjazdu do cieľa" in body, body
    first = rec.id
    later = now + timedelta(minutes=10)
    set_fake_now(later)
    session.add(PositionReport(train_id=t.id, train_number=t.number, ts_utc=later, lat=point[0], lon=point[1],
                               source="gps"))
    session.commit()
    assert evaluate_train(session, t.id, later, "nová poloha").id == first  # bez zmeny údajov žiadne nové odporúčanie
    c.until_revoked, c.end_utc = False, later + timedelta(hours=2)   # ŽSR oznámila koniec mimoriadnosti
    session.commit()
    known = evaluate_train(session, t.id, later + timedelta(minutes=1), "koniec mimoriadnosti")
    assert known.id != first and known.chosen.get("arrival_p50") and not known.chosen.get("do_odvolania")
    assert session.get(Recommendation, first).state == "zastarane"


# ------------------------------------------------------------------ web – preklep v roku
def test_formular_vlaku_odmietne_pripravenost_o_roky_dopredu(env, session):
    """[P1] Formulár vlaku neprijme čas pripravenosti (ani odchod, termín či avízo) viac ako rok dopredu – preklep
    v roku (2072) by dal vlak o desaťročia; chyba je slovensky a vlak sa nezaloží."""
    from fastapi.testclient import TestClient

    from test_web_spolocne import csrf, login_as
    from vlaky.web.app import create_app

    set_fake_now(loc(2026, 11, 21, 9))
    with TestClient(create_app(auto_evaluate=False)) as client:
        login_as(client, "dispecer")
        base = {"number": "88950", "run_date": "", "origin": "Čadca", "destination": "Žilina", "gauge": "1435",
                "traction": "E", "vmax_kmh": "100", "status": "planovany"}
        r = client.post("/vlaky/novy", data={**base, "csrf_token": csrf(client, "/vlaky/novy"),
                                             "ready_utc": "2072-11-21T10:00"})
        assert r.status_code == 422, r.text[:500]
        assert "Pripravený (miestny čas): najviac 1 rok dopredu (do 22.11.2027) – skontrolujte dátum, najmä rok." \
            in r.text, r.text[:2000]
        r = client.post("/vlaky/novy", data={**base, "csrf_token": csrf(client, "/vlaky/novy"),
                                             "ready_utc": "2026-11-22T10:00", "deadline_utc": "2208-11-22T18:00"})
        assert r.status_code == 422 and "Termín doručenia (miestny čas): najviac 1 rok dopredu" in r.text
        ok = client.post("/vlaky/novy", follow_redirects=False,
                         data={**base, "csrf_token": csrf(client, "/vlaky/novy"), "ready_utc": "2027-11-20T10:00"})
        assert ok.status_code == 303, ok.text[:500]
        session.expire_all()
        trains = list(session.scalars(select(Train).where(Train.number == "88950")))
        assert len(trains) == 1 and _l(trains[0].ready_utc) == "20.11. 10:00"
        r = client.post("/hranice/preberanie/nove", follow_redirects=True, data={
            "csrf_token": csrf(client, "/hranice"), "train_id": str(trains[0].id), "border_id": "1",
            "direction": "vstup", "avizo_eta_utc": "2027-12-24T10:00"})
        assert "Avízo – predpokladaný príchod: najviac 1 rok dopredu" in r.text, r.text[:2000]


def test_mapa_a_register_ukazu_vyluku_do_odvolania_aj_po_technickom_konci(env, session):
    """[P2] Web: výluka do odvolania, ktorej technický koniec už uplynul (agent ho pri výpadku neposunul), platí ďalej
    ako v plánovači – je v zozname platných výluk, na mape aj medzi výlukami na trase vlaku; mapa a register uvádzajú
    „do odvolania“, nie technický koniec ako koniec výluky. Denná výluka do odvolania má interval so skutočným koncom."""
    from fastapi.testclient import TestClient

    from test_web_spolocne import login_as, make_train
    from vlaky.web.app import create_app

    now = loc(2026, 11, 21, 9)
    set_fake_now(now)
    full = _closure(session, a="Krásno nad Kysucou", b="Kysucké Nové Mesto", start=loc(2026, 11, 1, 8),
                    end=loc(2026, 11, 20, 8), ref="4300/2026")
    daily = _closure(session, a="Čadca", b="Krásno nad Kysucou", daily=("08:00", "18:00"), start=loc(2026, 11, 1, 8),
                     end=loc(2026, 11, 20, 8), ref="4301/2026")
    train_id = make_train(number="88960", origin="Čadca", destination="Žilina", status="planovany",
                          ready_utc=now + timedelta(hours=1))
    with TestClient(create_app(auto_evaluate=False)) as client:
        login_as(client, "dispecer")
        page = client.get("/vyluky").text
        assert "od 01.11. 08:00 do odvolania" in page and "(technický koniec 20.11. 08:00)" in page, page[:3000]
        features = {f["id"]: f["properties"] for f in client.get(
            "/api/vyluky", params={"od": now.isoformat(), "do": (now + timedelta(hours=24)).isoformat()}).json()["features"]}
        assert features[full.id]["do_odvolania"] is True and features[daily.id]["do_odvolania"] is True
        assert features[full.id]["aktivna"] is True, features[full.id]
        t = next(x for x in client.get("/api/vlaky").json()["vlaky"] if x["id"] == train_id)
        rc = {x["id"]: x for x in t["route_closures"]}
        assert full.id in rc and rc[full.id]["do_odvolania"] is True and rc[full.id]["aktivna"] is True, rc
        assert daily.id in rc and rc[daily.id]["do_odvolania"] is False, rc
        assert all(_l(datetime.fromisoformat(b))[-5:] == "18:00" for _a, b in rc[daily.id]["intervaly"]), rc
    js = (Path(__file__).resolve().parent.parent / "vlaky" / "web" / "static" / "js" / "mapa.js").read_text("utf-8")
    assert "' do odvolania'" in js
