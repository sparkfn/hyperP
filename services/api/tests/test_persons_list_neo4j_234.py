"""Disposable Neo4j execution coverage for CRM deal person-list queries."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from neo4j import Driver, GraphDatabase
from src.graph.queries.persons_list import build_count_persons_query, build_list_persons_query


@dataclass(frozen=True)
class _TestGraph:
    driver: Driver
    run_id: str


@pytest.fixture
def neo4j_driver() -> Iterator[_TestGraph]:
    uri = os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_URI")
    if uri is None:
        pytest.skip("disposable person-list Neo4j test database is not configured")
    host = urlparse(uri).hostname
    service_host = os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_SERVICE_HOST")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if service_host is not None:
        allowed_hosts.add(service_host)
    if host not in allowed_hosts:
        pytest.fail("person-list integration tests only accept an explicitly configured Neo4j host")
    password = os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_PERSON_LIST_TEST_PASSWORD is required")

    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_USER", "neo4j"), password),
    )
    driver.verify_connectivity()
    test_graph = _TestGraph(driver=driver, run_id=uuid4().hex)
    try:
        yield test_graph
    finally:
        with driver.session() as session:
            session.run(
                """
                MATCH (node {_person_list_test_run: $test_run_id})
                DETACH DELETE node
                """,
                test_run_id=test_graph.run_id,
            ).consume()
        driver.close()


def _seed_graph(test_graph: _TestGraph) -> None:
    with test_graph.driver.session() as session:
        session.run(
            """
            CREATE (source:SourceSystem {
              source_key: 'bitrix_chat', _person_list_test_run: $test_run_id
            })
            CREATE (with_deals:Person {
              person_id: 'with-deals', status: 'active', profile_completeness_score: 0.4,
              _person_list_test_run: $test_run_id
            })
            CREATE (without_deals:Person {
              person_id: 'without-deals', status: 'active', profile_completeness_score: 0.9,
              _person_list_test_run: $test_run_id
            })
            CREATE (deal_one:SourceRecord {
              source_record_pk: 'deal-one', record_type: 'crm_deal',
              lifecycle_status: 'active', is_latest: true,
              _person_list_test_run: $test_run_id
            })
            CREATE (deal_two:SourceRecord {
              source_record_pk: 'deal-two', record_type: 'crm_deal',
              lifecycle_status: 'active', is_latest: true,
              _person_list_test_run: $test_run_id
            })
            CREATE (deal_one)-[:FROM_SOURCE]->(source)
            CREATE (deal_two)-[:FROM_SOURCE]->(source)
            CREATE (deal_one)-[:LINKED_TO {is_active: true}]->(with_deals)
            CREATE (deal_one)-[:LINKED_TO {is_active: true}]->(with_deals)
            CREATE (deal_two)-[:LINKED_TO {is_active: true}]->(with_deals)
            """,
            test_run_id=test_graph.run_id,
        ).consume()


def _list_rows(test_graph: _TestGraph, query: str, **parameters: int) -> list[dict[str, object]]:
    with test_graph.driver.session() as session:
        result = session.run(query, skip=0, limit=10, **parameters)
        return [dict(record) for record in result]


def test_crm_deal_list_queries_execute_with_distinct_counts_and_zero_filter(
    neo4j_driver: _TestGraph,
) -> None:
    _seed_graph(neo4j_driver)

    default_rows = _list_rows(
        neo4j_driver,
        build_list_persons_query("profile_completeness_score", "desc", has_q=False),
    )
    assert [(row["person"]["person_id"], row["crm_deal_count"]) for row in default_rows] == [
        ("without-deals", 0),
        ("with-deals", 2),
    ]

    sorted_rows = _list_rows(
        neo4j_driver,
        build_list_persons_query("crm_deal_count", "desc", has_q=False),
    )
    assert [(row["person"]["person_id"], row["crm_deal_count"]) for row in sorted_rows] == [
        ("with-deals", 2),
        ("without-deals", 0),
    ]

    no_deal_filters = frozenset({"crm_deal_count_min", "crm_deal_count_max"})
    no_deal_rows = _list_rows(
        neo4j_driver,
        build_list_persons_query(
            "preferred_full_name",
            "asc",
            has_q=False,
            active_filters=no_deal_filters,
        ),
        crm_deal_count_min=0,
        crm_deal_count_max=0,
    )
    assert [(row["person"]["person_id"], row["crm_deal_count"]) for row in no_deal_rows] == [
        ("without-deals", 0)
    ]

    with neo4j_driver.driver.session() as session:
        count_record = session.run(
            build_count_persons_query(has_q=False, active_filters=no_deal_filters),
            crm_deal_count_min=0,
            crm_deal_count_max=0,
        ).single(strict=True)
    assert count_record["total"] == 1
