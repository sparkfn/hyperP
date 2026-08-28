"""Shared strict literals, validation, and errors for standalone CRM census contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

type StandaloneCrmCensusKind = Literal["source_sync", "mapping_prepare", "mapping_rollback"]
type StandaloneCrmStreamKind = Literal["contact", "lead", "company"]
type StandaloneCrmParentState = Literal[
    "allocated",
    "freezing",
    "frozen",
    "publishing",
    "running",
    "paused_with_checkpoint",
    "cancel_requested",
    "recovering",
    "completed",
    "failed",
    "cancelled_with_checkpoint",
    "freeze_failed",
]
type StandaloneCrmAttemptState = Literal[
    "queued", "running", "paused_with_checkpoint", "failed", "superseded", "completed"
]
type StandaloneCrmUnitState = Literal[
    "pending_publication",
    "publishing",
    "queued",
    "running",
    "paused",
    "completed",
    "failed",
    "cancelled",
    "superseded",
    "no_work",
]
type StandaloneCrmPublicationState = Literal["pending", "publishing", "published", "retired"]
type StandaloneCrmCallKind = Literal["probe", "page", "company_binding"]
type StandaloneCrmCallOutcomeState = Literal["reserved", "succeeded", "failed", "unknown"]
type StandaloneCrmCheckpointDecision = Literal[
    "stored", "stale_or_conflict", "attempt_exhausted", "occurrence_exhausted"
]
type StandaloneCrmTerminalState = Literal[
    "completed", "failed", "cancelled_with_checkpoint", "freeze_failed"
]
type StandaloneCrmReasonCode = Literal[
    "completed",
    "authority_unavailable",
    "authority_stale",
    "budget_exhausted",
    "cancelled",
    "child_handler_unavailable",
    "freeze_incomplete",
    "publication_unsettled",
    "reservation_unknown",
    "stale_fence",
    "deadline_elapsed",
    "invalid_checkpoint",
    "source_disabled",
    "call_failed",
    "call_unknown",
    "handler_missing",
    "publication_failed",
    "recovery_required",
    "attempt_budget_exhausted",
    "occurrence_budget_exhausted",
    "deadline_exhausted",
    "freeze_failed",
    "publication_conflict",
    "fence_unsettled",
]

_CENSUS_KINDS = frozenset({"source_sync", "mapping_prepare", "mapping_rollback"})
_STREAM_KINDS = frozenset({"contact", "lead", "company"})
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled_with_checkpoint", "freeze_failed"})
_PARENT_STATES = frozenset(
    {
        "allocated",
        "freezing",
        "frozen",
        "publishing",
        "running",
        "paused_with_checkpoint",
        "cancel_requested",
        "recovering",
        "completed",
        "failed",
        "cancelled_with_checkpoint",
        "freeze_failed",
    }
)
_UNIT_STATES = frozenset(
    {
        "pending_publication",
        "publishing",
        "queued",
        "running",
        "paused",
        "completed",
        "failed",
        "cancelled",
        "superseded",
        "no_work",
    }
)
_REASON_CODES = frozenset(
    {
        "completed",
        "authority_unavailable",
        "authority_stale",
        "budget_exhausted",
        "cancelled",
        "child_handler_unavailable",
        "freeze_incomplete",
        "publication_unsettled",
        "reservation_unknown",
        "stale_fence",
        "deadline_elapsed",
        "invalid_checkpoint",
        "source_disabled",
        "call_failed",
        "call_unknown",
        "handler_missing",
        "publication_failed",
        "recovery_required",
        "attempt_budget_exhausted",
        "occurrence_budget_exhausted",
        "deadline_exhausted",
        "freeze_failed",
        "publication_conflict",
        "fence_unsettled",
    }
)


class StandaloneCrmCensusError(RuntimeError):
    pass


class StandaloneCrmCensusConflictError(StandaloneCrmCensusError):
    pass


class StandaloneCrmCensusAuthorityError(StandaloneCrmCensusError):
    pass


class StandaloneCrmCensusReservationError(StandaloneCrmCensusError):
    pass


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value.strip()


def _integer(value: int, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _utc(value: str, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class StandaloneCrmReason:
    code: StandaloneCrmReasonCode
    detail: str

    def __post_init__(self) -> None:
        if self.code not in _REASON_CODES:
            raise ValueError("invalid census reason code")
        object.__setattr__(self, "detail", _text(self.detail, "reason detail"))


def is_terminal_state(state: str) -> bool:
    return state in _TERMINAL_STATES
