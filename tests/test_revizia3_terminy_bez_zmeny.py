"""Tretia revízia – termíny z e-mailov (oblasť M1): poistky, že opravy MR0, MR1, MR3, MR4 a PR4 nezmenili správne
správanie (tieto testy prechádzajú aj pred opravou – stránia falošnému zrušeniu mimoriadnosti, strate termínu
z citácie či preposlania a posunu okien, ktoré boli správne).

Regresné testy samotných nálezov sú v ``test_revizia3_terminy.py``. Všetky adresy sú fiktívne (*.example).
"""
from __future__ import annotations

import pytest

from test_revizia3_terminy import SEC, SUBJ_RE, iso, rules, terms, utc


# ====================================================================== MR0 – okná, ktoré sa nemenia
@pytest.mark.parametrize("term,expected", [
    # večerné okno noci a okno cez polnoc začínajú prvým dňom noci
    ("v noci z 12. na 13.11.2026 od 21:00 do 23:30", [(iso(2026, 11, 12, 20), iso(2026, 11, 12, 22, 30))]),
    ("v noci z 12. na 13.11.2026 od 22:00 do 04:00", [(iso(2026, 11, 12, 21), iso(2026, 11, 13, 3))]),
    # noc s mesiacom slovom a oknom po polnoci
    ("v noci z 12. na 13. novembra 2026 od 0:30 do 4:30", [(iso(2026, 11, 12, 23, 30), iso(2026, 11, 13, 3, 30))]),
    # presun termínu bez označenia noci je nový deň
    ("sa presúva z 12. na 13.11.2026 od 0:30 do 4:30", [(iso(2026, 11, 12, 23, 30), iso(2026, 11, 13, 3, 30))]),
])
def test_mr0_okna_bez_zmeny(cfg, term, expected):
    """MR0 poistka: večerné okno, okno cez polnoc a presun termínu ostávajú, ako boli."""
    items = rules(f"{SEC} {term}.")
    assert terms(items) == expected
    assert all(i.confidence == 0.85 for i in items)


# ====================================================================== MR3 – dátum bez vlastného času
def test_mr3_datum_za_ciarkou_bez_casu_ostava_koncom_obdobia(cfg):
    """MR3 poistka: „od 12.11.2026 8:00 do 14:00, 13.11.2026“ (dátum za čiarkou nemá vlastný čas) ostáva jedným
    obdobím do 13.11. 14:00 (prísnejší prípad)."""
    (it,) = rules(f"{SEC} od 12.11.2026 8:00 do 14:00, 13.11.2026.")
    assert (it.data["start"], it.data["end"]) == (iso(2026, 11, 12, 7), iso(2026, 11, 13, 13))


# ====================================================================== PR4 – koniec bez zmeny
@pytest.mark.parametrize("text,end", [
    # predpokladaný neskorší koniec – prísnejší prípad
    ("TLG č. 45/2026: mimoriadna udalosť v ŽST Ilava, porucha výhybky, prevádzka obmedzená od 02.11.2026 04:30 do "
     "02.11.2026 13:00, predpokladané obnovenie prevádzky o 15:00.", iso(2026, 11, 2, 14)),
    # bez konca ostáva mimoriadnosť do odvolania
    ("Mimoriadnosť v žst. Ilava od 02.11.2026 04:30 do odvolania.", None),
])
def test_pr4_koniec_mimoriadnosti_bez_zmeny(cfg, text, end):
    """PR4 poistka: predpokladaný neskorší koniec platí (prísnejší) a „do odvolania“ ostáva bez konca."""
    (it,) = rules(text, subject="Mimoriadnosť Ilava")
    assert it.kind == "mimoriadnost" and it.data["start"] == iso(2026, 11, 2, 3, 30)
    assert it.data["end"] == end


# ====================================================================== MR4 – trvajúca mimoriadnosť
@pytest.mark.parametrize("text", [
    "Mimoriadna udalosť: v úseku Kraľovany – Ružomberok prerušená doprava od 02.11.2026 04:30, predpokladané obnovenie "
    "prevádzky o 12:00.",
    "Prevádzka v úseku Kraľovany – Ružomberok je prerušená od 02.11.2026 04:30, obnovená bude o 12:00.",
    "Trať Kraľovany – Ružomberok je prejazdná len po 1. koľaji, porucha trvá od 02.11.2026 04:30.",
    "Mimoriadnosť v ŽST Kraľovany: 1. koľaj je zjazdná, 2. koľaj zostáva neprejazdná od 02.11.2026 05:00.",
    "Trať Kraľovany – Ružomberok ešte nie je zjazdná, porucha od 02.11.2026 04:30.",
    "Porucha trakčného vedenia: úsek Kraľovany – Ružomberok je prejazdný pre motorové vlaky od 02.11.2026 05:00.",
    "Mimoriadnosť Kraľovany – Ružomberok: koľaj č. 2 je uvoľnená, trakčné vedenie je vypnuté od 02.11.2026 05:00.",
    "Mimoriadnosť Kraľovany – Ružomberok: doprava je obnovená, nákladné vlaky sú odklonené od 02.11.2026 05:00.",
])
def test_mr4_trvajuca_mimoriadnost_ostava_mimoriadnostou(cfg, text):
    """MR4 poistka proti falošnému zrušeniu: mimoriadnosť, ktorá ešte trvá (predpokladané či budúce obnovenie,
    prejazdná len jedna koľaj alebo len pre motorové vlaky, vypnuté trakčné vedenie, odklon, „nie je zjazdná“),
    ostáva mimoriadnosťou (prísnejší prípad)."""
    items = rules(text, subject="Mimoriadnosť", received=utc(2026, 11, 2, 7, 30))
    assert [i.kind for i in items] == ["mimoriadnost"]


# ====================================================================== MR1 – citácia či preposlanie ako zdroj termínu
def test_mr1_citacia_bez_noveho_terminu_ostava_zdrojom_terminu(cfg):
    """MR1 poistka: odpoveď bez vlastného dátumu („Potvrdzujeme.“) – termín zostáva z citovaného ROV (inak by sa
    nič nenašlo)."""
    body = ("Potvrdzujeme.\n\nDňa 2.11.2026 o 9:00 ŽSR OR Poprad napísal(a):\n"
            "> Rozkaz o výluke č. 1142/2026: výluka 1. TK v úseku Poprad-Tatry – Liptovský Mikuláš 12.11.2026\n"
            "> od 8:00 do 14:00.\n")
    (it,) = rules(body, subject=SUBJ_RE)
    assert (it.data["start"], it.data["end"]) == (iso(2026, 11, 12, 7), iso(2026, 11, 12, 13))


def test_mr1_preposlana_sprava_nie_je_citacia(cfg):
    """MR1 poistka: pri preposlaní („FW:“) je obsah pod oddeľovačom preposlanou správou – termín z nej sa číta
    ďalej, aj keď nad ňou je dátum."""
    body = ("Na vedomie, prijaté 2.11.2026.\n\n-----Original Message-----\n"
            "From: ŽSR OR Poprad <vyluky.orpp@zsr.example>\nSent: Monday, November 2, 2026 9:00 AM\n"
            "Subject: ROV c. 1142/2026\n\nRozkaz o výluke č. 1142/2026: výluka 1. TK v úseku Poprad-Tatry – "
            "Liptovský Mikuláš 20.11.2026 od 8:00 do 14:00.\n")
    (it,) = rules(body, subject="FW: ROV c. 1142/2026 Poprad-Tatry - Liptovsky Mikulas")
    assert (it.data["start"], it.data["end"]) == (iso(2026, 11, 20, 7), iso(2026, 11, 20, 13))


# ====================================================================== oprava po kontrole – správanie bez zmeny
def test_pomala_jazda_ktora_sa_zrusi_po_oprave_je_pomala_jazda(cfg):
    """Poistka: „zavádza sa pomalá jazda …, ktorá sa zruší po oprave“ (budúce zrušenie) je nová pomalá jazda."""
    items = rules("Pre poruchu koľaje sa od 03.11.2026 12:00 zavádza pomalá jazda 30 km/h v úseku Kysak – Košice, ktorá "
                  "sa zruší po oprave.", subject="TLG c. 46/2026")
    assert [i.kind for i in items] == ["pomala_jazda"] and items[0].data["speed_kmh"] == 30


def test_zrusenie_vyluky_podla_cisla_v_tele(cfg):
    """Poistka: „Výluka podľa ROV č. 1142/2026 sa ruší. Oznámené TLG č. 50/2026.“ – zrušenie ROV č. 1142/2026."""
    (it,) = rules("Výluka podľa ROV č. 1142/2026 sa ruší. Oznámené TLG č. 50/2026.", subject="Zrusenie vyluky")
    assert it.kind == "vyluka_zrusenie" and it.data["external_ref"] == "1142/2026"


def test_rocny_zoznam_v_tom_istom_roku_ostava_chronologicky(cfg):
    """MR6 poistka: ročný plán „10.1. a 28.12.2026“ prijatý v decembri 2025 – 10.1.2026 (nie 2027), bez upozornenia."""
    items = rules(f"{SEC} 10.1. a 28.12.2026 od 8:00 do 14:00.", received=utc(2025, 12, 15, 9))
    assert terms(items) == [(iso(2026, 1, 10, 7), iso(2026, 1, 10, 13)), (iso(2026, 12, 28, 7), iso(2026, 12, 28, 13))]
    assert all(i.confidence == 0.85 for i in items)
