"""6. revízia (strana FB – register, oprava po kontrole): predĺženie a skrátenie výluky bez začiatku, zmena bez zámeru,
ktorá výluku predlžuje.

- Pravidlá pri „výluka … sa predĺži do 29.11.2026“ uvádzajú zámer „predlzenie“ a len koniec (začiatok ostáva podľa
  registra). Register doplní začiatok z výluky, ktorej sa zmena týka (číslo a úsek): udalosť nie je blokovaná, dočasný
  záznam pokryje dni predĺženia a Potvrdiť posunie koniec výluky. Predĺženie času jedného dňa („dňa 25.11. sa predlžuje
  do 16:00“) začína oknom výluky v ten deň; skrátenie („skončí už 25.11.“) začína začiatkom výluky.
- Zmena bez zámeru, ktorej e-mail výluku predlžuje a nový termín končí po výluke, po Potvrdiť len posunie koniec (dni
  medzi koncom výluky a novým termínom ostávajú uzavreté).
- Udalosť bez úseku (len číslo ROV) začiatok z registra nedostane (miesto sa z registra nedopĺňa).

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from vlaky.models import Closure
from test_revizia3_terminy import as_utc, drop, events, raw_mail, utc
from test_revizia4_pravidla import _decide, _process, env  # noqa: F401 – fixture env

ROV_ZA = "ZSR OR Zilina <vyluky.orza@zsr.example>"
TZ = ZoneInfo("Europe/Bratislava")
RL = ("Ružomberok", "Liptovský Mikuláš")
_ROV2331 = ("Dobrý deň,\n\núplná výluka v úseku Ružomberok – Liptovský Mikuláš v dňoch 23.11. – 27.11.2026 denne od "
            "08:00 do 14:00 hod. (ROV č. 2331/2026).\n\nOR Žilina\n")


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


def _seed(env, session) -> Closure:
    for e in _mails(env, session, "r.eml", "ROV c. 2331/2026 Ruzomberok - Liptovsky Mikulas", _ROV2331, ROV_ZA,
                    "Mon, 16 Nov 2026 09:00:00 +0100", utc(2026, 11, 16, 8, 2)):
        _confirm(session, e.id)
    (c,) = [c for c in session.scalars(select(Closure)) if c.status != "zrusene"]
    return c


def _green(r) -> bool:
    return r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"


def _live(session) -> list[Closure]:
    return [c for c in session.scalars(select(Closure).order_by(Closure.id))
            if c.status != "zrusene" and c.proposed_change_of is None]


@pytest.fixture()
def fa_change(monkeypatch):
    """Výstup pravidiel strany FA pre zmenu výluky (zámer, len koniec – „začiatok ostáva podľa registra“): udalosť
    vyluka_zmena e-mailu so zmenou ROV č. 2331/2026 dostane dáta ``data`` (ostatné e-maily bez zmeny)."""
    from vlaky.posta import spracovanie, udalosti
    from vlaky.posta.udalosti import ExtractedItem

    monkeypatch.setitem(udalosti.SCHEMAS, "vyluka_zmena", list(udalosti.SCHEMAS["vyluka_zmena"]) + ["zamer"])
    holder: dict = {}
    orig = spracovanie.extract

    def fake(doc, *a, **kw):
        res = orig(doc, *a, **kw)
        if "data" not in holder or not str(doc.subject or "").startswith("Zmena ROV"):
            return res
        base = {"from_station": RL[0], "to_station": RL[1], "external_ref": "ROV č. 2331/2026", "kind": "uplna",
                "track": "", "line_ref": "", "daily_from": None, "daily_to": None, "start": None, "end": None,
                "until_revoked": None}
        items = [it for it in res.items if it.kind != "vyluka_zmena"]
        items.append(ExtractedItem(kind="vyluka_zmena", data={**base, **copy.deepcopy(holder["data"])},
                                   confidence=0.73, notes=["Začiatok e-mail neuvádza, ostáva podľa registra."]))
        res.items = items
        return res

    monkeypatch.setattr(spracovanie, "extract", fake)
    return holder


def test_predlzenie_bez_zaciatku_dostane_zaciatok_z_registra(env, session, fa_change):
    """FC6: „výluka … podľa ROV č. 2331/2026 sa predĺži do 29.11.2026 v rovnakom čase od 08:00 do 14:00 hod.“ – zámer
    predĺženie, len koniec. Začiatok podľa registra (23.11. 08:00): udalosť nie je blokovaná, vlak 28.11. o 10:00 nie je
    ZELENÝ ani pred potvrdením, Potvrdiť posunie koniec výluky na 29.11. 14:00 (v e0a3d0a „chýba začiatok“ – Potvrdiť
    zlyhalo a dni predĺženia v registri chýbali)."""
    c = _seed(env, session)
    fa_change["data"] = {"zamer": "predlzenie", "end": loc(2026, 11, 29, 14).isoformat(), "daily_from": "08:00",
                         "daily_to": "14:00"}
    (ev,) = [e for e in _mails(env, session, "z.eml", "Zmena ROV č. 2331/2026 – predĺženie",
                               "Dobrý deň,\n\nvýluka podľa ROV č. 2331/2026 v úseku Ružomberok – Liptovský Mikuláš sa "
                               "predĺži do 29.11.2026 v rovnakom čase od 08:00 do 14:00 hod.\n\nOR Žilina\n", ROV_ZA,
                               "Wed, 18 Nov 2026 10:00:00 +0100", utc(2026, 11, 18, 9, 2)) if e.kind == "vyluka_zmena"]
    assert not ev.validation.get("blocking"), ev.validation.get("blocking")
    assert ev.data.get("start") and as_utc(datetime.fromisoformat(ev.data["start"])) == loc(2026, 11, 23, 8)
    r = _decide(session, "79101", *RL, loc(2026, 11, 28, 10), loc(2026, 11, 28, 7))
    assert not _green(r), r.summary
    res = _confirm(session, ev.id)
    assert res.get("ok") is True, res
    session.expire_all()
    c = session.get(Closure, c.id)
    assert (as_utc(c.start_utc), as_utc(c.end_utc)) == (loc(2026, 11, 23, 8), loc(2026, 11, 29, 14))
    for n, day in enumerate((24, 28, 29)):
        r = _decide(session, f"7911{n}", *RL, loc(2026, 11, day, 10), loc(2026, 11, day, 7))
        assert not _green(r), (day, r.summary)


def test_predlzenie_casu_jedneho_dna_bez_zaciatku(env, session, fa_change):
    """„výluka … sa dňa 25.11.2026 predlžuje do 16:00 hod. (pôvodne do 14:00 hod.)“ – zámer predĺženie, koniec 25.11.
    16:00: začiatok je okno výluky v ten deň (25.11. 08:00), po Potvrdiť vlak 25.11. o 15:00 nie je ZELENÝ a ostatné dni
    výluky ostávajú (v e0a3d0a Potvrdiť zlyhalo – „chýba začiatok“ – a 25.11. 14:00 – 16:00 v registri chýbal)."""
    _seed(env, session)
    fa_change["data"] = {"zamer": "predlzenie", "end": loc(2026, 11, 25, 16).isoformat()}
    (ev,) = [e for e in _mails(env, session, "z.eml", "Zmena ROV č. 2331/2026 – predĺženie času",
                               "Dobrý deň,\n\nvýluka podľa ROV č. 2331/2026 v úseku Ružomberok – Liptovský Mikuláš sa dňa "
                               "25.11.2026 predlžuje do 16:00 hod. (pôvodne do 14:00 hod.).\n\nOR Žilina\n", ROV_ZA,
                               "Wed, 18 Nov 2026 10:00:00 +0100", utc(2026, 11, 18, 9, 2)) if e.kind == "vyluka_zmena"]
    assert as_utc(datetime.fromisoformat(ev.data["start"])) == loc(2026, 11, 25, 8), ev.data
    res = _confirm(session, ev.id)
    assert res.get("ok") is True, res
    r = _decide(session, "79121", *RL, loc(2026, 11, 25, 15), loc(2026, 11, 25, 12))
    assert not _green(r), r.summary
    r = _decide(session, "79122", *RL, loc(2026, 11, 26, 10), loc(2026, 11, 26, 7))
    assert not _green(r), r.summary


def test_skratenie_bez_zaciatku_zacina_zaciatkom_vyluky(env, session, fa_change):
    """„výluka … sa skracuje a skončí už 25.11.2026 o 14:00 hod.“ – zámer skrátenie, len koniec: začiatok podľa registra
    (23.11. 08:00), po Potvrdiť výluka 23. – 25.11.: vlak 24.11. o 10:00 zdržaný, 26.11. o 10:00 VYPRAVIŤ ZELENÁ
    (v e0a3d0a „chýba začiatok“ – skrátenie sa nedalo zapísať)."""
    _seed(env, session)
    fa_change["data"] = {"zamer": "skratenie", "end": loc(2026, 11, 25, 14).isoformat(), "daily_from": "08:00",
                         "daily_to": "14:00"}
    (ev,) = [e for e in _mails(env, session, "z.eml", "Zmena ROV č. 2331/2026 – skrátenie",
                               "Dobrý deň,\n\nvýluka podľa ROV č. 2331/2026 v úseku Ružomberok – Liptovský Mikuláš sa "
                               "skracuje a skončí už 25.11.2026 o 14:00 hod.\n\nOR Žilina\n", ROV_ZA,
                               "Wed, 18 Nov 2026 10:00:00 +0100", utc(2026, 11, 18, 9, 2)) if e.kind == "vyluka_zmena"]
    assert as_utc(datetime.fromisoformat(ev.data["start"])) == loc(2026, 11, 23, 8), ev.data
    res = _confirm(session, ev.id)
    assert res.get("ok") is True, res
    assert [(as_utc(c.start_utc), as_utc(c.end_utc)) for c in _live(session)] == \
        [(loc(2026, 11, 23, 8), loc(2026, 11, 25, 14))]
    r = _decide(session, "79131", *RL, loc(2026, 11, 24, 10), loc(2026, 11, 24, 7))
    assert not _green(r), r.summary
    r = _decide(session, "79132", *RL, loc(2026, 11, 26, 10), loc(2026, 11, 26, 7))
    assert _green(r), r.summary


def test_bez_useku_sa_zaciatok_z_registra_nedopni(env, session):
    """Zmena len s číslom ROV (bez úseku) dostane začiatok z registra až po výbere výluky dispečerom – sama nie."""
    from vlaky.posta.spracovanie import _register_start

    _seed(env, session)
    d = {"external_ref": "ROV č. 2331/2026", "zamer": "predlzenie", "end": loc(2026, 11, 29, 14).isoformat(),
         "start": None, "from_station": None, "to_station": None}
    notes: list = []
    assert _register_start(session, "vyluka_zmena", d, None, notes) is d and not notes


def test_bez_zameru_predlzenie_po_potvrdeni_drzi_medzidni(env, session):
    """FC6 (bez zámeru): „výluka … podľa ROV č. 2331/2026 sa predlžuje do 29.11.2026, denne od 08:00 do 14:00 hod.“
    a nový termín prečítaný len ako 29.11. – po Potvrdiť sa koniec výluky len posunie: vlak 28.11. o 10:00 nie je ZELENÝ
    a pôvodné dni ostávajú (v e0a3d0a sa termín nahradil 29.11. a vlak 28.11. dostal VYPRAVIŤ ZELENÁ)."""
    _seed(env, session)
    new = _mails(env, session, "z.eml", "Zmena ROV č. 2331/2026 – predĺženie",
                 "Dobrý deň,\n\nvýluka v úseku Ružomberok – Liptovský Mikuláš podľa ROV č. 2331/2026 sa predlžuje do "
                 "29.11.2026, denne od 08:00 do 14:00 hod.\n\nOR Žilina\n", ROV_ZA, "Thu, 19 Nov 2026 09:00:00 +0100",
                 utc(2026, 11, 19, 8, 2))
    (ev,) = [e for e in new if e.kind == "vyluka_zmena"]
    res = _confirm(session, ev.id)
    assert res.get("ok") is True, res
    for n, day in enumerate((24, 28, 29)):
        r = _decide(session, f"7914{n}", *RL, loc(2026, 11, day, 10), loc(2026, 11, day, 7))
        assert not _green(r), (day, r.summary)


def test_vyber_vyluky_z_registra_doplni_zaciatok_predlzenia(env, session, fa_change):
    """Predĺženie len s číslom ROV (bez úseku a začiatku: „ROV č. 2331/2026 sa predĺži do 29.11.2026“) ostáva udalosťou
    bez miesta; dispečer vo fronte vyberie výluku z registra (úsek z návrhu) a potvrdí – začiatok sa doplní z tej
    výluky, Potvrdiť posunie koniec na 29.11. 14:00 a vlak 28.11. o 10:00 nie je ZELENÝ (v e0a3d0a zápis zlyhal –
    „chýba začiatok“ – a dni predĺženia v registri chýbali)."""
    from vlaky.posta.spracovanie import candidate_edits

    c = _seed(env, session)
    fa_change["data"] = {"zamer": "predlzenie", "from_station": None, "to_station": None,
                         "end": loc(2026, 11, 29, 14).isoformat(), "daily_from": "08:00", "daily_to": "14:00"}
    (ev,) = [e for e in _mails(env, session, "z.eml", "Zmena ROV č. 2331/2026 – predĺženie",
                               "Dobrý deň,\n\nROV č. 2331/2026 sa predĺži do 29.11.2026 v rovnakom čase.\n\nOR Žilina\n",
                               ROV_ZA, "Wed, 18 Nov 2026 10:00:00 +0100", utc(2026, 11, 18, 9, 2))
             if e.kind == "vyluka_zmena"]
    assert not ev.data.get("from_station") and not ev.data.get("start"), ev.data
    edits = candidate_edits(session, ev, c.id)
    assert edits.get("from_station") == RL[0], edits
    res = _confirm(session, ev.id, **edits)
    assert res.get("ok") is True, res
    session.expire_all()
    c = session.get(Closure, c.id)
    assert (as_utc(c.start_utc), as_utc(c.end_utc)) == (loc(2026, 11, 23, 8), loc(2026, 11, 29, 14))
    for n, day in enumerate((24, 28)):
        r = _decide(session, f"7915{n}", *RL, loc(2026, 11, day, 10), loc(2026, 11, day, 7))
        assert not _green(r), (day, r.summary)


@pytest.mark.parametrize("zamer,end,new_end", [
    ("predlzenie", (2026, 11, 30, 0), (2026, 11, 29, 14)),
    ("skratenie", (2026, 11, 26, 0), (2026, 11, 25, 14)),
], ids=["predlzenie_do_dna", "skratenie_do_dna"])
def test_zmena_dennej_vyluky_len_s_datumom_zachova_denne_okno(env, session, fa_change, zamer, end, new_end):
    """Pravidlá pri „výluka … sa predĺži do 29.11.2026“ / „sa skracuje do 25.11.2026“ uvádzajú len koniec dňa (bez
    začiatku a denného okna). Denná výluka 08:00 – 14:00 ostáva dennou: začiatok a okno podľa registra, koniec koncom
    okna v posledný deň – po Potvrdiť jediná výluka s oknom 08:00 – 14:00 do nového konca, noci ostávajú voľné (vlak
    o 02:00 VYPRAVIŤ ZELENÁ). Bez prevzatia okna by sa zapísala súvislá výluka 23.11. – 30.11. cez noci a všetky vlaky
    by týždeň stáli (prvá verzia tejto opravy)."""
    c = _seed(env, session)
    fa_change["data"] = {"zamer": zamer, "end": loc(*end).isoformat()}
    (ev,) = [e for e in _mails(env, session, "z.eml", f"Zmena ROV č. 2331/2026 – {zamer}",
                               "Dobrý deň,\n\nvýluka podľa ROV č. 2331/2026 v úseku Ružomberok – Liptovský Mikuláš sa "
                               + ("predĺži do 29.11.2026." if zamer == "predlzenie" else "skracuje do 25.11.2026.")
                               + "\n\nOR Žilina\n", ROV_ZA, "Wed, 18 Nov 2026 10:00:00 +0100",
                               utc(2026, 11, 18, 9, 2)) if e.kind == "vyluka_zmena"]
    assert (ev.data.get("daily_from"), ev.data.get("daily_to")) == ("08:00", "14:00"), ev.data
    res = _confirm(session, ev.id)
    assert res.get("ok") is True, res
    assert [(as_utc(x.start_utc), as_utc(x.end_utc), x.daily_from, x.daily_to) for x in _live(session)] == \
        [(loc(2026, 11, 23, 8), loc(*new_end), "08:00", "14:00")]
    last = new_end[2]
    r = _decide(session, "79171", *RL, loc(2026, 11, last, 10), loc(2026, 11, last, 7))
    assert not _green(r), r.summary
    r = _decide(session, "79172", *RL, loc(2026, 11, 24, 2), loc(2026, 11, 23, 23))
    assert _green(r), r.summary
    r = _decide(session, "79173", *RL, loc(2026, 11, last + 1, 10), loc(2026, 11, last + 1, 7))
    assert _green(r), r.summary


def test_predlzenie_dennej_vyluky_s_koncom_mimo_okna_nic_nestrati(env, session, fa_change):
    """„sa predĺži do 29.11.2026 do 18:00 hod.“ (koniec mimo okna 08:00 – 14:00): okno sa neprevezme – predĺženie začína
    koncom výluky, nič sa nestratí: vlak 28.11. o 10:00 a 29.11. o 16:00 nie je ZELENÝ, pôvodné dni ostávajú."""
    _seed(env, session)
    fa_change["data"] = {"zamer": "predlzenie", "end": loc(2026, 11, 29, 18).isoformat()}
    (ev,) = [e for e in _mails(env, session, "z.eml", "Zmena ROV č. 2331/2026 – predĺženie",
                               "Dobrý deň,\n\nvýluka podľa ROV č. 2331/2026 v úseku Ružomberok – Liptovský Mikuláš sa "
                               "predĺži do 29.11.2026 do 18:00 hod.\n\nOR Žilina\n", ROV_ZA,
                               "Wed, 18 Nov 2026 10:00:00 +0100", utc(2026, 11, 18, 9, 2)) if e.kind == "vyluka_zmena"]
    assert as_utc(datetime.fromisoformat(ev.data["start"])) == loc(2026, 11, 27, 14), ev.data
    assert _confirm(session, ev.id).get("ok") is True
    for n, (day, h) in enumerate(((24, 10), (28, 10), (29, 16))):
        r = _decide(session, f"7918{n}", *RL, loc(2026, 11, day, h), loc(2026, 11, day, h - 3))
        assert not _green(r), (day, h, r.summary)


def test_predlzenie_nocnej_vyluky_do_dna_zahrnie_noc_ktora_v_nom_zacina(env, session):
    """Nočná výluka 22:00 – 04:00 (23. – 28.11. 04:00) a „predĺži sa do 29.11.2026“ (len dátum): okno ostáva nočné
    a koniec je koncom noci, ktorá 29.11. začína (30.11. 04:00 – noc patrí dňu, ktorým začína; prísnejší prípad)."""
    from vlaky.posta.spracovanie import _register_start

    session.add(Closure(kind="uplna", from_station=RL[0], to_station=RL[1], start_utc=loc(2026, 11, 23, 22),
                        end_utc=loc(2026, 11, 28, 4), daily_from="22:00", daily_to="04:00", status="overene",
                        source="manual", external_ref="ROV č. 2390/2026", track="", line_ref=""))
    session.commit()
    d = {"from_station": RL[0], "to_station": RL[1], "external_ref": "ROV č. 2390/2026", "zamer": "predlzenie",
         "start": None, "end": loc(2026, 11, 30, 0).isoformat(), "daily_from": None, "daily_to": None}
    notes: list = []
    out = _register_start(session, "vyluka_zmena", d, None, notes)
    assert (out["daily_from"], out["daily_to"]) == ("22:00", "04:00"), out
    assert as_utc(datetime.fromisoformat(out["start"])) == loc(2026, 11, 23, 22)
    assert as_utc(datetime.fromisoformat(out["end"])) == loc(2026, 11, 30, 4)
    assert notes and "denne 22:00 – 04:00" in notes[0], notes
