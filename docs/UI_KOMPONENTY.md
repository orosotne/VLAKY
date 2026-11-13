# UI komponenty

Knižnica opakovane použiteľných komponentov webovej aplikácie Agent dispečer. Živý katalóg so všetkými stavmi je
na stránke **/komponenty** (menu Správa → Komponenty UI).

## Architektúra

Aplikácia sa vykresľuje na serveri (FastAPI + Jinja2), bez build kroku a bez frameworku v prehliadači. Komponent
má preto tri vrstvy, každú v jednom súbore:

| Vrstva | Súbor | Úloha |
|---|---|---|
| Štruktúra | `vlaky/web/templates/_ui.html` | Jinja makrá: prístupný HTML, varianty, prázdne stavy |
| Vzhľad | `vlaky/web/static/css/ui.css` | štýly komponentov, len z tokenov v `app.css` (tmavý aj svetlý režim) |
| Správanie | `vlaky/web/static/js/ui.js` | progresívne vylepšenie cez atribúty `data-ui-*` |

Zásady:

- **Funguje bez JavaScriptu.** Makro vykreslí kompletný a použiteľný HTML. Skript pridá len pohodlie: stav
  odosielania tlačidla, filter tabuľky, karty na mobile a zavretie hlásenia.
- **Jeden zdroj pravdy pre farby a písmo.** Komponenty používajú tokeny (`--panel`, `--text`, `--green`, `--mono` …).
  Nová téma sa robí zmenou tokenov, nie komponentov.
- **Žiadny inline štýl ani skript.** CSP stránky (`style-src 'self'`, `script-src 'self'`) by ich zablokovala.
  Test `test_komponenty_nevkladaju_inline_styly` to stráži.
- **Bezpečné predvolené hodnoty.** Neznámy `variant`, `tone` či `size` padne na neutrálny vzhľad. Neznámy `type`
  tlačidla sa zmení na `button`, takže formulár sa náhodou neodošle.
- **Escapovanie.** Všetky textové parametre sa escapujú (autoescape). HTML ide do komponentu len cez `{% call %}`
  blok (`caller()`).
- **Staršie makrá ostávajú.** Polia formulára (`field`, `select`, `checkbox`), `semafor`, `priority` a `pager`
  sú naďalej v `_makra.html`. Nové komponenty ich dopĺňajú, nenahrádzajú.

Načítanie (už je v `base.html` pre každú stránku): `ui.css` hneď za `app.css`, `ui.js` hneď za `app.js`.
Živá obnova stránky (`data-zivy-stav` v `app.js`) po výmene obsahu volá `vlakyUI.refreshTable(main)`, takže filter
a karty na mobile fungujú aj po obnove.

```jinja
{% import "_ui.html" as ui with context %}
```

## API

### `button(label, variant, size, type, href, busy_label, disabled, icon_only, block, attrs)`

| Parameter | Hodnoty | Predvolené | Poznámka |
|---|---|---|---|
| `label` | text | povinný | pri `icon_only` slúži ako `aria-label` |
| `variant` | `primary` `secondary` `danger` `ghost` | `secondary` | jedna `primary` akcia na obrazovku |
| `size` | `sm` `md` `lg` | `md` | |
| `type` | `button` `submit` `reset` | `button` | pri `href` sa ignoruje |
| `href` | URL | – | vykreslí `<a>` |
| `busy_label` | text | – | počas odosielania: `aria-busy`, zablokovanie, tento text; po 20 s sa tlačidlo samo povolí |
| `disabled` | bool | `False` | odkaz dostane `aria-disabled` a stratí `href` |
| `icon_only` | bool | `False` | ikonu dajte do `{% call %}` s `aria-hidden="true"` |
| `block` | bool | `False` | celá šírka |
| `attrs` | dict | `{}` | ďalšie atribúty; `None` atribút vynechá, `True` vykreslí len názov |

### `status(text, tone, dot, title)`
Krátky stavový štítok. `tone`: `neutral` `info` `success` `warning` `danger`. Semafor odporúčania kreslí makro
`_makra.semafor` (má pevné slová ZELENÁ / ORANŽOVÁ / ČERVENÁ).

### `alert(text, tone, title, live, dismissible, attrs)`
Hlásenie v obsahu, s textom alebo HTML cez `{% call %}`. `tone`: `info` `success` `warning` `danger`.
`live=True` ho oznámi čítačke obrazovky (`danger` ako `role="alert"`, ostatné ako `role="status"`). Používajte ho len
pri hláseniach, ktoré vzniknú po akcii používateľa. `dismissible` pridá tlačidlo Zavrieť. Fokus potom prejde na hlavný
obsah, aby nezostal na odstránenom prvku.

### `empty_state(title, text, action_label, action_href, action_variant, reset_href, tone, compact)`
Prázdny stav, vždy s ďalším krokom. Oba prípady treba rozlíšiť:
- **Prázdne údaje:** povedzte, prečo nič nie je a ako to pribudne. Pridajte `action_*`.
- **Prázdny výsledok filtra:** povedzte, čo skúsiť. Pridajte `reset_href` (tlačidlo „Zrušiť filter“).

`tone="success"` je pre stav „všetko vybavené“.

### `stat(label, value, unit, tone, hint, href)`
KPI dlaždica. `value=None` alebo `""` sa zobrazí ako „—“ s poznámkou „bez údajov“. **Nula je hodnota**, nie chýbajúci
údaj. Dlaždice dajte do `<div class="ui-stats">`, mriežka sa na mobile sama zalomí.

### `skeleton(lines, label)`
Zástupný obsah počas načítania. Kontajner má `aria-busy="true"` a skrytý text pre čítačku (`role="status"`).
Počet riadkov je obmedzený na 1–12. Keď je obsah hotový, nahraďte skeleton, alebo nastavte `aria-busy="false"`.

### `card(title, subtitle, level, id, actions, busy, attrs)`
Sekcia s hlavičkou. Pri zadanom `id` dostane nadpis `id-nadpis` a sekcia `aria-labelledby`. `level` (2–4) udržiava
správnu osnovu nadpisov stránky. `actions` je HTML zostavené cez `{% set %}`. `busy=True` označí sekciu
počas načítania.

### `data_table(caption, columns, count, empty_*, filter, stack, compact, caption_visible, id)`
Tabuľka s prázdnym stavom, voliteľným filtrom a kartami na mobile.

- **`columns`:** zoznam reťazcov alebo `{"label", "align": "end", "sr_only": True}`. `align: "end"` zarovná čísla
  vpravo s rovnakou šírkou číslic. `sr_only` je stĺpec akcií s popisom len pre čítačku.
- **`count`:** počet riadkov. Pri `0` sa namiesto tabuľky zobrazí prázdny stav (`empty_title`, `empty_text`,
  `empty_action_label`, `empty_action_href`, `empty_reset_href`, `empty_tone`).
- **`filter=True`:** pole „Hľadať v tabuľke“ filtruje riadky aktuálnej stránky.
  - Hľadá bez diakritiky a bez ohľadu na veľké písmená („kosice“ nájde „Košice“).
  - Počet zobrazených riadkov sa oznamuje cez `aria-live`.
  - Esc pole vymaže.
  - Ak nič nezodpovedá, zobrazí sa vlastný prázdny stav.
  - Filter **nenahrádza** serverový filter pri stránkovaní.
- **`stack=True`** (predvolené): pod 700 px sa riadky zobrazia ako karty. Popis stĺpca sa doplní z `<th>`
  (`data-label`), takže riadky ho nemusia uvádzať.
- **Prístupnosť:**
  - Tabuľka má vždy `<caption>`, predvolene skrytý.
  - Posúvaná oblasť je `role="region"` s `tabindex="0"`, takže sa dá posúvať klávesnicou.

## Príklady

Zoznam s filtrom a rozlíšeným prázdnym stavom (skutočný kód z `odporucania.html`):

```jinja
{% set filtered = stav or farba or vlak %}
{% set cols = ["Vytvorené", "Vlak", "Rozhodnutie", "Semafor", "Priorita", "Zhrnutie", "Stav", "Rozhodol"] %}
{% call ui.data_table("Odporúčania agenta", cols, count=recs|length, filter=True, id="tabulka-odporucani",
                      empty_title=("Nič nezodpovedá filtru" if filtered else "Žiadne odporúčania"),
                      empty_text=("Skúste iný stav, semafor alebo vlak." if filtered
                                  else "Agent vytvorí odporúčanie pri ďalšom cykle pre každý aktívny vlak."),
                      empty_reset_href=("/odporucania" if filtered else None)) %}
  {% for r in recs %}<tr>…</tr>{% endfor %}
{% endcall %}
```

Formulár s ochranou pred dvojitým odoslaním a stavom odosielania:

```jinja
<form method="post" action="/fronta/{{ ev.id }}/potvrdit">
  {{ m.csrf() }}
  {{ ui.button("Potvrdiť", variant="primary", type="submit", busy_label="Potvrdzuje sa…") }}
  {{ ui.button("Zrušiť", variant="ghost", href="/fronta") }}
</form>
```

KPI a karta s akciami:

```jinja
{% set akcie %}{{ ui.button("Export CSV", size="sm", href="/reporty.csv") }}{% endset %}
{% call ui.card("Prehľad dňa", subtitle="posledných 24 h", id="prehlad", actions=akcie) %}
  <div class="ui-stats">
    {{ ui.stat("Aktívne vlaky", trains|length) }}
    {{ ui.stat("Čaká vo fronte", counts.fronta, tone="warning", href="/fronta") }}
    {{ ui.stat("Priemerná rezerva", avg_buffer, unit="min") }}
  </div>
{% endcall %}
```

Hlásenie po akcii:

```jinja
{{ ui.alert("Výluka uložená.", tone="success", live=True, dismissible=True) }}
```

API pre ďalšie skripty: `window.vlakyUI.setBusy(button, true|false)` a `window.vlakyUI.refreshTable(root)`.
Druhé volajte po vložení nového HTML s tabuľkami.

## Okrajové prípady, ktoré komponenty riešia

| Prípad | Riešenie |
|---|---|
| Dvojité odoslanie | `app.js` blokuje opakované odoslanie. `busy_label` tlačidlo zablokuje, až keď sa zostavia údaje formulára (inak by `name`/`value` tlačidla vypadli). |
| Návrat tlačidlom Späť | Ak sa stránka vráti z bfcache, tlačidlá sa znova povolia (`pageshow`). |
| Výpadok siete | Tlačidlo v stave odosielania sa po 20 s samo povolí. |
| Potvrdenie akcie | `data-confirm` na formulári (`app.js`). Zrušené potvrdenie stav odosielania nespustí. |
| Prázdne údaje a prázdny filter | Majú iný text. Prázdny filter má odkaz „Zrušiť filter“. |
| Filter bez výsledku | Tabuľka sa skryje a zobrazí sa „Nič nezodpovedá hľadanému textu“. Počet sa oznámi čítačke. |
| Slovenské znaky | Filter porovnáva bez diakritiky (NFD). Písma obsahujú latin-ext. |
| Skloňovanie | 1 riadok, 2–4 riadky, 5+ riadkov. |
| Chýbajúca hodnota KPI a nula | „—“ znamená chýbajúci údaj, „0“ je platná hodnota. |
| Dlhý text a čísla | `overflow-wrap: anywhere` v kartách a tabuľkách. Čísla majú rovnakú šírku číslic (`tabular-nums`). |
| Úzka obrazovka | Tabuľky sa zobrazia ako karty a stránka sa neroztiahne do strán. Overené pri 390 px. |
| Živá obnova obsahu | Komponenty sa po výmene obsahu inicializujú znova. Hodnota filtra sa zachová. |
| Obmedzený pohyb | Pri `prefers-reduced-motion` sa vypnú animácie (skeleton, točiaci sa ukazovateľ, pulz hodín). |

## Pravidlá pre vývoj

1. **Nový zoznam** je vždy `ui.data_table` s rozlíšeným prázdnym stavom, nie holý `<table>` s riadkom „Žiadne záznamy“.
2. **Akcie:**
   - Jedna `primary` akcia na obrazovku.
   - Deštruktívne akcie majú `variant="danger"` a `data-confirm` na formulári.
   - Odkaz, ktorý len naviguje, je `href`, nie formulár.
3. **Texty:**
   - Píšte, čo sa stane („Potvrdiť“, „Ukladá sa…“), nie všeobecné „OK“.
   - Chyby hovoria, čo je zle a ako to opraviť.
4. **Farby:**
   - Nikdy nie priamo v šablóne, vždy tón komponentu alebo token.
   - Semantické farby (zelená, oranžová, červená) sú len pre stav, nie na dekoráciu.
5. **Nový komponent:**
   - Pridajte makro do `_ui.html` s dokumentačným komentárom (parametre, hodnoty, predvolené).
   - Štýl patrí do `ui.css` a správanie cez `data-ui-*` do `ui.js`.
   - Doplňte ukážku na `/komponenty` a test do `tests/test_ui_komponenty.py` (varianty, neznáma hodnota, escapovanie, aria).
6. **Prístupnosť:**
   - Každé pole má `<label>` a každé tlačidlo či odkaz čitateľný názov.
   - Na stránke nesmú byť duplicitné `id` (test to overuje pre `/komponenty`).
   - Fokus je vždy viditeľný (`:focus-visible`).
7. **Overenie pred odovzdaním:**
   - `pytest tests/test_ui_komponenty.py`.
   - Testy webu (`tests/test_*web*.py`, `tests/test_*prehliadac*.py`).
   - Pohľad na `/komponenty` v tmavom aj svetlom režime a pri šírke 390 px.
