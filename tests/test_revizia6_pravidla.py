"""6. revízia – strana pravidiel (FA): formulácie FC0 – FC7 a sieť proti strate termínu.

- FC0: „(až) do odvolania“ pred podstatným menom („je od 23.11.2026 08:00 hod. až do odvolania úplná výluka …“, „Do
  odvolania platí …“, TLG, česky „do odvolání“, dvojitá medzera, zalomený riadok) je trvanie – nová výluka do
  odvolania, nikdy zrušenie; skutočné odvolanie výluky ostáva zrušením;
- FC1: náhradný či nový termín v e-maile o zrušení (predmet „Zrušenie/STORNO/Odvolanie … ROV“, „… sa ruší
  a uskutoční sa dňa …“) je vlastná nová výluka s číslom ROV; nový termín nikdy nie je rušeným termínom;
- FC2: zmena, ktorá termín pridáva („bude aj …“, „dopĺňa sa o …“, „opakovať aj …“, „pribúda …“, „rozširuje o …“), má
  ``zamer = doplnenie`` a len nové termíny (pôvodný termín pri „okrem 10.12. aj 17.12.“ nie);
- FC3: zrušenie dní v týždni bez dátumu, času bez dňa či „s výnimkou …“ nemá termín (``partial``) a zápis ho odmietne
  (validácia blokuje); deň v týždni v uvedenom období výluky dá zrušenie každého takého dňa;
- FC4: rušený termín je z časti, ktorá sa ruší („nekoná od 08:00 do 10:00, ale až od 10:00 do 14:00“), nikdy z časti,
  ktorá ostáva, ani z výnimky („… sa ruší okrem 25.11.“);
- FC5: „v noci na <deň>“ je noc, ktorá v ten deň končí;
- FC6: predĺženie má ``zamer = predlzenie`` a nový koniec (bez začiatku – začiatok ostáva podľa registra), nikdy
  jednodňový termín; skrátenie „do …“ rovnako (``skratenie``); „koniec výluky sa posúva na …“ je predĺženie;
- FC7: zmena času „z <starý> na <nový>“ zapíše nový čas (``zamer = zmena_casu``);
- zámer každej zmeny je jedna z hodnôt ``udalosti.CHANGE_INTENTS`` alebo None;
- sieť: termín e-mailu ŽSR, ktorý žiadna udalosť nepokrýva, dá čakajúcu blokujúcu udalosť s poznámkou „overte“ (bez
  úseku poistka pre celý vozový park; noc bez času od 12:00 do 12:00); udalosť bez termínu ho dostane ako poznámku;
  vedľajšie termíny (NAD, podpis, dátum vydania) ani e-mail iného odosielateľa sieť nepočíta;
- dátumy: okno patrí najbližšiemu obdobiu vety (nie obdobiu, ktoré ostáva), „…, 05.12.2026 celý deň“ je ďalší deň
  zoznamu (nie koniec obdobia).

Všetky adresy, osoby a čísla sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("Europe/Bratislava")
ZA = "vyluky.orza@zsr.example"
KE = "vyluky.orke@zsr.example"


def _recv(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).astimezone(timezone.utc)


def _x(subject: str, body: str, received: str = "2026-11-20 09:00", sender: str = ZA):
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


def _kinds(items) -> list:
    return sorted(i.kind for i in items)


def _show(items) -> str:
    return "; ".join(f"{i.kind} {i.data} {i.notes}" for i in items)


def _net(items) -> list:
    return [i for i in items if (i.evidence or {}).get("siet_proti_strate") == "termin"]


# ====================================================================== FC0 „do odvolania“ pred podstatným menom
@pytest.mark.parametrize("subject,body,sec,start", [
    ("ROV č. 2701/2026 – výluka do odvolania", "z dôvodu poškodenia mosta je od 23.11.2026 08:00 hod. až do odvolania "
     "úplná výluka trate v úseku Kraľovany – Ružomberok (ROV č. 2701/2026).", ("Kraľovany", "Ružomberok"),
     "23.11. 08:00"),
    ("ROV č. 2702/2026", "od 23.11.2026 od 08:00 hod. do odvolania platí úplná výluka trate v úseku Kraľovany – "
     "Ružomberok.", ("Kraľovany", "Ružomberok"), "23.11. 08:00"),
    ("ROV č. 2703/2026 – zosuv svahu", "z dôvodu zosuvu svahu bude do odvolania výluka oboch traťových koľají v úseku "
     "Kraľovany – Ružomberok, začiatok 23.11.2026 o 08:00 hod. ROV č. 2703/2026.", ("Kraľovany", "Ružomberok"),
     "23.11. 08:00"),
    ("TLG č. 88/2026 – Kraľovany – Ružomberok", "od 23.11.2026 08:00 hod. do odvolania výluka 1. a 2. traťovej koľaje "
     "v úseku Kraľovany – Ružomberok, porucha mostného objektu. Kontrolný dispečer OR Žilina.",
     ("Kraľovany", "Ružomberok"), "23.11. 08:00"),
    ("ROV č. 4707/2026 – Kysak – Košice", "trať v úseku Kysak – Košice je od 23.11.2026 08:00 hod. uzavretá, do "
     "odvolania úplná výluka oboch traťových koľají (ROV č. 4707/2026).", ("Kysak", "Košice"), "23.11. 08:00"),
    ("ROV č. 2912/2026", "Do odvolania platí úplná výluka trate v úseku Kysak – Košice od 23.11.2026 08:00 hod.",
     ("Kysak", "Košice"), "23.11. 08:00"),
    ("Do odvolania – výluka Kraľovany – Ružomberok", "Od 23.11.2026 10:00 hod. do odvolania výluka oboch traťových "
     "koľají v úseku Kraľovany – Ružomberok (ROV č. 2913/2026).", ("Kraľovany", "Ružomberok"), "23.11. 10:00"),
    ("ROV č. 2914/2026", "Od 23.11.2026 8:00 do odvolání výluka koleje v úseku Čadca – Krásno nad Kysucou.",
     ("Čadca", "Krásno nad Kysucou"), "23.11. 08:00"),
    ("ROV č. 2915/2026", "od 23.11.2026 08:00 hod. až do  odvolania úplná výluka trate v úseku Kysak – Košice.",
     ("Kysak", "Košice"), "23.11. 08:00"),
    ("ROV č. 2916/2026", "od 23.11.2026 08:00 hod. až do\nodvolania úplná výluka trate v úseku Kysak – Košice.",
     ("Kysak", "Košice"), "23.11. 08:00"),
], ids=["je_az_do_odvolania", "do_odvolania_plati", "bude_do_odvolania_zaciatok", "tlg", "uzavreta_do_odvolania",
        "veta_zacina", "predmet", "cesky", "dve_medzery", "zalomeny_riadok"])
def test_fc0_do_odvolania_pred_vylukou_je_nova_vyluka(cfg, subject, body, sec, start):
    """[FC0] „do odvolania“ pred podstatným menom je otvorený koniec novej výluky, nie jej odvolanie."""
    items = _x(subject, body)
    assert _kinds(items) == ["vyluka_nova"], _show(items)
    (it,) = items
    assert _sec(it) == sec and _term(it) == (start, None) and it.data.get("until_revoked") is True, _show(items)


def test_fc0_aktualizacia_do_odvolania_vyluku_nezrusi(cfg):
    """[FC0 b] Aktualizácia potvrdenej výluky „od 23.11. 08:00 do odvolania výluka 1. a 2. TK …, predpokladané
    ukončenie 27.11.“ výluku nezruší – je to výluka do odvolania toho istého ROV."""
    items = _x("ROV č. 2708/2026 – aktualizácia", "od 23.11.2026 08:00 hod. do odvolania výluka 1. a 2. traťovej "
               "koľaje v úseku Kraľovany – Ružomberok, predpokladané ukončenie 27.11.2026 o 18:00 hod. "
               "(ROV č. 2708/2026).", "2026-11-21 09:00")
    assert "vyluka_zrusenie" not in _kinds(items), _show(items)
    (it,) = items
    assert it.data["external_ref"] == "2708/2026" and _term(it)[0] == "23.11. 08:00" and it.data["until_revoked"]


@pytest.mark.parametrize("subject,body", [
    ("Odvolanie výluky – ROV č. 2915/2026", "Výluka podľa ROV č. 2915/2026 v úseku Kysak – Košice platná od "
     "01.12.2026 do odvolania sa ruší."),
    ("ROV č. 2916/2026", "Odvolávame výluku podľa ROV č. 2916/2026 (Kysak – Košice), ktorá platila do odvolania."),
    ("ROV č. 2917/2026", "Výluka do odvolania podľa ROV č. 2917/2026 v úseku Kysak – Košice sa ruší."),
])
def test_fc0_kontrola_skutocne_odvolanie_je_zrusenim(cfg, subject, body):
    """[FC0 kontrola] Skutočné odvolanie výluky (aj výluky „do odvolania“) ostáva zrušením, nie novou výlukou."""
    items = _x(subject, body, "2026-12-02 09:00")
    assert _kinds(items) == ["vyluka_zrusenie"], _show(items)


# ====================================================================== FC1 náhradný termín v e-maile o zrušení
@pytest.mark.parametrize("subject,body,ref,cancel,new", [
    ("Zrušenie výluky dňa 24.11.2026 – ROV č. 2328/2026", "výluka v úseku Ružomberok – Liptovský Mikuláš dňa "
     "24.11.2026 sa ruší. Náhradný termín výluky je 28.11.2026 od 08:00 do 14:00 hod.", "2328/2026",
     ("24.11. 00:00", "25.11. 00:00"), ("28.11. 08:00", "28.11. 14:00")),
    ("STORNO ROV č. 3118/2026", "pôvodný termín výluky Trnava – Leopoldov 10.12.2026 sa ruší, výluka bude vykonaná "
     "15.12.2026 od 08:00 do 14:00 hod.", "3118/2026", ("10.12. 00:00", "11.12. 00:00"),
     ("15.12. 08:00", "15.12. 14:00")),
    ("Zrušenie ROV č. 3119/2026", "výluka podľa ROV č. 3119/2026 dňa 10.12.2026 sa ruší.\n\nNový termín výluky: "
     "15.12.2026 od 08:00 do 14:00 hod., úsek Trnava – Leopoldov bez zmeny.", "3119/2026",
     ("10.12. 00:00", "11.12. 00:00"), ("15.12. 08:00", "15.12. 14:00")),
    ("Odvolanie výluky – ROV č. 3120/2026", "výluku Trnava – Leopoldov dňa 10.12.2026 odvolávame. Výluka bude "
     "vykonaná až 17.12.2026 od 08:00 do 14:00 hod.", "3120/2026", ("10.12. 00:00", "11.12. 00:00"),
     ("17.12. 08:00", "17.12. 14:00")),
    ("ROV č. 2908/2026 – zmena", "výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2908/2026 dňa 24.11.2026 sa ruší "
     "a uskutoční sa dňa 28.11.2026 od 08:00 do 14:00 hod.", "2908/2026", ("24.11. 00:00", "25.11. 00:00"),
     ("28.11. 08:00", "28.11. 14:00")),
    ("Zrušenie výluky – ROV č. 2921/2026", "Výluka v úseku Trnava – Leopoldov dňa 10.12.2026 sa ruší. Výluka sa "
     "presúva na 12.12.2026 od 08:00 do 14:00 hod.", "2921/2026", ("10.12. 00:00", "11.12. 00:00"),
     ("12.12. 08:00", "12.12. 14:00")),
    ("Odvolanie výluky – ROV č. 2923/2026", "Výluku Galanta – Šaľa dňa 11.12.2026 odvolávame, uskutoční sa až "
     "18.12.2026 v čase od 08:00 do 14:00 hod.", "2923/2026", ("11.12. 00:00", "12.12. 00:00"),
     ("18.12. 08:00", "18.12. 14:00")),
], ids=["zrusenie_nahradny", "storno_bude_vykonana", "zrusenie_novy_termin_odsek", "odvolanie_az", "ta_ista_veta",
        "presuva_sa", "odvolavame_ciarka"])
def test_fc1_nahradny_termin_v_zruseni_je_nova_vyluka(cfg, subject, body, ref, cancel, new):
    """[FC1] Nový termín v e-maile o zrušení je vlastná nová výluka s číslom ROV; zrušenie má pôvodný deň."""
    items = _x(subject, body, "2026-11-21 09:00")
    news = [i for i in items if i.kind == "vyluka_nova"]
    cancels = [i for i in items if i.kind == "vyluka_zrusenie"]
    assert [(i.data["external_ref"], _term(i)) for i in news] == [(ref, new)], _show(items)
    assert [(i.data["external_ref"], _term(i)) for i in cancels] == [(ref, cancel)], _show(items)


def test_fc1_storno_s_novym_terminom_bez_useku(cfg):
    """[FC1 D13] „ROV … sa stornuje. Výluka sa uskutoční v novom termíne 15.12. …“ – zrušenie ROV (bez termínu)
    a nová výluka 15.12. (úsek podľa čísla v registri); nový termín nie je termínom zrušenia."""
    items = _x("STORNO – ROV č. 3113/2026, nový termín", "ROV č. 3113/2026 sa stornuje. Výluka sa uskutoční v novom "
               "termíne 15.12.2026 od 08:00 do 14:00 hod.", "2026-12-01 09:00")
    cancels = [i for i in items if i.kind == "vyluka_zrusenie"]
    news = [i for i in items if i.kind == "vyluka_nova"]
    assert [_term(i) for i in cancels] == [(None, None)], _show(items)
    assert [(i.data["external_ref"], _term(i)) for i in news] == [("3113/2026", ("15.12. 08:00", "15.12. 14:00"))]


@pytest.mark.parametrize("body", [
    "Výluka Galanta – Šaľa dňa 11.12.2026 sa ruší. Ostatné termíny podľa ROV bez zmeny.",
    "Výluka Galanta – Šaľa dňa 11.12.2026 sa ruší. Nový termín oznámime samostatne.",
])
def test_fc1_kontrola_zrusenie_bez_noveho_terminu(cfg, body):
    """[FC1 kontrola] Bez nového termínu ostáva len zrušenie dňa (oznámenie budúceho termínu výluku neohlasuje)."""
    items = _x("Zrušenie výluky – ROV č. 2924/2026", body, "2026-12-01 09:00")
    assert [(i.kind, _term(i)) for i in items] == [("vyluka_zrusenie", ("11.12. 00:00", "12.12. 00:00"))], \
        _show(items)


# ====================================================================== FC2 doplnenie termínu
@pytest.mark.parametrize("subject,body,term,daily", [
    ("Zmena ROV č. 2331/2026 – doplnenie termínu", "ROV č. 2331/2026 sa dopĺňa o ďalší termín: výluka v úseku "
     "Ružomberok – Liptovský Mikuláš bude aj v dňoch 30.11.2026 – 04.12.2026 denne od 08:00 do 14:00 hod.",
     ("30.11. 08:00", "04.12. 14:00"), ("08:00", "14:00")),
    ("Zmena ROV č. 2333/2026 – opakovanie výluky", "výluka podľa ROV č. 2333/2026 v úseku Ružomberok – Liptovský "
     "Mikuláš sa bude opakovať aj v termíne od 30.11.2026 do 04.12.2026 denne od 08:00 do 14:00 hod.",
     ("30.11. 08:00", "04.12. 14:00"), ("08:00", "14:00")),
    ("Zmena ROV č. 3125/2026 – doplnenie ďalšieho termínu", "výluka v úseku Trnava – Leopoldov podľa ROV č. 3125/2026 "
     "sa uskutoční aj dňa 17.12.2026 v čase od 08:00 do 14:00 hod.", ("17.12. 08:00", "17.12. 14:00"), (None, None)),
    ("Zmena ROV č. 3124/2026", "výluka Trnava – Leopoldov podľa ROV č. 3124/2026 bude okrem 10.12.2026 aj dňa "
     "17.12.2026 od 08:00 do 14:00 hod.", ("17.12. 08:00", "17.12. 14:00"), (None, None)),
    ("Zmena ROV č. 2933/2026", "ROV č. 2933/2026 sa rozširuje o termín 17.12.2026 od 08:00 do 14:00 hod. (úsek "
     "Galanta – Šaľa).", ("17.12. 08:00", "17.12. 14:00"), (None, None)),
    ("Zmena ROV č. 2323/2026 – doplnenie termínu", "ROV č. 2323/2026 sa dopĺňa o ďalší termín: výluka v úseku "
     "Ružomberok – Liptovský Mikuláš bude aj v dňoch 30.11.2026 – 04.12.2026 denne od 08:00 do 14:00 hod. Termín "
     "23.11. – 27.11.2026 platí.", ("30.11. 08:00", "04.12. 14:00"), ("08:00", "14:00")),
], ids=["dopln_aj_v_dnoch", "opakovat_aj", "uskutocni_aj", "okrem_aj", "rozsiruje_o", "termin_plati"])
def test_fc2_doplnenie_ma_zamer_a_len_novy_termin(cfg, subject, body, term, daily):
    """[FC2] Zmena, ktorá termín pridáva, má zámer „doplnenie“ a len nový termín (pôvodný nie)."""
    items = _x(subject, body, "2026-11-21 09:00")
    assert [(i.kind, i.data.get("zamer"), _term(i), (i.data.get("daily_from"), i.data.get("daily_to")))
            for i in items] == [("vyluka_zmena", "doplnenie", term, daily)], _show(items)


def test_fc2_noci_doplnenie(cfg):
    """[FC2 D26] „nočné výluky … budú aj v nociach od 30.11. do 05.12. vždy od 22:00 do 04:00“ – doplnenie nocí."""
    items = _x("Zmena ROV č. 4426/2026 – rozšírenie", "nočné výluky Kysak – Košice podľa ROV č. 4426/2026 budú aj "
               "v nociach od 30.11.2026 do 05.12.2026 vždy od 22:00 do 04:00 hod.", "2026-11-21 09:00", KE)
    (it,) = items
    assert (it.kind, it.data["zamer"], it.data["daily_from"], it.data["daily_to"]) == \
        ("vyluka_zmena", "doplnenie", "22:00", "04:00") and _term(it)[0] == "30.11. 22:00", _show(items)


# ====================================================================== FC3 dni v týždni, čas bez dňa
@pytest.mark.parametrize("subject,body,days", [
    ("ROV č. 2501/2026 – víkend", "výluka podľa ROV č. 2501/2026 v úseku Ružomberok – Liptovský Mikuláš sa v sobotu "
     "a v nedeľu nevykoná. Od pondelka do piatku bez zmeny.", "sobota, nedeľa"),
    ("ROV č. 2809/2026 – víkend", "výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2809/2026 sa cez víkend "
     "nevykoná, v pracovné dni sa vykoná podľa ROV.", "sobota, nedeľa"),
    ("Zrušenie výluky v piatok – ROV č. 2810/2026", "výluka podľa ROV č. 2810/2026 v úseku Ružomberok – Liptovský "
     "Mikuláš sa v piatok ruší, od pondelka do štvrtka platí.", "piatok"),
    ("ROV č. 4606/2026", "výluka Kysak – Košice podľa ROV č. 4606/2026 sa v noci z piatka na sobotu ruší, ostatné "
     "noci platia.", None),
    ("ROV č. 2941/2026", "Výluka podľa ROV č. 2941/2026 v úseku Galanta – Šaľa sa v sobotu nekoná.", "sobota"),
    ("ROV č. 2948/2026", "Výluka Galanta – Šaľa podľa ROV č. 2948/2026 sa ruší v pracovné dni, cez víkend platí.",
     "pondelok"),
    ("ROV č. 2351/2026 – skrátenie denného času", "vo všetkých dňoch výluky podľa ROV č. 2351/2026 (Ružomberok – "
     "Liptovský Mikuláš) sa ruší čas od 12:00 do 14:00 hod.; výluka bude denne len od 08:00 do 12:00 hod.", None),
    ("ROV č. 2314/2026 – zrušenie výluky", "výluka podľa ROV č. 2314/2026 v úseku Ružomberok – Liptovský Mikuláš sa "
     "ruší s výnimkou dňa 25.11.2026, kedy sa vykoná od 08:00 do 14:00 hod.", None),
    ("ROV č. 2953/2026", "Výluka Galanta – Šaľa podľa ROV č. 2953/2026 sa ruší s výnimkou dní 01.12. a 03.12.2026.",
     None),
], ids=["sobota_nedela", "cez_vikend", "v_piatok", "noc_z_piatka", "v_sobotu", "pracovne_dni", "cas_bez_dna",
        "s_vynimkou_dna", "s_vynimkou_dni"])
def test_fc3_zrusenie_casti_bez_terminu_zapis_odmietne(cfg, session, subject, body, days):
    """[FC3] Zrušenie len časti výluky bez určeného termínu: bez termínu, príznak ``partial``, poznámka – a validácia
    ho zablokuje (zápis by zrušil celú výluku)."""
    from vlaky.posta.validacia import validate_event

    items = _x(subject, body, "2026-11-21 09:00", KE)
    (it,) = items
    assert it.kind == "vyluka_zrusenie" and it.data.get("partial") is True and _term(it) == (None, None), _show(items)
    notes = " ".join(it.notes)
    assert "celá výluka sa automaticky nezruší" in notes and (days is None or days in notes), notes
    res = validate_event(session, it.kind, it.data, confidence=it.confidence)
    assert any("bez rušeného termínu" in b for b in res.blocking), res.blocking
    # doplnený rušený termín (dispečer) blokovanie zruší
    done = dict(it.data, start="2026-11-28T00:00:00+01:00", end="2026-11-29T00:00:00+01:00")
    assert not any("bez rušeného termínu" in b for b in validate_event(session, it.kind, done, confidence=0.9).blocking)


@pytest.mark.parametrize("subject,body,terms", [
    ("Zrušenie výluky – ROV č. 2608/2026", "výluka Ružomberok – Liptovský Mikuláš v dňoch 23.11. – 27.11.2026 sa "
     "v stredu ruší.", [("25.11. 00:00", "26.11. 00:00")]),
    ("ROV č. 2944/2026", "Výluka Galanta – Šaľa podľa ROV č. 2944/2026 v dňoch 30.11. – 06.12.2026 sa v sobotu a "
     "nedeľu nevykoná.", [("05.12. 00:00", "06.12. 00:00"), ("06.12. 00:00", "07.12. 00:00")]),
    ("ROV č. 2945/2026", "Výluka Kysak – Košice podľa ROV č. 2945/2026 v nociach od 30.11. do 06.12.2026 vždy od 22:00 "
     "do 04:00 hod. sa v noci zo soboty na nedeľu ruší.", [("05.12. 22:00", "06.12. 04:00")]),
], ids=["streda_v_obdobi", "vikend_v_obdobi", "noc_zo_soboty"])
def test_fc3_den_v_tyzdni_v_obdobi_vyluky(cfg, subject, body, terms):
    """[FC3] Deň v týždni v uvedenom období výluky dá zrušenie každého takého dňa (noci), nie celého obdobia."""
    items = _x(subject, body, "2026-11-20 09:00", KE)
    assert all(i.kind == "vyluka_zrusenie" for i in items), _show(items)
    assert sorted(_term(i) for i in items) == terms, _show(items)


@pytest.mark.parametrize("body", [
    "Výluka podľa ROV č. 1142/2026 sa ruší. Obmedzenie rýchlosti v úseku Kysak – Košice podľa POTR ostáva v platnosti.",
    "Výluka podľa ROV č. 1142/2026 sa ruší. Pomalá jazda 50 km/h v úseku Kysak – Košice platí naďalej.",
    "Výluka podľa ROV č. 1142/2026 sa ruší. Náhradná autobusová doprava v úseku Kysak – Košice ostáva v platnosti.",
], ids=["potr_ostava", "pomala_jazda_nadalej", "nad_ostava"])
def test_fc3_kontrola_ine_obmedzenie_ostava_zrusenie_celej_vyluky(cfg, body):
    """[FC3 kontrola] Iné obmedzenie, ktoré ostáva (POTR, pomalá jazda, náhradná doprava), nie je časťou rušenej výluky
    – zrušenie celej výluky nemá príznak ``partial`` (zápis ho neodmietne)."""
    items = _x("Zrusenie vyluky ROV c. 1142/2026", body, "2026-11-09 10:00", KE)
    cancels = [i for i in items if i.kind == "vyluka_zrusenie"]
    assert cancels and not any(i.data.get("partial") for i in cancels), _show(items)


# ====================================================================== FC4 časť, ktorá ostáva
@pytest.mark.parametrize("subject,body,terms", [
    ("ROV č. 3206/2026 – posun začiatku", "výluka Trnava – Leopoldov dňa 10.12.2026 sa nekoná v čase od 08:00 do "
     "10:00 hod., ale až od 10:00 do 14:00 hod. (ROV č. 3206/2026).", [("10.12. 08:00", "10.12. 10:00")]),
    ("ROV č. 3214/2026", "výluka Trnava – Leopoldov dňa 10.12.2026 v čase od 08:00 do 10:00 hod. sa ruší, ale od 10:00 "
     "do 14:00 hod. bude. ROV č. 3214/2026.", [("10.12. 08:00", "10.12. 10:00")]),
    ("ROV č. 2356/2026 – 25.11.", "výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2356/2026 sa dňa 25.11.2026 "
     "nekoná od 08:00 do 11:00 hod., ale až od 11:00 do 14:00 hod. Ostatné dni bez zmeny.",
     [("25.11. 08:00", "25.11. 11:00")]),
    ("ROV č. 2907/2026", "výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2907/2026 v dňoch 23.11. až 27.11.2026 sa "
     "ruší okrem 25.11.2026, kedy sa vykoná v pôvodnom čase.",
     [("23.11. 00:00", "25.11. 00:00"), ("26.11. 00:00", "28.11. 00:00")]),
    ("ROV č. 2954/2026", "Výluka Trnava – Leopoldov dňa 10.12.2026 sa v čase od 10:00 do 14:00 ruší, od 08:00 do 10:00 "
     "sa vykoná (ROV č. 2954/2026).", [("10.12. 10:00", "10.12. 14:00")]),
    ("ROV č. 2359/2026", "nočná výluka Ružomberok – Liptovský Mikuláš z 12.12. na 13.12.2026 sa nekoná od 22:00 do "
     "00:00 hod., ale až od 00:00 do 04:00 hod. ROV č. 2359/2026.", [("12.12. 22:00", "13.12. 00:00")]),
], ids=["ale_az", "sa_rusi_ale_bude", "den_viacdennej", "okrem_dna", "kontrola_vykona_sa_druha_cast",
                                                                   "noc_ale_az"])
def test_fc4_ruseny_termin_z_casti_ktora_sa_rusi(cfg, subject, body, terms):
    """[FC4] Rušený termín je z časti, ktorá sa ruší – nie z časti „ale (až) …“, ktorá ostáva, ani z výnimky."""
    items = _x(subject, body, "2026-11-20 09:00")
    assert all(i.kind == "vyluka_zrusenie" for i in items) and sorted(_term(i) for i in items) == terms, _show(items)


# ====================================================================== FC5 „v noci na <deň>“
@pytest.mark.parametrize("subject,body,kind,term", [
    ("ROV č. 4503/2026", "v noci na 26.11.2026 sa výluka Kysak – Košice podľa ROV č. 4503/2026 nekoná.",
     "vyluka_zrusenie", ("25.11. 22:00", "26.11. 06:00")),
    ("ROV č. 4801/2026 – zrušenie jednej noci", "výluka Kysak – Košice podľa ROV č. 4801/2026 sa v noci na piatok "
     "27.11.2026 nekoná. Ostatné noci bez zmeny.", "vyluka_zrusenie", ("26.11. 22:00", "27.11. 06:00")),
    ("Zrušenie výluky v noci na 25.11.2026 – ROV č. 4802/2026", "nočná výluka Kysak – Košice v noci na 25.11.2026 sa "
     "ruší, ďalšie noci sa vykonajú podľa ROV.", "vyluka_zrusenie", ("24.11. 22:00", "25.11. 06:00")),
    ("ROV č. 2962/2026", "Nočná výluka Kysak – Košice bude v noci na 05.12.2026 od 23:00 do 03:00 hod. "
     "(ROV č. 2962/2026).", "vyluka_nova", ("04.12. 23:00", "05.12. 03:00")),
], ids=["noc_na_den", "noc_na_piatok", "predmet_noc_na", "nova_noc_na"])
def test_fc5_v_noci_na_den_je_noc_konciaca_v_ten_den(cfg, subject, body, kind, term):
    """[FC5] „v noci na D“ je noc (D-1)/D – zrušenie nezruší nasledujúcu noc a nová výluka začína deň pred D."""
    items = _x(subject, body, "2026-11-20 09:00", KE)
    assert [(i.kind, _term(i)) for i in items] == [(kind, term)], _show(items)


def test_fc5_datumy_noc_na_den(cfg):
    """[FC5] datumy: dátum za „v noci na“ (aj ďalšie dni zoznamu) je nočný token prvého dňa noci (deň v týždni
    pravidlá pred hľadaním dátumov vynechajú)."""
    from datetime import date

    from vlaky.posta import datumy
    from vlaky.posta.text import fold

    for text, days in (("v noci na 26.11.2026 sa výluka nekoná", [date(2026, 11, 25)]),
                       ("noc na 01.12.2026", [date(2026, 11, 30)]),
                       ("v noci na 26. a 27.11.2026 sa výluka nekoná", [date(2026, 11, 25), date(2026, 11, 26)])):
        toks = [t for t in datumy.scan(fold(text), date(2026, 11, 20)) if t.kind in ("date", "datetime")]
        assert [(t.day, t.night) for t in toks] == [(d, True) for d in days], (text, toks)
    # noc s dvoma dňami sa nemení
    toks = [t for t in datumy.scan(fold("v noci z 25. na 26.11.2026"), date(2026, 11, 20)) if t.kind == "date"]
    assert [(t.day, t.night) for t in toks] == [(date(2026, 11, 25), True)]


# ====================================================================== FC6 predĺženie, skrátenie
@pytest.mark.parametrize("subject,body,zamer,term,daily", [
    ("Predĺženie výluky – ROV č. 2609/2026", "výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2609/2026 sa "
     "predlžuje do 29.11.2026, denne od 08:00 do 14:00 hod.", "predlzenie", (None, "29.11. 14:00"), ("08:00", "14:00")),
    ("Predĺženie výluky – ROV č. 2803/2026", "z dôvodu nepriaznivého počasia sa výluka Ružomberok – Liptovský Mikuláš "
     "podľa ROV č. 2803/2026 predĺži o dva dni, t. j. do 29.11.2026, v rovnakom čase od 08:00 do 14:00 hod.",
     "predlzenie", (None, "29.11. 14:00"), ("08:00", "14:00")),
    ("Zmena ROV č. 2805/2026 – koniec výluky", "výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2805/2026 bude "
     "namiesto do 27.11.2026 trvať až do 30.11.2026, denne od 08:00 do 14:00 hod.", "predlzenie",
     ("28.11. 08:00", "30.11. 14:00"), ("08:00", "14:00")),
    ("Predĺženie výluky – ROV č. 3103/2026", "výluka dňa 10.12.2026 v úseku Trnava – Leopoldov (ROV č. 3103/2026) sa "
     "predlžuje do 16:00 hod. (pôvodne do 14:00 hod.).", "predlzenie", (None, "10.12. 16:00"), (None, None)),
    ("Predĺženie výluky – ROV č. 2971/2026", "Výluka Galanta – Šaľa podľa ROV č. 2971/2026 sa predlžuje až do "
     "12.12.2026 do 16:00 hod.", "predlzenie", (None, "12.12. 16:00"), (None, None)),
    ("Zmena ROV č. 2973/2026", "Koniec výluky Galanta – Šaľa podľa ROV č. 2973/2026 sa posúva na 09.12.2026 18:00 hod.",
     "predlzenie", (None, "09.12. 18:00"), (None, None)),
    ("ROV č. 2709/2026 – predĺženie", "z dôvodu nedokončenia prác sa ROV č. 2709/2026 mení: od 27.11.2026 18:00 hod. "
     "do odvolania úplná výluka trate v úseku Kraľovany – Ružomberok.", "predlzenie", ("27.11. 18:00", None),
     (None, None)),
    ("Zmena ROV č. 2975/2026", "Výluka Galanta – Šaľa podľa ROV č. 2975/2026 sa skracuje do 03.12.2026, denne od "
     "08:00 do 14:00 hod.", "skratenie", (None, "03.12. 14:00"), ("08:00", "14:00")),
    ("Zmena ROV – úprava konca výluky", "Výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2364/2026 sa skracuje, "
     "skončí už 25.11.2026 o 14:00 hod.", "skratenie", (None, "25.11. 14:00"), (None, None)),
    ("Zmena ROV – úprava konca výluky", "Výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2365/2026 sa predlžuje, "
     "skončí až 29.11.2026 o 14:00 hod.", "predlzenie", (None, "29.11. 14:00"), (None, None)),
], ids=["predlzuje_do", "predlzi_o_dva_dni", "namiesto_do_az_do", "predlzuje_do_casu", "az_do_dna_do_casu",
        "koniec_sa_posuva", "meni_od_konca_do_odvolania", "skracuje_do", "skracuje_skonci_uz", "predlzuje_skonci_az"])
def test_fc6_predlzenie_a_skratenie_nikdy_jednodnovy_termin(cfg, subject, body, zamer, term, daily):
    """[FC6] Predĺženie (skrátenie) „do …“ má zámer a nový koniec – začiatok podľa registra (None), nikdy jednodňový
    termín posledného dňa; „namiesto do X až do Y“ začína dňom po X."""
    items = _x(subject, body, "2026-11-21 09:00")
    assert [(i.kind, i.data.get("zamer"), _term(i), (i.data.get("daily_from"), i.data.get("daily_to")))
            for i in items] == [("vyluka_zmena", zamer, term, daily)], _show(items)


def test_fc6_predlzenie_o_den_s_terminom_dna(cfg):
    """[FC6 kontrola] „predlžuje sa o jeden deň, t. j. aj na 28.11.2026 od 08:00 do 14:00“ – termín je ten nový deň."""
    items = _x("Zmena ROV č. 2321/2026 – predĺženie", "výluka v úseku Ružomberok – Liptovský Mikuláš podľa ROV č. "
               "2321/2026 sa predlžuje o jeden deň, t. j. aj na 28.11.2026 od 08:00 do 14:00 hod.", "2026-11-21 09:00")
    assert [(i.kind, i.data.get("zamer"), _term(i)) for i in items] == [
        ("vyluka_zmena", "predlzenie", ("28.11. 08:00", "28.11. 14:00"))], _show(items)


# ====================================================================== FC7 zmena času „z … na …“
@pytest.mark.parametrize("subject,body,term", [
    ("ROV č. 3807/2026 – zmena času", "výluka Trnava – Leopoldov dňa 10.12.2026 (ROV č. 3807/2026) sa mení z 08:00 – "
     "14:00 hod. na 06:00 – 16:00 hod.", ("10.12. 06:00", "10.12. 16:00")),
    ("ROV č. 4513/2026 – zmena času jednej noci", "v noci 25./26.11.2026 sa čas výluky Kysak – Košice mení z 22:00 – "
     "04:00 hod. na 20:00 – 06:00 hod. Ostatné noci podľa ROV č. 4513/2026.", ("25.11. 20:00", "26.11. 06:00")),
    ("ROV č. 2806/2026 – zmena času 25.11.", "dňa 25.11.2026 sa čas výluky Ružomberok – Liptovský Mikuláš podľa ROV "
     "č. 2806/2026 mení z 08:00 – 14:00 hod. na 07:00 – 16:00 hod. Ostatné dni bez zmeny.",
     ("25.11. 07:00", "25.11. 16:00")),
    ("ROV č. 2982/2026", "Výluka Trnava – Leopoldov dňa 10.12.2026 sa posúva z 08:00 – 14:00 hod. na 10:00 – 16:00 hod. "
     "(ROV č. 2982/2026).", ("10.12. 10:00", "10.12. 16:00")),
    ("ROV č. 2983/2026", "V noci 08./09.12.2026 sa mení čas výluky Kysak – Košice z 22:00 – 04:00 na 21:00 – 05:00 hod. "
     "(ROV č. 2983/2026).", ("08.12. 21:00", "09.12. 05:00")),
], ids=["den", "noc", "den_viacdennej", "posuva_z_na", "meni_cas_vyluky"])
def test_fc7_zmena_casu_zapise_novy_cas(cfg, subject, body, term):
    """[FC7] „z <starý čas> na <nový čas>“ – zmena so zámerom „zmena_casu“ a novým časom (nie pôvodným)."""
    items = _x(subject, body, "2026-11-20 09:00", KE)
    assert [(i.kind, i.data.get("zamer"), _term(i)) for i in items] == [("vyluka_zmena", "zmena_casu", term)], \
        _show(items)


# ====================================================================== zámer zmeny – povolené hodnoty
def test_zamer_zmeny_je_povolena_hodnota(cfg):
    """Každá zmena výluky má ``zamer`` z ``CHANGE_INTENTS`` alebo None; schéma zámer aj príznak ``partial`` drží."""
    from vlaky.posta.udalosti import CHANGE_INTENTS, normalize_data

    assert set(CHANGE_INTENTS) == {"presun", "doplnenie", "predlzenie", "skratenie", "zmena_casu"}
    assert normalize_data("vyluka_zmena", {"zamer": "doplnenie", "x": 1}).get("zamer") == "doplnenie"
    assert normalize_data("vyluka_zrusenie", {"partial": True}).get("partial") is True
    mails = [("Zmena ROV č. 2934/2026", "Výluka Galanta – Šaľa podľa ROV č. 2934/2026 sa presúva z 10.12.2026 na "
              "17.12.2026, čas od 08:00 do 14:00 hod.", "presun"),
             ("Zmena ROV č. 2935/2026", "Výluka Galanta – Šaľa podľa ROV č. 2935/2026 bude okrem 10.12.2026 aj "
              "11.12.2026 v čase od 08:00 do 14:00 hod.", "doplnenie"),
             ("Zmena ROV č. 2974/2026", "ROV č. 2974/2026 sa mení: od 06.12.2026 18:00 hod. do odvolania úplná výluka "
              "v úseku Galanta – Šaľa.", "predlzenie"),
             ("Zmena ROV č. 2981/2026", "Čas výluky Trnava – Leopoldov dňa 10.12.2026 sa mení z 8:00 – 14:00 na "
              "9:00 – 17:00 hod. (ROV č. 2981/2026).", "zmena_casu")]
    for subject, body, want in mails:
        items = _x(subject, body, "2026-12-01 09:00")
        zmeny = [i for i in items if i.kind == "vyluka_zmena"]
        assert zmeny and all(i.data.get("zamer") in CHANGE_INTENTS + (None,) for i in zmeny), _show(items)
        assert [i.data.get("zamer") for i in zmeny] == [want], _show(items)


# ====================================================================== sieť proti strate termínu
def test_siet_termin_bez_udalosti_da_cakajucu_vyluku(cfg):
    """[sieť] „Výluka bude aj 08.12.2026, čas upresníme.“ – termín bez udalosti dá čakajúcu úplnú výluku úseku
    e-mailu na celý deň s poznámkou „overte“ (nie potichu stratený termín)."""
    from vlaky.config import settings

    items = _x("ROV č. 2994/2026", "Úplná výluka v úseku Kysak – Košice dňa 01.12.2026 od 08:00 do 12:00 hod. Výluka "
               "bude aj 08.12.2026, čas upresníme.", "2026-11-27 09:00", KE)
    (net,) = _net(items)
    assert net.kind == "vyluka_nova" and _sec(net) == ("Kysak", "Košice") and net.data["kind"] == "uplna"
    assert _term(net) == ("08.12. 00:00", "09.12. 00:00") and net.data["external_ref"] == "2994/2026"
    assert "nepatrí k žiadnej zapísanej udalosti – overte" in net.notes[0]
    assert net.confidence < settings().confirm_threshold


def test_siet_bez_useku_je_poistka_pre_vozovy_park(cfg):
    """[sieť] Termín bez úseku v e-maile s viacerými úsekmi – udalosť bez miesta (poistka pre celý vozový park)."""
    items = _x("ROV č. 2980/2026", "Úplná výluka v úseku Kysak – Košice dňa 01.12.2026 od 08:00 do 12:00 hod. Úplná "
               "výluka v úseku Galanta – Šaľa dňa 02.12.2026 od 08:00 do 12:00 hod. Výluka bude aj 08.12.2026, čas "
               "upresníme.", "2026-11-27 09:00", KE)
    (net,) = _net(items)
    assert _sec(net) == (None, None) and _term(net) == ("08.12. 00:00", "09.12. 00:00")
    assert any("poistka pre celý vozový park" in n for n in net.notes), net.notes


def test_siet_noc_bez_casu_od_poludnia_do_poludnia(cfg):
    """[sieť] „… a v noci 03./04.12.2026“ bez času – noc od 12:00 do 12:00 nasledujúceho dňa (nočná výluka môže začať
    aj pred 22:00), s poznámkou o predpokladanom čase."""
    items = _x("ROV č. 2999/2026", "Úplná výluka v úseku Kysak – Košice dňa 01.12.2026 od 08:00 do 12:00 hod. a v noci "
               "03./04.12.2026.", "2026-11-27 09:00", KE)
    (net,) = _net(items)
    assert _term(net) == ("03.12. 12:00", "04.12. 12:00"), _show(items)
    assert any("Čas nočnej výluky nie je uvedený" in n for n in net.notes), net.notes


@pytest.mark.parametrize("subject,body,count", [
    ("ROV 5310/2026 – odklad výluky", "Výluka podľa ROV 5310/2026 sa odkladá o týždeň, na noc 14./15.12.2026, "
     "v rovnakom čase.", 1),
    ("Výluka 1. TK Poprad-Tatry – Liptovský Mikuláš", "Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš začne "
     "03.12.2026 o 08:00 a potrvá 6 dní.", 1),
    ("ROV č. 2998/2026", "Úplná výluka v úseku Kysak – Košice v noci 14./15.12.2026.", 1),
], ids=["odklad_v_rovnakom_case", "potrva_6_dni", "noc_bez_casu"])
def test_siet_udalost_bez_terminu_dostane_poznamku(cfg, subject, body, count):
    """[sieť] Udalosť bez termínu (čaká na dispečera bez časového obmedzenia) termín e-mailu dostane ako poznámku –
    nie ďalšiu udalosť."""
    items = _x(subject, body, "2026-12-01 09:00", KE)
    assert len(items) == count and not _net(items), _show(items)
    assert any("nie je v údajoch udalosti – pri potvrdení doplňte začiatok a koniec" in n for n in items[0].notes), \
        items[0].notes


@pytest.mark.parametrize("subject,body", [
    ("ROV č. 2992/2026", "Úplná výluka v úseku Kysak – Košice dňa 01.12.2026 od 08:00 do 12:00 hod. Náhradná autobusová "
     "doprava dňa 02.12.2026 od 08:00 do 12:00 hod."),
    ("ROV č. 2993/2026", "Úplná výluka v úseku Kysak – Košice dňa 01.12.2026 od 08:00 do 12:00 hod.\n\nVybavuje: "
     "J. Skúšobný, tel. 0900 000 000, 26.11.2026"),
    ("ROV č. 2995/2026 zo dňa 20.11.2026", "Úplná výluka v úseku Kysak – Košice dňa 01.12.2026 od 08:00 do 12:00 hod. "
     "podľa ROV č. 2995/2026 zo dňa 20.11.2026."),
], ids=["nad", "podpis", "datum_vydania"])
def test_siet_kontrola_vedlajsie_terminy_nepocita(cfg, subject, body):
    """[sieť kontrola] Termín náhradnej dopravy, dátum v podpise či dátum vydania rozkazu výlukou nie sú."""
    items = _x(subject, body, "2026-11-27 09:00", KE)
    assert not _net(items) and [i.kind for i in items] == ["vyluka_nova"], _show(items)


def test_siet_len_pre_e_mail_zsr(cfg):
    """[sieť kontrola] E-mail iného odosielateľa než ŽSR sieť nepočíta (termín ostane v poznámke)."""
    items = _x("ROV č. 2994/2026", "Úplná výluka v úseku Kysak – Košice dňa 01.12.2026 od 08:00 do 12:00 hod. Výluka "
               "bude aj 08.12.2026, čas upresníme.", "2026-11-27 09:00", "dispecing@partner.example")
    assert not _net(items), _show(items)


# ====================================================================== dátumy – okná a zoznamy dní
def test_datumy_okno_k_obdobiu_presunu_nie_povodnemu(cfg):
    """[dátumy] „… presúva z 23.11. – 27.11.2026 na 30.11. – 04.12.2026, denne od 08:00 do 14:00“ – okno patrí
    novému obdobiu; jedna zmena 30.11. – 04.12. denne, nie dve s pomiešaným oknom."""
    items = _x("Zmena ROV č. 2324/2026", "výluka podľa ROV č. 2324/2026 v úseku Ružomberok – Liptovský Mikuláš sa "
               "z dôvodu nedodania materiálu presúva z 23.11. – 27.11.2026 na 30.11. – 04.12.2026, denne od 08:00 do "
               "14:00 hod.", "2026-11-20 09:00")
    assert [(i.kind, i.data.get("zamer"), _term(i), i.data.get("daily_from")) for i in items] == [
        ("vyluka_zmena", "presun", ("30.11. 08:00", "04.12. 14:00"), "08:00")], _show(items)


def test_datumy_cely_den_je_dalsi_den_zoznamu(cfg):
    """[dátumy] „v dňoch 01.12. a 03.12.2026 od 08:00 do 12:00 hod., 05.12.2026 celý deň“ – 05.12. nie je koniec
    obdobia od 03.12. (výluka by stratila 05.12. od 12:00); každý deň zoznamu je pokrytý."""
    items = _x("ROV č. 2996/2026", "Úplná výluka v úseku Kysak – Košice v dňoch 01.12. a 03.12.2026 od 08:00 do 12:00 "
               "hod., 05.12.2026 celý deň.", "2026-11-27 09:00", KE)
    terms = sorted(_term(i) for i in items)
    assert ("03.12. 08:00", "05.12. 12:00") not in terms, _show(items)
    assert terms == [("01.12. 08:00", "01.12. 12:00"), ("03.12. 08:00", "03.12. 12:00"),
                     ("05.12. 00:00", "06.12. 00:00")], _show(items)


# ====================================================================== zrušenie – podmet vety
def test_zrusenie_druhej_casti_s_casom_v_zatvorke(cfg):
    """„…: druhá časť výluky (od 12:00 do 16:00 hod.) sa ruší. Prvá časť … platí.“ – dvojbodka v čase „12:00“ časť
    vety neoddeľuje; zrušenie 12:00 – 16:00 (nie nová výluka bez termínu)."""
    items = _x("ROV č. 3208/2026 – zrušenie druhej časti", "výluka Trnava – Leopoldov dňa 10.12.2026 podľa ROV č. "
               "3208/2026: druhá časť výluky (od 12:00 do 16:00 hod.) sa ruší. Prvá časť od 08:00 do 12:00 hod. platí.",
               "2026-12-01 09:00")
    assert [(i.kind, _term(i)) for i in items] == [("vyluka_zrusenie", ("10.12. 12:00", "10.12. 16:00"))], _show(items)


def test_zrusenie_stanice_usek_ostava(cfg):
    """„výluka v žst. Ružomberok sa ruší, výluka v úseku Ružomberok – Liptovský Mikuláš zostáva.“ – zruší sa len
    výluka stanice; úsek, ktorý ostáva, sa nezruší."""
    items = _x("ROV č. 2408/2026 – zmena", "výluka v žst. Ružomberok sa ruší, výluka v úseku Ružomberok – Liptovský "
               "Mikuláš zostáva.", "2026-11-20 09:00")
    assert [(i.kind, _sec(i)) for i in items] == [("vyluka_zrusenie", ("Ružomberok", "Ružomberok"))], _show(items)
