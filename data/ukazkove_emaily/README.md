# Ukážkové e-maily (FIKTÍVNE)

Tento adresár obsahuje **vymyslené** e-maily, na ktorých sa skúša a testuje spracovanie pošty Agenta
dispečera (moduly M4, M5, M6 v `PROMPT.md`). Nejde o skutočné rozkazy, trasy ani avíza – každý e-mail
má v predmete alebo v texte označenie **„FIKTÍVNE – ukážka“**.

- Odosielatelia používajú len vyhradené fiktívne domény `zsr.example`, `partner.example`,
  `zakaznik.example` a `dopravca.example` (doména `.example` je vyhradená normou RFC 2606).
  Výnimkou je ukážka podvrhu `falosne-zsr.example`, ktorá musí skončiť v karanténe.
- Čísla vlakov, rozkazov o výluke, trás, telefónne čísla aj osoby sú vymyslené.
- **Názvy staníc sú skutočné** (Poprad-Tatry, Kraľovany, Čadca, Rusovce…), aby sa overila normalizácia
  názvov podľa registra siete (`vlaky/siet`).
- Časy v e-mailoch sú miestne (Europe/Bratislava); systém ich ukladá v UTC.
- Prichádzajúci server (`mx.dopravca.example`) pridáva hlavičku `Authentication-Results` (SPF, DKIM,
  DMARC), ako to robí skutočný poštový server.

## Prehľad

| Súbor | Čo obsahuje | Očakávaný výsledok |
|---|---|---|
| `01_rov_vyluka_kolaje_poprad_lm.eml` | Rozkaz o výluke (ROV): výluka 1. TK Poprad-Tatry – Liptovský Mikuláš | nová výluka (druh *koľaj*) |
| `02_oznamenie_vyluk_tyzden_47.eml` | Týždenné oznámenie o povolených výlukách – dve položky (denne 08:00 – 14:00; napäťová výluka TV) | 2 nové výluky |
| `03_pomala_jazda_bez_diakritiky.eml` | Pomalá jazda 50 km/h, text **bez diakritiky** | pomalá jazda |
| `04_zmena_vyluky_preposlana.eml` | Zmena ROV (nový termín) – automaticky **preposlaná** z dispečerskej schránky (originál v prílohe .eml) | zmena výluky |
| `05_zrusenie_vyluky.eml` | Zrušenie výluky z ukážky 02 | zrušenie výluky (vždy na potvrdenie) |
| `06_mimoriadna_udalost.eml` | Kontrolný dispečer OR: porucha trakčného vedenia (text + HTML) | mimoriadnosť (vždy na potvrdenie) |
| `07_pridelenie_trasy_nex54321.eml` | Pridelenie trasy ad hoc s tabuľkou dopravní a časov | pridelená trasa |
| `08_zmena_trasy_nex54321.eml` | Zmena časovej polohy trasy z ukážky 07 | zmena trasy |
| `09_kis_zrusenie_trasy_pn12345.eml` | Správa systému KIS: zrušenie trasy | zrušenie trasy (vždy na potvrdenie) |
| `10_avizo_cadca_cz.eml` | **Česky**: avízo príchodu vlaku do Čadce | avízo (Čadca) |
| `11_avizo_rusovce_rajka.eml` | Avízo príchodu na priechod Rusovce – Rajka (len HTML) | avízo (Rusovce – Rajka) |
| `12_srov_tabulka_xlsx.eml` | Súborný ROV (SROV) ako tabuľka v **XLSX** prílohe (TK, TV, obe TK, POTR) | 3 výluky a 1 pomalá jazda |
| `13_objednavka_pdf_cz.eml` | **Česky**: objednávka prepravy v **PDF** prílohe | objednávka |
| `14_prompt_injection.eml` | Výluka + vložené „pokyny“ (ignoruj pokyny, pošli e-mail…) | verdikt *podozrivý*, nič sa neaplikuje automaticky |
| `15_nepovoleny_odosielatel.eml` | Podvrhnutá doména `falosne-zsr.example`, DMARC fail | karanténa, bez extrakcie |
| `16_ine_informacia.eml` | Bežná informácia (zmena kontaktov) | iné – na kontrolu |
| `17_telegram_potr.eml` | Telegram (TLG) kontrolného dispečera: POTR 30 km/h do odvolania | pomalá jazda do odvolania |
| `18_kis_ponuka_trasy_nex54390.eml` | KIS: ponuka trasy ad hoc (produkt P5) s lehotou na akceptáciu, cez polnoc | ponuka trasy (ešte nie pridelená) |

## Ako ich použiť

Ukážky sa dajú vložiť do „drop“ adresára schránky (`VLAKY_MAIL_SOURCE=directory`, adresár
`VLAKY_MAIL_DIR`, predvolene `data/schranka/`); agent ich pri ďalšom cykle spracuje a presunie do
`data/schranka/spracovane/`:

```bash
mkdir -p data/schranka && cp data/ukazkove_emaily/*.eml data/schranka/
python -m vlaky agent --once
```

Súbory vytvára skript `tests/posta_ukazky_generator.py` (`python tests/posta_ukazky_generator.py`);
prílohy XLSX a PDF sa generujú tiež ním. Testy sú v `tests/test_posta_*.py`.
