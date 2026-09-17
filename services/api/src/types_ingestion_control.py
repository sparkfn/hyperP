"""Public, redacted models for bounded logical-run operator controls."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BoundedLogicalRunUsage(BaseModel):
    """Cumulative, non-sensitive resource accounting for one logical run."""

    records: int = Field(ge=0)
    source_requests: int = Field(ge=0)
    pages: int = Field(ge=0)
    bytes_read: int = Field(ge=0)
    extraction_calls: int = Field(ge=0)


class BoundedLogicalRunStatus(BaseModel):
    """Safe bounded logical-run progress visible to a human administrator."""

    logical_run_id: str
    source_key: str
    control_instance_id: str
    entity_key: str | None
    status: str
    pause_reason: str | None
    occurrence_id: str | None
    timezone: str | None
    starts_at: str | None
    drain_starts_at: str | None
    cutoff_at: str | None
    next_eligible_at: str | None
    usage: BoundedLogicalRunUsage
    reserved_usage: BoundedLogicalRunUsage
    attempt_generation: int = Field(ge=0)
    source_window_fingerprint: str
    checkpoint_cursor_present: bool
    phase: str | None
    checkpointed_at: str | None
    retry_backlog: int = Field(ge=0)
    retry_oldest_at: str | None
    failure_category: str | None
