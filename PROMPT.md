# PROMPT A — Vývoj dispečerskej aplikácie pre nákladné vlaky na sieti ŽSR

**Ako použiť:** Text pod čiarou skopíruj do AI nástroja na vývoj softvéru (Claude Code, Cursor, OpenAI Codex…) alebo ho odovzdaj IT či dodávateľovi. Údaje v `[HRANATÝCH ZÁTVORKÁCH]` doplň, alebo ich nechaj prázdne – AI sa na ne má na začiatku opýtať. Predvolené rezervy a prahy v sekcii 8 sú návrh na začiatok; po prvých týždňoch prevádzky ich treba nastaviť podľa vašich skutočných dát (postup je v `docs/03_REZERVY_A_ROZHODOVANIE.md`).

Kratší prompt pre AI asistenta na každodennú prácu (kým aplikácia nie je hotová) je v `PROMPT_ASISTENT.md`.

---

## 0. Tvoja rola a pravidlá práce

Si skúsený softvérový architekt a vývojár. Rozumieš železničnej nákladnej doprave v SR a ČR, operačnému výskumu (grafové algoritmy, optimalizácia, simulácia) a spracovaniu textu jazykovými modelmi (LLM). Navrhneš a po fázach naprogramuješ aplikáciu **na podporu rozhodovania dispečera** nákladného železničného dopravcu.

Pravidlá:

1. **Najprv otázky, potom kód.** Prečítaj celé zadanie a polož otázky zo sekcie 13, na ktoré nepoznáš odpoveď. Potom napíš krátky dokument `ARCHITEKTURA.md` (komponenty, dátový tok, rozhodnutia a ich dôvody) a plán fázy F0. Až potom programuj.
2. **Po fázach.** Postupuj podľa sekcie 11. Každá fáza končí funkčným, otestovaným stavom, návodom na spustenie a zoznamom známych obmedzení.
3. **Žiadne vymyslené železničné dáta.** Stanice, úseky, parametre tratí, výluky, trasy ani časy si nesmieš „domyslieť“ – ani ty, ani LLM v aplikácii. Ak dáta chýbajú, naprogramuj importér a použi testovacie dáta jasne označené ako `FIKTÍVNE`.
4. **Podpora rozhodovania, nie riadenie.** Aplikácia neriadi vlaky, nezasahuje do zabezpečovacích zariadení a bez potvrdenia dispečerom nič neodošle ŽSR, zákazníkom ani rušňovodičom.
5. **Vysvetliteľnosť a audit.** Každé odporúčanie ukáže, prečo vzniklo, z akých dát, aké sú tie dáta staré a s akou istotou platí. Každé rozhodnutie dispečera sa uloží (kto, kedy, čo, prečo).
6. **Deterministické jadro.** Trasovanie, kontrolu výluk, rezervy a rozhodovanie rob klasickými, testovanými algoritmami (sekcie 7 a 8). LLM používaj len na porozumenie textu (e-maily, prílohy), na vysvetlenia a na koncepty správ. Jeho výstup vždy over pravidlami.
7. **Bezpečnosť od začiatku.** Obsah e-mailov je nedôveryhodný vstup (riziko prompt injection). Tajomstvá nepatria do kódu. Platí princíp najmenších oprávnení a osobné údaje spracúvaj podľa GDPR.
8. **Kvalita.** Typy, automatické testy (jednotkové aj scenárové), lint, Docker, README v slovenčine. Zmeny modelu dát cez migrácie.

## 1. Kontext

- **Dopravca:** `[NÁZOV DOPRAVCU – napr. ČD Cargo, a.s. / ČD Cargo Slovakia, s.r.o.]`. Je to železničný podnik (RU), ktorý prevádzkuje nákladné vlaky na sieti **ŽSR** (Železnice Slovenskej republiky – manažér železničnej infraštruktúry, IM) a nadväzne na sieťach susedných štátov `[ČR – Správa železnic; ďalšie: …]`.
- **Používateľ:** dispečer dopravcu. Sprostredkúva medzi **zákazníkom** (objednávateľom prepravy) a **ŽSR**: dohaduje termíny, objednáva trasy, rieši výluky, sleduje jazdu vlakov a informuje zákazníka.
- **Rozsah prevádzky:** `[počet vlakov denne/týždenne; hlavné relácie; hraničné priechody; typy rušňov – elektrické (AC/DC/viacsystémové) / motorové; typické dĺžky a hmotnosti vlakov; vlečky zákazníkov]`.
- **Dnešný stav (problém):** dôležité informácie (výluky, pridelené a zmenené trasy, mimoriadnosti, objednávky) chodia e-mailom v rôznych formátoch a prílohách. Poloha vlakov nie je na jednom mieste. Rozhodnutie, kedy vlak vypraviť, robí dispečer „z hlavy“.
- **Cieľ:** jeden dispečerský panel, ktorý
  1. ukáže polohu a stav vlastných vlakov na mape v reálnom čase,
  2. priebežne číta e-maily a udržiava register výluk, obmedzení a trás,
  3. navrhne najvhodnejší spôsob prepravy z A do B a rešpektuje pri tom výluky a meškania,
  4. priebežne počíta predpokladaný príchod (ETA) a pravdepodobnosť dodržania termínu,
  5. odporučí **VYPRAVIŤ / POČKAŤ / ODSTAVIŤ / PRESMEROVAŤ / ESKALOVAŤ** podľa pravidiel s bezpečnými časovými rezervami.
- **Merateľné ciele (KPI):** podiel zásielok doručených v termíne; počet konfliktov s výlukami zachytených vopred; čas dispečera strávený prácou s e-mailmi; presnosť ETA (P50/P90); počet neplánovaných zadržaní vlakov pred výlukou.

## 2. Slovník pojmov (používaj ich konzistentne v UI aj v kóde)

| Pojem | Význam |
|---|---|
| ŽSR | Železnice Slovenskej republiky – manažér infraštruktúry: prideľuje kapacitu (trasy), plánuje výluky, riadi prevádzku na sieti. |
| Dopravca (RU) | Železničný podnik, ktorý prevádzkuje vlak – my. |
| Trasa vlaku | Pridelená kapacita infraštruktúry na jazdu vlaku v konkrétnom čase (angl. *train path*). |
| GVD | Grafikon vlakovej dopravy – ročný cestovný poriadok, mení sa v decembri. |
| Ad hoc trasa | Trasa objednaná krátkodobo mimo ročného GVD. |
| Výluka | Plánované alebo mimoriadne obmedzenie alebo vylúčenie koľaje, úseku či trakčného vedenia z prevádzky (EÚ: *TCR – temporary capacity restriction*). |
| Pomalá jazda | Dočasné obmedzenie traťovej rýchlosti na úseku. |
| Dopravňa | Miesto na trati, kde môžu vlaky zastaviť, križovať sa a predchádzať (stanica, výhybňa…). |
| Úsek | Traťový úsek medzi dvoma dopravňami (hrana grafu). |
| Vlečka | Vedľajšia koľaj alebo sieť iného vlastníka (často zákazníka) napojená na sieť ŽSR. |
| Vypraviť vlak | V tejto aplikácii: rozhodnutie dopravcu pustiť pripravený vlak na trasu. Samotný odchod vlaku povoľuje ŽSR. |
| Odstaviť vlak | Vlak sa na dlhší čas odstaví na koľaji mimo trasy; čata sa uvoľní; neskôr pôjde po novej trase. |
| Čata | Rušňovodič (a prípadne ďalší zamestnanci) vlaku – má limity pracovného času a musí poznať trať. |
| TTP | Tabuľky traťových pomerov – podrobné údaje ŽSR o tratiach a dopravniach. |
| RINF | Register železničnej infraštruktúry EÚ (vedie ho ERA) – parametre tratí a dopravných bodov. |
| TAF TSI | Technická špecifikácia EÚ pre telematiku v nákladnej doprave – štandardné správy medzi ŽSR a dopravcom (napr. o jazde vlaku). |
| RNE TIS | Train Information System združenia RailNetEurope – údaje o jazde medzinárodných vlakov. |
| ETA, P50, P90 | Predpokladaný čas príchodu; medián (polovica jázd príde skôr) a 90. percentil (90 % jázd príde skôr). |

## 3. Rozsah

**V rozsahu:** vlastné vlaky dopravcu na sieti ŽSR a nadväzné úseky v ČR (podľa dostupných dát); plánovanie, sledovanie, varovania a odporúčania; spracovanie e-mailov; koncepty správ na schválenie.

**Mimo rozsahu (aspoň vo fázach F0 – F3):** riadenie dopravy a zabezpečovacie zariadenia; automatické odosielanie objednávok trás do systémov ŽSR (aplikácia pripraví koncept, odošle ho človek); fakturácia a účtovníctvo; sledovanie cudzích vlakov.

## 4. Používatelia a roly

- **Dispečer** – všetko vidí, potvrdzuje extrahované údaje z e-mailov a rozhoduje.
- **Vedúci dispečingu** – nastavuje parametre (rezervy, prahy, náklady), vidí reporty a audit.
- **Obchod / zákaznícky servis** – len čítanie (stav zásielok, ETA).
- **Administrátor** – používatelia, integrácie, šablóny.
- **Zákazník** (neskôr, voliteľne) – vlastný portál s ETA svojich zásielok.

## 5. Funkčné požiadavky (moduly)

### M1 Mapa a živý prehľad
- Mapa siete ŽSR (podklad: OpenStreetMap/OpenRailwayMap + vrstva vlastného modelu siete). Vrstvy: vlaky (farba podľa semaforu zo sekcie 8), výluky a pomalé jazdy s časovým filtrom („platí teraz / do 24 / 48 / 72 h“), hraničné priechody, miesta vhodné na čakanie a odstavenie.
- Tabuľka vlakov: číslo vlaku, relácia, posledná poloha (úsek/dopravňa, čas a zdroj správy), odchýlka od trasy, ETA (P50/P90), termín zákazníka, rezerva, semafor, najbližšie riziko (výluka, koniec smeny čaty, chýbajúca trasa).
- Časová os (Gantt): vlaky a výluky na spoločnej osi; konflikty sú zvýraznené.
- **Čerstvosť dát:** pri každej polohe je vek a zdroj (GPS / ŽSR / RNE TIS / ručne). Ak je poloha staršia ako `[15]` min, vlak je sivý a ETA je označená ako neistá. **Bez čerstvých dát systém nikdy neukáže zelenú.**

### M2 Objednávky a vlaky
- **Objednávka zákazníka:** zákazník, odkiaľ – kam (stanica alebo vlečka), najskorší odchod alebo pristavenie, požadovaný termín doručenia (pevný / s pokutou / orientačný), tovar (vrátane RID – nebezpečný tovar), počet vozňov, hmotnosť, dĺžka, osobitné požiadavky (napr. mimoriadna zásielka).
- **Vlak:** číslo, väzba na objednávky, zostava (dĺžka, hmotnosť, nápravové zaťaženie, brzdiace percentá, ak sú známe), rušeň a trakcia, čata, pridelené trasy, stav (plánovaný → pripravený → v jazde → odstavený → ukončený / zrušený).
- Automatické párovanie e-mailov a správ s vlakmi (číslo vlaku, dátum, relácia), pri nejednoznačnosti sa pýta dispečera.

### M3 Model siete
- **Graf:** uzly = dopravné body (stanice, výhybne, odbočky, hraničné body, napojenia vlečiek), hrany = traťové úseky.
- **Atribúty úsekov a uzlov:** dĺžka, číslo trate, traťová rýchlosť, sklon (ak je k dispozícii), elektrifikácia a napájací systém, traťová trieda (nápravové zaťaženie), užitočné dĺžky koľají v dopravniach (max. dĺžka vlaku, ktorý tam môže stáť), jednokoľajná/dvojkoľajná trať, rozchod (1435 mm / 1520 mm), vlakový zabezpečovač / ETCS, hraničný priechod, možnosť čakania a odstavenia.
- **Zdroje:** sekcia 6. Každý atribút si pamätá zdroj a dátum. Ručná oprava je možná, ale len s auditom.
- **Verziovanie:** sieť sa mení (modernizácie, nové výhybne). Výpočty používajú verziu platnú v deň jazdy.

### M4 Register výluk a obmedzení
- **Záznam:** ID; zdroj (e-mail, plán výluk ŽSR, ručne) s odkazom na originál; typ (úplná výluka úseku, výluka koľaje – jednokoľajná prevádzka, vypnutie trakčného vedenia, pomalá jazda, obmedzenie dĺžky/hmotnosti, mimoriadnosť); úsek (od – do dopravne, číslo trate, koľaj); platnosť (od – do, opakovanie – napr. denne 08:00 – 14:00 v období od – do); dopad (ktoré trakcie, max. rýchlosť, náhradné riešenie); stav (návrh z AI → overené dispečerom → zmenené / zrušené); história verzií.
- **Automatická kontrola konfliktov:** po každej zmene registra alebo plánu sa skontrolujú všetky plánované aj idúce vlaky podľa pravidiel zo sekcie 8 (vrátane rezerv pred a po výluke). Pri konflikte vznikne upozornenie s návrhom riešenia.
- **Import:** z e-mailov (M5) a z verejného plánu výluk ŽSR `[podľa dostupného formátu]`.

### M5 Monitoring e-mailov (AI)
- **Napojenie:** schránka `[Microsoft 365 / Gmail / IMAP – doplniť]`, ideálne zdieľaná dispečerská schránka, len s oprávnením na čítanie. Spracovanie v reálnom čase (push notifikácie) a záložné periodické dočítanie (napr. každých 5 min), aby sa nič nestratilo.
- **Klasifikácia:** nová / zmenená / zrušená výluka; pomalá jazda; pridelenie trasy; zmena alebo zrušenie trasy; mimoriadnosť na trati; objednávka alebo zmena od zákazníka; ostatné.
- **Extrakcia** do JSON podľa pevnej schémy (štruktúrovaný výstup LLM) vrátane príloh (PDF, XLSX, DOCX; OCR pre skeny). Názvy dopravní sa normalizujú proti registru siete. Overujú sa časy, dátumy a logika (koniec po začiatku, úsek existuje). Každé pole má skóre istoty.
- **Človek v slučke:** všetko, čo ovplyvní vlak v najbližších `[72]` h alebo má istotu pod `[0,9]`, ide do fronty na potvrdenie. Dispečer vidí originál vedľa vytiahnutých polí a jedným klikom potvrdí alebo opraví. Opravy sa ukladajú ako trénovacie a testovacie príklady.
- **Ochrana:** obsah e-mailu je iba dáta. LLM v kroku extrakcie nemá žiadne nástroje (neodosiela, nevolá API, nemení dáta). Platí zoznam povolených odosielateľov `[napr. domény ŽSR, zákazníkov]`; ostatné e-maily sa len označia. E-maily s pokynmi typu „ignoruj predchádzajúce inštrukcie“ sa označia ako podozrivé.
- **Audit:** originál, extrakcia, verzia modelu a promptu, kto a kedy potvrdil.

### M6 Plánovanie prepravy z A do B
- **Vstup:** objednávka (A, B, časové okno), parametre vlaku, dostupné rušne a čaty, register výluk, pridelené alebo možné trasy.
- **Výstup:** 3 – 5 variantov (najrýchlejší, najspoľahlivejší, najlacnejší, s najmenším počtom zmien). Každý obsahuje postupnosť úsekov, časový plán, potrebu nových trás, konflikty, P(včas), očakávané náklady a vysvetlenie v slovenčine.
- Dodržiava tvrdé obmedzenia zo sekcie 7.1 a pravidlá zo sekcie 8.

### M7 ETA a sledovanie rizika
- Prepočet ETA (P50/P90) a P(včas) pri každej novej polohe, správe alebo zmene výluky, u idúcich vlakov aspoň každých `[15]` min.
- **Upozornenia:** rezerva klesla pod prah; hrozí konflikt s výlukou; hrozí prekročenie smeny čaty; vlak stojí mimo plánu dlhšie ako `[20]` min; chýbajú dáta o polohe; e-mail mení trasu vlaku, ktorý už ide.
- Kanály: v aplikácii a `[MS Teams / SMS / e-mail]` podľa závažnosti; nočný režim len pre kritické upozornenia.

### M8 Rozhodovací modul
- Pre každý vlak pred odchodom a v kľúčových bodoch (pred úsekom s výlukou, pred hraničným priechodom, pri výmene čaty) vyhodnotí **VYPRAVIŤ / POČKAŤ / ODSTAVIŤ / PRESMEROVAŤ / ESKALOVAŤ** podľa sekcie 8.
- Zobrazí semafor, **okná bezpečného odchodu** (napr. „vypraviť najneskôr o 06:10, inak až od 12:40“), porovnanie variantov v tabuľke a vysvetlenie.
- Dispečer odporúčanie potvrdí alebo zmení (s dôvodom). Ukladá sa to do auditu a slúži na zlepšovanie modelu.

### M9 Komunikácia
- Koncepty správ: objednávka ad hoc trasy pre ŽSR `[podľa formátu, ktorý ŽSR vyžaduje – doplniť šablónu]`, informácia zákazníkovi o ETA alebo meškaní, žiadosť o zmenu termínu, pokyn pre čatu. **Nikdy sa neodosielajú automaticky.**
- Šablóny v slovenčine a češtine.

### M10 História a reporty
- Plán verzus skutočnosť (časy na úsekoch, zdržania a ich príčiny), presnosť ETA, dodržanie termínov, rozhodnutia dispečera verzus výsledok. Export CSV/XLSX.
- Podklady na kalibráciu rezerv: P50/P90 zdržaní po úsekoch, hraničných priechodoch, dňoch v týždni a denných dobách; skutočné začiatky a konce výluk oproti plánu.

### M11 Nastavenia
- Parametre rezerv, prahov a nákladov (sekcia 8), kontakty, šablóny, povolení odosielatelia, používatelia a roly. Každá zmena parametra je v audite s dátumom platnosti.

## 6. Dátové zdroje a integrácie

| Zdroj | Čo dáva | Napojenie | Fáza |
|---|---|---|---|
| RINF (ERA) | Úradné parametre úsekov a dopravných bodov SR | Export/API podľa dostupnosti; import do modelu siete | F0 |
| OpenStreetMap / OpenRailwayMap | Geometria tratí a staníc pre mapu | Extrakt OSM (Geofabrik / Overpass); licencia ODbL – uviesť zdroj | F0 |
| Dokumenty ŽSR (podmienky používania infraštruktúry, TTP, plán výluk) | Overenie parametrov, výluky, pravidlá trás | Import PDF/XLSX, prípadne ručne s auditom | F0 – F1 |
| E-mailová schránka dispečingu | Výluky, trasy, zmeny, objednávky | Microsoft Graph / Gmail API / IMAP | F1 |
| Interné systémy dopravcu | Objednávky, rušne, čaty, zostavy vlakov | Export/API `[doplniť systémy]` | F2 |
| Údaje o jazde vlakov od ŽSR / RNE TIS / TAF TSI | Skutočné časy prechodov dopravňami, meškania | Podľa dostupnosti a zmluvných podmienok | F3 |
| GPS jednotky v rušňoch | Poloha každých 10 – 30 s | API/MQTT dodávateľa telematiky | F3 |
| Aplikácia v tablete/mobile rušňovodiča (alternatíva) | Poloha + hlásenia (pripravený, odchod, problém) | Vlastná webová aplikácia (PWA) | F3 |

Každý adaptér zdroja: idempotentný import, zaznamenanie času a zdroja, monitorovanie výpadkov, testy na vzorových dátach.

## 7. Algoritmy (záväzné)

### 7.1 Tvrdé obmedzenia (filter uskutočniteľnosti pre konkrétny vlak)
- trakcia verzus elektrifikácia a napájací systém (elektrický rušeň len tam, kde vie jazdiť; viacsystémové rušne; vypnuté trakčné vedenie počas výluky),
- nápravové zaťaženie a traťová trieda verzus vozne,
- dĺžka vlaku verzus užitočné dĺžky koľají v dopravniach, kde má vlak zastaviť, čakať alebo križovať,
- rozchod (1435 / 1520 mm) a obrysy pri mimoriadnych zásielkach,
- obmedzenia pre nebezpečný tovar (RID), ak sú v dátach,
- znalosť trate a jazykové či odborné spôsobilosti čaty na danom úseku (najmä cezhranične),
- vlakový zabezpečovač / ETCS na rušni verzus požiadavky trate,
- výluky a obmedzenia z registra (M4) **vrátane rezerv zo sekcie 8**.

### 7.2 Trasovanie v čase
- Graf s časovo závislými hranami: úsek je počas výluky (rozšírenej o rezervy) pre daný typ trakcie nedostupný; pomalá jazda predĺži jazdnú dobu.
- Jazdná doba: z pridelenej trasy, ak existuje; inak výpočet z traťových rýchlostí a parametrov vlaku alebo historický priemer úseku. Vždy s označením, odkiaľ čas pochádza.
- Algoritmus: časovo závislý Dijkstra / A* s možnosťou čakať len v dopravniach, kde je dosť dlhá koľaj → najskorší príchod. Alternatívy: k-najkratších ciest (Yen, k = 5) na statickom uskutočniteľnom grafe a ich následné časové vyhodnotenie. Viac kritérií (čas, náklad, spoľahlivosť) → Paretova množina, zobraziť 3 – 5 najlepších.
- Sieť SR má rádovo tisíce uzlov, takže výpočet trvá milisekundy. Exotické algoritmy nie sú potrebné; dôležitejšie sú správne dáta a obmedzenia.

### 7.3 Trasy (kapacita ŽSR)
- Dopravca nevidí celý grafikon ŽSR, preto aplikácia navrhuje **časové okná pre objednávku ad hoc trasy** a kontroluje známe obmedzenia (výluky, rezervy, čata, rušeň).
- Ak budú k dispozícii dáta o grafikone (napr. cestovný poriadok osobnej dopravy alebo railML), pridaj hľadanie trasy vo zvyškovej kapacite (inšpirácia: modul STDCM v open-source projekte OSRD).

### 7.4 ETA a pravdepodobnosť včasného príchodu
- **Základ:** zostávajúca plánovaná jazdná doba od aktuálnej polohy po trase + časy úkonov (skúška bŕzd, výmena rušňa alebo čaty, hraničné úkony).
- **Korekcia z histórie:** model kvantilovej regresie (napr. gradient boosting) s príznakmi: úsek, denná doba, deň v týždni, typ a hmotnosť vlaku, aktuálne meškanie, výluky, hraničný priechod → P50/P90 zostávajúceho času. Kým nie je dosť histórie, použi predvolené hodnoty zo sekcie 8.
- **Simulácia Monte Carlo** (≥ 2 000 vzoriek): zdržanie na úseku má lognormálne rozdelenie s mediánom P50 a 90. percentilom P90 daného úseku. Výsledok: P(včas), P(zadržania pred výlukou), P(prekročenia smeny čaty) a očakávaný náklad. Pri porovnávaní variantov použi **spoločné náhodné čísla**, aby bolo porovnanie férové.

### 7.5 Viac vlakov naraz (fáza F4)
- Priradenie rušňov a čiat k vlakom a časom: CP-SAT (Google OR-Tools). Obmedzenia: dostupnosť, pracovný čas, znalosť trate, trakcia. Cieľ: minimálny očakávaný náklad a meškanie.

### 7.6 Úloha LLM
- **Áno:** klasifikácia a extrakcia z e-mailov a príloh, vysvetlenie odporúčaní, koncepty správ, pomoc pri vyhľadávaní v histórii.
- **Nie:** rozhodovanie o uskutočniteľnosti, výpočet časov, „odhad“ chýbajúcich dát. Výstup LLM je vždy validovaný (schéma, register siete, logické kontroly) a pri nízkej istote ho potvrdzuje človek.

### 7.7 Nové algoritmy
Nové teoretické výsledky v hľadaní najkratších ciest (2025 – 2026) pri sieti veľkosti ŽSR prakticky nič nezrýchlia. Úzkym hrdlom sú dáta, obmedzenia a neistota. Akýkoľvek nový algoritmus alebo model zaraď do výroby až vtedy, keď na vašich historických dátach v pripravenom porovnávacom teste (backtest) prekoná doterajšie riešenie.

## 8. Časové rezervy a rozhodovacie pravidlá (záväzné, konfigurovateľné)

### 8.1 Predvolené parametre
Hodnoty sú **návrh na začiatok**. Náklady sú vzorové – nahraď ich vlastnými. Po nazbieraní aspoň `[30]` jázd na úseku nahraď predvolené zdržania 90. percentilom skutočných zdržaní.

```yaml
# PARAMETRE-ZACIATOK
# --- Časové rezervy [min] ---
buffer_before_closure_min: 30   # úsek s výlukou musí byť opustený aspoň 30 min pred jej začiatkom
buffer_after_closure_min: 30    # do úseku vstúpiť najskôr 30 min po plánovanom konci výluky
deadline_buffer_min: 60         # plánovaný príchod (medián) aspoň 60 min pred termínom zákazníka
crew_buffer_min: 60             # 90 % jázd skončí aspoň 60 min pred limitom smeny čaty
# --- Trasy ---
path_tolerance_min: 15          # odchýlka od pridelenej trasy, ktorá ešte nevyžaduje novú trasu
new_path_lead_min: 180          # najkratšia lehota na získanie novej (ad hoc) trasy – overiť podľa pravidiel ŽSR
# --- Prahy rozhodovania (pravdepodobnosti 0 – 1) ---
p_on_time_green: 0.90           # ZELENÁ len ak P(včas) >= 90 %
p_on_time_amber: 0.70           # pod 70 % je ČERVENÁ
p_hold_green: 0.02              # P(neplánovaného zadržania pred výlukou) pre ZELENÚ najviac 2 %
p_hold_red: 0.10                # nad 10 % je ČERVENÁ
p_crew_relief_red: 0.10         # P(prekročenia smeny čaty) nad 10 % je ČERVENÁ
robustness_premium: 0.10        # ZELENÁ varianta má prednosť, ak je drahšia najviac o 10 %
# --- Ekonomika [EUR] – VZOROVÉ hodnoty, nahradiť vlastnými ---
cost_wait_ready_per_h: 180      # pripravený vlak čaká (rušeň, vozne, obsadená koľaj)
cost_running_per_h: 300         # vlak ide alebo stojí na trati s čatou
cost_stabled_per_h: 60          # odstavený vlak (vozne, koľaj), bez čaty
cost_stable_fixed: 600          # jednorazovo: posun na odstavnú koľaj, nová skúška bŕzd, nová trasa
cost_new_path: 0                # poplatok za novú trasu podľa cenníka ŽSR
cost_late_fixed: 1500           # zmluvná pokuta alebo následky nedodržania termínu
cost_late_per_h: 200            # za každú hodinu meškania
cost_unplanned_hold: 800        # neplánované zadržanie vlaku pred výlukou
cost_stuck: 3000                # zadržanie tam, kde nie je kde čakať (blokovanie trate)
cost_crew_relief: 1200          # mimoriadna výmena čaty
# --- Čata [min] ---
crew_max_duty_min: 720          # limit smeny – nastaviť podľa predpisov a kolektívnej zmluvy
crew_prep_min: 60               # príprava pred odchodom (prevzatie rušňa, skúška bŕzd)
crew_relief_delay_min: 120      # zdržanie pri mimoriadnej výmene čaty
# PARAMETRE-KONIEC
```

### 8.2 Pravidlá rezerv
1. **Pred výlukou:** vlak musí úsek s výlukou opustiť najneskôr `buffer_before_closure_min` pred jej začiatkom.
2. **Po výluke:** do úseku smie vstúpiť najskôr `buffer_after_closure_min` po jej *plánovanom* konci. Výluky sa často predlžujú; ak história ukáže väčšie odchýlky, rezervu zvýš.
3. **Vypnuté trakčné vedenie** obmedzuje len elektrickú trakciu; motorový rušeň môže ísť, ak je koľaj prejazdná.
4. **Bod, odkiaľ niet návratu:** vlak sa nepustí za poslednú dopravňu, kde môže bezpečne čakať (dosť dlhá koľaj), ak nie je isté, že úsek s výlukou opustí včas. Inak systém odporučí čakať v tejto dopravni.
5. **Termín zákazníka:** P(včas) musí spĺňať prah a medián príchodu musí byť aspoň `deadline_buffer_min` pred termínom.
6. **Čata:** 90. percentil dĺžky smeny musí byť aspoň `crew_buffer_min` pod limitom.
7. **Trasa:** odchod mimo pridelenej trasy (± `path_tolerance_min`) znamená novú trasu. Tá je možná najskôr o `new_path_lead_min` od teraz a stojí `cost_new_path`. Presmerovanie vždy znamená novú trasu.

### 8.3 Semafor (pre každú variantu odchodu)
- **ČERVENÁ** – nevypravovať: P(včas) < `p_on_time_amber`, alebo P(zadržania pred výlukou) > `p_hold_red`, alebo P(prekročenia smeny) > `p_crew_relief_red`, alebo hrozí zablokovanie trate (pred úsekom s výlukou nie je kde čakať), alebo je porušené tvrdé obmedzenie zo 7.1.
- **ZELENÁ** – odporúčané: P(včas) ≥ `p_on_time_green`, P(zadržania) ≤ `p_hold_green`, rezerva voči termínu ≥ `deadline_buffer_min`, rezerva v smene ≥ `crew_buffer_min`, dáta o polohe a výlukách sú čerstvé.
- **ORANŽOVÁ** – ostatné prípady: len s výslovným potvrdením dispečera; zobraz dôvody.

### 8.4 Rozhodnutie VYPRAVIŤ / POČKAŤ / ODSTAVIŤ / PRESMEROVAŤ / ESKALOVAŤ
1. Vyhodnoť odchody v horizonte `[24]` h po krokoch `[10]` min, po hlavnej trase aj po alternatívach (7.2).
2. Vylúč ČERVENÉ varianty.
3. Pri každom neskoršom odchode porovnaj „stáť pripravený“ a „odstaviť“. Bod zlomu: `T = cost_stable_fixed / (cost_wait_ready_per_h − cost_stabled_per_h)`. Pri dlhšom čakaní je lacnejšie odstaviť (pri vzorových hodnotách T = 5 h).
4. Očakávaný náklad varianty = čakanie alebo odstavenie + jazda (čas × sadzba) + E[pokuta za meškanie] + P(zadržania) × `cost_unplanned_hold` + P(zablokovania) × `cost_stuck` + P(výmeny čaty) × `cost_crew_relief` + poplatky za novú trasu alebo dlhšiu trasu.
5. Vyber variantu s najnižším očakávaným nákladom. Ak je víťaz ORANŽOVÝ a existuje ZELENÁ varianta drahšia najviac o `robustness_premium`, vyber ZELENÚ.
6. Ak neostane nič → **ESKALOVAŤ:** návrh správy pre ŽSR (iná trasa alebo čas) a pre zákazníka (nový termín). Zobraz aj najmenej zlú variantu.
7. **Výstup:** rozhodnutie, okná bezpečného odchodu, tabuľka variantov (odchod, stav, P(včas), P(zadržania), príchod P50/P90, náklad), vysvetlenie a ďalšie kroky (objednať trasu, informovať zákazníka, odstaviť v stanici X).
8. **Prepočet:** pri novej polohe, novom alebo zmenenom e-maile či výluke, zmene pripravenosti vlaku; u idúcich vlakov aspoň každých `[15]` min.

### 8.5 Referenčná implementácia
Ak máš k dispozícii súbor `referencia/rozhodovanie.py` (z repozitára so zadaním), správaj sa ako on: rovnaké pravidlá, rovnaké výsledky na jeho testoch (`referencia/test_rozhodovanie.py`). Tie testy prenes do svojej testovacej sady.

## 9. Dátový model (minimum)
`Location` (dopravný bod; kódy: TAF TSI / číslo ŽSR, súradnice, užitočné dĺžky koľají, možnosť čakania/odstavenia) · `Section` (úsek; atribúty z M3; verzia) · `Closure` (výluka/obmedzenie; M4) · `Order` (objednávka zákazníka) · `Train` · `Consist` (zostava) · `Locomotive` (trakcia, systémy, zabezpečovače, telematika) · `Crew` / `CrewShift` (limity, znalosť tratí) · `Path` a `PathSegment` (pridelená/žiadaná trasa, časy po dopravniach, stav) · `PositionReport` (čas, súradnice, úsek, zdroj, presnosť) · `Email`, `Attachment`, `ExtractedEvent` (s istotou a stavom overenia) · `Evaluation` (variant, metriky, semafor) · `Decision` (odporúčanie, rozhodnutie dispečera, dôvod) · `Alert` · `ParameterSet` (verzia parametrov s platnosťou) · `AuditLog`.

Všetky časy ukladaj v UTC a zobrazuj v časovom pásme Europe/Bratislava. Ošetri prechod na letný a zimný čas (napr. nočná výluka cez zmenu času).

## 10. Nefunkčné požiadavky
- **Bezpečnosť:** prihlasovanie cez firemné SSO `[Microsoft Entra ID / Google]` s MFA, roly (sekcia 4), nemenný audit, tajomstvá v trezore (nie v kóde), šifrovanie, hosting v EÚ, zálohy a test obnovy.
- **GDPR a pracovné právo:** poloha rušňa nepriamo sleduje rušňovodiča. Pred spustením treba posúdenie vplyvu (DPIA) a informovanie zamestnancov a ich zástupcov. Surové GPS dáta uchovávaj `[90]` dní, v analytike pseudonymizuj.
- **Spoľahlivosť:** výpadok jedného zdroja nesmie zastaviť aplikáciu. Pri výpadku zobraz vek dát a nikdy neukazuj zelenú bez čerstvých dát.
- **Výkon:** poloha na mape do 30 s od prijatia; prepočet ETA a semaforu do 5 s; e-mail spracovaný do 2 min od doručenia.
- **Použiteľnosť:** UI v slovenčine (voliteľne v češtine), tmavý režim na nočné smeny, čitateľná mapa, rýchle klávesové skratky, mobilné zobrazenie pre pohotovosť.
- **Prevádzka:** logy, metriky, upozornenie pri výpadku integrácie (e-mail, GPS, ŽSR), nasadenie cez CI/CD, oddelené testovacie a produkčné prostredie.

## 11. Fázy dodávky a akceptačné kritériá

**F0 – Základ: dáta a mapa**
- Model siete SR (import RINF + OSM), mapa, ručné zadanie objednávky, vlaku a výluky, kontrola konfliktov s rezervami.
- *Akceptácia:* na `[5]` reálnych reláciách dopravcu sedí trasa a dĺžka s dokumentmi ŽSR. Vlak, ktorý prechádza úsekom v čase výluky (vrátane rezerv), je označený.

**F1 – E-maily a register výluk**
- Napojenie schránky, klasifikácia a extrakcia, fronta na potvrdenie, register výluk a trás.
- *Akceptácia:* na sade `[50]` anonymizovaných e-mailov je presnosť klasifikácie ≥ 95 % a kľúčové polia (úsek, od – do, číslo vlaku) sú správne v ≥ 90 % prípadov ešte pred potvrdením. Žiadna zmena s istotou pod prahom sa nedostane do registra bez potvrdenia. Testovací e-mail s vloženými „pokynmi“ nespustí žiadnu akciu.

**F2 – Plánovanie a rozhodovanie**
- Trasovanie A → B, varianty, semafor, okná bezpečného odchodu, odporúčania, koncepty správ.
- *Akceptácia:* testy referenčnej implementácie (8.5) prechádzajú. Fiktívny príklad z dokumentácie dá rovnaké odporúčanie. Každé odporúčanie má vysvetlenie.

**F3 – Živá poloha a ETA**
- Integrácia polohy (GPS a/alebo ŽSR / RNE TIS a/alebo aplikácia rušňovodiča), priradenie polohy k úseku (map-matching), ETA P50/P90, upozornenia.
- *Akceptácia:* poloha na mape do 60 s; výpadok dát dlhší ako 15 min je viditeľný; po `[4]` týždňoch pilotu je zmeraná a zverejnená chyba ETA.

**F4 – Učenie a optimalizácia**
- ETA model z histórie, automatická kalibrácia rezerv (s návrhom na schválenie), plánovanie rušňov a čiat (CP-SAT), reporty, voliteľne TAF TSI a portál zákazníka.

## 12. Odporúčané technológie (nie dogma – zdôvodni zmeny)
- **Backend:** Python 3.12, FastAPI, Pydantic; PostgreSQL 16 + PostGIS (+ TimescaleDB pre polohy); fronta úloh (Redis + RQ/Celery); OR-Tools; NetworkX na prototyp (v produkcii vlastná implementácia alebo igraph); LightGBM na ETA.
- **Frontend:** React + TypeScript, MapLibre GL JS; mapový podklad OpenRailwayMap/OSM (dodrž podmienky používania dlaždíc a uvedenie zdroja, prípadne vlastné dlaždice).
- **LLM:** adaptér na poskytovateľa so štruktúrovaným výstupom a spracovaním dát v EÚ `[Anthropic Claude / OpenAI / Azure OpenAI – podľa firemnej politiky]`; sada testovacích e-mailov na meranie presnosti.
- **Infra:** Docker Compose na vývoj, `[cloud v EÚ / vlastný server]` na prevádzku, CI s testami.

## 13. Otázky, ktoré polož na začiatku
1. Presný názov dopravcu a ktoré vlaky a relácie sú v rozsahu (počet vlakov, hraničné priechody, vlečky)?
2. Aký e-mailový systém sa používa (Microsoft 365 / Gmail / iný) a je k dispozícii zdieľaná dispečerská schránka?
3. Môžete poskytnúť 30 – 50 anonymizovaných vzorových e-mailov každého typu (výluky, trasy, zmeny, objednávky) aj s prílohami?
4. Aké interné systémy evidujú objednávky, rušne, čaty a zostavy a ako z nich dostať dáta?
5. Aké rušne sa používajú (vlastné/prenajaté, typy, trakcia) a majú už telematiku alebo GPS?
6. Aké dáta a systémy ŽSR máte k dispozícii (prístupy, TTP, plán výluk, údaje o jazde vlakov, RNE TIS)?
7. Aké sú skutočné náklady (rušeň, čata, vozne) a zmluvné pokuty zákazníkov?
8. Kde má aplikácia bežať (cloud v EÚ / vlastný server) a aké je firemné prihlasovanie (SSO)?
9. Kto schvaľuje rozhodnutia a ktoré upozornenia majú ísť cez SMS/Teams?
10. Aký je rozpočet a termín pilotu?

## 14. Výstupy
Repozitár s kódom, testami a Dockerom; `README.md` (spustenie); `ARCHITEKTURA.md`; dokumentácia importov dát; používateľská príručka pre dispečera (SK); správa o presnosti extrakcie e-mailov; dokumentácia rozhodovacích pravidiel a parametrov; zoznam otvorených otázok a rizík.
