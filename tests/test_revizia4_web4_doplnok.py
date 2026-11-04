"""Regresné testy k 4. revízii (W4, kontrola opráv) – starší záznam „prebieha“ s nástupom v budúcnosti a odovzdávka
počas dočítavania pošty:

- keď vlak dostane nástup, s ktorým plánovač počíta (Zadať nástup, Naplánovať zmenu, Nástup teraz, nástup z odkazu
  rušňovodiča), zmena „prebieha“ s nástupom v budúcnosti sa ukončí – inak by zakryla zadaný nástup: plánovač by
  počítal len s odhadom, vlak by ostal medzi vlakmi bez záznamu o nástupe a strážnik by posielal dve protichodné
  upozornenia (podľa zadanej zmeny aj podľa odhadu),
- stránka Rušňovodiči a upozornenie podľa odhadu povedia, že nástup zakrýva zmena s budúcim nástupom (a čo s ňou),
- vystriedanie zadané na webe upozorní na zmenu iného rušňovodiča, ktorá na tom istom vlaku ešte prebieha,
- odovzdávka počas dočítavania nahromadenej pošty netvrdí, že agent nebeží (agent beží), a dôvod nemá vnorené zátvorky.
"""
from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import select

from test_web_spolocne import csrf, login_as, make_recommendation
from vlaky.agent.zdravie import set_kv
from vlaky.cas import ensure_utc, fmt, local
from vlaky.hranice import seed_borders
from vlaky.models import AuditLog, Driver, DriverShift, Notification, Train
from vlaky.planovanie.planovac import MOBILE_DRIVER, evaluate_train

pytest_plugins = ["test_web_spolocne"]

A_NAME = "Rušňovodič A (fiktívny)"
B_NAME = "Rušňovodič B (fiktívny)"
C_NAME = "Rušňovodič C (fiktívny)"


def _local(dt) -> str:
    return local(dt).strftime("%Y-%m-%dT%H:%M")


def _flash(html: str) -> str:
    return " ".join(re.findall(r'class="flash[^"]*"[^>]*>(.*?)</', html, re.S))


def _missing(page: str) -> str:
    """Sekcia „Vlaky v jazde bez záznamu o nástupe rušňovodiča“ (prázdna, ak na stránke nie je)."""
    if 'id="h-bez-nastupu"' not in page:
        return ""
    return page.split('id="h-bez-nastupu"')[1].split("</table>")[0]


def _running_train(s, now, number: str, dep_h: float = 11 + 50 / 60) -> Train:
    dep = now - timedelta(hours=dep_h)
    t = Train(number=number, run_date=now.date().isoformat(), origin="Čadca", destination="Košice", traction="E",
              gauge="1435", vmax_kmh=100, status="v_jazde", deadline_utc=now + timedelta(hours=30),
              planned_departure_utc=dep, departed_utc=dep)
    s.add(t)
    s.flush()
    return t


def _driver(s, name: str) -> Driver:
    d = Driver(name=name)
    s.add(d)
    s.flush()
    return d


def _legacy(s, train: Train, now, hours: float = 3) -> DriverShift:
    """Starší záznam (pred opravou ho formulár prijal): „prebieha“ rušňovodiča A s nástupom o 3 h v budúcnosti."""
    sh = DriverShift(driver_id=_driver(s, A_NAME).id, train_id=train.id, start_utc=now + timedelta(hours=hours),
                     status="prebieha")
    s.add(sh)
    s.flush()
    return sh


def _crew_subjects(s) -> list:
    return sorted({n.subject for n in s.scalars(select(Notification).where(Notification.channel == "email"))
                   if "Rušňovodič" in (n.subject or "")})


# ------------------------------------------------------------------ zmena s budúcim nástupom sa ukončí
def test_zadat_nastup_ukonci_starsiu_zmenu_s_buducim_nastupom(client, cfg, fixed_now):
    """Idúci vlak so starším záznamom „prebieha“ (A, nástup o 3 h): dispečer cez ponúknuté „Zadať nástup“ zadá
    skutočný nástup B pred 12 h. Zmena A sa ukončí (audit s pôvodným nástupom a dôvodom), plánovač počíta s B (nie
    odhad), vlak už nie je medzi vlakmi bez nástupu a strážnik upozorní len podľa zmeny B – bez „odhadu“."""
    from vlaky.agent.strazenie import crew_alerts
    from vlaky.db import session_scope

    with session_scope() as s:
        seed_borders(s)
        t = _running_train(s, fixed_now, "95843")
        legacy = _legacy(s, t, fixed_now)
        b = _driver(s, B_NAME)
        assert evaluate_train(s, t.id, fixed_now, "test").crew.get("unknown_start")  # východisko: len odhad
        tid, bid, aid, a_start = t.id, b.id, legacy.id, ensure_utc(legacy.start_utc)
    login_as(client, "disp1")
    page = client.get("/rusnovodici").text
    assert ">95843</a>" in _missing(page)
    tok = csrf(client, "/rusnovodici")
    start_b = fixed_now - timedelta(hours=12)
    r = client.post("/zmeny/nova", data={"csrf_token": tok, "driver_id": bid, "train_id": tid, "status": "prebieha",
                                          "start_utc": _local(start_b)}, follow_redirects=True)
    flash = _flash(r.text)
    assert "Zmena uložená." in flash and f"Zmena rušňovodiča {A_NAME} „prebieha“ s nástupom v budúcnosti" in flash
    assert "je ukončená" in flash and fmt(a_start) in flash
    with session_scope() as s:
        a = s.get(DriverShift, aid)
        assert a.status == "ukoncena" and ensure_utc(a.actual_end_utc) == fixed_now
        log = s.scalar(select(AuditLog).where(AuditLog.action == "shift_end", AuditLog.entity_id == str(aid)))
        assert log is not None and "nástup v budúcnosti" in log.detail["dovod"]
        assert log.detail["start"] == a_start.replace(tzinfo=None).isoformat() or \
            log.detail["start"] == a_start.isoformat()
        rec = evaluate_train(s, tid, fixed_now, "po zadaní nástupu")
        assert not rec.crew.get("unknown_start"), rec.crew
        assert rec.crew["driver"] == B_NAME and rec.crew["start"] == start_b.isoformat()
    assert ">95843</a>" not in _missing(client.get("/rusnovodici").text)
    with session_scope() as s:
        crew_alerts(s, fixed_now + timedelta(minutes=5))
        subjects = _crew_subjects(s)
    assert subjects and all(B_NAME in x for x in subjects), subjects  # len zadaná zmena B (do limitu 55 min)
    assert not any("odhad" in x for x in subjects), subjects


def test_nastup_teraz_ukonci_starsiu_zmenu_s_buducim_nastupom(client, cfg, fixed_now):
    """„Nástup teraz“ pri naplánovanej zmene B toho istého idúceho vlaku: zaznamenaný nástup plánovač vezme (zmena A
    s budúcim nástupom sa ukončí) – predtým ostal vlak bez záznamu o nástupe s rušňovodičom A."""
    from vlaky.db import session_scope

    with session_scope() as s:
        seed_borders(s)
        t = _running_train(s, fixed_now, "95845", dep_h=1)
        legacy = _legacy(s, t, fixed_now)
        sb = DriverShift(driver_id=_driver(s, B_NAME).id, train_id=t.id, start_utc=fixed_now - timedelta(minutes=10),
                         status="planovana")
        s.add(sb)
        s.flush()
        tid, sbid, aid = t.id, sb.id, legacy.id
    login_as(client, "disp1")
    tok = csrf(client, "/rusnovodici")
    r = client.post(f"/zmeny/{sbid}/nastup", data={"csrf_token": tok}, follow_redirects=True)
    flash = _flash(r.text)
    assert "Nástup zaznamenaný" in flash and "je ukončená" in flash and A_NAME in flash
    with session_scope() as s:
        assert s.get(DriverShift, aid).status == "ukoncena"
        b = s.get(DriverShift, sbid)
        assert b.status == "prebieha" and ensure_utc(b.start_utc) == fixed_now
        rec = evaluate_train(s, tid, fixed_now, "po nástupe")
        assert not rec.crew.get("unknown_start") and rec.crew["driver"] == B_NAME, rec.crew
    assert ">95845</a>" not in _missing(client.get("/rusnovodici").text)


def test_naplanovana_zmena_s_nastupom_do_teraz_ukonci_starsiu_zmenu(client, cfg, fixed_now):
    """Naplánovaná zmena s nástupom v budúcnosti zmenu A nechá (vlak nástup ešte nemá – stránka to povie); naplánovaná
    zmena s nástupom najneskôr teraz (predvolený čas formulára „Naplánovať zmenu“) je pre plánovač nástup – zmena A
    s budúcim nástupom sa ukončí a plánovač počíta s B."""
    from vlaky.db import session_scope

    with session_scope() as s:
        seed_borders(s)
        t = _running_train(s, fixed_now, "95846", dep_h=2)
        legacy = _legacy(s, t, fixed_now)
        b, c = _driver(s, B_NAME), _driver(s, C_NAME)
        tid, aid, bid, cid = t.id, legacy.id, b.id, c.id
    login_as(client, "disp1")
    tok = csrf(client, "/rusnovodici")
    later = client.post("/zmeny/nova", data={"csrf_token": tok, "driver_id": cid, "train_id": tid,
                                              "status": "planovana",
                                              "start_utc": _local(fixed_now + timedelta(hours=1))},
                        follow_redirects=True)
    assert "ukončená" not in _flash(later.text)
    with session_scope() as s:
        assert s.get(DriverShift, aid).status == "prebieha"  # neskoršia naplánovaná zmena nie je nástup
    page = client.get("/rusnovodici").text
    missing = _missing(page)
    assert ">95846</a>" in missing
    # stránka povie, prečo vlak nástup nemá, a odkáže na zmenu s budúcim nástupom
    assert f"Zmena „prebieha“ rušňovodiča {A_NAME} má nástup v budúcnosti" in missing
    assert f'href="#zmena-{aid}"' in missing and f'id="zmena-{aid}"' in page
    now_form = client.post("/zmeny/nova", data={"csrf_token": tok, "driver_id": bid, "train_id": tid,
                                                 "status": "planovana", "start_utc": _local(fixed_now)},
                           follow_redirects=True)
    assert "je ukončená" in _flash(now_form.text)
    with session_scope() as s:
        assert s.get(DriverShift, aid).status == "ukoncena"
        rec = evaluate_train(s, tid, fixed_now, "po naplánovaní")
        assert not rec.crew.get("unknown_start") and rec.crew["driver"] == B_NAME, rec.crew
        assert rec.crew["start"] == fixed_now.isoformat()


def test_nastup_z_odkazu_pri_starsej_zmene_s_buducim_nastupom(client, cfg, fixed_now):
    """Hlásenie „Nástup na zmenu“ z odkazu rušňovodiča: zmena „prebieha“ s budúcim nástupom nie je nástup – hlásenie
    sa neodmietne („nástup už bol zaznamenaný o <budúci čas>“), založí sa skutočná zmena (idúci vlak: najneskôr od
    odchodu mínus príprava) a zmena s budúcim nástupom sa ukončí."""
    from vlaky.db import session_scope
    from vlaky.web.rusnovodic import make_driver_token

    with session_scope() as s:
        seed_borders(s)
        t = _running_train(s, fixed_now, "95847", dep_h=2)
        legacy = _legacy(s, t, fixed_now)
        tid, aid = t.id, legacy.id
        token = make_driver_token(t)
    r = client.post("/api/hlasenie", json={"t": token, "typ": "nastup"})
    assert r.status_code == 200, r.text
    msg = r.json()["message"]
    assert f"Starší záznam zmeny ({A_NAME}) s nástupom v budúcnosti je ukončený." in msg
    with session_scope() as s:
        assert s.get(DriverShift, aid).status == "ukoncena"
        new = s.scalar(select(DriverShift).where(DriverShift.train_id == tid, DriverShift.status == "prebieha"))
        assert new is not None and new.driver.name == MOBILE_DRIVER
        assert ensure_utc(new.start_utc) <= fixed_now - timedelta(hours=2)  # odchod mínus príprava, nie „teraz“
        log = s.scalar(select(AuditLog).where(AuditLog.action == "hlasenie_nastup"))
        assert log.detail.get("ended_future_shift_ids") == [aid]
        rec = evaluate_train(s, tid, fixed_now, "po nástupe z odkazu")
        assert not rec.crew.get("unknown_start"), rec.crew


def test_vystriedanie_z_odkazu_pri_starsej_zmene_s_buducim_nastupom(client, cfg, fixed_now):
    """Rušňovodič C v službe, vystriedanie D naplánované teraz a k tomu starší záznam A „prebieha“ s budúcim nástupom:
    nástup z odkazu je vystriedanie (C sa ukončí, D začne teraz) – zmena A ho nezablokuje a tiež sa ukončí."""
    from vlaky.db import session_scope
    from vlaky.web.rusnovodic import make_driver_token

    with session_scope() as s:
        seed_borders(s)
        t = _running_train(s, fixed_now, "95848", dep_h=6)
        legacy = _legacy(s, t, fixed_now)
        c = DriverShift(driver_id=_driver(s, C_NAME).id, train_id=t.id, start_utc=fixed_now - timedelta(hours=7),
                        status="prebieha")
        d = DriverShift(driver_id=_driver(s, "Rušňovodič D (fiktívny)").id, train_id=t.id,
                        start_utc=fixed_now - timedelta(minutes=5), status="planovana")
        s.add_all([c, d])
        s.flush()
        tid, aid, cid, did = t.id, legacy.id, c.id, d.id
        token = make_driver_token(t)
    r = client.post("/api/hlasenie", json={"t": token, "typ": "nastup"})
    assert r.status_code == 200, r.text
    assert f"Zmena predchádzajúceho rušňovodiča ({C_NAME}) je ukončená." in r.json()["message"]
    with session_scope() as s:
        assert s.get(DriverShift, aid).status == "ukoncena" and s.get(DriverShift, cid).status == "ukoncena"
        d = s.get(DriverShift, did)
        assert d.status == "prebieha" and ensure_utc(d.start_utc) == fixed_now
        rec = evaluate_train(s, tid, fixed_now, "po vystriedaní")
        assert not rec.crew.get("unknown_start") and rec.crew["driver"] == "Rušňovodič D (fiktívny)", rec.crew


# ------------------------------------------------------------------ texty: stránka a upozornenie podľa odhadu
def test_upozornenie_podla_odhadu_povie_o_zmene_s_buducim_nastupom(client, cfg, fixed_now):
    """Idúci vlak, ktorého nástup zakrýva zmena A „prebieha“ s budúcim nástupom: upozornenie podľa odhadu povie, že
    zmenu A agent za nástup neberie a čo s ňou (opraviť čas alebo zadať nástup toho, kto vlak vedie)."""
    from vlaky.agent.strazenie import crew_alerts
    from vlaky.db import session_scope

    with session_scope() as s:
        seed_borders(s)
        t = _running_train(s, fixed_now, "95849")  # odhad: nástup pred 12 h 50 min – do limitu 10 min
        legacy = _legacy(s, t, fixed_now)
        evaluate_train(s, t.id, fixed_now, "test")
        a_start = legacy.start_utc
    with session_scope() as s:
        assert crew_alerts(s, fixed_now) >= 1
        notes = [n for n in s.scalars(select(Notification).where(Notification.channel == "email"))
                 if "odhad" in (n.subject or "")]
    assert notes, "upozornenie podľa odhadu chýba"
    body = notes[0].body
    assert f"Zmena „prebieha“ rušňovodiča {A_NAME} má nástup v budúcnosti ({fmt(a_start)})" in body
    assert "opravte čas nástupu v zmene" in body and "zmena s budúcim nástupom sa ukončí" in body


def test_vystriedanie_na_webe_upozorni_na_inu_prebiehajucu_zmenu_vlaku(client, cfg, fixed_now):
    """„Nástup teraz“ pri vystriedaní B, keď na vlaku ešte prebieha zmena C: zmena C sa na webe neukončí sama (strážnik
    by ďalej strážil jej limit) – dispečer dostane upozornenie, aby ju ukončil, ak C vlak už nevedie."""
    from vlaky.db import session_scope

    with session_scope() as s:
        seed_borders(s)
        t = _running_train(s, fixed_now, "95850", dep_h=6)
        c = DriverShift(driver_id=_driver(s, C_NAME).id, train_id=t.id, start_utc=fixed_now - timedelta(hours=7),
                        status="prebieha")
        b = DriverShift(driver_id=_driver(s, B_NAME).id, train_id=t.id, start_utc=fixed_now, status="planovana")
        s.add_all([c, b])
        s.flush()
        bid, cid, c_start = b.id, c.id, c.start_utc
    login_as(client, "disp1")
    r = client.post(f"/zmeny/{bid}/nastup", data={"csrf_token": csrf(client, "/rusnovodici")}, follow_redirects=True)
    flash = _flash(r.text)
    assert "Nástup zaznamenaný" in flash
    assert (f"Pozor: na vlaku 95850 prebieha aj zmena rušňovodiča {C_NAME} (nástup {fmt(c_start)}) – ak vlak už "
            f"nevedie (vystriedanie), ukončite ju.") in flash
    assert 'flash-varovanie' in r.text
    with session_scope() as s:
        assert s.get(DriverShift, cid).status == "prebieha"  # len upozornenie – zmenu ukončí dispečer


# ------------------------------------------------------------------ odovzdávka a čerstvosť pri dočítavaní pošty
def _partial(s, now, last_full_min: float = 50, cycle_min: float = 0):
    set_kv(s, "agent_last_cycle", {"ts": (now - timedelta(minutes=cycle_min)).isoformat(), "errors": 0})
    set_kv(s, "mail_last_ok", {"partial": True, "last_full": (now - timedelta(minutes=last_full_min)).isoformat(),
                               "since": (now - timedelta(minutes=last_full_min - 1)).isoformat()})


def _green(number="95101"):
    from test_web_spolocne import make_train

    tid = make_train(number=number, status="v_jazde")
    make_recommendation(tid, decision="POKRAČOVAŤ", color="ZELENÁ", priority="INFO",
                        summary=f"Vlak {number}: POKRAČOVAŤ v jazde (FIKTÍVNE).", crew={})
    return tid


def test_odovzdavka_pri_dociatavani_posty_netvrdi_ze_agent_nebezi(cfg, fixed_now):
    """Agent beží (čerstvý cyklus), len dočítava nahromadenú poštu: odovzdávka (aj tá, ktorú agent pošle pri výmene)
    povie pravdu – agent beží a dočítava poštu, nič nie je ZELENÉ – nie „agent nebeží alebo nečíta schránku
    (kapitola 12)“; dôvod nemá vnorené zátvorky. Pri výpadku agenta ostáva odkaz na kapitolu 12."""
    from vlaky.agent.odovzdavka import build_report
    from vlaky.db import session_scope

    with session_scope() as s:
        _partial(s, fixed_now)
    _green()
    with session_scope() as s:
        text, data = build_report(s, fixed_now)
    pozor = text.split("\n")[2]
    assert data["stale"] and pozor.startswith("POZOR: Údaje agenta nie sú čerstvé (schránka agenta sa dočítava – "
                                              "posledná úplná kontrola pred 50 min).")
    assert "Agent beží a dočítava nahromadenú poštu" in pozor and "nie je nič ZELENÉ" in pozor
    assert "nebeží" not in pozor and "kapitola 12" not in pozor and "min))" not in pozor
    orange = text.split("ORANŽOVÉ vlaky:")[1].split("\n\n")[0]
    assert "95101" in orange and "pôvodne ZELENÁ" in orange
    # agent navyše stojí (starý cyklus): výpadok – odkaz na kapitolu 12 ostáva
    with session_scope() as s:
        _partial(s, fixed_now, cycle_min=150)
        text2, _ = build_report(s, fixed_now)
    pozor2 = text2.split("\n")[2]
    assert "posledný cyklus agenta pred 150 min" in pozor2
    assert "agent nebeží alebo nečíta schránku (príručka, kapitola 12)" in pozor2 and "Agent beží" not in pozor2


def test_cerstvost_dat_s_kodmi_dovodov_a_bez_vnorenych_zatvoriek(client, cfg, fixed_now):
    """Čerstvosť údajov agenta má okrem textu aj kódy dôvodov (dočítavanie pošty nie je výpadok) a text na webe
    nemá vnorené zátvorky („… (schránka agenta sa dočítava – posledná úplná kontrola pred 50 min).“)."""
    from vlaky.db import session_scope
    from vlaky.web.data import data_freshness

    with session_scope() as s:
        _partial(s, fixed_now)
        fresh = data_freshness(s, fixed_now)
        assert fresh.get("codes") == ["mailbox_partial"] and fresh.get("outage") is False, fresh
        assert fresh["text"] == ("Údaje agenta nie sú čerstvé (schránka agenta sa dočítava – posledná úplná kontrola "
                                 "pred 50 min).")
        set_kv(s, "agent_last_cycle", {"ts": (fixed_now - timedelta(minutes=150)).isoformat(), "errors": 0})
        set_kv(s, "mail_last_ok", {"ts": (fixed_now - timedelta(minutes=45)).isoformat()})
        fresh = data_freshness(s, fixed_now)
        assert fresh.get("codes") == ["cycle_stale", "mailbox_old"] and fresh.get("outage") is True, fresh
        _partial(s, fixed_now)
    tid = _green("95102")
    login_as(client, "disp1")
    page = client.get(f"/vlaky/{tid}").text
    assert "schránka agenta sa dočítava – posledná úplná kontrola pred 50 min" in page
    assert "kontrola pred 50 min))" not in page
