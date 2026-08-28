"""Immutable HTTP reservation and child-publication contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from src.standalone_crm_census_types import (
    _STREAM_KINDS,
    StandaloneCrmCallKind,
    StandaloneCrmCallOutcomeState,
    StandaloneCrmPublicationState,
    StandaloneCrmStreamKind,
    _integer,
    _text,
    _utc,
)


@dataclass(frozen=True)
class StandaloneCrmCallIntent:
    census_id: str
    generation: int
    intent_id: str
    sequence: int
    call_kind: StandaloneCrmCallKind
    stream_kind: StandaloneCrmStreamKind | None
    retry_ordinal: int
    deadline: str
    cursor: int | None = None
    subject_id: int | None = None
    task_id: str | None = None
    effective_deadline: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "census_id", _text(self.census_id, "census_id"))
        object.__setattr__(self, "intent_id", _text(self.intent_id, "intent_id"))
        _integer(self.generation, "generation", 1)
        _integer(self.sequence, "sequence", 1)
        _integer(self.retry_ordinal, "retry_ordinal")
        if self.call_kind not in {"probe", "page", "company_binding"}:
            raise ValueError("invalid call kind")
        if self.stream_kind is not None and self.stream_kind not in _STREAM_KINDS:
            raise ValueError("invalid call stream kind")
        if self.cursor is not None:
            _integer(self.cursor, "cursor")
        if self.subject_id is not None:
            _integer(self.subject_id, "subject_id", 1)
        if self.task_id is not None:
            object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        if self.call_kind == "probe":
            if self.stream_kind is None or self.cursor is not None or self.subject_id is not None:
                raise ValueError("probe intent requires a stream kind and no cursor or subject")
        elif self.call_kind == "page":
            if self.stream_kind is None or self.cursor is None or self.subject_id is not None:
                raise ValueError("page intent requires a stream kind and cursor only")
        elif self.stream_kind != "contact" or self.cursor is None or self.subject_id is None:
            raise ValueError("company_binding requires contact stream, cursor, and subject")
        if self.call_kind != "probe" and self.task_id is None:
            raise ValueError("non-probe intent requires a published child task_id")
        object.__setattr__(self, "deadline", _utc(self.deadline, "call deadline"))
        effective = self.deadline if self.effective_deadline is None else self.effective_deadline
        object.__setattr__(self, "effective_deadline", _utc(effective, "effective call deadline"))


@dataclass(frozen=True)
class StandaloneCrmCallOutcome:
    intent_id: str
    call_kind: StandaloneCrmCallKind
    state: StandaloneCrmCallOutcomeState
    observed_at: str
    upper_id: int | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _text(self.intent_id, "intent_id"))
        if self.call_kind not in {"probe", "page", "company_binding"}:
            raise ValueError("invalid call outcome kind")
        if self.state not in {"reserved", "succeeded", "failed", "unknown"}:
            raise ValueError("invalid call outcome state")
        if self.upper_id is not None:
            _integer(self.upper_id, "upper_id")
        if self.error_code is not None:
            object.__setattr__(self, "error_code", _text(self.error_code, "error_code"))
        if self.state == "reserved" and (self.upper_id is not None or self.error_code is not None):
            raise ValueError("reserved outcome cannot carry an upper id or error")
        if self.state == "succeeded":
            if self.error_code is not None:
                raise ValueError("successful outcome cannot carry an error")
            if self.call_kind == "probe" and self.upper_id is None:
                raise ValueError("successful probe requires an upper_id")
        elif self.upper_id is not None or self.error_code is None:
            raise ValueError("failed or unknown outcome requires error and no upper id")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))


@dataclass(frozen=True)
class StandaloneCrmChildEnvelope:
    census_id: str
    generation: int
    stream_kind: StandaloneCrmStreamKind
    frozen_upper_id: int | None
    revision_id: str | None
    task_name: str
    task_id: str
    queue: str
    payload_version: str = "standalone-crm-child-v1"

    def __post_init__(self) -> None:
        for field in ("census_id", "task_name", "task_id", "queue", "payload_version"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        _integer(self.generation, "generation", 1)
        if self.stream_kind not in _STREAM_KINDS:
            raise ValueError("invalid envelope stream kind")
        if (self.frozen_upper_id is None) == (self.revision_id is None):
            raise ValueError("envelope requires exactly one frozen bound or revision")
        if self.frozen_upper_id is not None:
            _integer(self.frozen_upper_id, "frozen_upper_id")
        if self.revision_id is not None:
            object.__setattr__(self, "revision_id", _text(self.revision_id, "revision_id"))

    def payload_digest(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class StandaloneCrmPublication:
    census_id: str
    generation: int
    stream_kind: StandaloneCrmStreamKind
    task_id: str
    payload_digest: str
    state: StandaloneCrmPublicationState

    def __post_init__(self) -> None:
        object.__setattr__(self, "census_id", _text(self.census_id, "census_id"))
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(self, "payload_digest", _text(self.payload_digest, "payload_digest"))
        _integer(self.generation, "generation", 1)
        if self.stream_kind not in _STREAM_KINDS or self.state not in {
            "pending",
            "publishing",
            "published",
            "retired",
        }:
            raise ValueError("invalid publication state")
