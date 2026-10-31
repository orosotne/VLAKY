#!/usr/bin/env bash
# =============================================================================
# Záloha Agenta dispečera
#   - databáza: pg_dump (čistý SQL) | gzip  ->  $BACKUP_DIR/db/vlaky-db-<UTC čas>[-štítok].sql.gz
#   - súbory:   data/schranka + data/odoslane ->  $BACKUP_DIR/files/vlaky-subory-<UTC čas>.tar.gz
#   - retencia: staršie ako BACKUP_RETENTION_DAYS dní sa zmažú (vždy zostane BACKUP_MIN_KEEP najnovších)
#   - mimo servera (voliteľné): restic (RESTIC_REPOSITORY) alebo rclone (RCLONE_REMOTE)
#   - monitoring (voliteľné): ping na BACKUP_HEARTBEAT_URL (pri chybe .../fail)
#
# Použitie:  deploy/backup/backup.sh [--db-only] [--no-offsite] [--label TEXT]
#   --db-only     len databáza (bez súborov schránky a odoslaných správ)
#   --no-offsite  bez kópie mimo servera (napr. rýchla záloha pred aktualizáciou)
#   --label TEXT  štítok v názve súboru (malé písmená, číslice, pomlčky), napr. pred-aktualizaciou
#   --no-ping     neposielať stav na BACKUP_HEARTBEAT_URL (príležitostné zálohy mimo časovača)
#
# Konfigurácia: /etc/vlaky/ops.env (vzor deploy/ops.env.example) alebo premenné prostredia.
# Spúšťa ju denne vlaky-backup.timer (deploy/systemd). Návratový kód 0 = OK, inak chyba.
# =============================================================================
set -euo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=SCRIPTDIR/../lib/common.sh
. "$SCRIPT_DIR/../lib/common.sh"
load_ops_env

DEPLOY_MODE=${DEPLOY_MODE:-docker}
APP_DIR=${VLAKY_APP_DIR:-$(cd -- "$SCRIPT_DIR/../.." && pwd)}
BACKUP_DIR=${BACKUP_DIR:-/opt/vlaky/backups}
BACKUP_RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-14}
BACKUP_MIN_KEEP=${BACKUP_MIN_KEEP:-3}
BACKUP_DATABASE_URL=${BACKUP_DATABASE_URL:-}
BACKUP_FILES_ROOT=${BACKUP_FILES_ROOT:-/var/lib/vlaky}
BACKUP_HEARTBEAT_URL=${BACKUP_HEARTBEAT_URL:-}
RESTIC_REPOSITORY=${RESTIC_REPOSITORY:-}
RESTIC_FORGET_ARGS=${RESTIC_FORGET_ARGS:---keep-daily 14 --keep-weekly 8 --keep-monthly 6}
RCLONE_REMOTE=${RCLONE_REMOTE:-}
OFFSITE_RETENTION_DAYS=${OFFSITE_RETENTION_DAYS:-90}

DB_ONLY=0
OFFSITE=1
LABEL=""
DB_FILE=""
SUCCESS=0
CURRENT_STEP="príprava"
TMP_FILES=()

usage() { sed -n '2,/^# =====/{/^# =====/d;s/^# \{0,1\}//;p}' "$0"; }

while (( $# )); do
  case $1 in
    --db-only)    DB_ONLY=1; shift ;;
    --no-offsite) OFFSITE=0; shift ;;
    --label)      LABEL=${2:?--label potrebuje text}; shift 2 ;;
    --no-ping)    BACKUP_HEARTBEAT_URL=""; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            die "Neznáma voľba: $1" ;;
  esac
done
[[ $LABEL =~ ^[a-z0-9-]*$ ]] || die "Štítok smie obsahovať len malé písmená bez diakritiky, číslice a pomlčky."
[[ $BACKUP_RETENTION_DAYS =~ ^[0-9]+$ && $BACKUP_MIN_KEEP =~ ^[0-9]+$ ]] \
  || die "BACKUP_RETENTION_DAYS a BACKUP_MIN_KEEP musia byť čísla."

on_exit() {
  local rc=$?
  if (( ${#TMP_FILES[@]} )); then rm -f -- "${TMP_FILES[@]}"; fi
  if (( rc == 0 && SUCCESS )); then
    hc_ping "$BACKUP_HEARTBEAT_URL" "" "Záloha OK na $(hostname): ${DB_FILE##*/}"
  else
    printf '%sCHYBA:%s záloha zlyhala (krok: %s, kód %s)\n' "$C_RED" "$C_OFF" "$CURRENT_STEP" "$rc" >&2
    hc_ping "$BACKUP_HEARTBEAT_URL" /fail "Záloha ZLYHALA na $(hostname) – krok: $CURRENT_STEP, kód $rc. Pozri: journalctl -u vlaky-backup"
    if (( rc == 0 )); then rc=1; fi
  fi
  exit "$rc"
}
trap on_exit EXIT

# ---------------------------------------------------------------------------------------------
dump_db() {
  local out=$1 tmp="$1.tmp" tail_lines
  TMP_FILES+=("$tmp")
  log "Databáza -> $out"
  case $DEPLOY_MODE in
    docker)
      svc_running db || die "Kontajner db nebeží (docker compose ps)."
      # shellcheck disable=SC2016  # premenné sa rozvinú až v kontajneri db
      dc exec -T db sh -c 'exec pg_dump --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
        | gzip -6 > "$tmp"
      ;;
    systemd)
      [[ -n $BACKUP_DATABASE_URL ]] || die "Pri DEPLOY_MODE=systemd nastav BACKUP_DATABASE_URL v /etc/vlaky/ops.env."
      command -v pg_dump >/dev/null || die "Chýba pg_dump (sudo apt install postgresql-client)."
      pg_dump --no-owner --no-privileges --dbname="$(libpq_url "$BACKUP_DATABASE_URL")" | gzip -6 > "$tmp"
      ;;
    *) die "Neznámy DEPLOY_MODE=$DEPLOY_MODE (docker | systemd)." ;;
  esac
  gzip -t "$tmp" || die "Súbor zálohy je poškodený: $tmp"
  # pg_dump píše na koniec úspešného výpisu "PostgreSQL database dump complete"
  tail_lines=$(gzip -dc "$tmp" | tail -n 20)
  [[ $tail_lines == *"PostgreSQL database dump complete"* ]] || die "Výpis databázy je neúplný."
  [[ ! -e $out ]] || die "Súbor $out už existuje – existujúca záloha sa neprepisuje."
  mv -- "$tmp" "$out"
  log "  hotovo, $(human_size "$(stat -c %s "$out")")"
}

dump_files() {
  local out=$1 tmp="$1.tmp" rc=0 dirs=() d
  TMP_FILES+=("$tmp")
  log "Súbory (schránka, odoslané) -> $out"
  case $DEPLOY_MODE in
    docker)
      if svc_running agent; then
        dc exec -T agent tar -C /app/data -czf - schranka odoslane > "$tmp" || rc=$?
      else
        dc run --rm --no-deps -T agent tar -C /app/data -czf - schranka odoslane > "$tmp" || rc=$?
      fi
      ;;
    systemd)
      for d in schranka odoslane; do
        if [[ -d $BACKUP_FILES_ROOT/$d ]]; then dirs+=("$d"); fi
      done
      if (( ${#dirs[@]} == 0 )); then
        warn "V $BACKUP_FILES_ROOT nie sú adresáre schranka/odoslane – záloha súborov sa preskakuje."
        return 0
      fi
      tar -C "$BACKUP_FILES_ROOT" -czf "$tmp" "${dirs[@]}" || rc=$?
      ;;
  esac
  # tar: 1 = súbor sa zmenil počas čítania (agent práve zapisoval) – záloha je použiteľná
  if (( rc == 1 )); then
    warn "Niektorý súbor sa počas zálohy zmenil (tar kód 1) – záloha je použiteľná."
  elif (( rc != 0 )); then
    die "Archivácia súborov zlyhala (kód $rc)."
  fi
  if ! gzip -t "$tmp" || ! tar -tzf "$tmp" >/dev/null; then die "Archív súborov je poškodený."; fi
  [[ ! -e $out ]] || die "Súbor $out už existuje – existujúca záloha sa neprepisuje."
  mv -- "$tmp" "$out"
  log "  hotovo, $(human_size "$(stat -c %s "$out")")"
}

# Zmaže zálohy staršie ako BACKUP_RETENTION_DAYS; BACKUP_MIN_KEEP najnovších zostane vždy
prune() {
  local dir=$1 pattern=$2 i=0 cutoff line mtime f
  cutoff=$(( $(date +%s) - BACKUP_RETENTION_DAYS * 86400 ))
  while IFS= read -r -d '' line; do
    i=$(( i + 1 ))
    mtime=${line%% *}
    mtime=${mtime%.*}
    f=${line#* }
    if (( i > BACKUP_MIN_KEEP && mtime < cutoff )); then
      rm -f -- "$f"
      log "  zmazaná stará záloha ${f##*/}"
    fi
  done < <(find "$dir" -maxdepth 1 -type f -name "$pattern" -printf '%T@ %p\0' | sort -z -rn)
}

offsite() {
  local done_any=0 forget_args=()
  if [[ -n $RESTIC_REPOSITORY ]]; then
    command -v restic >/dev/null || die "restic nie je nainštalovaný (sudo apt install restic)."
    restic cat config >/dev/null 2>&1 \
      || die "Repozitár restic ($RESTIC_REPOSITORY) nie je dostupný alebo nie je inicializovaný – pozri deploy/backup/README.md (restic init)."
    log "Kópia mimo servera: restic -> $RESTIC_REPOSITORY"
    restic backup --quiet --tag vlaky --host "$(hostname -s)" "$BACKUP_DIR/db" "$BACKUP_DIR/files"
    read -r -a forget_args <<< "$RESTIC_FORGET_ARGS"
    restic forget --quiet --tag vlaky --host "$(hostname -s)" "${forget_args[@]}" --prune
    done_any=1
  fi
  if [[ -n $RCLONE_REMOTE ]]; then
    command -v rclone >/dev/null || die "rclone nie je nainštalovaný (sudo apt install rclone)."
    log "Kópia mimo servera: rclone -> $RCLONE_REMOTE"
    rclone copy "$BACKUP_DIR" "$RCLONE_REMOTE" --include '/db/**' --include '/files/**'
    rclone delete "$RCLONE_REMOTE" --min-age "${OFFSITE_RETENTION_DAYS}d" --include '/db/**' --include '/files/**'
    done_any=1
  fi
  if (( ! done_any )); then
    warn "Záloha mimo servera NIE JE nastavená (RESTIC_REPOSITORY alebo RCLONE_REMOTE v /etc/vlaky/ops.env)."
    warn "Pri strate servera by sa stratili aj zálohy!"
  fi
}

# ---------------------------------------------------------------------------------------------
mkdir -p "$BACKUP_DIR/db" "$BACKUP_DIR/files"
exec 9> "$BACKUP_DIR/.zaloha.lock"
flock -n 9 || die "Práve beží iná záloha alebo obnova ($BACKUP_DIR/.zaloha.lock)."
hc_ping "$BACKUP_HEARTBEAT_URL" /start

# Existujúca záloha sa nikdy neprepíše: pri rovnakom čase (napr. dve zálohy v jednej sekunde)
# dostane nový súbor poradové číslo pred štítkom: vlaky-db-<čas>-2[-štítok].sql.gz
unique_name() {  # unique_name ADRESÁR PREDPONA PRÍPONA_ŠTÍTKU KONCOVKA
  local dir=$1 prefix=$2 suffix=$3 ext=$4 n=1 p
  p="$dir/$prefix$suffix$ext"
  while [[ -e $p || -e $p.tmp ]]; do
    n=$(( n + 1 ))
    p="$dir/$prefix-$n$suffix$ext"
  done
  printf '%s' "$p"
}

TS=$(date -u +%Y%m%dT%H%M%SZ)
SUFFIX=${LABEL:+-$LABEL}
DB_FILE=$(unique_name "$BACKUP_DIR/db" "vlaky-db-$TS" "$SUFFIX" .sql.gz)
log "Záloha Agenta dispečera (režim $DEPLOY_MODE, cieľ $BACKUP_DIR)"

CURRENT_STEP="záloha databázy"
dump_db "$DB_FILE"

if (( ! DB_ONLY )); then
  CURRENT_STEP="záloha súborov"
  dump_files "$(unique_name "$BACKUP_DIR/files" "vlaky-subory-$TS" "$SUFFIX" .tar.gz)"
fi

CURRENT_STEP="mazanie starých záloh"
prune "$BACKUP_DIR/db" 'vlaky-db-*.sql.gz'
prune "$BACKUP_DIR/files" 'vlaky-subory-*.tar.gz'

if (( OFFSITE )); then
  CURRENT_STEP="kópia mimo servera"
  offsite
fi

SUCCESS=1
log "Záloha dokončená: $DB_FILE"
