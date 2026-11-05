# VLAKY – návrh dispečerského systému pre nákladné vlaky na sieti ŽSR

Návrh **Agenta dispečera** pre nákladného železničného dopravcu: samostatného agenta, ktorý beží nepretržite na vlastnom serveri (VPS), má **vlastnú e-mailovú adresu** na prevádzkové zmeny, sám plánuje dopravu a **skutočnému dispečerovi iba odporúča**, ako navrhnúť trasu a čo urobiť. Agent spolu s webovou aplikáciou má:

- na **živej mape Slovenska** ukazovať, kde presne sú vlaky na trati,
- sledovať **preberanie vlakov na vstupných hraničných priechodoch SR** (Kúty, Rajka, Štúrovo, Čadca, Čierna nad Tisou a ďalšie),
- priebežne čítať e-maily (výluky, trasy, zmeny, avíza) a viesť z nich register,
- strážiť **pracovný čas rušňovodiča (najviac 13 h)** a včas navrhnúť výmenu,
- navrhovať najvhodnejší spôsob prepravy z A do B s ohľadom na výluky a meškania,
- vyhodnocovať, či vlak stihne termín,
- odporúčať, kedy vlak **vypraviť**, kedy **počkať**, **odstaviť** alebo **presmerovať**.

Celý popis toho, čo bude softvér robiť (bez technických detailov), je v [`docs/00_CO_BUDE_SOFTVER_ROBIT.md`](docs/00_CO_BUDE_SOFTVER_ROBIT.md).

Repozitár obsahuje **zadanie (prompt), podklady na zavedenie a hotový systém postavený podľa promptu**: agenta, webovú aplikáciu so živou mapou SR, nasadenie na VPS a testy. Na ostrú prevádzku treba doplniť prístupy (schránka agenta, server, prístupy k ŽSR) – pozri `docs/01_CO_POTREBUJEM.md`.

## Obsah

| Súbor | Na čo slúži |
|---|---|
| [`docs/00_CO_BUDE_SOFTVER_ROBIT.md`](docs/00_CO_BUDE_SOFTVER_ROBIT.md) | **Čo bude softvér robiť** – celý príbeh vlaku od objednávky po doručenie, zrozumiteľne. |
| [`PROMPT.md`](PROMPT.md) | **Hlavný prompt** pre AI nástroj na vývoj softvéru alebo pre dodávateľa: kompletné zadanie aplikácie. |
| [`PROMPT_ASISTENT.md`](PROMPT_ASISTENT.md) | Kratší prompt pre AI asistenta na každodennú prácu hneď teraz (e-maily → výluky → semafor vlakov). |
| [`docs/01_CO_POTREBUJEM.md`](docs/01_CO_POTREBUJEM.md) | Kontrolný zoznam podkladov, prístupov a rozhodnutí. |
| [`docs/02_ZAVEDENIE_SYSTEMU.md`](docs/02_ZAVEDENIE_SYSTEMU.md) | Čo treba na zavedenie: mapa siete, výluky, poloha vlakov (GPS), e-mail, IT, právo, fázy, riziká. |
| [`docs/03_REZERVY_A_ROZHODOVANIE.md`](docs/03_REZERVY_A_ROZHODOVANIE.md) | Bezpečné časové rezervy, semafor a pravidlá vypraviť / počkať / odstaviť / presmerovať, s príkladom. |
| [`docs/04_ALGORITMY.md`](docs/04_ALGORITMY.md) | Aké algoritmy použiť a prečo, vrátane otázky „nových algoritmov od OpenAI“. |
| [`docs/05_ZDROJE.md`](docs/05_ZDROJE.md) | Zdroje a odkazy (ŽSR, EÚ predpisy, dáta, technológie). |
| [`docs/PREVADZKA.md`](docs/PREVADZKA.md) | **Prevádzka na VPS** – inštalácia, schránka agenta, zálohy a test obnovy, monitoring, postup pri incidentoch. |
| [`docs/ARCHITEKTURA.md`](docs/ARCHITEKTURA.md) | Architektúra hotového systému: komponenty, dátový tok, rozhodnutia. |
| [`docs/PREDPOKLADY.md`](docs/PREDPOKLADY.md) | Predpoklady použité pri stavbe (odpovede na úvodné otázky promptu). |
| [`docs/POUZIVATELSKA_PRIRUCKA.md`](docs/POUZIVATELSKA_PRIRUCKA.md) | **Príručka pre dispečera** – mapa, fronta, odporúčania, výluky, hranice, 13 h, reporty. |
| [`vlaky/`](vlaky/) | **Hotový systém** (Python): agent, plánovanie, e-maily, notifikácie, web so živou mapou. |
| [`referencia/`](referencia/) | Spustiteľná referenčná implementácia rozhodovania a jej testy (Python, bez externých knižníc). |

## Ako postupovať

1. **Podklady:** prejdi [`docs/01_CO_POTREBUJEM.md`](docs/01_CO_POTREBUJEM.md) a priprav, čo sa dá (najdôležitejšie sú vzorové e-maily).
2. **Vývoj:** vlož [`PROMPT.md`](PROMPT.md) do AI nástroja na vývoj (napr. Claude Code) alebo ho odovzdaj IT či dodávateľovi. Postupuje sa po fázach F0 – F4.
3. **Hneď teraz:** na dennú prácu použi [`PROMPT_ASISTENT.md`](PROMPT_ASISTENT.md) v schválenom AI nástroji.
4. **Pravidlá rozhodovania:** pozri si príklad v [`docs/03_REZERVY_A_ROZHODOVANIE.md`](docs/03_REZERVY_A_ROZHODOVANIE.md) a odsúhlas alebo uprav predvolené rezervy.

## Spustenie systému

**Na skúšku na počítači (Python 3.12+), s fiktívnymi ukážkovými dátami:**

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```

```bash
cp .env.demo .env
```

```bash
python -m vlaky demo --reset
```

```bash
python -m vlaky agent --once
```

```bash
python -m vlaky web --port 8000
```

Potom otvor http://127.0.0.1:8000 a prihlás sa ako `dispecer` s heslom `ukazka-heslo-2026` (nastavené v `.env.demo`). Agent spracuje ukážkové e-maily zo schránky `data/schranka` a správy pre dispečera uloží do `data/odoslane`. Polohu vlakov v jazde posúva `python -m vlaky simulate`.

**Na serveri (VPS):** Docker Compose s PostgreSQL, webom, agentom a Caddy (HTTPS) – postup krok za krokom je v [`docs/PREVADZKA.md`](docs/PREVADZKA.md).

**Testy celého systému:**

```bash
python -m pytest -q
```

Rovnaké testy proti PostgreSQL (ako na serveri) – do prázdnej testovacej databázy, ktorej tabuľky sa pred každým testom zmažú a vytvoria znova:

```bash
VLAKY_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5432/vlaky_test python -m pytest -q
```

Formulácie e-mailov ŽSR overuje korpus `tests/data/posta_korpus` (test `tests/test_revizia5_korpus.py`) – každú novú skutočnú formuláciu z prevádzky doplň doň (postup v [`docs/ARCHITEKTURA.md`](docs/ARCHITEKTURA.md), bod 4 – „Korpus formulácií e-mailov ŽSR“).

Testy v skutočnom prehliadači (Playwright + Chromium cez Node.js) sa preskočia, ak `node` s balíkom `playwright` chýba (alebo pri `VLAKY_SKIP_BROWSER=1`). Nesťahujú dlaždice podkladovej mapy a všetky požiadavky mimo lokálneho servera blokujú – výnimkou je knižnica MapLibre z unpkg.com (raz za beh testu); bez prístupu k nej sa testy mapy preskočia.

## Spustenie referenčnej implementácie

Fiktívny príklad (vlak Košice → Žilina s výlukou):

```bash
python3 referencia/rozhodovanie.py
```

Testy (kontrolujú aj to, že parametre v `PROMPT.md` sedia s referenciou):

```bash
python3 -m unittest discover -s referencia -v
```

## Dôležité upozornenie

Aplikácia aj prompty slúžia na **podporu rozhodovania dispečera dopravcu**. Neriadia železničnú dopravu a nenahrádzajú pokyny ŽSR, predpisy ani rozhodnutie dispečera. Všetky číselné hodnoty (rezervy, náklady) sú návrh na začiatok a treba ich nastaviť podľa skutočných dát.
