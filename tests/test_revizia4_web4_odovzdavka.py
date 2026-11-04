"""Regresné testy k 4. revízii (W4, QU1) – odovzdávka služby počas výpadku agenta:

- odovzdávka (z webu aj od agenta) hneď na začiatku povie, že údaje agenta nie sú čerstvé (ako web),
- vlak, ktorý web pre staré údaje ukazuje ORANŽOVÝ („pôvodne ZELENÁ“), v nej nechýba,
- prekročený limit 13 h je „PREKROČENÝ“ (nie „blízko limitu“) a uplynulý čas vystriedania sa neponúka ako rada.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from test_web_spolocne import csrf, login_as, make_recommendation
from vlaky.agent.zdravie import set_kv
from vlaky.cas import fmt
from vlaky.models import Driver, DriverShift, ShiftHandoverReport, Train

pytest_plugins = ["test_web_spolocne"]


def _outage(s, now, minutes=150):
    stale = (now - timedelta(minutes=minutes)).isoformat()
    set_kv(s, "agent_last_cycle", {"ts": stale, "errors": 0})
    set_kv(s, "mail_last_ok", {"ts": stale, "fetched": 0})


def _setup(now):
    """95001 – posledné odporúčanie ZELENÁ (pred 150 min); 95004 – idúci vlak, rušňovodič 30 min po limite 13 h,
    odporúčanie z času pred výpadkom radí vystriedať o 04:00 (už uplynulo)."""
    from vlaky.db import session_scope

    old = now - timedelta(minutes=150)
    with session_scope() as s:
        green = Train(number="95001", origin="Čadca", destination="Košice", status="na_ceste_k_hranici")
        run = Train(number="95004", origin="Čierna nad Tisou", destination="Žilina", status="v_jazde")
        s.add_all([green, run])
        s.flush()
        d = Driver(name="Rušňovodič 4 (fiktívny)")
        s.add(d)
        s.flush()
        start = now - timedelta(hours=13, minutes=30)
        s.add(DriverShift(driver_id=d.id, train_id=run.id, start_utc=start, status="prebieha"))
        gid, rid = green.id, run.id
    make_recommendation(gid, created_at=old, decision="VYPRAVIŤ", color="ZELENÁ", priority="INFO",
                        summary="Vlak 95001 (Čadca → Košice): VYPRAVIŤ o 01.11. 18:00 (FIKTÍVNE).",
                        crew={"driver": "", "status": "ZELENÁ"}, data_age={"computed_at": old.isoformat()})
    limit = start + timedelta(hours=13)
    make_recommendation(rid, created_at=old, decision="POKRAČOVAŤ", color="ORANŽOVÁ", priority="DÔLEŽITÉ",
                        summary=f"Vlak 95004 (Čierna nad Tisou → Žilina): POKRAČOVAŤ v jazde (FIKTÍVNE). Rušňovodič: "
                                f"vystriedať najneskôr o {fmt(limit - timedelta(minutes=30))}.",
                        crew={"driver": "Rušňovodič 4 (fiktívny)", "start": start.isoformat(),
                              "limit": limit.isoformat(), "status": "ORANŽOVÁ",
                              "relieve_by": (limit - timedelta(minutes=30)).isoformat(), "max_duty_h": 13.0},
                        data_age={"computed_at": old.isoformat()})
    return limit


def test_odovzdavka_z_webu_pocas_vypadku_agenta(client, cfg, fixed_now):
    """Dispečer vytvorí odovzdávku na webe, keď agent 150 min nebeží: odovzdávka to povie, vlak so ZELENOU (web ho
    ukazuje ORANŽOVÝ) je medzi ORANŽOVÝMI s poznámkou a prekročený limit 13 h je „PREKROČENÝ“."""
    from vlaky.db import session_scope

    with session_scope() as s:
        _outage(s, fixed_now)
    limit = _setup(fixed_now)
    login_as(client, "disp1")
    web = {r["number"]: r for r in client.get("/api/vlaky").json()["vlaky"]}
    assert web["95001"]["color"] == "ORANŽOVÁ" and web["95001"]["downgraded"]
    tok = csrf(client, "/odovzdavka")
    r = client.post("/odovzdavka/nova", data={"csrf_token": tok}, follow_redirects=True)
    assert r.status_code == 200
    with session_scope() as s:
        rep = s.scalar(select(ShiftHandoverReport).order_by(ShiftHandoverReport.id.desc()))
        text, data = rep.content, rep.data
    head = text.split("Vlaky v jazde:")[0]
    assert "POZOR: Údaje agenta nie sú čerstvé (posledný cyklus agenta pred 150 min" in head
    assert "schránka agenta sa nekontrolovala 150 min" in head
    orange = text.split("ORANŽOVÉ vlaky:")[1].split("\n\n")[0]
    assert "Vlak 95001 (Čadca → Košice)" in orange and "pôvodne ZELENÁ" in orange
    assert f"[prepočet {fmt(fixed_now - timedelta(minutes=150))}]" in orange
    # text odporúčania z prepočtu radí vystriedať v čase, ktorý už uplynul – pri vlaku je hneď aj stav k teraz
    line_95004 = next(x for x in orange.splitlines() if "Vlak 95004" in x)
    assert line_95004.endswith("[teraz: rušňovodič – PREKROČENÝ limit 13 h, pozri Rušňovodiči nižšie]")
    exceeded = text.split("Rušňovodiči s PREKROČENÝM limitom 13 h")[1].split("\n\n")[0]
    assert f"vlak 95004: PREKROČENÝ limit 13 h o 30 min (limit {fmt(limit)}) – vystriedať ihneď" in exceeded
    near = text.split("Rušňovodiči blízko limitu 13 h:")[1].split("\n\n")[0]
    assert "95004" not in near
    past = fmt(limit - timedelta(minutes=30))
    assert f"vystriedať najneskôr o {past}" not in text.split("Rušňovodiči s PREKROČENÝM")[1]
    assert data["stale"] is True and data["crew_exceeded"] == 1
    trains = {x["number"]: x for x in data["trains"]}
    assert trains["95001"]["display_color"] == "ORANŽOVÁ" and trains["95001"]["downgraded"] is True
    assert trains["95001"]["color"] == "ZELENÁ"  # uložené odporúčanie sa nemení


def test_odovzdavka_s_cerstvymi_udajmi_bez_poznamky(cfg, fixed_now):
    """Agent beží (čerstvý cyklus aj schránka): bez poznámky o výpadku, ZELENÝ vlak nie je medzi ORANŽOVÝMI."""
    from vlaky.agent.odovzdavka import build_report
    from vlaky.db import session_scope

    _setup(fixed_now)
    with session_scope() as s:
        text, data = build_report(s, fixed_now)
    assert "POZOR" not in text and data["stale"] is False
    assert "95001" not in text.split("ORANŽOVÉ vlaky:")[1].split("\n\n")[0]
    assert "[prepočet" not in text


def test_odovzdavka_agenta_pri_vypadku_schranky(cfg, fixed_now):
    """Agent beží, ale schránka sa 45 min nedá prečítať: aj odovzdávka, ktorú pošle agent, to povie."""
    from vlaky.agent.odovzdavka import create_report
    from vlaky.db import session_scope

    with session_scope() as s:
        set_kv(s, "agent_last_cycle", {"ts": fixed_now.isoformat(), "errors": 1})
        set_kv(s, "mail_last_ok", {"ts": (fixed_now - timedelta(minutes=45)).isoformat()})
    _setup(fixed_now)
    with session_scope() as s:
        rep = create_report(s, fixed_now, notify=False)
        assert rep.content.split("\n")[2].startswith("POZOR: Údaje agenta nie sú čerstvé (schránka agenta sa "
                                                     "nekontrolovala 45 min)")


def test_cas_vystriedania_uplynul_limit_este_nie(cfg, fixed_now):
    """Čas vystriedania (limit − 30 min) uplynul, limit ešte nie: „vystriedať ihneď“ so zostávajúcim časom, nie
    „vystriedať najneskôr o <minulý čas>“; bez záznamu v odporúčaní (agent stál) rozhoduje živý stav zmeny."""
    from vlaky.agent.odovzdavka import build_report
    from vlaky.db import session_scope

    with session_scope() as s:
        t = Train(number="95006", origin="Maťovce ŠRT", destination="Haniska pri Košiciach", status="v_jazde")
        d = Driver(name="Rušňovodič 6 (fiktívny)")
        s.add_all([t, d])
        s.flush()
        start = fixed_now - timedelta(hours=12, minutes=40)  # do limitu 20 min
        s.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=start, status="prebieha"))
        tid = t.id
    make_recommendation(tid, created_at=fixed_now - timedelta(hours=3), decision="POKRAČOVAŤ", color="ZELENÁ",
                        priority="INFO", summary="Vlak 95006: POKRAČOVAŤ (FIKTÍVNE).",
                        crew={"driver": "Rušňovodič 6 (fiktívny)", "start": start.isoformat(), "status": "ZELENÁ",
                              "limit": (start + timedelta(hours=13)).isoformat(),
                              "relieve_by": (start + timedelta(hours=12, minutes=30)).isoformat()})
    with session_scope() as s:
        text, _data = build_report(s, fixed_now)
    near = text.split("Rušňovodiči blízko limitu 13 h:")[1].split("\n\n")[0]
    limit = start + timedelta(hours=13)
    assert f"vlak 95006: vystriedať ihneď – do limitu 13 h ({fmt(limit)}) zostáva 20 min" in near
    assert f"vystriedať najneskôr o {fmt(limit - timedelta(minutes=30))}" not in text


def test_pri_vlaku_stav_rusnovodica_k_teraz_ked_cas_vystriedania_uplynul(cfg, fixed_now):
    """ORANŽOVÝ vlak, ktorého text odporúčania z prepočtu radí „vystriedať najneskôr o <čas>“ a ten čas už uplynul:
    pri vlaku je hneď aj stav k teraz („vystriedať ihneď“), aby sa uplynulý čas nečítal ako rada."""
    from vlaky.agent.odovzdavka import build_report
    from vlaky.db import session_scope

    start = fixed_now - timedelta(hours=12, minutes=40)  # vystriedať najneskôr pred 10 min, do limitu 20 min
    relieve = start + timedelta(hours=12, minutes=30)
    with session_scope() as s:
        t = Train(number="95007", origin="Kúty", destination="Štúrovo", status="v_jazde")
        d = Driver(name="Rušňovodič 8 (fiktívny)")
        s.add_all([t, d])
        s.flush()
        s.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=start, status="prebieha"))
        tid = t.id
    make_recommendation(tid, created_at=fixed_now - timedelta(minutes=20), decision="POKRAČOVAŤ", color="ORANŽOVÁ",
                        priority="DÔLEŽITÉ",
                        summary=f"Vlak 95007: POKRAČOVAŤ (FIKTÍVNE). Rušňovodič: vystriedať najneskôr o "
                                f"{fmt(relieve)}.",
                        crew={"driver": "Rušňovodič 8 (fiktívny)", "start": start.isoformat(), "status": "ORANŽOVÁ",
                              "limit": (start + timedelta(hours=13)).isoformat(), "relieve_by": relieve.isoformat()})
    with session_scope() as s:
        text, _data = build_report(s, fixed_now)
    orange = text.split("ORANŽOVÉ vlaky:")[1].split("\n\n")[0]
    assert (f"  - Vlak 95007: POKRAČOVAŤ (FIKTÍVNE). Rušňovodič: vystriedať najneskôr o {fmt(relieve)}. "
            f"[teraz: rušňovodiča vystriedať ihneď, pozri Rušňovodiči nižšie]") in orange
    assert "[prepočet" not in orange  # agent beží – čas prepočtu sa pri vlaku neuvádza


def test_odhad_prekroceny_pri_iducom_vlaku_bez_nastupu(cfg, fixed_now):
    """Idúci vlak bez záznamu o nástupe: prekročený aj ODHAD limitu je „odhad – PREKROČENÝ“ (nie „vystriedať
    najneskôr o <minulý čas>“)."""
    from vlaky.agent.odovzdavka import build_report
    from vlaky.db import session_scope

    with session_scope() as s:
        t = Train(number="47260", origin="Čadca", destination="Košice", status="v_jazde")
        s.add(t)
        s.flush()
        tid = t.id
    start = fixed_now - timedelta(hours=14)
    limit = start + timedelta(hours=13)
    make_recommendation(tid, decision="POKRAČOVAŤ", color="ORANŽOVÁ",
                        crew={"driver": "neznámy (chýba záznam o nástupe)", "unknown_start": True, "estimated": True,
                              "status": "ČERVENÁ", "max_duty_h": 13.0, "start": start.isoformat(),
                              "limit": limit.isoformat(), "relieve_by": (limit - timedelta(minutes=30)).isoformat()})
    with session_scope() as s:
        text, _data = build_report(s, fixed_now)
    exceeded = text.split("Rušňovodiči s PREKROČENÝM limitom 13 h")[1].split("\n\n")[0]
    assert "vlak 47260: odhad – PREKROČENÝ limit 13 h o 1 h 00 min" in exceeded
    assert "nástup rušňovodiča nie je zadaný" in exceeded
    assert "47260" not in text.split("Rušňovodiči blízko limitu 13 h:")[1].split("\n\n")[0]
