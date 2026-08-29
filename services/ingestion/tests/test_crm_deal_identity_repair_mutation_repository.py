"""Repository contract checks supplementing executable Neo4j acceptance tests."""

from __future__ import annotations

import inspect

from src.graph.crm_deal_identity_repair_mutation import CrmDealIdentityRepairMutationRepository
from src.graph.queries import crm_deal_identity_repair_mutation as queries


def test_repository_owns_one_execute_write_boundary_and_no_pipeline_escape_hatch() -> None:
    source = inspect.getsource(CrmDealIdentityRepairMutationRepository)
    assert "execute_write" in source
    assert "IngestPipeline" not in source
    assert "create_person" not in source
    assert "record_auto_merge_event" not in source
    assert "recompute_person_crm_deal_counts" not in source


def test_snapshot_and_retirement_are_source_version_and_descendant_precise() -> None:
    assert "(descendant:SourceRecord)-[:CHILD_OF*1..2]->(source)" in (
        queries.READ_MUTATION_GRAPH_SNAPSHOT
    )
    assert "relationship.source_record_pk IN affected_pks" in (queries.READ_MUTATION_GRAPH_SNAPSHOT)
    assert "projection.source_record_pk = source_record_pk" in queries.RETIRE_EXACT_CONTAMINATION
    assert "DETACH DELETE" not in queries.RETIRE_EXACT_CONTAMINATION


def test_review_and_ledger_queries_encode_required_cardinality_and_bundle() -> None:
    assert "is_active: false, provisional: true, authoritative: false" in (
        queries.STAGE_PROVISIONAL_REPAIR_LINK
    )
    assert (
        "ABOUT_LEFT {entity_type: 'source_record', repair_mutation_id: $mutation_id}"
        in queries.CREATE_REPAIR_DECISION
    )
    assert (
        "ABOUT_RIGHT {entity_type: 'person', repair_mutation_id: $mutation_id}"
        in queries.STAGE_ACTIVE_REPAIR_LINK
    )
    assert "active_person_evidence" in queries.VERIFY_REPAIRED_MUTATION_POSTCONDITIONS
    assert "rollback_image_id: $rollback_image_id" in queries.PERSIST_REPAIR_MUTATION_LEDGER
    assert "checkpoint_id: $checkpoint_id" in queries.PERSIST_REPAIR_MUTATION_LEDGER
    assert "outbox_event_id: $outbox_event_id" in queries.PERSIST_REPAIR_MUTATION_LEDGER
