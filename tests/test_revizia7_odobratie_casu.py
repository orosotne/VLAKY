"""Automatický zápis nikdy neodoberie výluke zatvorený čas ani nezmierni obmedzenie (záverečná kontrola).

Opakovane poslaný ROV s kratším termínom či neskorším začiatkom, týždenný prehľad povolených výlukách, ktorý uvádza len
časť (týždeň) viacdennej výluky, pomalá jazda s vyššou rýchlosťou – udalosť s rovnakým číslom a úsekom sa zapisuje do
existujúcej výluky. Bez potvrdenia dispečerom sa taký zápis nesmie vykonať: výluka by sa skrátila a vlak v skutočnom
čase výluky by dostal VYPRAVIŤ ZELENÁ. Do potvrdenia ostáva výluka celá, nový termín je dočasný záznam „navrh“.
Rovnaký či dlhší termín sa zapíše automaticky ako doteraz. Pravidlo platí aj pri zápise udalosti, ktorú prečítala
staršia verzia agenta (obnova po prerušení medzi extrakciou a zápisom), a opakovane poslaná zmena, ktorú register už
obsahuje, nečaká vo fronte s nepravdivým dôvodom.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from vlaky.cas import set_fake_now
from vlaky.hranice import seed_borders
from vlaky.models import Closure, ExtractedEvent

TZ = ZoneInfo("Europe/Bratislava")
UTC = timezone.utc
ZA = "ZSR OR Zilina <vyluky.orza@zsr.example>"
AR = ("Authentication-Results: mx.dopravca.example; spf=pass smtp.mailfrom=zsr.example; dkim=pass\n"
      " header.d=zsr.example; dmarc=pass header.from=zsr.example\n")
A, B = "Ružomberok", "Liptovský Mikuláš"
REMOVAL = "odobral zatvorený čas"


def loc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=TZ).astimezone(UTC)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def _l(dt) -> str:
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
    return dt.astimezone(TZ).strftime("%d.%m. %H:%M")


def _raw(subject: str, body: str, mid: str, date: str) -> bytes:
    hdr = (AR + f"From: {ZA}\nTo: agent.dispecing@dopravca.example\nSubject: {subject}\nDate: {date}\n"
           f"Message-ID: <{mid}@zsr.example>\nMIME-Version: 1.0\nContent-Type: text/plain; charset=\"utf-8\"\n"
           "Content-Transfer-Encoding: 8bit\n")
    return (hdr + "\n" + body).replace("\n", "\r\n").encode("utf-8")


@pytest.fixture()
def env(cfg, session, monkeypatch):
    """Ako v prevádzke (overenie odosielateľa), prah istoty 0,8 – pravidlá (0,85) sú „istá“ udalosť; plánovač sa volá
    priamo (evaluate_train), hromadný prepočet sa vynechá."""
    from vlaky.planovanie import planovac

    monkeypatch.setattr(cfg, "require_dmarc_pass", True)
    monkeypatch.setattr(cfg, "trusted_authserv_ids", "mx.dopravca.example")
    monkeypatch.setattr(cfg, "confirm_threshold", 0.8)
    monkeypatch.setattr(planovac, "evaluate_all", lambda session, now=None, trigger="": [])
    seed_borders(session)
    session.commit()
    return cfg


def _events(session) -> list[ExtractedEvent]:
    return list(session.scalars(select(ExtractedEvent).order_by(ExtractedEvent.id)))


def _live(session) -> list[Closure]:
    return [c for c in session.scalars(select(Closure).order_by(Closure.id)) if c.status != "zrusene"]


def _mail(env, session, name: str, subject: str, body: str, now: datetime, date: str) -> tuple[dict, list]:
    from vlaky.posta.spracovanie import process_mailbox

    before = {e.id for e in _events(session)}
    (Path(env.mail_dir) / name).write_bytes(_raw(subject, body, name.replace(".", "-"), date))
    set_fake_now(now)
    summary = process_mailbox(session, now=now)
    session.commit()
    return summary, [e for e in _events(session) if e.id not in before]


_N = [81000]


def _decide(session, dep: datetime, now: datetime):
    """Nový vlak cez úsek s odchodom ``dep``, vyhodnotený skutočným plánovačom v čase ``now``."""
    from vlaky.agent.zdravie import set_kv
    from vlaky.models import Path as TrainPath, Train
    from vlaky.planovanie.planovac import evaluate_train

    _N[0] += 1
    number = str(_N[0])
    set_fake_now(now)
    t = Train(number=number, origin=A, destination=B, traction="E", gauge="1435", vmax_kmh=80, status="planovany",
              ready_utc=dep, deadline_utc=dep + timedelta(hours=8))
    session.add(t)
    session.flush()
    session.add(TrainPath(train_id=t.id, train_number=number, kind="ad_hoc", status="pridelena", departure_utc=dep))
    session.commit()
    set_kv(session, "mail_last_ok", {"ts": (now - timedelta(minutes=2)).isoformat(), "fetched": 0})
    session.commit()
    return evaluate_train(session, t.id, now, "odobratie času")


def _green(r) -> bool:
    return r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"


def _reasons(ev) -> list:
    return list((ev.validation or {}).get("confirmation_reasons") or [])


ROV_DENNA = ("Dobrý deň,\n\núplná výluka v úseku Ružomberok – Liptovský Mikuláš v dňoch 23.11. – 02.12.2026 denne od "
             "08:00 do 14:00 hod. (ROV č. 2331/2026).\n\nOR Žilina\n")
TYZDEN_48 = ("Dobrý deň,\n\nv 48. týždni (23.11. – 29.11.2026) sú povolené tieto výluky:\n\n1. Ružomberok – Liptovský "
             "Mikuláš, úplná výluka, 23.11. – 29.11.2026 denne od 08:00 do 14:00 hod., ROV č. 2331/2026\n\nOR Žilina\n")
ROV_JEDEN = ("Dobrý deň,\n\núplná výluka v úseku Ružomberok – Liptovský Mikuláš dňa 23.11.2026 od 08:00 do 14:00 hod. "
             "(ROV č. 2331/2026).\n\nOR Žilina\n")


def _rov(env, session, body: str = ROV_JEDEN) -> ExtractedEvent:
    _s, (ev,) = _mail(env, session, "rov.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", body,
                      utc(2026, 11, 16, 8, 2), "Mon, 16 Nov 2026 09:00:00 +0100")
    assert ev.status == "aplikovane" and not ev.needs_confirmation, (ev.status, _reasons(ev))
    return ev


def test_tyzdenny_prehlad_s_castou_vyluky_neskrati_vyluku(env, session):
    """ROV 23.11. – 02.12. denne 08:00 – 14:00 (zapísaný automaticky, neoverený); prehľad 48. týždňa uvádza len
    23.11. – 29.11. Výluka sa nesmie skrátiť: vlak 01.12. o 10:00 nedostane VYPRAVIŤ ZELENÁ."""
    e0 = _rov(env, session, ROV_DENNA)
    s, (ev,) = _mail(env, session, "t48.eml", "Oznámenie o povolených výlukách – 48. týždeň", TYZDEN_48,
                     utc(2026, 11, 17, 8, 2), "Tue, 17 Nov 2026 09:00:00 +0100")
    assert s["auto_applied"] == 0 and s["waiting_confirmation"] == 1, s
    assert ev.status == "navrh" and ev.needs_confirmation and any(REMOVAL in r for r in _reasons(ev)), _reasons(ev)
    rows = _live(session)
    orig = [c for c in rows if c.proposed_change_of is None]
    assert [(c.source_event_id, _l(c.start_utc), _l(c.end_utc)) for c in orig] == \
        [(e0.id, "23.11. 08:00", "02.12. 14:00")], [(c.id, c.start_utc, c.end_utc) for c in rows]
    temp = [c for c in rows if c.proposed_change_of is not None]
    assert len(temp) == 1 and temp[0].proposed_change_of == orig[0].id and _l(temp[0].end_utc) == "29.11. 14:00"
    for now in (loc(2026, 12, 1, 7), loc(2026, 12, 1, 9, 45)):
        r = _decide(session, loc(2026, 12, 1, 10), now)
        assert not _green(r), (r.decision, r.color, r.summary)


@pytest.mark.parametrize("cas", ["od 08:00 do 10:00", "od 08:55 do 14:00"])
def test_opakovany_rov_s_kratsim_terminom_caka_na_dispecera(env, session, cas):
    """Opakovaný ROV s kratším koncom či neskorším začiatkom (do 1 h – tá istá výluka) výluku nezmení sám; vlak
    v odobranom čase nedostane VYPRAVIŤ ZELENÁ."""
    _rov(env, session)
    body = ROV_JEDEN.replace("od 08:00 do 14:00", cas)
    s, (ev,) = _mail(env, session, "rov2.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", body,
                     utc(2026, 11, 17, 8, 2), "Tue, 17 Nov 2026 09:00:00 +0100")
    assert s["auto_applied"] == 0 and ev.needs_confirmation and any(REMOVAL in r for r in _reasons(ev)), _reasons(ev)
    (orig,) = [c for c in _live(session) if c.proposed_change_of is None]
    assert (_l(orig.start_utc), _l(orig.end_utc)) == ("23.11. 08:00", "23.11. 14:00")
    dep = loc(2026, 11, 23, 12, 30) if cas.endswith("10:00") else loc(2026, 11, 23, 8, 15)
    r = _decide(session, dep, dep - timedelta(minutes=15))
    assert not _green(r), (r.decision, r.color, r.summary)


def test_opakovany_rov_s_rovnakym_ci_dlhsim_terminom_sa_zapise_sam(env, session):
    """Rovnaký termín nič nemení, dlhší termín výluke nič neodoberie – zapíšu sa automaticky ako doteraz."""
    _rov(env, session)
    s, (same,) = _mail(env, session, "rov2.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", ROV_JEDEN,
                       utc(2026, 11, 17, 8, 2), "Tue, 17 Nov 2026 09:00:00 +0100")
    assert s["waiting_confirmation"] == 0 and same.status == "aplikovane", (same.status, _reasons(same))
    longer = ROV_JEDEN.replace("od 08:00 do 14:00", "od 08:00 do 16:00")
    s, (ev,) = _mail(env, session, "rov3.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", longer,
                     utc(2026, 11, 17, 9, 2), "Tue, 17 Nov 2026 10:00:00 +0100")
    assert s["auto_applied"] == 1 and ev.status == "aplikovane" and not ev.needs_confirmation, _reasons(ev)
    (c,) = _live(session)
    assert (_l(c.start_utc), _l(c.end_utc), c.source_event_id) == ("23.11. 08:00", "23.11. 16:00", ev.id)


def test_skratenie_potvrdi_alebo_zamietne_dispecer(env, session):
    """Potvrdenie dispečerom výluku skráti (jeho rozhodnutie), zamietnutie ponechá celú výluku bez dočasného záznamu."""
    from vlaky.posta.spracovanie import confirm_event, reject_event

    _rov(env, session)
    body = ROV_JEDEN.replace("od 08:00 do 14:00", "od 08:00 do 10:00")
    _s, (ev,) = _mail(env, session, "rov2.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", body,
                      utc(2026, 11, 17, 8, 2), "Tue, 17 Nov 2026 09:00:00 +0100")
    reject_event(session, ev.id, "dispecer1", reason="prehľad, nie skrátenie")
    session.commit()
    (c,) = _live(session)
    assert (_l(c.start_utc), _l(c.end_utc), c.proposed_change_of) == ("23.11. 08:00", "23.11. 14:00", None)

    _s, (ev2,) = _mail(env, session, "rov3.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", body,
                       utc(2026, 11, 17, 9, 2), "Tue, 17 Nov 2026 10:00:00 +0100")
    assert ev2.needs_confirmation and any(REMOVAL in r for r in _reasons(ev2))
    confirm_event(session, ev2.id, "dispecer1")
    session.commit()
    (c,) = _live(session)
    assert (_l(c.start_utc), _l(c.end_utc), c.status) == ("23.11. 08:00", "23.11. 10:00", "overene")


def test_pomala_jazda_s_vyssou_rychlostou_caka_na_dispecera(env, session):
    """Pomalá jazda 30 km/h, opakované oznámenie s 60 km/h (miernejšie obmedzenie) – rýchlosť sa sama nezvýši."""
    body = ("Dobrý deň,\n\npomalá jazda rýchlosťou 30 km/h v úseku Ružomberok – Liptovský Mikuláš dňa 23.11.2026 od "
            "08:00 do 14:00 hod. (ROV č. 2400/2026).\n\nOR Žilina\n")
    _s, (e0,) = _mail(env, session, "pj.eml", "ROV c. 2400/2026 pomala jazda", body, utc(2026, 11, 16, 8, 2),
                      "Mon, 16 Nov 2026 09:00:00 +0100")
    if e0.kind != "pomala_jazda" or e0.status != "aplikovane":
        pytest.skip(f"pravidlá prečítali e-mail inak: {e0.kind} {e0.status}")
    s, (ev,) = _mail(env, session, "pj2.eml", "ROV c. 2400/2026 pomala jazda", body.replace("30 km/h", "60 km/h"),
                     utc(2026, 11, 17, 8, 2), "Tue, 17 Nov 2026 09:00:00 +0100")
    assert s["auto_applied"] == 0 and ev.needs_confirmation, (s, _reasons(ev))
    assert any(REMOVAL in r and "rýchlosť" in r for r in _reasons(ev)), _reasons(ev)
    (orig,) = [c for c in _live(session) if c.proposed_change_of is None]
    assert float(orig.speed_kmh) == 30.0


def test_zmena_z_predchadzajucej_verzie_pri_obnove_nezapise_presun(env, session, monkeypatch):
    """Udalosť, ktorú prečítala staršia verzia agenta (zmena bez povinného potvrdenia) a pred zápisom sa agent
    zastavil (aktualizácia): obnova pri ďalšom cykle ju nesmie zapísať – presun by výluke odobral čas."""
    from vlaky.posta import spracovanie, validacia

    _rov(env, session)
    # staršia verzia: presun sa nepotvrdzoval; agent sa zastavil hneď po uložení udalosti (pred zápisom)
    monkeypatch.setattr(validacia, "ADDITIVE_INTENTS", ("doplnenie", "predlzenie", "presun"))
    monkeypatch.setattr(spracovanie, "_auto_apply", lambda session, ev, summary, applied: None)
    body = ("Dobrý deň,\n\nvýluka v úseku Ružomberok – Liptovský Mikuláš podľa ROV č. 2331/2026 sa presúva z 23.11.2026 "
            "na 25.11.2026, od 08:00 do 14:00 hod.\n\nOR Žilina\n")
    _s, (ev,) = _mail(env, session, "z.eml", "Zmena ROV č. 2331/2026", body, utc(2026, 11, 17, 8, 2),
                      "Tue, 17 Nov 2026 09:00:00 +0100")
    assert ev.kind == "vyluka_zmena" and ev.status == "navrh" and not ev.needs_confirmation, (ev.kind, _reasons(ev))
    monkeypatch.undo()
    monkeypatch.setattr(env, "require_dmarc_pass", True)
    monkeypatch.setattr(env, "trusted_authserv_ids", "mx.dopravca.example")
    monkeypatch.setattr(env, "confirm_threshold", 0.8)
    from vlaky.planovanie import planovac

    monkeypatch.setattr(planovac, "evaluate_all", lambda session, now=None, trigger="": [])
    s, _new = _mail(env, session, "nic.eml", "Informácia", "Dobrý deň,\n\nbez zmien.\n\nOR Žilina\n",
                    utc(2026, 11, 17, 9, 2), "Tue, 17 Nov 2026 10:00:00 +0100")
    session.refresh(ev)
    assert ev.id in s["recovered_event_ids"] and ev.status == "navrh" and ev.needs_confirmation, (ev.status, s)
    assert any(REMOVAL in r for r in _reasons(ev)), _reasons(ev)
    (orig,) = [c for c in _live(session) if c.proposed_change_of is None]
    assert (_l(orig.start_utc), _l(orig.end_utc)) == ("23.11. 08:00", "23.11. 14:00")
    r = _decide(session, loc(2026, 11, 23, 10), loc(2026, 11, 23, 9, 45))
    assert not _green(r), (r.decision, r.color, r.summary)


def test_opakovana_zmena_po_potvrdeni_necaka_s_nepravdivym_dovodom(env, session):
    """Zmenu (presun) potvrdí dispečer; ten istý e-mail príde znova (iný distribučný zoznam) – register ho už obsahuje,
    nič neodoberie, preto sa zapíše bez fronty (nie s dôvodom „môže odobrať zatvorený čas“)."""
    from vlaky.posta.spracovanie import confirm_event
    from vlaky.posta.validacia import CHANGE_REMOVES_TIME

    _rov(env, session)
    body = ("Dobrý deň,\n\nvýluka v úseku Ružomberok – Liptovský Mikuláš podľa ROV č. 2331/2026 sa presúva z 23.11.2026 "
            "na 25.11.2026, od 08:00 do 14:00 hod.\n\nOR Žilina\n")
    _s, (ev,) = _mail(env, session, "z.eml", "Zmena ROV č. 2331/2026", body, utc(2026, 11, 17, 8, 2),
                      "Tue, 17 Nov 2026 09:00:00 +0100")
    assert ev.needs_confirmation and CHANGE_REMOVES_TIME in _reasons(ev), _reasons(ev)
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    (c,) = _live(session)
    assert (_l(c.start_utc), _l(c.end_utc)) == ("25.11. 08:00", "25.11. 14:00")
    s, (again,) = _mail(env, session, "z2.eml", "Zmena ROV č. 2331/2026", body, utc(2026, 11, 17, 9, 2),
                        "Tue, 17 Nov 2026 10:00:00 +0100")
    assert CHANGE_REMOVES_TIME not in _reasons(again) and s["waiting_confirmation"] == 0, (s, _reasons(again))
    (c,) = _live(session)
    assert (_l(c.start_utc), _l(c.end_utc)) == ("25.11. 08:00", "25.11. 14:00")
