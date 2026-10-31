#!/usr/bin/env bash
# =============================================================================
# Obnova Agenta dispečera zo zálohy (vytvorenej deploy/backup/backup.sh)
#
#   deploy/backup/restore.sh --list
#       zoznam dostupných záloh
#
#   deploy/backup/restore.sh --test [ZÁLOHA.sql.gz]
#       MESAČNÝ TEST OBNOVY: nahrá zálohu (predvolene najnovšiu) do DOČASNEJ databázy
#       vlaky_obnova_test, overí tabuľky a počty riadkov, porovná ich s produkciou, overí archív
#       súborov a dočasnú databázu zmaže. Produkcia beží ďalej bez zmeny.
#       Výsledok sa zapíše do $BACKUP_DIR/obnova-test.log.
#
#   deploy/backup/restore.sh --db ZÁLOHA.sql.gz [--files SÚBORY.tar.gz]
#       OSTRÁ OBNOVA: uloží poistnú zálohu aktuálneho stavu, zastaví web a agenta, databázu
#       ZMAŽE a vytvorí NANOVO (prázdnu), nahrá do nej zálohu, voliteľne obnoví súbory
#       (schránka, odoslané) a aplikáciu znova spustí.
#       Vyžaduje výslovné potvrdenie – napísať názov databázy.
#
#   deploy/backup/restore.sh --files SÚBORY.tar.gz
#       obnoví len súbory schránky a odoslaných správ (databázu nemení)
#
# Voľby:
#   --confirm NÁZOV_DB   potvrdenie vopred (bez otázky) – len pri nácviku podľa runbooku
#   --keep               pri --test dočasnú databázu nezmazať (na ručnú kontrolu)
#
# Konfigurácia: /etc/vlaky/ops.env (DEPLOY_MODE, BACKUP_DIR, BACKUP_DATABASE_URL, …).
# Pri inštalácii bez Dockera spúšťaj ostrú obnovu ako root (sudo) – zastavuje služby systemd
# a databázu vytvára ako používateľ postgres (alebo cez RESTORE_ADMIN_URL).
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
BACKUP_DATABASE_URL=${BACKUP_DATABASE_URL:-}
BACKUP_FILES_ROOT=${BACKUP_FILES_ROOT:-/var/lib/vlaky}
RESTORE_TEST_DB=${RESTORE_TEST_DB:-vlaky_obnova_test}
RESTORE_ADMIN_URL=${RESTORE_ADMIN_URL:-}

MODE=""
DB_FILE=""
FILES_FILE=""
CONFIRM=""
KEEP=0
DB_NAME=""
DB_USER=""
RESTORE_PHASE=""

usage() { sed -n '2,/^# =====/{/^# =====/d;s/^# \{0,1\}//;p}' "$0"; }

while (( $# )); do
  case $1 in
    --list)    MODE=list; shift ;;
    --test)    MODE="test"
               if [[ -n ${2:-} && ${2:0:2} != -- ]]; then DB_FILE=$2; shift; fi
               shift ;;
    --db)      MODE=restore; DB_FILE=${2:?--db potrebuje súbor zálohy}; shift 2 ;;
    --files)   FILES_FILE=${2:?--files potrebuje súbor archívu}; shift 2 ;;
    --confirm) CONFIRM=${2:?--confirm potrebuje názov databázy}; shift 2 ;;
    --keep)    KEEP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)         die "Neznáma voľba: $1 (pomoc: $0 --help)" ;;
  esac
done
if [[ -z $MODE && -n $FILES_FILE ]]; then MODE=files; fi
[[ -n $MODE ]] || { usage; exit 2; }

# ------------------------------------------------------------------------------- databáza
db_identity() {  # nastaví DB_NAME a DB_USER produkčnej databázy
  case $DEPLOY_MODE in
    docker)
      svc_running db || die "Kontajner db nebeží – spusti ho: docker compose up -d db"
      DB_NAME=$(dc exec -T db printenv POSTGRES_DB | tr -d '\r')
      DB_USER=$(dc exec -T db printenv POSTGRES_USER | tr -d '\r')
      ;;
    systemd)
      [[ -n $BACKUP_DATABASE_URL ]] || die "Pri DEPLOY_MODE=systemd nastav BACKUP_DATABASE_URL v /etc/vlaky/ops.env."
      DB_NAME=$(url_db_name "$BACKUP_DATABASE_URL")
      DB_USER=$(url_user "$BACKUP_DATABASE_URL")
      ;;
    *) die "Neznámy DEPLOY_MODE=$DEPLOY_MODE (docker | systemd)." ;;
  esac
  [[ -n $DB_NAME && -n $DB_USER ]] || die "Nepodarilo sa zistiť názov databázy alebo používateľa."
}

# psql do servisnej databázy "postgres" s právami správcu (SQL zo štandardného vstupu)
admin_psql() {
  case $DEPLOY_MODE in
    docker)
      dc exec -T db psql -X -q -v ON_ERROR_STOP=1 -U "$DB_USER" -d postgres "$@"
      ;;
    systemd)
      if [[ -n $RESTORE_ADMIN_URL ]]; then
        psql -X -q -v ON_ERROR_STOP=1 "$(libpq_url "$RESTORE_ADMIN_URL")" "$@"
      elif [[ $EUID -eq 0 ]]; then
        runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -d postgres "$@"
      else
        die "Na vytvorenie databázy spusti skript ako root (sudo) alebo nastav RESTORE_ADMIN_URL."
      fi
      ;;
  esac
}

# psql do zvolenej databázy ako vlastník aplikácie
db_psql() {
  local db=$1
  shift
  case $DEPLOY_MODE in
    docker)  dc exec -T db psql -X -q -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$db" "$@" ;;
    systemd) psql -X -q -v ON_ERROR_STOP=1 "$(url_with_db "$(libpq_url "$BACKUP_DATABASE_URL")" "$db")" "$@" ;;
  esac
}

recreate_db() {
  admin_psql -v db="$1" -v owner="$DB_USER" <<'SQL'
SET client_min_messages = warning;
DROP DATABASE IF EXISTS :"db" WITH (FORCE);
CREATE DATABASE :"db" OWNER :"owner";
SQL
}

drop_db() {
  admin_psql -v db="$1" <<'SQL'
SET client_min_messages = warning;
DROP DATABASE IF EXISTS :"db" WITH (FORCE);
SQL
}

load_dump() {  # load_dump DATABÁZA SÚBOR – celé v jednej transakcii (pri chybe nič)
  gzip -dc "$2" | db_psql "$1" --single-transaction >/dev/null
}

# "tabuľka<TAB>počet riadkov" pre všetky tabuľky v schéme public
table_counts() {
  db_psql "$1" -At -F $'\t' <<'SQL'
SELECT table_name,
       (xpath('/row/n/text()',
              query_to_xml(format('SELECT count(*) AS n FROM %I.%I', table_schema, table_name),
                           false, true, '')))[1]::text
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name;
SQL
}

check_dump() {
  local f=$1 t
  [[ -f $f ]] || die "Súbor neexistuje: $f"
  gzip -t "$f" || die "Súbor je poškodený (gzip): $f"
  t=$(gzip -dc "$f" | tail -n 20)
  [[ $t == *"PostgreSQL database dump complete"* ]] || die "Záloha je neúplná (chýba koniec výpisu pg_dump): $f"
}

check_files_archive() {
  [[ -f $1 ]] || die "Súbor neexistuje: $1"
  if ! gzip -t "$1" || ! tar -tzf "$1" >/dev/null; then die "Archív súborov je poškodený: $1"; fi
}

# ------------------------------------------------------------------------------- aplikácia
stop_app() {
  case $DEPLOY_MODE in
    docker)  dc stop web agent ;;
    systemd) systemctl stop vlaky-web.service vlaky-agent.service ;;
  esac
}

start_app() {
  case $DEPLOY_MODE in
    docker)  dc up -d ;;
    systemd) systemctl start vlaky-web.service vlaky-agent.service ;;
  esac
}

restore_files() {
  log "Obnova súborov z ${1##*/}"
  case $DEPLOY_MODE in
    docker)  dc run --rm --no-deps -T agent tar -C /app/data -xzf - < "$1" ;;
    systemd) tar -C "$BACKUP_FILES_ROOT" -xzf "$1" ;;
  esac
}

wait_for_app() {
  local deadline=$(( $(date +%s) + 240 )) web agent
  if [[ $DEPLOY_MODE != docker ]]; then
    sleep 10
    systemctl --no-pager --lines=0 status vlaky-web.service vlaky-agent.service || true
    return 0
  fi
  while :; do
    web=$(svc_state web)
    agent=$(svc_state agent)
    if [[ $web == healthy && $agent == healthy ]]; then
      ok "web aj agent bežia a sú zdravé"
      return 0
    fi
    if (( $(date +%s) > deadline )); then
      warn "Po 4 minútach: web=$web, agent=$agent. Skontroluj: docker compose logs --tail 100 web agent"
      return 0
    fi
    sleep 5
  done
}

ask_db_name() {
  local ans
  if [[ -n $CONFIRM ]]; then
    [[ $CONFIRM == "$DB_NAME" ]] || die "--confirm '$CONFIRM' sa nezhoduje s názvom databázy '$DB_NAME'."
    return 0
  fi
  [[ -t 0 ]] || die "Ostrá obnova vyžaduje potvrdenie v termináli (alebo --confirm $DB_NAME)."
  read -r -p "Na potvrdenie napíš názov databázy ($DB_NAME): " ans
  [[ $ans == "$DB_NAME" ]] || die "Obnova zrušená – nič sa nezmenilo."
}

on_restore_exit() {
  local rc=$?
  if (( rc != 0 )) && [[ -n $RESTORE_PHASE ]]; then
    cat >&2 <<EOF

${C_RED}${C_BOLD}OBNOVA ZLYHALA vo fáze: $RESTORE_PHASE${C_OFF}
  Aplikácia môže byť zastavená a databáza prázdna. Dispečing pracuje podľa e-mailov ako doteraz.
  Postup:
    1. pozri chybu vyššie (napr. docker compose logs db),
    2. obnov poistnú zálohu:  $0 --db "$(latest_file "$BACKUP_DIR/db" 'vlaky-db-*-pred-obnovou.sql.gz')"
    3. alebo spusti aplikáciu bez obnovy: docker compose up -d
EOF
  fi
}

# ------------------------------------------------------------------------------- príkazy
cmd_list() {
  local kind dir pattern d s p
  for kind in db files; do
    if [[ $kind == db ]]; then pattern='vlaky-db-*.sql.gz'; else pattern='vlaky-subory-*.tar.gz'; fi
    dir="$BACKUP_DIR/$kind"
    printf '\n%s%s%s\n' "$C_BOLD" "$dir" "$C_OFF"
    [[ -d $dir ]] || { echo "    (adresár neexistuje)"; continue; }
    while IFS=$'\t' read -r d s p; do
      printf '    %s  %9s  %s\n' "$d" "$(human_size "$s")" "$p"
    done < <(find "$dir" -maxdepth 1 -type f -name "$pattern" -printf '%TY-%Tm-%Td %TH:%TM\t%s\t%p\n' | sort)
  done
  if [[ -f $BACKUP_DIR/obnova-test.log ]]; then
    printf '\n%sPosledné testy obnovy%s (%s)\n' "$C_BOLD" "$C_OFF" "$BACKUP_DIR/obnova-test.log"
    tail -n 5 "$BACKUP_DIR/obnova-test.log" | sed 's/^/    /'
  fi
}

cmd_test() {
  local file=$DB_FILE started dur rows_test rows_prod ntab prod_tmp files nfiles result=OK
  if [[ -z $file ]]; then file=$(latest_file "$BACKUP_DIR/db" 'vlaky-db-*.sql.gz'); fi
  [[ -n $file ]] || die "V $BACKUP_DIR/db nie je žiadna záloha databázy."

  step "Test obnovy: ${file##*/} -> dočasná databáza $RESTORE_TEST_DB"
  check_dump "$file"
  ok "súbor zálohy je celý a nepoškodený ($(human_size "$(stat -c %s "$file")"))"
  db_identity
  [[ $RESTORE_TEST_DB != "$DB_NAME" ]] || die "Testovacia databáza nesmie mať názov produkčnej ($DB_NAME)."

  recreate_db "$RESTORE_TEST_DB"
  started=$(date +%s)
  load_dump "$RESTORE_TEST_DB" "$file"
  dur=$(( $(date +%s) - started ))
  ok "záloha nahraná do $RESTORE_TEST_DB za $dur s"

  rows_test=$(table_counts "$RESTORE_TEST_DB")
  ntab=$(grep -c . <<<"$rows_test" || true)
  rows_prod=$(table_counts "$DB_NAME" 2>/dev/null || true)
  prod_tmp=$(mktemp)
  printf '%s\n' "$rows_prod" > "$prod_tmp"
  printf '\n    %-40s %14s %16s\n' "tabuľka" "v zálohe" "v produkcii teraz"
  awk -F'\t' -v prodfile="$prod_tmp" '
    BEGIN { while ((getline line < prodfile) > 0) { split(line, a, "\t"); if (a[1] != "") prod[a[1]] = a[2] } }
    NF >= 1 && $1 != "" { printf "    %-40s %14s %16s\n", $1, $2, (($1 in prod) ? prod[$1] : "-") }
  ' <<<"$rows_test"
  rm -f "$prod_tmp"
  echo
  if (( ntab > 0 )); then
    ok "obnovená databáza má $ntab tabuliek"
  else
    result=CHYBA
    warn "obnovená databáza neobsahuje žiadne tabuľky!"
  fi

  files=$(latest_file "$BACKUP_DIR/files" 'vlaky-subory-*.tar.gz')
  if [[ -n $files ]]; then
    if gzip -t "$files" && nfiles=$(tar -tzf "$files" | wc -l); then
      ok "archív súborov ${files##*/} je čitateľný ($nfiles položiek)"
    else
      result=CHYBA
      warn "archív súborov ${files##*/} je poškodený!"
    fi
  else
    warn "nenašiel sa žiadny archív súborov (vlaky-subory-*.tar.gz)"
  fi

  if (( KEEP )); then
    warn "dočasná databáza $RESTORE_TEST_DB zostáva (--keep); zmaž ju po kontrole: $0 --test … bez --keep"
  else
    drop_db "$RESTORE_TEST_DB"
    ok "dočasná databáza $RESTORE_TEST_DB zmazaná"
  fi

  printf '%s\t%s\t%s\t%s tabuliek\t%s s\t%s\n' "$(date -Is)" "$result" "${file##*/}" "$ntab" "$dur" "$(id -un)" \
    >> "$BACKUP_DIR/obnova-test.log"
  [[ $result == OK ]] || die "TEST OBNOVY ZLYHAL – zálohy treba opraviť (deploy/backup/README.md)."
  step "TEST OBNOVY ÚSPEŠNÝ – záznam v $BACKUP_DIR/obnova-test.log"
}

cmd_restore() {
  check_dump "$DB_FILE"
  if [[ -n $FILES_FILE ]]; then check_files_archive "$FILES_FILE"; fi
  if [[ $DEPLOY_MODE == systemd && $EUID -ne 0 ]]; then die "Pri inštalácii bez Dockera spusti obnovu ako root (sudo)."; fi
  db_identity
  local sum
  sum=$(sha256sum < "$DB_FILE")

  cat <<EOF

${C_RED}${C_BOLD}OSTRÁ OBNOVA${C_OFF}
  Databáza "${DB_NAME}" sa ZMAŽE a nahradí zálohou:
      $DB_FILE  ($(date -r "$DB_FILE" '+%Y-%m-%d %H:%M'))
  Súbory (schránka, odoslané): ${FILES_FILE:-bez zmeny}
  Web a agent budú počas obnovy zastavené – monitoring ohlási výpadok, dispečing pracuje ručne.
  Pred obnovou sa uloží poistná záloha aktuálneho stavu (…-pred-obnovou.sql.gz).
  Po obnove môže agent znova spracovať e-maily prijaté po čase zálohy (nič sa nestratí,
  niektoré upozornenia však môžu prísť dispečerom znova).
EOF
  ask_db_name
  trap on_restore_exit EXIT

  RESTORE_PHASE="poistná záloha"
  step "1/5 Poistná záloha aktuálneho stavu"
  if ! "$SCRIPT_DIR/backup.sh" --db-only --no-offsite --no-ping --label pred-obnovou; then
    warn "Poistná záloha zlyhala (aktuálna databáza môže byť poškodená)."
    if [[ -z $CONFIRM ]]; then confirm "Pokračovať v obnove BEZ poistnej zálohy?" || die "Obnova zrušená."; fi
  fi

  RESTORE_PHASE=""   # zatiaľ sa nič nezmenilo – pri chybe netreba návod na záchranu
  mkdir -p "$BACKUP_DIR"
  exec 9> "$BACKUP_DIR/.zaloha.lock"
  flock -n 9 || die "Práve beží záloha ($BACKUP_DIR/.zaloha.lock) – skús to o chvíľu."
  # poistka: obnovovaný súbor sa od potvrdenia nesmel zmeniť (napr. prepísať inou zálohou)
  [[ $(sha256sum < "$DB_FILE") == "$sum" ]] || die "Súbor zálohy $DB_FILE sa od potvrdenia zmenil – obnova zastavená, databáza je nezmenená."

  RESTORE_PHASE="zastavenie aplikácie"
  step "2/5 Zastavenie webu a agenta"
  stop_app

  RESTORE_PHASE="obnova databázy"
  step "3/5 Databáza $DB_NAME nanovo a nahratie zálohy"
  recreate_db "$DB_NAME"
  load_dump "$DB_NAME" "$DB_FILE"
  ok "databáza obnovená"

  RESTORE_PHASE="obnova súborov"
  step "4/5 Súbory"
  if [[ -n $FILES_FILE ]]; then restore_files "$FILES_FILE"; ok "súbory obnovené"; else log "bez zmeny"; fi

  RESTORE_PHASE="spustenie aplikácie"
  step "5/5 Spustenie aplikácie"
  start_app
  wait_for_app
  RESTORE_PHASE=""
  step "OBNOVA DOKONČENÁ – over aplikáciu v prehliadači a informuj dispečing."
}

cmd_files() {
  local ans
  check_files_archive "$FILES_FILE"
  if [[ $DEPLOY_MODE == systemd && $EUID -ne 0 ]]; then die "Spusti ako root (sudo)."; fi
  echo "Obnovia sa súbory schránky a odoslaných správ z: $FILES_FILE (rovnomenné súbory sa prepíšu)."
  if [[ -z $CONFIRM ]]; then
    [[ -t 0 ]] || die "Potrebné potvrdenie v termináli."
    read -r -p "Na potvrdenie napíš OBNOVIT: " ans
    [[ $ans == OBNOVIT ]] || die "Zrušené – nič sa nezmenilo."
  fi
  restore_files "$FILES_FILE"
  ok "súbory obnovené"
}

case $MODE in
  list)    cmd_list ;;
  test)    cmd_test ;;
  restore) cmd_restore ;;
  files)   cmd_files ;;
esac
