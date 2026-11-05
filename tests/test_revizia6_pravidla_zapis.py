"""6. revízia – strana pravidiel (FA) od e-mailu po plánovač: formulácie FC0 – FC7 a sieť proti strate termínu
skutočným spracovaním pošty (DMARC, ``process_mailbox``), potvrdením vo fronte a vyhodnotením vlaku.

- FC0: nová výluka „… je od 23.11. 08:00 hod. až do odvolania úplná výluka …“ – vlak v úseku nedostane VYPRAVIŤ
  ZELENÁ; aktualizácia „od … do odvolania výluka …, predpokladané ukončenie …“ potvrdenú výluku nezruší;
- FC1: náhradný termín v e-maile „Zrušenie výluky dňa 24.11. – ROV …“ – vlak v novom termíne nie je ZELENÝ;
- FC3: zrušenie „v sobotu a v nedeľu“ bez dátumu po potvrdení výluku nezruší (zápis odmietne);
- FC4: „nekoná v čase od 08:00 do 10:00, ale až od 10:00 do 14:00“ – po potvrdení je úsek uzavretý od 10:00;
- FC5: „v noci na 26.11.“ zruší noc 25./26.11., noc 26./27.11. ostane;
- FC6: „predlžuje sa do 29.11., denne 08:00 – 14:00“ – čakajúce predĺženie bez začiatku je poistkou aj 28.11.;
- FC7: „sa mení z 08:00 – 14:00 hod. na 06:00 – 16:00 hod.“ – vlak o 06:30 nie je ZELENÝ;
- sieť: „Výluka bude aj 08.12.2026, čas upresníme.“ – vlak 08.12. nie je ZELENÝ.

Všetky adresy a osoby sú fiktívne (*.example).
"""
from __future__ import annotations

from sqlalchemy import select

from vlaky.models import Closure
from test_revizia3_terminy import drop, events, raw_mail, utc
from test_revizia4_pravidla import _decide, _process, env  # noqa: F401 – fixture env
from test_revizia5_integracia_pravidla import _closed_at, _closure, _confirm_all, _l, loc

ZA = "ZSR OR Zilina <vyluky.orza@zsr.example>"
KE = "ZSR OR Kosice <vyluky.orke@zsr.example>"
TT = "ZSR OR Trnava <vyluky.ortt@zsr.example>"
SIG = "\n\nS pozdravom\nMarta Vzorová\nOR Žilina, oddelenie výluk\n"


def _mail(env, subject: str, body: str, mid: str, sender: str, date: str) -> None:
    drop(env, f"{mid}.eml", raw_mail(subject, "Dobrý deň,\n\n" + body + SIG, mid, sender=sender, date=date))


def _not_green(r) -> bool:
    return not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ")


def test_fc0_nova_vyluka_do_odvolania_e2e(env, session):
    """[FC0 a] Nová výluka do odvolania sa zapíše ako výluka (nie zrušenie) a vlak v úseku 24.11. o 10:00 nedostane
    VYPRAVIŤ ZELENÁ, kým čaká na potvrdenie."""
    _mail(env, "ROV č. 2701/2026 – výluka do odvolania", "z dôvodu poškodenia mosta je od 23.11.2026 08:00 hod. až "
          "do odvolania úplná výluka trate v úseku Kraľovany – Ružomberok (ROV č. 2701/2026).", "fc0a", ZA,
          "Fri, 20 Nov 2026 09:00:00 +0100")
    _process(session, utc(2026, 11, 20, 8, 2))
    evs = events(session)
    assert [e.kind for e in evs] == ["vyluka_nova"], [(e.kind, e.data) for e in evs]
    assert evs[0].data.get("until_revoked") is True and _l(evs[0].data.get("start")) == "23.11. 08:00"
    r = _decide(session, "61011", "Kraľovany", "Ružomberok", loc(2026, 11, 24, 10), loc(2026, 11, 24, 8))
    assert _not_green(r), r.summary


def test_fc0_aktualizacia_do_odvolania_e2e_vyluku_nezrusi(env, session):
    """[FC0 b] Potvrdená výluka 23.11. 08:00 – 27.11. 18:00; aktualizácia „od 23.11.2026 08:00 hod. do odvolania
    výluka 1. a 2. TK …, predpokladané ukončenie 27.11.2026 o 18:00“ – po potvrdení výluka nie je zrušená a vlak
    26.11. o 10:00 nedostane VYPRAVIŤ ZELENÁ."""
    c = _closure(session, "Kraľovany", "Ružomberok", loc(2026, 11, 23, 8), loc(2026, 11, 27, 18), "2708/2026")
    _mail(env, "ROV č. 2708/2026 – aktualizácia", "od 23.11.2026 08:00 hod. do odvolania výluka 1. a 2. traťovej "
          "koľaje v úseku Kraľovany – Ružomberok, predpokladané ukončenie 27.11.2026 o 18:00 hod. (ROV č. 2708/2026).",
          "fc0b", ZA, "Sat, 21 Nov 2026 09:00:00 +0100")
    _process(session, utc(2026, 11, 21, 8, 2))
    assert "vyluka_zrusenie" not in [e.kind for e in events(session)], [(e.kind, e.data) for e in events(session)]
    _confirm_all(session)
    session.refresh(c)
    assert c.status != "zrusene"
    assert _closed_at(session, loc(2026, 11, 26, 10))
    r = _decide(session, "61012", "Kraľovany", "Ružomberok", loc(2026, 11, 26, 10), loc(2026, 11, 26, 8))
    assert _not_green(r), r.summary


def test_fc1_nahradny_termin_e2e(env, session):
    """[FC1] „Zrušenie výluky dňa 24.11.2026 – ROV č. 2328/2026“ s náhradným termínom 28.11. 08:00 – 14:00 – vlak
    28.11. o 10:00 nedostane VYPRAVIŤ ZELENÁ (nový termín čaká na potvrdenie ako výluka)."""
    _closure(session, "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 24, 8), loc(2026, 11, 24, 14), "2328/2026")
    _mail(env, "Zrušenie výluky dňa 24.11.2026 – ROV č. 2328/2026", "výluka v úseku Ružomberok – Liptovský Mikuláš "
          "dňa 24.11.2026 sa ruší. Náhradný termín výluky je 28.11.2026 od 08:00 do 14:00 hod.", "fc1", ZA,
          "Sat, 21 Nov 2026 09:00:00 +0100")
    _process(session, utc(2026, 11, 21, 8, 2))
    news = [e for e in events(session) if e.kind == "vyluka_nova"]
    assert [(_l(e.data.get("start")), _l(e.data.get("end"))) for e in news] == [("28.11. 08:00", "28.11. 14:00")]
    r = _decide(session, "61021", "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 28, 10), loc(2026, 11, 28, 8))
    assert _not_green(r), r.summary


def test_fc3_zrusenie_dni_v_tyzdni_e2e_vyluku_nezrusi(env, session):
    """[FC3] Denná výluka 23. – 29.11. 08:00 – 14:00; „… sa v sobotu a v nedeľu nevykoná. Od pondelka do piatku bez
    zmeny.“ – zrušenie bez dátumu sa nezapíše (celú výluku nezruší); v stredu 25.11. o 10:00 je úsek uzavretý."""
    c = _closure(session, "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 23, 8), loc(2026, 11, 29, 14), "2501/2026",
                 daily=("08:00", "14:00"))
    _mail(env, "ROV č. 2501/2026 – víkend", "výluka podľa ROV č. 2501/2026 v úseku Ružomberok – Liptovský Mikuláš sa "
          "v sobotu a v nedeľu nevykoná. Od pondelka do piatku bez zmeny.", "fc3", ZA, "Sat, 21 Nov 2026 09:00:00 +0100")
    _process(session, utc(2026, 11, 21, 8, 2))
    (ev,) = events(session)
    assert ev.kind == "vyluka_zrusenie" and ev.data.get("partial") is True
    _confirm_all(session)
    session.refresh(ev)
    session.refresh(c)
    assert c.status != "zrusene"
    assert not (ev.validation.get("apply_result") or {}).get("ok"), ev.validation.get("apply_result")
    assert _closed_at(session, loc(2026, 11, 25, 10))


def test_fc4_ruseny_termin_e2e(env, session):
    """[FC4] Výluka 10.12. 08:00 – 14:00; „… sa nekoná v čase od 08:00 do 10:00 hod., ale až od 10:00 do 14:00 hod.“
    – po potvrdení je úsek o 09:00 prejazdný a o 11:00 uzavretý (vlak o 11:00 nie je ZELENÝ)."""
    _closure(session, "Trnava", "Leopoldov", loc(2026, 12, 10, 8), loc(2026, 12, 10, 14), "3206/2026")
    _mail(env, "ROV č. 3206/2026 – posun začiatku", "výluka Trnava – Leopoldov dňa 10.12.2026 sa nekoná v čase od 08:00 "
          "do 10:00 hod., ale až od 10:00 do 14:00 hod. (ROV č. 3206/2026).", "fc4", TT,
          "Tue, 01 Dec 2026 09:00:00 +0100")
    _process(session, utc(2026, 12, 1, 8, 2))
    (ev,) = events(session)
    assert (ev.kind, _l(ev.data.get("start")), _l(ev.data.get("end"))) == ("vyluka_zrusenie", "10.12. 08:00",
                                                                           "10.12. 10:00")
    _confirm_all(session)
    assert _closed_at(session, loc(2026, 12, 10, 11)) and not _closed_at(session, loc(2026, 12, 10, 9))
    r = _decide(session, "61041", "Trnava", "Leopoldov", loc(2026, 12, 10, 11), loc(2026, 12, 10, 9))
    assert _not_green(r), r.summary


def test_fc5_v_noci_na_den_e2e(env, session):
    """[FC5] Nočné výluky 23./24. – 28./29.11. 22:00 – 04:00; „v noci na 26.11.2026 sa výluka … nekoná“ – po
    potvrdení je noc 25./26. prejazdná a noc 26./27. ostane uzavretá."""
    _closure(session, "Kysak", "Košice", loc(2026, 11, 23, 22), loc(2026, 11, 29, 4), "4503/2026",
             daily=("22:00", "04:00"))
    _mail(env, "ROV č. 4503/2026", "v noci na 26.11.2026 sa výluka Kysak – Košice podľa ROV č. 4503/2026 nekoná.", "fc5",
          KE, "Sat, 21 Nov 2026 09:00:00 +0100")
    _process(session, utc(2026, 11, 21, 8, 2))
    (ev,) = events(session)
    assert (_l(ev.data.get("start")), _l(ev.data.get("end"))) == ("25.11. 22:00", "26.11. 06:00"), ev.data
    _confirm_all(session)
    assert not _closed_at(session, loc(2026, 11, 26, 1))
    assert _closed_at(session, loc(2026, 11, 26, 23)) and _closed_at(session, loc(2026, 11, 25, 1))


def test_fc6_predlzenie_e2e_cakajuca_poistka(env, session):
    """[FC6] Denná výluka 23. – 27.11. 08:00 – 14:00; „… sa predlžuje do 29.11.2026, denne od 08:00 do 14:00 hod.“ –
    predĺženie bez začiatku (nie jednodňový termín 29.11.) čaká na dispečera a vlak 28.11. o 10:00 nie je ZELENÝ."""
    _closure(session, "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 23, 8), loc(2026, 11, 27, 14), "2609/2026",
             daily=("08:00", "14:00"))
    _mail(env, "Predĺženie výluky – ROV č. 2609/2026", "výluka Ružomberok – Liptovský Mikuláš podľa ROV č. 2609/2026 sa "
          "predlžuje do 29.11.2026, denne od 08:00 do 14:00 hod.", "fc6", ZA, "Sat, 21 Nov 2026 09:00:00 +0100")
    _process(session, utc(2026, 11, 21, 8, 2))
    (ev,) = events(session)
    assert (ev.kind, ev.data.get("zamer"), _l(ev.data.get("start")), _l(ev.data.get("end"))) == \
        ("vyluka_zmena", "predlzenie", None, "29.11. 14:00"), ev.data
    r = _decide(session, "61061", "Ružomberok", "Liptovský Mikuláš", loc(2026, 11, 28, 10), loc(2026, 11, 28, 8))
    assert _not_green(r), r.summary


def test_fc7_zmena_casu_e2e(env, session):
    """[FC7] Výluka 10.12. 08:00 – 14:00; „… sa mení z 08:00 – 14:00 hod. na 06:00 – 16:00 hod.“ – čakajúca zmena má
    nový čas a vlak 10.12. o 06:30 nedostane VYPRAVIŤ ZELENÁ."""
    _closure(session, "Trnava", "Leopoldov", loc(2026, 12, 10, 8), loc(2026, 12, 10, 14), "3807/2026")
    _mail(env, "ROV č. 3807/2026 – zmena času", "výluka Trnava – Leopoldov dňa 10.12.2026 (ROV č. 3807/2026) sa mení "
          "z 08:00 – 14:00 hod. na 06:00 – 16:00 hod.", "fc7", TT, "Tue, 01 Dec 2026 09:00:00 +0100")
    _process(session, utc(2026, 12, 1, 8, 2))
    (ev,) = events(session)
    assert (ev.kind, ev.data.get("zamer"), _l(ev.data.get("start")), _l(ev.data.get("end"))) == \
        ("vyluka_zmena", "zmena_casu", "10.12. 06:00", "10.12. 16:00"), ev.data
    r = _decide(session, "61071", "Trnava", "Leopoldov", loc(2026, 12, 10, 6, 30), loc(2026, 12, 10, 5))
    assert _not_green(r), r.summary


def test_siet_termin_bez_udalosti_e2e(env, session):
    """[sieť] „Úplná výluka … dňa 01.12. … Výluka bude aj 08.12.2026, čas upresníme.“ – termín 08.12. dá čakajúcu
    výluku a vlak Kysak → Košice 08.12. o 10:00 nedostane VYPRAVIŤ ZELENÁ."""
    _mail(env, "ROV č. 2994/2026", "Úplná výluka v úseku Kysak – Košice dňa 01.12.2026 od 08:00 do 12:00 hod. Výluka "
          "bude aj 08.12.2026, čas upresníme.", "siet", KE, "Fri, 27 Nov 2026 09:00:00 +0100")
    _process(session, utc(2026, 11, 27, 8, 2))
    nets = [e for e in events(session) if "nepatrí k žiadnej zapísanej udalosti" in " ".join(
        (e.validation or {}).get("notes") or [])]
    assert len(nets) == 1 and nets[0].status == "navrh" and nets[0].needs_confirmation, \
        [(e.kind, e.data, e.validation) for e in events(session)]
    rows = [c for c in session.scalars(select(Closure)) if c.status != "zrusene"]
    assert rows, "čakajúca výluka nemá záznam pre plánovač"
    r = _decide(session, "61081", "Kysak", "Košice", loc(2026, 12, 8, 10), loc(2026, 12, 8, 8))
    assert _not_green(r), r.summary
