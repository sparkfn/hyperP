"""Admin repository protocol (source systems and field trust)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.types import TrustTier


@dataclass
class SourceSystemInfo:
    source_key: str
    is_active: bool
    field_trust: dict[str, str]
    source_system_id: str | None = None
    display_name: str | None = None
    system_type: str | None = None
    entity_key: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    latest_run_id: str | None = None
    latest_run_status: str | None = None
    latest_run_started_at: str | None = None
    latest_run_finished_at: str | None = None
    latest_failure_category: str | None = None
    latest_failure_exception_class: str | None = None
    latest_failure_message: str | None = None
    latest_failure_mode: str | None = None
    latest_failure_started_at: str | None = None
    latest_failure_task_id: str | None = None
    latest_failure_checkpoint: str | None = None


@dataclass
class FieldTrustResponse:
    source_key: str
    field_trust: dict[str, str]
    display_name: str | None = None


class AdminRepository(Protocol):
    async def get_all_source_systems(self) -> list[SourceSystemInfo]: ...

    async def get_field_trust(self, source_key: str) -> FieldTrustResponse | None: ...

    async def update_field_trust(
        self,
        source_key: str,
        updates: dict[str, TrustTier],
    ) -> dict[str, str] | None:
        """Returns merged field_trust dict, or None if source system not found."""
        ...
