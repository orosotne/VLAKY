"""5. revízia – pravidlá e-mailov ŽSR (oblasť A): nálezy R5F0 – R5F4, R5P0, R5C0 – R5C7 a všeobecné kontroly.

- druhý termín výluky s poznámkou (NAD, autobusy) sa nestratí; vedľajšia informácia v inom termíne je poznámka;
- „…, vlaková doprava nebude realizovaná“ výluku neruší (podmet je doprava, nie výluka);
- „trať … úplne uzavretá“, „úplná uzávera trate“ s číslom koľaje je úplná výluka; druh ani koľaj sa neberú z vety
  inej udalosti;
- obmedzenie nákladných vlakov po obnovení prevádzky („môžu ísť od 15:00“, „zákaz … nebol zrušený“, aj po
  „mimoriadna udalosť bola ukončená“) je mimoriadnosť do toho času, nie oznámenie o skončení;
- úsek „žst. A – žst. B“, „medzi ŽST A a ŽST B“ je úsek (nie jedna stanica);
- „zákaz jazdy (všetkých) vlakov“, „1. aj 2. TK“, „TV … jazda motorových vlakov nie je možná“ sú úplné výluky;
- zrušenie jedného úseku neruší úsek, ktorý podľa e-mailu ostáva; zákaz jazdy bez slova výluka je mimoriadnosť;
- stanica v genitíve, skratky názvov, KD len s časom, tabuľka „Dátum | Od | Do“, HTML bez textu, anglické avízo,
  trasa v próze, česká objednávka;
- všeobecné kontroly: prísnejší druh pri slovách úplného uzavretia (poznámka „overte druh“), sieť proti strate
  obsahu od ŽSR, informatívny prehľad a porada nič nezapíšu.

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from vlaky.models import Path as TrainPath, Train

from test_revizia3_terminy import as_utc, events, raw_mail, utc
from test_revizia4_pravidla4_zapis import ROV_KE, ROV_ZA, _active, _confirm_all, _decide, _process, env  # noqa: F401

TZ = ZoneInfo("Europe/Bratislava")


def _x(subject: str, body: str, received: datetime = utc(2026, 11, 9, 8), sender: str = "vyluky.orke@zsr.example",
       attachments: list | None = None):
    from vlaky.posta.extrakcia import extract
    from vlaky.posta.udalosti import MailDocument

    doc = MailDocument(subject=subject, body=body, sender=sender, received_at=received, attachments=attachments or [])
    return extract(doc, use_llm=False).items


def _drop(env, name: str, data: bytes, received: datetime) -> None:
    """E-mail do schránky s časom doručenia ``received`` (čas súboru) – termín bez dátumu („o 11:40“) dostane deň
    doručenia nezávisle od skutočného dátumu, keď test beží."""
    path = Path(env.mail_dir) / name
    path.write_bytes(data)
    os.utime(path, (received.timestamp(), received.timestamp()))


def _loc(v) -> str | None:
    if not v:
        return None
    dt = datetime.fromisoformat(v)
    return dt.astimezone(TZ).strftime("%d.%m. %H:%M")


def _ev(items) -> list[tuple]:
    return [(i.kind, i.data.get("kind"), i.data.get("from_station"), i.data.get("to_station"),
             _loc(i.data.get("start")), _loc(i.data.get("end"))) for i in items]


def _notes(items) -> str:
    return " ".join(n for i in items for n in i.notes)


KK = "Úplná výluka v úseku Kysak – Košice bude dňa 20.11.2026 od 08:00 do 14:00"


# ====================================================================== R5F0
def test_r5f0_druhy_termin_s_poznamkou_sa_nestrati(cfg):
    """R5F0: termín s poznámkou v zátvorke či za pomlčkou („(NAD)“, „– zabezpečená NAD“, položka „- 27.11. … (NAD)“)
    je termín výluky – nie vedľajšia informácia; NAD v inom termíne (samostatná veta) ostane aspoň v poznámke."""
    for body in ("úplná výluka v úseku Kysak – Košice bude v termínoch 20.11.2026 od 08:00 do 14:00, 27.11.2026 od "
                 "08:00 do 14:00 (v oboch termínoch NAD).",
                 "oznamujeme úplnú výluku v úseku Kysak – Košice (ROV č. 1501/2026) v termínoch:\n- 20.11.2026 od 08:00 do "
                 "14:00\n- 27.11.2026 od 08:00 do 14:00 (NAD)",
                 "oznamujeme úplnú výluku v úseku Kysak – Košice dňa 20.11.2026 od 08:00 do 14:00 (ROV č. 1501/2026).\n"
                 "Ďalšia výluka: 27.11.2026 od 08:00 do 14:00 (zabezpečená náhradná autobusová doprava).",
                 "úplná výluka v úseku Kysak – Košice bude dňa 20.11.2026 od 08:00 do 14:00 a dňa 27.11.2026 od 08:00 do "
                 "14:00 – zabezpečená NAD.",
                 "oznamujeme úplnú výluku v úseku Kysak – Košice v termínoch:\n1. 20.11.2026 od 08:00 do 14:00\n2. "
                 "27.11.2026 od 08:00 do 14:00 (NAD)"):
        items = _x("ROV č. 1501/2026 – výluka Kysak – Košice", "Dobrý deň,\n\n" + body + "\n")
        starts = sorted(_loc(i.data["start"]) for i in items if i.kind == "vyluka_nova")
        assert starts == ["20.11. 08:00", "27.11. 08:00"], (body, _ev(items))
    items = _x("ROV č. 1501/2026 – výluka Kysak – Košice", "oznamujeme úplnú výluku v úseku Kysak – Košice dňa "
               "20.11.2026 od 08:00 do 14:00.\nDňa 27.11.2026 od 08:00 do 14:00 bude v úseku Kysak – Košice zavedená NAD.\n")
    assert [e[4] for e in _ev(items)] == ["20.11. 08:00"]
    assert "27.11.2026" in _notes(items) and "overte" in _notes(items)
    # vedľajšia informácia v termíne výluky (náhradná doprava počas výluky, platnosť rozkazu) poznámku nedá
    items = _x("ROV č. 1502/2026", "Úplná výluka v úseku Kysak – Košice dňa 20.11.2026 od 08:00 do 14:00.\nNáhradná "
               "autobusová doprava bude zabezpečená dňa 20.11.2026 od 08:00 do 14:00.\n")
    assert len(items) == 1 and "nepodarilo priradiť" not in _notes(items)


def test_r5f0_e2e_vlak_v_druhom_termine_nedostane_vypravit(env, session):
    """R5F0 v toku pošty: po potvrdení sú v registri oba termíny a vlak 27.11. o 10:00 nedostane VYPRAVIŤ."""
    _drop(env, "f0.eml", raw_mail("ROV c. 1501/2026 - vyluka Kysak - Kosice",
                                 "úplná výluka v úseku Kysak – Košice bude v termínoch 20.11.2026 od 08:00 do 14:00, "
                                 "27.11.2026 od 08:00 do 14:00 (v oboch termínoch NAD).\n", "r5f0",
                                 sender=ROV_KE, date="Mon, 09 Nov 2026 09:00:00 +0100"), utc(2026, 11, 9, 8))
    _process(session, utc(2026, 11, 9, 8, 2))
    _confirm_all(session)
    assert sorted(as_utc(c.start_utc) for c in _active(session)) == [utc(2026, 11, 20, 7), utc(2026, 11, 27, 7)]
    r = _decide(session, "77501", "Kysak", "Košice", utc(2026, 11, 27, 9), utc(2026, 11, 27, 5))
    assert r.decision != "VYPRAVIŤ", r.summary


# ====================================================================== R5F1
def test_r5f1_zapor_inej_dopravy_nie_je_zrusenie(cfg):
    """R5F1: vedľajšia veta s iným podmetom („vlaková/osobná doprava, preprava, vlaky, jazdy, porada … nebude
    realizovaná / nekoná sa / odpadajú“) výluku neruší; skutočné zrušenia ostávajú zrušeniami."""
    tails = [", vlaková doprava nebude v tomto úseku realizovaná.", ", osobná doprava nebude v tomto čase realizovaná.",
             ", preprava cestujúcich nebude v tomto čase uskutočnená.",
             "; v čase výluky nebude v úseku Kysak – Košice doprava realizovaná.", ", doprava nebude realizovaná.",
             ", vlaky osobnej dopravy nebudú v tomto čase realizované.", ", počas ktorej nebude vlaková doprava "
             "realizovaná.", ", jazdy nákladných vlakov nebudú v tomto čase uskutočnené.", ". Porada k výluke sa "
             "nebude konať.", ", vlaky osobnej dopravy v tomto čase odpadajú.", ", vlaková doprava sa v tomto úseku "
             "nekoná."]
    for tail in tails + [" – nákladná doprava nebude v tomto čase realizovaná."]:
        items = _x("ROV č. 1601/2026 – výluka Kysak – Košice", KK + tail + "\n")
        assert [e[:5] for e in _ev(items)] == [("vyluka_nova", "uplna", "Kysak", "Košice", "20.11. 08:00")], (tail,
                                                                                                             _ev(items))
    for body in ("Výluka podľa ROV č. 2250/2026 (Leopoldov – Trnava, 25.11.2026) bola zrušená.",
                 "Výluka v úseku Leopoldov – Trnava plánovaná na 25.11.2026 sa neuskutoční.",
                 "Výluka podľa ROV 2250/2026 nebude z dôvodu nepriaznivého počasia realizovaná.",
                 "Oznamujeme, že výluka v úseku Kysak – Košice dňa 20.11.2026, z dôvodu počasia nebude vykonaná.",
                 "Výluka podľa ROV 2250/2026, ktorá mala byť dňa 25.11.2026 od 8:00 do 12:00, nebude vykonaná."):
        items = _x("Informácia o výluke", body + "\n")
        assert [i.kind for i in items] == ["vyluka_zrusenie"], (body, _ev(items))


def test_r5f1_e2e_pripomenutie_nezrusi_overenu_vyluku(env, session):
    """R5F1 v toku pošty: overená výluka ROV 1601/2026 a pripomenutie s vetou „vlaková doprava nebude … realizovaná“
    – nevznikne zrušenie, výluka ostane overená a vlak o 10:00 nedostane VYPRAVIŤ."""
    body = KK + ", vlaková doprava nebude v tomto úseku realizovaná.\n"
    _drop(env, "a.eml", raw_mail("ROV c. 1601/2026 - vyluka Kysak - Kosice", body, "r5f1a", sender=ROV_KE,
                                date="Mon, 09 Nov 2026 09:00:00 +0100"), utc(2026, 11, 9, 8))
    _process(session, utc(2026, 11, 9, 8, 2))
    _confirm_all(session)
    _drop(env, "b.eml", raw_mail("ROV c. 1601/2026 - pripomenutie", body, "r5f1b", sender=ROV_KE,
                                date="Wed, 18 Nov 2026 09:00:00 +0100"), utc(2026, 11, 18, 8))
    _process(session, utc(2026, 11, 18, 8, 2))
    assert not any(e.kind == "vyluka_zrusenie" for e in events(session))
    _confirm_all(session)
    assert [(c.status, as_utc(c.start_utc)) for c in _active(session)] == [("overene", utc(2026, 11, 20, 7))]
    r = _decide(session, "77511", "Kysak", "Košice", utc(2026, 11, 20, 9), utc(2026, 11, 20, 5))
    assert r.decision != "VYPRAVIŤ", r.summary


# ====================================================================== R5F2, R5F3
def test_r5f2_uplne_uzavreta_trat_s_cislom_kolaje_je_uplna(cfg):
    """R5F2: „bude trať … úplne uzavretá z dôvodu výluky 1. TK“, „úsek … úplne uzavretý“, „úplná uzávera/uzávierka
    trate“, „trať úplne zatvorená“ – úplná výluka; „úplnej rekonštrukcie“, „úplná uzávierka cesty“ a „trať nebude
    úplne uzavretá“ ostávajú výlukou koľaje."""
    for body in ("Dňa 20.11.2026 od 08:00 do 14:00 bude trať v úseku Kysak – Košice úplne uzavretá z dôvodu výluky 1. "
                 "TK a výmeny výhybky.",
                 "Z dôvodu výluky 2. TK bude dňa 20.11.2026 od 08:00 do 14:00 úsek Kysak – Košice úplne uzavretý.",
                 "Výluka 1. TK v úseku Kysak – Košice dňa 20.11.2026 od 08:00 do 14:00, v čase od 08:00 do 14:00 úplná "
                 "uzávera trate.",
                 "Výluka 1. TK v úseku Kysak – Košice dňa 20.11.2026 od 06:00 do 18:00, od 10:00 do 12:00 úplná "
                 "uzávierka trate.",
                 "Výluka 1. TK v úseku Kysak – Košice dňa 20.11.2026 od 06:00 do 18:00, od 10:00 do 12:00 bude trať "
                 "úplne uzatvorená."):
        kinds = {i.data.get("kind") for i in _x("ROV č. 1701/2026", body + "\n") if i.kind == "vyluka_nova"}
        assert kinds == {"uplna"}, (body, kinds)
    for body in ("Výluka 1. TK v úseku Kysak – Košice dňa 20.11.2026 od 08:00 do 14:00 z dôvodu úplnej rekonštrukcie "
                 "výhybky.",
                 "Výluka 1. TK v úseku Kysak – Košice dňa 20.11.2026 od 08:00 do 14:00, úplná uzávierka cesty pri "
                 "priecestí.",
                 "Výluka 1. TK v úseku Kysak – Košice dňa 20.11.2026 od 08:00 do 14:00. Trať nebude úplne uzavretá, "
                 "vlaky po 2. TK."):
        kinds = {i.data.get("kind") for i in _x("ROV č. 1702/2026", body + "\n")}
        assert kinds == {"kolaj"}, (body, kinds)


def test_r5f3_druh_a_kolaj_nie_z_vety_inej_udalosti(cfg):
    """R5F3: „Výluka v úseku Kysak – Košice …“ bez druhu nedostane trakčné vedenie ani 1. TK z vety o úseku
    Košice – Čaňa; veta bez úseku a termínu („Vylúčená bude 1. TK.“) druh určuje."""
    for other, kind in (("napäťová výluka TV", "trakcne_vedenie"), ("výluka 1. TK", "kolaj")):
        items = _x("ROV č. 1701/2026", "Výluka v úseku Kysak – Košice bude dňa 20.11.2026 od 08:00 do 14:00. Dňa "
                   f"21.11.2026 od 08:00 do 14:00 bude {other} v úseku Košice – Čaňa.\n")
        got = {i.data["from_station"]: (i.data.get("kind"), i.data.get("track")) for i in items}
        assert got["Kysak"] == ("uplna", None) and got["Košice"][0] == kind, got
    items = _x("ROV č. 1703/2026", "Výluka v úseku Kysak – Košice bude dňa 20.11.2026 od 08:00 do 14:00. Vylúčená "
               "bude 1. TK.\n")
    assert [(i.data.get("kind"), i.data.get("track")) for i in items] == [("kolaj", "1")]


def test_r5f2_e2e_vlak_nedostane_vypravit_cez_uzavretu_trat(env, session):
    """R5F2 v toku pošty: po potvrdení je výluka úplná a elektrický vlak o 10:00 nedostane VYPRAVIŤ."""
    _drop(env, "f2.eml", raw_mail("ROV c. 1701/2026", "Dňa 20.11.2026 od 08:00 do 14:00 bude trať v úseku Kysak – Košice "
                                 "úplne uzavretá z dôvodu výluky 1. TK a výmeny výhybky.\n", "r5f2", sender=ROV_KE,
                                 date="Mon, 09 Nov 2026 09:00:00 +0100"), utc(2026, 11, 9, 8))
    _process(session, utc(2026, 11, 9, 8, 2))
    _confirm_all(session)
    assert [c.kind for c in _active(session)] == ["uplna"]
    r = _decide(session, "77521", "Kysak", "Košice", utc(2026, 11, 20, 9), utc(2026, 11, 20, 5))
    assert r.decision != "VYPRAVIŤ", r.summary


# ====================================================================== R5F4, R5C4
def test_r5f4_r5c4_obmedzenie_nakladnych_vlakov_po_obnoveni(cfg):
    """R5F4 / R5C4: obnovenie prevádzky len pre osobné vlaky – nákladné „môžu ísť od 15:00“, „budú vypravené od
    15:00“, „zákaz … zatiaľ nebol zrušený“, aj za „Mimoriadna udalosť … bola ukončená“ a v e-maile bez slova
    mimoriadnosť – je mimoriadnosť do toho času (nie oznámenie o skončení s radou „ukončite ju“)."""
    p = ("mimoriadnosť v úseku Kraľovany – Ružomberok (strhnuté trakčné vedenie) – prevádzka bola o 10:30 obnovená "
         "pre osobné vlaky. ")
    for tail, end in (("Nákladné vlaky môžu ísť od 15:00.", "09.11. 15:00"),
                      ("Nákladné vlaky budú vypravené od 15:00.", "09.11. 15:00"),
                      ("Nákladné vlaky sa vypravujú od 15:00.", "09.11. 15:00"),
                      ("Zákaz jazdy nákladných vlakov zatiaľ nebol zrušený.", None)):
        items = _x("Mimoriadnosť Kraľovany – Ružomberok – obnovenie", p + tail + "\n", utc(2026, 11, 9, 9, 35),
                   "kdz.zilina@zsr.example")
        assert _ev(items) == [("mimoriadnost", None, "Kraľovany", "Ružomberok", "09.11. 10:30", end)], (tail, _ev(items))
        assert "ukončite ju" not in _notes(items)
    for subject, body in (("MU Plešivec ukončená", "Mimoriadna udalosť v žst. Plešivec bola ukončená o 11:40 hod. "
                           "Nákladné vlaky môžu stanicou prechádzať až od 18:00 hod."),
                          ("Ukončenie MU Plešivec", "Ukončenie mimoriadnej udalosti v žst. Plešivec o 11:40 hod. Pre "
                           "vlaky nákladnej dopravy je stanica uzavretá do 18:00 hod."),
                          ("MU – žst. Plešivec – obnovenie", "Prevádzka v žst. Plešivec bola o 11:40 hod. obnovená pre "
                           "osobné vlaky. Nákladné vlaky môžu stanicou Plešivec prechádzať až od 18:00 hod.")):
        items = _x(subject, body + "\n", utc(2026, 11, 30, 10, 50), "kd@orke.zsr.example")
        assert _ev(items) == [("mimoriadnost", None, "Plešivec", "Plešivec", "30.11. 11:40", "30.11. 18:00")], \
            (body, _ev(items))
    for tail in ("Odstavené nákladné vlaky pokračujú v jazde.", "Zákaz jazdy nákladných vlakov bol zrušený.",
                 "Nákladné vlaky budú vypravené postupne."):
        items = _x("Mimoriadnosť Kraľovany – Ružomberok – obnovenie", "Prevádzka v úseku Kraľovany – Ružomberok je "
                   "obnovená o 10:30. " + tail + "\n", utc(2026, 11, 9, 9, 35), "kdz.zilina@zsr.example")
        assert [i.kind for i in items] == ["ine"] and "ukončite ju" in _notes(items), (tail, _ev(items))


def test_r5f4_e2e_nakladny_vlak_po_ukonceni_mu_nedostane_vypravit(env, session):
    """R5F4 / R5C4 v toku pošty: „Mimoriadna udalosť v žst. Plešivec bola ukončená o 11:40 hod. Nákladné vlaky môžu
    stanicou prechádzať až od 18:00 hod.“ – po potvrdení je v registri mimoriadnosť Plešivec do 18:00 a motorový
    nákladný vlak Rožňava → Tornaľa o 13:00 nedostane VYPRAVIŤ (predtým „iné“ s radou mimoriadnosť ukončiť)."""
    from vlaky.agent.zdravie import set_kv
    from vlaky.planovanie.planovac import evaluate_train

    _drop(env, "f4.eml", raw_mail("MU Plesivec ukoncena", "Mimoriadna udalosť v žst. Plešivec bola ukončená o 11:40 hod. "
                                 "Nákladné vlaky môžu stanicou prechádzať až od 18:00 hod.\n", "r5f4", sender=ROV_KE,
                                 date="Mon, 30 Nov 2026 11:50:00 +0100"), utc(2026, 11, 30, 10, 50))
    _process(session, utc(2026, 11, 30, 10, 52))
    assert [e.kind for e in events(session)] == ["mimoriadnost"]
    _confirm_all(session)
    assert [(c.from_station, as_utc(c.end_utc)) for c in _active(session)] == [("Plešivec", utc(2026, 11, 30, 17))]
    dep, now = utc(2026, 11, 30, 12), utc(2026, 11, 30, 10, 55)
    t = Train(number="47501", origin="Rožňava", destination="Tornaľa", traction="M", gauge="1435", vmax_kmh=80,
              status="planovany", ready_utc=dep, deadline_utc=dep + timedelta(hours=8))
    session.add(t)
    session.flush()
    session.add(TrainPath(train_id=t.id, train_number="47501", kind="ad_hoc", status="pridelena", departure_utc=dep))
    set_kv(session, "mail_last_ok", {"ts": (now - timedelta(minutes=2)).isoformat(), "fetched": 0})
    session.commit()
    r = evaluate_train(session, t.id, now, "test R5F4")
    assert r.decision != "VYPRAVIŤ", r.summary


# ====================================================================== R5P0, R5C3, QM2, QC2
@pytest.mark.parametrize("sec", ["v úseku žst. Púchov – žst. Žilina", "v úseku Púchov – ŽST Žilina",
                                 "medzi ŽST Púchov a ŽST Žilina", "v medzistaničnom úseku žel. st. Púchov – žel. st. "
                                 "Žilina", "v traťovom úseku z. st. Púchov – z. st. Žilina",
                                 "v úseku Žst. Púchov - Žst. Žilina"])
def test_r5p0_usek_s_oznacenim_stanice(cfg, sec):
    """R5P0: úsek s označením dopravne („žst.“, „ŽST“, „žel. st.“, „z. st.“) je úsek A – B, nie jedna stanica –
    vo výluke, mimoriadnosti aj pomalej jazde."""
    for subject, body in (
            ("Oznámenie", f"Úplná výluka oboch traťových koľají {sec} dňa 02.11.2026 od 12:00 do 20:00 hod. (ROV č. "
                          "4415/2026)."),
            ("Oznámenie", f"Mimoriadnosť: {sec} porucha trakčného vedenia, doprava je prerušená od 02.11.2026 12:00 do "
                          "02.11.2026 20:00."),
            ("Oznámenie", f"Pomalá jazda 30 km/h {sec} od 02.11.2026 12:00 do 02.11.2026 20:00 (POTR č. 15/2026).")):
        items = _x(subject, body + "\n", utc(2026, 11, 2, 6), "vyluky.orza@zsr.example")
        assert [(i.data["from_station"].replace("žst. ", ""), i.data["to_station"]) for i in items] == \
            [("Púchov", "Žilina")], (body, _ev(items))


def test_r5p0_e2e_vlak_na_useku_nedostane_vypravit(env, session):
    """R5P0 v toku pošty: vlak Púchov → Považská Bystrica (na úseku, mimo Žiliny) nedostane VYPRAVIŤ pred
    potvrdením ani po ňom."""
    _drop(env, "p0.eml", raw_mail("ROV c. 4415/2026", "Úplná výluka oboch traťových koľají v úseku žst. Púchov – žst. "
                                 "Žilina dňa 02.11.2026 od 12:00 do 20:00 hod. (ROV č. 4415/2026).\n", "r5p0",
                                 sender=ROV_ZA, date="Mon, 02 Nov 2026 07:00:00 +0100"), utc(2026, 11, 2, 6))
    _process(session, utc(2026, 11, 2, 6, 2))
    r = _decide(session, "95501", "Púchov", "Považská Bystrica", utc(2026, 11, 2, 12), utc(2026, 11, 2, 6, 5))
    assert r.decision != "VYPRAVIŤ", r.summary
    _confirm_all(session)
    assert [(c.from_station, c.to_station) for c in _active(session)] == [("Púchov", "Žilina")]
    r = _decide(session, "95502", "Púchov", "Považská Bystrica", utc(2026, 11, 2, 12), utc(2026, 11, 2, 6, 10))
    assert r.decision != "VYPRAVIŤ", r.summary


def test_r5c3_stanica_v_genitive_zastavka_a_miestny_pad(cfg):
    """R5C3 a zvyšky QC2/QM2: „výluka trate A – B a stanice X“ dá aj výluku stanice X; „na zastávke X“, „Výluka
    v Ružomberku“; mimoriadnosť „Vo Vrútkach“ má názov dopravne zo siete; „na trati z Kraľovian do Ružomberka“ je
    úsek."""
    items = _x("ROV č. 1424/2026", "Úplná výluka trate Kraľovany – Ružomberok a stanice Vrútky dňa 24.11.2026 od 08:00 "
               "do 14:00 hod.\n", utc(2026, 11, 16, 9))
    assert sorted((i.data["from_station"], i.data["to_station"]) for i in items) == [("Kraľovany", "Ružomberok"),
                                                                                    ("Vrútky", "Vrútky")]
    items = _x("ROV č. 1416/2026", "Výluka nástupišťa na zastávke Ľubochňa dňa 24.11.2026 od 08:00 do 14:00 hod.\n",
               utc(2026, 11, 16, 9))
    assert [(i.data["from_station"], i.data["to_station"]) for i in items] == [("Ľubochňa", "Ľubochňa")]
    items = _x("ROV č. 1417/2026", "Výluka v Ružomberku dňa 24.11.2026 od 08:00 do 14:00 hod. Stanica neprejazdná.\n",
               utc(2026, 11, 16, 9))
    assert [(i.data.get("kind"), i.data["from_station"]) for i in items] == [("uplna", "Ružomberku")]
    items = _x("Mimoriadnosť", "Vo Vrútkach došlo k vykoľajeniu nákladného vozňa od 02.11.2026 04:30 do 02.11.2026 "
               "13:00, stanica je neprejazdná.\n", utc(2026, 11, 2, 4), "kd.orza@zsr.example")
    assert [(i.data["from_station"], i.data["to_station"]) for i in items] == [("Vrútky", "Vrútky")]
    items = _x("Mimoriadnosť", "Mimoriadnosť: porucha zabezpečovacieho zariadenia na trati z Kraľovian do Ružomberka od "
               "02.11.2026 04:30 do 02.11.2026 13:00.\n", utc(2026, 11, 2, 4), "kd.orza@zsr.example")
    assert [(i.data["from_station"], i.data["to_station"]) for i in items] == [("Kraľovany", "Ružomberok")]


# ====================================================================== R5C0, R5C2
def test_r5c0_uplne_vyluky_inymi_slovami(cfg):
    """R5C0: zákaz jazdy (všetkých) vlakov, obe koľaje skrátene („1. aj 2. TK“, „1.+2. TK“, „1., 2. TK“, „Vylúčenie
    1. a 2. TK“), druhá koľaj „nebude možná“, výluka TV, pri ktorej neprejdú ani motorové vlaky, a „Výluka TV aj
    koľají“ – úplná výluka (koľaj „obe“, nie „kolaj“); zákaz s obmedzením nápravy ostáva obmedzením, výluka TV
    s motorovými vlakmi trakčným vedením."""
    lp = "v úseku Lipany – Plaveč dňa 15.12.2026 od 8:00 do 14:00 hod."
    for body in (f"Výluka {lp} – zákaz jazdy všetkých vlakov.", f"Výluka {lp} Počas výluky zákaz jazdy vlakov.",
                 f"Dňa 15.12.2026 od 8:00 do 14:00 hod. bude v úseku Lipany – Plaveč výluka, zákaz jazdy vlakov."):
        assert {i.data.get("kind") for i in _x("ROV 5400/2026 Lipany – Plaveč", body + "\n", utc(2026, 12, 1, 8))} == \
            {"uplna"}, body
    kp = "v úseku Kysak – Prešov dňa 02.12.2026 od 8:00 do 12:00 hod."
    for body in (f"Výluka si vyžaduje vylúčenie 1. aj 2. TK {kp}", f"Výluka 1.+2. TK {kp}", f"Výluka 1., 2. TK {kp}",
                 f"Vylúčenie 1. a 2. TK {kp}", f"Výluka 2. TK {kp} Jazda po 1. TK nebude možná z dôvodu prác.",
                 f"Výluka TV {kp} Jazda motorových vlakov nie je možná.", f"Výluka TV aj koľají {kp}",
                 f"Výluka TV {kp} Úsek nebude prejazdný ani pre motorové vlaky."):
        items = _x("ROV 5501/2026", body + "\n", utc(2026, 11, 25, 8))
        assert [i.data.get("kind") for i in items] == ["uplna"], (body, _ev(items))
        assert items[0].data.get("track") in (None, "obe", "1", "2"), items[0].data
    assert [i.data.get("track") for i in _x("ROV 5502/2026", f"Výluka 1. aj 2. TK {kp}\n", utc(2026, 11, 25, 8))] == \
        ["obe"]
    for body, kind in ((f"V úseku Lipany – Plaveč platí dňa 15.12.2026 od 8:00 do 14:00 hod. zákaz jazdy vlakov "
                        f"s nápravovým zaťažením nad 22,5 t (výluka mosta).", "obmedzenie"),
                       (f"Výluka TV {kp} Motorové vlaky môžu úsekom prejsť.", "trakcne_vedenie"),
                       (f"Výluka 1. TK {kp}, doprava po 2. TK.", "kolaj"),
                       (f"Výluka 2. TK {kp} Po 2. TK vlaky nepremávajú.", "kolaj")):
        assert [i.data.get("kind") for i in _x("ROV 5503/2026", body + "\n", utc(2026, 11, 25, 8))] == [kind], body


def test_r5c0_srov_riadok_obe_kolaje_a_druh_uplna(cfg):
    """R5C0 v tabuľke: riadok SROV s koľajou „1.+2. TK“ a druhom „úplná“ je úplná výluka oboch koľají."""
    body = ("Súhrnný rozkaz o výlukách č. 49/2026\n\n"
            "P.č. | ROV | Úsek | Koľaj | Začiatok | Koniec | Druh\n"
            "1 | 5541/2026 | Margecany – Krompachy | 1.+2. TK | 07.12.2026 08:00 | 07.12.2026 12:00 | výluka\n"
            "2 | 5542/2026 | Kysak – Prešov | 2. TK | 08.12.2026 08:00 | 08.12.2026 12:00 | výluka koľaje\n")
    got = {i.data["from_station"]: (i.data.get("kind"), i.data.get("track"))
           for i in _x("SROV č. 49/2026", body, utc(2026, 12, 1, 8))}
    assert got == {"Margecany": ("uplna", "obe"), "Kysak": ("kolaj", "2")}


def test_r5c2_zakaz_jazdy_bez_slova_vyluka(cfg):
    """R5C2: zákaz jazdy nákladných vlakov, zákaz vjazdu do stanice a zákaz jazdy vlakov s RID (bez slova výluka či
    mimoriadnosť) – mimoriadnosť s miestom a termínom na potvrdenie, nie „iné“."""
    rec = utc(2026, 12, 1, 8)
    items = _x("Zákaz jazdy nákladných vlakov Lipany – Plaveč", "V úseku Lipany – Plaveč platí dňa 15.12.2026 od 8:00 "
               "do 14:00 hod. zákaz jazdy nákladných vlakov (obmedzená únosnosť mosta).\n", rec, "kd.orke@zsr.example")
    assert _ev(items) == [("mimoriadnost", None, "Lipany", "Plaveč", "15.12. 08:00", "15.12. 14:00")]
    items = _x("Zákaz vjazdu – žst. Plaveč", "Zákaz vjazdu vlakov do žst. Plaveč dňa 15.12.2026 od 8:00 do 14:00 "
               "hod.\n", rec, "kd.orke@zsr.example")
    assert _ev(items) == [("mimoriadnost", None, "Plaveč", "Plaveč", "15.12. 08:00", "15.12. 14:00")]
    items = _x("Zákaz jazdy RID", "V žst. Čierna nad Tisou platí dnes od 07:00 do 15:00 hod. zákaz jazdy vlakov "
               "s nebezpečným tovarom (RID).\n", utc(2026, 12, 4, 5, 30), "kd.orke@zsr.example")
    assert _ev(items) == [("mimoriadnost", None, "Čierna nad Tisou", "Čierna nad Tisou", "04.12. 07:00",
                           "04.12. 15:00")]
    assert "RID" in _notes(items)


def test_r5c0_e2e_zakaz_jazdy_vlakov_po_potvrdeni(env, session):
    """R5C0 v toku pošty: „– zákaz jazdy všetkých vlakov“ je po potvrdení úplná výluka a vlak o 09:00 nedostane
    VYPRAVIŤ (predtým „obmedzenie“, ktoré plánovač neposudzuje)."""
    _drop(env, "c0.eml", raw_mail("ROV 5400/2026 Lipany - Plavec", "Výluka v úseku Lipany – Plaveč dňa 15.12.2026 od 8:00 "
                                 "do 14:00 hod. – zákaz jazdy všetkých vlakov.\n", "r5c0", sender=ROV_KE,
                                 date="Tue, 01 Dec 2026 09:00:00 +0100"), utc(2026, 12, 1, 8))
    _process(session, utc(2026, 12, 1, 8, 2))
    _confirm_all(session)
    assert [c.kind for c in _active(session)] == ["uplna"]
    r = _decide(session, "77531", "Lipany", "Plaveč", utc(2026, 12, 15, 8), utc(2026, 12, 15, 5))
    assert r.decision != "VYPRAVIŤ", r.summary


# ====================================================================== R5C1
def test_r5c1_zrusenie_useku_nezrusi_usek_ktory_ostava(cfg):
    """R5C1: „Výluka v úseku Senica – Kúty … sa ruší. Výluka v úseku Trnava – Senica sa uskutoční …“ (aj „ostáva
    v platnosti“, „sa vykoná podľa plánu“, „bez zmeny“, „platí“, „nie je dotknutá“, „ostáva“) – zrušenie len Senica –
    Kúty s poznámkou; zrušenie oboch úsekov ruší oba."""
    rec = utc(2026, 12, 1, 8)
    for body in ("Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší. Výluka v úseku Trnava – Senica sa uskutoční "
                 "podľa ROV 3360/2026.",
                 "Ruší sa výluka v úseku Senica – Kúty; v úseku Trnava – Senica výluka ostáva v platnosti.",
                 "Výluka podľa ROV 3360/2026 sa ruší len v úseku Senica – Kúty, v úseku Trnava – Senica sa vykoná podľa "
                 "plánu.",
                 "Zrušená je výluka Senica – Kúty dňa 09.12.2026. Úsek Trnava – Senica: výluka bez zmeny.",
                 "V úseku Senica – Kúty sa výluka dňa 09.12.2026 nekoná. Výluka Trnava – Senica platí.",
                 "Výluka Senica – Kúty dňa 09.12.2026 sa ruší. Výluka Trnava – Senica nie je dotknutá.",
                 "Ruší sa výluka Senica – Kúty. Výluka Trnava – Senica ostáva."):
        items = _x("ROV 3360/2026 – čiastočné zrušenie", body + "\n", rec, "vyluky.ortt@zsr.example")
        assert [(i.kind, i.data["from_station"], i.data["to_station"]) for i in items] == \
            [("vyluka_zrusenie", "Senica", "Kúty")], (body, _ev(items))
        assert "Trnava – Senica ostáva" in _notes(items)
    items = _x("ROV 3360/2026", "Výluka v úsekoch Trnava – Senica a Senica – Kúty dňa 09.12.2026 sa ruší.\n", rec,
               "vyluky.ortt@zsr.example")
    assert sorted((i.data["from_station"], i.data["to_station"]) for i in items) == [("Senica", "Kúty"),
                                                                                    ("Trnava", "Senica")]


# ====================================================================== R5C5
def test_r5c5_storno_odklad_termin_a_neprevadzkove(cfg):
    """R5C5: „STORNO – ROV …/výluku v predmete stornujeme“ je zrušenie ROV; „sa odkladá“ je zmena; „Termín … sa
    ruší. Výluka sa doplňuje o termín …“ dá zrušenie dňa aj nový termín s číslom ROV; konzultácie GVD a informatívny
    prehľad nič nezapíšu; „Pôvodný ROV … sa ruší a nahrádza týmto rozkazom“ je nová výluka a zrušenie pôvodného."""
    items = _x("STORNO – ROV 5330/2026", "Dobrý deň,\n\nvýluku v predmete stornujeme.\n\nS pozdravom\nOR Košice\n",
               utc(2026, 12, 4, 8, 30))
    assert [(i.kind, i.data.get("external_ref")) for i in items] == [("vyluka_zrusenie", "5330/2026")]
    items = _x("ROV 5310/2026 – odklad výluky", "Výluka podľa ROV 5310/2026 sa odkladá o týždeň, na noc 14./15.12.2026, "
               "v rovnakom čase.\n", utc(2026, 12, 1, 10, 20))
    assert [(i.kind, i.data.get("external_ref")) for i in items] == [("vyluka_zmena", "5310/2026")]
    items = _x("ROV 5340/2026 – úprava termínov", "Termín 15.12.2026 sa ruší. Výluka sa doplňuje o termín 17.12.2026 od "
               "8:00 do 14:00 hod. Ostatné termíny platia bez zmeny.\n", utc(2026, 12, 7, 8, 10))
    assert sorted((i.kind, i.data.get("external_ref"), _loc(i.data.get("start"))) for i in items) == [
        ("vyluka_nova", "5340/2026", "17.12. 08:00"), ("vyluka_zrusenie", "5340/2026", "15.12. 00:00")]
    items = _x("Konzultácie k návrhu GVD 2027/2028", "Konzultácie k návrhu GVD 2027/2028 sa uskutočnia 09.12.2026 od "
               "9:00 do 15:00 v Bratislave. Pripomienky k obmedzeniam kapacity (výluky) v úseku Bratislava-Rača – Trnava "
               "zasielajte do 30.11.2026.\n", utc(2026, 11, 23, 8), "gvd@zsr.example")
    assert [i.kind for i in items] == ["ine"]
    items = _x("Informatívny prehľad výluk OR Bratislava – 48. týždeň", "Informatívny prehľad výluk OR Bratislava na 48. "
               "týždeň (23.11. – 29.11.2026)\n\n• ROV 4120/2026 – Galanta – Šaľa, 25.11.\n• ROV 4121/2026 – Senec – "
               "Galanta, 26.11., 1. TK\n\nPrehľad je informatívny, záväzné sú údaje uvedené v ROV.\n",
               utc(2026, 11, 20, 6), "info.orba@zsr.example")
    assert [i.kind for i in items] == ["ine"] and "4120/2026" in _notes(items)
    items = _x("ROV č. 2250/2026", "Úplná výluka oboch traťových koľají v úseku Leopoldov – Trnava dňa 25.11.2026 od "
               "08:00 do 12:00 hod. (ROV č. 2250/2026).\nPôvodný ROV č. 2240/2026 sa ruší a nahrádza týmto rozkazom.\n",
               utc(2026, 11, 16, 9))
    assert sorted((i.kind, i.data.get("external_ref")) for i in items) == [("vyluka_nova", "2250/2026"),
                                                                          ("vyluka_zrusenie", "2240/2026")]
    items = _x("Zmeny výluk", "Výluka podľa ROV č. 2240/2026 sa nekoná. Výluka podľa ROV č. 2241/2026 sa ruší. Nová "
               "výluka: Úplná výluka oboch traťových koľají v úseku Leopoldov – Trnava dňa 25.11.2026 od 08:00 do 12:00 "
               "hod. (ROV č. 2250/2026).\n", utc(2026, 11, 16, 9))
    assert sorted((i.kind, i.data.get("external_ref")) for i in items) == [
        ("vyluka_nova", "2250/2026"), ("vyluka_zrusenie", "2240/2026"), ("vyluka_zrusenie", "2241/2026")], _ev(items)
    # SROV so štandardnou vetou „Záväzné sú údaje v jednotlivých ROV“ nie je informatívny prehľad
    items = _x("SROV č. 47/2026", "Súhrnný rozkaz o výlukách č. 47/2026\n\nP.č. | ROV | Úsek | Koľaj | Začiatok | Koniec\n"
               "1 | 5211/2026 | Kysak – Prešov | 1.+2. TK | 24.11.2026 08:00 | 24.11.2026 12:30\n\nZáväzné sú údaje "
               "v jednotlivých ROV.\n", utc(2026, 11, 16, 7))
    assert _ev(items) == [("vyluka_nova", "uplna", "Kysak", "Prešov", "24.11. 08:00", "24.11. 12:30")]


# ====================================================================== R5C6, R5C7
def test_r5c6_skratky_mimo_kd_cas_tabulka_a_html(cfg):
    """R5C6: skrátené názvy („N. Zámky“, „L. Mikuláš – L. Hrádok“, „Sp. N. Ves – Pop.-Tatry“, „B. Bystrica“) a „(mimo)“
    sú úseky; KD len s časom dostane deň prijatia; tabuľka „Dátum | Od | Do“ dá termín cez polnoc; HTML časť
    s rozkazom sa číta, keď textová časť je len náhrada."""
    from vlaky.posta.stanice import stations

    def name(text):
        st = stations().match(text) if text else None
        return st.name if st else text

    rec = utc(2026, 12, 2, 11)
    for sec, a, b in (("N. Zámky – Šurany", "Nové Zámky", "Šurany"),
                      ("v úseku L. Mikuláš – L. Hrádok", "Liptovský Mikuláš", "Liptovský Hrádok"),
                      ("v úseku Sp. N. Ves – Pop.-Tatry", "Spišská Nová Ves", "Poprad-Tatry"),
                      ("v úseku B. Bystrica – Zvolen os. st.", "Banská Bystrica", "Zvolen osobná stanica"),
                      ("v úseku Galanta (mimo) – Šaľa (mimo)", "Galanta", "Šaľa")):
        items = _x("ROV 4260/2026", f"Úplná výluka {sec} dňa 11.12.2026 od 8:00 do 12:00 hod.\n", rec,
                   "vyluky.orba@zsr.example")
        got = [(name(i.data.get("from_station")), name(i.data.get("to_station"))) for i in items]
        assert got == [(a, b)], (sec, _ev(items))
    items = _x("MU – žst. Fiľakovo, porucha výhybky", "Od 06:05 hod. je v žst. Fiľakovo porucha výhybky č. 4, stanica "
               "je neprejazdná. Predpokladané obnovenie prevádzky o 10:00 hod.\n", utc(2026, 12, 6, 5, 20),
               "kd.orzv@zsr.example")
    assert _ev(items) == [("mimoriadnost", None, "Fiľakovo", "Fiľakovo", "06.12. 06:05", "06.12. 10:00")]
    assert "deň prijatia" in _notes(items)
    items = _x("Oznámenie – nočné výluky (ROV 3207/2026)", "Oznamujeme výluky podľa ROV č. 3207/2026:\n\n"
               "Dátum\tOd\tDo\tÚsek\tKoľaj\tDruh výluky\n\n24.11.2026\t22:00\t05:00\tPiešťany – Nové Mesto nad Váhom\t"
               "1. a 2. TK\túplná\n", utc(2026, 11, 12, 12), "vyluky.ortt@zsr.example")
    assert [(e[1], e[4], e[5]) for e in _ev(items)] == [("uplna", "24.11. 22:00", "25.11. 05:00")]
    assert items[0].data.get("external_ref") == "3207/2026"
    from vlaky.posta.parser import parse_message

    raw = ("From: ZSR OR Zvolen <vyluky.orzv@zsr.example>\nSubject: ROV 6140/2026\nMIME-Version: 1.0\n"
           "Content-Type: multipart/alternative; boundary=\"b\"\n\n--b\nContent-Type: text/plain; charset=utf-8\n\n"
           "Táto správa je vo formáte HTML.\n--b\nContent-Type: text/html; charset=utf-8\n\n<html><body><table>"
           "<tr><td>Úsek</td><td>Lučenec – Fiľakovo</td></tr><tr><td>Termín</td><td>16.12.2026 od 08:00 do 14:00 "
           "hod.</td></tr><tr><td>Druh</td><td>úplná výluka</td></tr></table></body></html>\n--b--\n").encode()
    pm = parse_message(raw, sandbox=False)
    assert "Lučenec" in pm.body_text
    items = _x(pm.subject, pm.body_text, utc(2026, 11, 27, 9), pm.sender)
    assert _ev(items) == [("vyluka_nova", "uplna", "Lučenec", "Fiľakovo", "16.12. 08:00", "16.12. 14:00")]


def test_r5c7_anglicke_avizo_trasa_v_proze_ceska_objednavka(cfg):
    """R5C7: anglické avízo („is expected to arrive at Komárno border station on 22.11.2026 at 14:20“), pridelená
    trasa v próze (odchod a príchod s časom) a česká objednávka („Nakládka nejdříve:“)."""
    items = _x("Train 47460 – arrival notice Komárom/Komárno", "Dear colleagues,\n\ntrain 47460 (empty wagons) is "
               "expected to arrive at Komárno border station on 22.11.2026 at 14:20.\n\nBest regards\n",
               utc(2026, 11, 22, 9, 5), "dispatch@hu.partner.example")
    assert [(i.kind, i.data.get("train_number"), _loc(i.data.get("eta"))) for i in items] == [
        ("avizo", "47460", "22.11. 14:20")]
    items = _x("Pridelenie trasy ad hoc – vlak Nex 62017, 21.11.2026", "Dobrý deň,\n\nprideľujeme trasu ad hoc pre vlak "
               "Nex 62017 na deň 21.11.2026 s odchodom zo stanice Nové Zámky o 14:35 a príchodom do stanice Štúrovo o "
               "15:20.\n", utc(2026, 11, 19, 9), "kapacita@zsr.example")
    pts = items[0].data["points"]
    assert items[0].kind == "trasa_pridelena" and [(p["station"], _loc(p["arr"]), _loc(p["dep"])) for p in pts] == [
        ("Nové Zámky", None, "21.11. 14:35"), ("Štúrovo", "21.11. 15:20", None)]
    items = _x("Poptávka/objednávka přepravy dřeva Brezno – Štúrovo", "Dobrý den,\nprosíme o přepravu 24 vozů dřeva z "
               "Brezna do Štúrova. Nakládka nejdříve: 08.12.2026, dodání nejpozději: 10.12.2026.\n",
               utc(2026, 11, 25, 9, 10), "objednavky@drevo.zakaznik.example")
    assert items[0].kind == "objednavka" and _loc(items[0].data.get("earliest_departure")) == "08.12. 00:00"


# ====================================================================== všeobecné kontroly
def test_kontrola_druhu_prisnejsi_druh_s_poznamkou(cfg):
    """Kontrola druhu: výluka koľaje, v ktorej texte je „trať bude úplne uzavretá“ (v inej vete udalosti), je
    úplná výluka s poznámkou „Overte druh výluky“ a nižšou istotou; výluka TV s „jazda motorových vlakov nie je
    možná“ v inej vete tiež."""
    items = _x("ROV 5601/2026", "Výluka 1. TK v úseku Kysak – Prešov dňa 02.12.2026 od 8:00 do 12:00 hod. Počas výluky "
               "bude trať úplne uzavretá.\n", utc(2026, 11, 25, 8))
    assert [i.data.get("kind") for i in items] == ["uplna"] and "Overte druh výluky" in _notes(items)
    assert items[0].confidence <= 0.8
    items = _x("ROV 5560/2026 Margecany – Krompachy, výluka TV", "Výluka TV v úseku Margecany – Krompachy dňa "
               "08.12.2026 od 8:00 do 12:00 hod. (ROV 5560/2026). Jazda motorových vlakov nie je možná (práce na "
               "koľaji).\n", utc(2026, 12, 1, 10))
    assert [i.data.get("kind") for i in items] == ["uplna"] and "Overte druh výluky" in _notes(items)


def test_siet_proti_strate_obsahu(cfg):
    """Sieť proti strate: e-mail od ŽSR s prevádzkovými slovami („uzavretý“) a termínom, z ktorého pravidlá nevedeli
    zapísať udalosť, dá výluku na potvrdenie s poznámkou (aj bez miesta); od partnera nie; automatická odpoveď,
    oznámenie o skončení a pozvánka sieť nespustia."""
    items = _x("Oznámenie OR Košice", "Úsek Kysak – Košice bude dňa 20.11.2026 od 08:00 do 14:00 uzavretý.\n")
    assert _ev(items) == [("vyluka_nova", "uplna", "Kysak", "Košice", "20.11. 08:00", "20.11. 14:00")]
    assert "nevedeli zapísať udalosť" in _notes(items) and items[0].confidence < 0.6
    items = _x("Oznámenie OR Košice", "Dňa 20.11.2026 od 08:00 do 14:00 bude trať pri Kysaku uzavretá.\n")
    assert [(i.kind, i.data.get("from_station")) for i in items] == [("vyluka_nova", None)]
    assert "Miesto sa nepodarilo prečítať" in _notes(items)
    items = _x("Oznámenie", "Úsek Kysak – Košice bude dňa 20.11.2026 od 08:00 do 14:00 uzavretý.\n",
               sender="dispecing@partner.example")
    assert [i.kind for i in items] == ["ine"]
    for subject, body in (("Automatická odpoveď: výluka", "Do 20.11.2026 som mimo kancelárie, úsek Kysak – Košice je "
                           "uzavretý – kontaktujte kolegu.\n"),
                          ("Pozvánka: porada k výlukám", "Pozývame Vás na poradu 25.11.2026 od 9:00 k uzavretiu trate "
                           "Kysak – Košice.\n")):
        assert [i.kind for i in _x(subject, body)] == ["ine"], subject


def test_korpus_vykon_40kb(cfg):
    """Extrakcia 40 kB e-mailu trvá najviac približne sekundu: zoznam 220 výluk (bežne ~0,35 s) aj dlhá próza bez
    termínu (~0,25 s). Hranica 2 s je rezerva pre zaťažený počítač; zachytí opakované prechádzanie všetkých viet
    pre každú udalosť (pred opravou ~1,4 s)."""
    line = ("Úplná výluka v úseku Kysak – Košice dňa {d}.12.2026 od 08:00 do 14:00 hod. (ROV č. 5{d:03d}/2026), "
            "náhradná autobusová doprava je zabezpečená, vlaky nákladnej dopravy budú čakať v žst. Kysak.\n")
    prose = ("Dobrý deň, v nadväznosti na poradu oznamujeme, že v úseku Margecany – Krompachy bude prebiehať "
             "rekonštrukcia mosta a súvisiace práce na trakčnom vedení. Podrobnosti budú uvedené v jednotlivých ROV. ")
    for subject, body, n in (("SROV č. 50/2026", "".join(line.format(d=1 + i % 28) for i in range(220)), 28),
                             ("Informácia OR Košice", prose * 230, 1)):
        assert 38_000 <= len(body.encode("utf-8")) <= 52_000
        took = []
        for _ in range(2):
            t0 = time.perf_counter()
            items = _x(subject, body, utc(2026, 11, 20, 8))
            took.append(time.perf_counter() - t0)
        assert len(items) == n and min(took) < 2.0, (subject, took)
