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
- **Kliknutím na vlak** uvidíte zákazníka, reláciu, rušeň, rušňovodiča (rovnako ako v odporúčaní: toho v službe, inak toho, ktorého naplánovaná zmena je na rade, inak najbližšieho naplánovaného) a zostávajúci pracovný čas do 13 h, meškanie, príchod (bežne a v horšom prípade), najbližšie riziko a odkiaľ je posledná poloha a aká je stará.
- **Výber vlaku v zozname** (alebo vyhľadanie čísla) mapu priblíži a vlak dá do stredu. Okno s detailom je celé viditeľné: na širokej obrazovke vpravo od vlaku, na úzkej (mobil) pod ním a jeho obsah sa dá posúvať. Na mobile sa stránka posunie k mape.
- **Sledovať vlak:** mapa ide s vlakom. **Stopa 24 h:** kade vlak išiel.
- **Tabuľka vpravo** je zoradená podľa naliehavosti, navrchu ČERVENÉ. Pod ňou sú upozornenia a časová os (vlaky a výluky na 24 h).
- **Na veľký monitor:** celá obrazovka, nočný režim.
- **Bez čerstvých údajov agenta** (schránka sa 30 min úspešne neskontrolovala, agent ešte dočítava nahromadenú poštu – „schránka agenta sa dočítava (posledná úplná kontrola pred N min)“ – alebo agent neprebehol 2 × `VLAKY_REEVALUATE_MINUTES`) mapa, tabuľka, zoznam vlakov, detail vlaku, upozornenia na nástenke, detail odporúčania (aj história odporúčaní a varianty) ani `/api/vlaky` neukážu ZELENÚ: posledné zelené odporúčanie sa zobrazí ako ORANŽOVÁ s poznámkou „bez čerstvých údajov agenta – pôvodne ZELENÁ“ a s vekom údajov (v API polia `downgraded`, `data_age`, `data_note`). Staršie odporúčania v histórii si farbu ponechajú. Uložené odporúčanie sa nemení.

## 4. Fronta na potvrdenie

Agent potrebuje vaše potvrdenie, keď si nie je istý, keď zmena ovplyvní vlak v najbližších 72 h alebo keď ide o zrušenie (výluky, trasy) či mimoriadnosť.

1. Vľavo vidíte **pôvodný e-mail**, vpravo **čo z neho agent vytiahol**: úsek, časy, druh výluky, vlak.
2. **Potvrdiť** – údaje sú správne. Agent ich zapíše a prepočíta vlaky.
3. **Upraviť a potvrdiť** – opravte pole (napr. čas alebo stanicu) a potvrďte. Oprava sa uloží, aby sa agent zlepšoval.
4. **Zamietnuť** – s dôvodom. Agent vráti späť, čo už zapísal.

- **Úpravy v registri výluk sa zachovajú.** Ak ste výluku z e-mailu, ktorá čaká na potvrdenie (aj dočasný záznam navrhovanej zmeny alebo automaticky zapísanú výluku), upravili v registri výluk tlačidlom **Upraviť** (koniec, koľaj, druh …), potvrdenie – vo fronte aj tlačidlom **Potvrdiť** v registri – ponechá vaše hodnoty, nie údaje z e-mailu. Fronta ich pri udalosti ukazuje ako „údaj z e-mailu → hodnota v registri“ a hláška po potvrdení uvedie ponechané polia. Výslovná oprava toho istého poľa vo fronte (**Upraviť a potvrdiť**) má prednosť.
- **Oprava čísla výluky alebo druhu udalosti:** agent potom výluku v registri hľadá znova podľa opravených údajov. Zmena sa zapíše do výluky s opraveným alebo doplneným číslom (iná výluka sa nezmení). Zmena preklasifikovaná na novú výluku je ďalší termín a oprava na pomalú jazdu založí pomalú jazdu – pôvodná výluka v oboch prípadoch ostane. Oprava na „zrušenie výluky“ zruší výluku v registri.
- **Skončenie mimoriadnosti:** e-mail ŽSR o skončení mimoriadnosti („prevádzka je obnovená“) je vo fronte ako „iné“. Nič sa z neho samo nezapíše ani nezruší, aby sa omylom nezrušila iná výluka na tom istom úseku. Mimoriadnosť ukončite v registri výluk (Upraviť – koniec, alebo Zrušiť) a udalosť potvrďte alebo zamietnite. Obmedzenie rýchlosti po obnovení prevádzky príde ako samostatná pomalá jazda. Zrušenie pomalej jazdy uvedené v e-maile s novou výlukou je samostatná udalosť „zrušenie výluky“ s číslom rušenej pomalej jazdy.
- **„potvrdené – zápis zlyhal“:** ak sa potvrdená udalosť nedá zapísať (napr. avízo pre vlak, ktorý ešte nie je v systéme), uvidíte chybu a udalosť ostane vo fronte s týmto štítkom. Po odstránení príčiny (napr. založení vlaku) ju potvrďte znova alebo zamietnite. Pri avíze pre vlak, ktorý v systéme nie je, hláška povie dátum jazdy na založenie (odkaz **Založiť vlak**). Ak vlak založíte s prázdnym dátumom jazdy, avízo dostane, keď je jediným aktívnym vlakom s týmto číslom – fronta na to upozorní a takéto avízo vždy potvrdzujete vy. Ukončenému ani odovzdanému vlaku sa avízo nepriradí: pri minulotýždňovej jazde s rovnakým číslom založte nový vlak pre túto jazdu, nemeňte dátum ukončeného vlaku. Pri inom dátume jazdy alebo viacerých vlakoch bez dátumu hláška povie, čo opraviť.
- **Udalosti, s ktorými plánovač nevie počítať** (zrušenie či zmena pridelenej trasy, výluka s neprečítaným termínom alebo z podozrivého e-mailu): agent vám hneď pošle upozornenie DÔLEŽITÉ s odkazom do fronty a vlaky, ktorých sa môžu týkať, nebudú ZELENÉ, kým udalosť nepotvrdíte (s opravou, napr. termínu – **Upraviť a potvrdiť**) alebo nezamietnete.
- **Nahradená udalosť:** ak potvrdíte staršiu udalosť, ktorej záznam medzitým zmenila (napr. zrušila) novšia udalosť, nič sa nezapíše a hláška to povie („Udalosť potvrdená, ale nič sa nezapísalo“) – novšia správa ŽSR má prednosť (napr. zmena výluky, ktorú ŽSR medzitým zrušila; fronta pri nej upozorní).

E-maily od nepovolených odosielateľov sú v **karanténe**: nespracujú sa a uvidíte ich v súhrne. E-mail od overeného odosielateľa ŽSR, ktorý cituje alebo prikladá správu iného odosielateľa (napr. susednej správy železníc alebo interný ROV), do karantény nejde: všetky jeho udalosti idú na potvrdenie a nič sa z neho nezapíše automaticky. Preposlaný e-mail od iného odosielateľa ide do karantény, ak pôvodný odosielateľ nie je povolený a overený. **Podozrivé e-maily** (napríklad text typu „ignoruj pokyny“) sa nikdy nepoužijú automaticky.

## 5. Odporúčania

Každé odporúčanie obsahuje:

- čo sa zmenilo a čoho sa to týka,
- **rozhodnutie** (VYPRAVIŤ, POČKAŤ, ODSTAVIŤ, PRESMEROVAŤ, ESKALOVAŤ, pri idúcom vlaku POKRAČOVAŤ) s časom,
- **návrh trasy:** dopravne s časmi príchodu a odchodu a okno na objednanie ad hoc trasy,
- **okná bezpečného odchodu**, napr. „vypraviť najneskôr o 05:40, inak až od 12:30“,
- **rušňovodiča:** nástup, limit 13 h, „vystriedať najneskôr o …“ (pri idúcom vlaku bez záznamu o nástupe len odhad – kapitola 9), prípadne návrh posunúť nástup,
- **porovnanie variantov** a dôvody,
- **koncepty správ:** objednávka ad hoc trasy pre ŽSR (IS KIS, produkt P5) a informácia pre zákazníka. Skopírujte ich, doplňte a odošlite sami,
- **kontrolný zoznam pred odchodom:** súpis vlaku a správa o zostave v KIS, pripravenosť vlaku, skúška bŕzd, doklady.

**Čo s odporúčaním urobíte:** **Prijať**, **Upraviť** (s poznámkou) alebo **Zamietnuť** (s dôvodom). Dá sa to aj **odpoveďou na e-mail agenta**: prvý riadok `PRIJAŤ` alebo `ZAMIETNUŤ dôvod`. Platí to len z vašej firemnej adresy, ktorá je na zozname dispečerov. Príkaz musí byť celé prvé slovo prvého riadku (`PRIJAŤ`, `Prijať.`, `ZAMIETNUŤ: dôvod`) – veta, ktorá sa ním len začína (napr. „Prijateľnejšie by bolo…“), príkazom nie je. Rozhoduje sa len o aktuálnom odporúčaní vlaku: ak medzitým prišlo novšie, odpoveď sa nezapíše a agent vám pošle odkaz na aktuálne. Ak sa odpoveď nezapíše (aj zamietnutie bez dôvodu), agent vám krátko odpíše prečo. Automatické odpovede (neprítomnosť v kancelárii, potvrdenie o prečítaní) agent nespracúva a neodpovedá na ne.

**Prijaté (aj upravené) odporúčanie je vaše rozhodnutie** a agent ho drží, kým neprijmete iné, aj keď medzitým príde iné odporúčanie (napr. ORANŽOVÁ pri výpadku schránky): prijaté odstavenie neprepne na POČKAŤ len preto, že čas plynie, a novú trasu z prijatého odporúčania (objednanú aspoň 6 h vopred) ponúka ďalej aj v lehote 6 h – kým jej čas neuplynie a kým ŽSR nepridelí trasu (potom sa riadi pridelenou trasou, aj keď má iný čas). Koncept objednávky ad hoc trasy uvidíte len pri odchode, ktorý novú trasu potrebuje.

**Dôležité pravidlá ŽSR, s ktorými agent počíta:**

- ad hoc trasu (P5) objednať aspoň **6 h** pred odchodom; neskôr len výnimočne cez dispečera ŽSR,
- vlak, ktorý neodíde do **18 h**, ŽSR zruší – agent ráta 18 h od začiatku státia na koľajach ŽSR (nie od posledného prepočtu, pozri kapitolu 7) a neskorší odchod už neponúkne; ak limit uplynie skôr, ako bude vlak pripravený, odporučí len **ESKALOVAŤ**,
- odstavenie je najviac 18 h a koľaj určí ŽSR; ak vlak po odstavení pôjde na svoju pridelenú trasu, odporúčanie znie „ODSTAVIŤ …, vypraviť na pridelenú trasu o …“ – trasu neodrieknite; „pôvodnú trasu odrieknuť, nová trasa na …“ len vtedy, keď odchod potrebuje novú trasu (vlak bez trasy: „objednať trasu na …“); odstavenie idúceho vlaku znamená novú trasu (aspoň 6 h vopred),
- preberanie na hranici sa plánuje s dlhšou dobou preberania (P90), aby plán vyšiel aj pri pomalšom preberaní.

**Kedy agent nedá ZELENÚ** (dôvod je vždy vypísaný v odporúčaní):

- výluka na trase ešte nie je potvrdená (počíta sa s ňou),
- výluku sa nepodarilo umiestniť na sieť (skontrolujte názvy dopravní vo výluke),
- zrušenie alebo zmena pridelenej trasy vlaku (napr. z KIS) čaká vo fronte na potvrdenie, alebo výluka z e-mailu na trase vlaku čaká na potvrdenie a agent jej údaje nevedel použiť, napr. termín sa nepodarilo prečítať (kapitola 4, „Udalosti, s ktorými plánovač nevie počítať“),
- chýbajú čerstvé údaje o polohe alebo je poloha mimo trasy,
- pri idúcom vlaku chýba záznam o nástupe rušňovodiča,
- agent dlhšie ako 30 min úspešne neskontroloval svoju schránku, alebo ju po výpadku ešte dočítava (nahromadená pošta – „Agent ešte dočítava nahromadenú poštu (posledná úplná kontrola schránky pred X min)“): kým ju nedočíta, ZELENÁ nie je; zmenu na ORANŽOVÚ len pre dočítavanie vám neposiela, po dočítaní vlaky hneď prepočíta a novú výluku oznámi hneď,
- vlak zo zahraničia ešte neprišiel na hranicu: avízo uplynulo pred viac ako 30 min (alebo chýba) a príchod nie je zapísaný – zapíšte príchod alebo nové avízo (kapitola 8),
- odchod teraz na objednanú trasu, ktorú ŽSR ešte nepridelila (nie je zapísaná ako pridelená) – overte pridelenie v IS KIS.

Rovnaké poistky (schránka, výluka mimo siete, čakajúca výluka z e-mailu na trase) platia aj pre návrh prepravy k objednávke.

Keď agent obmedzí semafor alebo web nemá čerstvé údaje agenta, varianty v porovnaní majú najviac farbu odporúčania. Okná odchodu sú nadpísané „Okná odchodu podľa výluk – len informatívne“ aj s dôvodom a nie sú odporúčaním na odchod. Detail aktuálneho odporúčania ukazuje zostávajúci pracovný čas rušňovodiča, jeho farbu a vek polohy aj schránky k aktuálnemu času (hodnota pri prepočte je v zátvorke). Pri neaktuálnych údajoch agenta ukáže upozornenie pri každej farbe.

**Vlak v cieli:** keď je idúci vlak v cieľovej stanici (poloha v obvode stanice alebo na konci trasy, hlásenie ŽSR/TIS o príchode), agent odporučí POKRAČOVAŤ „vlak je v cieli (…, príchod …) – ukončite jazdu“ a vlak už neprepočítava (žiadne výluky za ním ani nová trasa) – nastavte vlaku stav „ukončený“. Ak vlak nemá aktuálnu polohu a podľa odchodu (alebo poslednej polohy) a jazdnej doby už mal byť v cieli, agent napíše „podľa plánu by už mal byť v cieli … – overte polohu“ (ORANŽOVÁ): overte u rušňovodiča alebo ŽSR, kde vlak je. Vlak čakajúci pred vjazdom do cieľa ešte nie je v cieli – rušňovodič je v službe. Pri polohe staršej ako 15 min počíta agent s poslednou známou polohou – vlak nie je ďalej, ako bol naposledy.

**Keď sa výpočet nepodarí:** vlak dostane odporúčanie **ESKALOVAŤ** s popisom chyby – staré odporúčanie vtedy neplatí, posúďte vlak sami a chybu nahláste správcovi.

**Kedy príde správa:** pri každej zmene odporúčania oproti tomu, čo ste o vlaku naposledy dostali – aj pri návrate k skoršiemu odporúčaniu (napr. výluka zrušená a potom znova platná) a pri opätovnej eskalácii (aj SMS). Rovnaká správa sa neopakuje. Odporúčanie s odchodom „čo najskôr“ (vlak v jazde, objednať novú trasu, eskalácia) sa pri ďalších prepočtoch len aktualizuje; nová správa príde až pri zmene situácie (napr. iné rozhodnutie, farba, stav rušňovodiča alebo posun príchodu idúceho vlaku o 30 min a viac). Ak e-mail, Teams alebo SMS nefungujú, agent správu pošle znova, keď sa spojenie obnoví (KRITICKÉ do 5 minút, ostatné najneskôr do hodiny); medzitým sú odporúčania na webe. Ak SMS brána odmietne jedno číslo, ostatní dispečeri SMS dostanú normálne.

## 6. Výluky

- Väčšinu výluk zapíše agent z e-mailov (ROV, SROV, „Povolené výluky“, telegramy, POTR).
- **Čo príde telefonicky alebo cez WhatsApp od kontrolného dispečera ŽSR, musíte zadať sami:** stránka **Výluky → Nová výluka** (úsek od – do, druh, čas, opakovanie, len trakčné vedenie, rýchlosť). Prípadne to prepošlite e-mailom do schránky agenta.
- **Výluka alebo mimoriadnosť v jednej dopravni:** do polí „Od dopravne“ aj „Po dopravňu“ zadajte tú istú dopravňu (napr. telefonicky nahlásená porucha výhybky v stanici). Platí pre všetky koľaje v obvode dopravne vrátane hlavných koľají, ktoré stanicou prechádzajú, pri odbočke len pre jej výhybky. Vlaky, ktoré dopravňu už prešli, sa jej netýkajú.
- **Číslo trate** vyplňte, ak ho poznáte (z e-mailu ŽSR sa preberie z riadku „Trať:“). Pri súbežných tratiach rôzneho rozchodu (ŠRT popri trati 190) určí, ktorej trate sa výluka týka. Výluku na trati, po ktorej vlak ide, nikdy nevypne.
- **Pomalá jazda a výluka jednej koľaje** sa vlaku započítajú len vtedy, keď ide úsekom počas ich platnosti – aj pri nočnej alebo denne opakovanej výluke.
- Výluka „NEPOTVRDENÁ“ sa do výpočtov počíta, aby bol výpočet na strane bezpečnosti. Potvrďte ju, keď ju overíte.
- **Výluka z e-mailu, ktorá čaká na potvrdenie** (stav „návrh“): vaše úpravy v registri sa pri potvrdení zachovajú (kapitola 4). Keď ju v registri zrušíte, agent zároveň zamietne jej udalosť vo fronte (dôvod „zrušené v registri výluk“) – neskoršie potvrdenie ju už znova nezapíše.
- **Výluka „do odvolania“** má technický koniec, ktorý agent posúva, kým ju ŽSR neodvolá (formulár to pripomína). Keď vo formulári úpravy koniec zmeníte, výluka bude platiť len do neho a agent ho už neposúva; keď koniec nezmeníte, ostane „do odvolania“ – aj keď agent medzitým technický koniec posunul.
- **Súbežná úprava:** ak výluku, ktorú práve upravujete, medzitým zmenil niekto iný (potvrdená zmena z e-mailu, overenie, iný dispečer), uloženie sa odmietne – formulár ukáže aktuálne údaje a vaše neuložené zadanie, aby sa potvrdená zmena potichu nevrátila. Úpravy zadajte znova. Posun technického konca výluky „do odvolania“ agentom otvorený formulár nezneplatní.

## 7. Vlaky a objednávky

- **Objednávky** zákazníkov prichádzajú e-mailom. Agent k nim pošle **návrh prepravy**: trasy, časy, okno na objednanie trasy. Ak pre objednávku opakovane nedokáže pripraviť návrh (chyba výpočtu), po troch pokusoch vám pošle upozornenie a objednávka bude v stave „chyba plánu“ – naplánujte ju bez agenta a chybu nahláste správcovi. Ostatné objednávky sa spracujú normálne.
- **Vlak z objednávky** vytvoríte jedným tlačidlom, alebo vlak zadáte ručne: odkiaľ, kam, vstupný hraničný priechod, dĺžka, hmotnosť, trakcia, termín, pripravenosť (miestny čas). Vlak zadaný bez dátumu jazdy patrí najbližšej jazde – použije sa trasa s jeho číslom z e-mailu pre tento deň.
- **Limit 18 h státia (PPŽS)** sa ráta od začiatku státia na koľajach ŽSR: od príchodu na hranicu (kapitola 8), od zmeškanej trasy, na ktorej vlak stál pripravený, alebo od odstavenia. Úprava času pripravenosti, dátumu jazdy či stavu vo formulári vlaku ho neposunie (začiatok státia sa pri uložení zapíše). Skončí sa až odchodom vlaku (stav „v jazde“ alebo hlásenie rušňovodiča „odchod“; omylom zaznamenaný odchod opravíte podľa odrážky „Omylom zaznamenaný odchod“ nižšie).
- **Prepočítať** spustí okamžité vyhodnotenie vlaku.
- **Odkaz pre rušňovodiča** vytvorí odkaz do mobilu, cez ktorý rušňovodič hlási nástup, pripravenosť, odchod a problém a posiela polohu. Odkazy sa dajú zrušiť. Nástup sa dá z odkazu zaznamenať len raz (začiatok bežiacej zmeny môže zmeniť iba dispečer); ktorú zmenu nástup začne, je v kapitole 9. Hlásenia stavu (nástup, pripravený, odchod) a hlásenia o probléme majú každé vlastný limit 6 za 10 min – opakované ani odmietnuté hlásenia stavu limit pre problém neminú. Upozornenie na problém príde najviac raz za 10 min (ďalšie hlásenia sú v detaile vlaku a v audite). Ak sa upozornenie nepodarí doručiť (výpadok e-mailu či Teams), agent ho pošle znova po obnovení (s časom nahlásenia). Rušňovodič v aplikácii vtedy neuvidí „odoslané“, ale varovanie „Hlásenie je uložené, ale upozornenie dispečerovi sa teraz nepodarilo odoslať – agent ho zopakuje. Ak ide o niečo naliehavé, zavolajte dispečerovi.“
  - Po každom hlásení stavu sa vlak hneď prepočíta (po hlásení o probléme najviac raz za 30 s). Už zaznamenaný stav aplikácia ukáže ako „Už zaznamenané: …“, nie ako chybu.
  - Hlásenie „odchod“ zapíše skutočný čas odchodu a ukončí státie; z neho vychádza aj odhad pracovného času rušňovodiča bez záznamu o nástupe (kapitola 9). Hlásenie „pripravený“ nezmení, odkedy vlak stojí: neposunie limit 18 h odstaveného ani prevzatého vlaku, ani vlaku, ktorý čaká od zmeškanej trasy, a nezmení jeho minulý čas pripravenosti – limit beží ďalej od skutočného začiatku státia a hlásenie ostane v audite.
  - **Omylom zaznamenaný odchod:** aplikácia rušňovodiča pred odoslaním „Odchod“ žiada potvrdenie (omylom ťuknutý odchod by ukončil státie). Ak vlak v skutočnosti nevyšiel, vráťte vo formulári vlaku stav na „odstavený“ alebo „prevzatý“ a v poli „išiel vlak naozaj?“ zvoľte „odchod bol omyl – vlak nevyšiel“ – limit 18 h beží ďalej od pôvodného začiatku státia; voľba „vlak išiel“ začne nové státie. Bez voľby rozhodnú polohy (pohyb viac ako 2 km = jazda, polohy ukazujú státie = omyl), inak čas: do 30 min od odchodu sa návrat považuje za omyl, neskôr za skutočnú jazdu. Správa po uložení vždy povie, ktoré pravidlo agent použil („Odchod sa považuje za omyl …“ alebo „Vlak sa považuje za odídený (v jazde od …) – státie a limit 18 h začínajú …“). Ak vlak označíte „v jazde“ vo webe, agent odhaduje jeho polohu od času tejto zmeny (vlak išiel najneskôr vtedy).
  - Odmietnuté požiadavky z odkazu (prekročený limit, nereálny skok polohy, opakovaný nástup) sa do auditu zapíšu najviac raz za 10 min na vlak a druh, s počtom nezapísaných („nezapisane“). Detail vlaku vždy ukáže posledných 10 hlásení o probléme a posledné odmietnutie každého druhu z posledných 7 dní, aj keď ich z 30 posledných záznamov vytlačili iné.
  - **Poloha z mobilu:** ak hodiny mobilu nesedia s časom servera o viac ako 30 s, aplikácia čas polohy prepočíta podľa servera a ukáže „Hodiny mobilu sa líšia od servera o N min – čas polohy sa opravuje.“ (odporučte zapnúť automatický dátum a čas). Keď GPS nedáva novú polohu (tunel, slabý signál), aplikácia starú polohu znova neposiela a ukáže „Čakám na novú polohu z GPS (posledná zistená o …)“. Polohu s časom v budúcnosti server uloží s časom prijatia; polohu staršiu ako posledná uložená neuloží („Poloha je staršia ako posledná uložená.“) – stopa sa spätne neprepisuje.

## 8. Hraničné priechody a preberanie

- 23 priechodov s preberacou stanicou (napr. Rusovce – **Rajka** (HU), Čadca, Kúty, Štúrovo, Čierna nad Tisou).
- **Avízo** príchodu vlaku na hranicu (z e-mailu partnera alebo ručne): agent z neho prepočíta pripravenosť vlaku (avízo + doba preberania P90). Ak avízo uplynulo o viac ako 30 min a príchod nie je zapísaný, vlak nie je ZELENÝ: bez nového času pripravenosti agent počíta s príchodom najskôr teraz („Vlak ešte neprišiel na hranicu … meškanie … min“) a povie, či vlak stihne pridelenú trasu; ak rušňovodič nahlásil „pripravený“ (alebo ste po avíze zapísali čas pripravenosti), počíta s ním a pridelenú trasu nezahodí („Príchod na hranicu … nie je zapísaný“). Zapíšte príchod a prevzatie (tlačidlá nižšie) alebo nové avízo. Vlak, ktorému vo formulári nastavíte stav „prevzatý“ alebo „odstavený“, agent berie ako vlak na koľajach ŽSR, aj keď preberanie ostalo nezapísané.
- **Tlačidlá:** **Prišiel na hranicu**, **Prevzatý** (čas a zistenia). Z týchto časov sa neskôr spresnia doby preberania.
- **Začiatok státia:** zaznamenaný príchod vlaku na hranicu je začiatkom státia na koľajach ŽSR (limit 18 h, kapitola 7); zaznamenanie prevzatia ho neposúva. Čas príchodu opravíte opätovným zaznamenaním príchodu, kým vlak nie je prevzatý.

## 9. Rušňovodiči (limit 13 h)

- Pri každom rušňovodičovi beží **odpočet do 13 h** (od nástupu po odovzdanie rušňa). Farby: zelená, oranžová (menej ako 60 min rezervy) a červená.
- **Upozornenia** prídu 90, 60 a 30 min pred limitom a pri prekročení.
- **Nástup z mobilu:** „Nástup na zmenu“ v aplikácii rušňovodiča začne zmenu, ktorá je na rade (naplánovaný nástup najneskôr o 30 min, pred odchodom vlaku inak najbližšiu naplánovanú), nie neskoršie vystriedanie. Vystriedanie začne rušňovodič svojím nástupom (najskôr 30 min pred naplánovaným) – zmena predchádzajúceho rušňovodiča sa ukončí. Bez naplánovanej zmeny sa nástup nestratí: vznikne zmena „neznámy (nástup z mobilu)“ s nahláseným časom – doplňte v nej meno rušňovodiča (Rušňovodiči → Upraviť). Hlavička aplikácie po hlásení ukáže zaznamenaný nástup. Ak rušňovodič ťukne „Nástup“ až keď vlak ide, limit 13 h (aj upozornenia 90/60/30 min) sa ráta najneskôr od odchodu mínus príprava – aplikácia mu to oznámi; ak išlo o vystriedanie počas jazdy, upravte začiatok zmeny.
- **Nástup nie je zadaný:** ak vlak ide a zmena rušňovodiča je len plánovaná (nástup nebol zaznamenaný), agent v odporúčaní aj v stĺpci „Vystriedať najneskôr“ uvádza len odhad „odhad: vystriedať najneskôr o … (nástup rušňovodiča nie je zadaný)“ – nástup odhaduje ako odchod vlaku (skutočný, inak plánovaný alebo podľa pridelenej trasy, ak nie je neskorší ako chvíľa, keď ste vlak označili „v jazde“, inak táto chvíľa) mínus príprava, predvolene 1 h; odhad sa s časom neposúva. Keď odchod nepozná, uvedie „limit 13 h sa nedá overiť – zadajte nástup rušňovodiča“. Zadajte zmenu rušňovodiča so skutočným nástupom.
- **Vlaky v jazde bez záznamu o nástupe:** stránka Rušňovodiči ich vypíše v samostatnej tabuľke s textom ako v odporúčaní („odhad: vystriedať najneskôr o …“ alebo „limit 13 h sa nedá overiť – zadajte nástup rušňovodiča“) a s formulárom „Zadať nástup“; návrh nasadenia (24 h) rieši len vlaky pred odchodom. Rušňovodiča zmeny môžete zmeniť v „Upraviť“.
- **Zaznamenaná zmena s nástupom až po odchode** vlaku mínus príprava sa nevynechá, ale vlak nie je ZELENÝ (aspoň ORANŽOVÁ) – agent navrhne posunúť nástup alebo zvoliť neskorší odchod.
- **Naplánované vystriedanie** (zmena s neskorším nástupom) nezakryje zmenu, ktorá prebieha, ani naplánovanú zmenu, ktorá je na rade: limit sa stráži pre rušňovodiča v službe a riadok vystriedania ukazuje jeho vlastný limit.
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
- rušňovodiči blízko limitu (pri idúcom vlaku bez záznamu o nástupe len odhad z odchodu vlaku alebo výzva „zadajte nástup rušňovodiča“, kapitola 9),
- očakávané preberania na hraniciach (s číslom vlaku),
- nové výluky,
- nepotvrdené e-maily.

Doplňte otvorené úlohy a potvrďte.

Ak sa odovzdávka (rovnako súhrn alebo denná správa) niektorému dispečerovi nepodarí doručiť, agent mu ju pošle znova – tú istú, len jemu; ostatní ju druhý raz nedostanú.

## 12. Keď agent nebeží

- **Upozorní vás nezávislý monitoring** (ak agent prestane posielať „heartbeat“) a denná správa o stave chýba.
- **Web neukáže ZELENÚ bez čerstvých údajov** – posledné zelené odporúčania sa zobrazia ako ORANŽOVÉ s poznámkou „bez čerstvých údajov agenta – pôvodne ZELENÁ“ (pozri kapitolu 3).
- **Pracujete ako doteraz** – prevádzkové e-maily máte vo svojej schránke.
- **Nahláste to správcovi servera.** Postup obnovy je v `docs/PREVADZKA.md`.
