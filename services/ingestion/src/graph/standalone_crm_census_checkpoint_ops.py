"""Fenced child checkpoint and terminal-settlement operations for census persistence."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from neo4j import ManagedTransaction

from src.graph.queries import standalone_crm_census as queries
from src.graph.standalone_crm_census_types import (
    StandaloneCrmCensusAdmission,
    StandaloneCrmCensusStaleError,
)
from src.standalone_crm_census_models import (
    StandaloneCrmCheckpoint,
    StandaloneCrmChildSettlementState,
)

if TYPE_CHECKING:
    from src.graph.client import Neo4jClient


class StandaloneCrmCheckpointOperations:
    """Mixin for monotonic checkpointing and atomic terminal child settlement."""

    _client: Neo4jClient

    def checkpoint_child_with_work(
        self,
        admission: StandaloneCrmCensusAdmission,
        checkpoint: StandaloneCrmCheckpoint,
        *,
        expected_version: int,
        max_rows_per_attempt: int,
        max_rows_per_occurrence: int,
        work: Callable[[ManagedTransaction], None],
    ) -> int:
        """Run #274 work after validation and before a nonterminal checkpoint."""
        params = _checkpoint_params(
            admission, checkpoint, expected_version, max_rows_per_attempt, max_rows_per_occurrence
        )

        def transaction(tx: ManagedTransaction) -> int:
            _validate(
                tx, queries.VALIDATE_CHECKPOINT_UNIT, params, "checkpoint validation rejected"
            )
            work(tx)
            return _commit_checkpoint(
                tx, queries.CHECKPOINT_UNIT, params, "checkpoint accounting CAS rejected"
            )

        return self._client.execute_write(transaction)

    def checkpoint_child(
        self,
        admission: StandaloneCrmCensusAdmission,
        checkpoint: StandaloneCrmCheckpoint,
        *,
        expected_version: int,
        max_rows_per_attempt: int,
        max_rows_per_occurrence: int,
    ) -> int:
        return self.checkpoint_child_with_work(
            admission,
            checkpoint,
            expected_version=expected_version,
            max_rows_per_attempt=max_rows_per_attempt,
            max_rows_per_occurrence=max_rows_per_occurrence,
            work=_no_work,
        )

    def settle_child_with_work(
        self,
        admission: StandaloneCrmCensusAdmission,
        checkpoint: StandaloneCrmCheckpoint,
        *,
        terminal_state: StandaloneCrmChildSettlementState,
        expected_version: int,
        max_rows_per_attempt: int,
        max_rows_per_occurrence: int,
        work: Callable[[ManagedTransaction], None],
    ) -> int:
        """Atomically apply #274 work, final checkpoint/accounting, unit state, and release."""
        params = _checkpoint_params(
            admission, checkpoint, expected_version, max_rows_per_attempt, max_rows_per_occurrence
        ) | {"terminal_state": terminal_state}

        def transaction(tx: ManagedTransaction) -> int:
            _validate(
                tx, queries.VALIDATE_SETTLE_UNIT, params, "child settlement validation rejected"
            )
            work(tx)
            return _commit_checkpoint(
                tx, queries.SETTLE_UNIT, params, "child settlement CAS rejected"
            )

        return self._client.execute_write(transaction)

    def settle_child(
        self,
        admission: StandaloneCrmCensusAdmission,
        checkpoint: StandaloneCrmCheckpoint,
        *,
        terminal_state: StandaloneCrmChildSettlementState,
        expected_version: int,
        max_rows_per_attempt: int,
        max_rows_per_occurrence: int,
    ) -> int:
        return self.settle_child_with_work(
            admission,
            checkpoint,
            terminal_state=terminal_state,
            expected_version=expected_version,
            max_rows_per_attempt=max_rows_per_attempt,
            max_rows_per_occurrence=max_rows_per_occurrence,
            work=_no_work,
        )

    def release_child_fence(
        self, admission: StandaloneCrmCensusAdmission, checkpoint: StandaloneCrmCheckpoint
    ) -> None:
        params = _fenced_checkpoint_params(admission, checkpoint)

        def transaction(tx: ManagedTransaction) -> None:
            _validate(tx, queries.RELEASE_UNIT_FENCE, params, "child fence release rejected")

        self._client.execute_write(transaction)


def _validate(tx: ManagedTransaction, query: str, params: dict[str, object], message: str) -> None:
    if tx.run(query, **params).single() is None:  # type: ignore[arg-type]
        raise StandaloneCrmCensusStaleError(message)


def _commit_checkpoint(
    tx: ManagedTransaction, query: str, params: dict[str, object], message: str
) -> int:
    record = tx.run(query, **params).single()  # type: ignore[arg-type]
    if record is None:
        raise StandaloneCrmCensusStaleError(message)
    value = record["version"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError("standalone CRM census returned invalid checkpoint version")
    return int(value)


def _no_work(_tx: ManagedTransaction) -> None:
    return None


def _guard(admission: StandaloneCrmCensusAdmission) -> dict[str, object]:
    return {
        "census_id": admission.census_id,
        "authority_digest": admission.authority_digest,
        "source_instance_id": admission.source_instance_id,
        "control_instance_id": admission.control_instance_id,
    }


def _fenced_checkpoint_params(
    admission: StandaloneCrmCensusAdmission, checkpoint: StandaloneCrmCheckpoint
) -> dict[str, object]:
    return _guard(admission) | {
        "generation": checkpoint.generation,
        "parent_fence_token": checkpoint.parent_fence_token,
        "child_fence_token": checkpoint.child_fence_token,
        "child_task_id": checkpoint.child_task_id,
        "unit_kind": checkpoint.unit_kind,
    }


def _checkpoint_params(
    admission: StandaloneCrmCensusAdmission,
    checkpoint: StandaloneCrmCheckpoint,
    expected_version: int,
    max_rows_per_attempt: int,
    max_rows_per_occurrence: int,
) -> dict[str, object]:
    return _fenced_checkpoint_params(admission, checkpoint) | {
        "expected_version": expected_version,
        "last_committed_id": checkpoint.last_committed_id,
        "company_binding_after_contact_id": checkpoint.company_binding_after_contact_id,
        "processed_count": checkpoint.processed_count,
        "skipped_count": checkpoint.skipped_count,
        "failed_count": checkpoint.failed_count,
        "no_work_count": checkpoint.no_work_count,
        "max_rows_per_attempt": max_rows_per_attempt,
        "max_rows_per_occurrence": max_rows_per_occurrence,
    }
