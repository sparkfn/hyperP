"""Non-secret finite configuration for the CRM activities archive."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from os import environ


def _positive(name: str, default: int, maximum: int) -> int:
    raw = environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} is outside its bounded range")
    return value


@dataclass(frozen=True)
class CrmActivitiesConfig:
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    neo4j_database: str | None
    source_instance_id: str
    source_key: str
    page_size: int
    max_rows: int
    max_pages: int
    max_checkpoint_bytes: int
    max_checkpoint_entries: int
    database_identity: str
    max_references_per_record: int

    @classmethod
    def from_environment(cls) -> CrmActivitiesConfig:
        uri = environ.get("INTELLIGENCE_NEO4J_URI", "")
        user = environ.get("INTELLIGENCE_NEO4J_USER", "")
        password = environ.get("INTELLIGENCE_NEO4J_PASSWORD", "")
        source_instance_id = environ.get("INTELLIGENCE_CRM_ACTIVITIES_SOURCE_INSTANCE", "")
        source_key = environ.get("INTELLIGENCE_CRM_ACTIVITIES_SOURCE_KEY", "bitrix_chat")
        if not uri or not user or not password or not source_instance_id:
            raise ValueError("CRM activities archive connection/source configuration is incomplete")
        return cls(
            uri,
            user,
            password,
            environ.get("INTELLIGENCE_NEO4J_DATABASE") or None,
            source_instance_id,
            source_key,
            _positive("INTELLIGENCE_CRM_ACTIVITIES_PAGE_SIZE", 100, 1_000),
            _positive("INTELLIGENCE_CRM_ACTIVITIES_MAX_ROWS", 10_000, 100_000),
            _positive("INTELLIGENCE_CRM_ACTIVITIES_MAX_PAGES", 200, 10_000),
            _positive(
                "INTELLIGENCE_CRM_ACTIVITIES_MAX_CHECKPOINT_BYTES",
                100_000_000,
                1_000_000_000,
            ),
            _positive("INTELLIGENCE_CRM_ACTIVITIES_MAX_CHECKPOINT_ENTRIES", 10_000, 100_000),
            _database_identity(uri, environ.get("INTELLIGENCE_NEO4J_DATABASE") or "default"),
            _positive("INTELLIGENCE_CRM_ACTIVITIES_MAX_REFERENCES_PER_RECORD", 100, 10_000),
        )


def _database_identity(uri: str, database: str) -> str:
    return "db-" + hashlib.sha256(f"{uri}|{database}".encode()).hexdigest()[:24]
