"""Neo4j coverage for person CRM metric aggregation.

Set ``HYPERP_NEO4J_CRM_METRICS_TEST_URI`` to a disposable localhost Neo4j
instance to enable this test. It clears the graph before and after each test.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase
from src.graph.queries.crm import GET_PERSON_CRM_METRICS


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    uri = os.getenv("HYPERP_NEO4J_CRM_METRICS_TEST_URI")
    if uri is None:
        pytest.skip("disposable CRM metrics Neo4j test database is not configured")
    host = urlparse(uri).hostname
    if host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("CRM metrics integration tests only accept a localhost Neo4j URI")
    password = os.getenv("HYPERP_NEO4J_CRM_METRICS_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_CRM_METRICS_TEST_PASSWORD is required")

    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_CRM_METRICS_TEST_USER", "neo4j"), password),
    )
    driver.verify_connectivity()
    with driver.session() as session:
        session.run("MATCH (node) DETACH DELETE node").consume()
    try:
        yield driver
    finally:
        with driver.session() as session:
            session.run("MATCH (node) DETACH DELETE node").consume()
        driver.close()


def test_metrics_query_uses_projected_stage_with_persisted_json_payload(
    neo4j_driver: Driver,
) -> None:
    """A JSON-string payload must not be dereferenced as a Cypher map."""
    raw_payload = json.dumps({"stage_id": "C2:WON"})
    with neo4j_driver.session() as session:
        session.run(
            """
            CREATE (person:Person {person_id: 'person-1'})
            CREATE (source:SourceSystem {source_key: 'bitrix_chat'})
            CREATE (deal:SourceRecord {
              record_type: 'crm_deal', lifecycle_status: 'active', is_latest: true,
              observed_at: datetime('2026-08-20T00:00:00Z'),
              raw_payload: $raw_payload, crm_deal_stage_id: 'C2:WON'
            })
            CREATE (deal)-[:FROM_SOURCE]->(source)
            CREATE (deal)-[:LINKED_TO {is_active: true}]->(person)
            """,
            raw_payload=raw_payload,
        ).consume()
        row = session.run(GET_PERSON_CRM_METRICS, person_id="person-1").single(strict=True)

    assert row["deal_count"] == 1
    assert row["deal_stage_breakdown"] == [{"stage_id": "C2:WON", "count": 1}]
