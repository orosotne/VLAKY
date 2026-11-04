"""Štvrtá revízia – pravidlá e-mailov ŽSR (oblasť M4): regresné testy.

- druh obmedzenia: výluka (ROV) sa nezmení na pomalú jazdu len preto, že text spomína pomalú jazdu; úplná výluka sa
  posudzuje pred trakčným vedením a koľajou; výluka koľaje bez čísla na jednokoľajnej trati je úplná výluka (QM0, QC1);
- miesto: výluka či mimoriadnosť v stanici („v žst. X“, „stanica X je neprejazdná“, „v Kraľovanoch“) má od = do = X a
  iný úsek v e-maile (náhradná doprava, odklon, prejazdný úsek) miestom nie je (QM1, QM2, QC2);
- zoznam termínov či úsekov („1)“, „a)“, „-“, „Termíny: … a …“) dá udalosť pre každý termín a úsek, nepriradený
  termín je v poznámke (QC0);
- citovaný pôvodný ROV v prílohe odpovede či v preposlanej odpovedi nie je druhou zmenou so starým termínom (QM3, QC3);
- zrušenie jedného dňa viacdennej výluky nezruší celú výluku (QC4); zrušenie POTR nezruší výluku iného druhu
  s rovnakým číslom (QM4);
- čiastočné obnovenie prevádzky so zákazom pre nákladné vlaky nie je koniec mimoriadnosti (QM5);
- automatická odpoveď a pozvánka nie sú výluka, zrušenie napísané v texte je zrušenie, podpis „oddelenie výluk“
  pomalú jazdu nezmení na výluku (QC5); avízo „príchod do Čadce DD.MM.RRRR o HH:MM“ (QC6);
- čakajúca výluka bez známej dopravne dostane upozornenie a prepočet (MR2/PR0 zvyšok).

Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
from sqlalchemy import select

from vlaky.cas import set_fake_now
from vlaky.hranice import seed_borders
from vlaky.models import Closure, Notification, Path as TrainPath, Train
from test_revizia3_terminy import as_utc, drop, events, iso, raw_mail, rows, rules, utc

KD_ZA = "Kontrolny dispecer OR Zilina <kd.orza@zsr.example>"
ROV_KE = "ZSR OR Kosice <vyluky.orke@zsr.example>"
ROV_ZA = "ZSR OR Zilina <vyluky.orza@zsr.example>"
AR_ZSR = ("mx.dopravca.example; spf=pass smtp.mailfrom=zsr.example; dkim=pass header.d=zsr.example; "
          "dmarc=pass header.from=zsr.example")


@pytest.fixture()
def env(cfg, session, monkeypatch):
    """Overenie odosielateľa ako v prevádzke (DMARC, dôveryhodný server) a register priechodov."""
    monkeypatch.setattr(cfg, "require_dmarc_pass", True)
    monkeypatch.setattr(cfg, "trusted_authserv_ids", "mx.dopravca.example")
    seed_borders(session)
    session.commit()
    return cfg


def _train(session, number: str, origin: str, destination: str, dep: datetime, traction: str = "E",
           hours: int = 8) -> Train:
    t = Train(number=number, origin=origin, destination=destination, traction=traction, gauge="1435", vmax_kmh=80,
              status="planovany", ready_utc=dep, deadline_utc=dep + timedelta(hours=hours))
    session.add(t)
    session.flush()
    session.add(TrainPath(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena", departure_utc=dep))
    session.commit()
    return t


def _decide(session, number: str, origin: str, destination: str, dep: datetime, now: datetime,
            traction: str = "E"):
    from vlaky.agent.zdravie import set_kv
    from vlaky.planovanie.planovac import evaluate_train

    t = _train(session, number, origin, destination, dep, traction)
    set_kv(session, "mail_last_ok", {"ts": (now - timedelta(minutes=2)).isoformat(), "fetched": 0})
    session.commit()
    return evaluate_train(session, t.id, now, "test M4")


def _mail(subject: str, body: str, mid: str, sender: str, date: str, attachments=(), headers=None) -> bytes:
    msg = EmailMessage()
    msg["Authentication-Results"] = AR_ZSR
    msg["From"] = sender
    msg["To"] = "agent.dispecing@dopravca.example"
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = f"<{mid}@zsr.example>"
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    for name, data, maintype, subtype in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return bytes(msg)


def _drop_at(cfg, name: str, data: bytes, received: datetime) -> None:
    p = Path(cfg.mail_dir) / name
    p.write_bytes(data)
    os.utime(p, (received.timestamp(), received.timestamp()))


def _process(session, now: datetime) -> dict:
    from vlaky.posta.spracovanie import process_mailbox

    set_fake_now(now)
    s = process_mailbox(session, now=now)
    session.commit()
    return s


def _confirm_all(session, evs) -> None:
    from vlaky.posta.spracovanie import confirm_event

    for e in evs:
        if e.status == "navrh":
            confirm_event(session, e.id, "dispecer1")
            session.commit()


def _section(it) -> tuple:
    return it.data.get("from_station"), it.data.get("to_station")


# ====================================================================== QM0 výluka so zmienkou o pomalej jazde
ROV_1160 = "Rozkaz o výluke č. 1160/2026: výluka v úseku Kysak – Košice 20.11.2026 od 8:00 do 14:00"


@pytest.mark.parametrize("tail", [
    ", po výluke platí do odvolania pomalá jazda 50 km/h.",
    ". Pomalá jazda podľa POTR č. 12/2026 ostáva v platnosti.",
    ". Po skončení výluky platí v úseku pomalá jazda 30 km/h do 30.11.2026.",
])
def test_qm0_rov_so_zmienkou_o_pomalej_jazde_ostava_uplnou_vylukou(cfg, tail):
    """QM0: ROV bez slov o koľaji (predvolene úplná výluka) ostáva výlukou, aj keď spomína pomalú jazdu po výluke
    alebo POTR, ktorá ostáva – nie pomalá jazda (VYPRAVIŤ ZELENÁ do výluky)."""
    items = rules(ROV_1160 + tail + "\n", subject="ROV c. 1160/2026 Kysak - Kosice")
    new = items[0]
    assert (new.kind, new.data["kind"]) == ("vyluka_nova", "uplna"), [(i.kind, i.data) for i in items]
    assert _section(new) == ("Kysak", "Košice")
    assert (new.data["start"], new.data["end"]) == (iso(2026, 11, 20, 7), iso(2026, 11, 20, 13))
    assert not any(i.kind == "pomala_jazda" and _section(i) == ("Kysak", "Košice") for i in items)


def test_qm0_pomala_jazda_pocas_vyluky_na_susednom_useku_je_samostatna(cfg):
    """QM0: „Počas výluky platí v úseku Kysak – Kostoľany … pomalá jazda 30 km/h“ – výluka Kysak – Košice ostáva
    úplnou výlukou, pomalá jazda je samostatná udalosť na svojom úseku."""
    items = rules(ROV_1160 + ". Počas výluky platí v úseku Kysak – Kostoľany nad Hornádom pomalá jazda 30 km/h.\n",
                  subject="ROV c. 1160/2026 Kysak - Kosice")
    kinds = {(i.kind, i.data.get("kind"), *_section(i)) for i in items}
    assert ("vyluka_nova", "uplna", "Kysak", "Košice") in kinds
    pj = next(i for i in items if i.kind == "pomala_jazda")
    assert _section(pj) == ("Kysak", "Kostoľany nad Hornádom") and pj.data["speed_kmh"] == 30


def test_qm0_note_o_pomalej_jazde_po_vyluke(cfg):
    """QM0 + princíp (b): pomalá jazda po výluke, ktorá nie je samostatnou udalosťou, je v poznámke výluky."""
    (it,) = rules(ROV_1160 + ", po výluke platí do odvolania pomalá jazda 50 km/h.\n",
                  subject="ROV c. 1160/2026 Kysak - Kosice")
    assert it.data["kind"] == "uplna"
    assert any("pomalá jazda" in n and "50 km/h" in n for n in it.notes), it.notes


@pytest.mark.parametrize("tail,expect", [
    (" Zároveň sa ruší pomalá jazda zavedená TLG č. 37/2026.", ["vyluka_zmena", "vyluka_zrusenie"]),
    (" Po skončení výluky platí v úseku pomalá jazda 50 km/h do odvolania.", ["vyluka_zmena"]),
])
def test_qm0_zmena_rov_so_zmienkou_o_pomalej_jazde(cfg, tail, expect):
    """QM0: „Zmena ROV …“, ktorá ruší aj pomalú jazdu či spomína pomalú jazdu po výluke, je zmena výluky (druh
    úplná) – nie zmena pomalej jazdy, ktorá by sa k ROV v registri nepriradila. Zrušenie TLG je zrušenie POMALEJ
    JAZDY (nie výluky s rovnakým číslom)."""
    items = rules("Oznamujeme zmenu Rozkazu o výluke č. 1142/2026: výluka v úseku Kysak – Košice sa presúva na "
                  "22.11.2026 od 8:00 do 14:00." + tail + "\n", subject="Zmena ROV c. 1142/2026")
    assert [i.kind for i in items] == expect
    change = items[0]
    assert (change.data["kind"], change.data["external_ref"]) == ("uplna", "1142/2026")
    assert (change.data["start"], change.data["end"]) == (iso(2026, 11, 22, 7), iso(2026, 11, 22, 13))
    if len(items) > 1:
        assert (items[1].data["external_ref"], items[1].data["kind"]) == ("37/2026", "pomala_jazda")


def test_qm0_zmena_rov_end_to_end_presunie_vyluku(env, session):
    """QM0 end to end: zmena ROV 1142/2026 so zrušením pomalej jazdy sa priradí k úplnej výluke v registri (dočasný
    záznam „navrh“ zmeny), po potvrdení je výluka na 22.11. a vlak v ten deň nejde do výluky."""
    c = Closure(kind="uplna", from_station="Kysak", to_station="Košice", start_utc=utc(2026, 11, 12, 7),
                end_utc=utc(2026, 11, 12, 13), status="overene", source="email", external_ref="1142/2026")
    session.add(c)
    session.commit()
    now = utc(2026, 11, 2, 4)
    drop(env, "zmena.eml", raw_mail("Zmena ROV c. 1142/2026", "Oznamujeme zmenu Rozkazu o výluke č. 1142/2026: výluka "
                                    "v úseku Kysak – Košice sa presúva na 22.11.2026 od 8:00 do 14:00. Zároveň sa ruší "
                                    "pomalá jazda zavedená TLG č. 37/2026.\n", "zmena1142", sender=ROV_KE))
    _process(session, now)
    evs = events(session)
    change = next(e for e in evs if e.kind == "vyluka_zmena")
    assert change.data["kind"] == "uplna"
    temp = session.get(Closure, change.validation["pending_closure"]["id"])
    assert temp.proposed_change_of == c.id and temp.kind == "uplna"
    _confirm_all(session, [change])
    session.refresh(c)
    assert (as_utc(c.start_utc), c.kind, c.status) == (utc(2026, 11, 22, 7), "uplna", "overene")
    r = _decide(session, "77540", "Kysak", "Košice", utc(2026, 11, 22, 8), utc(2026, 11, 22, 5))
    assert r.decision != "VYPRAVIŤ", (r.decision, r.summary)


# ====================================================================== QC1 úplná výluka nie je priechodná
@pytest.mark.parametrize("body", [
    "podľa ROV č. 451/2026 bude vykonaná výluka traťovej koľaje v medzistaničnom úseku\nZvolen osobná stanica – Banská "
    "Bystrica (trať 170, jednokoľajná trať) dňa 18.11.2026 od 08:00 do 14:00 hod.\nPočas výluky je doprava v úseku "
    "prerušená, obchádzka nie je možná.",
    "výluka traťovej koľaje v úseku Púchov – Lúky pod Makytou (trať 125) dňa 18.11.2026 od 08:00 do 14:00 hod. (ROV č. "
    "1198/2026). Vlaky v úseku nepôjdu.",
    "Úplná výluka oboch traťových koľají vrátane vypnutia trakčného vedenia v úseku Štrba – Poprad-Tatry dňa 24.11.2026 "
    "od 08:00 do 14:00 hod. (ROV č. 1700/2026).",
    "Výluka oboch traťových koľají a TV v úseku Štrba – Poprad-Tatry dňa 24.11.2026 od 08:00 do 14:00 hod. Úsek je "
    "neprejazdný pre všetky vlaky. (ROV č. 1701/2026)",
    "Výluka 1. a 2. TK v úseku Štrba – Poprad-Tatry dňa 24.11.2026 od 08:00 do 14:00 hod. (ROV č. 1702/2026)",
], ids=["jednokolajna_trat", "vlaky_nepojdu", "uplna_s_tv", "oboch_tk_a_tv", "prva_a_druha_tk"])
def test_qc1_uplna_vyluka_nie_je_vyluka_kolaje_ani_tv(cfg, body):
    """QC1: výluka traťovej koľaje na jednokoľajnej trati (doprava prerušená, vlaky nepôjdu) a úplná výluka oboch
    koľají aj s trakčným vedením je ÚPLNÁ výluka – nie „kolaj“ (malé zdržanie) ani „trakcne_vedenie“ (motorový vlak
    by prešiel)."""
    (it,) = rules("Vážení dopravcovia,\n\n" + body + "\n", subject="ROV – výluka")
    assert (it.kind, it.data["kind"]) == ("vyluka_nova", "uplna"), it.data


def test_qc1_vyluka_kolaje_bez_cisla_je_predpokladana_uplna(cfg):
    """QC1: výluka koľaje bez čísla koľaje – na jednokoľajnej trati je to celá trať: prísnejší prípad s poznámkou
    „overte druh“; s číslom koľaje či „jednokoľajná prevádzka“ ostáva výlukou koľaje, napäťová výluka TV oboch koľají
    ostáva trakčným vedením."""
    (it,) = rules("Výluka traťovej koľaje v úseku Púchov – Lúky pod Makytou dňa 18.11.2026 od 08:00 do 14:00 hod.")
    assert it.data["kind"] == "uplna" and any("bez čísla koľaje" in n for n in it.notes), (it.data, it.notes)
    assert it.confidence < 0.85
    (tk,) = rules("Výluka 1. TK v úseku Kysak – Košice dňa 18.11.2026 od 08:00 do 14:00, vlaky budú vedené po 2. TK.")
    assert tk.data["kind"] == "kolaj" and tk.data["track"] == "1"
    (op,) = rules("Výluka koľaje, jednokoľajná prevádzka, v úseku Kysak – Košice dňa 18.11.2026 od 08:00 do 14:00.")
    assert op.data["kind"] == "kolaj"
    (tv,) = rules("Napäťová výluka TV, obe traťové koľaje, v úseku Žilina – Čadca dňa 18.11.2026 od 08:00 do 14:00. "
                  "Vlaky s motorovou trakciou môžu úsekom prejsť.")
    assert tv.data["kind"] == "trakcne_vedenie"


def test_qc1_end_to_end_motorovy_vlak_cez_uplnu_vyluku_s_tv(env, session):
    """QC1 end to end: úplná výluka oboch TK vrátane vypnutia TV – motorový vlak počas výluky nesmie ísť (VYPRAVIŤ
    ZELENÁ cez uzavretý úsek)."""
    drop(env, "rov.eml", raw_mail("ROV c. 1700/2026 - uplna vyluka Strba - Poprad-Tatry", "Úplná výluka oboch traťových "
                                  "koľají vrátane vypnutia trakčného vedenia v úseku Štrba – Poprad-Tatry dňa 24.11.2026 "
                                  "od 08:00 do 14:00 hod. (ROV č. 1700/2026).\n", "rov1700", sender=ROV_KE))
    _process(session, utc(2026, 11, 10, 8))
    (row,) = rows(session)
    assert (row.kind, row.status, row.electric_only) == ("uplna", "navrh", False)
    r = _decide(session, "77560", "Štrba", "Poprad-Tatry", utc(2026, 11, 24, 9), utc(2026, 11, 24, 4), traction="M")
    assert r.decision != "VYPRAVIŤ", (r.decision, r.summary)


# ====================================================================== QC2 výluka v stanici
@pytest.mark.parametrize("place", ["v žst. Ružomberok", "v stanici Ružomberok", "v železničnej stanici Ružomberok",
                                   "v ŽST Ružomberok", "v obvode stanice Ružomberok"])
def test_qc2_vyluka_v_stanici_ma_dopravnu(cfg, place):
    """QC2: výluka určená len stanicou má od = do = stanica (nie výluka bez dopravne, ktorú plánovač nevidí)."""
    (it,) = rules(f"Vážení,\n\núplná výluka všetkých staničných koľají {place} v noci z 3. na 4.12.2026 od 23:30 do "
                  "03:30 hod.\nStanica bude pre všetky vlaky neprejazdná (ROV č. 1199/2026, rekonštrukcia zhlavia).\n\n"
                  "S pozdravom\nOR Žilina\n", subject="ROV č. 1199/2026 – úplná výluka", received=utc(2026, 11, 7, 9))
    assert (it.kind, it.data["kind"]) == ("vyluka_nova", "uplna")
    assert _section(it) == ("Ružomberok", "Ružomberok")
    assert (it.data["start"], it.data["end"]) == (iso(2026, 12, 3, 22, 30), iso(2026, 12, 4, 2, 30))


def test_qc2_end_to_end_vyluka_v_stanici_blokuje_vlak(env, session):
    """QC2 end to end: výluka v žst. Ružomberok dostane záznam „navrh“ (od = do) a vlak cez Ružomberok počas výluky
    nedostane VYPRAVIŤ."""
    drop(env, "rov.eml", raw_mail("ROV c. 1199/2026 - uplna vyluka v zst. Ruzomberok", "Úplná výluka všetkých staničných "
                                  "koľají v žst. Ružomberok v noci z 3. na 4.12.2026 od 23:30 do 03:30 hod. Stanica "
                                  "bude pre všetky vlaky neprejazdná (ROV č. 1199/2026).\n", "rov1199", sender=ROV_ZA,
                                  date="Sat, 07 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 7, 9, 5))
    (row,) = rows(session)
    assert (row.from_station, row.to_station, row.kind, row.status) == ("Ružomberok", "Ružomberok", "uplna", "navrh")
    r = _decide(session, "77520", "Kraľovany", "Liptovský Mikuláš", utc(2026, 12, 3, 23, 10), utc(2026, 12, 3, 19))
    assert r.decision != "VYPRAVIŤ", (r.decision, r.summary)


# ====================================================================== QM1 / QM2 miesto mimoriadnosti
INC = ("Mimoriadnosť v ŽST Žilina: od 02.11.2026 04:30 do 02.11.2026 13:00 porucha zabezpečovacieho zariadenia, "
       "stanica je neprejazdná.")


@pytest.mark.parametrize("tail", [" Osobné vlaky v úseku Žilina – Čadca sú nahradené autobusmi.",
                                  " Úsek Kraľovany – Ružomberok je už prejazdný.",
                                  " Vlaky smer Košice sú odklonené cez Kraľovany – Ružomberok po 1. koľaji."],
                         ids=["nad", "iny_usek_prejazdny", "odklon"])
def test_qm1_mimoriadnost_v_stanici_nie_je_na_inom_useku(cfg, tail):
    """QM1: miesto mimoriadnosti je zo vety o mimoriadnosti (ŽST Žilina) – nie úsek náhradnej autobusovej dopravy,
    odklonu či iného, už prejazdného úseku."""
    (it,) = rules(INC + tail + "\n", subject="Mimoriadnosť ŽST Žilina")
    assert it.kind == "mimoriadnost" and _section(it) == ("Žilina", "Žilina"), it.data
    assert (it.data["start"], it.data["end"]) == (iso(2026, 11, 2, 3, 30), iso(2026, 11, 2, 12))


def test_qm1_usek_mimoriadnosti_nie_je_stanica_s_cakajucimi_vlakmi(cfg):
    """QM1 poistka: „porucha TV v úseku Kraľovany – Ružomberok …, vlaky čakajú v žst. Kraľovany“ – miestom je úsek
    z vety o poruche, nie stanica, kde vlaky čakajú; ŽST v tej istej časti vety ako iný úsek – stanica a poznámka."""
    (it,) = rules("Porucha trakčného vedenia v úseku Kraľovany – Ružomberok od 02.11.2026 04:30, vlaky čakajú v žst. "
                  "Kraľovany.", subject="Mimoriadnosť")
    assert _section(it) == ("Kraľovany", "Ružomberok")
    (st,) = rules("Mimoriadnosť v ŽST Žilina, porucha výhybky v úseku Žilina – Varín od 02.11.2026 04:30.",
                  subject="Mimoriadnosť")
    assert _section(st) == ("Žilina", "Žilina") and any("Žilina – Varín" in n for n in st.notes), st.notes
    assert st.confidence < 0.85


@pytest.mark.parametrize("subject,body,station", [
    ("Mimoriadnosť Kraľovany", "Mimoriadnosť: stanica Kraľovany je neprejazdná (porucha zabezpečovacieho zariadenia) "
     "od 02.11.2026 04:30 do 02.11.2026 13:00.", "Kraľovany"),
    ("Mimoriadnosť", "Žst. Kraľovany je neprejazdná od 02.11.2026 04:30 do 02.11.2026 13:00 – porucha "
     "zabezpečovacieho zariadenia.", "Kraľovany"),
    ("Mimoriadnosť", "Prerušenie dopravy v Kraľovanoch od 02.11.2026 04:30 do 02.11.2026 13:00 pre poruchu "
     "zabezpečovacieho zariadenia.", "Kraľovany"),
    ("Mimoriadnosť", "V Ružomberku je porucha zabezpečovacieho zariadenia od 02.11.2026 04:30, stanica je neprejazdná.",
     "Ružomberok"),
    ("Mimoriadnosť Kraľovany", "Od 02.11.2026 04:30 stanica neprejazdná, porucha výhybky.", "Kraľovany"),
    ("Mimoriadnosť", "ŽST Kraľovany – neprejazdná od 02.11.2026 04:30 do 02.11.2026 13:00, porucha.", "Kraľovany"),
], ids=["stanica_je", "zst_je", "lokal", "lokal_ruzomberok", "predmet", "zst_pomlcka"])
def test_qm2_mimoriadnost_v_stanici_bez_predlozky(cfg, subject, body, station):
    """QM2: „stanica X je neprejazdná“, „Žst. X je …“, „v Kraľovanoch“, „ŽST X – …“ a miesto len v predmete dajú
    dopravňu X (od = do) – nie mimoriadnosť bez miesta, ktorú plánovač nevidí."""
    (it,) = rules(body, subject=subject)
    assert it.kind == "mimoriadnost"
    from vlaky.posta.stanice import stations

    names = {stations().match(str(x)).name for x in _section(it) if x}
    assert names == {station}, it.data


def test_qm2_end_to_end_stanica_je_neprejazdna_blokuje_vlak(env, session):
    """QM2 end to end: „stanica Kraľovany je neprejazdná“ – záznam „navrh“ v dopravni Kraľovany, vlak Čadca →
    Košice cez Kraľovany v čase mimoriadnosti nedostane VYPRAVIŤ ZELENÁ."""
    now = utc(2026, 11, 2, 4)
    drop(env, "mim.eml", raw_mail("Mimoriadnost Kralovany", "Mimoriadnosť: stanica Kraľovany je neprejazdná (porucha "
                                  "zabezpečovacieho zariadenia) od 02.11.2026 04:30 do 02.11.2026 13:00.\n", "mim1",
                                  sender=KD_ZA, date="Mon, 02 Nov 2026 04:55:00 +0100"))
    _process(session, now)
    (row,) = rows(session)
    assert (row.kind, row.from_station, row.to_station, row.status) == ("mimoriadnost", "Kraľovany", "Kraľovany",
                                                                       "navrh")
    r = _decide(session, "91901", "Čadca", "Košice", now + timedelta(minutes=30), now)
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), (r.decision, r.color, r.summary)


def test_qm1_end_to_end_nahradna_doprava_neurci_miesto(env, session):
    """QM1 end to end: záznam „navrh“ je ŽST Žilina (nie úsek náhradnej dopravy Žilina – Čadca) a vlak Púchov →
    Vrútky cez Žilinu počas mimoriadnosti nedostane VYPRAVIŤ."""
    now = utc(2026, 11, 2, 4)
    drop(env, "inc.eml", raw_mail("Mimoriadnost ZST Zilina", INC + " Osobné vlaky v úseku Žilina – Čadca sú nahradené "
                                  "autobusmi.\n", "inc1", sender=KD_ZA, date="Mon, 02 Nov 2026 04:50:00 +0100"))
    _process(session, now)
    (row,) = rows(session)
    assert (row.from_station, row.to_station) == ("Žilina", "Žilina")
    r = _decide(session, "66101", "Púchov", "Vrútky", now + timedelta(minutes=40), now)
    assert r.decision != "VYPRAVIŤ", (r.decision, r.summary)


def test_m2_mimoriadnost_bez_dopravne_dostane_upozornenie_a_prepocet(env, session):
    """MR2 / PR0 zvyšok: čakajúca mimoriadnosť, ktorej miesto sa nedá určiť (len trať a km), dostane okamžité
    upozornenie DÔLEŽITÉ s odkazom na udalosť a spustí prepočet vlakov (môže sa týkať každého vlaku)."""
    now = utc(2026, 11, 2, 4)
    drop(env, "km.eml", raw_mail("Mimoriadnost na trati 180", "Mimoriadna udalosť: porucha koľaje na trati 180 v km "
                                 "251,4 od 02.11.2026 04:30, trať je neprejazdná.\n", "km1", sender=KD_ZA,
                                 date="Mon, 02 Nov 2026 04:55:00 +0100"))
    s = _process(session, now)
    (ev,) = events(session)
    assert ev.kind == "mimoriadnost" and not ev.data.get("from_station") and rows(session) == []
    alerts = list(session.scalars(select(Notification).where(Notification.kind == "upozornenie")))
    assert alerts and any(f"/fronta/udalost/{ev.id}" in n.body for n in alerts), [n.body for n in alerts]
    assert s["planning"] is not None


# ====================================================================== QM5 čiastočné obnovenie, nákladné vlaky
@pytest.mark.parametrize("body,end", [
    ("Mimoriadnosť v ŽST Kraľovany: prevádzka je obnovená o 09:20. Pre nákladné vlaky platí zákaz jazdy cez stanicu "
     "do 14:00.", iso(2026, 11, 2, 13)),
    ("Mimoriadnosť v ŽST Kraľovany: prevádzka bola obnovená o 09:20 pre osobné vlaky, nákladné vlaky do 14:00 "
     "nepremávajú.", iso(2026, 11, 2, 13)),
    ("Mimoriadnosť v ŽST Kraľovany: prevádzka je obnovená o 09:20. Vlaky s nebezpečným tovarom (RID) do 15:00 nesmú "
     "stanicou prechádzať.", iso(2026, 11, 2, 14)),
], ids=["zakaz_nakladne", "nakladne_nepremavaju", "rid"])
def test_qm5_obnovenie_so_zakazom_pre_nakladne_vlaky_trva(cfg, body, end):
    """QM5: obnovenie prevádzky, pri ktorom pre nákladné vlaky (vlaky s RID) obmedzenie trvá, nie je koniec
    mimoriadnosti – je to mimoriadnosť v dopravni do uvedeného času (nie „iné“ s radou ukončiť mimoriadnosť)."""
    (it,) = rules(body, subject="Mimoriadnosť Kraľovany", received=utc(2026, 11, 2, 8, 25))
    assert it.kind == "mimoriadnost" and _section(it) == ("Kraľovany", "Kraľovany"), (it.kind, it.data)
    assert it.data["end"] == end and it.data["start"] == iso(2026, 11, 2, 8, 20)
    assert not any("skončení mimoriadnosti" in n for n in it.notes)
    (done,) = rules("Mimoriadnosť v ŽST Kraľovany bola ukončená, prevádzka je obnovená o 09:20.",
                    subject="Mimoriadnosť Kraľovany", received=utc(2026, 11, 2, 8, 25))
    assert done.kind == "ine"                                  # úplné obnovenie ostáva oznámením o skončení


def test_qm5_end_to_end_nakladny_vlak_pocas_zakazu(env, session):
    """QM5 end to end: po správe o čiastočnom obnovení je v registri záznam „navrh“ v Kraľovanoch do 14:00 a nákladný
    vlak cez Kraľovany o 10:00 nejde."""
    now = utc(2026, 11, 2, 8, 30)
    drop(env, "partial.eml", raw_mail("Mimoriadnost Kralovany", "Mimoriadnosť v ŽST Kraľovany: prevádzka je obnovená o "
                                      "09:20. Pre nákladné vlaky platí zákaz jazdy cez stanicu do 14:00.\n", "part1",
                                      sender=KD_ZA, date="Mon, 02 Nov 2026 09:25:00 +0100"))
    _process(session, now)
    (ev,) = events(session)
    assert ev.kind == "mimoriadnost"
    (row,) = rows(session)
    assert (row.from_station, as_utc(row.end_utc), row.status) == ("Kraľovany", utc(2026, 11, 2, 13), "navrh")
    r = _decide(session, "66102", "Žilina", "Ružomberok", utc(2026, 11, 2, 9), now)
    assert r.decision != "VYPRAVIŤ", (r.decision, r.summary)


# ====================================================================== QC0 zoznam termínov a úsekov
@pytest.mark.parametrize("subject,body,expected", [
    ("ROV č. 1322/2026 – výluky Kysak – Košice", "Vážení dopravcovia,\n\npodľa ROV č. 1322/2026 budú v úseku Kysak – "
     "Košice vykonané tieto výluky:\n\n1) Úplná výluka oboch traťových koľají dňa 01.12.2026 od 08:00 do 12:00 hod.\n"
     "2) Úplná výluka oboch traťových koľají dňa 03.12.2026 od 08:00 do 12:00 hod.\n",
     [("Kysak", "Košice", iso(2026, 12, 1, 7)), ("Kysak", "Košice", iso(2026, 12, 3, 7))]),
    ("ROV 1400/2026 – výluka", "Dobrý deň,\n\núplná výluka oboch TK v úseku Púchov – Považská Bystrica (ROV č. "
     "1400/2026):\na) dňa 24.11.2026 od 8:00 do 12:00 hod.,\nb) dňa 26.11.2026 od 8:00 do 12:00 hod.\n",
     [("Púchov", "Považská Bystrica", iso(2026, 11, 24, 7)), ("Púchov", "Považská Bystrica", iso(2026, 11, 26, 7))]),
    ("ROV 1401/2026 – úplná výluka", "Úplná výluka oboch TK v úseku Štrba – Poprad-Tatry (ROV č. 1401/2026) v "
     "termínoch:\n- 23.11.2026 od 08:00 do 12:00 hod.\n- 25.11.2026 od 13:00 do 17:00 hod.\n",
     [("Štrba", "Poprad-Tatry", iso(2026, 11, 23, 7)), ("Štrba", "Poprad-Tatry", iso(2026, 11, 25, 12))]),
    ("ROV č. 1402/2026 – úplné výluky 24.11.2026", "Dobrý deň,\n\ndňa 24.11.2026 od 8:00 do 12:00 hod. budú úplné "
     "výluky oboch TK v týchto úsekoch (ROV č. 1402/2026):\n- Púchov – Považská Bystrica\n- Bytča – Žilina\n",
     [("Púchov", "Považská Bystrica", iso(2026, 11, 24, 7)), ("Bytča", "Žilina", iso(2026, 11, 24, 7))]),
    ("ROV č. 1323/2026", "Vážení,\n\nv úseku Kysak – Košice bude úplná výluka oboch traťových koľají (ROV č. "
     "1323/2026).\n\nTermíny: dňa 01.12.2026 od 08:00 do 12:00 hod. a dňa 03.12.2026 od 08:00 do 12:00 hod.\n",
     [("Kysak", "Košice", iso(2026, 12, 1, 7)), ("Kysak", "Košice", iso(2026, 12, 3, 7))]),
], ids=["cislovane", "pismena", "pomlcky", "jeden_termin_viac_usekov", "terminy_v_riadku"])
def test_qc0_kazdy_termin_a_usek_zoznamu_je_vyluka(cfg, subject, body, expected):
    """QC0: úsek uvedený raz a termíny ako položky zoznamu (alebo jeden termín a úseky v zozname) – výluka pre KAŽDÝ
    termín a úsek, nie len pre prvý."""
    items = rules(body, subject=subject, received=utc(2026, 11, 16, 8))
    got = sorted((*_section(i), i.data["start"]) for i in items)
    assert [i.kind for i in items] == ["vyluka_nova"] * len(expected) and got == sorted(expected), got
    assert all(i.data["kind"] == "uplna" for i in items)


def test_qc0_nepriradeny_termin_je_v_poznamke(cfg):
    """QC0 + princíp (b): termín, ktorý sa nepodarilo priradiť k úseku, sa nestratí potichu – výluka má poznámku
    a nižšiu istotu (dispečer ho vidí vo fronte)."""
    items = rules("Dňa 01.12.2026 od 8:00 do 12:00 hod. (pozri prílohu).\n\nVýluka 1. TK v úseku Kysak – Košice dňa "
                  "03.12.2026 od 8:00 do 12:00 hod.\n", subject="ROV c. 1324/2026", received=utc(2026, 11, 16, 8))
    (it,) = items
    assert it.data["start"] == iso(2026, 12, 3, 7)
    assert any("01.12.2026" in n and "nepodarilo priradiť" in n for n in it.notes), it.notes
    assert it.confidence < 0.85


def test_qc0_end_to_end_druhy_termin_zoznamu_blokuje_vlak(env, session):
    """QC0 end to end: oba termíny zoznamu dostanú záznam „navrh“ a vlak počas DRUHÉHO termínu nejde."""
    drop(env, "rov.eml", raw_mail("ROV c. 1322/2026 - vyluky Kysak - Kosice", "Podľa ROV č. 1322/2026 budú v úseku Kysak "
                                  "– Košice vykonané tieto výluky:\n\n1) Úplná výluka oboch traťových koľají dňa "
                                  "01.12.2026 od 08:00 do 12:00 hod.\n2) Úplná výluka oboch traťových koľají dňa "
                                  "03.12.2026 od 08:00 do 12:00 hod.\n", "rov1322", sender=ROV_KE,
                                  date="Mon, 16 Nov 2026 09:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 8, 2))
    assert sorted(as_utc(r.start_utc) for r in rows(session)) == [utc(2026, 12, 1, 7), utc(2026, 12, 3, 7)]
    r = _decide(session, "77540", "Kysak", "Košice", utc(2026, 12, 3, 8), utc(2026, 12, 3, 4))
    assert r.decision != "VYPRAVIŤ", (r.decision, r.summary)


# ====================================================================== QC4 zrušenie jedného dňa viacdennej výluky
@pytest.mark.parametrize("body", [
    "Výluka podľa ROV č. 1315/2026 dňa 25.11.2026 sa ruší. V ostatných dňoch (23., 24., 26. a 27.11.2026) platí výluka "
    "podľa ROV bez zmeny.",
    "Ruší sa výluka podľa ROV č. 1315/2026 len v deň 25.11.2026, ostatné dni výluky ostávajú v platnosti.",
])
def test_qc4_zrusenie_jedneho_dna_ma_rusny_termin(cfg, body):
    """QC4: zrušenie jedného dňa viacdennej výluky nesie rušený deň (začiatok aj koniec) a poznámku o čiastočnom
    zrušení – nie zrušenie celej výluky bez termínu."""
    (it,) = rules("Dobrý deň,\n\n" + body + "\n", subject="ROV č. 1315/2026 – zrušenie výluky dňa 25.11.2026",
                  received=utc(2026, 11, 16, 9))
    assert it.kind == "vyluka_zrusenie" and it.data["external_ref"] == "1315/2026"
    assert (it.data["start"], it.data["end"]) == (iso(2026, 11, 24, 23), iso(2026, 11, 25, 23))
    assert any("len časť výluky" in n for n in it.notes), it.notes


def test_qc4_end_to_end_potvrdenie_nezrusi_celu_vyluku(env, session):
    """QC4 end to end: potvrdenie zrušenia jedného dňa nezruší celú výluku 23. – 27.11. – od 5. revízie sa výluka
    rozdelí (zruší sa len 25.11., ostatné dni ostanú overené a vlak 24.11. nejde); zrušenie celej jednodňovej výluky
    funguje."""
    from vlaky.posta.spracovanie import confirm_event

    c = Closure(kind="uplna", from_station="Ružomberok", to_station="Liptovský Mikuláš", start_utc=utc(2026, 11, 23, 7),
                end_utc=utc(2026, 11, 27, 13), daily_from="08:00", daily_to="14:00", status="overene", source="email",
                external_ref="1315/2026")
    whole = Closure(kind="uplna", from_station="Leopoldov", to_station="Trnava", start_utc=utc(2026, 11, 25, 7),
                    end_utc=utc(2026, 11, 25, 11), status="overene", source="email", external_ref="2241/2026")
    session.add_all([c, whole])
    session.commit()
    drop(env, "z.eml", raw_mail("ROV c. 1315/2026 - zrusenie vyluky dna 25.11.2026", "Výluka podľa ROV č. 1315/2026 dňa "
                                "25.11.2026 sa ruší. V ostatných dňoch (23., 24., 26. a 27.11.2026) platí výluka podľa "
                                "ROV bez zmeny.\n", "z1315", sender=ROV_ZA, date="Mon, 16 Nov 2026 10:00:00 +0100"))
    drop(env, "w.eml", raw_mail("Informacia k ROV c. 2241/2026", "ROV č. 2241/2026 (Leopoldov – Trnava, 25.11.2026) "
                                "sa ruší.\n", "w2241", sender=ROV_ZA, date="Mon, 16 Nov 2026 10:01:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 5))
    evs = {e.data.get("external_ref"): e for e in events(session)}
    part, full = evs["1315/2026"], evs["2241/2026"]
    assert any("len časť výluky" in w for w in part.validation["warnings"]), part.validation["warnings"]
    ev = confirm_event(session, part.id, "dispecer1")
    session.commit()
    assert ev.validation["apply_result"]["ok"] is True and ev.validation["apply_result"]["action"] == "rozdelene"
    session.refresh(c)
    assert c.status == "overene" and (c.start_utc.replace(tzinfo=None), c.end_utc.replace(tzinfo=None)) == (
        datetime(2026, 11, 23, 7), datetime(2026, 11, 24, 13))
    assert full.kind == "vyluka_zrusenie"
    confirm_event(session, full.id, "dispecer1")
    session.commit()
    session.refresh(whole)
    assert whole.status == "zrusene"
    r = _decide(session, "77530", "Ružomberok", "Liptovský Mikuláš", utc(2026, 11, 24, 9), utc(2026, 11, 24, 5))
    assert r.decision != "VYPRAVIŤ", (r.decision, r.summary)


# ====================================================================== QM4 zrušenie POTR len pre pomalú jazdu
POTR_MAIL = ("Rozkaz o výluke č. 1160/2026: úplná výluka oboch traťových koľají v úseku Kysak – Košice 20.11.2026 od "
             "8:00 do 14:00.\nSúčasne sa ruší POTR č. 12/2026.\n")


def test_qm4_zrusenie_potr_nezrusi_rov_s_rovnakym_cislom(env, session):
    """QM4: „Súčasne sa ruší POTR č. 12/2026“ – zrušenie POMALEJ JAZDY: potvrdenie nezruší úplnú výluku ROV 12/2026
    inej OR na inej trati (holé číslo), a keď je v registri pomalá jazda 12/2026, zruší sa práve tá."""
    from vlaky.posta.spracovanie import confirm_event

    rov = Closure(kind="uplna", from_station="Žilina", to_station="Púchov", start_utc=utc(2026, 11, 20, 7),
                  end_utc=utc(2026, 11, 20, 13), status="overene", source="email", external_ref="12/2026")
    session.add(rov)
    session.commit()
    drop(env, "rov.eml", raw_mail("ROV c. 1160/2026 Kysak - Kosice", POTR_MAIL, "rov1160-potr", sender=ROV_KE))
    _process(session, utc(2026, 11, 2, 4))
    cancel = next(e for e in events(session) if e.kind == "vyluka_zrusenie")
    assert (cancel.data["external_ref"], cancel.data["kind"]) == ("12/2026", "pomala_jazda")
    ev = confirm_event(session, cancel.id, "dispecer1")
    session.commit()
    session.refresh(rov)
    assert rov.status == "overene" and ev.validation["apply_result"]["ok"] is False
    pj = Closure(kind="pomala_jazda", from_station="Kysak", to_station="Košice", start_utc=utc(2026, 11, 1, 7),
                 end_utc=utc(2026, 12, 1, 7), speed_kmh=50, status="overene", source="email", external_ref="12/2026")
    session.add(pj)
    session.commit()
    ev = confirm_event(session, cancel.id, "dispecer1")
    session.commit()
    session.refresh(pj)
    session.refresh(rov)
    assert (pj.status, rov.status) == ("zrusene", "overene"), ev.validation["apply_result"]


# ====================================================================== QC5 zámer e-mailu
@pytest.mark.parametrize("subject,body", [
    ("Automatická odpoveď: Dotaz k ROV č. 1150/2026 Kraľovany – Ružomberok",
     "Dobrý deň,\n\nsom mimo kancelárie od 9.11.2026 do 13.11.2026 vrátane. Vo veci výluk v úseku Kraľovany – "
     "Ružomberok\nkontaktujte prosím kolegu Fiktívneho Zástupcu (zastupca.f@zsr.example).\n\nĎakujem.\n"),
    ("Pozvánka na koordinačnú poradu k výlukám 2027",
     "Vážení dopravcovia,\n\npozývame Vás na koordinačnú poradu k plánu výluk na rok 2027, ktorá sa uskutoční dňa "
     "25.11.2026\nod 9:00 do 12:00 hod. v zasadačke OR Žilina.\n\nProgram: plán výluk na trati Žilina – Košice v roku "
     "2027, koordinácia výluk s dopravcami, rôzne.\n"),
], ids=["automaticka_odpoved", "pozvanka"])
def test_qc5_automaticka_odpoved_a_pozvanka_nie_su_vyluka(cfg, subject, body):
    """QC5: automatická odpoveď a pozvánka na poradu spomínajú výluky, úsek aj dátumy, ale nič neohlasujú – nie je
    z nich predpokladaná úplná výluka (zastavila by vlaky), ale „iné“ s vysvetlením."""
    items = rules(body, subject=subject, received=utc(2026, 11, 9, 7))
    assert [i.kind for i in items] == ["ine"] and "prevádzkové oznámenie" in items[0].notes[0]


def test_qc5_automaticka_odpoved_a_pozvanka_podla_hlaviciek(env, session):
    """QC5 end to end: automatická odpoveď podľa hlavičky Auto-Submitted (bez typického predmetu) a pozvánka
    v kalendári (text/calendar) nevytvoria výluku ani záznam pre plánovač; správa KIS „auto-generated“ sa spracuje
    ďalej (zrušenie trasy)."""
    now = utc(2026, 11, 9, 7, 5)
    _drop_at(env, "ooo.eml", _mail("RE: Dotaz k ROV č. 1150/2026 Kraľovany – Ružomberok", "Dobrý deň,\n\nsom mimo "
                                   "kancelárie od 9.11.2026 do 13.11.2026. Vo veci výluk v úseku Kraľovany – Ružomberok "
                                   "kontaktujte kolegu.\n", "ooo1", "Fiktivny Vylukar <vylukar.f@zsr.example>",
                                   "Mon, 09 Nov 2026 08:05:00 +0100", headers={"Auto-Submitted": "auto-replied"}),
             now - timedelta(minutes=3))
    _drop_at(env, "ics.eml", _mail("Koordinácia výluk 2027", "Plán výluk na trati Žilina – Košice, dňa 25.11.2026 od "
                                   "9:00 do 12:00 hod. v zasadačke OR Žilina.\n", "ics1",
                                   "Sekretariat OR Zilina <sekretariat.orza@zsr.example>",
                                   "Mon, 09 Nov 2026 08:06:00 +0100",
                                   attachments=[("pozvanka.ics", b"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nEND:VCALENDAR\r\n",
                                                 "text", "calendar")]), now - timedelta(minutes=2))
    _drop_at(env, "kis.eml", _mail("KIS – zrušenie trasy vlaku Pn 12345 na deň 15.11.2026", "Typ správy: Zrušenie "
                                   "trasy\nVlak: Pn 12345\nDeň jazdy: 15.11.2026\n\nTrasa vlaku Pn 12345 na deň "
                                   "15.11.2026 bola zrušená.\n", "kis1", "KIS <kis-noreply@zsr.example>",
                                   "Mon, 09 Nov 2026 08:07:00 +0100", headers={"Auto-Submitted": "auto-generated"}),
             now - timedelta(minutes=1))
    _process(session, now)
    from vlaky.models import Email

    kinds = {session.get(Email, e.email_id).message_id.strip("<>").split("@")[0]: e.kind for e in events(session)}
    assert kinds == {"ooo1": "ine", "ics1": "ine", "kis1": "trasa_zrusena"}, kinds
    assert rows(session) == []


@pytest.mark.parametrize("body,ref", [
    ("Výluka 1. TK v úseku Leopoldov – Trnava plánovaná na 23.11.2026 sa nekoná.", None),
    ("Plánovaná výluka 1. TK Leopoldov – Trnava dňa 23.11.2026 bola zrušená.", None),
    ("Výluka 1. TK v úseku Leopoldov – Trnava dňa 23.11.2026 odpadá.", None),
    ("Oznamujeme, že výluka podľa ROV č. 2240/2026 nebude vykonaná.", "2240/2026"),
    ("Výluka TV v úseku Púchov – Považská Bystrica podľa ROV č. 1330/2026 dňa 19.11.2026 sa nekoná.", "1330/2026"),
    ("ROV č. 2241/2026 (Leopoldov – Trnava, 25.11.2026) sa ruší.", "2241/2026"),
], ids=["nekona", "bola_zrusena", "odpada", "nebude_vykonana", "tv_nekona", "rov_v_zatvorke"])
def test_qc5_zrusenie_napisane_v_texte_je_zrusenie(cfg, body, ref):
    """QC5: zrušenie napísané len v texte („… sa nekoná“, „… bola zrušená“, „… odpadá“, „… nebude vykonaná“,
    „ROV č. … (úsek, dátum) sa ruší“) je zrušenie výluky – nie nová výluka „? – ?“ s mylným upozornením."""
    (it,) = rules(body, subject="Informácia o výluke", received=utc(2026, 11, 9, 9))
    assert it.kind == "vyluka_zrusenie", (it.kind, it.data)
    assert it.data["external_ref"] == ref


def test_qc5_pomala_jazda_s_podpisom_oddelenia_vyluk_ostava_pomalou_jazdou(cfg):
    """QC5: podpis „OR Trnava, oddelenie výluk“ druh e-mailu nemení – oznámenie o pomalej jazde ostáva pomalou jazdou
    (s rýchlosťou), nie výlukou koľaje bez rýchlosti."""
    (it,) = rules("Oznamujeme zavedenie pomalej jazdy:\nÚsek: Štrba – Poprad-Tatry, 1. a 2. TK\nRýchlosť obmedzená na "
                  "50 km/h\nPlatnosť: od 16.11.2026 do 30.11.2026\n\nS pozdravom\nOR Trnava, oddelenie výluk\n",
                  subject="Pomalá jazda Štrba – Poprad-Tatry od 16.11.2026")
    assert (it.kind, it.data["speed_kmh"]) == ("pomala_jazda", 50)


def test_qc5_end_to_end_zrusenie_v_texte_zrusi_vyluku(env, session):
    """QC5 end to end: zrušenie napísané v texte je zrušenie (žiadne upozornenie „nová výluka ? – ?“), potvrdenie
    zruší výluku v registri."""
    from vlaky.posta.spracovanie import confirm_event

    c = Closure(kind="uplna", from_station="Leopoldov", to_station="Trnava", start_utc=utc(2026, 11, 25, 7),
                end_utc=utc(2026, 11, 25, 11), status="overene", source="email", external_ref="2241/2026")
    session.add(c)
    session.commit()
    drop(env, "z.eml", raw_mail("Informacia k ROV c. 2241/2026", "Dobrý deň,\n\nROV č. 2241/2026 (Leopoldov – Trnava, "
                                "25.11.2026) sa ruší.\n", "z2241", sender=ROV_ZA, date="Thu, 12 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 12, 9, 2))
    (ev,) = events(session)
    assert ev.kind == "vyluka_zrusenie"
    assert not any("nová výluka" in n.body for n in session.scalars(select(Notification)))
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    session.refresh(c)
    assert c.status == "zrusene"


# ====================================================================== QC6 avízo
def test_qc6_avizo_eta_s_datumom_za_nazvom_stanice(cfg):
    """QC6: „nový predpokladaný príchod do Čadce 03.11.2026 o 15:10 hod. (pôvodne 13:35)“ – ETA 15:10 (nie pôvodný čas
    13:35); „… do Čadce 04.11.2026 o 13:35 hod. Rušňovodiča prosíme pristaviť o 14:30.“ – ETA 13:35 (nie 14:30)."""
    (upd,) = rules("Dobrý deň,\n\naktualizácia avíza: vlak Nex 47250 mešká, nový predpokladaný príchod do Čadce "
                   "03.11.2026 o 15:10 hod. (pôvodne 13:35).\n", subject="Aktualizácia avíza – vlak Nex 47250 – Čadca",
                   received=utc(2026, 11, 3, 10))
    assert (upd.kind, upd.data["eta"]) == ("avizo", iso(2026, 11, 3, 14, 10))
    assert any("13:35" in n for n in upd.notes)
    (nxt,) = rules("Avízo: vlak Nex 47999, predpokladaný príchod do Čadce 04.11.2026 o 13:35 hod. Rušňovodiča prosíme "
                   "pristaviť o 14:30.\n", subject="Avízo vlaku Nex 47999 – Čadca", received=utc(2026, 11, 3, 11))
    assert nxt.data["eta"] == iso(2026, 11, 4, 12, 35) and any("14:30" in n for n in nxt.notes)
    (plain,) = rules("Dobrý deň,\n\navizujeme vlak Nex 47250 (24 vozňov, 1 520 t).\nPredpokladaný príchod do Čadce "
                     "03.11.2026 o 13:35 hod.\n", subject="Avízo vlaku Nex 47250 – Čadca", received=utc(2026, 11, 2, 9))
    assert plain.data["eta"] == iso(2026, 11, 3, 12, 35) and plain.confidence == 0.85


# ====================================================================== QM3 / QC3 citovaný pôvodný ROV
REPLY_1142 = ("Dobrý deň,\núplná výluka oboch traťových koľají v úseku Poprad-Tatry – Liptovský Mikuláš podľa ROV č. "
              "1142/2026 sa presúva na 14.11.2026 od 8:00 do 14:00.\nV prílohe pôvodný ROV.\n\nS pozdravom\nŽSR OR "
              "Poprad\n")
ORIG_1142 = ("Rozkaz o výluke č. 1142/2026: úplná výluka oboch traťových koľají v úseku Poprad-Tatry – Liptovský "
             "Mikuláš 12.11.2026 od 8:00 do 14:00.\n")


def test_qm3_odpoved_s_povodnym_rov_v_prilohe_je_jedna_zmena(cfg):
    """QM3: odpoveď s novým termínom v texte a pôvodným ROV v prílohe – jedna zmena s novým termínom, príloha je len
    súvislosť (nie druhá zmena so starým termínom, ktorá by po potvrdení vrátila výluku na 12.11.)."""
    from vlaky.posta.pravidla import RuleExtractor
    from vlaky.posta.udalosti import MailDocument

    items = RuleExtractor().extract(MailDocument("RE: ROV č. 1142/2026 Poprad-Tatry – Liptovský Mikuláš", REPLY_1142,
                                                 attachments=[("ROV_1142_2026.txt", ORIG_1142)],
                                                 received_at=utc(2026, 11, 3, 7, 55)))
    assert [(i.kind, i.data["start"]) for i in items] == [("vyluka_zmena", iso(2026, 11, 14, 7))]
    assert any("Príloha opakuje" in n for n in items[0].notes)


def test_qm3_end_to_end_potvrdenie_necha_novy_termin(env, session):
    """QM3 end to end: po potvrdení fronty je výluka v registri na 14.11. a vlak 14.11. nejde."""
    c = Closure(kind="uplna", from_station="Poprad-Tatry", to_station="Liptovský Mikuláš", start_utc=utc(2026, 11, 12, 7),
                end_utc=utc(2026, 11, 12, 13), status="overene", source="email", external_ref="1142/2026")
    session.add(c)
    session.commit()
    now = utc(2026, 11, 3, 8)
    _drop_at(env, "re.eml", _mail("RE: ROV č. 1142/2026 Poprad-Tatry – Liptovský Mikuláš", REPLY_1142, "re1142",
                                  "ZSR OR Poprad <vyluky.orpp@zsr.example>", "Tue, 03 Nov 2026 08:55:00 +0100",
                                  attachments=[("ROV_1142_2026.txt", ORIG_1142.encode("utf-8"), "text", "plain")]), now)
    _process(session, now)
    evs = events(session)
    assert [e.kind for e in evs] == ["vyluka_zmena"]
    _confirm_all(session, evs)
    session.refresh(c)
    assert as_utc(c.start_utc) == utc(2026, 11, 14, 7)
    r = _decide(session, "77542", "Poprad-Tatry", "Liptovský Mikuláš", utc(2026, 11, 14, 8), utc(2026, 11, 14, 5))
    assert r.decision != "VYPRAVIŤ", (r.decision, r.summary)


ORIG_1270 = ("Dobrý deň,\n\noznamujeme výluku oboch TK v úseku Žilina – Varín dňa 26.11.2026 od 08:00 do 14:00 hod. "
             "(ROV č. 1270/2026).\n")
REPLY_1270 = ("Dobrý deň,\n\nna základe požiadavky zhotoviteľa sa termín výluky mení na 27.11.2026 od 08:00 do 14:00 "
              "hod.\n\nS pozdravom\nOR Žilina\n\nOd: ŽSR OR Žilina – výluky <vyluky.orza@zsr.example>\nOdoslané: pondelok "
              "9. novembra 2026 10:15\nKomu: dispecing@dopravca.example\nPredmet: ROV č. 1270/2026 – výluka Žilina – "
              "Varín\n\n" + ORIG_1270)


@pytest.mark.parametrize("subject,body,attachments", [
    ("FW: RE: ROV č. 1270/2026 – výluka Žilina – Varín", "Na vedomie.\n\n-----Original Message-----\nFrom: ŽSR OR Žilina "
     "– výluky <vyluky.orza@zsr.example>\nSent: Thursday, November 12, 2026 11:00 AM\nTo: dispecing@dopravca.example\n"
     "Subject: RE: ROV č. 1270/2026 – výluka Žilina – Varín\n\n" + REPLY_1270, []),
    ("Fwd: RE: ROV č. 1270/2026 – výluka Žilina – Varín", "Preposielam odpoveď ŽSR (v prílohe).\n\n--- Preposlaná správa "
     "(príloha) ---\nOd: ŽSR OR Žilina – výluky <vyluky.orza@zsr.example>\nPredmet: RE: ROV č. 1270/2026 – výluka "
     "Žilina – Varín\nDátum: Thu, 12 Nov 2026 11:00:00 +0100\n\n" + REPLY_1270, []),
    ("Fwd: odpoveď ŽSR", "Preposielam odpoveď ŽSR (v prílohe).\n",
     [("odpoved_zsr.eml", "From: ŽSR OR Žilina – výluky <vyluky.orza@zsr.example>\nSubject: RE: ROV č. 1270/2026 – "
                          "výluka Žilina – Varín\nDate: Thu, 12 Nov 2026 11:00:00 +0100\nMessage-ID: <x@zsr.example>\n\n"
       + REPLY_1270)]),
], ids=["fw_re_v_texte", "preposlana_priloha", "priloha_eml"])
def test_qc3_preposlana_odpoved_je_jedna_zmena_s_novym_terminom(cfg, subject, body, attachments):
    """QC3: preposlaná odpoveď ŽSR („FW: RE:“ v texte, priložená správa, .eml) – citovaný pôvodný ROV v nej nie je
    druhá zmena so starým termínom 26.11.; platí nový termín 27.11."""
    from vlaky.posta.pravidla import RuleExtractor
    from vlaky.posta.udalosti import MailDocument

    items = RuleExtractor().extract(MailDocument(subject, body, attachments=attachments,
                                                 received_at=utc(2026, 11, 12, 10, 30)))
    assert [(i.kind, i.data["start"]) for i in items] == [("vyluka_zmena", iso(2026, 11, 27, 7))], \
        [(i.kind, i.data) for i in items]
    assert _section(items[0]) == ("Žilina", "Varín") and items[0].data["external_ref"] == "1270/2026"


def test_qc3_end_to_end_potvrdenie_preposlanej_odpovede(env, session):
    """QC3 end to end: dispečer prepošle odpoveď ŽSR (FW: RE:) – po potvrdení fronty je výluka na 27.11. a vlak 27.11.
    nejde (nie späť na 26.11.)."""
    c = Closure(kind="uplna", from_station="Žilina", to_station="Varín", start_utc=utc(2026, 11, 26, 7),
                end_utc=utc(2026, 11, 26, 13), status="overene", source="email", external_ref="1270/2026")
    session.add(c)
    session.commit()
    now = utc(2026, 11, 12, 10, 32)
    body = ("Na vedomie.\n\n-----Original Message-----\nFrom: ŽSR OR Žilina – výluky <vyluky.orza@zsr.example>\n"
            "Sent: Thursday, November 12, 2026 11:00 AM\nTo: dispecing@dopravca.example\nSubject: RE: ROV č. 1270/2026 – "
            "výluka Žilina – Varín\n\n" + REPLY_1270)
    msg = _mail("FW: RE: ROV č. 1270/2026 – výluka Žilina – Varín", body, "fw1270", "Dispecing <dispecing@dopravca.example>",
                "Thu, 12 Nov 2026 11:30:00 +0100")
    _drop_at(env, "fw.eml", msg.replace(b"header.d=zsr.example", b"header.d=dopravca.example")
             .replace(b"smtp.mailfrom=zsr.example", b"smtp.mailfrom=dopravca.example")
             .replace(b"header.from=zsr.example", b"header.from=dopravca.example"), now)
    _process(session, now)
    evs = events(session)
    assert [e.kind for e in evs] == ["vyluka_zmena"], [(e.kind, e.data) for e in evs]
    _confirm_all(session, evs)
    session.refresh(c)
    assert as_utc(c.start_utc) == utc(2026, 11, 27, 7)
    r = _decide(session, "77580", "Žilina", "Varín", utc(2026, 11, 27, 9), utc(2026, 11, 27, 4))
    assert r.decision != "VYPRAVIŤ", (r.decision, r.summary)
