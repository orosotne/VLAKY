"""Tretia revízia – oblasť „tok“: potvrdzovanie udalostí z e-mailov, čakajúce udalosti pre plánovač, úpravy
registra výluk a odpovede dispečerov e-mailom (nálezy MR2, MR5, MR7, MR8, MS2, MS3, UI2).

Každý test zodpovedá jednému nálezu (scenár je opísaný v docstringu testu). Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

import html
import re
import shutil
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest
from sqlalchemy import select

from test_web_spolocne import csrf, login_as
from vlaky.hranice import seed_borders
from vlaky.models import AuditLog, Closure, Email, ExtractedEvent, Handover, Notification, Path as TrainPath, \
    Recommendation, Train
from vlaky.posta.spracovanie import confirm_event, process_mailbox, reject_event

pytest_plugins = ["test_web_spolocne"]

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "ukazkove_emaily"
UTC = timezone.utc
AR = ("Authentication-Results: mx.dopravca.example; spf=pass smtp.mailfrom={d}; dkim=pass\n"
      " header.d={d}; dmarc=pass header.from={d}\n")
ZSR = "ZSR OR Poprad <vyluky.orpp@zsr.example>"
KIS = "KIS - ZSR <kis-noreply@zsr.example>"
AGENT = "agent.dispecing@dopravca.example"
SKUPINA = "dispecing@dopravca.example"


# ---------------------------------------------------------------------- spoločné pomôcky
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
    """Podvrhnutý plánovač (testy, ktoré nezávisia od výpočtu odporúčaní); zaznamená spúšťače prepočtu."""
    from vlaky.planovanie import planovac

    calls: list[str] = []

    def fake(session, now=None, trigger=""):  # noqa: ANN001
        calls.append(trigger)
        return []

    monkeypatch.setattr(planovac, "evaluate_all", fake)
    return calls


def mail(mid: str, subject: str, body: str, sender: str = ZSR, auth: str | None = None, extra: str = "",
         date: str = "Mon, 02 Nov 2026 05:10:00 +0100") -> bytes:
    domain = sender.split("@")[-1].rstrip(">")
    ar = AR.format(d=domain) if auth is None else auth
    headers = (f"{ar}{extra}From: {sender}\nTo: {AGENT}\nSubject: {subject}\nDate: {date}\n"
               f"Message-ID: <{mid}@{domain}>\nContent-Type: text/plain; charset=\"utf-8\"\n")
    return (headers + "\n" + body).replace("\n", "\r\n").encode("utf-8")


def drop(cfg, name: str, data: bytes) -> None:
    (Path(cfg.mail_dir) / name).write_bytes(data)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def events(session, kind: str | None = None) -> list[ExtractedEvent]:
    q = select(ExtractedEvent).order_by(ExtractedEvent.id)
    if kind:
        q = q.where(ExtractedEvent.kind == kind)
    return list(session.scalars(q))


def flash(r) -> str:
    return " ".join(re.sub(r"\s+", " ", html.unescape(x)).strip()
                    for x in re.findall(r'class="flash[^"]*"[^>]*>(.*?)</(?:div|p)>', r.text, re.S))


def window_ids(session, start: datetime, hours: int = 24 * 30) -> set:
    from vlaky.planovanie.planovac import closure_windows

    return {w.closure_id for w in closure_windows(session, start, start + timedelta(hours=hours))}


def form_of(page: str) -> dict:
    """Formulár tak, ako ho pošle prehliadač (vstupy, vybrané možnosti, textové pole; nezaškrtnuté políčko nie)."""
    form = {k: html.unescape(v) for k, v in re.findall(r'<input[^>]*name="(\w+)"[^>]*value="([^"]*)"', page)}
    form.pop("electric_only", None)
    for name in ("kind", "status"):
        m = re.search(rf'<select[^>]*name="{name}".*?<option[^>]*value="([^"]*)"[^>]*selected', page, re.S)
        if m:
            form[name] = m.group(1)
    note = re.search(r'<textarea[^>]*name="note"[^>]*>(.*?)</textarea>', page, re.S)
    form["note"] = html.unescape(note.group(1).strip()) if note else ""
    return form


ZMENA_1142 = ("Oznamujeme zmenu Rozkazu o výluke č. 1142/2026 (výluka 1. TK v úseku Poprad-Tatry – Liptovský "
              "Mikuláš).\nNový termín: 14.11.2026 od 08:00 do 14:00.\n")
ZRUSENIE_1142 = ("Rozkaz o výluke č. 1142/2026 (výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš) sa ruší. "
                 "Výluka sa nekoná.\n")


def registered_1142(session) -> int:
    """Overená výluka ROV 1142/2026 (1. TK Poprad-Tatry – Liptovský Mikuláš, 12.11. 08:00 – 14:00)."""
    c = Closure(kind="kolaj", track="1", from_station="Poprad-Tatry", to_station="Liptovský Mikuláš",
                start_utc=utc(2026, 11, 12, 7), end_utc=utc(2026, 11, 12, 13), status="overene", source="email",
                external_ref="1142/2026")
    session.add(c)
    session.commit()
    return c.id


# ====================================================================== MS2 vlastné správy agenta
def _agent_copy(cfg, session, monkeypatch, subject: str, body: str) -> bytes:
    """Správa presne tak, ako ju agent odošle cez SMTP (zachytí ju podvrhnutý server) – kópia pre schránku agenta."""
    from vlaky.notifikacie import odosielanie

    sent: list = []

    class FakeSMTP:
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            self.sock = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):  # noqa: ANN002
            return False

        def starttls(self, **k):  # noqa: ANN003
            return None

        def login(self, *a):  # noqa: ANN002
            return None

        def send_message(self, msg, to_addrs=None):  # noqa: ANN001
            sent.append(msg)
            return {}

    monkeypatch.setattr(odosielanie.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(cfg, "smtp_host", "smtp.dopravca.example")
    monkeypatch.setattr(cfg, "smtp_from", AGENT)
    monkeypatch.setattr(cfg, "notify_emails", f"{SKUPINA},dispecer1@dopravca.example")
    out = odosielanie.send_internal(session, "suhrn", subject, body, "INFO", channels=["email"],
                                    only={("email", SKUPINA)})
    session.commit()
    assert [n.status for n in out] == ["odoslana"] and len(sent) == 1
    return bytes(sent[0])


def test_ms2_kopia_vlastnej_spravy_sa_nespracuje(strict, session, monkeypatch, planner, fixed_now):
    """[MS2] Dispečerská schránka je medzi príjemcami notifikácií a pravidlo toku pošty ju kopíruje agentovi (ako
    radí PREVADZKA 5). Kópia súhrnu sa nesmie stať udalosťou vo fronte ani záznamom v karanténe – inak by súhrn
    každé 2 h vznikal sám zo seba. Spozná sa podľa adresy agenta aj podľa hlavičky X-Agent-Dispecer (distribučný
    zoznam môže odosielateľa prepísať), aj bez overenia odosielateľa. E-mail ŽSR sa spracuje ďalej."""
    from vlaky.notifikacie.odosielanie import send_digest

    raw = _agent_copy(strict, session, monkeypatch, "[AGENT][INFO] Súhrn",
                      "Súhrn Agenta dispečera od 02.11. 04:00\n\nČaká na potvrdenie dispečerom: 1 udalostí.\n")
    assert b"X-Agent-Dispecer" in raw
    # kópia s overením DMARC (vlastná doména je povolená kvôli odpovediam dispečerov)
    drop(strict, "kopia1.eml", AR.format(d="dopravca.example").replace("\n", "\r\n").encode() + raw)
    # kópia bez overenia (inak by išla do karantény a súhrn by ju hlásil)
    drop(strict, "kopia2.eml", b"Authentication-Results: mx.dopravca.example; spf=none; dkim=none; dmarc=none\r\n"
         + raw.replace(b"Message-ID: <", b"Message-ID: <b-"))
    # distribučný zoznam prepísal odosielateľa, hlavička agenta ostala
    drop(strict, "kopia3.eml", AR.format(d="dopravca.example").replace("\n", "\r\n").encode()
         + raw.replace(f"From: {AGENT}".encode(), f"From: Dispecing <{SKUPINA}>".encode())
              .replace(b"Message-ID: <", b"Message-ID: <c-"))
    s = process_mailbox(session, now=fixed_now)
    session.commit()
    assert s["fetched"] == 0 and s["quarantined"] == 0 and s["events"] == 0, s
    assert session.query(Email).count() == 0 and session.query(ExtractedEvent).count() == 0
    skipped = list(session.scalars(select(AuditLog).where(AuditLog.action == "email_vlastny_preskoceny")))
    assert len(skipped) == 3
    assert send_digest(session, fixed_now - timedelta(hours=2)) == []          # súhrn sa sám neudržiava
    # bežné e-maily ďalej: ŽSR a odpoveď dispečera
    drop(strict, "zsr.eml", mail("rov-9001", "ROV c. 9001/2026", "Rozkaz o výluke č. 9001/2026: výluka 1. TK v úseku "
                                 "Poprad-Tatry – Liptovský Mikuláš dňa 20.11.2026 od 08:00 do 14:00.\n"))
    s = process_mailbox(session, now=fixed_now)
    assert s["fetched"] == 1 and [e.kind for e in events(session)] == ["vyluka_nova"]


# ====================================================================== MS3 odpovede dispečerov
def _reply(cfg, name: str, first_line: str, rec_id: int, sender: str = "dispecer1@dopravca.example") -> None:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = AGENT
    msg["Subject"] = "RE: [AGENT][KRITICKÉ] Vlak 95801 Žilina → Košice"
    msg["Message-ID"] = f"<{name}@dopravca.example>"
    msg.set_content(f"{first_line}\n\n-----Pôvodná správa-----\n> Detail a potvrdenie: "
                    f"http://localhost:8000/odporucania/{rec_id}\n")
    (Path(cfg.mail_dir) / f"{name}.eml").write_bytes(bytes(msg))


def _answers(session, recipient: str = "dispecer1@dopravca.example") -> list[Notification]:
    return list(session.scalars(select(Notification).where(Notification.kind == "odpoved",
                                                           Notification.recipient == recipient)
                                .order_by(Notification.id)))


def test_ms3_odpoved_na_zastarane_odporucanie_a_presne_slovo(cfg, session, fixed_now):
    """[MS3] Odpoveď „PRIJAŤ“ na e-mail odporúčania, ktoré medzitým nahradilo novšie (zastarané), sa nezapíše –
    rovnako ako vo webe – a dispečer dostane vysvetlenie s odkazom na aktuálne odporúčanie. Príkaz je len celé prvé
    slovo: „Prijateľnejšie by bolo…“ nie je PRIJAŤ (KRITICKÉ odporúčanie nesmie potichu zmiznúť z upozornení)."""
    t = Train(number="95801", origin="Žilina", destination="Košice", status="planovany")
    session.add(t)
    session.flush()
    old = Recommendation(train_id=t.id, decision="VYPRAVIŤ", color="ZELENÁ", priority="DÔLEŽITÉ",
                         created_at=fixed_now - timedelta(minutes=30), state="zastarane")
    cur = Recommendation(train_id=t.id, decision="ESKALOVAŤ", color="ČERVENÁ", priority="KRITICKÉ",
                         created_at=fixed_now - timedelta(minutes=5), state="nove")
    session.add_all([old, cur])
    session.commit()
    _reply(cfg, "r1", "PRIJAŤ", old.id)
    out = process_mailbox(session)
    session.commit()
    session.refresh(old)
    session.refresh(cur)
    assert out.get("replies") == 1 and (old.state, cur.state) == ("zastarane", "nove") and not old.decided_by
    reply = session.scalar(select(Email).where(Email.message_id == "<r1@dopravca.example>"))
    assert "zastarané" in reply.error
    (ans,) = _answers(session)
    assert f"/odporucania/{cur.id}" in ans.body and "zastarané" in ans.body and ans.channel == "email"
    assert not _answers(session, "dispecer2@dopravca.example")              # len tomu, kto odpovedal
    # veta, ktorá sa len začína ako príkaz
    _reply(cfg, "r2", "Prijateľnejšie by bolo počkať do zajtra, prosím prepočítajte so ŽSR.", cur.id)
    process_mailbox(session)
    session.commit()
    session.refresh(cur)
    assert cur.state == "nove"
    r2 = session.scalar(select(Email).where(Email.message_id == "<r2@dopravca.example>"))
    assert "bez platného príkazu" in r2.error
    assert "nezačína príkazom" in _answers(session)[-1].body
    # ďalšia odpoveď bez príkazu v krátkom čase – vysvetlenie sa neopakuje (automatické odpovede, slučka)
    _reply(cfg, "r3", "Prijatie potvrdím neskôr.", cur.id)
    process_mailbox(session)
    session.commit()
    assert [n.status for n in _answers(session)][-1] == "potlacena"
    # celé slovo s interpunkciou platí
    _reply(cfg, "r4", "Prijať.", cur.id)
    process_mailbox(session)
    session.commit()
    session.refresh(cur)
    assert cur.state == "prijate" and cur.decided_by == "dispecer1@dopravca.example"
    # odporúčanie, ktoré už nie je posledné (novšie čaká na rozhodnutie), sa e-mailom nemení
    newer = Recommendation(train_id=t.id, decision="ODSTAVIŤ", color="ORANŽOVÁ", priority="DÔLEŽITÉ",
                           created_at=fixed_now, state="nove")
    session.add(newer)
    session.commit()
    _reply(cfg, "r5", "ZAMIETNUŤ: nesúhlasím", cur.id)
    process_mailbox(session)
    session.commit()
    session.refresh(cur)
    assert cur.state == "prijate" and f"/odporucania/{newer.id}" in _answers(session)[-1].body
    _reply(cfg, "r6", "ZAMIETNUŤ: rušňovodič nie je k dispozícii", newer.id)
    process_mailbox(session)
    session.commit()
    session.refresh(newer)
    assert newer.state == "zamietnute" and newer.dispatcher_note == "rušňovodič nie je k dispozícii"


# ====================================================================== MR8 opakované Message-ID
def test_mr8_ina_sprava_s_rovnakym_message_id_od_toho_isteho_odosielatela(strict, session, planner):
    """[MR8] KIS (alebo poštový server) použije Message-ID pridelenia trasy znova pre neskoršie zrušenie trasy.
    Zrušenie sa nesmie potichu zahodiť – uloží sa ako samostatný e-mail (Message-ID s dodatkom) a kolízia je
    v audite. Opakované doručenie tej istej správy (aj s inou hlavičkou Received) sa ďalej preskočí; neoverená
    kópia s iným obsahom sa neuloží, ale zostane v audite."""
    body1 = ("Typ správy: Pridelenie trasy\nVlak: Nex 54321\nDátum jazdy: 14.11.2026\n\n"
             "Dopravňa Príchod Odchod\nČadca – 06:10\nŽilina 06:52 –\n")
    body2 = ("Typ správy: Zrušenie trasy\nVlak: Nex 54321\nDeň jazdy: 14.11.2026\n\n"
             "Trasa vlaku Nex 54321 na deň 14.11.2026 bola zrušená.\n")
    first = mail("kis-54321", "KIS - pridelenie trasy Nex 54321", body1, sender=KIS)
    second = mail("kis-54321", "KIS - zrusenie trasy Nex 54321 na den 14.11.2026", body2, sender=KIS,
                  date="Mon, 02 Nov 2026 07:10:00 +0100")
    drop(strict, "1.eml", first)
    process_mailbox(session)
    drop(strict, "2.eml", second)
    s = process_mailbox(session)
    session.commit()
    assert s["fetched"] == 1, s
    copy = session.scalars(select(Email).where(Email.subject.contains("zrusenie"))).one()
    assert copy.message_id.startswith("<kis-54321@zsr.example>#") and "toho istého odosielateľa" in copy.error
    assert [e.kind for e in events(session)] == ["trasa_pridelena", "trasa_zrusena"]
    log = session.scalar(select(AuditLog).where(AuditLog.action == "email_message_id_kolizia"))
    assert log is not None and log.detail["rovnaky_odosielatel"] is True
    # tá istá správa znova (rovnaké bajty) a znova inou cestou (iná hlavička Received) – nič nové
    drop(strict, "2b.eml", second)
    drop(strict, "1b.eml", b"Received: from relay2.zsr.example by mx.dopravca.example; Mon, 02 Nov 2026 "
                           b"05:11:00 +0100\r\n" + first)
    assert process_mailbox(session)["fetched"] == 0 and session.query(Email).count() == 2
    # neoverená kópia s iným obsahom (podvrh) – neuloží sa, ale nezmizne potichu
    fake = mail("kis-54321", "KIS - zrusenie trasy Nex 54321 na den 15.11.2026", body2.replace("14.11", "15.11"),
                sender=KIS, auth="Authentication-Results: mx.dopravca.example; spf=fail; dkim=fail; dmarc=fail\n")
    drop(strict, "3.eml", fake)
    assert process_mailbox(session)["fetched"] == 0 and session.query(Email).count() == 2
    logs = list(session.scalars(select(AuditLog).where(AuditLog.action == "email_message_id_kolizia")))
    assert any(x.detail.get("ulozeny") is False for x in logs)


# ====================================================================== MR7 zmena po zrušení výluky
def test_mr7_starsia_zmena_po_potvrdenom_zruseni_vyluku_neobnovi(strict, session, planner, client):
    """[MR7] Zmena ROV 1142/2026 na 14.11. a potom zrušenie toho istého ROV čakajú na potvrdenie. Dispečer potvrdí
    najprv zrušenie: dočasný záznam zmeny (14.11.) sa odstráni, plánovač s ním už nepočíta. Potvrdenie staršej
    zmeny výluku znova nezaloží (novšie zrušenie má prednosť) a fronta povie, že sa nič nezapísalo."""
    cid = registered_1142(session)
    drop(strict, "a.eml", mail("zmena1142", "Zmena ROV c. 1142/2026", ZMENA_1142))
    process_mailbox(session)
    drop(strict, "b.eml", mail("zrus1142", "Zrusenie ROV c. 1142/2026", ZRUSENIE_1142,
                               date="Mon, 02 Nov 2026 06:10:00 +0100"))
    process_mailbox(session)
    session.commit()
    change, cancel = events(session)
    assert (change.kind, cancel.kind) == ("vyluka_zmena", "vyluka_zrusenie")
    temp = session.scalars(select(Closure).where(Closure.source_event_id == change.id)).one()
    assert temp.proposed_change_of == cid and temp.id in window_ids(session, utc(2026, 11, 13))
    confirm_event(session, cancel.id, "dispecer1")
    session.commit()
    session.expire_all()
    assert session.get(Closure, cid).status == "zrusene"
    assert session.scalars(select(Closure).where(Closure.status != "zrusene")).all() == []
    assert window_ids(session, utc(2026, 11, 13)) == set()                  # 14.11. je voľný
    change = session.get(ExtractedEvent, change.id)
    assert any("zrušila novšia udalosť" in w for w in change.validation["warnings"])
    login_as(client, "dispecer")
    r = client.post(f"/fronta/{change.id}/potvrdit", data={"csrf_token": csrf(client, "/fronta"), "akcia": "potvrdit"},
                    follow_redirects=True)
    assert "nič sa nezapísalo" in flash(r) and f"novšia udalosť #{cancel.id}" in flash(r)
    session.expire_all()
    assert session.scalars(select(Closure).where(Closure.status != "zrusene")).all() == []
    assert session.get(ExtractedEvent, change.id).validation["apply_result"]["action"] == "nahradene"


def test_mr7_zamietnute_zrusenie_vrati_cakajucu_zmenu_planovacu(strict, session, planner):
    """[MR7] Keď dispečer potvrdené zrušenie dodatočne zamietne, výluka sa obnoví a čakajúca zmena (14.11.) znova
    dostane dočasný záznam – plánovač počíta s oboma termínmi ako predtým."""
    cid = registered_1142(session)
    drop(strict, "a.eml", mail("zmena1142", "Zmena ROV c. 1142/2026", ZMENA_1142))
    process_mailbox(session)
    drop(strict, "b.eml", mail("zrus1142", "Zrusenie ROV c. 1142/2026", ZRUSENIE_1142,
                               date="Mon, 02 Nov 2026 06:10:00 +0100"))
    process_mailbox(session)
    change, cancel = events(session)
    confirm_event(session, cancel.id, "dispecer1")
    session.commit()
    assert session.scalars(select(Closure).where(Closure.source_event_id == change.id)).all() == []
    reject_event(session, cancel.id, "veduci", "zrušenie neplatí")
    session.commit()
    session.expire_all()
    assert session.get(Closure, cid).status == "overene"
    temp = session.scalars(select(Closure).where(Closure.source_event_id == change.id)).one()
    assert temp.proposed_change_of == cid and temp.status == "navrh"
    assert {cid, temp.id} <= window_ids(session, utc(2026, 11, 11))
    change = session.get(ExtractedEvent, change.id)
    assert not any("zrušila novšia udalosť" in w for w in change.validation.get("warnings") or [])


# ====================================================================== MR5 zastaraný formulár výluky
def test_mr5_zastaraly_formular_nevrati_potvrdenu_zmenu(strict, session, planner, client):
    """[MR5] Dispečer B otvorí úpravu overenej výluky ROV 1142/2026 (12.11.). Dispečer A medzitým potvrdí zmenu ŽSR
    na 14.11. B uloží starý formulár (len doplnil poznámku): uloženie sa odmietne (409), formulár ukáže aktuálne
    údaje a neuložené zadanie – potvrdená zmena sa potichu nevráti. Po načítaní aktuálneho formulára sa uloží."""
    cid = registered_1142(session)
    drop(strict, "zmena.eml", mail("zmena1142", "Zmena ROV c. 1142/2026", ZMENA_1142))
    process_mailbox(session)
    (ev,) = events(session)
    session.commit()
    login_as(client, "dispecer")
    form = form_of(client.get(f"/vyluky/{cid}/upravit").text)
    assert form["start_utc"] == "2026-11-12T08:00"
    form["note"] = (form["note"] + "\nKontakt na stavbyvedúceho doplnený.").strip()
    r = client.post(f"/fronta/{ev.id}/potvrdit", data={"csrf_token": csrf(client, "/fronta"), "akcia": "potvrdit"},
                    follow_redirects=True)
    assert "Udalosť potvrdená" in flash(r)
    r = client.post(f"/vyluky/{cid}/upravit", data={**form, "csrf_token": csrf(client, "/vyluky")},
                    follow_redirects=False)
    assert r.status_code == 409 and "medzitým zmenil" in flash(r)
    assert 'value="2026-11-14T08:00"' in r.text and "Kontakt na stavbyvedúceho" in r.text
    session.expire_all()
    c = session.get(Closure, cid)
    assert as_utc(c.start_utc) == utc(2026, 11, 14, 7) and "Kontakt" not in (c.note or "")
    # aktuálny formulár sa uloží
    form = form_of(client.get(f"/vyluky/{cid}/upravit").text)
    form["note"] = (form["note"] + "\nKontakt na stavbyvedúceho doplnený.").strip()
    r = client.post(f"/vyluky/{cid}/upravit", data={**form, "csrf_token": csrf(client, "/vyluky")},
                    follow_redirects=True)
    assert "Výluka uložená." in flash(r)
    session.expire_all()
    c = session.get(Closure, cid)
    assert as_utc(c.start_utc) == utc(2026, 11, 14, 7) and "Kontakt na stavbyvedúceho" in c.note


def test_mr5_potvrdenie_navrhu_zneplatni_formular_ale_posun_do_odvolania_nie(strict, session, planner, client,
                                                                            fixed_now):
    """[MR5] Potvrdenie výluky „navrh“ z e-mailu mení len stav (verzia ostáva 1) – zastaraný formulár so stavom
    „návrh“ by overenú výluku potichu vrátil do návrhu; odmietne sa. Posun technického konca výluky „do odvolania“
    agentom formulár nezneplatní (uloží sa ako doteraz – koniec ošetrí zobrazený koniec)."""
    drop(strict, "rov.eml", mail("rov-9100", "ROV c. 9100/2026", "Rozkaz o výluke č. 9100/2026: výluka 1. TK v úseku "
                                 "Poprad-Tatry – Liptovský Mikuláš dňa 03.11.2026 od 08:00 do 14:00.\n"))
    process_mailbox(session)
    (ev,) = events(session)
    session.commit()
    row = session.scalars(select(Closure).where(Closure.source_event_id == ev.id)).one()
    assert row.status == "navrh"
    login_as(client, "dispecer")
    form = form_of(client.get(f"/vyluky/{row.id}/upravit").text)
    assert form["status"] == "navrh"
    client.post(f"/fronta/{ev.id}/potvrdit", data={"csrf_token": csrf(client, "/fronta"), "akcia": "potvrdit"})
    session.expire_all()
    assert session.get(Closure, row.id).status == "overene"
    form["track"] = "2"
    r = client.post(f"/vyluky/{row.id}/upravit", data={**form, "csrf_token": csrf(client, "/vyluky")},
                    follow_redirects=False)
    assert r.status_code == 409
    session.expire_all()
    assert (session.get(Closure, row.id).status, session.get(Closure, row.id).track) == ("overene", "1")
    # výluka do odvolania: agent posunie technický koniec, otvorený formulár sa aj tak uloží
    c = Closure(kind="kolaj", from_station="Poprad-Tatry", to_station="Liptovský Mikuláš", track="1",
                start_utc=fixed_now - timedelta(days=20), end_utc=fixed_now + timedelta(days=6), status="overene",
                source="manual", until_revoked=True, external_ref="POTR 5/2026")
    session.add(c)
    session.commit()
    form = form_of(client.get(f"/vyluky/{c.id}/upravit").text)
    form["track"] = "2"
    process_mailbox(session, now=fixed_now)
    session.expire_all()
    extended = as_utc(session.get(Closure, c.id).end_utc)
    assert extended > fixed_now + timedelta(days=29)
    r = client.post(f"/vyluky/{c.id}/upravit", data={**form, "csrf_token": csrf(client, "/vyluky")},
                    follow_redirects=True)
    assert "Výluka uložená." in flash(r)
    session.expire_all()
    c = session.get(Closure, c.id)
    assert (c.track, c.until_revoked, as_utc(c.end_utc)) == ("2", True, extended)


# ====================================================================== UI2 avízo a vlak bez dátumu jazdy
def test_ui2_avizo_po_zalozeni_vlaku_bez_datumu_jazdy(strict, session, planner, client):
    """[UI2] Avízo pre vlak Nex 47250, ktorý ešte nie je v systéme: potvrdenie povie, že vlak treba založiť (aj
    s dátumom jazdy). Dispečer ho založí predvoleným formulárom (prázdny „Dátum jazdy“) a avízo potvrdí znova –
    priradí sa jedinému aktívnemu vlaku 47250 bez dátumu jazdy a preberanie vznikne."""
    shutil.copy(SAMPLES / "10_avizo_cadca_cz.eml", Path(strict.mail_dir) / "10.eml")
    process_mailbox(session)
    (ev,) = events(session, "avizo")
    ev_id = ev.id
    session.commit()
    login_as(client, "dispecer")
    r = client.post(f"/fronta/{ev_id}/potvrdit", data={"csrf_token": csrf(client, "/fronta"), "akcia": "potvrdit"},
                    follow_redirects=True)
    assert "nenájdený" in flash(r)
    r = client.post("/vlaky/novy", data={"csrf_token": csrf(client, "/vlaky/novy"), "number": "47250", "run_date": "",
                                         "origin": "Čadca", "destination": "Košice", "gauge": "1435",
                                         "traction": "E", "vmax_kmh": "100", "status": "planovany"},
                    follow_redirects=True)
    assert "Vlak 47250 uložený." in flash(r)
    session.expire_all()
    train = session.scalars(select(Train).where(Train.number == "47250")).one()
    assert train.run_date == ""
    from vlaky.posta.validacia import find_train

    # trasy sa vlaku bez dátumu jazdy takto nepriraďujú (patrí mu trasa najbližšej jazdy), avízo áno
    assert find_train(session, "47250", "2026-11-03") is None
    r = client.post(f"/fronta/{ev_id}/potvrdit", data={"csrf_token": csrf(client, "/fronta"), "akcia": "potvrdit"},
                    follow_redirects=True)
    assert "Udalosť potvrdená" in flash(r)
    session.expire_all()
    h = session.scalars(select(Handover)).one()
    assert h.train_id == train.id and session.get(ExtractedEvent, ev_id).status == "aplikovane"


def test_ui2_avizo_nejednoznacny_alebo_iny_datum_jazdy(strict, session, planner):
    """[UI2] Dva aktívne vlaky 47250 bez dátumu jazdy sú nejednoznačné (avízo sa nepriradí) a hláška to povie;
    pri vlaku mimo systému hláška poradí založiť ho aj s dátumom jazdy z avíza; vlak 47250 s iným dátumom jazdy
    nie je „nie je v systéme“ – hláška pomenuje nesediaci dátum."""
    from vlaky.posta.validacia import find_train

    shutil.copy(SAMPLES / "10_avizo_cadca_cz.eml", Path(strict.mail_dir) / "10.eml")
    process_mailbox(session)
    (ev,) = events(session, "avizo")
    session.add_all([Train(number="47250", run_date="", origin="Čadca", destination="Košice", status="planovany"),
                     Train(number="Nex 47250", run_date="", origin="Čadca", destination="Žilina",
                           status="planovany")])
    session.commit()
    assert find_train(session, "47250", "2026-11-03", avizo=True) is None
    confirm_event(session, ev.id, "dispecer1")
    assert "bez dátumu jazdy" in ev.validation["apply_result"]["error"]
    for t in list(session.scalars(select(Train))):
        session.delete(t)
    session.commit()
    confirm_event(session, ev.id, "dispecer1")                  # vlak nie je v systéme – rada aj s dátumom jazdy
    assert "nenájdený" in ev.validation["apply_result"]["error"]
    assert "dátumom jazdy 03.11.2026" in ev.validation["apply_result"]["error"]
    session.add_all([Train(number="47250", run_date="", origin="Čadca", destination="Košice", status="planovany"),
                     Train(number="Nex 47250", run_date="", origin="Čadca", destination="Žilina",
                           status="planovany")])
    session.commit()
    for t in session.scalars(select(Train)):
        t.run_date = "2026-11-10"
    session.commit()
    confirm_event(session, ev.id, "dispecer1")
    err = ev.validation["apply_result"]["error"]
    assert "nenájdený" in err and "iným dátumom jazdy (10.11.2026)" in err
    assert session.query(Handover).count() == 0


# ====================================================================== MR2 čakajúce udalosti mimo plánovača
def _route(origin: str, destination: str):
    """Trasa vlaku tak, ako ju plánovač odovzdá do pending_uncertainty (vlaky.siet.graf.Route)."""
    from vlaky.siet.graf import TrainSpec, network

    return network().route(origin, destination, TrainSpec(traction="E", gauge="1435", vmax_kmh=100))


def _alerts(session) -> list[Notification]:
    return list(session.scalars(select(Notification).where(Notification.kind == "upozornenie",
                                                           Notification.subject.contains("Na potvrdenie čaká"))
                                .order_by(Notification.id)))


def test_mr2_cakajuce_zrusenie_trasy_nie_je_neviditelne(strict, session, fixed_now):
    """[MR2] KIS zruší pridelenú trasu vlaku 12345 na dnes. Zrušenie trasy vždy potvrdzuje dispečer, trasa zatiaľ
    ostáva „pridelená“. Udalosť nesmie byť pre plánovač neviditeľná: prepočet beží v tom istom cykle, dispečeri
    dostanú hneď upozornenie (nie až súhrn o 2 h) a plánovač dostane dôvod, prečo vlak nesmie byť ZELENÝ
    (pending_uncertainty) – po zamietnutí zrušenia dôvod zmizne."""
    from vlaky.planovanie.stav import allocated_paths

    dep = fixed_now + timedelta(hours=3)
    t = Train(number="12345", origin="Čadca", destination="Žilina", traction="E", gauge="1435", vmax_kmh=90,
              status="planovany", run_date="2026-11-02", ready_utc=dep - timedelta(minutes=30),
              deadline_utc=dep + timedelta(hours=5))
    other = Train(number="12346", origin="Čadca", destination="Žilina", traction="E", gauge="1435", vmax_kmh=90,
                  status="planovany", run_date="2026-11-02", ready_utc=dep, deadline_utc=dep + timedelta(hours=5))
    session.add_all([t, other])
    session.flush()
    session.add_all([TrainPath(train_id=t.id, train_number="12345", kind="ad_hoc", status="pridelena",
                               departure_utc=dep, points=[]),
                     TrainPath(train_id=other.id, train_number="12346", kind="ad_hoc", status="pridelena",
                               departure_utc=dep, points=[])])
    session.commit()
    drop(strict, "kis.eml", mail("kis-445120", "KIS - zrusenie trasy vlaku Pn 12345 na den 02.11.2026",
                                 "Systém: KIS\nTyp správy: Zrušenie trasy\nID trasy: KIS-2026-445120\nVlak: Pn 12345\n"
                                 "Deň jazdy: 02.11.2026\nDôvod zrušenia: na žiadosť dopravcu\n\n"
                                 "Trasa vlaku Pn 12345 na deň 02.11.2026 bola zrušená.\n", sender=KIS))
    s = process_mailbox(session, now=fixed_now)
    session.commit()
    (ev,) = events(session)
    assert ev.kind == "trasa_zrusena" and ev.status == "navrh"
    assert s["planning"] and s["planning"]["ok"], s                        # prepočet v tom istom cykle
    alerts = _alerts(session)
    assert {n.recipient for n in alerts} == {"dispecer1@dopravca.example", "dispecer2@dopravca.example"}
    assert all("zrušenie trasy" in n.subject and f"/fronta#udalost-{ev.id}" in n.body for n in alerts)
    from vlaky.posta.spracovanie import pending_uncertainty

    route = _route("Čadca", "Žilina")
    notes = pending_uncertainty(session, t, route, allocated_paths(session, t, None, fixed_now))
    assert len(notes) == 1 and "Zrušenie trasy vlaku 12345" in notes[0] and f"#{ev.id}" in notes[0]
    assert "ZELENÝ" in notes[0]
    assert pending_uncertainty(session, other, route, allocated_paths(session, other, None, fixed_now)) == []
    # ďalší cyklus bez novej pošty upozornenie nezopakuje
    process_mailbox(session, now=fixed_now + timedelta(minutes=5))
    assert len(_alerts(session)) == 2
    reject_event(session, ev.id, "dispecer1", "trasa platí")
    session.commit()
    assert pending_uncertainty(session, t, route, allocated_paths(session, t, None, fixed_now)) == []


def test_mr2_vyluka_s_neprecitanym_terminom_nie_je_neviditelna(strict, session):
    """[MR2] ŽSR oznámi úplnú výluku úseku Poprad-Tatry – Liptovský Mikuláš s termínom, ktorý pravidlá neprečítajú
    („od 12.11.2026 (štvrtok) 8:00 do …“) – udalosť nemá záznam pre plánovač. Vlak cez tento úsek dostane dôvod,
    prečo nesmie byť ZELENÝ, prepočet beží hneď a dispečeri dostanú upozornenie; vlak na inej trati nie. Čakajúca
    výluka so záznamom „navrh“ upozornenie nevyvolá (plánovač s ňou počíta). Po potvrdení s doplneným termínom má
    výluka záznam a dôvod zmizne."""
    from vlaky.cas import set_fake_now

    now = utc(2026, 11, 12, 5)
    set_fake_now(now)
    dep = utc(2026, 11, 12, 9)
    a = Train(number="77502", origin="Poprad-Tatry", destination="Liptovský Mikuláš", traction="E", gauge="1435",
              vmax_kmh=80, status="planovany", ready_utc=dep, deadline_utc=dep + timedelta(hours=6))
    b = Train(number="77503", origin="Čadca", destination="Žilina", traction="E", gauge="1435", vmax_kmh=80,
              status="planovany", ready_utc=dep, deadline_utc=dep + timedelta(hours=6))
    session.add_all([a, b])
    session.commit()
    drop(strict, "rov.eml", mail("rov2700", "ROV c. 2700/2026 Poprad-Tatry - Liptovsky Mikulas",
                                 "Rozkaz o výluke č. 2700/2026\nÚplná výluka oboch traťových koľají v úseku "
                                 "Poprad-Tatry – Liptovský Mikuláš od 12.11.2026 (štvrtok) 8:00 do 13.11.2026 "
                                 "(piatok) 14:00.\n", date="Thu, 12 Nov 2026 06:00:00 +0100"))
    drop(strict, "rov2.eml", mail("rov2701", "ROV c. 2701/2026", "Rozkaz o výluke č. 2701/2026: výluka 1. TK v úseku "
                                  "Čadca – Žilina dňa 13.11.2026 od 08:00 do 14:00.\n",
                                  date="Thu, 12 Nov 2026 06:00:00 +0100"))
    s = process_mailbox(session, now=now)
    session.commit()
    ev, ok_ev = events(session)
    assert ev.data.get("start") is None and ev.validation["pending_closure"]["id"] is None
    assert ok_ev.validation["pending_closure"]["id"]                        # výluka so záznamom „navrh“
    assert s["planning"] and s["planning"]["ok"], s
    alerts = _alerts(session)
    assert len(alerts) == 2 and all(f"/fronta#udalost-{ev.id}" in n.body for n in alerts)
    assert not any(f"/fronta#udalost-{ok_ev.id}" in n.body for n in alerts)
    from vlaky.posta.spracovanie import pending_uncertainty

    route_a = _route("Poprad-Tatry", "Liptovský Mikuláš")
    notes = pending_uncertainty(session, a, route_a, window=(now, now + timedelta(hours=30)))
    assert len(notes) == 1 and "Poprad-Tatry – Liptovský Mikuláš" in notes[0] and "nevie počítať" in notes[0]
    assert "chýba začiatok" in notes[0] and f"#{ev.id}" in notes[0]
    assert pending_uncertainty(session, b, _route("Čadca", "Žilina")) == []
    confirm_event(session, ev.id, "dispecer1", edits={"start": "2026-11-12T07:00:00+00:00",
                                                      "end": "2026-11-13T13:00:00+00:00"})
    session.commit()
    assert session.scalars(select(Closure).where(Closure.source_event_id == ev.id)).one().status == "overene"
    assert pending_uncertainty(session, a, route_a) == []


def test_mr2_planovac_s_napojenim_neda_zelenu(strict, session, fixed_now):
    """[MR2] Integrácia s plánovačom: keď plánovač volá posta.spracovanie.pending_uncertainty (samostatná zmena
    v evaluate_train), vlak s čakajúcim zrušením pridelenej trasy je ORANŽOVÝ a odporúčanie povie prečo; po
    zamietnutí zrušenia je znova ZELENÝ. Kým napojenie v plánovači nie je, test sa preskočí."""
    import inspect

    from vlaky.planovanie import planovac

    if "pending_uncertainty" not in inspect.getsource(planovac):
        pytest.skip("plánovač ešte nevolá posta.spracovanie.pending_uncertainty (zmena v planovac.evaluate_train)")
    dep = fixed_now + timedelta(hours=3)
    t = Train(number="12345", origin="Čadca", destination="Žilina", traction="E", gauge="1435", vmax_kmh=90,
              status="planovany", run_date="2026-11-02", ready_utc=dep - timedelta(minutes=30),
              deadline_utc=dep + timedelta(hours=5))
    session.add(t)
    session.flush()
    session.add(TrainPath(train_id=t.id, train_number="12345", kind="ad_hoc", status="pridelena", departure_utc=dep,
                          points=[]))
    session.commit()
    before = planovac.evaluate_train(session, t.id, fixed_now, "pred e-mailom")
    session.commit()
    assert before.color == "ZELENÁ"
    drop(strict, "kis.eml", mail("kis-445120", "KIS - zrusenie trasy vlaku Pn 12345 na den 02.11.2026",
                                 "Typ správy: Zrušenie trasy\nVlak: Pn 12345\nDeň jazdy: 02.11.2026\n\n"
                                 "Trasa vlaku Pn 12345 na deň 02.11.2026 bola zrušená.\n", sender=KIS))
    process_mailbox(session, now=fixed_now)
    session.commit()
    (ev,) = events(session)
    rec = planovac.evaluate_train(session, t.id, fixed_now + timedelta(minutes=1), "po e-maile")
    session.commit()
    assert rec.color == "ORANŽOVÁ" and any("Zrušenie trasy vlaku 12345" in r for r in rec.reasons)
    reject_event(session, ev.id, "dispecer1", "trasa platí")
    session.commit()
    rec = planovac.evaluate_train(session, t.id, fixed_now + timedelta(minutes=2), "po zamietnutí")
    assert rec.color == "ZELENÁ" and not any("Zrušenie trasy" in r for r in rec.reasons)
