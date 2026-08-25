"""Source-scoped retirement for upstream deletion and eligibility events."""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.bitrix_ingestion_models import FenceContext
from src.graph import queries
from src.graph.client import Neo4jClient
from src.graph.crm_deal_count import recompute_source_person_crm_deal_counts
from src.graph.ingestion_control import assert_active_bitrix_fence
from src.source_instances import effective_source_instance_id


def retire_source_evidence(
    client: Neo4jClient,
    source_system: str,
    source_record_id: str,
    retired_at: str,
    reconciliation_snapshot_at: str,
    *,
    source_instance_id: str | None = None,
    fence_context: FenceContext | None = None,
) -> int:
    def _work(tx: ManagedTransaction) -> int:
        if fence_context is not None:
            assert_active_bitrix_fence(tx, fence_context)
        record = tx.run(
            queries.RETIRE_SOURCE_EVIDENCE,
            source_system=source_system,
            source_instance_id=effective_source_instance_id(source_instance_id),
            source_record_id=source_record_id,
            retired_at=retired_at,
            reconciliation_snapshot_at=reconciliation_snapshot_at,
        ).single()
        if record is None:
            return 0
        source_record_pks = record.get("source_record_pks", [])
        if isinstance(source_record_pks, list):
            recompute_source_person_crm_deal_counts(
                tx, [value for value in source_record_pks if isinstance(value, str)]
            )
        return int(record["retired_count"])

    return client.execute_write(_work)
