"""Strict contracts for the durable bounded-ingestion path."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from neo4j import ManagedTransaction
from pydantic.types import JsonValue

from src.bitrix_ingestion_models import FenceContext
from src.resumable import CheckpointCompatibility, CheckpointDescriptor, IngestionUnit

SCHEDULE_TIMEZONE = "Asia/Singapore"
RunStatus = Literal[
    "queued",
    "running",
    "paused_with_checkpoint",
    "completed",
    "failed",
    "blocked",
]
PauseReason = Literal[
    "disabled",
    "manual",
    "budget",
    "source_backoff",
    "schedule_window_closed",
    "shutdown",
]
FailureCategory = Literal[
    "capability",
    "checkpoint",
    "lease",
    "overrun",
    "source",
    "writer",
]
RecordDisposition = Literal[
    "committed",
    "duplicate",
    "excluded",
    "policy_dropped",
    "durable_retry",
]
BoundedMode = Literal["bootstrap", "delta", "one_time"]


@dataclass(frozen=True)
class RunScope:
    """Stable identity and compatibility contract for one logical run."""

    environment: str
    reset_generation: int
    source_key: str
    control_instance_id: str
    entity_key: str | None
    stream_key: str | None
    mode: BoundedMode
    configuration_fingerprint: str
    connector_version: str
    checkpoint_schema_version: int
    source_window: dict[str, JsonValue]

    def __post_init__(self) -> None:
        required = (
            self.environment,
            self.source_key,
            self.control_instance_id,
            self.configuration_fingerprint,
            self.connector_version,
        )
        if not all(value.strip() for value in required):
            raise ValueError("run scope fields must be non-empty")
        if self.reset_generation < 1 or self.checkpoint_schema_version < 1:
            raise ValueError("run scope generations must be positive")

    @property
    def scope_key(self) -> str:
        """Return identity only; compatibility drift must reject the same scope."""
        return "|".join(
            (
                self.environment,
                str(self.reset_generation),
                self.source_key,
                self.control_instance_id,
                self.entity_key or "-",
                self.stream_key or "-",
                self.mode,
            )
        )

    @property
    def source_window_fingerprint(self) -> str:
        encoded = json.dumps(
            self.source_window,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class OccurrenceContext:
    """Persisted occurrence deadline, independent of task delivery time."""

    occurrence_id: str
    starts_at: datetime
    drain_starts_at: datetime
    cutoff_at: datetime
    next_eligible_at: datetime
    timezone: str = SCHEDULE_TIMEZONE
    scheduled: bool = True

    def __post_init__(self) -> None:
        values = (
            self.starts_at,
            self.drain_starts_at,
            self.cutoff_at,
            self.next_eligible_at,
        )
        if not self.occurrence_id.strip():
            raise ValueError("occurrence ID must be non-empty")
        if any(value.tzinfo is None or value.utcoffset() is None for value in values):
            raise ValueError("occurrence timestamps must be timezone-aware")
        if not self.starts_at < self.drain_starts_at < self.cutoff_at:
            raise ValueError("occurrence requires start < drain < cutoff")
        if self.scheduled:
            self._validate_scheduled_window()

    def _validate_scheduled_window(self) -> None:
        if self.timezone != SCHEDULE_TIMEZONE:
            raise ValueError("scheduled ingestion must use Asia/Singapore")
        zone = ZoneInfo(self.timezone)
        start = self.starts_at.astimezone(zone)
        cutoff = self.cutoff_at.astimezone(zone)
        next_start = self.next_eligible_at.astimezone(zone)
        if (start.hour, start.minute, start.second, start.microsecond) != (9, 0, 0, 0):
            raise ValueError("scheduled occurrence must open at 09:00 local")
        if (cutoff.hour, cutoff.minute, cutoff.second, cutoff.microsecond) != (23, 0, 0, 0):
            raise ValueError("scheduled occurrence must close at 23:00 local")
        if start.date() != cutoff.date():
            raise ValueError("scheduled occurrence must close on its opening day")
        if (self.next_eligible_at - self.starts_at).total_seconds() != 7 * 24 * 60 * 60:
            raise ValueError("scheduled continuation must be next weekly opening")
        if (next_start.hour, next_start.minute) != (9, 0):
            raise ValueError("next scheduled occurrence must open at 09:00 local")

    def can_start(self, now: datetime) -> bool:
        return self.starts_at <= now < self.drain_starts_at

    def can_finish(self, now: datetime, worst_case_seconds: float) -> bool:
        if worst_case_seconds <= 0:
            raise ValueError("worst-case unit duration must be positive")
        return now.timestamp() + worst_case_seconds < self.cutoff_at.timestamp()

    def local_iso(self, value: datetime) -> str:
        return value.astimezone(ZoneInfo(self.timezone)).isoformat()


@dataclass(frozen=True)
class Usage:
    records: int = 0
    source_requests: int = 0
    pages: int = 0
    bytes_read: int = 0
    extraction_calls: int = 0

    def __post_init__(self) -> None:
        if any(value < 0 for value in self.values()):
            raise ValueError("usage must be non-negative")

    def values(self) -> tuple[int, int, int, int, int]:
        return (
            self.records,
            self.source_requests,
            self.pages,
            self.bytes_read,
            self.extraction_calls,
        )

    def add(self, other: Usage) -> Usage:
        return Usage(
            records=self.records + other.records,
            source_requests=self.source_requests + other.source_requests,
            pages=self.pages + other.pages,
            bytes_read=self.bytes_read + other.bytes_read,
            extraction_calls=self.extraction_calls + other.extraction_calls,
        )


class CancellationSignal(Protocol):
    def requested(self) -> bool: ...


@dataclass(frozen=True)
class AttemptContext:
    logical_run_id: str
    ingest_run_id: str
    worker_task_id: str
    attempt_generation: int
    fencing_token: int
    lease_token: str
    global_slot_index: int
    global_slot_fencing_token: int
    scope: RunScope
    occurrence: OccurrenceContext | None
    checkpoint: CheckpointDescriptor
    usage: Usage
    reserved_usage: Usage
    retry_backlog: int = 0
    terminal_observed: bool = False
    terminal_checkpoint_committed: bool = False
    bitrix_fence_context: FenceContext | None = None
    operation_deadline_at: datetime | None = None
    cancellation: CancellationSignal | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        identifiers = (
            self.logical_run_id,
            self.ingest_run_id,
            self.worker_task_id,
            self.lease_token,
        )
        if not all(value.strip() for value in identifiers):
            raise ValueError("attempt identity must be non-empty")
        fences = (
            self.attempt_generation,
            self.fencing_token,
            self.global_slot_fencing_token,
        )
        if self.lease_expires_at is not None and (
            self.lease_expires_at.tzinfo is None or self.lease_expires_at.utcoffset() is None
        ):
            raise ValueError("lease expiry must be timezone-aware")
        if any(value < 1 for value in fences) or self.global_slot_index < 0:
            raise ValueError("attempt fences must be positive")
        if self.retry_backlog < 0:
            raise ValueError("retry backlog must be non-negative")

    def remaining_seconds(self, now: datetime) -> float | None:
        deadline = self.operation_deadline_at
        if deadline is None and self.occurrence is not None:
            deadline = self.occurrence.cutoff_at
        if deadline is None:
            return None
        return max((deadline - now).total_seconds(), 0.0)

    def require_operation_budget(self, now: datetime, worst_case_seconds: float) -> None:
        remaining = self.remaining_seconds(now)
        if remaining is not None and remaining < worst_case_seconds:
            raise TimeoutError("bounded operation cannot finish before cutoff")


BoundedAdmissionResult = AttemptContext | Literal["completed"] | None


@dataclass(frozen=True)
class BoundedUnit:
    unit: IngestionUnit
    replay_id: str
    usage: Usage
    terminal: bool

    def __post_init__(self) -> None:
        if not self.replay_id.strip():
            raise ValueError("bounded unit replay ID must be non-empty")
        if self.unit.checkpoint_before == self.unit.checkpoint_after and not self.terminal:
            raise ValueError("non-terminal bounded unit must advance its checkpoint")


@dataclass(frozen=True)
class RetryObligation:
    replay_id: str
    source_record_id: str
    source_version: str
    category: str
    attempt_count: int
    eligible_at: datetime | None

    def __post_init__(self) -> None:
        required = (
            self.replay_id,
            self.source_record_id,
            self.source_version,
            self.category,
        )
        if not all(value.strip() for value in required) or self.attempt_count < 1:
            raise ValueError("retry obligation is invalid")


@dataclass(frozen=True)
class RetryResolution:
    replay_id: str
    source_record_id: str

    def __post_init__(self) -> None:
        if not self.replay_id.strip() or not self.source_record_id.strip():
            raise ValueError("retry resolution identity must be non-empty")


@dataclass(frozen=True)
class UnitApplyResult:
    dispositions: tuple[RecordDisposition, ...]
    retry_obligations: tuple[RetryObligation, ...] = ()
    resolved_retries: tuple[RetryResolution, ...] = ()


class BoundedUnitWriter(Protocol):
    def apply(
        self,
        tx: ManagedTransaction,
        context: AttemptContext,
        unit: BoundedUnit,
    ) -> UnitApplyResult:
        """Perform deterministic domain writes only; never call a source or LLM."""
        ...


class BoundedConnector(Protocol):
    def validate_checkpoint(
        self,
        checkpoint: CheckpointDescriptor,
    ) -> CheckpointCompatibility: ...

    def fetch_one_unit(
        self,
        checkpoint: CheckpointDescriptor,
        context: AttemptContext,
    ) -> BoundedUnit: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class BoundedReadinessCheck(Protocol):
    """Optional descriptor capability gating dispatch on proven source readiness."""

    def readiness_block(self) -> str | None:
        """Return a safe blocked reason while this source cannot be dispatched yet."""
        ...


class BoundedConnectorDescriptor(Protocol):
    source_key: str
    connector_version: str
    configuration_version: str
    checkpoint_schema_version: int
    supports_bootstrap: bool
    supports_delta: bool
    supports_one_time: bool
    max_records_per_unit: int
    max_source_requests_per_unit: int
    max_bytes_per_unit: int
    max_extraction_calls_per_unit: int
    max_close_seconds: float
    max_retry_backoff_seconds: float
    supports_deadline: bool
    supports_cancellation: bool
    writer: BoundedUnitWriter

    def initial_checkpoint(
        self,
        scope: RunScope,
        occurrence: OccurrenceContext | None,
    ) -> CheckpointDescriptor: ...

    def create(self, context: AttemptContext) -> BoundedConnector: ...


@dataclass(frozen=True)
class BoundedStatus:
    logical_run_id: str
    source_key: str
    control_instance_id: str
    entity_key: str | None
    status: RunStatus
    pause_reason: PauseReason | None
    occurrence_id: str | None
    timezone: str | None
    starts_at: str | None
    drain_starts_at: str | None
    cutoff_at: str | None
    next_eligible_at: str | None
    usage: Usage
    reserved_usage: Usage
    attempt_generation: int
    source_window_fingerprint: str
    checkpoint_cursor_present: bool
    phase: str | None
    checkpointed_at: str | None
    retry_backlog: int
    retry_oldest_at: str | None
    failure_category: FailureCategory | None


@dataclass(frozen=True)
class BoundedRecoveryState:
    scope: RunScope
    occurrence: OccurrenceContext


BoundedRecoveryResult = BoundedRecoveryState | Literal["completed"] | datetime | None


class BoundedRecoveryLeasedError(RuntimeError):
    def __init__(self, retry_at: datetime) -> None:
        super().__init__("bounded recovery lease is still active")
        self.retry_at = retry_at


@dataclass(frozen=True)
class BoundedRunResult:
    status: RunStatus
    context: AttemptContext | None
    pause_reason: PauseReason | None = None
    failure_category: FailureCategory | None = None
    safe_message: str | None = None
    unit_usage: Usage = Usage()


class SourceBackoffError(RuntimeError):
    def __init__(self, retry_at: datetime, safe_message: str = "source backoff") -> None:
        super().__init__(safe_message)
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            raise ValueError("source retry time must be timezone-aware")
        self.retry_at = retry_at
        self.safe_message = safe_message


def utc_now() -> datetime:
    return datetime.now(UTC)
