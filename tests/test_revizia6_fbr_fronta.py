"""6. revízia (strana FB – fronta, oprava po kontrole): výluka koľaje vo fronte len s koľajou (FE1).

Dispečer zmení vo fronte druh úplnej výluky (ROV) na „výluka koľaje“ bez koľaje: výluka koľaje bez koľaje nehovorí, či
ostáva zjazdná iná koľaj – formulár ju odmietne s výzvou doplniť koľaj (v e0a3d0a sa zapísala výluka koľaje bez koľaje
a vlak 95005 dostal VYPRAVIŤ ZELENÁ cez úplnú výluku). S koľajou sa zapíše.

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

from sqlalchemy import select

from vlaky.models import Closure, ExtractedEvent
from test_revizia4_pravidla import env  # noqa: F401 – fixture env
from test_revizia6_fb_druh import _pending_rov
from test_web_spolocne import client, csrf, login_as, web_app  # noqa: F401 – fixtures


def test_fronta_vyluka_kolaje_bez_kolaje_sa_odmietne(env, session, client):
    """FE1 (zvyšok): druh „výluka koľaje“ bez koľaje sa vo fronte nezapíše – chyba „uveďte koľaj“, udalosť ostáva vo
    fronte a dočasný záznam ostáva úplnou výlukou."""
    ev = _pending_rov(env, session)
    session.commit()
    login_as(client, "disp1")
    form = {"csrf_token": csrf(client), "akcia": "upravit", "event_kind": ev.kind, "f__kind": "kolaj", "f__track": ""}
    resp = client.post(f"/fronta/{ev.id}/potvrdit", data=form, follow_redirects=True)
    assert "uveďte koľaj" in resp.text, resp.text[:2000]
    session.expire_all()
    ev = session.get(ExtractedEvent, ev.id)
    assert ev.status == "navrh" and ev.data.get("kind") == "uplna", (ev.status, ev.data)
    assert [c.kind for c in session.scalars(select(Closure)) if c.status != "zrusene"] == ["uplna"]


def test_fronta_vyluka_kolaje_s_kolajou_sa_zapise(env, session, client):
    """Kontrola: „výluka koľaje“ s koľajou 1 sa zapíše ako výluka 1. koľaje."""
    ev = _pending_rov(env, session)
    session.commit()
    login_as(client, "disp1")
    form = {"csrf_token": csrf(client), "akcia": "upravit", "event_kind": ev.kind, "f__kind": "kolaj", "f__track": "1"}
    resp = client.post(f"/fronta/{ev.id}/potvrdit", data=form, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    session.expire_all()
    rows = [(c.kind, c.track, c.status) for c in session.scalars(select(Closure)) if c.status != "zrusene"]
    assert rows == [("kolaj", "1", "overene")], rows


def test_vymazana_kolaj_vyluky_kolaje_sa_odmietne():
    """Vymazaná koľaj výluky koľaje z e-mailu je chyba formulára; iné úpravy výluky koľaje bez koľaje z e-mailu nie."""
    from vlaky.web.views_prehlad import parse_event_edits

    data = {"from_station": "Kysak", "to_station": "Košice", "kind": "kolaj", "track": "2", "note": ""}
    _edits, errors = parse_event_edits({"f__track": ""}, data, "vyluka_nova")
    assert "track" in errors, errors
    blind = {**data, "track": ""}
    edits, errors = parse_event_edits({"f__note": "overené telefonicky"}, blind, "vyluka_nova")
    assert not errors and edits == {"note": "overené telefonicky"}, (edits, errors)


def test_vyluky_formular_vyluka_kolaje_bez_kolaje_sa_odmietne(client):
    """Formulár výluky (/vyluky/nova): výluka koľaje bez koľaje sa neuloží (chyba „zadajte koľaj“); s koľajou áno."""
    from datetime import timedelta

    from vlaky.cas import local, now_utc
    from vlaky.db import session_scope

    login_as(client, "dispecer")
    now = now_utc()
    form = {"csrf_token": csrf(client, "/vyluky"), "from_station": "Kysak", "to_station": "Košice", "kind": "kolaj",
            "track": "", "start_utc": local(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            "end_utc": local(now + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M"), "status": "overene",
            "note": "FIKTÍVNA"}
    r = client.post("/vyluky/nova", data=form, follow_redirects=False)
    assert r.status_code == 422 and "zadajte koľaj" in r.text, r.text[:500]
    with session_scope() as s:
        assert not list(s.scalars(select(Closure)))
    r = client.post("/vyluky/nova", data={**form, "track": "1"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    with session_scope() as s:
        assert [(c.kind, c.track) for c in s.scalars(select(Closure))] == [("kolaj", "1")]
