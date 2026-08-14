"""Stable contracts shared by the split Bitrix CRM ingestion streams.

The Bitrix source system remains ``bitrix_chat`` for compatibility.  A stream is
an execution/fencing concern, not a new source system or SourceRecord namespace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from src.bitrix_backfill_models import GenerationRunContext
    from src.resumable import CheckpointDescriptor

BitrixStreamKey = Literal[
    "crm_deals",
    "crm_activities",
    "openlines_conversations",
    "crm_stage_history",
]
DealScopeState = Literal["in_scope", "out_of_scope", "indeterminate"]

CRM_DEALS_STREAM: BitrixStreamKey = "crm_deals"
CRM_ACTIVITIES_STREAM: BitrixStreamKey = "crm_activities"
OPENLINES_CONVERSATIONS_STREAM: BitrixStreamKey = "openlines_conversations"
CRM_STAGE_HISTORY_STREAM: BitrixStreamKey = "crm_stage_history"
BITRIX_STREAM_KEYS: frozenset[BitrixStreamKey] = frozenset(
    {
        CRM_DEALS_STREAM,
        CRM_ACTIVITIES_STREAM,
        OPENLINES_CONVERSATIONS_STREAM,
        CRM_STAGE_HISTORY_STREAM,
    }
)

_HISTORY_KIND_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


@dataclass(frozen=True)
class FenceContext:
    """Identity required by a fenced Bitrix domain mutation."""

    logical_run_id: str
    ingest_run_id: str
    source_key: str
    stream_key: BitrixStreamKey
    stream_generation: int
    fencing_token: int
    attempt_generation: int

    def __post_init__(self) -> None:
        if self.source_key != "bitrix_chat":
            raise ValueError("Bitrix fences require source_key='bitrix_chat'")
        if self.stream_key not in BITRIX_STREAM_KEYS:
            raise ValueError("Fence stream_key must be a supported Bitrix stream")
        if not all(
            value.strip()
            for value in (self.logical_run_id, self.ingest_run_id, self.source_key, self.stream_key)
        ):
            raise ValueError("Fence identity values must be non-empty")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (self.stream_generation, self.fencing_token, self.attempt_generation)
        ):
            raise ValueError("Fence generation and token values must be positive")


@dataclass(frozen=True)
class ExecutionContext:
    """Claimed split-run identity passed intact into the ingestion runner."""

    worker_task_id: str
    fence_context: FenceContext
    checkpoint: CheckpointDescriptor
    generation_context: GenerationRunContext | None = None
    max_rows: int | None = None
    max_calls: int | None = None
    deadline_monotonic: float | None = None

    def __post_init__(self) -> None:
        if not self.worker_task_id.strip():
            raise ValueError("execution worker task ID must be non-empty")
        if self.max_rows is not None and self.max_rows < 1:
            raise ValueError("execution max_rows must be positive")
        if self.max_calls is not None and self.max_calls < 1:
            raise ValueError("execution max_calls must be positive")


@dataclass(frozen=True)
class CrmActivityProjection:
    """Typed metadata born with a generic CRM activity SourceRecord."""

    history_kind: str
    event_at: datetime | None
    projection_version: int = 2
    history_family: str = "activity"
    history_source: str = "bitrix_crm_activity"
    projection_source: str = "bitrix_crm_activity_v2"

    def __post_init__(self) -> None:
        if self.history_family != "activity":
            raise ValueError("history_family must be activity")
        if self.history_source != "bitrix_crm_activity":
            raise ValueError("history_source must be bitrix_crm_activity")
        if self.projection_version != 2:
            raise ValueError("projection_version must be 2")
        if self.projection_source != "bitrix_crm_activity_v2":
            raise ValueError("projection_source must be bitrix_crm_activity_v2")
        if not _HISTORY_KIND_PATTERN.fullmatch(self.history_kind):
            raise ValueError("history_kind must be a normalized stable identifier")
        if self.event_at is not None and self.event_at.tzinfo is None:
            raise ValueError("event_at must be timezone-aware")

    @property
    def event_at_iso(self) -> str | None:
        if self.event_at is None:
            return None
        return self.event_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def normalize_history_kind(value: str | None) -> str:
    """Normalize an upstream activity type without changing the raw payload."""
    if value is None:
        return "unknown"
    normalized = "_".join(value.strip().lower().replace("-", " ").split())
    return normalized if _HISTORY_KIND_PATTERN.fullmatch(normalized) else "unknown"


def activity_event_at(start_at: datetime | None, observed_at: datetime | None) -> datetime | None:
    """Select the deterministic typed activity event timestamp."""
    value = start_at if start_at is not None else observed_at
    if value is None or value.tzinfo is None:
        return None
    return value.astimezone(UTC)
