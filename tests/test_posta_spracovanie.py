"""Spracovanie pošty: príjem, idempotencia, karanténa, zápis do registrov, potvrdenie a zamietnutie."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from vlaky.hranice import seed_borders
from vlaky.models import AuditLog, BorderCrossing, Closure, Email, ExtractedEvent, Handover, Order, Train
from vlaky.models import Path as TrainPath
from vlaky.posta import spracovanie
from vlaky.posta.spracovanie import (apply_event, confirm_event, fetch_and_store, process_email, process_mailbox,
                                     reject_event)

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "ukazkove_emaily"


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
    """Prísne overenie odosielateľa ako v prevádzke a prah 0,8 (pravidlá dávajú najviac 0,85)."""
    monkeypatch.setattr(cfg, "require_dmarc_pass", True)
    monkeypatch.setattr(cfg, "trusted_authserv_ids", "mx.dopravca.example")
    monkeypatch.setattr(cfg, "confirm_threshold", 0.8)
    seed_borders(session)
    session.commit()
    return cfg


def drop(cfg, *names: str) -> None:
    for n in names:
        shutil.copy(SAMPLES / n, Path(cfg.mail_dir) / n)


def events_of(session, email_id: int) -> list[ExtractedEvent]:
    return list(session.scalars(select(ExtractedEvent).where(ExtractedEvent.email_id == email_id)
                                .order_by(ExtractedEvent.id)))


# ---------------------------------------------------------------------- príjem
def test_same_mail_twice_is_stored_once(env, session):
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    (email,) = fetch_and_store(session)
    assert email.verdict == "ok" and email.status == "prijaty"
    assert email.raw_path.endswith("spracovane/01_rov_vyluka_kolaje_poprad_lm.eml") and Path(email.raw_path).exists()
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    assert fetch_and_store(session) == []
    assert session.query(Email).count() == 1
    done = sorted(p.name for p in (Path(env.mail_dir) / "spracovane").iterdir())
    assert done == ["01_rov_vyluka_kolaje_poprad_lm.eml", "01_rov_vyluka_kolaje_poprad_lm_1.eml"]
    assert list(Path(env.mail_dir).glob("*.eml")) == []
    assert session.scalar(select(AuditLog).where(AuditLog.action == "email_prijaty")) is not None


def test_quarantined_mail_is_stored_but_not_extracted(env, session):
    drop(env, "15_nepovoleny_odosielatel.eml")
    (email,) = fetch_and_store(session)
    assert email.verdict == "karantena" and email.status == "spracovany"
    assert "nie je na zozname povolených" in email.verdict_reason
    assert process_email(session, email) == []
    summary = process_mailbox(session)
    assert summary["fetched"] == 0 and summary["events"] == 0
    assert session.query(ExtractedEvent).count() == 0


def test_quarantine_does_not_parse_attachments(env, session, monkeypatch):
    raw = (SAMPLES / "12_srov_tabulka_xlsx.eml").read_bytes().replace(b"vyluky.ortt@zsr.example",
                                                                         b"vyluky.ortt@utocnik.example")
    (Path(env.mail_dir) / "cudzi.eml").write_bytes(raw)
    calls = []
    monkeypatch.setattr("vlaky.posta.prilohy.convert", lambda *a, **k: calls.append(a) or ("", "", ""))
    (email,) = fetch_and_store(session)
    assert email.verdict == "karantena" and calls == []
    assert email.attachments[0].text == "" and email.attachments[0].size > 1000


# ---------------------------------------------------------------------- výluky
def test_closure_auto_apply_confirm_change_and_reject(env, session):
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    s1 = process_mailbox(session)
    assert s1["auto_applied"] == 1, s1
    (c,) = session.query(Closure).all()
    assert (c.status, c.kind, c.from_station, c.to_station, c.track, c.line_ref) == \
        ("navrh", "kolaj", "Poprad-Tatry", "Liptovský Mikuláš", "1", "180")
    assert c.start_utc.replace(tzinfo=timezone.utc) == datetime(2026, 11, 12, 7, 0, tzinfo=timezone.utc)
    assert c.external_ref == "1142/2026" and c.source == "email" and not c.electric_only
    (ev,) = session.query(ExtractedEvent).all()
    assert ev.status == "aplikovane" and c.source_event_id == ev.id
    # dispečer neskôr potvrdí → výluka je overená
    confirm_event(session, ev.id, "dispecer1")
    assert c.status == "overene" and c.version == 1 and ev.confirmed_by == "dispecer1"
    # zmena výluky (preposlaný e-mail) – nájde sa podľa čísla ROV; preposlaný obsah a zmena overenej výluky
    # sa nezapíšu bez dispečera: pôvodná výluka ostane, nový termín je dočasný záznam „navrh“
    drop(env, "04_zmena_vyluky_preposlana.eml")
    s2 = process_mailbox(session)
    assert s2["auto_applied"] == 0 and s2["waiting_confirmation"] == 1, s2
    session.refresh(c)
    assert c.version == 1 and c.status == "overene"
    assert c.end_utc.replace(tzinfo=timezone.utc) == datetime(2026, 11, 12, 13, 0, tzinfo=timezone.utc)
    change = session.scalars(select(ExtractedEvent).where(ExtractedEvent.kind == "vyluka_zmena")).one()
    reasons = " ".join(change.validation["confirmation_reasons"])
    assert change.needs_confirmation and "preposlaný" in reasons and "overená dispečerom" in reasons
    tmp = session.scalars(select(Closure).where(Closure.source_event_id == change.id)).one()
    assert tmp.proposed_change_of == c.id and tmp.status == "navrh"
    # dispečer zmenu potvrdí → zapíše sa do pôvodnej výluky, dočasný záznam zmizne
    confirm_event(session, change.id, "dispecer1")
    session.refresh(c)
    assert session.query(Closure).count() == 1
    assert c.version == 2 and c.status == "overene" and c.source_event_id == change.id
    assert c.start_utc.replace(tzinfo=timezone.utc) == datetime(2026, 11, 12, 8, 30, tzinfo=timezone.utc)
    assert c.end_utc.replace(tzinfo=timezone.utc) == datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
    # dispečer zmenu (dodatočne) zamietne → pôvodný (overený) stav sa obnoví
    reject_event(session, change.id, "dispecer2", "zmena neplatí")
    session.refresh(c)
    assert change.status == "zamietnute" and change.validation["rejection"]["reason"] == "zmena neplatí"
    assert c.version == 1 and c.status == "overene"
    assert c.end_utc.replace(tzinfo=timezone.utc) == datetime(2026, 11, 12, 13, 0, tzinfo=timezone.utc)
    actions = {a.action for a in session.query(AuditLog)}
    assert {"udalost_aplikovana", "udalost_potvrdena", "udalost_zamietnuta", "email_extrakcia"} <= actions


def test_cancellation_waits_for_confirmation(env, session):
    drop(env, "02_oznamenie_vyluk_tyzden_47.eml", "05_zrusenie_vyluky.eml")
    s = process_mailbox(session)
    assert s["auto_applied"] == 2 and s["waiting_confirmation"] == 1, s
    target = session.scalars(select(Closure).where(Closure.external_ref == "1150/2026")).one()
    assert target.daily_from == "08:00" and target.daily_to == "14:00" and target.status == "navrh"
    tv = session.scalars(select(Closure).where(Closure.external_ref == "1151/2026")).one()
    assert tv.kind == "trakcne_vedenie" and tv.electric_only
    cancel = session.scalars(select(ExtractedEvent).where(ExtractedEvent.kind == "vyluka_zrusenie")).one()
    assert cancel.status == "navrh" and cancel.needs_confirmation
    assert apply_event(session, cancel)["ok"] is False             # bez potvrdenia nič
    assert target.status == "navrh"
    confirm_event(session, cancel.id, "dispecer1")
    assert cancel.status == "aplikovane" and target.status == "zrusene"


def test_mimoriadnost_and_open_ended_potr(env, session):
    drop(env, "06_mimoriadna_udalost.eml", "17_telegram_potr.eml")
    s = process_mailbox(session)
    assert s["auto_applied"] == 0 and s["waiting_confirmation"] == 2
    for ev in session.query(ExtractedEvent).all():
        confirm_event(session, ev.id, "dispecer1")
    m = session.scalars(select(Closure).where(Closure.kind == "mimoriadnost")).one()
    assert m.status == "overene" and "trakčného vedenia" in m.note
    p = session.scalars(select(Closure).where(Closure.kind == "pomala_jazda")).one()
    assert p.speed_kmh == 30 and p.external_ref == "37/2026"
    assert (p.end_utc - p.start_utc).days == spracovanie.OPEN_END_DAYS and "do odvolania" in p.note


# ---------------------------------------------------------------------- trasy
def test_path_allocation_and_change_link_train(env, session):
    session.add(Train(number="54321", run_date="2026-11-14", origin="Čadca", destination="Košice"))
    session.commit()
    drop(env, "07_pridelenie_trasy_nex54321.eml", "08_zmena_trasy_nex54321.eml")
    s = process_mailbox(session)
    assert s["auto_applied"] == 2, s
    (p,) = session.query(TrainPath).all()
    assert p.status == "zmenena" and p.train_id is not None and p.train_number == "54321"
    assert [x["station"] for x in p.points] == ["Čadca", "Žilina", "Ružomberok", "Poprad-Tatry", "Košice"]
    assert p.departure_utc.replace(tzinfo=timezone.utc) == datetime(2026, 11, 14, 6, 10, tzinfo=timezone.utc)
    assert p.kind == "ad_hoc"


def test_path_cancellation(env, session):
    session.add(TrainPath(train_number="12345", status="pridelena",
                          departure_utc=datetime(2026, 11, 15, 5, 0, tzinfo=timezone.utc), points=[]))
    session.commit()
    drop(env, "09_kis_zrusenie_trasy_pn12345.eml")
    s = process_mailbox(session)
    assert s["waiting_confirmation"] == 1
    ev = session.query(ExtractedEvent).one()
    confirm_event(session, ev.id, "dispecer1")
    assert session.query(TrainPath).one().status == "zrusena" and ev.status == "aplikovane"
    reject_event(session, ev.id, "veduci", "omyl")
    assert session.query(TrainPath).one().status == "pridelena"


def test_kis_offer_creates_path_offer(env, session):
    drop(env, "18_kis_ponuka_trasy_nex54390.eml")
    process_mailbox(session)
    p = session.query(TrainPath).one()
    assert p.status == "ponuka" and "akceptovať do 18.11. 12:00" in p.note and len(p.points) == 5


# ---------------------------------------------------------------------- avíza
def test_avizo_creates_handover_after_confirmation(env, session):
    tr = Train(number="47250", run_date="2026-11-03", origin="Čadca", destination="Košice", status="planovany")
    session.add(tr)
    session.commit()
    drop(env, "10_avizo_cadca_cz.eml")
    s = process_mailbox(session)
    assert s["waiting_confirmation"] == 1 and s["auto_applied"] == 0       # ETA do 72 h → potvrdenie
    ev = session.query(ExtractedEvent).one()
    assert any("72 h" in r for r in ev.validation["confirmation_reasons"])
    confirm_event(session, ev.id, "dispecer1")
    h = session.query(Handover).one()
    bc = session.get(BorderCrossing, h.border_id)
    assert bc.code == "CADCA" and h.direction == "vstup" and h.status == "ocakavany"
    assert h.avizo_eta_utc.replace(tzinfo=timezone.utc) == datetime(2026, 11, 3, 12, 35, tzinfo=timezone.utc)
    assert tr.status == "na_ceste_k_hranici" and tr.entry_border_id == bc.id
    reject_event(session, ev.id, "dispecer1", "avízo bolo pre iný vlak")
    assert session.query(Handover).count() == 0 and tr.status == "planovany"


def test_avizo_for_unknown_train_creates_nothing(env, session):
    drop(env, "11_avizo_rusovce_rajka.eml")
    process_mailbox(session)
    ev = session.query(ExtractedEvent).one()
    assert any("nenájdený" in e for e in ev.validation["errors"])
    confirm_event(session, ev.id, "dispecer1")
    assert ev.status == "potvrdene" and ev.needs_confirmation
    assert ev.validation["apply_result"]["ok"] is False and "nenájdený" in ev.validation["apply_result"]["error"]
    assert session.query(Handover).count() == 0 and session.query(Train).count() == 0
    # dispečer vlak založí a avízo potvrdí znova
    session.add(Train(number="43130", run_date="2026-11-04", origin="Rusovce", destination="Bratislava východ"))
    session.flush()
    confirm_event(session, ev.id, "dispecer1")
    h = session.query(Handover).one()
    assert ev.status == "aplikovane" and session.get(BorderCrossing, h.border_id).code == "RUSOVCE"


# ---------------------------------------------------------------------- objednávky
def test_order_from_pdf(env, session):
    drop(env, "13_objednavka_pdf_cz.eml")
    s = process_mailbox(session)
    assert s["auto_applied"] == 1, s
    o = session.query(Order).one()
    assert (o.status, o.customer, o.origin, o.destination, o.wagons, o.weight_t, o.length_m, o.rid) == \
        ("nova", "FIKTIVNI Logistika s.r.o.", "Kúty", "Žilina-Teplička", 22, 1480.0, 590.0, False)
    assert o.deadline_utc.replace(tzinfo=timezone.utc) == datetime(2026, 11, 21, 13, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------- bezpečnosť
def test_prompt_injection_is_never_auto_applied(env, session, monkeypatch):
    monkeypatch.setattr(env, "confirm_threshold", 0.0)           # ani pri nulovom prahu
    drop(env, "14_prompt_injection.eml")
    s = process_mailbox(session)
    assert s["suspicious"] == 1 and s["auto_applied"] == 0 and s["waiting_confirmation"] == 1
    email = session.query(Email).one()
    assert email.verdict == "podozrivy" and "prompt injection" in email.verdict_reason
    ev = session.query(ExtractedEvent).one()
    assert ev.needs_confirmation and ev.status == "navrh" and ev.confidence <= 0.5
    ev.needs_confirmation = False                                # ani ručne zmenený príznak nepomôže
    assert apply_event(session, ev)["ok"] is False
    # plánovač s výlukou počíta len ako s nepotvrdenou: záznam „navrh“ udalosti, ktorá sa nezapísala
    (c,) = session.query(Closure).all()
    assert c.status == "navrh" and c.source_event_id == ev.id and ev.status == "navrh"
    confirm_event(session, ev.id, "dispecer1")                   # človek to overil a potvrdil
    assert session.query(Closure).one().status == "overene"


def test_confirm_with_edits_and_reclassification(env, session):
    drop(env, "16_ine_informacia.eml")
    process_mailbox(session)
    ev = session.query(ExtractedEvent).one()
    assert ev.kind == "ine" and ev.needs_confirmation
    edits = {"event_kind": "vyluka_nova", "from_station": "Kralovany", "to_station": "Ruzomberok",
             "start": "2026-11-25T07:00:00+00:00", "end": "2026-11-25T13:00:00+00:00", "kind": "kolaj"}
    confirm_event(session, ev.id, "dispecer1", edits=edits)
    assert ev.kind == "vyluka_nova" and ev.status == "aplikovane"
    c = session.query(Closure).one()
    assert (c.from_station, c.to_station, c.status) == ("Kraľovany", "Ružomberok", "overene")
    change = ev.validation["edits"][-1]
    assert change["by"] == "dispecer1" and change["changes"]["event_kind"] == {"pred": "ine", "po": "vyluka_nova"}
    assert ev.validation["original_data"] == {}                   # pôvodná extrakcia (trénovací príklad)
    with pytest.raises(ValueError):
        confirm_event(session, 99999, "dispecer1")
    reject_event(session, ev.id, "dispecer1", "omyl")
    with pytest.raises(ValueError):
        confirm_event(session, ev.id, "dispecer1")


# ---------------------------------------------------------------------- celý cyklus
def test_process_mailbox_end_to_end_over_samples(env, session, planner):
    session.add_all([Train(number="47250", run_date="2026-11-03", origin="Čadca", destination="Košice"),
                     Train(number="54321", run_date="2026-11-14", origin="Čadca", destination="Košice")])
    session.commit()
    names = sorted(p.name for p in SAMPLES.glob("*.eml"))
    drop(env, *names)
    s = process_mailbox(session)
    assert s["fetched"] == len(names) == 18 and s["quarantined"] == 1 and s["suspicious"] == 1
    assert s["events"] == 21 and s["auto_applied"] + s["waiting_confirmation"] == 21
    assert s["auto_applied"] == 12 and s["waiting_confirmation"] == 9, s
    assert s["errors"] == [] and s["planning"]["ok"] is True and s["fetch_ok"] is True
    assert len(planner) == 1 and planner[0].startswith("e-mail: ")
    waiting = {session.get(ExtractedEvent, i).kind for i in s["waiting_event_ids"]}
    assert waiting == {"vyluka_zrusenie", "mimoriadnost", "trasa_zrusena", "avizo", "vyluka_nova", "vyluka_zmena",
                       "ine", "pomala_jazda"}
    # 8 zapísaných výluk + 4 záznamy „navrh“ čakajúcich udalostí (mimoriadnosť, POTR do odvolania, výluka
    # z podozrivého e-mailu, dočasný záznam zmeny z preposlaného e-mailu) – plánovač počíta aj s nimi
    assert session.query(Closure).count() == 12 and session.query(Order).count() == 1
    assert session.query(Closure).filter(Closure.proposed_change_of.is_not(None)).count() == 1
    assert {p.status for p in session.query(TrainPath)} == {"zmenena", "ponuka"}
    assert all(e.status in ("spracovany",) for e in session.query(Email))
    assert len(list((Path(env.mail_dir) / "spracovane").glob("*.eml"))) == 18
    # druhý cyklus – nič nové, nič sa neduplikuje
    s2 = process_mailbox(session)
    assert s2["fetched"] == 0 and s2["events"] == 0 and len(planner) == 1
    assert session.query(Email).count() == 18 and session.query(ExtractedEvent).count() == 21
    assert session.query(Closure).count() == 12


def test_planner_errors_are_reported_not_raised(env, session, monkeypatch):
    from vlaky.planovanie import planovac

    def not_ready(session, now=None, trigger=""):  # noqa: ANN001
        raise NotImplementedError

    monkeypatch.setattr(planovac, "evaluate_all", not_ready)
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    s = process_mailbox(session)
    assert s["auto_applied"] == 1 and s["planning"]["ok"] is False
    assert any("Plánovač" in e for e in s["errors"])
    assert session.query(Closure).count() == 1                    # zápis výluky zostal

    def broken(session, now=None, trigger=""):  # noqa: ANN001
        raise RuntimeError("chyba v plánovači")

    monkeypatch.setattr(planovac, "evaluate_all", broken)
    drop(env, "13_objednavka_pdf_cz.eml")
    s = process_mailbox(session)
    assert any("chyba v plánovači" in e for e in s["errors"]) and session.query(Order).count() == 1


def test_extraction_failure_marks_email_and_continues(env, session, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("neočakávaný formát")

    monkeypatch.setattr(spracovanie, "extract", boom)
    drop(env, "01_rov_vyluka_kolaje_poprad_lm.eml")
    s = process_mailbox(session)
    email = session.query(Email).one()
    assert email.status == "chyba" and "neočakávaný formát" in email.error
    assert s["events"] == 0


def test_mail_stored_before_crash_is_processed_later(env, session):
    drop(env, "13_objednavka_pdf_cz.eml")
    (email,) = fetch_and_store(session)             # „pád“ po uložení, pred extrakciou
    assert email.status == "prijaty" and events_of(session, email.id) == []
    s = process_mailbox(session)
    assert s["fetched"] == 0 and s["events"] == 1 and s["auto_applied"] == 1


def test_mail_source_error_is_reported(env, session, monkeypatch):
    monkeypatch.setattr(env, "mail_source", "imap")
    s = process_mailbox(session)
    assert s["fetched"] == 0 and any("IMAP nie je nakonfigurované" in e for e in s["errors"])


def test_describe_event(env, session):
    drop(env, "02_oznamenie_vyluk_tyzden_47.eml")
    process_mailbox(session)
    texts = [spracovanie.describe_event(e) for e in session.query(ExtractedEvent).order_by(ExtractedEvent.id)]
    assert texts[0].startswith("nová výluka Kraľovany – Ružomberok 16.11. 08:00 – 20.11. 14:00, denne 08:00–14:00")
