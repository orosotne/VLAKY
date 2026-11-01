#!/usr/bin/env bash
# =============================================================================
# Kontrola servera Agenta dispečera (spúšťa vlaky-check.timer každých 15 min; dá sa aj ručne)
#   - voľné miesto na disku a inódy (/, Docker, zálohy)
#   - vek poslednej zálohy databázy
#   - bežia a sú zdravé kontajnery db, web, agent, caddy (alebo služby systemd)
#   - zdravie agenta (python -m vlaky health: cyklus, schránka, doručovanie, nastavenie) – pri Dockeri ho
#     kontroluje healthcheck kontajnera agent, bez Dockera (systemd) ho spustí tento skript
#   - HTTPS https://DOMÉNA/health cez lokálny Caddy a platnosť certifikátu; doména z CHECK_URL, VLAKY_DOMAIN
#     alebo VLAKY_PUBLIC_URL (súbor CHECK_ENV_FILE: pri Dockeri .env, bez Dockera /etc/vlaky/vlaky.env) –
#     neznáma doména je CHYBA (HTTPS ani certifikát by sa nekontrolovali)
#   - upozornenia: čakajúci reštart po aktualizácii, chýbajúca záloha mimo servera, práva .env
#
# Návratový kód: 0 = OK (môžu byť upozornenia POZOR), 1 = aspoň jedna CHYBA (dôvod je vo výpise).
# Voliteľne pošle výsledok na CHECK_HEARTBEAT_URL (healthchecks.io: OK -> ping, chyba -> /fail),
# takže o probléme sa dozvieš, aj keď nikto nečíta logy servera.
#
# Použitie:  deploy/monitoring/check.sh
# Konfigurácia: /etc/vlaky/ops.env (CHECK_*, BACKUP_DIR, DEPLOY_MODE) – vzor deploy/ops.env.example
# =============================================================================
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=SCRIPTDIR/../lib/common.sh
. "$SCRIPT_DIR/../lib/common.sh"
load_ops_env

DEPLOY_MODE=${DEPLOY_MODE:-docker}
APP_DIR=${VLAKY_APP_DIR:-$(cd -- "$SCRIPT_DIR/../.." && pwd)}
BACKUP_DIR=${BACKUP_DIR:-/opt/vlaky/backups}
CHECK_DISK_PATHS=${CHECK_DISK_PATHS:-/ /var/lib/docker /opt/vlaky/backups}
CHECK_DISK_MAX_PCT=${CHECK_DISK_MAX_PCT:-85}
CHECK_BACKUP_MAX_AGE_HOURS=${CHECK_BACKUP_MAX_AGE_HOURS:-26}
CHECK_CERT_MIN_DAYS=${CHECK_CERT_MIN_DAYS:-10}
CHECK_URL=${CHECK_URL:-}
CHECK_HEARTBEAT_URL=${CHECK_HEARTBEAT_URL:-}
if [[ $DEPLOY_MODE == docker ]]; then
  CHECK_SERVICES=${CHECK_SERVICES:-db web agent caddy}
  CHECK_ENV_FILE=${CHECK_ENV_FILE:-$APP_DIR/.env}
else
  CHECK_SERVICES=${CHECK_SERVICES:-vlaky-web vlaky-agent postgresql caddy}
  CHECK_ENV_FILE=${CHECK_ENV_FILE:-/etc/vlaky/vlaky.env}
fi
CHECK_PYTHON=${CHECK_PYTHON:-/opt/vlaky/venv/bin/python}

FAILS=()
WARNS=()
pass()  { printf 'OK     %s\n' "$*"; }
fail()  { FAILS+=("$*"); printf '%sCHYBA%s  %s\n' "$C_RED" "$C_OFF" "$*"; }
notice() { WARNS+=("$*"); printf '%sPOZOR%s  %s\n' "$C_YEL" "$C_OFF" "$*"; }

check_disk() {
  local p pct ipct
  for p in $CHECK_DISK_PATHS; do
    [[ -e $p ]] || continue
    pct=$(df -P "$p" 2>/dev/null | awk 'NR == 2 { sub(/%/, "", $5); print $5 }' || true)
    ipct=$(df -Pi "$p" 2>/dev/null | awk 'NR == 2 { sub(/%/, "", $5); print $5 }' || true)
    if [[ ! $pct =~ ^[0-9]+$ ]]; then
      notice "disk $p: zaplnenie sa nedá zistiť"
    elif (( pct >= CHECK_DISK_MAX_PCT )); then
      fail "disk $p je zaplnený na $pct % (limit $CHECK_DISK_MAX_PCT %) – pozri docs/PREVADZKA.md, kapitola 13 (Plný disk)"
    else
      pass "disk $p: $pct % zaplnené"
    fi
    if [[ $ipct =~ ^[0-9]+$ ]] && (( ipct >= CHECK_DISK_MAX_PCT )); then
      fail "disk $p: minú sa inódy ($ipct %)"
    fi
  done
}

check_backup() {
  local newest mtime age_h
  newest=$(latest_file "$BACKUP_DIR/db" 'vlaky-db-*.sql.gz')
  if [[ -z $newest ]]; then
    fail "v $BACKUP_DIR/db nie je žiadna záloha databázy"
    return 0
  fi
  mtime=$(stat -c %Y "$newest")
  age_h=$(( ( $(date +%s) - mtime ) / 3600 ))
  if (( age_h > CHECK_BACKUP_MAX_AGE_HOURS )); then
    fail "posledná záloha databázy je stará $age_h h (limit $CHECK_BACKUP_MAX_AGE_HOURS h): ${newest##*/} – journalctl -u vlaky-backup"
  else
    pass "posledná záloha databázy: pred $age_h h (${newest##*/})"
  fi
  if [[ -z ${RESTIC_REPOSITORY:-} && -z ${RCLONE_REMOTE:-} ]]; then
    notice "záloha mimo servera nie je nastavená (RESTIC_REPOSITORY / RCLONE_REMOTE v /etc/vlaky/ops.env)"
  fi
}

check_services() {
  local svc st
  for svc in $CHECK_SERVICES; do
    if [[ $DEPLOY_MODE == docker ]]; then
      st=$(svc_state "$svc")
      case $st in
        healthy|running) pass "kontajner $svc: $st" ;;
        starting)        notice "kontajner $svc sa ešte spúšťa" ;;
        *)               fail "kontajner $svc: $st – docker compose ps; docker compose logs --tail 100 $svc" ;;
      esac
    else
      if systemctl is-active --quiet "$svc"; then
        pass "služba $svc beží"
      else
        fail "služba $svc nebeží – systemctl status $svc; journalctl -u $svc -n 100"
      fi
    fi
  done
}

# Doména aplikácie: VLAKY_DOMAIN (prostredie alebo CHECK_ENV_FILE), inak hostiteľ z https:// VLAKY_PUBLIC_URL
app_domain() {
  local domain public
  domain=${VLAKY_DOMAIN:-$(read_env_var VLAKY_DOMAIN "$CHECK_ENV_FILE")}
  if [[ -z $domain ]]; then
    public=${VLAKY_PUBLIC_URL:-$(read_env_var VLAKY_PUBLIC_URL "$CHECK_ENV_FILE")}
    if [[ $public == https://* ]]; then
      domain=${public#https://}
      domain=${domain%%/*}
      domain=${domain%%:*}
    fi
  fi
  printf '%s' "$domain"
}

check_https() {
  local domain url code end end_s days curl_args=(-s -o /dev/null -w '%{http_code}' -m 15)
  domain=$(app_domain)
  if [[ -z $domain && -z $CHECK_URL ]]; then
    fail "HTTPS a certifikát sa NEKONTROLUJÚ: neznáma doména – nastav CHECK_URL v ops.env alebo VLAKY_DOMAIN či VLAKY_PUBLIC_URL (https://…) v $CHECK_ENV_FILE"
    return 0
  fi
  url=${CHECK_URL:-https://$domain/health}
  # cez lokálny Caddy (nezávisle od DNS); certifikát sa overuje voči doméne
  if [[ -n $domain ]]; then curl_args+=(--resolve "$domain:443:127.0.0.1"); fi
  code=$(curl "${curl_args[@]}" "$url" || true)
  if [[ $code == 200 ]]; then
    pass "$url odpovedá 200"
  elif [[ -z $code || $code == 000 ]]; then
    fail "$url je nedostupné (spojenie/TLS zlyhalo) – docker compose ps; docker compose logs --tail 100 caddy web"
  else
    fail "$url odpovedá $code (očakávané 200) – docker compose logs --tail 100 caddy web"
  fi

  [[ -n $domain ]] || return 0
  end=$(echo | openssl s_client -connect 127.0.0.1:443 -servername "$domain" 2>/dev/null \
          | openssl x509 -noout -enddate 2>/dev/null || true)
  end=${end#notAfter=}
  if [[ -z $end ]]; then
    fail "nepodarilo sa načítať TLS certifikát pre $domain z lokálneho Caddy"
    return 0
  fi
  end_s=$(date -d "$end" +%s)
  days=$(( (end_s - $(date +%s)) / 86400 ))
  if (( days < CHECK_CERT_MIN_DAYS )); then
    fail "certifikát pre $domain vyprší o $days dní ($end) – docker compose logs caddy | grep -i acme"
  else
    pass "certifikát pre $domain platí ešte $days dní"
  fi
}

# Zdravie agenta (DB, posledný cyklus, schránka, nedoručené správy, nastavenie odosielania). Pri Dockeri ho
# každú minútu overuje healthcheck kontajnera agent (stav „unhealthy“ ohlási check_services); bez Dockera ho
# spustí tento skript s konfiguráciou služieb (CHECK_ENV_FILE) – inak by sa chyba zdravia nikde neohlásila.
check_agent_health() {
  local out rc
  if [[ $DEPLOY_MODE == docker ]]; then
    return 0
  fi
  if [[ ! -x $CHECK_PYTHON ]]; then
    fail "zdravie agenta sa nedá overiť: chýba $CHECK_PYTHON (nastav CHECK_PYTHON v ops.env)"
    return 0
  fi
  if [[ ! -r $CHECK_ENV_FILE ]]; then
    fail "zdravie agenta sa nedá overiť: konfigurácia $CHECK_ENV_FILE sa nedá čítať (CHECK_ENV_FILE v ops.env)"
    return 0
  fi
  rc=0
  out=$(cd "$APP_DIR" && run_with_env_file "$CHECK_ENV_FILE" timeout 120 "$CHECK_PYTHON" -m vlaky health 2>&1) || rc=$?
  out=$(printf '%s' "$out" | tail -n 3 | tr '\n' ' ')
  if (( rc == 0 )); then
    pass "zdravie agenta: OK"
  else
    # podrobnosti len do výpisu (môžu obsahovať interné adresy) – na externý monitoring ide len riadok CHYBA
    fail "zdravie agenta: PROBLÉM (python -m vlaky health, kód $rc) – podrobnosti vo výpise a na stránke /zdravie; docs/PREVADZKA.md, kapitola 13"
    printf '       %s\n' "${out:0:800}"
  fi
}

# Spustí príkaz s premennými zo súboru KĽÚČ=hodnota (ako EnvironmentFile služieb systemd), bez vykonania
# obsahu súboru ako skriptu (hodnoty s medzerami, úvodzovky okolo hodnoty sa odstránia).
run_with_env_file() {
  local file=$1 line key val
  shift
  (
    while IFS= read -r line || [[ -n $line ]]; do
      line=${line%$'\r'}
      [[ $line =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
      key=${BASH_REMATCH[1]}
      val=${BASH_REMATCH[2]}
      if [[ $val == \"*\" || $val == \'*\' ]]; then
        val=${val:1:${#val}-2}
      fi
      export "$key=$val"
    done < "$file"
    "$@"
  )
}

check_misc() {
  local mode
  if [[ -f /var/run/reboot-required ]]; then
    notice "aktualizácie čakajú na reštart servera (naplánuj: sudo reboot)"
  fi
  if [[ $DEPLOY_MODE == docker && -f $APP_DIR/.env ]]; then
    mode=$(stat -c %a "$APP_DIR/.env")
    if (( (8#$mode & 8#077) != 0 )); then
      notice "$APP_DIR/.env je čitateľný aj pre iných (práva $mode) – oprav: chmod 600 $APP_DIR/.env"
    fi
  fi
}

printf 'Kontrola servera %s – %s (režim %s)\n' "$(hostname)" "$(date '+%Y-%m-%d %H:%M:%S')" "$DEPLOY_MODE"
check_disk
check_backup
check_services
check_agent_health
check_https
check_misc

if (( ${#FAILS[@]} )); then
  printf '\n%sVÝSLEDOK: %d chýb, %d upozornení%s\n' "$C_RED" "${#FAILS[@]}" "${#WARNS[@]}" "$C_OFF"
  hc_ping "$CHECK_HEARTBEAT_URL" /fail "$(hostname): $(printf '%s\n' "${FAILS[@]}" "${WARNS[@]}")"
  exit 1
fi
printf '\nVÝSLEDOK: OK (%d upozornení)\n' "${#WARNS[@]}"
hc_ping "$CHECK_HEARTBEAT_URL" "" "$(hostname): OK, upozornenia: ${#WARNS[@]}"
exit 0
