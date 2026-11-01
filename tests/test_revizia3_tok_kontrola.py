"""Tretia revízia – oblasť „tok“, kontrola opráv: nálezy oponentského preverenia opráv MR2, UI2 a MS3.

* MR2 – vlak, ktorý začína alebo končí vnútri úseku čakajúcej výluky bez záznamu pre plánovač, a čakajúce
  oznámenie, ktorého záznam prevzala novšia potvrdená udalosť (falošné obmedzenie),
* UI2 – avízo pre novú jazdu vlaku, ktorého predošlá jazda s rovnakým číslom je ukončená,
* MS3 – automatické odpovede (neprítomnosť, potvrdenie o prečítaní) na e-maily agenta.

Scenár je opísaný v docstringu testu. Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest
from sqlalchemy import select

from vlaky.hranice import seed_borders
from vlaky.models import Closure, Email, ExtractedEvent, Handover, Notification, Path as TrainPath, \
    Recommendation, Train
from vlaky.posta.spracovanie import confirm_event, process_mailbox

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "ukazkove_emaily"
UTC = timezone.utc
AR = ("Authentication-Results: mx.dopravca.example; spf=pass smtp.mailfrom={d}; dkim=pass\n"
      " header.d={d}; dmarc=pass header.from={d}\n")
ZSR = "ZSR OR Poprad <vyluky.orpp@zsr.example>"
AGENT = "agent.dispecing@dopravca.example"
ROV_2700 = ("Rozkaz o výluke č. 2700/2026\nÚplná výluka oboch traťových koľají v úseku Poprad-Tatry – Liptovský "
            "Mikuláš od 12.11.2026 (štvrtok) 8:00 do 13.11.2026 (piatok) 14:00.\n")


@pytest.fixture()
def strict(cfg, session, fixed_now, monkeypatch):
    """Overenie odosielateľa ako v prevádzke (DMARC, dôveryhodný server)."""
    monkeypatch.setattr(cfg, "require_dmarc_pass", True)
    monkeypatch.setattr(cfg, "trusted_authserv_ids", "mx.dopravca.example")
    seed_borders(session)
    session.commit()
    return cfg


@pytest.fixture()
def planner(monkeypatch):
    """Podvrhnutý prepočet všetkých vlakov (testy, ktoré nezávisia od výpočtu odporúčaní po e-maile)."""
    from vlaky.planovanie import planovac

    calls: list[str] = []
    monkeypatch.setattr(planovac, "evaluate_all", lambda session, now=None, trigger="": calls.append(trigger) or [])
    return calls


def mail(mid: str, subject: str, body: str, sender: str = ZSR, date: str = "Mon, 02 Nov 2026 05:10:00 +0100") -> bytes:
    domain = sender.split("@")[-1].rstrip(">")
    headers = (f"{AR.format(d=domain)}From: {sender}\nTo: {AGENT}\nSubject: {subject}\nDate: {date}\n"
               f"Message-ID: <{mid}@{domain}>\nContent-Type: text/plain; charset=\"utf-8\"\n")
    return (headers + "\n" + body).replace("\n", "\r\n").encode("utf-8")


def drop(cfg, name: str, data: bytes) -> None:
    (Path(cfg.mail_dir) / name).write_bytes(data)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def events(session) -> list[ExtractedEvent]:
    return list(session.scalars(select(ExtractedEvent).order_by(ExtractedEvent.id)))


def _route(origin: str, destination: str):
    """Trasa vlaku tak, ako ju plánovač odovzdá do pending_uncertainty (vlaky.siet.graf.Route)."""
    from vlaky.siet.graf import TrainSpec, network

    return network().route(origin, destination, TrainSpec(traction="E", gauge="1435", vmax_kmh=100))


def _train(session, number: str, origin: str, destination: str, dep: datetime) -> Train:
    t = Train(number=number, origin=origin, destination=destination, traction="E", gauge="1435", vmax_kmh=80,
              status="planovany", ready_utc=dep, deadline_utc=dep + timedelta(hours=8))
    session.add(t)
    session.flush()
    session.add(TrainPath(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena", departure_utc=dep,
                          points=[]))
    session.commit()
    return t


def _pending_notes(session, train: Train, route, start: datetime) -> list[str]:
    from vlaky.posta.spracovanie import pending_uncertainty

    return pending_uncertainty(session, train, route, (), window=(start - timedelta(hours=2),
                                                                  start + timedelta(hours=30)))


# ====================================================================== MR2 úsek čakajúcej výluky na trase vlaku
def test_mr2_vlak_zacina_alebo_konci_vnutri_useku_vyluky_bez_terminu(strict, session, planner):
    """[MR2 – kontrola] ŽSR oznámi úplnú výluku Poprad-Tatry – Liptovský Mikuláš s termínom, ktorý pravidlá
    neprečítajú (udalosť nemá záznam pre plánovač). Vlak, ktorý začína alebo končí vnútri úseku (Štrba → Žilina,
    Žilina → Štrba), ide po vylúčenej časti trate rovnako ako vlak cez celý úsek – nesmie byť ZELENÝ. Úsek sa na
    trasu vlaku umiestni ako výluka z registra (koľaje úseku), nie len podľa koncových dopravní. Vlak, ktorý
    z koncovej dopravne úseku ide na druhú stranu (Liptovský Mikuláš → Žilina), ani vlak na inej trati sa netýka."""
    from vlaky.cas import set_fake_now

    now = utc(2026, 11, 12, 5)
    set_fake_now(now)
    drop(strict, "rov.eml", mail("rov2700", "ROV c. 2700/2026 Poprad-Tatry - Liptovsky Mikulas", ROV_2700,
                                 date="Thu, 12 Nov 2026 06:00:00 +0100"))
    process_mailbox(session, now=now)
    session.commit()
    (ev,) = events(session)
    assert ev.data.get("start") is None and ev.validation["pending_closure"]["id"] is None
    dep = utc(2026, 11, 12, 9)
    cases = {("77502", "Poprad-Tatry", "Liptovský Mikuláš"): True,     # celý úsek
             ("77510", "Štrba", "Žilina"): True,                       # začína vnútri úseku
             ("77511", "Žilina", "Štrba"): True,                       # končí vnútri úseku
             ("77512", "Liptovský Mikuláš", "Žilina"): False,          # z koncovej dopravne na druhú stranu
             ("77513", "Čadca", "Žilina"): False}                      # iná trať
    got = {}
    for (number, origin, destination), _expected in cases.items():
        t = _train(session, number, origin, destination, dep)
        got[(number, origin, destination)] = _pending_notes(session, t, _route(origin, destination), dep)
    for key, expected in cases.items():
        notes = got[key]
        if expected:
            assert len(notes) == 1, (key, notes)
            assert "Poprad-Tatry – Liptovský Mikuláš" in notes[0] and f"#{ev.id}" in notes[0], (key, notes)
            assert "ZELENÝ" in notes[0]
        else:
            assert notes == [], (key, notes)


def test_mr2_planovac_vlak_vnutri_useku_vyluky_bez_terminu(strict, session, planner):
    """[MR2 – kontrola] Integrácia s plánovačom: keď plánovač volá posta.spracovanie.pending_uncertainty, vlak
    Štrba → Žilina (začína vnútri úseku oznámenej výluky bez prečítaného termínu) nie je VYPRAVIŤ ZELENÁ a dôvod
    pomenuje udalosť; vlak Liptovský Mikuláš → Žilina ostáva bez obmedzenia. Kým napojenie v plánovači nie je,
    test sa preskočí."""
    import inspect

    from vlaky.cas import set_fake_now
    from vlaky.planovanie import planovac

    if "pending_uncertainty" not in inspect.getsource(planovac):
        pytest.skip("plánovač ešte nevolá posta.spracovanie.pending_uncertainty (zmena v planovac.evaluate_train)")
    now = utc(2026, 11, 12, 5)
    set_fake_now(now)
    drop(strict, "rov.eml", mail("rov2700", "ROV c. 2700/2026 Poprad-Tatry - Liptovsky Mikulas", ROV_2700,
                                 date="Thu, 12 Nov 2026 06:00:00 +0100"))
    process_mailbox(session, now=now)
    session.commit()
    (ev,) = events(session)
    dep = utc(2026, 11, 12, 9)
    inside = _train(session, "77510", "Štrba", "Žilina", dep)
    west = _train(session, "77512", "Liptovský Mikuláš", "Žilina", dep)
    rec = planovac.evaluate_train(session, inside.id, now, "kontrola")
    assert rec.color != "ZELENÁ" and any(f"(udalosť #{ev.id})" in r for r in rec.reasons), rec.reasons
    rec = planovac.evaluate_train(session, west.id, now, "kontrola")
    assert not any(f"(udalosť #{ev.id})" in r for r in rec.reasons), rec.reasons


# ====================================================================== MR2 záznam prevzatý novšou udalosťou
ROV_9100 = ("Rozkaz o výluke č. 9100/2026: výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš dňa 03.11.2026 "
            "od 08:00 do 14:00.\n")


def test_mr2_cakajuce_oznamenie_prevzate_potvrdenou_zmenou_neobmedzi_vlak(strict, session, planner, fixed_now):
    """[MR2 – kontrola] ROV 9100/2026 na 03.11. čaká na potvrdenie (záznam „navrh“) a ŽSR ho presunie na 05.11.
    (čakajúca zmena). Dispečer potvrdí najprv novšiu zmenu – výluka v registri je 05.11. a patrí zmene. Staršie
    oznámenie, ktoré ešte čaká, plánovač nevidí „naslepo“: jeho záznam prevzala novšia udalosť, ktorá má prednosť
    (po potvrdení bude nahradené). Vlak 03.11. preto nedostane falošné obmedzenie s dôvodom „údaje nestačia“."""
    drop(strict, "a.eml", mail("rov9100", "ROV c. 9100/2026", ROV_9100))
    process_mailbox(session, now=fixed_now)
    drop(strict, "b.eml", mail("zmena9100", "Zmena ROV c. 9100/2026", "Oznamujeme zmenu Rozkazu o výluke č. "
                               "9100/2026 (výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš).\nNový termín: "
                               "05.11.2026 od 08:00 do 14:00.\n", date="Mon, 02 Nov 2026 06:10:00 +0100"))
    process_mailbox(session, now=fixed_now)
    session.commit()
    ann, change = events(session)
    assert (ann.kind, change.kind) == ("vyluka_nova", "vyluka_zmena")
    confirm_event(session, change.id, "dispecer1")
    session.commit()
    (row,) = session.scalars(select(Closure)).all()
    assert (row.source_event_id, row.status, row.start_utc.day) == (change.id, "overene", 5)
    dep = utc(2026, 11, 3, 8)
    t = _train(session, "77521", "Poprad-Tatry", "Liptovský Mikuláš", dep)
    assert _pending_notes(session, t, _route("Poprad-Tatry", "Liptovský Mikuláš"), dep) == []
    confirm_event(session, ann.id, "dispecer1")                 # novšia zmena má prednosť
    assert ann.validation["apply_result"]["action"] == "nahradene"


def test_mr2_cakajuce_oznamenie_po_potvrdenom_zruseni_neobmedzi_vlak(strict, session, planner, fixed_now):
    """[MR2 – kontrola] ROV 9100/2026 na 03.11. čaká na potvrdenie a ŽSR ho zruší; dispečer potvrdí zrušenie
    (záznam výluky je zrušený a patrí zrušeniu). Čakajúce oznámenie už nesmie obmedzovať vlaky v úseku – výluka sa
    nekoná, novšie zrušenie má prednosť."""
    drop(strict, "a.eml", mail("rov9100", "ROV c. 9100/2026", ROV_9100))
    process_mailbox(session, now=fixed_now)
    drop(strict, "b.eml", mail("zrus9100", "Zrusenie ROV c. 9100/2026", "Rozkaz o výluke č. 9100/2026 (výluka 1. TK "
                               "v úseku Poprad-Tatry – Liptovský Mikuláš) sa ruší. Výluka sa nekoná.\n",
                               date="Mon, 02 Nov 2026 06:10:00 +0100"))
    process_mailbox(session, now=fixed_now)
    session.commit()
    ann, cancel = events(session)
    assert (ann.kind, cancel.kind) == ("vyluka_nova", "vyluka_zrusenie")
    confirm_event(session, cancel.id, "dispecer1")
    session.commit()
    (row,) = session.scalars(select(Closure)).all()
    assert (row.source_event_id, row.status) == (cancel.id, "zrusene")
    assert ann.status == "navrh" and ann.needs_confirmation
    dep = utc(2026, 11, 3, 8)
    t = _train(session, "77520", "Poprad-Tatry", "Liptovský Mikuláš", dep)
    assert _pending_notes(session, t, _route("Poprad-Tatry", "Liptovský Mikuláš"), dep) == []


# ====================================================================== UI2 avízo a ukončená jazda vlaku
def test_ui2_avizo_ked_predosla_jazda_vlaku_je_ukoncena(strict, session, planner, fixed_now):
    """[UI2 – kontrola] Vlak Nex 47250 jazdí každý týždeň; minulotýždňová jazda (27.10.) je v systéme ukončená.
    Avízo na 03.11.: hláška nesmie radiť opraviť dátum ukončeného vlaku, ale založiť vlak pre túto jazdu (s dátumom
    jazdy z avíza). Ak dispečer aj tak zmení dátum ukončeného vlaku, avízo sa k nemu nepriradí (plánovač ukončený
    vlak nevyhodnocuje – preberanie by zapadlo bez odporúčania). Nový vlak založený bez dátumu jazdy avízo dostane
    a vo fronte je upozornenie, že vlak nemá dátum jazdy."""
    old = Train(number="47250", run_date="2026-10-27", origin="Čadca", destination="Košice", status="ukonceny")
    session.add(old)
    session.commit()
    shutil.copy(SAMPLES / "10_avizo_cadca_cz.eml", Path(strict.mail_dir) / "10.eml")
    process_mailbox(session, now=fixed_now)
    (ev,) = [e for e in events(session) if e.kind == "avizo"]
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    err = ev.validation["apply_result"]["error"]
    assert "založte vlak pre túto jazdu (dátum jazdy 03.11.2026)" in err and "ukončená" in err, err
    assert "opravte" not in err, err
    # dispečer aj tak „opraví“ dátum ukončeného vlaku – avízo sa ukončenému vlaku nepriradí
    old.run_date = "2026-11-03"
    session.commit()
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    assert ev.validation["apply_result"]["ok"] is False and session.query(Handover).count() == 0
    err = ev.validation["apply_result"]["error"]
    assert "s dátumom jazdy 03.11.2026 je v systéme ukončený" in err and "zmeňte stav vlaku" in err, err
    new = Train(number="47250", run_date="", origin="Čadca", destination="Košice", status="planovany")
    session.add(new)
    session.commit()
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    assert ev.validation["apply_result"]["ok"] is True and ev.status == "aplikovane"
    (h,) = session.scalars(select(Handover)).all()
    assert h.train_id == new.id and new.status == "na_ceste_k_hranici" and old.status == "ukonceny"
    assert any("nemá dátum jazdy" in w for w in ev.validation["warnings"]), ev.validation["warnings"]


def test_ui2_avizo_pre_vlak_bez_datumu_jazdy_potvrdzuje_dispecer(strict, session, fixed_now):
    """[UI2 – kontrola] Jediný aktívny vlak 47250 nemá dátum jazdy – môže to byť aj minulotýždňová, neukončená
    jazda s rovnakým číslom. Avízo sa mu priradí len po potvrdení dispečerom (ani pri vysokej istote a príchode
    o 5 dní sa nezapíše automaticky, rovnako ako predtým, keď sa takému vlaku nepriradilo vôbec) a fronta na to
    upozorní. Vlaku s dátumom jazdy z avíza sa avízo priradí bez tohto dôvodu."""
    from vlaky.cas import local
    from vlaky.posta.validacia import validate_event

    t = Train(number="47250", run_date="", origin="Čadca", destination="Košice", status="pripraveny")
    session.add(t)
    session.commit()
    eta = fixed_now + timedelta(days=5)
    data = {"train_number": "Nex 47250", "border": "Čadca", "eta": eta.isoformat()}
    res = validate_event(session, "avizo", dict(data), confidence=0.97, verdict="ok")
    assert res.train_id == t.id and res.needs_confirmation, res.reasons
    assert any("bez dátumu jazdy" in r for r in res.reasons)
    assert any("nemá dátum jazdy" in w for w in res.warnings)
    t.run_date = local(eta).date().isoformat()
    session.commit()
    res = validate_event(session, "avizo", dict(data), confidence=0.97, verdict="ok")
    assert res.train_id == t.id and not res.needs_confirmation, res.reasons
    assert not any("dátumu jazdy" in w for w in res.warnings)


# ====================================================================== MS3 automatické odpovede
def _auto_mail(cfg, name: str, *, subject: str, body: str, rec_id: int, headers: dict | None = None,
               sender: str = "dispecer1@dopravca.example", report: bool = False) -> None:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = AGENT
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{name}@dopravca.example>"
    for k, v in (headers or {}).items():
        msg[k] = v
    text = f"{body}\n\n-----Pôvodná správa-----\n> Detail a potvrdenie: http://localhost:8000/odporucania/{rec_id}\n"
    if report:                                   # potvrdenie o prečítaní (MDN, RFC 8098)
        msg.set_content(text)
        msg.add_attachment("Reporting-UA: klient.dopravca.example\nFinal-Recipient: rfc822; " + sender +
                           "\nDisposition: manual-action/MDN-sent-manually; displayed\n",
                           subtype="disposition-notification")
        msg.replace_header("Content-Type", msg["Content-Type"].replace("multipart/mixed", "multipart/report"))
    else:
        msg.set_content(text)
    (Path(cfg.mail_dir) / f"{name}.eml").write_bytes(bytes(msg))


def _answers(session, recipient: str) -> list[Notification]:
    return list(session.scalars(select(Notification).where(Notification.kind == "odpoved",
                                                           Notification.recipient == recipient)
                                .order_by(Notification.id)))


def test_ms3_automaticke_odpovede_bez_odpovede_agenta(cfg, session, fixed_now):
    """[MS3 – kontrola] Na e-mail odporúčania príde automatická odpoveď dispečera (neprítomnosť, Auto-Submitted),
    potvrdenie o prečítaní (multipart/report) či správa s predmetom automatickej odpovede. Agent na ne neodpovedá –
    ani po uplynutí intervalu deduplikácie (inak slučka s automatickou odpoveďou) – a „PRIJAŤ“ v automatickej
    správe stav odporúčania nezmení (nie je to rozhodnutie dispečera). Odpoveď napísaná dispečerom bez platného
    príkazu vysvetlenie dostane; správe, ktorá automatické odpovede odmieta (X-Auto-Response-Suppress), agent
    neodpovedá, príkaz z nej však platí."""
    from vlaky.cas import set_fake_now

    t = Train(number="95801", origin="Žilina", destination="Košice", status="planovany")
    session.add(t)
    session.flush()
    rec = Recommendation(train_id=t.id, decision="VYPRAVIŤ", color="ZELENÁ", priority="DÔLEŽITÉ",
                         created_at=fixed_now, state="nove")
    session.add(rec)
    session.commit()
    subject = "[AGENT][DÔLEŽITÉ] Vlak 95801 Žilina → Košice: VYPRAVIŤ"
    ooo = {"Auto-Submitted": "auto-replied", "X-Auto-Response-Suppress": "All"}
    for i, minutes in enumerate((0, 200, 400)):                 # interval deduplikácie je 180 min
        set_fake_now(fixed_now + timedelta(minutes=minutes))
        _auto_mail(cfg, f"ooo{i}", subject=f"Automatická odpoveď: {subject}", rec_id=rec.id, headers=ooo,
                   body="Som mimo kancelárie do 15.11. V naliehavých prípadoch volajte dispečing.")
        process_mailbox(session)
        session.commit()
    assert [(n.status, n.subject) for n in _answers(session, "dispecer1@dopravca.example")] == []
    _auto_mail(cfg, "auto-prijat", subject=f"RE: {subject}", rec_id=rec.id, body="PRIJAŤ",
               headers={"Auto-Submitted": "auto-generated"})
    _auto_mail(cfg, "mdn", subject=f"Prečítané: {subject}", rec_id=rec.id, body="Správa bola zobrazená.", report=True)
    process_mailbox(session)
    session.commit()
    session.refresh(rec)
    assert rec.state == "nove" and not rec.decided_by
    assert _answers(session, "dispecer1@dopravca.example") == []
    mails = {e.message_id: e for e in session.scalars(select(Email))}
    for name in ("ooo0", "ooo1", "ooo2", "auto-prijat", "mdn"):
        e = mails[f"<{name}@dopravca.example>"]
        assert e.status == "spracovany" and "Automatická správa" in e.error, (name, e.error)
    # originál nedostupný (napr. po uchovávacej lehote): rozhodne predmet automatickej odpovede
    from vlaky.posta.odpovede import handle_dispatcher_reply

    plain = Email(message_id="<bez-originalu@dopravca.example>", sender="dispecer1@dopravca.example",
                  subject=f"Automatic reply: {subject}", body_text=f"Out of office.\n/odporucania/{rec.id}\n",
                  verdict="ok", status="prijaty", raw_path="")
    session.add(plain)
    session.flush()
    assert handle_dispatcher_reply(session, plain) and "Automatická správa" in plain.error
    # odpoveď napísaná dispečerom bez príkazu – vysvetlenie dostane (len on)
    _auto_mail(cfg, "clovek", subject=f"RE: {subject}", rec_id=rec.id, body="Ďakujem, pozriem sa na to.")
    process_mailbox(session)
    session.commit()
    (ans,) = _answers(session, "dispecer1@dopravca.example")
    assert ans.status in ("odoslana", "outbox") and "nezačína príkazom" in ans.body
    # správa, ktorá automatické odpovede odmieta: bez vysvetlenia, príkaz z nej platí
    _auto_mail(cfg, "potlacit", subject=f"RE: {subject}", rec_id=rec.id, sender="dispecer2@dopravca.example",
               body="Prijateľnejšie by bolo počkať.", headers={"X-Auto-Response-Suppress": "All"})
    process_mailbox(session)
    session.commit()
    assert _answers(session, "dispecer2@dopravca.example") == []
    _auto_mail(cfg, "potlacit-prijat", subject=f"RE: {subject}", rec_id=rec.id, sender="dispecer2@dopravca.example",
               body="PRIJAŤ", headers={"X-Auto-Response-Suppress": "All"})
    process_mailbox(session)
    session.commit()
    session.refresh(rec)
    assert rec.state == "prijate" and rec.decided_by == "dispecer2@dopravca.example"
