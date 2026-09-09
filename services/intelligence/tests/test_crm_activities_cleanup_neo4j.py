"""Disposable real-Neo4j adversarial contract for CRM activity cleanup."""

from __future__ import annotations

import os
from collections.abc import Iterator
from threading import Event, Thread
from uuid import uuid4

import pytest
from intelligence.artifacts import canonical_json
from intelligence.crm.activities.cleanup.receipt import _protected_dict
from intelligence.repositories.neo4j.crm_activity_cleanup import (
    Neo4jCrmActivityCleanupRepository,
)
from intelligence.repositories.protocols.crm_activity_cleanup import (
    BatchOutcome,
    CleanupPlan,
    LiveTargetIdentity,
    ParentIdentity,
    RecordOutcome,
)
from neo4j import Driver, GraphDatabase

GraphFixture = tuple[Driver, str, str, str, str, str, str]


@pytest.fixture()
def graph() -> Iterator[GraphFixture]:
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
        yield driver, uri, user, password, source_key, source_instance, fixture_id
    finally:
        with driver.session() as session:
            session.run(
                "MATCH (node {fixture_id: $fixture_id}) DETACH DELETE node",
                fixture_id=fixture_id,
            ).consume()
        driver.close()


def _repository(graph: GraphFixture) -> Neo4jCrmActivityCleanupRepository:
    _, uri, user, password, _, _, _ = graph
    return Neo4jCrmActivityCleanupRepository(uri, user, password)


def _create_activity(
    graph: GraphFixture,
    source_record_pk: str,
    *,
    source_record_id: str = "activity-id",
    source_version_key: str = "activity-v1",
    history_family: str = "activity",
    history_source: str | None = "bitrix_crm_activity",
    projection_source: str | None = "bitrix_crm_activity_v2",
    projection_version: str | None = "2",
) -> LiveTargetIdentity:
    driver, _, _, _, source_key, source_instance, fixture_id = graph
    with driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: $source_key})
            CREATE (record:SourceRecord {
              fixture_id: $fixture_id, source_record_pk: $source_record_pk,
              source_record_id: $source_record_id, source_record_version: '1',
              source_version_key: $source_version_key,
              record_hash: 'activity-hash', source_instance_id: $source_instance,
              record_type: 'crm_history', lifecycle_status: 'active',
              history_family: $history_family,
              history_source: $history_source, projection_source: $projection_source,
              projection_version: $projection_version
            })-[:FROM_SOURCE]->(source)
            """,
            source_key=source_key,
            source_instance=source_instance,
            fixture_id=fixture_id,
            source_record_pk=source_record_pk,
            source_record_id=source_record_id,
            source_version_key=source_version_key,
            history_family=history_family,
            history_source=history_source,
            projection_source=projection_source,
            projection_version=projection_version,
        ).consume()
    return LiveTargetIdentity(
        source_record_pk,
        "crm_history",
        source_instance,
        "1",
        "activity-hash",
        "active",
        history_family,
        ParentIdentity(None, None, None, None, None),
        source_key,
        source_record_id=source_record_id,
        source_version_key=source_version_key,
        history_source=history_source,
        projection_source=projection_source,
        projection_version=projection_version,
    )


def _create_call(
    graph: GraphFixture,
    source_record_pk: str,
    activity: LiveTargetIdentity,
) -> LiveTargetIdentity:
    driver, _, _, _, source_key, source_instance, fixture_id = graph
    with driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: $source_key})
            MATCH (activity:SourceRecord {source_record_pk: $activity_pk})
            CREATE (call:SourceRecord {
              fixture_id: $fixture_id, source_record_pk: $source_record_pk,
              source_record_id: 'call-id', source_record_version: '1', record_hash: 'call-hash',
              source_version_key: 'call-v1',
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
        source_record_id="call-id",
        source_version_key="call-v1",
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
    graph: GraphFixture,
) -> None:
    driver, _, _, _, _, _, fixture_id = graph
    activity = _create_activity(graph, f"a-activity-{fixture_id}")
    call = _create_call(graph, f"b-call-{fixture_id}", activity)
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
    graph: GraphFixture,
) -> None:
    _, _, _, _, source_key, source_instance, _ = graph
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


def test_crm_activities_neo4j_all_absent_batch_observes_absence_without_graph_mutation(
    graph: GraphFixture,
) -> None:
    driver, _, _, _, source_key, source_instance, fixture_id = graph
    identity = LiveTargetIdentity(
        f"a-all-absent-{fixture_id}",
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
        database_identity = repository.database_identity()
        before = repository.inspect((identity.source_record_pk,))
        plan = repository.plan((identity,), before)
        outcome = repository.delete_batch_with_required_absences(
            database_identity,
            plan.expected_deletions,
            (identity,),
        )
        after = repository.inspect((identity.source_record_pk,))
    finally:
        repository.close()
    assert before[0].matching_node_count == 0
    assert plan.outcomes[0].classification == "already_absent"
    assert outcome == BatchOutcome(
        database_identity,
        (RecordOutcome(identity.source_record_pk, "already_absent", "absent_at_mutation"),),
        False,
    )
    assert after[0].matching_node_count == 0
    assert _count(driver, fixture_id) == (1, 0)


def test_crm_activities_neo4j_ready_and_required_absent_identities_commit_together(
    graph: GraphFixture,
) -> None:
    driver, _, _, _, source_key, source_instance, fixture_id = graph
    activity = _create_activity(graph, f"a-ready-{fixture_id}")
    absent = LiveTargetIdentity(
        f"b-absent-{fixture_id}",
        "crm_history",
        source_instance,
        "1",
        "activity-hash",
        "active",
        "activity",
        ParentIdentity(None, None, None, None, None),
        source_key,
        source_record_id="activity-id",
        source_version_key="activity-v1",
        history_source="bitrix_crm_activity",
        projection_source="bitrix_crm_activity_v2",
        projection_version="2",
    )
    repository = _repository(graph)
    try:
        database_identity = repository.database_identity()
        plan = _plan(repository, (activity,))
        outcome = repository.delete_batch_with_required_absences(
            database_identity,
            plan.expected_deletions,
            (absent,),
        )
        inspected = repository.inspect((activity.source_record_pk, absent.source_record_pk))
    finally:
        repository.close()
    assert [item.classification for item in outcome.outcomes] == ["deleted", "already_absent"]
    assert outcome.mutation_applied is True
    assert [item.matching_node_count for item in inspected] == [0, 0]
    assert _count(driver, fixture_id) == (1, 0)


def test_crm_activities_neo4j_reappeared_required_absence_rolls_back_ready_deletion(
    graph: GraphFixture,
) -> None:
    driver, _, _, _, _, _, fixture_id = graph
    activity = _create_activity(graph, f"a-ready-{fixture_id}")
    reappeared = _create_activity(graph, f"b-reappeared-{fixture_id}")
    before = _count(driver, fixture_id)
    repository = _repository(graph)
    try:
        database_identity = repository.database_identity()
        plan = _plan(repository, (activity,))
        outcome = repository.delete_batch_with_required_absences(
            database_identity,
            plan.expected_deletions,
            (reappeared,),
        )
    finally:
        repository.close()
    outcomes = {item.source_record_pk: item for item in outcome.outcomes}
    assert outcome.mutation_applied is False
    assert outcomes[activity.source_record_pk] == RecordOutcome(
        activity.source_record_pk,
        "retained",
        "batch_rolled_back_due_to_required_absence_conflict",
    )
    assert outcomes[reappeared.source_record_pk] == RecordOutcome(
        reappeared.source_record_pk,
        "conflict",
        "required_absence_reappeared",
    )
    assert _count(driver, fixture_id) == before


def test_crm_activities_neo4j_legacy_activity_projection_drift_is_conflict(
    graph: GraphFixture,
) -> None:
    driver, _, _, _, _, _, fixture_id = graph
    activity = _create_activity(
        graph,
        f"a-legacy-{fixture_id}",
        history_family="crm_activity",
        history_source="bitrix_crm_activity",
        projection_source="bitrix_crm_activity_v1",
        projection_version="1",
    )
    with driver.session() as session:
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: $source_record_pk}) "
            "SET record.projection_source = 'unsupported-projection'",
            source_record_pk=activity.source_record_pk,
        ).consume()
    repository = _repository(graph)
    try:
        plan = _plan(repository, (activity,))
    finally:
        repository.close()
    assert plan.expected_deletions == ()
    assert plan.outcomes[0].classification == "conflict"
    assert plan.outcomes[0].reason_code == "wrong_projection_source"


def test_crm_activities_neo4j_companion_parent_relationship_drift_is_conflict(
    graph: GraphFixture,
) -> None:
    driver, _, _, _, _, _, fixture_id = graph
    activity = _create_activity(graph, f"a-parent-{fixture_id}")
    call = _create_call(graph, f"b-parent-{fixture_id}", activity)
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
        ("source_record_id", "wrong-id", "wrong_source_record_id"),
        ("source_record_version", "2", "wrong_source_record_version"),
        ("source_version_key", "wrong-version-key", "wrong_source_version_key"),
        ("record_hash", "wrong-hash", "wrong_record_hash"),
        ("lifecycle_status", "retired", "wrong_lifecycle_status"),
        ("history_family", "stage", "wrong_history_family"),
        ("history_source", "wrong-history-source", "wrong_history_source"),
        ("projection_source", "wrong-projection-source", "wrong_projection_source"),
        ("projection_version", "99", "wrong_projection_version"),
        ("parent_source_record_id", "wrong-parent", "stored_parent_drift"),
    ),
)
def test_crm_activities_neo4j_exact_identity_drift_is_conflict_without_mutation(
    graph: GraphFixture,
    property_name: str,
    replacement: str,
    expected_reason: str,
) -> None:
    driver, _, _, _, _, _, fixture_id = graph
    activity = _create_activity(graph, f"a-drift-{fixture_id}")
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
    graph: GraphFixture,
) -> None:
    driver, _, _, _, source_key, source_instance, fixture_id = graph
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
    graph: GraphFixture,
) -> None:
    driver, _, _, _, source_key, source_instance, fixture_id = graph
    activity = _create_activity(graph, f"a-protected-{fixture_id}")
    sensitive_identifier = f"sensitive-identifier-{fixture_id}"
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
            identifier_value=sensitive_identifier,
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
    identifier_evidence = next(
        item
        for item in plan.protected_evidence
        if item.relationship.other_endpoint.labels == ("Identifier",)
    )
    serialized = canonical_json(_protected_dict(identifier_evidence))
    assert plan.expected_deletions == ()
    assert plan.outcomes[0].classification == "retained"
    assert missing == ()
    assert {"SourceRecord", "Person", "Identifier", "ReviewCase", "MatchDecision"}.issubset(
        {label for labels_item in labels for label in labels_item}
    )
    assert identifier_evidence.relationship.other_endpoint.identifier_comparison_token
    assert sensitive_identifier not in serialized
    assert before == after


def test_crm_activities_neo4j_mixed_batch_revalidation_rolls_back_without_mutation(
    graph: GraphFixture,
) -> None:
    driver, _, _, _, _, _, fixture_id = graph
    first = _create_activity(graph, f"a-first-{fixture_id}")
    second = _create_activity(graph, f"b-second-{fixture_id}")
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


def test_crm_activities_neo4j_write_lock_blocks_post_inspection_mutation_until_deletion(
    graph: GraphFixture,
) -> None:
    driver, uri, user, password, _, _, fixture_id = graph
    activity = _create_activity(graph, f"a-race-{fixture_id}")
    lock_acquired = Event()
    allow_revalidation = Event()
    mutation_started = Event()
    mutation_finished = Event()
    outcomes: list[BatchOutcome] = []
    cleanup_failures: list[BaseException] = []
    mutation_failures: list[BaseException] = []

    def pause_after_lock() -> None:
        lock_acquired.set()
        if not allow_revalidation.wait(timeout=5):
            raise RuntimeError("test did not release cleanup revalidation")

    repository = Neo4jCrmActivityCleanupRepository(
        uri,
        user,
        password,
    )
    repository._before_revalidation = pause_after_lock
    database_identity = repository.database_identity()
    plan = _plan(repository, (activity,))

    def delete() -> None:
        try:
            outcomes.append(repository.delete_batch(database_identity, plan.expected_deletions))
        except BaseException as error:
            cleanup_failures.append(error)

    def mutate() -> None:
        mutation_started.set()
        try:
            with driver.session() as session:
                session.run(
                    "MATCH (record:SourceRecord {source_record_pk: $source_record_pk}) "
                    "SET record.record_hash = 'raced-after-lock'",
                    source_record_pk=activity.source_record_pk,
                ).consume()
        except BaseException as error:
            mutation_failures.append(error)
        finally:
            mutation_finished.set()

    deleter = Thread(target=delete)
    mutator = Thread(target=mutate)
    try:
        deleter.start()
        assert lock_acquired.wait(timeout=5)
        mutator.start()
        assert mutation_started.wait(timeout=5)
        assert not mutation_finished.wait(timeout=0.25)
        allow_revalidation.set()
        deleter.join(timeout=5)
        mutator.join(timeout=5)
    finally:
        allow_revalidation.set()
        deleter.join(timeout=5)
        mutator.join(timeout=5)
        repository.close()
    assert not deleter.is_alive()
    assert not mutator.is_alive()
    assert cleanup_failures == []
    assert mutation_failures == []
    assert outcomes == [
        BatchOutcome(
            database_identity,
            (RecordOutcome(activity.source_record_pk, "deleted", "deleted_after_revalidation"),),
            True,
        )
    ]
    with driver.session() as session:
        row = session.run(
            "MATCH (record:SourceRecord {source_record_pk: $source_record_pk}) "
            "RETURN count(record) AS count",
            source_record_pk=activity.source_record_pk,
        ).single()
    assert row is not None
    assert row["count"] == 0


def test_crm_activities_neo4j_source_lock_serializes_future_required_absence_creation(
    graph: GraphFixture,
) -> None:
    driver, uri, user, password, source_key, source_instance, fixture_id = graph
    activity = _create_activity(graph, f"a-source-lock-{fixture_id}")
    future = LiveTargetIdentity(
        f"b-future-{fixture_id}",
        "crm_history",
        source_instance,
        "1",
        "activity-hash",
        "active",
        "activity",
        ParentIdentity(None, None, None, None, None),
        source_key,
        source_record_id="activity-id",
        source_version_key="activity-v1",
        history_source="bitrix_crm_activity",
        projection_source="bitrix_crm_activity_v2",
        projection_version="2",
    )
    lock_acquired = Event()
    allow_revalidation = Event()
    creation_started = Event()
    creation_finished = Event()
    outcomes: list[BatchOutcome] = []
    cleanup_failures: list[BaseException] = []
    creation_failures: list[BaseException] = []

    def pause_after_lock() -> None:
        lock_acquired.set()
        if not allow_revalidation.wait(timeout=5):
            raise RuntimeError("test did not release cleanup revalidation")

    repository = Neo4jCrmActivityCleanupRepository(uri, user, password)
    repository._before_revalidation = pause_after_lock
    database_identity = repository.database_identity()
    plan = _plan(repository, (activity,))

    def delete() -> None:
        try:
            outcomes.append(
                repository.delete_batch_with_required_absences(
                    database_identity,
                    plan.expected_deletions,
                    (future,),
                )
            )
        except BaseException as error:
            cleanup_failures.append(error)

    def create_future() -> None:
        creation_started.set()
        try:
            with driver.session() as session:
                session.run(
                    """
                    MATCH (source:SourceSystem {source_key: $source_key})
                    CREATE (record:SourceRecord {
                      fixture_id: $fixture_id, source_record_pk: $source_record_pk,
                      source_record_id: 'activity-id', source_record_version: '1',
                      source_version_key: 'activity-v1', record_hash: 'activity-hash',
                      source_instance_id: $source_instance, record_type: 'crm_history',
                      lifecycle_status: 'active', history_family: 'activity',
                      history_source: 'bitrix_crm_activity',
                      projection_source: 'bitrix_crm_activity_v2', projection_version: '2'
                    })-[:FROM_SOURCE]->(source)
                    """,
                    source_key=source_key,
                    fixture_id=fixture_id,
                    source_record_pk=future.source_record_pk,
                    source_instance=source_instance,
                ).consume()
        except BaseException as error:
            creation_failures.append(error)
        finally:
            creation_finished.set()

    deleter = Thread(target=delete)
    creator = Thread(target=create_future)
    try:
        deleter.start()
        assert lock_acquired.wait(timeout=5)
        creator.start()
        assert creation_started.wait(timeout=5)
        assert not creation_finished.wait(timeout=0.25)
        allow_revalidation.set()
        deleter.join(timeout=5)
        creator.join(timeout=5)
    finally:
        allow_revalidation.set()
        deleter.join(timeout=5)
        creator.join(timeout=5)
        repository.close()
    assert not deleter.is_alive()
    assert not creator.is_alive()
    assert cleanup_failures == []
    assert creation_failures == []
    assert outcomes == [
        BatchOutcome(
            database_identity,
            (
                RecordOutcome(activity.source_record_pk, "deleted", "deleted_after_revalidation"),
                RecordOutcome(future.source_record_pk, "already_absent", "absent_at_mutation"),
            ),
            True,
        )
    ]
    inspected = _repository(graph)
    try:
        records = inspected.inspect((activity.source_record_pk, future.source_record_pk))
    finally:
        inspected.close()
    assert [item.matching_node_count for item in records] == [0, 1]
