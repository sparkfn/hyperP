"""Environment-only cleanup capability configuration."""

from __future__ import annotations

from dataclasses import dataclass
from os import environ

from intelligence.crm.activities.config import CrmActivitiesConfig


def _boolean(name: str) -> bool:
    value = environ.get(name, "false").lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class CleanupConfig:
    archive: CrmActivitiesConfig
    environment_id: str
    enabled: bool

    @property
    def neo4j_uri(self) -> str:
        return self.archive.neo4j_uri

    @property
    def neo4j_user(self) -> str:
        return self.archive.neo4j_user

    @property
    def neo4j_password(self) -> str:
        return self.archive.neo4j_password

    @property
    def neo4j_database(self) -> str | None:
        return self.archive.neo4j_database

    @classmethod
    def from_environment(cls) -> CleanupConfig:
        environment_id = environ.get("INTELLIGENCE_ENVIRONMENT_ID", "")
        if not environment_id:
            raise ValueError("INTELLIGENCE_ENVIRONMENT_ID is required for cleanup")
        return cls(
            CrmActivitiesConfig.from_environment(),
            environment_id,
            _boolean("INTELLIGENCE_CRM_ACTIVITY_CLEANUP_ENABLED"),
        )
