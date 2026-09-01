"""Projection write-boundary checks for Issue #305."""

from __future__ import annotations

import pytest
from src.graph import crm_tenant_projection_write as projection_write
from src.graph.queries import crm_tenant_projection as queries
from src.graph.queries import crm_tenant_projection_integrity as integrity_queries
from src.graph.queries import crm_tenant_projection_projection as projection_queries

_DIGEST = "sha256:" + "a" * 64


def test_projection_queries_do_not_write_active_heads_or_source_membership_state() -> None:
    writes = (
        queries.CREATE_RELEASE,
        queries.WRITE_INPUTS,
        queries.ADVANCE_CAPTURE,
        projection_queries.WRITE_ASSOCIATIONS,
        projection_queries.WRITE_DECISION,
        projection_queries.ADVANCE_PROJECTION,
        integrity_queries.COMPLETE_RELEASE,
        integrity_queries.CANCEL_RELEASE,
        integrity_queries.FAIL_RELEASE,
    )
    forbidden = (
        "CREATE (head:CrmTenantProjectionActiveHead",
        "CREATE (head:CrmTenantMappingActiveHead",
        "SET head.",
        "CREATE (head:CrmCompanyMembershipHead",
        "CREATE (snapshot:CrmCompanyMembershipSnapshot",
        "CREATE (observation:CrmCompanyMembershipObservation",
        "CREATE (entity:Entity",
        "CREATE (person:Person",
    )
    for query in writes:
        assert all(token not in query for token in forbidden)
    assert "MATERIALIZES_MAPPING_REVISION" in queries.CREATE_RELEASE
    assert "MATERIALIZES_SOURCE_CENSUS" in queries.CREATE_RELEASE


def test_capture_cursor_filter_is_applied_after_optional_checkpoint_matching() -> None:
    query = queries.CAPTURE_CANDIDATES
    optional_checkpoint = query.index("OPTIONAL MATCH (checkpoint:StandaloneCrmCensusCheckpoint")
    scope_boundary = query.index(
        "WITH release, census, head, snapshot, unit, checkpoint",
        optional_checkpoint,
    )
    cursor_filter = query.index("WHERE head.subject_kind IN ['contact', 'lead']")

    assert optional_checkpoint < scope_boundary < cursor_filter


def test_completion_boundary_retains_census_for_checkpoint_uniqueness_checks() -> None:
    query = integrity_queries.COMPLETE_RELEASE
    completion_scope = query.index(
        "WITH DISTINCT release, census, contact, lead, contact_checkpoint, lead_checkpoint"
    )
    checkpoint_guard = query.index("census_id: census.census_id", completion_scope)

    assert completion_scope < checkpoint_guard


def test_completion_query_authorizes_ledger_integrity_atomically() -> None:
    query = integrity_queries.COMPLETE_RELEASE

    assert "actual_input_count = release.input_count" in query
    assert "actual_decision_count = release.decision_count" in query
    assert "actual_association_count = release.association_count" in query
    assert "actual_support_count = release.support_count" in query
    assert "WITH release, revision, actual_input_count" in query
    assert "CASE WHEN owner = release THEN owner END" in query
    assert "owner:CrmTenantProjectionRelease" not in query
    assert "input_ids <> [decision.input_id]" in query
    assert "input_ids <> [association.input_id]" in query
    assert "input_release_ids <> [release.release_id]" in query
    assert "entity_keys <> [association.entity_key]" in query
    assert "support_release_ids <> [release.release_id]" in query
    assert "association_release_ids <> [release.release_id]" in query
    assert "association_ids <> [support.association_id]" in query
    assert "observation_ids <> [support.membership_observation_id]" in query
    assert "target_ids <> [support.mapping_target_id]" in query
    assert "owned_input.release_id <> release.release_id" in query
    assert "snapshot_digests <> [input.snapshot_digest]" in query
    assert "snapshot_subject_kinds <> [input.subject_kind]" in query
    assert "snapshot_binding_counts <> [0]" in query
    assert "snapshot_binding_counts = [0]" in query
    assert "input_subject_kinds <> [association.subject_kind]" in query
    assert "input_subject_ids <> [association.subject_id]" in query
    assert "association.relationship_kind <> 'tenant_member'" in query
    assert "observation_snapshot_ids <> snapshot_ids" in query
    assert "snapshot_ids <> input_snapshot_ids" in query
    assert "observation_subject_kinds <> input_subject_kinds" in query
    assert "observation_subject_ids <> input_subject_ids" in query
    assert "entry_revision_ids <> [release.mapping_revision_id]" in query
    assert "entry_company_ids <> observation_company_ids" in query
    assert "entity_keys <> association_entity_keys" in query
    assert "target_relationship_kinds <> association_relationship_kinds" in query
    assert "size(input.input_digest) <> 71" in query
    assert "size(snapshot_digests[0]) <> 64" in query
    assert "snapshot_digests[0] =~ '^[0-9a-f]{64}$'" in query
    assert "size(input.snapshot_digest) <> 64" in query
    assert "input.snapshot_digest =~ '^[0-9a-f]{64}$'" in query
    assert "size(decision.decision_digest) <> 71" in query
    assert "size(support.support_digest) <> 71" in query
    assert (
        "decision.decision IS NULL OR NOT (decision.decision IN ['associated', 'zero_target'])"
        in query
    )
    assert "decision.zero_target_reason IS NOT NULL" in query
    assert "decision.zero_target_reason IS NULL" in query
    assert "NOT (decision.zero_target_reason IN ['empty_membership', 'no_mapped_targets'])" in query
    support_guard = query[query.index("MATCH (support:CrmTenantProjectionSupport") :]
    support_where = support_guard[support_guard.index("WHERE associations <> 1") :]
    assert "collect(DISTINCT input.snapshot_id) AS input_snapshot_ids" in support_guard
    assert "collect(DISTINCT association.entity_key) AS association_entity_keys" in support_guard
    assert (
        "collect(DISTINCT association.relationship_kind) AS association_relationship_kinds"
        in support_guard
    )
    assert "snapshot_ids <> input_snapshot_ids" in support_where
    assert "entity_keys <> association_entity_keys" in support_where
    assert "target_relationship_kinds <> association_relationship_kinds" in support_where
    assert "input." not in support_where
    assert "association." not in support_where
    assert query.index("actual_input_count = release.input_count") < query.index(
        "SET release.state = 'completed'"
    )


def test_projection_support_read_is_hard_limited_after_deterministic_ordering() -> None:
    query = projection_queries.READ_INPUT_SUPPORTS
    preflight = projection_queries.READ_INPUT_SUPPORT_BOUND

    assert "ORDER BY observation.observation_id, target.target_id" in query
    assert "LIMIT $support_row_limit" in query
    assert query.index("ORDER BY observation.observation_id, target.target_id") < query.index(
        "LIMIT $support_row_limit"
    )
    assert "snapshot.binding_count AS binding_count" in preflight
    assert "count(*) AS support_row_count" in preflight
    assert "LIMIT $support_row_limit" in preflight
    assert "ORDER BY" not in preflight
    assert "release.mapping_target_count" not in preflight
    assert "OPTIONAL MATCH (snapshot)-[:HAS_MEMBERSHIP_OBSERVATION]" in preflight
    assert "OPTIONAL MATCH (revision)-[:HAS_MAPPING_ENTRY]" in preflight


def test_failure_code_is_rejected_before_any_graph_write() -> None:
    class _Result:
        def single(self) -> dict[str, object]:
            return {}

    class _Tx:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def run(self, query: str, **parameters: object) -> _Result:
            self.calls.append((query, parameters))
            return _Result()

    tx = _Tx()
    with pytest.raises(ValueError, match="unsupported projection failure code"):
        projection_write._fail_release(tx, "release", _DIGEST, "x" * 129)
    assert tx.calls == []
