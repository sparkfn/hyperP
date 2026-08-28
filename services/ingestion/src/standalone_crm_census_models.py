"""Strict, immutable models for bounded standalone Bitrix CRM censuses."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal

StandaloneCrmKind = Literal["contact", "lead", "company"]
CRM_KINDS: Final[frozenset[str]] = frozenset({"contact", "lead", "company"})
BITRIX_SOURCE_KEY: Final[str] = "bitrix_chat"


class CensusKind(StrEnum):
    """Disjoint census request domains."""

    SOURCE_SYNC = "source_sync"
    MAPPING_PREPARE = "mapping_prepare"
    MAPPING_ROLLBACK = "mapping_rollback"


class ParentState(StrEnum):
    """Parent states; terminal values are an exhaustive closed set."""

    ALLOCATED = "allocated"
    FREEZING = "freezing"
    FROZEN = "frozen"
    PUBLISHING = "publishing"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED_WITH_CHECKPOINT = "paused_with_checkpoint"
    CONTINUING = "continuing"
    CANCEL_REQUESTED = "cancel_requested"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED_WITH_CHECKPOINT = "cancelled_with_checkpoint"
    FREEZE_FAILED = "freeze_failed"


TERMINAL_PARENT_STATES: Final[frozenset[ParentState]] = frozenset(
    {
        ParentState.COMPLETED,
        ParentState.FAILED,
        ParentState.CANCELLED_WITH_CHECKPOINT,
        ParentState.FREEZE_FAILED,
    }
)


class AttemptState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED_WITH_CHECKPOINT = "paused_with_checkpoint"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    COMPLETED = "completed"


class ChildState(StrEnum):
    PENDING_PUBLICATION = "pending_publication"
    PUBLISHING = "publishing"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class PublicationState(StrEnum):
    RESERVED = "reserved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class HttpCallKind(StrEnum):
    PROBE = "probe"
    PAGE = "page"
    COMPANY_BINDING = "company_binding"


class HttpCallState(StrEnum):
    RESERVED = "reserved"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class CensusError(RuntimeError):
    """Base class for census control-plane errors."""


class CensusAdmissionError(CensusError):
    """Census admission was rejected before any work was authorized."""


class CensusConflictError(CensusAdmissionError):
    """An immutable occurrence or active ownership conflict was detected."""


class StaleCensusError(CensusError):
    """An attempt, fence, authority, or immutable snapshot is stale."""


class CensusBudgetError(CensusError):
    """A durable budget, deadline, or attempt-count limit was exhausted."""


class CensusPublicationError(CensusError):
    """A child publication is missing, ambiguous, or immutable-conflicting."""


class MappingAuthorityUnavailableError(CensusError):
    """No authoritative mapping revision/projection head is available."""


class MissingCensusChildHandlerError(CensusError):
    """A legitimate child is waiting for a future handler implementation."""


@dataclass(frozen=True)
class CensusBudgets:
    """Immutable attempt and occurrence limits for one census occurrence."""

    attempt_calls: int
    attempt_rows: int
    attempt_runtime_seconds: float
    occurrence_calls: int
    occurrence_rows: int
    occurrence_wall_clock_seconds: float
    max_attempts: int

    def __post_init__(self) -> None:
        positive_ints = (
            self.attempt_calls,
            self.attempt_rows,
            self.occurrence_calls,
            self.occurrence_rows,
            self.max_attempts,
        )
        if any(isinstance(value, bool) or value < 1 for value in positive_ints):
            raise ValueError("census call, row, and attempt ceilings must be positive")
        positive_floats = (
            self.attempt_runtime_seconds,
            self.occurrence_wall_clock_seconds,
        )
        if any(not isinstance(value, (int, float)) or value <= 0 for value in positive_floats):
            raise ValueError("census runtime ceilings must be positive")
        if self.attempt_calls > self.occurrence_calls:
            raise ValueError("attempt call ceiling exceeds the occurrence ceiling")
        if self.attempt_rows > self.occurrence_rows:
            raise ValueError("attempt row ceiling exceeds the occurrence row ceiling")
        if self.attempt_runtime_seconds > self.occurrence_wall_clock_seconds:
            raise ValueError("attempt runtime ceiling exceeds occurrence wall clock")


CensusBudget = CensusBudgets


@dataclass(frozen=True)
class CensusIdentity:
    """Stable identities and non-secret operator context."""

    source_key: str = BITRIX_SOURCE_KEY
    source_instance_id: str = ""
    control_instance_id: str = ""
    census_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    occurrence_key: str = ""
    operator: str = ""

    def __post_init__(self) -> None:
        if self.source_instance_id.strip() == "":
            raise ValueError("source_instance_id is required")
        if self.control_instance_id.strip() == "":
            raise ValueError("control_instance_id is required")
        if self.occurrence_key.strip() == "":
            raise ValueError("occurrence_key is required")
        if len(self.operator) > 200:
            raise ValueError("operator must be at most 200 characters")


@dataclass(frozen=True)
class SourceWindow:
    """Immutable complete selected numeric upper-bound window."""

    bounds: dict[StandaloneCrmKind, int]

    def __post_init__(self) -> None:
        if not self.bounds:
            raise ValueError("source_sync must freeze at least one selected kind")
        for kind, bound in self.bounds.items():
            if kind not in CRM_KINDS:
                raise ValueError(f"unknown CRM kind {kind}")
            if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
                raise ValueError(f"{kind} upper bound must be a non-negative integer")

    @property
    def selected_kinds(self) -> tuple[StandaloneCrmKind, ...]:
        return tuple(sorted(self.bounds))


@dataclass(frozen=True)
class NoSourceWindow:
    """Complete marker proving mapping-only work made zero source calls."""

    contract_version: str


@dataclass(frozen=True)
class SourceSyncCensusRequest:
    """Strict source-sync request; heads and bounds are runtime outputs."""

    selected_kinds: tuple[StandaloneCrmKind, ...]
    policy_version: str = "standalone-crm-census-v1"
    freeze_contract_version: str = "source-window-freeze-v1"
    association_contract_version: str = "crm-company-membership-snapshot-v1"
    configuration_digest: str = ""

    def __post_init__(self) -> None:
        if not self.selected_kinds or len(set(self.selected_kinds)) != len(self.selected_kinds):
            raise ValueError("source_sync must select one or more unique CRM kinds")
        if any(kind not in CRM_KINDS for kind in self.selected_kinds):
            raise ValueError("source_sync selected kind is invalid")
        if not all(
            (self.policy_version, self.freeze_contract_version, self.association_contract_version)
        ):
            raise ValueError("source_sync contract versions are required")
        if not self.configuration_digest:
            raise ValueError("source_sync configuration_digest is required")


@dataclass(frozen=True)
class MappingPrepareCensusRequest:
    """Strict no-source mapping preparation request."""

    prepared_revision_id: str
    prepared_revision_digest: str
    expected_current_head: str
    policy_version: str = "standalone-crm-census-v1"
    no_source_call_contract_version: str = "mapping-no-source-calls-v1"

    def __post_init__(self) -> None:
        if not all(
            (
                self.prepared_revision_id,
                self.prepared_revision_digest,
                self.expected_current_head,
                self.policy_version,
                self.no_source_call_contract_version,
            )
        ):
            raise ValueError("mapping_prepare revision and contract fields are required")


@dataclass(frozen=True)
class MappingRollbackCensusRequest:
    """Strict no-source mapping rollback request."""

    target_revision_id: str
    target_revision_digest: str
    expected_current_head: str
    rollback_head: str
    policy_version: str = "standalone-crm-census-v1"
    no_source_call_contract_version: str = "mapping-no-source-calls-v1"

    def __post_init__(self) -> None:
        if not all(
            (
                self.target_revision_id,
                self.target_revision_digest,
                self.expected_current_head,
                self.rollback_head,
                self.policy_version,
                self.no_source_call_contract_version,
            )
        ):
            raise ValueError("mapping_rollback revision and contract fields are required")


CensusRequest = SourceSyncCensusRequest | MappingPrepareCensusRequest | MappingRollbackCensusRequest


@dataclass(frozen=True)
class AuthorityHeads:
    """Authority values captured before source window freezing."""

    mapping_head: str = ""
    projection_head: str = ""
    prepared_revision_id: str = ""
    prepared_revision_digest: str = ""
    rollback_head: str = ""

    def validate(self, kind: CensusKind) -> None:
        if kind is CensusKind.SOURCE_SYNC and (not self.mapping_head or not self.projection_head):
            raise ValueError("source_sync requires mapping and projection heads")
        if kind is CensusKind.MAPPING_PREPARE and not (
            self.prepared_revision_id and self.prepared_revision_digest
        ):
            raise ValueError("mapping_prepare requires an exact prepared revision")
        if kind is CensusKind.MAPPING_ROLLBACK and not (
            self.prepared_revision_id and self.prepared_revision_digest and self.rollback_head
        ):
            raise ValueError("mapping_rollback requires exact target and rollback heads")


@dataclass(frozen=True)
class CensusAttempt:
    """One immutable execution generation."""

    census_id: str
    generation: int
    state: AttemptState
    fence_token: str
    lease_until: datetime
    started_at: datetime
    ended_at: datetime | None = None
    calls_used: int = 0
    rows_processed: int = 0

    def __post_init__(self) -> None:
        if self.generation < 1 or self.calls_used < 0 or self.rows_processed < 0:
            raise ValueError("census attempt counters and generation must be non-negative")
        if self.lease_until.tzinfo is None:
            raise ValueError("census attempt lease deadline must include UTC offset")
        if self.ended_at is not None and self.ended_at.tzinfo is None:
            raise ValueError("census attempt end timestamp must include UTC offset")


@dataclass(frozen=True)
class CensusCheckpoint:
    """Monotonic child position and accounting used to resume safely."""

    census_id: str
    unit_kind: StandaloneCrmKind
    last_id: int
    rows_processed: int = 0
    binding_position: int = 0
    checkpoint_version: int = 1

    def __post_init__(self) -> None:
        if self.last_id < 0 or self.rows_processed < 0 or self.binding_position < 0:
            raise ValueError("census checkpoint values must be non-negative")


@dataclass(frozen=True)
class CensusChildUnit:
    """One frozen source or mapping child allocated by the parent."""

    census_id: str
    unit_kind: StandaloneCrmKind
    frozen_upper_id: int
    revision_id: str
    state: ChildState
    fence_token: str
    fence_generation: int
    expected_rows: int
    processed_rows: int = 0
    skipped_rows: int = 0
    failed_rows: int = 0

    def __post_init__(self) -> None:
        values = (
            self.frozen_upper_id,
            self.fence_generation,
            self.expected_rows,
            self.processed_rows,
            self.skipped_rows,
            self.failed_rows,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values
        ):
            raise ValueError("census child accounting must be non-negative")


@dataclass(frozen=True)
class CensusPublication:
    """Durable outbox identity created before any broker I/O."""

    census_id: str
    generation: int
    unit_kind: StandaloneCrmKind
    publication_sequence: int
    task_name: str
    task_id: str
    queue: str
    payload_version: str
    payload_digest: str
    payload_json: str
    state: PublicationState

    def __post_init__(self) -> None:
        if not all((self.task_name, self.task_id, self.queue, self.payload_digest)):
            raise ValueError("census publication requires a complete task identity")


@dataclass(frozen=True)
class HttpCallIntent:
    """One immutable intent authorizing exactly one network attempt."""

    census_id: str
    generation: int
    call_kind: HttpCallKind
    unit_kind: StandaloneCrmKind
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    call_kind_metadata: str = ""
    frozen_upper_id: int | None = None
    cursor: int | None = None
    retry_ordinal: int = 1

    def __post_init__(self) -> None:
        if self.retry_ordinal < 1:
            raise ValueError("HTTP retry ordinal must be positive")
        if self.generation < 1:
            raise ValueError("HTTP intent generation must be positive")


@dataclass(frozen=True)
class HttpCallReservation:
    """A durably consumed reservation; it can never authorize another call."""

    intent: HttpCallIntent
    sequence: int
    state: HttpCallState
    reserved_at: datetime
    outcome_recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("HTTP reservation sequence must be positive")
        if self.reserved_at.tzinfo is None:
            raise ValueError("HTTP reservation timestamp must include UTC offset")


def _canonical_payload(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def census_fingerprint(
    kind: CensusKind,
    identity: CensusIdentity,
    request: CensusRequest,
    budget: CensusBudget,
    heads: AuthorityHeads,
) -> str:
    """Build a versioned, domain-separated immutable census fingerprint."""
    identity_payload = asdict(identity)
    identity_payload["census_id"] = ""
    identity_payload["operator"] = ""
    heads.validate(kind)
    payload: dict[str, object] = {
        "fingerprint_version": 1,
        "kind": kind.value,
        "identity": identity_payload,
        "budget": asdict(budget),
    }
    if kind is CensusKind.SOURCE_SYNC:
        if not isinstance(request, SourceSyncCensusRequest):
            raise CensusConflictError("source_sync request kind mismatch")
        payload.update(
            {
                "domain": "source_sync_v1",
                "request": asdict(request),
                "mapping_head": heads.mapping_head,
                "projection_head": heads.projection_head,
            }
        )
    elif kind is CensusKind.MAPPING_PREPARE:
        if not isinstance(request, MappingPrepareCensusRequest):
            raise CensusConflictError("mapping_prepare request kind mismatch")
        payload.update({"domain": "mapping_prepare_v1", "request": asdict(request)})
    else:
        if not isinstance(request, MappingRollbackCensusRequest):
            raise CensusConflictError("mapping_rollback request kind mismatch")
        payload.update({"domain": "mapping_rollback_v1", "request": asdict(request)})
    return hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TerminalAccounting:
    """Derived, durable totals required before a terminal CAS."""

    total_calls: int
    total_rows_processed: int
    total_rows_skipped: int
    total_rows_failed: int
    no_work_units: int

    def __post_init__(self) -> None:
        values = (
            self.total_calls,
            self.total_rows_processed,
            self.total_rows_skipped,
            self.total_rows_failed,
            self.no_work_units,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values
        ):
            raise ValueError("terminal accounting values must be non-negative")
