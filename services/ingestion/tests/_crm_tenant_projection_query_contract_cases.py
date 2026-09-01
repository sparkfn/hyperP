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
