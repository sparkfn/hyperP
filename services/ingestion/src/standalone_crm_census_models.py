"""Core immutable types for the default-off standalone CRM census control plane."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

StandaloneCrmCensusKind = Literal["source_sync", "mapping_prepare", "mapping_rollback"]
StandaloneCrmSourceUnitKind = Literal["contact", "lead", "company"]
StandaloneCrmMappingUnitKind = Literal["mapping_prepare", "mapping_rollback"]
StandaloneCrmUnitKind = StandaloneCrmSourceUnitKind | StandaloneCrmMappingUnitKind
StandaloneCrmTerminalState = Literal[
    "completed", "failed", "cancelled_with_checkpoint", "freeze_failed"
]
StandaloneCrmCensusState = Literal[
    "allocated",
    "freezing",
    "frozen",
    "publishing",
    "running",
    "pause_requested",
    "paused_with_checkpoint",
    "continuing",
    "cancel_requested",
    "recovering",
    "authority_stale_pending",
    "completed",
    "failed",
    "cancelled_with_checkpoint",
    "freeze_failed",
]
StandaloneCrmAttemptState = Literal[
    "queued", "running", "paused_with_checkpoint", "failed", "superseded", "completed"
]
StandaloneCrmChildState = Literal[
    "pending_publication",
    "publishing",
    "queued",
    "running",
    "paused",
    "completed",
    "failed",
    "cancelled",
    "superseded",
]
StandaloneCrmChildSettlementState = Literal["completed", "failed", "cancelled"]
StandaloneCrmPublicationState = Literal[
    "reserved",
    "publishing",
    "published",
    "ambiguous",
    "retired",
]
StandaloneCrmPublicationObservation = Literal["none", "fence_claim", "checkpoint_advanced"]
StandaloneCrmCallKind = Literal["probe", "page", "company_binding"]
StandaloneCrmCallOutcome = Literal["reserved", "succeeded", "failed", "unknown"]
StandaloneCrmReasonCode = Literal[
    "authority_unavailable",
    "authority_stale",
    "budget_exhausted",
    "cancelled",
    "child_handler_unavailable",
    "freeze_incomplete",
    "publication_ambiguous",
    "stale_fence",
    "terminal_invariant_failed",
    "unknown_call_outcome",
    "payload_conflict",
]

SOURCE_UNIT_KINDS = frozenset({"contact", "lead", "company"})
MAPPING_UNIT_KINDS = frozenset({"mapping_prepare", "mapping_rollback"})
TERMINAL_CENSUS_STATES = frozenset(
    {"completed", "failed", "cancelled_with_checkpoint", "freeze_failed"}
)


def required_text(value: object, field: str, *, limit: int = 200) -> str:
    """Validate a bounded non-secret text identifier."""
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{field} must be a non-empty bounded string")
    return value.strip()


def non_negative_int(value: object, field: str) -> int:
    """Validate an integer counter or numeric source bound."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def positive_int(value: object, field: str) -> int:
    """Validate a strictly positive integer identity component."""
    result = non_negative_int(value, field)
    if result < 1:
        raise ValueError(f"{field} must be a positive integer")
    return result


def utc_datetime(value: datetime, field: str) -> datetime:
    """Normalize one persistable absolute timestamp."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class StandaloneCrmBudgetSnapshot:
    """Immutable per-attempt and occurrence ceilings for one census occurrence."""

    max_calls_per_attempt: int
    max_rows_per_attempt: int
    max_runtime_seconds_per_attempt: float
    max_calls_per_occurrence: int
    max_rows_per_occurrence: int
    max_attempts_per_occurrence: int
    max_wall_clock_seconds_per_occurrence: float

    def __post_init__(self) -> None:
        for field in (
            "max_calls_per_attempt",
            "max_rows_per_attempt",
            "max_calls_per_occurrence",
            "max_rows_per_occurrence",
            "max_attempts_per_occurrence",
        ):
            positive_int(getattr(self, field), field)
        for field in ("max_runtime_seconds_per_attempt", "max_wall_clock_seconds_per_occurrence"):
            value = getattr(self, field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
                or not math.isfinite(value)
            ):
                raise ValueError(f"{field} must be a positive finite number")
        if self.max_calls_per_attempt > self.max_calls_per_occurrence:
            raise ValueError("attempt call ceiling exceeds occurrence ceiling")
        if self.max_rows_per_attempt > self.max_rows_per_occurrence:
            raise ValueError("attempt row ceiling exceeds occurrence ceiling")
        if self.max_runtime_seconds_per_attempt > self.max_wall_clock_seconds_per_occurrence:
            raise ValueError("attempt runtime exceeds occurrence wall-clock ceiling")


@dataclass(frozen=True)
class FrozenSourceWindow:
    """Complete immutable selected source bounds; absence differs from bound zero."""

    selected_kinds: tuple[StandaloneCrmSourceUnitKind, ...]
    upper_bounds: tuple[tuple[StandaloneCrmSourceUnitKind, int], ...]
    algorithm_version: str = "standalone-crm-source-window-v1"

    def __post_init__(self) -> None:
        if not self.selected_kinds:
            raise ValueError("frozen source window requires selected kinds")
        if tuple(sorted(self.selected_kinds)) != self.selected_kinds:
            raise ValueError("selected kinds must use canonical sorted order")
        if len(set(self.selected_kinds)) != len(self.selected_kinds):
            raise ValueError("selected kinds must be unique")
        if any(kind not in SOURCE_UNIT_KINDS for kind in self.selected_kinds):
            raise ValueError("frozen source window contains unsupported CRM kind")
        if tuple(kind for kind, _upper in self.upper_bounds) != self.selected_kinds:
            raise ValueError("frozen source window must contain every selected kind exactly once")
        for _kind, upper_id in self.upper_bounds:
            non_negative_int(upper_id, "upper_id")
        required_text(self.algorithm_version, "algorithm_version")

    def upper_id_for(self, kind: StandaloneCrmSourceUnitKind) -> int:
        for candidate, upper_id in self.upper_bounds:
            if candidate == kind:
                return upper_id
        raise ValueError("CRM kind is not selected in this frozen source window")


@dataclass(frozen=True)
class NoSourceWindow:
    """Immutable marker proving a mapping-only census has a complete zero-I/O window."""

    contract_version: str = "standalone-crm-no-source-window-v1"

    def __post_init__(self) -> None:
        required_text(self.contract_version, "contract_version")


@dataclass(frozen=True)
class StandaloneCrmAttempt:
    """One immutable generation claim with durable lease/fence authority."""

    census_id: str
    generation: int
    task_id: str
    state: StandaloneCrmAttemptState
    parent_fence_token: int
    deadline_at: datetime
    occurrence_deadline_at: datetime

    def __post_init__(self) -> None:
        required_text(self.census_id, "census_id")
        positive_int(self.generation, "generation")
        required_text(self.task_id, "task_id")
        positive_int(self.parent_fence_token, "parent_fence_token")
        if self.state not in {
            "queued",
            "running",
            "paused_with_checkpoint",
            "failed",
            "superseded",
            "completed",
        }:
            raise ValueError("attempt state is invalid")
        if utc_datetime(self.deadline_at, "deadline_at") > utc_datetime(
            self.occurrence_deadline_at, "occurrence_deadline_at"
        ):
            raise ValueError("attempt deadline cannot exceed occurrence deadline")


@dataclass(frozen=True)
class StandaloneCrmCheckpoint:
    """Fenced, monotonic source-unit checkpoint and durable row accounting."""

    census_id: str
    unit_kind: StandaloneCrmUnitKind
    upper_id: int | None
    last_committed_id: int | None
    company_binding_after_contact_id: int | None
    processed_count: int
    skipped_count: int
    failed_count: int
    no_work_count: int
    generation: int
    parent_fence_token: int
    child_fence_token: int
    child_task_id: str
    version: int = 1

    def __post_init__(self) -> None:
        required_text(self.census_id, "census_id")
        if self.unit_kind not in SOURCE_UNIT_KINDS | MAPPING_UNIT_KINDS:
            raise ValueError("checkpoint unit kind is invalid")
        if self.upper_id is not None:
            non_negative_int(self.upper_id, "upper_id")
        if self.last_committed_id is not None:
            positive_int(self.last_committed_id, "last_committed_id")
            if self.upper_id is None or self.last_committed_id > self.upper_id:
                raise ValueError("checkpoint cursor is outside frozen source window")
        if self.company_binding_after_contact_id is not None:
            positive_int(self.company_binding_after_contact_id, "company_binding_after_contact_id")
        for field in ("processed_count", "skipped_count", "failed_count", "no_work_count"):
            non_negative_int(getattr(self, field), field)
        positive_int(self.generation, "generation")
        positive_int(self.parent_fence_token, "parent_fence_token")
        positive_int(self.child_fence_token, "child_fence_token")
        required_text(self.child_task_id, "child_task_id")
        positive_int(self.version, "version")


@dataclass(frozen=True)
class StandaloneCrmChildEnvelope:
    """Parent-issued immutable child contract consumed by #274/#275 only."""

    census_id: str
    generation: int
    parent_fence_token: int
    unit_kind: StandaloneCrmUnitKind
    upper_id: int | None
    revision_id: str | None
    publication_id: str
    task_id: str
    payload_digest: str
    source_instance_id: str
    control_instance_id: str

    def __post_init__(self) -> None:
        required_text(self.census_id, "census_id")
        positive_int(self.generation, "generation")
        positive_int(self.parent_fence_token, "parent_fence_token")
        if self.unit_kind not in SOURCE_UNIT_KINDS | MAPPING_UNIT_KINDS:
            raise ValueError("child envelope unit kind is invalid")
        required_text(self.publication_id, "publication_id")
        required_text(self.task_id, "task_id")
        required_text(self.payload_digest, "payload_digest")
        required_text(self.source_instance_id, "source_instance_id")
        required_text(self.control_instance_id, "control_instance_id")
        if self.unit_kind in SOURCE_UNIT_KINDS:
            if self.upper_id is None or self.revision_id is not None:
                raise ValueError("source child envelope must carry only a frozen upper ID")
            non_negative_int(self.upper_id, "upper_id")
        elif self.revision_id is None or self.upper_id is not None:
            raise ValueError("mapping child envelope must carry only an exact revision ID")
        else:
            required_text(self.revision_id, "revision_id")


@dataclass(frozen=True)
class StandaloneCrmFreshness:
    """Persisted authority captured at admission and required by every mutation."""

    census_id: str
    fingerprint: str
    authority_digest: str
    source_instance_id: str
    control_instance_id: str

    def __post_init__(self) -> None:
        for field in (
            "census_id",
            "fingerprint",
            "authority_digest",
            "source_instance_id",
            "control_instance_id",
        ):
            required_text(getattr(self, field), field)


@dataclass(frozen=True)
class StandaloneCrmCallIntent:
    """One immutable physical-I/O authorization, never reusable after reservation."""

    census_id: str
    generation: int
    parent_fence_token: int
    freshness: StandaloneCrmFreshness
    intent_id: str
    sequence: int
    call_kind: StandaloneCrmCallKind
    unit_kind: StandaloneCrmSourceUnitKind | None
    retry_ordinal: int
    metadata_digest: str
    cursor_id: int | None = None
    subject_id: str | None = None
    upper_id: int | None = None

    def __post_init__(self) -> None:
        required_text(self.census_id, "census_id")
        if self.freshness.census_id != self.census_id:
            raise ValueError("call intent freshness census does not match intent")
        positive_int(self.generation, "generation")
        positive_int(self.parent_fence_token, "parent_fence_token")
        required_text(self.intent_id, "intent_id")
        positive_int(self.sequence, "sequence")
        if self.call_kind not in {"probe", "page", "company_binding"}:
            raise ValueError("call intent kind is invalid")
        if self.unit_kind is not None and self.unit_kind not in SOURCE_UNIT_KINDS:
            raise ValueError("call intent unit kind is invalid")
        non_negative_int(self.retry_ordinal, "retry_ordinal")
        required_text(self.metadata_digest, "metadata_digest")
        if self.cursor_id is not None:
            non_negative_int(self.cursor_id, "cursor_id")
        if self.subject_id is not None:
            required_text(self.subject_id, "subject_id")
        if self.upper_id is not None:
            non_negative_int(self.upper_id, "upper_id")


@dataclass(frozen=True)
class StandaloneCrmTerminalAccounting:
    """Exact durable totals derived by terminal reconciliation, never task supplied."""

    expected_units: int
    processed_units: int
    skipped_units: int
    failed_units: int
    no_work_units: int

    def __post_init__(self) -> None:
        for field in (
            "expected_units",
            "processed_units",
            "skipped_units",
            "failed_units",
            "no_work_units",
        ):
            non_negative_int(getattr(self, field), field)
        # Expected units are terminal child classifications; counters are independent
        # cumulative source-row totals and may validly be zero or exceed child count.


def attempt_deadlines(
    now: datetime, budget: StandaloneCrmBudgetSnapshot
) -> tuple[datetime, datetime]:
    """Derive UTC absolute deadlines without persisting process-local monotonic time."""
    current = utc_datetime(now, "now")
    return (
        current + timedelta(seconds=budget.max_runtime_seconds_per_attempt),
        current + timedelta(seconds=budget.max_wall_clock_seconds_per_occurrence),
    )


# Request/fingerprint classes are kept in a separate module so state models remain cohesive.
