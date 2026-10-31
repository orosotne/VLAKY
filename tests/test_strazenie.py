"""Stráženie: 13 h limit (90/60/30 min), státie vlaku, GDPR uchovávanie."""
from datetime import timedelta

from sqlalchemy import select

from vlaky.agent.strazenie import crew_alerts, retention_purge, standing_alerts
from vlaky.models import Driver, DriverShift, Email, Notification, PositionReport, Train


def _shift(session, start, number="7"):
    t = Train(number=number, origin="Čadca", destination="Košice", status="v_jazde")
    d = Driver(name="Rušňovodič T")
    session.add_all([t, d])
    session.flush()
    sh = DriverShift(driver_id=d.id, train_id=t.id, start_utc=start, status="prebieha")
    session.add(sh)
    session.flush()
    return t, sh


def test_upozornenie_60_a_30_min(cfg, session, fixed_now):
    _shift(session, fixed_now - timedelta(hours=12, minutes=5))  # do limitu 55 min
    assert crew_alerts(session, fixed_now) == 1
    n = session.scalar(select(Notification).where(Notification.kind == "upozornenie"))
    assert "zostáva 55 min" in n.subject and n.priority == "DÔLEŽITÉ"
    assert crew_alerts(session, fixed_now + timedelta(minutes=5)) == 0  # rovnaká úroveň sa neopakuje
    assert crew_alerts(session, fixed_now + timedelta(minutes=30)) == 1  # 25 min → KRITICKÉ
    last = session.scalars(select(Notification).order_by(Notification.id.desc())).first()
    assert last.priority == "KRITICKÉ"


def test_prekroceny_limit(cfg, session, fixed_now):
    _shift(session, fixed_now - timedelta(hours=13, minutes=10))
    assert crew_alerts(session, fixed_now) == 1
    n = session.scalar(select(Notification).where(Notification.kind == "upozornenie"))
    assert "PREKROČENÝ" in n.subject and n.priority == "KRITICKÉ"


def test_statie_vlaku(cfg, session, fixed_now):
    t, _ = _shift(session, fixed_now - timedelta(hours=1))
    for k in range(4):  # 4 polohy za 30 min na tom istom mieste
        session.add(PositionReport(train_id=t.id, ts_utc=fixed_now - timedelta(minutes=30 - k * 10),
                                   lat=49.2265, lon=18.7395, source="gps", nearest_station="Žilina"))
    session.flush()
    assert standing_alerts(session, fixed_now) == 1
    assert standing_alerts(session, fixed_now) == 0


def test_gdpr_uchovavanie(cfg, session, fixed_now):
    session.add(PositionReport(ts_utc=fixed_now - timedelta(days=100), lat=48.0, lon=17.0, source="gps"))
    session.add(PositionReport(ts_utc=fixed_now - timedelta(days=1), lat=48.0, lon=17.0, source="gps"))
    session.add(Email(message_id="<x@y>", received_at=fixed_now - timedelta(days=400), body_text="osobné údaje"))
    session.flush()
    out = retention_purge(session, fixed_now)
    assert out == {"positions_deleted": 1, "emails_redacted": 1}
    assert session.scalar(select(Email)).body_text == ""
