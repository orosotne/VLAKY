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
