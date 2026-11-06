"""Ôsma revízia (pravidlá) – deň a časové okno z tej istej vety (položky zoznamu).

Nález overenia ver16: „sa mení: dňa 24.11.2026 bude od 10:00 do 16:00 hod. a dňa 25.11.2026 bude od 06:00 do 12:00
hod.“ dal dva termíny 25.11. – okno prvého dňa dostalo dátum druhého (vzdialenosť „ bude od “ je dlhšia ako „ a dňa “)
a nový čas 24.11. sa do registra nikdy nedostal (24.11. 14:00 – 16:00 VYPRAVIŤ ZELENÁ).

* Okno patrí dňu vlastnej vety: „dňa D bude od …“, „dňa D od …“, „D: od …“, „D – od …“, „dňa D (utorok) od …“, okno
  pred dňom („od … do … hod. dňa D“), zoznamy s „a“, „,“, „;“, „potom“, novým riadkom, vety „… hod. Dňa …“, noci,
  obdobia „v dňoch … bude denne od …“; koniec okna nie je koncom obdobia, keď za oddeľovačom nasleduje ďalší deň so
  svojím oknom. Druhé okno dňa („… a od 13:00 do 16:00 hod.“) patrí tomuto dňu aj pred dňom vlastnej vety („…, dňa
  24.11. sa výluka nekoná“, „… hod. Dňa 26.11. bude od …“).
* „sa predlžuje do 29.11.2026 od 08:00 do 14:00“ – okno je denné okno obdobia, koniec je 29.11. 14:00 (nie 08:00);
  „namiesto 24.11.2026 dňa 25.11.2026 od …“ – nový deň za pôvodným je novým termínom.
* Kontrola dní (sieť): dva termíny tej istej výluky na tom istom dni, hoci vety termínov uvádzajú aj iný deň bez
  termínu – udalosti čakajú na dispečera s upozornením (istota pod prahom potvrdenia). Overuje sa aj pri párovaní
  pred opravou (simulovaná formulácia, ktorú pravidlá nespárujú) a blízke varianty ju nespustia.
* Korpus ``tests/data/posta_korpus`` (pôvod „r8-pravidla“) – vyše 40 formulácií vrátane blízkych variantov, nocí,
  období, zrušení, presunu „z D1 na D2“, „namiesto“, odpovede a preposlania; presnú zhodu overuje aj
  ``test_revizia5_korpus.test_korpus_presne``.

Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

import itertools
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from test_revizia5_korpus import CASES, _judge, _run, korpus  # noqa: F401 – fixture korpus
from test_revizia7_odobratie_casu import env  # noqa: F401 – env
from test_revizia8_register import (ROV3, SEC, SUBJ, ZSUBJ, _body, _clock, _closed, _confirm, _one,  # noqa: F401
                                    _send, _waiting)

TZ = ZoneInfo("Europe/Bratislava")
R8 = [c for c in CASES if str(c.get("origin", "")).startswith("r8-pravidla")]
NET_NOTE = "vyšli na ten istý deň"


def _ids(c: dict) -> str:
    return c["id"]


def _periods(text: str, ref: date = date(2026, 11, 16)) -> list[tuple]:
    """Obdobia z :mod:`vlaky.posta.datumy` (miestny čas) – (začiatok, koniec, denné okno)."""
    from vlaky.posta import datumy
    from vlaky.posta.text import fold

    f = fold(text)
    return [(f"{p.start:%d.%m. %H:%M}" if p.start else None, f"{p.end:%d.%m. %H:%M}" if p.end else None,
             p.daily_from, p.daily_to) for p in datumy.periods(f, datumy.scan(f, ref))]


def _x(subject: str, body: str, sender: str = "vyluky.orza@zsr.example"):
    from vlaky.posta.extrakcia import extract
    from vlaky.posta.udalosti import MailDocument

    doc = MailDocument(subject=subject, body=body, sender=sender,
                       received_at=datetime(2026, 11, 16, 8, tzinfo=timezone.utc))
    return extract(doc, use_llm=False).items


def _loc(v) -> str | None:
    if not v:
        return None
    return datetime.fromisoformat(str(v)).astimezone(TZ).strftime("%d.%m. %H:%M")


def _terms(items) -> list[tuple]:
    return sorted((_loc(i.data.get("start")), _loc(i.data.get("end"))) for i in items
                  if i.kind in ("vyluka_nova", "vyluka_zmena"))


PAIR = [("24.11. 10:00", "24.11. 16:00"), ("25.11. 06:00", "25.11. 12:00")]


# ====================================================================== datumy: deň a okno z jednej vety
@pytest.mark.parametrize("text", [
    "dňa 24.11.2026 bude od 10:00 do 16:00 hod. a dňa 25.11.2026 bude od 06:00 do 12:00 hod.",
    "dňa 24.11.2026 bude od 10:00 do 16:00 hod.; dňa 25.11.2026 bude od 06:00 do 12:00 hod.",
    "dňa 24.11.2026 bude od 10:00 do 16:00 hod., dňa 25.11.2026 bude od 06:00 do 12:00 hod.",
    "Dňa 24.11.2026 bude od 10:00 do 16:00 hod. Dňa 25.11.2026 bude od 06:00 do 12:00 hod.",
    "dňa 24.11.2026 bude od 10:00 do 16:00 hod.\ndňa 25.11.2026 bude od 06:00 do 12:00 hod.",
    "dňa 24.11.2026 od 10:00 do 16:00\ndňa 25.11.2026 od 06:00 do 12:00",
    "dňa 24.11.2026 bude od 10:00 do 16:00 hod., potom dňa 25.11.2026 bude od 06:00 do 12:00 hod.",
    "dňa 24.11.2026 bude výluka v čase od 10:00 do 16:00 hod. a dňa 25.11.2026 platí od 06:00 do 12:00 hod.",
    "24.11.2026: od 10:00 do 16:00 hod., 25.11.2026: od 06:00 do 12:00 hod.",
    "24.11.2026 – od 10:00 do 16:00 hod., 25.11.2026 – od 06:00 do 12:00 hod.",
    "dňa 24.11.2026 (utorok) od 10:00 do 16:00 hod. a dňa 25.11.2026 (streda) od 06:00 do 12:00 hod.",
    "od 10:00 do 16:00 hod. dňa 24.11.2026 a od 06:00 do 12:00 hod. dňa 25.11.2026.",
    "od 10:00 do 16:00 dňa 24.11.2026, od 06:00 do 12:00 dňa 25.11.2026.",
    "v čase 10:00 – 16:00 hod. dňa 24.11.2026 a v čase 06:00 – 12:00 hod. dňa 25.11.2026.",
], ids=["bude_a_bude", "bodkociarka", "ciarka", "dve_vety", "novy_riadok", "novy_riadok_bez_hod", "potom",
        "slova_vety", "dvojbodka", "pomlcka", "den_v_tyzdni", "okno_pred_dnom", "okno_pred_dnom_ciarka",
        "v_case_pred_dnom"])
def test_okno_patri_dnu_vlastnej_vety(text):
    """Každé okno dostane deň svojej vety (položky zoznamu), nie deň, ktorý je od okna znakovo najbližšie."""
    assert [(s, e) for s, e, _a, _b in _periods(text)] == PAIR, _periods(text)


@pytest.mark.parametrize("text,expected", [
    ("od 10:00 do 16:00 hod. dňa 24.11.2026, od 06:00 do 12:00 hod. dňa 25.11.2026, od 08:00 do 14:00 hod. dňa "
     "26.11.2026.", PAIR + [("26.11. 08:00", "26.11. 14:00")]),
    ("10:00 – 16:00 hod. 24.11.2026\n06:00 – 12:00 hod. 25.11.2026\n08:00 – 14:00 hod. 26.11.2026",
     PAIR + [("26.11. 08:00", "26.11. 14:00")]),
    ("Od 10:00 do 16:00 hod. 24.11.2026. Od 06:00 do 12:00 hod. 25.11.2026.", PAIR),
    ("od 22:00 do 04:00 hod. 24.11.2026, od 23:00 do 05:00 hod. 25.11.2026.",
     [("24.11. 22:00", "25.11. 04:00"), ("25.11. 23:00", "26.11. 05:00")]),
], ids=["tri_okna_dna", "riadky_bez_dna", "vety", "noci"])
def test_zoznam_okno_pred_dnom(text, expected):
    """Zoznam „okno – deň“: deň hneď za oknom (bez oddeľovača) je dňom tohto okna, nie začiatkom ďalšieho okna za
    čiarkou – inak by si dni okná vymenili (každý deň by mal jeden termín, len s cudzím časom); okno cez polnoc v
    takom zozname začína uvedeným dňom."""
    assert [(s, e) for s, e, _a, _b in _periods(text)] == expected, _periods(text)


_DAYS = [("24.11.2026", 24), ("25.11.2026", 25), ("26.11.2026", 26)]
_WINS = [("10:00", "16:00"), ("06:00", "12:00"), ("08:00", "14:00")]


def test_kombinacie_den_okno_v_zozname():
    """Všetky kombinácie zoznamu dvoch a troch dní s vlastnými oknami: tvar dňa („dňa D“, „D“, „D:“, „dňa D (streda)“,
    „v deň D“) × slová vety („bude“, „bude výluka“, „platí“, „sa výluka uskutoční“, „výluka potrvá“) × tvar okna ×
    oddeľovač („ a “, „, “, „; “, „, potom “, nový riadok, koniec vety, „, resp. “) × poradie deň – okno či okno – deň.
    Každé okno dostane deň svojej položky (pred opravou 2 440 z 3 024 kombinácií nesprávne)."""
    from vlaky.posta import datumy
    from vlaky.posta.text import fold

    date_fmt = ["dňa {d}", "{d}", "{d}:", "dňa {d} (streda)", "v deň {d}"]
    filler = ["", "bude ", "bude výluka ", "platí ", "sa výluka uskutoční ", "výluka potrvá "]
    win_fmt = ["od {a} do {b} hod.", "od {a} do {b}", "v čase {a} – {b} hod.", "{a} – {b} hod."]
    seps = [" a ", ", ", "; ", ", potom ", "\n", ". ", ", resp. "]
    bad, total = [], 0
    for n, dfmt, fill, wfmt, sep, first in itertools.product((2, 3), date_fmt, filler, win_fmt, seps, ("den", "okno")):
        if first == "okno" and dfmt.endswith(":"):
            continue
        total += 1
        parts = []
        for i in range(n):
            w, d = wfmt.format(a=_WINS[i][0], b=_WINS[i][1]), dfmt.format(d=_DAYS[i][0])
            parts.append(f"{d} {fill}{w}" if first == "den" else f"{fill}{w} {d.replace(':', '')}")
        text = "sa mení: " + sep.join(parts)
        f = fold(text)
        got = sorted((p.start.day, f"{p.start:%H:%M}", f"{p.end:%H:%M}")
                     for p in datumy.periods(f, datumy.scan(f, date(2026, 11, 16))) if p.start and p.end)
        if got != sorted((_DAYS[i][1], *_WINS[i]) for i in range(n)):
            bad.append((text, got))
    assert total == 3024 and not bad, f"{len(bad)} z {total}: " + "; ".join(f"{t!r} -> {g}" for t, g in bad[:5])


def test_noci_a_obdobia_dostanu_okno_svojej_vety():
    """Noc „v noci z D na D+1 bude od …“ patrí dňu, v ktorom začína; obdobie „v dňoch … bude denne od …“ dostane
    svoje okno, nie okno susednej položky."""
    assert _periods("v noci z 24. na 25.11.2026 bude od 22:00 do 04:00 hod. a v noci z 25. na 26.11.2026 bude od 23:00 "
                    "do 05:00 hod.") == [("24.11. 22:00", "25.11. 04:00", None, None),
                                         ("25.11. 23:00", "26.11. 05:00", None, None)]
    assert _periods("v noci z 24. na 25.11.2026 bude od 22:00 do 04:00 hod. a dňa 26.11.2026 bude od 08:00 do 14:00 "
                    "hod.") == [("24.11. 22:00", "25.11. 04:00", None, None),
                                ("26.11. 08:00", "26.11. 14:00", None, None)]
    assert _periods("v dňoch 23.11. – 24.11.2026 bude denne od 10:00 do 16:00 hod. a v dňoch 26.11. – 27.11.2026 bude "
                    "denne od 06:00 do 12:00 hod.") == [("23.11. 10:00", "24.11. 16:00", "10:00", "16:00"),
                                                        ("26.11. 06:00", "27.11. 12:00", "06:00", "12:00")]
    assert _periods("dňa 25.11.2026 bude od 06:00 do 12:00 hod. a v dňoch 23.11. – 24.11.2026 bude denne od 10:00 do "
                    "16:00 hod.") == [("25.11. 06:00", "25.11. 12:00", None, None),
                                      ("23.11. 10:00", "24.11. 16:00", "10:00", "16:00")]


def test_blizke_varianty_parovania():
    """Blízke varianty: zoznam dní so spoločným oknom, jeden deň s dvoma oknami, obdobie s koncom v ďalší deň, rok
    pred dvojbodkou, okno cez polnoc pred dňom (bez obdobia – noc začínajúca či končiaca v ten deň nie je istá)."""
    assert [(s, e) for s, e, _a, _b in _periods("dňa 24.11. a 25.11.2026 bude od 10:00 do 16:00 hod.")] == [
        ("24.11. 10:00", "24.11. 16:00"), ("25.11. 10:00", "25.11. 16:00")]
    two = _periods("dňa 24.11.2026 bude od 08:00 do 10:00 hod. a od 13:00 do 16:00 hod.")
    assert [(s, e) for s, e, _a, _b in two] == [("24.11. 08:00", "24.11. 10:00"), ("24.11. 13:00", "24.11. 16:00")]
    assert _periods("dňa 23.11.2026 bude od 08:00 do 16:00 dňa 24.11.2026.") == [
        ("23.11. 08:00", "24.11. 16:00", None, None)]
    # MR3: dátum za čiarkou bez vlastného času ostáva koncom obdobia (prísnejší prípad)
    assert _periods("od 12.11.2026 8:00 do 14:00, 13.11.2026.") == [("12.11. 08:00", "13.11. 14:00", None, None)]
    assert _periods("od 22:00 do 04:00 hod. dňa 25.11.2026.") == []


W25 = [("25.11. 06:00", "25.11. 10:00"), ("25.11. 13:00", "25.11. 16:00")]


@pytest.mark.parametrize("text,expected", [
    ("dňa 25.11.2026 bude od 06:00 do 10:00 a od 13:00 do 16:00 hod., dňa 24.11.2026 sa výluka nekoná.", W25),
    ("dňa 25.11.2026 bude od 06:00 do 10:00 a od 13:00 do 16:00 hod. Dňa 24.11.2026 sa výluka nekoná.", W25),
    ("dňa 25.11.2026 od 06:00 do 10:00 a od 13:00 do 16:00 hod., dňa 24.11.2026 platí pôvodný čas.", W25),
    ("dňa 25.11.2026 bude od 06:00 do 10:00 a od 13:00 do 16:00 hod. Dňa 26.11.2026 bude od 06:00 do 12:00 hod.",
     W25 + [("26.11. 06:00", "26.11. 12:00")]),
    ("dňa 25.11.2026 od 06:00 do 10:00 a od 13:00 do 16:00 hod. Dňa 26.11.2026 od 06:00 do 12:00 hod.",
     W25 + [("26.11. 06:00", "26.11. 12:00")]),
    ("dňa 24.11.2026 od 08:00 do 10:00 a od 13:00 do 16:00 hod., 25.11. a 26.11.2026 od 06:00 do 12:00 hod.",
     [("24.11. 08:00", "24.11. 10:00"), ("24.11. 13:00", "24.11. 16:00"), ("25.11. 06:00", "25.11. 12:00"),
      ("26.11. 06:00", "26.11. 12:00")]),
    ("od 08:00 do 12:00 hod. dňa 24.11.2026, 25.11. a 26.11.2026 od 06:00 do 12:00 hod.",
     [("24.11. 08:00", "24.11. 12:00"), ("25.11. 06:00", "25.11. 12:00"), ("26.11. 06:00", "26.11. 12:00")]),
    ("dňa 25.11.2026 bude od 06:00 do 12:00 hod., dňa 24.11.2026 sa výluka nekoná.",
     [("25.11. 06:00", "25.11. 12:00")]),
    ("dňa 25.11.2026 od 06:00 do 12:00 hod., 24.11.2026 bez zmeny.", [("25.11. 06:00", "25.11. 12:00")]),
    ("od 10:00 do 16:00 hod., dňa 24.11.2026 (utorok).", [("24.11. 10:00", "24.11. 16:00")]),
    ("od 10:00 do 16:00 hod., dňa 24.11.2026, výluka sa týka koľaje č. 1.", [("24.11. 10:00", "24.11. 16:00")]),
    # okno s vlastným dňom hneď za ním k reťazi nepatrí (ako doteraz)
    ("dňa 25.11.2026 od 06:00 do 10:00 a od 13:00 do 16:00 hod. dňa 26.11.2026.",
     [("25.11. 06:00", "25.11. 10:00"), ("26.11. 13:00", "26.11. 16:00")]),
], ids=["ciarka_zrusenie", "veta_zrusenie", "ciarka_povodny_cas", "veta_dalsi_den_bude", "veta_dalsi_den_od",
        "ciarka_zoznam_dni", "okno_den_ciarka_zoznam", "skorsi_den_zrusenie", "skorsi_den_bez_zmeny", "den_v_tyzdni",
        "den_ciarka_pokracuje", "vlastny_den_za_oknom"])
def test_druhe_okno_dna_a_den_vlastnej_vety(text, expected):
    """Druhé okno dňa („dňa 25.11. od 06:00 do 10:00 a od 13:00 do 16:00 hod.“) a za čiarkou či koncom vety deň vlastnej
    vety („…, dňa 24.11. sa výluka nekoná“, „… hod. Dňa 26.11. bude od …“, zoznam „25.11. a 26.11. od …“) – okno patrí
    dňu pred ním (pred opravou ho dostal deň vlastnej vety, či sa stratilo). Deň za čiarkou, ktorý je skôr ako začiatok
    obdobia, nie je jeho koncom (pred opravou obdobie 25.11. 06:00 – 24.11. 12:00); deň na konci vety je deň okna."""
    assert [(s, e) for s, e, _a, _b in _periods(text)] == expected


def test_druhe_okno_dna_a_zrusenie_ineho_dna_caka(cfg):
    """„… a od 13:00 do 16:00 hod., dňa 24.11.2026 sa výluka nekoná.“ – oba nové časy 25.11. (nie 24.11. 13:00 – 16:00
    ako pred týmto doplnením) a udalosti čakajú na dispečera s upozornením (rušený deň v tej istej vete)."""
    items = _x(ZSUBJ, _body(f"{SEC} sa mení: dňa 25.11.2026 bude od 06:00 do 10:00 a od 13:00 do 16:00 hod., dňa "
                            "24.11.2026 sa výluka nekoná."))
    changes = [i for i in items if i.kind == "vyluka_zmena"]
    assert _terms(changes) == W25
    assert changes and all(i.confidence < 0.8 for i in changes), [i.confidence for i in changes]
    assert all(any("24.11.2026" in n and "overte" in n.lower() for n in i.notes) for i in changes)


def test_rok_pred_dvojbodkou_je_uvedeny():
    """„24.11.2026: od …“ – rok je uvedený (bez príznaku doplneného roka); „24.11. 10:00“ – 10 je hodina, nie rok."""
    from vlaky.posta import datumy
    from vlaky.posta.text import fold

    f = fold("24.11.2026: od 10:00 do 16:00 hod.")
    (p,) = datumy.periods(f, datumy.scan(f, date(2026, 11, 16)))
    assert p.start == datetime(2026, 11, 24, 10) and "rok_doplneny" not in p.flags
    toks = datumy.scan(fold("dňa 24.11. 10:00 – 16:00"), date(2026, 11, 16))
    assert [(t.kind, t.dt) for t in toks][:1] == [("datetime", datetime(2026, 11, 24, 10))]


def test_predlzenie_do_dna_s_oknom_konci_koncom_okna(cfg):
    """„sa predlžuje do 29.11.2026 od 08:00 do 14:00“ – okno je denné okno predĺženej výluky: koniec 29.11. 14:00 (pred
    opravou 29.11. 08:00 – čas 08:00 – 14:00 posledného dňa by ostal otvorený)."""
    items = _x("Zmena ROV č. 2609/2026", _body(f"{SEC} sa predlžuje do 29.11.2026 od 08:00 do 14:00 hod."))
    (it,) = [i for i in items if i.kind == "vyluka_zmena"]
    assert it.data["zamer"] == "predlzenie" and _loc(it.data["end"]) == "29.11. 14:00"
    assert (it.data["daily_from"], it.data["daily_to"]) == ("08:00", "14:00")


@pytest.mark.parametrize("text", [
    f"{SEC} bude namiesto 24.11.2026 dňa 25.11.2026 od 06:00 do 12:00 hod.",
    f"{SEC} bude namiesto 24.11.2026 od 08:00 do 14:00 hod. dňa 25.11.2026 od 06:00 do 12:00 hod.",
    f"{SEC}: namiesto dňa 24.11.2026 bude výluka dňa 25.11.2026 od 06:00 do 12:00 hod.",
], ids=["namiesto_den_dna", "namiesto_s_oknom", "namiesto_dna"])
def test_namiesto_pred_novym_dnom(cfg, text):
    """Nový deň za pôvodným („namiesto 24.11.2026 dňa 25.11.2026 od …“) je novým termínom presunu – nestratí sa ako
    „pôvodný“; pôvodný deň nie je nepriradeným dátumom."""
    items = _x(ZSUBJ, _body(text))
    (it,) = [i for i in items if i.kind == "vyluka_zmena"]
    assert it.data["zamer"] == "presun" and _terms(items) == [("25.11. 06:00", "25.11. 12:00")]
    assert not any("nepodarilo priradiť" in n for n in it.notes), it.notes


# ====================================================================== kontrola dní (sieť)
_FINDING = (f"{SEC} sa mení: dňa 24.11.2026 bude od 10:00 do 16:00 hod. a dňa 25.11.2026 bude od 06:00 do 12:00 "
            "hod.")


def _nearest_date(folded, windows, k, near, tails, ws):
    wa, wb = windows[k]
    return min(near, key=lambda t: min(abs(wa.start - t.end), abs(t.start - wb.end), abs(ws - t.end)))


@pytest.fixture(params=["nove", "povodne"])
def parovanie(request, monkeypatch):
    """„povodne“ – párovanie dňa a okna ako pred opravou (najbližší dátum podľa znakov, slová vety „bude“ deň s oknom
    nespoja): simuluje formuláciu, ktorú pravidlá nespárujú, aby sa kontrola dní overila nezávisle od párovania."""
    if request.param == "povodne":
        from vlaky.posta import datumy

        monkeypatch.setattr(datumy, "_own_window_gap",
                            lambda folded, toks, j, gap: len(gap) <= 16 and bool(datumy._AT_GAP.match(gap)))
        monkeypatch.setattr(datumy, "_clause_date", _nearest_date)
    return request.param


def test_kontrola_dni_dva_terminy_v_jeden_den_cakaju(cfg, monkeypatch):
    """Pri párovaní pred opravou dá formulácia nálezu dva termíny 25.11., hoci e-mail uvádza aj 24.11. – kontrola dní
    dá obom udalostiam istotu pod prahom potvrdenia a upozornenie s oboma dňami (aj pri novej výluke)."""
    from vlaky.posta import datumy

    monkeypatch.setattr(datumy, "_own_window_gap",
                        lambda folded, toks, j, gap: len(gap) <= 16 and bool(datumy._AT_GAP.match(gap)))
    monkeypatch.setattr(datumy, "_clause_date", _nearest_date)
    for subject, text, kind in ((ZSUBJ, _FINDING, "vyluka_zmena"),
                                (SUBJ, "úplná výluka v úseku Ružomberok – Liptovský Mikuláš (ROV č. 2331/2026) dňa "
                                 "24.11.2026 bude od 10:00 do 16:00 hod. a dňa 25.11.2026 bude od 06:00 do 12:00 hod.",
                                 "vyluka_nova")):
        items = [i for i in _x(subject, _body(text)) if i.kind == kind and "siet_proti_strate" not in i.evidence]
        assert _terms(items) == [("25.11. 06:00", "25.11. 12:00"), ("25.11. 10:00", "25.11. 16:00")], _terms(items)
        for it in items:
            assert it.confidence <= 0.55, it.confidence
            note = next(n for n in it.notes if NET_NOTE in n)
            assert "na ten istý deň 25.11.2026" in note and "aj deň 24.11.2026, ktorý žiadny termín nemá" in note
            assert "Overte termíny podľa e-mailu" in note and it.evidence.get("kontrola_dni") == "2026-11-25"


@pytest.mark.parametrize("text", [
    f"{SEC} sa mení: dňa 24.11.2026 bude od 08:00 do 10:00 hod. a od 13:00 do 16:00 hod.",
    f"{SEC} sa mení: dňa 24.11.2026 od 08:00 do 10:00 a od 13:00 do 16:00 hod., dňa 25.11.2026 od 08:00 do 12:00 hod.",
    f"{SEC} sa presúva z 24.11.2026 na 25.11.2026, kde bude od 06:00 do 10:00 a od 13:00 do 16:00 hod.",
    f"{SEC} bude dňa 25.11.2026 (namiesto 24.11.2026) od 06:00 do 10:00 a od 13:00 do 16:00 hod.",
    f"{SEC} zo dňa 20.11.2026 sa mení: dňa 25.11.2026 od 06:00 do 10:00 a od 13:00 do 16:00 hod.",
    f"{SEC} dňa 24.11.2026 sa ruší. Dňa 25.11.2026 bude výluka od 06:00 do 10:00 a od 13:00 do 16:00 hod.",
], ids=["jeden_den_dve_okna", "dva_dni_tri_okna", "presun_z_na", "namiesto", "datum_vydania", "zrusenie_inej_vety"])
def test_kontrola_dni_blizke_varianty_bez_upozornenia(cfg, parovanie, text):
    """Dva termíny jedného dňa sú v poriadku, keď iný uvedený deň termín má, je pôvodným dňom presunu („z … na …“,
    „namiesto“), dátumom vydania či rušeným dňom inej vety – kontrola dní sa nespustí (pri novom aj pôvodnom
    párovaní)."""
    items = _x(ZSUBJ, _body(text))
    assert len([i for i in items if i.kind == "vyluka_zmena"]) >= 2
    assert not any(NET_NOTE in n for i in items for n in i.notes), [n for i in items for n in i.notes]
    assert all(i.confidence > 0.55 for i in items if i.kind == "vyluka_zmena")


# ====================================================================== korpus r8-pravidla
def test_korpus8_rozsah():
    """Korpus má vyše 40 formulácií párovania dňa a okna v skupinách deň-okno, okno-deň, noc, viac dní, zoznam, presun
    („z D1 na D2“, „namiesto“), zámer, blízke varianty, zrušenie, citácia (odpoveď, preposlanie) a nová výluka;
    fiktívny obsah (adresy *.example)."""
    groups = {c["origin"].split(":", 1)[1].split()[0] for c in R8}
    assert {"deň-okno", "okno-deň", "noc", "viac-dní", "zoznam", "presun", "zámer", "blízky", "zrušenie", "citácia",
            "nová"} <= groups, groups
    assert len(R8) >= 40 and len({c["id"] for c in R8}) == len(R8)
    assert all(str(c.get("sender", "")).endswith(".example") for c in R8)
    texts = " ".join(c["body"] for c in R8)
    assert "sa presúva z 24.11.2026 na 25.11.2026" in texts and "namiesto" in texts
    assert any(c["subject"].startswith("RE:") for c in R8) and any(c["subject"].startswith("Fwd:") for c in R8)


@pytest.mark.parametrize("case", R8, ids=_ids)
def test_korpus8_bezpecne_a_zamer(korpus, case):
    """Žiadny nebezpečný výsledok (stratený termín, zrušenie navyše, miernejší druh); zámer zmeny podľa orákula;
    kontrola dní pri správne spárovaných termínoch neupozorňuje (žiadny falošný poplach)."""
    items = _run(korpus, case)
    _inexact, unsafe = _judge(case, items)
    assert not unsafe, f"{case['id']}: " + "; ".join(unsafe)
    for x in case["expect"]:
        if x.get("zamer"):
            zamers = [it.data.get("zamer") for it in items if it.kind == "vyluka_zmena"]
            assert x["zamer"] in zamers, f"{case['id']}: zámer {zamers} != {x['zamer']}"
    noted = [n for it in items for n in it.notes if NET_NOTE in n]
    assert not noted, f"{case['id']}: " + "; ".join(noted)


# ====================================================================== celý tok: e-mail → register → plánovač
_VARIANTS = {
    "bude_a_bude": f"{SEC} sa mení: dňa 24.11.2026 bude od 10:00 do 16:00 hod. a dňa 25.11.2026 bude od 06:00 do 12:00 "
                   "hod.",
    "bodkociarka": f"{SEC} sa mení: dňa 24.11.2026 bude od 10:00 do 16:00 hod.; dňa 25.11.2026 bude od 06:00 do 12:00 "
                   "hod.",
    "ciarka": f"{SEC} sa mení: dňa 24.11.2026 bude od 10:00 do 16:00 hod., dňa 25.11.2026 bude od 06:00 do 12:00 hod.",
    "dve_vety": f"{SEC} sa mení. Dňa 24.11.2026 bude od 10:00 do 16:00 hod. Dňa 25.11.2026 bude od 06:00 do 12:00 hod.",
    "riadky": f"{SEC} sa mení:\ndňa 24.11.2026 od 10:00 do 16:00\ndňa 25.11.2026 od 06:00 do 12:00",
    "okno_pred_dnom": f"{SEC} sa mení: od 10:00 do 16:00 hod. dňa 24.11.2026 a od 06:00 do 12:00 hod. dňa 25.11.2026.",
    "dvojbodka": f"{SEC} sa mení: 24.11.2026: od 10:00 do 16:00 hod., 25.11.2026: od 06:00 do 12:00 hod.",
}


@pytest.mark.parametrize("variant", list(_VARIANTS))
def test_novy_cas_dvoch_dni_v_registri(env, session, variant):
    """ROV 23. – 25.11. denne 08 – 14 (overený); zmena so zoznamom dvoch dní s vlastnými časmi. Udalosti majú dni
    24.11. a 25.11.; po zápise (a potvrdení čakajúcich) je 24.11. 15:00 aj 25.11. 07:00 zatvorené – nový čas 24.11.
    sa nestratil (pred opravou VYPRAVIŤ ZELENÁ 24.11. 15:00)."""
    rov = _one(env, session, SUBJ, ROV3)
    _confirm(session, rov)
    evs = _send(env, session, ZSUBJ, _body(_VARIANTS[variant]))
    assert _terms(evs) == PAIR, [(e.kind, e.data.get("start"), e.data.get("end")) for e in evs]
    for e in evs:
        if _waiting(e):
            _confirm(session, e)
    assert _closed(session, 2026, 11, 24, 15) and _closed(session, 2026, 11, 25, 7)
    assert _closed(session, 2026, 11, 23, 10)


def test_kontrola_dni_udalosti_cakaju_vo_fronte(env, session, monkeypatch):
    """Formulácia, ktorú pravidlá nespárujú (simulované párovanie pred opravou): obe udalosti čakajú na dispečera
    (istota pod prahom) a vo fronte majú upozornenie – nezapíšu sa samé ako dva termíny 25.11."""
    from vlaky.posta import datumy

    monkeypatch.setattr(datumy, "_own_window_gap",
                        lambda folded, toks, j, gap: len(gap) <= 16 and bool(datumy._AT_GAP.match(gap)))
    monkeypatch.setattr(datumy, "_clause_date", _nearest_date)
    rov = _one(env, session, SUBJ, ROV3)
    _confirm(session, rov)
    evs = _send(env, session, ZSUBJ, _body(_FINDING))
    assert len(evs) == 2 and all(_waiting(e) for e in evs), [(e.status, e.needs_confirmation) for e in evs]
    for e in evs:
        val = e.validation or {}
        assert any(NET_NOTE in n for n in val.get("notes") or []), val.get("notes")
        assert any("pod prahom" in r for r in val.get("confirmation_reasons") or []), val.get("confirmation_reasons")
    # pôvodný termín ROV ostáva (nič sa nezapísalo)
    assert _closed(session, 2026, 11, 24, 10) and _closed(session, 2026, 11, 25, 10)
