"""Štvrtá revízia – pravidlá e-mailov ŽSR (oblasť M4, opravy po kontrole): zápis do registra výluk a odporúčanie.

- udalosti toho istého e-mailu nie sú zmenou jedna druhej: ROV s dvoma termínmi má po potvrdení oba termíny;
- nová výluka s číslom výluky v registri, ale v inom termíne, je ďalší termín toho ROV (nie presun);
- presun jedného dňa viacdennej výluky („dňa 25.11. sa ruší a presúva sa na 28.11.“, aj ako „Zmena ROV …“) celú
  výluku nepresunie: pôvodná ostáva, nový termín je ďalšia výluka, zrušenie dňa sa odmietne s vysvetlením;
- ROV s kalendárovou prílohou (.ics bez pozvánky) sa spracuje ako výluka;
- iný (už) prejazdný úsek nekončí mimoriadnosť, kladná formulácia o nákladných vlakoch nevytvorí obmedzenie.

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
from vlaky.models import Closure, Path as TrainPath, Train
from test_revizia3_terminy import as_utc, drop, events, raw_mail, utc

ROV_ZA = "ZSR OR Zilina <vyluky.orza@zsr.example>"
ROV_KE = "ZSR OR Kosice <vyluky.orke@zsr.example>"
KD_ZA = "Kontrolny dispecer OR Zilina <kd.orza@zsr.example>"
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


def _process(session, now: datetime) -> dict:
    from vlaky.posta.spracovanie import process_mailbox

    set_fake_now(now)
    s = process_mailbox(session, now=now)
    session.commit()
    return s


def _confirm_all(session) -> None:
    from vlaky.posta.spracovanie import confirm_event

    for e in events(session):
        if e.status == "navrh":
            confirm_event(session, e.id, "dispecer1")
            session.commit()


def _decide(session, number: str, origin: str, destination: str, dep: datetime, now: datetime):
    from vlaky.agent.zdravie import set_kv
    from vlaky.planovanie.planovac import evaluate_train

    t = Train(number=number, origin=origin, destination=destination, traction="E", gauge="1435", vmax_kmh=80,
              status="planovany", ready_utc=dep, deadline_utc=dep + timedelta(hours=8))
    session.add(t)
    session.flush()
    session.add(TrainPath(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena", departure_utc=dep))
    set_kv(session, "mail_last_ok", {"ts": (now - timedelta(minutes=2)).isoformat(), "fetched": 0})
    session.commit()
    return evaluate_train(session, t.id, now, "test M4 revízia")


def _active(session) -> list:
    return [c for c in session.scalars(select(Closure).order_by(Closure.id))
            if c.status != "zrusene" and c.proposed_change_of is None]


def _registered_1315(session) -> Closure:
    """ROV 1315/2026 v registri: 23. – 27.11.2026 denne 08:00 – 14:00, overený dispečerom."""
    c = Closure(kind="uplna", from_station="Ružomberok", to_station="Liptovský Mikuláš", start_utc=utc(2026, 11, 23, 7),
                end_utc=utc(2026, 11, 27, 13), daily_from="08:00", daily_to="14:00", status="overene", source="email",
                external_ref="1315/2026")
    session.add(c)
    session.commit()
    return c


# ====================================================================== [2] termíny toho istého ROV
def test_i2_rov_s_dvoma_terminmi_po_potvrdeni_ma_oba_terminy(env, session):
    """[2] ROV č. 1322/2026 s dvoma termínmi (1) 01.12., 2) 03.12.) dá dve udalosti s rovnakým číslom. Potvrdenie
    druhej nesmie presunúť prvú – v registri ostanú oba termíny a vlak 01.12. nedostane VYPRAVIŤ do výluky."""
    drop(env, "rov.eml", raw_mail("ROV c. 1322/2026 - vyluky Kysak - Kosice",
                                  "Podľa ROV č. 1322/2026 budú v úseku Kysak – Košice vykonané tieto výluky:\n\n"
                                  "1) Úplná výluka oboch traťových koľají dňa 01.12.2026 od 08:00 do 12:00 hod.\n"
                                  "2) Úplná výluka oboch traťových koľají dňa 03.12.2026 od 08:00 do 12:00 hod.\n",
                                  "rov1322", sender=ROV_KE, date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 2))
    assert len(events(session)) == 2
    _confirm_all(session)
    assert sorted((as_utc(c.start_utc), c.status, c.external_ref) for c in _active(session)) == [
        (utc(2026, 12, 1, 7), "overene", "1322/2026"), (utc(2026, 12, 3, 7), "overene", "1322/2026")]
    r = _decide(session, "77610", "Kysak", "Košice", utc(2026, 12, 1, 8), utc(2026, 12, 1, 4))
    assert r.decision != "VYPRAVIŤ", r.summary


def test_i2_iny_termin_s_rovnakym_cislom_je_dalsi_termin(env, session):
    """[2] V registri je ROV č. 1600/2026 na 20.11.; nový e-mail ohlasuje ROV č. 1600/2026 na 22.11. (bez slova
    zmena) – je to ďalší termín (ROV môže mať viac termínov), nie presun: 20.11. ostane a dispečer dostane upozornenie.
    Znovu poslaný ROV s rovnakým termínom duplicitu nezaloží."""
    c = Closure(kind="uplna", from_station="Kysak", to_station="Košice", start_utc=utc(2026, 11, 20, 7),
                end_utc=utc(2026, 11, 20, 13), status="overene", source="email", external_ref="1600/2026")
    session.add(c)
    session.commit()
    drop(env, "a.eml", raw_mail("ROV c. 1600/2026 - vyluka Kysak - Kosice", "Úplná výluka v úseku Kysak – Košice dňa "
                                "22.11.2026 od 08:00 do 14:00 hod. (ROV č. 1600/2026).\n", "rov1600b", sender=ROV_KE,
                                date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 2))
    (ev,) = events(session)
    assert any("v inom termíne" in w for w in ev.validation["warnings"]), ev.validation["warnings"]
    _confirm_all(session)
    session.refresh(c)
    assert (as_utc(c.start_utc), c.status) == (utc(2026, 11, 20, 7), "overene")
    assert sorted(as_utc(x.start_utc) for x in _active(session)) == [utc(2026, 11, 20, 7), utc(2026, 11, 22, 7)]
    drop(env, "b.eml", raw_mail("ROV c. 1600/2026 - vyluka Kysak - Kosice", "Úplná výluka v úseku Kysak – Košice dňa "
                                "20.11.2026 od 08:00 do 14:00 hod. (ROV č. 1600/2026).\n", "rov1600c", sender=ROV_KE,
                                date="Mon, 16 Nov 2026 11:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 10, 2))
    _confirm_all(session)
    assert len(_active(session)) == 2


@pytest.mark.parametrize("subject,body", [
    ("ROV c. 1315/2026 Ruzomberok - Liptovsky Mikulas",
     "Výluka podľa ROV č. 1315/2026 dňa 25.11.2026 sa ruší a presúva sa na 28.11.2026 od 08:00 do 14:00 hod.\n"),
    ("ROV c. 1315/2026 Ruzomberok - Liptovsky Mikulas",
     "Výluka podľa ROV č. 1315/2026 dňa 25.11.2026 sa nekoná. Náhradný termín: 28.11.2026 od 08:00 do 14:00 hod.\n"),
])
def test_i2_presun_jedneho_dna_nepresunie_celu_vyluku(env, session, subject, body):
    """[2] Viacdenná výluka 23. – 27.11.; e-mail presúva len deň 25.11. na 28.11. Po potvrdení všetkého ostáva výluka
    23. – 27.11. (zrušenie dňa sa odmietne – dispečer upraví termín), 28.11. je ďalšia výluka – vlaky 24. aj 28.11.
    nedostanú VYPRAVIŤ (predtým sa celá výluka presunula na 28.11.)."""
    from vlaky.posta.spracovanie import apply_failed

    c = _registered_1315(session)
    drop(env, "z.eml", raw_mail(subject, body, "z1315", sender=ROV_ZA, date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 2))
    _confirm_all(session)
    session.refresh(c)
    assert (as_utc(c.start_utc), as_utc(c.end_utc), c.status) == (utc(2026, 11, 23, 7), utc(2026, 11, 27, 13),
                                                                  "overene")
    assert sorted(as_utc(x.start_utc) for x in _active(session)) == [utc(2026, 11, 23, 7), utc(2026, 11, 28, 7)]
    cancel = next(e for e in events(session) if e.kind == "vyluka_zrusenie")
    assert apply_failed(cancel) and "len časti" in cancel.validation["apply_result"]["error"]
    for i, day in enumerate((24, 28)):
        r = _decide(session, f"7762{i}", "Ružomberok", "Liptovský Mikuláš", utc(2026, 11, day, 9),
                    utc(2026, 11, day, 4))
        assert r.decision != "VYPRAVIŤ", (day, r.summary)


def test_i2_zmena_jedneho_dna_viacdennej_vyluky_ju_celu_nepresunie(env, session):
    """[2] „Zmena ROV …: výluka dňa 25.11. sa presúva na 28.11.“ pri výluke 23. – 27.11.: zmena by inak presunula
    celú výluku na 28.11. – potvrdenie ponechá pôvodnú výluku a nový termín založí ako ďalšiu výluku (s vysvetlením)."""
    c = _registered_1315(session)
    drop(env, "z.eml", raw_mail("Zmena ROV c. 1315/2026 Ruzomberok - Liptovsky Mikulas", "Výluka podľa ROV č. "
                                "1315/2026 dňa 25.11.2026 sa presúva na 28.11.2026 od 08:00 do 14:00 hod.\n", "zm1315",
                                sender=ROV_ZA, date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 2))
    (ev,) = events(session)
    assert ev.kind == "vyluka_zmena" and ev.needs_confirmation
    assert any("len časť viacdennej výluky" in w for w in ev.validation["warnings"]), ev.validation["warnings"]
    _confirm_all(session)
    session.refresh(c)
    assert (as_utc(c.start_utc), as_utc(c.end_utc)) == (utc(2026, 11, 23, 7), utc(2026, 11, 27, 13))
    new = [x for x in _active(session) if x.id != c.id]
    assert [(as_utc(x.start_utc), x.status) for x in new] == [(utc(2026, 11, 28, 7), "overene")]
    assert "len časť viacdennej výluky" in new[0].note
    r = _decide(session, "77630", "Ružomberok", "Liptovský Mikuláš", utc(2026, 11, 24, 9), utc(2026, 11, 24, 4))
    assert r.decision != "VYPRAVIŤ", r.summary


# ====================================================================== [5] ROV s kalendárovou prílohou
def _mail(subject: str, body: str, mid: str, sender: str, date: str, attachments=()) -> bytes:
    msg = EmailMessage()
    msg["Authentication-Results"] = AR_ZSR
    msg["From"] = sender
    msg["To"] = "agent.dispecing@dopravca.example"
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = f"<{mid}@zsr.example>"
    msg.set_content(body)
    for name, data, maintype, subtype in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return bytes(msg)


def test_i5_rov_s_kalendarovou_prilohou_je_vyluka(env, session):
    """[5] ROV s prílohou .ics (termín výluky do kalendára, bez pozvánky) je výluka so záznamom pre plánovač – nie
    „iné“ (vlak by dostal ZELENÚ do výluky); pozvánka na poradu (METHOD:REQUEST) ostáva „iné“."""
    ics = (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nSUMMARY:Vyluka Leopoldov - Trnava\r\n"
           b"DTSTART:20261125T070000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    invite = b"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nSUMMARY:Porada\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    now = utc(2026, 11, 16, 9, 5)
    for name, data in (("rov.eml", _mail("ROV č. 2250/2026 – výluka Leopoldov – Trnava", "Úplná výluka oboch traťových "
                                         "koľají v úseku Leopoldov – Trnava dňa 25.11.2026 od 08:00 do 12:00 hod. (ROV "
                                         "č. 2250/2026).\n", "rov2250", ROV_ZA, "Mon, 16 Nov 2026 10:00:00 +0100",
                                         [("vyluka.ics", ics, "text", "calendar")])),
                       ("inv.eml", _mail("Koordinácia výluk 2027", "Porada k výluke ROV č. 2250/2026 v úseku Leopoldov – "
                                         "Trnava dňa 25.11.2026 o 9:00 hod.\n", "inv1", ROV_ZA,
                                         "Mon, 16 Nov 2026 10:01:00 +0100",
                                         [("pozvanka.ics", invite, "text", "calendar")]))):
        p = Path(env.mail_dir) / name
        p.write_bytes(data)
        os.utime(p, ((now - timedelta(minutes=2)).timestamp(), (now - timedelta(minutes=2)).timestamp()))
    _process(session, now)
    kinds = sorted(e.kind for e in events(session))
    assert kinds == ["ine", "vyluka_nova"], kinds
    (row,) = _active(session)
    assert (row.from_station, row.to_station, as_utc(row.start_utc)) == ("Leopoldov", "Trnava", utc(2026, 11, 25, 7))


# ====================================================================== [12], [7] mimoriadnosti
def test_i12_iny_usek_prejazdny_vlak_cez_mimoriadnost_nedostane_vypravit(env, session):
    """[12] Mimoriadnosť Púchov – Považská Bystrica a veta „Úsek Žilina – Varín je už prejazdný.“ – mimoriadnosť
    trvá (záznam „navrh“), vlak cez Púchov – Považská Bystrica nedostane VYPRAVIŤ."""
    drop(env, "m.eml", raw_mail("Mimoriadnost", "Mimoriadnosť: v úseku Púchov – Považská Bystrica strhnuté trakčné "
                                "vedenie od 02.11.2026 04:30 do 02.11.2026 13:00. Úsek Žilina – Varín je už prejazdný.\n",
                                "mim12", sender=KD_ZA, date="Mon, 02 Nov 2026 05:10:00 +0100"))
    _process(session, utc(2026, 11, 2, 4, 15))
    (ev,) = events(session)
    assert (ev.kind, ev.data["from_station"], ev.data["to_station"]) == ("mimoriadnost", "Púchov", "Považská Bystrica")
    r = _decide(session, "77571", "Púchov", "Považská Bystrica", utc(2026, 11, 2, 6), utc(2026, 11, 2, 4, 20))
    assert r.decision != "VYPRAVIŤ", r.summary


def test_i7_kladna_formulacia_o_nakladnych_vlakoch_nevytvori_obmedzenie(env, session):
    """[7] „… prevádzka je obnovená o 09:20, obmedzenia pre nákladné vlaky nie sú.“ – oznámenie o skončení („iné“),
    žiadny záznam do odvolania: nákladný vlak cez Kraľovany neeskaluje."""
    drop(env, "k.eml", raw_mail("Mimoriadnost Kralovany", "Mimoriadnosť v ŽST Kraľovany: prevádzka je obnovená o 09:20, "
                                "obmedzenia pre nákladné vlaky nie sú.\n", "kon7", sender=KD_ZA,
                                date="Mon, 02 Nov 2026 09:25:00 +0100"))
    _process(session, utc(2026, 11, 2, 8, 30))
    assert [e.kind for e in events(session)] == ["ine"]
    assert _active(session) == []
    r = _decide(session, "66102", "Žilina", "Ružomberok", utc(2026, 11, 2, 9), utc(2026, 11, 2, 8, 35))
    assert r.decision == "VYPRAVIŤ", r.summary
