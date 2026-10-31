#!/usr/bin/env bash
# =============================================================================
# Kontrola servera Agenta dispečera (spúšťa vlaky-check.timer každých 15 min; dá sa aj ručne)
#   - voľné miesto na disku a inódy (/, Docker, zálohy)
#   - vek poslednej zálohy databázy
#   - bežia a sú zdravé kontajnery db, web, agent, caddy (alebo služby systemd)
#   - HTTPS https://DOMÉNA/health cez lokálny Caddy a platnosť certifikátu
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
else
  CHECK_SERVICES=${CHECK_SERVICES:-vlaky-web vlaky-agent postgresql caddy}
fi

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
      fail "disk $p je zaplnený na $pct % (limit $CHECK_DISK_MAX_PCT %) – pozri docs/PREVADZKA.md, kapitola 12"
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

check_https() {
  local domain url code end end_s days curl_args=(-s -o /dev/null -w '%{http_code}' -m 15)
  domain=${VLAKY_DOMAIN:-$(read_env_var VLAKY_DOMAIN "$APP_DIR/.env")}
  if [[ -z $domain && -z $CHECK_URL ]]; then
    notice "neznáma doména (VLAKY_DOMAIN v .env alebo CHECK_URL) – kontrola HTTPS sa preskakuje"
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
