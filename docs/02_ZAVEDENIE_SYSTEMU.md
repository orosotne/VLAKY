# Zavedenie systému – čo treba pripraviť

Stav k 31. 10. 2026. Fakty sú overené v zdrojoch (zoznam v `docs/05_ZDROJE.md`). Čo treba ešte potvrdiť so ŽSR alebo vo firme, je výslovne označené.

## V skratke

1. **Firma a prístupy k ŽSR.** V zozname dopravcov ŽSR je skupina ČD Cargo vedená ako **CD Cargo Slovakia, s.r.o.** (kód 9052, skratka CDCSK). Firma „ČS KD Cargo“ sa v zdrojoch nenašla. Najpravdepodobnejšie ide o ČD Cargo / CD Cargo Slovakia. Foneticky blízka je aj **Rail Cargo Operator – ČSKD**. **Treba potvrdiť**, ktorá firma to je.
2. **Prístup do systému ŽSR KIS.** KIS je portál ŽSR pre dopravcov: rola „Dopravca dispečer“, aplikácie Poloha vlaku, Pripravenosť vlaku, Dôvody meškania, CUD výluky a prehľad obmedzení a ďalšie. Žiadosť sa podáva formulárom (xlsx) na **gro410@zsr.sk**.
3. **Server (VPS) v EÚ a vlastná e-mailová adresa agenta.** Návod je v `docs/PREVADZKA.md`.
4. **Mapa siete je hotová** z OpenStreetMap: 1 115 dopravní s kódmi UIC a skratkami ŽSR a 23 hraničných priechodov. Presnejšie parametre (dĺžky vlakov, užitočné dĺžky koľají) sa doplnia z **Tabuliek traťových pomerov (TTP)** ŽSR.
5. **Výluky prichádzajú ako dokumenty, nie ako dáta:** Rozkaz o výluke (ROV), Súborný ROV, týždenné „Povolené výluky“ (PDF) a telegramy. Mimoriadnosti hlási kontrolný dispečer ŽSR **telefonicky alebo cez WhatsApp**. Tie treba agentovi preposlať e-mailom alebo zadať ručne.
6. **Poloha vlakov má tri zdroje:**
   - údaje ŽSR a RNE TIS z bodov hlásenia (bez hardvéru),
   - GPS z telematiky rušňov (Vectron a TRAXX ju často už majú),
   - aplikácia v mobile rušňovodiča (dá sa nasadiť hneď).
7. **Pred sledovaním polohy:**
   - posúdenie vplyvu na ochranu údajov (DPIA, na Slovensku povinné),
   - prerokovanie so zástupcami zamestnancov (§13 ods. 4 Zákonníka práce),
   - pri montáži zariadenia do rušňa súhlas držiteľa rušňa a jeho ECM (subjekt zodpovedný za údržbu) – posúdenie zmeny vozidla podľa nariadenia 2018/545.
8. **Pravidlá ŽSR, ktoré agent dodržiava:**
   - ad hoc trasa najneskôr **6 h** pred odchodom,
   - vlak neodídený do **18 h** systém ŽSR (PIS) zruší,
   - odstavenie najviac **18 h**,
   - pracovná zmena rušňovodiča najviac **13 h** (zákon 462/2007 Z. z.).
9. **Zavádzanie:** pilot na 1 – 2 reláciách, 4 – 6 týždňov tieňovej prevádzky, potom ostrá prevádzka v režime „len odporúča“.
10. **Náklady:** server rádovo desiatky € mesačne, jazykový model desiatky až nižšie stovky € mesačne, GPS podľa ponuky. RNE TIS cez web je zadarmo, cez API platený (pozri nižšie).

---

## 1. Firma a prístupy

| Čo | Prečo | Ako |
|---|---|---|
| Potvrdiť firmu (CD Cargo Slovakia, s.r.o.? ČD Cargo, a.s.? Rail Cargo Operator – ČSKD?) | Od firmy závisia prístupy k ŽSR, zmluvy a kódy (RICS). | Interne |
| Zmluva o prístupe k železničnej infraštruktúre ŽSR | Podmienka všetkých prístupov | Má ju dopravca |
| **ŽSR KIS** – rola „Dopravca dispečer“ | Poloha vlakov, pripravenosť vlaku, súpis vlaku, dôvody meškania, výluky | Formulár „Žiadosť o prístup a školenie KIS“ na gro410@zsr.sk |
| Predpisy ŽSR DP 2, DP 3, DP 4 (a relevantné časti Z 1) | Záväzné pre dopravcu: PIS, riadenie dopravy, výluková činnosť | Objednať cez ŽSR CLaO |
| **RNE TIS** – firemný účet | Medzinárodné vlaky naprieč SK, CZ, PL, HU, AT, ETA siete | Registrácia na tis-prod.rne.eu, užívateľská zmluva s RNE |
| (neskôr) zmluva o dátovej komunikácii podľa DP 2 + RNE Common Interface | Strojová výmena správ s KIS (TSI XML) | Až keď bude potrebná automatizácia |
| SŽ (ČR): GRAPP, KADR, ISOŘ | Vlaky na českej strane (pred preberaním) | ppdpristup@spravazeleznic.cz |

## 2. Server a e-mailová adresa agenta

Podrobný postup je v `docs/PREVADZKA.md`. Hlavné body:

- VPS v EÚ (4 vCPU, 8 – 16 GB RAM, 160 GB SSD), doména, TLS, zálohy a externý monitoring.
- Schránka na firemnej doméne (napr. `agent.dispecing@firma.sk`) a **kópie** prevádzkových e-mailov do nej. Ľudia dostávajú e-maily naďalej, agent ich iba číta navyše.
- Zoznam povolených odosielateľov (domény ŽSR, partnerov, zákazníkov) a zoznam dispečerov, ktorým agent posiela odporúčania.

## 3. Mapa siete a údaje o infraštruktúre

| Zdroj | Čo dáva | Obmedzenia | Stav v systéme |
|---|---|---|---|
| **OpenStreetMap** (extrakt pre SR) | Geometria, topológia, kódy UIC a skratky ŽSR, elektrifikácia, napätie, rozchod, rýchlosti, čísla tratí | Licencia ODbL (uvádzať zdroj). Verejné dlaždice OSM ani OpenRailwayMap sa nesmú používať vo veľkom → na serveri vlastné dlaždice. | **Hotové** – `data/siet`, aktualizácia príkazom `python -m vlaky import-osm` |
| **TTP – Tabuľky traťových pomerov ŽSR** (125 PDF podľa tratí 101 A … 130 D) | Normy dĺžky nákladných vlakov po smeroch, užitočné dĺžky koľají, rozhodujúce sklony, traťové triedy, obrysy, miesta zmeny trakcie | PDF, časté zmeny (číslo zmeny a platnosť) | Doplniť do „Dopravne“ (čakanie, odstavenie, dĺžky) a do pravidiel |
| **ERA RINF** (verejný SPARQL) | Úradné identifikátory, kategórie tratí, ETCS, RFC | Údaje SR sú z 30. 7. 2025, chýba max. dĺžka vlaku a sú v nich chyby jednotiek | Na kontrolu a prepojenie kódov |
| **ŽSR INSPIRE WMS/WMTS** | Oficiálna mapa ŽSR (zadarmo, © ŽSR) | WFS bez železničných vrstiev | Voliteľná vrstva mapy |
| **Sieťové vyhlásenie ŽSR 2027 (PPŽS)** | 23 hraničných priechodov, systémy trakcie, zoznam 444 dopravných bodov pre nákladnú dopravu | PDF | Hraničné priechody **hotové** (`data/hranice.json`) |

**Pozor:**

- **InfoMapa ŽSR a mapa výluk ŽSR zakazujú automatické sťahovanie.** Systém ich preto nepoužíva.
- **Kódy dopravných bodov:** číslo ŽSR (6 číslic) = 5-ciferné číslo miesta + kontrolná číslica. OSM `uic_ref` = 56 + 5 číslic (napr. Žilina 5617915).

## 4. Výluky, trasy a e-maily

**Trasy:**

- **Systém na objednávanie:** IS KIS (objednávkový modul).
- **Medzinárodné trasy na koridoroch RFC 5 a RFC 9** sa objednávajú cez RNE PCS, na koridore cez jeho kontaktné miesto C-OSS.
- **Lehoty ad hoc trás:** produkty na krátky čas (P5, OM, ID) aspoň **6 h** pred odchodom. Pri iných produktoch je lehota 5 pracovných dní alebo 30 dní. Odpoveď môže trvať až 5 pracovných dní.
- **Platnosť trasy:** vlak neodídený do **18 h** od plánovaného odchodu PIS automaticky zruší a odstavenie v nácestnej stanici je najviac **18 h**. PPŽS na inom mieste uvádza 24 h – **treba overiť so ŽSR**. Systém používa prísnejšiu hodnotu.
- **Poplatky:** pri zrušení trasy menej ako 20 pracovných dní vopred sa platí poplatok U1. Zmena po pridelení sa účtuje ako zrušenie pôvodnej trasy.
- **Dohoda o kvalite (dobrovoľná):** odchod z prvej stanice ŽSR −60 až 0 min, príchod do +60 min, pokuta 33 € za vlak.

**Výluky:**

- **Formy, v akých prichádzajú:** Rozkaz o výluke (ROV), Súborný ROV (SROV), týždenné „Oznámenie o povolených výlukách“ a plány výluk (PDF na zsr.sk/…/plan-vyluk) a telegramy. Mimoriadnosti hlási kontrolný dispečer ŽSR (OR Trnava, Žilina, Zvolen, Košice) telefonicky alebo cez **WhatsApp**.
- **Pomalé jazdy (POTR):** pribúdajú, 20. 9. 2026 ich bolo 146. V horúčavách ŽSR obmedzuje rýchlosť na 80 km/h medzi 10:00 a 21:00.
- **Strojovo čitateľné výluky zatiaľ neexistujú:** výmena údajov o výlukách je v TEL TSI stále otvorený bod. Preto systém číta e-maily a prílohy (PDF, XLSX).

**Odporúčanie:** dohodnúť sa vo firme, že **všetko podstatné z telefónu a WhatsAppu sa krátko prepošle e-mailom** do schránky agenta (alebo zadá na stránke Výluky). Agent inak o zmene nevie.

## 5. Poloha vlakov – porovnanie zdrojov

| Zdroj | Presnosť a frekvencia | Cena | Kedy |
|---|---|---|---|
| **ŽSR KIS „Poloha vlaku“** | Body hlásenia (stanice), nie GPS | V rámci prístupu (bez osobitného poplatku) | Hneď po pridelení prístupu – zatiaľ ručne v portáli KIS. Strojovo cez DP 2 a Common Interface. |
| **RNE TIS** | Body hlásenia pri medzinárodných vlakoch. ŽSR v roku 2024 dodalo 83 % správ TRI. K dispozícii je aj ETA siete. | Web zadarmo. API (Data Flow) platené: v cenníku 2023 od 8 000 € na zriadenie + 300 €/mesiac, pre stredného dopravcu ~18 – 31 tis. €/rok. | Hneď (web), neskôr API |
| **Telematika rušňov** | GPS každých ~10 – 60 s. Na úrovni trate 10 – 20 m, súbežné koľaje sa rozlíšiť nedajú. | Podľa dodávateľa | Najprv zistiť, čo rušne už majú (Vectron: Remote Data Access/Railigent X; TRAXX: Railnova; CZ LOKO: GPS/GSM) |
| **Aplikácia rušňovodiča** (súčasť systému) | GPS mobilu každých 30 s, hlásenia nástupu, pripravenosti a odchodu | Bez hardvéru | **Hneď** |
| **ČD Cargo Eroza / ČDCgo** | Poloha a ETA pre zákazníkov. Mimo ČR podľa FAQ môžu chýbať. | – | Na porovnanie |

Systém prijíma všetky zdroje cez `/api/pozicie` a `/api/udalosti-jazdy`, polohu priradí ku koľaji a pri každej uvedie vek a zdroj.

## 6. GPS v rušňoch – požiadavky na nákup a postup

**Technické požiadavky (do dopytu dodávateľom):**

- železničná elektronika podľa **EN 50155:2026**, EMC **EN 50121-3-2**, požiarna bezpečnosť **EN 45545-2**, vibrácie EN 61373,
- napájanie z rušňa (24/110 V DC podľa typu), GNSS (GPS + Galileo), **LTE** s roamingom SK/CZ/PL/HU/AT (3G je už vypnuté, 2G končí ~2028 – 2029), eSIM (SGP.32),
- ukladanie pri výpadku signálu (tunely), interval 10 – 30 s v jazde, API (HTTPS/MQTT alebo Kafka) a otvorený formát dát,
- zabezpečenie (IEC 62443), vzdialená správa, montáž bez zásahu do bezpečnostných obvodov.

**Postup:**

1. **Inventúra rušňov:** rad, EVN, vlastník alebo prenajímateľ, ECM a existujúca telematika.
2. **Využiť existujúcu telematiku:** Vectron Remote Data Access/Railigent X, Railnova na TRAXX, CZ LOKO. Vyžiadať si prístup k API od prenajímateľa alebo výrobcu.
3. **Nová montáž:** zmenu posúdi a zaradí subjekt, ktorý zmenu riadi (zvyčajne držiteľ alebo ECM), podľa **nariadenia (EÚ) 2018/545, čl. 15 a 16**. Nezasahujúca skrinka spravidla nové povolenie nepotrebuje, ale je to rozhodnutie v každom prípade zvlášť. Pri prenajatých rušňoch treba **súhlas prenajímateľa**.
4. **Právne kroky pred spustením:**
   - **DPIA** – podľa zoznamu ÚOOÚ SR povinná pri lokalizačných údajoch a monitorovaní zamestnancov,
   - **prerokovanie so zástupcami zamestnancov** a informovanie zamestnancov (SK §13 ods. 4 Zákonníka práce, CZ §316),
   - **účel:** dispečing a ETA, nie hodnotenie rušňovodičov.
5. **Pilot na 3 – 5 rušňoch** a kontrola priradenia ku koľaji na mape.

## 7. Pracovný čas rušňovodičov (právny rámec)

- **SR:** zákon 462/2007 Z. z. – zmena najviac **13 h**, denný odpočinok 11 h (s lôžkom možno skrátiť na 6 h).
- **ČR:** nariadenie vlády 589/2006 Sb. – zmena najviac 13 h, s režijnou jazdou na konci zmeny až 15 h.
- **Cezhraničná interoperabilná prevádzka:** smernica 2005/47/ES – jazda najviac 9 h cez deň a 8 h v noci, 80 h za dva týždne, odpočinok 12 h doma alebo 8 h mimo domova, prestávky 30/45 min.
- **Jazyk:** rušňovodič bez slovenčiny na úrovni B1 smie podľa výnimky ŽSR ísť len po hraničnú stanicu kvôli výmene.

**V systéme:** 13 h limit je implementovaný. Čas jazdy (9/8 h), prestávky a odpočinok treba doplniť podľa kolektívnej zmluvy (nastavenia).

## 8. Legislatíva, ktorá sa práve mení

- **TEL TSI – vykonávacie nariadenie (EÚ) 2026/253** (účinné od 2. 3. 2026) nahradilo TAF TSI 1305/2014. Manažér infraštruktúry musí ponúknuť API aj webové rozhranie. Pre údaje o nákladnej doprave platí termín 9. 12. 2029.
- **Nariadenie (EÚ) 2026/1184 o kapacite** (prijaté 20. 5. 2026) dáva od **31. 12. 2027** právo na prístup k údajom o jazde a prognózam vlakov. Pravidlá o výlukách (príloha VII smernice 2012/34/EÚ) nahrádza od grafikonu 2031.
- **RNE Capacity Planning Tool** (od 19. 10. 2026) nahradil nástroj TCR na výluky.

## 9. Kúpiť alebo postaviť?

Na trhu sú hotové systémy pre dopravcov: OLTIS SAFIRA Dispečink (dodávateľ GRAPP a KIS), RailCube, ZEDAS cargo, IVU.rail. **Žiadny sa však nenašiel, ktorý by čítal e-maily ŽSR o výlukách a plánoval okolo nich.** Odporúčanie: **telematiku kúpiť, rozhodovaciu vrstvu (tento Agent dispečer) mať vlastnú** a napojiť ju na existujúce systémy cez API.

## 10. Fázy zavedenia

| Fáza | Obsah | Dĺžka (orientačne) |
|---|---|---|
| 0 | Potvrdenie firmy, prístupy KIS a TIS, server, schránka, vzorové e-maily | 2 – 4 týždne |
| 1 | Inštalácia, import siete, vlastné náklady a parametre, používatelia | 1 týždeň |
| 2 | **Tieňová prevádzka**: agent odporúča len vedúcemu, porovnanie s rozhodnutiami dispečerov | 4 – 6 týždňov |
| 3 | Ostrá prevádzka „len odporúča“ pre všetkých dispečerov | – |
| 4 | Poloha z telematiky a TIS, kalibrácia rezerv z histórie, ďalšie relácie | priebežne |

## 11. Riziká a opatrenia

| Riziko | Opatrenie |
|---|---|
| Zmena príde telefonicky alebo cez WhatsApp a agent o nej nevie | Firemné pravidlo preposlať e-mailom; ručné zadanie výluky na webe; v odporúčaní je vždy uvedené, z akých dát vychádza |
| Chybne vytiahnutá výluka z e-mailu | Fronta na potvrdenie, istota, citácia originálu, konzervatívne pravidlá |
| Podvrhnutý e-mail alebo prompt injection | Zoznam odosielateľov, DMARC, karanténa, model bez nástrojov, agent nič neposiela navonok |
| Výpadok agenta | Externý heartbeat, ľudia majú e-maily naďalej, postup v PREVADZKA.md |
| Nepresné časy z mapy | Časy z pridelených trás ŽSR, kalibrácia z histórie, rezervy |
| GDPR a pracovné právo | DPIA, prerokovanie so zástupcami, minimálne údaje, len dispečerské účely |
| Rozpory v PPŽS (18 h / 24 h) | Prísnejšia hodnota, potvrdenie so ŽSR, nastaviteľný parameter |
