"""Source-scoped retirement for upstream deletion and eligibility events."""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.bitrix_ingestion_models import FenceContext
from src.graph import queries
from src.graph.client import Neo4jClient
from src.graph.crm_deal_count import recompute_source_person_crm_deal_counts
from src.graph.ingestion_control import assert_active_bitrix_fence
from src.graph.person_list_summary_deltas import PersonListSummaryDeltaCollector
from src.identity_link_revisions import IdentityLinkDesiredRevision, append_identity_link_revisions
from src.source_instances import effective_source_instance_id


def _source_identity_deal_element_ids(
    tx: ManagedTransaction,
    *,
    source_system: str,
    source_instance_id: str,
    source_record_id: str,
    reconciliation_snapshot_at: str,
) -> tuple[str, ...]:
    """Return the element ids of the distinct deals a retirement may retire."""
    element_ids: list[str] = []
    for row in tx.run(
        queries.FIND_SOURCE_IDENTITY_DEAL_ELEMENT_IDS,
        source_system=source_system,
        source_instance_id=source_instance_id,
        source_record_id=source_record_id,
        reconciliation_snapshot_at=reconciliation_snapshot_at,
    ):
        element_id = row.get("deal_element_id")
        if isinstance(element_id, str) and element_id and element_id not in element_ids:
            element_ids.append(element_id)
    return tuple(element_ids)


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
        # Lock the source identity before capturing, so a concurrent
        # retirement cannot apply a stale summary delta.
        tx.run(
            queries.LOCK_SOURCE_RECORD_IDENTITY,
            source_system=source_system,
            source_instance_id=effective_source_instance_id(source_instance_id),
            source_record_id=source_record_id,
        ).consume()
        collector = PersonListSummaryDeltaCollector()
        collector.capture_before(
            tx,
            deal_element_ids=_source_identity_deal_element_ids(
                tx,
                source_system=source_system,
                source_instance_id=effective_source_instance_id(source_instance_id),
                source_record_id=source_record_id,
                reconciliation_snapshot_at=reconciliation_snapshot_at,
            ),
        )
        provenance = tx.run(
            """
MATCH (sr:SourceRecord {source_instance_id: $source_instance_id,
                         source_record_id: $source_record_id})
      -[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system})
WHERE sr.lifecycle_status IN ['active', 'pending_review']
RETURN sr.source_entity_type AS source_entity_type, sr.source_entity_id AS source_entity_id,
       sr.identity_policy_version AS identity_policy_version,
       sr.source_record_pk AS source_record_pk
ORDER BY toInteger(sr.source_record_version) DESC LIMIT 1
""",
            source_system=source_system,
            source_instance_id=effective_source_instance_id(source_instance_id),
            source_record_id=source_record_id,
        ).single()
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
        if provenance is not None:
            entity_type = provenance.get("source_entity_type")
            entity_id = provenance.get("source_entity_id")
            policy = provenance.get("identity_policy_version")
            source_pk = provenance.get("source_record_pk")
            if all(
                isinstance(value, str) and value
                for value in (entity_type, entity_id, policy, source_pk)
            ):
                append_identity_link_revisions(
                    tx,
                    [
                        IdentityLinkDesiredRevision(
                            source_system=source_system,
                            source_instance_id=effective_source_instance_id(source_instance_id),
                            source_entity_type=entity_type,
                            source_entity_id=entity_id,
                            identity_policy_version=policy,
                            link_status="retired",
                            hyperp_person_id=None,
                            resolution_kind="source_retirement",
                            effective_at=retired_at,
                            cause_key=f"source-retirement:{source_pk}",
                        )
                    ],
                )
        source_record_pks = record.get("source_record_pks", [])
        if isinstance(source_record_pks, list):
            recompute_source_person_crm_deal_counts(
                tx, [value for value in source_record_pks if isinstance(value, str)]
            )
        retired_count = int(record["retired_count"])
        if retired_count:
            collector.finalize(tx)
        return retired_count

    return client.execute_write(_work)
