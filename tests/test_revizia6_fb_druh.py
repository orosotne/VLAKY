"""6. revízia (strana FB – fronta, register a plánovač): druh výluky len zo zoznamu (FE0), koľaj slovom (FE1), texty (FE2).

FE0: druh výluky zadaný vo fronte voľným textom („úplná výluka“, „Uplna“, „výluka koľaje“) sa zapísal ako overená výluka
neznámeho druhu – plánovač ju vynechal a agent odporúčal VYPRAVIŤ ZELENÁ cez úplnú výluku. Teraz: fronta ponúka druh
zo zoznamu (slovenské názvy), slovenský názov sa prevedie na kód, neznámy druh sa odmietne (vo formulári aj pri zápise)
a plánovač neznámy druh v registri počíta ako úplnú výluku s poznámkou.
FE1: koľaj „prvá“, „druhá“, „II. TK“ sa pri výlukách oboch koľají kombinuje ako číslo.
FE2: „Na potvrdenie čakajú 2 udalosti“ (sloveso podľa počtu) a istota s desatinnou čiarkou.

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from vlaky.models import Closure, ExtractedEvent, Notification
from test_revizia3_terminy import drop, events, raw_mail, utc
from test_revizia4_pravidla import _decide, _process, env  # noqa: F401 – fixture env
from test_web_spolocne import client, csrf, login_as, web_app  # noqa: F401 – fixtures

ROV_KE = "ZSR OR Kosice <vyluky.orke@zsr.example>"
_ROV1501 = ("Dobrý deň,\n\núplná výluka v úseku Kysak – Košice dňa 05.11.2026 od 03:00 do 07:00 hod. "
            "(ROV č. 1501/2026).\n\nOR Košice\n")


def _green(r) -> bool:
    return r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"


def _pending_rov(env, session) -> ExtractedEvent:
    """ROV úplnej výluky Kysak – Košice 05.11. 03:00 – 07:00, do 72 h – čaká vo fronte."""
    before = {e.id for e in events(session)}
    drop(env, "r.eml", raw_mail("ROV č. 1501/2026 Kysak - Kosice", _ROV1501, "r-1501", sender=ROV_KE,
                                date="Wed, 04 Nov 2026 23:30:00 +0100"))
    _process(session, utc(2026, 11, 4, 22, 32))
    (ev,) = [e for e in events(session) if e.id not in before]
    assert ev.status == "navrh" and ev.data.get("kind") == "uplna", (ev.status, ev.data)
    return ev


@pytest.mark.parametrize("text,code", [
    ("úplná výluka", "uplna"), ("úplná", "uplna"), ("Uplna", "uplna"), ("UPLNA", "uplna"),
    ("úplná výluka úseku", "uplna"), ("výluka oboch koľají", "uplna"), ("výluka koľaje", "kolaj"),
    ("koľaj", "kolaj"), ("kolaj", "kolaj"), ("vypnuté trakčné vedenie", "trakcne_vedenie"),
    ("trakcne_vedenie", "trakcne_vedenie"), ("Pomalá jazda", "pomala_jazda"), ("obmedzenie rýchlosti", "pomala_jazda"),
    ("obmedzenie (dĺžka, hmotnosť)", "obmedzenie"), ("mimoriadnosť", "mimoriadnost"), ("xyz", None), ("", None),
])
def test_druh_zo_slovenskeho_nazvu(text, code):
    from vlaky.posta.spracovanie import restriction_code

    assert restriction_code(text) == code


def test_fronta_ponuka_druh_zo_zoznamu_a_slovensky_nazov_prevedie_na_kod(env, session, client):
    """FE0 cez web: formulár fronty má pre druh výber (nie voľný text); „úplná výluka“ napísané do staršieho formulára
    sa zapíše ako kód „uplna“ – vlak 95005 Kysak → Košice o 04:00 nedostane VYPRAVIŤ ZELENÁ."""
    ev = _pending_rov(env, session)
    session.commit()
    login_as(client, "disp1")
    page = client.get(f"/fronta?udalost={ev.id}")
    assert page.status_code == 200
    assert f'<select id="e{ev.id}-kind" name="f__kind">' in page.text, page.text[:3000]
    assert '<option value="uplna" selected>úplná výluka úseku</option>' in page.text
    form = {"csrf_token": csrf(client), "akcia": "upravit", "event_kind": ev.kind, "f__kind": "úplná výluka"}
    resp = client.post(f"/fronta/{ev.id}/potvrdit", data=form, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    session.expire_all()
    kinds = [c.kind for c in session.scalars(select(Closure)) if c.status != "zrusene"]
    assert kinds == ["uplna"], kinds
    r = _decide(session, "95005", "Kysak", "Košice", utc(2026, 11, 5, 3), utc(2026, 11, 4, 23))
    assert not _green(r), r.summary


def test_fronta_odmietne_neznamy_druh(env, session, client):
    """FE0: neznámy druh z formulára („xyz“) sa nezapíše – chyba vo formulári, udalosť ostáva vo fronte."""
    ev = _pending_rov(env, session)
    session.commit()
    login_as(client, "disp1")
    form = {"csrf_token": csrf(client), "akcia": "upravit", "event_kind": ev.kind, "f__kind": "xyz"}
    resp = client.post(f"/fronta/{ev.id}/potvrdit", data=form, follow_redirects=True)
    assert "neznámy druh obmedzenia" in resp.text, resp.text[:2000]
    session.expire_all()
    ev = session.get(ExtractedEvent, ev.id)
    assert ev.status == "navrh" and ev.data.get("kind") == "uplna", (ev.status, ev.data)
    assert not [c for c in session.scalars(select(Closure)) if c.kind == "xyz"]


def test_zapis_odmietne_neznamy_druh(env, session):
    """FE0 bez webu: potvrdenie s neznámym druhom výluku nezapíše (správa pre dispečera), dočasný záznam ostáva ako
    úplná výluka – vlak nedostane ZELENÚ."""
    from vlaky.posta.spracovanie import confirm_event

    ev = _pending_rov(env, session)
    ev = confirm_event(session, ev.id, "dispecer1", edits={"kind": "xyz"})
    session.commit()
    res = (ev.validation or {}).get("apply_result") or {}
    assert res.get("ok") is False and "Neznámy druh obmedzenia" in str(res.get("error")), res
    assert res.get("retry") is False, res
    assert not [c for c in session.scalars(select(Closure)) if c.kind == "xyz" and c.status == "overene"]
    r = _decide(session, "95006", "Kysak", "Košice", utc(2026, 11, 5, 3), utc(2026, 11, 4, 23))
    assert not _green(r), r.summary


@pytest.mark.parametrize("kind", ["úplná výluka", "Uplna", "neznamy"])
def test_planovac_pocita_neznamy_druh_ako_uplnu_vyluku(session, kind):
    """FE0 poistka plánovača: výluka neznámeho druhu v registri (zo staršej verzie) je úplná výluka s poznámkou – vlak
    cez ňu nedostane VYPRAVIŤ ZELENÁ (v 631f185 ju plánovač vynechal)."""
    from vlaky.planovanie.planovac import closure_windows

    session.add(Closure(kind=kind, from_station="Kysak", to_station="Košice", start_utc=utc(2026, 11, 5, 2),
                        end_utc=utc(2026, 11, 5, 6), status="overene", source="manual", track="", line_ref="",
                        daily_from="", daily_to="", electric_only=False, external_ref="ROV č. 1501/2026"))
    session.commit()
    (w,) = closure_windows(session, utc(2026, 11, 4), utc(2026, 11, 6))
    assert w.kind == "uplna" and "neznámy druh" in w.note, (w.kind, w.note)
    r = _decide(session, "95007", "Kysak", "Košice", utc(2026, 11, 5, 3), utc(2026, 11, 4, 23))
    assert not _green(r), r.summary


def test_vyluky_ukazu_neznamy_druh_s_upozornenim():
    from vlaky.web.common import closure_kind_text

    assert closure_kind_text("uplna") == "úplná výluka úseku"
    assert "neznámy druh" in closure_kind_text("úplná výluka")


# ====================================================================== FE1 – koľaj slovom
@pytest.mark.parametrize("track,nums", [
    ("prvá", {"1"}), ("druhá", {"2"}), ("II. TK", {"2"}), ("I.", {"1"}), ("I. a II. TK", {"1", "2"}),
    ("druhej traťovej koľaje", {"2"}), ("2. TK", {"2"}), ("pravá", {"P"}), ("TK", set()),
])
def test_kolaj_slovom_a_rimskou_cislicou(track, nums):
    from vlaky.planovanie.planovac import _track_numbers

    assert _track_numbers(track) == nums


def test_vyluky_prvej_a_druhej_kolaje_slovom_su_uplna_vyluka(session):
    """FE1: výluky koľaje „prvá“ a „druhá“ v tom istom čase a úseku – trať nie je zjazdná (úplná výluka)."""
    from vlaky.planovanie.planovac import closure_windows

    for n, track in enumerate(("prvá", "druhá")):
        session.add(Closure(kind="kolaj", from_station="Kysak", to_station="Košice", start_utc=utc(2026, 11, 5, 2),
                            end_utc=utc(2026, 11, 5, 6), status="overene", source="manual", track=track, line_ref="",
                            daily_from="", daily_to="", electric_only=False, external_ref=f"ROV č. 130{n}/2026"))
    session.commit()
    full = [w for w in closure_windows(session, utc(2026, 11, 4), utc(2026, 11, 6)) if w.kind == "uplna"]
    assert full and "obe traťové koľaje" in full[0].note, [(w.kind, w.note) for w in full]
    r = _decide(session, "95008", "Kysak", "Košice", utc(2026, 11, 5, 3), utc(2026, 11, 4, 23))
    assert not _green(r), r.summary


# ====================================================================== FE2 – texty
def test_upozornenie_na_dve_cakajuce_udalosti_ma_sloveso_v_mnoznom_cisle(cfg, session, monkeypatch):
    """„Na potvrdenie čakajú 2 udalosti z e-mailov …“ (v 631f185 „čaká 2 udalosti“)."""
    from test_revizia4_pravidla import KD_ZA
    from vlaky.hranice import seed_borders

    monkeypatch.setattr(cfg, "require_dmarc_pass", True)
    monkeypatch.setattr(cfg, "trusted_authserv_ids", "mx.dopravca.example")
    seed_borders(session)
    session.commit()
    drop(cfg, "km1.eml", raw_mail("Mimoriadnost na trati 180", "Mimoriadna udalosť: porucha koľaje na trati 180 v km "
                                  "251,4 od 02.11.2026 04:30, trať je neprejazdná.\n", "km1", sender=KD_ZA,
                                  date="Mon, 02 Nov 2026 04:55:00 +0100"))
    drop(cfg, "km2.eml", raw_mail("Mimoriadnost na trati 120", "Mimoriadna udalosť: porucha zabezpečovacieho "
                                  "zariadenia na trati 120 v km 88,2 od 02.11.2026 04:40, trať je neprejazdná.\n",
                                  "km2", sender=KD_ZA, date="Mon, 02 Nov 2026 04:56:00 +0100"))
    _process(session, utc(2026, 11, 2, 4))
    subjects = [n.subject for n in session.scalars(select(Notification).where(Notification.kind == "upozornenie"))]
    multi = [x for x in subjects if "Na potvrdenie" in x and "2 udalosti" in x]
    assert multi and all("Na potvrdenie čakajú 2 udalosti" in x for x in multi), subjects


def test_prehlad_cakajucich_udalosti_ma_sloveso_podla_poctu(session, fixed_now):
    from vlaky.web.data import alerts

    def queue_text() -> str:
        return next(a["text"] for a in alerts(session, fixed_now) if a.get("kind") == "fronta")

    for n in range(5):
        session.add(ExtractedEvent(kind="ine", status="navrh", needs_confirmation=True, data={}, confidence=0.5,
                                   created_at=fixed_now - timedelta(minutes=5 + n)))
        session.commit()
        expected = {1: "čaká 1 udalosť", 2: "čakajú 2 udalosti", 3: "čakajú 3 udalosti", 4: "čakajú 4 udalosti",
                    5: "čaká 5 udalostí"}[n + 1]
        assert f"Na potvrdenie {expected} z e-mailov" in queue_text(), queue_text()


def test_istota_vo_fronte_s_desatinnou_ciarkou():
    from vlaky.web.views_prehlad import _issues

    ev = ExtractedEvent(kind="vyluka_nova", validation={"confirmation_reasons": [
        "istota 0.50 je pod prahom 0.80", "termín 24.11.2026 08:00"]})
    assert _issues(ev)["confirmation_reasons"] == ["istota 0,50 je pod prahom 0,80", "termín 24.11.2026 08:00"]
