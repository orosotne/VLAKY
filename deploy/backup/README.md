# Zálohy a obnova – Agent dispečer

Skripty: `backup.sh` (záloha), `restore.sh` (obnova a test obnovy). Konfigurácia je v
`/etc/vlaky/ops.env` (vzor `deploy/ops.env.example`). Príkazy spúšťaj ako používateľ `vlaky`
v adresári `/opt/vlaky/app` (`sudo -iu vlaky`, `cd /opt/vlaky/app`), ak nie je uvedené inak.

## Čo sa zálohuje

| Čo | Súbor | Poznámka |
|---|---|---|
| Databáza PostgreSQL (registre, vlaky, výluky, odporúčania, audit, používatelia) | `db/vlaky-db-<UTC čas>.sql.gz` | `pg_dump` (čistý SQL) + gzip; kontroluje sa úplnosť výpisu |
| Schránka a odoslané správy (`data/schranka`, `data/odoslane`) | `files/vlaky-subory-<UTC čas>.tar.gz` | v Dockeri pomenované zväzky `vlaky_schranka`, `vlaky_odoslane` |

**Nezálohuje sa** (a netreba): obraz aplikácie (zostaví sa z gitu), dáta siete `data/siet`
(sú v gite), certifikáty Caddy (vydajú sa nanovo). **Tajomstvá** – `.env` a `/etc/vlaky/*.env`,
heslo k zálohám – ulož do firemného trezoru hesiel; bez nich sa server obnovuje pomalšie
a bez hesla restic sa zálohy mimo servera nedajú vôbec obnoviť.

## Kedy a kam

- **Denne o 02:30** (`vlaky-backup.timer`) do `/opt/vlaky/backups` – lokálne sa drží
  `BACKUP_RETENTION_DAYS` (14) dní, vždy aspoň `BACKUP_MIN_KEEP` (3) najnovšie zálohy.
- **Pred aktualizáciou** (`deploy/scripts/update.sh`) – súbor so štítkom `-pred-aktualizaciou`.
- **Pred obnovou** (`restore.sh --db`) – poistná záloha so štítkom `-pred-obnovou`.
- **Mimo servera** (iný poskytovateľ alebo aspoň iný región v EÚ) cez **restic** (odporúčané – šifruje
  a deduplikuje) alebo **rclone**. Bez toho by pri strate servera zanikli aj zálohy – `check.sh`
  to hlási ako upozornenie.
- Existujúca záloha sa nikdy neprepíše (pri zhode času dostane nový súbor poradové číslo).

Ručná záloha: `deploy/backup/backup.sh` (alebo `sudo systemctl start vlaky-backup`).
Výsledok: `journalctl -u vlaky-backup -n 50`. Plán: `systemctl list-timers vlaky-backup.timer`.

## Záloha mimo servera – restic (odporúčané)

1. Priprav úložisko v EÚ (napr. S3 kompatibilný „bucket“ u iného poskytovateľa) a prístupový kľúč,
   ktorý smie len do tohto bucketu. Ak to úložisko podporuje, zapni verzie objektov / object lock
   (ochrana pred zmazaním pri napadnutí servera).
2. Heslo k repozitáru (ulož ho AJ do trezoru hesiel a na papier do trezoru – bez neho sa nedá obnoviť):
   ```bash
   sudo sh -c 'openssl rand -base64 32 > /etc/vlaky/restic.pass'
   sudo chown root:vlaky /etc/vlaky/restic.pass && sudo chmod 640 /etc/vlaky/restic.pass
   ```
3. V `/etc/vlaky/ops.env` nastav `RESTIC_REPOSITORY` (napr. `s3:https://s3.ADRESA/vlaky-zalohy`)
   a odkomentuj `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`.
4. Raz inicializuj repozitár a over zálohu:
   ```bash
   sudo -u vlaky bash -c 'set -a; . /etc/vlaky/ops.env; restic init'
   sudo systemctl start vlaky-backup && journalctl -u vlaky-backup -n 30
   sudo -u vlaky bash -c 'set -a; . /etc/vlaky/ops.env; restic snapshots'
   ```
5. Staré snímky maže `restic forget` podľa `RESTIC_FORGET_ARGS`
   (predvolene 14 denných, 8 týždenných, 6 mesačných). Dlhšie uchovávanie zváž kvôli GDPR.

**Alternatíva rclone:** `sudo rclone config --config /etc/vlaky/rclone.conf` – vytvor vzdialené úložisko
a nad ním šifrované úložisko typu `crypt`; v `ops.env` nastav `RCLONE_REMOTE=nazov-crypt:vlaky`.
Vzdialené zálohy staršie ako `OFFSITE_RETENTION_DAYS` (90) dní sa mažú. Súbor `rclone.conf`
musí byť čitateľný pre skupinu `vlaky` (`chown root:vlaky`, `chmod 640`).

## Častejšie zálohy (cieľ strata dát najviac 1 h)

Denná záloha znamená, že pri havárii sa môžu stratiť zmeny až za 24 h (e-maily však zostávajú
v schránke a agent ich po obnove spracuje znova). Na cieľ z PROMPT.md (najviac 1 h) zapni zálohu
databázy každú hodinu:

```bash
sudo systemctl edit vlaky-backup.timer
```
a vlož:
```ini
[Timer]
OnCalendar=
OnCalendar=hourly
```
Pri hodinových zálohách zváž `BACKUP_RETENTION_DAYS=7` (databáza je malá, ale súborov je veľa).
Kratšiu stratu dát (minúty) dosiahne až priebežná archivácia WAL (pgBackRest, WAL-G) – mimo tohto návodu.

## Obnova

### Zoznam záloh
```bash
deploy/backup/restore.sh --list
```

### Ostrá obnova (havária, poškodená databáza)
```bash
deploy/backup/restore.sh --db /opt/vlaky/backups/db/vlaky-db-20261031T023000Z.sql.gz \
                         --files /opt/vlaky/backups/files/vlaky-subory-20261031T023000Z.tar.gz
```
Skript: overí súbory → ukáže, čo sa stane → **vyžiada napísanie názvu databázy** → uloží poistnú
zálohu aktuálneho stavu → zastaví `web` a `agent` → databázu **zmaže a vytvorí nanovo** → nahrá
zálohu (v jednej transakcii – pri chybe nezostane nič polovičaté) → obnoví súbory → spustí aplikáciu
a počká na jej zdravie. Pri chybe vypíše presný postup (napr. obnovu poistnej zálohy).

Počas obnovy monitoring ohlási výpadok – dispečing vopred upozorni, pracuje podľa e-mailov ako doteraz.
Po obnove môže agent znova spracovať e-maily prijaté po čase zálohy; niektoré upozornenia
tak môžu prísť znova.

Obnova len súborov (databáza sa nemení): `deploy/backup/restore.sh --files SÚBOR.tar.gz`.

### Obnova zo zálohy mimo servera
```bash
sudo -u vlaky bash -c 'set -a; . /etc/vlaky/ops.env; restic snapshots'
sudo -u vlaky bash -c 'set -a; . /etc/vlaky/ops.env; restic restore latest --target /opt/vlaky/obnova'
deploy/backup/restore.sh --db /opt/vlaky/obnova/opt/vlaky/backups/db/vlaky-db-….sql.gz
```

### Strata celého servera (cieľ obnovy do 4 h)
1. Nový VPS v EÚ → `deploy/vps/setup.sh` a `harden-ssh.sh` (docs/PREVADZKA.md, kapitola 4).
2. `git clone` repozitára do `/opt/vlaky/app`, `.env` a `/etc/vlaky/ops.env` z trezoru hesiel.
3. `docker compose build && docker compose up -d db` (len databáza).
4. Stiahni zálohu z restic/rclone (vyššie) a spusti `deploy/backup/restore.sh --db … --files …`.
5. DNS záznam domény presmeruj na novú IP; Caddy sám získa nový certifikát.
6. `deploy/monitoring/check.sh`, prihlásenie do aplikácie, `docker compose exec agent python -m vlaky test-notify`.

Obnovu rob klientom PostgreSQL rovnakej alebo novšej verzie, než akou bola záloha vytvorená
(novšie `pg_dump` píšu do výpisu príkazy `\restrict`, ktorým staršie `psql` nerozumie). V Dockeri
to platí automaticky – obnova beží v kontajneri `db`.

## Mesačný test obnovy (povinný)

Záloha, ktorú nikto neskúsil obnoviť, nie je záloha. Raz mesačne (pripomienka v kalendári správcu):

1. `sudo -iu vlaky` a `cd /opt/vlaky/app`
2. `deploy/backup/restore.sh --test`
   – nahrá najnovšiu zálohu do **dočasnej** databázy `vlaky_obnova_test`, porovná počty riadkov
   s produkciou, overí archív súborov a dočasnú databázu zmaže. Produkcia beží ďalej.
3. Očakávaný výsledok: `TEST OBNOVY ÚSPEŠNÝ`. Záznam sa automaticky pridá do
   `/opt/vlaky/backups/obnova-test.log` (dátum, výsledok, súbor, počet tabuliek, trvanie).
4. **Raz za štvrťrok** testuj zálohu **mimo servera**: `restic restore latest --target /opt/vlaky/obnova`
   (vyššie) a potom `deploy/backup/restore.sh --test /opt/vlaky/obnova/…/vlaky-db-….sql.gz`;
   po teste `rm -rf /opt/vlaky/obnova`.
5. **Raz ročne** nácvik úplnej obnovy na dočasnom VPS podľa časti „Strata celého servera“ –
   zmeraj čas (cieľ 4 h) a doplň do tohto návodu, čo chýbalo.
6. Výsledok zapíš do prevádzkového denníka (kto, kedy, výsledok, čas obnovy, zistenia).

## Riešenie problémov

| Hlásenie | Čo urobiť |
|---|---|
| `Kontajner db nebeží` | `docker compose ps`, `docker compose up -d db`, `docker compose logs --tail 100 db` |
| `Výpis databázy je neúplný` | pg_dump skončil predčasne – pozri `docker compose logs db`; voľné miesto `df -h` |
| `Repozitár restic nie je dostupný…` | over sieť, kľúče a heslo; raz `restic init` (vyššie) |
| `Práve beží iná záloha alebo obnova` | počkaj; ak nič nebeží, súbor zámku `/opt/vlaky/backups/.zaloha.lock` je neškodný – zámok drží len bežiaci proces |
| Plný disk | `deploy/monitoring/check.sh`, zníž `BACKUP_RETENTION_DAYS`, `docker image prune -a --filter until=168h` |
| Upozornenie monitoringu „záloha chýba“ | `systemctl list-timers vlaky-backup.timer`, `journalctl -u vlaky-backup -n 100` |
