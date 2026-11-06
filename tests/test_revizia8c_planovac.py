"""Plánovač: nezávislá kontrola 8. kola, druhý doplnok (výluky za koncom obdobia, priebeh jazdy ako simulácia, texty,
uložená rezerva nad limitom formulára).

T0 – výluka do odvolania, ktorá začne až po konci prehľadávaného obdobia (na ďalšom úseku alebo ako pokračovanie
„do odvolania“ zapísané ako ďalšia výluka toho istého úseku), nesmie pustiť najmenej zlú možnosť s časmi, návrh trasy
ani koncept objednávky trasy (vlak aj objednávka).
T1 – kontrola okien za koncom obdobia posudzuje vlak podľa priebehu jazdy simulácie (čakanie pred každým úsekom
simulácie s výlukou, nie pred prvým z radu susedných úsekov s výlukami): žiadny falošný „termín jazdy nie je známy“
pre výluku, do ktorej vlak nevojde; návrh trasy čaká tam, kde čaká simulácia, a nikdy nevojde do okna výluky.
T2 – porovnanie variantov (e-mail aj web, vlak aj objednávka) neuvádza čas odchodu varianty, ktorej termín nie je známy.
T3 – odovzdávka služby uvádza pri nových a zmenených výlukách druh, koľaj a denné okno (ako fronta a register výluk).
T4 – texty nadväzujúcich okien: množné číslo pri viacerých výlukách, pevný koniec nie je „do odvolania“, začiatok
okna raz, číslo rozkazu pri svojej výluke, mimoriadnosť ako mimoriadnosť.
S5 – rezerva uložená nad limitom 1 440 min (staršia verzia formulára): prepočet nie je o rády pomalší, rozhodnutie nie
je zelenšie ako s uloženou hodnotou, vlak, ktorého správne posúdenie potrebuje vzdialenejšie okná, nie je ZELENÝ,
a nastavenia, zdravie agenta aj odporúčania to povedia.

Všetky vlaky, výluky, osoby a adresy sú fiktívne.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
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
             until_revoked: bool = True, ref: str = "8700/2026", a: str = A, b: str = B, status: str = "overene",
             speed=None) -> Closure:
    c = Closure(kind=kind, from_station=a, to_station=b, line_ref="", track=track,
                start_utc=start or loc(2026, 11, 23, 8), end_utc=end or loc(2026, 12, 23, 8),
                daily_from=daily[0], daily_to=daily[1], electric_only=False, speed_kmh=speed, external_ref=ref,
                until_revoked=until_revoked, status=status, source="rucne", note="FIKTÍVNA")
    session.add(c)
    session.commit()
    return c


def _params(session, **values) -> ParameterSet:
    ps = ParameterSet(valid_from=NOW - timedelta(days=1), created_by="veduci", values=values,
                      note="staršia verzia formulára (FIKTÍVNE)")
    session.add(ps)
    session.commit()
    return ps


_N = [88700]


def _train(session, ready: datetime, now: datetime = NOW, origin: str = A, destination: str = B,
           deadline_h: float = 8, path_at: datetime | None = None) -> Train:
    from vlaky.agent.zdravie import set_kv

    _N[0] += 1
    set_fake_now(now)
    t = Train(number=str(_N[0]), origin=origin, destination=destination, traction="E", gauge="1435", vmax_kmh=80,
              status="planovany", ready_utc=ready, deadline_utc=ready + timedelta(hours=deadline_h))
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
    rec = evaluate_train(session, t.id, now, "kontrola 8c")
    session.commit()
    return rec


def _order(session, ready: datetime = READY, origin: str = A, destination: str = B, hours: float = 8) -> Order:
    set_fake_now(NOW)
    o = Order(customer="FIKTÍVNY zákazník", origin=origin, destination=destination, earliest_departure_utc=ready,
              deadline_utc=ready + timedelta(hours=hours), status="nova")
    session.add(o)
    session.commit()
    return o


def _texts(rec) -> list:
    return [rec.summary or ""] + [str(r) for r in rec.reasons or []]


def _variant_rows(body: str) -> str:
    return body.split("Porovnanie variantov:")[1].split("Dôvody a upozornenia:")[0]


def _web(paths: list) -> dict:
    from fastapi.testclient import TestClient

    from test_web_spolocne import login_as
    from vlaky.web.app import create_app

    out = {}
    with TestClient(create_app(auto_evaluate=False)) as client:
        login_as(client, "dispecer")
        for p in paths:
            out[p] = client.get(p).text
    return out


def _table(page: str, title: str) -> str:
    part = page[page.index(title):]
    return part[:part.index("</table>")]


def _station(pp: dict, name: str) -> dict:
    return next(s for s in pp["stations"] if s["name"] == name)


# ------------------------------------------------------------------ T0 – výluka do odvolania za koncom obdobia
OPEN_BEYOND = {
    "dalsi-usek": dict(a=B, b=H, start=loc(2026, 12, 1, 8), end=loc(2026, 12, 31, 8), ref="8702/2026"),
    "ten-isty-usek": dict(a=A, b=B, start=loc(2026, 12, 4, 8), end=loc(2026, 12, 31, 8), ref="8703/2026"),
}


@pytest.mark.parametrize("shape", list(OPEN_BEYOND))
def test_t0_vyluka_do_odvolania_za_koncom_obdobia_bez_casov_navrhu_a_konceptu(env, session, shape):
    """[T0] Úplná výluka Ružomberok – Liptovský Mikuláš 24.11. – 04.12. 08:00 (najmenej zlá možnosť vlaku Kraľovany →
    Liptovský Hrádok pripraveného 25.11. 10:00 čaká na jej koniec) a výluka do odvolania, ktorá začína až po konci
    prehľadávaného obdobia – na ďalšom úseku (Liptovský Mikuláš – Liptovský Hrádok od 01.12. 08:00) alebo ako
    pokračovanie toho istého úseku (od 04.12. 08:00): termín jazdy nie je známy, žiadne časy najmenej zlej možnosti,
    návrh trasy ani koncept objednávky trasy v IS KIS; varianty bez odchodu, príchodu a nákladu."""
    from vlaky.notifikacie.texty import recommendation_body

    _closure(session, start=loc(2026, 11, 24, 8), end=loc(2026, 12, 4, 8), until_revoked=False, ref="8701/2026")
    late = _closure(session, **OPEN_BEYOND[shape])
    rec = _decide(session, origin=K, destination=H)
    assert "termín jazdy nie je známy" in rec.summary, rec.summary
    assert f"platí od {_l(late.start_utc)} do odvolania" in rec.summary, rec.summary
    assert (rec.decision, rec.color) == ("ESKALOVAŤ", "ČERVENÁ") and "s odchodom" not in rec.summary, rec.summary
    assert not rec.chosen and rec.path_proposal == {}, (rec.chosen, rec.path_proposal)
    assert any(f"#{late.id}" in r and "č. " + late.external_ref in r for r in rec.reasons), rec.reasons
    assert rec.variants and all(v.get("arrival_p50") is None and v.get("expected_cost") is None
                                for v in rec.variants), rec.variants
    body = recommendation_body(rec)
    assert "NÁVRH TRASY" not in body and "objednávka ad hoc trasy" not in body and "Odchod:" not in body, body
    assert "04.12. 11:01" not in body and "04.12. 08:30" not in body, body
    assert "26.11. 04:00" not in _variant_rows(body), _variant_rows(body)


def test_t0_objednavka_s_vylukou_do_odvolania_za_koncom_obdobia(env, session):
    """[T0] Objednávka Kraľovany → Liptovský Hrádok s rovnakými výlukami (dlhá výluka a výluka do odvolania na ďalšom
    úseku od 01.12. 08:00): termín prepravy nie je známy, žiadny návrh trasy na objednanie a e-mail o objednávke
    neradí najmenej zlú možnosť s časmi."""
    from vlaky.notifikacie.texty import order_proposal_text
    from vlaky.planovanie.planovac import plan_order

    _closure(session, start=loc(2026, 11, 24, 8), end=loc(2026, 12, 4, 8), until_revoked=False, ref="8711/2026")
    late = _closure(session, a=B, b=H, start=loc(2026, 12, 1, 8), end=loc(2026, 12, 31, 8), ref="8712/2026")
    o = _order(session, origin=K, destination=H)
    plan = plan_order(session, o.id, NOW)
    assert plan.get("date_unknown") and "platí od 01.12. 08:00 do odvolania" in plan["date_unknown"], plan
    assert plan["decision"] == "ESKALOVAŤ" and not plan.get("path_proposal"), plan
    assert all(v.get("arrival_p50") is None for v in plan["variants"]), plan["variants"]
    _s, body = order_proposal_text(o, plan)
    assert "NÁVRH TRASY" not in body and "najmenej zlá možnosť" not in body, body
    assert "termín zatiaľ nie je známy" in body and f"platí od {_l(late.start_utc)} do odvolania" in body, body


# ------------------------------------------------------------------ T1 – priebeh jazdy ako simulácia
def _no_proposal_in_windows(pp: dict, windows: list, a: str, b: str, before_min: float = 30, after_min: float = 30):
    """Návrh trasy prejde úsek a – b (odchod z a, príchod do b) mimo každého okna výluky s rezervami."""
    dep, arr = _iso(_station(pp, a)["dep"]), _iso(_station(pp, b)["arr"])
    for ws, we in windows:
        lo, hi = ws - timedelta(minutes=before_min), we + timedelta(minutes=after_min)
        assert not (dep < hi and arr > lo), (a, b, _l(dep), _l(arr), _l(ws), _l(we))


def test_t1_denna_vyluka_na_predchadzajucom_useku_nezastavi_termin(env, session):
    """[T1] Úplná výluka Ružomberok – Liptovský Mikuláš 24.11. – 05.12. 08:00 a denná výluka susedného predchádzajúceho
    úseku Kraľovany – Ružomberok 08:00 – 10:00 (20.11. – 31.12.): simulácia vlak pustí cez Kraľovany – Ružomberok
    26.11. a čaká v Ružomberku. Termín jazdy je známy (nie „po čakaní by vlak vošiel do ďalšieho okna výluky“ pre
    výluku Kraľovany – Ružomberok, do ktorej vlak nevojde); návrh trasy čaká v Ružomberku a nevojde do žiadneho okna
    výluky (s rezervami); koncept objednávky trasy má odchod z Kraľovian 26.11."""
    from vlaky.planovanie.planovac import closure_windows

    _closure(session, start=loc(2026, 11, 24, 8), end=loc(2026, 12, 5, 8), until_revoked=False, ref="8721/2026")
    daily = _closure(session, a=K, b=A, daily=("08:00", "10:00"), start=loc(2026, 11, 20, 0), end=loc(2026, 12, 31, 0),
                     until_revoked=False, ref="8722/2026")
    rec = _decide(session, origin=K, destination=H)
    assert "termín jazdy nie je známy" not in rec.summary, rec.summary
    assert f"#{daily.id}" not in rec.summary and "s odchodom 26.11." in rec.summary, rec.summary
    pp = rec.path_proposal
    assert pp and pp.get("stations"), rec.path_proposal
    assert _station(pp, A).get("hold") and not _station(pp, K).get("hold"), pp["stations"]
    assert _l(_iso(_station(pp, K)["dep"])).startswith("26.11."), pp["stations"]
    wins = [(w.start, w.end) for w in closure_windows(session, loc(2026, 11, 20, 0), loc(2026, 12, 31, 0))
            if w.closure_id == daily.id]
    _no_proposal_in_windows(pp, wins, K, A)
    assert _iso(_station(pp, A)["dep"]) >= loc(2026, 12, 5, 8, 30), pp["stations"]
    assert "Východisková dopravňa: Kraľovany   odchod 26.11." in pp["drafts"]["zsr_path_request"], pp["drafts"]


def test_t1_objednavka_s_dennou_vylukou_na_predchadzajucom_useku(env, session):
    """[T1] Objednávka Kraľovany → Liptovský Hrádok s rovnakými výlukami: termín prepravy je známy (žiadny falošný
    „termín zatiaľ nie je známy“), návrh trasy na objednanie čaká v Ružomberku a úsek Kraľovany – Ružomberok prejde
    mimo jeho denných okien."""
    from vlaky.notifikacie.texty import order_proposal_text
    from vlaky.planovanie.planovac import closure_windows, plan_order

    _closure(session, start=loc(2026, 11, 24, 8), end=loc(2026, 12, 5, 8), until_revoked=False, ref="8731/2026")
    daily = _closure(session, a=K, b=A, daily=("08:00", "10:00"), start=loc(2026, 11, 20, 0), end=loc(2026, 12, 31, 0),
                     until_revoked=False, ref="8732/2026")
    o = _order(session, origin=K, destination=H)
    plan = plan_order(session, o.id, NOW)
    assert not plan.get("date_unknown"), plan.get("date_unknown")
    pp = plan.get("path_proposal") or {}
    assert pp.get("stations") and _station(pp, A).get("hold") and not _station(pp, K).get("hold"), pp
    wins = [(w.start, w.end) for w in closure_windows(session, loc(2026, 11, 20, 0), loc(2026, 12, 31, 0))
            if w.closure_id == daily.id]
    _no_proposal_in_windows(pp, wins, K, A)
    _s, body = order_proposal_text(o, plan)
    assert "NÁVRH TRASY" in body and "termín zatiaľ nie je známy" not in body, body


def test_t1_vyluka_do_odvolania_ktorej_usek_vlak_prejde_pred_jej_zaciatkom(env, session):
    """[T1] Výluka do odvolania Kraľovany – Ružomberok od 27.11. 08:00 (technický koniec 28.11. 08:00) a dlhá výluka
    Ružomberok – Liptovský Mikuláš 24.11. – 04.12. 08:00: najmenej zlá možnosť prejde Kraľovany – Ružomberok 26.11.,
    dávno pred začiatkom výluky do odvolania, a čaká v Ružomberku. Termín nie je „neznámy“ kvôli výluke Kraľovany –
    Ružomberok (vlak do nej nevojde, hoci príde až po konci jej okna – technickom konci); návrh trasy prejde úsek pred
    jej začiatkom a čaká v Ružomberku."""
    _closure(session, start=loc(2026, 11, 24, 8), end=loc(2026, 12, 4, 8), until_revoked=False, ref="8741/2026")
    opened = _closure(session, a=K, b=A, start=loc(2026, 11, 27, 8), end=loc(2026, 11, 28, 8), ref="8742/2026")
    rec = _decide(session, origin=K, destination=H)
    assert "termín jazdy nie je známy" not in rec.summary and f"#{opened.id}" not in rec.summary, rec.summary
    assert "s odchodom 26.11." in rec.summary, rec.summary
    pp = rec.path_proposal
    assert pp and _iso(_station(pp, A)["arr"]) < loc(2026, 11, 27, 7, 30) and _station(pp, A).get("hold"), pp
    assert all(v.get("arrival_p50") for v in rec.variants if v.get("depart", "").startswith("2026-11-26")), \
        rec.variants


# ------------------------------------------------------------------ T2 – porovnanie variantov bez času odchodu
def test_t2_porovnanie_variantov_bez_casu_odchodu(env, session):
    """[T2] Denná výluka Ružomberok – Liptovský Mikuláš 00:00 – 24:00 do odvolania: najmenej zlá možnosť nemá termín –
    porovnanie variantov v e-maile aj na webe (vlak aj objednávka) neuvádza ani jej čas odchodu (26.11. 04:00 leží
    vnútri výluky), len „nie je známy“."""
    from vlaky.notifikacie.texty import recommendation_body

    _closure(session, daily=("00:00", "24:00"), start=loc(2026, 11, 1, 0), ref="8751/2026")
    rec = _decide(session)
    unknown = [v for v in rec.variants if v.get("do_odvolania")]
    assert unknown, rec.variants
    shown = {_l(_iso(v["depart"])) for v in unknown}
    rows = _variant_rows(recommendation_body(rec))
    assert not any(d in rows for d in shown) and "nie je známy ODSTAVIŤ" in rows, (shown, rows)
    o = _order(session)
    pages = _web([f"/odporucania/{rec.id}", f"/objednavky/{o.id}"])
    table = _table(pages[f"/odporucania/{rec.id}"], "Porovnanie variantov")
    assert not any(d in table for d in shown), table
    assert re.search(r'<td class="nowrap">nie je známy</td>\s*<td>ODSTAVIŤ</td>', table), table
    order_table = _table(pages[f"/objednavky/{o.id}"], "<h3>Varianty</h3>")
    kinds = r"(?:VYPRAVIŤ|POČKAŤ|ODSTAVIŤ|PRESMEROVAŤ)"
    assert re.search(rf'<td class="nowrap">nie je známy</td><td>{kinds}</td>', order_table), order_table
    assert not re.search(rf'<td class="nowrap">\d\d\.\d\d\. \d\d:\d\d</td><td>{kinds}</td>', order_table), order_table


# ------------------------------------------------------------------ T3 – odovzdávka služby
def test_t3_odovzdavka_uvadza_druh_kolaj_a_denne_okno(env, session):
    """[T3] Odovzdávka služby (nové a zmenené výluky) uvádza druh výluky ako register a fronta, koľaj výluky koľaje a
    denné okno: „výluka koľaje 1, denne 08:00 – 14:00, od … do odvolania (technický koniec …)“ – denná výluka koľaje do
    odvolania sa nečíta ako úplná výluka celé obdobie; úplná výluka a pomalá jazda (s rýchlosťou) sa rozlíšia."""
    from vlaky.agent.odovzdavka import build_report

    _closure(session, kind="kolaj", track="1", daily=("08:00", "14:00"), a=K, b=A, ref="8761/2026")
    _closure(session, start=loc(2026, 11, 24, 8), end=loc(2026, 11, 24, 14), until_revoked=False, ref="8762/2026")
    _closure(session, kind="pomala_jazda", a=B, b=H, speed=30, start=loc(2026, 11, 25, 8), end=loc(2026, 11, 26, 8),
             until_revoked=False, ref="8763/2026")
    content, _data = build_report(session, datetime.now(UTC) + timedelta(minutes=5))
    lines = content.splitlines()
    assert any("Kraľovany – Ružomberok: výluka koľaje 1, denne 08:00 – 14:00, od 23.11. 08:00 do odvolania "
               "(technický koniec 23.12. 08:00) (nová, overená)" in x for x in lines), content
    assert any("Ružomberok – Liptovský Mikuláš: úplná výluka úseku, 24.11. 08:00 – 24.11. 14:00" in x
               for x in lines), content
    assert any("Liptovský Mikuláš – Liptovský Hrádok: pomalá jazda (max 30 km/h), 25.11. 08:00 – 26.11. 08:00" in x
               for x in lines), content


# ------------------------------------------------------------------ T4 – texty nadväzujúcich okien
def test_t4_viac_vyluk_v_mnoznom_cisle(env, session):
    """[T4] Dve striedavé denné výluky do odvolania (06 – 18 a 18 – 06): „výluky #1 a #2 … nenechávajú …; vlak úsekom
    neprejde, kým ŽSR výluky neukončí“ – nie jednotné číslo „výluku“."""
    c1 = _closure(session, daily=("06:00", "18:00"), start=loc(2026, 11, 1, 0), ref="8771/2026")
    c2 = _closure(session, daily=("18:00", "06:00"), start=loc(2026, 11, 1, 0), ref="8772/2026")
    rec = _decide(session)
    assert "kým ŽSR výluky neukončí alebo neurčí čas na prejazd" in rec.summary, rec.summary
    assert "výluku neukončí" not in rec.summary, rec.summary
    assert f"výluky #{c1.id} a #{c2.id}" in rec.summary and "nenechávajú" in rec.summary, rec.summary


@pytest.mark.parametrize("many", [False, True], ids=["jedna", "dve"])
def test_t4_pevny_koniec_nie_je_do_odvolania(env, session, many):
    """[T4] Nadväzujúce okná výluky s pevným koncom v registri (denne 00:00 – 24:00 do 01.03.2027; aj dve striedavé
    výluky s pevným koncom): termín jazdy nie je známy, text však nepovie „kým ŽSR výluku neukončí“ (ako pri výluke do
    odvolania) – vlak úsekom neprejde, kým výluka trvá (do jej konca), ak ŽSR neurčí čas na prejazd."""
    end = loc(2027, 3, 1, 0)
    if many:
        _closure(session, daily=("06:00", "18:00"), start=loc(2026, 11, 1, 0), end=end, until_revoked=False,
                 ref="8781/2026")
        _closure(session, daily=("18:00", "06:00"), start=loc(2026, 11, 1, 0), end=end - timedelta(days=7),
                 until_revoked=False, ref="8782/2026")
    else:
        _closure(session, daily=("00:00", "24:00"), start=loc(2026, 11, 1, 0), end=end, until_revoked=False,
                 ref="8783/2026")
    rec = _decide(session)
    assert "termín jazdy nie je známy" in rec.summary, rec.summary
    assert "kým ŽSR" not in rec.summary and "neukončí" not in rec.summary, rec.summary
    expected = "kým výluky trvajú (posledná do 01.03. 00:00)" if many else "kým výluka trvá (do 01.03. 00:00)"
    assert f"vlak úsekom neprejde, {expected}, ak ŽSR neurčí čas na prejazd" in rec.summary, rec.summary


def test_t4_zaciatok_okna_raz(env, session):
    """[T4] Po dlhom čakaní by vlak vošiel do krátkej výluky Liptovský Mikuláš – Liptovský Hrádok 04.12. 08:45 – 10:00:
    text uvedie jej začiatok raz („od 04.12. 08:45 do 04.12. 10:00“), nie „od 04.12. 08:45 (04.12. 08:45 – …)“."""
    _closure(session, start=loc(2026, 11, 24, 8), end=loc(2026, 12, 4, 8), until_revoked=False, ref="8791/2026")
    late = _closure(session, a=B, b=H, start=loc(2026, 12, 4, 8, 45), end=loc(2026, 12, 4, 10), until_revoked=False,
                    ref="8792/2026")
    rec = _decide(session, origin=K, destination=H)
    assert f"výluka #{late.id} {B} – {H} od 04.12. 08:45 do 04.12. 10:00" in rec.summary, rec.summary
    assert not any("(04.12. 08:45 – 04.12. 10:00)" in t for t in _texts(rec)), _texts(rec)


def test_t4_cislo_rozkazu_pri_svojej_vyluke_a_mimoriadnost(env, session):
    """[T4] Mimoriadnosť 06 – 18 (bez čísla rozkazu) a výluka 18 – 06 (č. 8796/2026) na tom istom úseku, obe do
    odvolania: mimoriadnosť sa menuje mimoriadnosťou a číslo rozkazu je pri svojej výluke („mimoriadnosť #1 a výluka
    #2 (č. 8796/2026)“), nie „výluky #1 a #2 (č. 8796/2026)“."""
    m = _closure(session, kind="mimoriadnost", daily=("06:00", "18:00"), start=loc(2026, 11, 1, 0), ref="")
    v = _closure(session, daily=("18:00", "06:00"), start=loc(2026, 11, 1, 0), ref="8796/2026")
    rec = _decide(session)
    assert f"mimoriadnosť #{m.id} a výluka #{v.id}" in rec.summary, rec.summary
    assert any(f"Mimoriadnosť #{m.id} a výluka #{v.id} (č. 8796/2026)" in r for r in rec.reasons), rec.reasons
    assert not any(f"#{m.id} a #{v.id} (č. 8796/2026)" in t for t in _texts(rec)), _texts(rec)


# ------------------------------------------------------------------ S5 – rezerva uložená nad limitom formulára
def _old_daily_register(session, now: datetime = NOW) -> None:
    """Denné výluky oboch koľají za posledných ~700 dní (skončili pred 3 dňami) a dve denné výluky do odvolania na trase
    Kraľovany → Liptovský Hrádok (ako meranie overovateľa)."""
    i = 0
    for a, b in ((A, B), (K, A), (B, H)):
        for track in ("1", "2"):
            i += 1
            session.add(Closure(kind="kolaj", from_station=a, to_station=b, line_ref="", track=track,
                                start_utc=now - timedelta(days=700), end_utc=now - timedelta(days=3),
                                daily_from="08:00" if track == "1" else "10:00",
                                daily_to="14:00" if track == "1" else "16:00", electric_only=False, speed_kmh=None,
                                external_ref=f"{8800 + i}/2026", until_revoked=False, status="overene",
                                source="rucne", note="FIKTÍVNA"))
    for k in range(2):
        session.add(Closure(kind="uplna", from_station=A, to_station=B, line_ref="", track="",
                            start_utc=now - timedelta(days=400), end_utc=now + timedelta(days=30),
                            daily_from="01:00" if k == 0 else "03:00", daily_to="02:00" if k == 0 else "04:00",
                            electric_only=False, speed_kmh=None, external_ref=f"{8900 + k}/2026", until_revoked=True,
                            status="overene", source="rucne", note="FIKTÍVNA"))
    session.commit()


@pytest.mark.parametrize("before,after", [(30, 1_000_000), (1_000_000, 30)])
def test_s5_rezerva_nad_limitom_neprechadza_roky_okien(env, session, monkeypatch, before, after):
    """[S5] Sada parametrov zo staršieho formulára s rezervou pred výlukou alebo po nej nad limitom 1 440 min: úseky
    simulácie nemajú stovky okien výluk (denné okná ďalej ako 24 h sú súhrnné), prepočet netrvá desiatky sekúnd,
    rozhodnutie je rovnako prísne ako s uloženou hodnotou (ESKALOVAŤ – úsek je s rezervou zatvorený) a dôvod povie,
    že rezerva je nad limitom."""
    from vlaky.planovanie import planovac

    _params(session, buffer_before_closure_min=float(before), buffer_after_closure_min=float(after))
    _old_daily_register(session)
    seen: list = []
    real = planovac.two_stage_recommend

    def spy(routes, *args, **kwargs):
        seen.append(max((len(s.closures) for r in routes for s in r.segments), default=0))
        return real(routes, *args, **kwargs)

    monkeypatch.setattr(planovac, "two_stage_recommend", spy)
    t0 = time.monotonic()
    rec = _decide(session, ready=NOW, origin=K, destination=H)
    took = time.monotonic() - t0
    assert seen and max(seen) <= 80, seen
    assert (rec.decision, rec.color) == ("ESKALOVAŤ", "ČERVENÁ"), rec.summary
    assert any("nad limitom 1 440 min (24 h)" in r for r in rec.reasons), rec.reasons
    assert took < 30, took


def test_s5_rezerva_nad_limitom_nedovoli_zelenu(env, session):
    """[S5] Rezerva po výluke 3 000 min (50 h): posledné okno dennej výluky Ružomberok – Liptovský Mikuláš skončilo
    19.11. 10:00, úsek je s rezervou zatvorený do 21.11. 12:00 – vlak pripravený 21.11. 09:00 s pridelenou trasou 12:30
    pôjde až po nej (ako s uloženou hodnotou), ale jeho správne posúdenie potrebuje okná ďalej ako 24 h: nie je ZELENÝ
    a dôvod to povie."""
    _params(session, buffer_after_closure_min=3000.0)
    _closure(session, daily=("08:00", "10:00"), start=loc(2026, 11, 15, 0), end=loc(2026, 11, 19, 23, 59),
             until_revoked=False, ref="8811/2026")
    rec = _decide(session, ready=NOW, path_at=loc(2026, 11, 21, 12, 30))
    dep = _iso(rec.chosen["depart"]) if (rec.chosen or {}).get("depart") else None
    assert dep is None or dep >= loc(2026, 11, 21, 12), rec.summary
    assert rec.color != "ZELENÁ", (rec.color, rec.summary)
    assert any("nad limitom 1 440 min (24 h)" in r and "nemôže byť ZELENÝ" in r for r in rec.reasons), rec.reasons
    assert any("nad limitom 24 h" in c for c in (rec.data_age or {}).get("obmedzenie") or []), rec.data_age


def test_s5_nastavenia_ukazu_ulozenu_hodnotu_nad_limitom(env, session):
    """[S5] Stránka Nastavenia ukáže, že platná sada má rezervu po výluke 4 200 min nad limitom 1 440 min (24 h), čo
    s ňou robí plánovač a že ju treba opraviť – nie len hodnotu „4200.0“ bez vysvetlenia."""
    from fastapi.testclient import TestClient

    from test_web_spolocne import login_as
    from vlaky.web.app import create_app

    set_fake_now(NOW)
    _params(session, buffer_after_closure_min=4200.0)
    with TestClient(create_app(auto_evaluate=False)) as client:
        login_as(client, "veduci", "veduci")
        page = client.get("/nastavenia").text
    text = re.sub(r"\s+", " ", page)
    assert "uložená hodnota 4 200 min (70,0 h) je nad limitom 1 440 min (24 h)" in text, text[:3000]
    assert "Platná sada parametrov:" in text and "Opravte hodnotu a uložte novú sadu parametrov" in text, text[:3000]


def test_s5_zdravie_a_cyklus_agenta_hlasia_rezervu_nad_limitom(env, session):
    """[S5] Zdravie agenta (aj `python -m vlaky health`) hlási rezervu nad limitom ako problém, cyklus agenta ju hlási
    ako chybu cyklu a dispečeri dostanú k platnej sade parametrov jedno upozornenie DÔLEŽITÉ (nie v každom cykle)."""
    from vlaky.agent.beh import run_cycle
    from vlaky.agent.zdravie import health_status

    set_fake_now(NOW)
    ps = _params(session, buffer_before_closure_min=2000.0)
    ok, detail = health_status()
    assert not ok and "nad limitom 1 440 min (24 h)" in detail.get("nastavenia", ""), detail
    out = run_cycle(NOW)
    assert any(e.startswith("nastavenia rozhodovania:") and "pred výlukou 2 000 min" in e for e in out["errors"]), out
    run_cycle(NOW + timedelta(minutes=2))
    session.expire_all()
    notes = list(session.scalars(select(Notification).where(Notification.dedup_key == f"nastavenia:{ps.id}")))
    sent = [n for n in notes if n.status in ("odoslana", "outbox")]
    assert sent and len({n.recipient for n in sent}) == len(sent), [(n.recipient, n.status) for n in notes]
    assert all(n.priority == "DÔLEŽITÉ" and "nad limitom 24 h" in n.subject for n in sent), sent
