"""6. revízia – strana pravidiel (FA), pokračovanie opravy po kontrole: od e-mailu po plánovač (DMARC,
``process_mailbox``, potvrdenie vo fronte, register, vyhodnotenie vlaku) – vlak v čase, ktorý e-mail uzatvára,
nedostane VYPRAVIŤ ZELENÁ:

- zrušenie „okrem utorka 24.11. a štvrtka 26.11.“ – 24. a 26.11. ostanú uzavreté;
- zrušenie „vo všetky dni okrem nedele“ bez termínu – výluka sa po potvrdení nezruší;
- noci „v noci na 26.11. a na 27.11.“ – noc 25./26.11. sa nestratí;
- okno vo vete bez dňa („Druhá časť výluky bude od 13:00 do 16:00 hod.“) – čakajúca udalosť;
- e-mail ŽSR s úsekom a časom bez prevádzkového slova („… sa v úseku … nebude dať prejsť“) – čakajúca výluka;
- „nebude možný prejazd vlakov úsekom …“ – blokujúca udalosť; kontrola: výťah mimo prevádzky vlak nezdrží;
- predmet „ROV č. 2816/2026 a 2817/2026“ – neskoršie STORNO 2816 nezruší novú výluku 2817.

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

from test_revizia3_terminy import drop, events, raw_mail, utc
from test_revizia4_pravidla import _decide, _process, env  # noqa: F401 – fixture env
from test_revizia5_integracia_pravidla import _closed_at, _closure, _confirm_all, _l, loc

ZA = "ZSR OR Zilina <vyluky.orza@zsr.example>"
KE = "ZSR OR Kosice <vyluky.orke@zsr.example>"
TT = "ZSR OR Trnava <vyluky.ortt@zsr.example>"
SIG = "\n\nS pozdravom\nMarta Vzorová\nOR skúšobná, oddelenie výluk\n"


def _mail(env, subject: str, body: str, mid: str, sender: str, date: str) -> None:
    drop(env, f"{mid}.eml", raw_mail(subject, "Dobrý deň,\n\n" + body + SIG, mid, sender=sender, date=date))


def _not_green(r) -> bool:
    return not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ")


def _show(session) -> list:
    return [(e.kind, e.status, e.data) for e in events(session)]


def test_zrusenie_okrem_utorka_a_stvrtka_e2e(env, session):
    """[FC4] Denná výluka 23. – 27.11. 08:00 – 14:00; „v dňoch 23.11. – 27.11. sa ruší okrem utorka 24.11. a štvrtka
    26.11.“ – po potvrdení sú 24. a 26.11. o 10:00 uzavreté, 25.11. voľný; vlak 26.11. o 10:00 nie je ZELENÝ."""
    _closure(session, "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 23, 8), loc(2026, 11, 27, 14), "2601/2026",
             daily=("08:00", "14:00"))
    _mail(env, "Zrušenie výluky – ROV č. 2601/2026", "Výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2601/2026 "
          "v dňoch 23.11. – 27.11.2026 sa ruší okrem utorka 24.11. a štvrtka 26.11.2026.", "o7e06", ZA,
          "Wed, 18 Nov 2026 10:00:00 +0100")
    _process(session, utc(2026, 11, 18, 9, 2))
    cancels = [e for e in events(session) if e.kind == "vyluka_zrusenie"]
    assert sorted(_l(e.data.get("start"))[:5] for e in cancels) == ["23.11", "25.11", "27.11"], _show(session)
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 11, 24, 10)) and _closed_at(session, loc(2026, 11, 26, 10)), _show(session)
    r = _decide(session, "63011", "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 26, 10), loc(2026, 11, 26, 8))
    assert _not_green(r), r.summary


def test_zrusenie_vsetky_dni_okrem_nedele_e2e(env, session):
    """[FC3 sieť] „… sa ruší vo všetky dni okrem nedele.“ – zrušenie bez termínu je čiastočné; po potvrdení výluka
    ostane a vlak v nedeľu 29.11. o 10:00 nie je ZELENÝ."""
    c = _closure(session, "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 23, 8), loc(2026, 11, 29, 14), "2501/2026",
                 daily=("08:00", "14:00"))
    _mail(env, "Zrušenie výluky – ROV č. 2501/2026", "Výluka podľa ROV č. 2501/2026 v úseku Ružomberok – Liptovský "
          "Mikuláš sa ruší vo všetky dni okrem nedele.", "o7d02", ZA, "Wed, 18 Nov 2026 10:00:00 +0100")
    _process(session, utc(2026, 11, 18, 9, 2))
    (ev,) = events(session)
    assert ev.kind == "vyluka_zrusenie" and ev.data.get("partial") is True, _show(session)
    _confirm_all(session)
    session.refresh(c)
    assert c.status != "zrusene"
    assert _closed_at(session, loc(2026, 11, 29, 10)), _show(session)
    r = _decide(session, "63021", "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 29, 10), loc(2026, 11, 29, 8))
    assert _not_green(r), r.summary


def test_noci_so_zopakovanym_na_e2e(env, session):
    """[FC5] „úplná výluka … v noci na 26.11. a na 27.11.2026 od 22:00 do 04:00“ – po potvrdení je noc 25./26.11.
    o 23:00 uzavretá (nie posunutá o noc) a vlak 25.11. o 23:00 nie je ZELENÝ."""
    _mail(env, "ROV č. 4610/2026", "Úplná výluka trate Kysak – Košice v noci na 26.11. a na 27.11.2026 od 22:00 do "
          "04:00 hod. (ROV č. 4610/2026).", "o7n02", KE, "Wed, 18 Nov 2026 10:00:00 +0100")
    _process(session, utc(2026, 11, 18, 9, 2))
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 11, 25, 23)) and _closed_at(session, loc(2026, 11, 26, 23)), _show(session)
    r = _decide(session, "63031", "Kysak", "Košice", loc(2026, 11, 25, 23), loc(2026, 11, 25, 21))
    assert _not_green(r), r.summary


def test_osamele_okno_vo_vete_bez_dna_e2e(env, session):
    """[sieť] „… dňa 10.12.2026 od 08:00 do 10:00 hod. Druhá časť výluky bude od 13:00 do 16:00 hod.“ – vlak
    10.12. o 14:00 nie je ZELENÝ (pred potvrdením ani po ňom)."""
    _mail(env, "ROV č. 3922/2026", "Úplná výluka trate Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 10:00 hod. Druhá "
          "časť výluky bude od 13:00 do 16:00 hod.", "o7l05", TT, "Tue, 01 Dec 2026 09:00:00 +0100")
    _process(session, utc(2026, 12, 1, 8, 2))
    r = _decide(session, "63041", "Trnava", "Leopoldov", loc(2026, 12, 10, 14), loc(2026, 12, 10, 12))
    assert _not_green(r), r.summary
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 12, 10, 14)) and _closed_at(session, loc(2026, 12, 10, 9)), _show(session)


def test_usek_a_cas_bez_prevadzkoveho_slova_e2e(env, session):
    """[sieť „iné“] „Dňa 10.12.2026 od 08:00 do 14:00 hod. sa v úseku Trnava – Leopoldov nebude dať prejsť.“ –
    čakajúca úplná výluka; vlak 10.12. o 10:00 nie je ZELENÝ."""
    _mail(env, "Oznámenie", "Dňa 10.12.2026 od 08:00 do 14:00 hod. sa v úseku Trnava – Leopoldov nebude dať prejsť.",
          "o7s01", TT, "Tue, 01 Dec 2026 09:00:00 +0100")
    _process(session, utc(2026, 12, 1, 8, 2))
    assert [(e.kind, e.data.get("kind")) for e in events(session)] == [("vyluka_nova", "uplna")], _show(session)
    r = _decide(session, "63051", "Trnava", "Leopoldov", loc(2026, 12, 10, 10), loc(2026, 12, 10, 8))
    assert _not_green(r), r.summary


def test_nebude_mozny_prejazd_vlakov_e2e(env, session):
    """[„iné“] „… nebude možný prejazd vlakov úsekom Trnava – Leopoldov.“ – vlak 10.12. o 10:00 nie je ZELENÝ."""
    _mail(env, "Oznámenie", "Dňa 10.12.2026 od 08:00 do 14:00 hod. nebude možný prejazd vlakov úsekom Trnava – "
          "Leopoldov.", "o7i06", TT, "Tue, 01 Dec 2026 09:00:00 +0100")
    _process(session, utc(2026, 12, 1, 8, 2))
    assert [e.kind for e in events(session)] != ["ine"], _show(session)
    r = _decide(session, "63061", "Trnava", "Leopoldov", loc(2026, 12, 10, 10), loc(2026, 12, 10, 8))
    assert _not_green(r), r.summary


def test_kontrola_vytah_mimo_prevadzky_e2e(env, session):
    """[kontrola] „Výťah v žst. Košice je od 20.11.2026 mimo prevádzky …“ – e-mail je „iné“ a vlak Kysak → Košice
    ostane VYPRAVIŤ ZELENÁ (falošná výluka stanice by ho zdržala)."""
    _mail(env, "Oznámenie", "Výťah v žst. Košice je od 20.11.2026 mimo prevádzky, cestujúci nesmú používať nástupište "
          "3.", "o7i03", KE, "Wed, 18 Nov 2026 10:00:00 +0100")
    _process(session, utc(2026, 11, 18, 9, 2))
    assert [e.kind for e in events(session)] == ["ine"], _show(session)
    r = _decide(session, "63071", "Kysak", "Košice", loc(2026, 11, 25, 10), loc(2026, 11, 25, 8))
    assert (r.decision, r.color) == ("VYPRAVIŤ", "ZELENÁ"), r.summary


def test_storno_rusenej_vyluky_nezrusi_novu_e2e(env, session):
    """[FC0 nízka] „ROV č. 2816/2026 a 2817/2026“: zrušenie 2816 a nová výluka 2817 dňa 30.11. 08:00 – 14:00; neskoršie
    „STORNO ROV č. 2816/2026“ novú výluku nezruší – vlak 30.11. o 10:00 nie je ZELENÝ."""
    _mail(env, "ROV č. 2816/2026 a 2817/2026", "ROV č. 2816/2026: výluka Kraľovany – Ružomberok dňa 24.11.2026 sa "
          "ruší. ROV č. 2817/2026: úplná výluka Kraľovany – Ružomberok dňa 30.11.2026 od 08:00 do 14:00 hod.", "o7r01",
          ZA, "Wed, 18 Nov 2026 10:00:00 +0100")
    _process(session, utc(2026, 11, 18, 9, 2))
    news = [e for e in events(session) if e.kind == "vyluka_nova"]
    assert [e.data.get("external_ref") for e in news] == ["2817/2026"], _show(session)
    _confirm_all(session)
    _mail(env, "STORNO ROV č. 2816/2026", "STORNO ROV č. 2816/2026 (Kraľovany – Ružomberok).", "o7r02", ZA,
          "Thu, 19 Nov 2026 10:00:00 +0100")
    _process(session, utc(2026, 11, 19, 9, 2))
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 11, 30, 10)), _show(session)
    r = _decide(session, "63081", "Kraľovany", "Ružomberok", loc(2026, 11, 30, 10), loc(2026, 11, 30, 8))
    assert _not_green(r), r.summary


def test_kontrola_uvedeny_den_odpada_e2e(env, session):
    """[FC4, kontrola regresie denného okna] Denná výluka 23. – 27.11. 08:00 – 14:00; „STORNO výluky 25.11.2026 – ROV …“
    a „pre výluku … (23.11. – 27.11.2026, denne 08:00 – 14:00 hod.) platí: uvedený deň výluka odpadá, v ostatné dni sa
    vykoná.“ – po potvrdení sú 24. a 26.11. o 10:00 uzavreté (opis v zátvorke nezruší všetky dni)."""
    _closure(session, "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 23, 8), loc(2026, 11, 27, 14), "2603/2026",
             daily=("08:00", "14:00"))
    _mail(env, "STORNO výluky 25.11.2026 – ROV č. 2603/2026", "pre výluku Ružomberok – Liptovský Mikuláš (23.11. – "
          "27.11.2026, denne 08:00 – 14:00 hod.) platí: uvedený deň výluka odpadá, v ostatné dni sa vykoná.", "o7g03", ZA,
          "Wed, 18 Nov 2026 10:00:00 +0100")
    _process(session, utc(2026, 11, 18, 9, 2))
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 11, 24, 10)) and _closed_at(session, loc(2026, 11, 26, 10)), _show(session)
    r = _decide(session, "63091", "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 24, 10), loc(2026, 11, 24, 8))
    assert _not_green(r), r.summary
