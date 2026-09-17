"""Repository contract for bounded logical-run operator controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class BoundedLogicalRunUsageRecord:
    """Repository-facing cumulative resource accounting."""

    records: int
    source_requests: int
    pages: int
    bytes_read: int
    extraction_calls: int


@dataclass(frozen=True)
class BoundedLogicalRunStatusRecord:
    """Redacted, repository-facing logical-run status projection."""

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
    usage: BoundedLogicalRunUsageRecord
    phase: str | None
    checkpointed_at: str | None
    retry_backlog: int
    failure_category: str | None


BoundedLogicalRunControlOutcome = Literal["updated", "not_found", "conflict"]


@dataclass(frozen=True)
class BoundedLogicalRunControlResult:
    """One exact-identity operator mutation result."""

    outcome: BoundedLogicalRunControlOutcome
    status: BoundedLogicalRunStatusRecord | None = None
    publish_recovery: bool = False


class IngestionControlRepository(Protocol):
    """Read and mutate a bounded run only through its persisted identity."""

    async def get_bounded_run(
        self,
        logical_run_id: str,
    ) -> BoundedLogicalRunStatusRecord | None:
        """Return a redacted bounded run, or ``None`` when it is not bounded."""
        ...

    async def pause_bounded_run(
        self,
        logical_run_id: str,
        source_key: str,
        control_instance_id: str,
        reset_generation: int,
        reason: str,
    ) -> BoundedLogicalRunControlResult:
        """Persist a durable manual pause intent for one exact bounded run."""
        ...

    async def resume_bounded_run(
        self,
        logical_run_id: str,
        source_key: str,
        control_instance_id: str,
        reset_generation: int,
    ) -> BoundedLogicalRunControlResult:
        """Release an exact manual pause and persist recovery publication intent."""
        ...
