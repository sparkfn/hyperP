"""Admission, parent-attempt, and source-call operations for CRM census state."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from neo4j import ManagedTransaction, Record

from src.graph.queries import standalone_crm_census as queries
from src.graph.standalone_crm_census_types import (
    StandaloneCrmCensusAdmission,
    StandaloneCrmCensusConflictError,
    StandaloneCrmCensusStaleError,
)
from src.standalone_crm_census_models import (
    StandaloneCrmAttempt,
    StandaloneCrmCallIntent,
    StandaloneCrmCallOutcome,
    StandaloneCrmFreshness,
)
from src.standalone_crm_census_requests import (
    SourceSyncAuthoritySnapshot,
    StandaloneCrmCensusRequest,
    admitted_request_fingerprint,
    admitted_request_json,
)

if TYPE_CHECKING:
    from src.graph.client import Neo4jClient


class StandaloneCrmCensusCoreOperations:
    """Mixin for immutable admission, attempt ownership, and physical I/O accounting."""

    _client: Neo4jClient

    def _require_mutation(self, query: str, params: dict[str, object], message: str) -> Record:
        raise NotImplementedError

    def admit(
        self,
        request: StandaloneCrmCensusRequest,
        *,
        authority: SourceSyncAuthoritySnapshot | None,
    ) -> StandaloneCrmCensusAdmission:
        fingerprint = admitted_request_fingerprint(request, authority)
        authority_json = census_json(authority)
        authority_digest = census_digest(authority_json)

        def work(tx: ManagedTransaction) -> StandaloneCrmCensusAdmission:
            record = tx.run(
                queries.ADMIT_CENSUS,
                census_id=uuid.uuid4().hex,
                source_key=request.source_key,
                source_instance_id=request.source_instance_id,
                control_instance_id=request.control_instance_id,
                census_kind=request.census_kind,
                occurrence_key=request.occurrence_key,
                fingerprint=fingerprint,
                request_json=admitted_request_json(request),
                budget_json=census_json(request.budget),
                authority_json=authority_json,
                authority_digest=authority_digest,
            ).single()
            if record is None:
                raise StandaloneCrmCensusConflictError(
                    "census occurrence conflicts or active scope is owned"
                )
            return StandaloneCrmCensusAdmission(
                record_text(record, "census_id"),
                record_text(record, "state"),
                record_text(record, "fingerprint"),
                record_text(record, "authority_digest"),
                request.source_instance_id,
                request.control_instance_id,
                record_bool(record, "created"),
            )

        return self._client.execute_write(work)

    def claim_attempt(
        self,
        admission: StandaloneCrmCensusAdmission,
        request: StandaloneCrmCensusRequest,
        *,
        task_id: str,
        lease_seconds: int = 300,
    ) -> StandaloneCrmAttempt:
        if not task_id.strip() or lease_seconds < 1:
            raise ValueError("task_id and lease_seconds must be positive")
        effective_lease_seconds = max(
            lease_seconds, math.ceil(request.budget.max_runtime_seconds_per_attempt)
        )
        params = freshness_guard(admission) | {
            "fingerprint": admission.fingerprint,
            "task_id": task_id,
            "lease_seconds": effective_lease_seconds,
            "max_attempts": request.budget.max_attempts_per_occurrence,
            "attempt_runtime_seconds": request.budget.max_runtime_seconds_per_attempt,
            "occurrence_runtime_seconds": request.budget.max_wall_clock_seconds_per_occurrence,
        }

        def work(tx: ManagedTransaction) -> StandaloneCrmAttempt:
            record = tx.run(queries.CLAIM_ATTEMPT, **params).single()  # type: ignore[arg-type]
            if record is None:
                self._fail_if_exhausted(admission, request, "attempt_claim_budget_exhausted")
                raise StandaloneCrmCensusStaleError("attempt claim rejected")
            return attempt_from_record(record)

        return self._client.execute_write(work)

    def recover_expired_attempt(
        self, admission: StandaloneCrmCensusAdmission, attempt: StandaloneCrmAttempt
    ) -> None:
        self._require_mutation(
            queries.RECOVER_ATTEMPT,
            freshness_guard(admission)
            | {
                "fingerprint": admission.fingerprint,
                "generation": attempt.generation,
                "parent_fence_token": attempt.parent_fence_token,
            },
            "expired attempt recovery rejected",
        )

    def reserve_call(
        self,
        *,
        intent: StandaloneCrmCallIntent,
        budget_calls_per_attempt: int,
        budget_calls_per_occurrence: int,
    ) -> bool:
        params = freshness_guard(intent.freshness) | {
            "generation": intent.generation,
            "parent_fence_token": intent.parent_fence_token,
            "intent_id": intent.intent_id,
            "sequence": intent.sequence,
            "call_kind": intent.call_kind,
            "unit_kind": intent.unit_kind,
            "retry_ordinal": intent.retry_ordinal,
            "metadata_digest": intent.metadata_digest,
            "cursor_id": intent.cursor_id,
            "subject_id": intent.subject_id,
            "upper_id": intent.upper_id,
            "max_calls_per_attempt": budget_calls_per_attempt,
            "max_calls_per_occurrence": budget_calls_per_occurrence,
        }

        def work(tx: ManagedTransaction) -> bool:
            return tx.run(queries.RESERVE_HTTP_CALL, **params).single() is not None  # type: ignore[arg-type]

        return self._client.execute_write(work)

    def classify_current_reserved_call_unknown(
        self, admission: StandaloneCrmCensusAdmission, *, intent_id: str
    ) -> bool:
        """Consume one current fenced reservation without trusting operator-supplied fences."""
        if not intent_id.strip():
            raise ValueError("intent_id must be non-empty")
        params = freshness_guard(admission) | {
            "fingerprint": admission.fingerprint,
            "intent_id": intent_id,
        }

        def work(tx: ManagedTransaction) -> bool:
            result = tx.run(queries.CLASSIFY_CURRENT_RESERVED_HTTP_CALL_UNKNOWN, **params)  # type: ignore[arg-type]
            return result.single() is not None

        return self._client.execute_write(work)

    def classify_reserved_call_unknown(self, intent: StandaloneCrmCallIntent) -> bool:
        """Consume one unresolved reservation without authorizing its reuse."""
        params = freshness_guard(intent.freshness) | {
            "fingerprint": intent.freshness.fingerprint,
            "generation": intent.generation,
            "parent_fence_token": intent.parent_fence_token,
            "intent_id": intent.intent_id,
        }

        def work(tx: ManagedTransaction) -> bool:
            result = tx.run(queries.CLASSIFY_RESERVED_HTTP_CALL_UNKNOWN, **params)  # type: ignore[arg-type]
            return result.single() is not None

        return self._client.execute_write(work)

    def record_call_outcome(
        self,
        intent: StandaloneCrmCallIntent,
        outcome: StandaloneCrmCallOutcome,
        *,
        numeric_result: int | None = None,
        result_digest: str = "",
    ) -> bool:
        if outcome not in {"succeeded", "failed", "unknown"}:
            raise ValueError("only resolved outcomes may be recorded")
        params = freshness_guard(intent.freshness) | {
            "generation": intent.generation,
            "parent_fence_token": intent.parent_fence_token,
            "intent_id": intent.intent_id,
            "outcome": outcome,
            "numeric_result": numeric_result,
            "result_digest": result_digest[:200],
        }

        def work(tx: ManagedTransaction) -> bool:
            return tx.run(queries.RECORD_HTTP_OUTCOME, **params).single() is not None  # type: ignore[arg-type]

        return self._client.execute_write(work)

    def _fail_if_exhausted(
        self,
        admission: StandaloneCrmCensusAdmission,
        request: StandaloneCrmCensusRequest,
        reason: str,
    ) -> bool:
        params = freshness_guard(admission) | {
            "fingerprint": admission.fingerprint,
            "max_attempts": request.budget.max_attempts_per_occurrence,
            "max_calls_per_occurrence": request.budget.max_calls_per_occurrence,
            "max_rows_per_occurrence": request.budget.max_rows_per_occurrence,
            "reason": reason,
        }

        def work(tx: ManagedTransaction) -> bool:
            return tx.run(queries.FAIL_EXHAUSTED_CENSUS, **params).single() is not None  # type: ignore[arg-type]

        return self._client.execute_write(work)


def freshness_guard(
    admission: StandaloneCrmCensusAdmission | StandaloneCrmFreshness,
) -> dict[str, object]:
    return {
        "census_id": admission.census_id,
        "authority_digest": admission.authority_digest,
        "source_instance_id": admission.source_instance_id,
        "control_instance_id": admission.control_instance_id,
    }


def census_json(value: object) -> str:
    def default(item: object) -> object:
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        raise TypeError(f"unsupported standalone census JSON item: {type(item).__name__}")

    return json.dumps(value, default=default, sort_keys=True, separators=(",", ":"))


def census_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def attempt_from_record(record: Record) -> StandaloneCrmAttempt:
    return StandaloneCrmAttempt(
        record_text(record, "census_id"),
        record_positive(record, "generation"),
        record_text(record, "task_id"),
        cast(
            Literal[
                "queued", "running", "paused_with_checkpoint", "failed", "superseded", "completed"
            ],
            record_text(record, "state"),
        ),
        record_positive(record, "fence_token"),
        record_timestamp(record, "deadline_at"),
        record_timestamp(record, "occurrence_deadline_at"),
    )


def record_text(record: Record, key: str) -> str:
    return value_text(record[key], key)


def value_text(value: object, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"standalone CRM census returned invalid {key}")
    return value


def record_bool(record: Record, key: str) -> bool:
    value = record[key]
    if not isinstance(value, bool):
        raise RuntimeError(f"standalone CRM census returned invalid {key}")
    return value


def record_non_negative(record: Record, key: str) -> int:
    value = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"standalone CRM census returned invalid {key}")
    return int(value)


def record_positive(record: Record, key: str) -> int:
    value = record_non_negative(record, key)
    if value < 1:
        raise RuntimeError(f"standalone CRM census returned invalid {key}")
    return value


def record_timestamp(record: Record, key: str) -> datetime:
    parsed = datetime.fromisoformat(record_text(record, key).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"standalone CRM census returned invalid {key}")
    return parsed.astimezone(UTC)


def record_mapping(record: Record, key: str) -> dict[str, object]:
    value = record[key]
    if not isinstance(value, dict):
        raise RuntimeError(f"standalone CRM census returned invalid {key}")
    return cast(dict[str, object], value)


def record_mappings(record: Record, key: str) -> tuple[dict[str, object], ...]:
    value = record[key]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError(f"standalone CRM census returned invalid {key}")
    return tuple(cast(dict[str, object], item) for item in value)
