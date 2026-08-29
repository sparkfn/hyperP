"""Typed, secret-safe configuration for the disabled Deal Intelligence platform."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from os import getenv

from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

DEFAULT_DATABASE_URL = "postgresql+psycopg://localhost/deal_intelligence"
TEST_DATABASE_URL_ENV = "HYPERP_DEAL_INTELLIGENCE_TEST_DATABASE_URL"


@dataclass(frozen=True, slots=True)
class Settings:
    """Process configuration whose secret-bearing URL is redacted in repr output."""

    database_url: SecretStr = field(default_factory=lambda: SecretStr(DEFAULT_DATABASE_URL))
    sql_echo: bool = False

    def __post_init__(self) -> None:
        """Validate the configured SQLAlchemy URL before any engine is created."""
        try:
            drivername = make_url(self.database_url.get_secret_value()).drivername
        except ArgumentError as error:
            raise ValueError("database_url must be a valid SQLAlchemy URL") from error
        if drivername not in {"postgresql", "postgresql+psycopg"}:
            raise ValueError("database_url must use the PostgreSQL psycopg dialect")

    @classmethod
    def from_environment(cls) -> Settings:
        """Create settings from opt-in environment variables with a safe local default."""
        database_url = SecretStr(
            getenv("DEAL_INTELLIGENCE_DATABASE_URL")
            or getenv(TEST_DATABASE_URL_ENV)
            or DEFAULT_DATABASE_URL
        )
        return cls(
            database_url=database_url, sql_echo=_environment_bool("DEAL_INTELLIGENCE_SQL_ECHO")
        )

    def sqlalchemy_database_url(self) -> str:
        """Return the URL only at the database boundary, never for logging."""
        return self.database_url.get_secret_value()


def _environment_bool(name: str) -> bool:
    """Parse an optional boolean environment setting strictly."""
    value = getenv(name)
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


@lru_cache
def get_settings() -> Settings:
    """Return the immutable cached process configuration."""
    return Settings.from_environment()
