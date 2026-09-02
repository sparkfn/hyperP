"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    """Strongly-typed application settings."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    neo4j_uri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")
    neo4j_user: str = Field(default="neo4j", alias="NEO4J_USER")
    neo4j_password: str = Field(alias="NEO4J_PASSWORD")
    port: int = Field(default=3000, alias="PORT")
    log_level: str = Field(default="info", alias="LOG_LEVEL")
    root_path: str = Field(default="", alias="ROOT_PATH")
    dumps_root: str = Field(default="/app/dumps", alias="DUMPS_ROOT")
    celery_broker_url: str = Field(default="redis://localhost:6379/0", alias="CELERY_BROKER_URL")
    forwarded_allow_ips: str = Field(default="*", alias="FORWARDED_ALLOW_IPS")
    profile_analysis_enabled: bool = Field(default=False, alias="PROFILE_ANALYSIS_ENABLED")
    profile_analysis_retry_limit: int = Field(
        default=3,
        ge=1,
        le=20,
        alias="PROFILE_ANALYSIS_RETRY_LIMIT",
    )
    profile_analysis_claim_lease_seconds: int = Field(
        default=900,
        ge=180,
        le=86_400,
        alias="PROFILE_ANALYSIS_CLAIM_LEASE_SECONDS",
    )

    auth_enabled: bool = Field(default=True, alias="AUTH_ENABLED")
    # Same OAuth client the frontend uses via Auth.js (AUTH_GOOGLE_ID).
    google_oauth_client_id: str | None = Field(default=None, alias="AUTH_GOOGLE_ID")
    google_oauth_client_secret: str | None = Field(default=None, alias="AUTH_GOOGLE_SECRET")
    google_oauth_hosted_domain: str | None = Field(default=None, alias="AUTH_GOOGLE_HOSTED_DOMAIN")
    redis_url: str = Field(default="redis://redis:6379", alias="REDIS_URL")
    person_list_summary_cache_ttl_seconds: int = Field(
        default=30,
        ge=0,
        le=300,
        alias="PERSON_LIST_SUMMARY_CACHE_TTL_SECONDS",
    )
    neo4j_web_read_transaction_timeout_seconds: float = Field(
        default=25.0,
        ge=1.0,
        le=55.0,
        alias="NEO4J_WEB_READ_TRANSACTION_TIMEOUT_SECONDS",
    )
    entity_summary_cache_ttl_seconds: int = Field(
        default=30,
        ge=0,
        le=300,
        alias="ENTITY_SUMMARY_CACHE_TTL_SECONDS",
    )
    # Minutes before a Google access token is considered expired for revocation purposes.
    # Google issues tokens with a 1-hour expiry; set this to match or slightly above.
    access_token_expiry_minutes: int = Field(default=60, alias="ACCESS_TOKEN_EXPIRY_MINUTES")
    # Minutes before a Google refresh token is considered expired.
    # Google refresh tokens do not expire but may be revoked; 30 days is a safe default.
    refresh_token_expiry_minutes: int = Field(default=43200, alias="REFRESH_TOKEN_EXPIRY_MINUTES")
    bootstrap_admin_emails: str = Field(default="", alias="BOOTSTRAP_ADMIN_EMAILS")
    public_page_expiry_minutes: int = Field(default=30, alias="PUBLIC_PAGE_EXPIRY_MINUTES")
    oauth_issuer: str = Field(default="http://localhost/api", alias="OAUTH_ISSUER")
    oauth_audience: str = Field(default="hyperp-api", alias="OAUTH_AUDIENCE")
    # Access-token lifetime is per-client (OAuthClient.access_token_ttl_seconds,
    # bounded 300–86400 in the model), not a global env var.
    oauth_active_key_id: str = Field(default="local-dev", alias="OAUTH_ACTIVE_KEY_ID")
    oauth_private_key_pem: str = Field(default="", alias="OAUTH_PRIVATE_KEY_PEM")
    oauth_public_key_pem: str = Field(default="", alias="OAUTH_PUBLIC_KEY_PEM")
    oauth_secret_hash_key: str = Field(default="", alias="OAUTH_SECRET_HASH_KEY")
    # LLM service (OpenAI-compatible endpoint)
    llm_api_base_url: str = Field(
        default="https://api.openai.com",
        alias="LLM_API_BASE_URL",
    )
    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    llm_default_model: str | None = Field(default=None, alias="LLM_DEFAULT_MODEL")
    # Proclaude service (proxy-claude-v2 Anthropic Messages API)
    proclaude_api_base_url: str | None = Field(default=None, alias="PROCLAUDE_API_BASE_URL")
    proclaude_api_key: str | None = Field(default=None, alias="PROCLAUDE_API_KEY")
    proclaude_default_model: str | None = Field(default=None, alias="PROCLAUDE_DEFAULT_MODEL")

    @property
    def bootstrap_admin_email_set(self) -> frozenset[str]:
        """Parse BOOTSTRAP_ADMIN_EMAILS (comma-separated) into a lowercase set."""
        raw = [e.strip().lower() for e in self.bootstrap_admin_emails.split(",")]
        return frozenset(e for e in raw if e)


def get_config() -> AppConfig:
    """Return a fresh AppConfig instance."""
    return AppConfig()  # type: ignore[call-arg]  # pydantic-settings reads env at runtime


config: AppConfig = get_config()
