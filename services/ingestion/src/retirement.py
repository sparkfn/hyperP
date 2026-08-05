"""Source-scoped retirement for upstream deletion and eligibility events."""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.graph import queries
from src.graph.client import Neo4jClient


def retire_source_evidence(
    client: Neo4jClient,
    source_system: str,
    source_record_id: str,
    retired_at: str,
    reconciliation_snapshot_at: str,
) -> int:
    def _work(tx: ManagedTransaction) -> int:
        record = tx.run(
            queries.RETIRE_SOURCE_EVIDENCE,
            source_system=source_system,
            source_record_id=source_record_id,
            retired_at=retired_at,
            reconciliation_snapshot_at=reconciliation_snapshot_at,
        ).single()
        return int(record["retired_count"]) if record is not None else 0

    return client.execute_write(_work)
