"""Automatický zápis nikdy neodoberie výluke zatvorený čas ani nezmierni obmedzenie (záverečná kontrola).

Opakovane poslaný ROV s kratším termínom či neskorším začiatkom, týždenný prehľad povolených výlukách, ktorý uvádza len
časť (týždeň) viacdennej výluky, pomalá jazda s vyššou rýchlosťou – udalosť s rovnakým číslom a úsekom sa zapisuje do
existujúcej výluky. Bez potvrdenia dispečerom sa taký zápis nesmie vykonať: výluka by sa skrátila a vlak v skutočnom
čase výluky by dostal VYPRAVIŤ ZELENÁ. Do potvrdenia ostáva výluka celá, nový termín je dočasný záznam „navrh“.
Rovnaký či dlhší termín sa zapíše automaticky ako doteraz. Pravidlo platí aj pri zápise udalosti, ktorú prečítala
staršia verzia agenta (obnova po prerušení medzi extrakciou a zápisom).

Zápis, ktorý nič nezmenil (opakovaný ROV, prehľad s tým istým termínom), sa nestane „novším“ zapisovateľom výluky:
inak by potvrdenie čakajúcej zmeny (presun, predĺženie, doplnenie) skončilo ako „nahradené“ a jej dočasný záznam by
sa zahodil – plánovač by čas, s ktorým počítal, považoval za voľný. Ak výluku medzitým naozaj zmenila novšia udalosť,
potvrdený termín, ktorý výluka nemá, sa zapíše ako samostatná výluka (prísnejší prípad).
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


def _decide(session, dep: datetime, now: datetime, origin: str = A, destination: str = B, traction: str = "E",
            gauge: str = "1435"):
    """Nový vlak cez úsek s odchodom ``dep``, vyhodnotený skutočným plánovačom v čase ``now``."""
    from vlaky.agent.zdravie import set_kv
    from vlaky.models import Path as TrainPath, Train
    from vlaky.planovanie.planovac import evaluate_train

    _N[0] += 1
    number = str(_N[0])
    set_fake_now(now)
    t = Train(number=number, origin=origin, destination=destination, traction=traction, gauge=gauge, vmax_kmh=80,
              status="planovany", ready_utc=dep, deadline_utc=dep + timedelta(hours=8))
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
    assert any("zmiernil obmedzenie" in r and "rýchlosť 30 km/h → 60 km/h" in r for r in _reasons(ev)), _reasons(ev)
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


def test_opakovana_zmena_po_potvrdeni_caka_s_upozornenim(env, session):
    """Zmenu (presun) potvrdí dispečer; ten istý e-mail príde znova (iný distribučný zoznam) – čaká ako každá zmena
    výluky, fronta však povie, že register termín už má (zmena nič nezmení)."""
    from vlaky.posta.spracovanie import confirm_event
    from vlaky.posta.validacia import CHANGE_REMOVES_TIME

    _rov(env, session)
    _s, (ev,) = _mail(env, session, "z.eml", "Zmena ROV č. 2331/2026", PRESUN, utc(2026, 11, 17, 8, 2),
                      "Tue, 17 Nov 2026 09:00:00 +0100")
    assert ev.needs_confirmation and CHANGE_REMOVES_TIME in _reasons(ev), _reasons(ev)
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    _s, (again,) = _mail(env, session, "z2.eml", "Zmena ROV č. 2331/2026", PRESUN, utc(2026, 11, 17, 9, 2),
                         "Tue, 17 Nov 2026 10:00:00 +0100")
    warnings = (again.validation or {}).get("warnings") or []
    assert again.needs_confirmation and any("už má nový termín" in w for w in warnings), warnings
    (c,) = [x for x in _live(session) if x.proposed_change_of is None]
    assert (_l(c.start_utc), _l(c.end_utc)) == ("25.11. 08:00", "25.11. 14:00")


PRESUN = ("Dobrý deň,\n\nvýluka v úseku Ružomberok – Liptovský Mikuláš podľa ROV č. 2331/2026 sa presúva z 23.11.2026 "
          "na 25.11.2026, od 08:00 do 14:00 hod.\n\nOR Žilina\n")
ZMENY = {
    "presun": (PRESUN, (2026, 11, 25, 10, 0)),
    "predlzenie": ("Dobrý deň,\n\nvýluka v úseku Ružomberok – Liptovský Mikuláš podľa ROV č. 2331/2026 sa predlžuje do "
                   "24.11.2026 14:00 hod.\n\nOR Žilina\n", (2026, 11, 24, 10, 0)),
    "doplnenie": ("Dobrý deň,\n\nvýluka v úseku Ružomberok – Liptovský Mikuláš podľa ROV č. 2331/2026 sa dopĺňa o ďalší "
                  "termín 26.11.2026 od 08:00 do 14:00 hod.\n\nOR Žilina\n", (2026, 11, 26, 10, 0)),
}
TYZDEN_STARY = ("Dobrý deň,\n\nv 48. týždni (23.11. – 29.11.2026) sú povolené tieto výluky:\n\n1. Ružomberok – "
                "Liptovský Mikuláš, úplná výluka, 23.11.2026 od 08:00 do 14:00 hod., ROV č. 2331/2026\n\nOR Žilina\n")


@pytest.mark.parametrize("zmena", list(ZMENY))
@pytest.mark.parametrize("treti", ["opakovany_rov", "tyzdenny_prehlad"])
def test_zapis_bez_zmeny_nenahradi_cakajucu_zmenu(env, session, zmena, treti):
    """ROV (zapísaný sám) → zmena čaká s dočasným záznamom → opakovaný ROV či prehľad s pôvodným termínom (zápis bez
    zmeny) → dispečer zmenu potvrdí: musí sa zapísať (nie „nahradené“), čas zmeny ostáva uzavretý."""
    from vlaky.posta.spracovanie import confirm_event

    _rov(env, session)
    body, dep_t = ZMENY[zmena]
    _s, (ev,) = _mail(env, session, "z.eml", "Zmena ROV č. 2331/2026", body, utc(2026, 11, 17, 8, 2),
                      "Tue, 17 Nov 2026 09:00:00 +0100")
    assert ev.kind == "vyluka_zmena" and ev.needs_confirmation, (ev.kind, _reasons(ev))
    dep = loc(*dep_t)
    assert not _green(_decide(session, dep, dep - timedelta(minutes=15)))
    if treti == "opakovany_rov":
        _s, (noop,) = _mail(env, session, "rov2.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", ROV_JEDEN,
                            utc(2026, 11, 17, 9, 2), "Tue, 17 Nov 2026 10:00:00 +0100")
    else:
        _s, (noop,) = _mail(env, session, "t48.eml", "Oznámenie o povolených výlukách – 48. týždeň", TYZDEN_STARY,
                            utc(2026, 11, 17, 9, 2), "Tue, 17 Nov 2026 10:00:00 +0100")
    assert noop.status == "aplikovane", (noop.status, _reasons(noop))
    set_fake_now(utc(2026, 11, 17, 10, 2))
    ev = confirm_event(session, ev.id, "dispecer1")
    session.commit()
    result = (ev.validation or {}).get("apply_result") or {}
    assert result.get("action") != "nahradene", result
    r = _decide(session, dep, dep - timedelta(minutes=15))
    assert not _green(r), (r.decision, r.color, r.summary)


def test_novsia_dlhsia_vyluka_nezahodi_potvrdeny_presun(env, session):
    """ROV 23.11. 08–14 (zapísaný sám) → presun na 25.11. čaká → opakovaný ROV s dlhším časom 23.11. 08–16 sa zapíše
    sám (pridáva čas) → dispečer potvrdí presun: novšia udalosť výluku zmenila, termín presunu (25.11.) však výluka
    nemá – zapíše sa ako samostatná výluka, 25.11. ostáva uzavretý."""
    from vlaky.posta.spracovanie import confirm_event

    _rov(env, session)
    _s, (ev,) = _mail(env, session, "z.eml", "Zmena ROV č. 2331/2026", PRESUN, utc(2026, 11, 17, 8, 2),
                      "Tue, 17 Nov 2026 09:00:00 +0100")
    longer = ROV_JEDEN.replace("od 08:00 do 14:00", "od 08:00 do 16:00")
    _s, (re2,) = _mail(env, session, "rov2.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", longer,
                       utc(2026, 11, 17, 9, 2), "Tue, 17 Nov 2026 10:00:00 +0100")
    assert re2.status == "aplikovane" and not re2.needs_confirmation, _reasons(re2)
    set_fake_now(utc(2026, 11, 17, 10, 2))
    ev = confirm_event(session, ev.id, "dispecer1")
    session.commit()
    result = (ev.validation or {}).get("apply_result") or {}
    assert result.get("action") == "vytvorene" and result.get("superseded_by") == re2.id, result
    terms = sorted((_l(c.start_utc), _l(c.end_utc), c.proposed_change_of) for c in _live(session))
    assert terms == [("23.11. 08:00", "23.11. 16:00", None), ("25.11. 08:00", "25.11. 14:00", None)], terms
    dep = loc(2026, 11, 25, 10)
    r = _decide(session, dep, dep - timedelta(minutes=15))
    assert not _green(r), (r.decision, r.color, r.summary)


ROV_SRT = ("Dobrý deň,\n\núplná výluka v úseku Slivník ŠRT – Budkovce ŠRT dňa 23.11.2026 od 08:00 do 14:00 hod. "
           "(ROV č. 2331/2026).\n\nOR Košice\n")


def test_doplnene_cislo_trate_neodoberie_rozchod(env, session):
    """Úsek oboch rozchodov (Slivník ŠRT – Budkovce ŠRT) bez čísla trate uzatvára 1435 aj 1520 mm; opakovaný ROV s „(trať
    190)“ by ponechal len 1435 mm – sám sa nezapíše a vlak širokého rozchodu nedostane VYPRAVIŤ ZELENÁ."""
    _s, (e0,) = _mail(env, session, "s0.eml", "ROV c. 2331/2026 Slivnik SRT - Budkovce SRT", ROV_SRT,
                      utc(2026, 11, 16, 8, 2), "Mon, 16 Nov 2026 09:00:00 +0100")
    if e0.status != "aplikovane":
        pytest.skip(f"úsek sa nezapísal sám: {e0.status} {_reasons(e0)}")
    with_line = ROV_SRT.replace("Budkovce ŠRT dňa", "Budkovce ŠRT (trať 190) dňa")
    s, (ev,) = _mail(env, session, "s1.eml", "ROV c. 2331/2026 Slivnik SRT - Budkovce SRT", with_line,
                     utc(2026, 11, 17, 8, 2), "Tue, 17 Nov 2026 09:00:00 +0100")
    assert s["auto_applied"] == 0 and any("úsek či trať" in r for r in _reasons(ev)), (s, _reasons(ev))
    dep = loc(2026, 11, 23, 10)
    r = _decide(session, dep, dep - timedelta(minutes=15), "Haniska pri Košiciach", "Maťovce ŠRT", "M", "1520")
    assert not _green(r), (r.decision, r.color, r.summary)


# ---------------------------------------------------------------------- _removal_reason (jednotkovo)
def _closure(**kw) -> Closure:
    base = dict(id=1, kind="uplna", from_station=A, to_station=B, line_ref="", track="", start_utc=loc(2026, 11, 23, 8),
                end_utc=loc(2026, 11, 23, 14), daily_from="", daily_to="", electric_only=False, speed_kmh=None,
                external_ref="2331/2026", until_revoked=False)
    base.update(kw)
    return Closure(**base)


def _fields(c: Closure, **kw) -> dict:
    from vlaky.posta.spracovanie import _closure_fields_of

    out = _closure_fields_of(c)
    out.update(kw)
    return out


def test_odobratie_casu_presne_podla_planovaca():
    from vlaky.posta.spracovanie import _removal_reason

    full = _closure()
    # koľaj pri úplnej výluke plánovač nepoužíva – „obe“ ani iné označenie nie je zmiernenie
    assert _removal_reason(full, _fields(full, track="obe")) == ""
    track = _closure(kind="kolaj", track="1")
    assert _removal_reason(track, _fields(track, track="1. TK")) == ""
    assert _removal_reason(track, _fields(track, track="obe")) == ""
    assert _removal_reason(track, _fields(track, track="1 a 2")) == ""
    assert "koľaj 1 → 2" in _removal_reason(track, _fields(track, track="2"))
    unknown = _closure(kind="kolaj", track="")
    assert "koľaj neuvedená → 1" in _removal_reason(unknown, _fields(unknown, track="1"))
    # do odvolania: technický koniec sa neporovnáva – skorší začiatok či zmena na „do odvolania“ čas neodoberú
    open_end = _closure(until_revoked=True, end_utc=loc(2026, 12, 23, 8))
    assert _removal_reason(open_end, _fields(open_end, start_utc=loc(2026, 11, 23, 7, 30),
                                             end_utc=loc(2026, 12, 20, 8))) == ""
    assert _removal_reason(full, _fields(full, until_revoked=True, end_utc=loc(2026, 11, 23, 12))) == ""
    assert "odobral zatvorený čas" in _removal_reason(open_end, _fields(open_end, until_revoked=False,
                                                                        end_utc=loc(2026, 11, 30, 8)))
    # priveľa denných okien (dlhá denná výluka): predĺženie obdobia nič neodoberie, kratšie obdobie áno
    long = _closure(start_utc=loc(2027, 1, 1, 8), end_utc=loc(2028, 3, 31, 14), daily_from="08:00", daily_to="14:00")
    assert _removal_reason(long, _fields(long, end_utc=loc(2028, 4, 30, 14))) == ""
    assert "odobral zatvorený čas" in _removal_reason(long, _fields(long, end_utc=loc(2028, 2, 28, 14)))
    # text dôvodu uvádza, čo sa zmierňuje
    slow = _closure(kind="pomala_jazda", speed_kmh=30)
    why = _removal_reason(slow, _fields(slow, speed_kmh=60))
    assert why.startswith("zápis by zmiernil obmedzenie výluky #1") and "rýchlosť 30 km/h → 60 km/h" in why, why
    track_both = _closure(kind="kolaj", track="obe")
    assert "koľaj obe → neuvedená" in _removal_reason(track_both, _fields(track_both, track=""))


# ---------------------------------------------------------------------- druhé kolo kontroly
ZRUSENIE_DNA = ("Dobrý deň,\n\nvýluka v úseku Ružomberok – Liptovský Mikuláš podľa ROV č. 2331/2026 dňa 23.11.2026 sa "
                "ruší.\n\nOR Žilina\n")
DOPLNENIE = ZMENY["doplnenie"][0]


def test_novsi_opakovany_rov_chrani_vyluku_pred_starsim_zrusenim(env, session):
    """ROV (sám) → zrušenie čaká → ten istý ROV znova (zápis bez zmeny) → dispečer potvrdí staršie zrušenie: novšia
    správa výluku znova ohlásila – zrušenie sa nezapíše (nahradené), výluka ostáva, vlak 23.11. nie je ZELENÝ."""
    from vlaky.posta.spracovanie import confirm_event

    _rov(env, session)
    _s, (cancel,) = _mail(env, session, "zr.eml", "Zrusenie ROV c. 2331/2026", ZRUSENIE_DNA, utc(2026, 11, 17, 8, 2),
                          "Tue, 17 Nov 2026 09:00:00 +0100")
    assert cancel.kind == "vyluka_zrusenie" and cancel.needs_confirmation
    _s, (noop,) = _mail(env, session, "rov2.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", ROV_JEDEN,
                        utc(2026, 11, 17, 9, 2), "Tue, 17 Nov 2026 10:00:00 +0100")
    assert noop.status == "aplikovane"
    set_fake_now(utc(2026, 11, 17, 10, 2))
    cancel = confirm_event(session, cancel.id, "dispecer1")
    session.commit()
    assert (cancel.validation or {}).get("apply_result", {}).get("action") == "nahradene", cancel.validation
    (c,) = _live(session)
    assert (_l(c.start_utc), _l(c.end_utc)) == ("23.11. 08:00", "23.11. 14:00")
    r = _decide(session, loc(2026, 11, 23, 10), loc(2026, 11, 23, 9, 45))
    assert not _green(r), (r.decision, r.color, r.summary)


def test_novsi_opakovany_rov_chrani_vyluku_pred_starsim_skratenim(env, session):
    """Kratší opakovaný ROV (08–10) čaká → plný ROV znova → dispečer potvrdí kratší: nahradený, výluka 08–14 ostáva."""
    from vlaky.posta.spracovanie import confirm_event

    _rov(env, session)
    short = ROV_JEDEN.replace("od 08:00 do 14:00", "od 08:00 do 10:00")
    _s, (ev,) = _mail(env, session, "rov2.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", short,
                      utc(2026, 11, 17, 8, 2), "Tue, 17 Nov 2026 09:00:00 +0100")
    _mail(env, session, "rov3.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", ROV_JEDEN,
          utc(2026, 11, 17, 9, 2), "Tue, 17 Nov 2026 10:00:00 +0100")
    set_fake_now(utc(2026, 11, 17, 10, 2))
    ev = confirm_event(session, ev.id, "dispecer1")
    session.commit()
    assert (ev.validation or {}).get("apply_result", {}).get("action") == "nahradene", ev.validation
    (c,) = [x for x in _live(session) if x.proposed_change_of is None]
    assert (_l(c.start_utc), _l(c.end_utc)) == ("23.11. 08:00", "23.11. 14:00")


def test_samostatna_vyluka_z_potvrdenych_udajov_s_upravou_dispecera(env, session):
    """Presun čaká → dlhší ROV (novšia udalosť) → dispečer zmenu opraví (koniec 16:00) a potvrdí: samostatná výluka má
    opravený termín, nie údaje z e-mailu."""
    from vlaky.posta.spracovanie import confirm_event

    _rov(env, session)
    _s, (ev,) = _mail(env, session, "z.eml", "Zmena ROV č. 2331/2026", PRESUN, utc(2026, 11, 17, 8, 2),
                      "Tue, 17 Nov 2026 09:00:00 +0100")
    longer = ROV_JEDEN.replace("od 08:00 do 14:00", "od 08:00 do 16:00")
    _mail(env, session, "rov2.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", longer, utc(2026, 11, 17, 9, 2),
          "Tue, 17 Nov 2026 10:00:00 +0100")
    set_fake_now(utc(2026, 11, 17, 10, 2))
    ev = confirm_event(session, ev.id, "dispecer1", edits={"end": loc(2026, 11, 25, 16).isoformat()})
    session.commit()
    result = (ev.validation or {}).get("apply_result") or {}
    assert result.get("action") == "vytvorene", result
    row = session.get(Closure, result["id"])
    assert (_l(row.start_utc), _l(row.end_utc), row.status) == ("25.11. 08:00", "25.11. 16:00", "overene")
    r = _decide(session, loc(2026, 11, 25, 15), loc(2026, 11, 25, 14, 45))
    assert not _green(r), (r.decision, r.color, r.summary)


def test_zrusenie_povodneho_dna_nezahodi_cakajuce_doplnenie(env, session):
    """ROV 23.11. → doplnenie 26.11. čaká → zrušenie len 23.11. (potvrdené): termín doplnenia zrušenie nepokrýva – ostáva
    pre plánovač, kým čaká, a po potvrdení sa zapíše ako samostatná výluka."""
    from vlaky.posta.spracovanie import confirm_event

    _rov(env, session)
    _s, (dopl,) = _mail(env, session, "d.eml", "Zmena ROV č. 2331/2026", DOPLNENIE, utc(2026, 11, 17, 8, 2),
                        "Tue, 17 Nov 2026 09:00:00 +0100")
    _s, (cancel,) = _mail(env, session, "zr.eml", "Zrusenie ROV c. 2331/2026", ZRUSENIE_DNA, utc(2026, 11, 17, 9, 2),
                          "Tue, 17 Nov 2026 10:00:00 +0100")
    set_fake_now(utc(2026, 11, 17, 10, 2))
    confirm_event(session, cancel.id, "dispecer1")
    session.commit()
    dep = loc(2026, 11, 26, 10)
    r = _decide(session, dep, dep - timedelta(minutes=15))
    assert not _green(r), (r.decision, r.color, r.summary)          # doplnenie ešte čaká
    set_fake_now(utc(2026, 11, 17, 11, 2))
    dopl = confirm_event(session, dopl.id, "dispecer1")
    session.commit()
    result = (dopl.validation or {}).get("apply_result") or {}
    assert result.get("action") == "vytvorene", result
    r = _decide(session, dep, dep - timedelta(minutes=15))
    assert not _green(r), (r.decision, r.color, r.summary)


def test_zmena_pred_rov_nezahodi_svoj_termin(env, session):
    """Doplnenie (s číslom ROV) príde skôr ako ROV – čaká so svojím návrhom; ROV potom príde a zapíše sa sám; dispečer
    potvrdí doplnenie: jeho termín (26.11.) sa nezahodí."""
    from vlaky.posta.spracovanie import confirm_event

    _s, (dopl,) = _mail(env, session, "d.eml", "Zmena ROV č. 2331/2026", DOPLNENIE, utc(2026, 11, 16, 7, 2),
                        "Mon, 16 Nov 2026 08:00:00 +0100")
    _mail(env, session, "rov.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", ROV_JEDEN, utc(2026, 11, 16, 8, 2),
          "Mon, 16 Nov 2026 09:00:00 +0100")
    set_fake_now(utc(2026, 11, 16, 9, 2))
    confirm_event(session, dopl.id, "dispecer1")
    session.commit()
    dep = loc(2026, 11, 26, 10)
    r = _decide(session, dep, dep - timedelta(minutes=15))
    assert not _green(r), (r.decision, r.color, r.summary)


def test_novsie_cakajuce_rovnake_oznamenie_neostane_neviditelne(env, session):
    """ROV 23.11. (sám) → presun na 25.11. čaká → ten istý ROV znova do 72 h (čaká, výluka ho obsahuje – bez záznamu) →
    dispečer potvrdí starší presun: novšie čakajúce oznámenie 23.11. dostane záznam, vlak 23.11. nie je ZELENÝ."""
    from vlaky.posta.spracovanie import confirm_event

    _rov(env, session)
    _s, (move,) = _mail(env, session, "z.eml", "Zmena ROV č. 2331/2026", PRESUN, utc(2026, 11, 20, 8, 2),
                        "Fri, 20 Nov 2026 09:00:00 +0100")
    _s, (again,) = _mail(env, session, "rov2.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", ROV_JEDEN,
                         utc(2026, 11, 21, 8, 2), "Sat, 21 Nov 2026 09:00:00 +0100")
    assert again.needs_confirmation, _reasons(again)
    set_fake_now(utc(2026, 11, 21, 9, 2))
    confirm_event(session, move.id, "dispecer1")
    session.commit()
    r = _decide(session, loc(2026, 11, 23, 10), loc(2026, 11, 23, 9, 45))
    assert not _green(r), (r.decision, r.color, r.summary)


def test_vyluka_do_odvolania_je_otvorena_aj_po_technickom_konci(env, session):
    """Overená výluka do odvolania: plánovač ju počíta aj pre vlak o 60 dní (technický koniec je o 30 dní); opakované
    oznámenie „do odvolania“ neskráti dlhší pevný koniec výluky."""
    from vlaky.planovanie.planovac import closure_windows

    c = Closure(kind="uplna", from_station=A, to_station=B, line_ref="", track="", start_utc=loc(2026, 11, 23, 8),
                end_utc=loc(2026, 12, 23, 8), daily_from="", daily_to="", electric_only=False, speed_kmh=None,
                external_ref="2331/2026", until_revoked=True, status="overene", source="email")
    session.add(c)
    session.commit()
    far = loc(2027, 1, 20, 10)
    wins = closure_windows(session, far - timedelta(hours=2), far + timedelta(hours=6))
    assert any(w.closure_id == c.id and w.start <= far <= w.end for w in wins), wins
    r = _decide(session, far, loc(2026, 11, 21, 9))
    assert not _green(r), (r.decision, r.color, r.summary)
    # pevný dlhý koniec + opakované oznámenie „do odvolania“ – koniec sa neskráti na technický
    long_rov = ROV_JEDEN.replace("dňa 23.11.2026 od 08:00 do 14:00 hod.", "od 23.11.2026 08:00 hod. do 31.03.2027 14:00 hod.")
    _s, (e0,) = _mail(env, session, "r.eml", "ROV c. 2400/2026 Ruzomberok - Liptovsky Mikulas",
                      long_rov.replace("2331/2026", "2400/2026"), utc(2026, 11, 16, 8, 2), "Mon, 16 Nov 2026 09:00:00 +0100")
    assert e0.status == "aplikovane", (e0.status, _reasons(e0))
    open_rov = ROV_JEDEN.replace("dňa 23.11.2026 od 08:00 do 14:00 hod.", "od 23.11.2026 08:00 hod. do odvolania")
    _mail(env, session, "r2.eml", "ROV c. 2400/2026 Ruzomberok - Liptovsky Mikulas", open_rov.replace("2331/2026", "2400/2026"),
          utc(2026, 11, 17, 8, 2), "Tue, 17 Nov 2026 09:00:00 +0100")
    rows = [x for x in _live(session) if x.external_ref and "2400" in x.external_ref and x.proposed_change_of is None]
    assert rows and all(_l(x.end_utc) == "31.03. 14:00" for x in rows), [(x.id, x.end_utc, x.until_revoked) for x in rows]
