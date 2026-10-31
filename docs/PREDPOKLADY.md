# Predpoklady pri stavbe (odpovede na otázky zo sekcie 15 PROMPT.md)

Pri stavbe som sa nepýtal, lebo si dal súhlas na všetko. Kde chýbala odpoveď, zvolil som bezpečný predpoklad. Všetko sa dá zmeniť v nastaveniach (`.env`, stránka Nastavenia) bez zásahu do kódu.

| # | Otázka | Predpoklad v systéme | Kde zmeniť |
|---|---|---|---|
| 1 | Názov dopravcu, vlaky, relácie | Názov je zástupný (`VLAKY_CARRIER_NAME`). Vlaky sa zadávajú vo webe alebo prídu z e-mailov. Ukážka obsahuje 6 fiktívnych vlakov na reálnych reláciách. | `.env`, web → Vlaky |
| 2 | Hraničné priechody a preberanie | Všetkých 23 priechodov ŽSR s preberacími stanicami podľa medzištátnych zmlúv a Sieťového vyhlásenia 2027. Doby preberania sú odhad: 60/150 min, na hranici s Ukrajinou 180/420 min. | web → Hranice |
| 3 | E-mailová adresa agenta | Podporované sú Microsoft 365 (Graph), IMAP aj „adresár“ na ukážku. Povolení odosielatelia sa nastavujú (`VLAKY_ALLOWED_SENDERS`). Vyžaduje sa DMARC. | `.env` |
| 4 | Vzorové e-maily | Použité sú fiktívne vzorky (`data/ukazkove_emaily`). Skutočné anonymizované e-maily zlepšia pravidlá extrakcie – pošli ich. | – |
| 5 | Pracovný čas rušňovodiča | Limit 13 h od nástupu (príprava 60 min pred odchodom) po odovzdanie rušňa (30 min po príchode). Rezerva 60 min, upozornenia pri 90/60/30 min. Prestávky a odpočinok zatiaľ nie sú modelované – treba ich doplniť podľa kolektívnej zmluvy. | web → Nastavenia |
| 6 | Interné systémy | Nie sú napojené. Objednávky, vlaky a rušňovodiči sa zadávajú vo webe alebo cez e-maily. Import sa dá doplniť neskôr. | – |
| 7 | Rušne a GPS | GPS jednotky zatiaľ nie sú. Poloha ide cez API `/api/pozicie` (GPS brána, ŽSR/RNE TIS), cez stránku pre rušňovodiča v mobile alebo zo simulátora. | `.env` (`VLAKY_POSITIONS_TOKEN`) |
| 8 | Dáta a systémy ŽSR | Výluky a trasy prichádzajú e-mailom. Priame napojenie (RNE TIS, TAF TSI) vyžaduje zmluvu a prístupy. | – |
| 9 | Náklady a pokuty | Vzorové sadzby (napr. čakanie 180 €/h, jazda 300 €/h, odstavenie 600 €). Bod zlomu čakať/odstaviť je 5 h. **Treba nahradiť skutočnými číslami.** | web → Nastavenia |
| 10 | Server, prihlasovanie, schvaľovanie, pilot | VPS v EÚ, Docker Compose, lokálne účty s rolami (SSO sa dá doplniť cez reverse proxy). Prvých 4 – 6 týždňov tieňová prevádzka (`VLAKY_SHADOW_MODE=true`): odporúčania idú len vedúcemu. | `.env`, `docs/PREVADZKA.md` |
