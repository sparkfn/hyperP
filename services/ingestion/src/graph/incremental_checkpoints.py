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
        defer_terminal_updates: bool = True,
    ) -> None:
        self._client = client
        self._source_key = source_key
        self._legacy = legacy
        self._active_ingest_run_id = active_ingest_run_id
        self._fence_context = fence_context
        self._defer_terminal_updates = defer_terminal_updates
        self._staged: dict[str, _Operation] = {}

    def __enter__(self) -> Neo4jCheckpointRedis:
        return self

    def __exit__(self, *_args: object) -> Literal[False]:
        self.close()
        return False

    def get(self, name: str) -> str | None:
        def _read(tx: ManagedTransaction) -> str | None:
            record = tx.run(LOAD_INCREMENTAL_CHECKPOINT, checkpoint_key=name).single()
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
        if self._legacy is None:
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
        self._write(_Operation(name, legacy_value, "migrated"), ingest_run_id=None)
        logger.info(
            "Incremental checkpoint source=%s key=%s mode=redis_migrated found=true",
            self._source_key,
            name,
        )
        return legacy_value

    def set(self, name: str, value: str) -> None:
        status = "resume" if name.endswith((":page", ":retries")) else "completed"
        operation = _Operation(name, value, status)
        if self._defer_set(name):
            self._staged[name] = operation
        else:
            self._write(operation, ingest_run_id=self._active_ingest_run_id)

    def delete(self, *names: str) -> None:
        for name in names:
            operation = _Operation(name, None, "completed")
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
        for operation in self._staged.values():
            successful = _Operation(operation.key, operation.value, run_status)
            self._write_in_transaction(tx, successful, ingest_run_id)

    def clear_staged(self) -> None:
        self._staged.clear()

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
                checkpoint_key=operation.key,
                ingest_run_id=ingest_run_id,
            )
            return
        tx.run(
            UPSERT_INCREMENTAL_CHECKPOINT,
            checkpoint_key=operation.key,
            source_key=self._source_key,
            value=operation.value,
            status=operation.status,
            ingest_run_id=ingest_run_id,
        )

    def _defer_set(self, key: str) -> bool:
        return self._defer_terminal_updates and (
            ":watermark:" in key or key.endswith(":watermark") or ":fundbox_api:source_ids:" in key
        )

    def _defer_delete(self, key: str) -> bool:
        return self._defer_terminal_updates and key.endswith(":page")
