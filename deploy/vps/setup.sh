#!/usr/bin/env bash
# =============================================================================
# Príprava VPS (Ubuntu 24.04 LTS) pre Agenta dispečera.
#
# Čo skript urobí (každý krok vypíše; je idempotentný – dá sa bezpečne spustiť opakovane):
#   1. aktualizácia balíkov + automatické bezpečnostné aktualizácie (unattended-upgrades)
#   2. synchronizácia času, limity systémových logov (journald), swap (ak žiadny nie je)
#   3. Docker Engine + compose plugin z oficiálneho repozitára Dockeru
#   4. používateľ "vlaky" (UID 10001 – rovnaké ako v kontajneri) a adresáre /opt/vlaky, /etc/vlaky
#   5. fail2ban (ochrana SSH)
#   6. firewall UFW: dnu len 80/443 (web) a SSH LEN z povolených adries (VPN / pevná IP)
#   7. ak už je repozitár v /opt/vlaky/app: systemd časovače na zálohy a kontrolu servera
#
# Použitie (ako root):
#   sudo bash deploy/vps/setup.sh --ssh-allow 203.0.113.10/32 [--ssh-allow 10.8.0.0/24] [voľby]
#
# Voľby:
#   --ssh-allow CIDR        adresa alebo rozsah, z ktorého je povolené SSH (opakovateľné; POVINNÉ)
#   --ssh-port PORT         port SSH (predvolene 22)
#   --wireguard-port PORT   otvoriť UDP port pre WireGuard VPN (voliteľné, napr. 51820)
#   --swap-size VEĽKOSŤ     veľkosť swapu, ak žiadny nie je (predvolene 4G)
#   --timezone PÁSMO        časové pásmo servera (napr. Europe/Bratislava; predvolene bez zmeny)
#   --auto-reboot HH:MM     po aktualizáciách vyžadujúcich reštart reštartovať server v daný čas
#   --repo URL              naklonovať repozitár do /opt/vlaky/app (ako používateľ vlaky)
#   --branch VETVA          vetva pre --repo
#   --force                 zapnúť firewall, aj keď vaša aktuálna IP nie je v povolenom rozsahu
#   --yes                   bez otázok (automatizácia)
#   -h, --help              táto pomoc
# =============================================================================
set -euo pipefail

readonly VLAKY_USER=vlaky
readonly VLAKY_UID=10001
readonly BASE_DIR=/opt/vlaky
readonly APP_DIR=$BASE_DIR/app
readonly ETC_DIR=/etc/vlaky

SSH_ALLOW=()
SSH_PORT=22
WG_PORT=""
SWAP_SIZE=4G
TIMEZONE=""
AUTO_REBOOT=""
REPO_URL=""
REPO_BRANCH=""
ASSUME_YES=0
FORCE=0
CHANGED=0

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
APT_OPTS=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

if [[ -t 1 ]]; then
  B=$'\e[1m' R=$'\e[31m' Y=$'\e[33m' G=$'\e[32m' N=$'\e[0m'
else
  B='' R='' Y='' G='' N=''
fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %sOK%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s!!! %s%s\n' "$Y" "$*" "$N" >&2; }
die()  { printf '%sCHYBA: %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

usage() {
  sed -n '2,/^# =====/{/^# =====/d;s/^# \{0,1\}//;p}' "$0"
}

confirm() {
  local ans
  if (( ASSUME_YES )); then return 0; fi
  read -r -p "$1 [a/N] " ans || return 1
  case ${ans,,} in a|ano|áno|y|yes) return 0 ;; *) return 1 ;; esac
}

# write_file CESTA REŽIM < obsah – zapíše len pri zmene obsahu; nastaví CHANGED=1/0
write_file() {
  local path=$1 mode=$2 tmp
  tmp=$(mktemp)
  cat > "$tmp"
  if [[ -f $path ]] && cmp -s "$tmp" "$path"; then
    rm -f "$tmp"
    CHANGED=0
    info "bez zmeny: $path"
  else
    install -D -m "$mode" "$tmp" "$path"
    rm -f "$tmp"
    CHANGED=1
    info "zapísané: $path"
  fi
}

pkg_installed() {
  local st
  st=$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null || true)
  [[ $st == "install ok installed" ]]
}

valid_cidr() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import ipaddress, sys; ipaddress.ip_network(sys.argv[1], strict=False)' "$1" 2>/dev/null
  else
    [[ $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$ || $1 =~ ^[0-9a-fA-F:]+(/[0-9]{1,3})?$ ]]
  fi
}

# IP adresa, z ktorej je pripojená aktuálna SSH relácia (prázdne, ak sa nedá zistiť)
current_ssh_ip() {
  local ip=""
  if [[ -n ${SSH_CONNECTION:-} ]]; then
    ip=${SSH_CONNECTION%% *}
  else
    ip=$(who -m --ips 2>/dev/null | awk '{print $NF}' | tr -d '()' || true)
  fi
  if [[ -n $ip ]] && valid_cidr "$ip"; then printf '%s' "$ip"; fi
}

ip_in_allowlist() {
  command -v python3 >/dev/null 2>&1 || return 2
  python3 - "$1" "${SSH_ALLOW[@]}" <<'PY'
import ipaddress, sys
ip = ipaddress.ip_address(sys.argv[1])
sys.exit(0 if any(ip in ipaddress.ip_network(n, strict=False) for n in sys.argv[2:]) else 1)
PY
}

# ----------------------------------------------------------------------------- argumenty
while (( $# )); do
  case $1 in
    --ssh-allow)      SSH_ALLOW+=("${2:?--ssh-allow potrebuje hodnotu}"); shift 2 ;;
    --ssh-port)       SSH_PORT=${2:?}; shift 2 ;;
    --wireguard-port) WG_PORT=${2:?}; shift 2 ;;
    --swap-size)      SWAP_SIZE=${2:?}; shift 2 ;;
    --timezone)       TIMEZONE=${2:?}; shift 2 ;;
    --auto-reboot)    AUTO_REBOOT=${2:?}; shift 2 ;;
    --repo)           REPO_URL=${2:?}; shift 2 ;;
    --branch)         REPO_BRANCH=${2:?}; shift 2 ;;
    --force)          FORCE=1; shift ;;
    --yes|-y)         ASSUME_YES=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "Neznáma voľba: $1 (pomoc: $0 --help)" ;;
  esac
done

# ----------------------------------------------------------------------------- kontroly
[[ $EUID -eq 0 ]] || die "Spusti ako root: sudo bash $0 --ssh-allow VAŠA_IP/32"
(( ${#SSH_ALLOW[@]} > 0 )) || die "Zadaj aspoň jednu voľbu --ssh-allow ADRESA/ROZSAH (IP kancelárie alebo rozsah VPN).
       Bez nej by sa po zapnutí firewallu nikto nedostal na server cez SSH."
for cidr in "${SSH_ALLOW[@]}"; do
  valid_cidr "$cidr" || die "Neplatná adresa/rozsah pre --ssh-allow: $cidr"
done
[[ $SSH_PORT =~ ^[0-9]+$ ]] || die "Neplatný --ssh-port: $SSH_PORT"
if [[ -n $WG_PORT && ! $WG_PORT =~ ^[0-9]+$ ]]; then die "Neplatný --wireguard-port: $WG_PORT"; fi
if [[ -n $AUTO_REBOOT && ! $AUTO_REBOOT =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
  die "Neplatný čas pre --auto-reboot (formát HH:MM): $AUTO_REBOOT"
fi

# shellcheck source=/dev/null
. /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 24.04 ]]; then
  warn "Skript je pripravený pre Ubuntu 24.04 LTS, zistený systém: ${PRETTY_NAME:-neznámy}."
  confirm "Pokračovať aj tak?" || die "Prerušené."
fi

cat <<EOF
${B}Príprava servera pre Agenta dispečera${N}
  Systém:            ${PRETTY_NAME:-?}
  SSH povolené z:    ${SSH_ALLOW[*]} (port $SSH_PORT)
  WireGuard:         ${WG_PORT:-nie}
  Swap (ak chýba):   $SWAP_SIZE
  Časové pásmo:      ${TIMEZONE:-bez zmeny}
  Automatický reštart po aktualizáciách: ${AUTO_REBOOT:-nie (reštart urobí správca)}
  Repozitár:         ${REPO_URL:-neklonovať (urobíš neskôr)}

Kroky: aktualizácie, unattended-upgrades, čas, journald, swap, Docker, používateľ $VLAKY_USER,
       $BASE_DIR, fail2ban, firewall UFW${REPO_URL:+, klonovanie repozitára}, časovače záloh a kontroly.
EOF
confirm "Pokračovať?" || die "Prerušené používateľom."

# ----------------------------------------------------------------------------- 1. balíky
step "1/7 Aktualizácia systému a základné balíky"
apt-get update
apt-get "${APT_OPTS[@]}" upgrade
apt-get "${APT_OPTS[@]}" install ca-certificates curl gnupg git openssl ufw fail2ban \
  python3 python3-systemd unattended-upgrades apt-listchanges restic
ok "balíky sú aktuálne"

step "Automatické bezpečnostné aktualizácie (unattended-upgrades)"
write_file /etc/apt/apt.conf.d/20auto-upgrades 0644 <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
reboot_flag=false
reboot_time=03:40
if [[ -n $AUTO_REBOOT ]]; then reboot_flag=true; reboot_time=$AUTO_REBOOT; fi
write_file /etc/apt/apt.conf.d/52vlaky-unattended-upgrades 0644 <<EOF
// Spravuje deploy/vps/setup.sh (Agent dispečer)
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-New-Unused-Dependencies "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "$reboot_flag";
Unattended-Upgrade::Automatic-Reboot-Time "$reboot_time";
EOF
systemctl enable --now unattended-upgrades >/dev/null
ok "bezpečnostné aktualizácie sa inštalujú automaticky (automatický reštart: $reboot_flag)"

# ----------------------------------------------------------------------------- 2. čas, logy, swap
step "2/7 Synchronizácia času"
if systemctl is-active --quiet chrony 2>/dev/null || systemctl is-active --quiet chronyd 2>/dev/null; then
  ok "beží chrony"
else
  if ! pkg_installed systemd-timesyncd; then apt-get "${APT_OPTS[@]}" install systemd-timesyncd; fi
  timedatectl set-ntp true
  ok "zapnutý systemd-timesyncd"
fi
if [[ -n $TIMEZONE ]]; then
  timedatectl set-timezone "$TIMEZONE"
  ok "časové pásmo: $TIMEZONE"
fi
timedatectl status | sed 's/^/    /'

step "Limity systémových logov (journald)"
write_file /etc/systemd/journald.conf.d/50-vlaky.conf 0644 <<'EOF'
# Spravuje deploy/vps/setup.sh (Agent dispečer): veľkosť a vek systémových logov
[Journal]
Storage=persistent
Compress=yes
SystemMaxUse=1G
SystemKeepFree=2G
MaxRetentionSec=1month
EOF
if (( CHANGED )); then systemctl restart systemd-journald; fi
ok "journald: najviac 1 GB a 1 mesiac"

step "Swap"
if [[ -n $(swapon --show --noheadings 2>/dev/null || true) ]]; then
  ok "swap už existuje:"
  swapon --show | sed 's/^/    /'
else
  if [[ ! -f /swapfile ]]; then
    info "vytváram /swapfile ($SWAP_SIZE)"
    if ! fallocate -l "$SWAP_SIZE" /swapfile 2>/dev/null; then
      size_mb=$(( $(numfmt --from=iec "$SWAP_SIZE") / 1048576 ))
      dd if=/dev/zero of=/swapfile bs=1M count="$size_mb" status=none
    fi
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
  fi
  swapon /swapfile || die "Nepodarilo sa zapnúť /swapfile – skontroluj ho (ls -l /swapfile)."
  if ! grep -qE '^/swapfile[[:space:]]' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
  ok "swap zapnutý"
fi
write_file /etc/sysctl.d/90-vlaky.conf 0644 <<'EOF'
# Spravuje deploy/vps/setup.sh (Agent dispečer): swap používať až pri nedostatku pamäte
vm.swappiness = 10
EOF
if (( CHANGED )); then sysctl --system >/dev/null; fi

# ----------------------------------------------------------------------------- 3. Docker
step "3/7 Docker Engine + Compose plugin (oficiálny repozitár Dockeru)"
for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
  if pkg_installed "$pkg"; then
    info "odstraňujem konfliktný balík $pkg"
    apt-get "${APT_OPTS[@]}" remove "$pkg"
  fi
done
install -m 0755 -d /etc/apt/keyrings
if [[ ! -s /etc/apt/keyrings/docker.asc ]]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi
if [[ -f /etc/apt/sources.list.d/docker.list ]]; then
  info "pôvodný /etc/apt/sources.list.d/docker.list presúvam do /root/docker.list.bak"
  mv /etc/apt/sources.list.d/docker.list /root/docker.list.bak
fi
codename=${UBUNTU_CODENAME:-${VERSION_CODENAME:-noble}}
write_file /etc/apt/sources.list.d/docker.sources 0644 <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $codename
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
if (( CHANGED )) || ! pkg_installed docker-ce; then apt-get update; fi
apt-get "${APT_OPTS[@]}" install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

daemon_json='{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" },
  "live-restore": true
}'
install -d -m 0755 /etc/docker
if [[ ! -f /etc/docker/daemon.json ]]; then
  printf '%s\n' "$daemon_json" > /etc/docker/daemon.json
  info "zapísané: /etc/docker/daemon.json (rotácia logov, live-restore)"
  systemctl restart docker
elif cmp -s <(printf '%s\n' "$daemon_json") /etc/docker/daemon.json; then
  info "bez zmeny: /etc/docker/daemon.json"
else
  warn "/etc/docker/daemon.json už existuje s iným obsahom – ponechávam ho. Odporúčané nastavenie:"
  printf '%s\n' "$daemon_json" | sed 's/^/    /' >&2
fi
systemctl enable --now docker containerd >/dev/null
ok "$(docker --version); $(docker compose version)"

# ----------------------------------------------------------------------------- 4. používateľ a adresáre
step "4/7 Používateľ $VLAKY_USER a adresáre"
if ! getent group "$VLAKY_USER" >/dev/null; then
  if getent group "$VLAKY_UID" >/dev/null; then
    groupadd "$VLAKY_USER"
  else
    groupadd --gid "$VLAKY_UID" "$VLAKY_USER"
  fi
fi
if ! id -u "$VLAKY_USER" >/dev/null 2>&1; then
  uid_opt=()
  if ! getent passwd "$VLAKY_UID" >/dev/null; then uid_opt=(--uid "$VLAKY_UID"); fi
  useradd "${uid_opt[@]}" --gid "$VLAKY_USER" --create-home --home-dir "$BASE_DIR" \
          --shell /bin/bash --comment "Agent dispecer" "$VLAKY_USER"
  info "vytvorený používateľ $VLAKY_USER (UID $(id -u "$VLAKY_USER")), prihlásenie heslom je zablokované"
fi
if [[ $(id -u "$VLAKY_USER") != "$VLAKY_UID" ]]; then
  warn "Používateľ $VLAKY_USER má UID $(id -u "$VLAKY_USER"), kontajnery používajú $VLAKY_UID – pri importe siete použi --user \"\$(id -u):\$(id -g)\"."
fi
usermod -aG docker "$VLAKY_USER"
install -d -m 0750 -o "$VLAKY_USER" -g "$VLAKY_USER" "$BASE_DIR" "$BASE_DIR/import"
install -d -m 0700 -o "$VLAKY_USER" -g "$VLAKY_USER" "$BASE_DIR/backups"
install -d -m 0750 -o root -g "$VLAKY_USER" "$ETC_DIR"
ok "$BASE_DIR/{app,backups,import} a $ETC_DIR pripravené"
warn "Členovia skupiny docker majú v praxi práva root-a – pridávaj do nej len správcov."

# ----------------------------------------------------------------------------- 5. fail2ban
step "5/7 fail2ban (ochrana SSH)"
write_file /etc/fail2ban/jail.d/vlaky.local 0644 <<EOF
# Spravuje deploy/vps/setup.sh (Agent dispečer)
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5
bantime.increment = true
ignoreip = 127.0.0.1/8 ::1 ${SSH_ALLOW[*]}

[sshd]
enabled = true
port    = $SSH_PORT
backend = systemd
EOF
systemctl enable fail2ban >/dev/null
if (( CHANGED )); then systemctl restart fail2ban; else systemctl start fail2ban; fi
ok "fail2ban chráni SSH (5 pokusov / 10 min → zákaz 1 h, opakovane dlhšie)"

# ----------------------------------------------------------------------------- 6. firewall
step "6/7 Firewall (UFW)"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 80/tcp comment 'HTTP - presmerovanie na HTTPS a Lets Encrypt' >/dev/null
ufw allow 443/tcp comment 'HTTPS (Caddy)' >/dev/null
ufw allow 443/udp comment 'HTTP/3 (Caddy)' >/dev/null
for cidr in "${SSH_ALLOW[@]}"; do
  ufw allow proto tcp from "$cidr" to any port "$SSH_PORT" comment 'SSH - povolene adresy (vlaky)' >/dev/null
done
if [[ -n $WG_PORT ]]; then
  ufw allow "$WG_PORT/udp" comment 'WireGuard VPN' >/dev/null
fi
# všeobecné pravidlá "SSH odkiaľkoľvek" (napr. z cloud-init) zrušiť – SSH len z povolených adries
for rule in OpenSSH 22/tcp 22 "$SSH_PORT/tcp" "$SSH_PORT"; do
  ufw --force delete allow "$rule" >/dev/null 2>&1 || true
done

cat >&2 <<EOF

${R}${B}!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!!  POZOR: zapína sa firewall. SSH (port $SSH_PORT) bude povolené LEN z:
!!!      ${SSH_ALLOW[*]}
!!!  Ak sa pripájaš odinakiaľ, po odpojení sa na server cez SSH NEDOSTANEŠ.
!!!  Záchrana: webová konzola poskytovateľa VPS a príkaz  ufw disable
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!${N}
EOF
cur_ip=$(current_ssh_ip)
if [[ -z $cur_ip ]]; then
  warn "Nepodarilo sa zistiť IP adresu, z ktorej si pripojený (napr. konzola poskytovateľa)."
  confirm "Zapnúť firewall aj tak?" || die "Firewall NEBOL zapnutý. Spusti skript znova so správnym --ssh-allow."
else
  in_list=0
  if ip_in_allowlist "$cur_ip"; then in_list=1; fi
  if (( in_list )); then
    ok "tvoja aktuálna adresa $cur_ip je v povolenom rozsahu"
  elif (( FORCE )); then
    warn "Tvoja aktuálna adresa $cur_ip NIE JE v povolenom rozsahu – pokračujem kvôli --force."
  else
    die "Tvoja aktuálna adresa $cur_ip NIE JE v povolenom rozsahu. Firewall NEBOL zapnutý.
       Pridaj --ssh-allow $cur_ip/32 (alebo rozsah VPN), prípadne použi --force."
  fi
  confirm "Zapnúť firewall teraz?" || die "Firewall NEBOL zapnutý."
fi
ufw logging low >/dev/null
ufw --force enable >/dev/null
ufw status verbose | sed 's/^/    /'
info "Pozn.: porty publikované Dockerom obchádzajú UFW. Preto v docker-compose.yml publikuje"
info "porty LEN caddy (80/443); databázu ani aplikáciu nikdy nepublikuj na hostiteľovi."

# ----------------------------------------------------------------------------- 7. aplikácia
step "7/7 Repozitár a časovače"
if [[ -n $REPO_URL ]]; then
  if [[ -d $APP_DIR/.git ]]; then
    ok "repozitár už existuje v $APP_DIR"
  else
    branch_opt=()
    if [[ -n $REPO_BRANCH ]]; then branch_opt=(--branch "$REPO_BRANCH"); fi
    sudo -u "$VLAKY_USER" -H git clone "${branch_opt[@]}" "$REPO_URL" "$APP_DIR"
    ok "naklonované do $APP_DIR"
  fi
fi
if [[ -d $APP_DIR ]] && [[ $(stat -c %U "$APP_DIR") != "$VLAKY_USER" ]]; then
  warn "$APP_DIR nepatrí používateľovi $VLAKY_USER – aktualizácie (git pull) by zlyhali. Oprava:"
  warn "  sudo chown -R $VLAKY_USER:$VLAKY_USER $APP_DIR"
fi

if [[ -d $APP_DIR/deploy/systemd ]]; then
  units_changed=0
  for unit in vlaky-backup.service vlaky-backup.timer vlaky-check.service vlaky-check.timer; do
    write_file "/etc/systemd/system/$unit" 0644 < "$APP_DIR/deploy/systemd/$unit"
    if (( CHANGED )); then units_changed=1; fi
  done
  if (( units_changed )); then systemctl daemon-reload; fi
  if [[ ! -f $ETC_DIR/ops.env ]]; then
    install -m 0640 -o root -g "$VLAKY_USER" "$APP_DIR/deploy/ops.env.example" "$ETC_DIR/ops.env"
    info "vytvorený $ETC_DIR/ops.env (nastavenia záloh a monitoringu – doplň ho)"
  fi
  if [[ -f $APP_DIR/.env ]]; then
    systemctl enable --now vlaky-backup.timer vlaky-check.timer >/dev/null
    ok "časovače zapnuté: denná záloha (vlaky-backup.timer), kontrola každých 15 min (vlaky-check.timer)"
  else
    info "časovače sú nainštalované, zapnú sa po vytvorení $APP_DIR/.env:"
    info "  sudo systemctl enable --now vlaky-backup.timer vlaky-check.timer"
  fi
else
  info "repozitár ešte nie je v $APP_DIR – po naklonovaní spusti tento skript znova"
  info "(nainštaluje časovače záloh a kontroly servera)."
fi

# ----------------------------------------------------------------------------- zhrnutie
step "Hotovo"
cat <<EOF
    Ďalšie kroky (docs/PREVADZKA.md, kapitola 4):
      1. sudo bash $APP_DIR/deploy/vps/harden-ssh.sh       (len kľúče, bez root-a)
      2. sudo -iu $VLAKY_USER  →  git clone … $APP_DIR      (ak ešte nie je)
      3. cd $APP_DIR && cp .env.example .env && chmod 600 .env && nano .env
      4. docker compose build && docker compose up -d
EOF
if [[ -f /var/run/reboot-required ]]; then
  warn "Aktualizácie vyžadujú reštart servera: sudo reboot"
fi
