"""6. revízia (strana FB, oprava po kontrole): zrušenie výluky – poistka zrušenia bez cieľa bez nadbytočných blokovaní,
OR preposlaného obsahu, zrušenie časti dlhej výluky bez čísla.

- Opakované zrušenie už zrušenej výluky (znova poslané, preposlané kontrolným dispečerom, po zrušení dispečerom
  v registri výluk) nie je „zrušením bez cieľa“: žiadny záznam poistky, žiadne upozornenie, vlaky ostávajú ZELENÉ;
  vo fronte upozornenie „je v registri už zrušená“, potvrdenie register nezmení.
- Odpoveď „Ďakujeme za informáciu“ s citovaným zrušením nie je novým oznámením (poistka nevznikne).
- „Do odvolania“ v inej vete (náhradná autobusová doprava) poistku zrušenia nepredĺži na mesiac a skutočné zrušenie
  výluky s pevným koncom neodmietne.
- Poistka zrušenia bez konca zmenená dispečerom na novú výluku sa zapíše ako výluka do odvolania.
- Zrušenie jedného dňa dlhej výluky bez čísla („dňa 15.12. sa nekoná“) nájde výluku, ktorej obdobie deň obsahuje.
- Preposlaný e-mail vydala OR preposlaného obsahu (nie OR preposielateľa s adresou kd.orza@): STORNO ROV 15/2026 OR
  Košice nezruší ROV 15/2026 OR Žilina.

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from vlaky.models import Closure, Email, Notification
from test_revizia3_terminy import as_utc, drop, events, raw_mail, utc
from test_revizia4_pravidla import _decide, _process, env  # noqa: F401 – fixture env

ROV_ZA = "ZSR OR Zilina <vyluky.orza@zsr.example>"
ROV_TT = "ZSR OR Trnava <vyluky.ortt@zsr.example>"
KD_ZA = "Kontrolny dispecer OR Zilina <kd.orza@zsr.example>"
TZ = ZoneInfo("Europe/Bratislava")


def loc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=TZ).astimezone(timezone.utc)


def _confirm(session, ev_id: int, **edits) -> dict:
    from vlaky.posta.spracovanie import confirm_event

    ev = confirm_event(session, ev_id, "dispecer1", edits=edits or None)
    session.commit()
    return (ev.validation or {}).get("apply_result") or {}


def _mails(env, session, name: str, subject: str, body: str, sender: str, date: str, now) -> list:
    before = {e.id for e in events(session)}
    drop(env, name, raw_mail(subject, body, name.replace(".", "-"), sender=sender, date=date))
    _process(session, now)
    return [e for e in events(session) if e.id not in before]


def _register(env, session, *a) -> list:
    new = _mails(env, session, *a)
    for e in new:
        _confirm(session, e.id)
    return new


def _green(r) -> bool:
    return r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"


def _nets(session) -> list[Closure]:
    return [c for c in session.scalars(select(Closure)) if c.source == "email_poistka" and c.status != "zrusene"]


def _alerts(session) -> list[str]:
    return [s for (s,) in session.execute(select(Notification.subject)
                                          .where(Notification.subject.contains("Na potvrdenie čaká")))]


def _warnings(ev) -> str:
    return " | ".join(ev.validation.get("warnings") or [])


_TL_ROV = ("Dobrý deň,\n\núplná výluka v úseku Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 14:00 hod. "
           "(ROV č. {ref}).\n\nOR Trnava\n")
_TL_CANCEL = "Dobrý deň,\n\nvýluka podľa ROV č. {ref} v úseku Trnava – Leopoldov dňa 10.12.2026 sa ruší.\n\nOR Trnava\n"


def _seed_tl(env, session, ref: str) -> Closure:
    _register(env, session, "r.eml", f"ROV č. {ref} Trnava – Leopoldov", _TL_ROV.format(ref=ref), ROV_TT,
              "Fri, 20 Nov 2026 09:00:00 +0100", utc(2026, 11, 20, 8, 2))
    (c,) = [c for c in session.scalars(select(Closure)) if c.status != "zrusene"]
    return c


# ====================================================================== opakované zrušenie
@pytest.mark.parametrize("how", ["znova_poslane", "preposlane_kd", "zrusena_v_registri"])
def test_opakovane_zrusenie_zrusenej_vyluky_nie_je_poistkou(env, session, how):
    """Výluka ROV 3906/2026 Trnava – Leopoldov 10.12. 08:00 – 14:00 je už zrušená (potvrdené zrušenie, alebo ju zrušil
    dispečer v registri výluk); znova poslané či kontrolným dispečerom preposlané zrušenie nie je zrušením bez cieľa:
    žiadny záznam poistky ani upozornenie DÔLEŽITÉ, vlak 10.12. o 10:00 ostáva VYPRAVIŤ ZELENÁ, vo fronte „je v registri
    už zrušená“ a Potvrdiť register nezmení (v e0a3d0a celodenná poistka 10.12. – vlak ESKALOVAŤ ČERVENÁ a upozornenia
    KRITICKÉ pre vlaky v zrušenej výluke)."""
    ref = "3906/2026"
    c = _seed_tl(env, session, ref)
    if how == "zrusena_v_registri":
        c.status = "zrusene"
        c.version = (c.version or 1) + 1
        session.commit()
    else:
        (first,) = _mails(env, session, "z1.eml", f"Zrušenie výluky – ROV č. {ref}", _TL_CANCEL.format(ref=ref), ROV_TT,
                          "Sat, 21 Nov 2026 09:00:00 +0100", utc(2026, 11, 21, 8, 2))
        assert _confirm(session, first.id).get("ok") is True
    session.expire_all()
    assert session.get(Closure, c.id).status == "zrusene"
    alerts_before = len(_alerts(session))
    if how == "preposlane_kd":
        body = ("Preposielam na vedomie.\n\n---------- Forwarded message ---------\nOd: ZSR OR Trnava "
                "<vyluky.ortt@zsr.example>\nDátum: 21.11.2026 9:00\nPredmet: Zrušenie výluky – ROV č. 3906/2026\n\n"
                + _TL_CANCEL.format(ref=ref))
        new = _mails(env, session, "z2.eml", f"Fwd: Zrušenie výluky – ROV č. {ref}", body, KD_ZA,
                     "Sat, 21 Nov 2026 11:00:00 +0100", utc(2026, 11, 21, 10, 2))
    else:
        new = _mails(env, session, "z2.eml", f"Opakovane: zrušenie výluky – ROV č. {ref}",
                     _TL_CANCEL.format(ref=ref), ROV_TT, "Sat, 21 Nov 2026 11:00:00 +0100", utc(2026, 11, 21, 10, 2))
    (ev,) = [e for e in new if e.kind == "vyluka_zrusenie"]
    assert not ev.validation.get("zrusenie_bez_ciela"), ev.validation
    assert "už zrušená" in _warnings(ev), _warnings(ev)
    assert not _nets(session)
    assert len(_alerts(session)) == alerts_before
    r = _decide(session, "78101", "Trnava", "Leopoldov", loc(2026, 12, 10, 10), loc(2026, 12, 10, 7))
    assert _green(r), r.summary
    res = _confirm(session, ev.id)
    assert res.get("ok") is True and res.get("action") == "bez_zmeny", res
    assert session.get(Closure, c.id).status == "zrusene"


def test_zrusenie_ineho_terminu_po_zruseni_ostava_poistkou(env, session):
    """Kontrola: výluka 10.12. je zrušená; zrušenie s tým istým číslom pre 15.12. (iný termín – môže ísť o nesprávne
    prečítaný nový termín) ostáva zrušením bez cieľa – poistka pre 15.12."""
    ref = "3908/2026"
    _seed_tl(env, session, ref)
    (first,) = _mails(env, session, "z1.eml", f"Zrušenie výluky – ROV č. {ref}", _TL_CANCEL.format(ref=ref), ROV_TT,
                      "Sat, 21 Nov 2026 09:00:00 +0100", utc(2026, 11, 21, 8, 2))
    assert _confirm(session, first.id).get("ok") is True
    (ev,) = _mails(env, session, "z2.eml", f"Zrušenie výluky – ROV č. {ref}",
                   f"Dobrý deň,\n\nvýluka podľa ROV č. {ref} v úseku Trnava – Leopoldov dňa 15.12.2026 od 08:00 do "
                   "14:00 hod. sa ruší.\n\nOR Trnava\n", ROV_TT, "Sat, 21 Nov 2026 11:00:00 +0100",
                   utc(2026, 11, 21, 10, 2))
    assert ev.validation.get("zrusenie_bez_ciela"), ev.validation
    r = _decide(session, "78102", "Trnava", "Leopoldov", loc(2026, 12, 15, 10), loc(2026, 12, 15, 7))
    assert not _green(r), r.summary


def test_odpoved_s_citovanym_zrusenim_nie_je_poistkou(env, session):
    """„Re: Zrušenie výluky – ROV č. 3941/2026“ s textom „ďakujeme za informáciu“ a citovaným zrušením (výluka v registri
    nie je): žiadny záznam poistky, vlak 10.12. o 10:00 ostáva VYPRAVIŤ ZELENÁ (v e0a3d0a celodenná poistka – vlak
    ESKALOVAŤ ČERVENÁ)."""
    new = _mails(env, session, "re.eml", "Re: Zrušenie výluky – ROV č. 3941/2026",
                 "Dobrý deň,\n\nďakujeme za informáciu.\n\nS pozdravom\nDispečing\n\n> Výluka podľa ROV č. 3941/2026 "
                 "v úseku Trnava – Leopoldov dňa 10.12.2026 sa ruší.\n", ROV_TT, "Sat, 21 Nov 2026 09:00:00 +0100",
                 utc(2026, 11, 21, 8, 2))
    for e in new:
        assert not e.validation.get("zrusenie_bez_ciela"), (e.kind, e.validation)
    assert not _nets(session)
    r = _decide(session, "78103", "Trnava", "Leopoldov", loc(2026, 12, 10, 10), loc(2026, 12, 10, 7))
    assert _green(r), r.summary


# ====================================================================== „do odvolania“ v inej vete
def test_do_odvolania_v_inej_vete_nepredlzi_poistku(env, session):
    """Zrušenie bez cieľa „výluka v úseku Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 14:00 hod. sa ruší. Náhradná
    autobusová doprava na trati platí do odvolania.“ – poistka len 10.12. 08:00 – 14:00 (vlak 10.12. o 10:00 nie je
    ZELENÝ), nie mesiac do odvolania: vlak 20.12. o 10:00 je VYPRAVIŤ ZELENÁ (v e0a3d0a ESKALOVAŤ ČERVENÁ)."""
    (ev,) = _mails(env, session, "z.eml", "Zrušenie výluky dňa 10.12.2026",
                   "Dobrý deň,\n\nvýluka v úseku Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 14:00 hod. sa ruší. "
                   "Náhradná autobusová doprava na trati platí do odvolania.\n\nOR Trnava\n", ROV_TT,
                   "Sat, 21 Nov 2026 09:00:00 +0100", utc(2026, 11, 21, 8, 2))
    assert ev.validation.get("zrusenie_bez_ciela"), ev.validation
    (net,) = _nets(session)
    assert (as_utc(net.start_utc), as_utc(net.end_utc)) == (loc(2026, 12, 10, 8), loc(2026, 12, 10, 14))
    assert not net.until_revoked
    r = _decide(session, "78111", "Trnava", "Leopoldov", loc(2026, 12, 10, 10), loc(2026, 12, 10, 7))
    assert not _green(r), r.summary
    r = _decide(session, "78112", "Trnava", "Leopoldov", loc(2026, 12, 20, 10), loc(2026, 12, 20, 7))
    assert _green(r), r.summary


def test_skutocne_zrusenie_s_nahradnou_dopravou_do_odvolania_sa_zapise(env, session):
    """Výluka s pevným koncom (10.12. 08:00 – 14:00); zrušenie „… sa ruší. Náhradná autobusová doprava medzi Trnavou
    a Leopoldovom platí do odvolania …“ – „do odvolania“ sa týka náhradnej dopravy, nie výluky: Potvrdiť výluku zruší
    (v e0a3d0a odmietnuté ako možné oznámenie výluky do odvolania)."""
    ref = "3931/2026"
    c = _seed_tl(env, session, ref)
    (ev,) = _mails(env, session, "z.eml", f"Zrušenie výluky – ROV č. {ref}",
                   f"Dobrý deň,\n\nvýluka podľa ROV č. {ref} v úseku Trnava – Leopoldov dňa 10.12.2026 sa ruší. "
                   "Náhradná autobusová doprava medzi Trnavou a Leopoldovom platí do odvolania podľa samostatného "
                   "oznámenia.\n\nOR Trnava\n", ROV_TT, "Sat, 21 Nov 2026 09:00:00 +0100", utc(2026, 11, 21, 8, 2))
    res = _confirm(session, ev.id)
    assert res.get("ok") is True, res
    session.expire_all()
    assert session.get(Closure, c.id).status == "zrusene"


@pytest.mark.parametrize("text,expect", [
    ("Náhradná autobusová doprava na trati platí do odvolania.", False),
    ("Výluka oboch traťových koľají potrvá do odvolania.", True),
    ("Trať je uzavretá do odvolania.", True),
    ("ROV č. 2703/2026 platí do odvolania.", True),
    ("V úseku Kraľovany – Ružomberok do odvolania nejazdia vlaky.", True),
    ("Náhradná doprava medzi Kraľovanmi a Ružomberkom do odvolania.", False),
], ids=["nahradna_doprava", "vyluka", "uzavreta", "rov", "dopravne_useku", "nahradna_doprava_dopravne"])
def test_do_odvolania_len_vo_vete_o_vyluke(text, expect):
    """„Do odvolania“ sa počíta len vo vete o výluke (slovník výluk, číslo dokumentu, dopravne úseku) – nie vo vete
    o náhradnej doprave."""
    from types import SimpleNamespace

    from vlaky.posta.spracovanie import _says_until_revoked

    mail = SimpleNamespace(subject="Oznam", body_text=f"Dobrý deň,\n\n{text}\n\nOR Žilina\n", attachments=[])
    assert _says_until_revoked(mail, {"from_station": "Kraľovany", "to_station": "Ružomberok"}) is expect


# ====================================================================== poistka bez konca → nová výluka
def test_poistka_bez_konca_zmenena_na_novu_vyluku_plati_do_odvolania(env, session):
    """Zrušenie bez cieľa a bez konca (oznámenie „od 23.11. 08:00 hod. až do odvolania úplná výluka …“ prečítané ako
    zrušenie) – poistka do odvolania; dispečer zmení druh na novú výluku (Upraviť a potvrdiť bez zadania konca): výluka
    sa zapíše ako overená výluka do odvolania, poistka zanikne a vlak 27.11. o 10:00 nie je ZELENÝ (v e0a3d0a „chýba
    koniec“ – výluka sa nezapísala, ostala len poistka)."""
    from vlaky.posta.spracovanie import _write_orphan_pending

    (ev,) = _mails(env, session, "z.eml", "Zrušenie výluky – ROV č. 2795/2026",
                   "Dobrý deň,\n\nvýluka v úseku Kraľovany – Ružomberok dňa 24.11.2026 od 08:00 do 14:00 hod. sa ruší "
                   "(ROV č. 2795/2026).\n\nOR Žilina\n", ROV_ZA, "Fri, 20 Nov 2026 09:00:00 +0100",
                   utc(2026, 11, 20, 8, 2))
    ev.data = {**ev.data, "start": loc(2026, 11, 23, 8).isoformat(), "end": None}
    session.flush()
    row = _write_orphan_pending(session, ev)
    session.commit()
    assert row is not None and row.until_revoked
    res = _confirm(session, ev.id, event_kind="vyluka_nova")
    assert res.get("ok") is True, res
    live = [c for c in session.scalars(select(Closure)) if c.status != "zrusene" and c.proposed_change_of is None]
    assert [(c.status, c.source, c.until_revoked) for c in live] == [("overene", "email", True)], live
    assert as_utc(live[0].start_utc) == loc(2026, 11, 23, 8)
    r = _decide(session, "78121", "Kraľovany", "Ružomberok", loc(2026, 11, 27, 10), loc(2026, 11, 27, 7))
    assert not _green(r), r.summary


# ====================================================================== zrušenie dňa dlhej výluky bez čísla
def test_zrusenie_dna_dlhej_vyluky_bez_cisla_najde_vyluku(env, session):
    """Výluka Trnava – Leopoldov 01. – 20.12.2026 denne 08:00 – 14:00 (ROV č. 3921/2026); „výluka v úseku Trnava –
    Leopoldov dňa 15.12.2026 sa nekoná. Ostatné dni bez zmeny.“ (bez čísla) – nie je zrušením bez cieľa (žiadna poistka
    ani výzva, že môže ísť o novú výluku); Potvrdiť zruší len 15.12.: vlak 15.12. o 10:00 VYPRAVIŤ ZELENÁ, 16.12. o
    10:00 zdržaný (v e0a3d0a poistka zrušenia bez cieľa a Potvrdiť odmietnuté)."""
    _register(env, session, "r.eml", "ROV č. 3921/2026 – výluka Trnava – Leopoldov",
              "Dobrý deň,\n\noznamujeme úplnú výluku trate v úseku Trnava – Leopoldov v dňoch 01.12.2026 – 20.12.2026 "
              "denne od 08:00 do 14:00 hod. (ROV č. 3921/2026).\n\nOR Trnava\n", ROV_TT,
              "Fri, 20 Nov 2026 08:00:00 +0100", utc(2026, 11, 20, 7, 2))
    (ev,) = _mails(env, session, "z.eml", "Výluka dňa 15.12.2026 sa nekoná",
                   "Dobrý deň,\n\nvýluka v úseku Trnava – Leopoldov dňa 15.12.2026 sa nekoná. Ostatné dni bez zmeny."
                   "\n\nOR Trnava\n", ROV_TT, "Sat, 21 Nov 2026 09:00:00 +0100", utc(2026, 11, 21, 8, 2))
    assert ev.kind == "vyluka_zrusenie", ev.kind
    assert not ev.validation.get("zrusenie_bez_ciela"), ev.validation
    assert not _nets(session)
    res = _confirm(session, ev.id)
    assert res.get("ok") is True, res
    r = _decide(session, "78131", "Trnava", "Leopoldov", loc(2026, 12, 15, 10), loc(2026, 12, 15, 7))
    assert _green(r), r.summary
    r = _decide(session, "78132", "Trnava", "Leopoldov", loc(2026, 12, 16, 10), loc(2026, 12, 16, 7))
    assert not _green(r), r.summary


# ====================================================================== OR preposlaného obsahu
def _rov15_za(env, session) -> Closure:
    _register(env, session, "rov15.eml", "ROV č. 15/2026 – Žilina – Púchov",
              "Dobrý deň,\n\núplná výluka v úseku Žilina – Púchov dňa 10.12.2026 od 08:00 do 14:00 hod. "
              "(ROV č. 15/2026).\n\nOR Žilina\n", ROV_ZA, "Tue, 01 Dec 2026 09:00:00 +0100", utc(2026, 12, 1, 8, 2))
    (c,) = [c for c in session.scalars(select(Closure)) if c.status != "zrusene"]
    return c


def _kd_forward(origin: str, sign: str) -> str:
    return ("Na vedomie.\n\n---------- Forwarded message ---------\n"
            f"Od: {origin}\nSubject: STORNO ROV 15/2026\n\nROV č. 15/2026 sa stornuje.\n\n{sign}\n")


def test_preposlane_storno_inej_or_kontrolnym_dispecerom_nezrusi_vyluku(env, session):
    """FR4 (zvyšok): kontrolný dispečer OR Žilina (kd.orza@…) prepošle „STORNO ROV 15/2026“ OR Košice; ROV 15/2026
    v registri je OR Žilina (Žilina – Púchov 10.12.). OR e-mailu je OR Košice (preposlaný obsah), nie OR preposielateľa:
    Potvrdiť výluku nezruší a vlak Žilina → Púchov 10.12. o 10:00 nie je ZELENÝ (v e0a3d0a sa výluka OR Žilina zrušila
    a vlak dostal VYPRAVIŤ ZELENÁ)."""
    from vlaky.posta.spracovanie import mail_issuer, register_candidates

    c = _rov15_za(env, session)
    (ev,) = _mails(env, session, "f.eml", "Fwd: STORNO ROV 15/2026",
                   _kd_forward("ZSR OR Kosice <vyluky.orke@zsr.example>", "OR Košice"), KD_ZA,
                   "Sat, 05 Dec 2026 09:00:00 +0100", utc(2026, 12, 5, 8, 2))
    assert ev.kind == "vyluka_zrusenie", ev.kind
    assert mail_issuer(session.get(Email, ev.email_id)) == "KE"
    assert "OR Košice" in _warnings(ev) and "OR Žilina" in _warnings(ev), _warnings(ev)
    assert any("OR Žilina" in f for x in register_candidates(session, ev) for f in x["flags"])
    res = _confirm(session, ev.id)
    assert res.get("ok") is False, res
    session.expire_all()
    assert session.get(Closure, c.id).status == "overene"
    r = _decide(session, "78141", "Žilina", "Púchov", loc(2026, 12, 10, 10), loc(2026, 12, 10, 7))
    assert not _green(r), r.summary


def test_preposlane_storno_vlastnej_or_kontrolnym_dispecerom_zrusi_vyluku(env, session):
    """Kontrola: kontrolný dispečer OR Žilina prepošle STORNO ROV 15/2026 od OR Žilina – výluka OR Žilina sa po Potvrdiť
    zruší (OR preposlaného obsahu = OR výluky)."""
    from vlaky.posta.spracovanie import mail_issuer

    c = _rov15_za(env, session)
    (ev,) = _mails(env, session, "f.eml", "Fwd: STORNO ROV 15/2026",
                   _kd_forward("ZSR OR Zilina <vyluky.orza@zsr.example>", "OR Žilina"), KD_ZA,
                   "Sat, 05 Dec 2026 09:00:00 +0100", utc(2026, 12, 5, 8, 2))
    assert mail_issuer(session.get(Email, ev.email_id)) == "ZA"
    assert _confirm(session, ev.id).get("ok") is True
    session.expire_all()
    assert session.get(Closure, c.id).status == "zrusene"


@pytest.mark.parametrize("sender,forwarded,body,expect", [
    ("kd.orza@zsr.example", "vyluky.orke@zsr.example", "", "KE"),
    ("kd.orza@zsr.example", "?", "> ROV sa stornuje.\n> OR Košice", "KE"),
    ("kd.orza@zsr.example", "?", "> ROV sa stornuje.", None),
    ("kd.orza@zsr.example", "", "ROV sa stornuje.", "ZA"),
    ("dispecing@dopravca.example", "vyluky.ortt@zsr.example", "", "TT"),
], ids=["adresa_povodnej_or", "podpis_povodnej_or", "povodna_or_neznama", "bez_preposlania", "dopravca"])
def test_or_preposlaneho_obsahu(sender, forwarded, body, expect):
    """OR preposlaného e-mailu: pôvodný odosielateľ, inak podpis v preposlanom texte; ak sa nedá určiť, OR preposielateľa
    s adresou OR sa nepoužije (None – fronta upozorní). Bez preposlania OR odosielateľa."""
    from types import SimpleNamespace

    from vlaky.posta.spracovanie import mail_issuer

    mail = SimpleNamespace(sender=sender, forwarded_from=forwarded, subject="Fwd: STORNO ROV 15/2026" if forwarded
                           else "STORNO ROV 15/2026", body_text=f"Na vedomie.\n\n{body}\n\nKontrolný dispečer OR Žilina\n")
    assert mail_issuer(mail) == expect


# ====================================================================== opakované zrušenie dňa rozdelenej výluky
def test_opakovane_zrusenie_zruseneho_dna_rozdelenej_vyluky_nie_je_poistkou(env, session):
    """Výluka ROV 3909/2026 Trnava – Leopoldov 10. – 11.12. denne 08:00 – 14:00; zrušenie 11.12. ju rozdelilo (ostal
    10.12.). Znova poslané zrušenie 11.12. nie je zrušením bez cieľa: žiadna poistka ani upozornenie DÔLEŽITÉ, vlak
    11.12. o 10:00 ostáva VYPRAVIŤ ZELENÁ, 10.12. ostáva uzavretý, vo fronte „už zrušený“ a Potvrdiť register nezmení
    (v e0a3d0a poistka pre 11.12. – vlak ESKALOVAŤ a upozornenia pre vlaky v zrušenom dni)."""
    ref = "3909/2026"
    _register(env, session, "r.eml", f"ROV č. {ref} Trnava – Leopoldov",
              f"Dobrý deň,\n\núplná výluka v úseku Trnava – Leopoldov v dňoch 10.12.2026 – 11.12.2026 denne od 08:00 do "
              f"14:00 hod. (ROV č. {ref}).\n\nOR Trnava\n", ROV_TT, "Fri, 20 Nov 2026 09:00:00 +0100",
              utc(2026, 11, 20, 8, 2))
    cancel = f"Dobrý deň,\n\nvýluka podľa ROV č. {ref} v úseku Trnava – Leopoldov dňa 11.12.2026 sa ruší.\n\nOR Trnava\n"
    (first,) = _mails(env, session, "z1.eml", f"Zrušenie výluky – ROV č. {ref}", cancel, ROV_TT,
                      "Sat, 21 Nov 2026 09:00:00 +0100", utc(2026, 11, 21, 8, 2))
    res = _confirm(session, first.id)
    assert res.get("ok") is True and res.get("action") == "rozdelene", res
    session.expire_all()
    live = [(as_utc(c.start_utc), as_utc(c.end_utc)) for c in session.scalars(select(Closure))
            if c.status != "zrusene" and c.source != "email_poistka"]
    assert live == [(loc(2026, 12, 10, 8), loc(2026, 12, 10, 14))], live
    alerts_before = len(_alerts(session))
    (ev,) = _mails(env, session, "z2.eml", f"Opakovane: zrušenie výluky – ROV č. {ref}", cancel, ROV_TT,
                   "Sat, 21 Nov 2026 11:00:00 +0100", utc(2026, 11, 21, 10, 2))
    assert not ev.validation.get("zrusenie_bez_ciela"), ev.validation
    assert "už zrušený" in _warnings(ev), _warnings(ev)
    assert not _nets(session)
    assert len(_alerts(session)) == alerts_before
    r = _decide(session, "78151", "Trnava", "Leopoldov", loc(2026, 12, 11, 10), loc(2026, 12, 11, 7))
    assert _green(r), r.summary
    r = _decide(session, "78152", "Trnava", "Leopoldov", loc(2026, 12, 10, 10), loc(2026, 12, 10, 7))
    assert not _green(r), r.summary
    res = _confirm(session, ev.id)
    assert res.get("ok") is True and res.get("action") == "bez_zmeny", res


def test_zrusenie_ineho_dna_rozdelenej_vyluky_sa_zapise(env, session):
    """Kontrola: po zrušení 11.12. (výluka rozdelená) zrušenie 10.12. nie je opakovaním – Potvrdiť zruší aj 10.12."""
    ref = "3910/2026"
    _register(env, session, "r.eml", f"ROV č. {ref} Trnava – Leopoldov",
              f"Dobrý deň,\n\núplná výluka v úseku Trnava – Leopoldov v dňoch 10.12.2026 – 11.12.2026 denne od 08:00 do "
              f"14:00 hod. (ROV č. {ref}).\n\nOR Trnava\n", ROV_TT, "Fri, 20 Nov 2026 09:00:00 +0100",
              utc(2026, 11, 20, 8, 2))
    for n, day in enumerate((11, 10)):
        (ev,) = _mails(env, session, f"z{n}.eml", f"Zrušenie výluky – ROV č. {ref}",
                       f"Dobrý deň,\n\nvýluka podľa ROV č. {ref} v úseku Trnava – Leopoldov dňa {day}.12.2026 sa ruší."
                       "\n\nOR Trnava\n", ROV_TT, f"Sat, 21 Nov 2026 {9 + n}:00:00 +0100", utc(2026, 11, 21, 8 + n, 2))
        assert "už zrušen" not in _warnings(ev), _warnings(ev)
        assert _confirm(session, ev.id).get("ok") is True
    r = _decide(session, "78153", "Trnava", "Leopoldov", loc(2026, 12, 10, 10), loc(2026, 12, 10, 7))
    assert _green(r), r.summary


# ====================================================================== ukončenie výluky do odvolania
@pytest.fixture()
def fa_cancel(monkeypatch):
    """Výstup pravidiel strany FA pre zrušenie od času bez konca („výluka … vyhlásená do odvolania sa ruší od 25.11.2026
    12:00 hod.“ → začiatok 25.11. 12:00, koniec žiadny): udalosť vyluka_zrusenie e-mailu s predmetom „Zrušenie ROV“
    dostane dáta ``data``."""
    from vlaky.posta import spracovanie
    from vlaky.posta.udalosti import ExtractedItem

    holder: dict = {}
    orig = spracovanie.extract

    def fake(doc, *a, **kw):
        res = orig(doc, *a, **kw)
        if "data" not in holder or not str(doc.subject or "").startswith("Zrušenie ROV"):
            return res
        items = [it for it in res.items if it.kind != "vyluka_zrusenie"]
        items.append(ExtractedItem(kind="vyluka_zrusenie", data=dict(holder["data"]), confidence=0.85,
                                   notes=["Zrušenie od uvedeného času – ďalej bez konca."]))
        res.items = items
        return res

    monkeypatch.setattr(spracovanie, "extract", fake)
    return holder


def _until_revoked_tl(env, session, ref: str) -> Closure:
    _register(env, session, "r.eml", f"ROV č. {ref} – výluka do odvolania",
              f"Dobrý deň,\n\núplná výluka trate v úseku Trnava – Leopoldov od 23.11.2026 08:00 hod. do odvolania "
              f"(ROV č. {ref}).\n\nOR Trnava\n", ROV_TT, "Fri, 20 Nov 2026 08:00:00 +0100", utc(2026, 11, 20, 7, 2))
    (c,) = [c for c in session.scalars(select(Closure)) if c.status != "zrusene"]
    assert c.until_revoked
    return c


def test_zrusenie_vyluky_do_odvolania_od_casu_ju_ukonci(env, session, fa_cancel):
    """U2: výluka do odvolania od 23.11. 08:00 (ROV č. 3932/2026); „výluka … vyhlásená do odvolania sa ruší od
    25.11.2026 12:00 hod.“ (začiatok, bez konca) – Potvrdiť výluku ukončí 25.11. 12:00 (už nie do odvolania): vlak
    27.11. o 10:00 VYPRAVIŤ ZELENÁ, 24.11. o 10:00 ostáva zdržaný (v e0a3d0a odmietnuté – „automaticky ju nemožno
    rozdeliť, lebo výluka platí do odvolania“ – vlak 27.11. ESKALOVAŤ ČERVENÁ)."""
    ref = "3932/2026"
    c = _until_revoked_tl(env, session, ref)
    fa_cancel["data"] = {"from_station": "Trnava", "to_station": "Leopoldov", "external_ref": f"ROV č. {ref}",
                         "start": loc(2026, 11, 25, 12).isoformat(), "end": None}
    (ev,) = [e for e in _mails(env, session, "z.eml", f"Zrušenie ROV č. {ref}",
                               f"Dobrý deň,\n\nvýluka podľa ROV č. {ref} v úseku Trnava – Leopoldov vyhlásená do "
                               "odvolania sa ruší od 25.11.2026 12:00 hod.\n\nOR Trnava\n", ROV_TT,
                               "Tue, 24 Nov 2026 09:00:00 +0100", utc(2026, 11, 24, 8, 2))
             if e.kind == "vyluka_zrusenie"]
    assert "Ukončí výluku" in _warnings(ev), _warnings(ev)
    res = _confirm(session, ev.id)
    assert res.get("ok") is True, res
    session.expire_all()
    c = session.get(Closure, c.id)
    assert (as_utc(c.start_utc), as_utc(c.end_utc), bool(c.until_revoked)) == \
        (loc(2026, 11, 23, 8), loc(2026, 11, 25, 12), False)
    r = _decide(session, "78161", "Trnava", "Leopoldov", loc(2026, 11, 27, 10), loc(2026, 11, 27, 7))
    assert _green(r), r.summary
    r = _decide(session, "78162", "Trnava", "Leopoldov", loc(2026, 11, 24, 10), loc(2026, 11, 24, 7))
    assert not _green(r), r.summary


def test_zrusenie_vnutornej_casti_vyluky_do_odvolania_sa_odmietne(env, session, fa_cancel):
    """Kontrola: zrušenie len jedného dňa výluky do odvolania (25.11. 00:00 – 26.11. 00:00) ostáva odmietnuté (výluku
    s technickým koncom nemožno rozdeliť) – výluka ostáva celá do odvolania."""
    ref = "3934/2026"
    c = _until_revoked_tl(env, session, ref)
    fa_cancel["data"] = {"from_station": "Trnava", "to_station": "Leopoldov", "external_ref": f"ROV č. {ref}",
                         "start": loc(2026, 11, 25, 0).isoformat(), "end": loc(2026, 11, 26, 0).isoformat()}
    (ev,) = [e for e in _mails(env, session, "z.eml", f"Zrušenie ROV č. {ref}",
                               f"Dobrý deň,\n\nvýluka podľa ROV č. {ref} v úseku Trnava – Leopoldov sa dňa 25.11.2026 "
                               "nekoná.\n\nOR Trnava\n", ROV_TT, "Tue, 24 Nov 2026 09:00:00 +0100",
                               utc(2026, 11, 24, 8, 2)) if e.kind == "vyluka_zrusenie"]
    res = _confirm(session, ev.id)
    assert res.get("ok") is False, res
    session.expire_all()
    c = session.get(Closure, c.id)
    assert c.status == "overene" and c.until_revoked
