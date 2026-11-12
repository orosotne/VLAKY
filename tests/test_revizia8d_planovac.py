"""Plánovač: nezávislá kontrola 8. kola, tretí doplnok (výluka do odvolania pri zdržaní, porovnanie variantov, súhrnné
okná výluk koľají, okná za koncom obdobia so zdržaním, návrh trasy podľa simulácie, história výluk, texty, odovzdávka
a prevádzková príručka).

N1 – do výluky do odvolania vojde len jazda so zdržaním (P90): príchod P90, náklad, koniec služby rušňovodiča, mapa
     (eta_p90), odovzdávka, koncepty správ ani e-mail k objednávke neuvádzajú čas z jej technického konca; odchod
     a predpokladaný príchod (medián) ostávajú.
N2 – porovnanie variantov uvedie odchod odporúčanej varianty, ktorej odchod je známy (nie je známy len príchod P90).
N3 – súhrnné okná (rezerva uložená nad limitom formulára) nevytvoria úplnú výluku z výluk oboch koľají, ktorá
     neexistuje; skutočné prekrytia sa počítajú z presných okien; žiadna varianta (ani obchádzka) nie je ZELENÁ, keď
     súhrnné okná zasahujú plán.
N4 – výluka do odvolania, ktorá začne až po konci prehľadávaného obdobia, sa posudzuje aj podľa jazdy so zdržaním (vlak
     aj objednávka).
N5 – návrh trasy ani koncept objednávky trasy neprejde úsekom, pred ktorým simulácia čaká, a nenatiahne zvyšok jazdy
     na dni – ak návrh nemôže sledovať simuláciu, nepripraví sa a dôvod to povie.
N6 – pri uloženej rezerve 1 000 000 min sa stará história výluk počíta súhrnne (úseky simulácie a dôvody nerastú
     s históriou registra, prepočet nie je o rád pomalší).
N7 – Nastavenia ukážu uloženú hodnotu presne, dve rezervy nad limitom sú v každej správe v množnom čísle.
N8 – odovzdávka služby: pomalá jazda len pre elektrickú trakciu s rýchlosťou.
N9 – prevádzková príručka a návod na monitoring uvádzajú chybu „nastavenia rozhodovania“.

Všetky vlaky, výluky, osoby a adresy sú fiktívne.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from vlaky.cas import set_fake_now
from vlaky.hranice import seed_borders
from vlaky.models import Closure, Notification, Order, ParameterSet, Path as TrainPath, Train

TZ = ZoneInfo("Europe/Bratislava")
UTC = timezone.utc
A, B, K, H = "Ružomberok", "Liptovský Mikuláš", "Kraľovany", "Liptovský Hrádok"
NOW = datetime(2026, 11, 21, 9, tzinfo=TZ).astimezone(UTC)
READY = datetime(2026, 11, 25, 10, tzinfo=TZ).astimezone(UTC)
TECH_END = "23.12."  # technický koniec výluky do odvolania (23.12. 08:00) – nie je koniec výluky
ROOT = Path(__file__).resolve().parent.parent


def loc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=TZ).astimezone(UTC)


def _l(dt) -> str:
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
    return dt.astimezone(TZ).strftime("%d.%m. %H:%M")


def _iso(value) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


@pytest.fixture()
def env(cfg, session):
    seed_borders(session)
    session.commit()
    return cfg


def _closure(session, kind: str = "uplna", track: str = "", daily: tuple = ("", ""), start=None, end=None,
             until_revoked: bool = True, ref: str = "8900/2026", a: str = A, b: str = B, status: str = "overene",
             speed=None, electric: bool = False, event_id=None) -> Closure:
    c = Closure(kind=kind, from_station=a, to_station=b, line_ref="", track=track,
                start_utc=start or loc(2026, 11, 23, 8), end_utc=end or loc(2026, 12, 23, 8),
                daily_from=daily[0], daily_to=daily[1], electric_only=electric, speed_kmh=speed, external_ref=ref,
                until_revoked=until_revoked, status=status, source="email" if event_id else "rucne",
                source_event_id=event_id, note="FIKTÍVNA")
    session.add(c)
    session.commit()
    return c


def _params(session, **values) -> ParameterSet:
    ps = ParameterSet(valid_from=NOW - timedelta(days=1), created_by="veduci", values=values, note="FIKTÍVNE")
    session.add(ps)
    session.commit()
    return ps


_N = [89900]


def _train(session, ready: datetime, now: datetime = NOW, origin: str = A, destination: str = B,
           deadline_h: float = 8, path_at: datetime | None = None, status: str = "planovany") -> Train:
    from vlaky.agent.zdravie import set_kv

    _N[0] += 1
    set_fake_now(now)
    t = Train(number=str(_N[0]), origin=origin, destination=destination, traction="E", gauge="1435", vmax_kmh=80,
              status=status, ready_utc=ready, deadline_utc=ready + timedelta(hours=deadline_h))
    session.add(t)
    session.flush()
    session.add(TrainPath(train_id=t.id, train_number=t.number, kind="ad_hoc", status="pridelena",
                          departure_utc=path_at or ready))
    session.commit()
    set_kv(session, "mail_last_ok", {"ts": (now - timedelta(minutes=2)).isoformat(), "fetched": 0})
    session.commit()
    return t


def _decide(session, ready: datetime = READY, now: datetime = NOW, **kw):
    from vlaky.planovanie.planovac import evaluate_train

    t = _train(session, ready, now, **kw)
    rec = evaluate_train(session, t.id, now, "kontrola 8d")
    session.commit()
    return rec


def _order(session, ready: datetime = READY, origin: str = A, destination: str = B, hours: float = 8) -> Order:
    set_fake_now(NOW)
    o = Order(customer="FIKTÍVNY zákazník", origin=origin, destination=destination, earliest_departure_utc=ready,
              deadline_utc=ready + timedelta(hours=hours), status="nova")
    session.add(o)
    session.commit()
    return o


def _texts(rec) -> str:
    return " | ".join([rec.summary or ""] + [str(r) for r in rec.reasons or []])


def _web(paths: list, user: str = "dispecer", role: str = "dispecer") -> dict:
    from fastapi.testclient import TestClient

    from test_web_spolocne import login_as
    from vlaky.web.app import create_app

    out = {}
    with TestClient(create_app(auto_evaluate=False)) as client:
        login_as(client, user, role)
        for p in paths:
            r = client.get(p)
            out[p] = r.json() if p.startswith("/api/") else r.text
    return out


def _plain(page: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))


def _email_event(session) -> int:
    from vlaky.models import Email, ExtractedEvent

    m = Email(message_id=f"<n3-{_N[0]}@zsr.example>", sender="vyluky@zsr.example", subject="FIKTÍVNA")
    session.add(m)
    session.flush()
    ev = ExtractedEvent(email_id=m.id, kind="pomala_jazda", confidence=0.9, status="aplikovane",
                        needs_confirmation=False, data={})
    session.add(ev)
    session.commit()
    return ev.id


# ------------------------------------------------------------------ N1 – výluka do odvolania len pri zdržaní
def _p90_closure(session, minutes: float = 54) -> Closure:
    """Výluka do odvolania Ružomberok – Liptovský Mikuláš od 25.11. 10:54 (technický koniec 23.12. 08:00): medián vlaku
    pripraveného 25.11. 10:00 úsek prejde pred jej začiatkom (s rezervou), asi každá siedma jazda nie."""
    return _closure(session, start=READY + timedelta(minutes=minutes), end=loc(2026, 12, 23, 8), ref="8901/2026")


def test_n1_p90_vyluka_do_odvolania_bez_casov_z_technickeho_konca(env, session):
    """[N1] Do výluky do odvolania vojde len jazda so zdržaním: súhrn, e-mail, web (príchod P50 / P90, náklad, koniec
    služby rušňovodiča), mapa (eta_p90), odovzdávka služby ani dôvody neuvádzajú čas z jej technického konca (23.12.);
    odchod a predpokladaný príchod ostávajú, príchod P90 a náklad „nie je známy“; návrh trasy ani koncepty správ nie
    sú (dôvod to povie)."""
    from vlaky.agent.odovzdavka import build_report
    from vlaky.notifikacie.texty import recommendation_body

    late = _p90_closure(session)
    rec = _decide(session)
    assert "s odchodom 25.11. 10:00, príchod 25.11. 10:" in rec.summary, rec.summary
    assert "90 % jázd: príchod nie je známy – výluka Ružomberok – Liptovský Mikuláš platí od 25.11. 10:54 do " \
           "odvolania" in rec.summary, rec.summary
    assert TECH_END not in _texts(rec), _texts(rec)
    ch = rec.chosen
    assert ch["arrival_p50"] and ch["arrival_p90"] is None and ch["expected_cost"] is None, ch
    assert ch.get("p90_do_odvolania") and not ch.get("do_odvolania"), ch
    assert (rec.crew or {}).get("end_p50") and (rec.crew or {}).get("end_p90") is None, rec.crew
    assert rec.path_proposal == {}, rec.path_proposal
    assert any("Príchod pri zdržaní (90 % jázd) ani očakávaný náklad sa preto nedajú určiť" in r and
               f"#{late.id}" in r for r in rec.reasons), rec.reasons
    body = recommendation_body(rec)
    assert TECH_END not in body, [ln for ln in body.splitlines() if TECH_END in ln]
    assert "Príchod: 25.11. 10:" in body and "(90 % jázd: príchod nie je známy – výluka" in body, body
    assert "Očakávaný náklad: nie je známy" in body and "(90 % jázd: nie je známy)" in body, body
    pages = _web([f"/odporucania/{rec.id}", f"/api/vlaky/{rec.train_id}"])
    page = _plain(pages[f"/odporucania/{rec.id}"])
    assert TECH_END not in page, page[page.find(TECH_END) - 200:page.find(TECH_END) + 100]
    assert re.search(r"Príchod P50 / P90 25\.11\. 10:\d\d / nie je známy – výluka", page), page[:4000]
    assert "Očakávaný náklad nie je známy" in page and re.search(r"P50 / P90 25\.11\. \d\d:\d\d / nie je známy", page)
    data = pages[f"/api/vlaky/{rec.train_id}"]
    row = data if "eta_p90" in data else (data.get("vlak") or data)
    assert row.get("eta_p50") and row.get("eta_p90") is None, row
    content, _data = build_report(session, datetime.now(UTC) + timedelta(minutes=5))
    lines = [ln for ln in content.splitlines() if f"Vlak {rec.train.number}" in ln]
    assert lines and not any(TECH_END in ln for ln in lines), lines


def test_n1_p90_s_volnejsimi_prahmi_bez_konceptu_s_casom(env, session):
    """[N1] Vedúci zvýšil prahy ČERVENEJ (P(zadržania), P(prekročenia smeny) 30 %): VYPRAVIŤ ORANŽOVÁ s odchodom
    a predpokladaným príchodom, ale bez príchodu P90 z technického konca – ani v koncepte pre zákazníka (koncepty sa
    nepripravia)."""
    from vlaky.notifikacie.texty import recommendation_body

    _params(session, p_hold_red=0.3, p_crew_relief_red=0.3)
    _p90_closure(session)
    rec = _decide(session)
    assert (rec.decision, rec.color) == ("VYPRAVIŤ", "ORANŽOVÁ"), rec.summary
    assert "VYPRAVIŤ o 25.11. 10:00" in rec.summary and "90 % jázd: príchod nie je známy" in rec.summary, rec.summary
    assert TECH_END not in _texts(rec), _texts(rec)
    drafts = (rec.path_proposal or {}).get("drafts") or {}
    assert not drafts and rec.path_proposal == {}, rec.path_proposal
    body = recommendation_body(rec)
    assert TECH_END not in body and "KONCEPTY SPRÁV" not in body and "NÁVRH TRASY" not in body, body


def test_n1_objednavka_p90_bez_casu_z_technickeho_konca(env, session):
    """[N1] Objednávka s výlukou do odvolania od 25.11. 10:50 a voľnejšími prahmi: vysvetlenie aj e-mail o objednávke
    uvedú odchod a predpokladaný príchod, no nie príchod P90 ani náklad z technického konca; návrh trasy nie je a ďalší
    krok nepovie „potvrdí termín zákazníkovi“."""
    from vlaky.notifikacie.texty import order_proposal_text
    from vlaky.planovanie.planovac import plan_order

    _params(session, p_hold_red=0.3, p_crew_relief_red=0.3)
    _p90_closure(session, minutes=50)
    o = _order(session)
    plan = plan_order(session, o.id, NOW)
    text = " | ".join(plan["explanation"])
    assert plan["decision"] == "VYPRAVIŤ" and TECH_END not in text, text
    assert "s odchodom 25.11. 10:00" in text and "90 % jázd: príchod nie je známy" in text, text
    assert "očakávaný náklad nie je známy" in text and plan.get("p90_unknown"), text
    assert "ORANŽOVÁ – vyžaduje potvrdenie dispečera" in text and "príchod pri zdržaní nie je známy" in text, text
    assert not plan.get("path_proposal"), plan.get("path_proposal")
    _s, body = order_proposal_text(o, plan)
    assert TECH_END not in body and "potvrdí termín zákazníkovi" not in body, body
    assert "najneskorší príchod zatiaľ nie je známy" in body, body


# ------------------------------------------------------------------ N2 – porovnanie variantov s odchodom
def test_n2_porovnanie_variantov_uvadza_znamy_odchod(env, session):
    """[N2] V prípade N1 je odchod odporúčanej varianty skutočný (25.11. 10:00): porovnanie variantov v e-maile aj na
    webe (odporúčanie aj objednávka s voľnejšími prahmi vedúceho) ho uvedie; „nie je známy“ je len príchod P90
    a náklad. Aj staršie uložené odporúčanie (do_odvolania so známym príchodom P50) uvedie odchod."""
    from vlaky.notifikacie.texty import recommendation_body, variant_departure_unknown
    from vlaky.planovanie.planovac import plan_order

    _p90_closure(session)
    rec = _decide(session)
    body = recommendation_body(rec)
    rows = [ln for ln in body.split("Porovnanie variantov:")[1].splitlines() if "VYPRAVIŤ" in ln]
    assert rows and "25.11. 10:00" in rows[0] and "nie je známy" in rows[0], rows
    _params(session, p_hold_red=0.3, p_crew_relief_red=0.3)
    o = _order(session)
    plan = plan_order(session, o.id, NOW)
    go = [v for v in plan["variants"] if v.get("kind") == "VYPRAVIŤ" and _iso(v["depart"]) == READY]
    assert go and go[0].get("arrival_p50") and not go[0].get("do_odvolania"), plan["variants"]
    pages = _web([f"/odporucania/{rec.id}", f"/objednavky/{o.id}"])
    page = _plain(pages[f"/odporucania/{rec.id}"])
    i = page.find("odporúčaná")
    assert page[i:i + 40].startswith("odporúčaná 25.11. 10:00"), page[i:i + 120]
    assert "nie je známy nie je známy" in page[i:i + 160], page[i:i + 160]
    order_page = pages[f"/objednavky/{o.id}"]
    table = order_page[order_page.index("<h3>Varianty</h3>"):]
    assert re.search(r'<td class="nowrap">25\.11\. 10:00</td><td>VYPRAVIŤ</td>', table), table[:2000]
    legacy = {"depart": READY.isoformat(), "arrival_p50": (READY + timedelta(minutes=22)).isoformat(),
              "arrival_p90": None, "do_odvolania": "výluka … platí od 25.11. 10:54 do odvolania"}
    assert not variant_departure_unknown(legacy)
    assert variant_departure_unknown({**legacy, "arrival_p50": None})


# ------------------------------------------------------------------ N3 – súhrnné okná výluk koľají
def _ba_nz_train(session, guard: bool = False):
    if guard:
        _closure(session, kind="pomala_jazda", a="Bratislava-Vajnory", b="Bernolákovo", start=NOW + timedelta(hours=1),
                 end=NOW + timedelta(hours=6), until_revoked=False, status="navrh", event_id=_email_event(session),
                 speed=30, ref="8931/2026")
    return _decide(session, ready=NOW, origin="Bratislava hlavná stanica", destination="Nové Zámky", deadline_h=30,
                   path_at=NOW)


@pytest.mark.parametrize("guard", [False, True], ids=["bez-poistky", "neoverena-na-hlavnej"])
def test_n3_suhrnne_okna_kolaji_nevytvoria_uplnu_vyluku(env, session, guard):
    """[N3] Rezerva po výluke uložená 4 200 min: denná výluka 1. koľaje Senec – Galanta 08 – 12 a 2. koľaje 14 – 18
    (skončili pred 26 h) sa nikdy neprekrývajú – ich súhrnné okná áno. Plánovač nevytvorí úplnú výluku, ktorá
    neexistuje: vlak Bratislava → Nové Zámky ide hneď po hlavnej trase (nie obchádzkou o 6 h neskôr ani odstavenie);
    s neoverenou výlukou z e-mailu na hlavnej trase nie je ZELENÁ ani obchádzka."""
    _params(session, buffer_before_closure_min=30.0, buffer_after_closure_min=4200.0)
    s0, e0 = NOW - timedelta(days=10), NOW - timedelta(hours=26)
    _closure(session, kind="kolaj", track="1", daily=("08:00", "12:00"), a="Senec", b="Galanta", start=s0, end=e0,
             until_revoked=False, ref="8921/2026")
    _closure(session, kind="kolaj", track="2", daily=("14:00", "18:00"), a="Senec", b="Galanta", start=s0, end=e0,
             until_revoked=False, ref="8922/2026")
    rec = _ba_nz_train(session, guard)
    assert rec.decision == "VYPRAVIŤ" and "VYPRAVIŤ o 21.11. 09:00" in rec.summary, rec.summary
    assert not any("sú vylúčené obe traťové koľaje" in r for r in rec.reasons), rec.reasons
    if guard:
        assert rec.color != "ZELENÁ" and all(v.get("status") != "ZELENÁ" for v in rec.variants), rec.variants


def test_n3_skutocne_prekrytie_z_presnych_okien_a_obchadzka_nie_je_zelena(env, session):
    """[N3] Skutočné denné prekrytie výluk oboch koľají Senec – Galanta (1. koľaj 08 – 12, 2. koľaj 10 – 14, skončili
    pred 26 h) pri rezerve po výluke 4 200 min úsek uzatvára (z presných okien – „denne“) a vlak nejde po hlavnej trase
    hneď; obchádzka, ktorú súhrnné okná hlavnej trasy vyvolali, ani žiadna iná varianta nie je ZELENÁ (súhrnné okná
    zasahujú plán)."""
    from vlaky.planovanie.planovac import COARSE_CAP

    _params(session, buffer_before_closure_min=30.0, buffer_after_closure_min=4200.0)
    s0, e0 = NOW - timedelta(days=10), NOW - timedelta(hours=26)
    _closure(session, kind="kolaj", track="1", daily=("08:00", "12:00"), a="Senec", b="Galanta", start=s0, end=e0,
             until_revoked=False, ref="8923/2026")
    _closure(session, kind="kolaj", track="2", daily=("10:00", "14:00"), a="Senec", b="Galanta", start=s0, end=e0,
             until_revoked=False, ref="8924/2026")
    rec = _ba_nz_train(session)
    assert any("sú vylúčené obe traťové koľaje" in r and "(denne)" in r for r in rec.reasons), rec.reasons
    assert "VYPRAVIŤ o 21.11. 09:00" not in rec.summary, rec.summary
    assert rec.color != "ZELENÁ" and all(v.get("status") != "ZELENÁ" for v in rec.variants), \
        (rec.summary, rec.variants)
    assert COARSE_CAP in (rec.data_age or {}).get("obmedzenie", []), rec.data_age


def test_n3_obchadzka_pri_suhrnnych_oknach_hlavnej_trasy_nie_je_zelena(env, session):
    """[N3] Denná úplná výluka Senec – Galanta 08 – 12 skončila pred 26 h, rezerva po výluke 4 200 min: hlavná trasa je
    s uloženou rezervou zatvorená a vlak ide obchádzkou cez Sereď. Obchádzku vyvolali súhrnné okná hlavnej trasy –
    nie je ZELENÁ (ani žiadna varianta)."""
    _params(session, buffer_before_closure_min=30.0, buffer_after_closure_min=4200.0)
    _closure(session, daily=("08:00", "12:00"), a="Senec", b="Galanta", start=NOW - timedelta(days=10),
             end=NOW - timedelta(hours=26), until_revoked=False, ref="8925/2026")
    rec = _ba_nz_train(session)
    assert rec.decision == "PRESMEROVAŤ", rec.summary
    assert rec.color != "ZELENÁ" and all(v.get("status") != "ZELENÁ" for v in rec.variants), rec.variants
    assert not rec.windows or all(not w for w in rec.windows.values()) or rec.color != "ZELENÁ", rec.windows


# ------------------------------------------------------------------ N4 – okná za koncom obdobia so zdržaním
def _p3_register(session, delta: float) -> Closure:
    """Dlhá úplná výluka Ružomberok – Liptovský Mikuláš do teraz + 4 dni (vlak čaká v Ružomberku), krátka výluka
    Liptovský Mikuláš – Liptovský Hrádok v najbližších hodinách (úsek simulácie s výlukou) a výluka do odvolania toho
    istého úseku, ktorá začne až po konci prehľadávaného obdobia ``delta`` min po tom, čo by úsek opustil priebeh jazdy
    bez zdržania (s rezervou pred výlukou 30 min)."""
    end_long = NOW + timedelta(days=4)
    _closure(session, start=NOW - timedelta(hours=1), end=end_long, until_revoked=False, ref="8941/2026")
    _closure(session, a=B, b=H, start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2), until_revoked=False,
             ref="8942/2026")
    exit_lh = end_long + timedelta(minutes=30 + (16.0 + 6.7) * 1.04)
    start = exit_lh + timedelta(minutes=30 + delta)
    return _closure(session, a=B, b=H, start=start, end=start + timedelta(days=30), ref="8943/2026")


def test_n4_vyluka_do_odvolania_za_obdobim_podla_jazdy_so_zdrzanim(env, session):
    """[N4] Výluka do odvolania Liptovský Mikuláš – Liptovský Hrádok začne 6 min po tom, čo by úsek opustil priebeh bez
    zdržania – takmer každá jazda so zdržaním do nej vojde: termín jazdy nie je známy (bez časov najmenej zlej
    možnosti, návrhu trasy a konceptov)."""
    late = _p3_register(session, 6)
    rec = _decide(session, ready=NOW, origin=K, destination=H, path_at=NOW)
    assert "termín jazdy nie je známy" in rec.summary and f"platí od {_l(late.start_utc)} do odvolania" in \
        rec.summary, rec.summary
    assert rec.path_proposal == {} and not rec.chosen, (rec.chosen, rec.path_proposal)


def test_n4_vyluka_do_odvolania_za_obdobim_len_pri_zdrzani(env, session):
    """[N4] Výluka do odvolania začne 10 min po priebehu bez zdržania – vojde do nej len jazda so zdržaním (asi pätina):
    simulácia ju nevidela, žiadna varianta nie je bezpečná (ESKALOVAŤ); najmenej zlá možnosť uvedie odchod
    a predpokladaný príchod, príchod P90 nie je známy, návrh trasy ani koncept objednávky trasy sa nepripraví."""
    from vlaky.planovanie.planovac import P90_CAP

    late = _p3_register(session, 10)
    rec = _decide(session, ready=NOW, origin=K, destination=H, path_at=NOW)
    assert rec.decision == "ESKALOVAŤ" and "Najmenej zlá možnosť:" in rec.summary, rec.summary
    assert "termín jazdy nie je známy" not in rec.summary, rec.summary
    assert "90 % jázd: príchod nie je známy" in rec.summary and f"platí od {_l(late.start_utc)} do odvolania" in \
        rec.summary, rec.summary
    assert rec.path_proposal == {}, rec.path_proposal
    assert rec.chosen.get("arrival_p90") is None and rec.chosen.get("expected_cost") is None, rec.chosen
    # simulácia výluku za koncom obdobia nevidela – vlak, ktorý pri zdržaní uviazne, nie je ZELENÝ (ani jeho varianta)
    assert rec.color != "ZELENÁ" and P90_CAP in (rec.data_age or {}).get("obmedzenie", []), (rec.color, rec.data_age)
    assert all(v.get("status") != "ZELENÁ" for v in rec.variants or [] if v.get("p90_do_odvolania")), rec.variants


def test_n4_objednavka_vyluka_do_odvolania_za_obdobim_so_zdrzanim(env, session):
    """[N4] Objednávka Kraľovany → Liptovský Hrádok s rovnakými výlukami (výluka do odvolania 6 min po priebehu vlaku
    bez zdržania; objednávka počíta s rýchlosťou nákladného vlaku z konfigurácie): jazda so zdržaním do nej vojde –
    termín prepravy (alebo aspoň príchod pri zdržaní) nie je známy a návrh trasy na objednanie sa nepripraví."""
    from vlaky.notifikacie.texty import order_proposal_text
    from vlaky.planovanie.planovac import plan_order

    late = _p3_register(session, 6)
    o = _order(session, ready=NOW, origin=K, destination=H)
    plan = plan_order(session, o.id, NOW)
    why = plan.get("date_unknown") or plan.get("p90_unknown") or ""
    assert f"platí od {_l(late.start_utc)} do odvolania" in why, plan
    assert not plan.get("path_proposal"), plan.get("path_proposal")
    _s, body = order_proposal_text(o, plan)
    assert "NÁVRH TRASY" not in body and "potvrdí termín zákazníkovi" not in body, body


# ------------------------------------------------------------------ N5 – návrh trasy podľa simulácie
def test_n5_navrh_trasy_neprejde_usekom_kde_simulacia_caka(env, session):
    """[N5] Výluka 1. koľaje Ružomberok – Liptovský Mikuláš do odvolania (zdržanie aj v úseku) a denná výluka 2. koľaje
    10 – 16 (prekrytie = úplná výluka denne; rezerva po výluke 1 441 min ich spojí): jazda podľa jazdnej doby úsek
    tesne prejde pred oknom, väčšina jázd simulácie so zdržaním však čaká v Ružomberku do 29.11. Návrh trasy, ktorý by
    úsek prešiel bez čakania a zvyšok jazdy natiahol na dni, ani koncept objednávky trasy sa nepripraví – dôvod to
    povie; predpokladaný príchod je po čakaní (29.11.)."""
    now = datetime(2026, 11, 22, 7, 45, tzinfo=UTC)
    set_fake_now(now)
    session.add(ParameterSet(valid_from=now - timedelta(days=1), created_by="veduci", note="FIKTÍVNE",
                             values={"buffer_before_closure_min": 30.0, "buffer_after_closure_min": 1441.0}))
    session.commit()
    _closure(session, kind="kolaj", track="1", start=datetime(2026, 11, 23, 7, 45, tzinfo=UTC),
             end=datetime(2026, 11, 24, 7, 45, tzinfo=UTC), status="navrh", ref="8951/2026")
    _closure(session, kind="kolaj", track="2", daily=("10:00", "16:00"), start=datetime(2026, 11, 26, 3, 45, tzinfo=UTC),
             end=datetime(2026, 11, 28, 15, 45, tzinfo=UTC), until_revoked=False, ref="8952/2026")
    _closure(session, a=K, b=A, start=datetime(2026, 11, 26, 8, 45, tzinfo=UTC),
             end=datetime(2026, 11, 27, 8, 45, tzinfo=UTC), status="navrh", ref="8953/2026")
    ready = datetime(2026, 11, 26, 7, 45, tzinfo=UTC)
    rec = _decide(session, ready=ready, now=now, origin=K, destination=H, deadline_h=6)
    pp = rec.path_proposal or {}
    assert "príchod 29.11." in rec.summary, rec.summary
    if pp.get("stations"):
        # návrh by musel čakať pred úsekom Ružomberok – Liptovský Mikuláš ako simulácia (nie prejsť ho 26.11.)
        sts = {s["name"]: s for s in pp["stations"]}
        assert _iso(sts[B]["arr"]) > datetime(2026, 11, 29, tzinfo=UTC), pp["stations"]
    else:
        assert any("Návrh trasy ani koncepty správ agent nepripravil" in r for r in rec.reasons), rec.reasons
    assert not any(s.get("name") == "Okoličné" and s.get("arr", "").startswith("2026-11-27")
                   for s in pp.get("stations") or []), pp


# ------------------------------------------------------------------ N6 – história výluk pri rezerve nad limitom
SECTIONS = [(K, A), (A, B), (B, H)]


def _history_register(session, n: int = 300) -> None:
    for i in range(n):
        a, b = SECTIONS[i % 3]
        end = NOW - timedelta(days=3) - timedelta(hours=(600 * 24 - 72) * i / n)
        session.add(Closure(kind="uplna", from_station=a, to_station=b, line_ref="", track="",
                            start_utc=end - timedelta(hours=4), end_utc=end, daily_from="", daily_to="",
                            electric_only=False, speed_kmh=None, external_ref=f"{6000 + i}/2025", until_revoked=False,
                            status="overene", source="rucne", note="FIKTÍVNA história"))
    session.commit()


def test_n6_historia_vyluk_pri_rezerve_nad_limitom_je_suhrnna(env, session, monkeypatch):
    """[N6] 300 starých 4-h výluk za 600 dní na trase Kraľovany → Liptovský Hrádok a rezerva po výluke 1 000 000 min:
    každá s uloženou rezervou blokuje dodnes (ESKALOVAŤ ako s uloženou hodnotou), plánovač ich však počíta súhrnne –
    úsek simulácie má najviac niekoľko okien, dôvody nerastú s históriou registra a prepočet netrvá o rád dlhšie ako
    s predvolenými rezervami. Zlúčenie prekrývajúcich sa okien úseku simuláciu nemení."""
    from vlaky.planovanie import planovac
    from vlaky.rozhodovanie import Closure as DClosure, Params, Route, Segment, evaluate

    _history_register(session)
    t0 = time.monotonic()
    base = _decide(session, ready=NOW, origin=K, destination=H, path_at=NOW)
    took_default = time.monotonic() - t0
    assert base.decision == "VYPRAVIŤ", base.summary
    _params(session, buffer_before_closure_min=30.0, buffer_after_closure_min=1_000_000.0)
    seen: list = []
    real = planovac.two_stage_recommend

    def spy(routes, *args, **kwargs):
        seen.append(max((len(s.closures) for r in routes for s in r.segments), default=0))
        return real(routes, *args, **kwargs)

    monkeypatch.setattr(planovac, "two_stage_recommend", spy)
    t0 = time.monotonic()
    rec = _decide(session, ready=NOW, origin=K, destination=H, path_at=NOW)
    took = time.monotonic() - t0
    assert (rec.decision, rec.color) == ("ESKALOVAŤ", "ČERVENÁ"), rec.summary
    assert seen and max(seen) <= 5, seen
    assert len(rec.reasons) <= 40, len(rec.reasons)
    assert any("výluk tohto úseku, ktoré skončili pred" in r and "počítajú súhrnne" in r for r in rec.reasons), \
        rec.reasons
    assert took < 2 * took_default + 2.0, (took, took_default)
    # zlúčenie prekrývajúcich sa okien (s rezervami) v úseku simulácie nemení výsledok simulácie
    p = Params(samples=400)
    closures = (DClosure(10, 50), DClosure(60, 70), DClosure(65, 300), DClosure(900, 950))
    seg = Segment("úsek", 30.0, 2.0, 8.0, closures)
    merged = planovac._union_closures(seg, p)
    assert len(merged) == 2, merged
    for depart in (0.0, 5.0, 40.0, 320.0, 860.0, 1000.0):
        one = evaluate(Route("r", (seg,)), depart, 0.0, 2000.0, p)
        two = evaluate(Route("r", (Segment("úsek", 30.0, 2.0, 8.0, merged),)), depart, 0.0, 2000.0, p)
        assert (one.arrival_p50, one.arrival_p90, one.p_hold, one.expected_cost) == \
            (two.arrival_p50, two.arrival_p90, two.p_hold, two.expected_cost), depart


# ------------------------------------------------------------------ N7 – texty rezervy nad limitom
def test_n7_nastavenia_presna_hodnota_a_mnozne_cislo(env, session):
    """[N7] Uložená rezerva 1 440,4 min: Nastavenia ju ukážu presne (nie „1 440 min … je nad limitom 1 440 min“). Dve
    rezervy nad limitom: dôvod v odporúčaní aj upozornenie DÔLEŽITÉ sú v množnom čísle („opravte rezervy“, „s uloženými
    hodnotami“)."""
    from vlaky.agent.beh import _notify_params
    from vlaky.agent.zdravie import params_status
    from vlaky.planovanie.planovac import _excess_reason

    set_fake_now(NOW)
    _params(session, buffer_before_closure_min=1440.4)
    page = _plain(_web(["/nastavenia"], "veduci", "veduci")["/nastavenia"])
    assert "uložená hodnota 1 440,4 min (24,01 h) je nad limitom 1 440 min (24 h)" in page, page[:3000]
    ok, text, _ps = params_status(session)
    assert not ok and "pred výlukou 1 440,4 min" in text, text
    excess = {"buffer_before_closure_min": 4200.0, "buffer_after_closure_min": 2000.0}
    reason = _excess_reason(excess, True)
    assert "opravte rezervy v Nastaveniach" in reason and "opravte rezervu" not in reason, reason
    ps = _params(session, **excess)
    _ok, text, ps_id = params_status(session)
    assert ps_id == ps.id and "opravte ich" in text, text
    _notify_params(session, text, ps_id)
    session.commit()
    body = next(n.body for n in session.scalars(select(Notification).where(
        Notification.dedup_key == f"nastavenia:{ps.id}")))
    assert "s uloženými hodnotami" in body and "opravte rezervy v Nastaveniach" in body, body
    assert "s uloženou hodnotou" not in body and "opravte rezervu" not in body, body


# ------------------------------------------------------------------ N8 – odovzdávka služby
def test_n8_odovzdavka_pomala_jazda_len_pre_elektricku_trakciu(env, session):
    """[N8] Pomalá jazda 30 km/h len pre elektrickú trakciu: odovzdávka služby uvedie rýchlosť aj „len elektrická
    trakcia“ (ako stĺpec Dopad v registri výluk) – inak by ju nasledujúca služba čítala ako pomalú jazdu všetkých
    vlakov."""
    from vlaky.agent.odovzdavka import build_report

    set_fake_now(NOW)
    _closure(session, kind="pomala_jazda", speed=30, electric=True, ref="8981/2026")
    content, _data = build_report(session, datetime.now(UTC) + timedelta(minutes=5))
    line = next(x for x in content.splitlines() if f"{A} – {B}:" in x)
    assert "pomalá jazda (max 30 km/h, len elektrická trakcia)" in line, line


# ------------------------------------------------------------------ N9 – prevádzková príručka a monitoring
def test_n9_prirucka_a_monitoring_uvadzaju_chybu_nastavenia_rozhodovania():
    """[N9] Heartbeat `/fail` a „unhealthy“ agent pri rezerve nad limitom: prevádzková príručka (kapitola 12 aj postup
    v kapitole 13) a návod na monitoring vysvetlia chybu „nastavenia rozhodovania“ a čo robiť."""
    manual = (ROOT / "docs" / "PREVADZKA.md").read_text(encoding="utf-8")
    monitoring = (ROOT / "deploy" / "monitoring" / "README.md").read_text(encoding="utf-8")
    table = manual[manual.index("## 12. Monitoring"):manual.index("## 13.")]
    assert "| Nastavenia rozhodovania |" in table and "nastavenia rozhodovania:" in table, table
    incidents = manual[manual.index("## 13."):manual.index("## 14.")]
    assert "### Chyba nastavenia rozhodovania" in incidents and "Nastavenia" in incidents, incidents[:500]
    assert "nastavenia rozhodovania" in monitoring and "nad limitom 1 440 min" in monitoring, monitoring
