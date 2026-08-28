"""Immutable windows, attempts, checkpoints, units, and terminal accounting."""

from __future__ import annotations

from dataclasses import dataclass

from src.standalone_crm_census_requests import StandaloneCrmCensusRequest
from src.standalone_crm_census_types import (
    _PARENT_STATES,
    _STREAM_KINDS,
    _TERMINAL_STATES,
    _UNIT_STATES,
    StandaloneCrmAttemptState,
    StandaloneCrmCheckpointDecision,
    StandaloneCrmParentState,
    StandaloneCrmReason,
    StandaloneCrmStreamKind,
    StandaloneCrmUnitState,
    _integer,
    _text,
    _utc,
)


@dataclass(frozen=True)
class StandaloneCrmCheckpointResult:
    decision: StandaloneCrmCheckpointDecision

    def __post_init__(self) -> None:
        if self.decision not in {
            "stored",
            "stale_or_conflict",
            "attempt_exhausted",
            "occurrence_exhausted",
        }:
            raise ValueError("invalid checkpoint decision")

    @property
    def stored(self) -> bool:
        return self.decision == "stored"


@dataclass(frozen=True)
class SourceWindow:
    selected_bounds: tuple[tuple[StandaloneCrmStreamKind, int], ...]
    window_version: str = "standalone-crm-source-window-v1"

    def __post_init__(self) -> None:
        values = tuple(sorted(self.selected_bounds))
        kinds = tuple(kind for kind, _bound in values)
        if (
            not values
            or len(kinds) != len(set(kinds))
            or any(kind not in _STREAM_KINDS for kind in kinds)
        ):
            raise ValueError("source window must contain unique selected kinds")
        for _kind, bound in values:
            _integer(bound, "source upper bound")
        object.__setattr__(self, "selected_bounds", values)
        object.__setattr__(self, "window_version", _text(self.window_version, "window_version"))

    def bound_for(self, kind: StandaloneCrmStreamKind) -> int | None:
        return next((bound for selected, bound in self.selected_bounds if selected == kind), None)


@dataclass(frozen=True)
class NoSourceWindow:
    revision_id: str
    revision_digest: str
    window_version: str = "standalone-crm-no-source-window-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision_id", _text(self.revision_id, "revision_id"))
        object.__setattr__(self, "revision_digest", _text(self.revision_digest, "revision_digest"))
        object.__setattr__(self, "window_version", _text(self.window_version, "window_version"))


@dataclass(frozen=True)
class StandaloneCrmAttempt:
    census_id: str
    generation: int
    fence_token: int
    state: StandaloneCrmAttemptState
    deadline: str
    task_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "census_id", _text(self.census_id, "census_id"))
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        _integer(self.generation, "generation", 1)
        _integer(self.fence_token, "fence_token", 1)
        if self.state not in {
            "queued",
            "running",
            "paused_with_checkpoint",
            "failed",
            "superseded",
            "completed",
        }:
            raise ValueError("invalid attempt state")
        object.__setattr__(self, "deadline", _utc(self.deadline, "attempt deadline"))


@dataclass(frozen=True)
class StandaloneCrmCheckpoint:
    census_id: str
    stream_kind: StandaloneCrmStreamKind
    frozen_upper_id: int | None
    revision_id: str | None
    last_committed_id: int
    binding_subject_id: int | None
    binding_offset: int | None
    processed_rows: int
    skipped_rows: int
    generation: int
    fence_token: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "census_id", _text(self.census_id, "census_id"))
        if self.stream_kind not in _STREAM_KINDS:
            raise ValueError("invalid checkpoint stream kind")
        if (self.frozen_upper_id is None) == (self.revision_id is None):
            raise ValueError("checkpoint requires exactly one source bound or revision")
        if self.frozen_upper_id is not None:
            _integer(self.frozen_upper_id, "frozen_upper_id")
            if self.last_committed_id > self.frozen_upper_id:
                raise ValueError("last_committed_id cannot exceed frozen_upper_id")
        if self.revision_id is not None:
            object.__setattr__(self, "revision_id", _text(self.revision_id, "revision_id"))
        for field in ("last_committed_id", "processed_rows", "skipped_rows"):
            _integer(getattr(self, field), field)
        for field in ("generation", "fence_token"):
            _integer(getattr(self, field), field, 1)
        if (self.binding_subject_id is None) != (self.binding_offset is None):
            raise ValueError("binding checkpoint requires both subject and offset")
        if self.binding_subject_id is not None:
            _integer(self.binding_subject_id, "binding_subject_id")
            if self.binding_offset is None:
                raise ValueError("binding offset is required")
            _integer(self.binding_offset, "binding_offset")
        if self.skipped_rows > self.processed_rows:
            raise ValueError("skipped_rows cannot exceed processed_rows")

    def can_advance_to(self, successor: StandaloneCrmCheckpoint) -> bool:
        if (
            self.census_id != successor.census_id
            or self.stream_kind != successor.stream_kind
            or self.frozen_upper_id != successor.frozen_upper_id
            or self.revision_id != successor.revision_id
            or successor.generation < self.generation
            or successor.fence_token < self.fence_token
        ):
            return False
        if (
            successor.last_committed_id < self.last_committed_id
            or successor.processed_rows < self.processed_rows
            or successor.skipped_rows < self.skipped_rows
        ):
            return False
        if self.binding_subject_id is None:
            return successor.binding_subject_id is None
        if successor.binding_subject_id is None or successor.binding_offset is None:
            return False
        return (successor.binding_subject_id, successor.binding_offset) >= (
            self.binding_subject_id,
            self.binding_offset or 0,
        )


@dataclass(frozen=True)
class StandaloneCrmContinuation:
    census_id: str
    prior_generation: int
    next_generation: int
    requested_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "census_id", _text(self.census_id, "census_id"))
        _integer(self.prior_generation, "prior_generation", 1)
        _integer(self.next_generation, "next_generation", 2)
        if self.next_generation != self.prior_generation + 1:
            raise ValueError("continuation generation must advance exactly one")
        object.__setattr__(self, "requested_at", _utc(self.requested_at, "requested_at"))


@dataclass(frozen=True)
class StandaloneCrmCensusUnit:
    census_id: str
    generation: int
    stream_kind: StandaloneCrmStreamKind
    state: StandaloneCrmUnitState
    frozen_upper_id: int | None
    revision_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "census_id", _text(self.census_id, "census_id"))
        _integer(self.generation, "generation", 1)
        if self.stream_kind not in _STREAM_KINDS:
            raise ValueError("invalid unit stream kind")
        if self.state not in _UNIT_STATES:
            raise ValueError("invalid unit state")
        if (self.frozen_upper_id is None) == (self.revision_id is None):
            raise ValueError("unit requires exactly one frozen bound or revision")
        if self.frozen_upper_id is not None:
            _integer(self.frozen_upper_id, "frozen_upper_id")
        if self.revision_id is not None:
            object.__setattr__(self, "revision_id", _text(self.revision_id, "revision_id"))


@dataclass(frozen=True)
class StandaloneCrmTerminalAccounting:
    expected_units: int
    processed_rows: int
    skipped_rows: int
    failed_rows: int
    no_work_units: int
    completed_units: int = 0
    failed_units: int = 0
    cancelled_units: int = 0
    unresolved_publications: int = 0
    active_fences: int = 0

    def __post_init__(self) -> None:
        for field in (
            "expected_units",
            "processed_rows",
            "skipped_rows",
            "failed_rows",
            "no_work_units",
            "completed_units",
            "failed_units",
            "cancelled_units",
            "unresolved_publications",
            "active_fences",
        ):
            _integer(getattr(self, field), field)
        if self.skipped_rows > self.processed_rows:
            raise ValueError("skipped_rows cannot exceed processed_rows")
        if self.settled_units > self.expected_units:
            raise ValueError("settled units cannot exceed expected units")

    @property
    def settled_units(self) -> int:
        return self.no_work_units + self.completed_units + self.failed_units + self.cancelled_units

    def can_terminalize(self, state: StandaloneCrmParentState) -> bool:
        return (
            state in _TERMINAL_STATES
            and self.settled_units == self.expected_units
            and self.unresolved_publications == 0
            and self.active_fences == 0
        )


@dataclass(frozen=True)
class StandaloneCrmCensus:
    census_id: str
    request: StandaloneCrmCensusRequest
    state: StandaloneCrmParentState
    created_at: str
    terminal_reason: StandaloneCrmReason | None = None
    terminal_accounting: StandaloneCrmTerminalAccounting | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "census_id", _text(self.census_id, "census_id"))
        if self.state not in _PARENT_STATES:
            raise ValueError("invalid census parent state")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.state in _TERMINAL_STATES:
            if self.terminal_reason is None or self.terminal_accounting is None:
                raise ValueError("terminal census requires reason and accounting")
            if not self.terminal_accounting.can_terminalize(self.state):
                raise ValueError("terminal census accounting is not settled")
        elif self.terminal_reason is not None or self.terminal_accounting is not None:
            raise ValueError("non-terminal census cannot carry terminal data")
