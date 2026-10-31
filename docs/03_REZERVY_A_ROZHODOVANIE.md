# Časové rezervy a rozhodovanie: vypraviť, počkať, odstaviť, presmerovať

Tento dokument vysvetľuje pravidlá, ktoré má aplikácia používať (sekcia 10 v `PROMPT.md`). Presne ich implementuje `referencia/rozhodovanie.py`. Všetky čísla sú **návrh na začiatok** a treba ich nastaviť podľa vašich dát (kapitola 9).

## Zhrnutie na jednu obrazovku

| Pravidlo | Predvolene | Zmysel |
|---|---|---|
| Rezerva pred výlukou | 30 min | Úsek s výlukou musí byť opustený aspoň 30 min pred jej začiatkom. |
| Rezerva po výluke | 30 min | Do úseku vstúpiť najskôr 30 min po *plánovanom* konci výluky. |
| Rezerva voči termínu | 60 min | Plánovaný príchod (medián) aspoň 60 min pred termínom zákazníka. |
| Pracovný čas rušňovodiča | max. 13 h | Od nástupu (príprava) po odovzdanie rušňa. |
| Rezerva do limitu 13 h | 60 min | Aj „zlá“ jazda (90. percentil) skončí aspoň 60 min pred limitom. |
| Spoľahlivosť pre ZELENÚ | 90 % | Aspoň 90 zo 100 takýchto jázd príde včas. |
| Bod zlomu čakať/odstaviť | 5 h | Pri dlhšom čakaní je lacnejšie vlak odstaviť (pri vzorových nákladoch). |
| Lehota na novú (ad hoc) trasu | 6 h | Krátkodobú ad hoc trasu (produkt P5) treba objednať najneskôr 6 h pred odchodom (PPŽS). |
| Najdlhšie čakanie / odstavenie | 18 h | Vlak, ktorý neodíde do 18 h od plánu, systém ŽSR (PIS) zruší; odstavenie je najviac 18 h. |

**Semafor:** ZELENÁ = vypraviť · ORANŽOVÁ = len s potvrdením dispečera · ČERVENÁ = nevypravovať.

**Rozhodnutia:** VYPRAVIŤ teraz · POČKAŤ (vlak stojí pripravený) · ODSTAVIŤ (dlhšie čakanie, rušňovodič sa uvoľní) · PRESMEROVAŤ (iná trasa) · ESKALOVAŤ (nič nie je bezpečné → ŽSR a zákazník).

---

## 1. Prečo rezervy a prečo „percentily“

Zdržania vlakov sú náhodné. Priemer nestačí: ak plánujete na priemer, zhruba polovica vlakov príde neskôr. Preto aplikácia pracuje s rozdelením zdržaní:

- **P50 (medián):** polovica jázd je rýchlejšia, polovica pomalšia – „bežný“ priebeh.
- **P90:** 90 % jázd je rýchlejších – „zlý, ale nie extrémny“ deň.

Pevná rezerva (napr. vždy 60 min) je na jednej trati zbytočne veľká a na inej malá. Preto sa po nazbieraní dát rezervy nastavia **po úsekoch** (a prípadne podľa dennej doby) z P90 skutočných zdržaní.

## 2. Predvolené rezervy a ako ich nastaviť

| Rezerva | Predvolene | Prečo | Ako ju nastaviť z dát |
|---|---|---|---|
| Pred výlukou (`buffer_before_closure_min`) | 30 min | Výluka začína načas a ŽSR nepustí vlak do úseku, ktorý by nestihol opustiť. Meškanie vlaku pred úsekom by inak znamenalo státie pred výlukou. | 90. percentil odchýlky príchodu vlakov k úseku oproti plánu. |
| Po výluke (`buffer_after_closure_min`) | 30 min | Výluky sa niekedy predĺžia, po výluke býva pomalá jazda a čakajú aj iné vlaky. | Rozdelenie skutočného konca výluk oproti plánu (P90) + čas na rozjazd prevádzky. |
| Termín (`deadline_buffer_min`) | 60 min | Rezerva na koniec cesty (vlečka, posun, prevzatie zákazníkom). | P90 meškania na danej relácii. |
| Limit pracovného času rušňovodiča (`crew_max_duty_min`) | 780 min (13 h) | Rušňovodič nesmie prekročiť 13 h pracovného času na rušni (od nástupu po odovzdanie rušňa). | Podľa predpisov a kolektívnej zmluvy; overiť, čo presne sa započítava. |
| Rezerva do limitu (`crew_buffer_min`) | 60 min | Vlak nesmie zostať stáť bez rušňovodiča – to je jedna z najdrahších situácií. | P90 skutočnej dĺžky zmien na relácii. |
| Ukončenie po príchode (`crew_post_min`) | 30 min | Odstavenie a odovzdanie rušňa sa ráta do pracovného času. | Podľa vašich interných noriem. |
| Tolerancia trasy (`path_tolerance_min`) | 15 min | Do akej odchýlky od pridelenej trasy ešte nie je potrebná nová trasa. | Podľa pravidiel ŽSR a vašej praxe. |
| Lehota na novú trasu (`new_path_lead_min`) | 360 min (6 h) | Ad hoc trasa na krátky čas (produkt P5) sa podľa PPŽS objednáva najneskôr 6 h pred odchodom; iné druhy trás majú dlhšie lehoty (5 pracovných dní, 30 dní). | Podľa pravidiel ŽSR a skutočných časov vybavenia vašich žiadostí. |
| Najdlhšie čakanie (`max_hold_h`) | 18 h | PIS automaticky zruší vlak, ktorý neodišiel do 18 h od plánovaného odchodu; odstavenie v nácestnej stanici je najviac 18 h (PPŽS uvádza na inom mieste 24 h – rozpor treba overiť so ŽSR, použitá je prísnejšia hodnota). | Podľa potvrdenia ŽSR. |
| Príprava rušňovodiča (`crew_prep_min`) | 60 min | Prevzatie rušňa, skúška bŕzd a ďalšie úkony pred odchodom – započítava sa do pracovného času. | Podľa vašich interných noriem. |

**Úkony počas jazdy** (preberanie na hraničnom priechode, skúška bŕzd, technická prehliadka vozňov, výmena rušňa alebo rušňovodiča) zadávajte ako súčasť jazdnej doby dotknutého úseku. Ich typické trvanie (P50/P90) treba nastaviť podľa vašich noriem a histórie. Zdroje a orientačné hodnoty sú v `docs/02_ZAVEDENIE_SYSTEMU.md`.

## 3. Semafor

Aplikácia vyhodnotí každú variantu (odchod v čase X po trase Y):

- **ČERVENÁ** (nevypravovať), ak platí čokoľvek z tohto:
  - P(včas) < 70 %,
  - P(neplánovaného zadržania pred výlukou) > 10 %,
  - P(prekročenia 13 h pracovného času rušňovodiča) > 10 %,
  - pred úsekom s výlukou nie je kde čakať (hrozí zablokovanie trate),
  - je porušené tvrdé obmedzenie (trakcia, dĺžka vlaku, nápravové zaťaženie, rozchod, znalosť trate…).
- **ZELENÁ** (odporúčané), ak platí všetko:
  - P(včas) ≥ 90 %,
  - P(zadržania pred výlukou) ≤ 2 %,
  - medián príchodu je aspoň 60 min pred termínom,
  - rušňovodičovi ostáva aspoň 60 min do limitu 13 h (pri 90. percentile),
  - dáta o polohe a výlukách sú čerstvé.
- **ORANŽOVÁ:** všetko ostatné. Dá sa vypraviť len s výslovným potvrdením dispečera, ktorý vidí dôvody.

## 4. Ako aplikácia rozhoduje

```mermaid
flowchart TD
    A["Vlak je alebo bude pripravený"] --> B["Vyhodnoť odchody v najbližších 24 h<br/>po hlavnej trase aj po obchádzkach"]
    B --> C{"Je nejaká varianta<br/>ZELENÁ alebo ORANŽOVÁ?"}
    C -- nie --> E["ESKALOVAŤ: ŽSR iná trasa alebo čas,<br/>zákazník nový termín"]
    C -- áno --> D["Vyber najnižší očakávaný náklad<br/>ZELENÁ má prednosť, ak je drahšia najviac o 10 percent"]
    D --> F{"Kedy a kadiaľ?"}
    F -- "teraz, hlavná trasa" --> G["VYPRAVIŤ"]
    F -- "neskôr, čakanie kratšie ako bod zlomu" --> H["POČKAŤ do času X"]
    F -- "neskôr, čakanie dlhšie ako bod zlomu" --> I["ODSTAVIŤ, nová trasa na čas X"]
    F -- "iná trasa" --> J["PRESMEROVAŤ, objednať novú trasu"]
```

**Očakávaný náklad varianty** = čakanie alebo odstavenie + jazda (čas × sadzba) + očakávaná pokuta za meškanie + riziko neplánovaného zadržania + riziko zablokovania trate + riziko mimoriadnej výmeny rušňovodiča + poplatky (nová trasa, dlhšia trasa).

Aplikácia tak vždy porovnáva **istotu** (P(včas), riziko výluky a pracovného času rušňovodiča) s **cenou** (koľko stojí čakanie, obchádzka alebo meškanie). Spoľahlivejšej ZELENEJ variante dá prednosť, aj keď je o niečo drahšia.

## 5. Čakať, alebo odstaviť? (bod zlomu)

- **Počkať** = vlak stojí pripravený (rušeň, vozne, obsadená koľaj, platná skúška bŕzd). Stojí to `cost_wait_ready_per_h` za hodinu.
- **Odstaviť** = presun na odstavnú koľaj, uvoľnenie rušňovodiča, neskôr nová skúška bŕzd a nová trasa. Jednorazovo `cost_stable_fixed`, potom už len `cost_stabled_per_h` za hodinu.

Odstaviť sa oplatí, ak čakanie potrvá dlhšie ako

> **T = cost_stable_fixed / (cost_wait_ready_per_h − cost_stabled_per_h)**

Pri vzorových hodnotách: T = 600 / (180 − 60) = **5 hodín**. S vašimi skutočnými nákladmi vyjde iné číslo.

## 6. Najneskorší bezpečný odchod a „bod, odkiaľ niet návratu“

Pri výluke na trase aplikácia ukáže **okná bezpečného odchodu**, napríklad:

> *„Vypraviť najneskôr o 05:40. Inak až po výluke, najskôr o 12:30.“*

- **Najneskorší bezpečný odchod pred výlukou:** vlak úsek s výlukou opustí včas (s rezervou pred výlukou) aj pri zlom priebehu cesty.
- **Najskorší bezpečný odchod po výluke:** vlak k úseku nepríde skôr ako 30 min po plánovanom konci výluky, takže nebude čakať na trati.

**Bod, odkiaľ niet návratu:** idúci vlak sa nepustí za poslednú dopravňu, kde môže bezpečne čakať (dosť dlhá koľaj), ak nie je isté, že úsek s výlukou stihne opustiť. Lepšie je počkať v stanici s vhodnou koľajou ako zostať stáť na trati alebo v malej dopravni a blokovať ju.

## 7. Pracovný čas rušňovodiča (limit 13 h)

- **Čo sa ráta:** od nástupu na zmenu (prevzatie rušňa, príprava, skúška bŕzd – predvolene 60 min pred odchodom) cez jazdu a čakanie až po odstavenie a odovzdanie rušňa po príchode (predvolene 30 min). Či sa rátajú aj presuny rušňovodiča (napr. cesta na hraničnú stanicu), nastaví dopravca podľa predpisov a kolektívnej zmluvy.
- **Limit:** 13 h (`crew_max_duty_min` = 780 min), nastaviteľný.
- **Semafor rušňovodiča:**
  - ČERVENÁ – limit by prekročil už pri bežnom priebehu jazdy (P50) → výmenu treba naplánovať hneď,
  - ORANŽOVÁ – limit by prekročil pri horšom priebehu (P90) alebo by mu zostalo menej ako 60 min → pripraviť výmenu,
  - ZELENÁ – aj pri horšom priebehu ostane aspoň 60 min.
- **Výstup:** „vystriedať najneskôr o HH:MM“ a návrh dopravne, kde je výmena možná a kam sa náhradný rušňovodič stihne dopraviť.
- **Pri preberaní na hranici:** ak vlak mešká, rušňovodič na hranici čaká a míňa pracovný čas. Aplikácia preto pri zmene avíza navrhne posunúť nástup rušňovodiča tak, aby mu na jazdu zostal dostatok času.
- **Pravidlo pre vypravenie:** vlak sa nikdy neodporučí vypraviť (ZELENÁ), ak by rušňovodič limit prekročil. Mimoriadnu výmenu rušňovodiča počas jazdy rozhodovací model započíta ako náklad a zdržanie.

## 8. Fiktívny príklad (vypočítaný referenčnou implementáciou)

> Výluka, časy aj náklady sú **vymyslené**. Ide len o ilustráciu výpočtu. Spustenie: `python3 referencia/rozhodovanie.py`.

Vlak Košice → Žilina, termín u zákazníka **18:00**. Výluka **Poprad-Tatry – Liptovský Mikuláš 10:00 – 14:00** (rezervy 30/30 min). Jazdná doba bez zdržaní je 4 h 35 min.

**Scenár 1 – vlak je pripravený načas (05:30, trasa 05:30)**

| Odchod | Rozhodnutie | Stav | P(včas) | P(zadržania) | Príchod P50 / P90 | Očak. náklad |
|---|---|---|---|---|---|---|
| 05:30 | VYPRAVIŤ | ZELENÁ | 99 % | 1 % | 10:21 / 10:41 | 1 548 € |

→ **VYPRAVIŤ.** Najneskorší bezpečný odchod pred výlukou je **05:40**.

**Scenár 2 – vlak bude pripravený až o 06:30 (dispečer to vie o 00:30), obchádzka nie je možná**

| Odchod | Rozhodnutie | Stav | P(včas) | P(zadržania) | Príchod P50 / P90 | Očak. náklad |
|---|---|---|---|---|---|---|
| 06:30 | VYPRAVIŤ | ČERVENÁ | 99 % | 100 % | 17:13 / 17:28 | 4 071 € |
| 09:00 | POČKAŤ | ČERVENÁ | 99 % | 100 % | 17:13 / 17:28 | 3 747 € |
| **12:30** | **ODSTAVIŤ** | **ORANŽOVÁ** | 97 % | 0 % | 17:21 / 17:41 | **2 483 €** |
| 13:00 | ODSTAVIŤ | ORANŽOVÁ | 76 % | 0 % | 17:51 / 18:11 | 2 837 € |

→ Vypraviť o 06:30 je ČERVENÉ: vlak by prišiel k výluke a takmer 5 hodín by stál v Poprade. Najlepšie je **ODSTAVIŤ** (čakanie 6 h > bod zlomu 5 h) a **hneď objednať novú trasu na 12:30**. Lehota na ad hoc trasu je 6 h, takže objednávka musí ísť ešte v ten deň ráno. Výsledok je ORANŽOVÝ, lebo rezerva voči termínu je len 39 min. Treba preto potvrdenie dispečera a je vhodné **vopred informovať zákazníka**.

**Scenár 3 – ako scenár 2, ale existuje obchádzka (fiktívna)**

| Trasa | Odchod | Rozhodnutie | Stav | P(včas) | Príchod P50 / P90 | Očak. náklad |
|---|---|---|---|---|---|---|
| hlavná | 12:30 | ODSTAVIŤ | ORANŽOVÁ | 97 % | 17:21 / 17:41 | 2 483 € |
| **obchádzka** | **06:30** | **PRESMEROVAŤ** | **ZELENÁ** | 100 % | 12:03 / 12:31 | **2 100 €** |
| obchádzka | 09:00 | PRESMEROVAŤ | ZELENÁ | 100 % | 14:33 / 15:01 | 2 552 € |

→ **PRESMEROVAŤ** cez obchádzku o 06:30: je lacnejšia aj spoľahlivejšia a vlak príde o 5 hodín skôr. Podmienka: ad hoc trasu po obchádzke treba objednať **hneď o 00:30**, inak 6-hodinová lehota nevyjde. Keby dispečer zmenu zistil až o 06:00, najskorší odchod po obchádzke by bol o 12:00 a výhodnejšie by bolo vlak odstaviť (scenár 2).

**Scenár 4 – pracovný čas rušňovodiča**

Rušňovodič nastúpil o 04:00, teraz je 14:00. Do cieľa zostáva bežne 2 h 10 min, v horšom prípade 2 h 50 min; odovzdanie rušňa trvá 30 min.

| Odpracované | Do limitu 13 h | Limit | Koniec P50 | Koniec P90 | Stav |
|---|---|---|---|---|---|
| 10,0 h | 3,0 h | 17:00 | 16:40 | 17:20 | ORANŽOVÁ |

→ Pri bežnom priebehu to stihne (16:40), pri horšom nie (17:20). Aplikácia upozorní: **pripraviť výmenu, vystriedať najneskôr o 16:30** (17:00 mínus 30 min na odovzdanie rušňa).

## 9. Kalibrácia z histórie (postup)

1. **Zbierať dáta od prvého dňa:** plán verzus skutočnosť po úsekoch a hraničných priechodoch (príchod, odchod, prejazd, prevzatie), príčiny zdržaní, skutočné začiatky a konce výluk, skutočný pracovný čas rušňovodičov, trvanie úkonov.
2. **Po 4 – 8 týždňoch** (alebo aspoň 30 jazdách na úseku) vypočítať P50 a P90 zdržaní po úsekoch, hraničných priechodoch, dňoch v týždni a denných dobách.
3. **Mesačne navrhnúť nové hodnoty** rezerv a zdržaní. Schvaľuje ich vedúci dispečingu a platia od dátumu (audit).
4. **Kontrola kalibrácie:** zo ZELENÝCH rozhodnutí má prísť včas aspoň 90 % vlakov. Ak je to menej, rezervy sú malé. Ak takmer 100 % a vlaky často zbytočne čakajú, rezervy sú príliš veľké.
5. **Náklady:** vzorové sadzby v `PROMPT.md` nahradiť skutočnými (rušeň, rušňovodič, vozne, zmluvné pokuty). Bez nich je porovnanie variantov len orientačné.

## 10. Čo rozhoduje dopravca a čo ŽSR

Dopravca rozhoduje o **pripravenosti** vlaku, o tom, či vlak **použije pridelenú trasu**, či požiada o **novú trasu** alebo o **zrušenie trasy**, a o tom, či vlak **odstaví**. Samotnú jazdu vlaku po sieti, poradie vlakov a povolenie odchodu riadi **ŽSR**. Aplikácia preto odporúča kroky dopravcu a pripravuje podklady na dohodu so ŽSR a so zákazníkom. Riadenie dopravy nenahrádza.
