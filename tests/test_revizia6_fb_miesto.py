"""6. revízia (strana FB – register): úsek z registra podľa čísla ROV je len návrh, nikdy automatické doplnenie.

Udalosť len s číslom výluky (bez úseku a termínu z registra) ostáva udalosťou bez miesta – poistka pre celý vozový park
a jedno upozornenie DÔLEŽITÉ, ako pred doplňovaním úseku (FR0, FR1, FR4): e-mail môže uvádzať iné miesto v preposlanom
či citovanom texte, za riadkom „Ďakujeme/Kontakt“, v kalendárovej prílohe, v skratke („N.Zámky“), km polohe či mestskej
časti, a číslo ROV môže patriť inej OR alebo výluke zadanej ručne. Fronta ukáže výluky s týmto číslom (úsek, termín, OR,
druh dokumentu) ako návrh – dispečer jednu vyberie (úsek sa doplní z nej) alebo úsek a termín opraví. Kontrola miesta
pod zámkom registra sa nerobí (FR5).

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

import re
import time

import pytest
from sqlalchemy import select

from vlaky.models import Closure, ExtractedEvent, Notification
from test_revizia3_terminy import as_utc, drop, events, raw_mail, utc
from test_revizia4_pravidla import _decide, _process, env  # noqa: F401 – fixture env
from test_web_spolocne import client, csrf, login_as, web_app  # noqa: F401 – fixtures

ROV_BA = "ZSR OR Bratislava <vyluky.orba@zsr.example>"
ROV_KE = "ZSR OR Kosice <vyluky.orke@zsr.example>"
ROV_ZA = "ZSR OR Zilina <vyluky.orza@zsr.example>"
KD_BA = "Kontrolny dispecer OR Bratislava <kd.orba@zsr.example>"
_ROV4120 = ("Dobrý deň,\n\núplná výluka v úseku Senec – Galanta dňa 12.12.2026 od 08:00 do 14:00 hod. "
            "(ROV 4120/2026).\n\nOR Bratislava\n")


def _confirm(session, ev_id: int, **edits) -> dict:
    from vlaky.posta.spracovanie import confirm_event

    ev = confirm_event(session, ev_id, "dispecer1", edits=edits or None)
    session.commit()
    return (ev.validation or {}).get("apply_result") or {}


def _register(env, session, name: str, subject: str, body: str, sender: str, date: str, now) -> list:
    before = {e.id for e in events(session)}
    drop(env, name, raw_mail(subject, body, name.replace(".", "-"), sender=sender, date=date))
    _process(session, now)
    new = [e for e in events(session) if e.id not in before]
    for e in new:
        _confirm(session, e.id)
    return new


def _new_events(env, session, name: str, subject: str, body: str, sender: str, date: str, now) -> list:
    before = {e.id for e in events(session)}
    drop(env, name, raw_mail(subject, body, name.replace(".", "-"), sender=sender, date=date))
    _process(session, now)
    return [e for e in events(session) if e.id not in before]


def _alerts(session) -> list[str]:
    return list(session.scalars(select(Notification.subject).where(Notification.subject.contains("Na potvrdenie čaká"))))


def _green(r) -> bool:
    return r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"


def _rov4120(env, session) -> Closure:
    _register(env, session, "r.eml", "ROV c. 4120/2026 Senec - Galanta", _ROV4120, ROV_BA,
              "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    [c] = [c for c in session.scalars(select(Closure)) if c.status != "zrusene"]
    return c


_ORIG = "Šurany: výluka podľa ROV 4120/2026 sa presúva na 11.12.2026 od 8:00 do 12:00 hod."
_WRAPS = {
    "fwd_gmail": ("Fwd: ROV 4120/2026", "Na vedomie.\n\n---------- Forwarded message ---------\n"
                                        "Od: ZSR OR Bratislava <vyluky.orba@zsr.example>\nDate: št 26. 11. 2026 o 9:30\n"
                                        "Subject: ROV 4120/2026\nTo: <kd.orba@zsr.example>\n\n" + _ORIG + "\n\nOR Bratislava\n",
                  KD_BA),
    "fwd_outlook": ("FW: ROV 4120/2026", "Preposielam.\n\n________________________________\n"
                                         "Od: ZSR OR Bratislava <vyluky.orba@zsr.example>\nOdoslané: 26. novembra 2026 9:30\n"
                                         "Predmet: ROV 4120/2026\n\n" + _ORIG + "\n\nOR Bratislava\n", KD_BA),
    "povodna_sprava": ("ROV 4120/2026", "Posielam ďalej.\n\n-----Pôvodná správa-----\n"
                                        "Od: ZSR OR Bratislava <vyluky.orba@zsr.example>\nPredmet: ROV 4120/2026\n\n"
                                        + _ORIG + "\n", KD_BA),
    "dakujeme_v_strede": ("ROV 4120/2026", "Dobrý deň,\nďakujeme za trpezlivosť.\n" + _ORIG + "\n\nOR Bratislava\n",
                          ROV_BA),
    "kontakt_v_strede": ("ROV 4120/2026", "Kontaktná osoba: Ing. Ján Novák\n" + _ORIG + "\n\nOR Bratislava\n", ROV_BA),
    "skratka_bez_medzery": ("ROV 4120/2026", "Výluka podľa ROV 4120/2026 sa rozširuje aj na N.Zámky dňa 11.12.2026 od "
                                             "8:00 do 12:00 hod.\n\nOR Bratislava\n", ROV_BA),
    "zkm": ("ROV 4120/2026", "Výluka podľa ROV 4120/2026 sa rozširuje do žkm 25,600 – 27,300 dňa 11.12.2026 od 8:00 do "
                             "12:00 hod.\n\nOR Bratislava\n", ROV_BA),
    "ciarka_bez_medzery": ("ROV 4120/2026", "Výluka podľa ROV 4120/2026 sa presúva do Šurian,dňa 11.12.2026 od 8:00 do "
                                            "12:00 hod.\n\nOR Bratislava\n", ROV_BA),
}


@pytest.mark.parametrize("how", list(_WRAPS))
def test_fr0_fr1_usek_z_registra_sa_nikdy_nedoplni(env, session, how):
    """FR0/FR1: ROV 4120/2026 je v registri na úseku Senec – Galanta; e-mail len s číslom uvádza iné miesto v tvare, ktorý
    pravidlá neprečítali (preposlaný či citovaný text, riadok „Ďakujeme/Kontakt“ v strede, „N.Zámky“, „žkm“, chýbajúca
    medzera). Úsek sa z registra nedoplní: udalosť ostáva bez miesta, vlak Nové Zámky → Šurany 11.12. o 10:00 nedostane
    VYPRAVIŤ ZELENÁ a dispečeri dostanú upozornenie DÔLEŽITÉ (v 631f185 úsek Senec – Galanta z registra, ZELENÁ, žiadne
    upozornenie). Fronta ponúkne výluku #1 ako návrh."""
    from vlaky.posta.spracovanie import register_candidates

    c = _rov4120(env, session)
    subject, body, sender = _WRAPS[how]
    new = _new_events(env, session, "z.eml", subject, body, sender, "Thu, 26 Nov 2026 09:40:00 +0100",
                      utc(2026, 11, 26, 8, 42))
    assert new, how
    for ev in new:
        assert (ev.validation or {}).get("field_source", {}).get("from_station") != "register", ev.validation
        if ev.kind in ("vyluka_zmena", "vyluka_zrusenie") and not ev.data.get("from_station"):
            assert [x["id"] for x in register_candidates(session, ev)] == [c.id]
    r = _decide(session, "72101", "Nové Zámky", "Šurany", utc(2026, 12, 11, 9), utc(2026, 12, 11, 6))
    assert not _green(r), (how, r.summary)
    ref_only = [e for e in new if not e.data.get("from_station")]
    if ref_only:
        assert _alerts(session), "udalosť bez miesta musí dostať upozornenie DÔLEŽITÉ"
    # výluka v registri sa nezmenila
    session.expire_all()
    assert (c.from_station, c.to_station, as_utc(c.start_utc)) == ("Senec", "Galanta", utc(2026, 12, 12, 7))


def test_fr0_kalendarova_priloha_s_miestom(env, session):
    """FR0 (e): miesto len v kalendárovej prílohe (LOCATION: Šurany) – úsek sa z registra nedoplní, vlak Nové Zámky →
    Šurany nie je ZELENÝ."""
    from email.message import EmailMessage

    _rov4120(env, session)
    msg = EmailMessage()
    msg["Authentication-Results"] = ("mx.dopravca.example; spf=pass smtp.mailfrom=zsr.example; dkim=pass "
                                     "header.d=zsr.example; dmarc=pass header.from=zsr.example")
    msg["From"] = ROV_BA
    msg["To"] = "agent.dispecing@dopravca.example"
    msg["Subject"] = "ROV 4120/2026 – presun"
    msg["Date"] = "Thu, 26 Nov 2026 09:40:00 +0100"
    msg["Message-ID"] = "<ics6@zsr.example>"
    msg.set_content("Výluka podľa ROV 4120/2026 sa presúva na 11.12.2026 od 8:00 do 12:00 hod. Termín v prílohe.\n\n"
                    "OR Bratislava\n")
    ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nSUMMARY:Výluka ROV 4120/2026\r\nLOCATION:Šurany\r\n"
           "DTSTART:20261211T070000Z\r\nDTEND:20261211T110000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    msg.add_attachment(ics.encode(), maintype="text", subtype="calendar", filename="termin.ics")
    from pathlib import Path

    (Path(env.mail_dir) / "ics.eml").write_bytes(bytes(msg))
    _process(session, utc(2026, 11, 26, 8, 42))
    new = [e for e in events(session) if e.email_id and e.email_id > 1]
    assert all((e.validation or {}).get("field_source", {}).get("from_station") != "register" for e in new)
    r = _decide(session, "72102", "Nové Zámky", "Šurany", utc(2026, 12, 11, 9), utc(2026, 12, 11, 6))
    assert not _green(r), r.summary


_ODKLAD15 = "Výluka podľa ROV 15/2026 sa odkladá o týždeň, na 17.12.2026 od 8:00 do 14:00 hod."


def _rfc822_forward() -> bytes:
    from email.message import EmailMessage

    inner = EmailMessage()
    inner["Authentication-Results"] = ("mx.dopravca.example; spf=pass smtp.mailfrom=zsr.example; dkim=pass "
                                       "header.d=zsr.example; dmarc=pass header.from=zsr.example")
    inner["From"] = ROV_KE
    inner["To"] = "dispecing@dopravca.example"
    inner["Subject"] = "ROV 15/2026 - odklad"
    inner["Date"] = "Thu, 26 Nov 2026 09:30:00 +0100"
    inner["Message-ID"] = "<inner6-15@zsr.example>"
    inner.set_content("Dobrý deň,\n\n" + _ODKLAD15 + "\n\nOR Košice\n")
    outer = EmailMessage()
    outer["Authentication-Results"] = ("mx.dopravca.example; spf=pass smtp.mailfrom=dopravca.example; dkim=pass "
                                       "header.d=dopravca.example; dmarc=pass header.from=dopravca.example")
    outer["From"] = "Dispecing - automaticke preposielanie <dispecing@dopravca.example>"
    outer["To"] = "agent.dispecing@dopravca.example"
    outer["Subject"] = "Fwd: ROV 15/2026 - odklad"
    outer["Date"] = "Thu, 26 Nov 2026 09:40:00 +0100"
    outer["Message-ID"] = "<outer6-15@dopravca.example>"
    outer.set_content("Automaticky preposlané z dispečerskej schránky (originál je v prílohe).\n")
    outer.add_attachment(inner, filename="povodna_sprava.eml")
    return bytes(outer)


def _inline_forward() -> bytes:
    body = ("Na vedomie.\n\n---------- Forwarded message ---------\nOd: ZSR OR Kosice <vyluky.orke@zsr.example>\n"
            "Date: št 26. 11. 2026 o 9:30\nSubject: ROV 15/2026 - odklad\nTo: <dispecing@dopravca.example>\n\n"
            + _ODKLAD15 + "\n\nOR Košice\n")
    return raw_mail("Fwd: ROV 15/2026 - odklad", body, "fwd6-15", sender="Kontrolny dispecer <kd.dispecing@zsr.example>",
                    date="Thu, 26 Nov 2026 09:40:00 +0100")


@pytest.mark.parametrize("how", ["rfc822", "inline"])
def test_fr4_cislo_inej_or_v_preposlanom_emaili_nezmeni_cudziu_vyluku(env, session, how):
    """FR4 (a): ROV 15/2026 v registri je od OR Žilina; dopravca prepošle (príloha message/rfc822 či vložené Fwd) e-mail
    OR Košice „Výluka podľa ROV 15/2026 sa odkladá o týždeň, na 17.12.2026 …“. OR e-mailu je pôvodný odosielateľ (OR
    Košice): úsek sa nedoplní, Potvrdiť výluku OR Žilina nepresunie a vlak Žilina → Púchov 10.12. o 10:00 ostáva zdržaný
    (v 631f185 sa výluka OR Žilina presunula na 17.12. a vlak dostal ZELENÚ)."""
    from vlaky.posta.spracovanie import mail_issuer, register_candidates

    _register(env, session, "rov15.eml", "ROV c. 15/2026 Zilina - Puchov",
              "Dobrý deň,\n\núplná výluka v úseku Žilina – Púchov dňa 10.12.2026 od 08:00 do 14:00 hod. "
              "(ROV č. 15/2026).\n", ROV_ZA, "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    before = {e.id for e in events(session)}
    drop(env, "f.eml", _rfc822_forward() if how == "rfc822" else _inline_forward())
    _process(session, utc(2026, 11, 26, 8, 42))
    new = [e for e in events(session) if e.id not in before]
    ev = next(e for e in new if e.kind == "vyluka_zmena")
    assert not ev.data.get("from_station"), ev.data
    from vlaky.models import Email

    assert mail_issuer(session.get(Email, ev.email_id)) == "KE"
    cands = register_candidates(session, ev)
    assert cands and any("OR Žilina" in f for x in cands for f in x["flags"]), cands
    res = _confirm(session, ev.id)
    assert res.get("ok") is False, res                       # bez úseku sa zmena nezapíše
    [c] = [c for c in session.scalars(select(Closure)) if c.status != "zrusene" and c.proposed_change_of is None]
    assert as_utc(c.start_utc) == utc(2026, 12, 10, 7), "výluka OR Žilina sa nesmie presunúť"
    r = _decide(session, "72103", "Žilina", "Púchov", utc(2026, 12, 10, 9), utc(2026, 12, 10, 6))
    assert not _green(r), r.summary


def test_fr4_rucna_vyluka_bez_or_sa_nezmeni_bez_vyberu(env, session):
    """FR4 (b): výluka zadaná ručne „ROV č. 15/2026“ Košice – Čaňa (bez OR); e-mail OR Žilina „ROV 15/2026 sa odkladá
    o týždeň …“ – úsek sa nedoplní, Potvrdiť ručnú výluku nepresunie (v 631f185 sa presunula bez upozornenia na OR)."""
    session.add(Closure(kind="uplna", from_station="Košice", to_station="Čaňa", start_utc=utc(2026, 12, 10, 7),
                        end_utc=utc(2026, 12, 10, 13), status="overene", source="manual", external_ref="ROV č. 15/2026"))
    session.commit()
    (ev,) = _new_events(env, session, "z.eml", "ROV 15/2026 - odklad",
                        "Výluka podľa ROV 15/2026 sa odkladá o týždeň, na 17.12.2026 od 8:00 do 14:00 hod.\n\nOR Žilina\n",
                        ROV_ZA, "Thu, 26 Nov 2026 09:40:00 +0100", utc(2026, 11, 26, 8, 42))
    assert ev.kind == "vyluka_zmena" and not ev.data.get("from_station"), ev.data
    res = _confirm(session, ev.id)
    assert res.get("ok") is False, res
    [c] = [c for c in session.scalars(select(Closure)) if c.status != "zrusene" and c.proposed_change_of is None]
    assert as_utc(c.start_utc) == utc(2026, 12, 10, 7)
    r = _decide(session, "72104", "Košice", "Čaňa", utc(2026, 12, 10, 9), utc(2026, 12, 10, 6))
    assert not _green(r), r.summary


def test_fr4_dispecer_vyberie_vyluku_z_registra_vo_fronte(env, session, client):
    """Fronta ukáže výluku s číslom udalosti (úsek, termín, OR, druh dokumentu) a výber „Výluka z registra“; po výbere
    sa zmena zapíše do vybranej výluky (úsek z nej, termín z e-mailu)."""
    c = _rov4120(env, session)
    (ev,) = _new_events(env, session, "z.eml", "ROV 4120/2026 - presun",
                        "Výluka podľa ROV 4120/2026 sa presúva na 13.12.2026 od 8:00 do 14:00 hod.\n\nOR Bratislava\n",
                        ROV_BA, "Thu, 26 Nov 2026 09:40:00 +0100", utc(2026, 11, 26, 8, 42))
    assert ev.kind == "vyluka_zmena" and not ev.data.get("from_station"), ev.data
    login_as(client, "disp1")
    page = client.get(f"/fronta?udalost={ev.id}")
    assert page.status_code == 200
    assert 'name="kandidat"' in page.text and f'value="{c.id}"' in page.text, page.text[:2000]
    assert "Senec – Galanta" in page.text and "OR Bratislava" in page.text and "ROV" in page.text
    form = {"csrf_token": csrf(client), "akcia": "potvrdit", "kandidat": str(c.id), "event_kind": ev.kind}
    resp = client.post(f"/fronta/{ev.id}/potvrdit", data=form, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    session.expire_all()
    ev = session.get(ExtractedEvent, ev.id)
    assert (ev.data.get("from_station"), ev.data.get("to_station")) == ("Senec", "Galanta"), ev.data
    assert (ev.validation or {}).get("apply_result", {}).get("ok") is True, ev.validation.get("apply_result")
    session.refresh(c)
    assert as_utc(c.start_utc) == utc(2026, 12, 13, 7), (c.start_utc, c.end_utc)


def test_fr5_kontrola_miesta_nedrzi_zamok_registra(env, session):
    """FR5: e-mail len s číslom ROV a 58 KB prílohou plnou skratiek (harmonogram) – žiadne prehľadávanie textu pod
    zámkom registra (v 631f185 ~14 s na udalosť pri zámku, súbežné potvrdenie čakalo ~25 s)."""
    _rov4120(env, session)
    row = "N. Xxxx B. Yyyy Sp. Zzzz Ab. Cdef Gh. Ijkl Mn. Opqr práce podľa harmonogramu č. 12 – 15 bez zmeny\n"
    text = row * (58_000 // len(row))
    from email.message import EmailMessage
    from pathlib import Path

    msg = EmailMessage()
    msg["Authentication-Results"] = ("mx.dopravca.example; spf=pass smtp.mailfrom=zsr.example; dkim=pass "
                                     "header.d=zsr.example; dmarc=pass header.from=zsr.example")
    msg["From"] = ROV_BA
    msg["To"] = "agent.dispecing@dopravca.example"
    msg["Subject"] = "ROV 4120/2026 – presun"
    msg["Date"] = "Thu, 26 Nov 2026 09:40:00 +0100"
    msg["Message-ID"] = "<perf6@zsr.example>"
    msg.set_content("Výluka podľa ROV 4120/2026 sa presúva na 11.12. a 12.12.2026 od 8:00 do 12:00 hod., harmonogram "
                    "v prílohe.\n\nOR Bratislava\n")
    msg.add_attachment(text.encode(), maintype="text", subtype="plain", filename="harmonogram.txt")
    (Path(env.mail_dir) / "perf.eml").write_bytes(bytes(msg))
    from vlaky.posta import spracovanie

    held: list = []
    original = spracovanie.lock_register

    def timed(session_):
        original(session_)
        held.append(time.perf_counter())

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(spracovanie, "lock_register", timed)
    try:
        from vlaky.cas import set_fake_now

        set_fake_now(utc(2026, 11, 26, 8, 42))
        email = spracovanie.fetch_and_store(session)
        session.commit()
        (mail,) = [e for e in email if e.subject.startswith("ROV 4120/2026 – presun")]
        held.clear()
        spracovanie.process_email(session, mail)
        done = time.perf_counter()
        session.commit()
    finally:
        mp.undo()
    assert held, "process_email zámok registra nezískal"
    assert done - held[0] < 3.0, f"zámok registra držaný {done - held[0]:.1f} s"
    assert not hasattr(spracovanie, "_mail_names_place")
