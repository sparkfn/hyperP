"""Structural guardrails for mapping topology and active-reader isolation."""

from __future__ import annotations

from src.graph.queries import crm_tenant_mapping as queries


def test_strict_revision_reader_starts_from_exact_revision_and_checks_topology() -> None:
    query = queries.READ_REVISION.upper()
    topology = queries.READ_TOPOLOGY_VIOLATIONS.upper()

    assert "REVISION_ID: $REVISION_ID" in query
    assert "MANIFEST_DIGEST = $MANIFEST_DIGEST" in query
    assert "HAS_MAPPING_ENTRY" in query
    assert "HAS_MAPPING_TARGET" in query
    assert "TARGETS_ENTITY" in query
    assert "BAD_REVISION_LINKS" in topology
    assert "BAD_ENTRY_LINKS" in topology
    assert "BAD_TARGET_LINKS" in topology
    assert "ORPHAN_ENTRIES" in topology
    assert "ORPHAN_TARGETS" in topology
    assert "BAD_ENTRY_OWNERS" in topology
    assert "BAD_TARGET_OWNERS" in topology
    assert "OWNER = REVISION" in topology
    assert "OWNER = ENTRY" in topology
    assert topology.count("OPTIONAL MATCH (ENTRY:CRMTENANTMAPPINGENTRY") >= 2
    assert "WHEN ENTRY IS NULL THEN NULL" in topology
    assert "WHEN TARGET IS NULL THEN NULL" in topology


def test_mapping_write_queries_leave_active_heads_and_people_untouched() -> None:
    writes = "\n".join(
        (
            queries.ALLOCATE_REVISION_NUMBER,
            queries.LOCK_SCOPE,
            queries.CHECK_REVISION_ID,
            queries.CREATE_REVISION,
            queries.CREATE_ENTRIES,
            queries.CREATE_TARGETS,
            queries.REJECT_REVISION,
        )
    ).upper()

    assert "CRMTENANTMAPPINGACTIVEHEAD" not in writes
    assert "PERSON" not in writes
    assert "DELETE" not in writes
    assert "MERGE (ENTITY" not in writes
