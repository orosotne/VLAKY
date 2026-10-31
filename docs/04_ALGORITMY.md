# Algoritmy – čo naozaj pomôže

## Krátka odpoveď

- **„Nové efektívnejšie algoritmy od OpenAI“ na plánovanie vlakov neexistujú.** K 31. 10. 2026 OpenAI nezverejnilo žiadny nový algoritmus na trasovanie, najkratšie cesty ani rozvrhovanie, ktorý by dispečer mohol použiť. Jeho posledné „algoritmické“ správy sú teoretická matematika (podrobnosti nižšie).
- **Na sieti ŽSR stačia klasické algoritmy.** Sieť má asi 3 628 km tratí a rádovo tisíce dopravných bodov a výhybiek. Dijkstra alebo A* nájde trasu za milisekundy. V Agentovi dispečerovi trvá trasa naprieč Slovenskom 10 – 150 ms.
- **Ťažké nie je nájsť najkratšiu cestu.** Ťažké je mať správne dáta (výluky, parametre tratí), dodržať obmedzenia (rozchod, trakcia, dĺžka vlaku, úvrate, rušňovodič a jeho 13 h, pridelená trasa a lehoty ŽSR) a pracovať s neistotou meškaní. Na to sú v systéme overené metódy.

## Čo sa skrýva za „novými algoritmami“

| Novinka | Kto | Čo to je | Význam pre dispečing |
|---|---|---|---|
| „Exponential Time Hypothesis je nepravdivá“ (26. 10. 2026) | OpenAI | Teoretický dôkaz (počítanie riešení logických formúl k-CNF) s formálnou verziou v jazyku Lean. Zatiaľ ho overujú odborníci, nejde o recenzovanú prácu. | Žiadny – netýka sa trasovania. |
| „Ten advances in mathematics“ (1. 8. 2026), „377 results“ (6. 10. 2026) | OpenAI | Hromadné zverejnenie matematických dôkazov | Žiadny |
| Prelomenie „triediacej bariéry“ pri najkratších cestách (STOC 2025, najlepší článok) | Duan a kol., Tsinghua (ľudia, nie AI) | Asymptoticky rýchlejší algoritmus na najkratšie cesty z jedného zdroja | V praxi je Dijkstra 3 – 4× rýchlejší až do 10 miliónov vrcholov. |
| C-HD (20. 9. 2026) | Vals AI, agenti Claude | Formálne overená asymptotická hranica | Autori neuvádzajú praktické zrýchlenie |
| Rýchlejšie APSP a 3SUM (5. 10. 2026) | Alman, Vassilevska Williams (s pomocou Claude) | Teória zložitosti | Žiadny pri tejto veľkosti siete |
| AlphaEvolve (2025), OpenEvolve | Google DeepMind / open source | Hľadanie heuristík pomocou LLM proti spoľahlivému simulátoru | Najviac neskôr, offline ladenie heuristík. Nie na rozhodovanie. |

**Kde umelá inteligencia (ChatGPT, Claude) reálne pomáha:** pri čítaní e-mailov a príloh, pri vysvetľovaní odporúčaní, písaní konceptov správ a pri programovaní. **Nesmie** však rozhodovať o uskutočniteľnosti a bezpečnosti a pri čítaní nedôveryhodných e-mailov nesmie mať právo nič vykonať.

## Čo používa Agent dispečer

| Úloha | Metóda v systéme | Súbor |
|---|---|---|
| Sieť | Graf z OpenStreetMap: 23 176 uzlov, 27 098 úsekov, atribúty rozchodu, trakcie, napätia a rýchlosti | `vlaky/siet/osm_import.py` |
| Trasovanie | **Dijkstra nad hranami** (stav = uzol + hrana príchodu) s kontrolou úvratí. Obrat je možný len v obvode stanice (+20 min), nie na výhybke na trati. Trasa začína a končí kdekoľvek v obvode stanice. | `vlaky/siet/graf.py` |
| Filter uskutočniteľnosti | Rozchod 1435/1520, elektrická trakcia len po elektrifikovaných úsekoch (aj podľa napäťových systémov rušňa), penalizácia vedľajších koľají | `graf.py` |
| Výluky | Úsek výluky = cesta medzi dopravňami **plus všetky súbežné koľaje** (do 40 m). Úplná výluka zakáže úsek v časovom okne rozšírenom o rezervy, výluka trakčného vedenia platí len pre elektrickú trakciu, výluka koľaje a pomalá jazda predĺžia čas. | `planovanie/planovac.py` |
| Obchádzky | Najkratšia cesta so zakázanými úsekmi výluky. Alternatívy po koridoroch: penalizácia celých tratí podľa čísla trate, porovnanie podľa staníc. | `graf.py`, `planovac.py` |
| Jazdné doby | Z pridelenej trasy ŽSR, ak má časy. Inak z traťovej rýchlosti obmedzenej rýchlosťou vlaku + 4 % (UIC 451-1). Prirážky sa nezarátavajú dvakrát. | `planovac.py` |
| Neistota | **Simulácia Monte Carlo** (lognormálne zdržania P50/P90 na úsek), spoločné náhodné čísla pri porovnávaní variantov, dvojstupňový výpočet (600 → 4 000 vzoriek) | `vlaky/rozhodovanie.py` |
| Rozhodnutie | Minimalizácia **očakávaných nákladov** pri tvrdých obmedzeniach a prahoch pravdepodobnosti (semafor). Bod zlomu čakať/odstaviť. Zelené okná odchodu. Lehoty ŽSR (6 h, 18 h). Prepočet pri každej udalosti. | `rozhodovanie.py`, `planovac.py` |
| Rušňovodič | Limit 13 h (príprava + jazda + čakanie + odovzdanie), stav aktuálneho rušňovodiča aj pri odstavení, „vystriedať najneskôr o …“ | `planovac.py`, `rozhodovanie.py` |
| Priradenie polohy | Najbližšia koľaj s prednosťou koľají plánovanej trasy; vypočíta prejdené a zostávajúce km | `graf.py` |

## Čo ešte zlepší presnosť (ďalšie kroky)

1. **ETA z histórie:** kvantilová regresia (napr. LightGBM) s kalibráciou (conformal prediction). Vstupy: úsek, denná doba, deň v týždni, typ a hmotnosť vlaku, aktuálne meškanie, výluky, hraničný priechod. Výstup P50/P90 namiesto predvolených zdržaní. Porovnať s ETA siete v RNE TIS.
2. **Viac vlakov naraz** (rušne a rušňovodiči): CP-SAT (Google OR-Tools) alebo MILP s časovým limitom. Oplatí sa až pri desiatkach vlakov denne.
3. **Hľadanie voľnej kapacity v grafikone** (ako modul STDCM v open-source projekte OSRD) potrebuje úplný grafikon a výluky ŽSR, ktoré dopravca nemá. Preto agent navrhuje **okná pre objednávku ad hoc trasy**, nie konkrétnu voľnú trasu.
4. **Priradenie polohy pomocou skrytého Markovovho modelu** (Newson & Krumm) pri riedkych GPS bodoch a súbežných tratiach. Hotová implementácia je napríklad OpenRailRouting.

## Ako overiť, že nový algoritmus pomáha

Každý nový algoritmus alebo model sa nasadí až po **porovnávacom teste na vlastných historických dátach** (backtest):

- presnosť ETA (P50/P90),
- podiel ZELENÝCH rozhodnutí, pri ktorých vlak naozaj prišiel včas (cieľ ≥ 90 %),
- počet neplánovaných zadržaní pred výlukou,
- falošné poplachy.

Bez dát nemá zmysel siahať po „najnovšom“ algoritme.
