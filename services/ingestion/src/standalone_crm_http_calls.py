"""Typed durable pre-I/O reservation adapters for standalone CRM census calls."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Literal, Protocol

from src.standalone_crm_census_models import (
    StandaloneCrmAttempt,
    StandaloneCrmCallIntent,
    StandaloneCrmCallKind,
    StandaloneCrmCallOutcome,
    StandaloneCrmFreshness,
    StandaloneCrmSourceUnitKind,
)

BitrixHttpOutcome = Literal["succeeded", "failed"]


@dataclass(frozen=True)
class BitrixHttpCallContext:
    """Sanitized census metadata for exactly one physical Bitrix call class."""

    call_kind: StandaloneCrmCallKind
    unit_kind: StandaloneCrmSourceUnitKind | None
    cursor_id: int | None = None
    subject_id: str | None = None
    upper_id: int | None = None

    def __post_init__(self) -> None:
        if self.call_kind not in {"probe", "page", "company_binding"}:
            raise ValueError("Bitrix census call kind is invalid")
        if self.unit_kind not in {None, "contact", "lead", "company"}:
            raise ValueError("Bitrix census unit kind is invalid")
        for field_name in ("cursor_id", "upper_id"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.subject_id is not None and (not self.subject_id or len(self.subject_id) > 200):
            raise ValueError("subject_id must be a bounded non-empty value")


@dataclass(frozen=True)
class BitrixHttpAttempt:
    """One unique physical attempt; no URLs, request body, or source values are retained."""

    method: str
    retry_ordinal: int
    context: BitrixHttpCallContext | None = None
    physical_attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if not self.method or len(self.method) > 100:
            raise ValueError("Bitrix HTTP method must be a bounded non-empty value")
        if self.retry_ordinal < 0:
            raise ValueError("Bitrix retry ordinal must be non-negative")
        if not self.physical_attempt_id or len(self.physical_attempt_id) > 100:
            raise ValueError("Bitrix physical attempt ID must be a bounded non-empty value")


class BitrixHttpReservationHook(Protocol):
    """Durable authorization adapter. ``reserve`` false means zero network I/O."""

    def reserve(self, attempt: BitrixHttpAttempt) -> bool: ...

    def record_outcome(
        self,
        attempt: BitrixHttpAttempt,
        outcome: BitrixHttpOutcome,
        *,
        numeric_result: int | None = None,
    ) -> None: ...


class CensusCallRepository(Protocol):
    """Repository subset used by the production adapter; all calls are durable mutations."""

    def reserve_call(
        self,
        *,
        intent: StandaloneCrmCallIntent,
        budget_calls_per_attempt: int,
        budget_calls_per_occurrence: int,
    ) -> bool: ...

    def record_call_outcome(
        self,
        intent: StandaloneCrmCallIntent,
        outcome: StandaloneCrmCallOutcome,
        *,
        numeric_result: int | None = None,
        result_digest: str = "",
    ) -> bool: ...


class StandaloneCrmHttpReservationAdapter:
    """Binds every client retry to a distinct immutable census call reservation."""

    def __init__(
        self,
        *,
        repository: CensusCallRepository,
        attempt: StandaloneCrmAttempt,
        freshness: StandaloneCrmFreshness,
        max_calls_per_attempt: int,
        max_calls_per_occurrence: int,
    ) -> None:
        self._repository = repository
        self._attempt = attempt
        self._freshness = freshness
        self._max_calls_per_attempt = max_calls_per_attempt
        self._max_calls_per_occurrence = max_calls_per_occurrence
        self._intents: dict[str, StandaloneCrmCallIntent] = {}
        self._next_sequence = 1

    def reserve(self, attempt: BitrixHttpAttempt) -> bool:
        """Commit a unique intent before the matching physical ``http.post``."""
        context = attempt.context
        if context is None or context.unit_kind is None:
            raise RuntimeError(
                "standalone CRM census reservation requires typed source call metadata"
            )
        key = attempt.physical_attempt_id
        if key in self._intents:
            return False
        intent = StandaloneCrmCallIntent(
            census_id=self._attempt.census_id,
            generation=self._attempt.generation,
            parent_fence_token=self._attempt.parent_fence_token,
            freshness=self._freshness,
            intent_id=uuid.uuid4().hex,
            sequence=self._next_sequence,
            call_kind=context.call_kind,
            unit_kind=context.unit_kind,
            retry_ordinal=attempt.retry_ordinal,
            metadata_digest=_metadata_digest(attempt.method, context),
            cursor_id=context.cursor_id,
            subject_id=context.subject_id,
            upper_id=context.upper_id,
        )
        reserved = self._repository.reserve_call(
            intent=intent,
            budget_calls_per_attempt=self._max_calls_per_attempt,
            budget_calls_per_occurrence=self._max_calls_per_occurrence,
        )
        if reserved:
            self._intents[key] = intent
            self._next_sequence += 1
        return reserved

    def record_outcome(
        self,
        attempt: BitrixHttpAttempt,
        outcome: BitrixHttpOutcome,
        *,
        numeric_result: int | None = None,
    ) -> None:
        """Persist a completed-I/O outcome; persistence failure leaves the reservation consumed."""
        intent = self._intents.get(attempt.physical_attempt_id)
        if intent is None:
            raise RuntimeError("Bitrix outcome has no durable census reservation")
        durable_outcome: StandaloneCrmCallOutcome = outcome
        result_digest = _result_digest(numeric_result) if numeric_result is not None else ""
        if not self._repository.record_call_outcome(
            intent,
            durable_outcome,
            numeric_result=numeric_result,
            result_digest=result_digest,
        ):
            raise RuntimeError("Bitrix census outcome persistence was rejected")


def _metadata_digest(method: str, context: BitrixHttpCallContext) -> str:
    payload = {
        "method": method,
        "call_kind": context.call_kind,
        "unit_kind": context.unit_kind,
        "cursor_id": context.cursor_id,
        "subject_id": context.subject_id,
        "upper_id": context.upper_id,
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _result_digest(numeric_result: int) -> str:
    return "sha256:" + hashlib.sha256(f"probe:{numeric_result}".encode("ascii")).hexdigest()
