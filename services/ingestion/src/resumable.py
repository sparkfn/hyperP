"""Connector-neutral primitives for checkpointed ingestion.

The existing ``SourceConnector`` iterator remains supported during the rollout,
but new connector paths use these types to make their replay boundary explicit.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol

from src.models import JsonValue

LogicalRunStatus = Literal[
    "queued",
    "running",
    "stop_requested",
    "paused_with_checkpoint",
    "completed",
    "completed_with_errors",
    "failed",
]
AttemptStatus = Literal[
    "queued",
    "started",
    "paused_with_checkpoint",
    "completed",
    "completed_with_errors",
    "failed",
    "superseded",
]
CheckpointStatus = Literal["active", "paused", "completed", "incompatible", "archived"]
RecordDisposition = Literal[
    "committed",
    "duplicate",
    "excluded",
    "policy_dropped",
    "durable_retry",
]
CheckpointCompatibility = Literal[
    "compatible",
    "incompatible",
    "expired",
    "rejected",
    "corrupted",
    "temporarily_unavailable",
]


@dataclass(frozen=True)
class CheckpointDescriptor:
    """Durable continuation position after one fully committed unit."""

    phase: str
    cursor: dict[str, JsonValue]
    source_window: dict[str, JsonValue]
    last_committed_record_id: str | None
    connector_version: str
    schema_version: int
    replay_boundary: str

    def __post_init__(self) -> None:
        if not self.phase.strip():
            raise ValueError("Checkpoint phase must be non-empty")
        if not self.connector_version.strip():
            raise ValueError("Checkpoint connector version must be non-empty")
        if isinstance(self.schema_version, bool) or self.schema_version < 1:
            raise ValueError("Checkpoint schema version must be positive")
        if not self.replay_boundary.strip():
            raise ValueError("Checkpoint replay boundary must be non-empty")


@dataclass(frozen=True)
class IngestionUnit:
    """One bounded source page, keyset range, dump chunk, or LLM batch."""

    checkpoint_before: CheckpointDescriptor
    checkpoint_after: CheckpointDescriptor
    records: tuple[dict[str, JsonValue], ...]
    source_total: int | None = None

    def __post_init__(self) -> None:
        if self.checkpoint_before.phase != self.checkpoint_after.phase:
            raise ValueError("One ingestion unit cannot cross checkpoint phases")
        if self.checkpoint_before.connector_version != self.checkpoint_after.connector_version:
            raise ValueError("One ingestion unit cannot cross connector versions")
        if self.checkpoint_before.schema_version != self.checkpoint_after.schema_version:
            raise ValueError("One ingestion unit cannot cross checkpoint schema versions")
        if self.source_total is not None and (
            isinstance(self.source_total, bool) or self.source_total < 0
        ):
            raise ValueError("Ingestion unit source total must be non-negative")


class ResumableConnector(Protocol):
    """Optional connector capability used by the checkpoint-aware runner."""

    def get_source_key(self) -> str:
        """Return the source-system key for this connector."""
        ...

    def validate_checkpoint(
        self,
        checkpoint: CheckpointDescriptor,
    ) -> CheckpointCompatibility:
        """Return whether a checkpoint can safely continue against the upstream."""
        ...

    def fetch_units(
        self,
        checkpoint: CheckpointDescriptor | None,
    ) -> Iterator[IngestionUnit]:
        """Yield bounded units beginning at the durable replay boundary."""
        ...


def checkpoint_can_advance(dispositions: tuple[RecordDisposition, ...]) -> bool:
    """Return whether every source record has a durable terminal disposition."""
    return all(
        disposition
        in {
            "committed",
            "duplicate",
            "excluded",
            "policy_dropped",
            "durable_retry",
        }
        for disposition in dispositions
    )
