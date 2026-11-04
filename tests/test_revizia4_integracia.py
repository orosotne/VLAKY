"""Štvrtá revízia – integrácia oblastí P4, M4 a W4: požiadavky medzi oblasťami, ktoré doplnil integrátor.

- súhrn a denná správa: počty so správnym tvarom („1 udalosť“, „3 udalosti“, „5 udalostí“) a stav agenta po
  slovensky (bez kľúčov „schranka:“, „dorucenie:“, „konfiguracia:“),
- kroky v detaile odporúčania: idúci vlak sa nevypravuje (POČKAŤ pred cieľom, odstavenie počas jazdy).
"""
from __future__ import annotations

from sqlalchemy import select

from vlaky.agent import beh
from vlaky.cas import set_fake_now
from vlaky.db import session_scope
from vlaky.models import Notification, Recommendation, Train
from vlaky.notifikacie.texty import count
from vlaky.web.views_prehlad import _next_steps


def test_pocet_s_tvarom_podstatneho_mena():
    tvary = ("udalosť", "udalosti", "udalostí")
    assert count(1, *tvary) == "1 udalosť"
    assert [count(n, *tvary) for n in (2, 3, 4)] == ["2 udalosti", "3 udalosti", "4 udalosti"]
    assert count(5, *tvary) == "5 udalostí" and count(0, *tvary) == "0 udalostí"
    assert count(12, "e-mail", "e-maily", "e-mailov") == "12 e-mailov"


def test_denna_sprava_bez_surovych_klucov_stavu(cfg, fixed_now):
    now = fixed_now.replace(hour=cfg.daily_report_hour + 1)
    set_fake_now(now)
    beh.run_cycle(now)
    with session_scope() as s:
        body = s.scalar(select(Notification.body).where(Notification.subject.contains("Denná správa")))
    assert body is not None
    lines = body.splitlines()
    assert not [x for x in lines if x.split(":")[0] in ("schranka", "dorucenie", "konfiguracia", "cyklus")], body
    assert "posledný stav čítania schránky: v poriadku" in body


def _rec(decision: str, color: str, train_status: str, **chosen) -> Recommendation:
    train = Train(number="95999", origin="Žilina", destination="Košice", status=train_status)
    return Recommendation(decision=decision, color=color, priority="DÔLEŽITÉ", summary="", chosen=chosen,
                          path_proposal={}, crew={}, train=train)


def test_kroky_idúceho_vlaku_pocka_pred_cielom_nevypravuje():
    steps = _next_steps(_rec("POČKAŤ", "ORANŽOVÁ", "v_jazde", until_entry=True, floating=True))
    text = " ".join(steps)
    assert "vypraviť" not in text, steps
    assert any(x.startswith("Počkať pred vjazdom do cieľa") for x in steps), steps
    assert any(x.startswith("ORANŽOVÁ: vlak je v jazde") for x in steps), steps


def test_kroky_idúceho_vlaku_pri_odstaveni_nevypravuje():
    steps = _next_steps(_rec("ODSTAVIŤ", "ORANŽOVÁ", "v_jazde"))
    assert not any("vypraviť" in x for x in steps), steps
    assert any(x.startswith("ORANŽOVÁ: vlak je v jazde") for x in steps), steps


def test_kroky_vlaku_pred_odchodom_ostavaju():
    steps = _next_steps(_rec("POČKAŤ", "ORANŽOVÁ", "planovany"))
    assert "ORANŽOVÁ: vypraviť len po výslovnom potvrdení dispečera (zvážiť dôvody nižšie)." in steps
    assert not any(x.startswith("Počkať pred vjazdom") for x in steps)


# ---------------------------------------------------------------- integrácia s M4 (fronta, upozornenia, odkazy)
def test_popis_udalosti_bez_miesta_a_terminu():
    from vlaky.models import ExtractedEvent
    from vlaky.posta.spracovanie import describe_event

    ev = ExtractedEvent(id=7, kind="vyluka_zmena", data={})
    text = describe_event(ev)
    assert "? – ?" not in text, text
    assert "(miesto sa nepodarilo prečítať)" in text and "(termín sa nepodarilo prečítať)" in text, text
    known = ExtractedEvent(id=8, kind="vyluka_nova", data={"from_station": "Kysak", "to_station": "Košice",
                                                           "start": "2026-11-20T07:00:00+00:00",
                                                           "end": "2026-11-20T13:00:00+00:00"})
    assert describe_event(known).startswith("nová výluka Kysak – Košice 20.11. 08:00 – 20.11. 14:00"), \
        describe_event(known)
    incident = ExtractedEvent(id=9, kind="mimoriadnost", data={"start": "2026-11-02T03:30:00+00:00"})
    assert "(miesto sa nepodarilo prečítať)" in describe_event(incident)
    assert "do odvolania" in describe_event(incident)


def test_upozornenie_na_dve_cakajuce_udalosti_ma_spravny_tvar(cfg, session, monkeypatch):
    """Dve čakajúce mimoriadnosti bez dopravne v jednom e-maile: predmet „2 udalosti“ (nie „2 udalostí“)."""
    from test_revizia4_pravidla import KD_ZA, _process  # rovnaké prostredie ako testy oblasti M4
    from test_revizia3_terminy import drop, events, raw_mail, utc
    from vlaky.hranice import seed_borders

    monkeypatch.setattr(cfg, "require_dmarc_pass", True)
    monkeypatch.setattr(cfg, "trusted_authserv_ids", "mx.dopravca.example")
    seed_borders(session)
    session.commit()
    now = utc(2026, 11, 2, 4)
    drop(cfg, "km1.eml", raw_mail("Mimoriadnost na trati 180", "Mimoriadna udalosť: porucha koľaje na trati 180 v km "
                                  "251,4 od 02.11.2026 04:30, trať je neprejazdná.\n", "km1", sender=KD_ZA,
                                  date="Mon, 02 Nov 2026 04:55:00 +0100"))
    drop(cfg, "km2.eml", raw_mail("Mimoriadnost na trati 120", "Mimoriadna udalosť: porucha zabezpečovacieho "
                                  "zariadenia na trati 120 v km 88,2 od 02.11.2026 04:40, trať je neprejazdná.\n",
                                  "km2", sender=KD_ZA, date="Mon, 02 Nov 2026 04:56:00 +0100"))
    _process(session, now)
    assert len([e for e in events(session) if e.kind == "mimoriadnost"]) == 2
    subjects = [n.subject for n in session.scalars(select(Notification).where(Notification.kind == "upozornenie"))]
    multi = [x for x in subjects if "Na potvrdenie čaká 2 " in x]
    assert multi and all("2 udalosti z e-mailov" in x for x in multi), subjects
    assert not any("udalostí" in x for x in multi), subjects


def test_odkazy_na_udalost_vo_vylukach_a_po_chybe_potvrdenia_vedu_na_formular():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    vyluky = (root / "vlaky" / "web" / "templates" / "vyluky.html").read_text(encoding="utf-8")
    assert '/fronta/udalost/{{ c.source_event_id }}' in vyluky and "/fronta#udalost-" not in vyluky
    evidencia = (root / "vlaky" / "web" / "views_evidencia.py").read_text(encoding="utf-8")
    assert 'redirect(f"/fronta#udalost-' not in evidencia
    assert evidencia.count('/fronta?udalost={event_id}#udalost-{event_id}') >= 2
    fronta = (root / "vlaky" / "web" / "templates" / "fronta.html").read_text(encoding="utf-8")
    assert "(a.size / 1024)|num(1)" in fronta and "|round(1) }} kB" not in fronta
