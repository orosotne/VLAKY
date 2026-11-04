"""Regresné testy ku kontrole opravy R5U0 a R5U6 (piata revízia webu, oblasť D – web5).

Živý stav otvorenej stránky (static/js/app.js; správanie v prehliadači overuje test_revizia5_web5_zivy_prehliadac.py):
  - odpoveď na dopyt otvorenej stránky nesie čas servera a aktuálne časy údajov (poloha, kontrola schránky, cyklus
    agenta), ku ktorým stránka „pred N min“ znova ukotví – vek inak rástol od načítania stránky aj pri bežiacom agentovi,
  - „Zostáva do limitu“ (13 h) na detaile odporúčania sa odpočítava k limitu, nie je zmrazené z času načítania,
  - stránky nesú hlavné údaje (semafor, rozhodnutie, stav…), podľa ktorých otvorená stránka odlíši podstatnú zmenu
    (zníženie semaforu pri výpadku agenta) od bežného prepočtu s rovnakým výsledkom.
Export CSV udalostí: zapísaná automaticky (bez potvrdenia dispečera) nie je „zapísaná do registra“ ako potvrdená.
"""
from __future__ import annotations

import csv
import html
import io
import json
import re
from datetime import timedelta

from test_web_spolocne import csrf, login_as, make_recommendation, make_train

pytest_plugins = ["test_web_spolocne"]

STATE = {"X-Vlaky-Stav": "1"}


# ------------------------------------------------------------------ pomôcky
def _agent_at(at) -> None:
    from vlaky.agent.zdravie import set_kv
    from vlaky.db import session_scope

    with session_scope() as s:
        set_kv(s, "agent_last_cycle", {"ts": at.isoformat(), "errors": 0})
        set_kv(s, "mail_last_ok", {"ts": at.isoformat(), "fetched": 0})


def _position(train_id: int, number: str, at) -> None:
    from vlaky.db import session_scope
    from vlaky.models import PositionReport

    with session_scope() as s:
        s.add(PositionReport(train_id=train_id, train_number=number, ts_utc=at, lat=49.4381, lon=18.7880, source="gps",
                             speed_kmh=0))


def _rec(t0, number: str, **kw) -> tuple:
    """Plánovaný vlak s aktuálnym odporúčaním VYPRAVIŤ ZELENÁ (dáta agenta k t0 čerstvé)."""
    tid = make_train(number=number, status="planovany", origin="Čadca", destination="Žilina",
                     ready_utc=t0 + timedelta(hours=1), deadline_utc=t0 + timedelta(hours=10))
    chosen = {"route": "Čadca – Žilina", "depart": (t0 + timedelta(hours=1)).isoformat(), "kind": "VYPRAVIŤ",
              "status": "ZELENÁ", "p_on_time": 0.97, "p_hold": 0.0}
    values = dict(decision="VYPRAVIŤ", color="ZELENÁ", priority="INFO", chosen=chosen, variants=[dict(chosen)],
                  reasons=[], summary=f"Vlak {number}: VYPRAVIŤ (FIKTÍVNE).",
                  data_age={"mailbox_min": 0.0, "computed_at": t0.isoformat()})
    values.update(kw)
    return tid, make_recommendation(tid, **values)


def _head(page: str) -> str:
    m = re.search(r'<div class="page-head"[^>]*>', page)
    assert m, "Stránka nemá hlavičku so živým stavom"
    return m.group(0)


def _attr(fragment: str, name: str):
    m = re.search(name + r'="([^"]*)"', fragment)
    return html.unescape(m.group(1)) if m else None


def _summary(page: str) -> dict:
    raw = _attr(_head(page), "data-zivy-hlavne")
    assert raw, "Stránka nemá hlavné údaje (data-zivy-hlavne)"
    return dict(json.loads(raw))


def _dt(page: str, label: str) -> str:
    m = re.search(r"<dt[^>]*>" + re.escape(label) + "</dt>", page)
    assert m, f"Na stránke chýba {label!r}"
    return m.group(0)


def _dd(page: str, label: str) -> str:
    i = page.find(_dt(page, label))
    j = page.find("<dd>", i)
    return page[j:page.find("</dd>", j)]


def _iso(dt) -> str:
    return dt.isoformat()


# ================================================================== vek údajov sa znova ukotví k serveru
def test_dopyt_otvorenej_stranky_vracia_cas_servera_a_aktualny_vek_udajov(client, fixed_now):
    from vlaky.cas import set_fake_now

    t0 = fixed_now
    _agent_at(t0)
    tid, rid = _rec(t0, "95020")
    _position(tid, "95020", t0 - timedelta(minutes=2))
    login_as(client, "d541")
    page = client.get(f"/odporucania/{rid}").text
    assert _attr(_head(page), "data-teraz") == _iso(t0)  # čas servera pri vykreslení
    for label, key, at in (("Poloha vlaku", "poloha", t0 - timedelta(minutes=2)),
                           ("Posledná kontrola schránky", "schranka", t0), ("Posledný cyklus agenta", "cyklus", t0)):
        dt = _dt(page, label)
        assert _attr(dt, "data-vek-kluc") == key and _attr(dt, "data-vek") == _iso(at), dt
    assert _attr(_dt(page, "Posledná kontrola schránky"), "data-vek-limit") == "30"
    assert _attr(_dt(page, "Posledná kontrola schránky"), "data-vek-odznak") == "staršia ako 30 min"
    state = re.search(r'data-zivy-stav="([0-9a-f]+)"', page).group(1)

    # o 20 min: agent beží (cyklus a schránka práve teraz), nová poloha pred minútou – stránka sa nemení (rovnaký
    # odtlačok), ale vek údajov sa ukotví k novým časom zo servera
    set_fake_now(t0 + timedelta(minutes=20))
    _agent_at(t0 + timedelta(minutes=20))
    _position(tid, "95020", t0 + timedelta(minutes=19))
    body = client.get(f"/odporucania/{rid}", headers=STATE).json()
    assert body["stav"] == state
    assert body["teraz"] == _iso(t0 + timedelta(minutes=20))
    assert body["vek"] == {"poloha": _iso(t0 + timedelta(minutes=19)), "schranka": _iso(t0 + timedelta(minutes=20)),
                           "cyklus": _iso(t0 + timedelta(minutes=20))}, body

    # staršie (nie aktuálne) odporúčanie: vek k teraz neukazuje, nič sa neukotvuje
    make_recommendation(tid, decision="POČKAŤ", color="ORANŽOVÁ")  # novšie odporúčanie vlaku 95020
    body = client.get(f"/odporucania/{rid}", headers=STATE).json()
    assert body["stav"] != state and body["vek"] == {}, body


def test_ostatne_zive_stranky_vracaju_cas_servera(client, fixed_now):
    from vlaky.cas import set_fake_now

    tid, _rid = _rec(fixed_now, "95022")
    _agent_at(fixed_now)
    login_as(client, "d542")
    set_fake_now(fixed_now + timedelta(minutes=7))
    for url in (f"/vlaky/{tid}", "/odporucania", "/vlaky"):
        page = client.get(url).text
        assert _attr(_head(page), "data-teraz") == _iso(fixed_now + timedelta(minutes=7)), url
        body = client.get(url, headers=STATE).json()
        assert body["teraz"] == _iso(fixed_now + timedelta(minutes=7)) and body["stav"], (url, body)


# ================================================================== „Zostáva do limitu“ sa odpočítava
def test_zostava_do_limitu_sa_na_otvorenej_stranke_odpocitava(client, fixed_now):
    t0 = fixed_now
    _agent_at(t0)
    crew = {"driver": "Ján FIKTÍVNY", "start": (t0 - timedelta(hours=1)).isoformat(),
            "limit": (t0 + timedelta(hours=12)).isoformat(), "left_min": 720, "status": "ZELENÁ", "max_duty_h": 13.0,
            "end_p50": (t0 + timedelta(hours=3)).isoformat(), "end_p90": (t0 + timedelta(hours=4)).isoformat()}
    _tid, rid = _rec(t0, "95023", crew=crew)
    login_as(client, "d543")
    page = client.get(f"/odporucania/{rid}").text
    dt = _dt(page, "Zostáva do limitu")  # odpočet k limitu: kotva na <dt>, prvý údaj <dd> prepíše skript stránky
    assert _attr(dt, "data-countdown-limit") == _iso(t0 + timedelta(hours=12)), dt
    assert float(_attr(dt, "data-limit-min")) == 780, dt
    assert _dd(page, "Zostáva do limitu").startswith("<dd>12 h 00 min"), _dd(page, "Zostáva do limitu")

    # rušňovodič ešte nenastúpil (nástup v budúcnosti) – limit nebeží, žiadny odpočet
    waiting = {**crew, "start": (t0 + timedelta(hours=2)).isoformat(), "limit": (t0 + timedelta(hours=15)).isoformat()}
    _tid, rid2 = _rec(t0, "95024", crew=waiting)
    page = client.get(f"/odporucania/{rid2}").text
    assert "data-countdown" not in _dt(page, "Zostáva do limitu")
    assert "začne plynúť nástupom" in _dd(page, "Zostáva do limitu")
    # staršie odporúčanie: hodnota pri prepočte, žiadny odpočet k teraz
    tid3, rid3 = _rec(t0, "95025", crew=crew)
    make_recommendation(tid3, decision="POČKAŤ", color="ORANŽOVÁ")
    page = client.get(f"/odporucania/{rid3}").text
    assert "data-countdown" not in _dt(page, "Zostáva do limitu")
    assert "(pri prepočte)" in _dd(page, "Zostáva do limitu")
    # skript stránky pozná odpočet k limitu
    assert "data-countdown-limit" in client.get("/static/js/app.js").text


# ================================================================== hlavné údaje: podstatná zmena vs. bežná obnova
def test_hlavne_udaje_odlisia_zmenu_semaforu_od_bezneho_prepoctu(client, fixed_now):
    from vlaky.cas import set_fake_now
    from vlaky.db import session_scope
    from vlaky.models import Recommendation

    t0 = fixed_now
    _agent_at(t0)
    tid, rid = _rec(t0, "95026")
    login_as(client, "d544")
    urls = {"detail": f"/odporucania/{rid}", "vlak": f"/vlaky/{tid}", "odporucania": "/odporucania",
            "vlaky": "/vlaky"}

    def pages() -> dict:
        return {k: client.get(u).text for k, u in urls.items()}

    before = pages()
    detail = _summary(before["detail"])
    assert detail["Semafor"] == "ZELENÁ" and detail["Rozhodnutie"] == "VYPRAVIŤ", detail
    assert detail["Stav odporúčania"] == "nové" and detail["Údaje agenta"] == "čerstvé", detail
    assert detail["Posledné odporúčanie vlaku"] == "áno", detail
    assert _summary(before["vlak"])["Semafor"] == "ZELENÁ"
    row_label = next(k for k in _summary(before["odporucania"]) if k.startswith("Vlak 95026 ("))
    assert _summary(before["odporucania"])[row_label] == "VYPRAVIŤ, ZELENÁ, nové"
    assert _summary(before["vlaky"])["Vlak 95026 (02.11.2026)"] == "plánovaný, VYPRAVIŤ, ZELENÁ"

    # bežný prepočet s rovnakým výsledkom: odtlačok sa zmení (stránka sa obnoví), hlavné údaje nie
    with session_scope() as s:
        rec = s.get(Recommendation, rid)
        rec.data_age = {**(rec.data_age or {}), "computed_at": (t0 + timedelta(minutes=15)).isoformat()}
    routine = pages()
    for key in ("detail", "vlak", "odporucania"):
        assert _attr(_head(routine[key]), "data-zivy-stav") != _attr(_head(before[key]), "data-zivy-stav"), key
        assert _summary(routine[key]) == _summary(before[key]), key

    # výpadok agenta: zmena semaforu v hlavných údajoch každej stránky
    set_fake_now(t0 + timedelta(minutes=45))
    outage = pages()
    shown = "ORANŽOVÁ (bez čerstvých údajov agenta)"
    detail = _summary(outage["detail"])
    assert detail["Semafor"] == shown and detail["Údaje agenta"] == "nie sú čerstvé", detail
    assert _summary(outage["vlak"])["Semafor"] == shown
    assert _summary(outage["odporucania"])[row_label] == f"VYPRAVIŤ, {shown}, nové"
    assert _summary(outage["vlaky"])["Vlak 95026 (02.11.2026)"] == f"plánovaný, VYPRAVIŤ, {shown}"

    # rozhodnutie iného dispečera je podstatná zmena (stav odporúčania aj s menom)
    token = csrf(client, f"/odporucania/{rid}")
    client.post(f"/odporucania/{rid}/rozhodnutie", data={"csrf_token": token, "akcia": "zamietnut",
                                                          "poznamka": "ŽSR pridelila inú trasu"})
    assert _summary(client.get(f"/odporucania/{rid}").text)["Stav odporúčania"] == "zamietnuté (d544)"


# ================================================================== export CSV: stav udalosti ako vo fronte
def test_export_csv_automaticky_zapisane_udalosti_nie_su_potvrdene(client, fixed_now):
    from vlaky.agent.zdravie import queue_counts
    from vlaky.db import session_scope
    from vlaky.models import Closure, ExtractedEvent

    with session_scope() as s:
        events = {
            "auto_navrh": ExtractedEvent(kind="vyluka_nova", status="aplikovane", confidence=0.9, needs_confirmation=False,
                                         created_at=fixed_now),
            "auto_overena": ExtractedEvent(kind="pomala_jazda", status="aplikovane", confidence=0.9,
                                           needs_confirmation=False, created_at=fixed_now),
            "auto_avizo": ExtractedEvent(kind="avizo", status="aplikovane", confidence=0.9, needs_confirmation=False,
                                         created_at=fixed_now),
            "potvrdena_zapisana": ExtractedEvent(kind="vyluka_nova", status="aplikovane", confidence=0.7,
                                                 confirmed_by="d545", confirmed_at=fixed_now, created_at=fixed_now),
            "zapis_zlyhal": ExtractedEvent(kind="avizo", status="potvrdene", confidence=0.7, confirmed_by="d545",
                                           confirmed_at=fixed_now, created_at=fixed_now,
                                           validation={"apply_result": {"ok": False, "error": "vlak nie je v systéme"}}),
            "potvrdena": ExtractedEvent(kind="ine", status="potvrdene", confidence=0.7, confirmed_by="d545",
                                        confirmed_at=fixed_now, created_at=fixed_now,
                                        validation={"apply_result": {"ok": True}}),
        }
        s.add_all(events.values())
        s.flush()
        span = {"start_utc": fixed_now + timedelta(days=1), "end_utc": fixed_now + timedelta(days=1, hours=6)}
        s.add_all([Closure(kind="uplna", from_station="Čadca", to_station="Žilina", status="navrh",
                           source_event_id=events["auto_navrh"].id, **span),
                   Closure(kind="pomala_jazda", from_station="Kysak", to_station="Košice", status="overene",
                           speed_kmh=30, source_event_id=events["auto_overena"].id, **span),
                   Closure(kind="uplna", from_station="Púchov", to_station="Žilina", status="overene",
                           source_event_id=events["potvrdena_zapisana"].id, **span)])
        ids = {k: e.id for k, e in events.items()}
        auto_in_queue = queue_counts(s)["auto"]
    login_as(client, "veduci6", "veduci")
    r = client.get("/reporty/export/udalosti.csv", params={"od": "2026-11-01", "do": "2026-11-02"})
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))
    head = rows[0]
    state = {int(row[0]): row[head.index("stav")] for row in rows[1:]}
    assert state[ids["auto_navrh"]] == "zapísaná automaticky – výluka čaká na overenie", state
    assert state[ids["auto_overena"]] == "zapísaná automaticky (bez potvrdenia)", state
    assert state[ids["auto_avizo"]] == "zapísaná automaticky (bez potvrdenia)", state
    assert state[ids["potvrdena_zapisana"]] == "zapísaná do registra", state
    assert state[ids["zapis_zlyhal"]] == "potvrdená – zápis zlyhal (čaká vo fronte)", state
    assert state[ids["potvrdena"]] == "potvrdená", state
    # rovnaké počítanie ako fronta na potvrdenie (Zapísané automaticky – výluka čaká na overenie)
    waiting = sum(1 for v in state.values() if v == "zapísaná automaticky – výluka čaká na overenie")
    assert auto_in_queue == waiting == 1
