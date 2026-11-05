"""6. revízia – strana pravidiel (FA), pokračovanie opravy po kontrole: ďalšie triedy formulácií a sieťky.

Každá oprava s formuláciou, ktorú kontrola ani predchádzajúca oprava nepokryli, a s blízkymi variantmi (kontrolné
prípady sa nesmú zmeniť):

* „… mimo prevádzky“ len pri trati, úseku, koľaji či stanici ako podmete (nie výťah, pokladňa, informačný systém) –
  sieť proti strate obsahu, nie istá úplná výluka; „nebude … prejazdný“ nikdy pri ceste, priecestí či podchode
  (ani pred úsekom či stanicou, ani za desatinnou čiarkou „km 12,345“); „nebude možný prejazd vlakov“ blokuje;
* sieť pre „iné“ e-maily ŽSR: veta s úsekom trate a budúcim termínom s časom bez prevádzkového slova dá čakajúcu
  úplnú výluku (nie NAD, porada, obnovenie, „opäť prejazdný“, upozornenie, minulý termín);
* noci so zopakovaným „na“ („v noci na 26.11. a na 27.11.“), osamelé okno vo vete bez dňa („Druhá časť výluky bude
  od 13:00 do 16:00 hod.“ – deň z predchádzajúcej vety);
* pridanie termínu: „rozširuje (sa) aj na“, „ešte raz“, „k pôvodnému termínu sa pridáva (ešte) …“, „okrem
  pôvodných dní … aj …“ (dátum za „pôvodný“ a slovami pridania je novým termínom), viac termínov bez zámeru –
  doplnenie; „bude trvať o dva dni dlhšie“ – predĺženie; pôvodný termín v časti vety („… dňa 10.12. ostáva, …“,
  „okrem 10.12. … taktiež …“) ani pôvodná noc presunu („z noci na 26.11. na noc na 03.12.“) nie sú termínom siete;
* zrušenie časti výluky bez termínu: „vo všetky dni okrem nedele“, „čiastočne“, „keď nie je pracovný deň“, „v nočných
  hodinách“, „… sa ruší. Výluka v ostatných dňoch platí.“ – nikdy nezruší celú výluku; výnimka so zoznamom dní
  v týždni („okrem utorka 24.11. a štvrtka 26.11.“) – rušené sú ostatné dni;
* predmet so zoznamom čísel („ROV č. 2816/2026 a 2817/2026“) – nová výluka má číslo svojej vety, nikdy číslo výluky,
  ktorú e-mail ruší.

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
TL = ("Trnava", "Leopoldov")
BLOCKING = ("vyluka_nova", "vyluka_zmena", "mimoriadnost", "pomala_jazda")


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


def _cancels(items) -> list:
    return sorted(_term(i) for i in _of(items, "vyluka_zrusenie"))


def _periods(text: str) -> list:
    from vlaky.posta import datumy
    from vlaky.posta.text import fold

    f = fold(text)
    return [(f"{p.start:%d.%m. %H:%M}", f"{p.end:%d.%m. %H:%M}" if p.end else None)
            for p in datumy.periods(f, datumy.scan(f, date(2026, 11, 18)))]


# ====================================================================== „mimo prevádzky“, „nebude … prejazdný“
@pytest.mark.parametrize("body", [
    "Dňa 10.12.2026 od 08:00 do 14:00 hod. bude trať Trnava – Leopoldov mimo prevádzky.",
    "Trať v úseku Trnava – Leopoldov bude dňa 10.12.2026 od 08:00 do 14:00 hod. mimo prevádzky.",
], ids=["bude_trat", "trat_v_useku"])
def test_mimo_prevadzky_trate_je_cakajuca_vyluka(cfg, body):
    """„… trať … mimo prevádzky“ – čakajúca úplná výluka úseku s termínom (sieť proti strate obsahu, istota nízka)."""
    items = _x("Oznámenie", body, RD, TT)
    news = _of(items, "vyluka_nova")
    assert [(_sec(i), _term(i), i.data.get("kind")) for i in news] == \
        [(TL, ("10.12. 08:00", "10.12. 14:00"), "uplna")], _show(items)
    assert news[0].confidence < 0.8 and any("overte" in n for n in news[0].notes), _show(items)


@pytest.mark.parametrize("body", [
    "Výťah v žst. Košice je od 20.11.2026 mimo prevádzky, cestujúci nesmú používať nástupište 3.",
    "Pokladňa v stanici Trnava bude dňa 10.12.2026 mimo prevádzky.",
    "Informačný systém stanice Trnava bude dňa 10.12.2026 od 08:00 do 14:00 hod. mimo prevádzky.",
    "Priecestie v km 12,345 pri žst. Zavar nebude dňa 10.12.2026 prejazdné.",
    "Priecestie pri žst. Zavar nebude dňa 10.12.2026 prejazdné.",
    "Úsek cesty II/507 pri žst. Zavar nebude dňa 10.12.2026 prejazdný.",
    "Cesta nebude v úseku Trnava – Leopoldov dňa 10.12.2026 prejazdná.",
    "Cestný most pri žst. Kraľovany nebude dňa 05.12.2026 prejazdný.",
    "Podchod v žst. Zavar nebude dňa 10.12.2026 priechodný.",
], ids=["kontrola_vytah", "kontrola_pokladna", "kontrola_informacny_system", "kontrola_priecestie_km",
        "kontrola_priecestie_pri_zst", "kontrola_usek_cesty", "kontrola_cesta_pred_usekom", "kontrola_cestny_most",
        "kontrola_podchod"])
def test_kontrola_mimo_prevadzky_a_neprejazdne_nie_trat(cfg, body):
    """[kontrola] Výťah, pokladňa, informačný systém, priecestie, cesta, cestný most či podchod – žiadna blokujúca
    udalosť (falošná výluka stanice či úseku by zdržala vlaky)."""
    items = _x("Oznámenie", body, RD if "10.12." in body else R, TT)
    assert not [i for i in items if i.kind in BLOCKING], _show(items)


def test_nebude_mozny_prejazd_vlakov_blokuje(cfg):
    """„nebude možný prejazd vlakov úsekom …“ – blokujúca udalosť úseku s termínom (nie „iné“)."""
    items = _x("Oznámenie", "Dňa 10.12.2026 od 08:00 do 14:00 hod. nebude možný prejazd vlakov úsekom Trnava – "
               "Leopoldov.", RD, TT)
    evs = [i for i in items if i.kind in BLOCKING]
    assert [(_sec(i), _term(i)) for i in evs] == [(TL, ("10.12. 08:00", "10.12. 14:00"))], _show(items)


# ====================================================================== sieť pre „iné“ e-maily ŽSR (úsek a čas)
@pytest.mark.parametrize("body", [
    "Dňa 10.12.2026 od 08:00 do 14:00 hod. sa v úseku Trnava – Leopoldov nebude dať prejsť.",
    "V úseku Trnava – Leopoldov budú dňa 10.12.2026 od 08:00 do 14:00 hod. prebiehať práce na koľaji.",
], ids=["nebude_dat_prejst", "prace_na_kolaji"])
def test_siet_usek_a_cas_bez_prevadzkoveho_slova(cfg, body):
    """E-mail ŽSR s úsekom trate a termínom s časom, ktorý pravidlá nerozpoznali – čakajúca úplná výluka úseku
    s poznámkou (nie „iné“: vlak 10.12. o 10:00 by dostal ZELENÚ)."""
    items = _x("Oznámenie", body, RD, TT)
    (ev,) = items
    assert (ev.kind, _sec(ev), _term(ev), ev.data.get("kind")) == \
        ("vyluka_nova", TL, ("10.12. 08:00", "10.12. 14:00"), "uplna"), _show(items)
    assert ev.confidence < 0.8 and (ev.evidence or {}).get("siet_proti_strate") == "usek_termin"
    assert any("nerozpoznali obmedzenie" in n for n in ev.notes), ev.notes


@pytest.mark.parametrize("body", [
    "Náhradná autobusová doprava v úseku Trnava – Leopoldov bude dňa 10.12.2026 od 08:00 do 14:00 hod.",
    "Porada k úseku Trnava – Leopoldov sa uskutoční 10.12.2026 od 09:00 do 11:00 hod.",
    "Úsek Trnava – Leopoldov je dňa 10.12.2026 od 08:00 do 14:00 hod. opäť prejazdný.",
    "V úseku Trnava – Leopoldov bude dňa 10.12.2026 od 08:00 do 14:00 hod. zvýšený pohyb pracovníkov.",
    "V úseku Trnava – Leopoldov dňa 01.11.2026 od 08:00 do 14:00 hod. prebehla údržba.",
    "Cestujúci v úseku Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 14:00 hod. použijú autobusy.",
], ids=["kontrola_nad", "kontrola_porada", "kontrola_opat_prejazdny", "kontrola_pohyb_pracovnikov",
        "kontrola_minuly_termin", "kontrola_cestujuci"])
def test_kontrola_siet_usek_a_cas_nic_neohlasuje(cfg, body):
    """[kontrola] NAD, porada, „opäť prejazdný“, upozornenie, minulý termín, cestujúci – žiadna udalosť siete."""
    items = _x("Oznámenie", body, RD, TT)
    assert not [i for i in items if i.kind in BLOCKING], _show(items)


def test_kontrola_siet_usek_a_cas_len_pre_e_mail_zsr(cfg):
    """[kontrola] Sieť pracuje len pre e-maily od ŽSR – dopravca či zákazník ju nespustí."""
    items = _x("Oznámenie", "V úseku Trnava – Leopoldov budú dňa 10.12.2026 od 08:00 do 14:00 hod. prebiehať práce "
               "na koľaji.", RD, "dispecing@dopravca.example")
    assert not [i for i in items if i.kind in BLOCKING], _show(items)


# ====================================================================== noci so zopakovaným „na“, osamelé okno
@pytest.mark.parametrize("text,want", [
    ("úplná výluka v noci na 26.11. a na 27.11.2026 od 22:00 do 04:00 hod.",
     [("25.11. 22:00", "26.11. 04:00"), ("26.11. 22:00", "27.11. 04:00")]),
    ("úplná výluka v noci na 26.11., na 27.11. a na 28.11.2026 od 22:00 do 04:00 hod.",
     [("25.11. 22:00", "26.11. 04:00"), ("26.11. 22:00", "27.11. 04:00"), ("27.11. 22:00", "28.11. 04:00")]),
    ("úplná výluka v nociach na 26.11. a na 27.11.2026 vždy od 22:00 do 04:00 hod.",
     [("25.11. 22:00", "26.11. 04:00"), ("26.11. 22:00", "27.11. 04:00")]),
    # kontrola: presun „z 26.11. na 27.11.“ nie je noc
    ("výluka sa presúva z 26.11. na 27.11.2026 od 08:00 do 14:00.", [("27.11. 08:00", "27.11. 14:00")]),
], ids=["a_na", "ciarka_na", "v_nociach_a_na", "kontrola_presun"])
def test_noci_so_zopakovanym_na(text, want):
    """[FC5] „v noci na 26.11. a na 27.11.“ – obe noci končia v uvedený deň (25./26. a 26./27.11.), žiadna noc sa
    nestratí ani neposunie o deň."""
    assert _periods(text) == want


def test_zrusenie_noci_so_zopakovanym_na(cfg):
    """[FC5] „Výluka … v noci na 26.11. a na 27.11.2026 sa ruší.“ – rušené sú noci 25./26. a 26./27.11."""
    items = _x("Zrušenie ROV č. 4610/2026", "Výluka Kysak – Košice v noci na 26.11. a na 27.11.2026 sa ruší.", R, KE)
    assert _cancels(items) == [("25.11. 22:00", "26.11. 06:00"), ("26.11. 22:00", "27.11. 06:00")], _show(items)


def test_osamele_okno_vo_vete_bez_dna(cfg):
    """[sieť] „… dňa 10.12. od 08:00 do 10:00 hod. Druhá časť výluky bude od 13:00 do 16:00 hod.“ – druhé okno je
    čakajúca udalosť toho istého dňa s poznámkou (deň z predchádzajúcej vety)."""
    items = _x("ROV č. 3922/2026", "Úplná výluka trate Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 10:00 hod. Druhá "
               "časť výluky bude od 13:00 do 16:00 hod.", RD, TT)
    (net,) = _net(items)
    assert (_sec(net), _term(net)) == (TL, ("10.12. 13:00", "10.12. 16:00")), _show(items)
    assert any("predchádzajúcej vety" in n for n in net.notes), net.notes


# ====================================================================== zámer zmeny – pridanie, predĺženie
_P = "Výluka Trnava – Leopoldov podľa ROV č. 3160/2026"


@pytest.mark.parametrize("body", [
    _P + " sa rozširuje aj na 17.12.2026 od 08:00 do 14:00 hod.",
    _P + " sa uskutoční ešte raz, a to 17.12.2026 od 08:00 do 14:00 hod.",
    _P + ": k pôvodnému termínu sa pridáva ešte 17.12.2026 od 08:00 do 14:00 hod.",
    _P + " dňa 10.12.2026 ostáva, nový termín 17.12.2026 od 08:00 do 14:00 hod. sa pridáva.",
    _P + " sa okrem 10.12.2026 vykoná taktiež 17.12.2026 od 08:00 do 14:00 hod.",
], ids=["rozsiruje_aj_na", "este_raz", "k_povodnemu_terminu", "povodny_ostava", "okrem_taktiez"])
def test_pridanie_terminu_dalsie_formulacie(cfg, body):
    """[FC2] Pridanie termínu je doplnením s novým termínom 17.12. – pôvodný termín 10.12. nie je ďalšou udalosťou
    siete (ostáva) a dátum za „pôvodný“ a slovami pridania sa nestratí."""
    items = _x("Zmena ROV č. 3160/2026", body, RD, TT)
    (ch,) = _of(items, "vyluka_zmena")
    assert (ch.data.get("zamer"), _term(ch)) == ("doplnenie", ("17.12. 08:00", "17.12. 14:00")), _show(items)
    assert not _net(items), _show(items)


def test_okrem_povodnych_dni_aj_novy_termin(cfg):
    """[FC2] „… sa okrem pôvodných dní vykoná aj 30.11.2026 od 08:00 do 14:00“ – nový termín sa nestratí."""
    items = _x("Zmena ROV č. 2331/2026", "Výluka Ružomberok – Liptovský Mikuláš (ROV č. 2331/2026) sa okrem pôvodných "
               "dní vykoná aj 30.11.2026 od 08:00 do 14:00 hod.")
    (ch,) = _of(items, "vyluka_zmena")
    assert (ch.data.get("zamer"), _term(ch)) == ("doplnenie", ("30.11. 08:00", "30.11. 14:00")), _show(items)


def test_viac_terminov_bez_zameru_je_doplnenie(cfg):
    """[FC2 sieť] „bude vykonaná v dvoch termínoch: 10.12. a 17.12.“ – zámer neuvedený; viac termínov tej istej
    výluky nikdy nenahrádza termín (druhá zmena by zmazala prvú) – oba sú doplnením s poznámkou."""
    items = _x("Zmena ROV č. 3160/2026", _P + " bude vykonaná v dvoch termínoch: 10.12.2026 a 17.12.2026 od 08:00 do "
               "14:00 hod.", RD, TT)
    changes = _of(items, "vyluka_zmena")
    assert sorted(_term(i) for i in changes) == [("10.12. 08:00", "10.12. 14:00"), ("17.12. 08:00", "17.12. 14:00")]
    assert all(i.data.get("zamer") == "doplnenie" for i in changes), _show(items)
    assert all(any("zámer neuvedený" in n for n in i.notes) for i in changes), _show(items)


def test_kontrola_novy_termin_bez_slov_pridania(cfg):
    """[FC2, kontrola] Jeden nový termín bez slov pridania ostáva bez zámeru (register postupuje ako doteraz)."""
    items = _x("Zmena ROV č. 3160/2026", _P + " – nový termín 17.12.2026 od 08:00 do 14:00 hod.", RD, TT)
    (ch,) = _of(items, "vyluka_zmena")
    assert ch.data.get("zamer") is None and _term(ch) == ("17.12. 08:00", "17.12. 14:00"), _show(items)


def test_predlzenie_o_dni_dlhsie(cfg):
    """[FC6] „bude trvať o dva dni dlhšie, do 29.11.“ – predĺženie s novým koncom (začiatok podľa registra)."""
    items = _x("Zmena ROV č. 2609/2026", "Výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2609/2026 bude trvať o dva "
               "dni dlhšie, do 29.11.2026.")
    (ch,) = _of(items, "vyluka_zmena")
    assert ch.data.get("zamer") == "predlzenie" and not ch.data.get("start") and ch.data.get("end"), _show(items)


def test_siet_presun_noci_povodna_noc_nie_je_terminom(cfg):
    """[sieť] „… sa presúva z noci na 26.11. na noc na 03.12.“ – pôvodná noc nie je novým termínom siete."""
    items = _x("Zmena ROV č. 4610/2026", "Výluka Kysak – Košice sa presúva z noci na 26.11. na noc na 03.12.2026, vždy "
               "od 22:00 do 04:00 hod.", R, KE)
    (ch,) = _of(items, "vyluka_zmena")
    assert _term(ch) == ("02.12. 22:00", "03.12. 04:00") and ch.data.get("zamer") == "presun", _show(items)
    assert not _net(items), _show(items)


# ====================================================================== zrušenie časti výluky
_S3 = "Zrušenie výluky – ROV č. 2501/2026"
_P3 = "Výluka podľa ROV č. 2501/2026 v úseku Ružomberok – Liptovský Mikuláš"


@pytest.mark.parametrize("tail", [
    " sa ruší vo všetky dni okrem nedele.",
    " sa ruší čiastočne.",
    " sa ruší v dňoch, keď nie je pracovný deň.",
    " sa ruší v nočných hodinách.",
    " sa ruší. Výluka v ostatných dňoch platí.",
], ids=["vsetky_dni_okrem", "ciastocne", "ked_nie_je_pracovny_den", "nocne_hodiny", "ostatne_dni_plati"])
def test_siet_zrusenie_casti_bez_terminu_dalsie(cfg, tail):
    """[FC3 sieť] Zrušenie len časti výluky bez určeného termínu je čiastočné (``partial``, bez termínu – zápis ho
    odmietne); nikdy nezruší celú výluku."""
    items = _x(_S3, _P3 + tail)
    (ev,) = _of(items, "vyluka_zrusenie")
    assert ev.data.get("partial") is True and not ev.data.get("start") and not ev.data.get("end"), _show(items)


@pytest.mark.parametrize("tail", [" sa ruší vo všetky dni.", " sa ruší v celom rozsahu."],
                         ids=["kontrola_vsetky_dni", "kontrola_cely_rozsah"])
def test_kontrola_zrusenie_celej_vyluky(cfg, tail):
    """[FC3, kontrola] „vo všetky dni“, „v celom rozsahu“ bez výnimky – zrušenie celej výluky (nie čiastočné)."""
    items = _x(_S3, _P3 + tail)
    (ev,) = _of(items, "vyluka_zrusenie")
    assert not ev.data.get("partial"), _show(items)


@pytest.mark.parametrize("body", [
    "Zrušenie výluky sa týka iba úseku Senica – Kúty. Úsek Trnava – Senica bude uzavretý podľa ROV.",
    "Ruší sa len výluka v úseku Senica – Kúty.",
    "Výluka sa ruší iba v úseku Senica – Kúty.",
], ids=["kontrola_tyka_iba_useku", "kontrola_len_vyluka_v_useku", "kontrola_iba_v_useku"])
def test_kontrola_vyber_useku_nie_je_ciastocne_zrusenie(cfg, body):
    """[FC3 sieť, kontrola] „iba úseku …“, „len výluka v úseku …“ vyberajú miesto, nie časť termínu – zrušenie úseku
    Senica – Kúty je celé (nie čiastočné, zápis ho neodmietne)."""
    items = _x("ROV 3360/2026 - zrusenie", body, "2026-12-03 14:00", TT)
    (ev,) = _of(items, "vyluka_zrusenie")
    assert _sec(ev) == ("Senica", "Kúty") and not ev.data.get("partial"), _show(items)


def test_zrusenie_len_v_stanici_nezrusi_usek(cfg):
    """[FC4] „Výluka v úseku Senica – Kúty sa ruší len v stanici Senica.“ – ruší sa len stanica, nikdy celý úsek
    (výluka úseku ostáva)."""
    items = _x("ROV 3360/2026 - zrusenie", "Výluka v úseku Senica – Kúty sa ruší len v stanici Senica.",
               "2026-12-03 14:00", TT)
    assert [_sec(i) for i in _of(items, "vyluka_zrusenie")] == [("Senica", "Senica")], _show(items)


@pytest.mark.parametrize("subject,body,want", [
    ("STORNO výluky 25.11.2026 – ROV č. 2603/2026", "pre výluku Ružomberok – Liptovský Mikuláš (23.11. – 27.11.2026, "
     "denne 08:00 – 14:00 hod.) platí: uvedený deň výluka odpadá, v ostatné dni sa vykoná.", [("25.11. 00:00", "26.11. 00:00")]),
    ("Zrušenie výluky 25.11.2026 – ROV č. 2603/2026", "Výluka Ružomberok – Liptovský Mikuláš (23.11. – 27.11.2026, denne "
     "08:00 – 14:00 hod.) sa v uvedený deň ruší.", [("25.11. 00:00", "26.11. 00:00")]),
    ("STORNO výluky 25.11.2026 – ROV č. 2603/2026", "Výluka Ružomberok – Liptovský Mikuláš (23.11. – 27.11.2026, denne "
     "08:00 – 14:00 hod.): v ostatné dni sa vykoná.", [("25.11. 00:00", "26.11. 00:00")]),
    # kontrola: deň zrušenia v tele, opis výluky v zátvorke; obdobie bez zátvorky ruší celé obdobie
    ("Zrušenie výluky – ROV č. 2603/2026", "Výluka Ružomberok – Liptovský Mikuláš (23.11. – 27.11.2026, denne 08:00 – "
     "14:00 hod.) sa dňa 25.11.2026 ruší, ostatné dni ostávajú.", [("25.11. 08:00", "25.11. 14:00")]),
    ("Zrušenie výluky – ROV č. 2603/2026", "Výluka Ružomberok – Liptovský Mikuláš v dňoch 23.11. – 27.11.2026 denne "
     "08:00 – 14:00 hod. sa ruší.", [("23.11. 08:00", "27.11. 14:00")]),
], ids=["uvedeny_den_odpada", "v_uvedeny_den", "kontrola_ostatne_dni_bez_vyroku", "kontrola_den_v_tele",
        "kontrola_cele_obdobie"])
def test_opis_v_zatvorke_a_uvedeny_den(cfg, subject, body, want):
    """[FC4, regresia denného okna] Obdobie v zátvorke výluku len opisuje – „uvedený deň výluka odpadá, v ostatné dni sa
    vykoná“ ruší len deň z predmetu (25.11.), nikdy všetky dni opisu (vlak 24.11. by dostal ZELENÚ)."""
    assert _cancels(_x(subject, body)) == want


def test_opis_v_zatvorke_ostatne_dni_bez_dna_je_ciastocne(cfg):
    """[FC3 sieť] „Výluka … (23.11. – 27.11., denne …) sa ruší, v ostatné dni sa vykoná.“ – rušený deň e-mail
    neuvádza: zrušenie je čiastočné (zápis ho odmietne), nikdy nezruší všetky dni opisu."""
    items = _x("Zrušenie výluky – ROV č. 2603/2026", "Výluka Ružomberok – Liptovský Mikuláš (23.11. – 27.11.2026, denne "
               "08:00 – 14:00 hod.) sa ruší, v ostatné dni sa vykoná.")
    (ev,) = _of(items, "vyluka_zrusenie")
    assert ev.data.get("partial") is True and not ev.data.get("start"), _show(items)


def test_vynimka_so_zoznamom_dni_v_tyzdni(cfg):
    """[FC4] „v dňoch 23.11. – 27.11. sa ruší okrem utorka 24.11. a štvrtka 26.11.“ – rušené sú 23., 25. a 27.11.;
    nikdy nie práve dni, ktoré ostávajú (26.11.)."""
    items = _x("Zrušenie výluky – ROV č. 2601/2026", "Výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2601/2026 "
               "v dňoch 23.11. – 27.11.2026 sa ruší okrem utorka 24.11. a štvrtka 26.11.2026.")
    assert _cancels(items) == [(f"{d}.11. 00:00", f"{d + 1}.11. 00:00") for d in (23, 25, 27)], _show(items)


# ====================================================================== číslo novej výluky
@pytest.mark.parametrize("subject,body", [
    ("ROV č. 2816/2026 a 2817/2026", "ROV č. 2816/2026: výluka Kraľovany – Ružomberok dňa 24.11.2026 sa ruší. ROV č. "
     "2817/2026: úplná výluka Kraľovany – Ružomberok dňa 30.11.2026 od 08:00 do 14:00 hod."),
    ("ROV 2816/2026, 2817/2026", "Výluka podľa ROV č. 2816/2026 v úseku Kraľovany – Ružomberok dňa 24.11.2026 sa ruší. "
     "Od 30.11.2026 08:00 hod. platí úplná výluka podľa ROV č. 2817/2026 v úseku Kraľovany – Ružomberok do 02.12.2026 "
     "14:00 hod."),
], ids=["a_zoznam", "ciarka_bez_terminu"])
def test_cislo_novej_vyluky_pri_zozname_cisel_v_predmete(cfg, subject, body):
    """[FC0 nízka] Predmet so zoznamom čísel – nová výluka má číslo 2817 (nie číslo rušenej výluky 2816, ktorej
    neskoršie STORNO by ju zrušilo)."""
    items = _x(subject, body)
    (new,) = _of(items, "vyluka_nova")
    (can,) = _of(items, "vyluka_zrusenie")
    assert (new.data.get("external_ref"), can.data.get("external_ref")) == ("2817/2026", "2816/2026"), _show(items)
