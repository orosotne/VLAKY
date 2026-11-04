"""Regresné testy k 4. revízii (W4, QU3) – slovenské texty a zobrazenie na webe:

- /zdravie bez surových kľúčov („konfiguracia“, „cyklus“, „schranka“),
- každá akcia v audite má slovenský popis,
- stĺpec „Rušňovodič (do 13 h)“ a okno vlaku: bez prideleného rušňovodiča alebo pred nástupom žiadny odpočet
  hypotetického rušňovodiča (napr. „63 h 17 min“), ale čas nástupu,
- kroky pri idúcom vlaku (POKRAČOVAŤ) nehovoria „vypraviť“,
- čísla s desatinnou čiarkou („12,5 h“), správny pád („najďalej po dopravňu …“).
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from test_web_spolocne import csrf, login_as, make_recommendation, make_train
from vlaky.agent.zdravie import record
from vlaky.cas import fmt

pytest_plugins = ["test_web_spolocne"]

ROOT = Path(__file__).resolve().parent.parent


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


# ------------------------------------------------------------------ /zdravie a audit
def test_zdravie_bez_surovych_klucov(client, cfg, fixed_now):
    from vlaky.db import session_scope

    with session_scope() as s:
        record(s, "cyklus", "ok")
        record(s, "schranka", "ok")
        record(s, "planovanie", "warn")
    login_as(client, "veduci1", "veduci")
    page = client.get("/zdravie").text
    text = _text(page)
    assert "Nastavenie odosielania správ" in text and "konfiguracia" not in text
    comps = page.split("Komponenty agenta")[1].split("</table>")[0]
    cells = [_text(c) for c in re.findall(r"<td><strong[^>]*>(.*?)</strong></td>", comps, re.S)]
    assert set(cells) == {"Cyklus agenta", "Schránka (kontrola e-mailov)", "Plánovanie (prepočet vlakov)"}


def _audit_codes() -> dict:
    """Všetky kódy akcií auditu v kóde (tretí argument volania audit(...)): kód → miesto."""
    codes: dict = {}
    for path in (ROOT / "vlaky").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 3:
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            arg = node.args[2]
            if name == "audit" and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                codes.setdefault(arg.value, f"{path.relative_to(ROOT)}:{node.lineno}")
    return codes


def test_kazda_akcia_auditu_ma_slovensky_popis(client, cfg, fixed_now):
    from vlaky.audit import audit
    from vlaky.db import session_scope
    from vlaky.web.common import AUDIT_ACTION

    codes = _audit_codes()
    assert len(codes) > 50
    missing = {c: where for c, where in codes.items() if c not in AUDIT_ACTION}
    assert not missing, missing
    with session_scope() as s:
        audit(s, "agent", "zmeny_zrusenej_vyluky_odstranene", "Closure", 4, udalost=7, zmeny=[3])
        audit(s, "agent", "reply_automatic", "email", 5, reason="neprítomnosť")
    login_as(client, "veduci1", "veduci")
    text = _text(client.get("/audit").text)
    assert "dočasné záznamy zmien zrušenej výluky odstránené" in text
    assert "automatická odpoveď na e-mail agenta (nespracovaná)" in text
    assert "zmeny zrusenej vyluky odstranene" not in text and "reply automatic" not in text


# ------------------------------------------------------------------ rušňovodič (do 13 h) bez odpočtu pred nástupom
def test_vlak_bez_rusnovodica_neukazuje_odpocet_13_h(client, cfg, fixed_now):
    """Vlak bez zmeny rušňovodiča (odchod o 2,5 dňa): odporúčanie opisuje potrebný nástup nového rušňovodiča –
    tabuľka, okno vlaku (API) ani nástenka neukážu „do limitu 13 h: 72 h 00 min“, ale „nepriradený“ a nástup."""
    start = fixed_now + timedelta(hours=59)
    tid = make_train(number="47250", status="planovany", ready_utc=fixed_now + timedelta(hours=60),
                     planned_departure_utc=fixed_now + timedelta(hours=60))
    make_recommendation(tid, decision="VYPRAVIŤ", color="ZELENÁ", priority="INFO",
                        crew={"driver": "", "start": start.isoformat(),
                              "limit": (start + timedelta(hours=13)).isoformat(), "status": "ZELENÁ",
                              "relieve_by": (start + timedelta(hours=12, minutes=30)).isoformat(),
                              "left_min": 4320, "max_duty_h": 13.0})
    login_as(client, "disp1")
    drv = next(r for r in client.get("/api/vlaky").json()["vlaky"] if r["number"] == "47250")["driver"]
    assert drv["left_min"] is None and drv["not_started"] is True and drv["name"] == ""
    assert drv["start"] == start.isoformat()
    row = _text(re.search(r'<tr data-id="%d"[^>]*>(.*?)</tr>' % tid, client.get("/").text, re.S).group(1))
    assert "72 h" not in row and "nepriradený" in row and f"potrebný nástup o {fmt(start)}" in row


def test_naplanovana_zmena_pred_nastupom_bez_odpoctu(client, cfg, fixed_now):
    """Naplánovaná zmena s nástupom o 2 h: odpočet „do 13 h“ ešte nebeží (nie 15 h), ukáže sa čas nástupu."""
    from vlaky.db import session_scope
    from vlaky.models import Driver, DriverShift

    tid = make_train(number="47251", status="planovany", ready_utc=fixed_now + timedelta(hours=3))
    start = fixed_now + timedelta(hours=2)
    with session_scope() as s:
        d = Driver(name="Ján FIKTÍVNY")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=tid, start_utc=start, status="planovana"))
    login_as(client, "disp1")
    drv = next(r for r in client.get("/api/vlaky").json()["vlaky"] if r["number"] == "47251")["driver"]
    assert drv["name"] == "Ján FIKTÍVNY" and drv["left_min"] is None and drv["not_started"] is True
    row = _text(re.search(r'<tr data-id="%d"[^>]*>(.*?)</tr>' % tid, client.get("/").text, re.S).group(1))
    assert f"nástup o {fmt(start)}" in row and "potrebný" not in row and "15 h" not in row


def _js_functions(path: Path, names: list) -> str:
    """Zdrojový text funkcií zo súboru JS (od „function meno(“ po zodpovedajúcu uzatváraciu zátvorku)."""
    src = path.read_text(encoding="utf-8")
    out = []
    for name in names:
        i = src.index(f"function {name}(")
        depth, j = 0, src.index("{", i)
        while True:
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(src[i:j + 1])
    return "\n".join(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="node nie je k dispozícii")
def test_okno_vlaku_a_tabulka_v_js_bez_odpoctu_pred_nastupom(tmp_path):
    """Skutočné funkcie z mapa.js (okno vlaku a stĺpec tabuľky) v Node.js: pred nástupom čas nástupu, nie odpočet;
    rýchlosť pomalej jazdy s desatinnou čiarkou."""
    js = _js_functions(ROOT / "vlaky/web/static/js/mapa.js", ["fmtNum", "crewText", "crewLabel"])
    script = tmp_path / "t.js"
    script.write_text(
        "function fmt(iso, withDate) { return 'F[' + iso + ']'; }\n"
        "function fmtMin(m) { return 'M[' + m + ']'; }\n" + js + "\n"
        "const cases = [\n"
        "  {name: '', start: 'S', left_min: null, not_started: true},\n"
        "  {name: 'Ján', start: 'S', left_min: null, not_started: true},\n"
        "  {name: 'Ján', left_min: 125},\n"
        "  {name: 'neznámy', unknown_start: true, relieve_note: 'odhad: X', left_min: null},\n"
        "];\n"
        "console.log(JSON.stringify({label: cases.map(crewLabel), text: cases.map(crewText),\n"
        "  num: [fmtNum(37.5), fmtNum(50), fmtNum('')]}));\n", encoding="utf-8")
    out = json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, check=True,
                                    timeout=60).stdout)
    assert out["label"] == ["nepriradený · potrebný nástup o F[S]", "Ján · nástup o F[S]",
                            "Ján · do limitu 13 h: M[125]", "neznámy"]
    assert out["text"] == ["potrebný nástup o F[S]", "nástup o F[S]", "M[125]", "odhad: X"]
    assert out["num"] == ["37,5", "50", "—"]
    src = (ROOT / "vlaky/web/static/js/mapa.js").read_text(encoding="utf-8")
    assert "najďalej po dopravňu '" in src and "najďalej po ' +" not in src
    assert "'pomalá jazda ' + (p.rychlost ? fmtNum(p.rychlost)" in src
    app = (ROOT / "vlaky/web/static/js/app.js").read_text(encoding="utf-8")
    assert "String(Math.round(limit / 60 * 10) / 10).replace('.', ',')" in app


# ------------------------------------------------------------------ kroky pre dispečera pri idúcom vlaku
def test_kroky_iduceho_vlaku_nehovoria_vypravit(client, cfg, fixed_now):
    tid = make_train(number="73300", status="v_jazde")
    presumed = make_recommendation(
        tid, decision="POKRAČOVAŤ", color="ORANŽOVÁ", priority="DÔLEŽITÉ",
        summary="Vlak 73300: POKRAČOVAŤ – podľa plánu by už mal byť v cieli (FIKTÍVNE).",
        chosen={"route": "Čadca – Žilina", "depart": fixed_now.isoformat(), "kind": "VYPRAVIŤ", "status": "ORANŽOVÁ",
                "v_cieli": True, "arrival_floating": True, "prichod_v_cieli": None},
        path_proposal={}, variants=[], windows={},
        crew={"driver": "neznámy (chýba záznam o nástupe)", "unknown_start": True, "status": "ORANŽOVÁ",
              "max_duty_h": 13.0})
    login_as(client, "disp1")
    text = _text(client.get(f"/odporucania/{presumed}").text)
    assert "vypraviť len po výslovnom potvrdení" not in text
    assert "Overiť polohu vlaku (rušňovodič, ŽSR); po príchode do cieľa ukončiť jazdu" in text
    assert "ORANŽOVÁ: vlak je v jazde – preveriť dôvody nižšie" in text
    assert "Zadať skutočný nástup rušňovodiča" in text
    arrived = make_recommendation(
        tid, decision="POKRAČOVAŤ", color="ZELENÁ", priority="INFO", summary="Vlak 73300: v cieli (FIKTÍVNE).",
        chosen={"route": "Čadca – Žilina", "depart": fixed_now.isoformat(), "kind": "VYPRAVIŤ", "status": "ZELENÁ",
                "v_cieli": True, "prichod_v_cieli": fixed_now.isoformat()},
        path_proposal={}, variants=[], windows={}, crew={})
    text = _text(client.get(f"/odporucania/{arrived}").text)
    assert "Ukončiť jazdu vlaku (stav „ukončený“) – vlak je v cieli." in text
    # vlak pred odchodom: ORANŽOVÁ stále znamená vypraviť len po potvrdení
    other = make_train(number="73301", status="planovany")
    rid = make_recommendation(other)
    assert "ORANŽOVÁ: vypraviť len po výslovnom potvrdení dispečera" in _text(client.get(f"/odporucania/{rid}").text)


# ------------------------------------------------------------------ desatinná čiarka na stránke Rušňovodiči
def test_limit_v_hodinach_s_desatinnou_ciarkou_na_strane_rusnovodicov(client, cfg, fixed_now):
    """Odhad plánovača pri limite 12,5 h (riadok naplánovanej zmeny idúceho vlaku): „12,5 h“, nie „12.5 h“."""
    from vlaky.db import session_scope
    from vlaky.models import Driver, DriverShift

    tid = make_train(number="73302", status="v_jazde")
    make_recommendation(tid, decision="POKRAČOVAŤ",
                        crew={"driver": "R. FIKTÍVNY (chýba záznam o nástupe)", "unknown_start": True,
                              "status": "ORANŽOVÁ", "max_duty_h": 12.5})
    with session_scope() as s:
        d = Driver(name="R. FIKTÍVNY")
        s.add(d)
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=tid, start_utc=fixed_now + timedelta(hours=2), status="planovana"))
    login_as(client, "disp1")
    page = client.get("/rusnovodici").text
    row = _text(re.search(r"<td><strong>R\. FIKTÍVNY</strong></td>(.*?)</tr>", page.split('id="h-zmeny"')[1],
                          re.S).group(1))
    assert "limit 12,5 h sa nedá overiť – zadajte nástup rušňovodiča" in row and "12.5" not in row


# ------------------------------------------------------------------ desatinná čiarka na stránke Nastavenia
def test_nastavenia_cisla_s_desatinnou_ciarkou(client, cfg, fixed_now):
    """Predvolené hodnoty parametrov, kalibrované hodnoty a prah istoty extrakcie sa zobrazia po slovensky
    („0,9“, „30“, „6,5“), nie „0.9“, „30.0“ či „6.5“ (hodnoty vo formulári ostávajú strojovo čitateľné)."""
    from vlaky.db import session_scope
    from vlaky.models import ParameterSet

    with session_scope() as s:
        s.add(ParameterSet(values={"delay_p50_min_per_100km": 6.5}, created_by="kalibracia", note="FIKTÍVNE",
                           valid_from=fixed_now - timedelta(minutes=5)))
    login_as(client, "veduci1", "veduci")
    text = _text(client.get("/nastavenia").text)
    assert "p_on_time_green · predvolené 0,9" in text and "predvolené 0.9" not in text
    assert "buffer_before_closure_min · predvolené 30" in text and "predvolené 30.0" not in text
    assert "delay_p50_min_per_100km 6,5" in text
    assert "Prah istoty extrakcie (pod ním ide udalosť na potvrdenie) 0,90" in text


# ------------------------------------------------------------------ pády a čas v hláškach, stav agenta po slovensky
def test_statie_vlaku_pri_dopravni_v_spravnom_pade(session, fixed_now):
    """Upozornenie na státie: „stojí 30 min pri dopravni Žilina“, nie „pri Žilina“."""
    from sqlalchemy import select

    from vlaky.agent.strazenie import standing_alerts
    from vlaky.models import Notification, PositionReport, Train

    t = Train(number="7", origin="Čadca", destination="Košice", status="v_jazde")
    session.add(t)
    session.flush()
    for k in range(4):
        session.add(PositionReport(train_id=t.id, ts_utc=fixed_now - timedelta(minutes=30 - k * 10), lat=49.2265,
                                   lon=18.7395, source="gps", nearest_station="Žilina"))
    session.flush()
    assert standing_alerts(session, fixed_now) == 1
    n = session.scalar(select(Notification).where(Notification.channel == "email"))
    assert n.subject.endswith("stojí 30 min pri dopravni Žilina") and "pri Žilina" not in n.subject + n.body


def test_zmena_z_navrhu_hlasi_cas_po_slovensky(client, cfg, fixed_now):
    """Hláška po vytvorení zmeny z návrhu nasadenia uvádza miestny čas („02.11. 07:00“), nie ISO v UTC."""
    from vlaky.db import session_scope
    from vlaky.models import Driver

    tid = make_train(number="47252", status="planovany")
    with session_scope() as s:
        d = Driver(name="Ján FIKTÍVNY")
        s.add(d)
        s.flush()
        did = d.id
    login_as(client, "disp1")
    start = fixed_now + timedelta(hours=2)
    r = client.post("/zmeny/z-navrhu", data={"csrf_token": csrf(client, "/rusnovodici"), "driver_id": did,
                                              "train_id": tid, "start": start.isoformat(),
                                              "end": (start + timedelta(hours=8)).isoformat()}, follow_redirects=True)
    assert f"Zmena pre Ján FIKTÍVNY na vlak 47252 naplánovaná od {fmt(start)}." in r.text
    assert start.isoformat() not in _text(r.text)


def test_stav_agenta_po_slovensky_pre_dennu_spravu():
    """Riadky stavu agenta bez surových kľúčov (pre dennú správu) – rovnaké popisy ako na /zdravie."""
    from vlaky.agent.zdravie import detail_lines
    from vlaky.web.common import HEALTH_DETAIL

    lines = detail_lines({"db": "ok", "cyklus": "pred 2 min", "schranka": "pred 3 min",
                          "konfiguracia": "ok", "dorucenie": "ok", "stav": "OK"})
    assert lines == ["Databáza: v poriadku", "Posledný cyklus agenta: pred 2 min",
                     "Posledná kontrola schránky: pred 3 min", "Nastavenie odosielania správ: v poriadku",
                     "Doručovanie správ: v poriadku", "Stav: OK"]
    assert HEALTH_DETAIL["konfiguracia"] == "Nastavenie odosielania správ"


# ------------------------------------------------------------------ detail odporúčania: odpočet len po nástupe
def test_detail_odporucania_bez_odpoctu_pred_nastupom(client, cfg, fixed_now):
    """Detail odporúčania: nepridelený rušňovodič (potrebný nástup o 59 h) ani naplánovaná zmena pred nástupom
    nemajú „Zostáva do limitu 72 h 00 min“; rušňovodič v službe odpočet má."""
    def crew_block(rid: int) -> str:
        page = client.get(f"/odporucania/{rid}").text
        return _text(page.split('id="h-crew"')[1].split("</section>")[0])

    login_as(client, "disp1")
    tid = make_train(number="47253", status="planovany")
    start = fixed_now + timedelta(hours=59)
    crew = {"start": start.isoformat(), "limit": (start + timedelta(hours=13)).isoformat(), "status": "ZELENÁ",
            "relieve_by": (start + timedelta(hours=12, minutes=30)).isoformat(), "left_min": 4320,
            "max_duty_h": 13.0}
    rid = make_recommendation(tid, crew=dict(crew, driver=""))
    block = crew_block(rid)
    assert "Zostáva do limitu — (rušňovodič nie je pridelený – limit 13 h začne plynúť nástupom)" in block
    assert "potrebný nástup podľa plánu" in block and "72 h" not in block
    rid = make_recommendation(tid, crew=dict(crew, driver="Ján FIKTÍVNY"))
    block = crew_block(rid)
    assert "Zostáva do limitu — (limit 13 h začne plynúť nástupom)" in block and "72 h" not in block
    on_duty = fixed_now - timedelta(hours=1)
    rid = make_recommendation(tid, crew=dict(crew, driver="Ján FIKTÍVNY", start=on_duty.isoformat(),
                                             limit=(on_duty + timedelta(hours=13)).isoformat()))
    assert "Zostáva do limitu 12 h 00 min" in crew_block(rid)


def _audit_entities() -> dict:
    """Entity (štvrtý argument volania audit(...)) v kóde: kód → miesto."""
    out: dict = {}
    for path in (ROOT / "vlaky").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and len(node.args) >= 4:
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                arg = node.args[3]
                if name == "audit" and isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value:
                    out.setdefault(arg.value, f"{path.relative_to(ROOT)}:{node.lineno}")
    return out


def test_kazda_entita_auditu_ma_slovensky_popis(client, cfg, fixed_now):
    """Stĺpec „Entita“ v audite po slovensky („výluka #4“), kód ostáva viditeľný pre filter."""
    from vlaky.audit import audit
    from vlaky.db import session_scope
    from vlaky.web.common import AUDIT_ENTITY

    entities = _audit_entities()
    assert len(entities) > 10
    missing = {e: where for e, where in entities.items() if e not in AUDIT_ENTITY}
    assert not missing, missing
    with session_scope() as s:
        audit(s, "agent", "zmeny_zrusenej_vyluky_odstranene", "Closure", 4, udalost=7)
    login_as(client, "veduci1", "veduci")
    page = client.get("/audit").text
    row = _text(re.search(r"<tr>\s*<td class=\"nowrap\">[^<]*</td>\s*<td>agent</td>(.*?)</tr>", page, re.S).group(1))
    assert "výluka #4 Closure" in row
