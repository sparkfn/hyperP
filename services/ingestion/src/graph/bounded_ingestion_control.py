"""Graph-authoritative bounded-run lifecycle and atomic unit commit."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

from neo4j import ManagedTransaction

from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedRecoveryState,
    BoundedStatus,
    BoundedUnit,
    BoundedUnitWriter,
    FailureCategory,
    OccurrenceContext,
    PauseReason,
    RetryObligation,
    RetryResolution,
    RunScope,
    UnitApplyResult,
    Usage,
)
from src.graph.bounded_ingestion_records import (
    attempt_from_record as _attempt,
)
from src.graph.bounded_ingestion_records import (
    committed_receipt_result as _committed_receipt_result,
)
from src.graph.bounded_ingestion_records import (
    fence_parameters as _fence_parameters,
)
from src.graph.bounded_ingestion_records import (
    logical_key as _logical_key,
)
from src.graph.bounded_ingestion_records import (
    occurrence_parameters as _occurrence_parameters,
)
from src.graph.bounded_ingestion_records import (
    recovery_from_record as _recovery_state,
)
from src.graph.bounded_ingestion_records import (
    required_positive_int as _required_positive_int,
)
from src.graph.bounded_ingestion_records import (
    required_text as _required_text,
)
from src.graph.bounded_ingestion_records import (
    scope_parameters as _scope_parameters,
)
from src.graph.bounded_ingestion_records import (
    status_from_record as _status,
)
from src.graph.bounded_ingestion_records import (
    usage_parameters as _usage_parameters,
)
from src.graph.bounded_ingestion_records import (
    validate_apply_result as _validate_apply_result,
)
from src.graph.client import Neo4jClient
from src.graph.ingestion_control_models import encode_json
from src.graph.queries.bounded_ingestion_control import (
    CLAIM_BOUNDED_ATTEMPT,
    CLAIM_BOUNDED_RECEIPT,
    COMPARE_AND_ADVANCE_RESET_GENERATION,
    ENSURE_BOUNDED_LOGICAL_RUN,
    FAIL_BOUNDED_RUN,
    FINALIZE_BOUNDED_RUN,
    FINALIZE_BOUNDED_UNIT,
    GET_ACTIVE_RESET_GENERATION,
    GET_BOUNDED_RECOVERY,
    GET_BOUNDED_STATUS,
    PAUSE_BOUNDED_RUN,
    PAUSE_BOUNDED_UNCLAIMED,
    PERSIST_BOUNDED_RETRY,
    REBIND_BOUNDED_OCCURRENCE,
    RELEASE_MANUAL_PAUSE,
    REQUEST_MANUAL_PAUSE,
    RESERVE_BOUNDED_USAGE,
    RESOLVE_BOUNDED_RETRY,
)
from src.resumable import CheckpointDescriptor


class BoundedIngestionControl:
    """Serialize admission, fencing, receipts, retries, and checkpoints in Neo4j."""

    def __init__(
        self,
        client: Neo4jClient,
        *,
        transaction_timeout_seconds: float = 30.0,
    ) -> None:
        if transaction_timeout_seconds <= 0:
            raise ValueError("transaction timeout must be positive")
        self._client = client
        self._transaction_timeout_seconds = transaction_timeout_seconds

    def admit_or_resume(
        self,
        *,
        scope: RunScope,
        occurrence: OccurrenceContext,
        initial_checkpoint: CheckpointDescriptor,
        worker_task_id: str,
        now: datetime,
        lease_token: str | None = None,
        lease_seconds: float = 300.0,
    ) -> AttemptContext | None:
        if lease_seconds <= 0:
            raise ValueError("bounded lease duration must be positive")
        logical_key = _logical_key(scope, initial_checkpoint)
        ensured = self._ensure(scope, occurrence, initial_checkpoint, logical_key)
        if ensured is None:
            return None
        logical_run_id, status, occurrence_id = ensured
        if status == "completed":
            return None
        if status == "paused_with_checkpoint" and occurrence_id != occurrence.occurrence_id:
            if not self._rebind(logical_run_id, scope, occurrence, now):
                return None
        return self._claim(
            logical_run_id=logical_run_id,
            scope=scope,
            occurrence=occurrence,
            worker_task_id=worker_task_id,
            lease_token=lease_token or worker_task_id,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            now=now,
        )

    def reserve_usage(
        self,
        context: AttemptContext,
        requested: Usage,
        budget: BoundedIngestionBudget,
    ) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                RESERVE_BOUNDED_USAGE,
                **_fence_parameters(context),
                **_usage_parameters(requested),
                max_records=budget.max_records,
                max_source_requests=budget.max_source_requests,
                max_pages=budget.max_pages,
                max_bytes=budget.max_bytes,
                max_extraction_calls=budget.max_extraction_calls,
            ).single()
            return record is not None

        return self._client.execute_write(
            work,
            transaction_timeout_seconds=self._transaction_timeout_seconds,
        )

    def commit_unit(
        self,
        context: AttemptContext,
        unit: BoundedUnit,
        writer: BoundedUnitWriter,
    ) -> UnitApplyResult | None:
        """Commit domain writes, retry facts, receipt, and checkpoint atomically."""

        def work(tx: ManagedTransaction) -> UnitApplyResult | None:
            receipt = tx.run(
                CLAIM_BOUNDED_RECEIPT,
                **_fence_parameters(context),
                phase=context.checkpoint.phase,
                cursor_before_json=encode_json(context.checkpoint.cursor),
                replay_id=unit.replay_id,
                creation_token=uuid4().hex,
            ).single()
            if receipt is None:
                return None
            if receipt["created"] is not True:
                return _committed_receipt_result(receipt)
            result = writer.apply(tx, context, unit)
            _validate_apply_result(unit, result)
            new_retry_count = sum(
                1
                for retry in result.retry_obligations
                if _persist_retry(tx, context.logical_run_id, retry)
            )
            resolved_retry_count = sum(
                1
                for resolution in result.resolved_retries
                if _resolve_retry(tx, context.logical_run_id, resolution)
            )
            finalized = tx.run(
                FINALIZE_BOUNDED_UNIT,
                **_fence_parameters(context),
                phase=context.checkpoint.phase,
                replay_id=unit.replay_id,
                cursor_after_json=encode_json(unit.unit.checkpoint_after.cursor),
                last_committed_record_id=unit.unit.checkpoint_after.last_committed_record_id,
                dispositions_json=json.dumps(result.dispositions),
                **_usage_parameters(unit.usage),
                committed_delta=result.dispositions.count("committed"),
                duplicate_delta=result.dispositions.count("duplicate"),
                excluded_delta=(
                    result.dispositions.count("excluded")
                    + result.dispositions.count("policy_dropped")
                ),
                retry_delta=result.dispositions.count("durable_retry"),
                retry_backlog_delta=new_retry_count - resolved_retry_count,
            ).single()
            if finalized is None:
                raise RuntimeError("bounded unit lost its write fence")
            return result

        return self._client.execute_write(
            work,
            transaction_timeout_seconds=self._transaction_timeout_seconds,
        )

    def pause(
        self,
        context: AttemptContext,
        reason: PauseReason,
        next_eligible_at: datetime,
    ) -> bool:
        return self._finish_attempt(
            PAUSE_BOUNDED_RUN,
            context,
            pause_reason=reason,
            next_eligible_at=next_eligible_at.isoformat(),
        )

    def fail(
        self,
        context: AttemptContext,
        category: FailureCategory,
        safe_message: str,
        next_eligible_at: datetime,
    ) -> bool:
        return self._finish_attempt(
            FAIL_BOUNDED_RUN,
            context,
            failure_category=category,
            failure_message=safe_message[:200],
            next_eligible_at=next_eligible_at.isoformat(),
        )

    def finalize(self, context: AttemptContext) -> bool:
        return self._finish_attempt(FINALIZE_BOUNDED_RUN, context)

    def status(self, logical_run_id: str) -> BoundedStatus | None:
        def work(tx: ManagedTransaction) -> BoundedStatus | None:
            record = tx.run(GET_BOUNDED_STATUS, logical_run_id=logical_run_id).single()
            return _status(record) if record is not None else None

        return self._client.execute_read(work)

    def active_reset_generation(self, environment: str) -> int | None:
        def work(tx: ManagedTransaction) -> int | None:
            record = tx.run(
                GET_ACTIVE_RESET_GENERATION,
                environment=environment,
            ).single()
            if record is None:
                return None
            return _required_positive_int(record, "generation")

        return self._client.execute_read(work)

    def compare_and_advance_reset_generation(
        self,
        *,
        environment: str,
        expected_generation: int,
        actor: str,
        authorization_reference: str,
    ) -> int | None:
        """Provide #441 a CAS primitive; this runner never calls it automatically."""
        if expected_generation < 1:
            raise ValueError("expected reset generation must be positive")
        if not actor.strip() or not authorization_reference.strip():
            raise ValueError("reset-generation authorization fields must be non-empty")

        def work(tx: ManagedTransaction) -> int | None:
            record = tx.run(
                COMPARE_AND_ADVANCE_RESET_GENERATION,
                environment=environment,
                expected_generation=expected_generation,
                actor=actor,
                reference=authorization_reference,
            ).single()
            if record is None:
                return None
            return _required_positive_int(record, "generation")

        return self._client.execute_write(
            work,
            transaction_timeout_seconds=self._transaction_timeout_seconds,
        )

    def recovery_state(
        self,
        logical_run_id: str,
        source_key: str,
        control_instance_id: str,
        reset_generation: int,
    ) -> BoundedRecoveryState | None:
        def work(tx: ManagedTransaction) -> BoundedRecoveryState | None:
            record = tx.run(
                GET_BOUNDED_RECOVERY,
                logical_run_id=logical_run_id,
                source_key=source_key,
                control_instance_id=control_instance_id,
                reset_generation=reset_generation,
            ).single()
            return _recovery_state(record, reset_generation) if record else None

        return self._client.execute_read(work)

    def pause_unclaimed(
        self,
        logical_run_id: str,
        source_key: str,
        control_instance_id: str,
        reset_generation: int,
        reason: PauseReason,
    ) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                PAUSE_BOUNDED_UNCLAIMED,
                logical_run_id=logical_run_id,
                source_key=source_key,
                control_instance_id=control_instance_id,
                reset_generation=reset_generation,
                pause_reason=reason,
            ).single()
            return record is not None

        return self._client.execute_write(
            work,
            transaction_timeout_seconds=self._transaction_timeout_seconds,
        )

    def request_manual_pause(
        self,
        logical_run_id: str,
        source_key: str,
        control_instance_id: str,
        reset_generation: int,
    ) -> bool:
        return self._identity_write(
            REQUEST_MANUAL_PAUSE,
            logical_run_id,
            source_key,
            control_instance_id,
            reset_generation,
        )

    def release_manual_pause(
        self,
        logical_run_id: str,
        source_key: str,
        control_instance_id: str,
        reset_generation: int,
    ) -> bool:
        return self._identity_write(
            RELEASE_MANUAL_PAUSE,
            logical_run_id,
            source_key,
            control_instance_id,
            reset_generation,
        )

    def _ensure(
        self,
        scope: RunScope,
        occurrence: OccurrenceContext,
        checkpoint: CheckpointDescriptor,
        logical_key: str,
    ) -> tuple[str, str, str] | None:
        def work(tx: ManagedTransaction) -> tuple[str, str, str] | None:
            record = tx.run(
                ENSURE_BOUNDED_LOGICAL_RUN,
                **_scope_parameters(scope),
                **_occurrence_parameters(occurrence),
                logical_key=logical_key,
                phase=checkpoint.phase,
                cursor_json=encode_json(checkpoint.cursor),
                source_window_json=encode_json(checkpoint.source_window),
                replay_boundary=checkpoint.replay_boundary,
            ).single()
            if record is None:
                return None
            return (
                _required_text(record, "logical_run_id"),
                _required_text(record, "status"),
                _required_text(record, "occurrence_id"),
            )

        return self._client.execute_write(
            work,
            transaction_timeout_seconds=self._transaction_timeout_seconds,
        )

    def _rebind(
        self,
        logical_run_id: str,
        scope: RunScope,
        occurrence: OccurrenceContext,
        now: datetime,
    ) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                REBIND_BOUNDED_OCCURRENCE,
                logical_run_id=logical_run_id,
                reset_generation=scope.reset_generation,
                now=now.isoformat(),
                **_occurrence_parameters(occurrence),
            ).single()
            return record is not None

        return self._client.execute_write(
            work,
            transaction_timeout_seconds=self._transaction_timeout_seconds,
        )

    def _claim(
        self,
        *,
        logical_run_id: str,
        scope: RunScope,
        occurrence: OccurrenceContext,
        worker_task_id: str,
        lease_token: str,
        lease_expires_at: datetime,
        now: datetime,
    ) -> AttemptContext | None:
        def work(tx: ManagedTransaction) -> AttemptContext | None:
            record = tx.run(
                CLAIM_BOUNDED_ATTEMPT,
                logical_run_id=logical_run_id,
                environment=scope.environment,
                reset_generation=scope.reset_generation,
                source_key=scope.source_key,
                control_instance_id=scope.control_instance_id,
                worker_task_id=worker_task_id,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at.isoformat(),
                now=now.isoformat(),
            ).single()
            return _attempt(record, scope, occurrence, worker_task_id) if record else None

        return self._client.execute_write(
            work,
            transaction_timeout_seconds=self._transaction_timeout_seconds,
        )

    def _finish_attempt(
        self,
        query: str,
        context: AttemptContext,
        **parameters: str,
    ) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                query,
                **_fence_parameters(context),
                **parameters,
            ).single()
            return record is not None

        return self._client.execute_write(
            work,
            transaction_timeout_seconds=self._transaction_timeout_seconds,
        )

    def _identity_write(
        self,
        query: str,
        logical_run_id: str,
        source_key: str,
        control_instance_id: str,
        reset_generation: int,
    ) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                query,
                logical_run_id=logical_run_id,
                source_key=source_key,
                control_instance_id=control_instance_id,
                reset_generation=reset_generation,
            ).single()
            return record is not None

        return self._client.execute_write(
            work,
            transaction_timeout_seconds=self._transaction_timeout_seconds,
        )


def _persist_retry(
    tx: ManagedTransaction,
    logical_run_id: str,
    retry: RetryObligation,
) -> bool:
    record = tx.run(
        PERSIST_BOUNDED_RETRY,
        logical_run_id=logical_run_id,
        replay_id=retry.replay_id,
        source_record_id=retry.source_record_id,
        source_version=retry.source_version,
        category=retry.category,
        attempt_count=retry.attempt_count,
        eligible_at=retry.eligible_at.isoformat() if retry.eligible_at else None,
        creation_token=uuid4().hex,
    ).single()
    return record is not None and record["created"] is True


def _resolve_retry(
    tx: ManagedTransaction,
    logical_run_id: str,
    resolution: RetryResolution,
) -> bool:
    record = tx.run(
        RESOLVE_BOUNDED_RETRY,
        logical_run_id=logical_run_id,
        replay_id=resolution.replay_id,
        source_record_id=resolution.source_record_id,
    ).single()
    return record is not None
