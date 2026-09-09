"""Graph-only unit contracts for manifest-gated cleanup planning."""

from __future__ import annotations

from intelligence.graph.queries.crm_activity_cleanup import (
    DELETE_NODES_BY_ELEMENT_IDS,
    DELETE_RELATIONSHIPS_BY_OWNERS,
    INSPECT_IDENTITIES,
)
from intelligence.repositories.neo4j.crm_activity_cleanup import Neo4jCrmActivityCleanupRepository
from intelligence.repositories.protocols.crm_activity_cleanup import (
    ExactRecordInspection,
    LiveTargetIdentity,
    ParentIdentity,
)


def _identity(source_record_pk: str = "activity-a") -> LiveTargetIdentity:
    return LiveTargetIdentity(
        source_record_pk,
        "crm_history",
        "bitrix-primary",
        "1",
        "hash-a",
        "active",
        "activity",
        ParentIdentity(None, None, None, None, None),
        "bitrix_chat",
    )


def test_absent_identity_is_a_typed_already_absent_plan_not_a_deletion() -> None:
    repository = object.__new__(Neo4jCrmActivityCleanupRepository)
    identity = _identity()
    plan = repository.plan(
        (identity,),
        (ExactRecordInspection(identity.source_record_pk, 0, None, (), 0, False),),
    )
    assert plan.expected_deletions == ()
    assert plan.outcomes[0].classification == "already_absent"


def test_cleanup_queries_are_closed_parameterized_and_never_detach_delete() -> None:
    inspection = INSPECT_IDENTITIES.lower()
    deletes = (DELETE_RELATIONSHIPS_BY_OWNERS + DELETE_NODES_BY_ELEMENT_IDS).lower()
    assert "$source_record_pks" in INSPECT_IDENTITIES
    assert "$relationship_limit_plus_one" in INSPECT_IDENTITIES
    assert "optional match (candidate {source_record_pk: source_record_pk})" in inspection
    assert "detach delete" not in deletes
    assert "delete relationship" in deletes
    assert "delete record" in deletes
    assert "elementid(relationship)" in deletes
    assert "match ()-[relationship]->()" not in deletes
    assert "elementid(record)" in deletes
