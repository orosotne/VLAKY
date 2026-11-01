"""Regresné testy k zlúčeniu opráv druhej revízie (súvislosti medzi oblasťami).

- príchod vlaku na vstupnú hranicu hlásený cez /api/udalosti-jazdy zapíše začiatok státia na koľajach ŽSR
  (Train.standing_since_utc) rovnako ako zápis príchodu vo webe – limit 18 h beží od skutočného príchodu,
- mapa a nástenka (train_rows) ukážu rušňovodiča v službe, nie neskoršie naplánované vystriedanie
  (rovnaké pravidlo ako planovac.active_shift).
"""
from __future__ import annotations

from datetime import timedelta

from test_web_spolocne import login_as, make_train

pytest_plugins = ["test_web_spolocne"]

TOKEN = {"Authorization": "Bearer test-token"}


def test_prichod_na_hranicu_z_udalosti_jazdy_zapise_zaciatok_statia(client):
    from sqlalchemy import select

    from vlaky.db import session_scope
    from vlaky.models import BorderCrossing, Handover, Train

    with session_scope() as s:
        border_id = s.scalar(select(BorderCrossing).where(BorderCrossing.code == "CADCA")).id
    train_id = make_train(number="44901", status="na_ceste_k_hranici", entry_border_id=border_id,
                          origin="Čadca", destination="Žilina")
    with session_scope() as s:
        s.add(Handover(train_id=train_id, border_id=border_id, direction="vstup", status="ocakavany"))
    body = {"train_number": "44901", "station": "Cadca", "event": "prichod", "ts": "2026-10-31T10:15:00+01:00"}
    r = client.post("/api/udalosti-jazdy", json=body, headers=TOKEN)
    assert r.status_code == 200, r.text
    assert r.json()["handover_id"] is not None
    with session_scope() as s:
        t = s.get(Train, train_id)
        assert t.standing_since_utc is not None
        assert t.standing_since_utc.replace(tzinfo=None).isoformat() == "2026-10-31T09:15:00"


def test_mapa_ukaze_rusnovodica_v_sluzbe_nie_naplanovane_vystriedanie(client, fixed_now):
    from vlaky.db import session_scope
    from vlaky.models import Driver, DriverShift

    train_id = make_train(number="44902", status="v_jazde")
    with session_scope() as s:
        on_duty = Driver(name="Rušňovodič v službe (FIKTÍVNY)")
        relief = Driver(name="Vystriedanie (FIKTÍVNE)")
        s.add_all([on_duty, relief])
        s.flush()
        s.add_all([
            DriverShift(driver_id=on_duty.id, train_id=train_id, start_utc=fixed_now - timedelta(hours=12),
                        status="prebieha"),
            DriverShift(driver_id=relief.id, train_id=train_id, start_utc=fixed_now + timedelta(minutes=40),
                        status="planovana"),
        ])
    login_as(client, "integ1")
    row = next(r for r in client.get("/api/vlaky").json()["vlaky"] if r["id"] == train_id)
    assert row["driver"]["name"] == "Rušňovodič v službe (FIKTÍVNY)"
    assert row["driver"]["left_min"] is not None and row["driver"]["left_min"] <= 60


def test_zrusenie_zmeskanej_trasy_emailom_zachova_kotvu_18h(session, fixed_now):
    """Plánovaný vlak stojí pripravený od odchodu svojej trasy (kotva limitu 18 h). Keď ŽSR túto trasu e-mailom
    zruší, plánovač nesmie kotvu stratiť – začiatok státia sa pred zrušením trasy zapíše (stav.pin_standing)."""
    from vlaky.cas import ensure_utc, fmt, local
    from vlaky.hranice import seed_borders
    from vlaky.models import Closure, Email, ExtractedEvent, Path, Train
    from vlaky.planovanie.planovac import evaluate_train
    from vlaky.posta.spracovanie import confirm_event

    seed_borders(session)
    dep = fixed_now - timedelta(hours=17)
    t = Train(number="70300", run_date=local(dep).date().isoformat(), origin="Čadca", destination="Košice",
              traction="E", gauge="1435", vmax_kmh=100, status="planovany", ready_utc=dep,
              deadline_utc=fixed_now + timedelta(hours=30))
    session.add(t)
    session.flush()
    path = Path(train_id=t.id, train_number=t.number, status="pridelena", departure_utc=dep)
    session.add(path)
    session.add(Closure(kind="uplna", from_station="Liptovský Mikuláš", to_station="Poprad-Tatry",
                        start_utc=fixed_now - timedelta(hours=1), end_utc=fixed_now + timedelta(hours=5),
                        status="overene"))
    em = Email(message_id="<zrusenie-70300@zsr.example>", sender="kis@zsr.example", subject="Zrušenie trasy",
               body_text="Trasa vlaku 70300 sa ruší (FIKTÍVNE).")
    session.add(em)
    session.flush()
    ev = ExtractedEvent(email_id=em.id, kind="trasa_zrusena", status="navrh", needs_confirmation=True,
                        data={"train_number": "70300", "date": local(dep).date().isoformat()}, confidence=0.95)
    session.add(ev)
    session.commit()
    before = evaluate_train(session, t.id, fixed_now, "pred zrušením trasy")
    assert any(f"Vlak stojí od {fmt(dep)}" in r for r in before.reasons)

    confirm_event(session, ev.id, "dispecer")
    session.commit()
    assert session.get(Path, path.id).status == "zrusena"
    assert ensure_utc(session.get(Train, t.id).standing_since_utc) == dep
    after = evaluate_train(session, t.id, fixed_now + timedelta(minutes=1), "po zrušení trasy")
    assert any(f"Vlak stojí od {fmt(dep)}" in r for r in after.reasons)
    assert after.decision == "ESKALOVAŤ"


def _stale_agent(now) -> None:
    from vlaky.agent.zdravie import set_kv
    from vlaky.db import session_scope

    with session_scope() as s:
        for key in ("mail_last_ok", "agent_last_cycle"):
            set_kv(s, key, {"ts": (now - timedelta(minutes=120)).isoformat()})


def _row(html: str, rec_id: int) -> str:
    return next(r for r in html.split("<tr>") if f'href="/odporucania/{rec_id}"' in r)


def test_odporucania_bez_cerstvych_dat_neukazu_aktualnu_zelenu(client, fixed_now):
    """[44] Aj zoznam a detail odporúčaní: aktuálne zelené odporúčanie idúceho vlaku sa pri neaktuálnych dátach
    agenta ukáže ako ORANŽOVÁ; staršie odporúčania si farbu ponechajú (záznam o tom, čo sa vtedy odporučilo)."""
    from test_web_spolocne import make_recommendation

    train_id = make_train(number="44903", status="planovany")
    older = make_recommendation(train_id, color="ZELENÁ", decision="VYPRAVIŤ", priority="INFO")
    newest = make_recommendation(train_id, color="ZELENÁ", decision="VYPRAVIŤ", priority="INFO")
    login_as(client, "integ2")
    fresh_page = client.get("/odporucania").text
    assert "semafor-zelena" in _row(fresh_page, newest)  # s čerstvými dátami bez zmeny
    _stale_agent(fixed_now)
    page = client.get("/odporucania").text
    assert "semafor-zelena" not in _row(page, newest) and "bez čerstvých údajov agenta" in _row(page, newest)
    assert "semafor-zelena" in _row(page, older)
    detail = client.get(f"/odporucania/{newest}").text
    assert "decision-zelena" not in detail and "bez čerstvých údajov agenta" in detail
    old_detail = client.get(f"/odporucania/{older}").text  # staršie odporúčanie: pôvodná farba
    assert "bez čerstvých údajov agenta" not in old_detail


def test_zlyhane_potvrdenie_sa_pocita_vo_fronte_suhrne_aj_odovzdavke(session, fixed_now):
    """[43] Potvrdená udalosť, ktorú sa nepodarilo zapísať (avízo pre vlak, ktorý ešte nie je v systéme), čaká vo
    fronte – počítadlo v hlavičke, upozornenie na nástenke, súhrn aj odovzdávka ju musia započítať."""
    from vlaky.agent.odovzdavka import build_report
    from vlaky.models import Email, ExtractedEvent
    from vlaky.notifikacie.odosielanie import send_digest
    from vlaky.web.data import alerts, pending_queue_count

    em = Email(message_id="<avizo-47250@zsr.example>", sender="dispecing@zsr.example", subject="Avízo",
               body_text="Avízo vlaku 47250 (FIKTÍVNE).")
    session.add(em)
    session.flush()
    for i in range(3):  # bežne zapísané potvrdené udalosti sa nepočítajú
        session.add(ExtractedEvent(email_id=em.id, kind="avizo", status="potvrdene", needs_confirmation=True,
                                   data={}, confidence=0.9, validation={"apply_result": {"ok": True}}))
    session.add(ExtractedEvent(email_id=em.id, kind="avizo", status="potvrdene", needs_confirmation=True,
                               data={"train_number": "47250"}, confidence=0.9,
                               validation={"apply_result": {"ok": False, "error": "Vlak 47250 nenájdený"}}))
    session.commit()
    assert pending_queue_count(session) == 1
    assert any("Na potvrdenie čaká 1 udalosť" in a["text"] for a in alerts(session, fixed_now))
    sent = send_digest(session, fixed_now - timedelta(hours=2))
    assert sent and all("Čaká na potvrdenie dispečerom: 1 udalostí" in n.body for n in sent if n.channel == "email")
    content, data = build_report(session, fixed_now)
    assert data["waiting"] == 1 and "Nepotvrdené udalosti z e-mailov: 1" in content


def test_chyba_jedneho_upozornenia_nevrati_uz_odoslane(session, fixed_now, monkeypatch):
    """Stráženie: chyba pri jednej zmene rušňovodiča nesmie vrátiť (rollback) záznamy upozornení, ktoré v tom istom
    cykle už odišli – inak by sa pri trvalej chybe posielali v každom cykle znova. Chyba sa ohlási v cykle."""
    from sqlalchemy import select

    import vlaky.agent.strazenie as strazenie
    from vlaky.models import Driver, DriverShift, Notification, Train

    ids = []
    for number in ("71001", "71002"):
        t = Train(number=number, origin="Čadca", destination="Košice", status="v_jazde")
        d = Driver(name=f"Rušňovodič {number} (FIKTÍVNY)")
        session.add_all([t, d])
        session.flush()
        sh = DriverShift(driver_id=d.id, train_id=t.id, start_utc=fixed_now - timedelta(hours=12, minutes=5),
                         status="prebieha")
        session.add(sh)
        session.flush()
        ids.append(sh.id)
    session.commit()
    real = strazenie.send_internal

    def flaky(session_, kind, subject, body, priority="INFO", dedup_key="", **kw):
        if dedup_key.startswith(f"crew:{ids[1]}:"):
            raise RuntimeError("chyba pri príprave upozornenia (FIKTÍVNA)")
        return real(session_, kind, subject, body, priority, dedup_key=dedup_key, **kw)

    monkeypatch.setattr(strazenie, "send_internal", flaky)
    errors: list = []
    assert strazenie.crew_alerts(session, fixed_now, errors) == 1
    session.commit()
    assert len(errors) == 1 and "RuntimeError" in errors[0] and str(ids[1]) in errors[0]
    first = session.scalars(select(Notification).where(Notification.dedup_key.like(f"crew:{ids[0]}:%"))).all()
    assert first and all(n.status in ("odoslana", "outbox") for n in first)
    # ďalší cyklus: prvé upozornenie sa nezopakuje (záznamy ostali), chyba sa hlási znova
    errors2: list = []
    assert strazenie.crew_alerts(session, fixed_now + timedelta(minutes=1), errors2) == 0
    assert len(errors2) == 1


def test_obchadzka_okolo_vyluky_z_poistky_nie_je_ta_ista_trasa(session, fixed_now, monkeypatch):
    """Výluka Žilina-Teplička – Varín sa vlaku priradí poistkou (jej koľaje v modeli siete trasu nepretínajú).
    Obchádzka sa potom nesmie hľadať len okolo koľají výluky – vyšla by tá istá trasa s tou istou výlukou."""
    from vlaky.hranice import seed_borders
    from vlaky.models import Closure, Path, Train
    from vlaky.planovanie import planovac

    seed_borders(session)
    t = Train(number="54399", run_date=fixed_now.date().isoformat(), origin="Žilina", destination="Poprad-Tatry",
              traction="E", gauge="1435", vmax_kmh=100, status="planovany", ready_utc=fixed_now + timedelta(minutes=30),
              deadline_utc=fixed_now + timedelta(hours=14))
    session.add(t)
    session.flush()
    session.add(Path(train_id=t.id, train_number=t.number, status="pridelena",
                     departure_utc=fixed_now + timedelta(minutes=30)))
    session.add(Closure(kind="uplna", from_station="Žilina-Teplička", to_station="Varín", status="overene",
                        start_utc=fixed_now - timedelta(minutes=30), end_utc=fixed_now + timedelta(hours=6)))
    session.flush()
    routes: list = []
    real = planovac.build_segments

    def spy(net, groute, *a, **kw):
        routes.append(groute)
        return real(net, groute, *a, **kw)

    monkeypatch.setattr(planovac, "build_segments", spy)
    planovac.evaluate_train(session, t.id, fixed_now, "test")
    primary, *alternatives = routes
    for alt in alternatives:
        assert tuple(alt.edges) != tuple(primary.edges)
        names = [s.name for s, _km, _m in alt.stations]
        assert not ("Žilina-Teplička" in names and "Varín" in names and
                    abs(names.index("Varín") - names.index("Žilina-Teplička")) <= 2)
