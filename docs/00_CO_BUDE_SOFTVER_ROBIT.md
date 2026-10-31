# Čo bude softvér robiť – celý popis od začiatku

Jednoduchý popis bez technických detailov. Podrobné zadanie pre vývoj je v `PROMPT.md`.

## V skratke

Softvér je **pravá ruka dispečera**. Sleduje e-maily, výluky, vlaky na mape Slovenska, preberanie na hraniciach a pracovný čas rušňovodičov. Pri každom vlaku stále prepočítava, či stihne termín, a odporučí, čo urobiť: **vypraviť, počkať, odstaviť, presmerovať** alebo **eskalovať** (dohodnúť sa so ŽSR a zákazníkom). Rozhoduje vždy dispečer. Softvér navrhuje, upozorňuje, vysvetľuje a pripravuje podklady.

## Čo uvidí dispečer na obrazovke

1. **Živá mapa Slovenska** – celá sieť ŽSR, na nej každý náš vlak ako ikona s číslom priamo na trati. Farba ukazuje stav (zelená / oranžová / červená). Na mape sú aj výluky (červené úseky), pomalé jazdy (oranžové) a **hraničné priechody, kde sa vlaky preberajú** – Kúty, Rajka, Štúrovo, Čadca, Čierna nad Tisou a ďalšie. Vlaky, ktoré ešte len idú k hranici, čakajú pri svojej preberacej stanici s časom príchodu.
2. **Po kliknutí na vlak:** číslo vlaku, zákazník, odkiaľ – kam, rušeň, rušňovodič a **koľko mu zostáva z 13 hodín pracovného času**, meškanie, kedy príde (bežne aj v horšom prípade), najbližšie riziko a odkiaľ je posledná poloha (GPS, ŽSR…) a aká je stará.
3. **Tabuľka vlakov** zoradená podľa naliehavosti – navrchu tie, ktoré potrebujú rozhodnutie.
4. **Časová os** – vlaky, výluky a preberania na jednej osi; konflikty sú zvýraznené.
5. **Upozornenia** – v aplikácii, a tie kritické aj cez SMS alebo Teams.
6. **Fronta na potvrdenie** – čo softvér vyčítal z e-mailov a čo treba skontrolovať jedným klikom.

## Celý príbeh vlaku – čo softvér robí krok za krokom

### A. Keď príde objednávka (dni až týždne vopred)
1. **Rozpozná objednávku** zákazníka v e-maile (alebo ju dispečer zadá) a založí zásielku: odkiaľ, kam, odkedy, do kedy, tovar (aj nebezpečný), počet vozňov, hmotnosť, dĺžka.
2. **Určí, kde vlak vstúpi na Slovensko** a kde sa preberá (napr. Čadca), a kde Slovensko opustí alebo kde končí (stanica, vlečka zákazníka).
3. **Navrhne niekoľko variantov prepravy** cez Slovensko: kadiaľ, kedy, aké trasy treba objednať u ŽSR, ktorý rušeň a rušňovodič, kde sa vymení rušňovodič, aké výluky sú po ceste, aké sú rezervy, náklady a aká je šanca, že termín vyjde.
4. **Pripraví koncept objednávky trasy** pre ŽSR a koncept potvrdenia termínu pre zákazníka. Odošle ich dispečer.
5. **Postráži odpoveď ŽSR** v e-mailoch (trasa pridelená, zmenená, odmietnutá) a priradí ju k vlaku.

### B. Nepretržite, 24 hodín denne
6. **Číta e-maily:** výluky, pomalé jazdy, zmeny a zrušenia trás, mimoriadnosti na trati, **avíza partnerov, kedy vlak príde na hranicu**, objednávky. Vedie z nich prehľadný register. Čo je dôležité alebo neisté, dá dispečerovi na potvrdenie.
7. **Po každej zmene prepočíta všetky vlaky**, ktorých sa týka, a povie, čo s tým robiť. Napríklad (vymyslený príklad): *„Nová výluka Kraľovany – Ružomberok zajtra 08:00 – 14:00 sa týka vlakov 1, 2 a 3; vlak 2 treba vypraviť do 05:40 alebo až po 13:30.“*
8. **Ukazuje vlaky na živej mape** (pozri vyššie).

### C. Pred príchodom vlaku na hranicu (preberanie)
9. **Sleduje vlaky na ceste k slovenskej hranici** (podľa avíza partnera alebo údajov zahraničnej siete) a počíta, kedy prídu do preberacej stanice.
10. **Skontroluje, či je všetko pripravené na prevzatie:** rušeň a rušňovodič budú na mieste včas, rušňovodič má dosť pracovného času na celú jazdu, ŽSR pridelila trasu, sú pripravené nákladné listy a je čas na technickú prehliadku a skúšku bŕzd.
11. **Keď vlak mešká alebo príde skôr,** prepočíta plán a navrhne úpravu: neskorší nástup rušňovodiča, novú trasu, čakanie. Napríklad (vymyslený príklad): *„Vlak príde do Čadce o 2 h neskôr. Rušňovodič by čakal a na jazdu do Košíc by mu nezostal pracovný čas → posunúť nástup na 14:00 a objednať novú trasu.“*

### D. Počas jazdy po Slovensku
12. **Zaznamená prevzatie** (čas, zistené závady, zdržanie) a **pred odchodom vyhodnotí semafor:**
    - **VYPRAVIŤ** – všetko je v poriadku s rezervou,
    - **POČKAŤ** – lepšie je vyraziť neskôr (napr. po výluke), čakanie je krátke,
    - **ODSTAVIŤ** – čakanie by bolo dlhé, lacnejšie je odstaviť a ísť neskôr po novej trase,
    - **PRESMEROVAŤ** – iná trasa je spoľahlivejšia,
    - **ESKALOVAŤ** – nič nie je bezpečné, treba sa dohodnúť so ŽSR a zákazníkom.

    Každé odporúčanie má vysvetlenie a časové okná, napr. *„vypraviť najneskôr o 05:40, inak až od 12:30“*.
13. **Stráži pracovný čas rušňovodiča (limit 13 hodín).** Počíta odpracovaný čas a predpokladaný koniec jazdy aj s prípravou a odovzdaním rušňa. Včas upozorní a navrhne výmenu: *„Rušňovodičovi zostáva 3 h, do cieľa v horšom prípade 2 h 50 min + odovzdanie rušňa → pripraviť výmenu, vystriedať najneskôr o 16:30 v Žiline.“* Nikdy neodporučí vypraviť vlak, pri ktorom by rušňovodič limit prekročil.
14. **Priebežne prepočítava, kedy vlak príde,** a upozorní, keď klesá rezerva, hrozí výluka, vlak neplánovane stojí alebo chýbajú údaje o polohe.
15. **Pripraví správy** pre zákazníka (kedy príde, mešká) a pre ŽSR (zmena trasy) na odoslanie jedným klikom.

### E. Na konci jazdy a po nej
16. **Zaznamená odovzdanie** vlaku na výstupnej hranici, v cieľovej stanici alebo na vlečke a doručenie zásielky.
17. **Uloží, ako to naozaj prebehlo** (plán verzus skutočnosť, príčiny meškaní) a z toho sa učí: spresňuje rezervy, odhady príchodu a doby preberania na jednotlivých hraniciach.
18. **Pripraví reporty:** dochvíľnosť, meškania podľa príčin, doby preberania na hraniciach, pracovný čas rušňovodičov, využitie rušňov.

### F. Pre dispečing ako tím
19. **Odovzdávka služby:** na konci služby pripraví prehľad pre ďalšieho dispečera (čo ide, čo hrozí, ktorým rušňovodičom končí čas, nové výluky, nepotvrdené e-maily, otvorené úlohy).
20. **Všetko je dohľadateľné** (kto, kedy, čo rozhodol a prečo) a **nastaviteľné** (rezervy, limity, náklady, kontakty, šablóny správ).

## Čo softvér nerobí
- Neriadi vlaky a nezasahuje do zabezpečovacích zariadení – jazdu vlakov riadi ŽSR.
- Bez potvrdenia dispečera nič neodošle ŽSR, zákazníkom ani rušňovodičom.
- Nevymýšľa si údaje. Keď niečo chýba alebo je neisté, opýta sa.

## Čo bude potrebovať
Zoznam podkladov je v `docs/01_CO_POTREBUJEM.md`. Zdroje polohy vlakov (GPS v rušňoch, údaje od ŽSR, aplikácia pre rušňovodičov) a ďalšie kroky zavedenia sú v `docs/02_ZAVEDENIE_SYSTEMU.md`. Ako sa rozhoduje a s akými rezervami, je v `docs/03_REZERVY_A_ROZHODOVANIE.md`.
