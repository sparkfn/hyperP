"""Disposable real-Neo4j adversarial contract for CRM activity cleanup."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from intelligence.repositories.neo4j.crm_activity_cleanup import (
    Neo4jCrmActivityCleanupRepository,
)
from intelligence.repositories.protocols.crm_activity_cleanup import (
    CleanupPlan,
    LiveTargetIdentity,
    ParentIdentity,
)
from neo4j import Driver, GraphDatabase


@pytest.fixture()
def graph() -> Iterator[tuple[Driver, str, str, str, str, str]]:
    uri = os.environ.get("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_URI")
    user = os.environ.get("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_USER")
    password = os.environ.get("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_PASSWORD")
    if not uri or not user or not password:
        pytest.skip("real Neo4j shard environment is absent")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    fixture_id = uuid4().hex
    source_key = f"cleanup-source-{fixture_id}"
    source_instance = f"cleanup-instance-{fixture_id}"
    try:
        with driver.session() as session:
            session.run(
                "CREATE (:SourceSystem {source_key: $source_key, fixture_id: $fixture_id})",
                source_key=source_key,
                fixture_id=fixture_id,
            ).consume()
        yield driver, uri, user, password, source_key, source_instance
    finally:
        with driver.session() as session:
            session.run(
                "MATCH (node {fixture_id: $fixture_id}) DETACH DELETE node",
                fixture_id=fixture_id,
            ).consume()
        driver.close()


def _repository(graph: tuple[Driver, str, str, str, str, str]) -> Neo4jCrmActivityCleanupRepository:
    _, uri, user, password, _, _ = graph
    return Neo4jCrmActivityCleanupRepository(uri, user, password)


def _create_activity(
    graph: tuple[Driver, str, str, str, str, str],
    source_record_pk: str,
    *,
    fixture_id: str,
    source_record_id: str = "activity-id",
) -> LiveTargetIdentity:
    driver, _, _, _, source_key, source_instance = graph
    with driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: $source_key})
            CREATE (record:SourceRecord {
              fixture_id: $fixture_id, source_record_pk: $source_record_pk,
              source_record_id: $source_record_id, source_record_version: '1',
              record_hash: 'activity-hash', source_instance_id: $source_instance,
              record_type: 'crm_history', lifecycle_status: 'active', history_family: 'activity'
            })-[:FROM_SOURCE]->(source)
            """,
            source_key=source_key,
            source_instance=source_instance,
            fixture_id=fixture_id,
            source_record_pk=source_record_pk,
            source_record_id=source_record_id,
        ).consume()
    return LiveTargetIdentity(
        source_record_pk,
        "crm_history",
        source_instance,
        "1",
        "activity-hash",
        "active",
        "activity",
        ParentIdentity(None, None, None, None, None),
        source_key,
    )


def _create_call(
    graph: tuple[Driver, str, str, str, str, str],
    source_record_pk: str,
    activity: LiveTargetIdentity,
    *,
    fixture_id: str,
) -> LiveTargetIdentity:
    driver, _, _, _, source_key, source_instance = graph
    with driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: $source_key})
            MATCH (activity:SourceRecord {source_record_pk: $activity_pk})
            CREATE (call:SourceRecord {
              fixture_id: $fixture_id, source_record_pk: $source_record_pk,
              source_record_id: 'call-id', source_record_version: '1', record_hash: 'call-hash',
              source_instance_id: $source_instance, record_type: 'call', lifecycle_status: 'active',
              parent_source_record_pk: $activity_pk, parent_source_instance_id: $source_instance,
              parent_source_record_id: 'activity-id', parent_record_type: 'crm_history',
              parent_source_system: $source_key
            })-[:FROM_SOURCE]->(source)
            CREATE (call)-[:CHILD_OF]->(activity)
            CREATE (call)-[:DETAILS_HISTORY_ITEM]->(activity)
            """,
            source_key=source_key,
            source_instance=source_instance,
            fixture_id=fixture_id,
            source_record_pk=source_record_pk,
            activity_pk=activity.source_record_pk,
        ).consume()
    return LiveTargetIdentity(
        source_record_pk,
        "call",
        source_instance,
        "1",
        "call-hash",
        "active",
        None,
        ParentIdentity(
            activity.source_record_pk,
            source_instance,
            "activity-id",
            "crm_history",
            source_key,
        ),
        source_key,
        (activity.source_record_pk,),
        (activity.source_record_pk,),
    )


def _plan(
    repository: Neo4jCrmActivityCleanupRepository,
    identities: tuple[LiveTargetIdentity, ...],
) -> CleanupPlan:
    inspections = repository.inspect(tuple(item.source_record_pk for item in identities))
    return repository.plan(identities, inspections)


def _count(driver: Driver, fixture_id: str) -> tuple[int, int]:
    with driver.session() as session:
        node_row = session.run(
            "MATCH (node {fixture_id: $fixture_id}) RETURN count(node) AS count",
            fixture_id=fixture_id,
        ).single()
        relationship_row = session.run(
            """
            MATCH (node {fixture_id: $fixture_id})-[relationship]-()
            RETURN count(DISTINCT relationship) AS count
            """,
            fixture_id=fixture_id,
        ).single()
    assert node_row is not None
    assert relationship_row is not None
    nodes = node_row["count"]
    relationships = relationship_row["count"]
    assert isinstance(nodes, int)
    assert isinstance(relationships, int)
    return nodes, relationships


def test_crm_activities_neo4j_database_identity_and_selected_activity_call_are_deleted_calls_first(
    graph: tuple[Driver, str, str, str, str, str],
) -> None:
    driver, _, _, _, _, _ = graph
    fixture_id = uuid4().hex
    activity = _create_activity(graph, f"a-activity-{fixture_id}", fixture_id=fixture_id)
    call = _create_call(graph, f"b-call-{fixture_id}", activity, fixture_id=fixture_id)
    identities = (activity, call)
    repository = _repository(graph)
    try:
        database_identity = repository.database_identity()
        plan = _plan(repository, identities)
        assert database_identity
        assert tuple(item.target.source_record_pk for item in plan.expected_deletions) == tuple(
            item.source_record_pk for item in identities
        )
        outcome = repository.delete_batch(database_identity, plan.expected_deletions)
        assert outcome.mutation_applied is True
        assert [item.classification for item in outcome.outcomes] == ["deleted", "deleted"]
        inspected = repository.inspect(tuple(item.source_record_pk for item in identities))
    finally:
        repository.close()
    assert [item.matching_node_count for item in inspected] == [0, 0]
    with driver.session() as session:
        row = session.run(
            "MATCH (:SourceSystem {fixture_id: $fixture_id}) RETURN count(*) AS count",
            fixture_id=fixture_id,
        ).single()
    assert row is not None
    assert row["count"] == 1


def test_crm_activities_neo4j_exact_absence_is_not_authorization_for_a_replacement(
    graph: tuple[Driver, str, str, str, str, str],
) -> None:
    _, _, _, _, source_key, source_instance = graph
    identity = LiveTargetIdentity(
        f"a-absent-{uuid4().hex}",
        "crm_history",
        source_instance,
        "1",
        "hash",
        "active",
        "activity",
        ParentIdentity(None, None, None, None, None),
        source_key,
    )
    repository = _repository(graph)
    try:
        plan = _plan(repository, (identity,))
    finally:
        repository.close()
    assert plan.expected_deletions == ()
    assert plan.outcomes[0].classification == "already_absent"


def test_crm_activities_neo4j_companion_parent_relationship_drift_is_conflict(
    graph: tuple[Driver, str, str, str, str, str],
) -> None:
    driver, _, _, _, _, _ = graph
    fixture_id = uuid4().hex
    activity = _create_activity(graph, f"a-parent-{fixture_id}", fixture_id=fixture_id)
    call = _create_call(graph, f"b-parent-{fixture_id}", activity, fixture_id=fixture_id)
    with driver.session() as session:
        session.run(
            "MATCH (:SourceRecord {source_record_pk: $call_pk})"
            "-[relationship:DETAILS_HISTORY_ITEM]->() DELETE relationship",
            call_pk=call.source_record_pk,
        ).consume()
    repository = _repository(graph)
    try:
        plan = _plan(repository, (activity, call))
    finally:
        repository.close()
    outcome = {item.source_record_pk: item for item in plan.outcomes}[call.source_record_pk]
    assert outcome.classification == "conflict"
    assert outcome.reason_code == "companion_parent_relationship_drift"


@pytest.mark.parametrize(
    ("property_name", "replacement", "expected_reason"),
    (
        ("record_type", "crm_deal", "wrong_record_type"),
        ("source_instance_id", "wrong-instance", "wrong_source_instance"),
        ("source_record_version", "2", "wrong_source_record_version"),
        ("record_hash", "wrong-hash", "wrong_record_hash"),
        ("lifecycle_status", "retired", "wrong_lifecycle_status"),
        ("history_family", "stage", "wrong_history_family"),
        ("parent_source_record_id", "wrong-parent", "stored_parent_drift"),
    ),
)
def test_crm_activities_neo4j_exact_identity_drift_is_conflict_without_mutation(
    graph: tuple[Driver, str, str, str, str, str],
    property_name: str,
    replacement: str,
    expected_reason: str,
) -> None:
    driver, _, _, _, _, _ = graph
    fixture_id = uuid4().hex
    activity = _create_activity(graph, f"a-drift-{fixture_id}", fixture_id=fixture_id)
    before = _count(driver, fixture_id)
    with driver.session() as session:
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: $source_record_pk}) "
            "SET record += $changes",
            source_record_pk=activity.source_record_pk,
            changes={property_name: replacement},
        ).consume()
    repository = _repository(graph)
    try:
        plan = _plan(repository, (activity,))
    finally:
        repository.close()
    after = _count(driver, fixture_id)
    assert plan.expected_deletions == ()
    assert plan.outcomes[0].classification == "conflict"
    assert plan.outcomes[0].reason_code == expected_reason
    assert before == after


def test_crm_activities_neo4j_wrong_label_is_conflict_without_type_prefilter(
    graph: tuple[Driver, str, str, str, str, str],
) -> None:
    driver, _, _, _, source_key, source_instance = graph
    fixture_id = uuid4().hex
    identity = LiveTargetIdentity(
        f"a-label-{fixture_id}",
        "crm_history",
        source_instance,
        "1",
        "hash",
        "active",
        "activity",
        ParentIdentity(None, None, None, None, None),
        source_key,
    )
    with driver.session() as session:
        session.run(
            """
            CREATE (:WrongLabel {
              fixture_id: $fixture_id, source_record_pk: $source_record_pk,
              record_type: 'crm_history',
              source_instance_id: $source_instance, source_record_version: '1', record_hash: 'hash',
              lifecycle_status: 'active', history_family: 'activity'
            })
            """,
            fixture_id=fixture_id,
            source_record_pk=identity.source_record_pk,
            source_instance=source_instance,
        ).consume()
    repository = _repository(graph)
    try:
        plan = _plan(repository, (identity,))
    finally:
        repository.close()
    assert plan.outcomes[0].classification == "conflict"
    assert plan.outcomes[0].reason_code == "wrong_labels"


def test_crm_activities_neo4j_unowned_dependencies_are_retained_and_protected_evidence_survives(
    graph: tuple[Driver, str, str, str, str, str],
) -> None:
    driver, _, _, _, source_key, source_instance = graph
    fixture_id = uuid4().hex
    activity = _create_activity(graph, f"a-protected-{fixture_id}", fixture_id=fixture_id)
    with driver.session() as session:
        session.run(
            """
            MATCH (activity:SourceRecord {source_record_pk: $activity_pk})
            CREATE (deal:SourceRecord {fixture_id: $fixture_id, source_record_pk: $deal_pk,
              record_type: 'crm_deal'})
            CREATE (stage:SourceRecord {fixture_id: $fixture_id, source_record_pk: $stage_pk,
              record_type: 'crm_history', history_family: 'stage'})
            CREATE (conversation:SourceRecord {fixture_id: $fixture_id,
              source_record_pk: $conversation_pk,
              record_type: 'conversation'})
            CREATE (person:Person {fixture_id: $fixture_id, person_id: $person_id})
            CREATE (identifier:Identifier {fixture_id: $fixture_id, identifier_type: 'phone',
              normalized_value: $identifier_value})
            CREATE (review:ReviewCase {fixture_id: $fixture_id, review_case_id: $review_case_id})
            CREATE (decision:MatchDecision {fixture_id: $fixture_id,
              match_decision_id: $match_decision_id})
            CREATE (unrelated:SourceRecord {fixture_id: $fixture_id,
              source_record_pk: $unrelated_pk,
              record_type: 'crm_deal', source_instance_id: $source_instance})
            CREATE (activity)-[:CHILD_OF]->(deal)
            CREATE (activity)-[:RELATES_STAGE]->(stage)
            CREATE (activity)-[:LINKED_TO]->(conversation)
            CREATE (activity)-[:LINKED_TO]->(person)
            CREATE (activity)-[:USES_IDENTIFIER]->(identifier)
            CREATE (activity)<-[:FOR_RECORD]-(review)
            CREATE (activity)<-[:ABOUT_RECORD]-(decision)
            CREATE (activity)<-[:UNRELATED_EDGE]-(unrelated)
            """,
            activity_pk=activity.source_record_pk,
            fixture_id=fixture_id,
            deal_pk=f"deal-{fixture_id}",
            stage_pk=f"stage-{fixture_id}",
            conversation_pk=f"conversation-{fixture_id}",
            person_id=f"person-{fixture_id}",
            identifier_value=f"identifier-{fixture_id}",
            review_case_id=f"review-{fixture_id}",
            match_decision_id=f"decision-{fixture_id}",
            unrelated_pk=f"unrelated-{fixture_id}",
            source_instance=source_instance,
        ).consume()
    before = _count(driver, fixture_id)
    repository = _repository(graph)
    try:
        plan = _plan(repository, (activity,))
        missing = repository.verify_protected(plan.protected_evidence)
    finally:
        repository.close()
    after = _count(driver, fixture_id)
    labels = {item.relationship.other_endpoint.labels for item in plan.protected_evidence}
    assert plan.expected_deletions == ()
    assert plan.outcomes[0].classification == "retained"
    assert missing == ()
    assert {"SourceRecord", "Person", "Identifier", "ReviewCase", "MatchDecision"}.issubset(
        {label for labels_item in labels for label in labels_item}
    )
    assert before == after


def test_crm_activities_neo4j_mixed_batch_revalidation_rolls_back_without_mutation(
    graph: tuple[Driver, str, str, str, str, str],
) -> None:
    driver, _, _, _, _, _ = graph
    fixture_id = uuid4().hex
    first = _create_activity(graph, f"a-first-{fixture_id}", fixture_id=fixture_id)
    second = _create_activity(graph, f"b-second-{fixture_id}", fixture_id=fixture_id)
    identities = (first, second)
    before = _count(driver, fixture_id)
    repository = _repository(graph)
    try:
        database_identity = repository.database_identity()
        plan = _plan(repository, identities)
        assert len(plan.expected_deletions) == 2
        with driver.session() as session:
            session.run(
                "MATCH (record:SourceRecord {source_record_pk: $source_record_pk}) "
                "SET record.record_hash = 'changed'",
                source_record_pk=second.source_record_pk,
            ).consume()
        outcome = repository.delete_batch(database_identity, plan.expected_deletions)
    finally:
        repository.close()
    assert outcome.mutation_applied is False
    assert [item.classification for item in outcome.outcomes] == ["retained", "conflict"]
    assert _count(driver, fixture_id) == before
    with driver.session() as session:
        row = session.run(
            "MATCH (record:SourceRecord {fixture_id: $fixture_id}) RETURN count(record) AS count",
            fixture_id=fixture_id,
        ).single()
    assert row is not None
    assert row["count"] == 2
