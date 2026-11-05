"""6. revízia – strana pravidiel (FA), oprava po kontrole: pravidlá e-mailov ŽSR.

Každá oprava s formuláciou z kontroly a blízkymi variantmi (aj kontrolné prípady, ktoré sa nesmú zmeniť):

* dátumy: dve obdobia, každé s vlastným denným oknom – okno za obdobím patrí jemu, nie ďalšiemu obdobiu (regresia);
  okno za obdobím aj pri čiarke a dátume ďalšieho obdobia; ďalšie okno toho istého dňa („…, ďalej od 13:00 do 16:00“,
  „na 06:00 – 12:00 a 13:00 – 16:00“); noci „v nociach na 26.11. a 27.11.“;
* zámer zmeny: pridanie termínu („ešte“, „i dňa“, „zopakuje“, „opätovne“, „pridáva“, „ďalší / druhý termín“, „Termín
  10.12. platí“, „aj v noci z … na …“, „len od …“, „v novom čase“) je doplnenie – nikdy zámer, ktorý termín nahrádza;
  viac nových termínov tej istej výluky so zámerom, ktorý nahrádza – doplnenie (žiadny sa nestratí);
* zrušenie s novým termínom: termín za slovami nového termínu (aj v predmete, „presúva sa na“, „namiesto nej bude“,
  „Zároveň oznamujeme výluku …“) nie je rušeným termínom – je to nová výluka; termín vnútri rušeného termínu
  („Výluka sa uskutoční 10.12. od 10:00 do 14:00“) sa nestratí; iný úsek sa nezruší;
* čiastočné zrušenie: dni v týždni za výnimkou a v časti o tom, čo ostáva, ostávajú; rozsahy dní v týždni; sieť –
  zrušenie so slovom o časti výluky a bez určeného termínu je čiastočné (zápis ho odmietne, celú výluku nezruší);
* rušený termín: „…, len popoludní od …“ ostáva; „okrem stredy 25.11.“; „ponechané sú len dni …“; denné okno
  v období po dňoch (nie súvislý čas cez noci); vedľajšia veta s dôvodom („…, pretože …“) termínom nie je;
* sieť proti strate termínu: lehoty a administratíva („Odpoveď očakávame do …“, „Zmena platí od …“, „sa nepracuje“)
  termínom výluky nie sú; termín bez úseku pri viacerých úsekoch – udalosť na každom úseku (nie poistka pre celý
  vozový park); samostatné časové okno bez obdobia sa nestratí; „nebude úsek … prejazdný“ je úplná výluka;
* číslo novej výluky z vlastnej vety pri predmete s viacerými číslami; dôvod potvrdenia s desatinnou čiarkou.

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("Europe/Bratislava")
ZA = "vyluky.orza@zsr.example"
TT = "vyluky.ortt@zsr.example"
KE = "vyluky.orke@zsr.example"
R = "2026-11-18 10:00"
RD = "2026-12-01 09:00"
RL = ("Ružomberok", "Liptovský Mikuláš")
TL = ("Trnava", "Leopoldov")


@pytest.fixture(scope="module")
def cfg():
    from pathlib import Path

    from vlaky import config as config_mod

    root = Path(__file__).resolve().parent.parent
    config_mod.override_settings(env="test", database_url="sqlite://", data_dir=root / "data", llm_provider="none",
                                 allowed_senders="zsr.example,partner.example,zakaznik.example,dopravca.example")
    yield
    config_mod._OVERRIDE.clear()


def _recv(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).astimezone(timezone.utc)


def _x(subject: str, body: str, received: str = R, sender: str = ZA):
    from vlaky.posta.extrakcia import extract
    from vlaky.posta.udalosti import MailDocument

    doc = MailDocument(subject=subject, body="Dobrý deň,\n\n" + body + "\n\nS pozdravom\nOR skúšobná, oddelenie výluk",
                       sender=sender, received_at=_recv(received))
    return extract(doc, use_llm=False).items


def _l(v) -> str | None:
    if not v:
        return None
    return datetime.fromisoformat(str(v)).astimezone(TZ).strftime("%d.%m. %H:%M")


def _name(text):
    from vlaky.posta.stanice import stations

    st = stations().match(text) if text else None
    return st.name if st else text


def _sec(it) -> tuple:
    return _name(it.data.get("from_station")), _name(it.data.get("to_station"))


def _term(it) -> tuple:
    return _l(it.data.get("start")), _l(it.data.get("end"))


def _show(items) -> str:
    return "; ".join(f"{i.kind} {i.data} {i.notes}" for i in items)


def _of(items, kind: str) -> list:
    return [i for i in items if i.kind == kind]


def _net(items) -> list:
    return [i for i in items if (i.evidence or {}).get("siet_proti_strate") == "termin"]


def _closed(items) -> list:
    """Termíny udalostí, ktoré výluku zapisujú (nová výluka, zmena) – [(úsek, začiatok, koniec)] zoradené."""
    return sorted((_sec(i), *_term(i)) for i in items if i.kind in ("vyluka_nova", "vyluka_zmena"))


def _cancels(items) -> list:
    return sorted(_term(i) for i in _of(items, "vyluka_zrusenie"))


def _periods(text: str) -> list:
    from vlaky.posta import datumy
    from vlaky.posta.text import fold

    f = fold(text)
    return [(f"{p.start:%d.%m. %H:%M}", f"{p.end:%d.%m. %H:%M}" if p.end else None, p.daily_from, p.daily_to)
            for p in datumy.periods(f, datumy.scan(f, date(2026, 11, 18)))]


# ====================================================================== dátumy: obdobia s vlastným denným oknom
@pytest.mark.parametrize("text,want", [
    ("úplná výluka v dňoch 23.11. – 27.11.2026 denne 10:00 – 12:00 a 30.11. – 04.12.2026 denne 08:00 – 14:00.",
     [("23.11. 10:00", "27.11. 12:00", "10:00", "12:00"), ("30.11. 08:00", "04.12. 14:00", "08:00", "14:00")]),
    ("úplná výluka od 23.11.2026 do 27.11.2026 denne v čase 10:00 – 12:00 hod., od 30.11.2026 do 04.12.2026 denne "
     "v čase 08:00 – 14:00 hod.",
     [("23.11. 10:00", "27.11. 12:00", "10:00", "12:00"), ("30.11. 08:00", "04.12. 14:00", "08:00", "14:00")]),
    ("v dňoch 23.11. – 27.11.2026, vždy od 10:00 do 12:00 hod. a v dňoch 30.11. – 04.12.2026, vždy od 08:00 do "
     "14:00 hod.",
     [("23.11. 10:00", "27.11. 12:00", "10:00", "12:00"), ("30.11. 08:00", "04.12. 14:00", "08:00", "14:00")]),
    ("v dňoch 23.11. – 25.11.2026 denne 10:00 – 12:00, 26.11. – 27.11.2026 denne 08:00 – 14:00 a 30.11. – "
     "04.12.2026 denne 06:00 – 10:00.",
     [("23.11. 10:00", "25.11. 12:00", "10:00", "12:00"), ("26.11. 08:00", "27.11. 14:00", "08:00", "14:00"),
      ("30.11. 06:00", "04.12. 10:00", "06:00", "10:00")]),
    # kontrola: zoznam období s jedným oknom a presun na nový termín s oknom
    ("v dňoch 23.11. – 27.11. a 30.11. – 04.12.2026 denne 08:00 – 14:00.",
     [("23.11. 08:00", "27.11. 14:00", "08:00", "14:00"), ("30.11. 08:00", "04.12. 14:00", "08:00", "14:00")]),
    ("výluka sa presúva z 23.11. – 27.11.2026 na 30.11. – 04.12.2026, denne 08:00 – 14:00.",
     [("23.11. 00:00", "28.11. 00:00", None, None), ("30.11. 08:00", "04.12. 14:00", "08:00", "14:00")]),
], ids=["denne_a", "od_do_v_case", "kontrola_vzdy_od", "tri_obdobia", "kontrola_zoznam", "kontrola_presun"])
def test_dve_obdobia_kazde_s_vlastnym_oknom(text, want):
    """[dátumy, regresia] Okno hneď za obdobím patrí jemu – druhé obdobie nedostane okno prvého a prvé nie je celé
    dni (po potvrdení by vlak 01.12. o 09:00 dostal ZELENÚ)."""
    assert _periods(text) == want


@pytest.mark.parametrize("text,want", [
    ("úplná výluka trate Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 10:00 hod., ďalej od 13:00 do 16:00 hod.",
     [("10.12. 08:00", "10.12. 10:00", None, None), ("10.12. 13:00", "10.12. 16:00", None, None)]),
    ("výluka Trnava – Leopoldov dňa 10.12.2026 (ROV č. 3807/2026) sa mení z 08:00 – 14:00 hod. na 06:00 – 12:00 hod. "
     "a 13:00 – 16:00 hod.",
     [("10.12. 06:00", "10.12. 12:00", None, None), ("10.12. 13:00", "10.12. 16:00", None, None)]),
    ("úplná výluka v nociach na 26.11. a 27.11.2026 vždy od 22:00 do 04:00 hod.",
     [("25.11. 22:00", "26.11. 04:00", None, None), ("26.11. 22:00", "27.11. 04:00", None, None)]),
    # kontrola: okno s vlastným dňom za ním nie je oknom predchádzajúceho dňa
    ("dňa 10.12.2026 od 08:00 do 10:00 a dňa 11.12.2026 od 13:00 do 16:00.",
     [("10.12. 08:00", "10.12. 10:00", None, None), ("11.12. 13:00", "11.12. 16:00", None, None)]),
], ids=["dalej_od", "zmena_na_dve_okna", "v_nociach_na", "kontrola_vlastny_den"])
def test_dalsie_okno_toho_isteho_dna_a_noci(text, want):
    """[osamelé okno, noci] Ďalšie okno toho istého dňa dostane deň; „v nociach na 26.11. a 27.11.“ sú noci, ktoré
    v uvedené dni končia (25./26. a 26./27.11.)."""
    assert _periods(text) == want


# ====================================================================== zámer zmeny – pridanie termínu
_P = "Výluka Trnava – Leopoldov podľa ROV č. 3160/2026"


@pytest.mark.parametrize("body", [
    _P + " sa vykoná ešte 17.12.2026 od 08:00 do 14:00 hod.",
    _P + " sa bude konať i dňa 17.12.2026 od 08:00 do 14:00 hod.",
    _P + " sa zopakuje 17.12.2026 od 08:00 do 14:00 hod.",
    _P + " bude opätovne 17.12.2026 od 08:00 do 14:00 hod.",
    _P + ": k termínu 10.12.2026 sa pridáva termín 17.12.2026 od 08:00 do 14:00 hod.",
    _P + " bude v ďalšom termíne 17.12.2026 od 08:00 do 14:00 hod.",
    _P + " – druhý termín: 17.12.2026 od 08:00 do 14:00 hod.",
    _P + " sa uskutoční 17.12.2026 od 08:00 do 14:00 hod. Termín 10.12.2026 platí.",
    _P + " bude 17.12.2026 od 06:00 do 12:00 hod. Termín 10.12.2026 zostáva bez zmeny.",
    _P + " sa dopĺňa o deň 17.12.2026, kedy bude výluka len od 08:00 do 10:00 hod.",
    _P + " bude aj dňa 17.12.2026, a to len od 08:00 do 12:00 hod.",
    _P + " sa uskutoční aj 17.12.2026, ale iba od 10:00 do 14:00 hod.",
    _P + " bude aj dňa 17.12.2026 v skrátenom čase od 08:00 do 12:00 hod.",
    _P + " sa zopakuje 17.12.2026 v novom čase od 06:00 do 12:00 hod.",
    _P + " bude ešte 17.12.2026 v novom čase od 06:00 do 12:00 hod.",
], ids=["este", "i_dna", "zopakuje", "opatovne", "pridava", "dalsi_termin", "druhy_termin", "termin_plati",
        "termin_zostava", "doplna_len_od", "aj_len_od", "aj_ale_iba", "skrateny_cas", "zopakuje_novy_cas",
        "este_novy_cas"])
def test_pridanie_terminu_je_doplnenie(cfg, body):
    """[FC2] Pridanie termínu je doplnenie – „len od …“, „v skrátenom / novom čase“ opisujú nový termín, nie
    pôvodnú výluku (zámer, ktorý termín nahrádza, by v registri zmazal pôvodný deň 10.12.)."""
    items = _x("Zmena ROV č. 3160/2026", body, RD, TT)
    changes = _of(items, "vyluka_zmena")
    assert changes and all(i.data.get("zamer") == "doplnenie" for i in changes), _show(items)
    assert all(_term(i)[0].startswith("17.12.") for i in changes), _show(items)


def test_pridanie_noci_z_na_nie_je_presun(cfg):
    """[FC2] „budú aj v noci z 30.11. na 01.12.2026“ – zápis noci nie je presun „z 30.11. na 01.12.“."""
    items = _x("Zmena ROV č. 4505/2026", "Nočné výluky Kysak – Košice podľa ROV č. 4505/2026 budú aj v noci z 30.11. "
               "na 01.12.2026 od 22:00 do 04:00 hod.", R, KE)
    (ch,) = _of(items, "vyluka_zmena")
    assert ch.data.get("zamer") == "doplnenie" and _term(ch) == ("30.11. 22:00", "01.12. 04:00"), _show(items)


@pytest.mark.parametrize("body,zamer", [
    (_P + " sa presúva aj s NAD na 17.12.2026 od 08:00 do 14:00 hod.", "presun"),
    (_P + " sa namiesto 10.12. vykoná 17.12.2026 od 08:00 do 14:00 hod.", "presun"),
    (_P + " sa nevykoná 10.12., ale až 17.12.2026 od 08:00 do 14:00 hod.", "presun"),
], ids=["presuva", "namiesto", "nevykona_ale_az"])
def test_kontrola_presun_ostava_presunom(cfg, body, zamer):
    """[FC2, kontrola] Výslovný presun ostáva presunom (s novým termínom)."""
    items = _x("Zmena ROV č. 3160/2026", body, RD, TT)
    (ch,) = _of(items, "vyluka_zmena")
    assert (ch.data.get("zamer"), _term(ch)) == (zamer, ("17.12. 08:00", "17.12. 14:00")), _show(items)


def test_viac_novych_terminov_s_nahradzajucim_zamerom_je_doplnenie(cfg):
    """[FC7, osamelé okno] „sa mení z 08:00 – 14:00 hod. na 06:00 – 12:00 hod. a 13:00 – 16:00 hod.“ – oba nové
    časy; zmena času by v registri nahradila termín a druhá zmena by zmazala prvú – obe sú doplnením s poznámkou."""
    items = _x("Zmena ROV č. 3807/2026", "Výluka Trnava – Leopoldov dňa 10.12.2026 (ROV č. 3807/2026) sa mení z 08:00 – "
               "14:00 hod. na 06:00 – 12:00 hod. a 13:00 – 16:00 hod.", RD, TT)
    changes = _of(items, "vyluka_zmena")
    assert sorted(_term(i) for i in changes) == [("10.12. 06:00", "10.12. 12:00"), ("10.12. 13:00", "10.12. 16:00")]
    assert all(i.data.get("zamer") == "doplnenie" for i in changes), _show(items)
    assert all(any("nové termíny tej istej výluky" in n for n in i.notes) for i in changes), _show(items)
    assert not _net(items), _show(items)


# ====================================================================== zrušenie s novým termínom
@pytest.mark.parametrize("subject,body,sender,cancel,new", [
    ("Zrušenie výluky – ROV č. 2331/2026", "výluka v úseku Ružomberok – Liptovský Mikuláš dňa 24.11.2026 sa ruší a "
     "presúva sa na 28.11.2026 v rovnakom čase.", ZA, ("24.11. 00:00", "25.11. 00:00"),
     (RL, "28.11. 00:00", "29.11. 00:00")),
    ("Zrušenie výluky dňa 10.12.2026 – ROV č. 3122/2026", "Výluka Trnava – Leopoldov sa presúva na 17.12.2026 od 08:00 "
     "do 14:00 hod.", TT, ("10.12. 00:00", "11.12. 00:00"), (TL, "17.12. 08:00", "17.12. 14:00")),
    ("Zrušenie ROV č. 3128/2026 – náhradný termín 15.12.2026 08:00 – 14:00", "Výluka Trnava – Leopoldov dňa "
     "10.12.2026 sa ruší.", TT, ("10.12. 00:00", "11.12. 00:00"), (TL, "15.12. 08:00", "15.12. 14:00")),
    ("Zrušenie ROV č. 3131/2026", "Ruší sa výluka Trnava – Leopoldov 10.12.2026 (ROV č. 3131/2026); namiesto nej bude "
     "výluka 17.12.2026 od 08:00 do 14:00 hod.", TT, ("10.12. 00:00", "11.12. 00:00"), (TL, "17.12. 08:00", "17.12. 14:00")),
    ("Zrušenie ROV č. 3132/2026", "Výluka Trnava – Leopoldov dňa 10.12.2026 sa ruší. Zároveň oznamujeme výluku "
     "Leopoldov – Piešťany dňa 11.12.2026 od 08:00 do 14:00 hod.", TT, ("10.12. 00:00", "11.12. 00:00"),
     (("Leopoldov", "Piešťany"), "11.12. 08:00", "11.12. 14:00")),
    ("STORNO ROV č. 3129/2026", "STORNO ROV č. 3129/2026 (Trnava – Leopoldov 10.12.2026 od 08:00 do 14:00). Výluka sa "
     "uskutoční 10.12.2026 od 10:00 do 14:00 hod.", TT, ("10.12. 08:00", "10.12. 14:00"), (TL, "10.12. 10:00", "10.12. 14:00")),
], ids=["rusi_a_presuva", "predmet_den_telo_presun", "predmet_nahradny_termin", "namiesto_nej", "zaroven_iny_usek",
        "storno_uzsie_okno"])
def test_zrusenie_s_novym_terminom(cfg, subject, body, sender, cancel, new):
    """[FC1] Termín za slovami nového termínu nie je rušeným termínom – je novou výlukou (aj v predmete, aj na inom
    úseku); termín, ktorý leží v rušenom termíne, sa nestratí (zrušenie ruší pôvodný termín celý)."""
    items = _x(subject, body, RD if sender == TT else R, sender)
    assert _cancels(items) == [cancel], _show(items)
    assert _closed(items) == [new], _show(items)


def test_zrusenie_iny_usek_sa_nezrusi(cfg):
    """[FC1] Úsek vety „Zároveň oznamujeme výluku Leopoldov – Piešťany …“ sa nezruší (žiadne zrušenie navyše)."""
    items = _x("Zrušenie ROV č. 3132/2026", "Výluka Trnava – Leopoldov dňa 10.12.2026 sa ruší. Zároveň oznamujeme výluku "
               "Leopoldov – Piešťany dňa 11.12.2026 od 08:00 do 14:00 hod.", RD, TT)
    assert [_sec(i) for i in _of(items, "vyluka_zrusenie")] == [TL], _show(items)


@pytest.mark.parametrize("body,cancel,new", [
    ("Výluka Trnava – Leopoldov dňa 10.12.2026 sa ruší. Výluka sa vykoná dňa 18.12.2026 od 08:00 do 14:00 hod.",
     ("10.12. 00:00", "11.12. 00:00"), [(TL, "18.12. 08:00", "18.12. 14:00")]),
    ("Výluka Trnava – Leopoldov dňa 10.12.2026 sa ruší. Nový termín bude oznámený.",
     ("10.12. 00:00", "11.12. 00:00"), []),
    ("Výluka Trnava – Leopoldov dňa 10.12.2026 sa ruší, pretože práce boli vykonané už 05.12.2026 od 08:00 do "
     "14:00 hod.", ("10.12. 00:00", "11.12. 00:00"), []),
], ids=["kontrola_vykona", "kontrola_buduci_termin", "dovod_nie_je_termin"])
def test_kontrola_zrusenie_a_dovod(cfg, body, cancel, new):
    """[FC1, kontrola] Nový termín v ďalšej vete ostáva novou výlukou, budúci termín bez dátumu nič nepridá; dátum
    vo vedľajšej vete s dôvodom („…, pretože …“) nie je ďalším rušeným termínom."""
    items = _x("Zrušenie ROV č. 3136/2026", body, RD, TT)
    assert _cancels(items) == [cancel], _show(items)
    assert _closed(items) == new, _show(items)


# ====================================================================== čiastočné zrušenie – dni v týždni, sieť
_S3 = "Zrušenie výluky – ROV č. 2501/2026"
_P3 = "Výluka podľa ROV č. 2501/2026 v úseku Ružomberok – Liptovský Mikuláš"


def _days(*dd: int) -> list:
    return [(f"{d:02d}.11. 00:00", f"{d + 1:02d}.11. 00:00") for d in dd]


@pytest.mark.parametrize("tail,want", [
    (" v dňoch 23.11. – 29.11.2026 sa ruší okrem soboty a nedele.", _days(23, 24, 25, 26, 27)),
    (" v dňoch 23.11. – 29.11.2026 sa ruší okrem soboty.", _days(23, 24, 25, 26, 27) + _days(29)),
    (" v dňoch 23.11. – 29.11.2026 sa nevykoná, s výnimkou soboty a nedele.", _days(23, 24, 25, 26, 27)),
    (" v dňoch 23.11. – 29.11.2026 sa ruší, výluka bude len v sobotu a v nedeľu.", _days(23, 24, 25, 26, 27)),
    (" v dňoch 23.11. – 29.11.2026 sa ruší okrem piatku a soboty.", _days(23, 24, 25, 26) + _days(29)),
    (" v dňoch 23.11. – 29.11.2026 sa ruší, ale v sobotu a v nedeľu ostáva.", _days(23, 24, 25, 26, 27)),
    (" v dňoch 23.11. – 29.11.2026 sa ruší mimo víkendu.", _days(23, 24, 25, 26, 27)),
    (" v dňoch 23.11. – 29.11.2026 sa nevykoná v dňoch pondelok až streda.", _days(23, 24, 25)),
    # kontrola: dni, ktoré sa rušia, ostávajú rušené
    (" v dňoch 23.11. – 29.11.2026 sa nevykoná v sobotu a v nedeľu, ani v piatok.", _days(27, 28, 29)),
    (" v dňoch 23.11. – 29.11.2026 sa ruší v pracovné dni, v sobotu a v nedeľu sa vykoná.", _days(23, 24, 25, 26, 27)),
    (" v dňoch 23.11. – 29.11.2026, ktorá platí od pondelka do piatku, sa v stredu ruší.", _days(25)),
], ids=["okrem_soboty_a_nedele", "okrem_soboty", "s_vynimkou", "bude_len", "okrem_piatku_a_soboty", "ale_ostava",
        "mimo_vikendu", "rozsah_dni", "kontrola_ani", "kontrola_pracovne_dni", "vedlajsia_veta_opisuje"])
def test_dni_v_tyzdni_vynimka_a_co_ostava(cfg, tail, want):
    """[FC3] Deň v týždni za výnimkou či v časti o tom, čo ostáva, sa neruší – rušené sú ostatné dni obdobia (nikdy
    práve tie, ktoré ostávajú); rozsah „pondelok až streda“ dá všetky dni rozsahu."""
    assert _cancels(_x(_S3, _P3 + tail)) == want


@pytest.mark.parametrize("subject,tail", [
    (_S3, " sa nevykoná v dňoch pondelok až streda."),
    (_S3, " sa nevykoná v dňoch utorok – štvrtok."),
    (_S3, " sa ruší okrem soboty."),
    (_S3, " sa ruší s výnimkou nedele."),
    (_S3, " sa ruší v prvý deň výluky."),
    (_S3, " sa ruší v posledný deň."),
    (_S3, " sa ruší v dopoludňajších hodinách."),
    (_S3, " sa ruší v dňoch pracovného pokoja."),
    (_S3, " sa ruší počas sviatkov."),
    (_S3, " sa ruší v druhej polovici týždňa."),
    ("Zrušenie výluky v nedeľu – ROV č. 2501/2026", " sa ruší."),
], ids=["rozsah", "rozsah_pomlcka", "okrem", "s_vynimkou", "prvy_den", "posledny_den", "dopoludnie", "pracovny_pokoj",
        "sviatky", "polovica_tyzdna", "predmet_v_nedelu"])
def test_siet_zrusenie_casti_bez_terminu(cfg, subject, tail):
    """[FC3 sieť] Zrušenie so slovom o časti výluky a bez určeného termínu je čiastočné (``partial``, bez termínu –
    zápis ho odmietne); nikdy nezruší celú výluku."""
    items = _x(subject, _P3 + tail)
    (ev,) = _of(items, "vyluka_zrusenie")
    assert ev.data.get("partial") is True and not ev.data.get("start") and not ev.data.get("end"), _show(items)


@pytest.mark.parametrize("subject,body", [
    (_S3, _P3 + " sa ruší."),
    (_S3, _P3 + " sa ruší. V sobotu a v nedeľu bude NAD."),
    ("Zrušenie víkendovej výluky – ROV č. 2501/2026", "Výluka Ružomberok – Liptovský Mikuláš sa ruší."),
    ("Zrušenie výluky – ROV č. 2501/2026", "Výluka podľa ROV č. 2501/2026 sa ruší v celom rozsahu okrem úseku "
     "Ružomberok – Liptovský Mikuláš."),
], ids=["cela", "nad_v_sobotu", "vikendova", "okrem_useku"])
def test_kontrola_cele_zrusenie_bez_slov_o_casti(cfg, subject, body):
    """[FC3 sieť, kontrola] Zrušenie celej výluky ostáva celým (náhradná doprava, prívlastok „víkendová“ či výnimka
    úseku čiastočným zrušením nie sú)."""
    items = _x(subject, body)
    assert all(not i.data.get("partial") for i in _of(items, "vyluka_zrusenie")), _show(items)


# ====================================================================== rušený termín z časti, ktorá sa ruší
_S4 = "Zrušenie časti výluky – ROV č. 3206/2026"
_P4 = "Výluka Trnava – Leopoldov podľa ROV č. 3206/2026 dňa 10.12.2026"
_S4b = "Zrušenie výluky – ROV č. 2601/2026"
_P4b = "Výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2601/2026"


@pytest.mark.parametrize("subject,body,sender,want", [
    (_S4, _P4 + " sa nekoná dopoludnia od 08:00 do 10:00, len popoludní od 10:00 do 14:00.", TT,
     [("10.12. 08:00", "10.12. 10:00")]),
    (_S4b, _P4b + " v dňoch 23.11. – 27.11.2026 sa ruší okrem stredy 25.11.2026.", ZA,
     [("23.11. 00:00", "25.11. 00:00"), ("26.11. 00:00", "28.11. 00:00")]),
    (_S4b, _P4b + " v dňoch 23.11. – 27.11.2026 sa ruší, ponechané sú len dni 24. a 25.11.2026.", ZA,
     [("23.11. 00:00", "24.11. 00:00"), ("26.11. 00:00", "28.11. 00:00")]),
    (_S4b, _P4b + " v dňoch 23.11. – 27.11.2026 sa ruší v čase od 12:00 do 14:00 hod., od 08:00 do 12:00 hod. ostáva.",
     ZA, [(f"{d}.11. 12:00", f"{d}.11. 14:00") for d in (23, 24, 25, 26, 27)]),
    (_S4b, _P4b + " v dňoch 23.11. – 27.11.2026 sa ruší v čase od 12:00 do 14:00 hod.", ZA,
     [(f"{d}.11. 12:00", f"{d}.11. 14:00") for d in (23, 24, 25, 26, 27)]),
    # kontrola: celé obdobie s denným oknom (opis výluky) – jeden rušený termín
    (_S4b, "Výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2601/2026, plánovaná v dňoch 23.11. – 27.11.2026 denne "
     "08:00 – 14:00, sa ruší.", ZA, [("23.11. 08:00", "27.11. 14:00")]),
], ids=["len_popoludni", "okrem_stredy_datum", "ponechane_su", "denne_okno_ostava", "denne_okno_bez_ostava",
        "kontrola_opis_obdobia"])
def test_ruseny_termin_z_casti_ktora_sa_rusi(cfg, subject, body, sender, want):
    """[FC4] Rušený termín je len čas, ktorý sa ruší – nie „…, len popoludní …“, nie deň za „okrem stredy“, nie
    „ponechané dni“; denné okno v období sa ruší v každom dni (súvislý čas by zrušil aj 08:00 – 12:00 a noci)."""
    assert _cancels(_x(subject, body, RD if sender == TT else R, sender)) == want


# ====================================================================== sieť proti strate termínu
_B = ("Úplná výluka Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 14:00 hod. a výluka Leopoldov – Piešťany dňa "
      "11.12.2026 od 08:00 do 14:00 hod. (ROV č. 3990/2026). ")


@pytest.mark.parametrize("extra", [
    "Odpoveď očakávame do 07.12.2026 12:00 hod.",
    "V sobotu 12.12. a v nedeľu 13.12.2026 sa nepracuje.",
    "Stanovisko dopravcov prosíme zaslať do 04.12.2026.",
    "Súhlas s výlukou potvrďte najneskôr 03.12.2026.",
    "Zmena platí od 01.12.2026.",
    "Prosíme o potvrdenie prijatia do 02.12.2026 10:00 hod.",
], ids=["odpoved", "nepracuje", "stanovisko", "suhlas", "zmena_plati", "potvrdenie_prijatia"])
def test_siet_administrativne_lehoty_nie_su_terminom(cfg, extra):
    """[sieť, falošný poplach] Lehoty a administratíva nie sú termínom výluky – žiadna udalosť siete (s dvomi
    úsekmi by išlo o poistku pre celý vozový park a upozornenia DÔLEŽITÉ)."""
    items = _x("ROV č. 3990/2026", _B + extra, RD, TT)
    assert not _net(items), _show(items)
    assert _closed(items) == [(("Leopoldov", "Piešťany"), "11.12. 08:00", "11.12. 14:00"),
                              (TL, "10.12. 08:00", "10.12. 14:00")], _show(items)


def test_siet_termin_bez_useku_na_kazdom_useku(cfg):
    """[sieť] Termín bez úseku v e-maile s dvomi úsekmi – udalosť na každom z nich (nie bez miesta)."""
    items = _x("ROV č. 3990/2026", _B + "Výluka bude aj 15.12.2026, čas upresníme.", RD, TT)
    nets = _net(items)
    assert sorted(_sec(n) for n in nets) == [("Leopoldov", "Piešťany"), TL], _show(items)
    assert all(_term(n) == ("15.12. 00:00", "16.12. 00:00") for n in nets)


@pytest.mark.parametrize("body,want", [
    ("Úplná výluka trate Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 10:00 hod. a následne opäť od 13:00 do "
     "16:00 hod.", ("10.12. 13:00", "10.12. 16:00")),
    ("Výluka Trnava – Leopoldov dňa 10.12.2026 (ROV č. 3807/2026) sa mení z 08:00 – 14:00 hod. na 06:00 – 12:00 hod. "
     "ako aj 13:00 – 16:00 hod.", ("10.12. 13:00", "10.12. 16:00")),
], ids=["nasledne_opat", "ako_aj"])
def test_siet_samostatne_okno_sa_nestrati(cfg, body, want):
    """[sieť] Samostatné časové okno, ktoré žiadna udalosť nepokrýva, dá čakajúcu udalosť (deň z tej istej vety)."""
    items = _x("ROV č. 3922/2026", body, RD, TT)
    assert [_term(n) for n in _net(items)] == [want], _show(items)


def test_siet_kontrola_kontakt_a_stary_den(cfg):
    """[sieť, kontrola] Okno kontaktu ani deň výluky, ktorá sa mení („… dňa 10.12. … sa mení z 10.12. 08:00 – 14:00 na
    11.12. …“), udalosť siete nedajú."""
    items = _x("ROV č. 3922/2026", "Úplná výluka trate Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 14:00 hod. "
               "Kontakt: dispečer 7:00 – 15:30.", RD, TT)
    assert not _net(items), _show(items)
    items = _x("Zmena ROV č. 3807/2026", "Výluka Trnava – Leopoldov dňa 10.12.2026 (ROV č. 3807/2026) sa mení z "
               "10.12.2026 08:00 – 14:00 na 11.12.2026 06:00 – 16:00.", RD, TT)
    assert not _net(items) and _closed(items) == [(TL, "11.12. 06:00", "11.12. 16:00")], _show(items)


@pytest.mark.parametrize("body", [
    "Dňa 10.12.2026 od 08:00 do 14:00 hod. nebude úsek Trnava – Leopoldov prejazdný.",
    "Úsek Trnava – Leopoldov nebude dňa 10.12.2026 od 08:00 do 14:00 hod. prejazdný.",
    "Trať v úseku Trnava – Leopoldov bude dňa 10.12.2026 od 08:00 do 14:00 hod. mimo prevádzky.",
], ids=["nebude_usek_prejazdny", "usek_nebude_prejazdny", "mimo_prevadzky"])
def test_nebude_prejazdny_je_uplna_vyluka(cfg, body):
    """[sieť, „iné“] „nebude úsek … prejazdný“, „… mimo prevádzky“ je úplná výluka úseku (nie „iné“)."""
    items = _x("Oznámenie", body, RD, TT)
    news = _of(items, "vyluka_nova")
    assert [(_sec(i), _term(i), i.data.get("kind")) for i in news] == \
        [(TL, ("10.12. 08:00", "10.12. 14:00"), "uplna")], _show(items)


def test_kontrola_priecestie_nie_je_vyluka(cfg):
    """[„iné“, kontrola] Neprejazdné priecestie (cestná premávka) výlukou úseku nie je."""
    items = _x("Oznámenie", "Dňa 10.12.2026 od 08:00 do 14:00 hod. nebude priecestie v km 12,345 v obci Zavar "
               "prejazdné pre cestné vozidlá.", RD, TT)
    assert not _of(items, "vyluka_nova"), _show(items)


# ====================================================================== číslo novej výluky, dôvod potvrdenia
def test_cislo_novej_vyluky_z_vlastnej_vety(cfg):
    """[FC0 nízka] Predmet s dvomi číslami: nová výluka do odvolania má číslo svojej vety (2817), nie číslo rušenej
    výluky (neskoršie zrušenie 2816 ju nezruší)."""
    items = _x("ROV č. 2816/2026 a ROV č. 2817/2026", "výluka podľa ROV č. 2816/2026 v úseku Kraľovany – Ružomberok "
               "dňa 24.11.2026 sa odvoláva. Od 30.11.2026 08:00 hod. do odvolania platí úplná výluka podľa ROV č. "
               "2817/2026 v úseku Kraľovany – Ružomberok.")
    (new,) = _of(items, "vyluka_nova")
    (can,) = _of(items, "vyluka_zrusenie")
    assert (new.data.get("external_ref"), can.data.get("external_ref")) == ("2817/2026", "2816/2026"), _show(items)


def test_kontrola_cislo_z_predmetu_s_jednym_cislom(cfg):
    """[FC0 nízka, kontrola] Predmet s jedným číslom: číslo výluky je z predmetu (ako doteraz)."""
    items = _x("ROV č. 2252/2026", "Úplná výluka oboch traťových koľají v úseku Leopoldov – Trnava dňa 25.11.2026 od "
               "08:00 do 12:00 hod. (ROV č. 2250/2026).")
    (new,) = _of(items, "vyluka_nova")
    assert new.data.get("external_ref") == "2252/2026", _show(items)


def test_dovod_potvrdenia_s_desatinnou_ciarkou(cfg):
    """[FE2] Dôvod potvrdenia „istota 0,50 je pod prahom 0,80“ – desatinná čiarka (text je vo fronte)."""
    from datetime import datetime as dt

    from vlaky.posta.validacia import ValidationResult, confirmation_needed

    needed, reasons = confirmation_needed("vyluka_nova", {}, 0.5, "", ValidationResult(data={}),
                                          dt.now(timezone.utc))
    assert needed and reasons[0].startswith("istota 0,50 je pod prahom 0,"), reasons
