# Používateľská príručka – Agent dispečer

Pre dispečerov, vedúceho dispečingu a plánovačov. Inštalácia a prevádzka servera sú v `docs/PREVADZKA.md`.

## 1. Ako to funguje v skratke

- **Agent dispečer beží nepretržite na serveri** a číta svoju e-mailovú schránku. Prevádzkové e-maily (výluky, trasy, avíza, objednávky) dostávate aj vy ako doteraz. Agent ich číta navyše.
- **Z e-mailov vytiahne udalosti, prepočíta vlaky a pošle vám odporúčanie:** e-mailom, pri naliehavých veciach aj cez Teams alebo SMS.
- **Agent nič nerobí navonok.** Neobjednáva trasy, nepíše ŽSR ani zákazníkom, nedáva pokyny rušňovodičom. Rozhodujete a konáte vy. Agent pripraví podklady a koncepty správ.
- **Semafor:**
  - **ZELENÁ** – vypraviť je bezpečné s rezervou,
  - **ORANŽOVÁ** – len s vaším potvrdením (dôvody sú vypísané),
  - **ČERVENÁ** – nevypravovať,
  - sivý vlak – chýbajú čerstvé dáta o polohe.

## 2. Prihlásenie a roly

Prihlasujete sa menom a heslom (prihlásenie platí 12 h). Po 5 neúspešných pokusoch z jedného počítača (alebo 10 pokusoch na jeden účet) sa prihlásenie na 15 min zablokuje; pri IPv6 sa pokusy počítajú pre celú sieť /64. **Odhlásiť** ukončí všetky vaše prihlásenia (aj na iných zariadeniach).

Server overuje najviac 3 heslá naraz. Pri návale prihlásení môže ukázať „Server je zaneprázdnený, skúste to o chvíľu.“ – stačí to o pár sekúnd skúsiť znova. Prihlásenie s prázdnym menom alebo heslom sa odmietne hláškou „Zadajte meno aj heslo.“

| Rola | Čo môže |
|---|---|
| dispečer | Všetko prevádzkové: vlaky, výluky, fronta, odporúčania, hranice, rušňovodiči |
| vedúci dispečingu | To isté a k tomu nastavenia (rezervy, náklady), používatelia, audit, údaje o hraniciach, kalibrácia |
| plánovač | Rušňovodiči, zmeny, rušne |
| obchod | Len čítanie |
| admin | Používatelia, audit |

## 3. Nástenka – živá mapa Slovenska

- **Mapa:** všetky trate ŽSR (farba podľa elektrifikácie), 23 hraničných priechodov s preberacou stanicou (PPS), výluky (červené) a pomalé jazdy (oranžové) s časovým posuvníkom (teraz, +6, +24, +48 h).
- **Vlak** je ikona s číslom priamo na trati, farba podľa semaforu. Vlaky idúce k hranici čakajú pri svojej preberacej stanici.
- **Kliknutím na vlak** uvidíte zákazníka, reláciu, rušeň, rušňovodiča a zostávajúci pracovný čas do 13 h, meškanie, príchod (bežne a v horšom prípade), najbližšie riziko a odkiaľ je posledná poloha a aká je stará.
- **Sledovať vlak:** mapa ide s vlakom. **Stopa 24 h:** kade vlak išiel.
- **Tabuľka vpravo** je zoradená podľa naliehavosti, navrchu ČERVENÉ. Pod ňou sú upozornenia a časová os (vlaky a výluky na 24 h).
- **Na veľký monitor:** celá obrazovka, nočný režim.
- **Bez čerstvých údajov agenta** (schránka sa 30 min úspešne neskontrolovala alebo agent neprebehol 2 × `VLAKY_REEVALUATE_MINUTES`) mapa, tabuľka, zoznam vlakov, detail vlaku ani `/api/vlaky` neukážu ZELENÚ: posledné zelené odporúčanie sa zobrazí ako ORANŽOVÁ s poznámkou „bez čerstvých údajov agenta – pôvodne ZELENÁ“ a s vekom údajov (v API polia `downgraded`, `data_age`, `data_note`). Uložené odporúčanie sa nemení.

## 4. Fronta na potvrdenie

Agent potrebuje vaše potvrdenie, keď si nie je istý, keď zmena ovplyvní vlak v najbližších 72 h alebo keď ide o zrušenie (výluky, trasy) či mimoriadnosť.

1. Vľavo vidíte **pôvodný e-mail**, vpravo **čo z neho agent vytiahol**: úsek, časy, druh výluky, vlak.
2. **Potvrdiť** – údaje sú správne. Agent ich zapíše a prepočíta vlaky.
3. **Upraviť a potvrdiť** – opravte pole (napr. čas alebo stanicu) a potvrďte. Oprava sa uloží, aby sa agent zlepšoval.
4. **Zamietnuť** – s dôvodom. Agent vráti späť, čo už zapísal.

E-maily od nepovolených odosielateľov sú v **karanténe**: nespracujú sa a uvidíte ich v súhrne. **Podozrivé e-maily** (napríklad text typu „ignoruj pokyny“) sa nikdy nepoužijú automaticky.

## 5. Odporúčania

Každé odporúčanie obsahuje:

- čo sa zmenilo a čoho sa to týka,
- **rozhodnutie** (VYPRAVIŤ, POČKAŤ, ODSTAVIŤ, PRESMEROVAŤ, ESKALOVAŤ, pri idúcom vlaku POKRAČOVAŤ) s časom,
- **návrh trasy:** dopravne s časmi príchodu a odchodu a okno na objednanie ad hoc trasy,
- **okná bezpečného odchodu**, napr. „vypraviť najneskôr o 05:40, inak až od 12:30“,
- **rušňovodiča:** nástup, limit 13 h, „vystriedať najneskôr o …“, prípadne návrh posunúť nástup,
- **porovnanie variantov** a dôvody,
- **koncepty správ:** objednávka ad hoc trasy pre ŽSR (IS KIS, produkt P5) a informácia pre zákazníka. Skopírujte ich, doplňte a odošlite sami,
- **kontrolný zoznam pred odchodom:** súpis vlaku a správa o zostave v KIS, pripravenosť vlaku, skúška bŕzd, doklady.

**Čo s odporúčaním urobíte:** **Prijať**, **Upraviť** (s poznámkou) alebo **Zamietnuť** (s dôvodom). Dá sa to aj **odpoveďou na e-mail agenta**: prvý riadok `PRIJAŤ` alebo `ZAMIETNUŤ dôvod`. Platí to len z vašej firemnej adresy, ktorá je na zozname dispečerov.

**Dôležité pravidlá ŽSR, s ktorými agent počíta:**

- ad hoc trasu (P5) objednať aspoň **6 h** pred odchodom; neskôr len výnimočne cez dispečera ŽSR,
- vlak, ktorý neodíde do **18 h**, ŽSR zruší – agent ráta 18 h od začiatku státia (nie od posledného prepočtu) a neskorší odchod už neponúkne,
- odstavenie je najviac 18 h a koľaj určí ŽSR; odstavenie idúceho vlaku znamená novú trasu (aspoň 6 h vopred),
- preberanie na hranici sa plánuje s dlhšou dobou preberania (P90), aby plán vyšiel aj pri pomalšom preberaní.

**Kedy agent nedá ZELENÚ:** výluka na trase ešte nie je potvrdená (počíta sa s ňou), výluku sa nepodarilo umiestniť na sieť (skontrolujte názvy dopravní vo výluke), chýbajú čerstvé údaje o polohe alebo je poloha mimo trasy, pri idúcom vlaku chýba záznam o nástupe rušňovodiča, alebo agent dlhšie ako 30 min úspešne neskontroloval svoju schránku. Dôvod je vždy vypísaný v odporúčaní.

**Keď sa výpočet nepodarí:** vlak dostane odporúčanie **ESKALOVAŤ** s popisom chyby – staré odporúčanie vtedy neplatí, posúďte vlak sami a chybu nahláste správcovi.

## 6. Výluky

- Väčšinu výluk zapíše agent z e-mailov (ROV, SROV, „Povolené výluky“, telegramy, POTR).
- **Čo príde telefonicky alebo cez WhatsApp od kontrolného dispečera ŽSR, musíte zadať sami:** stránka **Výluky → Nová výluka** (úsek od – do, druh, čas, opakovanie, len trakčné vedenie, rýchlosť). Prípadne to prepošlite e-mailom do schránky agenta.
- Výluka „NEPOTVRDENÁ“ sa do výpočtov počíta, aby bol výpočet na strane bezpečnosti. Potvrďte ju, keď ju overíte.

## 7. Vlaky a objednávky

- **Objednávky** zákazníkov prichádzajú e-mailom. Agent k nim pošle **návrh prepravy**: trasy, časy, okno na objednanie trasy.
- **Vlak z objednávky** vytvoríte jedným tlačidlom, alebo vlak zadáte ručne: odkiaľ, kam, vstupný hraničný priechod, dĺžka, hmotnosť, trakcia, termín, pripravenosť (miestny čas).
- **Prepočítať** spustí okamžité vyhodnotenie vlaku.
- **Odkaz pre rušňovodiča** vytvorí odkaz do mobilu, cez ktorý rušňovodič hlási nástup, pripravenosť, odchod a problém a posiela polohu. Odkazy sa dajú zrušiť. Nástup sa dá z odkazu zaznamenať len raz (začiatok bežiacej zmeny môže zmeniť iba dispečer). Hlásenia stavu (nástup, pripravený, odchod) a hlásenia o probléme majú každé vlastný limit 6 za 10 min – opakované ani odmietnuté hlásenia stavu limit pre problém neminú. Upozornenie na problém príde najviac raz za 10 min (ďalšie hlásenia sú v detaile vlaku a v audite).
  - Po každom hlásení stavu sa vlak hneď prepočíta (po hlásení o probléme najviac raz za 30 s). Už zaznamenaný stav aplikácia ukáže ako „Už zaznamenané: …“, nie ako chybu.
  - Hlásenie „pripravený“ od stojaceho vlaku nezmení, odkedy vlak stojí – 18 h limit státia beží ďalej od skutočného začiatku státia.
  - Odmietnuté požiadavky z odkazu (prekročený limit, nereálny skok polohy, opakovaný nástup) sa do auditu zapíšu najviac raz za 10 min na vlak a druh, s počtom nezapísaných („nezapisane“). Detail vlaku vždy ukáže posledných 10 hlásení o probléme a posledné odmietnutie každého druhu z posledných 7 dní, aj keď ich z 30 posledných záznamov vytlačili iné.
  - **Poloha z mobilu:** ak hodiny mobilu nesedia s časom servera o viac ako 30 s, aplikácia čas polohy prepočíta podľa servera a ukáže „Hodiny mobilu sa líšia od servera o N min – čas polohy sa opravuje.“ (odporučte zapnúť automatický dátum a čas). Keď GPS nedáva novú polohu (tunel, slabý signál), aplikácia starú polohu znova neposiela a ukáže „Čakám na novú polohu z GPS (posledná zistená o …)“. Polohu s časom v budúcnosti server uloží s časom prijatia; polohu staršiu ako posledná uložená neuloží („Poloha je staršia ako posledná uložená.“) – stopa sa spätne neprepisuje.

## 8. Hraničné priechody a preberanie

- 23 priechodov s preberacou stanicou (napr. Rusovce – **Rajka** (HU), Čadca, Kúty, Štúrovo, Čierna nad Tisou).
- **Avízo** príchodu vlaku na hranicu (z e-mailu partnera alebo ručne): agent z neho prepočíta pripravenosť vlaku (príchod + doba preberania).
- **Tlačidlá:** **Prišiel na hranicu**, **Prevzatý** (čas a zistenia). Z týchto časov sa neskôr spresnia doby preberania.

## 9. Rušňovodiči (limit 13 h)

- Pri každom rušňovodičovi beží **odpočet do 13 h** (od nástupu po odovzdanie rušňa). Farby: zelená, oranžová (menej ako 60 min rezervy) a červená.
- **Upozornenia** prídu 90, 60 a 30 min pred limitom a pri prekročení.
- **Navrhnúť nasadenie (24 h):** agent navrhne, ktorý rušňovodič môže viesť ktorý vlak (13 h, odpočinok 11 h, znalosť trate, čo najkratšie presuny). Je to len návrh.

## 10. Reporty a nastavenia

- **Reporty:**
  - odporúčania a vaše reakcie (koľko ste prijali a prečo ste zamietli),
  - e-maily a udalosti,
  - doby preberania na hraniciach,
  - export CSV.
  - V **tieňovej prevádzke** tu vedúci vyhodnocuje, či sa agentovi dá veriť.
- **Kalibrácia z histórie (vedúci):** návrh nových dôb preberania a zdržaní z vašich dát. Použije sa až po schválení.
- **Nastavenia (vedúci):** rezervy (pred výlukou, po výluke, termín, rušňovodič), prahy semaforu a **náklady**. Vzorové sadzby **nahraďte skutočnými**, inak je porovnanie variantov len orientačné. Každá zmena je v audite.

## 11. Odovzdávka služby

Na konci služby (podľa nastavenia o 6:00 a 18:00) agent pošle a uloží prehľad:

- čo ide,
- čo hrozí,
- rušňovodiči blízko limitu,
- nové výluky,
- nepotvrdené e-maily.

Doplňte otvorené úlohy a potvrďte.

## 12. Keď agent nebeží

- **Upozorní vás nezávislý monitoring** (ak agent prestane posielať „heartbeat“) a denná správa o stave chýba.
- **Web neukáže ZELENÚ bez čerstvých údajov** – posledné zelené odporúčania sa zobrazia ako ORANŽOVÉ s poznámkou „bez čerstvých údajov agenta – pôvodne ZELENÁ“ (pozri kapitolu 3).
- **Pracujete ako doteraz** – prevádzkové e-maily máte vo svojej schránke.
- **Nahláste to správcovi servera.** Postup obnovy je v `docs/PREVADZKA.md`.
