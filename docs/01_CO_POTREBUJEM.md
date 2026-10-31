# Čo budem od teba potrebovať

Zoznam podkladov, prístupov a rozhodnutí na zavedenie systému. Je zoradený podľa toho, kedy budú potrebné. Čím skôr budú body z časti A, tým skôr môže začať vývoj (fázy F0 – F1 v `PROMPT.md`).

## A. Hneď na začiatok (fázy F0 – F1)

- [ ] **Firma a rozsah.** Presný názov dopravcu (predpokladám ČD Cargo, a.s., alebo dcérsku ČD Cargo Slovakia, s.r.o. – oprav ma). Ďalej hlavné relácie (top 10), počet vlakov za týždeň, hraničné priechody a vlečky zákazníkov.
- [ ] **Vzorové e-maily (najdôležitejší podklad).** 30 – 50 kusov z každého typu:
  - výluky (nové, zmenené, zrušené), pomalé jazdy, mimoriadnosti na trati,
  - pridelenie, zmena a zrušenie trasy,
  - avíza partnerov o príchode vlakov na hranicu,
  - objednávky a zmeny od zákazníkov.

  Všetko aj s prílohami (PDF, Excel…), anonymizované (mená a telefóny môžu byť začiernené). Ideálne aj s tým, ako si to dispečer zapísal: to bude „správna odpoveď“ na testovanie presnosti AI.
- [ ] **E-mail technicky.** Outlook / Microsoft 365, Gmail alebo iný systém? Existuje zdieľaná dispečerská schránka? Kontakt na IT, ktoré schváli prístup aplikácie k schránke (stačí len čítanie).
- [ ] **Dokumenty a prístupy k ŽSR.** Čo z toho máte a používate:
  - podmienky používania železničnej infraštruktúry ŽSR (verejný dokument),
  - Tabuľky traťových pomerov (TTP) pre vaše trate,
  - plán výluk a spôsob, akým vám chodia oznámenia o výlukách,
  - prístupy do informačných systémov ŽSR pre dopravcov (ktoré a s akými rolami),
  - postup a formulár objednávky ad hoc trasy,
  - prístup do RNE TIS alebo iných systémov s údajmi o jazde vlakov.
- [ ] **Rušne.** Zoznam s typom, vlastníkom (vlastný alebo prenajatý – od koho), trakciou (elektrická AC/DC/viacsystémová, motorová) a informáciou, či už majú telematiku alebo GPS (aj od výrobcu či prenajímateľa).
- [ ] **Hraničné priechody a preberanie.** Na ktorých priechodoch preberáte a odovzdávate vlaky (Kúty, Rajka, Štúrovo, Čadca, Čierna nad Tisou, ďalšie?), od ktorých partnerov a ako chodia avíza o príchode vlaku na hranicu (e-mail, systém, telefón). Ako dlho preberanie zvyčajne trvá a čo pri ňom robíte (prehliadka, doklady, skúška bŕzd, výmena rušňa).
- [ ] **Rušňovodiči a pracovný čas.** Potvrdenie limitu **13 h** a čo presne sa doňho ráta (príprava, jazda, čakanie, presun na hranicu, odovzdanie rušňa). Ďalšie pravidlá (prestávky, odpočinok medzi zmenami, nočné zmeny), plán služieb a odkiaľ sa dá získať, miesta výmeny rušňovodičov, znalosť tratí (kto smie kam), cezhraničné oprávnenia.
- [ ] **Náklady a zmluvy.** Približné hodinové náklady (rušeň, čata, vozne), zmluvné pokuty a SLA zákazníkov, poplatky ŽSR, ktoré reálne platíte (zrušenie alebo zmena trasy, odstavenie). Bez nich bude porovnanie variantov len orientačné.
- [ ] **Rozhodnutia o pravidlách.** Súhlasíš s predvolenými rezervami (pred výlukou 30 min, po výluke 30 min, termín 60 min, rezerva do limitu 13 h pracovného času 60 min) a s cieľovou spoľahlivosťou 90 %? Kto schvaľuje ORANŽOVÉ rozhodnutia? Ktoré upozornenia majú ísť cez SMS alebo Teams?

## B. Do 1 – 2 mesiacov (fázy F2 – F3)

- [ ] **História jázd** za posledných 6 – 12 mesiacov: plán verzus skutočnosť (príchody na hranicu a časy prevzatia, odchody, príchody, meškania a príčiny, skutočný pracovný čas rušňovodičov). Stačí export z interných systémov; doplniť sa dá aj z údajov ŽSR alebo RNE TIS.
- [ ] **Zdroj polohy vlakov.** Rozhodnutie, z čoho bude poloha: GPS jednotky v rušňoch (rozpočet, súhlas vlastníka rušňa, kto ich nainštaluje), údaje od ŽSR alebo RNE TIS, aplikácia v tablete rušňovodiča, alebo kombinácia. Možnosti sú v `docs/02_ZAVEDENIE_SYSTEMU.md`.
- [ ] **GDPR a zamestnanci.** Posúdenie vplyvu na ochranu údajov (DPIA) a prerokovanie so zástupcami zamestnancov, pretože poloha rušňa nepriamo sleduje rušňovodiča. Zodpovedná osoba (DPO) vo firme.
- [ ] **IT prostredie.** Cloud v EÚ alebo vlastný server, firemné prihlasovanie (SSO), kto bude systém prevádzkovať a kto bude vyvíjať (interné IT, dodávateľ, vývoj s pomocou AI). Pravidlá firmy pre používanie AI nástrojov s firemnými dátami.
- [ ] **Rozpočet a termín pilotu.** Na ktorých 1 – 2 reláciách začneme.

## C. Čo odo mňa nepotrebuješ zháňať

- **Mapu železničnej siete** ako obrázok nepotrebujeme. Aplikácia potrebuje sieť ako **dáta** (graf úsekov a dopravní s parametrami). Tie sa dajú zostaviť z verejných zdrojov: register infraštruktúry RINF (ERA), OpenStreetMap/OpenRailwayMap a údaje ŽSR. Od teba potrebujem len **overenie na vašich reláciách** a interné dokumenty (TTP), ktoré sú presnejšie a aktuálnejšie.
- **Algoritmy** si nevyberáš ty. Ich voľba je zdôvodnená v `docs/04_ALGORITMY.md` a v prompte sú záväzné.
