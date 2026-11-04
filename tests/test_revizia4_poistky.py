"""Regresné testy k 4. revízii – poistky plánovača pri údajoch z e-mailov (oblasť P4 „poistky“).

* QO2: výluka z e-mailu, ktorú dispečer ešte neoveril (záznam „navrh“ so zdrojovou udalosťou), nedovolí ZELENÚ
  vlaku, ktorého trasou vedie v čase jeho možnej jazdy (aj variante návrhu k objednávke); overená a ručne zadaná
  výluka nie.
* QP0/QS0: čakajúca výluka či mimoriadnosť z e-mailu bez dopravne (len trať a km) – s číslom trate obmedzí vlaky na
  tejto trati, bez neho každý vlak v jej období; aj návrh k objednávke.
* QP1/QS1: poistky ZELENEJ (výluka mimo siete, čakajúca udalosť z e-mailu) sa posudzujú pri trase, ktorú agent
  odporúča – aj pri obchádzke (PRESMEROVAŤ); varianty a okná inej trasy s poistkou nie sú ZELENÉ.
* QP4: po prijatom „ODSTAVIŤ …, nová trasa na …“ a zrušení pôvodnej trasy agent nevyzýva trasu objednať znova;
  očakávané zrušenie pôvodnej trasy z KIS neznižuje farbu plánu na novú trasu.

Všetky adresy a vlaky sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from test_revizia2_posta import drop_raw, events, mail, strict  # noqa: F401 (strict – fixture)
from vlaky.agent.zdravie import set_kv
from vlaky.cas import fmt, local
from vlaky.hranice import seed_borders
from vlaky.models import (Closure, Driver, DriverShift, ExtractedEvent, Order, Path, Recommendation, Train)
from vlaky.notifikacie.texty import recommendation_body
from vlaky.planovanie.planovac import evaluate_train, plan_order
from vlaky.posta.spracovanie import confirm_event, pending_uncertainty, process_mailbox

pytest_plugins = ["test_web_spolocne"]

UNVERIFIED = "ešte nie je overená dispečerom"
NO_PLACE = "miesto sa nepodarilo prečítať – overte vo fronte"


def _recs(session, train_id) -> list:
    return list(session.scalars(select(Recommendation).where(Recommendation.train_id == train_id)
                                .order_by(Recommendation.id)))


def _train(session, now, number, origin="Žilina", destination="Košice", ready_min=30, path_min=40,
           deadline_h=10, **kw) -> Train:
    ready = now + timedelta(minutes=ready_min)
    t = Train(number=number, run_date=local(ready).date().isoformat(), origin=origin, destination=destination,
              traction=kw.pop("traction", "E"), gauge="1435", vmax_kmh=100, status="planovany", ready_utc=ready,
              deadline_utc=ready + timedelta(hours=deadline_h), length_m=500, weight_t=1500, **kw)
    session.add(t)
    session.flush()
    if path_min is not None:
        session.add(Path(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena",
                         departure_utc=now + timedelta(minutes=path_min)))
    d = Driver(name=f"R. {number} FIKTÍVNY")
    session.add(d)
    session.flush()
    session.add(DriverShift(driver_id=d.id, train_id=t.id, start_utc=ready - timedelta(minutes=80),
                            status="planovana"))
    session.flush()
    return t


def _email_closure(session, now, a, b, start_h, end_h, kind="uplna", status="navrh") -> Closure:
    """Výluka z e-mailu: udalosť (čaká vo fronte) a jej záznam pre plánovač, ako ich zapíše spracovanie pošty."""
    start, end = now + timedelta(hours=start_h), now + timedelta(hours=end_h)
    ev = ExtractedEvent(kind="vyluka_nova", status="potvrdene" if status == "overene" else "navrh",
                        needs_confirmation=True, confidence=0.85,
                        extractor="pravidla", data={"from_station": a, "to_station": b, "start": start.isoformat(),
                                                    "end": end.isoformat(), "kind": kind},
                        validation={"blocking": [], "pending_closure": {"id": None, "note": ""}})
    session.add(ev)
    session.flush()
    c = Closure(kind=kind, from_station=a, to_station=b, start_utc=start, end_utc=end, status=status,
                source="email", source_event_id=ev.id)
    session.add(c)
    session.flush()
    ev.validation = {"blocking": [], "pending_closure": {"id": c.id, "note": ""}}
    session.flush()
    return c


def _blind_event(session, now, kind="mimoriadnost", line_ref=None, start_h=0.5, end_h=8.0, a=None, b=None):
    """Čakajúca udalosť z e-mailu bez záznamu pre plánovač (napr. miesto len „trať 180 v km 212,4“) – ako ju
    zapíše spracovanie pošty: blokujúce chyby, poznámka „údaje nestačia na záznam výluky“."""
    data = {"from_station": a, "to_station": b, "line_ref": line_ref,
            "start": (now + timedelta(hours=start_h)).isoformat() if start_h is not None else None,
            "end": (now + timedelta(hours=end_h)).isoformat() if end_h is not None else None}
    blocking = [x for x, v in (("chýba od dopravne", a), ("chýba do dopravne", b), ("chýba začiatok", start_h),
                               ("chýba koniec", end_h)) if v is None]
    ev = ExtractedEvent(kind=kind, status="navrh", needs_confirmation=True, confidence=0.85, extractor="pravidla",
                        data=data, validation={"blocking": blocking, "pending_closure": {
                            "id": None, "proposed_change_of": None,
                            "note": "údaje nestačia na záznam výluky (úsek alebo čas)"}})
    session.add(ev)
    session.flush()
    return ev


# ====================================================================== QO2 neoverená výluka z e-mailu
def test_qo2_vyluka_z_emailu_cakajuca_na_potvrdenie_nedovoli_zelenu(strict, session, fixed_now):
    """[QO2] ROV z e-mailu (výluka Kysak – Košice o 2 h, čaká vo fronte so záznamom „navrh“): plánovač s ňou počíta
    (ODSTAVIŤ až po výluke), ale údaje z e-mailu mohli byť prečítané nesprávne – kým ich dispečer neoverí, vlak nie je
    ZELENÝ, dôvod to povie a obmedzenie je v data_age (web aj e-mail neukážu ZELENÉ okná). Informačný dôvod
    „1 výluka na trase ešte nie je potvrdená“ sa k nej neuvádza. Po potvrdení je vlak znova ZELENÝ."""
    t = _train(session, fixed_now, "95111")
    session.commit()
    first = evaluate_train(session, t.id, fixed_now, "test")
    assert first.color == "ZELENÁ"
    start, end = local(fixed_now + timedelta(hours=2)), local(fixed_now + timedelta(hours=7))
    body = ("Vážení dopravcovia,\n\noznamujeme Vám, že na základe Rozkazu o výluke (ROV) č. 2601/2026 bude "
            "vykonaná výluka:\n\nÚsek: Kysak – Košice\nDruh výluky: úplná výluka trate\n"
            f"Termín: od {start:%d.%m.%Y %H:%M} do {end:%d.%m.%Y %H:%M}\nDôvod výluky: oprava mosta\n\n"
            "S pozdravom\nOR Košice – prevádzka (FIKTÍVNE)\n")
    drop_raw(strict, "rov.eml", mail("rov2601", "Rozkaz o výluke (ROV) č. 2601/2026 – výluka Kysak – Košice", body))
    summary = process_mailbox(session, now=fixed_now)
    session.commit()
    assert summary["planning"] and summary["planning"]["ok"], summary
    (ev,) = events(session)
    row = session.scalars(select(Closure).where(Closure.source_event_id == ev.id)).one()
    assert ev.status == "navrh" and row.status == "navrh"
    rec = _recs(session, t.id)[-1]
    assert rec.decision == "ODSTAVIŤ" and rec.color == "ORANŽOVÁ", (rec.decision, rec.color, rec.reasons)
    assert any(UNVERIFIED in r and f"udalosť #{ev.id}" in r and "Kysak – Košice" in r and "nemôže byť ZELENÝ" in r
               for r in rec.reasons), rec.reasons
    assert not any("ešte nie je potvrdená dispečerom" in r for r in rec.reasons)       # nie oba dôvody
    assert "výluka z e-mailu na trase ešte nie je overená dispečerom" in rec.data_age["obmedzenie"]
    text = recommendation_body(rec)
    assert "Okná bezpečného odchodu (ZELENÁ)" not in text and "len informatívne" in text
    # dispečer udalosť overí (potvrdí) → výluka je overená, vlak môže byť znova ZELENÝ
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    assert session.get(Closure, row.id).status == "overene"
    again = evaluate_train(session, t.id, fixed_now + timedelta(minutes=1), "potvrdená výluka")
    assert again.decision == "ODSTAVIŤ" and again.color == "ZELENÁ", again.reasons
    assert not any(UNVERIFIED in r for r in again.reasons)
    assert "obmedzenie" not in (again.data_age or {})


def _clear_closures(session) -> None:
    session.query(Closure).delete()
    session.query(ExtractedEvent).delete()
    session.commit()


def test_qo2_overena_a_rucne_zadana_vyluka_poistku_nemaju(session, fixed_now):
    """[QO2] Overená výluka z e-mailu a ručne zadaná výluka („návrh“ bez udalosti) ZELENÚ nebránia; ručný návrh má
    len informačný dôvod. Neoverená výluka z e-mailu mimo obdobia možnej jazdy vlaku (o 3 dni) tiež nie. Neoverená
    výluka z e-mailu v období jazdy ZELENÚ nedovolí."""
    seed_borders(session)
    a = _train(session, fixed_now, "95112")
    b = _train(session, fixed_now, "95113")
    c = _train(session, fixed_now, "95114")
    d = _train(session, fixed_now, "95115")
    _email_closure(session, fixed_now, "Kysak", "Košice", 2, 7, status="overene")
    session.commit()
    rec_a = evaluate_train(session, a.id, fixed_now, "test")
    assert rec_a.color == "ZELENÁ" and not any(UNVERIFIED in r for r in rec_a.reasons)
    _clear_closures(session)
    session.add(Closure(kind="uplna", from_station="Kysak", to_station="Košice", status="navrh", source="manual",
                        start_utc=fixed_now + timedelta(hours=2), end_utc=fixed_now + timedelta(hours=7)))
    session.commit()
    rec_b = evaluate_train(session, b.id, fixed_now, "test")
    assert rec_b.color == "ZELENÁ" and not any(UNVERIFIED in r for r in rec_b.reasons)
    assert any("1 výluka na trase ešte nie je potvrdená dispečerom" in r for r in rec_b.reasons)
    _clear_closures(session)
    _email_closure(session, fixed_now, "Kysak", "Košice", 72, 76)
    session.commit()
    rec_c = evaluate_train(session, c.id, fixed_now, "test")
    assert rec_c.color == "ZELENÁ" and not any(UNVERIFIED in r for r in rec_c.reasons)
    _clear_closures(session)
    _email_closure(session, fixed_now, "Kysak", "Košice", 2, 7)
    session.commit()
    rec_d = evaluate_train(session, d.id, fixed_now, "test")
    assert rec_d.color == "ORANŽOVÁ" and any(UNVERIFIED in r for r in rec_d.reasons)


def test_qo2_navrh_k_objednavke_s_neoverenou_vylukou(session, fixed_now):
    """[QO2] Návrh prepravy k objednávke: varianta po trase s neoverenou výlukou z e-mailu nie je ZELENÁ a dôvod je
    vo vysvetlení aj vo variante."""
    seed_borders(session)
    o = Order(customer="Zákazník FIKTÍVNY", origin="Žilina", destination="Košice",
              earliest_departure_utc=fixed_now + timedelta(hours=1), deadline_utc=fixed_now + timedelta(hours=30))
    session.add(o)
    _email_closure(session, fixed_now, "Kysak", "Košice", 3, 8)
    session.commit()
    plan = plan_order(session, o.id, fixed_now)
    assert plan["variants"]
    assert not any(v["status"] == "ZELENÁ" for v in plan["variants"]), plan["variants"]
    assert any(UNVERIFIED in r for v in plan["variants"] for r in v.get("reasons") or [])
    assert any(UNVERIFIED in e for e in plan["explanation"]), plan["explanation"]


# ====================================================================== QP0/QS0 čakajúca udalosť bez dopravne
def test_qp0_mimoriadnost_len_s_cislom_trate_obmedzi_vlaky_na_trati(session, fixed_now):
    """[QP0/QS0] ŽSR: „porucha … na trati č. 180 v km 212,4 … od 05:30 do 13:00“ – udalosť čaká bez záznamu (chýba
    od/do dopravne). Vlak po trati 180 (Žilina → Poprad-Tatry) nie je ZELENÝ a dôvod hovorí, že miesto sa nepodarilo
    prečítať; vlak na inej trati (Bratislava → Nové Zámky) ostáva ZELENÝ."""
    seed_borders(session)
    on_line = _train(session, fixed_now, "77601", origin="Žilina", destination="Poprad-Tatry")
    other = _train(session, fixed_now, "77602", origin="Bratislava hlavná stanica", destination="Nové Zámky")
    session.commit()
    assert evaluate_train(session, on_line.id, fixed_now, "test").color == "ZELENÁ"
    assert evaluate_train(session, other.id, fixed_now, "test").color == "ZELENÁ"
    ev = _blind_event(session, fixed_now, line_ref="180", start_h=1.5, end_h=9)
    session.commit()
    rec = evaluate_train(session, on_line.id, fixed_now + timedelta(minutes=1), "e-mail")
    assert rec.color != "ZELENÁ", rec.reasons
    note = next(r for r in rec.reasons if f"udalosť #{ev.id}" in r)
    assert NO_PLACE in note and "trať 180" in note and "nemôže byť ZELENÝ" in note
    assert "čakajúca udalosť z e-mailu, s ktorou plánovač nevie počítať" in rec.data_age["obmedzenie"]
    rec_other = evaluate_train(session, other.id, fixed_now + timedelta(minutes=1), "e-mail")
    assert rec_other.color == "ZELENÁ" and not any(f"#{ev.id}" in r for r in rec_other.reasons)


def test_qp0_udalost_bez_miesta_obmedzi_kazdy_vlak_v_jej_obdobi(session, fixed_now):
    """[QP0/QS0/QM2] Mimoriadnosť bez dopravne aj bez trate (miesto sa nepodarilo prečítať) môže byť kdekoľvek: každý
    vlak, ktorého obdobie jazdy sa s ňou prekrýva, nie je ZELENÝ (aj návrh k objednávke); udalosť o 3 dni nie.
    pending_uncertainty to vráti aj bez trasy vlaku."""
    seed_borders(session)
    t = _train(session, fixed_now, "77603", origin="Bratislava hlavná stanica", destination="Nové Zámky")
    o = Order(customer="Zákazník FIKTÍVNY", origin="Žilina", destination="Košice",
              earliest_departure_utc=fixed_now + timedelta(hours=1), deadline_utc=fixed_now + timedelta(hours=30))
    session.add(o)
    later = _blind_event(session, fixed_now, start_h=72, end_h=80)
    session.commit()
    assert evaluate_train(session, t.id, fixed_now, "test").color == "ZELENÁ"
    assert pending_uncertainty(session, t, None, (), window=(fixed_now, fixed_now + timedelta(hours=30))) == []
    ev = _blind_event(session, fixed_now, start_h=0.5, end_h=None)
    session.commit()
    rec = evaluate_train(session, t.id, fixed_now + timedelta(minutes=1), "e-mail")
    assert rec.color != "ZELENÁ"
    note = next(r for r in rec.reasons if f"udalosť #{ev.id}" in r)
    assert NO_PLACE in note and "ktoréhokoľvek vlaku" in note
    assert not any(f"udalosť #{later.id}" in r for r in rec.reasons)
    notes = pending_uncertainty(session, t, None, (), window=(fixed_now, fixed_now + timedelta(hours=30)))
    assert len(notes) == 1 and f"#{ev.id}" in notes[0]
    plan = plan_order(session, o.id, fixed_now)
    assert not any(v["status"] == "ZELENÁ" for v in plan["variants"]), plan["variants"]
    assert any(NO_PLACE in e for e in plan["explanation"])


# ====================================================================== QP1/QS1 poistky pri obchádzke
def _detour_train(session, now, number) -> Train:
    seed_borders(session)
    t = Train(number=number, origin="Bratislava hlavná stanica", destination="Nové Zámky", traction="E",
              gauge="1435", vmax_kmh=100, status="planovany", run_date=now.date().isoformat(), ready_utc=now,
              deadline_utc=now + timedelta(hours=12))
    session.add(t)
    session.add(Closure(kind="uplna", from_station="Bratislava-Vajnory", to_station="Bernolákovo", status="overene",
                        start_utc=now - timedelta(hours=1), end_utc=now + timedelta(hours=40)))
    session.flush()
    return t


def test_qp1_obchadzka_cez_vyluku_mimo_siete_nie_je_zelena(session, fixed_now):
    """[QP1/QS1] Hlavná trasa je 40 h zatvorená, agent odporučí obchádzku cez Trnavu. Výluka „Trnava – Neznáma
    Dopravňa XY“ sa nedá umiestniť na sieť a Trnava leží na obchádzke: PRESMEROVAŤ nesmie byť ZELENÉ a dôvod je
    v odporúčaní."""
    t = _detour_train(session, fixed_now, "77702")
    session.commit()
    plain = evaluate_train(session, t.id, fixed_now, "test")
    assert plain.decision == "PRESMEROVAŤ" and plain.color == "ZELENÁ"
    assert any(x["name"] == "Trnava" for x in plain.path_proposal["stations"])
    session.add(Closure(kind="uplna", from_station="Trnava", to_station="Neznáma Dopravňa XY", status="overene",
                        start_utc=fixed_now + timedelta(hours=5), end_utc=fixed_now + timedelta(hours=14)))
    session.commit()
    rec = evaluate_train(session, t.id, fixed_now + timedelta(minutes=1), "nová výluka")
    assert rec.decision == "PRESMEROVAŤ" and rec.color == "ORANŽOVÁ", (rec.decision, rec.color)
    assert any("Neznáma Dopravňa XY" in r and "nepodarilo umiestniť na sieť" in r for r in rec.reasons)
    assert "výluku sa nepodarilo umiestniť na sieť" in rec.data_age["obmedzenie"]


def test_qp1_obchadzka_cez_cakajucu_vyluku_z_emailu_nie_je_zelena(session, fixed_now):
    """[QP1/QS1] Čakajúca výluka z e-mailu „Cífer – Trnava“ s neprečítaným termínom (bez záznamu pre plánovač) leží
    na odporúčanej obchádzke: PRESMEROVAŤ nie je ZELENÉ (upozornenie dispečerom to sľubuje). Rovnaká udalosť len na
    hlavnej trase (Senec – Galanta) sa obchádzky, ktorou vlak pôjde, netýka."""
    t = _detour_train(session, fixed_now, "77703")
    ev = _blind_event(session, fixed_now, kind="vyluka_nova", a="Cífer", b="Trnava", start_h=None, end_h=None)
    session.commit()
    rec = evaluate_train(session, t.id, fixed_now, "e-mail")
    assert rec.decision == "PRESMEROVAŤ" and rec.color == "ORANŽOVÁ", (rec.decision, rec.color, rec.reasons)
    assert any(f"udalosť #{ev.id}" in r and "Cífer – Trnava" in r for r in rec.reasons)
    assert "čakajúca udalosť z e-mailu, s ktorou plánovač nevie počítať" in rec.data_age["obmedzenie"]
    # tá istá udalosť na hlavnej trase: zvolená obchádzka ňou nevedie
    session.delete(ev)
    session.flush()
    t2 = _detour_train(session, fixed_now, "77704")
    ev2 = _blind_event(session, fixed_now, kind="vyluka_nova", a="Senec", b="Galanta", start_h=None, end_h=None)
    session.commit()
    rec2 = evaluate_train(session, t2.id, fixed_now, "e-mail")
    assert rec2.decision == "PRESMEROVAŤ" and rec2.color == "ZELENÁ", (rec2.decision, rec2.color, rec2.reasons)
    assert not any(f"#{ev2.id}" in r for r in rec2.reasons)


def test_qp1_nezvolena_obchadzka_s_poistkou_nema_zelene_varianty_ani_okna(session, fixed_now):
    """[QP1/QS1] Hlavná trasa je zatvorená len 2 h – agent odporučí ODSTAVIŤ na hlavnej trase (ZELENÁ). Obchádzka cez
    Trnavu je v porovnaní variantov, ale pri nej leží výluka, ktorú sa nepodarilo umiestniť na sieť: jej varianta nie
    je ZELENÁ (s dôvodom) a jej okná sa neponúkajú ako okná bezpečného odchodu; odporúčanie hlavnej trasy to nemení."""
    t = Train(number="77705", origin="Bratislava hlavná stanica", destination="Nové Zámky", traction="E",
              gauge="1435", vmax_kmh=100, status="planovany", run_date=fixed_now.date().isoformat(),
              ready_utc=fixed_now, deadline_utc=fixed_now + timedelta(hours=30))
    seed_borders(session)
    session.add(t)
    session.add(Closure(kind="uplna", from_station="Bratislava-Vajnory", to_station="Bernolákovo", status="overene",
                        start_utc=fixed_now - timedelta(hours=1), end_utc=fixed_now + timedelta(hours=2)))
    session.add(Closure(kind="uplna", from_station="Trnava", to_station="Neznáma Dopravňa XY", status="overene",
                        start_utc=fixed_now + timedelta(hours=5), end_utc=fixed_now + timedelta(hours=14)))
    session.commit()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    primary = f"{t.origin} – {t.destination}"
    assert rec.chosen["route"] == primary and rec.color == "ZELENÁ", (rec.decision, rec.color, rec.chosen["route"])
    detour = [v for v in rec.variants if v["route"].startswith("obchádzka")]
    assert detour and all(v["status"] != "ZELENÁ" for v in detour), detour
    assert all(any("Neznáma Dopravňa XY" in r for r in v["reasons"]) for v in detour)
    assert list(rec.windows) == [primary]


# ====================================================================== QP4 objednaná nová trasa
def _ordered_setup(session, now, number):
    """Vlak s pridelenou trasou o 30 min, ktorú blokuje výluka (-1 h … +8 h): agent odporučí ODSTAVIŤ s novou
    trasou po výluke; dispečer odporúčanie prijme (novú trasu objedná a pôvodnú odriekne)."""
    seed_borders(session)
    t = Train(number=number, run_date=now.date().isoformat(), origin="Čadca", destination="Košice", traction="E",
              gauge="1435", vmax_kmh=100, status="planovany", ready_utc=now, deadline_utc=now + timedelta(hours=30))
    session.add(t)
    session.flush()
    path = Path(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena",
                departure_utc=now + timedelta(minutes=30))
    session.add(path)
    session.add(Closure(kind="uplna", from_station="Liptovský Mikuláš", to_station="Poprad-Tatry", status="overene",
                        start_utc=now - timedelta(hours=1), end_utc=now + timedelta(hours=8)))
    session.flush()
    first = evaluate_train(session, t.id, now, "test")
    assert first.decision == "ODSTAVIŤ" and first.chosen["needs_new_path"]
    first.state = "prijate"
    session.flush()
    return t, path, first


def test_qp4_po_zruseni_povodnej_trasy_nevyzyva_objednat_znova(session, fixed_now):
    """[QP4] Prijaté „ODSTAVIŤ …, pôvodnú trasu odrieknuť, nová trasa na D“; pôvodná trasa je zrušená (vlak nemá
    pridelenú trasu) a výpadok schránky vytvorí nové odporúčanie: text nesmie vyzývať trasu objednať (ani „objednať
    trasu na D“, ani „treba ju objednať“) – trasa je objednaná podľa prijatého odporúčania, overiť jej pridelenie."""
    t, path, first = _ordered_setup(session, fixed_now, "90101")
    dep = fmt(datetime.fromisoformat(first.chosen["depart"]))
    path.status = "zrusena"
    out = fixed_now + timedelta(minutes=45)
    set_kv(session, "mail_last_ok", {"ts": (out - timedelta(minutes=50)).isoformat(), "fetched": 0})
    session.commit()
    rec = evaluate_train(session, t.id, out, "výpadok schránky")
    assert rec.id != first.id and rec.decision == "ODSTAVIŤ" and rec.color == "ORANŽOVÁ"
    assert f"nová trasa na {dep} (objednaná podľa prijatého odporúčania)" in rec.summary, rec.summary
    assert "objednať trasu na" not in rec.summary and "Treba objednať" not in rec.summary
    assert "Vlak zatiaľ nemá pridelenú trasu – treba ju objednať." not in rec.reasons
    assert any("dispečer objednal podľa prijatého odporúčania" in r for r in rec.reasons)


def test_qp4_cakajuce_zrusenie_povodnej_trasy_neznizi_farbu_planu_na_novu_trasu(session, fixed_now):
    """[QP4] KIS potvrdí zrušenie pôvodnej trasy, ktoré prijaté odporúčanie samo nariadilo (udalosť čaká vo fronte):
    plán ide na novú trasu, nech ŽSR pôvodnú zruší alebo nie – žiadne nové ORANŽOVÉ odporúčanie (ďalší e-mail)."""
    t, path, first = _ordered_setup(session, fixed_now, "90102")
    ev = ExtractedEvent(kind="trasa_zrusena", status="navrh", needs_confirmation=True, confidence=0.9,
                        extractor="pravidla", data={"train_number": "90102", "date": fixed_now.date().isoformat()},
                        validation={"blocking": []})
    session.add(ev)
    session.commit()
    rec = evaluate_train(session, t.id, fixed_now + timedelta(minutes=30), "e-mail")
    assert rec.id == first.id and rec.color == "ZELENÁ", (rec.id, first.id, rec.color, rec.reasons)
    # odchod na pôvodnú (zrušovanú) trasu by ZELENÝ nebol – udalosť sa týka trasy, s ktorou plánovač počíta
    notes = pending_uncertainty(session, t, None, [path])
    assert len(notes) == 1 and f"#{ev.id}" in notes[0]


# ====================================================================== pomocné texty (UI7 – e-mail)
def test_texty_cisla_po_slovensky_v_odporucani_a_koncepte(session, fixed_now):
    """[UI7 – e-mail] Limit rušňovodiča „13 h“ (nie „13.0 h“), čakanie „4,7 h“ a hmotnosť „1 500 t“ v koncepte
    objednávky trasy – rovnaký zápis čísel ako na webe."""
    seed_borders(session)
    t = _train(session, fixed_now, "95120", ready_min=360, path_min=None)
    sh = session.scalars(select(DriverShift).where(DriverShift.train_id == t.id)).one()
    sh.start_utc = fixed_now + timedelta(minutes=20)
    session.commit()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    body = recommendation_body(rec)
    assert "limit 13 h do" in body and "13.0 h" not in body
    idle = next(r for r in rec.reasons if "Posunúť nástup rušňovodiča" in r)
    assert " h čakal" in idle and "." not in idle.split("inak by ")[1].split(" h čakal")[0]
    draft = rec.path_proposal["drafts"]["zsr_path_request"]
    assert "Hmotnosť vlaku: 1 500 t" in draft and "Dĺžka vlaku: 500 m" in draft
