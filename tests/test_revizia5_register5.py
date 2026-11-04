"""Piata revízia – oprava oblasti B (register výluk) po kontrole: regresné testy k nálezom kontroly.

- zrušenie dňa nočnej výluky („dňa 25.11.2026 sa ruší“, výluka v nociach 22:00 – 06:00): ruší sa celá noc, ktorá
  v ten deň začína (noc patrí dňu, ktorým začína); noc, ktorá v ňom len končí, ostáva celá – zrušenie nikdy nereže
  noc o polnoci (inak vlak o 02:00 v noci, ktorú ŽSR neruší, dostane VYPRAVIŤ ZELENÁ); jediná noc zrušená dňom,
  v ktorý len končí, sa odmietne s vysvetlením; aj cez zmenu času;
- poistka R5C1 (zápis zrušenia): úsek, o ktorom e-mail píše, že výluka v ňom ostáva, sa nezruší pri rôznych
  formuláciách (zostane v platnosti, nie je zrušená, sa nemení, bude sa konať, platí ROV, bude uzavretý, „hod.“ na
  konci vety, skloňované názvy, dotted „hl. st.“); zrušenie oboch úsekov sa zapíše; veta o inom obmedzení (pomalá
  jazda, POTR, náhradná doprava) či o inom ROV a dni zrušenie neodmietne; „uskutoční sa len od 08:00 do 10:00“ pri
  zrušení 10:00 – 14:00 výluku rozdelí (nie odmietne);
- OR podľa skratky („OR KE“, „ORKE“) a adresy („ke.vyluky@…“): zrušenie len podľa čísla nezruší výluku inej OR;
- zmena viazaná na výluku, ktorú medzitým rozdelila iná udalosť, a staršia zmena či zrušenie iného dňa po rozdelení
  nestratia deň výluky ani sa neoznačia ako nahradené;
- skrátenie celej viacdennej výluky („sa skracuje, bude v dňoch …“) a koniec výluky do odvolania sa zapíšu; zmena
  jedného dňa ostáva zmenou toho dňa;
- texty: „v registri sú 2 výluky … v úseku …“.

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from vlaky.models import Closure
from test_revizia3_terminy import as_utc, drop, events, raw_mail, utc
from test_revizia4_pravidla import ROV_KE, ROV_ZA, _decide, _process, env  # noqa: F401 – fixture env

ROV_TT = "ZSR OR Trnava <vyluky.ortt@zsr.example>"
TZ = ZoneInfo("Europe/Bratislava")


def loc(*parts: int) -> datetime:
    """Miestny čas (Europe/Bratislava) v UTC."""
    return datetime(*parts, tzinfo=TZ).astimezone(timezone.utc)


def _active(session) -> list[Closure]:
    return [c for c in session.scalars(select(Closure).order_by(Closure.start_utc, Closure.id))
            if c.status != "zrusene" and c.proposed_change_of is None]


def _closure(session, a: str, b: str, start: datetime, end: datetime, ref: str, daily: tuple = ("", ""),
             **kw) -> Closure:
    c = Closure(kind=kw.pop("kind", "uplna"), from_station=a, to_station=b, start_utc=start, end_utc=end,
                daily_from=daily[0], daily_to=daily[1], status="overene", source="email", external_ref=ref, **kw)
    session.add(c)
    session.commit()
    return c


def _confirm(session, ev_id: int, **edits):
    from vlaky.posta.spracovanie import confirm_event

    ev = confirm_event(session, ev_id, "dispecer1", edits=edits or None)
    session.commit()
    return ev


def _closed_at(session, when: datetime) -> bool:
    """Plánovač má v čase ``when`` okno platnej výluky (denné opakovanie rozbalené)."""
    from vlaky.planovanie.planovac import closure_windows

    return any(w.start <= when < w.end for w in closure_windows(session, when - timedelta(hours=1),
                                                                 when + timedelta(hours=1)))


# ====================================================================== zrušenie dňa nočnej výluky
@pytest.mark.parametrize("body", [
    "Výluka podľa ROV č. 1500/2026 v úseku Kysak – Košice dňa 25.11.2026 sa ruší. V ostatných dňoch platí bez zmeny.",
    "Výluka podľa ROV č. 1500/2026 v úseku Kysak – Košice v noci z 25.11. na 26.11.2026 sa ruší, ostatné noci platia "
    "bez zmeny.",
    "Nočná výluka podľa ROV č. 1500/2026 (Kysak – Košice) dňa 25.11.2026 sa nekoná. V ostatných nociach sa výluka "
    "uskutoční podľa ROV.",
], ids=["dna_25", "noc_z_na", "nocna_nekona"])
def test_r5r_zrusenie_dna_nocnej_vyluky_neotvori_inu_noc(env, session, body):
    """Výluka v nociach 23./24. – 27./28.11. vždy 22:00 – 06:00; ŽSR ruší noc 25.11. Po „Potvrdiť“ ostanú noci
    24./25. a 26./27. celé (vlak o 02:00 v noci 24./25. nedostane VYPRAVIŤ ZELENÁ), zruší sa noc 25./26."""
    _closure(session, "Kysak", "Košice", loc(2026, 11, 23, 22), loc(2026, 11, 28, 6), "1500/2026",
             daily=("22:00", "06:00"))
    drop(env, "z.eml", raw_mail("ROV c. 1500/2026 - zrusenie", "Dobrý deň,\n\n" + body + "\n", "z1500", sender=ROV_KE,
                                date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 2))
    (ev,) = events(session)
    assert ev.kind == "vyluka_zrusenie"
    ev = _confirm(session, ev.id)
    res = ev.validation["apply_result"]
    assert res["ok"] is True and res["action"] == "rozdelene", res
    assert "noc patrí dňu, ktorým začína" in res["note"]
    for d, h in ((24, 23), (25, 2), (26, 23), (27, 2), (23, 23), (28, 2)):      # noci, ktoré ŽSR neruší
        assert _closed_at(session, loc(2026, 11, d, h)), f"{d}.11. {h}:00 má byť uzavreté"
    for d, h in ((25, 23), (26, 2)):                                             # zrušená noc 25./26.
        assert not _closed_at(session, loc(2026, 11, d, h)), f"{d}.11. {h}:00 má byť prejazdné"
    r = _decide(session, "77250", "Kysak", "Košice", loc(2026, 11, 25, 2), loc(2026, 11, 24, 23, 30))
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), r.summary


@pytest.mark.parametrize("day,cancelled", [(12, True), (13, False)], ids=["den_zaciatku", "den_konca"])
def test_r5r_jedna_noc_zrusena_dnom(env, session, day, cancelled):
    """Jediná noc 12./13.11. (22:00 – 06:00): „dňa 12.11. sa ruší“ ju zruší celú (nie len do polnoci); „dňa 13.11.“
    – noc v ten deň len končí (patrí k 12.11.), zrušenie sa odmietne s vysvetlením a noc ostane celá."""
    c = _closure(session, "Kysak", "Košice", loc(2026, 11, 12, 22), loc(2026, 11, 13, 6), "1501/2026")
    drop(env, "z.eml", raw_mail("ROV c. 1501/2026 - zrusenie", f"Dobrý deň,\n\nVýluka podľa ROV č. 1501/2026 v úseku "
                                f"Kysak – Košice dňa {day}.11.2026 sa ruší.\n", "z1501", sender=ROV_KE,
                                date="Mon, 09 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 9, 9, 2))
    (ev,) = events(session)
    ev = _confirm(session, ev.id)
    session.refresh(c)
    res = ev.validation["apply_result"]
    if cancelled:
        assert res["ok"] is True and c.status == "zrusene" and not _active(session), res
    else:
        assert res["ok"] is False and "noc patrí dňu, ktorým začína" in res["error"], res
        assert "potvrďte znova" not in res["error"] and res.get("retry") is False
        assert c.status == "overene" and [(as_utc(x.start_utc), as_utc(x.end_utc)) for x in _active(session)] == \
            [(loc(2026, 11, 12, 22), loc(2026, 11, 13, 6))]


def test_r5r_zrusenie_noci_cez_zmenu_casu(session):
    """Noci 26./27. – 29./30.03.2027 (22:00 – 06:00, cez prechod na letný čas); „dňa 28.03.2027 sa ruší“ zruší noc
    28./29.03. celú, noc 27./28.03. (prechod času) ostane celá."""
    from vlaky.posta.spracovanie import _cancel_plan

    c = _closure(session, "Kysak", "Košice", loc(2027, 3, 26, 22), loc(2027, 3, 30, 6), "1/2027",
                 daily=("22:00", "06:00"))
    plan = _cancel_plan(session, c, {"start": loc(2027, 3, 28, 0).isoformat(), "end": loc(2027, 3, 29, 0).isoformat()},
                        None)
    assert plan is not None and plan.kind == "rozdelit", plan
    assert [(s, e) for s, e, _keep in plan.parts] == [(loc(2027, 3, 26, 22), loc(2027, 3, 28, 6)),
                                                      (loc(2027, 3, 29, 22), loc(2027, 3, 30, 6))]


# ====================================================================== poistka R5C1: úsek, ktorý ostáva
_ROV_3360 = ("Dňa 09.12.2026 od 7:00 do 13:00 hod. budú úplné výluky v úsekoch Trnava – Senica a Senica – Kúty "
             "(ROV 3360/2026).\n")
_S = "Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší."
STAYS = [
    ("zostane_v_platnosti", _S + " Výluka v úseku Trnava – Senica zostane v platnosti."),
    ("nie_je_zrusena", _S + " Výluka v úseku Trnava – Senica nie je zrušená."),
    ("nemeni_sa", _S + " Výluka v úseku Trnava – Senica sa nemení."),
    ("bude_konat", _S + " V úseku Trnava – Senica sa výluka bude konať podľa ROV 3360/2026."),
    ("trva", _S + " Výluka v úseku Trnava – Senica trvá."),
    ("plati_rov", "Ruší sa výluka v úseku Senica – Kúty. Pre úsek Trnava – Senica platí ROV 3360/2026 v plnom "
                  "rozsahu."),
    ("bude_uzavrety", "Zrušenie výluky sa týka iba úseku Senica – Kúty. Úsek Trnava – Senica bude uzavretý podľa ROV."),
    ("ponechava", _S + " Výluka v úseku Trnava – Senica sa ponecháva."),
    ("sklonovane", _S + " Výluka medzi Trnavou a Senicou sa uskutoční podľa plánu."),
    ("hod_na_konci_vety", "Ruší sa výluka v úseku Senica – Kúty dňa 09.12.2026 od 07:00 do 13:00 hod. Výluka v úseku "
                          "Trnava – Senica ostáva v platnosti."),
    ("pokracovat", _S + " V úseku Trnava – Senica bude výluka pokračovať."),
]


def _register_3360(env, session) -> int:
    drop(env, "rov.eml", raw_mail("ROV 3360/2026 - vyluky na trati 116", _ROV_3360, "rov3360", sender=ROV_TT,
                                  date="Fri, 27 Nov 2026 09:00:00 +0100"))
    _process(session, utc(2026, 11, 27, 8, 2))
    for e in events(session):
        _confirm(session, e.id)
    assert {(c.from_station, c.to_station) for c in _active(session)} == {("Trnava", "Senica"), ("Senica", "Kúty")}
    return len(events(session))


@pytest.mark.parametrize("label,body", STAYS, ids=[k for k, _ in STAYS])
def test_r5r_usek_ktory_podla_emailu_ostava_sa_nezrusi(env, session, label, body):
    """ROV 3360/2026 s dvoma úsekmi; e-mail ruší Senica – Kúty a o Trnava – Senica píše, že výluka ostáva (iné slová
    ako v pôvodných testoch). Po potvrdení všetkých udalostí zrušenia ostáva Trnava – Senica v registri a vlak
    Trnava → Senica 09.12. o 08:00 nedostane VYPRAVIŤ ZELENÁ; Senica – Kúty sa zruší."""
    n0 = _register_3360(env, session)
    drop(env, "z.eml", raw_mail("ROV 3360/2026 - zrusenie", "Dobrý deň,\n\n" + body + "\n\nOR Trnava\n", "z3360",
                                sender=ROV_TT, date="Thu, 03 Dec 2026 14:00:00 +0100"))
    _process(session, utc(2026, 12, 3, 13, 2))
    for e in events(session)[n0:]:
        if e.kind != "vyluka_zrusenie":
            continue
        ev = _confirm(session, e.id)
        res = ev.validation["apply_result"]
        if (e.data.get("from_station"), e.data.get("to_station")) == ("Trnava", "Senica"):
            assert res["ok"] is False and "ostáva" in res["error"] and "netýka výluky" in res["error"], res
    left = {(c.from_station, c.to_station) for c in _active(session)}
    assert left == {("Trnava", "Senica")}, left
    r = _decide(session, "33601", "Trnava", "Senica", loc(2026, 12, 9, 8), loc(2026, 12, 9, 5))
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), r.summary


def test_r5r_poistka_ostava_korpus():
    """Poistka priamo nad textom e-mailu: formulácie „úsek ostáva“ zrušenie úseku Trnava – Senica odmietnu, zrušenie
    oboch úsekov a vety o inom obmedzení, inom ROV či dni nie (stavba vety, nie len slovník)."""
    from vlaky.posta.spracovanie import _stays_text

    target = Closure(id=1, kind="uplna", from_station="Trnava", to_station="Senica", start_utc=loc(2026, 12, 9, 7),
                     end_utc=loc(2026, 12, 9, 13), external_ref="3360/2026", status="overene")
    whole = {"start": loc(2026, 12, 9, 7).isoformat(), "end": loc(2026, 12, 9, 13).isoformat()}
    stays = [b for _k, b in STAYS] + [
        _S + " Výluka v úseku Trnava – Senica nebude zrušená.",
        _S + " Zrušenie sa netýka úseku Trnava – Senica.",
        "Zrušuje sa iba výluka Senica – Kúty, nie Trnava – Senica.",
        "Výluka podľa ROV 3360/2026 dňa 09.12.2026 sa ruší okrem úseku Trnava – Senica.",
        "Výluka v úseku Senica – Kúty sa ruší, výluka v úseku Trnava – Senica bude vykonaná.",
        "Výluka v úseku Trnava – Senica sa uskutoční dňa 09.12.2026 od 07:00 do 13:00 hod. Výluka v úseku Senica – "
        "Kúty sa ruší.",
        _S + " Výluka Trnava-Senica ostáva v platnosti.",
        _S + " Výluka medzi žst. Trnava a žst. Senica sa uskutoční podľa plánu.",
        "Výluky podľa ROV 3360/2026:\n- Senica – Kúty: ruší sa\n- Trnava – Senica: bez zmeny",
        "Výluka v úseku Senica – Kúty sa ruší.\nVýluka v úseku Trnava –\nSenica ostáva v platnosti.",
    ]
    both = [
        "Výluky v úsekoch Trnava – Senica a Senica – Kúty dňa 09.12.2026 sa rušia.",
        "Výluka v úseku Trnava – Senica, ktorá sa mala uskutočniť 09.12.2026, sa ruší. Výluka v úseku Senica – Kúty "
        "sa ruší tiež.",
        "ROV 3360/2026 (Trnava – Senica, Senica – Kúty) sa ruší v celom rozsahu.",
        "Výluky v úsekoch Trnava – Senica a Senica – Kúty sa rušia, náhradná autobusová doprava ostáva bez zmeny.",
        "Výluky v úsekoch Trnava – Senica, Senica – Kúty a Kúty – Hodonín sa rušia.",
        "Trnava – Senica: zrušené. Senica – Kúty: zrušené.",
        "Výluka v úseku Trnava – Senica sa ruší, výluka v úseku Senica – Kúty ostáva.",
        "Výluka Trnava – Senica platná podľa ROV 3360/2026 sa ruší. Výluka Senica – Kúty sa ruší.",
        "Výluka podľa ROV 3360/2026 sa ruší. Vlaky v úseku Trnava – Senica premávajú bez zmeny.",
        "Výluka v úseku Trnava – Senica sa neuskutoční. Výluka v úseku Senica – Kúty sa neuskutoční.",
    ]
    def mail(body: str) -> SimpleNamespace:
        return SimpleNamespace(subject="ROV 3360/2026 - zrusenie",
                               body_text="Dobrý deň,\n\n" + body + "\n\nOR Trnava\n")

    for body in stays:
        assert _stays_text(mail(body), target, whole), body
    for body in both:
        assert _stays_text(mail(body), target, whole) == "", body
    # rozhoduje vlastný text e-mailu, predmet len vtedy, keď text o úseku nič nehovorí
    assert _stays_text(SimpleNamespace(subject="Zrušenie výluky Trnava – Senica, Senica – Kúty",
                                       body_text="Výluka v úseku Senica – Kúty sa ruší. Výluka v úseku Trnava – Senica "
                                                 "ostáva."), target, whole)
    assert _stays_text(SimpleNamespace(subject="ROV 3360/2026 Trnava – Senica – zrušenie", body_text="Výluka sa ruší."),
                       target, whole) == ""
    # „od 08:00 do 10:00 platí“: bráni len zrušeniu toho času (nesprávny termín zrušenia), nie zrušeniu 10:00 – 14:00
    kk = Closure(id=3, kind="uplna", from_station="Kysak", to_station="Košice", start_utc=loc(2026, 11, 12, 8),
                 end_utc=loc(2026, 11, 12, 14), external_ref="1142/2026", status="overene")
    for body in ("Výluka v úseku Kysak – Košice dňa 12.11.2026 od 08:00 do 10:00 hod. platí bez zmeny, v čase od 10:00 "
                 "do 14:00 hod. sa ruší.",
                 "Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice sa dňa 12.11.2026 uskutoční len od 08:00 do "
                 "10:00 hod. Od 10:00 do 14:00 hod. sa výluka ruší."):
        mail = SimpleNamespace(subject="ROV c. 1142/2026", body_text=body)
        assert _stays_text(mail, kk, {"start": loc(2026, 11, 12, 10).isoformat(),
                                      "end": loc(2026, 11, 12, 14).isoformat()}) == "", body
        assert "aj v čase" in _stays_text(mail, kk, {"start": loc(2026, 11, 12, 8).isoformat(),
                                                     "end": loc(2026, 11, 12, 10).isoformat()}), body
    for body in ("Výluka podľa ROV č. 1142/2026 dňa 12.11.2026 sa ruší. Pomalá jazda 50 km/h v úseku Kysak – Košice "
                 "platí naďalej.", "Výluka podľa ROV č. 1142/2026 sa ruší. Na úseku Kysak – Košice ostáva v platnosti "
                 "TLG č. 12/2026.", "Výluka podľa ROV č. 1142/2026 dňa 12.11.2026 sa ruší. NAD v úseku Kysak – Košice "
                 "ostáva."):
        assert _stays_text(SimpleNamespace(subject="ROV c. 1142/2026", body_text=body), kk, None) == "", body
    # skrátený názov „hl. st.“ a skloňovaný tvar
    ba = Closure(id=2, kind="uplna", from_station="Bratislava hl. st.", to_station="Pezinok",
                 start_utc=loc(2026, 12, 9, 7), end_utc=loc(2026, 12, 9, 13), external_ref="77/2026", status="overene")
    for body in ("Výluka v úseku Pezinok – Šenkvice sa ruší. Výluka v úseku Bratislava hl. st. – Pezinok ostáva "
                 "v platnosti.", "Výluka medzi Bratislavou hl.st. a Pezinkom sa uskutoční."):
        assert _stays_text(SimpleNamespace(subject="ROV 77/2026", body_text=body), ba, None), body
    assert _stays_text(SimpleNamespace(subject="ROV 77/2026", body_text="Výluka v úseku Bratislava hl. st. – Pezinok "
                                                                       "sa ruší."), ba, None) == ""


_NEAR = [
    ("pomala_jazda_nadalej", "Výluka podľa ROV č. 1142/2026 dňa 12.11.2026 sa ruší. Pomalá jazda 50 km/h v úseku Kysak "
                             "– Košice platí naďalej."),
    ("potr_ostava", "Výluka podľa ROV č. 1142/2026 sa ruší. Obmedzenie rýchlosti v úseku Kysak – Košice podľa POTR "
                    "ostáva v platnosti."),
    ("nad_bez_zmeny", "Výluka podľa ROV č. 1142/2026 dňa 12.11.2026 sa ruší. Náhradná autobusová doprava v úseku Kysak "
                      "– Košice bude zabezpečená bez zmeny."),
    ("iny_rov_iny_den", "Výluka podľa ROV č. 1142/2026 dňa 12.11.2026 sa ruší. Výluka v úseku Kysak – Košice dňa "
                        "19.11.2026 podľa ROV č. 1150/2026 sa uskutoční."),
]


@pytest.mark.parametrize("label,body", _NEAR, ids=[k for k, _ in _NEAR])
def test_r5r_ine_obmedzenie_ostava_zrusenie_vyluky_sa_zapise(env, session, label, body):
    """Zrušenie jednoúsekovej výluky, keď e-mail o tom istom úseku píše, že ostáva INÉ obmedzenie (pomalá jazda, POTR,
    náhradná doprava) alebo výluka iného ROV v inom dni – výluka sa zruší (nie odmietne s nesprávnym vysvetlením)."""
    c = _closure(session, "Kysak", "Košice", loc(2026, 11, 12, 8), loc(2026, 11, 12, 14), "1142/2026")
    drop(env, "z.eml", raw_mail("Zrusenie vyluky ROV c. 1142/2026", "Dobrý deň,\n\n" + body + "\n\nOR Košice\n",
                                "z1142", sender=ROV_KE, date="Mon, 09 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 9, 9, 2))
    cancels = [e for e in events(session) if e.kind == "vyluka_zrusenie"]
    assert cancels
    for e in cancels:
        ev = _confirm(session, e.id)
        assert ev.validation["apply_result"]["ok"] is True, ev.validation["apply_result"]
    session.refresh(c)
    assert c.status == "zrusene"


@pytest.mark.parametrize("body", [
    "Výluka podľa ROV č. 1142/2026 dňa 12.11.2026 v čase od 10:00 do 14:00 hod. sa ruší. V úseku Kysak – Košice platí "
    "od 08:00 do 10:00 hod. bez zmeny.",
    "Dňa 12.11.2026 sa v čase od 10:00 do 14:00 hod. ruší výluka podľa ROV č. 1142/2026; v úseku Kysak – Košice od "
    "08:00 do 10:00 hod. platí bez zmeny.",
], ids=["veta_plati", "cast_vety_plati"])
def test_r5r_cast_ktora_ostava_v_inej_vete_vyluku_rozdeli(env, session, body):
    """Úsek je len vo vete o časti, ktorá ostáva („v úseku Kysak – Košice platí od 08:00 do 10:00 bez zmeny“), zrušenie
    10:00 – 14:00 je v inej vete či časti vety bez úseku: zrušenie sa zapíše – výluka sa rozdelí, 08:00 – 10:00 ostane
    (nie odmietnutie „výluka v úseku ostáva“)."""
    _closure(session, "Kysak", "Košice", loc(2026, 11, 12, 8), loc(2026, 11, 12, 14), "1142/2026")
    drop(env, "z.eml", raw_mail("ROV c. 1142/2026 - ciastocne zrusenie", "Dobrý deň,\n\n" + body + "\n", "z1142b",
                                sender=ROV_KE, date="Mon, 09 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 9, 9, 2))
    (ev,) = [e for e in events(session) if e.kind == "vyluka_zrusenie"]
    ev = _confirm(session, ev.id)
    assert ev.validation["apply_result"]["ok"] is True, ev.validation["apply_result"]
    assert [(as_utc(c.start_utc), as_utc(c.end_utc)) for c in _active(session)] == \
        [(loc(2026, 11, 12, 8), loc(2026, 11, 12, 10))]
    assert _closed_at(session, loc(2026, 11, 12, 9)) and not _closed_at(session, loc(2026, 11, 12, 11))


@pytest.mark.parametrize("body", [
    "Výluka v úseku Kysak – Košice dňa 12.11.2026 od 08:00 do 10:00 hod. platí bez zmeny, v čase od 10:00 do 14:00 "
    "hod. sa ruší.",
    "Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice sa dňa 12.11.2026 uskutoční len od 08:00 do 10:00 hod. Od "
    "10:00 do 14:00 hod. sa výluka ruší.",
], ids=["plati_prve", "uskutocni_len"])
def test_r5r_zrusenie_s_terminom_casti_ktora_ostava_sa_odmietne(env, session, body):
    """Termín zrušenia je práve čas, ktorý podľa e-mailu ostáva (napr. vzatý z vety „od 08:00 do 10:00 platí“): zrušenie
    sa nezapíše (inak by sa zrušilo 08:00 – 10:00, ktoré ŽSR ponecháva) – dispečer dostane vysvetlenie s časom a radou
    opraviť termín; výluka ostane celá. Ak sa termín zrušenia prečíta správne (10:00 – 14:00), výluka sa rozdelí. Čas,
    ktorý ŽSR ponecháva (08:00 – 10:00), ostáva uzavretý vždy."""
    c = _closure(session, "Kysak", "Košice", loc(2026, 11, 12, 8), loc(2026, 11, 12, 14), "1142/2026")
    drop(env, "z.eml", raw_mail("ROV c. 1142/2026 - ciastocne zrusenie", "Dobrý deň,\n\n" + body + "\n", "z1142c",
                                sender=ROV_KE, date="Mon, 09 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 9, 9, 2))
    for ev in [e for e in events(session) if e.kind == "vyluka_zrusenie"]:
        term = (as_utc(datetime.fromisoformat(ev.data["start"])) if ev.data.get("start") else None,
                as_utc(datetime.fromisoformat(ev.data["end"])) if ev.data.get("end") else None)
        on_section = (ev.data.get("from_station"), ev.data.get("to_station")) == ("Kysak", "Košice")
        res = _confirm(session, ev.id).validation["apply_result"]
        session.refresh(c)
        if on_section and term == (loc(2026, 11, 12, 8), loc(2026, 11, 12, 10)):
            assert res["ok"] is False and "aj v čase, ktorý zrušenie uvádza" in res["error"], res
            assert c.status == "overene" and (as_utc(c.start_utc), as_utc(c.end_utc)) == \
                (loc(2026, 11, 12, 8), loc(2026, 11, 12, 14))
        elif on_section and term == (loc(2026, 11, 12, 10), loc(2026, 11, 12, 14)):
            assert res["ok"] is True, res
    assert _closed_at(session, loc(2026, 11, 12, 9))          # 08:00 – 10:00 ŽSR ponecháva


# ====================================================================== OR podľa skratky a adresy
@pytest.mark.parametrize("sender,signature", [
    ("ZSR <vyluky@zsr.example>", "S pozdravom\nOR KE"),
    ("ZSR <ke.vyluky@zsr.example>", "S pozdravom\nvýluky"),
    ("ZSR <vyluky@zsr.example>", "ŽSR – ORKE, oddelenie GVD"),
], ids=["or_ke", "adresa_ke", "orke"])
def test_r5r_zrusenie_cislom_od_inej_or_podla_skratky(env, session, sender, signature):
    """ROV 15/2026 Žilina – Púchov od OR Žilina; OR Košice ruší „svoj“ ROV 15/2026 len číslom, OR je len skratkou
    v podpise či adrese. Výluka OR Žilina sa nezruší a dispečer dostane vysvetlenie (iná OR)."""
    drop(env, "rov.eml", raw_mail("ROV c. 15/2026 Zilina - Puchov", "Dobrý deň,\n\núplná výluka oboch traťových koľají "
                                  "v úseku Žilina – Púchov dňa 20.11.2026 od 08:00 do 14:00 hod. (ROV č. 15/2026).\n\n"
                                  "OR Žilina\n", "rov15za", sender=ROV_ZA, date="Mon, 09 Nov 2026 09:00:00 +0100"))
    _process(session, utc(2026, 11, 9, 8, 2))
    (rov,) = events(session)
    _confirm(session, rov.id)
    drop(env, "z.eml", raw_mail("Zrusenie ROV c. 15/2026", f"Dobrý deň,\n\nROV č. 15/2026 sa ruší.\n\n{signature}\n",
                                "z15ke", sender=sender, date="Mon, 16 Nov 2026 09:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 8, 2))
    cancel = events(session)[-1]
    assert cancel.kind == "vyluka_zrusenie"
    assert any("OR Žilina" in w and "OR Košice" in w for w in cancel.validation.get("warnings") or []), \
        cancel.validation.get("warnings")
    ev = _confirm(session, cancel.id)
    assert ev.validation["apply_result"]["ok"] is False
    (c,) = _active(session)
    assert c.status == "overene" and (c.from_station, c.to_station) == ("Žilina", "Púchov")


def test_r5r_zrusenie_cislom_bez_or_upozorni(env, session):
    """Zrušenie len podľa čísla z e-mailu, z ktorého OR určiť nemožno, výluke OR Žilina: fronta upozorní, že výluka je
    od OR Žilina a e-mail OR neuvádza (dispečer overí, či ide o tú istú výluku)."""
    drop(env, "rov.eml", raw_mail("ROV c. 15/2026 Zilina - Puchov", "Dobrý deň,\n\núplná výluka oboch traťových koľají "
                                  "v úseku Žilina – Púchov dňa 20.11.2026 od 08:00 do 14:00 hod. (ROV č. 15/2026).\n",
                                  "rov15za2", sender=ROV_ZA, date="Mon, 09 Nov 2026 09:00:00 +0100"))
    _process(session, utc(2026, 11, 9, 8, 2))
    (rov,) = events(session)
    _confirm(session, rov.id)
    drop(env, "z.eml", raw_mail("Zrusenie ROV c. 15/2026", "Dobrý deň,\n\nROV č. 15/2026 sa ruší.\n", "z15x",
                                sender="ZSR <vyluky@zsr.example>", date="Mon, 16 Nov 2026 09:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 8, 2))
    cancel = events(session)[-1]
    assert cancel.kind == "vyluka_zrusenie"
    assert any("je od OR Žilina, e-mail OR neuvádza" in w for w in cancel.validation.get("warnings") or []), \
        cancel.validation.get("warnings")


def test_r5r_or_z_adresy_a_podpisu(cfg):
    """OR z adresy odosielateľa a podpisu: skratky „OR KE“, „ORKE“, „OR-ZV“, adresy „ke.vyluky@zsr…“, „or.kosice@…“."""
    from vlaky.posta.spracovanie import mail_issuer

    def mi(sender: str, body: str = "", subject: str = "ROV") -> str | None:
        return mail_issuer(SimpleNamespace(sender=sender, subject=subject, body_text=body))

    assert mi("vyluky@zsr.example", "S pozdravom\nOR KE") == "KE"
    assert mi("vyluky@zsr.example", "ŽSR – ORKE, oddelenie GVD") == "KE"
    assert mi("vyluky@zsr.example", "OR-ZV") == "ZV"
    assert mi("ke.vyluky@zsr.example") == "KE"
    assert mi("vyluky@ke.zsr.example") == "KE"
    assert mi("or.kosice@zsr.example") == "KE"
    assert mi("vyluky.orza@zsr.example", "OR Košice") == "ZA"                 # adresa má prednosť
    assert mi("vyluky@zsr.example", "Oblastné riaditeľstvo Žilina") == "ZA"
    assert mi("vyluky@zsr.example", "OR Košice, kópia OR Žilina") is None      # viac OR – nedá sa určiť
    assert mi("jan.novak@zsr.example") is None
    assert mi("ba.dispecing@partner.example") is None                          # nie ŽSR
    assert mi("dispecer@partner.co.ke") is None                                # „.ke“ je doména Kene


# ====================================================================== zmena a rozdelená výluka
_ZMENA = ("Dobrý deň,\n\nZmena ROV č. 1315/2026: výluka v úseku Ružomberok – Liptovský Mikuláš dňa {day}.11.2026 "
          "od {od} do {do} hod.\n")


def _ruzomberok(session) -> Closure:
    return _closure(session, "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 23, 8), loc(2026, 11, 27, 14),
                    "1315/2026", daily=("08:00", "14:00"))


def test_r5r_zmena_viazana_na_vyluku_rozdelenu_inou_udalostou(env, session, monkeypatch):
    """Dočasný záznam zmeny B (26.11.) odkazuje na výluku #1 23. – 27.11., ktorú potom rozdelila zmena A (24.11.) – ako
    pri súbežnom potvrdení, keď A záznam B ešte nevidela. Potvrdenie B nesmie presunúť časť 23.11. (na ktorú ostal
    odkaz) – zmena patrí časti, s ktorou sa jej termín prekrýva; žiadny deň sa nestratí."""
    from vlaky.posta import spracovanie

    _ruzomberok(session)
    drop(env, "a.eml", raw_mail("Zmena ROV c. 1315/2026", _ZMENA.format(day=24, od="08:00", do="18:00"), "zmA",
                                sender=ROV_ZA, date="Mon, 16 Nov 2026 10:00:00 +0100"))
    drop(env, "b.eml", raw_mail("Zmena ROV c. 1315/2026", _ZMENA.format(day=26, od="06:00", do="14:00"), "zmB",
                                sender=ROV_ZA, date="Mon, 16 Nov 2026 10:20:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 30))
    ev_a, ev_b = events(session)
    assert [x.proposed_change_of for x in session.scalars(select(Closure).where(Closure.source_event_id == ev_b.id))] \
        == [1]
    monkeypatch.setattr(spracovanie, "_waiting_changes", lambda s, c, e: [])    # A nevidí čakajúcu zmenu B
    assert _confirm(session, ev_a.id).validation["apply_result"]["action"] == "rozdelene"
    monkeypatch.undo()
    res = _confirm(session, ev_b.id).validation["apply_result"]
    assert res["ok"] is True, res
    for d in (23, 24, 25, 26, 27):
        assert _closed_at(session, loc(2026, 11, d, 10)), f"{d}.11. 10:00 má byť uzavreté"
    assert _closed_at(session, loc(2026, 11, 26, 6, 30)) and _closed_at(session, loc(2026, 11, 24, 17))


# staršia udalosť bez úseku (nemá dočasný záznam, úsek doplní dispečer pri potvrdení) – ako v náleze kontroly
_STARSIA = "Dobrý deň,\n\nZmena ROV č. 1315/2026: výluka dňa {day}.11.2026 bude od 06:00 do 14:00 hod.\n"


@pytest.mark.parametrize("older,probe,closed", [
    (_STARSIA.format(day=26), (26, 6, 30), True),                              # zmena iného dňa sa zapíše
    ("Dobrý deň,\n\nVýluka podľa ROV č. 1315/2026 v úseku Ružomberok – Liptovský Mikuláš dňa 26.11.2026 sa ruší.\n",
     (26, 10, 0), False),                                                      # zrušenie iného dňa sa zapíše
    (_STARSIA.format(day=24), (24, 6, 30), False),                             # ten istý deň: novšia zmena má prednosť
], ids=["zmena_iny_den", "zrusenie_iny_den", "ten_isty_den_novsia"])
def test_r5r_starsia_udalost_ineho_dna_po_rozdeleni(env, session, older, probe, closed):
    """Staršia udalosť (zmena či zrušenie 26.11.) potvrdená po novšej zmene 24.11., ktorá výluku rozdelila: novšia
    zmena 26.11. nezapísala – staršia sa zapíše (nie „nahradená“, deň sa nestratí); tá istá časť (24.11.) ostáva
    podľa novšej zmeny (staršia je nahradená)."""
    _ruzomberok(session)
    # ROV 1315/2026 aj na inom úseku: staršia zmena bez úseku ho z registra nedostane (integrácia 5. revízie dopĺňa úsek
    # len pri jednoznačnom čísle) – nemá dočasný záznam, úsek doplní dispečer pri potvrdení (ako v náleze kontroly)
    _closure(session, "Poprad-Tatry", "Spišská Nová Ves", loc(2026, 12, 7, 8), loc(2026, 12, 7, 14), "1315/2026")
    drop(env, "b.eml", raw_mail("ROV c. 1315/2026", older, "starsia", sender=ROV_ZA,
                                date="Mon, 16 Nov 2026 09:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 8, 2))
    drop(env, "a.eml", raw_mail("Zmena ROV c. 1315/2026", _ZMENA.format(day=24, od="08:00", do="18:00"), "novsia",
                                sender=ROV_ZA, date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 2))
    ev_old, ev_new = events(session)
    assert ev_old.id < ev_new.id
    assert _confirm(session, ev_new.id).validation["apply_result"]["action"] == "rozdelene"
    edits = {} if ev_old.kind == "vyluka_zrusenie" else {"from_station": "Ružomberok",
                                                          "to_station": "Liptovský Mikuláš"}
    res = _confirm(session, ev_old.id, **edits).validation["apply_result"]
    assert res["ok"] is True, res
    assert (res["action"] == "nahradene") == (probe[0] == 24), res
    assert _closed_at(session, loc(2026, 11, *probe)) is closed
    for d in (23, 25, 27):
        assert _closed_at(session, loc(2026, 11, d, 10)), f"{d}.11. 10:00 má byť uzavreté"
    assert _closed_at(session, loc(2026, 11, 24, 17))                        # novšia zmena 24.11. 08:00 – 18:00


def test_r5r_zmena_celym_dnom_pri_nocnej_vyluke_nestrati_cast_noci(session):
    """Nová výluka „dňa 26.11. celý deň“ pri výluke v nociach 22:00 – 06:00 zasahuje dve noci (25./26. a 26./27.) –
    nenahradí ich (stratili by sa 25.11. 22:00 – 24:00 a 27.11. 00:00 – 06:00), pôvodná výluka ostane celá a nový
    termín je ďalšia výluka."""
    from vlaky.posta.spracovanie import _change_plan

    c = _closure(session, "Kysak", "Košice", loc(2026, 11, 23, 22), loc(2026, 11, 28, 6), "1500/2026",
                 daily=("22:00", "06:00"))
    fields = {"kind": "uplna", "from_station": "Kysak", "to_station": "Košice", "line_ref": "", "track": "",
              "start_utc": loc(2026, 11, 26, 0), "end_utc": loc(2026, 11, 27, 0), "daily_from": "", "daily_to": "",
              "electric_only": False, "speed_kmh": None, "external_ref": "1500/2026", "until_revoked": False}
    plan = _change_plan(session, c, fields, None)
    assert plan is not None and plan.kind == "pridat", plan


# ====================================================================== skrátenie celej výluky, koniec do odvolania
@pytest.mark.parametrize("setup,body,expect", [
    ("denna", "Zmena ROV č. 1150/2026: výluka v úseku Kysak – Košice sa skracuje, bude v dňoch 23.11. – 25.11.2026 "
              "denne od 08:00 do 14:00 hod.", {23: True, 24: True, 25: True, 26: False, 27: False}),
    ("suvisla", "Zmena ROV č. 1150/2026: výluka v úseku Kysak – Košice sa skracuje a potrvá od 23.11.2026 08:00 do "
                "25.11.2026 14:00 hod.", {23: True, 24: True, 25: True, 26: False, 27: False}),
    ("denna", "Zmena ROV č. 1150/2026: výluka v úseku Kysak – Košice dňa 23.11.2026 sa skracuje, bude len od 08:00 do "
              "12:00 hod.", {23: True, 24: True, 25: True, 26: True, 27: True}),       # zmena jedného dňa
    ("denna", "Zmena ROV č. 1150/2026: výluka v úseku Kysak – Košice v dňoch 23.11. a 24.11.2026 od 08:00 do 14:00 "
              "hod., ostatné dni bez zmeny.", {23: True, 24: True, 25: True, 26: True, 27: True}),   # nie skrátenie
], ids=["denna_skrati", "suvisla_skrati", "jeden_den_skrati", "dva_dni_bez_zmeny"])
def test_r5r_skratenie_celej_viacdennej_vyluky(env, session, setup, body, expect):
    """„Výluka sa skracuje, bude v dňoch 23.11. – 25.11.“ skráti celú výluku 23. – 27.11. (26. a 27.11. sú prejazdné);
    zmena jedného dňa („dňa 23.11. sa skracuje, bude len od 08:00 do 12:00“) ani dva dni bez slova o skrátení
    ostatné dni nezrušia."""
    if setup == "denna":
        _closure(session, "Kysak", "Košice", loc(2026, 11, 23, 8), loc(2026, 11, 27, 14), "1150/2026",
                 daily=("08:00", "14:00"))
    else:
        _closure(session, "Kysak", "Košice", loc(2026, 11, 23, 8), loc(2026, 11, 27, 14), "1150/2026")
    drop(env, "z.eml", raw_mail("Zmena ROV c. 1150/2026", "Dobrý deň,\n\n" + body + "\n", "zm1150", sender=ROV_KE,
                                date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 2))
    for e in events(session):
        assert _confirm(session, e.id).validation["apply_result"]["ok"] is True
    got = {d: _closed_at(session, loc(2026, 11, d, 9)) for d in expect}
    assert got == expect, [(as_utc(c.start_utc), as_utc(c.end_utc), c.daily_from) for c in _active(session)]


def test_r5r_koniec_vyluky_do_odvolania_zmenou(env, session):
    """Výluka od 10.11. 08:00 do odvolania; „Zmena ROV …: výluka … bude platiť od 10.11.2026 08:00 do 20.11.2026
    14:00 hod.“ dá výluke koniec (už nie do odvolania) – nie „register sa nezmenil“."""
    c = _closure(session, "Kysak", "Košice", loc(2026, 11, 10, 8), loc(2026, 12, 10, 8), "1150/2026",
                 until_revoked=True)
    drop(env, "z.eml", raw_mail("Zmena ROV c. 1150/2026", "Dobrý deň,\n\nZmena ROV č. 1150/2026: výluka v úseku Kysak "
                                "– Košice bude platiť od 10.11.2026 08:00 do 20.11.2026 14:00 hod.\n", "zm1150k",
                                sender=ROV_KE, date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 2))
    (ev,) = events(session)
    res = _confirm(session, ev.id).validation["apply_result"]
    assert res["ok"] is True and res["action"] == "aktualizovane", res
    session.refresh(c)
    assert (as_utc(c.start_utc), as_utc(c.end_utc), bool(c.until_revoked)) == \
        (loc(2026, 11, 10, 8), loc(2026, 11, 20, 14), False)


# ====================================================================== texty
def test_r5r_nejednoznacne_cislo_text(env, session):
    """Dve výluky s číslom 1322/2026 v tom istom úseku: zrušenie bez termínu sa nedá priradiť – text „v registri sú
    2 výluky … v úseku …“ (zhoda podmetu s prísudkom, „v úseku“)."""
    _closure(session, "Kysak", "Košice", loc(2026, 12, 1, 8), loc(2026, 12, 1, 12), "1322/2026")
    _closure(session, "Kysak", "Košice", loc(2026, 12, 3, 8), loc(2026, 12, 3, 12), "1322/2026")
    drop(env, "z.eml", raw_mail("Zrusenie ROV c. 1322/2026", "Dobrý deň,\n\nVýluka podľa ROV č. 1322/2026 v úseku "
                                "Kysak – Košice sa ruší.\n", "z1322", sender=ROV_KE,
                                date="Mon, 23 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 23, 9, 2))
    (ev,) = events(session)
    res = _confirm(session, ev.id).validation["apply_result"]
    assert res["ok"] is False
    assert "v registri sú 2 výluky s číslom 1322/2026 v úseku Kysak – Košice" in res["error"], res["error"]
    assert len(_active(session)) == 2
