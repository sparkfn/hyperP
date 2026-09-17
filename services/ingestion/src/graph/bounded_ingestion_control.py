"""Graph-authoritative bounded-run lifecycle and atomic unit commit."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from typing import Protocol, cast
from uuid import uuid4

from neo4j import ManagedTransaction, Record, Result

from src.bitrix_ingestion_models import BitrixStreamKey
from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedAdmissionResult,
    BoundedRecoveryResult,
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
    reservation_key as _reservation_key,
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
from src.graph.ingestion_control import (
    BitrixStreamControl,
    assert_active_bitrix_fence,
)
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
    LOAD_BOUNDED_RETRIES,
    PAUSE_BOUNDED_RUN,
    PAUSE_BOUNDED_UNCLAIMED,
    PERSIST_BOUNDED_RETRY,
    REBIND_BOUNDED_OCCURRENCE,
    RELEASE_MANUAL_PAUSE,
    REQUEST_MANUAL_PAUSE,
    RESERVE_BOUNDED_USAGE,
    RESOLVE_BOUNDED_RETRY,
    RETIRE_OWNED_BITRIX_PREDECESSOR,
)
from src.graph.queries.ingestion_control import SET_FENCED_BITRIX_STREAM_STATUS
from src.resumable import CheckpointDescriptor


class _BoundedAdmissionRejectedError(RuntimeError):
    pass


class _QueryTransaction(Protocol):
    def run(self, query: str, **parameters: object) -> Result: ...


def _run(
    tx: ManagedTransaction,
    query: str,
    **parameters: object,
) -> Result:
    return cast(_QueryTransaction, tx).run(query, **parameters)


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
        max_graph_writers: int = 1,
    ) -> BoundedAdmissionResult:
        if lease_seconds <= 0 or max_graph_writers < 1:
            raise ValueError("bounded lease and writer count must be positive")
        logical_key = _logical_key(scope, initial_checkpoint)
        ensured = self._ensure(scope, occurrence, initial_checkpoint, logical_key)
        if ensured is None:
            return None
        logical_run_id, status, occurrence_id = ensured
        if status == "completed":
            return "completed"
        if occurrence_id == occurrence.occurrence_id and not occurrence.can_start(now):
            next_eligible = (
                occurrence.starts_at if now < occurrence.starts_at else occurrence.next_eligible_at
            )
            self.pause_unclaimed(
                logical_run_id,
                scope.source_key,
                scope.control_instance_id,
                scope.reset_generation,
                "schedule_window_closed",
                next_eligible,
            )
            return None
        if status in {"queued", "paused_with_checkpoint", "failed", "running"} and (
            occurrence_id != occurrence.occurrence_id
        ):
            if not self._rebind(logical_run_id, scope, occurrence, now):
                return None
        context = self._claim(
            logical_run_id=logical_run_id,
            scope=scope,
            occurrence=occurrence,
            worker_task_id=worker_task_id,
            lease_token=lease_token or uuid4().hex,
            lease_seconds=lease_seconds,
            max_graph_writers=max_graph_writers,
            now=now,
        )
        if context is None or scope.source_key != "bitrix_chat":
            return context
        if scope.stream_key not in {
            "crm_deals",
            "openlines_conversations",
            "crm_stage_history",
        }:
            raise ValueError("bounded Bitrix run requires an active supported stream")
        stream_key = cast(BitrixStreamKey, scope.stream_key)
        if context.attempt_generation > 1:
            self._retire_owned_bitrix_predecessor(context, stream_key)
        try:
            admission = BitrixStreamControl(self._client).admit_or_coalesce(
                stream_key=stream_key,
                logical_run_id=context.logical_run_id,
                ingest_run_id=context.ingest_run_id,
                attempt_generation=context.attempt_generation,
                worker_task_id=context.worker_task_id,
                control_instance_id=scope.control_instance_id,
                replace_active=False,
            )
        except Exception:
            self.fail(
                context,
                "lease",
                "bitrix_stream_admission_conflict",
                occurrence.next_eligible_at,
            )
            raise
        return replace(context, bitrix_fence_context=admission.fence_context)

    def _retire_owned_bitrix_predecessor(
        self,
        context: AttemptContext,
        stream_key: BitrixStreamKey,
    ) -> None:
        def work(tx: ManagedTransaction) -> None:
            record = _run(
                tx,
                RETIRE_OWNED_BITRIX_PREDECESSOR,
                control_instance_id=context.scope.control_instance_id,
                stream_key=stream_key,
                logical_run_id=context.logical_run_id,
                attempt_generation=context.attempt_generation,
            ).single()
            if record is None:
                raise _BoundedAdmissionRejectedError()

        try:
            self._client.execute_write(
                work,
                transaction_timeout_seconds=self._transaction_timeout_seconds,
            )
        except _BoundedAdmissionRejectedError:
            return

    def reserve_usage(
        self,
        context: AttemptContext,
        requested: Usage,
        budget: BoundedIngestionBudget,
    ) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            record = _run(
                tx,
                RESERVE_BOUNDED_USAGE,
                **_fence_parameters(context),
                **_usage_parameters(requested),
                reservation_key=_reservation_key(context, requested),
                creation_token=uuid4().hex,
                max_records=budget.max_records,
                max_source_requests=budget.max_source_requests,
                max_pages=budget.max_pages,
                max_bytes=budget.max_bytes,
                max_extraction_calls=budget.max_extraction_calls,
            ).single()
            if record is None:
                raise _BoundedAdmissionRejectedError()
            _assert_bitrix_fence(tx, context)
            return True

        try:
            return self._client.execute_write(
                work,
                transaction_timeout_seconds=self._transaction_timeout_seconds,
            )
        except _BoundedAdmissionRejectedError:
            return False

    def commit_unit(
        self,
        context: AttemptContext,
        unit: BoundedUnit,
        writer: BoundedUnitWriter,
    ) -> UnitApplyResult | None:
        """Commit domain writes, retry facts, receipt, and checkpoint atomically."""

        def work(tx: ManagedTransaction) -> UnitApplyResult | None:
            receipt = _run(
                tx,
                CLAIM_BOUNDED_RECEIPT,
                **_fence_parameters(context),
                phase=context.checkpoint.phase,
                cursor_before_json=encode_json(context.checkpoint.cursor),
                replay_id=unit.replay_id,
                creation_token=uuid4().hex,
            ).single()
            if receipt is None:
                raise _BoundedAdmissionRejectedError()
            _assert_bitrix_fence(tx, context)
            if receipt["created"] is not True and receipt["status"] == "committed":
                return _committed_receipt_result(receipt)
            if receipt["created"] is not True and receipt["status"] == "retry_pending":
                return _retry_pending_receipt_result(
                    tx,
                    context.logical_run_id,
                    unit.replay_id,
                    receipt,
                )
            if receipt["status"] not in {"pending", "retry_pending"}:
                raise RuntimeError("bounded receipt has an invalid state")
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
            finalized = _run(
                tx,
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
                checkpoint_can_advance=not result.retry_obligations,
                terminal=unit.terminal,
            ).single()
            if finalized is None:
                raise RuntimeError("bounded unit lost its write fence")
            return result

        try:
            return self._client.execute_write(
                work,
                transaction_timeout_seconds=self._transaction_timeout_seconds,
            )
        except _BoundedAdmissionRejectedError:
            return None

    def pause(
        self,
        context: AttemptContext,
        reason: PauseReason,
        next_eligible_at: datetime,
    ) -> bool:
        return self._finish_attempt(
            PAUSE_BOUNDED_RUN,
            context,
            bitrix_status="superseded",
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
            bitrix_status="terminated",
            failure_category=category,
            failure_message=safe_message[:200],
            next_eligible_at=next_eligible_at.isoformat(),
        )

    def finalize(self, context: AttemptContext) -> bool:
        return self._finish_attempt(
            FINALIZE_BOUNDED_RUN,
            context,
            bitrix_status="completed",
        )

    def status(self, logical_run_id: str) -> BoundedStatus | None:
        def work(tx: ManagedTransaction) -> BoundedStatus | None:
            record = _run(tx, GET_BOUNDED_STATUS, logical_run_id=logical_run_id).single()
            return _status(record) if record is not None else None

        return self._client.execute_read(work)

    def active_reset_generation(self, environment: str) -> int | None:
        def work(tx: ManagedTransaction) -> int | None:
            record = _run(
                tx,
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
            record = _run(
                tx,
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
        now: datetime,
    ) -> BoundedRecoveryResult:
        def work(tx: ManagedTransaction) -> BoundedRecoveryResult:
            record = _run(
                tx,
                GET_BOUNDED_RECOVERY,
                logical_run_id=logical_run_id,
                source_key=source_key,
                control_instance_id=control_instance_id,
                reset_generation=reset_generation,
                now=now.isoformat(),
            ).single()
            if record is None:
                return None
            recovery_status = _required_text(record, "recovery_status")
            if recovery_status == "completed":
                return "completed"
            if recovery_status == "running":
                lease_expires_at = _record_datetime(record, "lease_expires_at")
                if now < lease_expires_at:
                    return lease_expires_at
            if recovery_status not in {"running", "paused_with_checkpoint", "failed", "queued"}:
                return None
            return _recovery_state(record, reset_generation)

        return self._client.execute_read(work)

    def pause_unclaimed(
        self,
        logical_run_id: str,
        source_key: str,
        control_instance_id: str,
        reset_generation: int,
        reason: PauseReason,
        next_eligible_at: datetime | None = None,
    ) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            record = _run(
                tx,
                PAUSE_BOUNDED_UNCLAIMED,
                logical_run_id=logical_run_id,
                source_key=source_key,
                control_instance_id=control_instance_id,
                reset_generation=reset_generation,
                pause_reason=reason,
                next_eligible_at=(
                    next_eligible_at.isoformat() if next_eligible_at is not None else None
                ),
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
            record = _run(
                tx,
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
            record = _run(
                tx,
                REBIND_BOUNDED_OCCURRENCE,
                logical_run_id=logical_run_id,
                reset_generation=scope.reset_generation,
                now=now.isoformat(),
                **_occurrence_parameters(occurrence),
            ).single()
            if record is None:
                raise _BoundedAdmissionRejectedError()
            return True

        try:
            return self._client.execute_write(
                work,
                transaction_timeout_seconds=self._transaction_timeout_seconds,
            )
        except _BoundedAdmissionRejectedError:
            return False

    def _claim(
        self,
        *,
        logical_run_id: str,
        scope: RunScope,
        occurrence: OccurrenceContext,
        worker_task_id: str,
        lease_token: str,
        lease_seconds: float,
        max_graph_writers: int,
        now: datetime,
    ) -> AttemptContext | None:
        def work(tx: ManagedTransaction) -> AttemptContext | None:
            record = _run(
                tx,
                CLAIM_BOUNDED_ATTEMPT,
                logical_run_id=logical_run_id,
                environment=scope.environment,
                reset_generation=scope.reset_generation,
                source_key=scope.source_key,
                control_instance_id=scope.control_instance_id,
                worker_task_id=worker_task_id,
                lease_token=lease_token,
                lease_seconds=lease_seconds,
                max_graph_writers=max_graph_writers,
                now=now.isoformat(),
            ).single()
            if record is None:
                raise _BoundedAdmissionRejectedError()
            return _attempt(record, scope, occurrence, worker_task_id)

        try:
            return self._client.execute_write(
                work,
                transaction_timeout_seconds=self._transaction_timeout_seconds,
            )
        except _BoundedAdmissionRejectedError:
            return None

    def _finish_attempt(
        self,
        query: str,
        context: AttemptContext,
        *,
        bitrix_status: str,
        **parameters: str,
    ) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            record = _run(
                tx,
                query,
                **_fence_parameters(context),
                **parameters,
            ).single()
            if record is None:
                raise _BoundedAdmissionRejectedError()
            _assert_bitrix_fence(tx, context)
            if context.bitrix_fence_context is not None:
                stream = _run(
                    tx,
                    SET_FENCED_BITRIX_STREAM_STATUS,
                    **_bitrix_parameters(context),
                    status=bitrix_status,
                ).single()
                if stream is None:
                    raise RuntimeError("bounded Bitrix stream could not retire")
            return True

        try:
            return self._client.execute_write(
                work,
                transaction_timeout_seconds=self._transaction_timeout_seconds,
            )
        except _BoundedAdmissionRejectedError:
            return False

    def _identity_write(
        self,
        query: str,
        logical_run_id: str,
        source_key: str,
        control_instance_id: str,
        reset_generation: int,
    ) -> bool:
        def work(tx: ManagedTransaction) -> bool:
            record = _run(
                tx,
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
    record = _run(
        tx,
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
    record = _run(
        tx,
        RESOLVE_BOUNDED_RETRY,
        logical_run_id=logical_run_id,
        replay_id=resolution.replay_id,
        source_record_id=resolution.source_record_id,
    ).single()
    return record is not None


def _assert_bitrix_fence(tx: ManagedTransaction, context: AttemptContext) -> None:
    if context.bitrix_fence_context is not None:
        assert_active_bitrix_fence(tx, context.bitrix_fence_context)


def _bitrix_parameters(context: AttemptContext) -> dict[str, object]:
    fence = context.bitrix_fence_context
    if fence is None:
        return {}
    return {
        "source_key": fence.source_key,
        "control_instance_id": fence.control_instance_id,
        "stream_key": fence.stream_key,
        "logical_run_id": fence.logical_run_id,
        "ingest_run_id": fence.ingest_run_id,
        "attempt_generation": fence.attempt_generation,
        "stream_generation": fence.stream_generation,
        "fencing_token": fence.fencing_token,
    }


def _retry_pending_receipt_result(
    tx: ManagedTransaction,
    logical_run_id: str,
    replay_id: str,
    receipt: Record,
) -> UnitApplyResult:
    base = _committed_receipt_result(receipt)
    retries: list[RetryObligation] = []
    for row in _run(
        tx,
        LOAD_BOUNDED_RETRIES,
        logical_run_id=logical_run_id,
        replay_id=replay_id,
    ):
        eligible_raw: object = row["eligible_at"]
        eligible = (
            datetime.fromisoformat(eligible_raw.replace("Z", "+00:00"))
            if isinstance(eligible_raw, str) and eligible_raw
            else None
        )
        retries.append(
            RetryObligation(
                replay_id=replay_id,
                source_record_id=_record_text(row, "source_record_id"),
                source_version=_record_text(row, "source_version"),
                category=_record_text(row, "category"),
                attempt_count=_record_positive_int(row, "attempt_count"),
                eligible_at=eligible,
            )
        )
    return UnitApplyResult(
        dispositions=base.dispositions,
        retry_obligations=tuple(retries),
    )


def _record_text(record: Record, key: str) -> str:
    value: object = record[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"bounded retry returned invalid {key}")
    return value


def _record_positive_int(record: Record, key: str) -> int:
    value: object = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"bounded retry returned invalid {key}")
    return value


def _record_datetime(record: Record, key: str) -> datetime:
    value: object = record[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"bounded recovery returned invalid {key}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"bounded recovery returned naive {key}")
    return parsed
