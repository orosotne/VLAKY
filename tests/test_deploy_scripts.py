"""Statické kontroly nasadenia: shell skripty a jednotky systemd v deploy/."""
from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SCRIPTS = sorted(DEPLOY.rglob("*.sh"))
LIBRARIES = {DEPLOY / "lib" / "common.sh"}  # len sa načítavajú (source), nespúšťajú sa
EXECUTABLES = [p for p in SCRIPTS if p not in LIBRARIES]
IDS = [p.relative_to(ROOT).as_posix() for p in SCRIPTS]
EXEC_IDS = [p.relative_to(ROOT).as_posix() for p in EXECUTABLES]


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_expected_scripts_exist():
    names = set(IDS)
    for rel in ("deploy/vps/setup.sh", "deploy/vps/harden-ssh.sh", "deploy/backup/backup.sh",
                "deploy/backup/restore.sh", "deploy/monitoring/check.sh", "deploy/scripts/update.sh",
                "deploy/lib/common.sh"):
        assert rel in names, rel


@pytest.mark.parametrize("path", SCRIPTS, ids=IDS)
def test_scripts_use_strict_mode(path):
    assert re.search(r"^set -euo pipefail$", path.read_text(encoding="utf-8"), re.M)


@pytest.mark.parametrize("path", EXECUTABLES, ids=EXEC_IDS)
def test_scripts_have_shebang_and_are_executable(path):
    assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")
    assert os.access(path, os.X_OK), f"chmod +x {path}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash nie je k dispozícii")
@pytest.mark.parametrize("path", SCRIPTS, ids=IDS)
def test_bash_syntax(path):
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck nie je nainštalovaný")
def test_shellcheck():
    result = subprocess.run(["shellcheck", "-x", "-S", "warning", *map(str, SCRIPTS)], cwd=ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout


def test_ops_env_example_is_bash_sourceable():
    result = subprocess.run(["bash", "-c", 'set -euo pipefail; set -a; . "$1"; echo "$DEPLOY_MODE"', "_",
                             str(DEPLOY / "ops.env.example")], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "docker"


def test_setup_script_contract():
    text = _text("deploy/vps/setup.sh")
    assert "EUID" in text and "--yes" in text and "confirm" in text
    assert "--ssh-allow" in text and "ufw default deny incoming" in text
    assert "ufw allow 80/tcp" in text and "ufw allow 443/tcp" in text
    assert "download.docker.com" in text and "docker-compose-plugin" in text
    for word in ("unattended-upgrades", "fail2ban", "journald", "/swapfile", "useradd", "/opt/vlaky"):
        assert word in text, word
    assert re.search(r"timesyncd|chrony", text)


def test_harden_ssh_checks_keys_before_changing_config():
    text = _text("deploy/vps/harden-ssh.sh")
    assert "authorized_keys" in text and "ssh-keygen -l" in text and "sshd -t" in text
    assert "PasswordAuthentication no" in text and "PermitRootLogin no" in text
    # konfigurácia sa zapisuje až po úspešnej kontrole kľúčov
    assert text.index("(( found_keys > 0 ))") < text.index('cat > "$CONF"')


def test_backup_and_restore_contract():
    backup = _text("deploy/backup/backup.sh")
    for word in ("pg_dump", "gzip", "BACKUP_RETENTION_DAYS", "restic", "rclone", "schranka", "odoslane",
                 "flock", "dc exec -T db"):
        assert word in backup, word
    restore = _text("deploy/backup/restore.sh")
    for word in ("--test", "DROP DATABASE", "CREATE DATABASE", "--single-transaction",
                 "Na potvrdenie napíš názov databázy", "pred-obnovou"):
        assert word in restore, word


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
    parser.optionxform = str  # rozlišovať veľké/malé písmená v kľúčoch
    with (DEPLOY / "systemd" / name).open(encoding="utf-8") as f:
        parser.read_file(f)
    return parser


@pytest.mark.parametrize("name", ["vlaky-web.service", "vlaky-agent.service"])
def test_app_units_restart_user_env_and_hardening(name):
    service = _unit(name)["Service"]
    assert service["Restart"] == "always"
    assert service["User"] == "vlaky"
    assert service["EnvironmentFile"] == "/etc/vlaky/vlaky.env"
    assert service["NoNewPrivileges"] == "yes"
    assert service["ProtectSystem"] == "strict"
    assert "/var/lib/vlaky" in service["ReadWritePaths"]
    assert service["PrivateTmp"] == "yes"
    assert "python -m vlaky" in service["ExecStart"]


def test_web_unit_listens_only_locally():
    assert "--host 127.0.0.1" in _unit("vlaky-web.service")["Service"]["ExecStart"]


@pytest.mark.parametrize("timer", ["vlaky-backup.timer", "vlaky-check.timer"])
def test_timers_have_schedule_and_service(timer):
    unit = _unit(timer)
    assert unit["Timer"].get("OnCalendar")
    assert unit["Install"]["WantedBy"] == "timers.target"
    service = DEPLOY / "systemd" / timer.replace(".timer", ".service")
    assert service.is_file()
    assert _unit(service.name)["Service"]["Type"] == "oneshot"


def test_backup_timer_is_daily_and_persistent():
    timer = _unit("vlaky-backup.timer")["Timer"]
    assert re.fullmatch(r"\*-\*-\* \d{2}:\d{2}:\d{2}", timer["OnCalendar"])
    assert timer["Persistent"] == "true"
    assert _unit("vlaky-backup.service")["Service"]["ExecStart"].endswith("/deploy/backup/backup.sh")
