#!/usr/bin/env bash
# =============================================================================
# Aktualizácia Agenta dispečera (Docker Compose)
#   1. kontrola (čistý pracovný strom gitu, .env, Docker)
#   2. záloha databázy pred aktualizáciou (…-pred-aktualizaciou.sql.gz)
#   3. stiahnutie novej verzie (git pull --ff-only, alebo --ref TAG/VETVA/COMMIT)
#   4. zostavenie obrazu (docker compose build --pull) a aktualizácia obrazov db a caddy
#   5. init-db s NOVÝM kódom (vytvorí nové tabuľky; pri chybe sa beh zastaví, stará verzia beží)
#   6. reštart služieb (docker compose up -d) a kontrola zdravia
#   Pri problémoch vypíše presný postup návratu (rollback).
#
# Použitie (ako používateľ vlaky):
#   cd /opt/vlaky/app && deploy/scripts/update.sh [--ref REF] [--no-backup] [--yes]
#   deploy/scripts/update.sh --rollback      # návrat na obraz a commit pred poslednou aktualizáciou
#
#   --ref REF     nasadiť konkrétny tag, vetvu alebo commit (napr. v1.4.0)
#   --no-backup   bez zálohy pred aktualizáciou (neodporúča sa)
#   --yes         bez otázok
# =============================================================================
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=SCRIPTDIR/../lib/common.sh
. "$SCRIPT_DIR/../lib/common.sh"
load_ops_env

APP_DIR=${VLAKY_APP_DIR:-$(cd -- "$SCRIPT_DIR/../.." && pwd)}
STATE_DIR=${VLAKY_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/vlaky}
APP_IMAGE=vlaky-app
REF=""
DO_BACKUP=1
ROLLBACK=0
ASSUME_YES=${ASSUME_YES:-0}

usage() { sed -n '2,/^# =====/{/^# =====/d;s/^# \{0,1\}//;p}' "$0"; }

while (( $# )); do
  case $1 in
    --ref)       REF=${2:?--ref potrebuje tag, vetvu alebo commit}; shift 2 ;;
    --no-backup) DO_BACKUP=0; shift ;;
    --rollback)  ROLLBACK=1; shift ;;
    --yes|-y)    ASSUME_YES=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "Neznáma voľba: $1 (pomoc: $0 --help)" ;;
  esac
done

wait_healthy() {  # čaká, kým sú web a agent "healthy" (najviac $1 s)
  local deadline=$(( $(date +%s) + ${1:-300} )) web agent
  while :; do
    web=$(svc_state web)
    agent=$(svc_state agent)
    if [[ $web == healthy && $agent == healthy ]]; then
      ok "web: $web, agent: $agent"
      return 0
    fi
    if (( $(date +%s) > deadline )); then
      warn "web: $web, agent: $agent"
      return 1
    fi
    sleep 5
  done
}

check_https() {
  local domain code
  domain=$(read_env_var VLAKY_DOMAIN "$APP_DIR/.env")
  [[ -n $domain ]] || return 0
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 --resolve "$domain:443:127.0.0.1" "https://$domain/health" || true)
  if [[ $code == 200 ]]; then ok "https://$domain/health odpovedá 200"; else warn "https://$domain/health odpovedá '$code'"; fi
}

rollback_hint() {
  local prev=$1 pre_backup
  pre_backup=$(latest_file "${BACKUP_DIR:-/opt/vlaky/backups}/db" 'vlaky-db-*-pred-aktualizaciou.sql.gz')
  cat <<EOF

${C_BOLD}Návrat na predchádzajúcu verziu (rollback):${C_OFF}
  rýchlo (predchádzajúci obraz):   $0 --rollback
  ručne:                           git checkout --detach $prev && docker compose build && docker compose up -d
  ak nová verzia zmenila databázu: deploy/backup/restore.sh --db ${pre_backup:-<záloha pred aktualizáciou>}
EOF
}

do_rollback() {
  local prev
  [[ -r $STATE_DIR/rollback-commit ]] || die "Chýba záznam o predchádzajúcej verzii ($STATE_DIR/rollback-commit)."
  prev=$(<"$STATE_DIR/rollback-commit")
  docker image inspect "$APP_IMAGE:rollback" >/dev/null 2>&1 || die "Chýba obraz $APP_IMAGE:rollback (žiadna aktualizácia cez tento skript?)."
  [[ -z $(git status --porcelain --untracked-files=no) ]] || die "V repozitári sú lokálne zmeny – najprv ich odlož (git stash)."
  step "Návrat na commit $prev a predchádzajúci obraz"
  confirm "Vrátiť aplikáciu na verziu $(git log -1 --format='%h %s' "$prev" 2>/dev/null || echo "$prev")?" || die "Zrušené."
  git checkout --quiet --detach "$prev"
  docker image tag "$APP_IMAGE:rollback" "$APP_IMAGE:latest"
  dc up -d --no-build --remove-orphans
  if wait_healthy 300; then ok "predchádzajúca verzia beží"; else warn "služby nie sú zdravé – docker compose logs --tail 100 web agent"; fi
  printf '%s\trollback\t%s\n' "$(date -Is)" "$prev" >> "$STATE_DIR/deploy.log"
  warn "Ak nová verzia zmenila databázu, obnov zálohu pred aktualizáciou (deploy/backup/restore.sh --list)."
  warn "Repozitár je teraz na pevnom commite; ďalšia aktualizácia:  $0 --ref main   (alebo konkrétny tag)"
}

# ------------------------------------------------------------------------------- kontroly
cd "$APP_DIR"
command -v docker >/dev/null || die "Docker nie je nainštalovaný."
command -v git >/dev/null || die "git nie je nainštalovaný."
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$APP_DIR nie je repozitár git."
[[ -f .env ]] || die "Chýba $APP_DIR/.env (cp .env.example .env a vyplniť)."
docker info >/dev/null 2>&1 || die "Docker nebeží alebo nemáš práva (používateľ musí byť v skupine docker)."
mkdir -p "$STATE_DIR"

if (( ROLLBACK )); then
  do_rollback
  exit 0
fi

dirty=$(git status --porcelain --untracked-files=no)
if [[ -n $dirty ]]; then
  printf '%s\n' "$dirty" | sed 's/^/    /' >&2
  die "V repozitári sú lokálne zmeny (vyššie). Odlož ich (git stash), zahoď (git checkout -- SÚBOR)
       alebo ich zapíš do vlastnej vetvy. Typicky ide o data/siet po importe OSM – pozri docs/PREVADZKA.md."
fi

prev=$(git rev-parse HEAD)
step "Stiahnutie novej verzie"
git fetch --prune --tags origin
if [[ -n $REF ]]; then
  if git rev-parse --verify --quiet "refs/remotes/origin/$REF" >/dev/null; then
    target="origin/$REF"
  else
    target=$REF
  fi
else
  upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) \
    || die "Aktuálna vetva nemá nastavenú vzdialenú vetvu (napr. po rollbacku). Použi --ref main alebo --ref TAG."
  target=$upstream
fi
new=$(git rev-parse --verify --quiet "$target^{commit}") || die "Verzia '$target' neexistuje."

if [[ $new == "$prev" ]]; then
  ok "kód je aktuálny ($(git log -1 --format='%h %s' "$prev")) – obraz sa aj tak zostaví nanovo"
  question="Zostaviť a reštartovať aplikáciu (rovnaký kód, aktuálne bezpečnostné záplaty základných obrazov)?"
else
  printf '    Zmeny %s -> %s:\n' "$(git rev-parse --short "$prev")" "$(git rev-parse --short "$new")"
  git log --oneline --no-decorate "$prev..$new" | head -n 30 | sed 's/^/      /' || true
  if git merge-base --is-ancestor "$new" "$prev"; then warn "Cieľová verzia je STARŠIA ako aktuálna."; fi
  question="Aktualizovať aplikáciu na $(git rev-parse --short "$new")?"
fi
confirm "$question" || die "Zrušené – nič sa nezmenilo."

if (( DO_BACKUP )); then
  step "Záloha databázy pred aktualizáciou"
  "$APP_DIR/deploy/backup/backup.sh" --db-only --no-offsite --no-ping --label pred-aktualizaciou \
    || die "Záloha zlyhala – aktualizácia sa nezačala (vynechať: --no-backup, neodporúča sa)."
fi

step "Príprava návratu (rollback)"
printf '%s\n' "$prev" > "$STATE_DIR/rollback-commit"
if docker image inspect "$APP_IMAGE:latest" >/dev/null 2>&1; then
  docker image tag "$APP_IMAGE:latest" "$APP_IMAGE:rollback"
  ok "aktuálny obraz označený ako $APP_IMAGE:rollback, commit $prev"
fi

step "Prepnutie kódu"
if [[ -n $REF ]]; then
  git checkout --quiet --detach "$new"
else
  git merge --ff-only --quiet "$target"
fi
ok "kód: $(git log -1 --format='%h %s')"

step "Zostavenie obrazu a aktualizácia obrazov db/caddy"
if ! dc build --pull || ! dc pull db caddy; then
  rollback_hint "$prev"
  die "Zostavenie alebo stiahnutie obrazov zlyhalo – bežiace služby sa NEZMENILI (stále stará verzia).
       Pri dočasnej chybe (sieť, Docker Hub) spusti aktualizáciu znova; inak vráť kód: git checkout --detach $prev"
fi

step "Databáza: init-db s novým kódom"
if ! dc run --rm init-db; then
  rollback_hint "$prev"
  die "init-db zlyhal – bežiace služby ešte používajú starú verziu (nereštartovali sa).
       Vráť kód:  git checkout --detach $prev   (alebo oprav chybu a spusti aktualizáciu znova)."
fi

step "Reštart služieb"
if ! dc up -d --remove-orphans; then
  rollback_hint "$prev"
  die "Spustenie služieb zlyhalo – pozri docker compose ps a docker compose logs --tail 100."
fi
if wait_healthy 300; then
  check_https
  docker image prune -f >/dev/null || true
  printf '%s\tupdate\t%s -> %s\n' "$(date -Is)" "$prev" "$(git rev-parse HEAD)" >> "$STATE_DIR/deploy.log"
  step "AKTUALIZÁCIA DOKONČENÁ"
  rollback_hint "$prev"
else
  printf '%s\tupdate-NEZDRAVE\t%s -> %s\n' "$(date -Is)" "$prev" "$(git rev-parse HEAD)" >> "$STATE_DIR/deploy.log"
  warn "Služby nie sú po 5 minútach zdravé. Pozri:  docker compose ps;  docker compose logs --tail 100 web agent"
  rollback_hint "$prev"
  exit 1
fi
