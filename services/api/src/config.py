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

    auth_enabled: bool = Field(default=True, alias="AUTH_ENABLED")
    # Same OAuth client the frontend uses via Auth.js (AUTH_GOOGLE_ID).
    google_oauth_client_id: str | None = Field(default=None, alias="AUTH_GOOGLE_ID")
    google_oauth_hosted_domain: str | None = Field(default=None, alias="AUTH_GOOGLE_HOSTED_DOMAIN")
    bootstrap_admin_emails: str = Field(default="", alias="BOOTSTRAP_ADMIN_EMAILS")

    @property
    def bootstrap_admin_email_set(self) -> frozenset[str]:
        """Parse BOOTSTRAP_ADMIN_EMAILS (comma-separated) into a lowercase set."""
        raw = [e.strip().lower() for e in self.bootstrap_admin_emails.split(",")]
        return frozenset(e for e in raw if e)


def get_config() -> AppConfig:
    """Return a fresh AppConfig instance."""
    return AppConfig()  # type: ignore[call-arg]  # pydantic-settings reads env at runtime


config: AppConfig = get_config()
