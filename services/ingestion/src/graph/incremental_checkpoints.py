"""Durable Neo4j-backed state for incremental ingestion connectors.

The adapter deliberately presents the small Redis surface used by historical
connectors.  Successful watermarks are staged and flushed by the same Neo4j
transaction that completes the IngestRun; resumable page/retry state is written
immediately so a worker loss can resume safely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from neo4j import ManagedTransaction

from src.bitrix_ingestion_models import FenceContext
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import assert_active_bitrix_fence
from src.graph.queries.incremental_checkpoints import (
    DELETE_INCREMENTAL_CHECKPOINT,
    LOAD_INCREMENTAL_CHECKPOINT,
    UPSERT_INCREMENTAL_CHECKPOINT,
)
from src.source_instances import LEGACY_DEFAULT_CONTROL_INSTANCE_ID, effective_control_instance_id

logger = logging.getLogger(__name__)


class LegacyStateClient(Protocol):
    def get(self, name: str) -> object: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class _Operation:
    key: str
    value: str | None
    status: str


class Neo4jCheckpointRedis:
    """Redis-compatible incremental state adapter backed by Neo4j."""

    def __init__(
        self,
        client: Neo4jClient,
        source_key: str,
        *,
        legacy: LegacyStateClient | None = None,
        active_ingest_run_id: str | None = None,
        fence_context: FenceContext | None = None,
        control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
        defer_terminal_updates: bool = True,
        reset_generation: int | None = None,
    ) -> None:
        self._client = client
        self._source_key = source_key
        self._legacy = legacy
        self._active_ingest_run_id = active_ingest_run_id
        self._fence_context = fence_context
        self._control_instance_id = effective_control_instance_id(
            fence_context.control_instance_id if fence_context is not None else control_instance_id
        )
        if reset_generation is not None and reset_generation < 1:
            raise ValueError("reset generation must be positive")
        self._reset_generation = reset_generation
        self._defer_terminal_updates = defer_terminal_updates
        self._staged: dict[str, _Operation] = {}

    def __enter__(self) -> Neo4jCheckpointRedis:
        return self

    def __exit__(self, *_args: object) -> Literal[False]:
        self.close()
        return False

    def get(self, name: str) -> str | None:
        checkpoint_key = self._checkpoint_key(name)

        def _read(tx: ManagedTransaction) -> str | None:
            record = tx.run(
                LOAD_INCREMENTAL_CHECKPOINT,
                control_instance_id=self._control_instance_id,
                checkpoint_key=checkpoint_key,
            ).single()
            if record is None:
                return None
            value = record["value"]
            return value if isinstance(value, str) else None

        value = self._client.execute_read(_read)
        if value is not None:
            logger.info(
                "Incremental checkpoint source=%s key=%s mode=durable found=true",
                self._source_key,
                name,
            )
            return value
        if self._legacy is None or self._reset_generation is not None:
            logger.info(
                "Incremental checkpoint source=%s key=%s mode=durable found=false",
                self._source_key,
                name,
            )
            return None
        legacy_value = self._legacy.get(name)
        if isinstance(legacy_value, bytes):
            legacy_value = legacy_value.decode()
        if not isinstance(legacy_value, str):
            logger.info(
                "Incremental checkpoint source=%s key=%s mode=missing found=false",
                self._source_key,
                name,
            )
            return None
        self._write(
            _Operation(checkpoint_key, legacy_value, "migrated"),
            ingest_run_id=None,
        )
        logger.info(
            "Incremental checkpoint source=%s key=%s mode=redis_migrated found=true",
            self._source_key,
            name,
        )
        return legacy_value

    def set(self, name: str, value: str, *, status: str | None = None) -> None:
        operation = _Operation(
            self._checkpoint_key(name),
            value,
            status or checkpoint_status(name),
        )
        if self._defer_set(name):
            self._staged[name] = operation
        else:
            self._write(operation, ingest_run_id=self._active_ingest_run_id)

    def delete(self, *names: str) -> None:
        for name in names:
            operation = _Operation(self._checkpoint_key(name), None, "completed")
            if self._defer_delete(name):
                self._staged[name] = operation
            else:
                self._write(operation, ingest_run_id=self._active_ingest_run_id)

    def pipeline(self, *, transaction: bool) -> Neo4jCheckpointRedis:
        if not transaction:
            raise ValueError("incremental checkpoint pipeline must be transactional")
        return self

    def execute(self) -> list[object]:
        return []

    def close(self) -> None:
        if self._legacy is not None:
            self._legacy.close()
            self._legacy = None

    def flush(self, tx: ManagedTransaction, ingest_run_id: str, run_status: str) -> None:
        if self._reset_generation is not None and run_status not in {
            "completed",
            "completed_with_errors",
        }:
            raise ValueError("bounded watermarks require terminal completion")
        for operation in self._staged.values():
            successful = _Operation(operation.key, operation.value, run_status)
            self._write_in_transaction(tx, successful, ingest_run_id)

    def clear_staged(self) -> None:
        self._staged.clear()

    def bind_transaction(
        self,
        tx: ManagedTransaction,
        *,
        terminal_authorized: bool = False,
    ) -> TransactionBoundCheckpoints:
        """Return a view that reads and writes through ``tx`` only.

        The view performs no legacy import, no independent commit, and no
        connection close, so the caller keeps read-your-writes and rollback
        behavior inside its own fenced transaction.
        """
        return TransactionBoundCheckpoints(self, tx, terminal_authorized=terminal_authorized)

    def read_in_transaction(self, tx: ManagedTransaction, name: str) -> str | None:
        """Read one checkpoint through an open transaction, never via legacy state."""
        record = tx.run(
            LOAD_INCREMENTAL_CHECKPOINT,
            control_instance_id=self._control_instance_id,
            checkpoint_key=self._checkpoint_key(name),
        ).single()
        if record is None:
            return None
        value = record["value"]
        return value if isinstance(value, str) else None

    def write_in_transaction(
        self,
        tx: ManagedTransaction,
        name: str,
        value: str,
        *,
        status: str | None = None,
    ) -> None:
        """Upsert one checkpoint through an open transaction."""
        self._write_in_transaction(
            tx,
            _Operation(self._checkpoint_key(name), value, status or checkpoint_status(name)),
            self._active_ingest_run_id,
        )

    def delete_in_transaction(self, tx: ManagedTransaction, name: str) -> None:
        """Delete one checkpoint through an open transaction."""
        self._write_in_transaction(
            tx,
            _Operation(self._checkpoint_key(name), None, checkpoint_status(name)),
            self._active_ingest_run_id,
        )

    def checkpoint_key(self, name: str) -> str:
        """Return the generation-scoped durable key for a logical checkpoint name."""
        return self._checkpoint_key(name)

    def is_completed_watermark_key(self, name: str) -> bool:
        """Return whether a logical checkpoint name publishes a completed watermark."""
        return is_completed_watermark_key(name)

    @property
    def source_key(self) -> str:
        return self._source_key

    @property
    def reset_generation(self) -> int | None:
        return self._reset_generation

    @property
    def control_instance_id(self) -> str:
        return self._control_instance_id

    def _write(self, operation: _Operation, *, ingest_run_id: str | None) -> None:
        def _work(tx: ManagedTransaction) -> None:
            self._write_in_transaction(tx, operation, ingest_run_id)

        self._client.execute_write(_work)

    def _write_in_transaction(
        self,
        tx: ManagedTransaction,
        operation: _Operation,
        ingest_run_id: str | None,
    ) -> None:
        if self._fence_context is not None:
            assert_active_bitrix_fence(tx, self._fence_context)
        if operation.value is None:
            tx.run(
                DELETE_INCREMENTAL_CHECKPOINT,
                control_instance_id=self._control_instance_id,
                checkpoint_key=operation.key,
                ingest_run_id=ingest_run_id,
            )
            return
        tx.run(
            UPSERT_INCREMENTAL_CHECKPOINT,
            control_instance_id=self._control_instance_id,
            checkpoint_key=operation.key,
            source_key=self._source_key,
            value=operation.value,
            status=operation.status,
            ingest_run_id=ingest_run_id,
        )

    def _checkpoint_key(self, name: str) -> str:
        if self._reset_generation is None:
            return name
        return f"generation:{self._reset_generation}:{name}"

    def _defer_set(self, key: str) -> bool:
        return self._defer_terminal_updates and (
            is_completed_watermark_key(key) or ":fundbox_api:source_ids:" in key
        )

    def _defer_delete(self, key: str) -> bool:
        return self._defer_terminal_updates and key.endswith(":page")


class TransactionBoundCheckpoints:
    """Checkpoint access bound to one already-open writer transaction.

    This view never imports legacy state, opens its own transaction, or closes a
    connection. A completed watermark may only be written when the caller
    explicitly authorizes terminal completion.
    """

    def __init__(
        self,
        store: Neo4jCheckpointRedis,
        tx: ManagedTransaction,
        *,
        terminal_authorized: bool = False,
    ) -> None:
        self._store = store
        self._tx = tx
        self._terminal_authorized = terminal_authorized

    def get(self, name: str) -> str | None:
        """Read through the bound transaction, including its own uncommitted writes."""
        return self._store.read_in_transaction(self._tx, name)

    def set(self, name: str, value: str, *, status: str = "resume") -> None:
        """Write through the bound transaction, refusing unauthorized completion.

        The write is read back before the transaction commits so a silently
        skipped upsert cannot be reported as durable progress.
        """
        if status == "completed" and not self._terminal_authorized:
            raise RuntimeError("completed checkpoint writes require terminal authorization")
        if status != "completed" and self._store.is_completed_watermark_key(name):
            raise RuntimeError("completed watermark keys must be written as completed")
        self._store.write_in_transaction(self._tx, name, value, status=status)
        if self.get(name) != value:
            raise RuntimeError("checkpoint write was not durable in its transaction")

    def delete(self, name: str) -> None:
        """Delete through the bound transaction."""
        self._store.delete_in_transaction(self._tx, name)
        if self.get(name) is not None:
            raise RuntimeError("checkpoint delete was not durable in its transaction")


def is_completed_watermark_key(name: str) -> bool:
    """Return whether a logical checkpoint name is a completed watermark."""
    return ":watermark:" in name or name.endswith(":watermark")


def checkpoint_status(name: str) -> str:
    """Return the durable status recorded for a logical checkpoint name."""
    return "resume" if name.endswith((":page", ":retries")) else "completed"
