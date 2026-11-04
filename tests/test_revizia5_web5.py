"""Regresné testy k nálezom piatej revízie webu (oblasť D – web5).

R5U0 – otvorená stránka odporúčania neukazuje ZELENÚ po tom, čo dáta agenta zostarli (odtlačok stavu stránky pre
živú obnovu), a „Prijať“ / „Upraviť“ kontroluje čerstvosť na serveri: bez čerstvých dát agenta vyžaduje výslovné
potvrdenie a v audite je zobrazená farba (ORANŽOVÁ), nikdy neaktuálna ZELENÁ; zhoršený semafor od zobrazenia stránky
rozhodnutie neuloží.
R5U2 – najbližšie riziko, okno mapy a konflikt na časovej osi idúceho vlaku nepočítajú s výlukou, ktorú vlak už prešiel.
R5U5 – porovnanie variantov idúceho vlaku: pokračovanie je POKRAČOVAŤ (nie VYPRAVIŤ „teraz“), čakanie pred vjazdom do
zatvoreného cieľa „po uvoľnení vjazdu“ (nie hranica výpočtu ako ODCHOD).
R5U6 – slovenské texty webu: chyba 422 po slovensky, export CSV so slovenskými stavmi a desatinnou čiarkou, dátum jazdy
v tvare DD.MM.RRRR.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta

from sqlalchemy import select

from test_web_spolocne import csrf, login_as, make_recommendation, make_train

pytest_plugins = ["test_web_spolocne"]

STATE = {"X-Vlaky-Stav": "1"}


# ------------------------------------------------------------------ pomôcky
def _agent_at(at, mailbox_at=None) -> None:
    """Posledný cyklus agenta a posledná úspešná kontrola schránky (potom agent stojí)."""
    from vlaky.agent.zdravie import set_kv
    from vlaky.db import session_scope

    with session_scope() as s:
        set_kv(s, "agent_last_cycle", {"ts": at.isoformat(), "errors": 0})
        set_kv(s, "mail_last_ok", {"ts": (mailbox_at or at).isoformat(), "fetched": 0})


def _header(html: str) -> str:
    i = html.find('<section class="decision')
    assert i >= 0
    return html[i:html.find("</section>", i)]


def _section(html: str, heading: str) -> str:
    i = html.find(f">{heading}</h2>")
    assert i >= 0, f"Na stránke chýba časť {heading!r}"
    return html[i:html.find("</section>", i)]


def _semafors(fragment: str) -> list:
    return re.findall(r"semafor-(\w+)", fragment)


def _form(html: str, action: str) -> str:
    m = re.search(r'<form method="post" action="/odporucania/\d+/rozhodnutie" class="decide">\s*'
                  r'<input type="hidden" name="csrf_token"[^>]*><input type="hidden" name="akcia" value="'
                  + action + r'">.*?</form>', html, re.S)
    assert m, f"Formulár {action} chýba"
    return m.group(0)


def _page_state(html: str) -> str:
    m = re.search(r'data-zivy-stav="([0-9a-f]+)"', html)
    assert m, "Stránka nemá odtlačok stavu (data-zivy-stav)"
    return m.group(1)


def _flash(html: str) -> str:
    m = re.search(r'<div class="flash flash-(\w+)"[^>]*>(.*?)</div>', html, re.S)
    return f"{m.group(1)}: {m.group(2)}" if m else ""


def _audits(action: str, rec_id: int) -> list:
    from vlaky.db import session_scope
    from vlaky.models import AuditLog

    with session_scope() as s:
        return [dict(a.detail) for a in s.scalars(select(AuditLog).where(AuditLog.action == action,
                                                                         AuditLog.entity_id == str(rec_id)))]


def _green_rec(t0, number: str = "95006") -> tuple:
    """Plánovaný vlak s aktuálnym odporúčaním VYPRAVIŤ ZELENÁ (dáta agenta k t0 čerstvé)."""
    _agent_at(t0)
    tid = make_train(number=number, status="planovany", origin="Čadca", destination="Žilina",
                     ready_utc=t0 + timedelta(hours=1), deadline_utc=t0 + timedelta(hours=10))
    depart = (t0 + timedelta(hours=1)).isoformat()
    chosen = {"route": "Čadca – Žilina", "depart": depart, "kind": "VYPRAVIŤ", "status": "ZELENÁ",
              "p_on_time": 0.97, "p_hold": 0.0, "arrival_p50": (t0 + timedelta(hours=2)).isoformat(),
              "arrival_p90": (t0 + timedelta(hours=2, minutes=10)).isoformat(), "expected_cost": 400.0,
              "reasons": [], "needs_new_path": False}
    rid = make_recommendation(tid, decision="VYPRAVIŤ", color="ZELENÁ", priority="INFO",
                              summary=f"Vlak {number}: VYPRAVIŤ (FIKTÍVNE).", chosen=chosen, variants=[dict(chosen)],
                              reasons=[], data_age={"mailbox_min": 1.0, "computed_at": t0.isoformat()})
    return tid, rid


# ================================================================== R5U0: neaktuálna ZELENÁ a „Prijať“
def test_prijat_neaktualnu_zelenu_len_s_potvrdenim_a_v_audite_nie_je_zelena(client, fixed_now):
    from vlaky.cas import set_fake_now
    from vlaky.db import session_scope
    from vlaky.models import Recommendation

    t0 = fixed_now
    # kontrola: s čerstvými dátami agenta sa ZELENÁ prijme bez potvrdenia a v audite ostane ZELENÁ
    _tid, fresh_id = _green_rec(t0, "95008")
    login_as(client, "d501")
    token = csrf(client, f"/odporucania/{fresh_id}")
    r = client.post(f"/odporucania/{fresh_id}/rozhodnutie", follow_redirects=True,
                    data={"csrf_token": token, "akcia": "prijat", "farba": "ZELENÁ"})
    assert _flash(r.text) == "ok: Odporúčanie prijaté.", _flash(r.text)
    (detail,) = _audits("recommendation_prijat", fresh_id)
    assert detail["color"] == "ZELENÁ" and "computed_color" not in detail and "data_freshness" not in detail

    _tid, rid = _green_rec(t0)
    page0 = client.get(f"/odporucania/{rid}").text
    assert _semafors(_header(page0))[0] == "zelena"
    token = csrf(client, f"/odporucania/{rid}")

    # dispečer má stránku otvorenú, agent medzitým 45 min neprebehol a nečítal schránku
    set_fake_now(t0 + timedelta(minutes=45))
    for action, note in (("prijat", ""), ("upravit", "odchod o 10 min neskôr")):
        r = client.post(f"/odporucania/{rid}/rozhodnutie", follow_redirects=True,
                        data={"csrf_token": token, "akcia": action, "poznamka": note, "farba": "ZELENÁ"})
        flash = _flash(r.text)
        assert flash.startswith("varovanie: Údaje agenta nie sú čerstvé") and "neuložilo" in flash, flash
        assert "ako ORANŽOVÁ (pri prepočte ZELENÁ)" in flash, flash
    with session_scope() as s:
        assert s.get(Recommendation, rid).state == "nove"
    assert _audits("recommendation_prijat", rid) == [] and _audits("recommendation_upravit", rid) == []

    # stránka pri čerstvých dátach: skrytá farba ZELENÁ, bez potvrdenia; obnovená stránka: ORANŽOVÁ, potvrdenie
    # pri „Prijať“ aj „Upraviť“, skrytá farba je zobrazená farba
    prijat = _form(page0, "prijat")
    assert 'name="farba" value="ZELENÁ"' in prijat and "potvrdit_neaktualne" not in prijat
    page = client.get(f"/odporucania/{rid}").text
    assert _semafors(_header(page))[0] == "oranzova"
    for action in ("prijat", "upravit"):
        form = _form(page, action)
        assert 'name="farba" value="ORANŽOVÁ"' in form
        assert re.search(r'<input type="checkbox" name="potvrdit_neaktualne" value="1" required>', form), form
    assert "potvrdit_neaktualne" not in _form(page, "zamietnut")  # zamietnutie potvrdenie nepotrebuje

    r = client.post(f"/odporucania/{rid}/rozhodnutie", follow_redirects=True,
                    data={"csrf_token": token, "akcia": "prijat", "farba": "ORANŽOVÁ", "potvrdit_neaktualne": "1"})
    flash = _flash(r.text)
    assert flash.startswith("ok: Odporúčanie prijaté. Zaznamenané ako ORANŽOVÁ"), flash
    with session_scope() as s:
        assert s.get(Recommendation, rid).state == "prijate"
    (detail,) = _audits("recommendation_prijat", rid)
    assert detail["color"] == "ORANŽOVÁ" and detail["computed_color"] == "ZELENÁ", detail
    assert detail["stale_confirmed"] is True
    assert detail["data_freshness"].startswith("Údaje agenta nie sú čerstvé"), detail


def test_zamietnutie_pri_neaktualnych_datach_bez_potvrdenia_farba_v_audite(client, fixed_now):
    from vlaky.cas import set_fake_now

    t0 = fixed_now
    _tid, rid = _green_rec(t0, "95007")
    login_as(client, "d502")
    token = csrf(client, f"/odporucania/{rid}")
    set_fake_now(t0 + timedelta(hours=2))
    r = client.post(f"/odporucania/{rid}/rozhodnutie", follow_redirects=True,
                    data={"csrf_token": token, "akcia": "zamietnut", "poznamka": "ŽSR pridelila inú trasu",
                          "farba": "ZELENÁ"})
    assert _flash(r.text) == "ok: Odporúčanie zamietnuté.", _flash(r.text)
    (detail,) = _audits("recommendation_zamietnut", rid)
    assert detail["color"] == "ORANŽOVÁ" and detail["computed_color"] == "ZELENÁ", detail
    assert "stale_confirmed" not in detail and detail["data_freshness"].startswith("Údaje agenta nie sú čerstvé")


def test_zhorseny_semafor_od_zobrazenia_stranky_rozhodnutie_neulozi(client, fixed_now):
    """Plánovač odporúčanie potichu aktualizoval (mäkká poistka: ZELENÁ → ORANŽOVÁ) po zobrazení stránky: prijatie
    z pôvodnej (zelenej) stránky sa neuloží; zo stránky, ktorá ukazuje ORANŽOVÚ, áno."""
    from vlaky.db import session_scope
    from vlaky.models import Recommendation

    _tid, rid = _green_rec(fixed_now, "95009")
    login_as(client, "d504")
    page = client.get(f"/odporucania/{rid}").text
    token = csrf(client, f"/odporucania/{rid}")
    with session_scope() as s:
        s.get(Recommendation, rid).color = "ORANŽOVÁ"
    r = client.post(f"/odporucania/{rid}/rozhodnutie", follow_redirects=True,
                    data={"csrf_token": token, "akcia": "prijat", "farba": "ZELENÁ"})
    flash = _flash(r.text)
    assert flash.startswith("varovanie: Semafor odporúčania sa od zobrazenia stránky zhoršil (bol ZELENÁ, teraz "
                            "ORANŽOVÁ)"), flash
    assert _audits("recommendation_prijat", rid) == []
    assert _page_state(page) != _page_state(r.text)  # otvorená stránka sa pri ďalšom dopyte obnoví
    r = client.post(f"/odporucania/{rid}/rozhodnutie", follow_redirects=True,
                    data={"csrf_token": token, "akcia": "prijat", "farba": "ORANŽOVÁ"})
    assert _flash(r.text) == "ok: Odporúčanie prijaté.", _flash(r.text)
    (detail,) = _audits("recommendation_prijat", rid)
    assert detail["color"] == "ORANŽOVÁ"


def test_odtlacok_stavu_odporucania_pre_zivu_obnovu(client, fixed_now):
    """Otvorená stránka sa pýta na odtlačok (X-Vlaky-Stav): pri čerstvých dátach je rovnaký, po výpadku agenta iný
    (stránka si obsah obnoví a ukáže ORANŽOVÚ). Obnova na pozadí (X-Vlaky-Obnova) nezje flash správu."""
    from vlaky.cas import set_fake_now

    t0 = fixed_now
    _tid, rid = _green_rec(t0, "95010")
    login_as(client, "d505")
    page = client.get(f"/odporucania/{rid}").text
    fresh_state = _page_state(page)
    r = client.get(f"/odporucania/{rid}", headers=STATE)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json() == {"stav": fresh_state}
    assert r.headers.get("cache-control") == "no-store"
    set_fake_now(t0 + timedelta(minutes=5))
    assert client.get(f"/odporucania/{rid}", headers=STATE).json() == {"stav": fresh_state}  # nič sa nezmenilo

    set_fake_now(t0 + timedelta(minutes=45))
    stale_state = client.get(f"/odporucania/{rid}", headers=STATE).json()["stav"]
    assert stale_state != fresh_state
    refreshed = client.get(f"/odporucania/{rid}", headers={"X-Vlaky-Obnova": "1"}).text
    assert _page_state(refreshed) == stale_state and _semafors(_header(refreshed))[0] == "oranzova"

    # flash z presmerovania (napr. v inej karte) ostane pre stránku, ktorej patrí
    token = csrf(client, f"/odporucania/{rid}")
    r = client.post(f"/odporucania/{rid}/rozhodnutie", follow_redirects=False,
                    data={"csrf_token": token, "akcia": "zamietnut", "poznamka": ""})
    assert r.status_code == 303
    background = client.get(f"/odporucania/{rid}", headers={"X-Vlaky-Obnova": "1"}).text
    assert "Pri zamietnutí uveďte dôvod." not in background
    assert "Pri zamietnutí uveďte dôvod." in client.get(f"/odporucania/{rid}").text


def test_odtlacok_stavu_detailu_vlaku_a_zoznamu_odporucani(client, fixed_now):
    from vlaky.cas import set_fake_now

    t0 = fixed_now
    tid, rid = _green_rec(t0, "95011")
    login_as(client, "d506")
    urls = (f"/vlaky/{tid}", "/odporucania", "/vlaky")
    states = {url: _page_state(client.get(url).text) for url in urls}
    for url in urls:
        assert client.get(url, headers=STATE).json() == {"stav": states[url]}, url
    set_fake_now(t0 + timedelta(minutes=45))
    for url in urls:
        assert client.get(url, headers=STATE).json()["stav"] != states[url], url
    page = client.get(f"/vlaky/{tid}", headers={"X-Vlaky-Obnova": "1"}).text
    current = _section(page, "Aktuálne odporúčanie")
    assert _semafors(current) == ["oranzova"] and "pôvodne ZELENÁ" in current
    row = re.search(r"<tr>(?:(?!</tr>).)*95011(?:(?!</tr>).)*</tr>", client.get("/vlaky").text, re.S).group(0)
    assert _semafors(row) == ["oranzova"], row


def test_stranka_s_odkazom_pre_rusnovodica_sa_neprepise(client, fixed_now):
    """Jednorazový odkaz pre rušňovodiča (odpoveď na POST) sa živou obnovou neprepíše – stránka len upozorní."""
    tid, _rid = _green_rec(fixed_now, "95012")
    login_as(client, "d507")
    token = csrf(client, f"/vlaky/{tid}")
    page = client.post(f"/vlaky/{tid}/odkaz", data={"csrf_token": token}).text
    assert "Odkaz pre rušňovodiča (platí 24 h)" in page
    head = re.search(r'<div class="page-head"[^>]*>', page).group(0)
    assert f'data-zivy-url="/vlaky/{tid}"' in head and 'data-zivy-len-upozornit="1"' in head


def test_skript_zivej_obnovy_je_na_strankach(client, fixed_now):
    """Stránky so živým stavom načítajú spoločný skript, ktorý sa pýta na odtlačok a obnovuje obsah."""
    tid, rid = _green_rec(fixed_now, "95013")
    login_as(client, "d508")
    js = client.get("/static/js/app.js").text
    assert "X-Vlaky-Stav" in js and "X-Vlaky-Obnova" in js and "data-zivy-stav" in js
    for url in (f"/odporucania/{rid}", f"/vlaky/{tid}", "/odporucania", "/vlaky"):
        page = client.get(url).text
        assert '<script src="/static/js/app.js' in page and "data-zivy-stav=" in page, url


# ================================================================== R5U2: výluka za idúcim vlakom
def _closures(t0) -> None:
    """Pomalá jazda Kysak – Košice platí teraz, úplná výluka Kraľovany – Ružomberok od t0 + 3 h."""
    from vlaky.db import session_scope
    from vlaky.hranice import seed_borders
    from vlaky.models import Closure

    with session_scope() as s:
        seed_borders(s)
        s.add(Closure(kind="pomala_jazda", from_station="Kysak", to_station="Košice", speed_kmh=30,
                      start_utc=t0 - timedelta(days=1), end_utc=t0 + timedelta(days=30), status="overene"))
        s.add(Closure(kind="uplna", from_station="Kraľovany", to_station="Ružomberok",
                      start_utc=t0 + timedelta(hours=3), end_utc=t0 + timedelta(hours=7), status="overene"))


def _running(t0, number: str, lat, lon) -> tuple:
    """Idúci vlak Čierna nad Tisou → Žilina s polohou (lat, lon) a odporúčaním z plánovača."""
    from vlaky.db import session_scope
    from vlaky.models import Driver, DriverShift, PositionReport
    from vlaky.planovanie.planovac import evaluate_train

    tid = make_train(number=number, status="v_jazde", origin="Čierna nad Tisou", destination="Žilina",
                     planned_departure_utc=t0 - timedelta(hours=4), deadline_utc=t0 + timedelta(hours=10))
    with session_scope() as s:
        d = Driver(name=f"Rušňovodič {number} FIKTÍVNY")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=tid, start_utc=t0 - timedelta(hours=4, minutes=30),
                          status="prebieha"))
        s.add(PositionReport(train_id=tid, train_number=number, ts_utc=t0 - timedelta(minutes=1), lat=lat, lon=lon,
                             source="gps", speed_kmh=80))
        s.flush()
        rid = evaluate_train(s, tid, t0, trigger="test").id
    return tid, rid


def _row(client, tid) -> dict:
    return next(x for x in client.get("/api/vlaky").json()["vlaky"] if x["id"] == tid)


def _sections(row: dict) -> list:
    return [(c["od_stanice"], c["do_stanice"]) for c in row["route_closures"]]


def test_vyluka_za_iducim_vlakom_nie_je_riziko_ani_konflikt(client, fixed_now):
    from vlaky.siet.graf import network

    _closures(fixed_now)
    poprad = network().find_station("Poprad-Tatry")  # ~150 km za úsekom Košice – Kysak, pred Kraľovanmi
    tid, _rid = _running(fixed_now, "95004", poprad.lat, poprad.lon)
    origin = network().find_station("Čierna nad Tisou")  # kontrola: vlak pred výlukou
    tid0, _rid0 = _running(fixed_now, "95005", origin.lat, origin.lon)
    login_as(client, "d511")
    row = _row(client, tid)
    assert _sections(row) == [("Kraľovany", "Ružomberok")], row["route_closures"]
    assert row["risk"].startswith("Úplná výluka úseku Kraľovany – Ružomberok (od "), row["risk"]
    assert row["conflict"] is False  # vlak úsek Kraľovany – Ružomberok prejde pred začiatkom výluky
    # okno vlaku na mape (/api/vlaky/{id}) a tabuľka vlakov na nástenke – rovnaké riziko
    assert client.get(f"/api/vlaky/{tid}").json()["risk"] == row["risk"]
    table = client.get("/").text
    i = table.find(">95004<")
    assert i >= 0 and "Kysak – Košice" not in table[i:table.find("</tr>", i)]
    # vlak pri východiskovej stanici: pomalá jazda Kysak – Košice je pred ním – riziko aj konflikt ostávajú
    row0 = _row(client, tid0)
    assert _sections(row0) == [("Kysak", "Košice"), ("Kraľovany", "Ružomberok")], row0["route_closures"]
    assert row0["conflict"] is True


def test_poloha_mimo_trasy_nic_neskryje_hlasenie_tis_podla_dopravne(client, fixed_now):
    """Poloha mimo trasy (odklon, chybná poloha) výluky neskryje; hlásenie TIS z dopravne sa umiestni podľa km
    dopravne na trase (bod dopravne môže byť mimo koľají trasy)."""
    from vlaky.db import session_scope
    from vlaky.models import PositionReport

    _closures(fixed_now)
    tid, _rid = _running(fixed_now, "95004", 48.1486, 17.1077)  # Bratislava – mimo trasy vlaku
    login_as(client, "d513")
    assert ("Kysak", "Košice") in _sections(_row(client, tid))
    with session_scope() as s:
        s.add(PositionReport(train_id=tid, train_number="95004", ts_utc=fixed_now, lat=48.1486, lon=17.1077,
                             source="tis", nearest_station="Poprad-Tatry"))
    assert _sections(_row(client, tid)) == [("Kraľovany", "Ružomberok")]


# ================================================================== R5U5: varianty idúceho vlaku
def _variant_rows(page: str) -> list:
    table = _section(page, "Porovnanie variantov")
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)[1:]
    return [[re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", r,
                                                                                         re.S)] for r in rows]


def test_varianty_iduceho_vlaku_pokracovat_nie_vypravit(client, fixed_now):
    from vlaky.siet.graf import network

    _closures(fixed_now)
    st = network().find_station("Poprad-Tatry")
    _tid, rid = _running(fixed_now, "95004", st.lat, st.lon)
    _tid, planned = _green_rec(fixed_now, "95014")
    login_as(client, "d521")
    rows = _variant_rows(client.get(f"/odporucania/{rid}").text)
    kinds = [r[2] for r in rows]
    assert "VYPRAVIŤ" not in kinds and "POKRAČOVAŤ" in kinds, rows
    cont = next(r for r in rows if r[2] == "POKRAČOVAŤ")
    assert cont[1] == "v jazde", cont  # nie čas prepočtu ako „odchod“
    # kontrola: plánovaný vlak sa vypravuje – VYPRAVIŤ s časom odchodu
    rows = _variant_rows(client.get(f"/odporucania/{planned}").text)
    assert rows[0][2] == "VYPRAVIŤ" and rows[0][1] == "02.11. 06:00", rows
    # odporúčanie VYPRAVIŤ spred odchodu ostáva VYPRAVIŤ aj keď vlak medzitým ide (plánovač idúcemu vlaku VYPRAVIŤ
    # nedáva – je to záznam toho, čo sa odporučilo pred odchodom)
    from vlaky.db import session_scope
    from vlaky.models import Train

    with session_scope() as s:
        s.get(Train, _tid).status = "v_jazde"
    rows = _variant_rows(client.get(f"/odporucania/{planned}").text)
    assert rows[0][2] == "VYPRAVIŤ" and rows[0][1] == "02.11. 06:00", rows


def test_cakanie_pred_vjazdom_do_ciela_bez_casu_odchodu(client, fixed_now):
    """Idúci vlak 800 m pred cieľom zatvoreným 30 h (dlhšie, ako siaha výpočet): zvolená varianta POČKAŤ ukazuje
    „po uvoľnení vjazdu“, nie hranicu výpočtu ako ODCHOD."""
    from vlaky.cas import fmt
    from vlaky.db import session_scope
    from vlaky.hranice import seed_borders
    from vlaky.models import Closure, Driver, DriverShift, PositionReport, Train
    from vlaky.planovanie.planovac import evaluate_train
    from vlaky.siet.graf import TrainSpec, haversine, network

    net = network()
    r = net.route("Púchov", "Trenčín", TrainSpec())
    target, cum, point = r.length_m / 1000.0 - 0.8, 0.0, None
    for i, ei in enumerate(r.edges):
        e = net.edges[ei]
        g = e.geom if e.u == r.nodes[i] else list(reversed(e.geom))
        for (la1, lo1), (la2, lo2) in zip(g, g[1:]):
            d = haversine(la1, lo1, la2, lo2) / 1000.0
            if point is None and cum + d >= target:
                f = (target - cum) / d if d else 0.0
                point = (la1 + f * (la2 - la1), lo1 + f * (lo2 - lo1))
            cum += d
    with session_scope() as s:
        seed_borders(s)
        s.add(Closure(kind="mimoriadnost", from_station="Trenčín", to_station="Trenčín", status="overene",
                      start_utc=fixed_now - timedelta(hours=1), end_utc=fixed_now + timedelta(hours=30)))
        t = Train(number="79945", origin="Púchov", destination="Trenčín", traction="E", gauge="1435", vmax_kmh=100,
                  status="v_jazde", planned_departure_utc=fixed_now - timedelta(minutes=40),
                  departed_utc=fixed_now - timedelta(minutes=40), deadline_utc=fixed_now + timedelta(hours=8))
        s.add(t)
        s.flush()
        drv = Driver(name="R. 79945 FIKTÍVNY")
        s.add(drv)
        s.flush()
        s.add(DriverShift(driver_id=drv.id, train_id=t.id, start_utc=fixed_now - timedelta(hours=1),
                          status="prebieha"))
        s.add(PositionReport(train_id=t.id, train_number="79945", ts_utc=fixed_now, lat=point[0], lon=point[1],
                             source="gps"))
        s.flush()
        rec = evaluate_train(s, t.id, fixed_now, "test")
        assert rec.chosen.get("until_entry"), rec.summary  # predpoklad: čaká pred vjazdom do konca výluky
        rid, boundary = rec.id, fmt(datetime.fromisoformat(rec.chosen["depart"]))
    login_as(client, "d523")
    rows = _variant_rows(client.get(f"/odporucania/{rid}").text)
    chosen = next(r for r in rows if "odporúčaná" in r[0])
    assert chosen[1] == "po uvoľnení vjazdu" and chosen[2] == "POČKAŤ", chosen
    assert not any(boundary in r[1] for r in rows), (boundary, rows)


# ================================================================== R5U6: slovenské texty
def test_chyba_422_po_slovensky(client):
    login_as(client, "d531")
    for url, name in (("/fronta?strana=abc", "strana"), ("/odporucania?strana=x", "strana")):
        r = client.get(url)
        assert r.status_code == 422
        text = re.sub(r"<[^>]+>", " ", r.text)
        assert f"Parameter adresy „{name}“: musí byť celé číslo." in text, text[:800]
        assert "Input should" not in r.text and "valid integer" not in r.text
    bad = client.post("/api/pozicie", json={"train_number": "1", "lat": 123, "lon": 18},
                      headers={"Authorization": "Bearer test-token"})
    assert bad.status_code == 422
    body = bad.json()
    assert body["detail"] == "Neplatné údaje – Pole „lat“: musí byť najviac 90.", body
    assert body["errors"][0]["text"] == "Pole „lat“: musí byť najviac 90." and body["errors"][0]["type"]


def test_chyba_422_vlastny_text_validatora_a_dlzka(client):
    """Vlastný slovenský text validátora sa prevezme bez anglického „Value error,“; dĺžka s tvarom slova."""
    from vlaky.web.app import validation_text

    assert validation_text({"loc": ("body", "source"), "type": "value_error",
                            "ctx": {"error": ValueError("neznámy zdroj (povolené: gps)")},
                            "msg": "Value error, neznámy zdroj"}) == "Pole „source“: neznámy zdroj (povolené: gps)."
    assert validation_text({"loc": ("body", "t"), "type": "string_too_short", "ctx": {"min_length": 10}}) == \
        "Pole „t“: musí mať aspoň 10 znakov."
    assert validation_text({"loc": ("body", "text"), "type": "string_too_short", "ctx": {"min_length": 1}}) == \
        "Pole „text“: musí mať aspoň 1 znak."
    assert validation_text({"loc": ("body", "list[PositionIn]", 1, "lat"), "type": "missing"}) == \
        "Položka 2, pole „lat“: chýba."
    assert validation_text({"loc": ("query", "od"), "type": "something_new"}) == \
        "Parameter adresy „od“: má neplatnú hodnotu."
    assert validation_text({"loc": ("body", 17), "type": "json_invalid"}) == "Údaje: nie je platný JSON."


def test_chyba_422_davka_poloh_po_slovensky(client):
    """Dávka polôh (zoznam) s chybnou položkou: chyba konkrétnej položky, nie „musí byť objekt“."""
    bad = client.post("/api/pozicie", headers={"Authorization": "Bearer test-token"},
                      json=[{"train_number": "1", "lat": 48, "lon": 18}, {"train_number": "1", "lat": 48}])
    assert bad.status_code == 422
    assert bad.json()["detail"] == "Neplatné údaje – Položka 2, pole „lon“: chýba.", bad.json()


def test_export_csv_slovenske_stavy_a_desatinna_ciarka(client, fixed_now):
    from vlaky.db import session_scope
    from vlaky.models import BorderCrossing, ExtractedEvent, Handover

    tid = make_train(number="81501")
    base = {"route": "Čadca – Žilina", "depart": fixed_now.isoformat(), "kind": "VYPRAVIŤ", "status": "ZELENÁ"}
    make_recommendation(tid, chosen={**base, "expected_cost": 1683.0})
    make_recommendation(tid, chosen={**base, "expected_cost": 1234.5})
    with session_scope() as s:
        kuty = s.scalar(select(BorderCrossing).where(BorderCrossing.code == "KUTY"))
        s.add_all([
            Handover(train_id=tid, border_id=kuty.id, direction="vstup", status="ocakavany",
                     avizo_eta_utc=fixed_now + timedelta(hours=1)),
            Handover(train_id=tid, border_id=kuty.id, direction="vystup", status="prevzaty",
                     arrived_utc=fixed_now - timedelta(hours=3), taken_over_utc=fixed_now - timedelta(hours=2)),
            ExtractedEvent(kind="vyluka_nova", status="aplikovane", confidence=0.85, extractor="pravidla",
                           needs_confirmation=False, created_at=fixed_now),
            ExtractedEvent(kind="vyluka_zmena", status="zamietnute", confidence=0.5, extractor="llm",
                           created_at=fixed_now),
            ExtractedEvent(kind="avizo", status="navrh", confidence=0.6, needs_confirmation=True, created_at=fixed_now),
            ExtractedEvent(kind="ine", status="navrh", confidence=0.3, needs_confirmation=False,
                           created_at=fixed_now)])
    login_as(client, "veduci5", "veduci")
    period = {"od": "2026-11-01", "do": "2026-11-02"}

    def rows(typ: str) -> list:
        r = client.get(f"/reporty/export/{typ}.csv", params=period)
        assert r.status_code == 200
        return list(csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))

    recs = rows("odporucania")
    cost = recs[0].index("očakávaný náklad €")
    assert sorted(r[cost] for r in recs[1:]) == ["1234,5", "1683"], recs
    handovers = rows("preberania")
    head = handovers[0]
    got = sorted((r[head.index("smer")], r[head.index("stav")]) for r in handovers[1:])
    assert got == [("vstup", "očakávaný"), ("výstup", "prevzatý")], got
    events = rows("udalosti")
    head = events[0]
    got = sorted((r[head.index("stav")], r[head.index("extraktor")]) for r in events[1:])
    assert got == [("informačná (bez potvrdenia)", "pravidlá"), ("zamietnutá", "jazykový model (LLM)"),
                   ("zapísaná do registra", "pravidlá"), ("čaká na potvrdenie", "pravidlá")], got
    raw = "".join(";".join(r) for r in recs + handovers + events)
    for code in ("ocakavany", "prevzaty", "aplikovane", "zamietnute", "navrh", "1683.0", "1234.5"):
        assert code not in raw, code


def test_datum_jazdy_po_slovensky(client, fixed_now):
    tid = make_train(number="81502", run_date="2026-11-02")
    login_as(client, "d532")
    detail = client.get(f"/vlaky/{tid}").text
    assert re.search(r"<dt>Dátum jazdy</dt><dd>02\.11\.2026</dd>", detail)
    listing = client.get("/vlaky").text
    row = re.search(r"<tr[^>]*>(?:(?!</tr>).)*81502(?:(?!</tr>).)*</tr>", listing, re.S).group(0)
    assert "02.11.2026" in row and "2026-11-02" not in row, row
