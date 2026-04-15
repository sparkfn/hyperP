"""Application configuration via Pydantic Settings."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration for the ingestion service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_empty_strings(cls, values: dict[str, object]) -> dict[str, object]:
        """Drop empty-string env vars so field defaults apply."""
        return {k: v for k, v in values.items() if v != ""}

    # Neo4j connection --------------------------------------------------------
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str  # required, no default

    # Logging -----------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Ingestion tuning --------------------------------------------------------
    batch_size: int = 500

    # Celery / queue ----------------------------------------------------------
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"
    celery_worker_concurrency: int = 1
    # How many ingestion tasks may run concurrently across the entire cluster.
    # Enforced via a Redis-backed semaphore inside the task itself, independent
    # of `celery_worker_concurrency` (which controls per-worker process count).
    max_concurrent_ingestions: int = 1
    # Beat schedule for periodic Fundbox ingestion. Empty string disables.
    fundbox_consumer_backend_ingest_cron: str = ""  # e.g. "0 */6 * * *"

    # Fundbox Consumer Backend source DB (MySQL, optionally via SSH tunnel) ---
    # Set FUNDBOX_CONSUMER_BACKEND_SSH_HOST to enable SSH tunnelling.
    fundbox_consumer_backend_ssh_host: str = ""
    fundbox_consumer_backend_ssh_port: int = 22
    fundbox_consumer_backend_ssh_user: str = ""
    fundbox_consumer_backend_ssh_password: str = ""
    fundbox_consumer_backend_db_host: str = "mysql-fundbox"
    fundbox_consumer_backend_db_port: int = 3306
    fundbox_consumer_backend_db_user: str = "root"
    fundbox_consumer_backend_db_password: str = ""
    fundbox_consumer_backend_db_name: str = "dev"
    fundbox_consumer_backend_chunk_size: int = 1000

    # SpeedZone phppos source DB (MySQL, optionally via SSH tunnel) ----------
    # Set SPEEDZONE_PHPPOS_SSH_HOST to enable SSH tunnelling.
    speedzone_phppos_ssh_host: str = ""
    speedzone_phppos_ssh_port: int = 22
    speedzone_phppos_ssh_user: str = ""
    speedzone_phppos_ssh_password: str = ""
    speedzone_phppos_db_host: str = "mariadb-sz"
    speedzone_phppos_db_port: int = 3306
    speedzone_phppos_db_user: str = "root"
    speedzone_phppos_db_password: str = ""
    speedzone_phppos_db_name: str = "phppos_db"
    speedzone_phppos_chunk_size: int = 1000
    speedzone_phppos_ingest_cron: str = ""

    # Eko phppos source DB (MySQL, optionally via SSH tunnel) ----------------
    # Set EKO_PHPPOS_SSH_HOST to enable SSH tunnelling.
    eko_phppos_ssh_host: str = ""
    eko_phppos_ssh_port: int = 22
    eko_phppos_ssh_user: str = ""
    eko_phppos_ssh_password: str = ""
    eko_phppos_db_host: str = "mariadb-eko"
    eko_phppos_db_port: int = 3306
    eko_phppos_db_user: str = "root"
    eko_phppos_db_password: str = ""
    eko_phppos_db_name: str = "phppos_db"
    eko_phppos_chunk_size: int = 1000
    eko_phppos_ingest_cron: str = ""

    # WhatsApp API (chrishubert/whatsapp-api compatible) ----------------------
    # Multi-tenant WhatsApp Web REST API. Endpoints are session-scoped via
    # `sessionId` and authenticated with a static API key header.
    whatsapp_api_base_url: str = "https://whatsapi.ada.asia"
    whatsapp_api_key: str = ""
    whatsapp_api_default_session: str = "default"
    whatsapp_api_timeout_seconds: float = 30.0

    # Birthday greeting task -------------------------------------------------
    # Daily Celery beat job that sends a WhatsApp birthday message to every
    # active person whose `preferred_dob` (MM-DD) matches today. Disabled by
    # default — flip ``BIRTHDAY_TASK_ENABLED=true`` to schedule it.
    birthday_task_enabled: bool = False
    birthday_task_hour: int = 8  # local hour-of-day, interpreted in TZ below
    birthday_task_minute: int = 0
    # The WhatsApp session to send from. In chrishubert/whatsapp-api the
    # session name typically encodes the source phone number / tenant.
    whatsapp_source_number: str = ""
    # Message template. ``{name}`` is replaced with the person's preferred
    # full name (or "there" if unknown).
    birthday_message_template: str = "Happy birthday, {name}! 🎉"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()  # type: ignore[call-arg]
