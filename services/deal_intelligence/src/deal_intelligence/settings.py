"""Typed, secret-safe configuration for the disabled Deal Intelligence platform."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from os import getenv

from pydantic import SecretStr
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

DEFAULT_DATABASE_URL = "postgresql+psycopg://localhost/deal_intelligence"
TEST_DATABASE_URL_ENV = "HYPERP_DEAL_INTELLIGENCE_TEST_DATABASE_URL"
DATABASE_URL_ENV = "DEAL_INTELLIGENCE_DATABASE_URL"
DATABASE_HOST_ENV = "DEAL_INTELLIGENCE_DATABASE_HOST"
DATABASE_PORT_ENV = "DEAL_INTELLIGENCE_DATABASE_PORT"
DATABASE_NAME_ENV = "DEAL_INTELLIGENCE_DATABASE_NAME"
DATABASE_USER_ENV = "DEAL_INTELLIGENCE_DATABASE_USER"
DATABASE_PASSWORD_ENV = "DEAL_INTELLIGENCE_DATABASE_PASSWORD"


@dataclass(frozen=True, slots=True)
class Settings:
    """Process configuration whose secret-bearing URL is redacted in repr output."""

    database_url: SecretStr | None = None
    database_host: str | None = None
    database_port: int | None = None
    database_name: str | None = None
    database_user: str | None = None
    database_password: SecretStr | None = None
    sql_echo: bool = False

    def __post_init__(self) -> None:
        """Validate the configured SQLAlchemy URL before any engine is created."""
        if self.database_url is not None:
            normalized = _normalize_database_url(self.database_url.get_secret_value())
            object.__setattr__(self, "database_url", SecretStr(normalized))

    @classmethod
    def from_environment(cls) -> Settings:
        """Create settings from opt-in environment variables with a safe local default."""
        database_url = getenv(DATABASE_URL_ENV)
        if database_url is not None and database_url.strip():
            return cls(
                database_url=SecretStr(database_url),
                sql_echo=_environment_bool("DEAL_INTELLIGENCE_SQL_ECHO"),
            )
        test_database_url = getenv(TEST_DATABASE_URL_ENV)
        if test_database_url is not None:
            return cls(
                database_url=SecretStr(test_database_url),
                sql_echo=_environment_bool("DEAL_INTELLIGENCE_SQL_ECHO"),
            )
        if not _separate_database_environment_is_set():
            return cls(
                database_url=SecretStr(DEFAULT_DATABASE_URL),
                sql_echo=_environment_bool("DEAL_INTELLIGENCE_SQL_ECHO"),
            )
        return cls(
            database_host=getenv(DATABASE_HOST_ENV),
            database_port=_environment_port(DATABASE_PORT_ENV),
            database_name=getenv(DATABASE_NAME_ENV),
            database_user=getenv(DATABASE_USER_ENV),
            database_password=_environment_secret(DATABASE_PASSWORD_ENV),
            sql_echo=_environment_bool("DEAL_INTELLIGENCE_SQL_ECHO"),
        )

    def sqlalchemy_database_url(self) -> str:
        """Return the URL only at the database boundary, never for logging."""
        if self.database_url is not None:
            return self.database_url.get_secret_value()
        if (
            self.database_host is None
            and self.database_port is None
            and self.database_name is None
            and self.database_user is None
            and self.database_password is None
        ):
            return DEFAULT_DATABASE_URL
        return _build_database_url(
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
            username=self.database_user,
            password=self.database_password,
        )


def _normalize_database_url(value: str) -> str:
    """Validate a URL and normalize PostgreSQL's bare driver to psycopg 3."""
    try:
        url = make_url(value)
    except ArgumentError as error:
        raise ValueError("database_url must be a valid SQLAlchemy URL") from error
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        raise ValueError("database_url must use the PostgreSQL psycopg dialect")
    return url.render_as_string(hide_password=False)


def _separate_database_environment_is_set() -> bool:
    """Return whether any Compose-style database setting was supplied."""
    return any(
        getenv(name) is not None
        for name in (
            DATABASE_HOST_ENV,
            DATABASE_PORT_ENV,
            DATABASE_NAME_ENV,
            DATABASE_USER_ENV,
            DATABASE_PASSWORD_ENV,
        )
    )


def _environment_port(name: str) -> int | None:
    """Parse an optional TCP port without accepting non-positive values."""
    value = getenv(name)
    if value is None:
        return None
    try:
        port = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if port < 1 or port > 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


def _environment_secret(name: str) -> SecretStr | None:
    """Read a secret without rendering it in errors or representations."""
    value = getenv(name)
    return SecretStr(value) if value is not None else None


def _build_database_url(
    *,
    host: str | None,
    port: int | None,
    database: str | None,
    username: str | None,
    password: SecretStr | None,
) -> str:
    """Build the psycopg URL from Compose fields only when a process needs it."""
    if host is None or not host.strip():
        raise ValueError(f"{DATABASE_HOST_ENV} must be set when no database URL is configured")
    if port is None:
        raise ValueError(f"{DATABASE_PORT_ENV} must be set when no database URL is configured")
    if database is None or not database.strip():
        raise ValueError(f"{DATABASE_NAME_ENV} must be set when no database URL is configured")
    if username is None or not username.strip():
        raise ValueError(f"{DATABASE_USER_ENV} must be set when no database URL is configured")
    if password is None or not password.get_secret_value().strip():
        raise ValueError(
            f"{DATABASE_PASSWORD_ENV} must be nonblank when no database URL is configured"
        )
    return URL.create(
        drivername="postgresql+psycopg",
        username=username,
        password=password.get_secret_value(),
        host=host,
        port=port,
        database=database,
    ).render_as_string(hide_password=False)


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
