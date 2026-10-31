"""Statické kontroly nasadenia: docker-compose.yml (bez bežiaceho Dockera)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
APP_SERVICES = ("init-db", "web", "agent")
LONG_RUNNING = ("db", "web", "agent", "caddy")


@pytest.fixture(scope="module")
def compose() -> dict:
    with COMPOSE.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), "docker-compose.yml sa nedá načítať ako YAML mapa"
    return data


@pytest.fixture(scope="module")
def services(compose) -> dict:
    return compose["services"]


def test_compose_parses_and_has_all_services(compose, services):
    assert compose.get("name") == "vlaky"
    for name in ("db", "init-db", "web", "agent", "caddy"):
        assert name in services, name


def test_only_caddy_publishes_ports(services):
    publishing = sorted(name for name, svc in services.items() if svc.get("ports"))
    assert publishing == ["caddy"]
    ports = [str(p) for p in services["caddy"]["ports"]]
    assert any(p.startswith("80:") for p in ports)
    assert any(p.startswith("443:") for p in ports)


def test_db_is_private_with_volume_and_healthcheck(compose, services):
    db = services["db"]
    assert db["image"] == "postgres:16-alpine"
    assert "pg_isready" in " ".join(db["healthcheck"]["test"])
    assert any(str(v).startswith("pgdata:") for v in db["volumes"])
    assert "pgdata" in compose["volumes"]
    for net in db["networks"]:
        # databáza je len v internej sieti – bez cesty na internet a bez portov
        assert compose["networks"][net].get("internal") is True, net


def test_app_services_commands_env_restart_and_order(services):
    assert services["web"]["command"] == ["python", "-m", "vlaky", "web", "--host", "0.0.0.0", "--port", "8000"]
    assert services["agent"]["command"] == ["python", "-m", "vlaky", "agent"]
    assert services["init-db"]["command"] == ["python", "-m", "vlaky", "init-db"]
    assert services["init-db"]["restart"] == "no"
    for name in ("web", "agent"):
        svc = services[name]
        assert svc["restart"] == "unless-stopped"
        assert svc["env_file"] in (".env", [".env"])
        assert svc["depends_on"]["db"]["condition"] == "service_healthy"
        assert svc["depends_on"]["init-db"]["condition"] == "service_completed_successfully"


def test_agent_has_mailbox_and_outbox_volumes(compose, services):
    volumes = [str(v) for v in services["agent"]["volumes"]]
    assert "schranka:/app/data/schranka" in volumes
    assert "odoslane:/app/data/odoslane" in volumes
    assert {"schranka", "odoslane"} <= set(compose["volumes"])


def test_healthchecks(services):
    assert services["agent"]["healthcheck"]["test"] == ["CMD", "python", "-m", "vlaky", "health"]
    assert "/health" in " ".join(services["web"]["healthcheck"]["test"])
    assert services["init-db"]["healthcheck"]["disable"] is True


def test_secrets_are_required_and_db_url_uses_private_db(services):
    env = services["web"]["environment"]
    assert "${POSTGRES_PASSWORD:?" in env["VLAKY_DATABASE_URL"]
    assert env["VLAKY_DATABASE_URL"].startswith("postgresql+psycopg://")
    assert "@db:5432/" in env["VLAKY_DATABASE_URL"]
    assert "${VLAKY_SECRET_KEY:?" in env["VLAKY_SECRET_KEY"]


def test_hardening(services):
    for name in APP_SERVICES:
        svc = services[name]
        assert svc.get("read_only") is True, name
        assert "ALL" in svc.get("cap_drop", []), name
        assert "no-new-privileges:true" in svc.get("security_opt", []), name
    assert services["caddy"]["cap_drop"] == ["ALL"]
    assert services["caddy"]["cap_add"] == ["NET_BIND_SERVICE"]


def test_resource_limits(services):
    for name in LONG_RUNNING:
        limits = services[name]["deploy"]["resources"]["limits"]
        assert limits.get("memory") and limits.get("cpus"), name


def test_log_rotation_for_every_service(services):
    for name, svc in services.items():
        logging = svc.get("logging") or {}
        assert logging.get("driver") == "json-file", name
        assert logging.get("options", {}).get("max-size"), name
        assert logging.get("options", {}).get("max-file"), name


def test_caddy_service(services):
    caddy = services["caddy"]
    assert caddy["image"] == "caddy:2-alpine"
    assert any("deploy/Caddyfile:/etc/caddy/Caddyfile" in str(v) for v in caddy["volumes"])
    assert (ROOT / "deploy" / "Caddyfile").is_file()
    assert "VLAKY_DOMAIN:?" in caddy["environment"]["VLAKY_DOMAIN"]


def test_networks(compose, services):
    networks = compose["networks"]
    assert networks["backend"]["internal"] is True
    assert set(services["caddy"]["networks"]) == {"frontend"}  # proxy nevidí databázu
    assert "frontend" in services["web"]["networks"]
    assert "backend" in services["agent"]["networks"] and "egress" in services["agent"]["networks"]


def test_osm_import_tool_is_opt_in(services):
    osm = services["osm-import"]
    assert osm["profiles"] == ["tools"]
    assert osm["network_mode"] == "none"
    assert osm["build"]["target"] == "osm-tools"


def _compose_config(directory: Path, *extra: str) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(directory)}
    return subprocess.run(["docker", "compose", *extra, "config", "-q"], cwd=directory, env=env,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI nie je k dispozícii")
def test_docker_compose_config_validates(tmp_path):
    """`docker compose config -q` – kontrola podľa špecifikácie Compose (démon Dockeru netreba)."""
    (tmp_path / "deploy").mkdir()
    shutil.copy(COMPOSE, tmp_path / "docker-compose.yml")
    shutil.copy(ROOT / "deploy" / "Caddyfile", tmp_path / "deploy" / "Caddyfile")
    template = (ROOT / ".env.example").read_text(encoding="utf-8")

    # 1. so šablónou (prázdne povinné tajomstvá) musí Compose odmietnuť štart
    (tmp_path / ".env").write_text(template, encoding="utf-8")
    result = _compose_config(tmp_path)
    if result.returncode != 0 and "is not a docker command" in result.stderr:
        pytest.skip("docker compose plugin nie je nainštalovaný")
    assert result.returncode != 0
    assert "POSTGRES_PASSWORD" in result.stderr

    # 2. s vyplnenými tajomstvami je konfigurácia platná (aj s profilom tools)
    filled = template.replace("\nPOSTGRES_PASSWORD=\n", "\nPOSTGRES_PASSWORD=testheslo123\n").replace(
        "\nVLAKY_SECRET_KEY=\n", "\nVLAKY_SECRET_KEY=" + "a" * 64 + "\n")
    (tmp_path / ".env").write_text(filled, encoding="utf-8")
    result = _compose_config(tmp_path, "--profile", "tools")
    assert result.returncode == 0, result.stderr
