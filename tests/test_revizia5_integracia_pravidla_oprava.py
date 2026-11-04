"""5. revízia – integrácia, strana pravidiel (I1), oprava po kontrole: nový termín po zrušení sa nestratí, rušený termín
je presne čas, ktorý e-mail ruší, úsek, ktorý ostáva, sa nezruší, pôvodný termín nie je druhou zmenou, obmedzenie sa
nestratí.

- nový či náhradný termín vo vete pri zrušení („Náhradný termín výluky je 14.11. …, rozsah výluky sa nemení“, „Výluka
  sa uskutoční len dňa 12.11. od 18:00 do 22:00“ po zrušení 08:00 – 14:00) je nová výluka – nie „zvyšok“ rušenej;
  „uskutoční sa len …“ je zvyškom, len ak čas nadväzuje na rušený termín či v ňom leží;
- rušený termín: okno vo vete s obdobím výluky („… dňa 12.11. od 08:00 do 14:00 sa v čase od 10:00 do 14:00 ruší“),
  vlastný deň pri viacdennom období („… v dňoch 24.11. až 27.11. … sa dňa 25.11. ruší“), „od 10:00“ / „do 10:00“,
  obdobie vo vedľajšej vete nie je termínom („…, ktorá mala trvať od 08:00 do 14:00, sa dňa 12.11. ruší od 10:00“);
  viac okien, nocí či nesúvislých dní je viac zrušení (čas medzi nimi sa neruší); čas, ktorý podľa e-mailu ostáva
  („… sa ruší. Platí len v čase od 08:00 do 10:00“), sa nezruší; okno nočnej výluky len z vety o tej istej výluke (nie
  z vety o inom úseku či o službe dispečera); noc „z pondelka 23.11. na utorok 24.11.“; „nekoná sa v pôvodnom rozsahu,
  ale len …“ je zmena;
- úsek, ktorý ostáva: „…, okrem úseku …“, „… s výnimkou úseku …“ (aj za výrazom zrušenia), „… sa neruší / nezrušuje“,
  „V úseku … bude výluka podľa ROV“; „nevykonáva“, „nebude vykonávať“, „nebudú dňa … vykonané“ sú zrušenia; dva úseky
  toho istého ROV sú dve zrušenia;
- pôvodný termín: „Namiesto výluky dňa …“, „… a nie …“, „(predtým …)“, „(doteraz …)“, „miesto …“, „plánovaná na …
  sa presúva na …“, „sa z termínu … presúva na …“, „presúva z … na …“ – jedna zmena;
- obmedzenie sa nestratí: pomalé jazdy s inou rýchlosťou v riadkoch prehľadu, dve obmedzenia v dvoch odsekoch textu,
  „výluka 1. traťovej koľaje a vypnutie trakčného vedenia“.

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from vlaky.models import Closure
from test_revizia3_terminy import drop, events, raw_mail, utc
from test_revizia4_pravidla import ROV_KE, _decide, _process, env  # noqa: F401 – fixture env

TZ = ZoneInfo("Europe/Bratislava")
ROV_TT = "ZSR OR Trnava <vyluky.ortt@zsr.example>"
U = "Výluka podľa ROV č. 1142/2026 v úseku Kysak – Košice"
N = "Výluka podľa ROV č. 1500/2026 v úseku Kysak – Košice"
RK = "v úseku Ružomberok – Liptovský Mikuláš"


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


def _term(it) -> tuple:
    return _l(it.data.get("start")), _l(it.data.get("end"))


def _notes(items) -> str:
    return " ".join(n for i in items for n in i.notes)


def _show(items) -> list:
    return [(i.kind, i.data, i.notes) for i in items]


def _got(items) -> list:
    """Udalosti ako (druh, začiatok, koniec) zoradené – na porovnanie."""
    return sorted((i.kind, *_term(i)) for i in items)


def _closure(session, a: str, b: str, start: datetime, end: datetime, ref: str, daily: tuple = ("", ""),
             **kw) -> Closure:
    c = Closure(kind=kw.pop("kind", "uplna"), from_station=a, to_station=b, start_utc=start, end_utc=end,
                daily_from=daily[0], daily_to=daily[1], status="overene", source="email", external_ref=ref, **kw)
    session.add(c)
    session.commit()
    return c


def _confirm_all(session) -> None:
    from vlaky.posta.spracovanie import confirm_event

    for e in events(session):
        if e.status == "navrh":
            confirm_event(session, e.id, "dispecer1")
            session.commit()


def _closed_at(session, when: datetime) -> bool:
    """Plánovač má v čase ``when`` okno platnej výluky (denné opakovanie rozbalené)."""
    from vlaky.planovanie.planovac import closure_windows

    return any(w.start <= when < w.end for w in closure_windows(session, when - timedelta(hours=1),
                                                                 when + timedelta(hours=1)))


def _mail(env, session, subject: str, body: str, mid: str, now: datetime, sender: str = ROV_KE) -> None:
    """E-mail príde, spracuje sa a dispečer potvrdí všetky udalosti fronty."""
    drop(env, f"{mid}.eml", raw_mail(subject, "Dobrý deň,\n\n" + body + "\n", mid, sender=sender,
                                     date=(now - timedelta(minutes=2)).astimezone(TZ).strftime(
                                         "%a, %d %b %Y %H:%M:%S %z")))
    _process(session, now)
    _confirm_all(session)


def _not_green(r) -> None:
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), r.summary


# ====================================================================== nový termín po zrušení sa nestratí
NEW_AFTER_CANCEL = {
    "nahradny_rozsah_nemeni": (f"{U} dňa 12.11.2026 sa ruší. Náhradný termín výluky je 14.11.2026 od 08:00 do 14:00 "
                               "hod., rozsah výluky sa nemení.", ("14.11. 08:00", "14.11. 14:00"),
                               ("12.11. 00:00", "13.11. 00:00")),
    "nova_druh_nemeni": (f"{U} dňa 12.11.2026 sa ruší. Nová výluka bude dňa 14.11.2026 od 08:00 do 14:00 hod., druh "
                         "výluky sa nemení.", ("14.11. 08:00", "14.11. 14:00"), ("12.11. 00:00", "13.11. 00:00")),
    "bude_usek_nemeni": (f"{U} dňa 12.11.2026 sa ruší. Výluka bude dňa 14.11.2026 od 08:00 do 14:00 hod., úsek sa "
                         "nemení.", ("14.11. 08:00", "14.11. 14:00"), ("12.11. 00:00", "13.11. 00:00")),
    "len_vecer": (f"{U} dňa 12.11.2026 od 08:00 do 14:00 hod. sa ruší. Výluka sa uskutoční len dňa 12.11.2026 od 18:00 "
                  "do 22:00 hod.", ("12.11. 18:00", "12.11. 22:00"), ("12.11. 08:00", "12.11. 14:00")),
    "len_noc": (f"{U} dňa 12.11.2026 od 08:00 do 14:00 hod. sa ruší. Výluka sa uskutoční len v noci z 12.11. na "
                "13.11.2026 od 22:00 do 04:00 hod.", ("12.11. 22:00", "13.11. 04:00"),
                ("12.11. 08:00", "12.11. 14:00")),
    "vykona_sa_len": (f"{U} dňa 12.11.2026 v čase od 08:00 do 14:00 hod. sa nekoná. Vykoná sa len dňa 12.11.2026 od "
                      "16:00 do 20:00 hod.", ("12.11. 16:00", "12.11. 20:00"), ("12.11. 08:00", "12.11. 14:00")),
    "plati_len": (f"{U} dňa 12.11.2026 od 08:00 do 14:00 hod. sa ruší. Výluka platí len dňa 12.11.2026 od 15:00 do "
                  "18:00 hod.", ("12.11. 15:00", "12.11. 18:00"), ("12.11. 08:00", "12.11. 14:00")),
    "iba_cas_bez_dna": (f"{U} dňa 12.11.2026 od 08:00 do 14:00 hod. sa ruší. Výluka sa uskutoční iba v čase od 18:00 "
                        "do 22:00 hod.", ("12.11. 18:00", "12.11. 22:00"), ("12.11. 08:00", "12.11. 14:00")),
}


@pytest.mark.parametrize("key", list(NEW_AFTER_CANCEL))
def test_novy_termin_po_zruseni_sa_nestrati(cfg, key):
    """[kritické] Veta s novým či náhradným termínom („Náhradný termín …, rozsah sa nemení“, „Výluka sa uskutoční len
    … od 18:00 do 22:00“ po zrušení 08:00 – 14:00) nie je zvyškom rušenej výluky – je novou výlukou; zrušenie pôvodného
    termínu je samostatná udalosť (nový termín sa nestratí, úsek nedostane ZELENÚ v novom čase výluky)."""
    body, new, old = NEW_AFTER_CANCEL[key]
    items = _x("ROV č. 1142/2026", body)
    assert _got(items) == sorted([("vyluka_nova", *new), ("vyluka_zrusenie", *old)]), _show(items)
    assert all(_sec(i) == ("Kysak", "Košice") for i in items), _show(items)


def test_zvysok_vyluky_nadvazujuci_na_rusene_okno_ostava_zrusenim_kontrola(cfg):
    """Kontrola: „… sa uskutoční len od 08:00 do 10:00. Od 10:00 do 14:00 sa výluka ruší.“ (čas nadväzuje na rušený)
    a „… od 10:00 do 14:00 sa ruší. Výluka sa uskutoční len od 08:00 do 10:00.“ sú len zrušenie 10:00 – 14:00."""
    for body in (f"{U} sa dňa 12.11.2026 uskutoční len od 08:00 do 10:00 hod. Od 10:00 do 14:00 hod. sa výluka ruší.",
                 f"{U} dňa 12.11.2026 od 10:00 do 14:00 hod. sa ruší. Výluka sa uskutoční len od 08:00 do 10:00 hod."):
        items = _x("ROV č. 1142/2026", body)
        assert _got(items) == [("vyluka_zrusenie", "12.11. 10:00", "12.11. 14:00")], _show(items)


@pytest.mark.parametrize("key,probe", [
    ("nahradny_rozsah_nemeni", (2026, 11, 14, 10)), ("len_vecer", (2026, 11, 12, 19)),
    ("plati_len", (2026, 11, 12, 16)), ("iba_cas_bez_dna", (2026, 11, 12, 19)),
])
def test_novy_termin_po_zruseni_e2e(env, session, key, probe):
    """[kritické, e2e] Výluka 1142/2026 Kysak – Košice 12.11. 08:00 – 14:00; e-mail zruší pôvodný čas a uvedie nový –
    po potvrdení fronty je úsek v novom čase uzavretý a vlak Kysak → Košice nedostane VYPRAVIŤ ZELENÁ."""
    _closure(session, "Kysak", "Košice", loc(2026, 11, 12, 8), loc(2026, 11, 12, 14), "1142/2026")
    _mail(env, session, "ROV c. 1142/2026", NEW_AFTER_CANCEL[key][0], "n" + key, utc(2026, 11, 9, 9, 2))
    when = loc(*probe)
    assert _closed_at(session, when), [(c.start_utc, c.end_utc, c.status) for c in session.scalars(select(Closure))]
    _not_green(_decide(session, "77112", "Kysak", "Košice", when, when - timedelta(hours=2)))


# ====================================================================== rušený termín je čas, ktorý e-mail ruší
CANCEL_TERMS = {
    # okno vo vete s obdobím výluky
    "okno_vo_vete": (f"{U} dňa 12.11.2026 od 08:00 do 14:00 hod. sa v čase od 10:00 do 14:00 hod. ruší.",
                     [("12.11. 10:00", "12.11. 14:00")]),
    "rusi_v_case": (f"{U} dňa 12.11.2026 od 08:00 do 14:00 hod. sa ruší v čase od 10:00 do 14:00 hod.",
                    [("12.11. 10:00", "12.11. 14:00")]),
    # obdobie vedľajšej vety nie je rušeným termínom
    "vedlajsia_veta_od": ("Výluka v úseku Kysak – Košice, ktorá mala trvať od 08:00 do 14:00 hod., sa dňa 12.11.2026 "
                          "ruší od 10:00 hod. (ROV č. 1142/2026).", [("12.11. 10:00", "13.11. 00:00")]),
    "do_casu": (f"{U} dňa 12.11.2026 sa ruší do 10:00 hod.", [("12.11. 00:00", "12.11. 10:00")]),
    # vlastný deň pri viacdennom období
    "den_v_obdobi_okna": (f"{U} v dňoch 24.11. až 27.11.2026 od 08:00 do 14:00 hod. sa dňa 25.11.2026 ruší.",
                          [("25.11. 00:00", "26.11. 00:00")]),
    "naplanovana_na": (f"{U} naplánovaná na 24.11. – 27.11.2026 sa dňa 25.11.2026 ruší.",
                       [("25.11. 00:00", "26.11. 00:00")]),
    "platna_od_do": (f"{U} platná od 24.11.2026 do 27.11.2026 sa dňa 25.11.2026 nekoná.",
                     [("25.11. 00:00", "26.11. 00:00")]),
    # viac okien, nesúvislé dni – viac zrušení
    "dve_okna": (f"{U} dňa 12.11.2026 sa ruší od 08:00 do 10:00 a od 12:00 do 14:00 hod.",
                 [("12.11. 08:00", "12.11. 10:00"), ("12.11. 12:00", "12.11. 14:00")]),
    "nesuvisle_dni": (f"{U} sa dňa 24.11.2026 a dňa 26.11.2026 ruší.",
                      [("24.11. 00:00", "25.11. 00:00"), ("26.11. 00:00", "27.11. 00:00")]),
    # čas, ktorý podľa e-mailu ostáva, sa nezruší
    "rusi_plati_len": (f"{U} sa dňa 12.11.2026 ruší. Platí len v čase od 08:00 do 10:00 hod.",
                       [("12.11. 00:00", "12.11. 08:00"), ("12.11. 10:00", "13.11. 00:00")]),
    "dni_okrem_dna": (f"{U} v dňoch 24.11. – 27.11.2026 sa ruší, dňa 26.11.2026 platí bez zmeny.",
                      [("24.11. 00:00", "26.11. 00:00"), ("27.11. 00:00", "28.11. 00:00")]),
}


@pytest.mark.parametrize("key", list(CANCEL_TERMS))
def test_ruseny_termin_je_cas_ktory_email_rusi(cfg, key):
    """Rušený termín je čas z časti vety o zrušení: okno vo vete s obdobím výluky, vlastný deň pri viacdennom období,
    „od“/„do“ času; obdobie vo vedľajšej vete výluku len opisuje; viac okien či nesúvislých dní je viac zrušení (čas
    medzi nimi sa neruší) a čas, ktorý podľa e-mailu ostáva, sa nezruší."""
    body, terms = CANCEL_TERMS[key]
    items = _x("ROV č. 1142/2026", body)
    assert [i.kind for i in items] == ["vyluka_zrusenie"] * len(terms), _show(items)
    assert sorted(_term(i) for i in items) == terms, _show(items)
    assert all(_sec(i) == ("Kysak", "Košice") and i.data.get("external_ref") == "1142/2026" for i in items)


def test_ruseny_cas_s_dnom_len_v_predmete(cfg):
    """„… sa v čase od 10:00 do 14:00 hod. ruší.“ a deň len v predmete – zrušenie 12.11. 10:00 – 14:00 (nie celý deň)
    s poznámkou „overte“."""
    items = _x("ROV č. 1142/2026 – zmena 12.11.2026", f"{U} sa v čase od 10:00 do 14:00 hod. ruší.")
    assert [(i.kind, *_term(i)) for i in items] == [("vyluka_zrusenie", "12.11. 10:00", "12.11. 14:00")], _show(items)
    assert "z inej vety e-mailu – overte" in _notes(items)


@pytest.mark.parametrize("body,terms", [
    (f"{N} v nociach 25./26.11. a 27./28.11.2026 sa ruší.",
     [("25.11. 22:00", "26.11. 06:00"), ("27.11. 22:00", "28.11. 06:00")]),
    (f"{N} sa v noci z pondelka 23.11. na utorok 24.11.2026 ruší.", [("23.11. 22:00", "24.11. 06:00")]),
    (f"{N} sa dňa 25.11.2026 a v noci z 25.11. na 26.11.2026 ruší.", [("25.11. 00:00", "26.11. 06:00")]),
], ids=["noci_nesuvisle", "noc_s_dnom_v_tyzdni", "den_a_noc"])
def test_zrusenie_noci_po_jednej(cfg, body, terms):
    """Každá noc je samostatné zrušenie s oknom noci (noc 26./27. medzi nimi sa neruší); noc „z pondelka 23.11. na
    utorok 24.11.“ je noc, nie dva celé dni; deň a noc za ním sú jeden termín."""
    items = _x("ROV č. 1500/2026", body)
    assert [i.kind for i in items] == ["vyluka_zrusenie"] * len(terms), _show(items)
    assert sorted(_term(i) for i in items) == terms, _show(items)


@pytest.mark.parametrize("second", [
    "Výluka v úseku Margecany – Kysak platí naďalej denne v čase od 19:00 do 09:00 hod.",
    "Nočná služba dispečera je k dispozícii od 18:00 do 07:00 hod.",
], ids=["iny_usek", "sluzba_dispecera"])
def test_okno_nocnej_vyluky_nie_z_vety_o_inej_veci(cfg, second):
    """Okno nočnej výluky sa neberie z vety o inom úseku ani o službe dispečera – noc bez času je 22:00 – 06:00."""
    items = _x("ROV č. 1502/2026", "Výluka podľa ROV č. 1502/2026 v úseku Kysak – Košice v noci z 25.11. na "
               "26.11.2026 sa ruší.\n\n" + second)
    assert [(i.kind, *_term(i)) for i in items] == [("vyluka_zrusenie", "25.11. 22:00", "26.11. 06:00")], _show(items)


def test_okno_nocnej_vyluky_z_vety_o_tej_istej_vyluke_kontrola(cfg):
    """Kontrola: okno z vety o tej istej výluke (iný odsek) je termínom zrušenej noci."""
    items = _x("ROV č. 1502/2026", "Výluka podľa ROV č. 1502/2026 v úseku Kysak – Košice v noci z 25.11. na 26.11.2026 "
               "sa ruší.\n\nV ostatných nociach sa výluka v úseku Kysak – Košice koná podľa ROV od 23:00 do 04:00 hod.")
    assert [(i.kind, *_term(i)) for i in items] == [("vyluka_zrusenie", "25.11. 23:00", "26.11. 04:00")], _show(items)


@pytest.mark.parametrize("verb", ["nekoná v pôvodnom rozsahu", "nevykoná v plánovanom rozsahu"])
def test_nekona_v_povodnom_rozsahu_ale_len_je_zmena(cfg, verb):
    """„… sa nekoná v pôvodnom rozsahu, ale len od 08:00 do 10:00“ – zmena (skrátenie), nie zrušenie času, ktorý
    ostáva."""
    items = _x("ROV č. 1142/2026", f"{U} dňa 12.11.2026 sa {verb}, ale len od 08:00 do 10:00 hod.")
    assert [i.kind for i in items] == ["vyluka_zmena"], _show(items)


DAY_E2E = {
    "okno_vo_vete": (CANCEL_TERMS["okno_vo_vete"][0], [9], [11]),
    "vedlajsia_veta_od": (CANCEL_TERMS["vedlajsia_veta_od"][0], [9], [11]),
    "rusi_plati_len": (CANCEL_TERMS["rusi_plati_len"][0], [9], [11]),
    # druhé okno zapíše register až po úprave hľadania výluky (časť rozdelená sesterskou udalosťou) – čas medzi oknami
    # ostáva uzavretý v každom prípade
    "dve_okna": (CANCEL_TERMS["dve_okna"][0], [11], [9]),
    "povodny_rozsah": (f"{U} dňa 12.11.2026 sa nekoná v pôvodnom rozsahu, ale len od 08:00 do 10:00 hod.", [9], []),
}


@pytest.mark.parametrize("key", list(DAY_E2E))
def test_zrusenie_casti_dna_e2e(env, session, key):
    """[e2e] Výluka 1142/2026 Kysak – Košice 12.11. 08:00 – 14:00; e-mail ruší len časť dňa – po potvrdení fronty je
    úsek v čase, ktorý ostáva, uzavretý (vlak nedostane VYPRAVIŤ ZELENÁ) a v rušenom čase prejazdný."""
    body, closed, opened = DAY_E2E[key]
    _closure(session, "Kysak", "Košice", loc(2026, 11, 12, 8), loc(2026, 11, 12, 14), "1142/2026")
    _mail(env, session, "ROV c. 1142/2026", body, "d" + key, utc(2026, 11, 9, 9, 2))
    for h in closed:
        assert _closed_at(session, loc(2026, 11, 12, h)), h
    for h in opened:
        assert not _closed_at(session, loc(2026, 11, 12, h)), h
    when = loc(2026, 11, 12, closed[0])
    _not_green(_decide(session, "77112", "Kysak", "Košice", when, when - timedelta(hours=2)))


def test_zrusenie_dna_viacdennej_vyluky_e2e(env, session):
    """[e2e] Výluka 24.11. – 27.11. denne 08:00 – 14:00; „… v dňoch 24.11. až 27.11.2026 od 08:00 do 14:00 hod. sa dňa
    25.11.2026 ruší.“ – zruší sa len 25.11., 26.11. o 10:00 je úsek uzavretý."""
    _closure(session, "Kysak", "Košice", loc(2026, 11, 24, 8), loc(2026, 11, 27, 14), "1142/2026",
             daily=("08:00", "14:00"))
    _mail(env, session, "ROV c. 1142/2026", CANCEL_TERMS["den_v_obdobi_okna"][0], "dvd", utc(2026, 11, 16, 9, 2))
    assert not _closed_at(session, loc(2026, 11, 25, 10))
    assert _closed_at(session, loc(2026, 11, 26, 10)) and _closed_at(session, loc(2026, 11, 24, 10))
    _not_green(_decide(session, "77126", "Kysak", "Košice", loc(2026, 11, 26, 10), loc(2026, 11, 26, 8)))


def test_zrusenie_dvoch_noci_nocnej_vyluky_e2e(env, session):
    """[e2e] Nočná výluka 23./24. – 29./30.11. 22:00 – 06:00; „v nociach 25./26.11. a 27./28.11.2026 sa ruší“ – noc
    26./27. ostane uzavretá (nie jeden termín 25.11. 22:00 – 28.11. 06:00, ktorý by ju otvoril)."""
    _closure(session, "Kysak", "Košice", loc(2026, 11, 23, 22), loc(2026, 11, 30, 6), "1500/2026",
             daily=("22:00", "06:00"))
    _mail(env, session, "ROV c. 1500/2026", f"{N} v nociach 25./26.11. a 27./28.11.2026 sa ruší.", "dn2",
          utc(2026, 11, 16, 9, 2))
    assert not _closed_at(session, loc(2026, 11, 25, 23))
    for d, h in ((24, 23), (26, 23), (27, 2), (28, 23)):
        assert _closed_at(session, loc(2026, 11, d, h)), (d, h)
    _not_green(_decide(session, "77126", "Kysak", "Košice", loc(2026, 11, 26, 23), loc(2026, 11, 26, 21)))


def test_zrusenie_dvoch_noci_suvislej_vyluky_neotvori_den_e2e(env, session):
    """[e2e] Súvislá výluka 24.11. 08:00 – 29.11. 14:00; „v nociach 25./26.11. a 26./27.11.2026 sa ruší“ – deň 26.11.
    medzi nocami ostane uzavretý."""
    _closure(session, "Kysak", "Košice", loc(2026, 11, 24, 8), loc(2026, 11, 29, 14), "1502/2026")
    _mail(env, session, "ROV c. 1502/2026", "Výluka podľa ROV č. 1502/2026 v úseku Kysak – Košice v nociach 25./26.11. "
          "a 26./27.11.2026 sa ruší.", "dn3", utc(2026, 11, 16, 9, 2))
    assert _closed_at(session, loc(2026, 11, 26, 12)) and not _closed_at(session, loc(2026, 11, 26, 2))
    _not_green(_decide(session, "77126", "Kysak", "Košice", loc(2026, 11, 26, 12), loc(2026, 11, 26, 10)))


@pytest.mark.parametrize("second,probe", [
    ("Výluka v úseku Margecany – Kysak platí naďalej denne v čase od 19:00 do 09:00 hod.", (2026, 11, 26, 8)),
    ("Nočná služba dispečera je k dispozícii od 18:00 do 07:00 hod.", (2026, 11, 26, 6, 30)),
], ids=["iny_usek", "sluzba_dispecera"])
def test_zrusenie_noci_okno_z_inej_vety_neotvori_rano_e2e(env, session, second, probe):
    """[e2e] Súvislá výluka 24.11. 08:00 – 27.11. 14:00; zrušenie noci 25./26.11. bez času – okno z vety o inom úseku či
    o službe dispečera nie je oknom zrušenia: ráno 26.11. po 06:00 je úsek uzavretý."""
    _closure(session, "Kysak", "Košice", loc(2026, 11, 24, 8), loc(2026, 11, 27, 14), "1502/2026")
    _mail(env, session, "ROV c. 1502/2026", "Výluka podľa ROV č. 1502/2026 v úseku Kysak – Košice v noci z 25.11. na "
          "26.11.2026 sa ruší.\n\n" + second, "dw" + str(probe[3]), utc(2026, 11, 16, 9, 2))
    when = loc(*probe)
    assert _closed_at(session, when)
    _not_green(_decide(session, "77126", "Kysak", "Košice", when, when - timedelta(hours=2)))


# ====================================================================== úsek, ktorý ostáva; zápor zrušenia
C = "Výluka v úseku Senica – Kúty dňa 09.12.2026 sa ruší."
STAYS = {
    "okrem_za_vyrokom": "Výluka v úsekoch Trnava – Senica a Senica – Kúty dňa 09.12.2026 sa ruší, okrem úseku Trnava – "
                        "Senica.",
    "okrem_bez_ciarky": "Výluka v úsekoch Trnava – Senica a Senica – Kúty dňa 09.12.2026 sa ruší okrem úseku Trnava – "
                        "Senica.",
    "s_vynimkou": "Výluky v úsekoch Trnava – Senica a Senica – Kúty dňa 09.12.2026 sa rušia s výnimkou úseku Trnava – "
                  "Senica.",
    "nerusi": C + " Výluka v úseku Trnava – Senica sa neruší.",
    "nezrusuje": C + " Výluka v úseku Trnava – Senica sa nezrušuje.",
    "bude_vyluka_podla_rov": C + " V úseku Trnava – Senica bude výluka podľa ROV.",
}


@pytest.mark.parametrize("key", list(STAYS))
def test_usek_ktory_ostava_sa_nezrusi(cfg, key):
    """[R5C1] „…, okrem úseku …“ / „… s výnimkou úseku …“ za výrazom zrušenia, „… sa neruší / nezrušuje“, „V úseku …
    bude výluka podľa ROV“ – zruší sa len Senica – Kúty, úsek Trnava – Senica ostáva."""
    items = _x("ROV č. 3360/2026", STAYS[key])
    assert [(i.kind, _sec(i)) for i in items] == [("vyluka_zrusenie", ("Senica", "Kúty"))], _show(items)
    assert _term(items[0]) == ("09.12. 00:00", "10.12. 00:00")
    assert "Trnava – Senica ostáva" in _notes(items)


@pytest.mark.parametrize("body", [
    "Výluky v úseku Trnava – Senica a v úseku Senica – Kúty nebudú dňa 09.12.2026 vykonané.",
    "Výluka v úseku Trnava – Senica ani v úseku Senica – Kúty nebude dňa 09.12.2026 vykonaná.",
    "Výluka v úseku Trnava – Senica ani v úseku Senica – Kúty sa dňa 09.12.2026 nevykonáva.",
], ids=["nebudu_dna_vykonane", "nebude_dna_vykonana", "nevykonava"])
def test_zapor_vykonania_je_zrusenie_oboch_usekov(cfg, body):
    """„nebudú dňa … vykonané“, „nevykonáva“ – zrušenie oboch úsekov (nie dve nové výluky bez termínu)."""
    items = _x("ROV č. 3360/2026", body)
    assert sorted((i.kind, _sec(i)) for i in items) == [("vyluka_zrusenie", ("Senica", "Kúty")),
                                                        ("vyluka_zrusenie", ("Trnava", "Senica"))], _show(items)
    assert all(_term(i) == ("09.12. 00:00", "10.12. 00:00") for i in items)


def test_nebude_vykonavat_je_zrusenie(cfg):
    items = _x("ROV č. 3360/2026", "Výluka v úseku Trnava – Senica sa dňa 09.12.2026 nebude vykonávať.")
    assert [(i.kind, _sec(i), *_term(i)) for i in items] == [
        ("vyluka_zrusenie", ("Trnava", "Senica"), "09.12. 00:00", "10.12. 00:00")], _show(items)


def test_dva_useky_toho_isteho_rov_su_dve_zrusenia(cfg):
    """„Výluky v úsekoch Senica – Kúty a Kúty – Devínska Nová Ves … sa rušia. Výluka v úseku Trnava – Senica ostáva
    v platnosti.“ – dve zrušenia (rovnaké ROV nezlúči zrušenia rôznych úsekov)."""
    items = _x("ROV č. 3360/2026", "Výluky v úsekoch Senica – Kúty a Kúty – Devínska Nová Ves dňa 09.12.2026 sa rušia. "
               "Výluka v úseku Trnava – Senica ostáva v platnosti.")
    assert sorted((i.kind, _sec(i)) for i in items) == [("vyluka_zrusenie", ("Kúty", "Devínska Nová Ves")),
                                                        ("vyluka_zrusenie", ("Senica", "Kúty"))], _show(items)


@pytest.mark.parametrize("key", ["okrem_za_vyrokom", "bude_vyluka_podla_rov"])
def test_usek_ktory_ostava_e2e(env, session, key):
    """[R5C1, e2e] Výluky 3360/2026 Trnava – Senica a Senica – Kúty 09.12. 07:00 – 13:00 – po potvrdení fronty ostane
    Trnava – Senica uzavretá a vlak Trnava → Senica o 08:00 nedostane VYPRAVIŤ ZELENÁ."""
    _closure(session, "Trnava", "Senica", loc(2026, 12, 9, 7), loc(2026, 12, 9, 13), "3360/2026")
    _closure(session, "Senica", "Kúty", loc(2026, 12, 9, 7), loc(2026, 12, 9, 13), "3360/2026")
    _mail(env, session, "ROV c. 3360/2026", STAYS[key], "s" + key, utc(2026, 12, 1, 9, 2), sender=ROV_TT)
    active = sorted((c.from_station, c.to_station) for c in session.scalars(select(Closure))
                    if c.status != "zrusene" and c.proposed_change_of is None)
    assert active == [("Trnava", "Senica")], active
    _not_green(_decide(session, "66120", "Trnava", "Senica", loc(2026, 12, 9, 8), loc(2026, 12, 9, 6)))


# ====================================================================== pôvodný termín nie je druhou zmenou
OLD = {
    "namiesto_vyluky_dna": f"Namiesto výluky dňa 25.11.2026 od 08:00 do 14:00 hod. bude výluka {RK} dňa 26.11.2026 od "
                           "08:00 do 14:00 hod.",
    "a_nie": f"Výluka {RK} sa uskutoční 26.11.2026 od 08:00 do 14:00 hod. a nie 25.11.2026 od 08:00 do 14:00 hod., ako "
             "bolo pôvodne uvedené.",
    "predtym": f"Výluka {RK} bude 26.11.2026 od 08:00 do 14:00 hod. (predtým 25.11.2026 od 08:00 do 14:00 hod.).",
    "doteraz": f"Výluka {RK} bude 26.11.2026 od 08:00 do 14:00 hod. (doteraz 25.11.2026 od 08:00 do 14:00 hod.).",
    "miesto": f"Výluka {RK} bude 26.11.2026 od 08:00 do 14:00 hod. miesto 25.11.2026 od 08:00 do 14:00 hod.",
    "planovana_na_presuva": f"Výluka {RK} plánovaná na 25.11.2026 od 08:00 do 14:00 hod. sa presúva na 26.11.2026 od "
                            "08:00 do 14:00 hod.",
    "z_terminu": f"Výluka {RK} sa z termínu 25.11.2026 od 08:00 do 14:00 hod. presúva na 26.11.2026 od 08:00 do 14:00 "
                 "hod.",
    "z_na_okna": f"Výluka {RK} sa presúva z 25.11.2026 08:00 – 14:00 na 26.11.2026 08:00 – 14:00.",
}


@pytest.mark.parametrize("key", list(OLD))
def test_povodny_termin_nie_je_druhou_zmenou(cfg, key):
    """[R5L0] Pôvodný termín („Namiesto výluky dňa …“, „… a nie …“, „(predtým …)“, „(doteraz …)“, „miesto …“,
    „plánovaná na … sa presúva na …“, „sa z termínu … presúva na …“, „presúva z … na …“) nie je druhou zmenou – jedna
    zmena na 26.11. 08:00 – 14:00 (pôvodný deň sa neuzavrie navyše)."""
    items = _x("Zmena ROV č. 1315/2026", OLD[key])
    assert [(i.kind, _sec(i), *_term(i)) for i in items] == [
        ("vyluka_zmena", ("Ružomberok", "Liptovský Mikuláš"), "26.11. 08:00", "26.11. 14:00")], _show(items)


def test_predtym_na_zaciatku_vety_je_dalsi_termin_kontrola(cfg):
    """Kontrola: „Predtým dňa 20.11.2026 od 08:00 do 14:00 hod. bude … výluka“ je ďalší (skorší) termín – nestratí
    sa."""
    items = _x("Výluka a pomalá jazda Kysak – Košice", "Od 20.11.2026 14:00 do 30.11.2026 platí v úseku Kysak – Košice "
               "pomalá jazda 50 km/h.\nPredtým dňa 20.11.2026 od 08:00 do 14:00 hod. bude v úseku Kysak – Košice "
               "výluka podľa ROV č. 1178/2026.")
    assert ("vyluka_nova", "20.11. 08:00", "20.11. 14:00") in _got(items), _show(items)


# ====================================================================== obmedzenie sa nestratí
@pytest.mark.parametrize("head,row", [
    ("Úsek | Koľaj | Začiatok | Koniec | Rýchlosť | Poznámka",
     "Kysak – Košice | 1. TK | 23.11.2026 08:00 | 30.11.2026 14:00 | {v} km/h | km {km}"),
    ("Por.č. | Úsek | Koľaj | Začiatok | Koniec | Rýchlosť",
     "{n} | Kysak – Košice | 1. TK | 23.11.2026 08:00 | 30.11.2026 14:00 | {v} km/h"),
], ids=["poznamka", "poradie"])
def test_pomale_jazdy_s_inou_rychlostou_su_dve(cfg, head, row):
    """[R5L1] Dva riadky prehľadu pomalých jázd s rovnakým úsekom, koľajou a časom, ale inou rýchlosťou (50 a 30 km/h)
    sú dve pomalé jazdy – prísnejšia 30 km/h sa nestratí."""
    body = "Prehľad pomalých jázd:\n\n" + head + "\n" + row.format(n=1, v=50, km="10,0 – 12,0") + "\n" + \
        row.format(n=2, v=30, km="20,0 – 21,0") + "\n"
    items = _x("Pomalé jazdy – prehľad", body)
    assert sorted(i.data.get("speed_kmh") for i in items if i.kind == "pomala_jazda") == [30, 50], _show(items)


@pytest.mark.parametrize("second,want", [
    ("Výluka 2. traťovej koľaje v úseku Kysak – Košice dňa 23.11.2026 od 08:00 do 14:00 hod.",
     [("kolaj", "1"), ("kolaj", "2")]),
    ("V úseku Kysak – Košice dňa 23.11.2026 od 08:00 do 14:00 hod. bude vypnuté trakčné vedenie.",
     [("kolaj", "1"), ("trakcne_vedenie", None)]),
], ids=["druha_kolaj", "vypnute_tv"])
def test_dve_obmedzenia_v_dvoch_odsekoch_su_dve_udalosti(cfg, second, want):
    """[R5L1] Dva odseky s tým istým úsekom a časom, ale iným výslovným obmedzením (1. TK a 2. TK, 1. TK a vypnuté TV),
    sú dve udalosti – druhé obmedzenie sa nestratí."""
    items = _x("ROV č. 1419/2026", "Výluka 1. traťovej koľaje v úseku Kysak – Košice dňa 23.11.2026 od 08:00 do 14:00 "
               "hod. podľa ROV č. 1419/2026.\n\n" + second)
    got = sorted((i.data.get("kind"), i.data.get("track")) for i in items if i.kind == "vyluka_nova")
    assert got == sorted(want, key=lambda x: (x[0], x[1] or "")), _show(items)
    assert all(_term(i) == ("23.11. 08:00", "23.11. 14:00") for i in items)


@pytest.mark.parametrize("second", [
    "Počas výluky v úseku Kysak – Košice dňa 23.11.2026 od 08:00 do 14:00 hod. budú vlaky jazdiť po 2. traťovej "
    "koľaji.",
    "Výluka v úseku Kysak – Košice dňa 23.11.2026 od 08:00 do 14:00 hod. je vedená pod ROV č. 1419/2026.",
], ids=["pocas_vyluky", "bez_druhu"])
def test_odsek_ktory_vyluku_len_spomina_nezdvoji_kontrola(cfg, second):
    """Kontrola: odsek, ktorý výluku len spomína („Počas výluky … budú vlaky jazdiť po 2. koľaji“) či druh neuvádza,
    druhú udalosť nedá."""
    items = _x("ROV č. 1419/2026", "Výluka 1. traťovej koľaje v úseku Kysak – Košice dňa 23.11.2026 od 08:00 do 14:00 "
               "hod.\n\n" + second)
    assert [(i.kind, i.data.get("kind"), i.data.get("track")) for i in items] == [("vyluka_nova", "kolaj", "1")], \
        _show(items)


def test_vyluka_kolaje_a_vypnutie_tv_v_jednej_vete(cfg):
    """„Výluka 1. traťovej koľaje a vypnutie trakčného vedenia v úseku …“ – dve obmedzenia (koľaj aj TV); „… s vypnutím
    TV“ (TV nad vylúčenou koľajou) ostáva výlukou koľaje s poznámkou."""
    items = _x("ROV č. 1400/2026", "Výluka 1. traťovej koľaje a vypnutie trakčného vedenia v úseku Kysak – Košice dňa "
               "23.11.2026 od 08:00 do 14:00 hod. podľa ROV č. 1400/2026.")
    assert sorted((i.data.get("kind"), i.data.get("track") or "") for i in items) == [
        ("kolaj", "1"), ("trakcne_vedenie", "")], _show(items)
    items = _x("ROV č. 1401/2026", "Výluka 1. traťovej koľaje s vypnutím trakčného vedenia v úseku Kysak – Košice dňa "
               "23.11.2026 od 08:00 do 14:00 hod. podľa ROV č. 1401/2026.")
    assert [(i.data.get("kind"), i.data.get("track")) for i in items] == [("kolaj", "1")], _show(items)
    assert "zapíšte aj výluku trakčného vedenia" in _notes(items)


@pytest.mark.parametrize("body", [
    "Výluka 1. traťovej koľaje v úseku Kysak – Košice dňa 23.11.2026 od 08:00 do 14:00 hod. podľa ROV č. 1413/2026.\n\n"
    "V úseku Kysak – Košice dňa 23.11.2026 od 08:00 do 14:00 hod. bude vypnuté trakčné vedenie.",
    "Výluka 1. traťovej koľaje a vypnutie trakčného vedenia v úseku Kysak – Košice dňa 23.11.2026 od 08:00 do 14:00 "
    "hod. podľa ROV č. 1400/2026.",
], ids=["dva_odseky", "jedna_veta"])
def test_kolaj_a_tv_elektricky_vlak_nedostane_zelenu_e2e(env, session, body):
    """[R5L1, e2e] Výluka 1. TK a vypnuté trakčné vedenie Kysak – Košice 23.11. 08:00 – 14:00 – po potvrdení fronty
    elektrický vlak Kysak → Košice o 10:00 nedostane VYPRAVIŤ ZELENÁ (TV sa nestratí)."""
    _mail(env, session, "ROV c. 14xx/2026", body, "tv" + str(len(body)), utc(2026, 11, 16, 9, 2))
    kinds = sorted(c.kind for c in session.scalars(select(Closure)) if c.status != "zrusene")
    assert "trakcne_vedenie" in kinds, kinds
    _not_green(_decide(session, "77623", "Kysak", "Košice", loc(2026, 11, 23, 10), loc(2026, 11, 23, 8, 30), "E"))
