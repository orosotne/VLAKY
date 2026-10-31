"""Web: reporty (M13, vyhodnotenie tieňovej prevádzky), export CSV a kalibrácia z histórie."""
from __future__ import annotations

import csv
import io
import sys
import types
from datetime import timedelta

from sqlalchemy import select

from test_web_spolocne import csrf, login_as, make_recommendation, make_train

pytest_plugins = ["test_web_spolocne"]


def _seed(now):
    from vlaky.db import session_scope
    from vlaky.models import BorderCrossing, Email, ExtractedEvent, Handover, Notification

    train_id = make_train(number="81001")
    make_recommendation(train_id, state="prijate", color="ZELENÁ", decision="VYPRAVIŤ", decided_by="dispecer")
    make_recommendation(train_id, state="upravene", color="ORANŽOVÁ", decision="POČKAŤ", decided_by="dispecer")
    make_recommendation(train_id, state="zamietnute", color="ČERVENÁ", decision="ODSTAVIŤ", decided_by="dispecer",
                        dispatcher_note="ŽSR pridelila inú trasu (FIKTÍVNE)",
                        crew={"status": "ČERVENÁ", "relieve_by": now.isoformat()})
    make_recommendation(train_id, state="nove", color="ORANŽOVÁ", priority="KRITICKÉ")
    with session_scope() as s:
        s.add_all([Notification(channel="email", recipient="d@dopravca.example", status="odoslana", created_at=now),
                   Notification(channel="sms", recipient="+421999", status="zablokovana", created_at=now)])
        mails = [Email(message_id=f"<r{i}@x>", sender="a@zsr.example", subject=subj, verdict=v, received_at=now)
                 for i, (v, subj) in enumerate([("ok", "Výluka"), ("karantena", "Spam"),
                                                ("podozrivy", "=HYPERLINK(\"http://zly.example\")")])]
        s.add_all(mails)
        s.flush()
        s.add_all([
            ExtractedEvent(email_id=mails[0].id, kind="vyluka_nova", status="aplikovane", confidence=0.97,
                           needs_confirmation=False, created_at=now),
            ExtractedEvent(email_id=mails[2].id, kind="avizo", status="aplikovane", confidence=0.8, created_at=now,
                           confirmed_by="dispecer", confirmed_at=now,
                           validation={"edits": [{"by": "dispecer", "changes": {"eta": {}}}]}),
            ExtractedEvent(kind="vyluka_zmena", status="zamietnute", confidence=0.5, created_at=now),
            ExtractedEvent(kind="objednavka", status="navrh", confidence=0.6, created_at=now)])
        kuty = s.scalar(select(BorderCrossing).where(BorderCrossing.code == "KUTY"))
        for minutes in (30, 60, 90):
            arrived = now - timedelta(hours=3)
            s.add(Handover(train_id=train_id, border_id=kuty.id, direction="vstup", status="prevzaty",
                           arrived_utc=arrived, taken_over_utc=arrived + timedelta(minutes=minutes)))


def test_reporty_kpi(client, fixed_now):
    _seed(fixed_now)
    login_as(client, "veduci", "veduci")
    page = client.get("/reporty", params={"od": "2026-11-01", "do": "2026-11-02"}).text
    assert "ŽSR pridelila inú trasu (FIKTÍVNE)" in page
    assert "67 %" in page  # prijaté vrátane úprav: 2 z 3 rozhodnutých
    assert "Zablokované správy" in page and "zablokovana" in page
    assert "karanténa: 1" in page and "podozrivé: 1" in page
    assert "Zapísané automaticky</dt><dd>1" in page and "z toho s opravou 1" in page
    assert "Kúty" in page and "<td>3</td><td>60</td><td>84</td>" in page  # P50 60, P90 84 min
    assert "Odporúčania s ČERVENOU" in page
    empty = client.get("/reporty", params={"od": "2026-10-01", "do": "2026-10-02"}).text
    assert "Žiadne zamietnuté odporúčania" in empty


def test_export_csv(client, fixed_now):
    _seed(fixed_now)
    login_as(client, "dispecer")
    r = client.get("/reporty/export/odporucania.csv", params={"od": "2026-11-01", "do": "2026-11-02"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"] and "odporucania_2026-11-01_2026-11-02.csv" in \
        r.headers["content-disposition"]
    assert r.content.startswith(b"\xef\xbb\xbf")  # BOM pre Excel
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))
    assert rows[0][4] == "rozhodnutie" and len(rows) == 5
    assert any(row[10] == "ŽSR pridelila inú trasu (FIKTÍVNE)" for row in rows[1:])
    ev = client.get("/reporty/export/udalosti.csv", params={"od": "2026-11-01", "do": "2026-11-02"}).content
    text = ev.decode("utf-8-sig")
    assert "'=HYPERLINK" in text  # vzorec z e-mailu je neutralizovaný
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    assert any(row[8] == "1" for row in rows[1:])  # počet úprav dispečera
    ho = client.get("/reporty/export/preberania.csv", params={"od": "2026-11-01", "do": "2026-11-02"})
    assert ho.status_code == 200 and "Kúty" in ho.content.decode("utf-8-sig")
    assert client.get("/reporty/export/nieco.csv").status_code == 404


def test_opravnenia_reportov(client):
    login_as(client, "obchod", "obchod")
    assert client.get("/reporty").status_code == 403
    assert client.get("/reporty/export/odporucania.csv").status_code == 403
    client.cookies.clear()
    login_as(client, "dispecer")
    page = client.get("/reporty").text
    assert "Reporty a vyhodnotenie" in page and "Kalibrácia z histórie" not in page
    assert client.post("/reporty/kalibracia", data={"csrf_token": csrf(client)},
                       follow_redirects=False).status_code == 403


def test_kalibracia_navrh_a_pouzitie(client, monkeypatch):
    calls = {"propose": 0, "apply": []}
    proposal = {"period_days": 120, "min_samples": 30, "notes": [], "delay": {},
                "borders": [{"border_id": 1, "code": "KUTY", "name": "Kúty – Lanžhot", "n": 3, "p50": 55.0,
                             "p90": 80.0, "current_p50": 60.0, "current_p90": 150.0, "enough": False}]}
    mod = types.ModuleType("vlaky.planovanie.kalibracia")

    def propose_calibration(session, now=None, min_samples=30, days=120):
        calls["propose"] += 1
        return proposal

    def apply_calibration(session, prop, actor, include_small=False):
        calls["apply"].append((prop is proposal, actor, include_small))
        return {"borders": [{"code": "KUTY"}] if include_small else [], "delay": None}

    mod.propose_calibration, mod.apply_calibration = propose_calibration, apply_calibration
    monkeypatch.setitem(sys.modules, "vlaky.planovanie.kalibracia", mod)
    login_as(client, "veduci", "veduci")
    page = client.get("/reporty").text
    assert "Kalibrácia z histórie" in page and "55.0 / 80.0" in page and "Použiť návrh" in page
    token = csrf(client)
    before = calls["propose"]
    r = client.post("/reporty/kalibracia", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and calls["propose"] == before + 1  # návrh sa prepočítal na serveri pri POST
    assert calls["apply"][-1] == (True, "veduci", False)
    assert "Nič sa nezmenilo" in client.get(r.headers["location"]).text
    r = client.post("/reporty/kalibracia", data={"csrf_token": csrf(client), "aj_male": "1"}, follow_redirects=True)
    assert calls["apply"][-1] == (True, "veduci", True) and "Kalibrácia použitá" in r.text


def test_kalibracia_realnym_modulom(client, fixed_now):
    import pytest

    pytest.importorskip("vlaky.planovanie.kalibracia")
    from vlaky.db import session_scope
    from vlaky.models import AuditLog, BorderCrossing

    _seed(fixed_now)
    login_as(client, "veduci", "veduci")
    assert "KUTY" in client.get("/reporty").text
    r = client.post("/reporty/kalibracia", data={"csrf_token": csrf(client), "aj_male": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        kuty = s.scalar(select(BorderCrossing).where(BorderCrossing.code == "KUTY"))
        assert kuty.dwell_p50_min == 60.0 and kuty.dwell_p90_min >= 60.0
        assert s.scalar(select(AuditLog).where(AuditLog.action == "calibration_applied")) is not None
