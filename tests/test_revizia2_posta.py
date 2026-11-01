"""Druhá revízia spracovania pošty a potvrdzovania dispečerom: regresné testy k nálezom [12] – [19], [39], [41]
a [43] (oblasť „posta“).

Každý test zodpovedá jednému nálezu (scenár je opísaný v docstringu testu). Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import String, select
from sqlalchemy import event as sa_event
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

from test_web_spolocne import csrf, login_as
from vlaky.hranice import seed_borders
from vlaky.models import AuditLog, Closure, Email, ExtractedEvent, Handover, Notification, Path as TrainPath, \
    Recommendation, Train
from vlaky.posta.spracovanie import confirm_event, process_mailbox

pytest_plugins = ["test_web_spolocne"]

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "ukazkove_emaily"
UTC = timezone.utc
AR_ZSR = ("Authentication-Results: mx.dopravca.example; spf=pass smtp.mailfrom=zsr.example; dkim=pass\n"
          " header.d=zsr.example; dmarc=pass header.from=zsr.example\n")
AR_DOPRAVCA = ("Authentication-Results: mx.dopravca.example; spf=pass smtp.mailfrom=dopravca.example; dkim=pass\n"
               " header.d=dopravca.example; dmarc=pass header.from=dopravca.example\n")
AR_ZAKAZNIK = ("Authentication-Results: mx.dopravca.example; spf=pass smtp.mailfrom=zakaznik.example; dkim=pass\n"
               " header.d=zakaznik.example; dmarc=pass header.from=zakaznik.example\n")
SEC = "výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš"


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
def strict(cfg, session, fixed_now, monkeypatch):
    """Prísne overenie odosielateľa ako v prevádzke (DMARC, dôveryhodný server); prah istoty podľa nastavení."""
    monkeypatch.setattr(cfg, "require_dmarc_pass", True)
    monkeypatch.setattr(cfg, "trusted_authserv_ids", "mx.dopravca.example")
    seed_borders(session)
    session.commit()
    return cfg


@pytest.fixture()
def env(strict, planner):
    """Ako ``strict``, s podvrhnutým plánovačom."""
    return strict


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


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def events(session, kind: str | None = None) -> list[ExtractedEvent]:
    q = select(ExtractedEvent).order_by(ExtractedEvent.id)
    if kind:
        q = q.where(ExtractedEvent.kind == kind)
    return list(session.scalars(q))


def active(session) -> list[Closure]:
    return list(session.scalars(select(Closure).where(Closure.status != "zrusene").order_by(Closure.id)))


def registered(session, start: datetime, ref: str, kind: str = "kolaj", hours: int = 6) -> Closure:
    c = Closure(kind=kind, from_station="Poprad-Tatry", to_station="Liptovský Mikuláš", track="1", start_utc=start,
                end_utc=start + timedelta(hours=hours), status="overene", source="manual", external_ref=ref)
    session.add(c)
    session.commit()
    return c


def windows(session, start: datetime, hours: int = 24 * 40) -> list:
    from vlaky.planovanie.planovac import closure_windows

    return closure_windows(session, start, start + timedelta(hours=hours))


def flash(r) -> str:
    m = re.findall(r'class="flash[^"]*"[^>]*>(.*?)<', r.text, re.S)
    return " ".join(x.strip() for x in m)


def local_input(dt: datetime) -> str:
    from vlaky.cas import local

    return local(dt).strftime("%Y-%m-%dT%H:%M")


def register_form(c: Closure, **changes) -> dict:
    """Formulár úpravy výluky tak, ako ho pošle prehliadač (nezaškrtnuté políčko sa neposiela; skryté pole
    ``zobrazeny_koniec`` nesie koniec, ktorý formulár zobrazil)."""
    form = {"from_station": c.from_station, "to_station": c.to_station, "kind": c.kind, "line_ref": c.line_ref or "",
            "track": c.track or "", "start_utc": local_input(as_utc(c.start_utc)),
            "end_utc": local_input(as_utc(c.end_utc)), "daily_from": c.daily_from or "", "daily_to": c.daily_to or "",
            "speed_kmh": "" if c.speed_kmh is None else str(c.speed_kmh), "status": c.status,
            "external_ref": c.external_ref or "", "note": c.note or "",
            "zobrazeny_koniec": local_input(as_utc(c.end_utc))}
    if c.electric_only:
        form["electric_only"] = "1"
    form.update(changes)
    return form


CHANGE_NO_REF = ("Oznamujeme zmenu termínu výluky 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš.\n"
                 "Nový termín: od {d}.11.2026 08:00 do {d}.11.2026 14:00\n")


# ====================================================================== [12] cieľ potvrdenia s opravou
def test_r12_oprava_cisla_vyluky_pri_potvrdeni_zmeny(env, session):
    """[12a] Zmena bez čísla sa voľne priradila k 1600/2026 (20.11.); dispečer pri potvrdení doplní 1601/2026.

    Zmena sa musí zapísať do 1601/2026 (30.11. → 22.11.) – overená výluka 1600/2026 na 20.11. ostane bez zmeny."""
    x = registered(session, utc(2026, 11, 20, 7), "1600/2026")
    y = registered(session, utc(2026, 11, 30, 7), "1601/2026")
    drop_raw(env, "a.eml", mail("zmena-a", "Zmena terminu vyluky Poprad-Tatry - Liptovsky Mikulas",
                                CHANGE_NO_REF.format(d=22)))
    process_mailbox(session)
    (ev,) = events(session)
    assert ev.validation["pending_closure"]["proposed_change_of"] == x.id
    confirm_event(session, ev.id, "dispecer1", edits={"external_ref": "1601/2026"})
    session.commit()
    session.refresh(x)
    session.refresh(y)
    assert (x.external_ref, as_utc(x.start_utc), x.status) == ("1600/2026", utc(2026, 11, 20, 7), "overene")
    assert (y.external_ref, as_utc(y.start_utc), y.status) == ("1601/2026", utc(2026, 11, 22, 7), "overene")
    assert len(active(session)) == 2 and ev.validation["apply_result"]["id"] == y.id


def test_r12_oprava_druhu_na_pomalu_jazdu_nemeni_uplnu_vyluku(env, session):
    """[12b] Dispečer zmenu preklasifikuje na pomalú jazdu 30 km/h – úplná výluka 1600/2026 sa nesmie zmeniť
    na pomalú jazdu; pomalá jazda je samostatný záznam."""
    c = registered(session, utc(2026, 11, 20, 7), "1600/2026", hours=8)
    drop_raw(env, "b.eml", mail("zmena-b", "Zmena - usek Poprad-Tatry - Liptovsky Mikulas",
                                CHANGE_NO_REF.format(d=21)))
    process_mailbox(session)
    (ev,) = events(session)
    confirm_event(session, ev.id, "dispecer1", edits={"event_kind": "pomala_jazda", "speed_kmh": 30})
    session.commit()
    session.refresh(c)
    assert (c.kind, as_utc(c.start_utc), c.speed_kmh) == ("kolaj", utc(2026, 11, 20, 7), None)
    slow = [r for r in active(session) if r.id != c.id]
    assert [(r.kind, r.speed_kmh, as_utc(r.start_utc), r.proposed_change_of) for r in slow] == [
        ("pomala_jazda", 30, utc(2026, 11, 21, 7), None)]


def test_r12_nepriradena_zmena_s_doplnenym_cislom_nezaklada_duplicitu(env, session):
    """[12c] Zmena bez čísla sa nedala priradiť (dve výluky v okolí) a dostala vlastný návrh. Dispečer doplní
    1601/2026 – zmení sa výluka 1601/2026 a návrh sa odstráni (žiadna druhá 1601/2026)."""
    a = registered(session, utc(2026, 11, 20, 7), "1600/2026")
    b = registered(session, utc(2026, 11, 21, 7), "1601/2026")
    drop_raw(env, "c.eml", mail("zmena-c", "Zmena terminu vyluky Poprad-Tatry - Liptovsky Mikulas",
                                CHANGE_NO_REF.format(d=22)))
    process_mailbox(session)
    (ev,) = events(session)
    own = session.get(Closure, ev.validation["pending_closure"]["id"])
    assert own.proposed_change_of is None and own.status == "navrh"
    own_id = own.id
    confirm_event(session, ev.id, "dispecer1", edits={"external_ref": "1601/2026"})
    session.commit()
    session.refresh(a)
    session.refresh(b)
    assert session.get(Closure, own_id) is None
    assert [(r.id, r.external_ref, as_utc(r.start_utc)) for r in active(session)] == [
        (a.id, "1600/2026", utc(2026, 11, 20, 7)), (b.id, "1601/2026", utc(2026, 11, 22, 7))]
    assert ev.validation["apply_result"]["own_removed"] == own_id


def test_r12_nova_vyluka_preklasifikovana_na_zrusenie_zrusi_vyluku_v_registri(env, session):
    """[12d] E-mail „sa nekoná“ sa extrahoval ako nová výluka (s vlastným návrhom). Dispečer ho opraví na zrušenie
    1600/2026 – zruší sa výluka 1600/2026, nie len návrh tejto udalosti."""
    c = registered(session, utc(2026, 11, 20, 5), "1600/2026", hours=10)
    drop_raw(env, "d.eml", mail("d1", "Vyluka 1. TK Poprad-Tatry - Liptovsky Mikulas 20.11.",
                                "Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš dňa 20.11.2026 od 08:00 do "
                                "14:00 sa nekoná.\n"))
    process_mailbox(session)
    (ev,) = events(session)
    own_id = ev.validation["pending_closure"]["id"]
    assert ev.kind == "vyluka_nova" and own_id
    confirm_event(session, ev.id, "dispecer1", edits={"event_kind": "vyluka_zrusenie", "external_ref": "1600/2026"})
    session.commit()
    session.refresh(c)
    assert c.status == "zrusene" and ev.validation["apply_result"]["action"] == "zrusene"
    assert session.get(Closure, own_id) is None and active(session) == []


def test_r12_zrusenie_bez_cielovej_vyluky_navrh_ponecha(env, session):
    """[12d] Ak sa výluka na zrušenie nenájde, zápis zlyhá a návrh pôvodnej udalosti ostane (nič sa nestratí)."""
    drop_raw(env, "d.eml", mail("d2", "Vyluka 1. TK Poprad-Tatry - Liptovsky Mikulas 20.11.",
                                "Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš dňa 20.11.2026 od 08:00 do "
                                "14:00 sa nekoná.\n"))
    process_mailbox(session)
    (ev,) = events(session)
    own_id = ev.validation["pending_closure"]["id"]
    confirm_event(session, ev.id, "dispecer1", edits={"event_kind": "vyluka_zrusenie", "external_ref": "1999/2026"})
    session.commit()
    assert ev.status == "potvrdene" and ev.validation["apply_result"]["ok"] is False
    assert session.get(Closure, own_id).status == "navrh"


# ====================================================================== [13] + [39] úpravy v registri a potvrdenie
ROV_2100 = ("Rozkaz o výluke č. 2100/2026\nVýluka 1. traťovej koľaje v úseku Poprad-Tatry – Liptovský Mikuláš "
            "dňa 03.11.2026 od 08:00 do 14:00.\n")


def _pending_2100(env, session) -> tuple[int, int]:
    drop_raw(env, "r.eml", mail("rov2100", "ROV c. 2100/2026 Poprad-Tatry - Liptovsky Mikulas", ROV_2100))
    process_mailbox(session)
    (ev,) = events(session)
    (c,) = session.query(Closure).all()
    assert ev.status == "navrh" and ev.needs_confirmation and c.status == "navrh" and c.source_event_id == ev.id
    ids = (ev.id, c.id)
    session.commit()
    return ids


def test_r13_uprava_navrhu_v_registri_a_potvrdenie_v_registri(env, session, client):
    """[13.1] / [39.1] Dispečer v /vyluky predĺži koniec čakajúcej výluky z e-mailu (14:00 → 16:00) a klikne
    Potvrdiť – výluka je overená s jeho koncom (nie s údajmi z e-mailu) a fronta úpravu vopred ukazuje."""
    ev_id, cid = _pending_2100(env, session)
    login_as(client, "dispecer")
    c = session.get(Closure, cid)
    r = client.post(f"/vyluky/{cid}/upravit", data={"csrf_token": csrf(client, "/vyluky"),
                                                    **register_form(c, end_utc="2026-11-03T16:00",
                                                                    note="podľa telefonátu ŽSR")},
                    follow_redirects=True)
    assert "pri potvrdení ostanú vaše úpravy" in flash(r)
    page = client.get("/fronta").text
    assert "Záznam výluky upravil dispečer v registri výluk" in page and "03.11.2026 16:00" in page
    r = client.post(f"/vyluky/{cid}/overit", data={"csrf_token": csrf(client, "/vyluky")}, follow_redirects=True)
    assert "úpravy v registri výluk ostali zachované" in flash(r)
    session.expire_all()
    c = session.get(Closure, cid)
    ev = session.get(ExtractedEvent, ev_id)
    assert c.status == "overene" and as_utc(c.end_utc) == utc(2026, 11, 3, 15)
    assert ev.status == "aplikovane" and ev.data["end"] == "2026-11-03T15:00:00+00:00"
    assert ev.validation["original_data"]["end"] == "2026-11-03T13:00:00+00:00"   # trénovací príklad ostáva
    assert any(e.get("zdroj") == "register výluk" for e in ev.validation["edits"])
    assert "podľa telefonátu ŽSR" in c.note


def test_r13_uprava_docasneho_zaznamu_zmeny_a_potvrdenie(env, session, client):
    """[13.2] Dočasný záznam navrhovanej zmeny dispečer upraví (koniec 18:00) a potvrdí na ňom – pôvodná výluka
    dostane jeho koniec 18:00, nie 14:00 z e-mailu; dočasný záznam zmizne a poznámka sa prenesie."""
    orig = Closure(kind="kolaj", track="1", from_station="Poprad-Tatry", to_station="Liptovský Mikuláš",
                   start_utc=utc(2026, 11, 3, 7), end_utc=utc(2026, 11, 3, 13), status="overene", source="manual",
                   external_ref="2101/2026")
    session.add(orig)
    session.commit()
    drop_raw(env, "z.eml", mail("rov2101z", "Zmena ROV c. 2101/2026",
                                "Oznamujeme zmenu Rozkazu o výluke č. 2101/2026 (výluka 1. TK v úseku Poprad-Tatry – "
                                "Liptovský Mikuláš).\nNový termín: 04.11.2026 od 08:00 do 14:00.\n"))
    process_mailbox(session)
    temp = session.scalars(select(Closure).where(Closure.proposed_change_of.is_not(None))).one()
    tid, oid = temp.id, orig.id
    session.commit()
    login_as(client, "dispecer")
    r = client.post(f"/vyluky/{tid}/upravit", data={"csrf_token": csrf(client, "/vyluky"),
                                                    **register_form(temp, end_utc="2026-11-04T18:00",
                                                                    note="koniec 18:00 podľa telefonátu")},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/vyluky/{tid}/overit", data={"csrf_token": csrf(client, "/vyluky")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/vyluky"
    session.expire_all()
    o = session.get(Closure, oid)
    assert session.get(Closure, tid) is None
    assert (as_utc(o.start_utc), as_utc(o.end_utc), o.status) == (utc(2026, 11, 4, 7), utc(2026, 11, 4, 17),
                                                                   "overene")
    assert "koniec 18:00 podľa telefonátu" in o.note


def test_r39_uprava_a_overenie_v_registri_potom_potvrdenie_vo_fronte(env, session, client):
    """[39.2a] Dispečer v /vyluky upraví koniec a nastaví stav „overené“, udalosť potom potvrdí vo fronte –
    koniec ostane jeho; hláška povie, ktoré úpravy z registra ostali."""
    ev_id, cid = _pending_2100(env, session)
    login_as(client, "dispecer")
    c = session.get(Closure, cid)
    client.post(f"/vyluky/{cid}/upravit", data={"csrf_token": csrf(client, "/vyluky"),
                                                **register_form(c, end_utc="2026-11-03T17:00", status="overene")})
    r = client.post(f"/fronta/{ev_id}/potvrdit", data={"csrf_token": csrf(client, "/fronta"), "akcia": "potvrdit"},
                    follow_redirects=True)
    assert "ponechané úpravy z registra výluk (koniec)" in flash(r)
    session.expire_all()
    c = session.get(Closure, cid)
    assert c.status == "overene" and as_utc(c.end_utc) == utc(2026, 11, 3, 16)
    assert session.get(ExtractedEvent, ev_id).status == "aplikovane"


def test_r39_vyslovna_oprava_vo_fronte_ma_prednost_pred_upravou_v_registri(env, session, client):
    """Dispečer v registri zmení koniec na 16:00, vo fronte („Upraviť a potvrdiť“) koniec 18:00 a koľaj nechá –
    platí výslovná oprava vo fronte (18:00), ostatné úpravy z registra (koľaj 2) ostanú."""
    ev_id, cid = _pending_2100(env, session)
    login_as(client, "dispecer")
    c = session.get(Closure, cid)
    client.post(f"/vyluky/{cid}/upravit", data={"csrf_token": csrf(client, "/vyluky"),
                                                **register_form(c, end_utc="2026-11-03T16:00", track="2")})
    ev = session.get(ExtractedEvent, ev_id)
    form = {"csrf_token": csrf(client, "/fronta"), "akcia": "upravit", "event_kind": ev.kind}
    for key, value in ev.data.items():                       # formulár fronty ukazuje údaje z e-mailu
        if isinstance(value, str) and value.endswith("+00:00"):
            value = local_input(datetime.fromisoformat(value))
        form[f"f__{key}"] = "" if value is None else str(value)
    form["f__end"] = "2026-11-03T18:00"
    r = client.post(f"/fronta/{ev_id}/potvrdit", data=form, follow_redirects=True)
    assert "s úpravami (end)" in flash(r)
    session.expire_all()
    c = session.get(Closure, cid)
    assert as_utc(c.end_utc) == utc(2026, 11, 3, 17) and c.track == "2" and c.status == "overene"


def test_r39_automaticky_zapisana_vyluka_uprava_a_potvrdenie(env, session, client, monkeypatch, fixed_now):
    """[39.3] Výluka o viac ako 72 h zapísaná automaticky (udalosť „aplikovane“, výluka „navrh“): dispečer ju
    upraví (koniec 16:00) a potvrdí – koniec 16:00 ostane (predtým sa vrátil na 10:00 z e-mailu)."""
    monkeypatch.setattr(env, "confirm_threshold", 0.8)
    drop_raw(env, "a.eml", mail("rov2200", "ROV c. 2200/2026 Poprad-Tatry - Liptovsky Mikulas",
                                "Rozkaz o výluke č. 2200/2026\nVýluka 1. traťovej koľaje v úseku Poprad-Tatry – "
                                "Liptovský Mikuláš dňa 20.11.2026 od 08:00 do 10:00.\n"))
    assert process_mailbox(session)["auto_applied"] == 1
    (ev,) = events(session)
    (c,) = session.query(Closure).all()
    assert ev.status == "aplikovane" and ev.confirmed_at is None and c.status == "navrh"
    ev_id, cid = ev.id, c.id
    session.commit()
    login_as(client, "dispecer")
    assert "Zapísané automaticky" in client.get("/fronta").text
    client.post(f"/vyluky/{cid}/upravit", data={"csrf_token": csrf(client, "/vyluky"),
                                                **register_form(c, end_utc="2026-11-20T16:00")})
    assert "Upravené v registri výluk" in client.get("/fronta").text
    r = client.post(f"/vyluky/{cid}/overit", data={"csrf_token": csrf(client, "/vyluky")}, follow_redirects=True)
    assert "úpravy v registri výluk ostali zachované" in flash(r)
    session.expire_all()
    c = session.get(Closure, cid)
    assert c.status == "overene" and as_utc(c.end_utc) == utc(2026, 11, 20, 15)
    assert session.get(ExtractedEvent, ev_id).confirmed_at is not None


def test_r13_uprava_mimoriadnosti_pole_mimo_schemy_ostane(env, session, client):
    """Mimoriadnosť nemá v dátach koľaj ani druh – dispečer ich nastaví v registri; potvrdenie ich zachová
    (hodnoty z registra, nie z e-mailu)."""
    shutil.copy(SAMPLES / "06_mimoriadna_udalost.eml", Path(env.mail_dir) / "06.eml")
    process_mailbox(session)
    (ev,) = events(session, "mimoriadnost")
    (c,) = session.query(Closure).all()
    ev_id, cid = ev.id, c.id
    session.commit()
    login_as(client, "dispecer")
    client.post(f"/vyluky/{cid}/upravit", data={"csrf_token": csrf(client, "/vyluky"),
                                                **register_form(c, track="1", kind="trakcne_vedenie")})
    r = client.post(f"/fronta/{ev_id}/potvrdit", data={"csrf_token": csrf(client, "/fronta"), "akcia": "potvrdit"},
                    follow_redirects=True)
    assert "ponechané úpravy z registra výluk" in flash(r)
    session.expire_all()
    c = session.get(Closure, cid)
    assert (c.track, c.kind, c.electric_only, c.status) == ("1", "trakcne_vedenie", True, "overene")


def test_r13_koniec_vyluky_do_odvolania_zadany_dispecerom_ostane(env, session, client, fixed_now):
    """POTR „do odvolania“ čaká na potvrdenie; dispečer v registri zadá skutočný koniec (ŽSR ho oznámila
    telefonicky). Agent ho nesmie ďalej posúvať ako technický koniec a potvrdenie ho nesmie prepísať."""
    from datetime import timedelta as td

    shutil.copy(SAMPLES / "17_telegram_potr.eml", Path(env.mail_dir) / "17.eml")
    process_mailbox(session)
    (ev,) = events(session, "pomala_jazda")
    (c,) = session.query(Closure).all()
    assert c.until_revoked and c.status == "navrh"
    ev_id, cid = ev.id, c.id
    session.commit()
    login_as(client, "dispecer")
    client.post(f"/vyluky/{cid}/upravit", data={"csrf_token": csrf(client, "/vyluky"),
                                                **register_form(c, end_utc="2026-11-05T18:00")})
    session.expire_all()
    assert session.get(Closure, cid).until_revoked is False
    process_mailbox(session, now=fixed_now + td(days=2, hours=12))         # posun technického konca by tu zasiahol
    r = client.post(f"/fronta/{ev_id}/potvrdit", data={"csrf_token": csrf(client, "/fronta"), "akcia": "potvrdit"},
                    follow_redirects=True)
    assert "ponechané úpravy z registra výluk (koniec)" in flash(r)
    session.expire_all()
    c = session.get(Closure, cid)
    assert (as_utc(c.end_utc), bool(c.until_revoked), c.status) == (utc(2026, 11, 5, 17), False, "overene")


def test_r39_planovac_po_potvrdeni_pocita_s_predlzenou_vylukou(strict, session, client, fixed_now):
    """[39.4] Následok: vlak s odchodom v predĺženej časti výluky (po konci z e-mailu) nesmie dostať ZELENÚ."""
    from vlaky.planovanie.planovac import evaluate_train

    env = strict                                            # skutočný plánovač
    body = ("FIKTÍVNE.\nOznamujeme Vám, že na základe Rozkazu o výluke (ROV) č. 5005/2026 bude vykonaná výluka "
            "oboch traťových koľají:\nÚsek: Kysak – Košice\nDruh výluky: výluka oboch traťových koľají\n"
            "Termín: od 03.11.2026 00:00 do 03.11.2026 04:00\n")
    drop_raw(env, "k.eml", mail("rov5005", "ROV 5005/2026", body))
    process_mailbox(session)
    (c,) = session.query(Closure).all()
    cid = c.id
    session.commit()
    login_as(client, "dispecer")
    client.post(f"/vyluky/{cid}/upravit", data={"csrf_token": csrf(client, "/vyluky"),
                                                **register_form(c, end_utc="2026-11-03T10:00")})
    client.post(f"/vyluky/{cid}/overit", data={"csrf_token": csrf(client, "/vyluky")})
    session.expire_all()
    c = session.get(Closure, cid)
    assert c.status == "overene" and as_utc(c.end_utc) == utc(2026, 11, 3, 9)
    dep = utc(2026, 11, 3, 5)                               # po konci z e-mailu (03:00 UTC), v predĺžení
    t = Train(number="77020", origin="Plaveč", destination="Košice", traction="M", gauge="1435", vmax_kmh=80,
              status="planovany", ready_utc=dep, deadline_utc=dep + timedelta(hours=4))
    session.add(t)
    session.flush()
    session.add(TrainPath(train_id=t.id, train_number="77020", kind="ad_hoc", status="pridelena", departure_utc=dep))
    session.flush()
    rec = evaluate_train(session, t.id, fixed_now, "test")
    assert rec.color != "ZELENÁ", rec.summary


def test_r39_zrusenie_navrhu_v_registri_zamietne_udalost(env, session, client):
    """Zrušenie čakajúcej výluky z e-mailu v registri = zamietnutie udalosti; neskoršie potvrdenie vo fronte ju
    nesmie potichu znova zapísať."""
    ev_id, cid = _pending_2100(env, session)
    login_as(client, "dispecer")
    r = client.post(f"/vyluky/{cid}/zrusit", data={"csrf_token": csrf(client, "/vyluky"), "dovod": "ŽSR ju odvolala"},
                    follow_redirects=True)
    assert f"udalosť #{ev_id} z e-mailu" in flash(r)
    assert f'id="udalost-{ev_id}"' not in client.get("/fronta").text
    r = client.post(f"/fronta/{ev_id}/potvrdit", data={"csrf_token": csrf(client, "/fronta"), "akcia": "potvrdit"},
                    follow_redirects=True)
    assert "už bola spracovaná" in flash(r)
    session.expire_all()
    ev = session.get(ExtractedEvent, ev_id)
    assert ev.status == "zamietnute" and "zrušené v registri výluk" in ev.validation["rejection"]["reason"]
    assert active(session) == []


# ====================================================================== [14] presun termínu nie je noc
def _rules(body: str):
    from vlaky.posta.pravidla import RuleExtractor
    from vlaky.posta.udalosti import MailDocument

    return RuleExtractor().extract(MailDocument(subject="Výluka", body=body, received_at=utc(2026, 11, 2, 6)))


def _terms(items) -> list:
    return sorted((i.data["start"], i.data["end"]) for i in items)


SHIFTS = ["Výluka sa presúva z 12.11. na 13.11.2026, čas 8:00 – 14:00.",
          "Termín sa mení z 12. na 13.11.2026 od 8:00 do 14:00.",
          "Výluka bude preložená z 12.11.2026 na 13.11.2026 od 08:00 do 14:00."]


@pytest.mark.parametrize("line", SHIFTS)
def test_r14_presun_o_jeden_den_plati_novy_den(cfg, line):
    """[14] „z 12.11. na 13.11.“ pri presune termínu je nový deň 13.11., nie noc začínajúca 12.11."""
    (it,) = _rules(f"Oznamujeme zmenu ROV č. 1142/2026 ({SEC}). {line}")
    assert (it.data["start"], it.data["end"]) == ("2026-11-13T07:00:00+00:00", "2026-11-13T13:00:00+00:00")
    assert not any("cez polnoc" in n for n in it.notes)


def test_r14_noc_len_ked_ju_text_oznacuje(cfg):
    """[14] Noc (D/D+1 ako jeden dátum) len pri označení noci alebo okne cez polnoc; skutočné noci ostávajú."""
    cases = [
        ("v noci z 12. na 13.11.2026 od 22:00 do 05:00", [("2026-11-12T21:00:00+00:00", "2026-11-13T04:00:00+00:00")]),
        ("12./13.11.2026 od 22:00 do 05:00", [("2026-11-12T21:00:00+00:00", "2026-11-13T04:00:00+00:00")]),
        ("z 12. na 13.11.2026 od 22:00 do 05:00", [("2026-11-12T21:00:00+00:00", "2026-11-13T04:00:00+00:00")]),
        ("v noci z 12.11. na 13.11.2026 od 22:00 do 6:00",
         [("2026-11-12T21:00:00+00:00", "2026-11-13T05:00:00+00:00")]),
        # presun nočnej výluky na ďalšiu noc – nový termín je noc 13./14.11.
        ("sa presúva z 12.11.2026 na 13.11.2026, čas 22:00 – 05:00",
         [("2026-11-13T21:00:00+00:00", "2026-11-14T04:00:00+00:00")]),
        # „12./13.11.“ s denným oknom nie je noc – oba dni (prísnejší prípad)
        ("12./13.11.2026 od 8:00 do 14:00", [("2026-11-12T07:00:00+00:00", "2026-11-12T13:00:00+00:00"),
                                             ("2026-11-13T07:00:00+00:00", "2026-11-13T13:00:00+00:00")]),
    ]
    got = {text: _terms(_rules(f"Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš {text}.")) for text, _ in cases}
    assert got == dict(cases)


@pytest.mark.parametrize("line", SHIFTS)
def test_r14_presun_viditelny_pre_planovac_a_po_potvrdeni(env, session, fixed_now, line):
    """[14] End to end: výluka 1142/2026 je v registri na 12.11.; presun na 13.11. musí plánovač vidieť (dočasný
    záznam „navrh“ na 13.11.) a potvrdenie ho zapíše do registra."""
    c = registered(session, utc(2026, 11, 12, 7), "1142/2026")
    drop_raw(env, "z.eml", mail(f"posun{abs(hash(line)) % 10 ** 8}", "Zmena ROV c. 1142/2026",
                                "Oznamujeme zmenu Rozkazu o výluke č. 1142/2026 (výluka 1. TK v úseku Poprad-Tatry – "
                                f"Liptovský Mikuláš).\n{line}\n"))
    process_mailbox(session)
    (ev,) = events(session)
    temp = session.get(Closure, ev.validation["pending_closure"]["id"])
    assert temp.proposed_change_of == c.id and as_utc(temp.start_utc) == utc(2026, 11, 13, 7)
    assert {w.closure_id for w in windows(session, fixed_now)} == {c.id, temp.id}
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    session.refresh(c)
    assert as_utc(c.start_utc) == utc(2026, 11, 13, 7) and c.status == "overene"


# ====================================================================== [15] preposlané e-maily od ŽSR
def test_r15_zsr_preposiela_citovanu_spravu_susednej_spravy(env, session):
    """[15a] Overený e-mail ŽSR („FW:“) s vlastným ROV cituje blok SŽ od nepovoleného odosielateľa – nejde do
    karantény; pôvodný odosielateľ je v ``forwarded_from`` a nič sa nezapíše automaticky."""
    body = ("Na vedomie – ROV č. 1700/2026: výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš dňa 20.11.2026 "
            "od 08:00 do 14:00.\n\nFrom: Dispečink Ostrava <dispecink.ostrava@spravazeleznic.example>\n"
            "Sent: Monday, November 2, 2026 8:00 AM\nTo: dispecing.orza@zsr.example\nSubject: Výluka\n\n"
            "Dne 20.11.2026 od 08:00 do 14:00 výluka koleje.\n")
    drop_raw(env, "a.eml", mail("fw-a", "FW: ROV 1700/2026 Poprad-Tatry - Liptovsky Mikulas", body,
                                sender="ZSR OR Zilina dispecing <dispecing.orza@zsr.example>"))
    s = process_mailbox(session)
    em = session.query(Email).one()
    assert em.verdict == "ok" and em.forwarded_from == "dispecink.ostrava@spravazeleznic.example", em.verdict_reason
    assert s["auto_applied"] == 0 and s["waiting_confirmation"] == 1
    (ev,) = events(session)
    assert ev.needs_confirmation and any("preposlan" in r for r in ev.validation["confirmation_reasons"])
    # P1 ostáva: ten istý obsah prepošle vlastná schránka (nie ŽSR) od nepovoleného odosielateľa – karanténa
    fwd = ("Preposielam.\n\n---------- Forwarded message ---------\nFrom: Utocnik <vyluky@utocnik.example>\n"
           "Date: Mon, 2 Nov 2026 at 05:00\nSubject: ROV\nTo: dispecing@dopravca.example\n\n" + body)
    drop_raw(env, "p.eml", mail("fwd-p1", "Fwd: ROV 1700/2026", fwd, sender="Dispecing <dispecing@dopravca.example>",
                                ar=AR_DOPRAVCA))
    process_mailbox(session)
    em2 = session.scalars(select(Email).where(Email.sender == "dispecing@dopravca.example")).one()
    assert em2.verdict == "karantena" and "vyluky@utocnik.example" in em2.verdict_reason
    assert len(events(session)) == 1


def test_r15_zsr_preposiela_interny_rov_ako_prilohu(env, session):
    """[15b] ŽSR prepošle interný ROV GR (message/rfc822 bez Authentication-Results) – nie karanténa, ale potvrdenie."""
    inner = ("From: ZSR GR vyluky <vyluky.gr@zsr.example>\r\nTo: dispecing.orza@zsr.example\r\n"
             "Subject: ROV 1701/2026\r\nDate: Mon, 02 Nov 2026 04:00:00 +0100\r\nMessage-ID: <in-1701@zsr.example>\r\n"
             "Content-Type: text/plain; charset=utf-8\r\n\r\n"
             "Rozkaz o výluke č. 1701/2026: výluka 1. TK v úseku Žilina – Vrútky dňa 21.11.2026 od 08:00 do 14:00.\r\n")
    outer = (AR_ZSR.replace("\n", "\r\n") + "From: ZSR OR Zilina <vyluky.orza@zsr.example>\r\n"
             "To: agent.dispecing@dopravca.example\r\nSubject: FW: ROV 1701/2026\r\n"
             "Date: Mon, 02 Nov 2026 05:10:00 +0100\r\nMessage-ID: <fw-b@zsr.example>\r\nMIME-Version: 1.0\r\n"
             "Content-Type: multipart/mixed; boundary=XX\r\n\r\n--XX\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
             "Preposielame ROV GR.\r\n--XX\r\nContent-Type: message/rfc822\r\n\r\n" + inner + "--XX--\r\n")
    drop_raw(env, "b.eml", outer.encode("utf-8"))
    s = process_mailbox(session)
    em = session.query(Email).one()
    assert em.verdict == "ok" and em.forwarded_from == "vyluky.gr@zsr.example", em.verdict_reason
    assert "Authentication-Results" in em.verdict_reason and s["auto_applied"] == 0
    (ev,) = events(session)
    assert ev.needs_confirmation and ev.status == "navrh"


@pytest.mark.parametrize("att", [
    "Train: Nex 54321\nDate: 14.11.2026\nFrom: Bratislava-Petrzalka\nTo: Kuty\n\nPoints:\nBratislava-Petrzalka 06:10\n",
    "Vlak: Nex 54321\nDatum: 14.11.2026\nFrom: Bratislava-Petrzalka 06:10\nTo: Kuty 07:30\n",
])
def test_r15_textova_priloha_s_riadkom_from_nie_je_email(env, session, att):
    """[15c] KIS posiela trasu ako .txt s riadkami „From: Bratislava-Petrzalka“ – nie je to priložený e-mail."""
    raw = (AR_ZSR.replace("\n", "\r\n") + "From: KIS ZSR <kis-noreply@zsr.example>\r\n"
           "To: agent.dispecing@dopravca.example\r\nSubject: Pridelenie trasy ad hoc Nex 54321 14.11.2026\r\n"
           f"Date: Mon, 02 Nov 2026 05:10:00 +0100\r\nMessage-ID: <c-{len(att)}@zsr.example>\r\nMIME-Version: 1.0\r\n"
           "Content-Type: multipart/mixed; boundary=XX\r\n\r\n--XX\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
           "Pridelenie trasy ad hoc, podrobnosti v prílohe.\r\n--XX\r\n"
           "Content-Type: text/plain; charset=utf-8; name=trasa_54321.txt\r\n"
           "Content-Disposition: attachment; filename=trasa_54321.txt\r\n\r\n" + att.replace("\n", "\r\n")
           + "--XX--\r\n")
    drop_raw(env, "c.eml", raw.encode("utf-8"))
    process_mailbox(session)
    em = session.query(Email).one()
    assert em.verdict == "ok" and not em.forwarded_from, em.verdict_reason
    assert [e.kind for e in events(session)] == ["trasa_pridelena"]


def test_r15_priloha_so_skutocnymi_hlavickami_je_email(cfg):
    """Textová príloha so skutočným blokom hlavičiek (From s adresou, Subject, Date) sa naďalej overuje ako e-mail."""
    from vlaky.posta.parser import _embedded_message

    real = (b"From: ZSR OR Kosice <vyluky.orke@zsr.example>\nTo: x@dopravca.example\nSubject: ROV\n"
            b"Date: Mon, 02 Nov 2026 05:10:00 +0100\n\nText.\n")
    folded = b"From:\n =?utf-8?q?ZSR?= <vyluky.orke@zsr.example>\nMessage-ID: <a@zsr.example>\n\nText.\n"
    assert _embedded_message("povodna.eml", "text/plain", real) is not None
    assert _embedded_message("povodna.txt", "text/plain", folded) is not None
    assert _embedded_message("trasa.txt", "text/plain", b"Vlak: 1\nDate: 1.1.\nFrom: A\nTo: B\n") is None
    assert _embedded_message("x.txt", "text/plain", b"From: a@b.example\nPoznamka bez dvojbodky\nDate: x\n\nT") is None


# ====================================================================== [16] zoznamy dní a nocí
@pytest.mark.parametrize("text,expected", [
    ("v nociach 12./13.11. a 13./14.11.2026 od 22:00 do 05:00",
     [("2026-11-12T21:00:00+00:00", "2026-11-13T04:00:00+00:00"),
      ("2026-11-13T21:00:00+00:00", "2026-11-14T04:00:00+00:00")]),
    ("v noci z 12. na 13.11. a z 13. na 14.11.2026 od 22:00 do 05:00",
     [("2026-11-12T21:00:00+00:00", "2026-11-13T04:00:00+00:00"),
      ("2026-11-13T21:00:00+00:00", "2026-11-14T04:00:00+00:00")]),
    ("12./13.11.2026 a 13./14.11.2026 od 22:00 do 05:00",
     [("2026-11-12T21:00:00+00:00", "2026-11-13T04:00:00+00:00"),
      ("2026-11-13T21:00:00+00:00", "2026-11-14T04:00:00+00:00")]),
    ("12.11. a 13.11.2026 od 8:00 do 14:00",
     [("2026-11-12T07:00:00+00:00", "2026-11-12T13:00:00+00:00"),
      ("2026-11-13T07:00:00+00:00", "2026-11-13T13:00:00+00:00")]),
    ("dňa 12.11.2026, 14.11.2026 od 8:00 do 14:00",
     [("2026-11-12T07:00:00+00:00", "2026-11-12T13:00:00+00:00"),
      ("2026-11-14T07:00:00+00:00", "2026-11-14T13:00:00+00:00")]),
    ("v čase 8:00 – 14:00 v dňoch 12.11. a 13.11.2026",
     [("2026-11-12T07:00:00+00:00", "2026-11-12T13:00:00+00:00"),
      ("2026-11-13T07:00:00+00:00", "2026-11-13T13:00:00+00:00")]),
])
def test_r16_zoznam_dni_a_noci_s_jednym_oknom(cfg, text, expected):
    """[16] Každý deň (noc) zoznamu dostane obdobie – žiadny sa potichu nestratí; istota sa neznižuje."""
    items = _rules(f"Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš {text}.")
    assert _terms(items) == expected
    assert all(i.confidence == 0.85 for i in items)


def test_r16_nepriradeny_datum_znizi_istotu(cfg):
    """[16] Dátum v odseku, ktorý nepatrí k žiadnemu obdobiu, zníži istotu a dispečer dostane poznámku."""
    (it,) = _rules("Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš 12.11.2026 od 8:00 do 14:00. "
                   "Náhradná doprava od 10.11.2026.")
    assert it.confidence < 0.8 and any("dátum „10.11.2026“" in n and "overte termín" in n for n in it.notes)
    # dátum vydania či rozhodnutia nie je termín výluky – bez falošného upozornenia
    (ok,) = _rules("Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš 12.11.2026 od 08:00 do 14:00 "
                   "(ROV zo dňa 30.10.2026).")
    assert ok.confidence == 0.85 and not any("dátum" in n for n in ok.notes)


def test_r16_zoznam_noci_end_to_end_obe_noci_pre_planovac(env, session, fixed_now):
    """[16] E-mail so zoznamom dvoch nocí: obe noci majú záznam pre plánovač (nie len druhá)."""
    drop_raw(env, "n.eml", mail("noci", "ROV c. 2300/2026 Poprad-Tatry - Liptovsky Mikulas",
                                "Rozkaz o výluke č. 2300/2026: výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš "
                                "v nociach 03./04.11. a 04./05.11.2026 od 22:00 do 05:00.\n"))
    process_mailbox(session)
    starts = sorted(as_utc(c.start_utc) for c in active(session))
    assert starts == [utc(2026, 11, 3, 21), utc(2026, 11, 4, 21)]
    assert len({w.closure_id for w in windows(session, fixed_now, 96)}) == 2


# ====================================================================== [17] okno pred reťazou
def test_r17_skorsie_okno_kontaktu_nevytvori_dalsiu_vyluku(cfg):
    """[17] „Kontakt: dispečer 7:00 – 15:30.“ pred výlukou 8:00 – 14:00 nie je druhé okno tej istej výluky."""
    items = _rules("Kontakt: dispečer 7:00 – 15:30. Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš "
                   "12.11.2026 v čase 8:00 – 14:00.")
    assert _terms(items) == [("2026-11-12T07:00:00+00:00", "2026-11-12T13:00:00+00:00")]
    # druhé okno za prvým ostáva podporované
    assert len(_rules(f"Výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš 12.11.2026 v čase 8:00 – 12:00 "
                      "a 13:00 – 16:00.")) == 2


# ====================================================================== [18] chyba záznamu „navrh“ nezahodí udalosť
class _PgLengths:
    """Ako PostgreSQL: dlhšia hodnota ako VARCHAR(n) skončí chybou DataError pri zápise (flush)."""

    def __init__(self, extra=None) -> None:
        self.extra = extra

    def __call__(self, session, flush_context, instances):  # noqa: ANN001
        for obj in list(session.new) + list(session.dirty):
            mapper = getattr(obj, "__mapper__", None)
            if mapper is None:
                continue
            for col in mapper.columns:
                v = getattr(obj, col.key, None)
                if isinstance(v, str) and isinstance(col.type, String) and col.type.length and \
                        len(v) > col.type.length:
                    raise DataError("INSERT", {}, Exception(f"value too long for type character varying"
                                                            f"({col.type.length})"))
            if self.extra:
                self.extra(obj)


@pytest.fixture()
def pg_lengths():
    checks = []

    def install(extra=None):
        check = _PgLengths(extra)
        sa_event.listen(Session, "before_flush", check)
        checks.append(check)

    yield install
    for check in checks:
        sa_event.remove(Session, "before_flush", check)


LONG = ("Odbočka Skalica-sever, zhlavie smer Holíč nad Moravou vrátane výhybiek č. 5 až 17, manipulačných koľají "
        "a koľajiska nákladného obvodu")


def test_r18_dlhy_nazov_dopravne_z_llm_nezahodi_udalost(env, session, monkeypatch, pg_lengths):
    """[18] LLM vráti 133-znakový názov dopravne; na PostgreSQL by zápis záznamu „navrh“ zlyhal a s ním všetky
    udalosti e-mailu. Udalosť ostane vo fronte a záznam má názov skrátený na dĺžku stĺpca."""
    from vlaky.posta import llm
    from vlaky.posta.udalosti import ExtractedItem

    def fake_extract(self, doc):  # noqa: ANN001
        return [ExtractedItem("vyluka_nova", {"from_station": "Senica", "to_station": LONG,
                                              "start": "2026-11-03T07:00:00+00:00", "end": "2026-11-03T13:00:00+00:00",
                                              "kind": "uplna", "external_ref": "1401/2026"}, 0.9, [], {}, {},
                              extractor="llm")]

    monkeypatch.setattr(llm.LLMExtractor, "enabled", staticmethod(lambda: True))
    monkeypatch.setattr(llm.LLMExtractor, "extract", fake_extract)
    pg_lengths()
    drop_raw(env, "a.eml", mail("rov-1401", "ROV c. 1401/2026", "Rozkaz o výluke č. 1401/2026 – úplná výluka úseku "
                                                                f"Senica – {LONG}, 03.11.2026 od 08:00 do 14:00.\n"))
    s = process_mailbox(session)
    assert s["errors"] == [] and session.query(Email).one().status == "spracovany"
    (ev,) = events(session)
    (c,) = session.query(Closure).all()
    assert ev.status == "navrh" and len(c.to_station) == 120 and c.source_event_id == ev.id


def test_r18_zlyhanie_zaznamu_navrh_je_izolovane(env, session, pg_lengths):
    """[18] Ak zápis záznamu „navrh“ predsa zlyhá (chyba databázy), udalosť a ostatné udalosti e-mailu ostanú,
    dispečer vidí upozornenie a e-mail nie je „chyba“ (SAVEPOINT)."""
    def fail_for(obj):  # noqa: ANN001
        if isinstance(obj, Closure) and obj.from_station == "Žilina":
            raise DataError("INSERT", {}, Exception("simulovaná chyba databázy"))

    pg_lengths(fail_for)
    shutil.copy(SAMPLES / "02_oznamenie_vyluk_tyzden_47.eml", Path(env.mail_dir) / "02.eml")
    s = process_mailbox(session)
    assert s["errors"] == [] and session.query(Email).one().status == "spracovany"
    evs = events(session)
    assert len(evs) == 2 and all(e.status == "navrh" and e.needs_confirmation for e in evs), \
        [(e.kind, e.status) for e in evs]
    failed = next(e for e in evs if e.data["from_station"] == "Žilina")
    assert failed.validation["pending_closure"]["id"] is None
    assert "nepodaril" in failed.validation["pending_closure"]["note"]
    assert any("nepodarilo zapísať pre plánovač" in w for w in failed.validation["warnings"])
    assert [c.from_station for c in session.query(Closure)] == ["Kraľovany"]
    assert session.scalar(select(AuditLog).where(AuditLog.action == "vyluka_navrh_zlyhal")) is not None


# ====================================================================== [19] kolízia Message-ID
def test_r19_iny_odosielatel_s_rovnakym_message_id_nezhltne_rov_zsr(env, session):
    """[19] Povolený odosielateľ (zákazník) použije Message-ID ROV ŽSR skôr; pravý ROV sa uloží samostatne
    (iné Message-ID s dodatkom), spracuje sa a kolízia je v audite."""
    drop_raw(env, "1.eml", eml(AR_ZAKAZNIK + "From: Logistika <logistika@zakaznik.example>\n"
                                             "To: agent@dopravca.example\nSubject: dotaz\n"
                                             "Date: Mon, 02 Nov 2026 05:00:00 +0100\nMessage-ID: <rov-1301@zsr.example>\n"
                                             "Content-Type: text/plain; charset=utf-8",
                               "Dobrý deň, kedy príde náš vlak?\n"))
    process_mailbox(session)
    drop_raw(env, "2.eml", eml(AR_ZSR + "From: ZSR OR Kosice <vyluky.orke@zsr.example>\nTo: agent@dopravca.example\n"
                                        "Subject: ROV c. 1301/2026\nDate: Mon, 02 Nov 2026 05:10:00 +0100\n"
                                        "Message-ID: <rov-1301@zsr.example>\nContent-Type: text/plain; charset=utf-8",
                               "Rozkaz o výluke č. 1301/2026: výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš "
                               "dňa 20.11.2026 od 08:00 do 14:00.\n"))
    s = process_mailbox(session)
    assert s["fetched"] == 1
    zsr = session.scalars(select(Email).where(Email.sender == "vyluky.orke@zsr.example")).one()
    assert zsr.message_id.startswith("<rov-1301@zsr.example>#") and len(zsr.message_id) <= 300
    assert "Message-ID" in zsr.error and zsr.verdict == "ok"
    assert [e.kind for e in events(session)] == ["ine", "vyluka_nova"] and len(active(session)) == 1
    log = session.scalar(select(AuditLog).where(AuditLog.action == "email_message_id_kolizia"))
    assert log is not None
    # rovnaká kópia znova – bez duplicít; iná správa od toho istého odosielateľa s rovnakým Message-ID sa uloží
    # samostatne (revízia 3, MR8: nesmie potichu zmiznúť)
    shutil.copy(Path(env.mail_dir) / "spracovane" / "2.eml", Path(env.mail_dir) / "2b.eml")
    drop_raw(env, "3.eml", eml(AR_ZAKAZNIK + "From: logistika@zakaznik.example\nTo: agent@dopravca.example\n"
                                             "Subject: dotaz 2\nMessage-ID: <rov-1301@zsr.example>\n"
                                             "Content-Type: text/plain; charset=utf-8", "Ešte raz.\n"))
    assert process_mailbox(session)["fetched"] == 1 and session.query(Email).count() == 3


def test_r19_graph_stiahne_rovnake_message_id_od_ineho_odosielatela(env, session, monkeypatch):
    """[19] Microsoft Graph preskakuje známe Message-ID len pri opätovnom doručení tej istej správy (rovnaký
    odosielateľ a čas prijatia); iný odosielateľ alebo iná správa s rovnakým Message-ID sa stiahne (3. revízia,
    MR8 – o kópii rozhodne obsah)."""
    from vlaky.posta.zdroje import get_source

    received = utc(2026, 11, 2, 6, 12)
    session.add(Email(message_id="<rov-1@zsr.example>", sender="logistika@zakaznik.example", verdict="ok",
                      status="spracovany", received_at=received))
    session.commit()
    monkeypatch.setattr(env, "mail_source", "graph")
    src = get_source(session)
    assert src.known_sender("<rov-1@zsr.example>", "logistika@zakaznik.example", received) is True
    assert src.known_sender("<rov-1@zsr.example>", "logistika@zakaznik.example",
                            received + timedelta(hours=3)) is False      # iná správa s rovnakým Message-ID
    assert src.known_sender("<rov-1@zsr.example>", "logistika@zakaznik.example") is False
    assert src.known_sender("<rov-1@zsr.example>", "vyluky.orke@zsr.example", received) is False
    assert src.known("<rov-1@zsr.example>") is True


# ====================================================================== [41] čakajúca výluka → prepočet hneď
def test_r41_cakajuca_vyluka_spusti_prepocet_v_tom_istom_cykle(env, session, planner):
    """[41] Výluka do 72 h vždy čaká na potvrdenie; jej záznam „navrh“ musí hneď spustiť prepočet (a ``planning``,
    aby agent neprepočítaval druhýkrát). Čakajúca udalosť bez záznamu pre plánovač (napr. „iné“) ani ďalší cyklus
    bez zmien prepočet nespúšťajú – žiadny zbytočný šum."""
    shutil.copy(SAMPLES / "16_ine_informacia.eml", Path(env.mail_dir) / "16.eml")
    s = process_mailbox(session)
    assert s["waiting_confirmation"] == 1 and s["planning"] is None and planner == []
    drop_raw(env, "r.eml", mail("rov2100", "ROV c. 2100/2026 Poprad-Tatry - Liptovsky Mikulas", ROV_2100))
    s = process_mailbox(session)
    assert s["auto_applied"] == 0 and s["waiting_confirmation"] == 1
    assert s["planning"] == {"ok": True, "recommendations": 0}
    assert len(planner) == 1 and "čaká na potvrdenie" in planner[0]
    s2 = process_mailbox(session)
    assert s2["planning"] is None and len(planner) == 1


def test_r41_cyklus_agenta_upozorni_hned_po_e_maile(cfg):
    """[41] Cyklus agenta: e-mail s výlukou na trase vlaku (čaká na potvrdenie) zmení odporúčanie a upozorní
    dispečera v tom istom cykle (nie až pri pravidelnom prepočte o 15 min); ďalší cyklus neupozorní znova."""
    from vlaky.agent.beh import run_cycle
    from vlaky.cas import set_fake_now
    from vlaky.db import session_scope

    t0 = utc(2026, 11, 2, 4, 0)
    set_fake_now(t0)
    with session_scope() as s:
        t = Train(number="77013", origin="Plaveč", destination="Košice", traction="M", gauge="1435", vmax_kmh=80,
                  status="planovany", ready_utc=t0 + timedelta(minutes=30), deadline_utc=t0 + timedelta(hours=8))
        s.add(t)
        s.flush()
        s.add(TrainPath(train_id=t.id, train_number="77013", kind="ad_hoc", status="pridelena",
                        departure_utc=t0 + timedelta(minutes=30)))
    run_cycle(t0)
    with session_scope() as s:
        first = s.scalar(select(Recommendation).order_by(Recommendation.id.desc()))
        assert first.color == "ZELENÁ"
        first_id = first.id
    t1 = t0 + timedelta(minutes=2)
    set_fake_now(t1)
    body = ("FIKTÍVNE.\nOznamujeme Vám, že na základe Rozkazu o výluke (ROV) č. 3101/2026 bude vykonaná výluka "
            "oboch traťových koľají:\nTrať: 188 Košice – Plaveč\nÚsek: Kysak – Košice\n"
            "Druh výluky: výluka oboch traťových koľají\nTermín: od 02.11.2026 05:40 do 02.11.2026 09:00\n")
    drop_raw(cfg, "r.eml", mail("rov3101", "ROV 3101/2026 vyluka", body))
    out = run_cycle(t1)
    with session_scope() as s:
        latest = s.scalar(select(Recommendation).order_by(Recommendation.id.desc()))
        sent = list(s.scalars(select(Notification).where(Notification.kind == "odporucanie",
                                                         Notification.created_at >= t1)))
        assert out["mail"]["planning"]["ok"] is True and out["evaluated"] == 0      # bez dvojitého prepočtu
        assert latest.id != first_id and latest.color != "ZELENÁ" and "čaká na potvrdenie" in latest.trigger
        assert sent
        n_sent = len(sent)
    set_fake_now(t1 + timedelta(minutes=1))
    run_cycle(t1 + timedelta(minutes=1))
    with session_scope() as s:
        again = list(s.scalars(select(Notification).where(Notification.kind == "odporucanie",
                                                          Notification.created_at >= t1)))
        assert len(again) == n_sent
    set_fake_now(None)


# ====================================================================== [43] avízo pre vlak mimo systému
def test_r43_avizo_pre_nezalozeny_vlak_mozno_potvrdit_znova(env, session, client):
    """[43] Potvrdenie avíza pre vlak, ktorý ešte nie je v systéme: dispečer vidí chybu zápisu, udalosť ostáva vo
    fronte; po založení vlaku ju potvrdí znova a preberanie vznikne."""
    shutil.copy(SAMPLES / "10_avizo_cadca_cz.eml", Path(env.mail_dir) / "10.eml")
    process_mailbox(session)
    (ev,) = events(session, "avizo")
    ev_id, num = ev.id, ev.data["train_number"]
    session.commit()
    login_as(client, "dispecer")
    r = client.post(f"/fronta/{ev_id}/potvrdit", data={"csrf_token": csrf(client, "/fronta"), "akcia": "potvrdit"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/fronta#udalost-{ev_id}"
    page = client.get(r.headers["location"])
    assert "nezapísala" in flash(page) and "nenájdený" in flash(page)
    assert f'id="udalost-{ev_id}"' in page.text and "potvrdené – zápis zlyhal" in page.text
    session.expire_all()
    assert session.get(ExtractedEvent, ev_id).status == "potvrdene"
    session.add(Train(number=num, run_date="2026-11-03", origin="Čadca", destination="Žilina", status="planovany"))
    session.commit()
    r = client.post(f"/fronta/{ev_id}/potvrdit", data={"csrf_token": csrf(client, "/fronta"), "akcia": "potvrdit"},
                    follow_redirects=True)
    assert "Udalosť potvrdená" in flash(r)
    session.expire_all()
    assert session.get(ExtractedEvent, ev_id).status == "aplikovane"
    assert session.query(Handover).count() == 1
    assert f'id="udalost-{ev_id}"' not in client.get("/fronta").text


def test_r43_neuspesne_potvrdenu_udalost_mozno_zamietnut(env, session, client):
    """Potvrdenú udalosť, ktorej zápis zlyhal, môže dispečer aj zamietnuť (zmizne z fronty)."""
    shutil.copy(SAMPLES / "10_avizo_cadca_cz.eml", Path(env.mail_dir) / "10.eml")
    process_mailbox(session)
    (ev,) = events(session, "avizo")
    ev_id = ev.id
    confirm_event(session, ev_id, "dispecer1")
    session.commit()
    login_as(client, "dispecer")
    r = client.post(f"/fronta/{ev_id}/zamietnut", data={"csrf_token": csrf(client, "/fronta"), "dovod": "vlak nejde"},
                    follow_redirects=True)
    assert "zamietnutá" in flash(r)
    session.expire_all()
    assert session.get(ExtractedEvent, ev_id).status == "zamietnute"
