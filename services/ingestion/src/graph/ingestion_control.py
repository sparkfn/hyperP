"""Durable control-plane access for checkpointed ingestion attempts."""

from __future__ import annotations

import uuid

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient
from src.graph.ingestion_control_models import (
    LogicalRunAttempt,
    LogicalRunState,
    encode_json,
    logical_attempt,
    logical_state,
    resumed_attempt,
    validate_counts,
)
from src.graph.queries.ingestion_control import (
    ADVANCE_LOGICAL_CHECKPOINT,
    CLAIM_QUEUED_ATTEMPT,
    CREATE_LOGICAL_RUN_AND_ATTEMPT,
    CREATE_RESUME_ATTEMPT,
    FAIL_LOGICAL_RUN,
    FINALIZE_LOGICAL_RUN,
    GET_ACTIVE_LOGICAL_RUN,
    PAUSE_LOGICAL_RUN,
    REQUEST_LOGICAL_RUN_STOP,
    TRANSITION_LOGICAL_PHASE,
)
from src.resumable import AttemptStatus, CheckpointDescriptor


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

    def resume(
        self,
        *,
        logical_run_id: str,
        worker_task_id: str,
        configuration_fingerprint: str,
        connector_version: str,
        checkpoint_schema_version: int,
    ) -> LogicalRunAttempt | None:
        def _work(tx: ManagedTransaction) -> LogicalRunAttempt | None:
            record = tx.run(
                CREATE_RESUME_ATTEMPT,
                logical_run_id=logical_run_id,
                worker_task_id=worker_task_id,
                configuration_fingerprint=configuration_fingerprint,
                connector_version=connector_version,
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
