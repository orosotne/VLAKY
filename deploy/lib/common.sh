# shellcheck shell=bash
# =============================================================================
# Spoločné funkcie prevádzkových skriptov Agenta dispečera
# (deploy/backup/*.sh, deploy/monitoring/check.sh, deploy/scripts/update.sh).
# Načítava sa príkazom "source"; samostatne sa nespúšťa.
# =============================================================================
set -euo pipefail

# shellcheck disable=SC2034  # farby používajú skripty, ktoré tento súbor načítajú
if [[ -t 1 && -z ${NO_COLOR:-} ]]; then
  C_BOLD=$'\e[1m' C_RED=$'\e[31m' C_YEL=$'\e[33m' C_GRN=$'\e[32m' C_OFF=$'\e[0m'
else
  C_BOLD='' C_RED='' C_YEL='' C_GRN='' C_OFF=''
fi

log()  { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
step() { printf '\n%s==> %s%s\n' "$C_BOLD" "$*" "$C_OFF"; }
ok()   { printf '    %sOK%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn() { printf '%sPOZOR:%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
die()  { printf '%sCHYBA:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

# Načíta prevádzkovú konfiguráciu (/etc/vlaky/ops.env alebo $OPS_ENV_FILE).
# Hodnoty zo súboru majú prednosť pred premennými prostredia.
load_ops_env() {
  local f=${OPS_ENV_FILE:-/etc/vlaky/ops.env}
  if [[ -r $f ]]; then
    set -a
    # shellcheck source=/dev/null
    . "$f"
    set +a
  fi
}

# Otázka áno/nie; ASSUME_YES=1 odpovie áno. Bez terminálu (systemd, cron) skončí chybou.
confirm() {
  local ans
  if [[ ${ASSUME_YES:-0} == 1 ]]; then return 0; fi
  [[ -t 0 ]] || die "Potrebné potvrdenie, ale skript nebeží v termináli (použi --yes)."
  read -r -p "$1 [a/N] " ans
  case ${ans,,} in a|ano|áno|y|yes) return 0 ;; *) return 1 ;; esac
}

# docker compose v adresári aplikácie (APP_DIR musí byť nastavené)
dc() {
  docker compose --project-directory "$APP_DIR" -f "$APP_DIR/docker-compose.yml" "$@"
}

# Stav kontajnera služby: healthy | unhealthy | starting | running | exited | missing
svc_state() {
  local id
  id=$(dc ps -q "$1" 2>/dev/null | head -n 1 || true)
  if [[ -z $id ]]; then
    echo missing
    return 0
  fi
  docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null \
    || echo missing
}

svc_running() {
  local s
  s=$(svc_state "$1")
  [[ $s == healthy || $s == running || $s == starting || $s == unhealthy ]]
}

# postgresql+psycopg://… (SQLAlchemy) → postgresql://… (libpq: pg_dump, psql)
libpq_url() {
  local u=$1
  u=${u/#postgresql+psycopg2:/postgresql:}
  u=${u/#postgresql+psycopg:/postgresql:}
  u=${u/#postgres:/postgresql:}
  printf '%s' "$u"
}

# Názov databázy z URL
url_db_name() {
  local rest=${1#*://}
  rest=${rest#*/}
  printf '%s' "${rest%%\?*}"
}

# Používateľ z URL (prázdne, ak v URL nie je)
url_user() {
  local rest=${1#*://} auth
  [[ $rest == *@* ]] || return 0
  auth=${rest%%@*}
  printf '%s' "${auth%%:*}"
}

# Rovnaká URL s inou databázou
url_with_db() {
  local url=$1 db=$2 query="" scheme rest authority
  if [[ $url == *\?* ]]; then
    query="?${url#*\?}"
    url=${url%%\?*}
  fi
  scheme=${url%%://*}
  rest=${url#*://}
  authority=${rest%%/*}
  printf '%s://%s/%s%s' "$scheme" "$authority" "$db" "$query"
}

# Hodnota premennej zo súboru .env (bez úvodzoviek); prázdne, ak tam nie je
read_env_var() {
  local key=$1 file=$2 line val
  [[ -r $file ]] || return 0
  line=$(grep -E "^[[:space:]]*${key}=" "$file" | tail -n 1 || true)
  [[ -n $line ]] || return 0
  val=${line#*=}
  val=${val%$'\r'}
  if [[ $val == \"*\" || $val == \'*\' ]]; then
    val=${val:1:${#val}-2}
  fi
  printf '%s' "$val"
}

# Najnovší súbor v adresári podľa vzoru (prázdne, ak žiadny)
latest_file() {
  local dir=$1 pattern=$2
  [[ -d $dir ]] || return 0
  find "$dir" -maxdepth 1 -type f -name "$pattern" -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | awk 'NR == 1 { sub(/^[^ ]+ /, ""); print }'
}

human_size() {
  numfmt --to=iec --suffix=B "$1" 2>/dev/null || printf '%s B' "$1"
}

# Ping na monitoring v štýle healthchecks.io: hc_ping URL [/start|/fail] [text]
# Chyba pri pingu nikdy neukončí skript.
hc_ping() {
  local url=${1:-} suffix=${2:-} body=${3:-}
  [[ -n $url ]] || return 0
  local args=(-fsS -m 10 --retry 3 -o /dev/null)
  if [[ -n $body ]]; then args+=(--data-raw "${body:0:10000}"); fi
  curl "${args[@]}" "${url%/}${suffix}" || warn "Nepodarilo sa poslať stav na monitoring (${url%/}${suffix})."
  return 0
}
