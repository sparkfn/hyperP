"""Application configuration via Pydantic Settings."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.connectors.whatsadmin_api.credentials import (
    WhatsAdminEntity,
    is_valid_whatsadmin_handle_key,
    whatsadmin_entity_keys_are_distinct,
)


class Settings(BaseSettings):
    """Environment-driven configuration for the ingestion service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
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
    dumps_root: str = "/app/dumps"
    batch_size: int = 500

    # Celery / queue ----------------------------------------------------------
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"
    celery_broker_visibility_timeout: int = 60 * 60 * 8

    # Fundbox source DB (MySQL, optionally via SSH tunnel) --------------------
    # Set FUNDBOX_SSH_HOST to enable SSH tunnelling.
    fundbox_ssh_host: str = ""
    fundbox_ssh_port: int = 22
    fundbox_ssh_user: str = ""
    fundbox_ssh_password: str = ""
    fundbox_db_host: str = "mysql-fundbox"
    fundbox_db_port: int = 3306
    fundbox_db_user: str = "root"
    fundbox_db_password: str = ""
    fundbox_db_name: str = "dev"
    fundbox_chunk_size: int = 1000

    # Fundbox backdoor API extraction ----------------------------------------
    # Connection settings default to empty strings so the service can boot
    # before credentials are provisioned. Empty config is rejected at runtime
    # when a Fundbox API ingestion is dispatched (see FundboxApiCredentials),
    # not at process startup.
    fundbox_api_base_url: str = ""
    fundbox_api_username: str = ""
    fundbox_api_password: SecretStr = SecretStr("")
    fundbox_api_page_size: int = Field(default=100, ge=1, le=500)
    fundbox_api_timeout_seconds: float = Field(default=30.0, gt=0)
    fundbox_api_max_attempts: int = Field(default=3, ge=1, le=10)
    fundbox_api_overlap_seconds: int = Field(default=300, ge=0)

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

    # POS OAuth API extraction -----------------------------------------------
    phppos_api_base_url: str = ""
    phppos_api_client_id: str = ""
    phppos_api_client_secret: SecretStr = SecretStr("")
    phppos_api_page_size: int = 500
    phppos_api_timeout_seconds: float = 30.0
    phppos_api_max_attempts: int = 3
    speedzone_phppos_api_tenant_id: str = ""
    eko_phppos_api_tenant_id: str = ""

    # SG bankruptcy scraper export API --------------------------------------
    sgbankruptcy_api_base_url: str = ""
    sgbankruptcy_api_key: SecretStr = SecretStr("")
    sgbankruptcy_api_page_size: int = Field(default=500, ge=1, le=1000)
    sgbankruptcy_api_timeout_seconds: float = Field(default=30.0, gt=0)
    sgbankruptcy_api_max_attempts: int = Field(default=3, ge=1)

    # SG rental flats API extraction -----------------------------------------
    sgrentalflats_api_base_url: str = "https://sgrentalflats-api.ada.asia"
    sgrentalflats_api_key: SecretStr = SecretStr("")
    sgrentalflats_api_page_size: int = 500
    sgrentalflats_api_timeout_seconds: float = 30.0

    # WhatsApp API (chrishubert/whatsapp-api compatible) ----------------------
    # Multi-tenant WhatsApp Web REST API. Endpoints are session-scoped via
    # `sessionId` and authenticated with a static API key header.
    whatsapp_api_base_url: str = "https://whatsapi.ada.asia"
    whatsapp_api_key: str = ""
    whatsapp_api_default_session: str = "default"
    whatsapp_api_timeout_seconds: float = 30.0

    # Bitrix24 chat database (MariaDB) ---------------------------------------
    # Set BITRIX_CHAT_SSH_HOST to enable SSH tunnelling.
    bitrix_chat_ssh_host: str = ""
    bitrix_chat_ssh_port: int = 2222
    bitrix_chat_ssh_user: str = ""
    bitrix_chat_ssh_password: str = ""
    bitrix_chat_db_host: str = "mariadb-bitrix-chat"
    bitrix_chat_db_port: int = 3306
    bitrix_chat_db_user: str = "root"
    bitrix_chat_db_password: str = ""
    bitrix_chat_db_name: str = "bitrix_chat"
    bitrix_chat_chunk_size: int = 500

    # Bitrix Open Lines REST API --------------------------------------------
    # This is an inbound-webhook base URL and therefore contains a credential.
    bitrix_openlines_api_base_url: SecretStr = SecretStr("")
    bitrix_openlines_api_timeout_seconds: float = Field(default=30.0, gt=0)
    bitrix_openlines_api_max_attempts: int = Field(default=3, ge=1, le=10)
    bitrix_openlines_api_request_delay_seconds: float = Field(default=0.5, ge=0)

    # Restricted stage-history artifacts. The signing secret is supplied only
    # to the worker/operator runtime; it must never be placed in task payloads.
    stage_history_artifact_primary_root: str = "/app/restricted/stage-history"
    stage_history_artifact_backup_root: str = "/app/restricted/stage-history-backup"
    stage_history_artifact_signing_key_id: str = ""
    stage_history_artifact_signing_key_secret: SecretStr = SecretStr("")
    stage_history_repository_sha: str = ""
    stage_history_image_digest: str = ""

    # Historical Bitrix CRM-deal repair inventory is read-only and staging-only.
    # Mutation remains disabled unless a separately approved #255 operation
    # explicitly enables it in a staging runtime.
    deployment_environment: Literal["development", "staging", "production"] = "development"
    crm_deal_identity_repair_enabled: bool = False
    crm_deal_identity_repair_expected_workers: tuple[str, ...] = ()
    crm_deal_identity_repair_worker_timeout_seconds: int = Field(default=10, ge=1, le=120)
    crm_deal_identity_repair_artifact_primary_root: str = "/app/restricted/crm-deal-identity-repair"
    crm_deal_identity_repair_artifact_backup_root: str = (
        "/app/restricted/crm-deal-identity-repair-backup"
    )
    crm_deal_identity_repair_artifact_signing_key_id: str = ""
    crm_deal_identity_repair_artifact_signing_key_secret: SecretStr = SecretStr("")
    # Separate approver authority: an artifact-sealing key cannot authorize allocation rows.
    crm_deal_identity_repair_approval_overlay_verification_secret: SecretStr = SecretStr("")
    crm_deal_identity_repair_repository_sha: str = ""
    crm_deal_identity_repair_image_digest: str = ""

    # Restricted sales-prediction artifacts (issue #125 datasets/evaluation and
    # issue #126 model artifacts). Same trust model as stage history: persistent
    # restricted mounts, separate HMAC domain, secret only in worker/operator
    # runtime env — never in task payloads.
    sales_prediction_artifact_primary_root: str = "/app/restricted/sales-prediction"
    sales_prediction_artifact_backup_root: str = "/app/restricted/sales-prediction-backup"
    sales_prediction_artifact_signing_key_id: str = ""
    sales_prediction_artifact_signing_key_secret: SecretStr = SecretStr("")
    sales_prediction_repository_sha: str = ""
    sales_prediction_image_digest: str = ""

    # WhatsApp chat database (PostgreSQL) ------------------------------------
    # Set WHATSAPP_CHAT_SSH_HOST to enable SSH tunnelling.
    whatsapp_chat_ssh_host: str = ""
    whatsapp_chat_ssh_port: int = 2222
    whatsapp_chat_ssh_user: str = ""
    whatsapp_chat_ssh_password: str = ""
    whatsapp_chat_db_host: str = "postgres-whatsapp"
    whatsapp_chat_db_port: int = 5432
    whatsapp_chat_db_user: str = "postgres"
    whatsapp_chat_db_password: str = ""
    whatsapp_chat_db_name: str = "whatsapp_api"
    whatsapp_chat_chunk_size: int = 500

    # WhatsAdmin HyperP extraction API ---------------------------------------
    whatsadmin_api_base_url: str = ""
    whatsadmin_eko_api_key: SecretStr = SecretStr("")
    whatsadmin_speedzone_api_key: SecretStr = SecretStr("")
    whatsadmin_eko_enabled: bool = False
    whatsadmin_speedzone_enabled: bool = False
    whatsadmin_legacy_entity: WhatsAdminEntity | None = None
    whatsadmin_api_page_size: int = Field(default=25, ge=1)
    whatsadmin_api_timeout_seconds: float = Field(default=120.0, gt=0)
    whatsadmin_api_max_attempts: int = Field(default=5, ge=1, le=10)
    whatsadmin_api_retry_base_delay_seconds: float = Field(default=1.0, ge=0)

    @model_validator(mode="after")
    def _validate_enabled_whatsadmin_entities(self) -> Settings:
        enabled_keys: tuple[tuple[str, bool, SecretStr], ...] = (
            ("eko", self.whatsadmin_eko_enabled, self.whatsadmin_eko_api_key),
            (
                "speedzone",
                self.whatsadmin_speedzone_enabled,
                self.whatsadmin_speedzone_api_key,
            ),
        )
        if not any(enabled for _, enabled, _ in enabled_keys):
            return self
        if not self.whatsadmin_api_base_url.strip():
            raise ValueError("WhatsAdmin base URL is required when an entity is enabled")
        for entity_key, enabled, api_key in enabled_keys:
            if enabled and not is_valid_whatsadmin_handle_key(api_key):
                raise ValueError(
                    f"WhatsAdmin {entity_key} handle API key is required when the entity is enabled"
                )
        if not whatsadmin_entity_keys_are_distinct(
            self.whatsadmin_eko_api_key,
            self.whatsadmin_speedzone_api_key,
        ):
            raise ValueError("WhatsAdmin Eko and Speedzone API keys must be distinct")
        return self

    # Hard ingestion exclusions -------------------------------------------------
    company_mobile_numbers: list[str] = []
    company_email_addresses: list[str] = []
    internal_person_names: list[str] = []
    # Consolidated ingestion config (exclusions + LLM call tuning). LLM service
    # tuning (timeout/delay/retries) lives in this file's `llm` block; LLM
    # GPT and ProClaude provider credentials are read from the environment.
    ingestion_config_file: str = ""

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
