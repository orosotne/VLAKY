"""Regresné testy k 4. revízii (W4, QU0) – rušňovodiči a limit 13 h:

- „Vystriedať najneskôr“ na stránke Rušňovodiči je vždy vlastný limit zmeny mínus odovzdanie rušňa – nikdy čas
  iného rušňovodiča z odporúčania (nový rušňovodič po odstavení, rušňovodič v službe pri vystriedaní),
- zmena „prebieha“ nemôže mať skutočný nástup v budúcnosti (formulár ho odmietne, pár minút sa berie ako teraz),
- upozornenia strážnika na limit 13 h idú podľa toho istého pravidla ako plánovač – aj pri idúcom vlaku bez
  zaznamenaného nástupu (odhad z odporúčania), takže po limite plánovača nemlčia.
"""
from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import select

from test_web_spolocne import csrf, login_as
from vlaky.cas import ensure_utc, fmt, local
from vlaky.hranice import seed_borders
from vlaky.models import Driver, DriverShift, Notification, ParameterSet, Recommendation, Train
from vlaky.planovanie.planovac import evaluate_train

pytest_plugins = ["test_web_spolocne"]


def _local(dt) -> str:
    return local(dt).strftime("%Y-%m-%dT%H:%M")


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _zmeny(page: str) -> dict:
    """Riadky tabuľky Zmeny: meno rušňovodiča → bunky (text) [vlak, nástup, limit, zostáva, vystriedať, stav, …]."""
    part = page.split('id="h-zmeny"')[1].split("</table>")[0]
    out = {}
    for m in re.finditer(r'<tr class="crew-row crew-row-[a-z]+">\s*<td><strong>([^<]+)</strong></td>(.*?)</tr>',
                         part, re.S):
        out[m.group(1)] = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)]
    return out


def _train(s, now, **kw) -> Train:
    data = dict(number="74400", run_date=now.date().isoformat(), origin="Čadca", destination="Žilina",
                traction="E", gauge="1435", vmax_kmh=100, status="v_jazde", deadline_utc=now + timedelta(hours=30))
    data.update(kw)
    t = Train(**data)
    s.add(t)
    s.flush()
    return t


def _shift(s, train, start, status="prebieha", name="Rušňovodič A (fiktívny)") -> DriverShift:
    d = Driver(name=name)
    s.add(d)
    s.flush()
    sh = DriverShift(driver_id=d.id, train_id=train.id if train is not None else None, start_utc=start, status=status)
    s.add(sh)
    s.flush()
    return sh


# ------------------------------------------------------------------ A: vystriedať najneskôr = vlastný limit
def test_vystriedat_najneskor_nie_je_cas_noveho_rusnovodica_po_odstaveni(client, cfg, fixed_now):
    """Rušňovodič v službe a odporúčanie ODSTAVIŤ, ktorého pracovný čas opisuje nového rušňovodiča po odstavení:
    riadok zmeny ukazuje vlastný limit mínus 30 min, nie čas nového rušňovodiča (5 h po vlastnom limite)."""
    from vlaky.db import session_scope

    start = fixed_now - timedelta(hours=1)
    fresh = fixed_now + timedelta(hours=5)  # nový rušňovodič nastúpi po odstavení
    with session_scope() as s:
        t = _train(s, fixed_now, number="74401", status="prevzaty", ready_utc=fixed_now)
        _shift(s, t, start, name="Rušňovodič 6 (fiktívny)")
        s.add(Recommendation(train_id=t.id, created_at=fixed_now, decision="ODSTAVIŤ", color="ZELENÁ",
                             priority="DÔLEŽITÉ", summary="FIKTÍVNE", state="nove", fingerprint="x",
                             crew={"driver": "nový rušňovodič (po odstavení)", "start": fresh.isoformat(),
                                   "limit": (fresh + timedelta(hours=13)).isoformat(), "status": "ČERVENÁ",
                                   "relieve_by": (fresh + timedelta(hours=12, minutes=30)).isoformat(),
                                   "end_p90": (fresh + timedelta(hours=2)).isoformat(), "max_duty_h": 13.0}))
    login_as(client, "disp1")
    row = _zmeny(client.get("/rusnovodici").text)["Rušňovodič 6 (fiktívny)"]
    own_limit = start + timedelta(hours=13)
    assert row[2] == fmt(own_limit)                                  # Limit
    assert row[4] == fmt(own_limit - timedelta(minutes=30))         # Vystriedať najneskôr
    assert fmt(fresh + timedelta(hours=12, minutes=30)) not in " ".join(row)
    assert "koniec P90" not in row[3]                                # koniec P90 nového rušňovodiča tu nepatrí
    # červená farba nového rušňovodiča nepatrí rušňovodičovi v službe (má 12 h rezervu)
    page = client.get("/rusnovodici").text
    assert re.search(r'<tr class="crew-row crew-row-zelena">\s*<td><strong>Rušňovodič 6', page)


def test_vystriedanie_ukazuje_vlastny_limit_aj_pri_odporucani_s_novym_rusnovodicom(client, cfg, fixed_now):
    """Rušňovodič v službe a naplánované vystriedanie; odporúčanie opisuje nového rušňovodiča (nástup v budúcnosti):
    každý riadok má vlastný limit mínus odovzdanie – žiadny riadok nedostane čas z odporúčania."""
    from vlaky.db import session_scope

    a_start, b_start = fixed_now - timedelta(hours=10), fixed_now + timedelta(hours=2)
    fresh = fixed_now + timedelta(hours=1)
    with session_scope() as s:
        t = _train(s, fixed_now, number="74402", status="prevzaty", ready_utc=fixed_now)
        _shift(s, t, a_start, name="Rušňovodič 1 (fiktívny)")
        _shift(s, t, b_start, status="planovana", name="Rušňovodič 2 (fiktívny)")
        s.add(Recommendation(train_id=t.id, created_at=fixed_now, decision="ODSTAVIŤ", color="ORANŽOVÁ",
                             priority="DÔLEŽITÉ", summary="FIKTÍVNE", state="nove", fingerprint="x",
                             crew={"driver": "nový rušňovodič (po odstavení)", "start": fresh.isoformat(),
                                   "limit": (fresh + timedelta(hours=13)).isoformat(), "status": "ZELENÁ",
                                   "relieve_by": (fresh + timedelta(hours=12, minutes=30)).isoformat()}))
    login_as(client, "disp1")
    rows = _zmeny(client.get("/rusnovodici").text)
    for name, start in (("Rušňovodič 1 (fiktívny)", a_start), ("Rušňovodič 2 (fiktívny)", b_start)):
        assert rows[name][4] == fmt(start + timedelta(hours=12, minutes=30)), (name, rows[name])


def test_vystriedat_najneskor_nikdy_po_limite_pri_skutocnom_prepocte(client, cfg, fixed_now):
    """Skutočný prepočet: vlak bez pridelenej trasy, rušňovodič v službe 10 min. Nech plánovač odporučí čokoľvek,
    „Vystriedať najneskôr“ rušňovodiča v službe nie je neskôr ako jeho limit 13 h mínus odovzdanie rušňa."""
    from vlaky.db import session_scope

    start = fixed_now - timedelta(minutes=10)
    with session_scope() as s:
        seed_borders(s)
        t = _train(s, fixed_now, number="95804", status="prevzaty", ready_utc=fixed_now)
        _shift(s, t, start, name="Rušňovodič 6 (fiktívny)")
        evaluate_train(s, t.id, fixed_now, "test")
    login_as(client, "disp1")
    row = _zmeny(client.get("/rusnovodici").text)["Rušňovodič 6 (fiktívny)"]
    assert row[4] == fmt(start + timedelta(hours=12, minutes=30)), row


# ------------------------------------------------------------------ B: skutočný nástup nie je v budúcnosti
def test_zmena_prebieha_s_nastupom_v_buducnosti_sa_odmietne(client, cfg, fixed_now):
    """„Zadať nástup“ (aj „Naplánovať zmenu – prebieha“ a „Upraviť“) s nástupom o 3 h neskôr sa odmietne slovenskou
    hláškou; zmena sa neuloží (odpočet by ukázal viac ako 13 h, plánovač by nástup ignoroval)."""
    from vlaky.db import session_scope

    with session_scope() as s:
        t = _train(s, fixed_now, number="95840", planned_departure_utc=fixed_now - timedelta(hours=2))
        d = Driver(name="Rušňovodič 7 (fiktívny)")
        s.add(d)
        s.flush()
        tid, did = t.id, d.id
    login_as(client, "disp1")
    tok = csrf(client, "/rusnovodici")
    r = client.post("/zmeny/nova", data={"csrf_token": tok, "driver_id": did, "train_id": tid, "status": "prebieha",
                                          "start_utc": _local(fixed_now + timedelta(hours=3))}, follow_redirects=True)
    assert "Skutočný nástup nemôže byť v budúcnosti" in r.text and "Zmena uložená" not in r.text
    with session_scope() as s:
        assert s.scalar(select(DriverShift).where(DriverShift.train_id == tid)) is None
        sh = _shift(s, s.get(Train, tid), fixed_now - timedelta(hours=1), name="Rušňovodič 8 (fiktívny)")
        sid, did8 = sh.id, sh.driver_id
    r = client.post(f"/zmeny/{sid}/upravit", data={"csrf_token": tok, "driver_id": did8, "train_id": tid,
                                                   "status": "prebieha",
                                                   "start_utc": _local(fixed_now + timedelta(hours=2))},
                    follow_redirects=True)
    assert "Skutočný nástup nemôže byť v budúcnosti" in r.text
    with session_scope() as s:
        assert ensure_utc(s.get(DriverShift, sid).start_utc) == fixed_now - timedelta(hours=1)
    # naplánovaná zmena v budúcnosti je v poriadku
    r = client.post("/zmeny/nova", data={"csrf_token": tok, "driver_id": did, "train_id": tid, "status": "planovana",
                                          "start_utc": _local(fixed_now + timedelta(hours=3))}, follow_redirects=True)
    assert "Zmena uložená." in r.text


def test_nastup_par_minut_dopredu_sa_zapise_ako_teraz(client, cfg, fixed_now):
    """Čas vo formulári je v celých minútach: nástup o 2 min neskôr (zaokrúhlenie) sa pri zmene „prebieha“ zapíše
    ako teraz – plánovač ho hneď berie za nástup (nie „chýba záznam o nástupe“)."""
    from vlaky.db import session_scope

    with session_scope() as s:
        t = _train(s, fixed_now, number="95842", planned_departure_utc=fixed_now - timedelta(hours=1))
        d = Driver(name="Rušňovodič 9 (fiktívny)")
        s.add(d)
        s.flush()
        tid, did = t.id, d.id
    login_as(client, "disp1")
    tok = csrf(client, "/rusnovodici")
    r = client.post("/zmeny/nova", data={"csrf_token": tok, "driver_id": did, "train_id": tid, "status": "prebieha",
                                          "start_utc": _local(fixed_now + timedelta(minutes=2))}, follow_redirects=True)
    assert "Zmena uložená." in r.text and "zadaný čas" in r.text
    with session_scope() as s:
        assert ensure_utc(s.scalar(select(DriverShift).where(DriverShift.train_id == tid)).start_utc) == fixed_now


def test_rusnovodic_s_dvoma_prebiehajucimi_zmenami_dostane_varovanie(client, cfg, fixed_now):
    """Ten istý rušňovodič zapísaný „prebieha“ na dvoch vlakoch: zmena sa uloží, ale s upozornením."""
    from vlaky.db import session_scope

    with session_scope() as s:
        t1 = _train(s, fixed_now, number="95801")
        t2 = _train(s, fixed_now, number="95822")
        sh = _shift(s, t1, fixed_now - timedelta(hours=3), name="Rušňovodič 7 (fiktívny)")
        t2id, did = t2.id, sh.driver_id
    login_as(client, "disp1")
    tok = csrf(client, "/rusnovodici")
    r = client.post("/zmeny/nova", data={"csrf_token": tok, "driver_id": did, "train_id": t2id, "status": "prebieha",
                                          "start_utc": _local(fixed_now - timedelta(minutes=30))},
                    follow_redirects=True)
    assert "Zmena uložená." in r.text
    assert "Pozor: rušňovodič Rušňovodič 7 (fiktívny) má aj inú zmenu, ktorá prebieha (vlak 95801" in r.text
    assert 'flash flash-varovanie' in r.text


def test_starsia_zmena_s_buducim_nastupom_neukazuje_odpocet_nad_13_h(client, cfg, fixed_now):
    """Starší záznam „prebieha“ s nástupom v budúcnosti: riadok neukazuje odpočet viac ako 13 h, ale výzvu na opravu;
    vlak je ako v plánovači medzi idúcimi vlakmi bez záznamu o nástupe (odhad z odporúčania)."""
    from vlaky.db import session_scope

    with session_scope() as s:
        seed_borders(s)
        t = _train(s, fixed_now, number="95843", origin="Čadca", destination="Košice",
                   planned_departure_utc=fixed_now - timedelta(hours=2),
                   departed_utc=fixed_now - timedelta(hours=2))
        _shift(s, t, fixed_now + timedelta(hours=3), name="Rušňovodič 5 (fiktívny)")
        rec = evaluate_train(s, t.id, fixed_now, "test")
        assert rec.crew.get("unknown_start")  # plánovač budúci nástup za nástup neberie
        relieve = datetime_fmt(rec.crew["relieve_by"])
    login_as(client, "disp1")
    page = client.get("/rusnovodici").text
    row = _zmeny(page)["Rušňovodič 5 (fiktívny)"]
    assert "nástup je v budúcnosti – opravte čas nástupu" in row[3]
    raw = re.search(r'<tr class="crew-row[^"]*">\s*<td><strong>Rušňovodič 5 \(fiktívny\)</strong>(.*?)</tr>',
                    page.split('id="h-zmeny"')[1], re.S).group(1)
    assert "15 h" not in row[3] and "data-countdown" not in raw and 'crew-row-oranzova' in page
    assert f"odhad: vystriedať najneskôr o {relieve}" in row[4]
    missing = page.split('id="h-bez-nastupu"')[1].split("</table>")[0]
    assert ">95843</a>" in missing


def datetime_fmt(value: str) -> str:
    from datetime import datetime

    return fmt(datetime.fromisoformat(value))


# ------------------------------------------------------------------ C: upozornenia strážnika ako plánovač
def test_strazca_upozorni_podla_odhadu_planovaca_pri_iducom_vlaku_bez_nastupu(session, fixed_now):
    """Idúci vlak bez zaznamenaného nástupu: plánovač posudzuje limit 13 h odhadom (odchod mínus príprava) a odhad
    je prekročený – strážnik nesmie mlčať: KRITICKÉ upozornenie „odhad – PREKROČENÝ“, raz (bez opakovania)."""
    from vlaky.agent.strazenie import crew_alerts

    seed_borders(session)
    dep = fixed_now - timedelta(hours=12, minutes=30)
    t = _train(session, fixed_now, number="95841", destination="Košice", planned_departure_utc=dep,
               departed_utc=dep)
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.crew.get("unknown_start") and datetime_iso(rec.crew["limit"]) < fixed_now
    assert crew_alerts(session, fixed_now) == 1
    n = session.scalar(select(Notification).where(Notification.kind == "upozornenie", Notification.channel == "email"))
    assert n.priority == "KRITICKÉ" and "vlak 95841" in n.subject and "odhad – PREKROČENÝ limit 13 h" in n.subject
    assert "len ODHAD" in n.body and "Zadajte skutočný nástup rušňovodiča" in n.body
    assert crew_alerts(session, fixed_now + timedelta(minutes=5)) == 0  # rovnaká úroveň sa neopakuje
    # dispečer zadá skutočný nástup: ďalej stráži zmena (od jej nástupu, ako plánovač), odhad sa už neposiela
    last = session.scalar(select(Notification.id).order_by(Notification.id.desc()))
    _shift(session, t, fixed_now - timedelta(hours=12, minutes=5))  # pri +10 min do limitu 45 min
    assert crew_alerts(session, fixed_now + timedelta(minutes=10)) == 1
    new = [x.subject for x in session.scalars(select(Notification).where(Notification.id > last,
                                                                           Notification.channel == "email"))]
    assert new and all("odhad" not in x and "do limitu 13 h zostáva 45 min" in x for x in new)


def test_strazca_pri_buducom_nastupe_prebiehajucej_zmeny_upozorni_podla_odhadu(session, fixed_now):
    """Starší záznam „prebieha“ s nástupom v budúcnosti pri idúcom vlaku (scenár z nálezu): plánovač počíta odhad
    a strážnik upozorní podľa neho (pred opravou mlčal, kým budúci nástup nenastal)."""
    from vlaky.agent.strazenie import crew_alerts

    seed_borders(session)
    dep = fixed_now - timedelta(hours=11, minutes=55)  # odhad: nástup o 1 h skôr → do limitu 5 min
    t = _train(session, fixed_now, number="95844", destination="Košice", planned_departure_utc=dep,
               departed_utc=dep)
    _shift(session, t, fixed_now + timedelta(hours=3), name="Rušňovodič 7 (fiktívny)")
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.crew.get("unknown_start")
    assert crew_alerts(session, fixed_now) == 1
    n = session.scalar(select(Notification).where(Notification.kind == "upozornenie", Notification.channel == "email"))
    assert n.priority == "KRITICKÉ" and "odhad – do limitu 13 h zostáva 5 min" in n.subject
    assert "Rušňovodič 7 (fiktívny) (chýba záznam o nástupe)" in n.subject


def test_strazca_necele_hodiny_limitu_s_desatinnou_ciarkou(session, fixed_now):
    """Limit 12,5 h (750 min) sa v upozornení nesmie zaokrúhliť na „12 h“ ani písať s bodkou."""
    from vlaky.agent.strazenie import crew_alerts

    session.add(ParameterSet(values={"crew_max_duty_min": 750}, valid_from=fixed_now - timedelta(minutes=1)))
    t = _train(session, fixed_now, number="95846")
    _shift(session, t, fixed_now - timedelta(hours=11, minutes=50))  # do limitu 40 min
    assert crew_alerts(session, fixed_now) == 1
    n = session.scalar(select(Notification).where(Notification.channel == "email"))
    assert "do limitu 12,5 h zostáva 40 min" in n.subject


def datetime_iso(value: str):
    from datetime import datetime

    return ensure_utc(datetime.fromisoformat(value))


def test_upozornenia_strazcu_v_cykle_agenta_su_v_sulade_s_planovacom(cfg, fixed_now):
    """Celý cyklus agenta: idúci vlak bez nástupu – po prepočte (odhad prekročený) príde KRITICKÉ upozornenie
    strážnika a plánovač aj strážnik uvádzajú ten istý odhadovaný limit."""
    from vlaky.agent import beh
    from vlaky.db import session_scope

    with session_scope() as s:
        seed_borders(s)
        dep = fixed_now - timedelta(hours=12, minutes=40)
        t = _train(s, fixed_now, number="95847", destination="Košice", planned_departure_utc=dep, departed_utc=dep)
        tid = t.id
    beh.run_cycle(fixed_now)
    with session_scope() as s:
        rec = s.scalar(select(Recommendation).where(Recommendation.train_id == tid).order_by(Recommendation.id.desc()))
        limit = fmt(datetime_iso(rec.crew["limit"]))
        alerts = [n for n in s.scalars(select(Notification).where(Notification.kind == "upozornenie",
                                                                  Notification.channel == "email"))
                  if "vlak 95847" in n.subject]
    assert alerts and all("odhad – PREKROČENÝ" in n.subject and f"limit {limit}" in n.body for n in alerts)


# ------------------------------------------------------------------ odpočet 13 h na stránke vlaku a v prehliadači
def test_stranka_vlaku_bez_odpoctu_pri_buducom_nastupe(client, cfg, fixed_now):
    """Stránka vlaku: starší záznam „prebieha“ s nástupom v budúcnosti nedostane odpočet (ukázal by viac ako 13 h),
    ale výzvu na opravu; zmena s minulým nástupom odpočet má."""
    from vlaky.db import session_scope

    with session_scope() as s:
        t = _train(s, fixed_now, number="95848")
        _shift(s, t, fixed_now + timedelta(hours=3), name="Rušňovodič 3 (fiktívny)")
        other = _train(s, fixed_now, number="95849")
        _shift(s, other, fixed_now - timedelta(hours=1), name="Rušňovodič 4 (fiktívny)")
        tid, oid = t.id, other.id
    login_as(client, "disp1")
    para = re.search(r"<p>Rušňovodič 3 \(fiktívny\)(.*?)</p>", client.get(f"/vlaky/{tid}").text, re.S).group(1)
    assert "nástup je v budúcnosti – opravte čas nástupu" in para and "data-countdown" not in para
    para = re.search(r"<p>Rušňovodič 4 \(fiktívny\)(.*?)</p>", client.get(f"/vlaky/{oid}").text, re.S).group(1)
    assert "data-countdown" in para and "v budúcnosti" not in para


def _js_function(src: str, name: str) -> str:
    i = src.index(f"function {name}(")
    depth, j = 0, src.index("{", i)
    while True:
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[i:j + 1]
        j += 1


def test_odpocet_v_prehliadaci_nikdy_viac_ako_limit(tmp_path):
    """Skutočný odpočet z app.js (Node.js, falošný DOM): ani pri nástupe „v budúcnosti“ (hodiny prehliadača pozadu)
    neukáže viac ako 13 h; bežný odpočet a prekročenie ostávajú."""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    if shutil.which("node") is None:
        pytest.skip("node nie je k dispozícii")
    src = (Path(__file__).resolve().parent.parent / "vlaky/web/static/js/app.js").read_text(encoding="utf-8")
    script = tmp_path / "t.js"
    script.write_text(
        _js_function(src, "fmtMinutes") + "\n" + _js_function(src, "updateCountdowns") + "\n"
        "function mk(ms) { return { attrs: { 'data-countdown': new Date(Date.now() + ms).toISOString(),\n"
        "  'data-limit-min': '780', 'data-buffer-min': '60' }, textContent: '',\n"
        "  classList: { remove: function () {}, add: function () {} },\n"
        "  getAttribute: function (k) { return this.attrs[k]; },\n"
        "  setAttribute: function (k, v) { this.attrs[k] = v; } }; }\n"
        "const els = [mk(3 * 3600e3), mk(-3600e3), mk(-14 * 3600e3)];\n"
        "const document = { querySelectorAll: function () { return els; } };\n"
        "updateCountdowns();\n"
        "console.log(JSON.stringify(els.map(function (e) { return [e.textContent, e.attrs.title]; })));\n",
        encoding="utf-8")
    out = json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, check=True,
                                    timeout=60).stdout)
    assert [x[0] for x in out] == ["13 h 00 min", "12 h 00 min", "prekročené o 1 h 00 min"]
    assert out[0][1] == "Do limitu pracovného času (13 h)"
