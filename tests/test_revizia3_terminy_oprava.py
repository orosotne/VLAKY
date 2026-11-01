"""Tretia revízia – termíny a udalosti z e-mailov ŽSR (oblasť M1), oprava po kontrole opráv: regresné testy.

- zrušenie pomalej jazdy či POTR v e-maile s novou výlukou nesmie výluku stratiť (výluka ostáva, zrušenie je ďalšia
  udalosť); „do odvolania pomalá jazda“, „zrušenie … oznámime“ ani znovu oznámená POTR nie sú zrušenie;
- číslo zrušenia výluky je číslo ROV, nie telegramu, na základe ktorého sa ruší;
- trvajúca či len čiastočne obnovená mimoriadnosť („prevádzka nebola obnovená“, „prejazdná po 2. koľaji“) nie je
  jej skončenie; skončenie s meškaním vlakov či s obmedzením rýchlosti nie je nová mimoriadnosť;
- oznámenie o skončení mimoriadnosti bez čísla nezruší inú výluku (napr. plánovaný ROV na tom istom úseku);
- mimoriadnosť bez konca si nevezme okno náhradnej dopravy či kontaktu na dispečera;
- noc „12.11.2026/13.11.2026“ s oknom po polnoci je 13.11.; odpoveď s dátumom, ktorý nie je termínom, nestratí
  citovaný termín; zoznam „2.1. a 28.12.2026“ nie je január v minulosti.

Poistky správania, ktoré sa nemení, sú v ``test_revizia3_terminy_bez_zmeny.py``. Všetky adresy sú fiktívne
(*.example).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from vlaky.cas import set_fake_now
from vlaky.hranice import seed_borders
from vlaky.models import Closure
from test_revizia3_terminy import SEC, add_train, as_utc, drop, events, iso, raw_mail, rows, rules, terms, utc

KD = "Kontrolny dispecer OR Zilina <kd.orza@zsr.example>"
KE = "Kontrolny dispecer OR Kosice <kd.orke@zsr.example>"
ROV_KE = "ZSR OR Kosice <vyluky.orke@zsr.example>"


@pytest.fixture()
def env(cfg, session, monkeypatch):
    """Overenie odosielateľa ako v prevádzke (DMARC, dôveryhodný server) a register priechodov."""
    monkeypatch.setattr(cfg, "require_dmarc_pass", True)
    monkeypatch.setattr(cfg, "trusted_authserv_ids", "mx.dopravca.example")
    seed_borders(session)
    session.commit()
    return cfg


@pytest.fixture()
def no_planner(monkeypatch):
    """Podvrhnutý prepočet plánu (testy extrakcie nezávisia od plánovača)."""
    from vlaky.planovanie import planovac

    calls: list[str] = []
    monkeypatch.setattr(planovac, "evaluate_all", lambda session, now=None, trigger="": calls.append(trigger) or [])
    return calls


def _decide(session, number: str, origin: str, destination: str, dep: datetime, now: datetime, hours: int = 8):
    from vlaky.planovanie.planovac import evaluate_train

    t = add_train(session, number, origin, destination, dep, deadline_h=hours)
    return evaluate_train(session, t.id, now, "test M1")


def _not_green_go(r) -> None:
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), (r.decision, r.color, r.summary)


# ====================================================================== zrušenie pomalej jazdy v e-maile s výlukou
@pytest.mark.parametrize("subject,body,ref,kind", [
    ("ROV c. 1160/2026 Kysak - Kosice", "Rozkaz o výluke č. 1160/2026: úplná výluka oboch traťových koľají v úseku "
     "Kysak – Košice 20.11.2026 od 8:00 do 14:00. Zároveň sa ruší pomalá jazda zavedená TLG č. 37/2026.",
     "1160/2026", "uplna"),
    ("ROV c. 1161/2026 Kysak - Kosice", "Rozkaz o výluke č. 1161/2026: výluka 1. TK v úseku Kysak – Košice 20.11.2026 "
     "od 8:00 do 14:00. Po skončení výluky sa ruší POTR č. 12/2026.", "1161/2026", "kolaj"),
    # veta o zrušení pred výlukou – číslo výluky nie je číslo POTR
    ("ROV c. 1162/2026 Kysak - Kosice", "Zároveň sa ruší POTR č. 12/2026.\nRozkaz o výluke č. 1162/2026: výluka 1. TK "
     "v úseku Kysak – Košice 20.11.2026 od 8:00 do 14:00.", "1162/2026", "kolaj"),
])
def test_vyluka_so_zrusenim_pomalej_jazdy_ostava_vylukou(cfg, subject, body, ref, kind):
    """Kritické: e-mail s novou výlukou, ktorý zároveň ruší pomalú jazdu či POTR, je nová výluka (s vlastným číslom
    a termínom) – zrušenie pomalej jazdy s číslom je ďalšia udalosť, nie druh celého e-mailu."""
    items = rules(body, subject=subject, received=utc(2026, 11, 2, 7, 30))
    assert [i.kind for i in items] == ["vyluka_nova", "vyluka_zrusenie"]
    new, cancel = items
    assert new.data["external_ref"] == ref and new.data["kind"] == kind
    assert (new.data["start"], new.data["end"]) == (iso(2026, 11, 20, 7), iso(2026, 11, 20, 13))
    assert cancel.data["external_ref"] in ("37/2026", "12/2026") and cancel.data["external_ref"] != ref


def test_vyluka_s_pomalou_jazdou_do_odvolania_ostava_vylukou(cfg):
    """„… po výluke platí do odvolania pomalá jazda 50 km/h“ nie je zrušenie – výluka ostáva."""
    (it,) = rules("Výluka oboch traťových koľají v úseku Kysak – Košice od 20.11.2026 8:00 do 20.11.2026 14:00, po "
                  "výluke platí do odvolania pomalá jazda 50 km/h.", subject="Vyluka Kysak - Kosice")
    assert it.kind == "vyluka_nova"
    assert (it.data["start"], it.data["end"]) == (iso(2026, 11, 20, 7), iso(2026, 11, 20, 13))


def test_vyluka_so_zrusenim_pomalej_jazdy_end_to_end_planovac(env, session):
    """Kritické end to end: ROV č. 1160/2026 so zrušením pomalej jazdy TLG č. 37/2026 – výluka dostane záznam
    „navrh“ a vlak Kysak → Košice počas výluky nedostane VYPRAVIŤ ZELENÁ; zrušenie čaká na potvrdenie."""
    from vlaky.posta.spracovanie import process_mailbox

    now = utc(2026, 11, 20, 6)                                    # 07:00 miestneho času
    set_fake_now(now)
    drop(env, "rov.eml", raw_mail("ROV c. 1160/2026 Kysak - Kosice", "Rozkaz o výluke č. 1160/2026: úplná výluka "
                                  "oboch traťových koľají v úseku Kysak – Košice 20.11.2026 od 8:00 do 14:00. Zároveň "
                                  "sa ruší pomalá jazda zavedená TLG č. 37/2026.\n", "rov1160", sender=ROV_KE,
                                  date="Fri, 20 Nov 2026 06:30:00 +0100"))
    process_mailbox(session, now=now)
    session.commit()
    assert sorted(e.kind for e in events(session)) == ["vyluka_nova", "vyluka_zrusenie"]
    (row,) = rows(session)
    assert (as_utc(row.start_utc), as_utc(row.end_utc), row.external_ref) == (utc(2026, 11, 20, 7),
                                                                             utc(2026, 11, 20, 13), "1160/2026")
    _not_green_go(_decide(session, "77541", "Kysak", "Košice", utc(2026, 11, 20, 8), now))


# ====================================================================== nová či znovu oznámená pomalá jazda
@pytest.mark.parametrize("subject,body,speed,until_revoked", [
    ("Zavedenie pomalej jazdy Kysak - Kosice", "Telegram č. 44/2026: od 03.11.2026 06:00 do odvolania pomalá jazda "
     "50 km/h v úseku Kysak – Košice.", 50, True),
    ("TLG c. 45/2026", "Od 03.11.2026 12:00 do odvolania pomalá jazda 30 km/h v úseku Kysak – Košice.", 30, True),
    ("Pomala jazda Kysak - Kosice", "Zavedenie pomalej jazdy 30 km/h v úseku Kysak – Košice od 03.11.2026 12:00 do "
     "10.11.2026 12:00, zrušenie pomalej jazdy oznámime telegramom.", 30, False),
    ("POTR c. 15/2026", "Platí od 04.11.2026 08:00 do odvolania POTR č. 15/2026, 40 km/h v úseku Štrba – Poprad-Tatry.",
     40, True),
])
def test_nova_pomala_jazda_nie_je_zrusenie(cfg, subject, body, speed, until_revoked):
    """„do odvolania pomalá jazda / POTR“ a „zrušenie pomalej jazdy oznámime“ ohlasujú pomalú jazdu – nie jej
    zrušenie (inak by plánovač pomalú jazdu nevidel a potvrdenie by ju zrušilo)."""
    (it,) = rules(body, subject=subject)
    assert it.kind == "pomala_jazda" and it.data["speed_kmh"] == speed
    assert bool(it.data.get("until_revoked")) is until_revoked and it.data["start"]


def test_znovu_oznamena_potr_sa_potvrdenim_nezrusi(env, session, no_planner):
    """POTR č. 15/2026 je v registri; ŽSR ju pošle znova („Platí od … do odvolania POTR č. 15/2026 …“) – to nie je jej
    zrušenie: udalosť je pomalá jazda a po potvrdení POTR ostáva platná."""
    from vlaky.posta.spracovanie import confirm_event, process_mailbox

    set_fake_now(utc(2026, 11, 4, 9))
    potr = Closure(kind="pomala_jazda", from_station="Štrba", to_station="Poprad-Tatry", speed_kmh=40,
                   start_utc=utc(2026, 11, 4, 7), end_utc=utc(2026, 12, 4, 7), until_revoked=True, status="overene",
                   source="email", external_ref="15/2026")
    session.add(potr)
    session.commit()
    drop(env, "potr.eml", raw_mail("POTR c. 15/2026 Strba - Poprad-Tatry", "Platí od 04.11.2026 08:00 do odvolania "
                                   "POTR č. 15/2026, 40 km/h v úseku Štrba – Poprad-Tatry.\n", "potr15", sender=KE,
                                   date="Wed, 04 Nov 2026 10:00:00 +0100"))
    process_mailbox(session)
    session.commit()
    (ev,) = events(session)
    assert ev.kind == "pomala_jazda"
    if ev.status == "navrh":
        confirm_event(session, ev.id, "dispecer1")
        session.commit()
    session.refresh(potr)
    assert potr.status != "zrusene"


@pytest.mark.parametrize("subject,body,kinds", [
    # sloveso na konci vety („… zavedená TLG č. 37/2026 sa ruší“), aj zalomený riadok
    ("Pomala jazda Kysak - Kosice", "Pomalá jazda 40 km/h v úseku Kysak – Košice zavedená TLG č. 37/2026 sa ruší od "
     "03.11.2026 12:00.", ["vyluka_zrusenie"]),
    ("TLG c. 38/2026", "Pomalá jazda v úseku Kysak – Košice zavedená TLG č. 37/2026\nsa ruší od 03.11.2026 12:00.",
     ["vyluka_zrusenie"]),
    # zrušenie starej a zavedenie novej pomalej jazdy – nová pomalá jazda sa nestratí
    ("Pomala jazda Kysak - Kosice", "Ruší sa pomalá jazda zavedená TLG č. 37/2026.\nOd 03.11.2026 12:00 platí v úseku "
     "Kysak – Košice pomalá jazda 50 km/h do odvolania.", ["pomala_jazda", "vyluka_zrusenie"]),
])
def test_zrusenie_pomalej_jazdy_v_dalsich_tvaroch(cfg, subject, body, kinds):
    """Zrušenie pomalej jazdy so slovesom na konci vety je zrušenie s číslom rušeného dokumentu (37/2026); ak e-mail
    zároveň zavádza novú pomalú jazdu, tá ostáva."""
    items = rules(body, subject=subject)
    assert [i.kind for i in items] == kinds
    cancel = next(i for i in items if i.kind == "vyluka_zrusenie")
    assert cancel.data["external_ref"] == "37/2026"
    if "pomala_jazda" in kinds:
        assert next(i for i in items if i.kind == "pomala_jazda").data["speed_kmh"] == 50


# ====================================================================== číslo zrušenia výluky
@pytest.mark.parametrize("subject,body", [
    ("Zrusenie vyluky", "Ruší sa výluka ROV č. 1142/2026 v úseku Poprad-Tatry – Liptovský Mikuláš na základe TLG č. "
     "50/2026."),
    ("Zrusenie vyluky ROV c. 1142/2026", "Ruší sa výluka v úseku Poprad-Tatry – Liptovský Mikuláš podľa telegramu č. "
     "50/2026."),
])
def test_zrusenie_vyluky_ma_cislo_rov_nie_telegramu(env, session, no_planner, subject, body):
    """Zrušenie ROV č. 1142/2026 „na základe TLG č. 50/2026“ nesie číslo ROV – potvrdenie výluku zruší (s číslom
    telegramu by sa nenašla a ostala by platná)."""
    from vlaky.posta.spracovanie import confirm_event, process_mailbox

    set_fake_now(utc(2026, 11, 3, 8))
    c = Closure(kind="kolaj", track="1", from_station="Poprad-Tatry", to_station="Liptovský Mikuláš",
                start_utc=utc(2026, 11, 12, 7), end_utc=utc(2026, 11, 12, 13), status="overene", source="email",
                external_ref="1142/2026")
    session.add(c)
    session.commit()
    drop(env, "z.eml", raw_mail(subject, body + "\n", "zrus-1142", date="Tue, 03 Nov 2026 09:00:00 +0100"))
    process_mailbox(session)
    session.commit()
    (ev,) = events(session)
    assert ev.kind == "vyluka_zrusenie" and ev.data["external_ref"] == "1142/2026"
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    session.refresh(c)
    assert c.status == "zrusene"


# ====================================================================== trvajúca mimoriadnosť nie je jej skončenie
@pytest.mark.parametrize("text", [
    "Mimoriadnosť v ŽST Kraľovany trvá. Prevádzka nebola obnovená.",
    "Mimoriadnosť v ŽST Kraľovany trvá, prevádzka zatiaľ nie je obnovená.",
    "Vlak Pn 54321 stojí v žst. Kraľovany pre poruchu rušňa, hlavná koľaj je obsadená, stanica je prejazdná po 2. "
    "koľaji.",
    "Mimoriadnosť v úseku Kraľovany – Ružomberok: zrážka na priecestí. Trať je zjazdná po 1. traťovej koľaji.",
])
def test_trvajuca_ci_ciastocna_mimoriadnost_nie_je_skoncenie(cfg, text):
    """Zápor („prevádzka nebola obnovená“, „zatiaľ nie je obnovená“), „trvá“ a obnovenie len po jednej koľaji
    neznamenajú skončenie mimoriadnosti – ostáva mimoriadnosťou (prísnejší prípad)."""
    items = rules(text, subject="Informacia o mimoriadnosti Kralovany", received=utc(2026, 11, 2, 7, 30))
    assert [i.kind for i in items] == ["mimoriadnost"]


def test_trvajuca_mimoriadnost_end_to_end_vlak_stoji(env, session):
    """End to end: mimoriadnosť v ŽST Kraľovany (potvrdená, do odvolania); správa „Mimoriadnosť … trvá. Prevádzka
    nebola obnovená.“ nesmie byť zrušením – mimoriadnosť ostáva overená a vlak Vrútky → Ružomberok nedostane
    VYPRAVIŤ ZELENÁ."""
    from vlaky.posta.spracovanie import confirm_event, process_mailbox

    set_fake_now(utc(2026, 11, 2, 4))
    drop(env, "inc.eml", raw_mail("Mimoriadnost Kralovany", "Mimoriadnosť v stanici Kraľovany: porucha "
                                  "zabezpečovacieho zariadenia, stanica je neprejazdná od 02.11.2026 04:30 do "
                                  "odvolania.\n", "kd-inc-5", sender=KD, date="Mon, 02 Nov 2026 04:40:00 +0100"))
    process_mailbox(session)
    session.commit()
    (inc,) = events(session)
    confirm_event(session, inc.id, "dispecer1")
    session.commit()
    now = utc(2026, 11, 2, 7, 30)
    set_fake_now(now)
    drop(env, "upd.eml", raw_mail("Informacia o mimoriadnosti Kralovany", "Mimoriadnosť v ŽST Kraľovany trvá. "
                                  "Prevádzka nebola obnovená.\n", "kd-upd-5", sender=KD,
                                  date="Mon, 02 Nov 2026 08:20:00 +0100"))
    process_mailbox(session, now=now)
    session.commit()
    upd = events(session)[-1]
    assert upd.kind == "mimoriadnost"
    (row,) = [c for c in rows(session) if c.status != "navrh"]
    assert row.status == "overene" and row.from_station == "Kraľovany"
    _not_green_go(_decide(session, "77531", "Vrútky", "Ružomberok", now + timedelta(hours=1), now, hours=6))


@pytest.mark.parametrize("subject,text,speed", [
    ("Ukoncenie mimoriadnej udalosti Ruzomberok", "Oznamujeme ukončenie mimoriadnej udalosti v ŽST Ružomberok. "
     "Prevádzka je obnovená od 02.11.2026 08:15. Očakávajú sa meškania vlakov do 30 minút.", None),
    ("Mimoriadnost Ruzomberok", "Prevádzka v ŽST Ružomberok je obnovená od 02.11.2026 08:15, predpokladané meškanie "
     "vlakov 20 minút.", None),
    ("Mimoriadnost Ruzomberok", "Mimoriadnosť v ŽST Ružomberok bola ukončená o 08:15. Vlaky jazdia s meškaním, odhad "
     "15 min.", None),
    ("Mimoriadnost Kralovany - Ruzomberok", "Prerušenie dopravy v úseku Kraľovany – Ružomberok je ukončené, prevádzka "
     "je obnovená od 10:20.", None),
    ("Mimoriadnost Kralovany", "Prevádzka v stanici Kraľovany je obnovená od 02.11.2026 09:40. Rýchlosť cez stanicu "
     "obmedzená na 30 km/h do odvolania.", ("Kraľovany", 30, True, iso(2026, 11, 2, 8, 40))),
    ("Mimoriadnost", "Prevádzka v úseku Kraľovany – Ružomberok je obnovená s obmedzením rýchlosti na 40 km/h do 18:00.",
     ("Kraľovany", 40, None, None)),
])
def test_skoncenie_s_meskanim_ci_obmedzenim_rychlosti_nie_je_nova_mimoriadnost(cfg, subject, text, speed):
    """MR4: skončenie mimoriadnosti s bežnou vetou o meškaní vlakov („očakávajú sa meškania“, „predpokladané
    meškanie“, „odhad 15 min“), „prerušenie dopravy je ukončené“ či s obmedzením rýchlosti nie je nová mimoriadnosť
    do odvolania; obmedzenie rýchlosti je pomalá jazda (čas obnovenia nie je koniec obmedzenia „do 18:00“)."""
    items = rules(text, subject=subject, received=utc(2026, 11, 2, 7, 30))
    assert [i.kind for i in items] == (["ine", "pomala_jazda"] if speed else ["ine"])
    if speed:
        pj = items[1].data
        assert (pj["from_station"], pj["speed_kmh"], pj.get("until_revoked"), pj["start"]) == speed


# ====================================================================== skončenie mimoriadnosti nezruší inú výluku
def _planned_rov(session) -> Closure:
    rov = Closure(kind="uplna", from_station="Ružomberok", to_station="Liptovský Mikuláš",
                  start_utc=utc(2026, 11, 3, 7), end_utc=utc(2026, 11, 3, 13), status="overene", source="email",
                  external_ref="1150/2026")
    session.add(rov)
    session.commit()
    return rov


END_SECTION = ("Oznamujeme ukončenie mimoriadnej udalosti v ŽST Ružomberok (porucha zabezpečovacieho zariadenia). "
               "Prevádzka v úseku Ružomberok – Liptovský Mikuláš je obnovená od 02.11.2026 08:15.\n")


def test_skoncenie_mimoriadnosti_nezrusi_planovanu_vyluku(env, session, no_planner):
    """Oznámenie o skončení mimoriadnosti (bez čísla) pri plánovanej výluke ROV č. 1150/2026 na tom istom úseku:
    neponúkne jej zrušenie a ani potvrdenie ju nezruší; nevznikne ani nový záznam „navrh“."""
    from vlaky.posta.spracovanie import confirm_event, process_mailbox

    set_fake_now(utc(2026, 11, 2, 7, 30))
    rov = _planned_rov(session)
    drop(env, "end.eml", raw_mail("Ukoncenie mimoriadnej udalosti Ruzomberok", END_SECTION, "kd-end-2", sender=KD,
                                  date="Mon, 02 Nov 2026 08:20:00 +0100"))
    process_mailbox(session)
    session.commit()
    (ev,) = events(session)
    assert ev.kind == "ine"
    assert not any("Zruší výluku" in w for w in ev.validation.get("warnings") or [])
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    session.refresh(rov)
    assert rov.status == "overene"
    assert [c.id for c in rows(session)] == [rov.id]


def test_skoncenie_mimoriadnosti_v_stanici_nezrusi_vyluku_na_useku(env, session, no_planner):
    """Mimoriadnosť ohlásená „v ŽST Ružomberok“ (potvrdená) a plánovaný ROV Ružomberok – Liptovský Mikuláš na druhý
    deň: oznámenie o skončení s úsekom Ružomberok – Liptovský Mikuláš ROV nezruší a mimoriadnosť sa sama nezruší
    (ukončí ju dispečer v registri)."""
    from vlaky.posta.spracovanie import confirm_event, process_mailbox

    set_fake_now(utc(2026, 11, 2, 4))
    rov = _planned_rov(session)
    drop(env, "inc.eml", raw_mail("Mimoriadnost Ruzomberok", "Mimoriadnosť v ŽST Ružomberok: porucha zabezpečovacieho "
                                  "zariadenia, stanica je neprejazdná od 02.11.2026 04:30.\n", "kd-inc-3", sender=KD,
                                  date="Mon, 02 Nov 2026 04:40:00 +0100"))
    process_mailbox(session)
    session.commit()
    (inc,) = events(session)
    confirm_event(session, inc.id, "dispecer1")
    session.commit()
    set_fake_now(utc(2026, 11, 2, 7, 30))
    drop(env, "end.eml", raw_mail("Ukoncenie mimoriadnej udalosti Ruzomberok", END_SECTION, "kd-end-3", sender=KD,
                                  date="Mon, 02 Nov 2026 08:20:00 +0100"))
    process_mailbox(session)
    session.commit()
    end = events(session)[-1]
    assert end.kind == "ine"
    confirm_event(session, end.id, "dispecer1")
    session.commit()
    session.refresh(rov)
    assert rov.status == "overene"
    incident = next(c for c in rows(session) if c.kind == "mimoriadnost")
    assert incident.status == "overene" and incident.from_station == "Ružomberok"


def test_llm_mimoriadnost_pri_oznameni_o_skonceni_nie_je_nova_udalost(cfg):
    """S LLM: mimoriadnosť, ktorú LLM našiel v oznámení o skončení, sa spojí s „iné“ z pravidiel (nezhoda druhu,
    nižšia istota) – nevznikne samostatná mimoriadnosť, z ktorej by bol záznam „navrh“ do odvolania."""
    from vlaky.posta.extrakcia import combine
    from vlaky.posta.udalosti import ExtractedItem

    rule_items = rules(END_SECTION, subject="Ukoncenie mimoriadnej udalosti Ruzomberok", received=utc(2026, 11, 2, 7, 30))
    llm = [ExtractedItem("mimoriadnost", {"from_station": "Ružomberok", "to_station": "Liptovský Mikuláš",
                                          "start": iso(2026, 11, 2, 7, 15), "end": None, "description": "x"}, 0.8)]
    out = combine(rule_items, llm)
    assert [i.kind for i in out] == ["ine"]
    assert any("druh udalosti" in n for n in out[0].notes)


# ====================================================================== mimoriadnosť a okno náhradnej dopravy
@pytest.mark.parametrize("text,window", [
    ("Mimoriadnosť v úseku Kraľovany – Ružomberok: prerušená doprava od 02.11.2026 04:30. Náhradná autobusová doprava "
     "02.11.2026 od 6:00 do 22:00.", "22:00"),
    ("V žst. Kraľovany došlo 02.11.2026 o 04:30 k vykoľajeniu vozňa, stanica je neprejazdná. Dispečer je k dispozícii "
     "02.11.2026 v čase 6:00 – 14:00 na tel. 0900 000 000.", "14:00"),
    ("Mimoriadnosť v ŽST Kraľovany od 02.11.2026 04:30, stanica neprejazdná. Vlaky osobnej dopravy nahradí autobus "
     "02.11. 05:00 – 20:00.", "20:00"),
])
def test_mimoriadnost_bez_konca_nevezme_okno_nahradnej_dopravy(cfg, text, window):
    """PR4: mimoriadnosť s uvedeným začiatkom a bez konca si nevezme okno náhradnej dopravy či kontaktu na dispečera
    (začiatok ostáva 04:30, koniec nie je určený – do odvolania) a dispečer dostane „overte koniec“ s týmto oknom."""
    (it,) = rules(text, subject="Mimoriadnost", received=utc(2026, 11, 2, 3, 50))
    assert it.kind == "mimoriadnost"
    assert (it.data["start"], it.data["end"]) == (iso(2026, 11, 2, 3, 30), None)
    note = next(n for n in it.notes if "overte koniec" in n)
    assert window in note


def test_mimoriadnost_s_nahradnou_dopravou_v_inej_casti_vety_si_ponecha_koniec(cfg):
    """PR4: uvedené „od … do …“ mimoriadnosti platí, aj keď za čiarkou je zmienka o náhradnej doprave (okno sa
    vylúči, len keď je v časti vety o náhradnej doprave či kontakte)."""
    (it,) = rules("Stanica Kraľovany je neprejazdná od 02.11.2026 04:30 do 02.11.2026 13:00, zavedená je náhradná "
                  "autobusová doprava.", subject="Mimoriadnosť Kraľovany")
    assert it.kind == "mimoriadnost"
    assert (it.data["start"], it.data["end"]) == (iso(2026, 11, 2, 3, 30), iso(2026, 11, 2, 12))


def test_mimoriadnost_s_nahradnou_dopravou_end_to_end(env, session):
    """PR4 end to end: prerušená doprava od 04:30 (bez konca) s náhradnou dopravou 6:00 – 22:00 – vlak o 22:30
    nedostane VYPRAVIŤ ZELENÁ (mimoriadnosť trvá do odvolania, nie do 22:00)."""
    from vlaky.posta.spracovanie import process_mailbox

    now = utc(2026, 11, 2, 3, 50)
    set_fake_now(now)
    drop(env, "inc.eml", raw_mail("Mimoriadnost Kralovany - Ruzomberok", "Mimoriadnosť v úseku Kraľovany – Ružomberok: "
                                  "prerušená doprava od 02.11.2026 04:30. Náhradná autobusová doprava 02.11.2026 od "
                                  "6:00 do 22:00.\n", "kd-inc-bus", sender=KD, date="Mon, 02 Nov 2026 04:45:00 +0100"))
    process_mailbox(session, now=now)
    session.commit()
    (row,) = rows(session)
    assert as_utc(row.start_utc) == utc(2026, 11, 2, 3, 30) and row.until_revoked
    _not_green_go(_decide(session, "77521", "Vrútky", "Liptovský Mikuláš", utc(2026, 11, 2, 21, 30), now))


# ====================================================================== MR0: noc zapísaná dvoma dátumami s lomkou
@pytest.mark.parametrize("term", ["v noci 12.11.2026/13.11.2026 od 0:30 do 4:30",
                                  "v noci 12.11./13.11.2026 od 0:30 do 4:30"])
def test_noc_dvoma_datumami_s_lomkou_je_v_druhom_dni(cfg, term):
    """MR0: „v noci 12.11.2026/13.11.2026 od 0:30 do 4:30“ je 13.11. 0:30 – 4:30 (nie 12.11. – „13.11“ nie je čas
    13:11)."""
    items = rules(f"{SEC} {term}.")
    assert terms(items) == [(iso(2026, 11, 12, 23, 30), iso(2026, 11, 13, 3, 30))]
    assert all(i.confidence == 0.85 for i in items), [i.notes for i in items]


def test_dva_dni_s_lomkou_bez_noci_su_oba_dni(cfg):
    """„12.11.2026/13.11.2026 od 8:00 do 14:00“ (bez označenia noci) sú oba dni – ako „12./13.11.“."""
    items = rules(f"{SEC} 12.11.2026/13.11.2026 od 8:00 do 14:00.")
    assert terms(items) == [(iso(2026, 11, 12, 7), iso(2026, 11, 12, 13)), (iso(2026, 11, 13, 7), iso(2026, 11, 13, 13))]
    assert all(i.confidence == 0.85 for i in items), [i.notes for i in items]


def test_noc_s_lomkou_end_to_end_planovac_nevypravi_do_vyluky(env, session):
    """MR0 end to end: ROV „v noci 12.11.2026/13.11.2026 od 0:30 do 4:30“ – záznam „navrh“ na skutočnú noc a vlak
    13.11. o 01:00 nedostane VYPRAVIŤ ZELENÁ."""
    from vlaky.posta.spracovanie import process_mailbox

    now = utc(2026, 11, 12, 17)                                   # 12.11. 18:00 miestneho času
    set_fake_now(now)
    drop(env, "rov.eml", raw_mail("ROV c. 2500/2026 Poprad-Tatry - Liptovsky Mikulas", "Rozkaz o výluke č. 2500/2026\n"
                                  "Úplná výluka oboch traťových koľají v úseku Poprad-Tatry – Liptovský Mikuláš v noci "
                                  "12.11.2026/13.11.2026 od 0:30 do 4:30.\n", "rov2500",
                                  date="Thu, 12 Nov 2026 18:00:00 +0100"))
    process_mailbox(session, now=now)
    session.commit()
    (row,) = rows(session)
    assert (as_utc(row.start_utc), as_utc(row.end_utc)) == (utc(2026, 11, 12, 23, 30), utc(2026, 11, 13, 3, 30))
    assert not any("Platnosť už skončila." in e.validation["warnings"] for e in events(session))
    _not_green_go(_decide(session, "77501", "Poprad-Tatry", "Liptovský Mikuláš", utc(2026, 11, 13, 0), now, hours=6))


# ====================================================================== MR1: dátum v odpovedi, ktorý nie je termínom
SUBJ_1142 = "RE: ROV c. 1142/2026 Poprad-Tatry - Liptovsky Mikulas"
QUOTED_1142 = ("\n\n-----Original Message-----\nFrom: ŽSR OR Poprad <vyluky.orpp@zsr.example>\n"
               "Sent: Monday, November 2, 2026 9:00 AM\nSubject: ROV c. 1142/2026 Poprad-Tatry - Liptovsky Mikulas\n\n"
               "Rozkaz o výluke č. 1142/2026: úplná výluka oboch traťových koľají v úseku Poprad-Tatry – Liptovský "
               "Mikuláš 12.11.2026 od 8:00 do 14:00.\n")


@pytest.mark.parametrize("subject,body", [
    (SUBJ_1142, "Dobrý deň,\nvýluka podľa ROV č. 1142/2026 platí, ako bolo oznámené 30.10.2026." + QUOTED_1142),
    (SUBJ_1142, "Dobrý deň,\nna Vašu otázku zo dňa 2.11.2026: výluka podľa ROV č. 1142/2026 platí bez zmeny."
     + QUOTED_1142),
    (SUBJ_1142, "Dobrý deň,\npotvrdzujeme výluku podľa ROV č. 1142/2026 bez zmeny.\n\nS pozdravom\nŽSR OR Poprad, "
     "3.11.2026" + QUOTED_1142),
    # bez predmetu odpovede, citácia „>“ a dátum prijatia
    ("ROV c. 1142/2026 Poprad-Tatry - Liptovsky Mikulas", "Na vedomie, prijaté 2.11.2026.\n> Rozkaz o výluke č. "
     "1142/2026: úplná výluka oboch traťových koľají v úseku Poprad-Tatry – Liptovský Mikuláš 12.11.2026 od 8:00 do "
     "14:00.\n"),
])
def test_odpoved_s_datumom_ktory_nie_je_terminom_zachova_citovany_termin(cfg, subject, body):
    """MR1: nový text odpovede s dátumom, ktorý nie je termínom (oznámenie, „zo dňa“, podpis, prijatie), citáciu
    nevymaže – termín z citovaného ROV ostáva (inak by sa stratil a plánovač by výluku nevidel)."""
    (it,) = rules(body, subject=subject, received=utc(2026, 11, 3, 8))
    assert (it.data["start"], it.data["end"]) == (iso(2026, 11, 12, 7), iso(2026, 11, 12, 13))


def test_odpoved_s_datumom_oznamenia_end_to_end_planovac(env, session):
    """MR1 end to end: prvé oznámenie ROV č. 1142/2026 príde ako odpoveď „… ako bolo oznámené 30.10.2026“ nad
    citovaným ROV – záznam „navrh“ na 12.11. 8:00 – 14:00 a vlak o 09:00 nedostane VYPRAVIŤ ZELENÁ."""
    from vlaky.posta.spracovanie import process_mailbox

    now = utc(2026, 11, 12, 6)                                    # 12.11. 07:00 miestneho času
    set_fake_now(now)
    body = "Dobrý deň,\nvýluka podľa ROV č. 1142/2026 platí, ako bolo oznámené 30.10.2026." + QUOTED_1142
    drop(env, "re.eml", raw_mail(SUBJ_1142, body, "re-nonterm", date="Thu, 12 Nov 2026 06:30:00 +0100"))
    process_mailbox(session, now=now)
    session.commit()
    (row,) = rows(session)
    assert (as_utc(row.start_utc), as_utc(row.end_utc)) == (utc(2026, 11, 12, 7), utc(2026, 11, 12, 13))
    _not_green_go(_decide(session, "77511", "Poprad-Tatry", "Liptovský Mikuláš", utc(2026, 11, 12, 8), now))


# ====================================================================== MR6: zoznam s januárom pred decembrom
def test_zoznam_januar_pred_decembrom_nie_je_minuly_januar(cfg):
    """MR6 (opačný prípad): „2.1. a 28.12.2026“ (prijaté 2.11.2026) – 2.1.2027 s „overte termín“, nie 2.1.2026
    (desať mesiacov v minulosti) s plnou istotou."""
    items = rules(f"{SEC} 2.1. a 28.12.2026 od 8:00 do 14:00.")
    assert terms(items) == [(iso(2026, 12, 28, 7), iso(2026, 12, 28, 13)), (iso(2027, 1, 2, 7), iso(2027, 1, 2, 13))]
    jan = next(i for i in items if i.data["start"].startswith("2027-01-02"))
    assert jan.confidence < 0.8 and any("overte termín" in n for n in jan.notes)
    dec = next(i for i in items if i.data["start"].startswith("2026-12-28"))
    assert dec.confidence == 0.85
