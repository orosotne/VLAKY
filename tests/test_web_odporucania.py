"""Web: odporúčania – detail, prijatie, úprava, zamietnutie (s dôvodom), audit."""
from __future__ import annotations

from sqlalchemy import select

from test_web_spolocne import csrf, login_as, make_recommendation, make_train

pytest_plugins = ["test_web_spolocne"]


def _audit(action: str, rec_id: int):
    from vlaky.db import session_scope
    from vlaky.models import AuditLog

    with session_scope() as s:
        a = s.scalar(select(AuditLog).where(AuditLog.action == action, AuditLog.entity_id == str(rec_id)))
        return (a.actor, dict(a.detail)) if a else None


def test_detail_odporucania(client):
    rec_id = make_recommendation(make_train(number="49001"))
    login_as(client, "obchod", "obchod")
    page = client.get(f"/odporucania/{rec_id}").text
    assert "POČKAŤ" in page and "ORANŽOVÁ" in page
    assert "Vystriedať najneskôr o" in page and "Ján FIKTÍVNY" in page
    assert "Porovnanie variantov" in page and "82 %" in page and "1 234 €" in page
    assert "Okná bezpečného odchodu" in page and "Okno na objednanie odchodu" in page
    assert "Objednať ad hoc trasu u ŽSR" in page
    assert "Výluka Krásno nad Kysucou – Čadca (FIKTÍVNE)" in page
    assert "Prijať" not in page  # obchod nerozhoduje


def test_prijatie_odporucania(client):
    from vlaky.db import session_scope
    from vlaky.models import Recommendation

    rec_id = make_recommendation(make_train(number="49002"))
    login_as(client, "dispecer")
    token = csrf(client, f"/odporucania/{rec_id}")
    r = client.post(f"/odporucania/{rec_id}/rozhodnutie", data={"csrf_token": token, "akcia": "prijat"},
                    follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        rec = s.get(Recommendation, rec_id)
        assert rec.state == "prijate" and rec.decided_by == "dispecer" and rec.decided_at is not None
    actor, detail = _audit("recommendation_prijat", rec_id)
    assert actor == "dispecer" and detail["state"] == "prijate" and detail["previous_state"] == "nove"
    assert "Prijaté – dispecer" in client.get(f"/odporucania/{rec_id}").text


def test_zamietnutie_vyzaduje_dovod(client):
    from vlaky.db import session_scope
    from vlaky.models import Recommendation

    rec_id = make_recommendation(make_train(number="49003"))
    login_as(client, "dispecer")
    token = csrf(client, f"/odporucania/{rec_id}")
    r = client.post(f"/odporucania/{rec_id}/rozhodnutie", data={"csrf_token": token, "akcia": "zamietnut"},
                    follow_redirects=True)
    assert "uveďte dôvod" in r.text
    with session_scope() as s:
        assert s.get(Recommendation, rec_id).state == "nove"
    r = client.post(f"/odporucania/{rec_id}/rozhodnutie", follow_redirects=False,
                    data={"csrf_token": token, "akcia": "zamietnut", "poznamka": "ŽSR pridelila inú trasu"})
    assert r.status_code == 303
    with session_scope() as s:
        rec = s.get(Recommendation, rec_id)
        assert rec.state == "zamietnute" and rec.dispatcher_note == "ŽSR pridelila inú trasu"
    actor, detail = _audit("recommendation_zamietnut", rec_id)
    assert detail["note"] == "ŽSR pridelila inú trasu"


def test_uprava_a_zastarane(client):
    from vlaky.db import session_scope
    from vlaky.models import Recommendation

    train_id = make_train(number="49004")
    old_id = make_recommendation(train_id, state="zastarane")
    new_id = make_recommendation(train_id)
    login_as(client, "veduci", "veduci")
    token = csrf(client, f"/odporucania/{new_id}")
    old_page = client.get(f"/odporucania/{old_id}").text
    assert "Zobraziť aktuálne odporúčanie" in old_page
    r = client.post(f"/odporucania/{old_id}/rozhodnutie", data={"csrf_token": token, "akcia": "prijat"},
                    follow_redirects=True)
    assert "zastarané" in r.text
    r = client.post(f"/odporucania/{new_id}/rozhodnutie", follow_redirects=False,
                    data={"csrf_token": token, "akcia": "upravit", "poznamka": "odchod o 30 min neskôr"})
    assert r.status_code == 303
    with session_scope() as s:
        assert s.get(Recommendation, new_id).state == "upravene"
        assert s.get(Recommendation, old_id).state == "zastarane"
    listing = client.get("/odporucania").text
    assert "49004" in listing and "upravené" in listing
    assert client.get("/odporucania?farba=ČERVENÁ").status_code == 200


def test_obchod_nemoze_rozhodnut(client):
    rec_id = make_recommendation(make_train(number="49005"))
    login_as(client, "obchod", "obchod")
    token = csrf(client, f"/odporucania/{rec_id}")
    r = client.post(f"/odporucania/{rec_id}/rozhodnutie", data={"csrf_token": token, "akcia": "prijat"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_koncepty_sprav_na_kopirovanie(client):
    train_id = make_train(number="49010")
    rec_id = make_recommendation(train_id, path_proposal={
        "route": "hlavná", "stations": [], "needs_new_path": True,
        "drafts": {"zsr_path_request": "Žiadame o pridelenie ad hoc trasy pre vlak 49010 (FIKTÍVNE).\nČadca → Žilina",
                   "customer": "Vážený zákazník, <b>vlak</b> 49010 príde o 12:30 (FIKTÍVNE)."}})
    login_as(client, "dispecer")
    page = client.get(f"/odporucania/{rec_id}").text
    assert "Koncepty správ" in page and "Agent nič neposiela" in page
    assert "Žiadame o pridelenie ad hoc trasy pre vlak 49010 (FIKTÍVNE)." in page
    assert "&lt;b&gt;vlak&lt;/b&gt;" in page  # text konceptu je escapovaný
    assert 'data-copy="#koncept-zsr_path_request"' in page and 'data-copy="#koncept-customer"' in page
    assert "readonly" in page
