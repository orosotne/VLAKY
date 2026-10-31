"""Statické kontroly nasadenia: .env.example, Dockerfile, .dockerignore a Caddyfile."""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
DOCKERFILE = ROOT / "Dockerfile"
CADDYFILE = ROOT / "deploy" / "Caddyfile"
# KĽÚČ=hodnota, prípadne zakomentované "# KĽÚČ=hodnota" (dokumentované predvolené nastavenie)
ENV_LINE = re.compile(r"^\s*(#\s*)?([A-Z][A-Z0-9_]*)=(.*)$")
# VLAKY_ premenné, ktoré nečíta aplikácia, ale docker-compose.yml / Caddy
NON_APP_VLAKY = {"VLAKY_DOMAIN"}


def _env_lines() -> list[tuple[bool, str, str]]:
    out = []
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = ENV_LINE.match(raw)
        if m:
            out.append((bool(m.group(1)), m.group(2), m.group(3)))
    return out


def _settings_env_names() -> set[str]:
    from vlaky.config import Settings

    return {f"VLAKY_{name.upper()}" for name in Settings.model_fields}


# ------------------------------------------------------------------------------------- .env.example
def test_every_setting_from_config_is_in_env_example():
    documented = {key for _, key, _ in _env_lines()}
    missing = sorted(_settings_env_names() - documented)
    assert not missing, f"Doplň do .env.example (s komentárom): {missing}"


def test_env_example_has_no_unknown_vlaky_variables():
    names = {key for _, key, _ in _env_lines() if key.startswith("VLAKY_")}
    unknown = sorted(names - _settings_env_names() - NON_APP_VLAKY)
    assert not unknown, f"Neznáme nastavenia (preklep?): {unknown}"


def test_env_example_contains_compose_variables():
    active = {key for commented, key, _ in _env_lines() if not commented}
    assert {"VLAKY_DOMAIN", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"} <= active


def test_env_example_loads_into_settings(monkeypatch):
    """Aktívne hodnoty šablóny musí aplikácia načítať (napr. žiadne prázdne číselné nastavenie)."""
    from vlaky.config import Settings

    for key in list(os.environ):
        if key.startswith("VLAKY_"):
            monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=str(ENV_EXAMPLE))
    # bezpečné predvolené hodnoty pre nový server
    assert s.env == "prod"
    assert s.shadow_mode is True
    assert s.require_dmarc_pass is True
    assert s.llm_provider == "none"
    assert s.carrier_name == "[NÁZOV DOPRAVCU]"
    assert s.imap_port == 993 and s.smtp_port == 587


def test_env_example_contains_no_secrets():
    for commented, key, value in _env_lines():
        if commented:
            continue
        if any(marker in key for marker in ("PASSWORD", "SECRET", "TOKEN", "API_KEY")):
            assert value == "", f"{key}: v šablóne nesmie byť tajomstvo"
        if key.endswith("_URL") and key != "VLAKY_PUBLIC_URL":
            assert value == "", f"{key}: URL s tokenom (webhook, ping) patrí len do .env"


def test_env_example_uses_only_reserved_example_addresses():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for domain in re.findall(r"[\w.+-]+@((?:[\w-]+\.)+[A-Za-z]{2,})\b", text):
        assert domain.endswith(".example"), f"Skutočná e-mailová adresa v šablóne: …@{domain}"


def test_env_example_has_no_inline_comments():
    """systemd (EnvironmentFile) komentáre za hodnotou nepodporuje – komentár na samostatný riadok."""
    for commented, key, value in _env_lines():
        if not commented:
            assert " #" not in value, key


# ------------------------------------------------------------------------------------- Dockerfile
def _stages() -> list[dict]:
    text = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    stages: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        instr, _, rest = line.partition(" ")
        instr = instr.upper()
        if instr == "FROM":
            parts = rest.split()
            name = parts[2] if len(parts) >= 3 and parts[1].upper() == "AS" else None
            stages.append({"name": name, "base": parts[0], "instr": []})
        else:
            stages[-1]["instr"].append((instr, rest.strip()))
    return stages


def _effective(stages: list[dict], index: int, instruction: str):
    """Posledná hodnota inštrukcie v etape, inak zdedená z rodičovskej etapy."""
    values = [rest for instr, rest in stages[index]["instr"] if instr == instruction]
    if values:
        return values[-1]
    by_name = {s["name"]: i for i, s in enumerate(stages) if s["name"]}
    parent = stages[index]["base"]
    return _effective(stages, by_name[parent], instruction) if parent in by_name else None


def test_dockerfile_uses_python_312_slim_without_digest():
    stages = _stages()
    names = {s["name"] for s in stages}
    external = [s["base"] for s in stages if s["base"] not in names]
    assert external and set(external) == {"python:3.12-slim"}
    assert "@sha256:" not in DOCKERFILE.read_text(encoding="utf-8")


def test_dockerfile_final_image_is_non_root_with_healthcheck():
    stages = _stages()
    last = len(stages) - 1
    user = _effective(stages, last, "USER")
    assert user and user.split(":")[0] not in ("root", "0")
    healthcheck = _effective(stages, last, "HEALTHCHECK")
    assert healthcheck and not healthcheck.upper().startswith("NONE")
    assert '"vlaky"' in healthcheck and '"health"' in healthcheck
    cmd = _effective(stages, last, "CMD")
    assert cmd and '"web"' in cmd


def _lineage(stages: list[dict], index: int) -> list[dict]:
    """Etapy, z ktorých sa skladá daná etapa (od základnej po ňu)."""
    by_name = {s["name"]: i for i, s in enumerate(stages) if s["name"]}
    chain = [stages[index]]
    while chain[0]["base"] in by_name:
        chain.insert(0, stages[by_name[chain[0]["base"]]])
    return chain


def test_dockerfile_install_copy_env():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "pip install --no-cache-dir -r requirements.txt" in text
    for src in ("vlaky/", "data/", "referencia/"):
        assert re.search(rf"^COPY\s+{re.escape(src)}\s", text, re.M), src
    assert re.search(r"^ENV\s+PYTHONUNBUFFERED=1", text, re.M)


def test_production_image_has_no_build_tools():
    stages = _stages()
    production = " ".join(rest for stage in _lineage(stages, len(stages) - 1) for _, rest in stage["instr"])
    assert not re.search(r"apt-get\s+install|build-essential|\bgcc\b|\bmake\b", production), \
        "v produkčnom obraze nemajú byť balíky systému ani kompilátory"


def test_dockerignore_excludes_secrets_and_keeps_runtime_data():
    lines = {line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")}
    for pattern in (".env", ".git", ".venv"):
        assert pattern in lines, pattern
    for needed in ("data", "data/", "vlaky", "vlaky/", "referencia", "requirements.txt"):
        assert needed not in lines, needed


# ------------------------------------------------------------------------------------- Caddyfile
def test_caddyfile_reverse_proxy_tls_headers_compression_log():
    text = CADDYFILE.read_text(encoding="utf-8")
    assert re.search(r"^\{\$VLAKY_DOMAIN(:[^}]*)?\}\s*\{", text, re.M), "blok stránky pre {$VLAKY_DOMAIN}"
    assert re.search(r"reverse_proxy\s+\{\$VLAKY_UPSTREAM:web:8000\}", text)
    assert "zstd" in text and "gzip" in text
    for header in ("Strict-Transport-Security", "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"):
        assert header in text, header
    assert re.search(r"^\s*log\s*\{", text, re.M)
    assert text.count("{") == text.count("}"), "nevyvážené zátvorky v Caddyfile"
