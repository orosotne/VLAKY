"""Beh agenta: cyklus bez schránky, objednávka → návrh prepravy, zámok, zdravie."""
from datetime import timedelta

from sqlalchemy import select

from vlaky.agent.beh import acquire_lock, propose_new_orders, run_cycle
from vlaky.agent.odovzdavka import create_report
from vlaky.agent.zdravie import health_status, set_kv
from vlaky.db import session_scope
from vlaky.hranice import seed_borders
from vlaky.models import AgentHealth, Notification, Order, Train


def test_objednavka_navrh_prepravy(cfg, fixed_now):
    with session_scope() as s:
        seed_borders(s)
        s.add(Order(customer="Zákazník X (fiktívny)", origin="Kúty", destination="Štúrovo",
                    earliest_departure_utc=fixed_now + timedelta(hours=8),
                    deadline_utc=fixed_now + timedelta(hours=20), cargo="kontajnery", wagons=20))
    assert propose_new_orders(fixed_now) == 1
    with session_scope() as s:
        order = s.scalar(select(Order))
        assert order.status == "navrhnuta"
        notes = list(s.scalars(select(Notification).where(Notification.kind == "objednavka")))
        assert notes and "NÁVRH TRASY" in notes[0].body and "Kúty" in notes[0].body
    assert propose_new_orders(fixed_now) == 0  # už navrhnutá


def test_cyklus_a_zdravie(cfg, fixed_now):
    with session_scope() as s:
        seed_borders(s)
        s.add(Train(number="1", origin="Čadca", destination="Košice", traction="E", status="planovany",
                    ready_utc=fixed_now + timedelta(hours=1), deadline_utc=fixed_now + timedelta(hours=12)))
    out = run_cycle(fixed_now, force_evaluate=True)
    assert out["evaluated"] == 1
    with session_scope() as s:
        assert s.scalar(select(AgentHealth).where(AgentHealth.component == "cyklus")) is not None


def test_zamok_jednej_instancie(cfg, fixed_now):
    with session_scope() as s:
        set_kv(s, "agent_lock", {"owner": "iny-server:1", "ts": fixed_now.isoformat()})
    with session_scope() as s:
        assert acquire_lock(s) is False


def test_odovzdavka_sluzby(cfg, fixed_now):
    with session_scope() as s:
        seed_borders(s)
        rep = create_report(s, fixed_now)
        assert "ODOVZDÁVKA SLUŽBY" in rep.content
