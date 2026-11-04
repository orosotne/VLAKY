"""5. revízia – pravidlá e-mailov ŽSR (oblasť A), opravy po kontrole: zvyšky nálezov R5F1, R5F3, R5C0 – R5C2,
R5C5, R5C6 a regresie siete proti strate obsahu.

- „… 14:00 h. Výluka dňa 13.11. sa ruší.“ sú dve vety – ohlásená výluka sa nezmení na zrušenie (jednotka za číslom
  – „h.“, „min.“, „t.“ – nie je skratka mena), veta môže začínať aj dátumom („… 14:00. 21.11. od …“);
- dve udalosti v jednej vete („…; dňa 21.11. … výluka TV v úseku Košice – Čaňa“, „…, a dňa …“) sa párujú po
  častiach vety a druh jednej sa neprenesie na druhú;
- zápor vedľajšej časti vety s vlastným podmetom („…, spoje nebudú realizované“, „…, NAD nebude realizovaná“)
  výluku neruší; „Termín na zaslanie pripomienok … sa ruší“ nie je zrušenie výluky;
- „jazda vlakov nebude možná“, „vlaky nebudú premávať“, „nepôjde žiadny vlak“ pri výluke 1. TK – úplná výluka
  s poznámkou „Overte druh výluky“;
- zrušenie „okrem úseku …“, „s výnimkou úseku …“ a „… sa koná“ úsek, ktorý ostáva, nezruší;
- zákaz jazdy vlakov aj v trpnom rode, miesto „cez žst. X“ (nie „OR Košice“ z predmetu); zrušený zákaz, cestné
  vozidlá, osobné vlaky a neprevádzkové oznamy (budova, parkovisko, pokladne, BOZP) nie sú mimoriadnosťou;
- ROV, ktorý informatívny prehľad len spomína, je výluka; KD len s časom budúceho okna je dnes; stĺpec „Termín“
  s rozsahom dní dá celý termín.

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from test_revizia3_terminy import as_utc, events, raw_mail, utc
from test_revizia4_pravidla4_zapis import ROV_KE, _active, _confirm_all, _decide, _process, env  # noqa: F401

TZ = ZoneInfo("Europe/Bratislava")
ROV_TT = "ZSR OR Trnava <vyluky.ortt@zsr.example>"
ROV_BA = "ZSR OR Bratislava <vyluky.orba@zsr.example>"
KD_KE = "Kontrolny dispecer OR Kosice <kd.orke@zsr.example>"


def _x(subject: str, body: str, received: datetime = utc(2026, 11, 9, 8), sender: str = "vyluky.orke@zsr.example"):
    from vlaky.posta.extrakcia import extract
    from vlaky.posta.udalosti import MailDocument

    doc = MailDocument(subject=subject, body="Dobrý deň,\n\n" + body + "\n\nS pozdravom\nOR Košice\n", sender=sender,
                       received_at=received)
    return extract(doc, use_llm=False).items


def _drop(env, name: str, data: bytes, received: datetime) -> None:
    """E-mail do schránky s časom doručenia ``received`` (čas súboru)."""
    path = Path(env.mail_dir) / name
    path.write_bytes(data)
    os.utime(path, (received.timestamp(), received.timestamp()))


def _loc(v) -> str | None:
    if not v:
        return None
    return datetime.fromisoformat(v).astimezone(TZ).strftime("%d.%m. %H:%M")


def _ev(items) -> list[tuple]:
    return [(i.kind, i.data.get("kind"), i.data.get("from_station"), i.data.get("to_station"),
             _loc(i.data.get("start")), _loc(i.data.get("end"))) for i in items]


def _notes(items) -> str:
    return " ".join(n for i in items for n in i.notes)


def _name(text):
    from vlaky.posta.stanice import stations

    st = stations().match(text) if text else None
    return st.name if st else text


KK = "Úplná výluka v úseku Kysak – Košice dňa 20.11.2026 od 08:00 do 14:00"
KK_DOWN = ("vyluka_nova", "uplna", "Kysak", "Košice", "20.11. 08:00", "20.11. 14:00")
KC_TV = ("vyluka_nova", "trakcne_vedenie", "Košice", "Čaňa", "21.11. 08:00", "21.11. 14:00")


# ====================================================================== koniec vety a dve udalosti vo vete (R5F3)
def test_jednotka_za_cislom_konci_vetu_a_veta_moze_zacat_datumom(cfg):
    """„h.“, „min.“, „t.“ hneď za číslom pred veľkým písmenom sú koniec vety (nie skratka mena „J. Novák“, „N. Zámky“):
    ohlásená výluka 20.11. sa nezmení na zrušenie a druh výluky TV v úseku Košice – Čaňa sa neprenesie na Kysak –
    Košice; veta môže začínať aj dátumom („… do 14:00. 21.11. od 08:00 …“)."""
    items = _x("ROV č. 1701/2026", KK + " h. Výluka dňa 13.11.2026 sa ruší.")
    assert [e for e in _ev(items) if e[0] == "vyluka_nova"] == [KK_DOWN], _ev(items)
    assert not any(i.kind == "vyluka_zrusenie" and _loc(i.data.get("start")) == "20.11. 08:00" for i in items)
    a = "Výluka v úseku Kysak – Košice bude dňa 20.11.2026 od 08:00 do 14:00"
    for body in (a + " h. Dňa 21.11.2026 od 08:00 do 14:00 h. bude napäťová výluka TV v úseku Košice – Čaňa.",
                 a + " min. Dňa 21.11.2026 od 08:00 do 14:00 bude napäťová výluka TV v úseku Košice – Čaňa.",
                 a + ". 21.11. od 08:00 do 14:00 napäťová výluka TV Košice – Čaňa.",
                 "Výluka Kysak – Košice 20.11.2026 8:00 – 14:00 h. Napäťová výluka TV Košice – Čaňa 21.11.2026 "
                 "8:00 – 14:00 h.",
                 KK + " pre vlaky nad 1600 t. Dňa 21.11.2026 od 08:00 do 14:00 bude výluka TV v úseku Košice – Čaňa."):
        assert _ev(_x("ROV č. 1701/2026", body)) == [KK_DOWN, KC_TV], body
    # skratka mena či názvu vetu nerozdelí, termín za ňou ostáva termínom tej istej udalosti
    items = _x("ROV č. 1701/2026", KK + ". Výluku riadi Ing. J. Novák. Dňa 21.11.2026 od 08:00 do 14:00 bude výluka TV "
               "v úseku Košice – Čaňa.")
    assert _ev(items) == [KK_DOWN, KC_TV]


def test_dve_udalosti_v_jednej_vete_sa_paruju_po_castiach(cfg):
    """„Výluka A – B dňa X; dňa Y … výluka TV v úseku B – C.“, „…, a dňa Y …“ – každý úsek má svoj termín a svoj
    druh (nie výluka TV na oboch úsekoch v oboch dňoch, nie stratený úsek Košice – Čaňa)."""
    a = "Výluka v úseku Kysak – Košice bude dňa 20.11.2026 od 08:00 do 14:00"
    for body in (a + "; dňa 21.11.2026 od 08:00 do 14:00 bude napäťová výluka TV v úseku Košice – Čaňa.",
                 a + ", a dňa 21.11.2026 od 08:00 do 14:00 bude napäťová výluka TV v úseku Košice – Čaňa.",
                 a + " a dňa 21.11.2026 od 08:00 do 14:00 bude napäťová výluka TV v úseku Košice – Čaňa."):
        assert _ev(_x("ROV č. 1701/2026", body)) == [KK_DOWN, KC_TV], body
    items = _x("ROV č. 1701/2026", KK + "; dňa 21.11.2026 od 08:00 do 14:00 výluka 2. TK v úseku Košice – Čaňa.")
    assert [(i.data.get("kind"), i.data.get("track")) for i in items] == [("uplna", None), ("kolaj", "2")]
    # dátum s vlastným oknom za „h.“ je ďalší termín, nie koniec obdobia „do 14:00 21.11.“
    items = _x("ROV č. 1701/2026", "Výluka v úseku Kysak – Košice bude dňa 20.11.2026 od 08:00 do 14:00 h. Dňa "
               "21.11.2026 od 08:00 do 14:00 h. úplná výluka v úseku Košice – Čaňa.")
    assert [(e[2], e[4], e[5]) for e in _ev(items)] == [("Kysak", "20.11. 08:00", "20.11. 14:00"),
                                                         ("Košice", "21.11. 08:00", "21.11. 14:00")]


def test_e2e_h_bodka_pripomenutie_nezrusi_overenu_vyluku(env, session):
    """Overená výluka ROV 1701/2026 a pripomenutie „… od 08:00 do 14:00 h. Výluka dňa 13.11.2026 sa ruší.“ – výluka
    20.11. sa nezruší a vlak o 10:00 nedostane VYPRAVIŤ."""
    _drop(env, "a.eml", raw_mail("ROV c. 1701/2026 - vyluka Kysak - Kosice", "oznamujeme úplnú výluku v úseku Kysak – "
                                 "Košice dňa 20.11.2026 od 08:00 do 14:00 (ROV č. 1701/2026).\n", "h1", sender=ROV_KE,
                                 date="Mon, 09 Nov 2026 10:15:00 +0100"), utc(2026, 11, 9, 9, 15))
    _process(session, utc(2026, 11, 9, 9, 17))
    _confirm_all(session)
    _drop(env, "b.eml", raw_mail("ROV c. 1701/2026 - vyluka Kysak - Kosice", KK + " h. Výluka dňa 13.11.2026 sa ruší.\n",
                                 "h2", sender=ROV_KE, date="Tue, 10 Nov 2026 09:00:00 +0100"), utc(2026, 11, 10, 8))
    _process(session, utc(2026, 11, 10, 8, 2))
    _confirm_all(session)
    assert [(c.status, as_utc(c.start_utc)) for c in _active(session)] == [("overene", utc(2026, 11, 20, 7))]
    r = _decide(session, "72001", "Kysak", "Košice", utc(2026, 11, 20, 9), utc(2026, 11, 20, 5))
    assert r.decision != "VYPRAVIŤ", r.summary


# ====================================================================== zápor s iným podmetom (R5F1)
NOMINAL = ["spoje nebudú realizované.", "vlakové spoje nebudú v tomto čase realizované.", "NAD nebude realizovaná.",
           "obsluha vlečky nebude realizovaná.", "posilové spoje nebudú realizované."]


def test_zapor_vedlajsej_casti_s_vlastnym_podmetom_vyluku_nerusi(cfg):
    """Menná veta o výluke s vedľajšou časťou, ktorá má vlastný podmet („…, spoje nebudú realizované“, „– rýchliky
    nebudú realizované“, „; výkony nebudú uskutočnené“) – výluka, nie zrušenie; skutočné zrušenia ostávajú."""
    bodies = [KK + ", " + t for t in NOMINAL] + [KK + " – rýchliky nebudú realizované.",
                                                 KK + "; výkony nebudú uskutočnené.",
                                                 "Výluka v úseku Kysak – Košice: 20.11.2026 od 08:00 do 14:00, spoje "
                                                 "nebudú uskutočnené."]
    for body in bodies:
        items = _x("ROV č. 1601/2026 – výluka Kysak – Košice", body)
        assert [e[0] for e in _ev(items)] == ["vyluka_nova"], (body, _ev(items))
    for body in ("Výluka v úseku Kysak – Košice dňa 20.11.2026, z technických dôvodov nebude realizovaná.",
                 "Výluka v úseku Kysak – Košice dňa 20.11.2026, plánovaná od 8:00 do 14:00, sa na žiadosť "
                 "zhotoviteľa ruší.",
                 "Výluka v úseku Kysak – Košice dňa 20.11.2026; v dôsledku nepriaznivého počasia sa nekoná.",
                 "ŽSR OR Košice odvoláva výluku v úseku Kysak – Košice dňa 20.11.2026."):
        items = _x("Informácia o výluke", body)
        assert [e[0] for e in _ev(items)] == ["vyluka_zrusenie"], (body, _ev(items))
    # termín inej veci (pripomienky, uzávierka objednávok, kontrolná prehliadka) nie je rušený termín výluky
    for tail in ("Termín na zaslanie pripomienok 15.11.2026 sa ruší.", "Termín uzávierky objednávok trás sa ruší.",
                 "Termín kontrolnej prehliadky 18.11.2026 odpadá."):
        items = _x("ROV č. 1234/2026", KK + ". " + tail)
        assert _ev(items) == [KK_DOWN], (tail, _ev(items))


def test_e2e_spoje_nebudu_realizovane_nezrusi_overenu_vyluku(env, session):
    """R5F1 v toku pošty: nová správa „Úplná výluka … dňa 20.11. …, spoje nebudú realizované.“ výluku nezapíše ako
    zrušenie – overená výluka ostane a vlak o 10:00 nedostane VYPRAVIŤ."""
    _drop(env, "a.eml", raw_mail("ROV c. 1601/2026 - vyluka Kysak - Kosice", "oznamujeme úplnú výluku v úseku Kysak – "
                                 "Košice dňa 20.11.2026 od 08:00 do 14:00 (ROV č. 1601/2026).\n", "s1", sender=ROV_KE,
                                 date="Mon, 09 Nov 2026 10:15:00 +0100"), utc(2026, 11, 9, 9, 15))
    _process(session, utc(2026, 11, 9, 9, 17))
    _confirm_all(session)
    _drop(env, "b.eml", raw_mail("ROV c. 1601/2026 - vyluka Kysak - Kosice", KK + ", spoje nebudú realizované.\n", "s2",
                                 sender=ROV_KE, date="Tue, 10 Nov 2026 09:00:00 +0100"), utc(2026, 11, 10, 8))
    _process(session, utc(2026, 11, 10, 8, 2))
    assert not any(e.kind == "vyluka_zrusenie" for e in events(session))
    _confirm_all(session)
    assert [(c.status, as_utc(c.start_utc)) for c in _active(session)] == [("overene", utc(2026, 11, 20, 7))]
    r = _decide(session, "72002", "Kysak", "Košice", utc(2026, 11, 20, 9), utc(2026, 11, 20, 5))
    assert r.decision != "VYPRAVIŤ", r.summary


# ====================================================================== vlaky neprejdú (R5C0, kontrola druhu)
def test_vlaky_neprejdu_je_uplna_vyluka_s_poznamkou(cfg):
    """Výluka 1. TK a veta, že vlaky úsekom neprejdú („jazda vlakov nebude možná“, „vlaky nebudú premávať“, „nepôjde
    žiadny vlak“, „v úseku nepremávajú žiadne vlaky“, „všetky vlaky sú odrieknuté“, „vlaky budú odklonené“) – úplná
    výluka s poznámkou „Overte druh výluky“; vlaky podľa GVD, po 2. TK, len osobné či elektrické vlaky nie."""
    base = "Výluka 1. TK v úseku Kysak – Košice dňa 20.11.2026 od 08:00 do 14:00"
    for tail in (", jazda vlakov nebude možná.", ", jazda všetkých vlakov nebude možná.", ", vlaky nebudú premávať.",
                 ", nepôjde žiadny vlak.", ", úsekom neprejde žiadny vlak.", ", v úseku nepremávajú žiadne vlaky.",
                 ", nepremávajú vlaky.", ". Vlaky v úseku Kysak – Košice nepremávajú.", ", všetky vlaky sú odrieknuté.",
                 ", vlaky budú odklonené cez Prešov."):
        items = _x("ROV č. 1701/2026", base + tail)
        assert [i.data.get("kind") for i in items] == ["uplna"], (tail, _ev(items))
        assert "Overte druh výluky" in _notes(items), tail
    for tail, kind in ((", vlaky nepremávajú podľa GVD.", "kolaj"), (", vlaky nepôjdu po 1. TK.", "kolaj"),
                       (", vlaky budú odklonené po 2. TK.", "kolaj"), (", osobné vlaky nepremávajú, nahradí ich NAD.",
                                                                      "kolaj"),
                       (", nepremávajú vlaky osobnej dopravy.", "kolaj"), (", vlaky nie sú zrušené.", "kolaj"),
                       (", vlaky nebudú odklonené.", "kolaj"), (", jazda vlakov nebude možná bez obmedzenia.", "kolaj")):
        assert [i.data.get("kind") for i in _x("ROV č. 1701/2026", base + tail)] == [kind], tail
    for body in ("Napäťová výluka TV v úseku Kysak – Košice dňa 20.11.2026 od 08:00 do 14:00, elektrické vlaky "
                 "nepremávajú.",
                 "Výluka TV v úseku Kysak – Košice dňa 20.11.2026 od 08:00 do 14:00 – zákaz jazdy elektrických vlakov."):
        assert [i.data.get("kind") for i in _x("ROV č. 1701/2026", body)] == ["trakcne_vedenie"], body


def test_e2e_jazda_vlakov_nebude_mozna_po_potvrdeni_drzi_vlak(env, session):
    """R5C0 v toku pošty: „Výluka 1. TK …, jazda vlakov nebude možná.“ – po potvrdení je v registri úplná výluka a vlak
    o 10:00 nedostane VYPRAVIŤ."""
    _drop(env, "a.eml", raw_mail("ROV c. 1701/2026 - vyluka Kysak - Kosice", "Výluka 1. TK v úseku Kysak – Košice dňa "
                                 "20.11.2026 od 08:00 do 14:00, jazda vlakov nebude možná.\n", "c0", sender=ROV_KE,
                                 date="Mon, 09 Nov 2026 10:15:00 +0100"), utc(2026, 11, 9, 9, 15))
    _process(session, utc(2026, 11, 9, 9, 17))
    _confirm_all(session)
    assert [(c.kind, c.status) for c in _active(session)] == [("uplna", "overene")]
    r = _decide(session, "72003", "Kysak", "Košice", utc(2026, 11, 20, 9), utc(2026, 11, 20, 5))
    assert r.decision != "VYPRAVIŤ", r.summary


# ====================================================================== úsek, ktorý ostáva (R5C1)
def test_zrusenie_okrem_useku_a_kona_sa_nezrusi_usek_ktory_ostava(cfg):
    """„Rušíme výluku … okrem úseku Trnava – Senica“, „… sa ruší s výnimkou úseku Trnava – Senica“ a „…; výluka
    v úseku Trnava – Senica sa koná …“ – zrušenie nikdy nezruší úsek Trnava – Senica."""
    for body in ("Rušíme výluku podľa ROV 3360/2026 okrem úseku Trnava – Senica, kde výluka platí.",
                 "Výluka podľa ROV 3360/2026 sa ruší s výnimkou úseku Trnava – Senica.",
                 "Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší; výluka v úseku Trnava – Senica sa koná podľa "
                 "ROV 3360/2026."):
        items = _x("ROV č. 3360/2026 – zmena", body, utc(2026, 11, 16, 9), "vyluky.ortt@zsr.example")
        cancels = [i for i in items if i.kind == "vyluka_zrusenie"]
        assert cancels and all({i.data.get("from_station"), i.data.get("to_station")} != {"Trnava", "Senica"}
                               for i in cancels), (body, _ev(items))
        assert "Trnava – Senica" in _notes(cancels), body


def test_e2e_zrusenie_s_vynimkou_useku_ponecha_vyluku(env, session):
    """R5C1 v toku pošty: ROV 3360/2026 s dvoma úsekmi, potom „… sa ruší s výnimkou úseku Trnava – Senica“ – výluka
    Trnava – Senica ostane overená a vlak Trnava → Senica o 08:00 nedostane VYPRAVIŤ."""
    _drop(env, "a.eml", raw_mail("ROV 3360/2026 - vyluky na trati 116", "Dňa 09.12.2026 od 7:00 do 13:00 hod. budú "
                                 "úplné výluky v úsekoch Trnava – Senica a Senica – Kúty (ROV 3360/2026).\n", "c1a",
                                 sender=ROV_TT, date="Mon, 16 Nov 2026 10:00:00 +0100"), utc(2026, 11, 16, 9))
    _process(session, utc(2026, 11, 16, 9, 2))
    _confirm_all(session)
    _drop(env, "b.eml", raw_mail("ROV 3360/2026 - zmena", "Výluka podľa ROV 3360/2026 sa ruší s výnimkou úseku Trnava – "
                                 "Senica.\n", "c1b", sender=ROV_TT, date="Tue, 17 Nov 2026 10:00:00 +0100"),
          utc(2026, 11, 17, 9))
    _process(session, utc(2026, 11, 17, 9, 2))
    _confirm_all(session)
    assert ("Trnava", "Senica", "overene") in {(c.from_station, c.to_station, c.status) for c in _active(session)}
    r = _decide(session, "72004", "Trnava", "Senica", utc(2026, 12, 9, 7), utc(2026, 12, 9, 4))
    assert r.decision != "VYPRAVIŤ", r.summary


# ====================================================================== zákaz jazdy vlakov (R5C2)
def test_zakaz_jazdy_vlakov_miesto_trpny_rod_a_zruseny_zakaz(cfg):
    """Zákaz vjazdu či jazdy vlakov aj v trpnom rode je mimoriadnosť s miestom a termínom; „cez žst. Plaveč“ je
    miestom aj pri predmete „Oznámenie OR Košice“ (organizačná jednotka nie je miesto). Zrušený či neplatný zákaz,
    zákaz pre cestné vozidlá a zákaz len pre osobné vlaky mimoriadnosťou nie sú."""
    rec, kd = utc(2026, 12, 15, 5), "kd.orke@zsr.example"
    for subj, body, place in (
            ("Oznámenie", "Dňa 15.12.2026 od 8:00 do 14:00 hod. je zakázaný vjazd vlakov do úseku Lipany – Plaveč.",
             ("Lipany", "Plaveč")),
            ("Oznámenie", "Dňa 15.12.2026 od 8:00 do 14:00 hod. je jazda vlakov v úseku Lipany – Plaveč zakázaná.",
             ("Lipany", "Plaveč")),
            ("Oznámenie OR Košice", "Zákaz prejazdu vlakov cez žst. Plaveč dňa 15.12.2026 od 8:00 do 14:00 hod. "
             "z dôvodu opravy výhybky.", ("Plaveč", "Plaveč"))):
        items = _x(subj, body, rec, kd)
        assert _ev(items) == [("mimoriadnost", None, *place, "15.12. 08:00", "15.12. 14:00")], (body, _ev(items))
    for body in ("Zákaz jazdy nákladných vlakov v úseku Lipany – Plaveč bol dňa 15.12.2026 o 10:00 zrušený.",
                 "Od 15.12.2026 10:00 hod. sa ruší zákaz jazdy nákladných vlakov v úseku Lipany – Plaveč.",
                 "Dňa 15.12.2026 zákaz jazdy nákladných vlakov v úseku Lipany – Plaveč neplatí.",
                 "Zákaz vjazdu motorových vozidiel na parkovisko pri žst. Košice platí od 01.12.2026 do 31.12.2026.",
                 "Zákaz prejazdu cez priecestie v km 12,345 pri žst. Plaveč dňa 15.12.2026 od 8:00 do 14:00 hod. pre "
                 "cestné vozidlá.",
                 "Dňa 15.12.2026 od 8:00 do 14:00 hod. platí v úseku Lipany – Plaveč zákaz jazdy osobných vlakov "
                 "s cestujúcimi."):
        items = _x("Oznámenie OR Košice", body, rec, kd)
        assert not [i for i in items if i.kind in ("mimoriadnost", "vyluka_nova", "vyluka_zmena")], (body, _ev(items))


def test_e2e_zakaz_prejazdu_cez_stanicu_drzi_vlak_do_stanice(env, session):
    """R5C2 v toku pošty: „Zákaz prejazdu vlakov cez žst. Plaveč …“ s predmetom „Oznámenie OR Košice“ – mimoriadnosť
    v Plavči (nie v Košiciach): vlak Lipany → Plaveč o 10:00 nedostane VYPRAVIŤ ZELENÁ ani pred potvrdením."""
    _drop(env, "a.eml", raw_mail("Oznamenie OR Kosice", "Zákaz prejazdu vlakov cez žst. Plaveč dňa 15.12.2026 od 8:00 "
                                 "do 14:00 hod. z dôvodu opravy výhybky.\n", "c2", sender=KD_KE,
                                 date="Tue, 15 Dec 2026 06:00:00 +0100"), utc(2026, 12, 15, 5))
    _process(session, utc(2026, 12, 15, 5, 2))
    (ev,) = events(session)
    assert (ev.kind, ev.data.get("from_station")) == ("mimoriadnost", "Plaveč")
    r = _decide(session, "77101", "Lipany", "Plaveč", utc(2026, 12, 15, 9), utc(2026, 12, 15, 5, 5))
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), r.summary


# ====================================================================== sieť proti strate obsahu
NONOP = [
    ("Oznámenie – pokladne", "Oznamujeme, že pokladne v žst. Košice budú dňa 24.12.2026 uzavreté."),
    ("Oznámenie", "Budova OR Košice bude dňa 24.12.2026 uzavretá."),
    ("Parkovisko", "Parkovisko pri žst. Žilina bude od 01.12.2026 do 15.12.2026 uzavreté z dôvodu opravy."),
    ("Upozornenie BOZP", "Osoby bez platného preukazu nesmú od 01.12.2026 vstupovať do koľajiska."),
    ("Oznámenie", "Výťah v žst. Košice je od 20.11.2026 mimo prevádzky, cestujúci nesmú používať nástupište 3."),
    ("Úradné hodiny", "Podateľňa OR Žilina bude dňa 23.12.2026 uzavretá od 12:00 hod."),
    ("Čakáreň", "Čakáreň v žst. Poprad-Tatry bude od 01.12.2026 uzavretá z dôvodu rekonštrukcie."),
    ("Oznámenie obce", "Cestný most pri žst. Kraľovany bude dňa 05.12.2026 uzavretý pre cestnú dopravu."),
    ("Školenie", "Školenie k predpisu Z1 sa uskutoční 25.11.2026 o 9:00 v zasadačke OR Košice; mobilné telefóny nesmú "
                 "byť zapnuté."),
    ("Cestovné doklady", "Od 01.01.2027 nesmú byť predávané papierové cestovné lístky v žst. Bratislava hl. st."),
]


def test_siet_proti_strate_nezachyti_neprevadzkove_oznamy(cfg):
    """Sieť proti strate obsahu nezapíše výluku ani mimoriadnosť z oznamu o budove, pokladniach, parkovisku, čakárni,
    cestnom moste, BOZP či lístkoch (poistka pre celý vozový park a DÔLEŽITÉ upozornenie by boli falošné) – skutočné
    prevádzkové vety, ktoré pravidlá inak nezachytia, zachytí ďalej."""
    for subj, body in NONOP:
        items = _x(subj, body, sender="info.orke@zsr.example")
        assert not [i for i in items if i.kind in ("vyluka_nova", "mimoriadnost", "vyluka_zmena")], (body, _ev(items))
    for body in ("Dňa 05.12.2026 od 8:00 do 14:00 hod. nesmú vlaky vchádzať do žst. Kraľovany.",
                 "Dňa 05.12.2026 od 8:00 do 14:00 hod. bude trať Kraľovany – Ružomberok uzavretá.",
                 "Dňa 05.12.2026 od 8:00 do 14:00 hod. v úseku Kraľovany – Ružomberok nepremávajú vlaky."):
        items = _x("Oznámenie", body, sender="info.orke@zsr.example")
        assert [i for i in items if i.kind in ("vyluka_nova", "mimoriadnost")], body


def test_e2e_parkovisko_pri_stanici_nezastavi_vlaky(env, session):
    """Oznam „Parkovisko pri žst. Žilina bude … uzavreté“ nezapíše výluku stanice Žilina – vlak Žilina → Vrútky
    ostane VYPRAVIŤ ZELENÁ a e-mail je len „iné“."""
    _drop(env, "a.eml", raw_mail("Parkovisko", "Parkovisko pri žst. Žilina bude od 01.12.2026 do 15.12.2026 uzavreté "
                                 "z dôvodu opravy.\n", "net1", sender="ZSR OR Zilina <sekretariat.orza@zsr.example>",
                                 date="Sun, 06 Dec 2026 07:50:00 +0100"), utc(2026, 12, 6, 6, 50))
    _process(session, utc(2026, 12, 6, 6, 52))
    assert [e.kind for e in events(session)] == ["ine"]
    r = _decide(session, "79004", "Žilina", "Vrútky", utc(2026, 12, 7, 9), utc(2026, 12, 7, 7))
    assert (r.decision, r.color) == ("VYPRAVIŤ", "ZELENÁ"), r.summary


# ====================================================================== prehľad, KD, tabuľka (R5C5, R5C6)
def test_rov_ktory_prehlad_len_spomina_je_vyluka(cfg):
    """ROV, ktorý informatívny prehľad len spomína („V prílohe zasielame aj informatívny prehľad výluk …“), je
    výluka; e-mail, ktorý sám je prehľadom, ostáva „iné“."""
    items = _x("ROV č. 4120/2026 – výluka Senec – Galanta", "Úplná výluka v úseku Senec – Galanta dňa 23.11.2026 od "
               "08:00 do 12:00 hod. (ROV č. 4120/2026).\nV prílohe zasielame aj informatívny prehľad výluk na 48. "
               "týždeň.", utc(2026, 11, 16, 8), "vyluky.orba@zsr.example")
    assert _ev(items) == [("vyluka_nova", "uplna", "Senec", "Galanta", "23.11. 08:00", "23.11. 12:00")]
    items = _x("Informatívny prehľad výluk OR Bratislava – 48. týždeň", "zasielame informatívny prehľad výluk:\n"
               "- Senec – Galanta 23.11.2026 08:00 – 12:00 (ROV 4121/2026)\n\nPrehľad je informatívny, záväzné sú "
               "údaje uvedené v ROV.", utc(2026, 11, 16, 8), "vyluky.orba@zsr.example")
    assert [i.kind for i in items] == ["ine"]


def test_kd_len_s_casom_buduce_okno_je_dnes(cfg):
    """KD len s časom: budúce okno („Dnes od 14:00 do 18:00 hod. bude …“, „Od 14:00 do 18:00 hod. bude …“, „V čase
    14:00 – 18:00 …“) je v deň prijatia, nie včera; nočná mimoriadnosť ohlásená po polnoci („Od 23:40 hod. je …“)
    patrí predchádzajúcemu dňu; „zajtra“ je nasledujúci deň."""
    rec, kd = utc(2026, 12, 6, 8), "kd.orke@zsr.example"
    for body in ("Dnes od 14:00 do 18:00 hod. bude žst. Fiľakovo neprejazdná z dôvodu opravy výhybky č. 4.",
                 "Od 14:00 do 18:00 hod. bude žst. Fiľakovo neprejazdná z dôvodu opravy výhybky č. 4.",
                 "V čase 14:00 – 18:00 hod. bude v žst. Fiľakovo prerušená doprava z dôvodu opravy výhybky."):
        items = _x("Mimoriadnosť – žst. Fiľakovo", body, rec, kd)
        assert _ev(items) == [("mimoriadnost", None, "Fiľakovo", "Fiľakovo", "06.12. 14:00", "06.12. 18:00")], body
    items = _x("Mimoriadnosť – žst. Fiľakovo", "Od 23:40 hod. je v žst. Fiľakovo porucha zabezpečovacieho zariadenia, "
               "stanica je neprejazdná.", utc(2026, 12, 5, 23, 10), kd)
    assert [(e[4], e[5]) for e in _ev(items)] == [("05.12. 23:40", None)]
    assert "predchádzajúci deň" in _notes(items)
    items = _x("Mimoriadnosť – žst. Fiľakovo", "Zajtra od 08:00 do 12:00 hod. bude žst. Fiľakovo neprejazdná.", rec, kd)
    assert [(e[4], e[5]) for e in _ev(items)] == [("07.12. 08:00", "07.12. 12:00")]


def test_e2e_kd_buduce_okno_drzi_vlak(env, session):
    """KD „Dnes od 14:00 do 18:00 hod. bude žst. Fiľakovo neprejazdná …“ prijatý o 09:00 – vlak cez Fiľakovo o 15:00
    nedostane VYPRAVIŤ (pred potvrdením ani po ňom)."""
    _drop(env, "a.eml", raw_mail("Mimoriadnost - zst. Filakovo", "Dnes od 14:00 do 18:00 hod. bude žst. Fiľakovo "
                                 "neprejazdná z dôvodu opravy výhybky č. 4.\n", "kd1", sender=KD_KE,
                                 date="Sun, 06 Dec 2026 09:00:00 +0100"), utc(2026, 12, 6, 8))
    _process(session, utc(2026, 12, 6, 8, 2))
    r = _decide(session, "70011", "Lučenec", "Fiľakovo", utc(2026, 12, 6, 14), utc(2026, 12, 6, 8, 5))
    assert r.decision != "VYPRAVIŤ", r.summary
    _confirm_all(session)
    assert [(as_utc(c.start_utc), as_utc(c.end_utc)) for c in _active(session)] == [
        (utc(2026, 12, 6, 13), utc(2026, 12, 6, 17))]


TABLE_HEAD = "oznamujeme nasledovné výluky podľa ROV č. 3207/2026:\n\n"


def test_tabulka_stlpec_terminu_s_rozsahom(cfg):
    """Stĺpec „Termín“/„Dátum“ s celým termínom („24.11. – 26.11.2026“ s denným časom, „24.11.2026 08:00 – 25.11.2026
    14:00“, „24.11.2026 od 08:00 do 14:00“) – celý termín, nie len prvý deň; „Dátum | Od | Do“ ostáva."""
    rec = utc(2026, 11, 16, 8)
    for table, want in (
            ("Úsek | Termín | Čas\nMalacky – Kúty | 24.11. – 26.11.2026 | 08:00 – 14:00\n",
             ("24.11. 08:00", "26.11. 14:00", "08:00", "14:00")),
            ("Úsek | Termín | Druh\nMalacky – Kúty | 24.11.2026 08:00 – 25.11.2026 14:00 | úplná\n",
             ("24.11. 08:00", "25.11. 14:00", None, None)),
            ("Úsek | Termín | Druh\nMalacky – Kúty | 24.11.2026 od 08:00 do 14:00 | úplná\n",
             ("24.11. 08:00", "24.11. 14:00", None, None)),
            ("Úsek | Dátum | Od | Do\nMalacky – Kúty | 24.11.2026 | 22:00 | 05:00\n",
             ("24.11. 22:00", "25.11. 05:00", None, None))):
        items = _x("ROV č. 3207/2026 – výluky", TABLE_HEAD + table, rec, "vyluky.orba@zsr.example")
        got = [(_loc(i.data.get("start")), _loc(i.data.get("end")), i.data.get("daily_from"), i.data.get("daily_to"))
               for i in items]
        assert got == [want], (table, got)


def test_e2e_tabulka_termin_rozsah_drzi_vlak_v_druhy_den(env, session):
    """Tabuľka „Úsek | Termín | Čas“ s „24.11. – 26.11.2026 | 08:00 – 14:00“ – po potvrdení vlak Malacky → Kúty
    25.11. o 10:00 nedostane VYPRAVIŤ."""
    _drop(env, "a.eml", raw_mail("ROV c. 3207/2026 - vyluky", TABLE_HEAD + "Úsek | Termín | Čas\nMalacky – Kúty | "
                                 "24.11. – 26.11.2026 | 08:00 – 14:00\n", "tab1", sender=ROV_BA,
                                 date="Mon, 16 Nov 2026 09:00:00 +0100"), utc(2026, 11, 16, 8))
    _process(session, utc(2026, 11, 16, 8, 2))
    _confirm_all(session)
    r = _decide(session, "70002", "Malacky", "Kúty", utc(2026, 11, 25, 9), utc(2026, 11, 25, 5))
    assert r.decision != "VYPRAVIŤ", r.summary


# ====================================================================== nákladné vlaky, názvy staníc
def test_nakladne_vlaky_az_od_casu_a_obmedzenie_rychlosti(cfg):
    """Po obnovení prevádzky: „Nákladné vlaky pôjdu / premávajú / sa budú vypravovať od 15:00“, „Nákladná doprava od
    15:00“ – mimoriadnosť do 15:00; obmedzenie rýchlosti pre nákladné vlaky je pomalá jazda, nie zákaz ich jazdy."""
    rec, kd = utc(2026, 11, 9, 9, 35), "kd.orza@zsr.example"
    head = "Mimoriadnosť v úseku Kraľovany – Ružomberok (strhnuté trakčné vedenie) – prevádzka bola o 10:30 obnovená " \
           "pre osobné vlaky. "
    for tail in ("Nákladné vlaky pôjdu od 15:00.", "Nákladné vlaky premávajú od 15:00.", "Nákladná doprava od 15:00.",
                 "Nákladné vlaky sa budú vypravovať od 15:00."):
        items = _x("Mimoriadnosť – Kraľovany – Ružomberok", head + tail, rec, kd)
        assert _ev(items) == [("mimoriadnost", None, "Kraľovany", "Ružomberok", "09.11. 10:30", "09.11. 15:00")], tail
    for tail in ("Pre nákladné vlaky platí do 18:00 obmedzenie rýchlosti na 40 km/h.",
                 "Nákladné vlaky premávajú obmedzenou rýchlosťou 40 km/h."):
        items = _x("Mimoriadnosť", "Prevádzka v úseku Kraľovany – Ružomberok bola o 10:30 obnovená. " + tail, rec, kd)
        assert [i.kind for i in items] == ["ine", "pomala_jazda"], (tail, _ev(items))


def test_nazvy_stanic_skratky_druhy_pad_a_dopravna(cfg):
    """Poznámka ku skratke názvu („N. Zámky“) hovorí o skratke, nie o skloňovanom tvare; „do Vrútok“, „zo Šurian“ sú
    názvy staníc; „a dopravne Vrútky“ je ďalšia výluka stanice; „okrem stanice Ružomberok“ výlukou stanice nie je."""
    items = _x("ROV č. 5001/2026", "Úplná výluka v úseku N. Zámky (mimo) – Šurany (mimo) dňa 11.12.2026 od 8:00 do "
               "12:00 hod.")
    assert "„N. Zámky“ je skratka názvu dopravne → Nové Zámky" in _notes(items)
    assert "skloňovaného" not in _notes(items)
    items = _x("ROV č. 1424/2026", "Úplná výluka v úseku zo Žiliny do Vrútok dňa 24.11.2026 od 08:00 do 14:00 hod.")
    assert [(_name(i.data.get("from_station")), _name(i.data.get("to_station"))) for i in items] == [("Žilina", "Vrútky")]
    items = _x("ROV č. 1424/2026", "Úplná výluka trate Kraľovany – Ružomberok a dopravne Vrútky dňa 24.11.2026 od 08:00 "
               "do 14:00 hod.")
    assert sorted((i.data.get("from_station"), i.data.get("to_station")) for i in items) == [
        ("Kraľovany", "Ružomberok"), ("Vrútky", "Vrútky")]
    items = _x("ROV č. 1424/2026", "Úplná výluka trate Kraľovany – Ružomberok okrem stanice Ružomberok dňa 24.11.2026 "
               "od 08:00 do 14:00 hod.")
    assert [(i.data.get("from_station"), i.data.get("to_station")) for i in items] == [("Kraľovany", "Ružomberok")]
