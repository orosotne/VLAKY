"""Štvrtá revízia – pravidlá e-mailov ŽSR (oblasť M4), doplnky k QC0–QC6 a QM0–QM5: regresné testy.

- zámer e-mailu z tela: veta, ktorá ruší jednu výluku, nesmie zmazať novú výluku v ďalšej vete; možné či budúce
  zrušenie („o prípadnom zrušení Vás budeme informovať“) nie je zrušenie; „… sa ruší a nahrádza ho …“ je zmena
  (QC5, princíp „prísnejší prípad“);
- „úplná výluka trakčného vedenia“ bez zmienky o motorových vlakoch je predpokladaná úplná výluka s poznámkou,
  s výslovným „motorové vlaky môžu prejsť“ výluka trakčného vedenia; „napäťová výluka oboch TK“ je trakčné vedenie
  (QC1);
- výluka stanice uvedená vedľa úseku je ďalšia udalosť, stanica, kde vlaky len čakajú, nie (QC2);
- rušený deň len v predmete sa nestratí (QC4); číslo rušenej výluky je ROV, nie telegram, ktorým sa ruší; zrušenie
  tej istej výluky v predmete aj v texte priloženého e-mailu je jedna udalosť (QC5, QM4);
- príloha s INOU výlukou, ktorej číslo text odpovede len spomína, nie je citáciou (QM3);
- newsletter nie je prevádzkové oznámenie (QC5); označené riadky „Začiatok výluky:“ / „Koniec výluky:“, tabuľka z HTML
  a koľaj položky zoznamu (QC0, princíp „nič sa nestratí potichu“).

Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

from test_revizia3_terminy import drop, events, iso, raw_mail, rows, rules, utc
from test_revizia4_pravidla import ROV_KE, ROV_ZA, _decide, _process, env  # noqa: F401 – fixture env

LT = "úplná výluka oboch TK Leopoldov – Trnava dňa 26.11.2026 od 8:00 do 12:00"


def _kinds(items) -> list[tuple]:
    return [(i.kind, i.data.get("external_ref")) for i in items]


# ====================================================================== zámer e-mailu z tela (QC5)
def test_qc5_zrusenie_vyluky_a_nova_vyluka_v_jednom_emaili(cfg):
    """Zrušenie výluky v jednej vete a nová výluka s termínom v ďalšej – obe udalosti (nová výluka sa nestratí);
    budúce či možné zrušenie nie je zrušenie; „sa ruší a nahrádza ho“ je zmena výluky s novým termínom."""
    items = rules(f"Výluka podľa ROV č. 2241/2026 sa nekoná. Nová výluka podľa ROV č. 2250/2026: {LT}.\n",
                  subject="ROV", received=utc(2026, 11, 16, 8))
    assert _kinds(items) == [("vyluka_nova", "2250/2026"), ("vyluka_zrusenie", "2241/2026")], _kinds(items)
    assert (items[0].data["from_station"], items[0].data["start"]) == ("Leopoldov", iso(2026, 11, 26, 7))
    (new,) = rules(f"{LT[0].upper()}{LT[1:]} (ROV č. 2241/2026). O prípadnom zrušení výluky Vás budeme informovať.\n",
                   subject="ROV č. 2241/2026", received=utc(2026, 11, 16, 8))
    assert (new.kind, new.data["kind"], new.data["start"]) == ("vyluka_nova", "uplna", iso(2026, 11, 26, 7))
    (chg,) = rules(f"ROV č. 2241/2026 sa ruší a nahrádza ho ROV č. 2250/2026: {LT}.\n", subject="ROV č. 2241/2026",
                   received=utc(2026, 11, 16, 8))
    assert (chg.kind, chg.data["start"], chg.data["end"]) == ("vyluka_zmena", iso(2026, 11, 26, 7),
                                                              iso(2026, 11, 26, 11))
    (part,) = rules("Ruší sa výluka podľa ROV č. 1315/2026 len v deň 25.11.2026, ostatné dni výluky platia bez zmeny.\n",
                    subject="Informácia k ROV č. 1315/2026", received=utc(2026, 11, 16, 8))
    assert part.kind == "vyluka_zrusenie" and part.data["start"] == iso(2026, 11, 24, 23)


def test_qc5_end_to_end_nova_vyluka_vedla_zrusenia_blokuje_vlak(env, session):
    """End to end: e-mail „ROV … sa nekoná. Nová výluka …“ – nová výluka dostane záznam „navrh“ a vlak počas nej
    nedostane VYPRAVIŤ; zrušenie staršej výluky čaká na potvrdenie dispečera."""
    drop(env, "z.eml", raw_mail("ROV c. 2250/2026 - Leopoldov - Trnava", "Výluka podľa ROV č. 2241/2026 sa nekoná. Nová "
                                f"výluka podľa ROV č. 2250/2026: {LT}.\n", "z2250", sender=ROV_ZA,
                                date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 5))
    assert sorted(e.kind for e in events(session)) == ["vyluka_nova", "vyluka_zrusenie"]
    (row,) = rows(session)
    assert (row.from_station, row.to_station, row.kind, row.status) == ("Leopoldov", "Trnava", "uplna", "navrh")
    r = _decide(session, "77571", "Leopoldov", "Trnava", utc(2026, 11, 26, 8), utc(2026, 11, 26, 5))
    assert r.decision != "VYPRAVIŤ", (r.decision, r.summary)


def test_qc5_newsletter_nie_je_vyluka_ale_rov_s_patou_ano(cfg):
    """Newsletter (podľa predmetu) spomína výluky, ale nič neohlasuje – „iné“; prevádzkový ROV s pätou o odhlásení
    z rozosielania je výluka (telo e-mailu o zámere nerozhoduje)."""
    (nl,) = rules("Vážení partneri, v novembri prebehne 120 výluk, napríklad výluka v úseku Kysak – Košice dňa "
                  "20.11.2026 od 8:00 do 14:00.\n", subject="Newsletter ŽSR – november 2026", received=utc(2026, 11, 9, 9))
    assert nl.kind == "ine" and "newsletter" in nl.notes[0]
    (rov,) = rules("Úplná výluka oboch TK v úseku Kysak – Košice dňa 20.11.2026 od 8:00 do 14:00 (ROV č. 1160/2026).\n\n"
                   "Odhlásenie z rozosielania: rozosielanie@zsr.example\n", subject="ROV č. 1160/2026",
                   received=utc(2026, 11, 9, 9))
    assert (rov.kind, rov.data["kind"]) == ("vyluka_nova", "uplna")


# ====================================================================== úplná výluka a trakčné vedenie (QC1)
TV = "výluka trakčného vedenia v úseku Štrba – Poprad-Tatry dňa 24.11.2026 od 08:00 do 14:00 hod."


def test_qc1_uplna_vyluka_trakcneho_vedenia(cfg):
    """„Úplná výluka trakčného vedenia“: bez výslovného „motorové vlaky môžu prejsť“ prísnejší prípad (úplná výluka,
    poznámka „overte druh“ – motorový vlak by inak išiel do uzavretého úseku), s ním výluka trakčného vedenia;
    „…nemôžu prechádzať“ je úplná výluka; „napäťová výluka oboch TK“ je trakčné vedenie (nie úplná výluka)."""
    cases = [
        (f"Úplná {TV}", "uplna", True),
        (f"Úplná {TV} Vlaky s motorovou trakciou môžu úsekom prejsť.", "trakcne_vedenie", False),
        (f"Úplná {TV} Vlaky s motorovou trakciou nemôžu úsekom prechádzať.", "uplna", False),
        ("Napäťová výluka oboch traťových koľají v úseku Štrba – Poprad-Tatry dňa 24.11.2026 od 08:00 do 14:00 hod.",
         "trakcne_vedenie", False),
    ]
    for body, kind, note in cases:
        (it,) = rules(body + "\n", subject="ROV – výluka", received=utc(2026, 11, 16, 8))
        assert it.data["kind"] == kind, (body, it.data)
        assert any("trakčného vedenia" in n and "overte druh" in n for n in it.notes) == note, (body, it.notes)


# ====================================================================== výluka stanice vedľa úseku (QC2)
def test_qc2_vyluka_stanice_vedla_useku_je_dalsia_udalost(cfg):
    """„Úplná výluka v žst. Kraľovany a v úseku Kraľovany – Ružomberok“ – aj výluka stanice (zasahuje aj vlaky na
    trať do Trstenej); stanica, kde vlaky len čakajú, či stanica v zátvorke s odchodmi podľa GVD miestom nie je."""
    items = rules("Úplná výluka v žst. Kraľovany a v úseku Kraľovany – Ružomberok dňa 24.11.2026 od 08:00 do 14:00 "
                  "hod.\n", subject="ROV", received=utc(2026, 11, 16, 8))
    assert sorted((i.data["from_station"], i.data["to_station"]) for i in items) == [
        ("Kraľovany", "Kraľovany"), ("Kraľovany", "Ružomberok")]
    assert all(i.data["start"] == iso(2026, 11, 24, 7) for i in items)
    for body in ("Výluka 1. TK v úseku Kraľovany – Ružomberok dňa 24.11.2026 od 08:00 do 14:00 hod., vlaky čakajú v žst. "
                 "Kraľovany.", "Výluka 1. TK v úseku Kraľovany – Ružomberok dňa 24.11.2026 od 08:00 do 14:00 hod. Počas "
                 "výluky budú vlaky čakať v žst. Kraľovany.", "Výluka 1. TK Žilina – Varín 24.11.2026 od 08:00 do 14:00 "
                 "hod. (v žst. Žilina odchody podľa GVD)."):
        (one,) = rules(body + "\n", subject="ROV", received=utc(2026, 11, 16, 8))
        assert one.data["from_station"] != one.data["to_station"], body


# ====================================================================== zrušenie (QC4, QC5, QM4)
def test_qc4_ruseny_den_len_v_predmete(cfg):
    """„ROV č. 1315/2026 – zrušenie výluky dňa 25.11.2026“ a v texte len „oznamujeme zrušenie výluky“ – rušený deň
    sa nestratí (bez neho by potvrdenie zrušilo celú viacdennú výluku)."""
    (it,) = rules("Dobrý deň, oznamujeme zrušenie výluky.\n", subject="ROV č. 1315/2026 – zrušenie výluky dňa 25.11.2026",
                  received=utc(2026, 11, 16, 9))
    assert (it.kind, it.data["external_ref"]) == ("vyluka_zrusenie", "1315/2026")
    assert (it.data["start"], it.data["end"]) == (iso(2026, 11, 24, 23), iso(2026, 11, 25, 23))
    assert any("25.11.2026" in n for n in it.notes)


def test_qc5_cislo_zrusenia_je_rov_nie_telegram_a_duplicita(cfg):
    """Telegram „TLG č. 61/2026 – výluka … sa nekoná“ s „podľa ROV č. 1330/2026“ v texte ruší ROV 1330/2026 (nie
    výluku „61/2026“); preposlaný e-mail so zrušením v predmete aj v texte prílohy je jedno zrušenie s rušeným dňom."""
    from vlaky.posta.pravidla import RuleExtractor
    from vlaky.posta.udalosti import MailDocument

    (tlg,) = rules("TELEGRAM č. 61/2026\nVýluka TV v úseku Púchov – Považská Bystrica podľa ROV č. 1330/2026 dňa "
                   "19.11.2026 sa nekoná.\nKontrolný dispečer OR Žilina\n",
                   subject="TLG č. 61/2026 – výluka TV Púchov – Považská Bystrica sa nekoná", received=utc(2026, 11, 12, 9))
    assert (tlg.kind, tlg.data["external_ref"]) == ("vyluka_zrusenie", "1330/2026")
    att = ("From: ŽSR OR Košice <vyluky.orke@zsr.example>\nSubject: Zrušenie ROV č. 1360/2026 – Kysak – Košice\n"
           "Date: Mon, 16 Nov 2026 09:00:00 +0100\n\nVážení,\n\nROV č. 1360/2026 (výluka 2. TK Kysak – Košice dňa "
           "03.12.2026) sa ruší.\n")
    items = RuleExtractor().extract(MailDocument("Fwd: Zrušenie ROV č. 1360/2026 – Kysak – Košice",
                                                 "Preposielam (originál v prílohe).\n",
                                                 attachments=[("zrusenie_1360.eml", att)], received_at=utc(2026, 11, 17, 9)))
    assert [(i.kind, i.data["external_ref"], i.data.get("start")) for i in items] == [
        ("vyluka_zrusenie", "1360/2026", iso(2026, 12, 2, 23))], [(i.kind, i.data) for i in items]


def test_qc5_end_to_end_zrusenie_telegramom_zrusi_rov(env, session):
    """End to end: potvrdenie zrušenia z telegramu zruší výluku ROV 1330/2026 v registri (nie „nenašla sa“)."""
    from vlaky.models import Closure
    from vlaky.posta.spracovanie import confirm_event

    c = Closure(kind="trakcne_vedenie", from_station="Púchov", to_station="Považská Bystrica",
                start_utc=utc(2026, 11, 19, 8), end_utc=utc(2026, 11, 19, 12, 30), status="overene", source="email",
                external_ref="1330/2026")
    session.add(c)
    session.commit()
    drop(env, "tlg.eml", raw_mail("TLG c. 61/2026 - vyluka TV Puchov - Povazska Bystrica sa nekona", "TELEGRAM č. "
                                  "61/2026\nVýluka TV v úseku Púchov – Považská Bystrica podľa ROV č. 1330/2026 dňa "
                                  "19.11.2026 sa nekoná.\nKontrolný dispečer OR Žilina\n", "tlg61",
                                  sender="Kontrolny dispecer OR Zilina <kd.orza@zsr.example>",
                                  date="Thu, 12 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 12, 9, 5))
    (ev,) = events(session)
    assert ev.kind == "vyluka_zrusenie" and ev.data["external_ref"] == "1330/2026"
    confirm_event(session, ev.id, "dispecer1")
    session.commit()
    session.refresh(c)
    assert c.status == "zrusene"


# ====================================================================== príloha s inou výlukou (QM3)
def test_qm3_priloha_s_inou_vylukou_nie_je_citacia(cfg):
    """Odpoveď s novým termínom ROV 1142/2026: príloha s pôvodným ROV 1142/2026 je len citácia (jedna zmena s novým
    termínom), ale príloha s NOVÝM ROV 1150/2026, ktorý text odpovede len spomína, citáciou nie je – jej výluka sa
    nestratí a poznámka „Príloha opakuje tú istú výluku“ nie je."""
    from vlaky.posta.pravidla import RuleExtractor
    from vlaky.posta.udalosti import MailDocument

    reply = ("Úplná výluka oboch traťových koľají v úseku Poprad-Tatry – Liptovský Mikuláš podľa ROV č. 1142/2026 sa "
             "presúva na 14.11.2026 od 8:00 do 14:00.\n")
    same = RuleExtractor().extract(MailDocument(
        "RE: ROV č. 1142/2026", reply + "V prílohe pôvodný ROV.\n", attachments=[(
            "ROV_1142.txt", "Rozkaz o výluke č. 1142/2026: úplná výluka oboch traťových koľají v úseku Poprad-Tatry – "
                            "Liptovský Mikuláš 12.11.2026 od 8:00 do 14:00.\n")], received_at=utc(2026, 11, 3, 7, 55)))
    assert [(i.data["external_ref"], i.data["start"]) for i in same] == [("1142/2026", iso(2026, 11, 14, 7))]
    items = RuleExtractor().extract(MailDocument(
        "RE: ROV č. 1142/2026", reply + "V prílohe aj nový ROV č. 1150/2026.\n",
        attachments=[("ROV_1150.txt", "Rozkaz o výluke č. 1150/2026: úplná výluka oboch TK v úseku Štrba – Poprad-Tatry "
                                      "20.11.2026 od 8:00 do 14:00.\n")], received_at=utc(2026, 11, 3, 7, 55)))
    got = sorted((i.data["external_ref"], i.data["start"]) for i in items)
    assert got == [("1142/2026", iso(2026, 11, 14, 7)), ("1150/2026", iso(2026, 11, 20, 7))], got
    assert not any("Príloha opakuje" in n for i in items for n in i.notes)


# ====================================================================== termíny, ktoré sa nesmú stratiť (QC0)
def test_qc0_oznacene_riadky_html_tabulka_a_kolaj_polozky(cfg):
    """Formulár ROV s riadkami „Začiatok výluky:“ / „Koniec výluky:“ a tabuľka z HTML (prázdny riadok medzi riadkami
    tabuľky) dajú termín výluky; položka zoznamu „2) Výluka 2. TK …“ má koľaj 2 (nie „obe“ z prvej položky)."""
    (form,) = rules("Dobrý deň,\n\nRozkaz o výluke č. 1206/2026\nÚsek: Liptovský Mikuláš – Štrba\nKoľaj: 1. traťová "
                    "koľaj\nZačiatok výluky: 08.12.2026 07:00\nKoniec výluky: 08.12.2026 16:00\n",
                    subject="ROV č. 1206/2026 – výluka 1. TK Liptovský Mikuláš – Štrba", received=utc(2026, 11, 20, 8))
    assert (form.data["start"], form.data["end"], form.data["kind"]) == (iso(2026, 12, 8, 6), iso(2026, 12, 8, 15),
                                                                         "kolaj")
    (tab,) = rules("Dobrý deň,\n\noznamujeme výluku podľa ROV č. 2260/2026:\n\nÚsek\tKoľaj\tZačiatok\tKoniec\n\nŠaľa – Nové "
                   "Zámky\t2. TK\t09.12.2026 08:00\t09.12.2026 14:00\n\nVlaky po 1. TK.\n\nS pozdravom\nOR Trnava",
                   subject="ROV 2260/2026 – výluka 2. TK Šaľa – Nové Zámky", received=utc(2026, 11, 20, 8))
    assert (tab.data["from_station"], tab.data["start"], tab.data["end"]) == ("Šaľa", iso(2026, 12, 9, 7),
                                                                              iso(2026, 12, 9, 13))
    assert (tab.data["kind"], tab.data["track"], tab.data["external_ref"]) == ("kolaj", "2", "2260/2026")
    items = rules("Vážení dopravcovia,\n\npodľa ROV č. 1322/2026 budú v úseku Kysak – Košice vykonané tieto výluky:\n\n"
                  "1) Úplná výluka oboch traťových koľají v noci z 30.12. na 31.12. od 22:00 do 05:00 hod.\n"
                  "2) Výluka 2. TK dňa 4.1. od 08:00 do 12:00 hod., doprava po 1. TK.\n",
                  subject="ROV 1322/2026 – výluky Kysak – Košice počas sviatkov", received=utc(2026, 12, 15, 11))
    assert [(i.data["kind"], i.data["track"]) for i in items] == [("uplna", "obe"), ("kolaj", "2")], \
        [i.data for i in items]


def test_qc0_end_to_end_formular_rov_blokuje_vlak(env, session):
    """End to end: ROV s označenými riadkami začiatku a konca dostane záznam „navrh“ (nie výluku bez termínu) a vlak
    počas výluky nedostane VYPRAVIŤ ZELENÁ."""
    drop(env, "rov.eml", raw_mail("ROV c. 1207/2026 - vyluka Liptovsky Mikulas - Strba", "Rozkaz o výluke č. 1207/2026\n"
                                  "Úsek: Liptovský Mikuláš – Štrba\nDruh výluky: úplná výluka oboch traťových koľají\n"
                                  "Začiatok výluky: 08.12.2026 07:00\nKoniec výluky: 08.12.2026 16:00\n", "rov1207",
                                  sender=ROV_KE, date="Fri, 20 Nov 2026 09:00:00 +0100"))
    _process(session, utc(2026, 11, 20, 8, 5))
    (row,) = rows(session)
    assert (row.from_station, row.kind, row.status) == ("Liptovský Mikuláš", "uplna", "navrh")
    assert row.start_utc is not None and row.end_utc is not None
    r = _decide(session, "77590", "Liptovský Mikuláš", "Štrba", utc(2026, 12, 8, 9), utc(2026, 12, 8, 5))
    assert not (r.decision == "VYPRAVIŤ" and r.color == "ZELENÁ"), (r.decision, r.color, r.summary)


def test_qc4_end_to_end_ruseny_den_v_predmete_nezrusi_celu_vyluku(env, session):
    """End to end: zrušenie s rušeným dňom len v predmete nezruší celú výluku 23. – 27.11. (od 5. revízie sa výluka
    rozdelí – zruší sa len 25.11., ostatné dni ostávajú)."""
    from vlaky.models import Closure
    from vlaky.posta.spracovanie import confirm_event

    c = Closure(kind="uplna", from_station="Ružomberok", to_station="Liptovský Mikuláš", start_utc=utc(2026, 11, 23, 7),
                end_utc=utc(2026, 11, 27, 13), daily_from="08:00", daily_to="14:00", status="overene", source="email",
                external_ref="1315/2026")
    session.add(c)
    session.commit()
    drop(env, "z.eml", raw_mail("ROV c. 1315/2026 - zrusenie vyluky dna 25.11.2026", "Dobrý deň, oznamujeme zrušenie "
                                "výluky.\n", "z1315s", sender=ROV_ZA, date="Mon, 16 Nov 2026 10:00:00 +0100"))
    _process(session, utc(2026, 11, 16, 9, 5))
    (ev,) = events(session)
    ev = confirm_event(session, ev.id, "dispecer1")
    session.commit()
    session.refresh(c)
    assert ev.validation["apply_result"]["action"] == "rozdelene" and c.status == "overene"
    assert c.end_utc.replace(tzinfo=None) == utc(2026, 11, 24, 13).replace(tzinfo=None)
    assert _decide(session, "77531", "Ružomberok", "Liptovský Mikuláš", utc(2026, 11, 24, 9),
                   utc(2026, 11, 24, 5)).decision != "VYPRAVIŤ"


def test_qc0_html_tabulky_vyluk_a_pomalych_jazd(cfg):
    """Dve tabuľky z HTML (výluky a pomalé jazdy, medzi riadkami prázdne riadky): každý riadok je udalosť s termínom,
    riadok tabuľky pomalých jázd so stĺpcom „Rýchlosť“ je pomalá jazda (nie predpokladaná úplná výluka) a číslo
    súborného rozkazu (SROV) sa riadkom nepriradí ako číslo ich výluky."""
    from vlaky.posta.prilohy import html_to_text

    html = ("<p>Výluky:</p><table><tr><th>Úsek</th><th>Koľaj</th><th>Začiatok</th><th>Koniec</th></tr>"
            "<tr><td>Šaľa – Nové Zámky</td><td>2. TK</td><td>09.12.2026 08:00</td><td>09.12.2026 14:00</td></tr>"
            "<tr><td>Galanta – Šaľa</td><td>1. TK</td><td>10.12.2026 08:00</td><td>10.12.2026 14:00</td></tr></table>"
            "<p>Pomalé jazdy:</p><table><tr><th>Úsek</th><th>Rýchlosť</th><th>Začiatok</th><th>Koniec</th></tr>"
            "<tr><td>Kysak – Košice</td><td>50 km/h</td><td>11.12.2026 08:00</td><td>11.12.2026 14:00</td></tr>"
            "</table>")
    items = rules(html_to_text(html), subject="SROV č. 60/2026", received=utc(2026, 11, 20, 8))
    got = sorted((i.kind, i.data["kind"], i.data["from_station"], i.data["start"], i.data.get("speed_kmh"),
                  i.data.get("external_ref")) for i in items)
    assert got == [("pomala_jazda", "pomala_jazda", "Kysak", iso(2026, 12, 11, 7), 50, None),
                   ("vyluka_nova", "kolaj", "Galanta", iso(2026, 12, 10, 7), None, None),
                   ("vyluka_nova", "kolaj", "Šaľa", iso(2026, 12, 9, 7), None, None)], got


def test_qm5_obnovenie_nakladne_zatial_nie_a_bez_obmedzenia(cfg):
    """Obnovenie „pre osobné vlaky, nákladné zatiaľ nie“ či „nákladné vlaky až po 14:00“ nie je koniec mimoriadnosti
    (pre nákladné vlaky obmedzenie trvá); „nákladné vlaky nie sú obmedzené“ a „premávajú bez obmedzenia“ áno."""
    base = "Mimoriadnosť v ŽST Kraľovany: prevádzka je obnovená o 09:20"

    def one(tail: str):
        (it,) = rules(base + tail + "\n", subject="Mimoriadnosť Kraľovany", received=utc(2026, 11, 2, 8, 25))
        return it

    still = one(" pre osobné vlaky, nákladné zatiaľ nie.")
    assert still.kind == "mimoriadnost" and still.data["start"] == iso(2026, 11, 2, 8, 20) and still.data["end"] is None
    later = one(", nákladné vlaky až po 14:00.")
    assert (later.kind, later.data["end"]) == ("mimoriadnost", iso(2026, 11, 2, 13))
    assert one(", nákladné vlaky nie sú obmedzené.").kind == "ine"
    assert one(". Nákladné vlaky premávajú bez obmedzenia.").kind == "ine"


def test_qc0_odsek_s_viacerymi_usekmi_a_terminmi(cfg):
    """Odsek s viacerými úsekmi aj termínmi: „A – B a C – D dňa X a dňa Y“ – každý úsek v každom termíne (nie druhý
    termín potichu stratený); vety „úsek dňa …“ za sebou v jednom odseku – každý úsek so svojím termínom, aj keď sa
    úseky v odseku opakujú."""
    items = rules("Úplná výluka Kysak – Košice a Štrba – Poprad-Tatry dňa 01.12.2026 od 08:00 do 12:00 a dňa 03.12.2026 "
                  "od 08:00 do 12:00.\n", subject="ROV", received=utc(2026, 11, 16, 8))
    assert sorted((i.data["from_station"], i.data["start"]) for i in items) == [
        ("Kysak", iso(2026, 12, 1, 7)), ("Kysak", iso(2026, 12, 3, 7)), ("Štrba", iso(2026, 12, 1, 7)),
        ("Štrba", iso(2026, 12, 3, 7))]
    body = " ".join(f"Výluka 1. TK v úseku {a} dňa {d}.12.2026 od 08:00 do 14:00 hod." for d in (1, 2)
                    for a in ("Kysak – Košice", "Štrba – Poprad-Tatry", "Púchov – Považská Bystrica"))
    items = rules(body + "\n", subject="ROV", received=utc(2026, 11, 16, 8))
    got = sorted((i.data["from_station"], i.data["start"]) for i in items)
    assert got == sorted((a, iso(2026, 12, d, 7)) for d in (1, 2) for a in ("Kysak", "Štrba", "Púchov")), got
