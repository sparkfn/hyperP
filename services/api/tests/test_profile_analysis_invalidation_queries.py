"""Atomic invalidation contracts for API-side profile-analysis inputs."""

from __future__ import annotations

from src.graph import queries
from src.graph.queries import (
    ACTIVATE_PENDING_REVIEW_RECORD,
    EXECUTE_MANUAL_MERGE,
    MARK_REVIEW_SALES_RECORD_LINKED,
    PROMOTE_STAGED_REVIEW_SALE,
    REVERT_MERGE,
    UPDATE_GOLDEN_FIELD,
    UPDATE_GOLDEN_PROFILE,
)


def _assert_dirties_profile_analysis(query: str) -> None:
    assert "analysis_input_revision" in query
    assert "analysis_dirty_at" in query


def test_person_graph_mutations_dirty_profile_analysis_atomically() -> None:
    for query in (
        EXECUTE_MANUAL_MERGE,
        REVERT_MERGE,
        ACTIVATE_PENDING_REVIEW_RECORD,
        PROMOTE_STAGED_REVIEW_SALE,
        MARK_REVIEW_SALES_RECORD_LINKED,
        UPDATE_GOLDEN_FIELD,
        UPDATE_GOLDEN_PROFILE,
    ):
        _assert_dirties_profile_analysis(query)


def test_recompute_and_batch_updates_can_share_one_mutation_invalidation() -> None:
    batch_query = getattr(queries, "UPDATE_GOLDEN_FIELDS", None)

    assert isinstance(batch_query, str)
    assert "UNWIND $updates AS update" in batch_query
    assert batch_query.count("+ 1") == 1
    assert "$invalidate_analysis" in batch_query
    assert "$invalidate_analysis" in UPDATE_GOLDEN_PROFILE


def test_manual_merge_moves_sales_and_vehicle_context_to_survivor() -> None:
    for relationship_type in ("PURCHASED", "BOUGHT_VEHICLE", "OWNS_VEHICLE"):
        assert f"old_context:{relationship_type}" in EXECUTE_MANUAL_MERGE
        assert f"new_context:{relationship_type}" in EXECUTE_MANUAL_MERGE


def test_manual_merge_context_rewires_preserve_collision_provenance() -> None:
    assert EXECUTE_MANUAL_MERGE.count("relationship_type = 'PURCHASED'") == 1
    assert EXECUTE_MANUAL_MERGE.count("relationship_type = 'BOUGHT_VEHICLE'") == 1
    assert EXECUTE_MANUAL_MERGE.count("relationship_type = 'OWNS_VEHICLE'") == 1
    assert EXECUTE_MANUAL_MERGE.count("moved.created_on_survivor") == 9
    assert EXECUTE_MANUAL_MERGE.count("ON CREATE SET new_context += context_props") == 3
    assert "\n  SET new_context += context_props" not in EXECUTE_MANUAL_MERGE
    assert REVERT_MERGE.count("move:MOVED_RELATIONSHIP") == 9


def test_manual_merge_records_knows_evidence_for_unmerge() -> None:
    assert EXECUTE_MANUAL_MERGE.count("MERGE (me)-[:AFFECTED_RECORD]->(knows_source)") == 2


def test_manual_merge_records_immutable_relationship_provenance() -> None:
    for relationship_type in (
        "LINKED_TO",
        "IDENTIFIED_BY",
        "LIVES_AT",
        "KNOWS_OUT",
        "KNOWS_IN",
        "HAS_FACT",
        "PURCHASED",
        "BOUGHT_VEHICLE",
        "OWNS_VEHICLE",
    ):
        assert f"relationship_type = '{relationship_type}'" in EXECUTE_MANUAL_MERGE
        assert f"move.relationship_type = '{relationship_type}'" in REVERT_MERGE
    assert EXECUTE_MANUAL_MERGE.count("moved:MOVED_RELATIONSHIP") == 9
    assert EXECUTE_MANUAL_MERGE.count("moved.origin_person_id = coalesce(") == 9
    assert "moved_by_merge_event_id" not in REVERT_MERGE


def test_merge_lineage_compression_has_immutable_chain_aware_provenance() -> None:
    assert "old_merge:MERGED_INTO" in EXECUTE_MANUAL_MERGE
    assert "moved_lineage:MOVED_MERGE_LINEAGE" in EXECUTE_MANUAL_MERGE
    assert "moved_lineage.prior_survivor_person_id = absorbed.person_id" in (EXECUTE_MANUAL_MERGE)
    assert "moved_lineage:MOVED_MERGE_LINEAGE" in REVERT_MERGE
    assert "restored_lineage:MERGED_INTO" in REVERT_MERGE
    assert "lineage_person.status = 'merged'" in REVERT_MERGE
    assert "[:MERGED_INTO*0..1]" in REVERT_MERGE
    assert "origin.status = 'merged'" in REVERT_MERGE
    assert "origin = absorbed" in REVERT_MERGE
    assert "WHERE move IS NULL OR origin" not in REVERT_MERGE
    assert REVERT_MERGE.count("AND (origin = absorbed OR origin.status = 'merged')") == 18


def test_unmerge_does_not_delete_context_refreshed_after_merge() -> None:
    assert REVERT_MERGE.count("survivor_context.source_record_pk = move.source_record_pk") == 3


def test_relationship_mutations_dirty_both_active_endpoints() -> None:
    assert "changed_knows:KNOWS" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "changed_person.analysis_input_revision" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "merge_neighbor.analysis_input_revision" in EXECUTE_MANUAL_MERGE
    assert "unmerge_neighbor.analysis_input_revision" in REVERT_MERGE
