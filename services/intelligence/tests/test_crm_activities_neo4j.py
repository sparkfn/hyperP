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
    try:
        assert (
            repository.structural_invalid_count(
                ArchiveRequest("snapshot-a", source_instance, source_key)
            )
            == 0
        )
        rows = repository.page(ArchiveRequest("snapshot-a", source_instance, source_key), "")
    finally:
        repository.close()
    with driver.session() as session:
        after = session.run("MATCH (n) RETURN count(n) AS count").single()["count"]
        after_relationships = session.run("MATCH ()-[r]->() RETURN count(r) AS count").single()[
            "count"
        ]
    assert before == after
    assert before_relationships == after_relationships
    assert [row.record_type for row in rows] in (
        ["call", "crm_history"],
        ["crm_history", "call"],
    )
    assert {row.record_type for row in rows} == {"crm_history", "call"}
    assert [(item.source_record_pk, item.disposition) for item in classify(rows)]
    assert all(row.history_family != "stage" for row in rows)
    assert all("MUST_NOT_LEAK" not in str(row.as_dict()) for row in rows)


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
