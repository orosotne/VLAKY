# Monitoring – Agent dispečer

**Zásada:** o výpadku sa musí dozvedieť človek **z inej služby, než je sledovaný server** (PROMPT.md 2.6).
Keď agent nebeží, dispečing pracuje ako doteraz – prevádzkové e-maily chodia aj ľuďom, agent je len
ďalší príjemca. Monitoring preto hlavne včas povie: „agent teraz neodporúča, pracujte ručne“.

## Čo sa sleduje

| Kontrola | Kto ju vykonáva | Interval | Upozornenie, keď |
|---|---|---|---|
| **Heartbeat agenta** | agent pinguje `VLAKY_HEARTBEAT_URL` (healthchecks.io a pod.) | 5 min | ping neprišiel 15 min (perióda 5 + tolerancia 10) alebo agent poslal `/fail` |
| **Dostupnosť webu** | externá služba volá `https://DOMÉNA/health` | 1 – 5 min | 2 zlyhania po sebe (iný kód ako 200) |
| **Certifikát HTTPS** | externá služba (ak to vie) + `check.sh` | denne / 15 min | platí menej ako 10 dní |
| **Server** – disk, vek zálohy, kontajnery alebo služby, bez Dockera zdravie agenta, HTTPS | `deploy/monitoring/check.sh` (časovač `vlaky-check.timer`) → `CHECK_HEARTBEAT_URL` | 15 min | `/fail` s dôvodom, alebo ping chýba 30 min |
| **Denná záloha** | `deploy/backup/backup.sh` → `BACKUP_HEARTBEAT_URL` | 1 deň | `/fail`, alebo ping chýba viac ako 26 h |

`GET /health` je bez prihlásenia a overuje **webovú aplikáciu a databázu** (`{"status":"ok","db":"ok"}`,
inak HTTP 503). Stav **agenta** pokrýva heartbeat a `python -m vlaky health` (posledný cyklus agenta
do 10 min, posledná úplná kontrola schránky do 30 min – aj počas dočítavania nahromadenej pošty) – ten používa aj Docker ako kontrolu zdravia kontajnera `agent`.

## Nastavenie krok za krokom (príklad healthchecks.io)

Healthchecks.io je služba, ktorá upozorní, keď **prestanú** chodiť pingy. Rovnako funguje vlastná
inštalácia Healthchecks alebo Uptime Kuma – **nie na tom istom serveri** ako agent. Pred použitím
overte podmienky služby, umiestnenie dát a GDPR (pingy neobsahujú osobné údaje).

1. Založ projekt „Agent dispečer“ a nastav kanály upozornení: e-mail správcu, e-mail/SMS vedúceho
   dispečingu, prípadne kanál MS Teams dispečingu.
2. Kontrola **agent-heartbeat**: perióda 5 min, tolerancia (grace) 10 min.
   Jej „ping URL“ vlož do `.env`: `VLAKY_HEARTBEAT_URL=https://hc-ping.com/…` a spusti `docker compose up -d`.
3. Kontrola **server-kontrola**: perióda 15 min, tolerancia 30 min → `CHECK_HEARTBEAT_URL` v `/etc/vlaky/ops.env`.
4. Kontrola **zaloha**: perióda 1 deň, tolerancia 2 h → `BACKUP_HEARTBEAT_URL` v `/etc/vlaky/ops.env`.
   (Pri hodinových zálohách perióda 1 h.)
5. HTTP kontrola webu (Better Stack, UptimeRobot, Uptime Kuma na inom serveri …):
   `GET https://DOMÉNA/health`, očakávaný kód 200 (voliteľne text `"status":"ok"`),
   interval 1 – 5 min, upozornenie po 2 zlyhaniach, sledovanie platnosti certifikátu (10 dní).
6. **Vyskúšaj** (najlepšie mimo špičky, dispečing vopred upozorni):
   ```bash
   docker compose stop agent      # do ~15 min musí prísť upozornenie „agent-heartbeat DOWN“
   docker compose start agent     # upozornenie „UP“
   deploy/monitoring/check.sh     # ručne; výsledok OK / CHYBA s dôvodom
   ```
   Výsledok testu zapíš do prevádzkového denníka (akceptačné kritérium fázy F0).

## check.sh

```bash
deploy/monitoring/check.sh            # ručne (ako používateľ vlaky v /opt/vlaky/app)
systemctl list-timers vlaky-check.timer
journalctl -u vlaky-check -n 50       # výsledky z časovača
```

Kontroluje: zaplnenie diskov a inódov (`CHECK_DISK_PATHS`, limit `CHECK_DISK_MAX_PCT`=85 %),
vek najnovšej zálohy databázy (`CHECK_BACKUP_MAX_AGE_HOURS`=26), stav a zdravie kontajnerov
`db`, `web`, `agent`, `caddy` (pri inštalácii bez Dockera služieb systemd a navyše zdravie agenta
`python -m vlaky health` s konfiguráciou služieb – `CHECK_ENV_FILE`, predvolene `/etc/vlaky/vlaky.env`,
a Python `CHECK_PYTHON`), `https://DOMÉNA/health` cez lokálny Caddy a platnosť certifikátu
(`CHECK_CERT_MIN_DAYS`=10). Doména: `CHECK_URL`, inak `VLAKY_DOMAIN` alebo `VLAKY_PUBLIC_URL` zo súboru
`CHECK_ENV_FILE` (pri Dockeri `.env`); neznáma doména je CHYBA (HTTPS ani certifikát by sa nekontrolovali).
Upozorní aj na čakajúci reštart po aktualizáciách, chýbajúcu zálohu mimo servera a príliš otvorené práva
súboru `.env`.

Výstup: riadky `OK`, `POZOR` (upozornenie, nespôsobí chybu) a `CHYBA` (dôvod a ďalší krok – pri Dockeri
príkazy `docker compose`, bez Dockera `systemctl` a `journalctl`). Návratový kód 0 = v poriadku, 1 = aspoň
jedna chyba. Podrobnosti zdravia agenta idú len do výpisu, na externý monitoring len riadok CHYBA.
Nastavenia sú v `/etc/vlaky/ops.env`.

## Kto dostáva upozornenia

| Upozornenie | Správca servera | Vedúci dispečingu | Dispečing (služba) |
|---|---|---|---|
| Agent nebeží (heartbeat) | áno – rieši | áno | áno – „pracujte ručne“ |
| Web nedostupný | áno – rieši | áno | podľa dohody |
| Disk, záloha, certifikát | áno – rieši | nie | nie |

Mená, telefóny a zástupcov doplňte do prevádzkového denníka; upozornenia nesmú chodiť len jednému človeku.

## Čo robiť pri upozornení

Podrobný postup je v **docs/PREVADZKA.md, kapitola 13** (agent nebeží, vypršaný prístup do schránky,
nedostupný LLM, plný disk, certifikát). V skratke pri výpadku agenta:

1. Dispečing: pracuje ako doteraz podľa e-mailov vo svojej schránke; nečaká na odporúčania.
2. Správca: `docker compose ps`, `docker compose logs --tail 200 agent`,
   `docker compose exec agent python -m vlaky health`.
3. Reštart: `docker compose up -d agent` (pri zmene `.env`) alebo `docker compose restart agent`.
4. Ak to nepomôže do 30 min, eskaluj podľa kontaktov v prevádzkovom denníku; po vyriešení
   informuj dispečing a zapíš príčinu.
