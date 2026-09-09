"""Graph-only unit contracts for manifest-gated cleanup planning."""

from __future__ import annotations

from inspect import signature

from intelligence.graph.queries.crm_activity_cleanup import (
    DELETE_NODES_BY_ELEMENT_IDS,
    DELETE_RELATIONSHIPS_BY_OWNERS,
    INSPECT_IDENTITIES,
    LOCK_IDENTITIES_FOR_REVALIDATION,
    LOCK_SOURCE_SYSTEMS_FOR_REVALIDATION,
    VERIFY_PROTECTED,
)
from intelligence.repositories.neo4j.crm_activity_cleanup import Neo4jCrmActivityCleanupRepository
from intelligence.repositories.protocols.crm_activity_cleanup import (
    BatchOutcome,
    ExactRecordInspection,
    LiveTargetIdentity,
    ParentIdentity,
    RecordOutcome,
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


def test_empty_identity_set_is_a_valid_no_op_plan() -> None:
    repository = object.__new__(Neo4jCrmActivityCleanupRepository)
    plan = repository.plan((), ())
    assert plan.expected_deletions == ()
    assert plan.protected_evidence == ()
    assert plan.outcomes == ()


def test_required_absence_batch_requires_canonical_typed_absence_outcomes() -> None:
    parameter = signature(
        Neo4jCrmActivityCleanupRepository.delete_batch_with_required_absences
    ).parameters["required_absent"]
    assert parameter.default is parameter.empty
    outcome = BatchOutcome(
        "database-a",
        (
            RecordOutcome("activity-a", "deleted", "deleted_after_revalidation"),
            RecordOutcome("activity-b", "already_absent", "absent_at_mutation"),
        ),
        True,
    )
    assert tuple(item.source_record_pk for item in outcome.outcomes) == ("activity-a", "activity-b")


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
    assert "normalized_value" not in inspection
    assert "normalized_value" not in VERIFY_PROTECTED.lower()
    assert "other_identifier_comparison_token" in INSPECT_IDENTITIES
    assert (
        "set record.source_record_pk = record.source_record_pk"
        in LOCK_IDENTITIES_FOR_REVALIDATION.lower()
    )
    assert "source:SourceSystem {source_key: source_key}" in LOCK_SOURCE_SYSTEMS_FOR_REVALIDATION
    assert (
        "set source.source_key = source.source_key" in LOCK_SOURCE_SYSTEMS_FOR_REVALIDATION.lower()
    )
