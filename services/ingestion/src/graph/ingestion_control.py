"""Durable control-plane access for checkpointed ingestion attempts."""

from __future__ import annotations

import uuid

from neo4j import ManagedTransaction

from src.bitrix_ingestion_models import BITRIX_STREAM_KEYS, BitrixStreamKey, FenceContext
from src.graph.client import Neo4jClient
from src.graph.ingestion_control_models import (
    BitrixStreamAdmission,
    LogicalRunAttempt,
    LogicalRunState,
    bitrix_stream_admission,
    encode_json,
    logical_attempt,
    logical_state,
    resumed_attempt,
    validate_counts,
)
from src.graph.queries.ingestion_control import (
    ADMIT_OR_COALESCE_BITRIX_STREAM,
    ADVANCE_LOGICAL_CHECKPOINT,
    CLAIM_QUEUED_ATTEMPT,
    CREATE_LOGICAL_RUN_AND_ATTEMPT,
    CREATE_RESUME_ATTEMPT,
    FAIL_LOGICAL_RUN,
    FINALIZE_LOGICAL_RUN,
    FIND_BITRIX_FENCE_ROLLBACK_PROBE,
    GET_ACTIVE_LOGICAL_RUN,
    LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
    PAUSE_LOGICAL_RUN,
    PROBE_REJECTED_BITRIX_FENCE_ROLLBACK,
    REQUEST_LOGICAL_RUN_STOP,
    SET_FENCED_BITRIX_STREAM_STATUS,
    TRANSITION_LOGICAL_PHASE,
)
from src.resumable import AttemptStatus, CheckpointDescriptor


def assert_active_bitrix_fence(tx: ManagedTransaction, context: FenceContext) -> None:
    """Acquire the stream write lock and reject stale mutation ownership."""
    record = tx.run(
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
        source_key=context.source_key,
        stream_key=context.stream_key,
        logical_run_id=context.logical_run_id,
        ingest_run_id=context.ingest_run_id,
        attempt_generation=context.attempt_generation,
        stream_generation=context.stream_generation,
        fencing_token=context.fencing_token,
    ).single()
    if record is None:
        raise RuntimeError("Bitrix mutation fence is stale or inactive")


class _RollbackProbeCompleteError(Exception):
    """Internal signal that forces the probe transaction to roll back."""


def verify_rejected_bitrix_fence_rollback(
    client: Neo4jClient,
    context: FenceContext,
) -> bool:
    """Prove a rejected fence assertion rolls back without racing live lock increments."""
    probe_token = uuid.uuid4().hex
    rejected = False

    def _probe(tx: ManagedTransaction) -> None:
        nonlocal rejected
        record = tx.run(
            PROBE_REJECTED_BITRIX_FENCE_ROLLBACK,
            source_key=context.source_key,
            stream_key=context.stream_key,
            logical_run_id=context.logical_run_id,
            ingest_run_id=context.ingest_run_id,
            attempt_generation=context.attempt_generation,
            stream_generation=context.stream_generation,
            fencing_token=context.fencing_token,
            probe_token=probe_token,
        ).single()
        rejected = record is not None and record["fence_accepted"] is False
        raise _RollbackProbeCompleteError

    try:
        client.execute_write(_probe)
    except _RollbackProbeCompleteError:
        pass
    if not rejected:
        return False

    def _verify(tx: ManagedTransaction) -> bool:
        record = tx.run(
            FIND_BITRIX_FENCE_ROLLBACK_PROBE,
            probe_token=probe_token,
        ).single()
        return record is not None and int(record["persisted_probe_count"]) == 0

    return client.execute_read(_verify)


class LogicalRunControl:
    """Serialize logical-run ownership and checkpoints in Neo4j."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def create_or_reuse(
        self,
        *,
        source_key: str,
        mode: str,
        dump_path: str | None,
        entity_key: str | None,
        idempotency_key: str,
        worker_task_id: str,
        configuration_fingerprint: str,
        connector_version: str,
        checkpoint_schema_version: int,
        initial_checkpoint: CheckpointDescriptor,
    ) -> LogicalRunAttempt:
        if initial_checkpoint.connector_version != connector_version:
            raise ValueError("Initial checkpoint connector version is incompatible")
        if initial_checkpoint.schema_version != checkpoint_schema_version:
            raise ValueError("Initial checkpoint schema version is incompatible")
        creation_token = uuid.uuid4().hex

        def _work(tx: ManagedTransaction) -> LogicalRunAttempt:
            record = tx.run(
                CREATE_LOGICAL_RUN_AND_ATTEMPT,
                source_key=source_key,
                mode=mode,
                dump_path=dump_path,
                entity_key=entity_key,
                idempotency_key=idempotency_key,
                worker_task_id=worker_task_id,
                configuration_fingerprint=configuration_fingerprint,
                connector_version=connector_version,
                checkpoint_schema_version=checkpoint_schema_version,
                run_type=mode,
                initial_phase=initial_checkpoint.phase,
                initial_cursor_json=encode_json(initial_checkpoint.cursor),
                initial_source_window_json=encode_json(initial_checkpoint.source_window),
                replay_boundary=initial_checkpoint.replay_boundary,
                creation_token=creation_token,
            ).single()
            return logical_attempt(record)

        return self._client.execute_write(_work)

    def claim(
        self,
        *,
        logical_run_id: str,
        ingest_run_id: str,
        generation: int,
        worker_task_id: str,
    ) -> bool:
        """Claim the queued attempt, returning false for stop or stale ownership."""

        def _work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                CLAIM_QUEUED_ATTEMPT,
                logical_run_id=logical_run_id,
                ingest_run_id=ingest_run_id,
                generation=generation,
                worker_task_id=worker_task_id,
            ).single()
            if record is None:
                return False
            return record["stop_requested"] is not True

        return self._client.execute_write(_work)

    def request_stop(
        self,
        *,
        logical_run_id: str,
        requested_by: str,
        reason: str | None,
    ) -> LogicalRunState | None:
        def _work(tx: ManagedTransaction) -> bool:
            result = tx.run(
                REQUEST_LOGICAL_RUN_STOP,
                logical_run_id=logical_run_id,
                requested_by=requested_by,
                reason=reason,
            ).single()
            return result is not None

        if not self._client.execute_write(_work):
            return None
        return self.get(logical_run_id)

    def get(self, logical_run_id: str) -> LogicalRunState | None:
        def _work(tx: ManagedTransaction) -> LogicalRunState | None:
            record = tx.run(GET_ACTIVE_LOGICAL_RUN, logical_run_id=logical_run_id).single()
            if record is None:
                return None
            return logical_state(record)

        return self._client.execute_read(_work)

    def advance_checkpoint(
        self,
        *,
        logical_run_id: str,
        ingest_run_id: str,
        generation: int,
        checkpoint: CheckpointDescriptor,
        committed_count: int,
        duplicate_count: int,
        excluded_count: int,
        retry_count: int,
    ) -> bool | None:
        """Advance the fenced checkpoint and return the durable stop flag."""
        validate_counts(committed_count, duplicate_count, excluded_count, retry_count)

        def _work(tx: ManagedTransaction) -> bool | None:
            record = tx.run(
                ADVANCE_LOGICAL_CHECKPOINT,
                logical_run_id=logical_run_id,
                ingest_run_id=ingest_run_id,
                generation=generation,
                phase=checkpoint.phase,
                cursor_json=encode_json(checkpoint.cursor),
                source_window_json=encode_json(checkpoint.source_window),
                connector_version=checkpoint.connector_version,
                checkpoint_schema_version=checkpoint.schema_version,
                last_committed_record_id=checkpoint.last_committed_record_id,
                committed_count=committed_count,
                duplicate_count=duplicate_count,
                excluded_count=excluded_count,
                retry_count=retry_count,
            ).single()
            if record is None:
                return None
            return record["stop_requested"] is True

        return self._client.execute_write(_work)

    def advance_checkpoint_fenced(
        self,
        *,
        context: FenceContext,
        checkpoint: CheckpointDescriptor,
        committed_count: int,
        duplicate_count: int,
        excluded_count: int,
        retry_count: int,
    ) -> bool | None:
        """Advance a split checkpoint while holding its stream-node write lock."""
        validate_counts(committed_count, duplicate_count, excluded_count, retry_count)

        def _work(tx: ManagedTransaction) -> bool | None:
            assert_active_bitrix_fence(tx, context)
            record = tx.run(
                ADVANCE_LOGICAL_CHECKPOINT,
                logical_run_id=context.logical_run_id,
                ingest_run_id=context.ingest_run_id,
                generation=context.attempt_generation,
                phase=checkpoint.phase,
                cursor_json=encode_json(checkpoint.cursor),
                source_window_json=encode_json(checkpoint.source_window),
                connector_version=checkpoint.connector_version,
                checkpoint_schema_version=checkpoint.schema_version,
                last_committed_record_id=checkpoint.last_committed_record_id,
                committed_count=committed_count,
                duplicate_count=duplicate_count,
                excluded_count=excluded_count,
                retry_count=retry_count,
            ).single()
            if record is None:
                return None
            return record["stop_requested"] is True

        return self._client.execute_write(_work)

    def pause(
        self,
        *,
        logical_run_id: str,
        ingest_run_id: str,
        generation: int,
        phase: str,
    ) -> bool:
        def _work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                PAUSE_LOGICAL_RUN,
                logical_run_id=logical_run_id,
                ingest_run_id=ingest_run_id,
                generation=generation,
                phase=phase,
            ).single()
            return record is not None

        return self._client.execute_write(_work)

    def transition_phase(
        self,
        *,
        logical_run_id: str,
        ingest_run_id: str,
        generation: int,
        current_phase: str,
        next_checkpoint: CheckpointDescriptor,
        committed_count: int,
        duplicate_count: int,
        excluded_count: int,
        retry_count: int,
    ) -> bool | None:
        """Complete one phase and create or reactivate its fenced successor."""
        if not current_phase.strip():
            raise ValueError("Current checkpoint phase must be non-empty")
        if current_phase == next_checkpoint.phase:
            raise ValueError("Checkpoint phase transition must advance to a new phase")
        validate_counts(committed_count, duplicate_count, excluded_count, retry_count)

        def _work(tx: ManagedTransaction) -> bool | None:
            record = tx.run(
                TRANSITION_LOGICAL_PHASE,
                logical_run_id=logical_run_id,
                ingest_run_id=ingest_run_id,
                generation=generation,
                current_phase=current_phase,
                next_phase=next_checkpoint.phase,
                cursor_json=encode_json(next_checkpoint.cursor),
                source_window_json=encode_json(next_checkpoint.source_window),
                connector_version=next_checkpoint.connector_version,
                checkpoint_schema_version=next_checkpoint.schema_version,
                replay_boundary=next_checkpoint.replay_boundary,
                committed_count=committed_count,
                duplicate_count=duplicate_count,
                excluded_count=excluded_count,
                retry_count=retry_count,
            ).single()
            if record is None:
                return None
            return record["stop_requested"] is True

        return self._client.execute_write(_work)

    def transition_phase_fenced(
        self,
        *,
        context: FenceContext,
        current_phase: str,
        next_checkpoint: CheckpointDescriptor,
        committed_count: int,
        duplicate_count: int,
        excluded_count: int,
        retry_count: int,
    ) -> bool | None:
        """Advance a split run to its next schema phase under the stream fence."""
        if current_phase == next_checkpoint.phase:
            raise ValueError("Checkpoint phase transition must advance to a new phase")
        validate_counts(committed_count, duplicate_count, excluded_count, retry_count)

        def _work(tx: ManagedTransaction) -> bool | None:
            assert_active_bitrix_fence(tx, context)
            record = tx.run(
                TRANSITION_LOGICAL_PHASE,
                logical_run_id=context.logical_run_id,
                ingest_run_id=context.ingest_run_id,
                generation=context.attempt_generation,
                current_phase=current_phase,
                next_phase=next_checkpoint.phase,
                cursor_json=encode_json(next_checkpoint.cursor),
                source_window_json=encode_json(next_checkpoint.source_window),
                connector_version=next_checkpoint.connector_version,
                checkpoint_schema_version=next_checkpoint.schema_version,
                replay_boundary=next_checkpoint.replay_boundary,
                committed_count=committed_count,
                duplicate_count=duplicate_count,
                excluded_count=excluded_count,
                retry_count=retry_count,
            ).single()
            if record is None:
                return None
            return record["stop_requested"] is True

        return self._client.execute_write(_work)

    def resume(
        self,
        *,
        logical_run_id: str,
        worker_task_id: str,
        configuration_fingerprint: str,
        logical_connector_version: str,
        checkpoint_connector_version: str,
        checkpoint_schema_version: int,
    ) -> LogicalRunAttempt | None:
        def _work(tx: ManagedTransaction) -> LogicalRunAttempt | None:
            record = tx.run(
                CREATE_RESUME_ATTEMPT,
                logical_run_id=logical_run_id,
                worker_task_id=worker_task_id,
                configuration_fingerprint=configuration_fingerprint,
                logical_connector_version=logical_connector_version,
                checkpoint_connector_version=checkpoint_connector_version,
                checkpoint_schema_version=checkpoint_schema_version,
            ).single()
            if record is None:
                return None
            return resumed_attempt(record)

        return self._client.execute_write(_work)

    def finalize(
        self,
        *,
        logical_run_id: str,
        ingest_run_id: str,
        generation: int,
        phase: str,
        status: AttemptStatus,
        committed_count: int,
        duplicate_count: int,
        excluded_count: int,
        retry_count: int,
        record_count: int,
        rejected_count: int,
    ) -> bool:
        if status not in {"completed", "completed_with_errors"}:
            raise ValueError("Logical-run final status must be a completion status")
        validate_counts(
            committed_count,
            duplicate_count,
            excluded_count,
            retry_count,
            record_count,
            rejected_count,
        )

        def _work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                FINALIZE_LOGICAL_RUN,
                logical_run_id=logical_run_id,
                ingest_run_id=ingest_run_id,
                generation=generation,
                phase=phase,
                status=status,
                committed_count=committed_count,
                duplicate_count=duplicate_count,
                excluded_count=excluded_count,
                retry_count=retry_count,
                record_count=record_count,
                rejected_count=rejected_count,
            ).single()
            return record is not None

        return self._client.execute_write(_work)

    def finalize_fenced(
        self,
        *,
        context: FenceContext,
        phase: str,
        status: AttemptStatus,
        committed_count: int,
        duplicate_count: int,
        excluded_count: int,
        retry_count: int,
        record_count: int,
        rejected_count: int,
    ) -> None:
        """Complete the logical run and its stream under one stream-node lock."""
        if status not in {"completed", "completed_with_errors"}:
            raise ValueError("Logical-run final status must be a completion status")
        validate_counts(
            committed_count,
            duplicate_count,
            excluded_count,
            retry_count,
            record_count,
            rejected_count,
        )

        def _work(tx: ManagedTransaction) -> None:
            assert_active_bitrix_fence(tx, context)
            finalized = tx.run(
                FINALIZE_LOGICAL_RUN,
                logical_run_id=context.logical_run_id,
                ingest_run_id=context.ingest_run_id,
                generation=context.attempt_generation,
                phase=phase,
                status=status,
                committed_count=committed_count,
                duplicate_count=duplicate_count,
                excluded_count=excluded_count,
                retry_count=retry_count,
                record_count=record_count,
                rejected_count=rejected_count,
            ).single()
            if finalized is None:
                raise RuntimeError("Bitrix logical run could not be finalized under its fence")
            terminal = tx.run(
                SET_FENCED_BITRIX_STREAM_STATUS,
                source_key=context.source_key,
                stream_key=context.stream_key,
                logical_run_id=context.logical_run_id,
                ingest_run_id=context.ingest_run_id,
                attempt_generation=context.attempt_generation,
                stream_generation=context.stream_generation,
                fencing_token=context.fencing_token,
                status="completed",
            ).single()
            if terminal is None:
                raise RuntimeError("Bitrix stream could not be completed under its fence")

        self._client.execute_write(_work)

    def fail(
        self,
        *,
        logical_run_id: str,
        ingest_run_id: str,
        generation: int,
        failure_category: str,
        safe_failure_message: str,
    ) -> bool:
        if not failure_category.strip():
            raise ValueError("Failure category must be non-empty")
        bounded_message = safe_failure_message[:1000]

        def _work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                FAIL_LOGICAL_RUN,
                logical_run_id=logical_run_id,
                ingest_run_id=ingest_run_id,
                generation=generation,
                failure_category=failure_category,
                failure_message=bounded_message,
            ).single()
            return record is not None

        return self._client.execute_write(_work)

    def fail_fenced(
        self,
        *,
        context: FenceContext,
        failure_category: str,
        safe_failure_message: str,
    ) -> None:
        """Fail the logical attempt and terminate the stream atomically."""
        if not failure_category.strip():
            raise ValueError("Failure category must be non-empty")

        def _work(tx: ManagedTransaction) -> None:
            assert_active_bitrix_fence(tx, context)
            failed = tx.run(
                FAIL_LOGICAL_RUN,
                logical_run_id=context.logical_run_id,
                ingest_run_id=context.ingest_run_id,
                generation=context.attempt_generation,
                failure_category=failure_category,
                failure_message=safe_failure_message[:1000],
            ).single()
            if failed is None:
                raise RuntimeError("Bitrix logical run could not be failed under its fence")
            terminal = tx.run(
                SET_FENCED_BITRIX_STREAM_STATUS,
                source_key=context.source_key,
                stream_key=context.stream_key,
                logical_run_id=context.logical_run_id,
                ingest_run_id=context.ingest_run_id,
                attempt_generation=context.attempt_generation,
                stream_generation=context.stream_generation,
                fencing_token=context.fencing_token,
                status="terminated",
            ).single()
            if terminal is None:
                raise RuntimeError("Bitrix stream could not be terminated under its fence")

        self._client.execute_write(_work)


class BitrixStreamControl:
    """Durable admission for a single transaction-fenced Bitrix stream."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def admit_or_coalesce(
        self,
        *,
        stream_key: BitrixStreamKey,
        logical_run_id: str,
        ingest_run_id: str,
        attempt_generation: int,
        worker_task_id: str,
        replace_active: bool = False,
    ) -> BitrixStreamAdmission:
        """Admit a stream, coalesce a duplicate, or atomically replace it.

        A delivery for the same logical attempt coalesces. A different attempt
        fails closed unless ``replace_active`` is explicit; replacement advances
        both the stream generation and its fencing token in the same transaction.
        """
        _validate_bitrix_stream_admission(
            stream_key=stream_key,
            logical_run_id=logical_run_id,
            ingest_run_id=ingest_run_id,
            attempt_generation=attempt_generation,
            worker_task_id=worker_task_id,
            replace_active=replace_active,
        )
        creation_token = uuid.uuid4().hex

        def _work(tx: ManagedTransaction) -> BitrixStreamAdmission:
            record = tx.run(
                ADMIT_OR_COALESCE_BITRIX_STREAM,
                source_key="bitrix_chat",
                stream_key=stream_key,
                logical_run_id=logical_run_id,
                ingest_run_id=ingest_run_id,
                attempt_generation=attempt_generation,
                worker_task_id=worker_task_id,
                replace_active=replace_active,
                creation_token=creation_token,
            ).single()
            return bitrix_stream_admission(record)

        return self._client.execute_write(_work)


def _validate_bitrix_stream_admission(
    *,
    stream_key: BitrixStreamKey,
    logical_run_id: str,
    ingest_run_id: str,
    attempt_generation: int,
    worker_task_id: str,
    replace_active: bool,
) -> None:
    if stream_key not in BITRIX_STREAM_KEYS:
        raise ValueError("stream_key must be a supported Bitrix stream")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (logical_run_id, ingest_run_id, worker_task_id)
    ):
        raise ValueError("Bitrix stream admission identity values must be non-empty")
    if (
        isinstance(attempt_generation, bool)
        or not isinstance(attempt_generation, int)
        or attempt_generation < 1
    ):
        raise ValueError("attempt_generation must be a positive integer")
    if not isinstance(replace_active, bool):
        raise ValueError("replace_active must be a boolean")
