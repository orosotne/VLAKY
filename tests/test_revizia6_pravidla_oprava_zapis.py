"""6. revízia – strana pravidiel (FA), oprava po kontrole: od e-mailu po plánovač (DMARC, ``process_mailbox``,
potvrdenie vo fronte, register, vyhodnotenie vlaku) – vlak v čase, ktorý e-mail uzatvára, nedostane VYPRAVIŤ ZELENÁ:

- dve obdobia s vlastným denným oknom („… denne 10:00 – 12:00 a 30.11. – 04.12. denne 08:00 – 14:00“);
- náhradný termín len v predmete zrušenia; „STORNO … Výluka sa uskutoční 10.12. od 10:00 do 14:00“; „Zároveň
  oznamujeme výluku <iný úsek> …“ (iný úsek sa nezruší);
- zrušenie „okrem soboty a nedele“ – sobota a nedeľa ostanú uzavreté; denné okno zrušené po dňoch;
- noci „v nociach na 26.11. a 27.11.“; ďalšie okno toho istého dňa; zmena času na dve okná;
- „nebude úsek … prejazdný“ (nie „iné“); administratívna lehota nedá poistku pre celý vozový park.

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


def test_dve_obdobia_s_vlastnym_oknom_e2e(env, session):
    """[dátumy, regresia] „v dňoch 23.11. – 27.11. denne 10:00 – 12:00 a 30.11. – 04.12. denne 08:00 – 14:00“ – po
    potvrdení je úsek 01.12. o 09:00 a o 13:00 uzavretý (nie len 10:00 – 12:00) a vlak 01.12. o 09:00 nie je ZELENÝ."""
    _mail(env, "ROV č. 2702/2026 – úplná výluka Ružomberok – Liptovský Mikuláš", "úplná výluka v úseku Ružomberok – "
          "Liptovský Mikuláš v dňoch 23.11. – 27.11.2026 denne 10:00 – 12:00 a 30.11. – 04.12.2026 denne 08:00 – 14:00.",
          "o6dat", ZA, "Wed, 18 Nov 2026 10:00:00 +0100")
    _process(session, utc(2026, 11, 18, 9, 2))
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 12, 1, 9)) and _closed_at(session, loc(2026, 12, 1, 13)), _show(session)
    assert _closed_at(session, loc(2026, 11, 24, 11)) and not _closed_at(session, loc(2026, 11, 24, 9))
    r = _decide(session, "62011", "Ružomberok", "Liptovský Mikuláš", loc(2026, 12, 1, 9), loc(2026, 12, 1, 7))
    assert _not_green(r), r.summary


def test_nahradny_termin_v_predmete_zrusenia_e2e(env, session):
    """[FC1] „Zrušenie ROV č. 3128/2026 – náhradný termín 15.12.2026 08:00 – 14:00“ – nový termín je čakajúca výluka a
    vlak 15.12. o 10:00 nie je ZELENÝ (ani pred potvrdením, ani po ňom)."""
    _closure(session, "Trnava", "Leopoldov", loc(2026, 12, 10, 8), loc(2026, 12, 10, 14), "3128/2026")
    _mail(env, "Zrušenie ROV č. 3128/2026 – náhradný termín 15.12.2026 08:00 – 14:00", "výluka Trnava – Leopoldov dňa "
          "10.12.2026 sa ruší.", "o6b11", TT, "Tue, 01 Dec 2026 09:00:00 +0100")
    _process(session, utc(2026, 12, 1, 8, 2))
    news = [e for e in events(session) if e.kind == "vyluka_nova"]
    assert [(_l(e.data.get("start")), _l(e.data.get("end"))) for e in news] == [("15.12. 08:00", "15.12. 14:00")], \
        _show(session)
    r = _decide(session, "62021", "Trnava", "Leopoldov", loc(2026, 12, 15, 10), loc(2026, 12, 15, 8))
    assert _not_green(r), r.summary
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 12, 15, 10)), _show(session)


def test_storno_s_uzsim_oknom_e2e(env, session):
    """[FC1] „STORNO ROV … (10.12. 08:00 – 14:00). Výluka sa uskutoční 10.12. od 10:00 do 14:00“ – po potvrdení je
    úsek o 11:00 uzavretý (čas, ktorý ostáva, sa nestratí) a vlak o 11:00 nie je ZELENÝ."""
    _closure(session, "Trnava", "Leopoldov", loc(2026, 12, 10, 8), loc(2026, 12, 10, 14), "3129/2026")
    _mail(env, "STORNO ROV č. 3129/2026", "STORNO ROV č. 3129/2026 (Trnava – Leopoldov 10.12.2026 od 08:00 do 14:00). "
          "Výluka sa uskutoční 10.12.2026 od 10:00 do 14:00 hod.", "o6b18", TT, "Tue, 01 Dec 2026 09:00:00 +0100")
    _process(session, utc(2026, 12, 1, 8, 2))
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 12, 10, 11)), _show(session)
    r = _decide(session, "62031", "Trnava", "Leopoldov", loc(2026, 12, 10, 11), loc(2026, 12, 10, 9))
    assert _not_green(r), r.summary


def test_zaroven_oznamujeme_iny_usek_e2e(env, session):
    """[FC1] „… 10.12. sa ruší. Zároveň oznamujeme výluku Leopoldov – Piešťany dňa 11.12. 08:00 – 14:00“ – úsek
    Leopoldov – Piešťany sa nezruší, jeho výluka 11.12. sa zapíše a vlak 11.12. o 10:00 nie je ZELENÝ."""
    _closure(session, "Trnava", "Leopoldov", loc(2026, 12, 10, 8), loc(2026, 12, 10, 14), "3132/2026")
    _mail(env, "Zrušenie ROV č. 3132/2026", "Výluka Trnava – Leopoldov dňa 10.12.2026 sa ruší. Zároveň oznamujeme výluku "
          "Leopoldov – Piešťany dňa 11.12.2026 od 08:00 do 14:00 hod.", "o6b21", TT, "Tue, 01 Dec 2026 09:00:00 +0100")
    _process(session, utc(2026, 12, 1, 8, 2))
    cancels = [e for e in events(session) if e.kind == "vyluka_zrusenie"]
    assert [(e.data.get("from_station"), e.data.get("to_station")) for e in cancels] == [("Trnava", "Leopoldov")], \
        _show(session)
    _confirm_all(session)
    r = _decide(session, "62041", "Leopoldov", "Piešťany", loc(2026, 12, 11, 10), loc(2026, 12, 11, 8))
    assert _not_green(r), r.summary


def test_zrusenie_okrem_soboty_a_nedele_e2e(env, session):
    """[FC3] Denná výluka 23. – 29.11. 08:00 – 14:00; „v dňoch 23.11. – 29.11. sa ruší okrem soboty a nedele“ – po
    potvrdení sú sobota 28.11. a nedeľa 29.11. o 10:00 uzavreté (nikdy sa nezrušia práve dni, ktoré ostávajú)."""
    _closure(session, "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 23, 8), loc(2026, 11, 29, 14), "2501/2026",
             daily=("08:00", "14:00"))
    _mail(env, "Zrušenie výluky – ROV č. 2501/2026", "výluka podľa ROV č. 2501/2026 v úseku Ružomberok – Liptovský "
          "Mikuláš v dňoch 23.11. – 29.11.2026 sa ruší okrem soboty a nedele.", "o6d21", ZA,
          "Wed, 18 Nov 2026 10:00:00 +0100")
    _process(session, utc(2026, 11, 18, 9, 2))
    cancels = [e for e in events(session) if e.kind == "vyluka_zrusenie"]
    assert cancels and all(_l(e.data.get("start"))[:2] in ("23", "24", "25", "26", "27") for e in cancels), \
        _show(session)
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 11, 28, 10)) and _closed_at(session, loc(2026, 11, 29, 10)), _show(session)
    r = _decide(session, "62051", "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 28, 10), loc(2026, 11, 28, 8))
    assert _not_green(r), r.summary


def test_zrusenie_casti_bez_terminu_e2e(env, session):
    """[FC3 sieť] „… sa ruší v prvý deň výluky.“ – zrušenie bez určeného termínu po potvrdení výluku nezruší."""
    c = _closure(session, "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 23, 8), loc(2026, 11, 29, 14), "2501/2026",
                 daily=("08:00", "14:00"))
    _mail(env, "Zrušenie výluky – ROV č. 2501/2026", "výluka podľa ROV č. 2501/2026 v úseku Ružomberok – Liptovský "
          "Mikuláš sa ruší v prvý deň výluky.", "o6d24", ZA, "Wed, 18 Nov 2026 10:00:00 +0100")
    _process(session, utc(2026, 11, 18, 9, 2))
    (ev,) = events(session)
    assert ev.kind == "vyluka_zrusenie" and ev.data.get("partial") is True, _show(session)
    _confirm_all(session)
    session.refresh(c)
    assert c.status != "zrusene"
    assert _closed_at(session, loc(2026, 11, 24, 10)) and _closed_at(session, loc(2026, 11, 26, 10))


def test_denne_okno_zrusene_po_dnoch_e2e(env, session):
    """[FC4] Denná výluka 23. – 27.11. 08:00 – 14:00; „… sa ruší v čase od 12:00 do 14:00 hod., od 08:00 do 12:00 hod.
    ostáva“ – po potvrdení je 24.11. o 09:00 úsek uzavretý (súvislý rušený čas by zrušil aj 08:00 – 12:00)."""
    _closure(session, "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 23, 8), loc(2026, 11, 27, 14), "2603/2026",
             daily=("08:00", "14:00"))
    _mail(env, "Zrušenie časti výluky – ROV č. 2603/2026", "výluka Ružomberok – Liptovský Mikuláš podľa ROV č. "
          "2603/2026 v dňoch 23.11. – 27.11.2026 sa ruší v čase od 12:00 do 14:00 hod., od 08:00 do 12:00 hod. ostáva.",
          "o6e23", ZA, "Wed, 18 Nov 2026 10:00:00 +0100")
    _process(session, utc(2026, 11, 18, 9, 2))
    cancels = [e for e in events(session) if e.kind == "vyluka_zrusenie"]
    assert sorted((_l(e.data.get("start")), _l(e.data.get("end"))) for e in cancels) == \
        [(f"{d}.11. 12:00", f"{d}.11. 14:00") for d in (23, 24, 25, 26, 27)], _show(session)
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 11, 24, 9)) and _closed_at(session, loc(2026, 11, 25, 10))
    r = _decide(session, "62061", "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 24, 9), loc(2026, 11, 24, 7))
    assert _not_green(r), r.summary


def test_v_nociach_na_e2e(env, session):
    """[FC5] „úplná výluka … v nociach na 26.11. a 27.11.2026 vždy od 22:00 do 04:00“ – po potvrdení je noc
    25./26.11. o 23:00 uzavretá (nie o noc neskôr)."""
    _mail(env, "ROV č. 4610/2026", "úplná výluka trate Kysak – Košice v nociach na 26.11. a 27.11.2026 vždy od 22:00 do "
          "04:00 hod. (ROV č. 4610/2026).", "o6f21", KE, "Wed, 18 Nov 2026 10:00:00 +0100")
    _process(session, utc(2026, 11, 18, 9, 2))
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 11, 25, 23)) and _closed_at(session, loc(2026, 11, 26, 23)), _show(session)
    r = _decide(session, "62071", "Kysak", "Košice", loc(2026, 11, 25, 23), loc(2026, 11, 25, 21))
    assert _not_green(r), r.summary


def test_dalsie_okno_toho_isteho_dna_e2e(env, session):
    """[osamelé okno] „… dňa 10.12.2026 od 08:00 do 10:00 hod., ďalej od 13:00 do 16:00 hod.“ – po potvrdení je
    úsek o 14:00 uzavretý a vlak o 14:00 nie je ZELENÝ."""
    _mail(env, "ROV č. 3922/2026", "úplná výluka trate Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 10:00 hod., ďalej "
          "od 13:00 do 16:00 hod.", "o6n21", TT, "Tue, 01 Dec 2026 09:00:00 +0100")
    _process(session, utc(2026, 12, 1, 8, 2))
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 12, 10, 14)) and _closed_at(session, loc(2026, 12, 10, 9)), _show(session)
    r = _decide(session, "62081", "Trnava", "Leopoldov", loc(2026, 12, 10, 14), loc(2026, 12, 10, 12))
    assert _not_green(r), r.summary


def test_zmena_casu_na_dve_okna_e2e(env, session):
    """[FC7] Výluka 10.12. 08:00 – 14:00; „… sa mení z 08:00 – 14:00 hod. na 06:00 – 12:00 hod. a 13:00 – 16:00 hod.“ –
    po potvrdení je úsek o 06:30 aj o 13:30 uzavretý (druhé okno sa nestratí)."""
    _closure(session, "Trnava", "Leopoldov", loc(2026, 12, 10, 8), loc(2026, 12, 10, 14), "3807/2026")
    _mail(env, "Zmena ROV č. 3807/2026", "výluka Trnava – Leopoldov dňa 10.12.2026 (ROV č. 3807/2026) sa mení z 08:00 – "
          "14:00 hod. na 06:00 – 12:00 hod. a 13:00 – 16:00 hod.", "o6h18", TT, "Tue, 01 Dec 2026 09:00:00 +0100")
    _process(session, utc(2026, 12, 1, 8, 2))
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 12, 10, 13, 30)) and _closed_at(session, loc(2026, 12, 10, 6, 30)), \
        _show(session)
    r = _decide(session, "62091", "Trnava", "Leopoldov", loc(2026, 12, 10, 13, 30), loc(2026, 12, 10, 12))
    assert _not_green(r), r.summary


def test_nebude_usek_prejazdny_e2e(env, session):
    """[sieť, „iné“] „Dňa 10.12.2026 od 08:00 do 14:00 hod. nebude úsek Trnava – Leopoldov prejazdný.“ – čakajúca
    úplná výluka; vlak 10.12. o 10:00 nie je ZELENÝ."""
    _mail(env, "Oznámenie", "Dňa 10.12.2026 od 08:00 do 14:00 hod. nebude úsek Trnava – Leopoldov prejazdný.", "o6n26",
          TT, "Tue, 01 Dec 2026 09:00:00 +0100")
    _process(session, utc(2026, 12, 1, 8, 2))
    evs = events(session)
    assert [(e.kind, e.data.get("kind")) for e in evs] == [("vyluka_nova", "uplna")], _show(session)
    r = _decide(session, "62101", "Trnava", "Leopoldov", loc(2026, 12, 10, 10), loc(2026, 12, 10, 8))
    assert _not_green(r), r.summary


def test_administrativna_lehota_bez_poistky_pre_vozovy_park_e2e(env, session):
    """[sieť, falošný poplach] ROV s dvomi úsekmi a vetou „Prosíme o potvrdenie prijatia do 02.12.2026 10:00 hod.“ –
    žiadna udalosť bez miesta (poistka pre celý vozový park by zdržala nesúvisiaci vlak Kysak → Košice)."""
    _mail(env, "ROV č. 3990/2026", "Úplná výluka Trnava – Leopoldov dňa 10.12.2026 od 08:00 do 14:00 hod. a výluka "
          "Leopoldov – Piešťany dňa 11.12.2026 od 08:00 do 14:00 hod. (ROV č. 3990/2026). Prosíme o potvrdenie "
          "prijatia do 02.12.2026 10:00 hod.", "o6k08", TT, "Tue, 01 Dec 2026 09:00:00 +0100")
    _process(session, utc(2026, 12, 1, 8, 2))
    evs = events(session)
    assert len(evs) == 2 and all(e.data.get("from_station") and e.data.get("to_station") for e in evs), \
        _show(session)
    r = _decide(session, "62111", "Kysak", "Košice", loc(2026, 12, 2, 12), loc(2026, 12, 2, 10))
    assert (r.decision, r.color) == ("VYPRAVIŤ", "ZELENÁ"), r.summary
