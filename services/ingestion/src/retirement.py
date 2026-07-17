"""Source-scoped retirement for upstream deletion and eligibility events."""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient

_RETIRE_SOURCE_EVIDENCE = """
MATCH (sr:SourceRecord {source_record_id: $source_record_id})
      -[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system})
WHERE sr.lifecycle_status IN ['active', 'pending_review']
   OR (sr.lifecycle_status IS NULL AND sr.is_latest = true)
WITH collect(sr) AS records
FOREACH (sr IN records |
  SET sr.lifecycle_status = 'superseded', sr.is_latest = false,
      sr.retired_at = datetime($retired_at), sr.updated_at = datetime()
)
WITH [sr IN records | sr.source_record_pk] AS source_record_pks
OPTIONAL MATCH ()-[rel]->()
WHERE rel.source_record_pk IN source_record_pks
SET rel.is_active = false, rel.updated_at = datetime()
RETURN size(source_record_pks) AS retired_count
"""


def retire_source_evidence(
    client: Neo4jClient,
    source_system: str,
    source_record_id: str,
    retired_at: str,
) -> int:
    def _work(tx: ManagedTransaction) -> int:
        record = tx.run(
            _RETIRE_SOURCE_EVIDENCE,
            source_system=source_system,
            source_record_id=source_record_id,
            retired_at=retired_at,
        ).single()
        return int(record["retired_count"]) if record is not None else 0

    return client.execute_write(_work)
