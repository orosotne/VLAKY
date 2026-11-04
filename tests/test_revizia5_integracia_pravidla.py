"""5. revízia – integrácia oblastí, strana pravidiel (I1): požiadavky registra výluk (oblasť B) na pravidlá e-mailov.

- súborný rozkaz (SROV) s dvoma obmedzeniami toho istého úseku a času (1. TK a vypnutie TV, 1. TK a 2. TK) dá dve
  udalosti – riadok s iným druhom obmedzenia, koľajou či číslom riadku sa nikdy potichu nestratí; text, ktorý len
  opakuje riadok tabuľky, ho nezdvojí, výslovne iný (prísnejší) druh v texte sa nestratí;
- pôvodný termín v e-maile o zmene („… (namiesto 08:00 – 14:00)“, „namiesto 25.11.2026 …“, „(pôvodne …)“) nie je
  druhou zmenou – len poznámka pri skutočnej zmene; okno sa nespáruje s pôvodným dňom presunu;
- upozornenie validácie „Vlak … dňa …“ má slovenský dátum DD.MM.RRRR;
- rušený termín je z časti vety, ktorá výluku ruší („… od 08:00 do 10:00 platí bez zmeny, od 10:00 do 14:00 sa
  ruší“, „… sa uskutoční len od 08:00 do 10:00. Od 10:00 do 14:00 sa výluka ruší.“), nie prvé obdobie odseku;
  zrušenie noci bez času („v noci z 25.11. na 26.11.2026 sa ruší“) má termín noci (okno výluky z e-mailu, inak
  22:00 – 06:00 s poznámkou), nie celý deň – súvislej viacdennej výluke neotvorí deň;
- „Výluka v úseku A – B ani v úseku B – C sa … nevykoná“ zruší oba úseky; pri zrušení viacerých úsekov sa nikdy
  nezruší úsek, o ktorom e-mail píše, že ostáva („zostane v platnosti“, „sa nemení“, „trvá“, „nie je zrušená“,
  „bude sa konať“, „bude uzavretý“, „…, výluka v úseku … nie“); „nie je zrušená“ zrušením nie je.

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from vlaky.models import Closure, ExtractedEvent
from test_revizia3_terminy import drop, events, raw_mail, utc
from test_revizia4_pravidla import ROV_KE, _decide, _process, env  # noqa: F401 – fixture env

TZ = ZoneInfo("Europe/Bratislava")
ROV_TT = "ZSR OR Trnava <vyluky.ortt@zsr.example>"


def loc(*parts: int) -> datetime:
    """Miestny čas (Europe/Bratislava) v UTC."""
    return datetime(*parts, tzinfo=TZ).astimezone(timezone.utc)


def _x(subject: str, body: str, received: datetime = utc(2026, 11, 9, 8), sender: str = "vyluky.orke@zsr.example"):
    from vlaky.posta.extrakcia import extract
    from vlaky.posta.udalosti import MailDocument

    doc = MailDocument(subject=subject, body="Dobrý deň,\n\n" + body + "\n\nS pozdravom\nOR Košice\n", sender=sender,
                       received_at=received)
    return extract(doc, use_llm=False).items


def _l(v) -> str | None:
    """Čas udalosti (UTC ISO) v miestnom čase „DD.MM. HH:MM“."""
    if not v:
        return None
    return datetime.fromisoformat(v).astimezone(TZ).strftime("%d.%m. %H:%M")


def _name(text):
    from vlaky.posta.stanice import stations

    st = stations().match(text) if text else None
    return st.name if st else text


def _sec(it) -> tuple:
    return _name(it.data.get("from_station")), _name(it.data.get("to_station"))


def _notes(items) -> str:
    return " ".join(n for i in items for n in i.notes)


def _show(items) -> list:
    return [(i.kind, i.data) for i in items]


def _closure(session, a: str, b: str, start: datetime, end: datetime, ref: str, daily: tuple = ("", ""),
             **kw) -> Closure:
    c = Closure(kind=kw.pop("kind", "uplna"), from_station=a, to_station=b, start_utc=start, end_utc=end,
                daily_from=daily[0], daily_to=daily[1], status="overene", source="email", external_ref=ref, **kw)
    session.add(c)
    session.commit()
    return c


def _confirm_all(session) -> list[ExtractedEvent]:
    from vlaky.posta.spracovanie import confirm_event

    out = []
    for e in events(session):
        if e.status == "navrh":
            out.append(confirm_event(session, e.id, "dispecer1"))
            session.commit()
    return out


def _closed_at(session, when: datetime, a: str | None = None) -> bool:
    """Plánovač má v čase ``when`` okno platnej výluky (denné opakovanie rozbalené), voliteľne len v úseku od ``a``."""
    from vlaky.planovanie.planovac import closure_windows

    return any(w.start <= when < w.end and (a is None or a in (w.from_station, w.to_station))
               for w in closure_windows(session, when - timedelta(hours=1), when + timedelta(hours=1)))


# ====================================================================== (1) SROV: dve obmedzenia úseku a času
SROV = ("Zasielame SROV č. {n}/2026:\n\nPor.č. | Koľaj | Úsek | Začiatok | Koniec | Druh výluky\n"
        "1   | {r1} | Kysak – Košice | 23.11.2026 08:00 | 23.11.2026 14:00 | {k1}\n"
        "2   | {r2} | Kysak – Košice | 23.11.2026 08:00 | 23.11.2026 14:00 | {k2}\n")


@pytest.mark.parametrize("r1,k1,r2,k2,want", [
    ("1. TK", "výluka koľaje", "TV", "vypnutie trakčného vedenia", [("kolaj", "1"), ("trakcne_vedenie", "TV")]),
    ("TV", "vypnutie trakčného vedenia", "1. TK", "výluka koľaje", [("kolaj", "1"), ("trakcne_vedenie", "TV")]),
    ("1. TK", "výluka koľaje", "2. TK", "výluka koľaje", [("kolaj", "1"), ("kolaj", "2")]),
], ids=["tk_tv", "tv_tk", "tk1_tk2"])
def test_srov_dve_obmedzenia_toho_isteho_useku_a_casu_su_dve_udalosti(cfg, r1, k1, r2, k2, want):
    """[R5L1 a] Riadky SROV s rovnakým úsekom aj časom, ale inou koľajou či druhom obmedzenia sú dve udalosti (nie
    jedna, druhý riadok sa nesmie potichu stratiť – elektrický vlak by dostal ZELENÚ pod vypnutým TV)."""
    items = _x("SROV č. 61/2026", SROV.format(n=61, r1=r1, k1=k1, r2=r2, k2=k2))
    closures = [i for i in items if i.kind == "vyluka_nova"]
    assert sorted((i.data.get("kind"), i.data.get("track")) for i in closures) == want, _show(items)
    assert {i.data.get("external_ref") for i in closures} == {"61/2026/1", "61/2026/2"}
    assert all(_sec(i) == ("Kysak", "Košice") and (_l(i.data["start"]), _l(i.data["end"]))
               == ("23.11. 08:00", "23.11. 14:00") for i in closures)


def test_srov_bez_stlpca_poradia_a_bez_stlpca_kolaje_nestrati_riadok(cfg):
    """[R5L1 a] Aj bez stĺpca poradia (dve koľaje) a bez stĺpca koľaje (koľaj a TV len v druhu výluky) ostanú oba
    riadky – nie riadok s vyššou istotou namiesto prísnejšieho."""
    body = ("Zasielame SROV č. 64/2026:\n\nKoľaj | Úsek | Začiatok | Koniec | Druh výluky\n"
            "1. TK | Kysak – Košice | 23.11.2026 08:00 | 23.11.2026 14:00 | výluka koľaje\n"
            "2. TK | Kysak – Košice | 23.11.2026 08:00 | 23.11.2026 14:00 | výluka koľaje\n")
    items = _x("SROV č. 64/2026", body)
    assert sorted(i.data.get("track") for i in items if i.kind == "vyluka_nova") == ["1", "2"], _show(items)
    body = ("Zasielame SROV č. 65/2026:\n\nPor.č. | Úsek | Začiatok | Koniec | Druh výluky\n"
            "1 | Kysak – Košice | 23.11.2026 08:00 | 23.11.2026 14:00 | výluka koľaje\n"
            "2 | Kysak – Košice | 23.11.2026 08:00 | 23.11.2026 14:00 | vypnutie trakčného vedenia\n")
    items = _x("SROV č. 65/2026", body)
    kinds = sorted(i.data.get("kind") for i in items if i.kind == "vyluka_nova")
    assert kinds == ["trakcne_vedenie", "uplna"], _show(items)


def test_srov_text_opakujuci_riadok_nezdvoji_vyslovne_prisnejsi_druh_ostane(cfg):
    """[R5L1 a] Úvodná veta, ktorá len opakuje úsek a čas tabuľky (druh v nej neuvedený), riadky nezdvojí; výslovne
    úplná výluka v texte pri riadku „1. TK“ sa nestratí (prísnejší druh ostane ako ďalšia udalosť)."""
    body = ("Zasielame SROV č. 61/2026 – výluka v úseku Kysak – Košice dňa 23.11.2026 od 08:00 do 14:00 hod.:\n\n"
            + SROV.format(n=61, r1="1. TK", k1="výluka koľaje", r2="TV", k2="vypnutie trakčného vedenia")
            .split(":\n\n", 1)[1])
    items = _x("SROV č. 61/2026", body)
    assert sorted(i.data.get("kind") for i in items) == ["kolaj", "trakcne_vedenie"], _show(items)
    body = ("Úplná výluka v úseku Kysak – Košice dňa 23.11.2026 od 08:00 do 14:00 hod. podľa SROV č. 61/2026:\n\n"
            "Por.č. | Koľaj | Úsek | Začiatok | Koniec | Druh výluky\n"
            "1   | 1. TK | Kysak – Košice | 23.11.2026 08:00 | 23.11.2026 14:00 | výluka koľaje\n")
    items = _x("SROV č. 61/2026", body)
    assert sorted(i.data.get("kind") for i in items) == ["kolaj", "uplna"], _show(items)


def test_srov_rovnaky_riadok_dvakrat_je_jedna_udalost_kontrola(cfg):
    """Kontrola: ten istý riadok (rovnaké číslo riadku, koľaj, druh, úsek a čas) dvakrát je jedna udalosť."""
    body = ("Zasielame SROV č. 63/2026:\n\nPor.č. | Koľaj | Úsek | Začiatok | Koniec | Druh výluky\n"
            "1   | 1. TK | Kysak – Košice | 23.11.2026 08:00 | 23.11.2026 14:00 | výluka koľaje\n"
            "1   | 1. TK | Kysak – Košice | 23.11.2026 08:00 | 23.11.2026 14:00 | výluka koľaje\n")
    items = _x("SROV č. 63/2026", body)
    assert len(items) == 1 and items[0].data.get("track") == "1", _show(items)


def test_srov_tk_a_tv_po_potvrdeni_elektricky_vlak_nedostane_zelenu(env, session):
    """[R5L1 a, e2e] SROV s riadkami „1. TK – výluka koľaje“ a „TV – vypnutie trakčného vedenia“ (rovnaký úsek
    a čas): fronta má dve udalosti, po potvrdení sú v registri obe obmedzenia a elektrický vlak Kysak → Košice
    23.11. o 10:00 nedostane VYPRAVIŤ ZELENÁ."""
    body = SROV.format(n=61, r1="1. TK", k1="výluka koľaje", r2="TV", k2="vypnutie trakčného vedenia")
    drop(env, "srov.eml", raw_mail("SROV c. 61/2026", body, "srov61", sender=ROV_KE,
                                   date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 2))
    assert sorted(e.data.get("kind") for e in events(session)) == ["kolaj", "trakcne_vedenie"]
    _confirm_all(session)
    kinds = sorted(c.kind for c in session.scalars(select(Closure)) if c.status != "zrusene"
                   and c.proposed_change_of is None)
    assert kinds == ["kolaj", "trakcne_vedenie"], kinds
    r = _decide(session, "77623", "Kysak", "Košice", loc(2026, 11, 23, 10), loc(2026, 11, 23, 8, 30))
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), r.summary


# ====================================================================== (2) pôvodný termín pri zmene
@pytest.mark.parametrize("body,new,old", [
    ("Zmena ROV č. 1315/2026: výluka v úseku Ružomberok – Liptovský Mikuláš dňa 24.11.2026 bude od 08:00 do 18:00 "
     "hod. (namiesto 08:00 – 14:00).", ("24.11. 08:00", "24.11. 18:00"), "namiesto 08:00 – 14:00"),
    ("Zmena ROV č. 1315/2026: výluka v úseku Ružomberok – Liptovský Mikuláš dňa 24.11.2026 bude od 08:00 do 18:00 "
     "hod. (pôvodne 08:00 – 14:00).", ("24.11. 08:00", "24.11. 18:00"), "pôvodne 08:00 – 14:00"),
    ("Výluka podľa ROV č. 1315/2026 v úseku Ružomberok – Liptovský Mikuláš sa uskutoční 26.11.2026 od 08:00 do 14:00 "
     "hod. namiesto 25.11.2026 od 08:00 do 14:00 hod.", ("26.11. 08:00", "26.11. 14:00"), "namiesto 25.11.2026"),
    ("Výluka v úseku Ružomberok – Liptovský Mikuláš bude 26.11.2026 od 08:00 do 14:00 hod. místo 25.11.2026 od 08:00 "
     "do 14:00 hod.", ("26.11. 08:00", "26.11. 14:00"), "místo 25.11.2026"),
], ids=["namiesto_okno", "povodne_okno", "namiesto_den", "misto_cz"])
def test_zmena_s_povodnym_terminom_je_jedna_zmena(cfg, body, new, old):
    """[R5L0, strana pravidiel] Pôvodný termín za „namiesto“ či „pôvodne“ nie je druhou udalosťou vyluka_zmena –
    zmena je jedna, s novým termínom, a poznámka uvádza pôvodný termín."""
    items = _x("Zmena ROV č. 1315/2026", body)
    changes = [i for i in items if i.kind in ("vyluka_zmena", "vyluka_nova")]
    assert len(changes) == 1, _show(items)
    assert changes[0].kind == "vyluka_zmena" and (_l(changes[0].data["start"]), _l(changes[0].data["end"])) == new
    assert any("Pôvodný termín" in n and old in n for n in changes[0].notes), changes[0].notes


def test_zmena_pozna_novy_den_aj_ked_povodny_je_pri_okne(cfg):
    """[R5L0] „dňa 24.11.2026 (namiesto 23.11.2026) od 08:00 do 14:00“ – okno patrí k novému dňu 24.11., nie
    k pôvodnému 23.11. (zmena by inak presunula výluku na pôvodný deň)."""
    items = _x("Zmena ROV č. 1315/2026", "Zmena ROV č. 1315/2026: výluka v úseku Ružomberok – Liptovský Mikuláš bude "
               "dňa 24.11.2026 (namiesto 23.11.2026) od 08:00 do 14:00 hod.")
    assert [(i.kind, _l(i.data.get("start")), _l(i.data.get("end"))) for i in items] == [
        ("vyluka_zmena", "24.11. 08:00", "24.11. 14:00")], _show(items)


def test_povodne_a_novy_termin_v_jednej_vete_novy_ostane(cfg):
    """„Pôvodne 25.11.2026, nový termín 26.11.2026 od 08:00 do 14:00“ – nový termín sa nestratí (nie je pôvodný)."""
    items = _x("Zmena ROV č. 1315/2026", "Výluka v úseku Ružomberok – Liptovský Mikuláš sa presúva. Pôvodne "
               "25.11.2026, nový termín 26.11.2026 od 08:00 do 14:00 hod.")
    assert [(i.kind, _l(i.data.get("start")), _l(i.data.get("end"))) for i in items] == [
        ("vyluka_zmena", "26.11. 08:00", "26.11. 14:00")], _show(items)


def test_zmena_namiesto_v_registri_jedna_udalost_vo_fronte(env, session):
    """[R5L0, e2e] Zmena jedného dňa „… od 08:00 do 18:00 hod. (namiesto 08:00 – 14:00)“ dá vo fronte jednu udalosť
    (nie druhú zmenu s pôvodným časom, ktorá by sa po potvrdení zapísala ako ďalšia výluka)."""
    _closure(session, "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 23, 8), loc(2026, 11, 27, 14), "1315/2026",
             daily=("08:00", "14:00"))
    drop(env, "z.eml", raw_mail("Zmena ROV c. 1315/2026", "Dobrý deň,\n\nZmena ROV č. 1315/2026: výluka v úseku "
                                "Ružomberok – Liptovský Mikuláš dňa 24.11.2026 bude od 08:00 do 18:00 hod. (namiesto "
                                "08:00 – 14:00).\n", "z1315", sender=ROV_KE, date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 2))
    evs = events(session)
    assert [(e.kind, _l(e.data.get("start")), _l(e.data.get("end"))) for e in evs] == [
        ("vyluka_zmena", "24.11. 08:00", "24.11. 18:00")]


# ====================================================================== (3) dátum v upozornení validácie
def test_upozornenie_vlak_dna_ma_slovensky_datum(session, fixed_now):
    """[R5U6] „Vlak 54321 dňa 14.11.2026 zatiaľ nie je v systéme.“ – dátum DD.MM.RRRR, nie ISO."""
    from vlaky.posta.validacia import validate_event

    pts = [{"station": "Čadca", "arr": None, "dep": "2026-11-14T05:10:00+00:00"},
           {"station": "Žilina", "arr": "2026-11-14T05:52:00+00:00", "dep": "2026-11-14T06:05:00+00:00"}]
    vr = validate_event(session, "trasa_pridelena", {"train_number": "54321", "date": "2026-11-14", "points": pts},
                        confidence=0.95)
    assert "Vlak 54321 dňa 14.11.2026 zatiaľ nie je v systéme." in vr.warnings, vr.warnings
    assert not any("2026-11-14" in w for w in vr.warnings)


# ====================================================================== (4) rušený termín z vety o zrušení
@pytest.mark.parametrize("body,term,partial", [
    ("Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice dňa 12.11.2026 v čase od 08:00 do 10:00 hod. platí bez "
     "zmeny, v čase od 10:00 do 14:00 hod. sa ruší.", ("12.11. 10:00", "12.11. 14:00"), True),
    ("Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice sa dňa 12.11.2026 uskutoční len od 08:00 do 10:00 hod. Od "
     "10:00 do 14:00 hod. sa výluka ruší.", ("12.11. 10:00", "12.11. 14:00"), True),
    ("Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice dňa 12.11.2026 platí bez zmeny, v čase od 10:00 do 14:00 "
     "hod. sa však ruší.", ("12.11. 10:00", "12.11. 14:00"), True),
    ("Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice dňa 12.11.2026 od 08:00 do 10:00 hod. platí a od 10:00 do "
     "14:00 hod. sa ruší.", ("12.11. 10:00", "12.11. 14:00"), True),
    ("Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice dňa 24.11.2026 platí bez zmeny, dňa 25.11.2026 sa ruší.",
     ("25.11. 00:00", "26.11. 00:00"), True),
    ("Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice v dňoch 24., 25. a 26.11.2026 platí bez zmeny, dňa "
     "27.11.2026 sa ruší.", ("27.11. 00:00", "28.11. 00:00"), True),
    ("Výluka v úseku Kysak – Košice, ktorá je naplánovaná od 25.11.2026 do 27.11.2026, sa dňa 26.11.2026 ruší "
     "(ROV č. 1142/2026).", ("26.11. 00:00", "27.11. 00:00"), False),
    ("Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice dňa 12.11.2026 od 10:00 hod. sa ruší.",
     ("12.11. 10:00", "13.11. 00:00"), False),
], ids=["plati_rusi", "uskutocni_len", "bez_zmeny_bez_casu", "plati_a_rusi", "dva_dni", "zoznam_dni",
        "vedlajsia_veta", "od_casu"])
def test_zrusenie_ma_termin_z_casti_vety_ktora_rusi(cfg, body, term, partial):
    """[R5L2, strana pravidiel] Rušený termín je z časti vety s výrazom zrušenia – nie čas, ktorý ostáva („platí bez
    zmeny“, „uskutoční sa len …“), ani prvé obdobie odseku či vedľajšej vety; úsek vety ostáva úsekom zrušenia."""
    items = _x("ROV č. 1142/2026", body)
    assert [i.kind for i in items] == ["vyluka_zrusenie"], _show(items)
    it = items[0]
    assert _sec(it) == ("Kysak", "Košice") and it.data.get("external_ref") == "1142/2026", it.data
    assert (_l(it.data.get("start")), _l(it.data.get("end"))) == term, it.data
    assert ("Ruší sa len časť výluky" in _notes(items)) == partial, it.notes
    assert "zrušenie sa jej netýka" not in _notes(items)


@pytest.mark.parametrize("subject,body,term,listed", [
    ("ROV č. 1500/2026", "Výluka podľa ROV č. 1500/2026 v úseku Kysak – Košice v noci z 25.11. na 26.11.2026 sa ruší.",
     ("25.11. 22:00", "26.11. 06:00"), "noc 25.11./26.11.2026"),
    ("ROV 4411/2026 – zrušenie jednej noci", "Ruší sa výluka v noci 24./25.11.2026. Výluky v ostatných nociach sa "
     "uskutočnia podľa ROV 4411/2026.", ("24.11. 22:00", "25.11. 06:00"), "noc 24.11./25.11.2026"),
    ("ROV č. 1500/2026", "Výluka podľa ROV č. 1500/2026 v úseku Kysak – Košice v nociach 25./26.11. a 26./27.11.2026 sa "
     "ruší.", ("25.11. 22:00", "27.11. 06:00"), "noci 25.11./26.11.2026, 26.11./27.11.2026"),
], ids=["noc_z_na", "noc_lomka", "dve_noci"])
def test_zrusenie_noci_bez_casu_ma_nocny_termin(cfg, subject, body, term, listed):
    """[R5L2 noc] Zrušenie noci bez času má termín noci 22:00 – 06:00 s poznámkou (nie celý deň 00:00 – 24:00)."""
    items = _x(subject, body)
    assert [i.kind for i in items] == ["vyluka_zrusenie"], _show(items)
    assert (_l(items[0].data.get("start")), _l(items[0].data.get("end"))) == term, items[0].data
    assert any(listed in n and "22:00 – 06:00" in n for n in items[0].notes), items[0].notes


@pytest.mark.parametrize("subject,body", [
    ("ROV č. 1500/2026", "Výluka podľa ROV č. 1500/2026 v úseku Kysak – Košice sa v noci z 25.11. na 26.11.2026 ruší."
     "\n\nPripomíname, že výluka trvá v ostatných nociach vždy od 22:30 do 04:30 hod."),
    ("Zrušenie nočnej výluky ROV č. 1500/2026 (22:30 – 04:30)", "Výluka podľa ROV č. 1500/2026 v úseku Kysak – Košice "
     "v noci z 25.11. na 26.11.2026 sa ruší."),
], ids=["iny_odsek", "predmet"])
def test_zrusenie_noci_s_oknom_vyluky_v_emaile_ma_okno_vyluky(cfg, subject, body):
    """[R5L2 noc] Okno nočnej výluky uvedené inde v e-maile („vždy od 22:30 do 04:30“, v predmete) je termínom zrušenej
    noci (nie celý deň, nie 22:00 – 06:00)."""
    items = _x(subject, body)
    assert [(i.kind, _l(i.data.get("start")), _l(i.data.get("end"))) for i in items] == [
        ("vyluka_zrusenie", "25.11. 22:30", "26.11. 04:30")], _show(items)
    assert "okno výluky z e-mailu" in _notes(items)


def test_zrusenie_noci_s_oknom_vo_vete_kontrola(cfg):
    """Kontrola: okno vo vete o zrušení („(denne 22:30 – 04:30) sa v noci z 25.11. na 26.11.2026 ruší“) ostáva
    termínom zrušenia."""
    items = _x("ROV č. 1500/2026", "Nočná výluka podľa ROV č. 1500/2026 v úseku Kysak – Košice (denne 22:30 – 04:30) "
               "sa v noci z 25.11. na 26.11.2026 ruší. V ostatných nociach platí bez zmeny.")
    assert [(i.kind, _l(i.data.get("start")), _l(i.data.get("end"))) for i in items] == [
        ("vyluka_zrusenie", "25.11. 22:30", "26.11. 04:30")], _show(items)


def test_zrusenie_casu_bez_dna_ma_poznamku(cfg):
    """Čas bez dňa v celom e-maile („v čase od 10:00 do 14:00 sa ruší“) – termín sa nevymyslí, ale poznámka povie,
    že bez doplneného termínu by sa zrušila celá výluka; úsek vety ostáva úsekom zrušenia."""
    items = _x("ROV č. 1142/2026", "Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice v čase od 08:00 do 10:00 hod. "
               "platí bez zmeny, v čase od 10:00 do 14:00 hod. sa ruší.")
    assert [(i.kind, _sec(i), i.data.get("start")) for i in items] == [("vyluka_zrusenie", ("Kysak", "Košice"), None)]
    assert "Zrušenie uvádza len čas 10:00 – 14:00 bez dňa" in _notes(items), items[0].notes


@pytest.mark.parametrize("second", [
    "Nová výluka v úseku Kysak – Košice sa uskutoční len dňa 14.11.2026 od 08:00 do 14:00 hod.",
    "Výluka sa uskutoční len dňa 14.11.2026 od 08:00 do 14:00 hod.",
], ids=["nova_vyluka", "iny_den"])
def test_novy_termin_s_len_po_zruseni_sa_nestrati_kontrola(cfg, second):
    """Kontrola: „uskutoční sa len …“ v inom dni, než ktorý sa ruší (alebo „nová výluka“), je nový termín – nesmie
    sa stratiť ako „zvyšok rušenej výluky“ (ostane nová výluka 14.11. a zrušenie 12.11.)."""
    items = _x("ROV č. 1142/2026", "Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice dňa 12.11.2026 sa ruší. "
               + second)
    assert sorted((i.kind, _l(i.data.get("start"))) for i in items) == [
        ("vyluka_nova", "14.11. 08:00"), ("vyluka_zrusenie", "12.11. 00:00")], _show(items)


def test_zrusenie_zvysku_vyluky_nezrusi_cas_ktory_ostava(cfg):
    """„… sa uskutoční len od 08:00 do 10:00 hod. Zvyšok výluky sa ruší.“ – rušený termín nie je čas, ktorý ostáva
    (08:00 – 10:00); bez termínu zrušenia poznámka žiada doplniť ho."""
    items = _x("ROV č. 1142/2026", "Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice sa dňa 12.11.2026 uskutoční "
               "len od 08:00 do 10:00 hod. Zvyšok výluky sa ruší.")
    assert [(i.kind, _sec(i), i.data.get("start")) for i in items] == [("vyluka_zrusenie", ("Kysak", "Košice"), None)]
    assert "časť výluky ostáva" in _notes(items)


@pytest.mark.parametrize("body", [
    "Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice dňa 12.11.2026 v čase od 08:00 do 10:00 hod. platí bez "
    "zmeny, v čase od 10:00 do 14:00 hod. sa ruší.",
    "Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice sa dňa 12.11.2026 uskutoční len od 08:00 do 10:00 hod. Od "
    "10:00 do 14:00 hod. sa výluka ruší.",
], ids=["plati_rusi", "uskutocni_len"])
def test_zrusenie_casti_dna_po_potvrdeni_ostane_cast_ktora_plati(env, session, body):
    """[R5L2, e2e] ROV 1142/2026 Kysak – Košice 12.11. 08:00 – 14:00; „… od 08:00 do 10:00 hod. platí bez zmeny,
    v čase od 10:00 do 14:00 hod. sa ruší.“ (aj „… sa uskutoční len od 08:00 do 10:00. Od 10:00 do 14:00 sa výluka
    ruší.“) – zrušenie 10:00 – 14:00 sa po potvrdení zapíše (výluka sa rozdelí): o 09:00 je úsek uzavretý (vlak
    nedostane VYPRAVIŤ ZELENÁ), o 11:00 prejazdný."""
    _closure(session, "Kysak", "Košice", loc(2026, 11, 12, 8), loc(2026, 11, 12, 14), "1142/2026")
    drop(env, "z.eml", raw_mail("ROV c. 1142/2026", "Dobrý deň,\n\n" + body + "\n", "z1142", sender=ROV_KE,
                                date="Mon, 09 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 9, 9, 2))
    (ev,) = events(session)
    assert (ev.kind, _l(ev.data.get("start")), _l(ev.data.get("end"))) == ("vyluka_zrusenie", "12.11. 10:00",
                                                                           "12.11. 14:00")
    (ev,) = _confirm_all(session)
    assert ev.validation["apply_result"]["ok"] is True, ev.validation["apply_result"]
    assert _closed_at(session, loc(2026, 11, 12, 9)) and not _closed_at(session, loc(2026, 11, 12, 11))
    r = _decide(session, "77112", "Kysak", "Košice", loc(2026, 11, 12, 9), loc(2026, 11, 12, 7))
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), r.summary


def test_zrusenie_noci_suvislej_vyluky_neotvori_den(env, session):
    """[R5L2 noc, e2e] Súvislá výluka 24.11. 08:00 – 27.11. 14:00; „v noci z 25.11. na 26.11.2026 sa ruší“ – zruší
    sa noc 22:00 – 06:00, deň 25.11. ostane uzavretý (vlak 25.11. o 12:00 nedostane VYPRAVIŤ ZELENÁ)."""
    _closure(session, "Kysak", "Košice", loc(2026, 11, 24, 8), loc(2026, 11, 27, 14), "1502/2026")
    drop(env, "z.eml", raw_mail("ROV c. 1502/2026", "Dobrý deň,\n\nVýluka podľa ROV č. 1502/2026 v úseku Kysak – Košice "
                                "v noci z 25.11. na 26.11.2026 sa ruší.\n", "z1502", sender=ROV_KE,
                                date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 2))
    (ev,) = events(session)
    assert (_l(ev.data.get("start")), _l(ev.data.get("end"))) == ("25.11. 22:00", "26.11. 06:00"), ev.data
    (ev,) = _confirm_all(session)
    assert ev.validation["apply_result"]["ok"] is True, ev.validation["apply_result"]
    for when in (loc(2026, 11, 25, 12), loc(2026, 11, 25, 21), loc(2026, 11, 26, 7)):
        assert _closed_at(session, when), when
    assert not _closed_at(session, loc(2026, 11, 26, 2))
    r = _decide(session, "77250", "Kysak", "Košice", loc(2026, 11, 25, 12), loc(2026, 11, 25, 9))
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), r.summary


def test_zrusenie_noci_nocnej_vyluky_zrusi_len_tu_noc(env, session):
    """[R5L2 noc, e2e] Výluka v nociach 23./24. – 27./28.11. 22:00 – 06:00; zrušenie „v noci z 25.11. na 26.11.“ má
    termín noci a po potvrdení ostanú ostatné noci uzavreté."""
    _closure(session, "Kysak", "Košice", loc(2026, 11, 23, 22), loc(2026, 11, 28, 6), "1500/2026",
             daily=("22:00", "06:00"))
    drop(env, "z.eml", raw_mail("ROV c. 1500/2026 - zrusenie", "Dobrý deň,\n\nVýluka podľa ROV č. 1500/2026 v úseku "
                                "Kysak – Košice v noci z 25.11. na 26.11.2026 sa ruší, ostatné noci platia bez zmeny.\n",
                                "z1500", sender=ROV_KE, date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 2))
    (ev,) = events(session)
    assert (_l(ev.data.get("start")), _l(ev.data.get("end"))) == ("25.11. 22:00", "26.11. 06:00"), ev.data
    _confirm_all(session)
    for d, h in ((24, 23), (25, 2), (26, 23), (27, 2)):
        assert _closed_at(session, loc(2026, 11, d, h)), (d, h)
    assert not _closed_at(session, loc(2026, 11, 25, 23)) and not _closed_at(session, loc(2026, 11, 26, 2))


# ====================================================================== (5) „ani … nevykoná“ a úseky, ktoré ostávajú
@pytest.mark.parametrize("body", [
    "Výluka v úseku Trnava – Senica ani v úseku Senica – Kúty sa dňa 09.12.2026 nevykoná.",
    "Výluky v úsekoch Trnava – Senica a Senica – Kúty sa dňa 09.12.2026 nevykonajú.",
], ids=["ani_nevykona", "nevykonaju"])
def test_nevykona_zrusi_oba_useky(cfg, body):
    """[R5C1] Zápor „nevykoná“ („nevykonajú“) je zrušenie – oba úseky (nie dve nové výluky bez termínu)."""
    items = _x("ROV č. 3360/2026", body)
    assert sorted((i.kind, _sec(i)) for i in items) == [("vyluka_zrusenie", ("Senica", "Kúty")),
                                                        ("vyluka_zrusenie", ("Trnava", "Senica"))], _show(items)
    assert all((_l(i.data.get("start")), _l(i.data.get("end"))) == ("09.12. 00:00", "10.12. 00:00") for i in items)


STAYS = [
    "Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší. Výluka v úseku Trnava – Senica zostane v platnosti.",
    "Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší. Výluka v úseku Trnava – Senica sa nemení.",
    "Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší. Výluka v úseku Trnava – Senica trvá.",
    "Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší. Výluka v úseku Trnava – Senica nie je zrušená.",
    "Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší. Výluka v úseku Trnava – Senica nebola zrušená.",
    "Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší. Výluka v úseku Trnava – Senica bude sa konať podľa ROV.",
    "Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší. Úsek Trnava – Senica bude uzavretý podľa ROV.",
    "Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší, výluka v úseku Trnava – Senica zostane v platnosti.",
    "Výluka v úseku Trnava – Senica zostane v platnosti. Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší.",
    "Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší a výluka v úseku Trnava – Senica zostáva v platnosti.",
    "Ruší sa výluka v úseku Senica – Kúty dňa 09.12.2026, výluka v úseku Trnava – Senica nie.",
    "Ruší sa výluka v úseku Senica – Kúty dňa 09.12.2026, nie v úseku Trnava – Senica.",
    "Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší. Výluka v úseku Trnava – Senica dňa 09.12.2026 ostane "
    "v platnosti.",
]


@pytest.mark.parametrize("body", STAYS, ids=["zostane", "nemeni", "trva", "nie_je_zrusena", "nebola_zrusena",
                                             "bude_sa_konat", "bude_uzavrety", "ciarka", "najprv_ostava", "a_zostava",
                                             "ts_nie", "nie_v_useku", "ostane_s_datumom"])
def test_zrusenie_neodstrani_usek_ktory_ostava(cfg, body):
    """[R5C1] Úsek, o ktorom e-mail píše, že jeho výluka ostáva, sa nikdy neruší – fronta ponúkne len zrušenie úseku
    Senica – Kúty (s poznámkou o úseku, ktorý ostáva), nie zrušenie (ani novú výluku) Trnava – Senica."""
    items = _x("ROV č. 3360/2026", body)
    assert [(i.kind, _sec(i)) for i in items] == [("vyluka_zrusenie", ("Senica", "Kúty"))], _show(items)
    assert (_l(items[0].data.get("start")), _l(items[0].data.get("end"))) == ("09.12. 00:00", "10.12. 00:00")
    assert "Trnava – Senica ostáva" in _notes(items)


@pytest.mark.parametrize("body,want", [
    ("Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší. Výluka v úseku Trnava – Senica sa uskutoční podľa "
     "ROV 3360/2026.", [("Senica", "Kúty")]),
    ("Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší. Pre úsek Trnava – Senica platí ROV č. 3360/2026.",
     [("Senica", "Kúty")]),
    ("Výluka v úseku Trnava – Senica a v úseku Senica – Kúty dňa 09.12.2026 sa ruší.",
     [("Senica", "Kúty"), ("Trnava", "Senica")]),
], ids=["uskutocni", "plati_rov", "oba_kontrola"])
def test_zrusenie_viacerych_useku_kontrola(cfg, body, want):
    """Kontrola: formulácie, ktoré už pred touto zmenou rušili len správny úsek, a zrušenie oboch úsekov."""
    items = _x("ROV č. 3360/2026", body)
    assert sorted((i.kind, _sec(i)) for i in items) == [("vyluka_zrusenie", w) for w in want], _show(items)


def test_nie_je_zrusena_nie_je_zrusenie(cfg):
    """[R5C1] „Výluka … nie je zrušená, uskutoční sa podľa plánu.“ – nie zrušenie výluky (by ju zrušilo)."""
    items = _x("ROV č. 1600/2026", "Výluka v úseku Kysak – Košice dňa 20.11.2026 od 08:00 do 14:00 hod. nie je "
               "zrušená, uskutoční sa podľa plánu.")
    assert not any(i.kind == "vyluka_zrusenie" for i in items), _show(items)
    assert [(i.kind, _sec(i)) for i in items] == [("vyluka_nova", ("Kysak", "Košice"))]


@pytest.mark.parametrize("verb", ["nevykoná", "nekoná"])
def test_nevykona_v_plnom_rozsahu_je_zmena_nie_zrusenie(cfg, verb):
    """„… sa nevykoná (nekoná) v plnom rozsahu, ale len od 08:00 do 10:00“ – zmena výluky, nie jej zrušenie (zrušenie
    celej výluky či práve času, ktorý ostáva, by otvorilo úsek)."""
    items = _x("ROV č. 1142/2026", f"Výluka v úseku Kysak – Košice dňa 12.11.2026 sa {verb} v plnom rozsahu, ale len "
               "od 08:00 do 10:00 hod.")
    assert [(i.kind, _sec(i)) for i in items] == [("vyluka_zmena", ("Kysak", "Košice"))], _show(items)


def test_vedlajsia_veta_s_terminom_nezmeni_zrusenie_na_novu_vyluku(cfg):
    """„Výluka …, ktorá trvá od 07:00 do 13:00 hod., sa dňa 09.12.2026 ruší.“ – zrušenie (vedľajšia veta výluku len
    opisuje), nie nová výluka s poznámkou o zrušení."""
    items = _x("ROV č. 3360/2026", "Výluka v úseku Senica – Kúty, ktorá trvá od 07:00 do 13:00 hod., sa dňa 09.12.2026 "
               "ruší.")
    assert [(i.kind, _sec(i)) for i in items] == [("vyluka_zrusenie", ("Senica", "Kúty"))], _show(items)


def test_zrusenie_useku_ktory_ostava_e2e(env, session):
    """[R5C1, e2e] ROV 3360/2026 Trnava – Senica a Senica – Kúty 09.12. 07:00 – 13:00; „Výluka v úseku Senica – Kúty
    … sa ruší. Výluka v úseku Trnava – Senica zostane v platnosti.“ – fronta má jedno zrušenie (Senica – Kúty); po
    potvrdení ostane Trnava – Senica uzavretá a vlak Trnava → Senica 09.12. o 08:00 nedostane VYPRAVIŤ ZELENÁ."""
    _closure(session, "Trnava", "Senica", loc(2026, 12, 9, 7), loc(2026, 12, 9, 13), "3360/2026")
    _closure(session, "Senica", "Kúty", loc(2026, 12, 9, 7), loc(2026, 12, 9, 13), "3360/2026")
    drop(env, "z.eml", raw_mail("ROV c. 3360/2026", "Dobrý deň,\n\nVýluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší. "
                                "Výluka v úseku Trnava – Senica zostane v platnosti.\n", "z3360", sender=ROV_TT,
                                date="Tue, 01 Dec 2026 10:00:00 +0100"))
    _process(session, utc(2026, 12, 1, 9, 2))
    assert [(e.kind, _sec(e)) for e in events(session)] == [("vyluka_zrusenie", ("Senica", "Kúty"))]
    _confirm_all(session)
    active = sorted((c.from_station, c.to_station) for c in session.scalars(select(Closure))
                    if c.status != "zrusene" and c.proposed_change_of is None)
    assert active == [("Trnava", "Senica")], active
    r = _decide(session, "66120", "Trnava", "Senica", loc(2026, 12, 9, 8), loc(2026, 12, 9, 6))
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), r.summary


def test_ani_nevykona_e2e_zrusi_oba_useky(env, session):
    """[R5C1, e2e] „Výluka v úseku Trnava – Senica ani v úseku Senica – Kúty sa dňa 09.12.2026 nevykoná.“ – fronta má
    dve zrušenia; po potvrdení sú obe výluky zrušené (nie nové výluky bez termínu)."""
    _closure(session, "Trnava", "Senica", loc(2026, 12, 9, 7), loc(2026, 12, 9, 13), "3360/2026")
    _closure(session, "Senica", "Kúty", loc(2026, 12, 9, 7), loc(2026, 12, 9, 13), "3360/2026")
    drop(env, "z.eml", raw_mail("ROV c. 3360/2026", "Dobrý deň,\n\nVýluka v úseku Trnava – Senica ani v úseku Senica – "
                                "Kúty sa dňa 09.12.2026 nevykoná.\n", "z3360b", sender=ROV_TT,
                                date="Tue, 01 Dec 2026 10:00:00 +0100"))
    _process(session, utc(2026, 12, 1, 9, 2))
    assert sorted((e.kind, _sec(e)) for e in events(session)) == [("vyluka_zrusenie", ("Senica", "Kúty")),
                                                                  ("vyluka_zrusenie", ("Trnava", "Senica"))]
    _confirm_all(session)
    assert all(c.status == "zrusene" for c in session.scalars(select(Closure)) if c.proposed_change_of is None)
