#!/usr/bin/env bash
# =============================================================================
# Sprísnenie SSH na VPS Agenta dispečera:
#   - zakáže prihlásenie heslom (len SSH kľúče),
#   - zakáže priame prihlásenie používateľa root,
#   - obmedzí počet pokusov a nečinné spojenia.
#
# BEZPEČNOSTNÁ POISTKA: zmenu urobí LEN vtedy, ak administrátor (bežný používateľ s právom
# sudo) má v ~/.ssh/authorized_keys aspoň jeden platný verejný kľúč so správnymi právami.
# Inak skript odmietne pokračovať, aby ste sa na server nezamkli.
#
# Použitie:  sudo bash deploy/vps/harden-ssh.sh [--user MENO] [--yes]
#   --user MENO   administrátor, ktorého kľúč sa overí (predvolene ten, kto spustil sudo)
#   --yes         bez otázky
#
# Návrat zmeny:  sudo rm /etc/ssh/sshd_config.d/00-vlaky-hardening.conf && sudo systemctl reload ssh
# =============================================================================
set -euo pipefail

readonly CONF=/etc/ssh/sshd_config.d/00-vlaky-hardening.conf
ADMIN_USER=${SUDO_USER:-}
ASSUME_YES=0

if [[ -t 1 ]]; then
  B=$'\e[1m' R=$'\e[31m' Y=$'\e[33m' G=$'\e[32m' N=$'\e[0m'
else
  B='' R='' Y='' G='' N=''
fi
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %sOK%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s!!! %s%s\n' "$Y" "$*" "$N" >&2; }
die()  { printf '%sODMIETNUTÉ: %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

while (( $# )); do
  case $1 in
    --user)    ADMIN_USER=${2:?--user potrebuje meno}; shift 2 ;;
    --yes|-y)  ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,/^# =====/{/^# =====/d;s/^# \{0,1\}//;p}' "$0"; exit 0 ;;
    *)         die "Neznáma voľba: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "Spusti ako root: sudo bash $0"
command -v sshd >/dev/null || die "sshd nie je nainštalovaný."
[[ -d /etc/ssh/sshd_config.d ]] || die "Chýba /etc/ssh/sshd_config.d (očakáva sa Ubuntu 22.04+)."
if ! grep -qiE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/' /etc/ssh/sshd_config; then
  die "/etc/ssh/sshd_config nenačítava /etc/ssh/sshd_config.d/*.conf – uprav ho ručne."
fi

# --- 1. administrátor -------------------------------------------------------------------------
[[ -n $ADMIN_USER && $ADMIN_USER != root ]] || die "Spusti skript cez sudo z účtu administrátora (nie ako root),
       alebo zadaj --user MENO. Po zákaze prihlásenia root-a sa bude dať prihlásiť len týmto účtom."
id -u "$ADMIN_USER" >/dev/null 2>&1 || die "Používateľ $ADMIN_USER neexistuje."
groups_of=" $(id -nG "$ADMIN_USER") "
if [[ $groups_of != *" sudo "* && $groups_of != *" admin "* ]]; then
  die "Používateľ $ADMIN_USER nemá právo sudo (skupina sudo). Po zmene by nikto nemohol spravovať server.
       Oprava: usermod -aG sudo $ADMIN_USER"
fi
home=$(getent passwd "$ADMIN_USER" | cut -d: -f6)
[[ -d $home ]] || die "Domovský adresár $home neexistuje."
ok "administrátor: $ADMIN_USER (sudo), domov $home"

# --- 2. authorized_keys ------------------------------------------------------------------------
perm_ok() {  # súbor/adresár nesmie byť zapisovateľný pre skupinu/ostatných a musí patriť používateľovi alebo root-ovi
  local path=$1 owner mode
  owner=$(stat -c %U "$path")
  mode=$(stat -c %a "$path")
  [[ $owner == "$ADMIN_USER" || $owner == root ]] || return 1
  (( (8#$mode & 8#022) == 0 ))
}

found_keys=0
for f in "$home/.ssh/authorized_keys" "$home/.ssh/authorized_keys2"; do
  [[ -f $f ]] || continue
  if ! perm_ok "$f" || ! perm_ok "$home/.ssh" || ! perm_ok "$home"; then
    die "Nesprávne práva na $f alebo jeho adresároch – sshd by kľúč nepoužil (StrictModes).
       Oprava: chmod go-w $home && chmod 700 $home/.ssh && chmod 600 $f && chown -R $ADMIN_USER: $home/.ssh"
  fi
  if fp=$(ssh-keygen -l -f "$f" 2>/dev/null) && [[ -n $fp ]]; then
    n=$(printf '%s\n' "$fp" | wc -l)
    found_keys=$(( found_keys + n ))
    ok "$f – platné kľúče: $n"
    printf '%s\n' "$fp" | sed 's/^/        /'
  else
    warn "$f neobsahuje žiadny platný verejný kľúč"
  fi
done
(( found_keys > 0 )) || die "Používateľ $ADMIN_USER nemá v $home/.ssh/authorized_keys žiadny platný SSH kľúč.
       Najprv z VÁŠHO počítača:  ssh-copy-id $ADMIN_USER@SERVER   a overte prihlásenie kľúčom."

# Doplnkový dôkaz: úspešné prihlásenie kľúčom v posledných dňoch (len upozornenie)
ssh_log=$(journalctl -q --no-pager --since "-14d" -u ssh.service -u sshd.service 2>/dev/null || true)
if grep -qF "Accepted publickey for $ADMIN_USER " <<<"$ssh_log"; then
  ok "v logu je úspešné prihlásenie kľúčom pre $ADMIN_USER"
else
  warn "V logu za 14 dní nie je prihlásenie KĽÚČOM pre $ADMIN_USER. Over to v novom okne:"
  warn "  ssh -o PasswordAuthentication=no -o PreferredAuthentications=publickey $ADMIN_USER@SERVER"
fi

# --- 3. potvrdenie -----------------------------------------------------------------------------
cat <<EOF

${B}Nastaví sa ($CONF):${N}
    PermitRootLogin no, PasswordAuthentication no, KbdInteractiveAuthentication no,
    PubkeyAuthentication yes, MaxAuthTries 3, LoginGraceTime 30, X11Forwarding no
${Y}Toto okno NEZATVÁRAJ, kým v NOVOM okne neoveríš prihlásenie:  ssh $ADMIN_USER@SERVER${N}
EOF
if (( ! ASSUME_YES )); then
  read -r -p "Pokračovať? [a/N] " ans
  case ${ans,,} in a|ano|áno|y|yes) ;; *) die "Prerušené používateľom." ;; esac
fi

# --- 4. konfigurácia ---------------------------------------------------------------------------
backup=""
if [[ -f $CONF ]]; then
  backup=$(mktemp)
  cp -p "$CONF" "$backup"
fi
cat > "$CONF" <<'EOF'
# Spravuje deploy/vps/harden-ssh.sh (Agent dispečer). Súbor je prvý v poradí, preto má prednosť
# pred neskoršími súbormi (napr. 50-cloud-init.conf) – v sshd platí prvá nájdená hodnota.
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
AuthenticationMethods publickey
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
ClientAliveInterval 300
ClientAliveCountMax 2
EOF
chmod 0644 "$CONF"

restore_conf() {
  if [[ -n $backup ]]; then mv -f "$backup" "$CONF"; else rm -f "$CONF"; fi
}

if ! sshd -t; then
  restore_conf
  die "Kontrola konfigurácie sshd zlyhala – zmena bola vrátená, SSH sa nezmenilo."
fi

effective=$(sshd -T -C "user=$ADMIN_USER,host=localhost,addr=127.0.0.1" 2>/dev/null || sshd -T)
for expect in "permitrootlogin no" "passwordauthentication no" "kbdinteractiveauthentication no"; do
  if ! grep -qx "$expect" <<<"$effective"; then
    restore_conf
    die "Výsledné nastavenie nemá '$expect' (iný súbor ho prepisuje – skontroluj: grep -rni '${expect%% *}' /etc/ssh/).
       Zmena bola vrátená, SSH sa nezmenilo."
  fi
done
if [[ -n $backup ]]; then rm -f "$backup"; fi

systemctl try-reload-or-restart ssh.service
ok "SSH sprísnené: prihlásenie len kľúčom, root sa nemôže prihlásiť priamo."
cat <<EOF

${B}Teraz v NOVOM okne over:${N}  ssh $ADMIN_USER@SERVER   (a potom sudo -v)
Ak sa nedá prihlásiť, v TOMTO okne vráť zmenu:
    sudo rm $CONF && sudo systemctl reload ssh
EOF
