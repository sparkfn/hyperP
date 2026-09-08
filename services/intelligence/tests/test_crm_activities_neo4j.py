"""Disposable real-Neo4j proof that the archive reader is projection-only."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from intelligence.crm.activities.dispositions import classify
from intelligence.crm.activities.models import ArchiveRequest
from intelligence.repositories.neo4j.crm_activities import Neo4jCrmActivitiesRepository
from neo4j import Driver, GraphDatabase


@pytest.fixture()
def graph() -> Iterator[tuple[Driver, str, str, str, str, str, str]]:
    uri = os.environ.get("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_URI")
    user = os.environ.get("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_USER")
    password = os.environ.get("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_PASSWORD")
    if not uri or not user or not password:
        pytest.skip("real Neo4j shard environment is absent")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    source_instance = f"activities-{uuid4().hex}"
    source_key = f"activities-source-{uuid4().hex}"
    fixture_id = uuid4().hex
    try:
        with driver.session() as session:
            session.run(
                """
                CREATE (source:SourceSystem {source_key: $source_key, fixture_id: $fixture_id})
                CREATE (deal:SourceRecord {
                  source_record_pk: $deal, source_record_id: 'deal-a',
                  source_record_version: '1', source_version_key: 'deal-v1',
                  record_hash: 'deal-hash', source_instance_id: $instance, fixture_id: $fixture_id,
                  record_type: 'crm_deal'
                })-[:FROM_SOURCE]->(source)
                CREATE (history:SourceRecord {
                  source_record_pk: $history, source_record_id: 'history-a',
                  source_record_version: '1', source_version_key: 'history-v1',
                  record_hash: 'history-hash', source_instance_id: $instance,
                  fixture_id: $fixture_id,
                  record_type: 'crm_history', lifecycle_status: 'active',
                  history_family: 'activity', history_kind: 'call',
                  history_source: 'bitrix_crm_activity', projection_version: 2,
                  projection_source: 'bitrix_crm_activity_v2',
                  raw_payload: 'MUST_NOT_LEAK', parent_source_instance_id: $instance,
                  parent_source_record_id: 'deal-a', parent_record_type: 'crm_deal'
                })-[:FROM_SOURCE]->(source)
                CREATE (history)-[:CHILD_OF]->(deal)
                CREATE (unattributed:SourceRecord {
                  source_record_pk: $unattributed, source_record_id: 'unattributed-a',
                  source_record_version: '1', source_version_key: 'unattributed-v1',
                  record_hash: 'unattributed-hash', source_instance_id: $instance,
                  fixture_id: $fixture_id, record_type: 'crm_deal'
                })
                CREATE (history)-[:CHILD_OF]->(unattributed)
                CREATE (call:SourceRecord {
                  source_record_pk: $call, source_record_id: 'call-a',
                  source_record_version: '1', source_version_key: 'call-v1',
                  record_hash: 'call-hash', source_instance_id: $instance, fixture_id: $fixture_id,
                  record_type: 'call', lifecycle_status: 'active', raw_payload: 'MUST_NOT_LEAK',
                  parent_source_instance_id: $instance, parent_source_record_id: 'history-a',
                  parent_record_type: 'crm_history', parent_source_system: $source_key
                })-[:FROM_SOURCE]->(source)
                CREATE (call)-[:CHILD_OF]->(history)
                CREATE (call)-[:DETAILS_HISTORY_ITEM]->(history)
                CREATE (person:Person {
                  person_id: $person, status: 'active', revision: 3, fixture_id: $fixture_id
                })
                CREATE (history)-[:LINKED_TO {
                  is_active: true, source_record_pk: $history
                }]->(person)
                CREATE (stage:SourceRecord {
                  source_record_pk: $stage, source_record_id: 'stage-a',
                  source_record_version: '1', source_version_key: 'stage-v1',
                  record_hash: 'stage-hash', source_instance_id: $instance, fixture_id: $fixture_id,
                  record_type: 'crm_history', lifecycle_status: 'active', history_family: 'stage'
                })-[:FROM_SOURCE]->(source)
                CREATE (conversation:SourceRecord {
                  source_record_pk: $conversation, fixture_id: $fixture_id,
                  record_type: 'conversation', raw_payload: 'MUST_NOT_LEAK'
                })
                CREATE (history)-[:LINKED_TO]->(conversation)
                """,
                source_key=source_key,
                instance=source_instance,
                deal=f"deal-{uuid4().hex}",
                unattributed=f"unattributed-{uuid4().hex}",
                history=f"history-{uuid4().hex}",
                call=f"call-{uuid4().hex}",
                person=f"person-{uuid4().hex}",
                stage=f"stage-{uuid4().hex}",
                conversation=f"conversation-{uuid4().hex}",
                fixture_id=fixture_id,
            ).consume()
        yield driver, uri, user, password, source_instance, source_key, fixture_id
    finally:
        with driver.session() as session:
            session.run(
                "MATCH (n {fixture_id: $fixture_id}) DETACH DELETE n",
                fixture_id=fixture_id,
            ).consume()
            session.run(
                "MATCH (s:SourceSystem {fixture_id: $fixture_id}) DETACH DELETE s",
                fixture_id=fixture_id,
            ).consume()
        driver.close()


def test_reader_returns_only_safe_activity_and_companion_call_without_writes(
    graph: tuple[Driver, str, str, str, str, str, str],
) -> None:
    driver, uri, user, password, source_instance, source_key, fixture_id = graph
    with driver.session() as session:
        before = session.run("MATCH (n) RETURN count(n) AS count").single()["count"]
        before_relationships = session.run("MATCH ()-[r]->() RETURN count(r) AS count").single()[
            "count"
        ]
    repository = Neo4jCrmActivitiesRepository(uri, user, password)
    request = ArchiveRequest("snapshot-a", source_instance, source_key, max_references_per_record=2)
    try:
        assert repository.structural_invalid_count(request) == 0
        assert repository.reference_fanout_invalid_count(request) == 0
        rows = repository.page(request, "")
        by_identity = repository.by_identities(
            request,
            tuple(row.source_record_pk for row in rows),
        )
    finally:
        repository.close()
    with driver.session() as session:
        after = session.run("MATCH (n) RETURN count(n) AS count").single()["count"]
        after_relationships = session.run("MATCH ()-[r]->() RETURN count(r) AS count").single()[
            "count"
        ]
    assert before == after
    assert before_relationships == after_relationships
    assert [row.source_record_pk for row in rows] == sorted(row.source_record_pk for row in rows)
    assert by_identity == rows
    assert {row.record_type for row in rows} == {"crm_history", "call"}
    assert [(item.source_record_pk, item.disposition) for item in classify(rows)]
    assert all(row.history_family != "stage" for row in rows)
    assert all("MUST_NOT_LEAK" not in str(row.as_dict()) for row in rows)
    history = next(row for row in rows if row.record_type == "crm_history")
    assert any(parent.source_system == source_key for parent in history.child_parents)
    assert any(parent.source_system is None for parent in history.child_parents)


def test_by_identities_returns_only_the_requested_real_neo4j_record(
    graph: tuple[Driver, str, str, str, str, str, str],
) -> None:
    driver, uri, user, password, source_instance, source_key, fixture_id = graph
    del fixture_id
    repository = Neo4jCrmActivitiesRepository(uri, user, password)
    request = ArchiveRequest("snapshot-a", source_instance, source_key)
    try:
        page = repository.page(request, "")
        assert len(page) > 1
        selected = page[0].source_record_pk
        rows = repository.by_identities(request, (selected,))
    finally:
        repository.close()

    assert tuple(row.source_record_pk for row in rows) == (selected,)
    assert rows[0].record_type in {"crm_history", "call"}


def test_preflight_rejects_blank_identity_without_graph_mutation(
    graph: tuple[Driver, str, str, str, str, str, str],
) -> None:
    driver, uri, user, password, source_instance, source_key, fixture_id = graph
    with driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: $source_key})
            CREATE (bad:SourceRecord {
              fixture_id: $fixture_id, source_record_pk: '', source_record_id: 'bad-history',
              source_record_version: '1', source_version_key: 'bad-v1', record_hash: '',
              source_instance_id: $source_instance, record_type: 'crm_history',
              lifecycle_status: 'active', history_family: 'activity',
              history_source: 'bitrix_crm_activity', projection_version: 2,
              projection_source: 'bitrix_crm_activity_v2', raw_payload: 'MUST_NOT_LEAK'
            })-[:FROM_SOURCE]->(source)
            """,
            source_key=source_key,
            fixture_id=fixture_id,
            source_instance=source_instance,
        ).consume()
        nodes_before = session.run("MATCH (n) RETURN count(n) AS count").single()["count"]
        relationships_before = session.run("MATCH ()-[r]->() RETURN count(r) AS count").single()[
            "count"
        ]
    repository = Neo4jCrmActivitiesRepository(uri, user, password)
    try:
        invalid = repository.structural_invalid_count(
            ArchiveRequest("checkpoint-a", source_instance, source_key)
        )
    finally:
        repository.close()
    with driver.session() as session:
        nodes_after = session.run("MATCH (n) RETURN count(n) AS count").single()["count"]
        relationships_after = session.run("MATCH ()-[r]->() RETURN count(r) AS count").single()[
            "count"
        ]
    assert invalid > 0
    assert nodes_after == nodes_before
    assert relationships_after == relationships_before


def test_reference_fanout_preflight_rejects_each_bounded_reference_type_without_writes(
    graph: tuple[Driver, str, str, str, str, str, str],
) -> None:
    driver, uri, user, password, source_instance, source_key, fixture_id = graph
    with driver.session() as session:
        session.run(
            """
            MATCH (source:SourceSystem {source_key: $source_key})
            CREATE (child:SourceRecord {
              fixture_id: $fixture_id, source_record_pk: 'child-overflow-' + $fixture_id,
              source_record_id: 'child-overflow', source_record_version: '1',
              source_version_key: 'child-overflow-v1', record_hash: 'child-overflow-hash',
              source_instance_id: $source_instance, record_type: 'crm_history',
              lifecycle_status: 'active', history_family: 'activity'
            })-[:FROM_SOURCE]->(source)
            CREATE (details:SourceRecord {
              fixture_id: $fixture_id, source_record_pk: 'details-overflow-' + $fixture_id,
              source_record_id: 'details-overflow', source_record_version: '1',
              source_version_key: 'details-overflow-v1', record_hash: 'details-overflow-hash',
              source_instance_id: $source_instance, record_type: 'crm_history',
              lifecycle_status: 'active', history_family: 'activity'
            })-[:FROM_SOURCE]->(source)
            CREATE (people:SourceRecord {
              fixture_id: $fixture_id, source_record_pk: 'people-overflow-' + $fixture_id,
              source_record_id: 'people-overflow', source_record_version: '1',
              source_version_key: 'people-overflow-v1', record_hash: 'people-overflow-hash',
              source_instance_id: $source_instance, record_type: 'crm_history',
              lifecycle_status: 'active', history_family: 'activity'
            })-[:FROM_SOURCE]->(source)
            CREATE (source_fanout:SourceRecord {
              fixture_id: $fixture_id, source_record_pk: 'source-fanout-' + $fixture_id,
              source_record_id: 'source-fanout', source_record_version: '1',
              source_version_key: 'source-fanout-v1', record_hash: 'source-fanout-hash',
              source_instance_id: $source_instance, record_type: 'crm_history',
              lifecycle_status: 'active', history_family: 'activity'
            })-[:FROM_SOURCE]->(source)
            CREATE (child_parent_a:SourceRecord {fixture_id: $fixture_id})
            CREATE (child_parent_b:SourceRecord {fixture_id: $fixture_id})
            CREATE (child_parent_c:SourceRecord {fixture_id: $fixture_id})
            CREATE (child)-[:CHILD_OF]->(child_parent_a)
            CREATE (child)-[:CHILD_OF]->(child_parent_b)
            CREATE (child)-[:CHILD_OF]->(child_parent_c)
            CREATE (details_parent_a:SourceRecord {fixture_id: $fixture_id})
            CREATE (details_parent_b:SourceRecord {fixture_id: $fixture_id})
            CREATE (details_parent_c:SourceRecord {fixture_id: $fixture_id})
            CREATE (details)-[:DETAILS_HISTORY_ITEM]->(details_parent_a)
            CREATE (details)-[:DETAILS_HISTORY_ITEM]->(details_parent_b)
            CREATE (details)-[:DETAILS_HISTORY_ITEM]->(details_parent_c)
            CREATE (active_a:Person {fixture_id: $fixture_id, person_id: 'active-a-' + $fixture_id})
            CREATE (active_b:Person {fixture_id: $fixture_id, person_id: 'active-b-' + $fixture_id})
            CREATE (active_c:Person {fixture_id: $fixture_id, person_id: 'active-c-' + $fixture_id})
            CREATE (inactive:Person {fixture_id: $fixture_id, person_id: 'inactive-' + $fixture_id})
            CREATE (people)-[:LINKED_TO {is_active: true}]->(active_a)
            CREATE (people)-[:LINKED_TO {is_active: true}]->(active_b)
            CREATE (people)-[:LINKED_TO {is_active: true}]->(active_c)
            CREATE (people)-[:LINKED_TO {is_active: false}]->(inactive)
            CREATE (source_parent:SourceRecord {fixture_id: $fixture_id})
            CREATE (source_fanout)-[:CHILD_OF]->(source_parent)
            CREATE (parent_source_a:SourceSystem {
              fixture_id: $fixture_id, source_key: 'parent-source-a-' + $fixture_id
            })
            CREATE (parent_source_b:SourceSystem {
              fixture_id: $fixture_id, source_key: 'parent-source-b-' + $fixture_id
            })
            CREATE (parent_source_c:SourceSystem {
              fixture_id: $fixture_id, source_key: 'parent-source-c-' + $fixture_id
            })
            CREATE (source_parent)-[:FROM_SOURCE]->(parent_source_a)
            CREATE (source_parent)-[:FROM_SOURCE]->(parent_source_b)
            CREATE (source_parent)-[:FROM_SOURCE]->(parent_source_c)
            """,
            source_key=source_key,
            fixture_id=fixture_id,
            source_instance=source_instance,
        ).consume()
        nodes_before = session.run("MATCH (n) RETURN count(n) AS count").single()["count"]
        relationships_before = session.run("MATCH ()-[r]->() RETURN count(r) AS count").single()[
            "count"
        ]
    repository = Neo4jCrmActivitiesRepository(uri, user, password)
    try:
        invalid = repository.reference_fanout_invalid_count(
            ArchiveRequest(
                "checkpoint-a",
                source_instance,
                source_key,
                max_references_per_record=2,
            )
        )
    finally:
        repository.close()
    with driver.session() as session:
        nodes_after = session.run("MATCH (n) RETURN count(n) AS count").single()["count"]
        relationships_after = session.run("MATCH ()-[r]->() RETURN count(r) AS count").single()[
            "count"
        ]
    assert invalid == 4
    assert nodes_after == nodes_before
    assert relationships_after == relationships_before
