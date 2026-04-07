"""Application configuration via Pydantic Settings."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration for the ingestion service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

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
    fundbox_ingest_cron: str = ""  # e.g. "0 */6 * * *"

    # Fundbox source DB -------------------------------------------------------
    fundbox_db_host: str = "localhost"
    fundbox_db_port: int = 3306
    fundbox_db_user: str = "root"
    fundbox_db_password: str = ""
    fundbox_db_name: str = "fundbox-dev"
    # Streaming chunk size for connector queries (rows per fetch).
    fundbox_chunk_size: int = 1000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()  # type: ignore[call-arg]
