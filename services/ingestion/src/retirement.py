"""Source-scoped retirement for upstream deletion and eligibility events."""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.bitrix_ingestion_models import FenceContext
from src.graph import queries
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import assert_active_bitrix_fence


def retire_source_evidence(
    client: Neo4jClient,
    source_system: str,
    source_record_id: str,
    retired_at: str,
    reconciliation_snapshot_at: str,
    *,
    fence_context: FenceContext | None = None,
) -> int:
    def _work(tx: ManagedTransaction) -> int:
        if fence_context is not None:
            assert_active_bitrix_fence(tx, fence_context)
        record = tx.run(
            queries.RETIRE_SOURCE_EVIDENCE,
            source_system=source_system,
            source_record_id=source_record_id,
            retired_at=retired_at,
            reconciliation_snapshot_at=reconciliation_snapshot_at,
        ).single()
        return int(record["retired_count"]) if record is not None else 0

    return client.execute_write(_work)
