"""Konfigurácia Agenta dispečera (premenné prostredia s predponou VLAKY_ alebo súbor .env)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VLAKY_", env_file=".env", extra="ignore")

    # --- všeobecné ---
    env: str = "dev"                                   # dev | test | prod
    timezone: str = "Europe/Bratislava"
    data_dir: Path = ROOT / "data"
    database_url: str = f"sqlite:///{ROOT / 'data' / 'vlaky.db'}"
    secret_key: str = "zmen-ma-v-produkcii"            # podpisovanie session cookies
    public_url: str = "http://localhost:8000"          # odkazy v správach pre dispečera
    carrier_name: str = "[NÁZOV DOPRAVCU]"
    forwarded_allow_ips: str = "127.0.0.1"             # IP reverse proxy, ktorej sa verí X-Forwarded-For
    # mapový podklad (v prevádzke vlastné dlaždice alebo komerčný poskytovateľ – verejné OSM dlaždice
    # nie sú určené na nepretržitú prevádzku); {z}/{x}/{y} šablóna rastrových dlaždíc
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    map_tile_attribution: str = "© prispievatelia OpenStreetMap"

    # --- schránka agenta ---
    mail_source: str = "directory"                     # directory | imap | graph
    mail_dir: Path = ROOT / "data" / "schranka"        # pre directory: sem padajú .eml súbory
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_mailbox: str = ""                            # napr. agent.dispecing@firma.sk
    mail_poll_seconds: int = 60
    # povolení odosielatelia (domény alebo adresy), čiarkou oddelené; ostatné ide do karantény
    allowed_senders: str = "zsr.sk"
    require_dmarc_pass: bool = True                    # vyžadovať SPF/DKIM/DMARC pass v Authentication-Results
    trusted_authserv_ids: str = ""                     # authserv-id vášho poštového servera (čiarkou); prázdne = akceptovať prvú hlavičku
    max_attachment_mb: int = 15
    attachment_sandbox: bool = True                    # prílohy v izolovanom podprocese (limity pamäte/CPU)

    # --- extrakcia (LLM) ---
    llm_provider: str = "none"                         # none | anthropic | openai
    llm_model: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    llm_timeout_s: int = 60
    confirm_threshold: float = 0.9                     # pod touto istotou ide udalosť na potvrdenie
    confirm_horizon_h: int = 72                        # udalosti ovplyvňujúce vlak do 72 h idú na potvrdenie

    # --- notifikácie (LEN interní príjemcovia) ---
    # Pevný zoznam povolených príjemcov. Odosielacia služba iné adresy/čísla odmietne.
    notify_emails: str = ""                            # dispečeri, čiarkou oddelené
    notify_phones: str = ""                            # SMS, čiarkou oddelené (+421...)
    shadow_mode: bool = True                           # tieňová prevádzka: odporúčania len na shadow_emails
    shadow_emails: str = ""
    notify_transport: str = "smtp"                     # smtp | graph (Microsoft Graph sendMail z účtu agenta)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "agent.dispecing@localhost"
    smtp_starttls: bool = True
    teams_webhook_url: str = ""
    sms_gateway_url: str = ""                          # HTTP brána (POST JSON {to, text})
    sms_gateway_token: str = ""
    outbox_dir: Path = ROOT / "data" / "odoslane"      # kópia každej odoslanej správy (audit, test)
    digest_minutes: int = 120
    dedup_minutes: int = 180

    # --- agent ---
    reevaluate_minutes: int = 15
    heartbeat_url: str = ""                            # externý monitoring (napr. healthchecks.io ping URL)
    heartbeat_minutes: int = 5
    daily_report_hour: int = 6                         # miestny čas
    shift_change_hours: str = "6,18"                   # odovzdávka služby (miestny čas)
    position_stale_minutes: int = 15

    # --- pozície ---
    positions_token: str = ""                          # token pre POST /api/pozicie (GPS brána, aplikácia)

    # --- plánovanie (predvolené zdržania, kým nie je história) ---
    default_delay_p50_min_per_100km: float = 5.0
    default_delay_p90_min_per_100km: float = 25.0
    freight_vmax_kmh: float = 100.0
    horizon_hours: int = 24
    step_minutes: int = 10

    def csv(self, value: str) -> list[str]:
        return [x.strip() for x in value.split(",") if x.strip()]

    @property
    def graph_path(self) -> Path:
        return self.data_dir / "siet" / "graf.json.gz"

    @property
    def stations_path(self) -> Path:
        return self.data_dir / "siet" / "stanice.json"

    @property
    def lines_path(self) -> Path:
        return self.data_dir / "siet" / "trate.geojson.gz"

    @property
    def borders_path(self) -> Path:
        return self.data_dir / "hranice.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


# rezervované pre testy: umožní prepísať nastavenia
def override_settings(**kwargs) -> Settings:
    reset_settings_cache()
    s = Settings(**kwargs)
    get_settings.cache_clear()
    _OVERRIDE["s"] = s
    return s


_OVERRIDE: dict = {}


def settings() -> Settings:
    return _OVERRIDE.get("s") or get_settings()


__all__ = ["Settings", "settings", "override_settings", "ROOT", "Field"]
