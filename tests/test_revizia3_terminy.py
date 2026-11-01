"""Tretia revízia – termíny a udalosti z e-mailov ŽSR (oblasť M1): regresné testy k nálezom MR0, MR1, MR3, MR4,
MR6, PR0 a PR4.

- MR0: nočná výluka s oknom po polnoci („v noci z 12. na 13.11. od 0:30 do 4:30“) je 13.11. 0:30 – 4:30, nie 12.11.;
- MR3: dni oddelené čiarkou („12.11.2026 od 8:00 do 14:00, 13.11.2026 od 9:00 do 15:00“) sú dve obdobia;
- MR6: zoznam „3.12. a 27.11.2026“ nie je 3.12.2025 (zlé poradie → rok z ďalšieho dátumu a „overte termín“);
- MR4: oznámenie o skončení mimoriadnosti nie je nová mimoriadnosť do odvolania (je „iné“ – nič sa samo
  nezruší), zrušenie pomalej jazdy či POTR nie je nová pomalá jazda;
- PR4: mimoriadnosť s uvedeným „od … do …“ si koniec ponechá (nie 30 dní do odvolania);
- PR0: mimoriadnosť „v železničnej stanici X … neprejazdná“ je mimoriadnosť v dopravni X (od = do) so záznamom
  „navrh“ pre plánovač – vlak cez neprejazdnú stanicu nedostane ZELENÚ ani po potvrdení;
- MR1: odpoveď ŽSR s citovaným pôvodným ROV – citovaný starý termín nie je zmena, platí nový termín.

Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest
from sqlalchemy import select

from vlaky.cas import local, set_fake_now
from vlaky.hranice import seed_borders
from vlaky.models import Closure, ExtractedEvent, Path as TrainPath, Recommendation, Train

UTC = timezone.utc
SEC = "Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš"
ZSR = "ZSR OR Poprad <vyluky.orpp@zsr.example>"
AR = ("Authentication-Results: mx.dopravca.example; spf=pass smtp.mailfrom=zsr.example; dkim=pass\n"
      " header.d=zsr.example; dmarc=pass header.from=zsr.example\n")


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def iso(*parts: int) -> str:
    return utc(*parts).isoformat()


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def rules(body: str, subject: str = "Výluka", received: datetime = utc(2026, 11, 2, 6)):
    from vlaky.posta.pravidla import RuleExtractor
    from vlaky.posta.udalosti import MailDocument

    return RuleExtractor().extract(MailDocument(subject=subject, body=body, received_at=received))


def terms(items) -> list:
    return sorted((i.data["start"], i.data["end"]) for i in items)


def raw_mail(subject: str, body: str, mid: str, sender: str = ZSR,
             date: str = "Mon, 02 Nov 2026 05:10:00 +0100") -> bytes:
    hdr = (AR + f"From: {sender}\nTo: agent.dispecing@dopravca.example\nSubject: {subject}\nDate: {date}\n"
           f"Message-ID: <{mid}@zsr.example>\nMIME-Version: 1.0\nContent-Type: text/plain; charset=\"utf-8\"\n"
           "Content-Transfer-Encoding: 8bit\n")
    return (hdr + "\n" + body).replace("\n", "\r\n").encode("utf-8")


def drop(cfg, name: str, data: bytes) -> None:
    (Path(cfg.mail_dir) / name).write_bytes(data)


def events(session) -> list[ExtractedEvent]:
    return list(session.scalars(select(ExtractedEvent).order_by(ExtractedEvent.id)))


def rows(session) -> list[Closure]:
    return list(session.scalars(select(Closure).order_by(Closure.id)))


def add_train(session, number: str, origin: str, destination: str, dep: datetime, deadline_h: int = 6) -> Train:
    t = Train(number=number, origin=origin, destination=destination, traction="E", gauge="1435", vmax_kmh=80,
              status="planovany", ready_utc=dep, deadline_utc=dep + timedelta(hours=deadline_h))
    session.add(t)
    session.flush()
    session.add(TrainPath(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena", departure_utc=dep))
    session.flush()
    return t


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


# ====================================================================== MR0 noc s oknom po polnoci
def _night(day: int, h0: int, m0: int, h1: int, m1: int) -> tuple:
    """Okno v noci, ktorej DRUHÝ deň je ``day`` (miestny čas CET = UTC + 1)."""
    start = datetime(2026, 11, day, h0, m0, tzinfo=UTC) - timedelta(hours=1)
    end = datetime(2026, 11, day, h1, m1, tzinfo=UTC) - timedelta(hours=1)
    return start.isoformat(), end.isoformat()


@pytest.mark.parametrize("term,expected", [
    ("v noci z 12. na 13.11.2026 od 0:30 do 4:30", [_night(13, 0, 30, 4, 30)]),
    ("v noci z 12. na 13.11.2026 v čase 00:30 – 04:30", [_night(13, 0, 30, 4, 30)]),
    ("v noci 12./13.11.2026 v čase od 00:30 do 04:30 hod.", [_night(13, 0, 30, 4, 30)]),
    ("v noci z 12.11.2026 na 13.11.2026 od 0:10 do 4:10 hod.", [_night(13, 0, 10, 4, 10)]),
    ("v noci 12./13.11.2026 od 01:00 do 04:00", [_night(13, 1, 0, 4, 0)]),
    ("od 0:30 do 4:30 v noci z 12. na 13.11.2026", [_night(13, 0, 30, 4, 30)]),
    ("v noci z 12. na 13. novembra 2026 od 22:00 do 5:00", [(iso(2026, 11, 12, 21), iso(2026, 11, 13, 4))]),
    # zoznamy nocí – každá noc má okno vo svojom druhom dni
    ("v nociach 12./13. a 13./14.11.2026 od 0:30 do 4:30", [_night(13, 0, 30, 4, 30), _night(14, 0, 30, 4, 30)]),
    ("v nociach 12./13.11., 13./14.11. a 14./15.11.2026 od 1:00 do 4:00",
     [_night(13, 1, 0, 4, 0), _night(14, 1, 0, 4, 0), _night(15, 1, 0, 4, 0)]),
    # dve okná tej istej noci: pred polnocou (cez polnoc) a po polnoci
    ("v noci z 12. na 13.11.2026 od 23:30 do 24:00 a od 0:00 do 3:00",
     [(iso(2026, 11, 12, 22, 30), iso(2026, 11, 12, 23)), _night(13, 0, 0, 3, 0)]),
    ("v noci z 12. na 13.11.2026 od 22:00 do 2:00 a od 3:00 do 5:00",
     [(iso(2026, 11, 12, 21), iso(2026, 11, 13, 1)), _night(13, 3, 0, 5, 0)]),
])
def test_mr0_nocne_okno_po_polnoci_je_v_druhom_dni_noci(cfg, term, expected):
    """MR0: okno po polnoci (začiatok pred 12:00) patrí k druhému dňu noci; okno pred polnocou a cez polnoc začína
    prvým dňom (aj noc s mesiacom slovom). Istota sa neznižuje (termín je jednoznačný). Okná, ktoré sa nemenia,
    stráži ``test_revizia3_terminy_bez_zmeny.py``."""
    items = rules(f"{SEC} {term}.")
    assert terms(items) == sorted(expected)
    assert all(i.confidence == 0.85 for i in items), [i.notes for i in items]


@pytest.mark.parametrize("term,start,end,daily", [
    ("v nociach od 12./13. do 14./15.11.2026 od 0:30 do 4:30",
     iso(2026, 11, 12, 23, 30), iso(2026, 11, 15, 3, 30), ("00:30", "04:30")),
    ("v nociach z 12. na 13. až z 14. na 15.11.2026 od 22:00 do 5:00",
     iso(2026, 11, 12, 21), iso(2026, 11, 15, 4), ("22:00", "05:00")),
])
def test_mr0_rozsah_noci_nestrati_prve_noci(cfg, term, start, end, daily):
    """MR0: rozsah nocí („od 12./13. do 14./15.11.“) je denné okno cez všetky noci – prvé noci sa nestratia a okno
    po polnoci je v druhých dňoch nocí."""
    (it,) = rules(f"{SEC} {term}.")
    assert (it.data["start"], it.data["end"], (it.data["daily_from"], it.data["daily_to"])) == (start, end, daily)


def test_mr0_noc_cez_prelom_roka(cfg):
    """MR0: noc 31.12./1.1. s oknom po polnoci je 1.1. (aj keď prvý deň nemá rok)."""
    for term in ("v noci z 31.12. na 1.1.2027 od 0:30 do 4:30", "v noci z 31.12.2026 na 1.1.2027 od 0:30 do 4:30"):
        (it,) = rules(f"{SEC} {term}.", received=utc(2026, 12, 20, 8))
        assert (it.data["start"], it.data["end"]) == (iso(2026, 12, 31, 23, 30), iso(2027, 1, 1, 3, 30)), term


def test_mr0_nocna_vyluka_end_to_end_planovac_nevypravi_do_vyluky(env, session):
    """MR0 end to end: ROV na noc 12./13.11. od 0:30 do 4:30 prijatý 12.11. večer. Záznam „navrh“ je na skutočnej
    noci (13.11. 0:30 – 4:30), validácia nehlási „Platnosť už skončila“ a vlak s odchodom 13.11. 1:00 cez úsek
    nedostane VYPRAVIŤ do výluky."""
    from vlaky.planovanie.planovac import evaluate_train
    from vlaky.posta.spracovanie import process_mailbox

    now = utc(2026, 11, 12, 17)                                   # 12.11. 18:00 miestneho času
    set_fake_now(now)
    drop(env, "rov.eml", raw_mail("ROV c. 2500/2026 Poprad-Tatry - Liptovsky Mikulas",
                                  "Rozkaz o výluke č. 2500/2026\nÚplná výluka oboch traťových koľají v úseku "
                                  "Poprad-Tatry – Liptovský Mikuláš v noci z 12. na 13.11.2026 od 0:30 do 4:30.\n",
                                  "rov2500", date="Thu, 12 Nov 2026 18:00:00 +0100"))
    process_mailbox(session, now=now)
    session.commit()
    (ev,) = events(session)
    assert (ev.data["start"], ev.data["end"]) == (iso(2026, 11, 12, 23, 30), iso(2026, 11, 13, 3, 30))
    assert "Platnosť už skončila." not in ev.validation["warnings"]
    (row,) = rows(session)
    assert row.status == "navrh" and as_utc(row.start_utc) == utc(2026, 11, 12, 23, 30)
    dep = utc(2026, 11, 13, 0)                                    # 13.11. 1:00 – vo výluke
    t = add_train(session, "77501", "Poprad-Tatry", "Liptovský Mikuláš", dep)
    r = evaluate_train(session, t.id, now, "test MR0")
    assert r.decision != "VYPRAVIŤ", r.summary
    assert datetime.fromisoformat(r.chosen["depart"]) >= utc(2026, 11, 13, 3, 30)


# ====================================================================== MR3 dni oddelené čiarkou
@pytest.mark.parametrize("term,expected", [
    ("12.11.2026 od 8:00 do 14:00, 13.11.2026 od 9:00 do 15:00",
     [(iso(2026, 11, 12, 7), iso(2026, 11, 12, 13)), (iso(2026, 11, 13, 8), iso(2026, 11, 13, 14))]),
    ("12.11.2026 8:00 – 14:00, 13.11.2026 9:00 – 15:00, 14.11.2026 7:00 – 9:00",
     [(iso(2026, 11, 12, 7), iso(2026, 11, 12, 13)), (iso(2026, 11, 13, 8), iso(2026, 11, 13, 14)),
      (iso(2026, 11, 14, 6), iso(2026, 11, 14, 8))]),
    ("od 12.11.2026 8:00 do 14:00 hod., 13.11.2026 od 9:00 hod. do 15:00 hod.",
     [(iso(2026, 11, 12, 7), iso(2026, 11, 12, 13)), (iso(2026, 11, 13, 8), iso(2026, 11, 13, 14))]),
])
def test_mr3_dni_oddelene_ciarkou_su_samostatne_obdobia(cfg, term, expected):
    """MR3: čas uzatvárajúci okno a dátum za čiarkou s vlastným časom je ďalší deň zoznamu, nie koniec obdobia."""
    items = rules(f"{SEC} {term}.")
    assert terms(items) == expected
    assert not any("overte termín" in n for i in items for n in i.notes)


def test_mr3_end_to_end_vlak_v_druhom_okne_nedostane_vypravit(env, session):
    """MR3 end to end: vlak 13.11. o 14:30 je v druhom okne (9:00 – 15:00) – nesmie dostať VYPRAVIŤ o 14:30."""
    from vlaky.planovanie.planovac import evaluate_train
    from vlaky.posta.spracovanie import process_mailbox

    now = utc(2026, 11, 13, 10)                                   # 13.11. 11:00 miestneho času
    set_fake_now(now)
    drop(env, "rov.eml", raw_mail("ROV c. 2800/2026 Poprad-Tatry - Liptovsky Mikulas",
                                  "Rozkaz o výluke č. 2800/2026\nÚplná výluka oboch traťových koľají v úseku "
                                  "Poprad-Tatry – Liptovský Mikuláš 12.11.2026 od 8:00 do 14:00, 13.11.2026 od 9:00 do "
                                  "15:00.\n", "rov2800", date="Thu, 12 Nov 2026 06:00:00 +0100"))
    process_mailbox(session, now=now)
    session.commit()
    assert sorted((as_utc(c.start_utc), as_utc(c.end_utc)) for c in rows(session)) == [
        (utc(2026, 11, 12, 7), utc(2026, 11, 12, 13)), (utc(2026, 11, 13, 8), utc(2026, 11, 13, 14))]
    t = add_train(session, "77504", "Poprad-Tatry", "Liptovský Mikuláš", utc(2026, 11, 13, 13, 30))
    r = evaluate_train(session, t.id, now, "test MR3")
    assert r.decision != "VYPRAVIŤ", r.summary
    assert datetime.fromisoformat(r.chosen["depart"]) >= utc(2026, 11, 13, 14)


# ====================================================================== MR6 rok v zozname dní
def test_mr6_zoznam_s_neskorsim_mesiacom_na_zaciatku_nie_je_minuly_rok(cfg):
    """MR6: „3.12. a 27.11.2026“ (prijaté 2.11.2026) – 3.12.2026, nie 3.12.2025; zlé poradie zníži istotu
    a dispečer dostane „overte termín“. Prelom roka „28.12. a 2.1.2027“ ostáva bez upozornenia."""
    items = rules(f"{SEC} 3.12. a 27.11.2026 od 8:00 do 14:00.")
    assert terms(items) == [(iso(2026, 11, 27, 7), iso(2026, 11, 27, 13)), (iso(2026, 12, 3, 7), iso(2026, 12, 3, 13))]
    dec = next(i for i in items if i.data["start"].startswith("2026-12-03"))
    assert dec.confidence < 0.8 and any("overte termín" in n for n in dec.notes)
    nov = next(i for i in items if i.data["start"].startswith("2026-11-27"))
    assert nov.confidence == 0.85
    rollover = rules(f"{SEC} 28.12. a 2.1.2027 od 8:00 do 14:00.")
    assert terms(rollover) == [(iso(2026, 12, 28, 7), iso(2026, 12, 28, 13)), (iso(2027, 1, 2, 7), iso(2027, 1, 2, 13))]
    assert all(i.confidence == 0.85 for i in rollover)


def test_mr6_end_to_end_den_v_buducnosti_ide_do_planovaca(env, session, fixed_now, no_planner):
    """MR6 end to end: záznam „navrh“ pre 3.12.2026 (nie rok v minulosti) a bez upozornenia „Platnosť už skončila“."""
    from vlaky.posta.spracovanie import process_mailbox

    drop(env, "rov.eml", raw_mail("ROV c. 2600/2026 Poprad-Tatry - Liptovsky Mikulas",
                                  "Rozkaz o výluke č. 2600/2026\nVýluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš "
                                  "3.12. a 27.11.2026 od 8:00 do 14:00.\n", "rov2600"))
    process_mailbox(session, now=fixed_now)
    session.commit()
    assert sorted(as_utc(c.start_utc) for c in rows(session)) == [utc(2026, 11, 27, 7), utc(2026, 12, 3, 7)]
    assert not any("Platnosť už skončila." in e.validation["warnings"] for e in events(session))


# ====================================================================== PR0 / PR4 mimoriadnosť v stanici
@pytest.mark.parametrize("place", ["v železničnej stanici Kraľovany", "v žst. Kraľovany", "v ŽST Kraľovany",
                                   "v obvode stanice Kraľovany", "ve stanici Kraľovany", "v stanici Kraľovany"])
def test_pr0_mimoriadnost_v_stanici_je_dopravna_od_do(cfg, place):
    """PR0: miesto mimoriadnosti „v železničnej stanici X“ (aj „v žst.“, „v ŽST“, „v obvode stanice“, česky) je
    dopravňa X (od = do); PR4: uvedený koniec „do …“ sa zachová."""
    (it,) = rules(f"Mimoriadnosť: {place} porucha zabezpečovacieho zariadenia, stanica je neprejazdná od 02.11.2026 "
                  "04:30 do 02.11.2026 13:00.", subject="Mimoriadnosť Kraľovany")
    assert it.kind == "mimoriadnost"
    assert (it.data["from_station"], it.data["to_station"]) == ("Kraľovany", "Kraľovany")
    assert (it.data["start"], it.data["end"]) == (iso(2026, 11, 2, 3, 30), iso(2026, 11, 2, 12))


def test_pr4_mimoriadnost_s_obdobim_si_ponecha_koniec(cfg):
    """PR4: mimoriadnosť s „od … do …“ má koniec z obdobia (nie technický koniec +30 dní)."""
    (it,) = rules("Mimoriadnosť v stanici Ilava: prerušená doprava od 02.11.2026 04:30 do 02.11.2026 13:00.",
                  subject="Mimoriadnosť Ilava")
    assert it.kind == "mimoriadnost" and it.data["start"] == iso(2026, 11, 2, 3, 30)
    assert it.data["end"] == iso(2026, 11, 2, 12)


def test_pr4_neprecitany_koniec_mimoriadnosti_dostane_upozornenie(cfg):
    """PR4: čas, ktorý sa nepodarilo priradiť (možný koniec), pri mimoriadnosti bez konca (do odvolania) zníži
    istotu a dispečer dostane „overte koniec“."""
    (it,) = rules("Mimoriadnosť v ŽST Kraľovany od 02.11.2026 04:30, stanica neprejazdná, koniec cca 13:00 hod.",
                  subject="Mimoriadnosť Kraľovany")
    assert it.kind == "mimoriadnost" and it.data["end"] is None
    assert it.confidence < 0.8 and any("overte koniec" in n for n in it.notes)


def _incident_mail(cfg, name: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = "dispecing@zsr.example"
    msg["To"] = "agent.dispecing@dopravca.example"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{name}@zsr.example>"
    msg["Date"] = "Mon, 02 Nov 2026 05:00:00 +0100"
    msg.set_content(body)
    (cfg.mail_dir / f"{name}.eml").write_bytes(bytes(msg))


def _incident_train(s, now: datetime, number: str, origin: str, destination: str) -> int:
    t = Train(number=number, run_date=now.date().isoformat(), origin=origin, destination=destination, traction="E",
              gauge="1435", vmax_kmh=100, status="planovany", ready_utc=now + timedelta(minutes=30),
              deadline_utc=now + timedelta(hours=12))
    s.add(t)
    s.flush()
    s.add(TrainPath(train_id=t.id, train_number=number, status="pridelena", departure_utc=now + timedelta(minutes=30)))
    s.flush()
    return t.id


def _last_rec(s, train_id: int) -> Recommendation:
    return s.scalar(select(Recommendation).where(Recommendation.train_id == train_id)
                    .order_by(Recommendation.id.desc()))


def test_pr0_neprejazdna_stanica_nie_je_zelena_ani_po_potvrdeni(cfg, fixed_now):
    """PR0 end to end: e-mail „v železničnej stanici Kraľovany … neprejazdná od … do …“ dá mimoriadnosť
    Kraľovany – Kraľovany so záznamom „navrh“; vlak Čadca → Košice cez Kraľovany v tomto čase nedostane
    VYPRAVIŤ ZELENÁ – ani po potvrdení udalosti (potom je výluka overená)."""
    from vlaky.agent.beh import run_cycle
    from vlaky.db import session_scope
    from vlaky.posta.spracovanie import confirm_event

    with session_scope() as s:
        seed_borders(s)
        tid = _incident_train(s, fixed_now, "91901", "Čadca", "Košice")
    a, b = local(fixed_now - timedelta(minutes=30)), local(fixed_now + timedelta(hours=8))
    _incident_mail(cfg, "mim-kralovany", "Mimoriadnosť Kraľovany",
                   f"Mimoriadnosť: v železničnej stanici Kraľovany porucha zabezpečovacieho zariadenia, stanica je "
                   f"neprejazdná od {a:%d.%m.%Y %H:%M} do {b:%d.%m.%Y %H:%M}.")
    run_cycle(fixed_now, force_evaluate=True)
    with session_scope() as s:
        (ev,) = events(s)
        assert ev.kind == "mimoriadnost" and (ev.data["from_station"], ev.data["to_station"]) == ("Kraľovany",
                                                                                                  "Kraľovany")
        assert not ev.validation.get("blocking")
        (row,) = rows(s)
        assert (row.from_station, row.to_station, row.status, row.until_revoked) == ("Kraľovany", "Kraľovany",
                                                                                    "navrh", False)
        assert as_utc(row.end_utc) == fixed_now + timedelta(hours=8)
        rec = _last_rec(s, tid)
        assert not (rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ"), (rec.decision, rec.color, rec.reasons)
        assert any("Kraľovany" in r for r in rec.reasons)
        confirm_event(s, ev.id, "dispecer1")
    later = fixed_now + timedelta(minutes=1)
    set_fake_now(later)
    run_cycle(later, force_evaluate=True)
    with session_scope() as s:
        ev = s.get(ExtractedEvent, ev.id)
        assert ev.status == "aplikovane", ev.validation.get("apply_result")
        (row,) = rows(s)
        assert row.status == "overene"
        rec = _last_rec(s, tid)
        assert not (rec.decision == "VYPRAVIŤ" and rec.color == "ZELENÁ"), (rec.decision, rec.color, rec.reasons)
        assert any("Kraľovany" in r for r in rec.reasons)


def test_pr4_mimoriadnost_s_koncom_nie_je_mesacna_eskalacia(cfg, fixed_now):
    """PR4 end to end: mimoriadnosť „od … do …“ v stanici Ilava – záznam končí uvedeným koncom (nie do odvolania)
    a vlak Púchov → Trenčín nedostane ESKALOVAŤ s rezervou −42 000 min, ale počká na koniec mimoriadnosti."""
    from vlaky.agent.beh import run_cycle
    from vlaky.db import session_scope

    with session_scope() as s:
        seed_borders(s)
        tid = _incident_train(s, fixed_now, "92001", "Púchov", "Trenčín")
    a, b = local(fixed_now - timedelta(minutes=30)), local(fixed_now + timedelta(hours=8))
    _incident_mail(cfg, "mim-ilava", "Mimoriadnosť Ilava",
                   f"Mimoriadnosť v stanici Ilava: prerušená doprava od {a:%d.%m.%Y %H:%M} do {b:%d.%m.%Y %H:%M}.")
    run_cycle(fixed_now, force_evaluate=True)
    with session_scope() as s:
        (ev,) = events(s)
        assert ev.data["end"] == (fixed_now + timedelta(hours=8)).isoformat()
        (row,) = rows(s)
        assert not row.until_revoked and as_utc(row.end_utc) == fixed_now + timedelta(hours=8)
        rec = _last_rec(s, tid)
        assert rec.decision != "ESKALOVAŤ", (rec.decision, rec.reasons)
        assert datetime.fromisoformat(rec.chosen["depart"]) >= fixed_now + timedelta(hours=7)


# ====================================================================== MR4 skončenie mimoriadnosti
END_NOTICE = ("Oznamujeme ukončenie mimoriadnej udalosti v ŽST Ružomberok (porucha zabezpečovacieho zariadenia). "
              "Prevádzka v úseku Ružomberok – Liptovský Mikuláš je obnovená od 02.11.2026 08:15.")


@pytest.mark.parametrize("subject,text", [
    ("Ukoncenie mimoriadnej udalosti Ruzomberok", END_NOTICE),
    ("Mimoriadnost Ruzomberok - ukoncena", "Mimoriadna udalosť v úseku Ružomberok – Liptovský Mikuláš bola ukončená o "
                                           "08:15 dňa 02.11.2026, obe koľaje sú zjazdné."),
    ("Mimoriadnost Ruzomberok", "Porucha zabezpečovacieho zariadenia v úseku Ružomberok – Liptovský Mikuláš bola "
                                "odstránená, prevádzka obnovená o 8:15."),
    ("Ukoncenie prerusenia dopravy", "Ukončenie prerušenia dopravy v úseku Ružomberok – Liptovský Mikuláš o 08:15 dňa "
                                     "02.11.2026."),
])
def test_mr4_skoncenie_mimoriadnosti_nie_je_nova_mimoriadnost(cfg, subject, text):
    """MR4: oznámenie o skončení mimoriadnosti nie je nová mimoriadnosť do odvolania – je to „iné“ s vysvetlením
    (miesto a čas obnovenia); bez čísla sa nič automaticky neruší (podľa úseku by sa mohla zrušiť iná výluka)."""
    items = rules(text, subject=subject, received=utc(2026, 11, 2, 7, 30))
    assert [i.kind for i in items] == ["ine"]
    (it,) = items
    assert it.data == {}
    note = next(n for n in it.notes if "skončení mimoriadnosti" in n)
    assert "Ružomberok – Liptovský Mikuláš" in note and "02.11.2026 08:15" in note and "ručne" in note


def test_mr4_obnovenie_s_obmedzenim_rychlosti_je_pomala_jazda(cfg):
    """MR4: „prevádzka obnovená …, rýchlosť obmedzená na 30 km/h do odvolania“ – oznámenie o skončení (iné) a pomalá
    jazda do odvolania (nie mimoriadnosť, ktorá by úsek zablokovala na 30 dní)."""
    items = rules("Prevádzka v úseku Ružomberok – Liptovský Mikuláš obnovená od 02.11.2026 08:15, rýchlosť obmedzená "
                  "na 30 km/h do odvolania.", subject="Mimoriadnost Ruzomberok", received=utc(2026, 11, 2, 7, 30))
    assert [i.kind for i in items] == ["ine", "pomala_jazda"]
    pj = items[1].data
    assert (pj["from_station"], pj["to_station"], pj["start"], pj["speed_kmh"], pj["until_revoked"]) == (
        "Ružomberok", "Liptovský Mikuláš", iso(2026, 11, 2, 7, 15), 30, True)


@pytest.mark.parametrize("subject,text,ref", [
    ("TLG c. 38/2026 - zrusenie pomalej jazdy", "Telegram č. 38/2026: ruší sa pomalá jazda 40 km/h v úseku Kysak – "
     "Košice zavedená TLG č. 37/2026, platí od 03.11.2026 12:00.", "37/2026"),
    ("Odvolanie POTR c. 12/2026", "Odvolávame POTR č. 12/2026 (pomalá jazda 30 km/h Štrba – Poprad-Tatry) s "
     "platnosťou od 05.11.2026 06:00.", "12/2026"),
])
def test_mr4_zrusenie_pomalej_jazdy_nie_je_nova_pomala_jazda(cfg, subject, text, ref):
    """MR4: „ruší sa pomalá jazda … zavedená TLG č. 37/2026“, „Odvolávame POTR č. 12/2026“ sú zrušenia (s číslom
    rušeného dokumentu), nie nové pomalé jazdy."""
    (it,) = rules(text, subject=subject)
    assert it.kind == "vyluka_zrusenie" and ref in it.data["external_ref"]


def test_mr4_end_to_end_skoncenie_nevytvori_zaznam_a_nezrusi_nic_samo(env, session):
    """MR4 end to end: v registri je otvorená mimoriadnosť Ružomberok – Liptovský Mikuláš. Oznámenie o jej skončení
    nevytvorí nový záznam „navrh“ do odvolania (úsek by bol 30 dní červený) ani prepočet; čaká vo fronte ako „iné“.
    Mimoriadnosť sa automaticky nezruší (ani po potvrdení) – ukončí ju dispečer v registri, potom sa vlak cez úsek
    už nezastavuje."""
    from vlaky.planovanie.planovac import evaluate_train
    from vlaky.posta.spracovanie import confirm_event, process_mailbox

    now = utc(2026, 11, 2, 7, 30)                                 # 08:30 miestneho času
    set_fake_now(now)
    incident = Closure(kind="mimoriadnost", from_station="Ružomberok", to_station="Liptovský Mikuláš",
                       start_utc=utc(2026, 11, 2, 3), end_utc=utc(2026, 12, 2, 3), until_revoked=True,
                       status="overene", source="email")
    session.add(incident)
    session.commit()
    drop(env, "end.eml", raw_mail("Ukoncenie mimoriadnej udalosti Ruzomberok", END_NOTICE + "\n", "kd-end-1",
                                  sender="Kontrolny dispecer OR Zilina <kd.orza@zsr.example>",
                                  date="Mon, 02 Nov 2026 08:20:00 +0100"))
    summary = process_mailbox(session, now=now)
    session.commit()
    (ev,) = events(session)
    assert ev.kind == "ine" and ev.needs_confirmation and ev.status == "navrh"
    assert any("skončení mimoriadnosti" in n for n in ev.validation["notes"])
    assert [c.id for c in rows(session)] == [incident.id]          # žiadny nový záznam do odvolania
    assert summary["planning"] is None
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    session.refresh(incident)
    assert incident.status == "overene"                           # nič sa samo nezrušilo
    incident.status = "zrusene"                                   # dispečer mimoriadnosť ukončí v registri
    session.commit()
    t = add_train(session, "77503", "Ružomberok", "Liptovský Mikuláš", now + timedelta(hours=1), deadline_h=3)
    r = evaluate_train(session, t.id, now, "test MR4")
    assert r.decision != "ESKALOVAŤ" and not any("Mimoriadnosť" in x for x in r.reasons), (r.decision, r.reasons)


# ====================================================================== MR1 odpoveď s citovaným pôvodným ROV
SUBJ_RE = "RE: ROV c. 1142/2026 Poprad-Tatry - Liptovsky Mikulas"
OUTLOOK = ("Dobrý deň,\noznamujeme zmenu: výluka podľa ROV č. 1142/2026 sa presúva na 14.11.2026 od 8:00 do 14:00.\n\n"
           "S pozdravom\nŽSR OR Poprad\n\n-----Original Message-----\n"
           "From: ŽSR OR Poprad <vyluky.orpp@zsr.example>\nSent: Monday, November 2, 2026 9:00 AM\n"
           "To: agent.dispecing@dopravca.example\nSubject: ROV c. 1142/2026 Poprad-Tatry - Liptovsky Mikulas\n\n"
           "Rozkaz o výluke č. 1142/2026: výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš 12.11.2026 "
           "od 8:00 do 14:00.\n")
GT = ("Dobrý deň,\nzmena výluky: nový termín 14.11.2026 od 8:00 do 14:00.\n\n"
      "Dňa 2.11.2026 o 9:00 ŽSR OR Poprad napísal(a):\n"
      "> Rozkaz o výluke č. 1142/2026: výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš 12.11.2026\n"
      "> od 8:00 do 14:00.\n")
OUTLOOK_SK = ("Dobrý deň,\nvýluka podľa ROV č. 1142/2026 sa presúva na 14.11.2026 od 8:00 do 14:00.\n\n"
              "________________________________\nOd: ŽSR OR Poprad <vyluky.orpp@zsr.example>\n"
              "Odoslané: pondelok 2. novembra 2026 9:00\nKomu: agent.dispecing@dopravca.example\n"
              "Predmet: ROV č. 1142/2026 Poprad-Tatry - Liptovský Mikuláš\n\n"
              "Rozkaz o výluke č. 1142/2026: výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš 12.11.2026 "
              "od 8:00 do 14:00.\n")


def _registered(session) -> Closure:
    c = Closure(kind="kolaj", track="1", from_station="Poprad-Tatry", to_station="Liptovský Mikuláš",
                start_utc=utc(2026, 11, 12, 7), end_utc=utc(2026, 11, 12, 13), status="overene", source="email",
                external_ref="1142/2026")
    session.add(c)
    session.commit()
    return c


@pytest.mark.parametrize("body", [OUTLOOK, GT, OUTLOOK_SK], ids=["outlook", "citacia", "outlook-sk"])
def test_mr1_odpoved_s_citaciou_meni_vyluku_na_novy_termin(env, session, no_planner, body):
    """MR1: odpoveď ŽSR „RE: ROV …“ s novým termínom a citovaným pôvodným ROV dá jednu zmenu s novým termínom
    (citovaný starý termín nie je zmena); plánovač vidí dočasný záznam na 14.11. a potvrdenie presunie výluku."""
    from vlaky.posta.spracovanie import confirm_event, process_mailbox

    set_fake_now(utc(2026, 11, 3, 8))
    c = _registered(session)
    drop(env, "re.eml", raw_mail(SUBJ_RE, body, f"re-1142-{len(body)}", date="Tue, 03 Nov 2026 09:00:00 +0100"))
    process_mailbox(session)
    session.commit()
    (ev,) = events(session)
    assert ev.kind == "vyluka_zmena"
    assert (ev.data["start"], ev.data["end"]) == (iso(2026, 11, 14, 7), iso(2026, 11, 14, 13))
    assert not any("nepodarilo priradiť" in n for n in ev.validation["notes"])
    temp = session.get(Closure, ev.validation["pending_closure"]["id"])
    assert temp.proposed_change_of == c.id and as_utc(temp.start_utc) == utc(2026, 11, 14, 7)
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    session.refresh(c)
    assert as_utc(c.start_utc) == utc(2026, 11, 14, 7) and c.status == "overene"


def test_mr1_parser_citovane_useky(cfg):
    """MR1: rozpoznanie citácie – oddeľovač, „Dňa … napísal(a):“ (aj zalomený), blok hlavičiek pri „RE:“, riadky „>“;
    pri preposlaní nič."""
    from vlaky.posta.parser import reply_quote_spans
    from vlaky.posta.text import fold

    def quoted(subject: str, body: str) -> str:
        f = fold(body)
        return "".join(f[s:e] for s, e in reply_quote_spans(subject, f, 0, len(f)))

    assert "12.11.2026" in quoted("RE: ROV", OUTLOOK) and "14.11.2026" not in quoted("RE: ROV", OUTLOOK)
    assert "12.11.2026" in quoted("RE: ROV", GT) and "dna 2.11.2026" in quoted("RE: ROV", GT)
    wrapped = ("Nový termín 14.11.2026.\n\nDňa 2.11.2026 o 9:00 ŽSR OR Poprad <vyluky.orpp@zsr.example>\n"
               "napísal(a):\n> x\n")
    assert "dna 2.11.2026" in quoted("Re: ROV", wrapped) and "14.11.2026" not in quoted("Re: ROV", wrapped)
    assert "12.11.2026" in quoted("RE: ROV", OUTLOOK_SK)
    assert quoted("FW: ROV", OUTLOOK) == ""
    # oddeľovač bez predmetu odpovede môže byť preposlaním – nie je citácia
    assert quoted("ROV c. 1142/2026", OUTLOOK) == ""
    # bez predmetu „RE:“ blok „Od: … Do: …“ v texte ROV nie je citácia
    rov = "Výluka koľaje\nOd: 12.11.2026 8:00\nDo: 12.11.2026 14:00\nPredmet: oprava výhybky\nKomu: OR Poprad\n"
    assert quoted("ROV c. 1/2026", rov) == ""
