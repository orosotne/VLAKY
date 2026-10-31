"""Extrakcia pravidlami: každá ukážka, slovenské a české formulácie, dátumy a časy."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from vlaky.posta import datumy
from vlaky.posta.extrakcia import RuleExtractor, extract
from vlaky.posta.parser import parse_message
from vlaky.posta.pravidla import restriction_kind
from vlaky.posta.text import clean_text, fold
from vlaky.posta.udalosti import SCHEMAS, MailDocument

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "ukazkove_emaily"
RECEIVED = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def doc_from(name: str) -> MailDocument:
    pm = parse_message((SAMPLES / name).read_bytes())
    return MailDocument(pm.subject, pm.body_text, [(a.filename, a.text) for a in pm.attachments], pm.sender,
                        pm.sender_name, RECEIVED)


def rules(text: str, subject: str = "FIKTÍVNE – ukážka") -> list:
    return RuleExtractor().extract(MailDocument(subject, text, received_at=RECEIVED))


def same_station(a, b) -> bool:  # noqa: ANN001
    return " ".join(fold(str(a)).replace("-", " ").split()) == " ".join(fold(str(b)).replace("-", " ").split())


def check(item, expected: dict) -> None:
    for k, v in expected.items():
        got = item.data.get(k)
        if k in ("from_station", "to_station", "origin", "destination"):
            assert same_station(got, v), (k, got, v)
        else:
            assert got == v, (k, got, v)


Z = "+00:00"
EXPECTED = {
    "01_rov_vyluka_kolaje_poprad_lm.eml": [("vyluka_nova", dict(
        from_station="Poprad-Tatry", to_station="Liptovský Mikuláš", kind="kolaj", track="1", line_ref="180",
        start="2026-11-12T07:00:00" + Z, end="2026-11-12T13:00:00" + Z, external_ref="1142/2026"))],
    "02_oznamenie_vyluk_tyzden_47.eml": [
        ("vyluka_nova", dict(from_station="Kraľovany", to_station="Ružomberok", kind="kolaj", track="2",
                             start="2026-11-16T07:00:00" + Z, end="2026-11-20T13:00:00" + Z, daily_from="08:00",
                             daily_to="14:00", external_ref="1150/2026")),
        ("vyluka_nova", dict(from_station="Žilina", to_station="Čadca", kind="trakcne_vedenie", track="obe",
                             start="2026-11-18T07:00:00" + Z, end="2026-11-18T13:00:00" + Z,
                             external_ref="1151/2026"))],
    "03_pomala_jazda_bez_diakritiky.eml": [("pomala_jazda", dict(
        from_station="Štrba", to_station="Poprad-Tatry", kind="pomala_jazda", speed_kmh=50, track="obe",
        start="2026-11-15T23:00:00" + Z, end="2026-11-30T23:00:00" + Z))],
    "04_zmena_vyluky_preposlana.eml": [("vyluka_zmena", dict(
        from_station="Poprad-Tatry", to_station="Liptovský Mikuláš", start="2026-11-12T08:30:00" + Z,
        end="2026-11-12T15:00:00" + Z, external_ref="1142/2026"))],
    "05_zrusenie_vyluky.eml": [("vyluka_zrusenie", dict(
        external_ref="1150/2026", from_station="Kraľovany", to_station="Ružomberok", start="2026-11-16T07:00:00" + Z))],
    "06_mimoriadna_udalost.eml": [("mimoriadnost", dict(
        from_station="Ružomberok", to_station="Liptovský Mikuláš", start="2026-11-02T02:15:00" + Z,
        end="2026-11-02T08:00:00" + Z))],
    "07_pridelenie_trasy_nex54321.eml": [("trasa_pridelena", dict(
        train_number="54321", date="2026-11-14", kind="ad_hoc", external_ref="ZIA-2026-0815"))],
    "08_zmena_trasy_nex54321.eml": [("trasa_zmena", dict(
        train_number="54321", date="2026-11-14", external_ref="KIS-2026-447812"))],
    "09_kis_zrusenie_trasy_pn12345.eml": [("trasa_zrusena", dict(
        train_number="12345", date="2026-11-15", external_ref="KIS-2026-445120"))],
    "10_avizo_cadca_cz.eml": [("avizo", dict(train_number="47250", border_code="CADCA",
                                             eta="2026-11-03T12:35:00" + Z))],
    "11_avizo_rusovce_rajka.eml": [("avizo", dict(train_number="43130", border_code="RUSOVCE",
                                                  eta="2026-11-04T05:20:00" + Z))],
    "12_srov_tabulka_xlsx.eml": [
        ("vyluka_nova", dict(from_station="Trnava", to_station="Leopoldov", kind="kolaj", track="1", line_ref="120",
                             start="2026-11-23T07:00:00" + Z, end="2026-11-23T14:00:00" + Z,
                             external_ref="48/2026/1")),
        ("vyluka_nova", dict(from_station="Galanta", to_station="Šaľa", kind="trakcne_vedenie", track="obe",
                             start="2026-11-24T21:00:00" + Z, end="2026-11-25T03:00:00" + Z)),
        ("vyluka_nova", dict(from_station="Leopoldov", to_station="Piešťany", kind="uplna",
                             start="2026-11-26T08:00:00" + Z, end="2026-11-26T12:00:00" + Z)),
        ("pomala_jazda", dict(from_station="Šaľa", to_station="Nové Zámky", speed_kmh=60, track="2",
                              start="2026-11-23T05:00:00" + Z, end="2026-11-29T17:00:00" + Z))],
    "13_objednavka_pdf_cz.eml": [("objednavka", dict(
        customer="FIKTIVNI Logistika s.r.o.", origin="Kúty", destination="Žilina-Teplička",
        earliest_departure="2026-11-20T05:00:00" + Z, deadline="2026-11-21T13:00:00" + Z, wagons=22,
        weight_t=1480, length_m=590, rid=False))],
    "14_prompt_injection.eml": [("vyluka_nova", dict(
        from_station="Bratislava-Petržalka", to_station="Rusovce", kind="kolaj", start="2026-11-25T07:00:00" + Z,
        end="2026-11-25T11:00:00" + Z))],
    "16_ine_informacia.eml": [],
    "17_telegram_potr.eml": [("pomala_jazda", dict(
        from_station="Kysak", to_station="Košice", speed_kmh=30, track="2", until_revoked=True, end=None,
        start="2026-11-03T05:00:00" + Z, external_ref="37/2026"))],
    "18_kis_ponuka_trasy_nex54390.eml": [("trasa_pridelena", dict(
        train_number="54390", date="2026-11-20", kind="ad_hoc", offer=True, accept_by="2026-11-18T11:00:00" + Z,
        external_ref="KIS-2026-450077"))],
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_rule_extraction_on_samples(cfg, name):
    items = RuleExtractor().extract(doc_from(name))
    expected = EXPECTED[name]
    assert [i.kind for i in items] == [k for k, _ in expected], [(i.kind, i.data) for i in items]
    for item, (_, fields) in zip(items, expected):
        check(item, fields)
        assert 0.5 <= item.confidence <= 0.85
        assert set(item.data) == set(SCHEMAS[item.kind])        # dáta presne podľa schémy
        kind, data, confidence, notes = item                     # rozbalí sa ako štvorica
        assert kind == item.kind and isinstance(notes, list)


def test_every_sample_is_covered_and_every_kind_appears(cfg):
    names = {p.name for p in SAMPLES.glob("*.eml")}
    assert names - set(EXPECTED) == {"15_nepovoleny_odosielatel.eml"}     # karanténa sa neextrahuje
    kinds = {k for v in EXPECTED.values() for k, _ in v} | {extract(doc_from("16_ine_informacia.eml")).items[0].kind}
    assert kinds == set(SCHEMAS)


def test_path_points_with_times_and_midnight(cfg):
    (item,) = RuleExtractor().extract(doc_from("07_pridelenie_trasy_nex54321.eml"))
    pts = item.data["points"]
    assert [p["station"] for p in pts][:3] == ["Čadca", "Žilina", "Vrútky"] and len(pts) == 10
    assert pts[0] == {"station": "Čadca", "arr": None, "dep": "2026-11-14T05:10:00+00:00"}
    assert pts[-1] == {"station": "Košice", "arr": "2026-11-14T09:48:00+00:00", "dep": None}
    (offer,) = RuleExtractor().extract(doc_from("18_kis_ponuka_trasy_nex54390.eml"))
    by = {p["station"]: p for p in offer.data["points"]}
    assert by["Bratislava východ"]["dep"] == "2026-11-20T23:10:00+00:00"     # 00:10 miestneho času 21. 11.
    assert by["Devínska Nová Ves"]["arr"] == "2026-11-20T23:32:00+00:00"
    assert any("polnoc" in n for n in offer.notes)


def test_ine_for_unrecognised_mail_and_agent_replies(cfg):
    res = extract(doc_from("16_ine_informacia.eml"))
    assert [i.kind for i in res.items] == ["ine"] and res.items[0].confidence < 0.5
    reply = RuleExtractor().extract(MailDocument("RE: [AGENT][DÔLEŽITÉ] Vlak 12345 Čadca → Košice: POČKAŤ – výluka "
                                                 "Poprad-Tatry – Liptovský Mikuláš", "PRIJAŤ"))
    assert [i.kind for i in reply] == ["ine"]


# ---------------------------------------------------------------------- formulácie SK / CZ
def test_closure_without_diacritics_and_hyphen_section(cfg):
    (it,) = rules("Vyluka kolaje v useku Kralovany-Ruzomberok dna 12.11.2026 od 8.00 do 14.00 hod.")
    assert it.kind == "vyluka_nova" and it.data["kind"] == "kolaj"
    check(it, dict(from_station="Kraľovany", to_station="Ružomberok", start="2026-11-12T07:00:00" + Z,
                   end="2026-11-12T13:00:00" + Z))


def test_closure_medzi_stanicami_and_month_name(cfg):
    (it,) = rules("Úplná výluka medzi stanicami Vrútky a Kraľovany dňa 16. novembra 2026 v čase 22:00 – 04:00.")
    check(it, dict(from_station="Vrútky", to_station="Kraľovany", kind="uplna", start="2026-11-16T21:00:00" + Z,
                   end="2026-11-17T03:00:00" + Z))
    assert any("polnoc" in n for n in it.notes)


def test_czech_slow_running(cfg):
    (it,) = rules("Oznamujeme zavedení pomalé jízdy v úseku Čadca – Kysucké Nové Mesto, rychlost omezena na 40 km/h, "
                  "od 10.11.2026 6:00 do 12.11.2026 18:00.", "FIKTIVNÍ – ukázka: pomalá jízda")
    assert it.kind == "pomala_jazda"
    check(it, dict(from_station="Čadca", to_station="Kysucké Nové Mesto", speed_kmh=40,
                   start="2026-11-10T05:00:00" + Z, end="2026-11-12T17:00:00" + Z))


def test_heat_restriction_potr_daily_window(cfg):
    (it,) = rules("Pri výstrahe SHMÚ 3. stupňa (horúčavy) sa v úseku Nové Zámky – Štúrovo zavádza POTR max. 80 km/h "
                  "denne v čase 10:00 – 21:00 v dňoch 15.07.–17.07.2027.", "FIKTÍVNE – ukážka: POTR horúčavy")
    assert it.kind == "pomala_jazda"
    check(it, dict(speed_kmh=80, daily_from="10:00", daily_to="21:00", start="2027-07-15T08:00:00" + Z,
                   end="2027-07-17T19:00:00" + Z))


def test_czech_path_allocation_and_avizo_words(cfg):
    (it,) = rules("Přidělení trasy vlaku Nex 54321 dne 14.11.2026:\nČadca  -  06:10\nŽilina  06:52  07:05\n"
                  "Košice  10:48  -", "FIKTIVNÍ – ukázka: přidělení trasy")
    assert it.kind == "trasa_pridelena" and it.data["train_number"] == "54321" and len(it.data["points"]) == 3
    (av,) = rules("Předpokládaný příjezd vlaku Pn 47251 do Kút: 05.11.2026 v 22:10 hod.", "FIKTIVNÍ – ukázka: avízo")
    assert av.kind == "avizo" and av.data["border_code"] == "KUTY" and av.data["eta"] == "2026-11-05T21:10:00" + Z


def test_order_in_plain_slovak(cfg):
    (it,) = rules("Objednávame prepravu.\nZákazník: FIKTÍVNA Logistika s.r.o.\nOdosielacia stanica: Dunajská Streda\n"
                  "Cieľová stanica: Košice\nTovar: kontajnery, RID: áno (UN 1202)\nPočet vozňov: 20\n"
                  "Hmotnosť: cca 900 t\nDĺžka: 520 m\nNajskorší odchod: 16.11.2026 06:00\n"
                  "Termín dodania: 17.11.2026", "FIKTÍVNE – ukážka: Objednávka prepravy")
    check(it, dict(customer="FIKTÍVNA Logistika s.r.o.", origin="Dunajská Streda", destination="Košice", wagons=20,
                   weight_t=900, length_m=520, rid=True, earliest_departure="2026-11-16T05:00:00" + Z,
                   deadline="2026-11-17T22:59:00" + Z))
    assert any("23:59" in n for n in it.notes)


@pytest.mark.parametrize("text,expected", [
    ("Výluka 1. TK", ("kolaj", False)),
    ("výluka koľaje, jednokoľajná prevádzka", ("kolaj", False)),
    ("Vypnutie TV v úseku", ("trakcne_vedenie", False)),
    ("napäťová výluka TV, obe traťové koľaje", ("trakcne_vedenie", False)),
    ("úplná výluka úseku", ("uplna", False)),
    ("výluka obe TK", ("uplna", False)),
    ("POTR 30 km/h", ("pomala_jazda", False)),
    ("prechodné obmedzenie traťovej rýchlosti", ("pomala_jazda", False)),
    ("výluka v úseku", ("uplna", True)),
])
def test_restriction_kind(text, expected):
    assert restriction_kind(fold(text)) == expected


# ---------------------------------------------------------------------- dátumy a časy
@pytest.mark.parametrize("text,start,end,daily", [
    ("od 12.11.2026 08:00 do 12.11.2026 14:00", "2026-11-12 08:00", "2026-11-12 14:00", None),
    ("12.11.2026 v čase 08:00 – 14:00", "2026-11-12 08:00", "2026-11-12 14:00", None),
    ("denne 08:00–14:00 v dňoch 10.11.–14.11.2026", "2026-11-10 08:00", "2026-11-14 14:00", ("08:00", "14:00")),
    ("v dňoch 10. – 14. 11. 2026 v čase 8:00 – 14:00", "2026-11-10 08:00", "2026-11-14 14:00", ("08:00", "14:00")),
    ("dňa 12. 11. 2026 od 8.00 hod. do 14.00 hod.", "2026-11-12 08:00", "2026-11-12 14:00", None),
    ("dňa 18. 11. 2026 od 22.00 hod. do 4.00 hod.", "2026-11-18 22:00", "2026-11-19 04:00", None),
    ("od 8.00 hod. dňa 18.11.2026 do 14.00 hod. dňa 18.11.2026", "2026-11-18 08:00", "2026-11-18 14:00", None),
    ("platí od 05.11.2026 do 30.11.2026", "2026-11-05 00:00", "2026-12-01 00:00", None),
    ("2026-11-12T08:00 – 2026-11-12T14:00", "2026-11-12 08:00", "2026-11-12 14:00", None),
    ("výluka 28.12.–3.1.2027 denne 09:00 – 11:00", "2026-12-28 09:00", "2027-01-03 11:00", ("09:00", "11:00")),
])
def test_periods(text, start, end, daily):
    f = fold(clean_text(text))
    (p,) = datumy.periods(f, datumy.scan(f, date(2026, 11, 2)))
    assert p.start.strftime("%Y-%m-%d %H:%M") == start and p.end.strftime("%Y-%m-%d %H:%M") == end
    assert ((p.daily_from, p.daily_to) if p.daily_from else None) == daily


def test_until_revoked_and_numbers_are_not_dates():
    f = fold("od 03.11.2026 06:00 do odvolania")
    (p,) = datumy.periods(f, datumy.scan(f, date(2026, 11, 2)))
    assert p.end is None and "do_odvolania" in p.flags
    f = fold("km 245,300 – 246,100, 50 km/h, 1 650 t, vlak 54321, tel. 0905 123 456, 1. TK")
    assert datumy.scan(f, date(2026, 11, 2)) == []


def test_year_is_inferred_from_received_date():
    f = fold("dňa 12.11. od 08:00 do 14:00")
    (p,) = datumy.periods(f, datumy.scan(f, date(2026, 11, 2)))
    assert p.start == datetime(2026, 11, 12, 8, 0) and "rok_doplneny" in p.flags
    f = fold("dňa 5.1. od 08:00 do 14:00")       # január po novembri = budúci rok
    (p,) = datumy.periods(f, datumy.scan(f, date(2026, 11, 2)))
    assert p.start.year == 2027


def test_local_time_is_converted_to_utc_including_summer_time(cfg):
    (it,) = rules("Výluka 1. TK v úseku Kraľovany – Ružomberok od 12.06.2027 08:00 do 12.06.2027 14:00.")
    assert it.data["start"] == "2027-06-12T06:00:00" + Z            # CEST = UTC+2
