"""Štvrtá revízia – pravidlá e-mailov ŽSR (oblasť M4, opravy po kontrole): regresné testy extrakcie.

- druh obmedzenia: veta o pomalej jazde pred vetou o výluke na tom istom úseku výluku nezmení na pomalú jazdu; obe
  koľaje vymenované po jednej („1. TK a 2. TK“) sú úplná výluka; „úplnej rekonštrukcie“, „Po 2. TK vlaky
  nepremávajú“, prerušená cestná premávka z výluky koľaje úplnú výluku neurobia;
- zoznamy: nesúvisiaci odsek s obdobím (platnosť rozkazu, náhradná doprava, porada, pripomienky, GVD) nie je ďalšia
  výluka; číslo položky „1. 01.12.2026“ nie je deň; „od 20.11.2026 14:00 do 30.11.2026“ je obdobie do konca dňa;
- zámer: ROV s prílohou .ics či s predmetom bulletinu (tlačovej správy) sa nezahodí; nová výluka so zmienkou
  o inej, zrušenej výluke nie je zrušenie; „nebude z dôvodu … realizovaná“ je zrušenie; „sa v celom rozsahu ruší“
  ruší celú výluku; presun jedného dňa („dňa 25.11. sa ruší a presúva sa na 28.11.“) dá nový termín a zrušenie dňa;
- čiastočné obnovenie: kladné formulácie o nákladných vlakoch („obmedzenia … nie sú“, „pokračujú v jazde“) sú koniec
  mimoriadnosti, „musia čakať do 15:00“ a „nákladná doprava bude obnovená o 15:00“ obmedzenie do 15:00;
- miesto: iný (už) prejazdný úsek mimoriadnosť nekončí; dve mimoriadnosti v jednom e-maile, dve stanice vo vete
  o výluke; aktualizovaný ROV v prílohe odpovede nie je citácia; avízo dvoch vlakov s číslom v predmete.

Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import timezone

import pytest

from test_revizia3_terminy import iso, rules, utc

UTC = timezone.utc


def _closures(items) -> list:
    return [i for i in items if i.kind in ("vyluka_nova", "vyluka_zmena", "pomala_jazda", "mimoriadnost")]


def _dump(items) -> list:
    return [(i.kind, i.data.get("kind"), i.data.get("from_station"), i.data.get("to_station"), i.data.get("start"),
             i.data.get("end"), i.data.get("external_ref")) for i in items]


def _extract(subject: str, body: str, received=utc(2026, 11, 16, 9), attachments=()):
    from vlaky.posta.pravidla import RuleExtractor
    from vlaky.posta.udalosti import MailDocument

    return RuleExtractor().extract(MailDocument(subject=subject, body=body, received_at=received,
                                                attachments=list(attachments)))


# ====================================================================== druh obmedzenia (issues 1, 9, 10)
@pytest.mark.parametrize("subject,body", [
    ("Výluka Kysak – Košice", "Pomalá jazda 50 km/h v úseku Kysak – Košice platí do 19.11.2026. Následne dňa 20.11.2026 "
                              "od 08:00 do 14:00 hod. úplná výluka v úseku Kysak – Košice.\n"),
    ("Výluka Kysak – Košice", "V úseku Kysak – Košice platí od 15.11.2026 pomalá jazda 50 km/h. Dňa 20.11.2026 od 08:00 "
                              "do 14:00 hod. bude v tomto úseku výluka podľa ROV č. 1178/2026.\n"),
    ("ROV č. 1184/2026 Kysak – Košice", "Pomalá jazda 50 km/h v úseku Kysak – Košice platí od 15.11.2026. Výluka "
                                        "v úseku Kysak – Košice dňa 20.11.2026 od 08:00 do 14:00 hod.\n"),
    ("Výluka a pomalá jazda", "Pomalá jazda: Kysak – Košice, 50 km/h, od 15.11.2026 do 30.11.2026\nVýluka: Kysak – Košice,"
                              " 20.11.2026 od 08:00 do 14:00 (ROV č. 1185/2026)\n"),
    ("Pomalá jazda Kysak – Košice", "Od 15.11.2026 platí v úseku Kysak – Košice pomalá jazda 40 km/h. Dňa 20.11.2026 od "
                                    "08:00 do 14:00 bude úsek Kysak – Košice vylúčený (ROV č. 1183/2026).\n"),
])
def test_i1_pomala_jazda_pred_vetou_o_vyluke_ostava_vylukou(cfg, subject, body):
    """[1] Veta o pomalej jazde na tom istom úseku PRED vetou o výluke: výluka s vlastným termínom ostáva úplnou
    výlukou (nie pomalou jazdou – vlak by dostal VYPRAVIŤ do uzavretého úseku)."""
    items = rules(body, subject=subject, received=utc(2026, 11, 16, 9))
    hit = [i for i in items if i.data.get("start") == iso(2026, 11, 20, 7)]
    assert hit and all(i.kind == "vyluka_nova" and i.data["kind"] == "uplna" for i in hit), _dump(items)


def test_i1_pomala_jazda_na_susednom_useku_ostava_pomalou_jazdou(cfg):
    """[1] Kontrola: termín vo vete o výluke inom úseku nerobí z pomalej jazdy na susednom úseku výluku."""
    items = rules("Rozkaz o výluke č. 1160/2026: výluka v úseku Kysak – Košice 20.11.2026 od 8:00 do 14:00. Počas výluky"
                  " platí v úseku Kysak – Kostoľany nad Hornádom pomalá jazda 30 km/h.\n", subject="ROV č. 1160/2026",
                  received=utc(2026, 11, 16, 9))
    kinds = {(i.kind, i.data.get("from_station"), i.data.get("to_station")) for i in items}
    assert ("vyluka_nova", "Kysak", "Košice") in kinds and ("pomala_jazda", "Kysak", "Kostoľany nad Hornádom") in kinds


@pytest.mark.parametrize("words", ["Výluka 1. TK a 2. TK", "Výluka 1. TK aj 2. TK", "Výluka 1.TK a 2.TK",
                                   "Výluka koľaje č. 1 a koľaje č. 2", "Výluka TK č. 1 a TK č. 2"])
def test_i9_obe_kolaje_vymenovane_po_jednej_su_uplna_vyluka(cfg, words):
    """[9] Obe traťové koľaje vymenované po jednej sú úplná výluka (obe koľaje), nie výluka 1. koľaje."""
    (it,) = rules(words + " v úseku Kraľovany – Ružomberok dňa 18.11.2026 od 08:00 do 14:00 hod.\n", subject="ROV")
    assert (it.data["kind"], it.data["track"]) == ("uplna", "obe"), _dump([it])


def test_i9_stanicne_kolaje_c1_a_c2_nie_su_uplna_vyluka_stanice(cfg):
    """[9] Kontrola: staničné koľaje č. 1 a 2 – stanica má ďalšie koľaje, nie je to úplná výluka."""
    (it,) = rules("Výluka staničnej koľaje č. 1 a koľaje č. 2 v žst. Ružomberok dňa 18.11.2026 od 08:00 do 14:00 hod.\n",
                  subject="ROV")
    assert it.data["kind"] == "kolaj", _dump([it])


@pytest.mark.parametrize("tail", [
    " z dôvodu úplnej rekonštrukcie výhybky č. 5, doprava po 2. TK.",
    " Po 2. TK vlaky nepremávajú, doprava vedená po 1. TK.",
    " Cestná premávka na priecestí bude prerušená, vlaky po 2. TK.",
    " – rekonštrukcia priecestia v km 245,3, úplná uzávierka cesty II/583.",
    " Priecestie v km 250,1 bude úplne uzavreté pre cestnú dopravu. Vlaky po 2. TK.",
])
def test_i10_nahodne_slova_uplna_nezmenia_vyluku_kolaje(cfg, tail):
    """[10] „úplnej rekonštrukcie“, „Po 2. TK vlaky nepremávajú“, cestná premávka či uzávierka cesty z výluky 1. TK
    úplnú výluku neurobia (vlaky idú po druhej koľaji)."""
    (it,) = rules("Výluka 1. TK v úseku Kraľovany – Ružomberok dňa 18.11.2026 od 08:00 do 14:00 hod." + tail + "\n",
                  subject="ROV")
    assert it.data["kind"] == "kolaj", _dump([it])


@pytest.mark.parametrize("body", [
    "Výluka 2. TK v úseku Kraľovany – Ružomberok dňa 18.11.2026 od 08:00 do 14:00 hod. Doprava v úseku bude prerušená.\n",
    "Úplná výluka v úseku Kraľovany – Ružomberok dňa 18.11.2026 od 08:00 do 14:00 hod.\n",
    "V úseku Kraľovany – Ružomberok bude dňa 18.11.2026 od 08:00 do 14:00 hod. trať úplne uzavretá.\n",
])
def test_i10_vyslovne_uplna_vyluka_ostava_uplnou(cfg, body):
    """[10] Kontrola: výslovne úplná výluka či prerušená doprava v úseku ostáva úplnou výlukou."""
    (it,) = rules(body, subject="ROV")
    assert it.data["kind"] == "uplna", _dump([it])


# ====================================================================== zoznamy a termíny (issues 3, 4)
@pytest.mark.parametrize("tail", [
    "V dňoch 02.12. – 05.12.2026 premávajú vlaky podľa GVD.\n",
    "Rozkaz platí od 20.11.2026 do 31.12.2026.\n",
    "Náhradná autobusová doprava je zabezpečená od 30.11.2026 do 02.12.2026.\n",
    "Koordinačná porada sa uskutoční 25.11.2026 od 9:00 do 11:00 hod.\n",
    "Pripomienky k návrhu výluky zasielajte od 20.11.2026 do 25.11.2026.\n",
    "Žiadosti o výnimku prijímame v termíne od 20.11. do 25.11.2026.\n",
    "Objednávky náhradných trás prijímame od 20.11.2026 do 25.11.2026.\n",
    "Ďalšie výluky plánujeme v termíne od 10.01.2027 do 20.01.2027, budú oznámené samostatným ROV.\n",
])
def test_i3_nesuvisiaci_odsek_s_obdobim_nie_je_dalsia_vyluka(cfg, tail):
    """[3] Odsek s obdobím, ktorý je vedľajšou informáciou (GVD, platnosť rozkazu, náhradná doprava, porada,
    pripomienky, žiadosti, objednávky, ďalšie plánované výluky), nie je ďalšou výlukou úseku (vlak by dostal
    ESKALOVAŤ)."""
    items = rules("Oznamujeme úplnú výluku v úseku Kysak – Košice (ROV č. 1530/2026).\n\nTermín výluky: 01.12.2026 "
                  "od 08:00 do 12:00 hod.\n\n" + tail, subject="ROV č. 1530/2026", received=utc(2026, 11, 16, 9))
    closures = _closures(items)
    assert [(i.data["start"], i.data["end"]) for i in closures] == [(iso(2026, 12, 1, 7), iso(2026, 12, 1, 11))], \
        _dump(items)


def test_i3_odsek_s_dalsim_terminom_vyluky_ostava(cfg):
    """[3] Kontrola: ďalší termín výluky v samostatnom odseku je ďalšia udalosť; náhradná doprava ako jediný termín
    e-mailu sa použije (prísnejší prípad)."""
    items = rules("Oznamujeme úplnú výluku v úseku Kysak – Košice (ROV č. 1531/2026).\n\nTermín výluky: 01.12.2026 "
                  "od 08:00 do 12:00 hod.\n\nDruhý termín: 03.12.2026 od 08:00 do 12:00 hod.\n",
                  subject="ROV č. 1531/2026", received=utc(2026, 11, 16, 9))
    assert sorted(i.data["start"] for i in _closures(items)) == [iso(2026, 12, 1, 7), iso(2026, 12, 3, 7)]
    (only,) = _closures(rules("Počas výluky v úseku Kysak – Košice je od 30.11.2026 do 02.12.2026 zabezpečená náhradná "
                              "autobusová doprava.\n", subject="Výluka", received=utc(2026, 11, 16, 9)))
    assert (only.data["from_station"], only.data["start"]) == ("Kysak", iso(2026, 11, 29, 23))
    # výluka s poznámkou o náhradnej doprave v tej istej vete je výluka (nie vedľajšia informácia)
    items = rules("Výluka v úseku Kysak – Košice dňa 01.12.2026 od 08:00 do 12:00 hod. (náhradná autobusová doprava "
                  "zabezpečená).\n\nVýluka v úseku Kysak – Košice dňa 03.12.2026 od 08:00 do 12:00 hod.\n",
                  subject="ROV č. 1532/2026", received=utc(2026, 11, 16, 9))
    assert sorted(i.data["start"] for i in _closures(items)) == [iso(2026, 12, 1, 7), iso(2026, 12, 3, 7)]


def test_i4_cislo_polozky_pred_datumom_nie_je_den(cfg):
    """[4] „1. 01.12.2026 od 08:00 …“ – číslo položky zoznamu nie je deň (nie 02.02.2027), každá položka je termín."""
    items = rules("Podľa ROV č. 1500/2026 budú v úseku Kysak – Košice vykonané výluky oboch traťových koľají "
                  "v termínoch:\n1. 01.12.2026 od 08:00 do 12:00 hod.\n2. 02.12.2026 od 08:00 do 12:00 hod.\n"
                  "3. 04.12.2026 od 22:00 do 05.12.2026 04:00 hod.\n", subject="ROV č. 1500/2026",
                  received=utc(2026, 11, 16, 9))
    assert sorted((i.data["start"], i.data["end"]) for i in _closures(items)) == [
        (iso(2026, 12, 1, 7), iso(2026, 12, 1, 11)), (iso(2026, 12, 2, 7), iso(2026, 12, 2, 11)),
        (iso(2026, 12, 4, 21), iso(2026, 12, 5, 3))], _dump(items)


def test_obdobie_od_datumu_s_casom_do_datumu_bez_casu(cfg):
    """„Od 20.11.2026 14:00 do 30.11.2026“ je obdobie do konca 30.11. (prísnejší prípad) – pomalá jazda za výlukou je
    samostatná udalosť, nie nepriradený čas."""
    items = rules("Od 20.11.2026 14:00 do 30.11.2026 platí v úseku Kysak – Košice pomalá jazda 50 km/h.\nPredtým dňa "
                  "20.11.2026 od 08:00 do 14:00 hod. bude v úseku Kysak – Košice výluka podľa ROV č. 1178/2026.\n",
                  subject="Výluka a pomalá jazda Kysak – Košice", received=utc(2026, 11, 16, 9))
    got = {(i.kind, i.data["start"], i.data["end"]) for i in items}
    assert ("vyluka_nova", iso(2026, 11, 20, 7), iso(2026, 11, 20, 13)) in got, _dump(items)
    assert ("pomala_jazda", iso(2026, 11, 20, 13), iso(2026, 11, 30, 23)) in got, _dump(items)


# ====================================================================== zámer e-mailu (issues 5, 6, 15, QC4)
ROV_LT = ("Úplná výluka oboch traťových koľají v úseku Leopoldov – Trnava dňa 25.11.2026 od 08:00 do 12:00 hod. "
          "(ROV č. 2250/2026).\n")
ICS = "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nSUMMARY:Výluka\nDTSTART:20261125T070000Z\nEND:VEVENT\nEND:VCALENDAR\n"


@pytest.mark.parametrize("subject,atts", [("ROV č. 2250/2026 – výluka Leopoldov – Trnava", [("vyluka.ics", ICS)]),
                                          ("Mimoriadny bulletin – výluka Leopoldov – Trnava", []),
                                          ("Tlačová správa: výluka Leopoldov – Trnava", [])])
def test_i5_rov_s_ics_ci_predmetom_bulletinu_sa_nezahodi(cfg, subject, atts):
    """[5] Skutočný ROV s kalendárovou prílohou (bez pozvánky) či s predmetom bulletinu / tlačovej správy je výluka
    (s poznámkou „overte“) – nie „iné“ (stratená výluka = falošná ZELENÁ)."""
    items = _extract(subject, ROV_LT, attachments=atts)
    (it,) = _closures(items)
    assert (it.kind, it.data["from_station"], it.data["start"]) == ("vyluka_nova", "Leopoldov", iso(2026, 11, 25, 7))
    assert any("prevádzkové oznámenie" in n for n in it.notes), it.notes


def test_i5_pozvanka_a_newsletter_bez_cisla_ostavaju_ine(cfg):
    """[5] Kontrola: pozvánka (METHOD:REQUEST) a newsletter bez čísla ROV ostávajú „iné“."""
    (inv,) = _extract("Koordinácia výluk", "Porada k výluke ROV č. 2250/2026 v úseku Leopoldov – Trnava dňa 25.11.2026 o "
                      "9:00.\n", attachments=[("pozvanka.ics", "BEGIN:VCALENDAR\nMETHOD:REQUEST\nATTENDEE:mailto:"
                                                               "a@dopravca.example\nEND:VCALENDAR\n")])
    assert inv.kind == "ine"
    (nl,) = _extract("Newsletter ŽSR – november 2026", "Vážení partneri, v novembri prebehne 120 výluk, napríklad výluka "
                     "v úseku Kysak – Košice dňa 20.11.2026 od 8:00 do 14:00.\n")
    assert nl.kind == "ine"


@pytest.mark.parametrize("tail", [
    " ako náhrada za výluku, ktorá bola zrušená.",
    ", pretože pôvodná výluka z 20.11.2026 bola zrušená.",
    ", výluka 2. TK z 20.11.2026 odpadá.",
    " – po zrušení výluky podľa ROV č. 2240/2026.",
    " je náhradou za zrušenú výluku ROV č. 2240/2026.",
])
def test_i6_nova_vyluka_so_zmienkou_o_zrusenej_ostava_novou(cfg, tail):
    """[6] Nový ROV, ktorého veta spomína inú, zrušenú výluku, je nová výluka – nie zrušenie (vlak by dostal
    VYPRAVIŤ do výluky)."""
    items = _extract("ROV č. 2251/2026", "Úplná výluka oboch traťových koľají v úseku Leopoldov – Trnava dňa 26.11.2026 "
                     "od 08:00 do 12:00 hod. (ROV č. 2251/2026)" + tail + "\n")
    assert any(i.kind == "vyluka_nova" and i.data["start"] == iso(2026, 11, 26, 7) for i in items), _dump(items)
    assert not any(i.kind == "vyluka_zrusenie" and i.data.get("external_ref") == "2251/2026" for i in items), \
        _dump(items)


def test_i6_pomala_jazda_ako_nahrada_za_zrusenu_vyluku(cfg):
    """[6] „Pomalá jazda … – náhrada za zrušenú výluku“ je pomalá jazda, nie zrušenie výluky."""
    items = _extract("Pomalá jazda Kysak – Košice", "Pomalá jazda 50 km/h v úseku Kysak – Košice od 20.11.2026 do "
                     "30.11.2026 – náhrada za zrušenú výluku.\n")
    assert [i.kind for i in items] == ["pomala_jazda"], _dump(items)


@pytest.mark.parametrize("body", [
    "Výluka podľa ROV č. 2250/2026 nebude z dôvodu nepriaznivého počasia realizovaná.\n",
    "Výluka podľa ROV č. 2250/2026 sa z technických dôvodov nebude konať.\n",
])
def test_i15_nebude_z_dovodu_realizovana_je_zrusenie(cfg, body):
    """[15] „… nebude z dôvodu … realizovaná“ je zrušenie výluky (nie predpokladaná úplná výluka bez termínu)."""
    items = _extract("ROV č. 2250/2026", body)
    assert [(i.kind, i.data.get("external_ref")) for i in items] == [("vyluka_zrusenie", "2250/2026")], _dump(items)


def test_zrusenie_v_celom_rozsahu_rusi_celu_vyluku(cfg):
    """„Výluka podľa ROV … platná od 23.11.2026 sa v celom rozsahu ruší“ – ruší sa celá výluka (dátum ju len
    označuje), nie len 23.11."""
    (it,) = _extract("ROV č. 1315/2026 Ružomberok – Liptovský Mikuláš", "Výluka podľa ROV č. 1315/2026 platná od "
                     "23.11.2026 sa v celom rozsahu ruší.\n")
    assert (it.kind, it.data["external_ref"], it.data.get("start")) == ("vyluka_zrusenie", "1315/2026", None)


@pytest.mark.parametrize("body,cancelled", [
    ("Výluka podľa ROV č. 1315/2026 dňa 25.11.2026 sa ruší a presúva sa na 28.11.2026 od 08:00 do 14:00 hod.\n",
     (iso(2026, 11, 24, 23), iso(2026, 11, 25, 23))),
    ("Výluka podľa ROV č. 1315/2026 dňa 25.11.2026 sa nekoná. Náhradný termín: 28.11.2026 od 08:00 do 14:00 hod.\n",
     (iso(2026, 11, 24, 23), iso(2026, 11, 25, 23))),
    ("Výluka podľa ROV č. 1315/2026 dňa 25.11.2026 od 08:00 do 14:00 hod. sa ruší a presúva sa na 28.11.2026 od 08:00 "
     "do 14:00 hod.\n", (iso(2026, 11, 25, 7), iso(2026, 11, 25, 13))),
])
def test_presun_dna_je_novy_termin_a_zrusenie_dna(cfg, body, cancelled):
    """[2] Presun jedného dňa („dňa 25.11. sa ruší a presúva sa na 28.11.“, „… sa nekoná. Náhradný termín: …“) je
    nový termín ROV a zrušenie pôvodného dňa – rušený deň nie je novou výlukou a nový termín nie je zmenou celej
    (viacdennej) výluky."""
    items = _extract("ROV č. 1315/2026 Ružomberok – Liptovský Mikuláš", body)
    assert sorted((i.kind, i.data.get("start"), i.data.get("end"), i.data.get("external_ref")) for i in items) == [
        ("vyluka_nova", iso(2026, 11, 28, 7), iso(2026, 11, 28, 13), "1315/2026"),
        ("vyluka_zrusenie", *cancelled, "1315/2026")], _dump(items)


def test_skratenie_zmenou_s_dnami_ktore_sa_nekonaju(cfg):
    """Kontrola: „výluka bude od 07.12. do 09.12. denne …, v dňoch 10. a 11.12. sa nekoná“ (zmena ROV) – nový termín
    07. – 09.12. platí (časť vety o dňoch, ktoré sa nekonajú, ho nezruší ani nezmení na nepriradený dátum)."""
    (it,) = _extract("Zmena ROV č. 2270/2026 Nové Mesto nad Váhom – Piešťany", "Výluka 1. TK v úseku Nové Mesto nad Váhom"
                     " – Piešťany podľa ROV č. 2270/2026 sa skracuje: výluka bude od 07.12.2026 do 09.12.2026 denne od "
                     "08:00 do 15:00 hod., v dňoch 10. a 11.12.2026 sa nekoná.\n")
    assert (it.kind, it.data["start"], it.data["end"], it.data["daily_from"]) == (
        "vyluka_zmena", iso(2026, 12, 7, 7), iso(2026, 12, 9, 14), "08:00"), _dump([it])
    assert not any("nepodarilo priradiť" in n for n in it.notes), it.notes


def test_zruseny_termin_inej_vyluky_nie_je_nova_vyluka(cfg):
    """Termín vety, ktorá výluku ruší („… dňa 25.11. od 08:00 do 12:00 sa nekoná“), nie je novou výlukou e-mailu –
    je to rušený termín (zrušenie je samostatná udalosť)."""
    items = _extract("ROV", "Výluka podľa ROV č. 2241/2026 v úseku Leopoldov – Trnava dňa 25.11.2026 od 08:00 do 12:00 "
                     "hod. sa nekoná. Nová výluka podľa ROV č. 2250/2026 v úseku Leopoldov – Trnava dňa 26.11.2026 od "
                     "08:00 do 12:00 hod.\n")
    assert sorted((i.kind, i.data.get("start"), i.data.get("external_ref")) for i in items) == [
        ("vyluka_nova", iso(2026, 11, 26, 7), "2250/2026"), ("vyluka_zrusenie", iso(2026, 11, 25, 7), "2241/2026")], \
        _dump(items)


# ====================================================================== čiastočné obnovenie (issues 7, 8)
P0 = "Mimoriadnosť v ŽST Kraľovany: prevádzka je obnovená o 09:20"


@pytest.mark.parametrize("tail", [", obmedzenia pre nákladné vlaky nie sú.",
                                  ". Nákladné vlaky čakajúce v okolitých staniciach budú vypravené postupne.",
                                  ". Odstavené nákladné vlaky pokračujú v jazde podľa pokynov dispečera.",
                                  ", zastavené nákladné vlaky už pokračujú v jazde.",
                                  ". Odklonené nákladné vlaky sa vracajú na pôvodnú trasu."])
def test_i7_kladne_formulacie_o_nakladnych_vlakoch_su_koniec(cfg, tail):
    """[7] Kladná formulácia o nákladných vlakoch po obnovení prevádzky je oznámenie o skončení („iné“) – nie
    mimoriadnosť do odvolania (všetky vlaky by dostali ESKALOVAŤ)."""
    items = rules(P0 + tail + "\n", subject="Mimoriadnosť Kraľovany", received=utc(2026, 11, 2, 8, 25))
    assert [i.kind for i in items] == ["ine"], _dump(items)


@pytest.mark.parametrize("tail", [". Nákladné vlaky musia čakať do 15:00.",
                                  " pre osobnú dopravu, nákladná doprava bude obnovená o 15:00."])
def test_i8_nakladne_vlaky_cakaju_do_15_je_obmedzenie(cfg, tail):
    """[8] „Nákladné vlaky musia čakať do 15:00“, „nákladná doprava bude obnovená o 15:00“ – pre dopravcu
    nákladných vlakov mimoriadnosť v stanici trvá do 15:00 (nie oznámenie o skončení)."""
    items = rules(P0 + tail + "\n", subject="Mimoriadnosť Kraľovany", received=utc(2026, 11, 2, 8, 25))
    (it,) = [i for i in items if i.kind == "mimoriadnost"]
    assert (it.data["from_station"], it.data["start"], it.data["end"]) == (
        "Kraľovany", iso(2026, 11, 2, 8, 20), iso(2026, 11, 2, 14)), _dump(items)


# ====================================================================== miesto (issues 11, 12, 13, 14)
@pytest.mark.parametrize("tail", ["Úsek Žilina – Varín je už prejazdný.", "Úsek Žilina – Varín je opäť prejazdný."])
def test_i12_iny_usek_uz_prejazdny_nekonci_mimoriadnost(cfg, tail):
    """[12] Mimoriadnosť v úseku Púchov – Považská Bystrica a veta o inom, už prejazdnom úseku – mimoriadnosť trvá
    (nie oznámenie o skončení, vlak by dostal VYPRAVIŤ)."""
    items = rules("Mimoriadnosť: v úseku Púchov – Považská Bystrica strhnuté trakčné vedenie od 02.11.2026 04:30 do "
                  "02.11.2026 13:00. " + tail + "\n", subject="Mimoriadnosť", received=utc(2026, 11, 2, 4))
    assert [(i.kind, i.data.get("from_station"), i.data.get("to_station")) for i in items] == [
        ("mimoriadnost", "Púchov", "Považská Bystrica")], _dump(items)


def test_i12_ten_isty_usek_prejazdny_je_koniec(cfg):
    """[12] Kontrola: „Úsek Púchov – Považská Bystrica je opäť prejazdný“ pri tom istom úseku je koniec mimoriadnosti."""
    items = rules("Mimoriadnosť v úseku Púchov – Považská Bystrica: úsek Púchov – Považská Bystrica je opäť prejazdný "
                  "od 13:00.\n", subject="Mimoriadnosť Púchov – Považská Bystrica", received=utc(2026, 11, 2, 12))
    assert [i.kind for i in items] == ["ine"], _dump(items)


def test_i13_dve_mimoriadnosti_v_jednom_emaili(cfg):
    """[13] Dve mimoriadnosti v položkách zoznamu, každá na inom mieste – dve udalosti (nie len prvá)."""
    items = rules("1) Mimoriadnosť v ŽST Vrútky od 02.11.2026 04:30 do 02.11.2026 13:00, stanica neprejazdná.\n"
                  "2) Mimoriadnosť v úseku Štrba – Poprad-Tatry od 02.11.2026 06:00 do 02.11.2026 10:00, strhnuté "
                  "trakčné vedenie.\n", subject="Mimoriadnosti", received=utc(2026, 11, 2, 4))
    got = sorted((i.data["from_station"], i.data["to_station"], i.data["start"], i.data["end"]) for i in items
                 if i.kind == "mimoriadnost")
    assert got == [("Vrútky", "Vrútky", iso(2026, 11, 2, 3, 30), iso(2026, 11, 2, 12)),
                   ("Štrba", "Poprad-Tatry", iso(2026, 11, 2, 5), iso(2026, 11, 2, 9))], _dump(items)


def test_i11_dve_stanice_vo_vete_o_vyluke(cfg):
    """[11] Dve stanice vo vete o výluke – obe sú výluky (jedna s termínom, druhá so svojím termínom v zátvorke)."""
    items = rules("Úplná výluka v žst. Štrba a v žst. Poprad-Tatry dňa 24.11.2026 od 08:00 do 14:00 hod.\n",
                  subject="ROV č. 1405/2026", received=utc(2026, 11, 16, 9))
    assert sorted((i.data["from_station"], i.data["to_station"]) for i in items) == [
        ("Poprad-Tatry", "Poprad-Tatry"), ("Štrba", "Štrba")], _dump(items)
    items = rules("Výluky v žst. Štrba (24.11.2026 od 08:00 do 12:00 hod.) a v žst. Poprad-Tatry (24.11.2026 od 12:00 "
                  "do 16:00 hod.).\n", subject="ROV č. 1406/2026", received=utc(2026, 11, 16, 9))
    assert sorted((i.data["from_station"], i.data["start"]) for i in items) == [
        ("Poprad-Tatry", iso(2026, 11, 24, 11)), ("Štrba", iso(2026, 11, 24, 7))], _dump(items)


def test_i11_stanica_kde_vlaky_cakaju_nie_je_vylukou(cfg):
    """[11] Kontrola: stanica, kde vlaky počas výluky čakajú, miestom výluky nie je."""
    items = rules("Výluka v žst. Štrba dňa 24.11.2026 od 08:00 do 14:00 hod., vlaky budú čakať v žst. Poprad-Tatry.\n",
                  subject="ROV č. 1407/2026", received=utc(2026, 11, 16, 9))
    assert [(i.data["from_station"], i.data["to_station"]) for i in items] == [("Štrba", "Štrba")], _dump(items)


def test_i14_aktualizovany_rov_v_prilohe_odpovede_nie_je_citacia(cfg):
    """[14] Odpoveď „… sa presúva na 27.11.2026, aktualizovaný ROV je v prílohe“ – príloha je nový dokument s novými
    termínmi (27. aj 28.11.), nie citácia pôvodného ROV; pôvodný ROV v prílohe citáciou ostáva."""
    att = ("ROV_1270_zmena.txt", "Zmena ROV č. 1270/2026\nÚplná výluka oboch traťových koľají v úseku Žilina – Varín sa "
                                 "presúva na\n27.11.2026 od 08:00 do 14:00 hod. a 28.11.2026 od 08:00 do 14:00 hod.\n")
    items = _extract("RE: ROV č. 1270/2026 – výluka Žilina – Varín", "Dobrý deň,\n\nvýluka podľa ROV č. 1270/2026 sa "
                     "presúva na 27.11.2026, aktualizovaný ROV je v prílohe.\n", attachments=[att])
    assert sorted(i.data["start"] for i in _closures(items)) == [iso(2026, 11, 27, 7), iso(2026, 11, 28, 7)], \
        _dump(items)
    orig = ("ROV_1142.txt", "Rozkaz o výluke č. 1142/2026: úplná výluka oboch traťových koľají v úseku Poprad-Tatry – "
                            "Liptovský Mikuláš 12.11.2026 od 8:00 do 14:00.\n")
    (it,) = _extract("RE: ROV č. 1142/2026", "Úplná výluka oboch traťových koľají v úseku Poprad-Tatry – Liptovský "
                     "Mikuláš podľa ROV č. 1142/2026 sa presúva na 14.11.2026 od 8:00 do 14:00.\nV prílohe pôvodný ROV.\n",
                     received=utc(2026, 11, 3, 7, 55), attachments=[orig])
    assert it.data["start"] == iso(2026, 11, 14, 7)


def test_avizo_dvoch_vlakov_s_cislom_v_predmete(cfg):
    """Avízo dvoch vlakov („Avízo vlakov 47250 a 47252“): každý vlak dostane ETA zo svojho riadku (číslo v predmete
    nie je riadok s ETA)."""
    items = rules("Vlak Nex 47250: predpokladaný príchod do Čadce 03.11.2026 o 15:10 hod.\nVlak Nex 47252: "
                  "predpokladaný príchod do Čadce 03.11.2026 o 18:20 hod.\n", subject="Avízo vlakov 47250 a 47252",
                  received=utc(2026, 11, 2, 8, 55))
    assert sorted((i.data["train_number"], i.data["eta"]) for i in items) == [
        ("47250", iso(2026, 11, 3, 14, 10)), ("47252", iso(2026, 11, 3, 17, 20))]
