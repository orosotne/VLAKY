"""Revízia spracovania pošty: regresné testy k nálezom P1 – P12, k viditeľnosti nepotvrdených výluk pre plánovač
a k zápisu ``mail_last_ok`` (len po úspešnom načítaní schránky).

Každý test zodpovedá jednému nálezu z revízie (scenár je opísaný v docstringu testu).
"""
from __future__ import annotations

import imaplib
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select, update

from test_web_spolocne import csrf, login_as
from vlaky.hranice import seed_borders
from vlaky.models import AuditLog, Closure, Email, ExtractedEvent, Handover, Notification, Train
from vlaky.posta import spracovanie
from vlaky.posta.spracovanie import apply_event, confirm_event, process_mailbox, reject_event

pytest_plugins = ["test_web_spolocne"]

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "ukazkove_emaily"
UTC = timezone.utc
AR_ZSR = ("Authentication-Results: mx.dopravca.example; spf=pass smtp.mailfrom=zsr.example; dkim=pass\n"
          " header.d=zsr.example; dmarc=pass header.from=zsr.example\n")
AR_PARTNER = ("Authentication-Results: mx.dopravca.example; spf=pass smtp.mailfrom=partner.example; dkim=pass\n"
              " header.d=partner.example; dmarc=pass header.from=partner.example\n")
AR_DOPRAVCA = ("Authentication-Results: mx.dopravca.example; spf=pass smtp.mailfrom=dopravca.example; dkim=pass\n"
               " header.d=dopravca.example; dmarc=pass header.from=dopravca.example\n")


# ---------------------------------------------------------------------- spoločné pomôcky
@pytest.fixture()
def planner(monkeypatch):
    """Podvrhnutý plánovač (testy pošty nezávisia od implementácie plánovania)."""
    from vlaky.planovanie import planovac

    calls: list[str] = []

    def fake(session, now=None, trigger=""):  # noqa: ANN001
        calls.append(trigger)
        return []

    monkeypatch.setattr(planovac, "evaluate_all", fake)
    return calls


@pytest.fixture()
def env(cfg, session, fixed_now, planner, monkeypatch):
    """Prísne overenie odosielateľa ako v prevádzke; prah 0,8 (pravidlá samy dávajú najviac 0,85)."""
    monkeypatch.setattr(cfg, "require_dmarc_pass", True)
    monkeypatch.setattr(cfg, "trusted_authserv_ids", "mx.dopravca.example")
    monkeypatch.setattr(cfg, "confirm_threshold", 0.8)
    seed_borders(session)
    session.commit()
    return cfg


def eml(headers: str, body: str) -> bytes:
    return (headers.strip("\n") + "\n\n" + body).replace("\n", "\r\n").encode("utf-8")


def mail(mid: str, subject: str, body: str, sender: str = "ZSR OR Kosice <vyluky.orke@zsr.example>",
         ar: str = AR_ZSR) -> bytes:
    domain = sender.split("@")[-1].rstrip(">")
    headers = (f"{ar}From: {sender}\nTo: agent.dispecing@dopravca.example\nSubject: {subject}\n"
               f"Date: Mon, 02 Nov 2026 05:10:00 +0100\nMessage-ID: <{mid}@{domain}>\n"
               "Content-Type: text/plain; charset=\"utf-8\"\n")
    return eml(headers, body)


def drop_raw(cfg, name: str, data: bytes) -> None:
    (Path(cfg.mail_dir) / name).write_bytes(data)


def drop(cfg, *names: str) -> None:
    for n in names:
        shutil.copy(SAMPLES / n, Path(cfg.mail_dir) / n)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def active(session) -> list[Closure]:
    return list(session.scalars(select(Closure).where(Closure.status != "zrusene").order_by(Closure.id)))


def events(session, kind: str | None = None) -> list[ExtractedEvent]:
    q = select(ExtractedEvent).order_by(ExtractedEvent.id)
    if kind:
        q = q.where(ExtractedEvent.kind == kind)
    return list(session.scalars(q))


def queue(session) -> list[ExtractedEvent]:
    """Fronta na potvrdenie tak, ako ju vidí web (/fronta), odovzdávka a súhrn."""
    return list(session.scalars(select(ExtractedEvent).where(ExtractedEvent.status == "navrh",
                                                             ExtractedEvent.needs_confirmation.is_(True))))


def windows(session, start: datetime, hours: int = 24 * 40) -> list:
    from vlaky.planovanie.planovac import closure_windows

    return closure_windows(session, start, start + timedelta(hours=hours))


CHANGE_1142_BODY = """Oznamujeme zmenu Rozkazu o výluke č. 1142/2026 (výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš).

Pôvodný termín: 12.11.2026 08:00 – 14:00
Nový termín: od 12.11.2026 09:30 do 12.11.2026 16:00
"""


# ====================================================================== P1 preposlané e-maily
FWD_OUTER = """Authentication-Results: mx.dopravca.example; spf=pass smtp.mailfrom=dopravca.example; dkim=pass
 header.d=dopravca.example; dmarc=pass header.from=dopravca.example
From: Dispecing <dispecing@dopravca.example>
To: agent.dispecing@dopravca.example
Subject: Fwd: Zmena ROV c. 1142/2026
Date: Tue, 03 Nov 2026 10:41:00 +0100
Message-ID: <{mid}@dopravca.example>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BB"
"""


def forwarded(mid: str, inner_ar: str, inner_from: str, inner_body: str) -> bytes:
    body = f"""--BB
Content-Type: text/plain; charset="utf-8"

Automaticky preposlane z dispecerskej schranky (original je v prilohe).

--BB
Content-Type: message/rfc822
Content-Disposition: attachment; filename="povodna_sprava.eml"

{inner_ar}From: {inner_from}
To: dispecing@dopravca.example
Subject: Zmena ROV c. 1142/2026 - posun terminu vyluky Poprad-Tatry - Liptovsky Mikulas
Date: Tue, 03 Nov 2026 10:40:00 +0100
Message-ID: <inner-{mid}@zsr.example>
Content-Type: text/plain; charset="utf-8"

{inner_body}
--BB--
"""
    return eml(FWD_OUTER.format(mid=mid), body)


def test_p1_spoofed_inner_message_in_autoforward_is_quarantined(env, session):
    """Podvrh ŽSR (DMARC fail) preposlaný pravidlom schránky ako príloha nesmie prejsť len na DMARC preposielateľa."""
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    assert process_mailbox(session)["auto_applied"] == 1
    (c,) = session.query(Closure).all()
    inner_ar = ("Authentication-Results: mx.dopravca.example; spf=fail smtp.mailfrom=evil.example; dkim=none;\n"
                " dmarc=fail (p=REJECT) header.from=zsr.example\n")
    spoof = CHANGE_1142_BODY.replace("od 12.11.2026 09:30 do 12.11.2026 16:00", "od 28.11.2026 08:00 do 28.11.2026 14:00")
    drop_raw(env, "02.eml", forwarded("fwd-1", inner_ar, "ZSR OR Kosice <vyluky.orke@zsr.example>", spoof))
    s = process_mailbox(session)
    em = session.scalars(select(Email).where(Email.message_id == "<fwd-1@dopravca.example>")).one()
    assert em.verdict == "karantena", em.verdict_reason
    assert "preposlan" in em.verdict_reason and "DMARC=fail" in em.verdict_reason
    assert s["auto_applied"] == 0 and len(events(session)) == 1
    session.refresh(c)
    assert as_utc(c.start_utc) == utc(2026, 11, 12, 7) and len(active(session)) == 1


def test_p1_forwarded_message_from_unlisted_sender_is_quarantined(env, session):
    """Pôvodný odosielateľ preposlanej správy musí byť na zozname povolených (ako pri priamom e-maile)."""
    inner_ar = ("Authentication-Results: mx.dopravca.example; spf=pass smtp.mailfrom=evil.example; dkim=pass\n"
                " header.d=evil.example; dmarc=pass header.from=evil.example\n")
    drop_raw(env, "f.eml", forwarded("fwd-2", inner_ar, "Utocnik <vyluky@evil.example>", CHANGE_1142_BODY))
    process_mailbox(session)
    em = session.query(Email).one()
    assert em.verdict == "karantena" and "vyluky@evil.example" in em.verdict_reason
    assert events(session) == [] and session.query(Closure).count() == 0


def test_p1_forward_attached_as_text_is_verified_too(env, session):
    """Ten istý podvrh priložený ako text/plain („povodna_sprava.eml“) – obchádzka cez textovú prílohu nesmie prejsť."""
    inner_ar = ("Authentication-Results: mx.dopravca.example; spf=fail smtp.mailfrom=evil.example; dkim=none;\n"
                " dmarc=fail (p=REJECT) header.from=zsr.example\n")
    raw = forwarded("fwd-3", inner_ar, "ZSR OR Kosice <vyluky.orke@zsr.example>", CHANGE_1142_BODY)
    raw = raw.replace(b"Content-Type: message/rfc822", b"Content-Type: text/plain; charset=\"utf-8\"")
    drop_raw(env, "t.eml", raw)
    process_mailbox(session)
    em = session.query(Email).one()
    assert em.verdict == "karantena" and "DMARC=fail" in em.verdict_reason
    assert events(session) == []


def test_p1_verified_forward_is_never_auto_applied(env, session):
    """Aj overený preposlaný e-mail (ukážka 04) ide vždy na potvrdenie – obsah nemožno overiť u zdroja."""
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    process_mailbox(session)
    drop(env, "04_zmena_vyluky_preposlana.eml")
    s = process_mailbox(session)
    assert s["auto_applied"] == 0 and s["waiting_confirmation"] == 1, s
    em = session.scalars(select(Email).where(Email.sender == "dispecing@dopravca.example")).one()
    assert em.verdict == "ok" and em.forwarded_from == "vyluky.orke@zsr.example"
    (ev,) = events(session, "vyluka_zmena")
    assert ev.needs_confirmation and any("preposlan" in r for r in ev.validation["confirmation_reasons"])
    ev.needs_confirmation = False                       # ani ručne zmenený príznak nepomôže
    assert apply_event(session, ev)["ok"] is False


def test_p1_inline_forward_checks_inner_sender(env, session):
    """Vložené preposlanie („Forwarded message“): pôvodný odosielateľ musí byť povolený, obsah vždy na potvrdenie."""
    def inline(mid: str, inner_from: str) -> bytes:
        body = ("Preposielam na vedomie.\n\n---------- Forwarded message ---------\n"
                f"From: {inner_from}\nDate: Mon, 2 Nov 2026 at 05:00\nSubject: ROV c. 1600/2026\n"
                "To: dispecing@dopravca.example\n\n"
                "Rozkaz o výluke (ROV) č. 1600/2026 – výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš "
                "od 20.11.2026 08:00 do 20.11.2026 14:00.\n")
        return mail(mid, "Fwd: ROV c. 1600/2026", body, sender="Dispecing <dispecing@dopravca.example>",
                    ar=AR_DOPRAVCA)

    drop_raw(env, "a.eml", inline("in-1", "Utocnik <vyluky@evil.example>"))
    process_mailbox(session)
    em = session.query(Email).one()
    assert em.verdict == "karantena" and "vyluky@evil.example" in em.verdict_reason
    drop_raw(env, "b.eml", inline("in-2", "ZSR OR Kosice <vyluky.orke@zsr.example>"))
    s = process_mailbox(session)
    assert s["auto_applied"] == 0 and s["waiting_confirmation"] == 1, s
    (ev,) = events(session)
    assert ev.needs_confirmation and any("preposlan" in r for r in ev.validation["confirmation_reasons"])


def test_p1_closure_from_non_zsr_sender_needs_confirmation(env, session):
    """Výluku (rovnako trasu) zapíše agent automaticky len od ŽSR; od iného povoleného odosielateľa ide na potvrdenie."""
    body = ("Rozkaz o výluke (ROV) č. 1610/2026 – výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš "
            "od 20.11.2026 08:00 do 20.11.2026 14:00.\n")
    drop_raw(env, "p.eml", mail("rov-1610", "ROV c. 1610/2026 - vyluka 1. TK Poprad-Tatry - Liptovsky Mikulas", body,
                                sender="Partner <dispecink@partner.example>", ar=AR_PARTNER))
    s = process_mailbox(session)
    assert s["auto_applied"] == 0 and s["waiting_confirmation"] == 1, s
    (ev,) = events(session)
    assert any("ŽSR" in r for r in ev.validation["confirmation_reasons"])


# ====================================================================== P2 poradie potvrdení a zamietnutí
def _direct_change_1142(env, mid: str = "zmena-1142") -> None:
    drop_raw(env, f"{mid}.eml", mail(mid, "Zmena ROV c. 1142/2026 - posun terminu vyluky Poprad-Tatry - Liptovsky "
                                          "Mikulas", CHANGE_1142_BODY))


def _confirm_change_1142(env, session) -> None:
    """Priama zmena 1142 (posun termínu) výluke odoberá zatvorený čas – agent ju nezapíše sám, čaká na dispečera
    (do potvrdenia ostáva pôvodná výluka a nový termín má dočasný záznam); dispečer ju potvrdí."""
    _direct_change_1142(env)
    s = process_mailbox(session)
    assert s["auto_applied"] == 0 and s["waiting_confirmation"] == 1, s
    ev = events(session)[-1]
    assert ev.kind == "vyluka_zmena" and ev.status == "navrh"
    assert any("odobrať zatvorený čas" in r for r in ev.validation["confirmation_reasons"])
    confirm_event(session, ev.id, "dispecer1")
    session.commit()


def test_p2_change_removing_closed_time_waits_for_dispatcher(env, session):
    """Priama zmena 1142 (09:30 – 16:00 namiesto 08:00 – 14:00) by odobrala čas 08:00 – 09:30: do potvrdenia ostáva
    pôvodná výluka a nový termín je dočasný záznam „navrh“ – plánovač počíta s oboma."""
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    process_mailbox(session)
    _direct_change_1142(env)
    assert process_mailbox(session)["auto_applied"] == 0
    e1, e2 = events(session)
    rows = sorted(active(session), key=lambda c: c.id)
    assert [(c.source_event_id, as_utc(c.start_utc), as_utc(c.end_utc)) for c in rows] == [
        (e1.id, utc(2026, 11, 12, 7), utc(2026, 11, 12, 13)), (e2.id, utc(2026, 11, 12, 8, 30), utc(2026, 11, 12, 15))]
    assert rows[1].proposed_change_of == rows[0].id and e2.needs_confirmation


def test_p2_confirming_older_event_does_not_revert_newer_change(env, session):
    """01 je zapísaný automaticky, priamu zmenu 1142 potvrdí dispečer; potvrdenie staršieho 01 potom nesmie vrátiť
    koniec 16:00 na 14:00."""
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    process_mailbox(session)
    _confirm_change_1142(env, session)
    e1, e2 = events(session)
    (c,) = active(session)
    assert c.source_event_id == e2.id and as_utc(c.end_utc) == utc(2026, 11, 12, 15)
    confirm_event(session, e1.id, "dispecer1")
    session.commit()
    session.refresh(c)
    assert as_utc(c.start_utc) == utc(2026, 11, 12, 8, 30) and as_utc(c.end_utc) == utc(2026, 11, 12, 15)
    assert c.status == "overene" and c.source_event_id == e2.id
    assert e1.status == "aplikovane" and e1.validation["apply_result"]["action"] == "nahradene"
    assert e2.status == "aplikovane" and len(active(session)) == 1


def test_p2_confirming_queued_original_after_confirmed_change(env, session):
    """ROV 1500 na 4.11. čaká vo fronte, zmena na 10.11. sa potvrdí prvá – potvrdenie pôvodného ROV ju neprepíše."""
    drop_raw(env, "1.eml", mail("r1500", "ROV c. 1500/2026 - vyluka 1. TK Poprad-Tatry - Liptovsky Mikulas",
                               "Rozkaz o výluke (ROV) č. 1500/2026 – výluka 1. TK v úseku Poprad-Tatry – Liptovský "
                               "Mikuláš od 04.11.2026 08:00 do 04.11.2026 14:00.\n"))
    process_mailbox(session)
    drop_raw(env, "2.eml", mail("z1500", "Zmena ROV c. 1500/2026 - posun terminu",
                               "Oznamujeme zmenu Rozkazu o výluke č. 1500/2026 (výluka 1. TK v úseku Poprad-Tatry – "
                               "Liptovský Mikuláš).\nNový termín: od 10.11.2026 08:00 do 10.11.2026 14:00\n"))
    process_mailbox(session)
    e1, e2 = events(session)
    assert e1.status == e2.status == "navrh" and e1.needs_confirmation and e2.needs_confirmation
    confirm_event(session, e2.id, "dispecer1")
    session.commit()
    confirm_event(session, e1.id, "dispecer1")
    session.commit()
    (c,) = active(session)
    assert as_utc(c.start_utc) == utc(2026, 11, 10, 7) and c.status == "overene" and c.source_event_id == e2.id
    assert e1.validation["apply_result"]["action"] == "nahradene"


def test_p2_rejecting_superseded_original_keeps_closure_of_newer_change(env, session):
    """Zamietnutie staršieho 01 nesmie zrušiť výluku, ktorú už vlastní novšia (platná) zmena."""
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    process_mailbox(session)
    _confirm_change_1142(env, session)
    e1, e2 = events(session)
    reject_event(session, e1.id, "dispecer1", "nahradené zmenou ROV")
    session.commit()
    (c,) = active(session)
    assert c.source_event_id == e2.id and as_utc(c.end_utc) == utc(2026, 11, 12, 15) and e2.status == "aplikovane"
    assert "novšia udalosť" in e1.validation["rejection"]["rollback"]


def test_p2_rejecting_newest_change_restores_last_valid_version(env, session):
    """Dve zmeny za sebou: zamietnutie staršej zmenu nechá, zamietnutie novšej obnoví pôvodný ROV (nie zamietnutý stav)."""
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    process_mailbox(session)
    _confirm_change_1142(env, session)
    drop_raw(env, "z2.eml", mail("zmena-1142-b", "Zmena ROV c. 1142/2026 - dalsi posun",
                                 CHANGE_1142_BODY.replace("od 12.11.2026 09:30 do 12.11.2026 16:00",
                                                          "od 12.11.2026 10:00 do 12.11.2026 17:00")))
    process_mailbox(session)
    e1, e2, e3 = events(session)
    confirm_event(session, e3.id, "dispecer1")        # aj ďalší posun odoberá čas – potvrdí ho dispečer
    session.commit()
    (c,) = active(session)
    assert c.source_event_id == e3.id and as_utc(c.end_utc) == utc(2026, 11, 12, 16)
    reject_event(session, e2.id, "dispecer1", "neplatí")
    session.commit()
    assert c.source_event_id == e3.id and as_utc(c.end_utc) == utc(2026, 11, 12, 16)
    reject_event(session, e3.id, "dispecer1", "neplatí")
    session.commit()
    assert c.source_event_id == e1.id and c.status == "navrh"
    assert (as_utc(c.start_utc), as_utc(c.end_utc)) == (utc(2026, 11, 12, 7), utc(2026, 11, 12, 13))


def test_p2_older_avizo_does_not_touch_handover_of_newer_avizo(env, session):
    """Zamietnutie (aj potvrdenie) staršieho avíza nesmie zmazať ani prepísať preberanie s novším ETA."""
    session.add(Train(number="47120", run_date="2026-11-06", origin="Čadca", destination="Žilina",
                      status="planovany"))
    session.commit()
    for mid, text in (("av1", "06.11.2026 10:00"), ("av2", "06.11.2026 13:30")):
        drop_raw(env, f"{mid}.eml", mail(mid, "Avizo vlaku 47120 - Cadca",
                                         f"Avízo: vlak Nex 47120, predpokladaný príchod do Čadce {text}.\n",
                                         sender="Partner <dispecink@partner.example>", ar=AR_PARTNER))
        process_mailbox(session)
    a1, a2 = events(session)
    (h,) = session.query(Handover).all()
    assert a1.status == a2.status == "aplikovane" and h.source_event_id == a2.id
    reject_event(session, a1.id, "dispecer1", "staré avízo")
    session.commit()
    (h,) = session.query(Handover).all()
    assert as_utc(h.avizo_eta_utc) == utc(2026, 11, 6, 12, 30) and h.source_event_id == a2.id
    a1b = session.get(ExtractedEvent, a1.id)
    assert a1b.status == "zamietnute"
    # potvrdenie staršieho avíza (inej kópie) tiež nesmie vrátiť starší čas
    drop_raw(env, "av3.eml", mail("av3", "Avizo vlaku 47120 - Cadca",
                                  "Avízo: vlak Nex 47120, predpokladaný príchod do Čadce 06.11.2026 10:00.\n",
                                  sender="Partner <dispecink@partner.example>", ar=AR_PARTNER))
    process_mailbox(session)
    (h,) = session.query(Handover).all()
    assert h.source_event_id != a2.id                    # novšie avízo av3 (10:00) sa zapísalo normálne
    a3 = events(session)[-1]
    confirm_event(session, a2.id, "dispecer1")           # staršie a2 (13:30) je už nahradené novším av3
    assert as_utc(session.query(Handover).one().avizo_eta_utc) == utc(2026, 11, 6, 9, 0)
    assert session.query(Handover).one().source_event_id == a3.id


# ====================================================================== P3 horizont 72 h pri zmene
@pytest.mark.parametrize("status", ["overene", "navrh"])
def test_p3_moving_imminent_closure_needs_confirmation(env, session, fixed_now, status):
    """Zmena, ktorá presúva výluku začínajúcu o 2 h, sa nesmie zapísať bez dispečera (M5 – 72 h)."""
    c = Closure(kind="kolaj", from_station="Poprad-Tatry", to_station="Liptovský Mikuláš", track="1",
                start_utc=fixed_now + timedelta(hours=2), end_utc=fixed_now + timedelta(hours=8),
                status=status, source="manual", external_ref="1200/2026")
    session.add(c)
    session.commit()
    drop_raw(env, "z.eml", mail("zmena-1200", "Zmena ROV c. 1200/2026 - posun terminu vyluky Poprad-Tatry - "
                                              "Liptovsky Mikulas",
                                "Oznamujeme zmenu Rozkazu o výluke č. 1200/2026 (výluka 1. TK v úseku Poprad-Tatry – "
                                "Liptovský Mikuláš).\n\nNový termín: od 20.11.2026 08:00 do 20.11.2026 14:00\n"))
    s = process_mailbox(session)
    (ev,) = events(session)
    assert s["auto_applied"] == 0 and ev.needs_confirmation and ev.status == "navrh"
    reasons = " ".join(ev.validation["confirmation_reasons"])
    assert "72 h" in reasons
    if status == "overene":
        assert "overen" in reasons
    session.refresh(c)
    assert as_utc(c.start_utc) == fixed_now + timedelta(hours=2) and c.status == status and c.version == 1
    # plánovač počíta s oboma možnosťami: pôvodná výluka aj navrhovaný nový termín (dočasný záznam „návrh“)
    tmp = session.scalars(select(Closure).where(Closure.source_event_id == ev.id)).one()
    assert tmp.id != c.id and tmp.status == "navrh" and tmp.proposed_change_of == c.id
    assert as_utc(tmp.start_utc) == utc(2026, 11, 20, 7)
    ids = {w.closure_id for w in windows(session, fixed_now)}
    assert {c.id, tmp.id} <= ids


def test_p3_resent_identical_rov_does_not_downgrade_verified_closure(env, session):
    """Opakovane doručený rovnaký ROV (iný Message-ID) nesmie overenú výluku vrátiť do stavu „návrh“."""
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    process_mailbox(session)
    (e1,) = events(session)
    confirm_event(session, e1.id, "dispecer1")
    session.commit()
    raw = (SAMPLES / "01_rov_vyluka_kolaje_poprad_lm.eml").read_bytes().replace(b"<ukazka-01.fiktivne@",
                                                                                 b"<ukazka-01-kopia@")
    drop_raw(env, "kopia.eml", raw)
    process_mailbox(session)
    (c,) = active(session)
    assert c.status == "overene" and c.version == 1


# ====================================================================== P4 priradenie výluky podľa čísla
def test_p4_same_number_on_other_section_creates_new_closure(env, session):
    """ROV 142/2026 OR Bratislava nesmie prepísať overenú výluku 142/2026 OR Košice na inom úseku."""
    kosice = Closure(kind="uplna", from_station="Poprad-Tatry", to_station="Liptovský Mikuláš",
                     start_utc=utc(2026, 11, 20, 7), end_utc=utc(2026, 11, 20, 15), status="overene", source="manual",
                     external_ref="142/2026")
    session.add(kosice)
    session.commit()
    body = ("Na základe Rozkazu o výluke (ROV) č. 142/2026 bude vykonaná výluka koľaje:\n\n"
            "Úsek: Bratislava-Petržalka – Rusovce, 1. traťová koľaj (1. TK)\n"
            "Termín: od 25.11.2026 08:00 do 25.11.2026 12:00\n")
    drop_raw(env, "a.eml", mail("rov-142-orba", "Rozkaz o vyluke (ROV) c. 142/2026 - vyluka 1. TK "
                                                "Bratislava-Petrzalka - Rusovce", body,
                                sender="ZSR OR Bratislava <vyluky.orba@zsr.example>"))
    process_mailbox(session)
    (ev,) = events(session)
    if ev.status != "aplikovane":
        confirm_event(session, ev.id, "dispecer1")
        session.commit()
    rows = active(session)
    assert len(rows) == 2
    session.refresh(kosice)
    assert (kosice.from_station, kosice.to_station, kosice.status, kosice.version) == \
        ("Poprad-Tatry", "Liptovský Mikuláš", "overene", 1)
    new = next(r for r in rows if r.id != kosice.id)
    assert (new.from_station, new.to_station) == ("Bratislava-Petržalka", "Rusovce")
    assert any("142/2026" in w for w in ev.validation["warnings"])


def test_p4_ref_key_and_document_series(cfg):
    from vlaky.posta.spracovanie import ref_key, refs_match

    assert ref_key("ROV č. 1142/2026") == ref_key("1142/2026") == ref_key("č. 1142/2026") == ref_key("ROV 1142/2026")
    assert refs_match("ROV č. 1142/2026", "1142/2026")
    assert not refs_match("TLG 15/2026", "ROV 15/2026")             # iný druh dokumentu
    assert not refs_match("POTR 15/2026", "TLG č. 15/2026")
    assert refs_match("TLG 15/2026", "15/2026") and not refs_match("15/2026", "16/2026")
    assert not refs_match("", "") and not refs_match("1/12026", "11/2026")


def test_p4_without_number_tracks_must_agree(env, session):
    """Bez čísla výluky sa výluka 1. TK a 2. TK s rovnakým úsekom a časom nesmú zlúčiť."""
    first = Closure(kind="kolaj", from_station="Poprad-Tatry", to_station="Liptovský Mikuláš", track="1",
                    start_utc=utc(2026, 11, 20, 7), end_utc=utc(2026, 11, 20, 13), status="overene", source="manual")
    session.add(first)
    session.commit()
    drop_raw(env, "t.eml", mail("tk2", "Vyluka 2. TK Poprad-Tatry - Liptovsky Mikulas",
                                "Výluka 2. TK v úseku Poprad-Tatry – Liptovský Mikuláš od 20.11.2026 08:00 "
                                "do 20.11.2026 14:00.\n"))
    process_mailbox(session)
    rows = active(session)
    assert len(rows) == 2 and {r.track for r in rows} == {"1", "2"}
    session.refresh(first)
    assert first.track == "1" and first.status == "overene" and as_utc(first.end_utc) == utc(2026, 11, 20, 13)


def test_p4_change_without_number_does_not_hit_distant_closure(env, session):
    """Zmena bez čísla nesmie „uchmatnúť“ ľubovoľnú výluku na úseku (napr. o mesiac neskôr)."""
    far = Closure(kind="kolaj", from_station="Poprad-Tatry", to_station="Liptovský Mikuláš", track="1",
                  start_utc=utc(2026, 12, 20, 7), end_utc=utc(2026, 12, 20, 13), status="overene", source="manual")
    session.add(far)
    session.commit()
    drop_raw(env, "z.eml", mail("zmena-bez-cisla", "Zmena vyluky Poprad-Tatry - Liptovsky Mikulas",
                                "Oznamujeme zmenu výluky 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš.\n\n"
                                "Nový termín: od 20.11.2026 08:00 do 20.11.2026 14:00\n"))
    process_mailbox(session)
    (ev,) = events(session)
    assert ev.needs_confirmation and any("nedá priradiť" in r for r in ev.validation["confirmation_reasons"])
    session.refresh(far)
    assert as_utc(far.start_utc) == utc(2026, 12, 20, 7) and far.status == "overene" and far.version == 1


# ====================================================================== P5 jedovatá správa (NUL) v IMAP
POISON = (b"From: anyone@internet.example\r\nTo: agent.dispecing@dopravca.example\r\nSubject: hi\r\n"
          b"Message-ID: <x\x00y@internet.example>\r\n"
          b"Authentication-Results: mx.dopravca.example; dmarc=pass\x00 header.from=internet.example\r\n"
          b"Content-Type: text/plain\r\n\r\nhello\r\n")


def _flat(params):  # noqa: ANN001
    if isinstance(params, dict):
        for v in params.values():
            yield from _flat(v)
    elif isinstance(params, (list, tuple)):
        for v in params:
            yield from _flat(v)
    else:
        yield params


@pytest.fixture()
def pg_like(cfg):
    """Emulácia PostgreSQL/psycopg: text s NUL alebo s osamelými surrogate znakmi databáza odmietne."""
    import psycopg

    from vlaky.db import engine

    def check(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        for p in _flat(params):
            if isinstance(p, str):
                if "\x00" in p:
                    raise psycopg.DataError("PostgreSQL text fields cannot contain NUL (0x00) bytes")
                p.encode("utf-8")

    sa_event.listen(engine(), "before_cursor_execute", check)
    yield
    sa_event.remove(engine(), "before_cursor_execute", check)


@pytest.fixture()
def imap(env, monkeypatch):
    from test_posta_zdroje import FakeImap

    FakeImap.uidvalidity = 1001
    FakeImap.log = []
    for k, v in {"mail_source": "imap", "imap_host": "imap.dopravca.example", "imap_user": "agent",
                 "imap_password": "heslo"}.items():
        monkeypatch.setattr(env, k, v)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    return FakeImap


def test_p5_nul_in_headers_does_not_block_imap(imap, session, pg_like):
    """NUL v Message-ID/Authentication-Results od kohokoľvek nesmie natrvalo zastaviť príjem pošty."""
    from vlaky.posta.zdroje import kv_get

    imap.messages = {1: POISON, 2: (SAMPLES / "01_rov_vyluka_kolaje_poprad_lm.eml").read_bytes()}
    s = process_mailbox(session)
    assert s["fetched"] == 2, s["errors"]
    poison, rov = session.query(Email).order_by(Email.id).all()
    assert "\x00" not in poison.message_id + poison.auth_results + poison.sender + poison.subject
    assert poison.verdict == "karantena" and rov.verdict == "ok"
    assert len(events(session)) == 1 and session.query(Closure).count() == 1
    assert kv_get(session, "imap_last_uid")["last_uid"] == 2


def test_p5_encoded_nul_and_control_characters_are_removed(cfg):
    from vlaky.posta.parser import parse_message

    raw = (b"From: =?utf-8?q?ZSR=00?= <vyluky@zsr.example>\r\nSubject: =?utf-8?q?V=00yluka?=\r\n"
           b"Message-ID: =?utf-8?q?<a=00b=40x>?=\r\nAuthentication-Results: mx.dopravca.example;\x01 dmarc=pass\r\n"
           b"Content-Type: text/plain\r\n\r\ntext\r\n")
    pm = parse_message(raw)
    for value in (pm.message_id, pm.subject, pm.sender, pm.sender_name, pm.from_header, *pm.auth_results):
        assert not any(ord(ch) < 32 for ch in value), repr(value)
    assert pm.sender == "vyluky@zsr.example" and pm.message_id == "<ab@x>"


def test_p5_unstorable_mail_goes_to_dead_letter_after_retries(imap, session, monkeypatch):
    """E-mail, ktorý sa opakovane nedá uložiť, po niekoľkých pokusoch skončí ako záznam v karanténe a front ide ďalej."""
    from vlaky.posta.zdroje import kv_get

    imap.messages = {1: b"From: x@zsr.example\r\nSubject: zly\r\nMessage-ID: <zly@zsr.example>\r\n\r\nzly e-mail\r\n",
                     2: (SAMPLES / "01_rov_vyluka_kolaje_poprad_lm.eml").read_bytes()}
    real = spracovanie._store_one

    def broken(session, raw):  # noqa: ANN001
        if raw.uid == "1":
            raise RuntimeError("neuložiteľný e-mail")
        return real(session, raw)

    monkeypatch.setattr(spracovanie, "_store_one", broken)
    for _ in range(spracovanie.STORE_RETRIES - 1):
        s = process_mailbox(session)
        assert s["fetched"] == 0 and any("neuložiteľný" in e for e in s["errors"])
        assert session.query(Email).count() == 0
    s = process_mailbox(session)
    dead, rov = session.query(Email).order_by(Email.id).all()
    assert dead.verdict == "karantena" and dead.status == "chyba" and dead.message_id.startswith("sha256:")
    assert "neuložiteľný" in dead.error and Path(dead.raw_path).exists()
    assert rov.verdict == "ok" and len(events(session)) == 1
    assert kv_get(session, "imap_last_uid")["last_uid"] == 2


# ====================================================================== čas doručenia (IMAP INTERNALDATE)
def test_imap_internaldate_accepts_space_padded_day(cfg):
    """RFC 3501: deň 1 – 9 môže mať namiesto nuly medzeru („ 2-Nov-2026“) – čas doručenia sa nesmie stratiť."""
    from vlaky.posta.zdroje import _internaldate

    assert _internaldate(b'1 (UID 1 INTERNALDATE " 2-Nov-2026 10:00:00 +0000" BODY[] {5}') == utc(2026, 11, 2, 10)
    assert _internaldate(b'1 (UID 1 INTERNALDATE "02-Nov-2026 10:00:00 +0100" BODY[] {5}') == utc(2026, 11, 2, 9)
    assert _internaldate(b'1 (UID 1 INTERNALDATE "12-Nov-2026 10:00:00 -0130" BODY[] {5}') == utc(2026, 11, 12, 11, 30)


def test_received_at_comes_from_source_not_from_date_header(imap, session, monkeypatch, fixed_now):
    """Spätne datovaná hlavička Date (napr. rok 2015) nesmie určiť čas prijatia (uchovávanie, súhrny, odovzdávka)."""
    from vlaky.posta.zdroje import RawMail

    class Padded(imap):
        def uid(self, command, *args):  # noqa: ANN001, ANN002
            typ, data = super().uid(command, *args)
            if command == "FETCH":
                meta, raw = data[0]
                data = [(meta.replace(b'INTERNALDATE "02-', b'INTERNALDATE " 2-'), raw), data[1]]
            return typ, data

    monkeypatch.setattr(imaplib, "IMAP4_SSL", Padded)
    old = (SAMPLES / "01_rov_vyluka_kolaje_poprad_lm.eml").read_bytes().replace(b"Date: Mon, 02 Nov 2026",
                                                                                 b"Date: Mon, 02 Nov 2015")
    imap.messages = {1: old}
    process_mailbox(session)
    (em,) = session.query(Email).all()
    assert as_utc(em.received_at) == utc(2026, 11, 2, 6, 12)            # INTERNALDATE „ 2-Nov-2026 07:12 +0100“
    # zdroj čas doručenia neuviedol → čas spracovania, nie hlavička Date
    other = old.replace(b"<ukazka-01.fiktivne@", b"<ukazka-01-bez-casu@")
    email = spracovanie._store_one(session, RawMail(data=other, source="imap", uid="77", received_at=None))
    assert as_utc(email.received_at) == fixed_now


# ====================================================================== P6 skloňovanie názvov dopravní
def test_p6_skalica_is_not_matched_to_skalite(env, session):
    """„Senica – Skalica“ nesmie skončiť ako výluka Senica – Skalité (240 km ďalej) bez potvrdenia."""
    drop_raw(env, "s.eml", mail("rov-1400", "Rozkaz o vyluke c. 1400/2026 - vyluka Senica - Skalica",
                                "Rozkaz o výluke (ROV) č. 1400/2026 – úplná výluka úseku Senica – Skalica,\n"
                                "od 25.11.2026 08:00 do 25.11.2026 14:00.\n",
                                sender="ZSR OR Trnava <vyluky.ortt@zsr.example>"))
    s = process_mailbox(session)
    (ev,) = events(session)
    assert s["auto_applied"] == 0 and ev.needs_confirmation
    assert "Skalité" not in str(ev.data)
    assert all(c.to_station != "Skalité" and c.from_station != "Skalité" for c in session.query(Closure))


def test_p6_stem_matching_accepts_only_inflections(cfg):
    from vlaky.posta.stanice import stations

    st = stations()
    m = st.match("Skalica")
    assert m is not None and m.name == "Skalica na Slovensku" and m.method == "priblizne"
    for text in ("Skalice", "Skalici"):
        m = st.match(text)
        assert m is None or (m.name != "Skalité" and m.method != "sklonovanie"), (text, m)
    m = st.match("Kremnička")
    assert m is None or m.method == "priblizne"
    for text, name in [("Čadce", "Čadca"), ("Bratislavy-Petržalky", "Bratislava-Petržalka"),
                       ("Dunajskej Stredy", "Dunajská Streda"), ("Ružomberka", "Ružomberok"), ("Košíc", "Košice"),
                       ("Košiciach", "Košice"), ("Vrútkach", "Vrútky"), ("Nových Zámkov", "Nové Zámky"),
                       ("Kút", "Kúty"), ("Štúrova", "Štúrovo"), ("Lučenca", "Lučenec"), ("Žilinou", "Žilina"),
                       ("Leopoldova", "Leopoldov"), ("Michaloviec", "Michalovce"), ("Senice", "Senica")]:
        m = st.match(text)
        assert m is not None and (m.name, m.method) == (name, "sklonovanie"), (text, m)


def test_p6_unusually_long_section_needs_confirmation(session, fixed_now):
    from vlaky.posta.validacia import validate_event

    vr = validate_event(session, "vyluka_nova", {"from_station": "Senica", "to_station": "Skalité",
                                                 "start": "2026-11-25T07:00:00+00:00",
                                                 "end": "2026-11-25T13:00:00+00:00", "kind": "uplna"},
                        confidence=0.97)
    assert vr.needs_confirmation and any("dlhý" in r for r in vr.reasons), vr.reasons


# ====================================================================== P7 pád medzi extrakciou a zápisom
def test_p7_crash_between_extraction_and_apply_is_recovered(env, session, monkeypatch):
    """Proces padne hneď po commite extrakcie – pri ďalšom cykle sa udalosť zapíše (nič sa nestratí)."""
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    real = spracovanie.apply_event

    def killed(*a, **k):  # noqa: ANN002, ANN003
        raise SystemExit("killed")

    monkeypatch.setattr(spracovanie, "apply_event", killed)
    with pytest.raises(SystemExit):
        process_mailbox(session)
    session.rollback()
    monkeypatch.setattr(spracovanie, "apply_event", real)
    s = process_mailbox(session)
    (ev,) = events(session)
    assert ev.status == "aplikovane" and s["auto_applied"] == 1, s
    (c,) = active(session)
    assert c.source_event_id == ev.id


def test_p7_failed_auto_apply_goes_to_queue(env, session, monkeypatch):
    """Chyba pri automatickom zápise (napr. zamknutá databáza) pošle udalosť do fronty – neostane „nikde“."""
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")

    def locked(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("database is locked")

    monkeypatch.setattr(spracovanie, "apply_event", locked)
    s = process_mailbox(session)
    (ev,) = events(session)
    assert any("database is locked" in e for e in s["errors"])
    assert ev.status == "navrh" and ev.needs_confirmation and ev in queue(session)
    assert "database is locked" in ev.validation["apply_error"]
    (c,) = active(session)                               # plánovač ju vidí ako nepotvrdenú
    assert c.status == "navrh" and c.source_event_id == ev.id


# ====================================================================== P8 polia len z LLM
def test_p8_llm_only_key_fields_are_not_trusted(env, session, monkeypatch):
    """Začiatok a koniec našiel len LLM (pravidlá nie) – žiadne zvýšenie istoty, vždy potvrdenie dispečerom."""
    from vlaky.posta import llm
    from vlaky.posta.udalosti import ExtractedItem

    def fake_extract(self, doc):  # noqa: ANN001 – model si „6 dní“ vyloží ako 6 hodín
        return [ExtractedItem("vyluka_nova", {"from_station": "Poprad-Tatry", "to_station": "Liptovský Mikuláš",
                                              "start": "2026-11-20T07:00:00+00:00",
                                              "end": "2026-11-20T13:00:00+00:00", "kind": "kolaj", "track": "1"},
                              0.9, [], {}, {}, extractor="llm")]

    monkeypatch.setattr(llm.LLMExtractor, "enabled", staticmethod(lambda: True))
    monkeypatch.setattr(llm.LLMExtractor, "extract", fake_extract)
    drop_raw(env, "x.eml", mail("potrva", "Vyluka 1. TK Poprad-Tatry - Liptovsky Mikulas",
                                "Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš začne 20.11.2026 o 08:00 "
                                "a potrvá 6 dní.\n"))
    s = process_mailbox(session)
    (ev,) = events(session)
    assert s["auto_applied"] == 0 and ev.needs_confirmation and ev.status == "navrh"
    assert ev.confidence < env.confirm_threshold
    assert not any("zhodujú" in n for n in ev.validation["notes"])
    assert any("LLM" in r for r in ev.validation["confirmation_reasons"])


def test_p8_combine_marks_llm_only_fields(cfg):
    from vlaky.posta.extrakcia import combine
    from vlaky.posta.udalosti import ExtractedItem

    base = {"from_station": "Poprad-Tatry", "to_station": "Liptovský Mikuláš", "kind": "kolaj"}
    r = ExtractedItem("vyluka_nova", dict(base), 0.68)
    lm = ExtractedItem("vyluka_nova", {**base, "start": "2026-11-20T07:00:00+00:00",
                                       "end": "2026-11-20T13:00:00+00:00"}, 0.95, extractor="llm")
    (it,) = combine([r], [lm])
    assert sorted(it.unverified) == ["end", "start"] and it.confidence < cfg.confirm_threshold
    assert it.field_source["start"] == "llm" and it.field_source["from_station"] == "pravidla+llm"
    # úplná zhoda kľúčových polí istotu zvýši ako doteraz
    full = ExtractedItem("vyluka_nova", {**base, "start": "2026-11-20T07:00:00+00:00",
                                         "end": "2026-11-20T13:00:00+00:00"}, 0.85)
    (ok,) = combine([full], [lm])
    assert ok.unverified == [] and ok.confidence >= 0.95


# ====================================================================== P9 do odvolania
def test_p9_open_ended_restriction_stays_active_and_dispatcher_is_reminded(env, session, fixed_now):
    """POTR „do odvolania“ nesmie po 30 dňoch potichu zmiznúť z plánovania; dispečer dostane pripomienku."""
    from vlaky.cas import set_fake_now

    drop(env, "17_telegram_potr.eml")
    process_mailbox(session)
    (ev,) = events(session)
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    (c,) = active(session)
    assert c.until_revoked is True and c.kind == "pomala_jazda"
    later = utc(2026, 12, 1, 8)                       # 28 dní po začiatku, ŽSR obmedzenie neodvolala
    set_fake_now(later)
    process_mailbox(session, now=later)
    session.refresh(c)
    assert as_utc(c.end_utc) >= later + timedelta(days=spracovanie.OPEN_END_DAYS - 1)
    assert c.status == "overene" and c.until_revoked
    t2 = utc(2026, 12, 4, 8)
    assert [w.closure_id for w in windows(session, t2, 24)] == [c.id]
    reminders = session.scalars(select(Notification).where(Notification.subject.contains("do odvolania"))).all()
    assert reminders and all("Kysak" in n.body or "Košice" in n.body for n in reminders)
    assert session.scalar(select(AuditLog).where(AuditLog.action == "vyluka_do_odvolania_predlzena")) is not None


def test_p9_legacy_open_ended_closure_is_recognised(env, session, fixed_now):
    """Záznam zo staršej verzie (len poznámka „do odvolania“, bez príznaku) sa tiež posúva a nevypadne."""
    c = Closure(kind="pomala_jazda", from_station="Kysak", to_station="Košice", speed_kmh=30,
                start_utc=fixed_now - timedelta(days=28), end_utc=fixed_now + timedelta(days=2), status="overene",
                source="email", until_revoked=None,
                note="Koniec neurčený (do odvolania) – technicky nastavený na +30 dní, aktualizujte po oznámení "
                     "ukončenia.")
    session.add(c)
    session.commit()
    # stĺpec pridaný migráciou (ADD COLUMN) má v starších riadkoch NULL
    session.execute(update(Closure).where(Closure.id == c.id).values(until_revoked=None))
    session.commit()
    process_mailbox(session, now=fixed_now)
    session.refresh(c)
    assert c.until_revoked is True
    assert as_utc(c.end_utc) == fixed_now + timedelta(days=spracovanie.OPEN_END_DAYS)


# ====================================================================== P10 SPF+DKIM bez DMARC, hlavička From
def test_p10_spf_dkim_fallback_requires_aligned_identity(cfg):
    from vlaky.posta.overenie import check_auth

    ids = ["mx.dopravca.example"]
    # null reverse-path: SPF overené na HELO útočníka, DKIM podpísaný doménou útočníka, From = ŽSR
    assert check_auth(["mx.dopravca.example; spf=pass smtp.helo=mail.evil.example; dkim=pass header.d=evil.example"],
                      "zsr.example", ids)[0] is False
    # dvojdielna verejná prípona: evil.co.hu nie je tá istá organizácia ako partner.co.hu
    assert check_auth(["mx.dopravca.example; spf=pass smtp.mailfrom=x@evil.co.hu; dkim=pass header.d=evil.co.hu"],
                      "partner.co.hu", ids)[0] is False
    assert check_auth(["mx.dopravca.example; dmarc=pass header.from=evil.co.hu"], "partner.co.hu", ids)[0] is False
    # zarovnaná aspoň jedna identita – v poriadku
    assert check_auth(["mx.dopravca.example; spf=pass smtp.mailfrom=bounce@esp.example; dkim=pass "
                       "header.d=mail.zsr.example"], "zsr.example", ids)[0] is True
    assert check_auth(["mx.dopravca.example; spf=pass smtp.mailfrom=x@partner.co.hu; dkim=pass header.d=esp.example"],
                      "partner.co.hu", ids)[0] is True


def test_p10_bare_address_as_display_name_is_quarantined(cfg):
    from vlaky.posta.overenie import KARANTENA, verify
    from vlaky.posta.parser import parse_message

    raw = eml("From: vyluky@zsr.example <x@evil.example>\nTo: agent.dispecing@dopravca.example\nSubject: ROV\n"
              "Message-ID: <q@evil.example>\nContent-Type: text/plain; charset=\"utf-8\"\n", "Výluka.\n")
    pm = parse_message(raw)
    assert pm.from_count >= 2
    assert verify(pm).verdict == KARANTENA


# ====================================================================== P11 duplicitný Message-ID
LIST_BODY = """Na základe Rozkazu o výluke (ROV) č. 1300/2026 bude vykonaná výluka koľaje:
Úsek: Poprad-Tatry – Liptovský Mikuláš, 1. traťová koľaj (1. TK)
Termín: od 20.11.2026 08:00 do 20.11.2026 14:00
"""


def test_p11_authentic_copy_replaces_quarantined_copy(env, session):
    """Kópia z distribučného zoznamu (DMARC fail) ide do karantény; overená priama kópia ju nahradí a spracuje sa."""
    common = ("From: ZSR OR Kosice <vyluky.orke@zsr.example>\nTo: agent.dispecing@dopravca.example\n"
              "Subject: ROV c. 1300/2026 - vyluka 1. TK Poprad-Tatry - Liptovsky Mikulas\n"
              "Date: Mon, 02 Nov 2026 05:10:00 +0100\nMessage-ID: <rov-1300@zsr.example>\n"
              "Content-Type: text/plain; charset=\"utf-8\"\n")
    bad = AR_ZSR.replace("spf=pass", "spf=fail").replace("dkim=pass", "dkim=fail").replace("dmarc=pass", "dmarc=fail")
    drop_raw(env, "1.eml", eml(bad + common, LIST_BODY + "\n-- list footer --\n"))
    assert process_mailbox(session)["quarantined"] == 1
    drop_raw(env, "2.eml", eml(AR_ZSR + common, LIST_BODY))
    s = process_mailbox(session)
    (em,) = session.query(Email).all()
    assert em.verdict == "ok" and s["fetched"] == 1 and len(events(session)) == 1
    assert session.scalar(select(AuditLog).where(AuditLog.action == "email_nahradeny_overenou_kopiou")) is not None
    # ďalšia (podvrhnutá) kópia s rovnakým Message-ID už nič nezmení
    drop_raw(env, "3.eml", eml(bad + common, LIST_BODY.replace("14:00", "18:00")))
    assert process_mailbox(session)["fetched"] == 0 and session.query(Email).one().verdict == "ok"


def test_p11_graph_source_does_not_skip_quarantined_message_id(env, session, monkeypatch):
    from vlaky.posta.zdroje import get_source

    session.add_all([Email(message_id="<q@zsr.example>", verdict="karantena", status="spracovany"),
                     Email(message_id="<ok@zsr.example>", verdict="ok", status="spracovany")])
    session.commit()
    monkeypatch.setattr(env, "mail_source", "graph")
    src = get_source(session)
    assert src.known("<ok@zsr.example>") is True and src.known("<q@zsr.example>") is False


# ====================================================================== P12 dátumy a časy
def _rules(body: str):
    from vlaky.posta.pravidla import RuleExtractor
    from vlaky.posta.udalosti import MailDocument

    return RuleExtractor().extract(MailDocument(subject="Výluka", body=body, received_at=utc(2026, 11, 2, 6)))


@pytest.mark.parametrize("text", ["v noci z 12. na 13.11.2026 od 22:00 do 6:00",
                                  "v noci 12./13.11.2026 od 22:00 do 06:00",
                                  "v noci z 12.11. na 13.11.2026 od 22:00 do 6:00"])
def test_p12_night_closure_is_anchored_on_first_day(cfg, text):
    (it,) = _rules(f"Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš {text}.")
    assert (it.data["start"], it.data["end"]) == ("2026-11-12T21:00:00+00:00", "2026-11-13T05:00:00+00:00")


def test_p12_second_time_window_is_not_dropped(cfg):
    items = _rules("Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš 12.11.2026 v čase 8:00 - 12:00 a 13:00 - 16:00")
    assert sorted((i.data["start"], i.data["end"]) for i in items) == [
        ("2026-11-12T07:00:00+00:00", "2026-11-12T11:00:00+00:00"),
        ("2026-11-12T12:00:00+00:00", "2026-11-12T15:00:00+00:00")]


def test_p12_unused_time_lowers_confidence(cfg):
    (it,) = _rules("Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš 12.11.2026 od 8:00 do 12:00, "
                   "práce potrvajú do 15:30 podľa potreby.")
    assert it.confidence < 0.8 and any("čas" in n and "nepodarilo priradiť" in n for n in it.notes)


# ====================================================================== nepotvrdené výluky v plánovači
ROV_1700 = ("Rozkaz o výluke (ROV) č. 1700/2026 – výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš "
            "od 03.11.2026 08:00 do 03.11.2026 14:00.\n")


def _pending_rov_1700(env, session) -> tuple[ExtractedEvent, Closure]:
    drop_raw(env, "r.eml", mail("rov-1700", "ROV c. 1700/2026 - vyluka 1. TK Poprad-Tatry - Liptovsky Mikulas",
                                ROV_1700))
    s = process_mailbox(session)
    assert s["waiting_confirmation"] == 1 and s["auto_applied"] == 0, s
    (ev,) = events(session)
    (c,) = session.query(Closure).all()
    return ev, c


def test_pending_new_closure_is_planned_and_confirm_applies_edits(env, session, fixed_now):
    ev, c = _pending_rov_1700(env, session)                  # do 72 h → na potvrdenie
    assert (c.status, c.source_event_id, c.proposed_change_of) == ("navrh", ev.id, None)
    assert [w.status for w in windows(session, fixed_now, 72) if w.closure_id == c.id] == ["navrh"]
    confirm_event(session, ev.id, "dispecer1", edits={"end": "2026-11-03T14:00:00+00:00"})
    session.commit()
    (c2,) = session.query(Closure).all()
    assert c2.id == c.id and c2.status == "overene" and as_utc(c2.end_utc) == utc(2026, 11, 3, 14)
    assert ev.status == "aplikovane"


def test_pending_new_closure_reject_rolls_back_only_own_row(env, session, fixed_now):
    ev, c = _pending_rov_1700(env, session)
    reject_event(session, ev.id, "dispecer1", "neplatí")
    session.commit()
    assert c.status == "zrusene" and windows(session, fixed_now, 72) == []
    # riadok, ktorý medzitým prevzala iná udalosť, zamietnutie nezmení
    drop_raw(env, "r2.eml", mail("rov-1701", "ROV c. 1701/2026 - vyluka 1. TK Poprad-Tatry - Liptovsky Mikulas",
                                 ROV_1700.replace("1700", "1701")))
    process_mailbox(session)
    ev2 = events(session)[-1]
    c2 = session.scalars(select(Closure).where(Closure.source_event_id == ev2.id)).one()
    c2.source_event_id = None                               # napr. dispečer výluku prevzal ručne
    session.commit()
    reject_event(session, ev2.id, "dispecer1", "duplicita")
    session.commit()
    assert c2.status == "navrh"


def test_pending_mimoriadnost_is_planned(env, session, fixed_now):
    drop(env, "06_mimoriadna_udalost.eml")
    process_mailbox(session)
    (ev,) = events(session)
    assert ev.kind == "mimoriadnost" and ev.needs_confirmation
    (c,) = session.query(Closure).all()
    assert c.kind == "mimoriadnost" and c.status == "navrh" and c.source_event_id == ev.id
    assert any(w.closure_id == c.id for w in windows(session, fixed_now, 72))
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    (c2,) = session.query(Closure).all()
    assert c2.id == c.id and c2.status == "overene"


def test_pending_change_keeps_original_and_confirm_or_reject_cleans_up(env, session, fixed_now):
    """Čakajúca zmena: pôvodná výluka ostane, nový termín je dočasný „návrh“; potvrdenie/zamietnutie ho uprace."""
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    process_mailbox(session)
    (orig,) = session.query(Closure).all()
    drop(env, "04_zmena_vyluky_preposlana.eml")
    process_mailbox(session)
    (change,) = events(session, "vyluka_zmena")
    tmp = session.scalars(select(Closure).where(Closure.source_event_id == change.id)).one()
    assert tmp.proposed_change_of == orig.id and tmp.status == "navrh"
    assert as_utc(orig.end_utc) == utc(2026, 11, 12, 13) and as_utc(tmp.end_utc) == utc(2026, 11, 12, 15)
    ids = [w.closure_id for w in windows(session, fixed_now)]
    assert orig.id in ids and tmp.id in ids
    reject_event(session, change.id, "dispecer1", "zmena neplatí")
    session.commit()
    assert session.get(Closure, tmp.id) is None and [c.id for c in active(session)] == [orig.id]
    assert as_utc(orig.end_utc) == utc(2026, 11, 12, 13)
    # druhá zmena (ten istý e-mail inou cestou) – potvrdenie zmení pôvodnú výluku a dočasný záznam odstráni
    raw = (SAMPLES / "04_zmena_vyluky_preposlana.eml").read_bytes().replace(b"<ukazka-04.fiktivne@",
                                                                             b"<ukazka-04-kopia@")
    drop_raw(env, "04b.eml", raw)
    process_mailbox(session)
    change2 = events(session, "vyluka_zmena")[-1]
    tmp2 = session.scalars(select(Closure).where(Closure.source_event_id == change2.id)).one()
    confirm_event(session, change2.id, "dispecer1")
    session.commit()
    assert session.get(Closure, tmp2.id) is None
    (c,) = active(session)
    assert c.id == orig.id and c.status == "overene" and as_utc(c.end_utc) == utc(2026, 11, 12, 15)
    assert c.source_event_id == change2.id and c.version == 2


def test_pending_cancellation_keeps_closure_active(env, session, fixed_now):
    drop(env, "02_oznamenie_vyluk_tyzden_47.eml", "05_zrusenie_vyluky.eml")
    process_mailbox(session)
    target = session.scalars(select(Closure).where(Closure.external_ref == "1150/2026")).one()
    (cancel,) = events(session, "vyluka_zrusenie")
    assert cancel.needs_confirmation and target.status == "navrh"
    assert any(w.closure_id == target.id for w in windows(session, fixed_now))
    confirm_event(session, cancel.id, "dispecer1")
    session.commit()
    assert target.status == "zrusene" and all(w.closure_id != target.id for w in windows(session, fixed_now))


def test_web_queue_paths_work_with_pending_rows(env, session, client, fixed_now):
    """Web: potvrdenie dočasného záznamu cez /vyluky/{id}/overit a zamietnutie cez /fronta/{id}/zamietnut."""
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    process_mailbox(session)
    drop(env, "04_zmena_vyluky_preposlana.eml")
    process_mailbox(session)
    (orig,) = [c for c in session.query(Closure) if c.proposed_change_of is None]
    (change,) = events(session, "vyluka_zmena")
    tmp_id = session.scalars(select(Closure.id).where(Closure.source_event_id == change.id)).one()
    orig_id, change_id = orig.id, change.id
    session.commit()
    login_as(client, "dispecer")
    page = client.get("/fronta").text
    assert f'id="udalost-{change_id}"' in page
    r = client.post(f"/vyluky/{tmp_id}/overit", data={"csrf_token": csrf(client, "/vyluky"), "spat": "/vyluky"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/vyluky"
    session.expire_all()
    assert session.get(Closure, tmp_id) is None
    c = session.get(Closure, orig_id)
    assert c.status == "overene" and as_utc(c.end_utc) == utc(2026, 11, 12, 15)
    # zamietnutie čakajúcej novej výluky cez frontu
    drop_raw(env, "r.eml", mail("rov-1700", "ROV c. 1700/2026 - vyluka 1. TK Poprad-Tatry - Liptovsky Mikulas",
                                ROV_1700))
    process_mailbox(session)
    ev = events(session)[-1]
    row_id = session.scalars(select(Closure.id).where(Closure.source_event_id == ev.id)).one()
    ev_id = ev.id
    session.commit()
    r = client.post(f"/fronta/{ev_id}/zamietnut", data={"csrf_token": csrf(client, "/fronta"), "dovod": "omyl"},
                    follow_redirects=False)
    assert r.status_code == 303
    session.expire_all()
    assert session.get(ExtractedEvent, ev_id).status == "zamietnute"
    assert session.get(Closure, row_id).status == "zrusene"


# ====================================================================== mail_last_ok (zdravie schránky)
def test_mail_last_ok_only_after_successful_fetch(env, session, monkeypatch, fixed_now):
    from vlaky.agent.zdravie import get_kv, set_kv
    from vlaky.db import session_scope
    from vlaky.planovanie import planovac

    old = {"ts": "2026-11-01T00:00:00+00:00", "fetched": 0}
    set_kv(session, "mail_last_ok", old)
    session.commit()
    monkeypatch.setattr(env, "mail_source", "imap")                 # IMAP nie je nastavené → MailSourceError
    s = process_mailbox(session, now=fixed_now)
    assert s["fetch_ok"] is False and get_kv(session, "mail_last_ok") == old

    def boom(session=None):  # noqa: ANN001
        raise RuntimeError("výpadok siete")

    real_get_source = spracovanie.get_source
    monkeypatch.setattr(env, "mail_source", "directory")
    monkeypatch.setattr(spracovanie, "get_source", boom)            # iná chyba zdroja
    s = process_mailbox(session, now=fixed_now)
    assert s["fetch_ok"] is False and get_kv(session, "mail_last_ok") == old
    monkeypatch.setattr(spracovanie, "get_source", real_get_source)
    seen: list = []

    def fake_eval(session, now=None, trigger=""):  # noqa: ANN001 – plánovač musí vidieť čerstvý (uložený) stav
        with session_scope() as other:
            seen.append(get_kv(other, "mail_last_ok"))
        return []

    monkeypatch.setattr(planovac, "evaluate_all", fake_eval)
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    s = process_mailbox(session, now=fixed_now)
    assert s["fetch_ok"] is True and s["auto_applied"] == 1
    assert get_kv(session, "mail_last_ok") == {"ts": fixed_now.isoformat(), "fetched": 1}
    assert seen == [{"ts": fixed_now.isoformat(), "fetched": 1}]
    # jeden neuložiteľný e-mail nie je zlyhanie zdroja
    later = fixed_now + timedelta(minutes=5)
    monkeypatch.setattr(spracovanie, "_store_one", lambda session, raw: (_ for _ in ()).throw(RuntimeError("x")))
    drop(env, "13_objednavka_pdf_cz.eml")
    s = process_mailbox(session, now=later)
    assert s["fetch_ok"] is True and s["errors"]
    assert get_kv(session, "mail_last_ok") == {"ts": later.isoformat(), "fetched": 0}


def test_zsr_odosielatelia_z_nastavenia(cfg):
    from vlaky.config import override_settings
    from vlaky.posta.overenie import zsr_entries, zsr_sender

    assert zsr_sender("vyluky@zsr.example")              # odvodené z povolených domén zsr.*
    assert not zsr_sender("objednavky@zakaznik.example")
    override_settings(**{**cfg.model_dump(), "zsr_senders": "kis@zsr.example, or.zsr.example"})
    assert zsr_entries() == ["kis@zsr.example", "or.zsr.example"]
    assert zsr_sender("kis@zsr.example") and zsr_sender("vyluky@or.zsr.example")
    assert not zsr_sender("vyluky@zsr.example")           # nie je vo výslovnom zozname
