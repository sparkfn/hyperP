"""Disposable Neo4j execution and planner coverage for person-list queries."""

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


def test_completeness_list_keeps_suppressed_and_zero_scores_with_stable_pages(
    neo4j_driver: _TestGraph,
) -> None:
    with neo4j_driver.driver.session() as session:
        session.run(
            """
            UNWIND [
              {person_id: 'score-90-z', status: 'active', profile_completeness_score: 0.9},
              {person_id: 'score-90-a', status: 'suppressed', profile_completeness_score: 0.9},
              {person_id: 'score-90-m', status: 'active', profile_completeness_score: 0.9},
              {person_id: 'score-40', status: 'active', profile_completeness_score: 0.4},
              {person_id: 'score-00', status: 'active', profile_completeness_score: 0.0},
              {person_id: 'missing-score', status: 'active'},
              {person_id: 'merged-score', status: 'merged', profile_completeness_score: 1.0}
            ] AS row
            CREATE (person:Person)
            SET person += row, person._person_list_test_run = $test_run_id
            """,
            test_run_id=neo4j_driver.run_id,
        ).consume()
        query = build_list_persons_query("profile_completeness_score", "desc", has_q=False)
        first_window = list(session.run(query, skip=0, limit=2))
        second_window = list(session.run(query, skip=2, limit=2))
        third_window = list(session.run(query, skip=4, limit=2))
        completeness_total = session.run(
            build_count_persons_query(
                "profile_completeness_score",
                "desc",
                has_q=False,
            )
        ).single(strict=True)["total"]
        name_total = session.run(
            build_count_persons_query("preferred_full_name", "asc", has_q=False)
        ).single(strict=True)["total"]

    assert completeness_total == 5
    assert name_total == 6
    pages = first_window + second_window + third_window
    page_ids = [record["person"]["person_id"] for record in pages]
    assert page_ids == [
        "score-90-a",
        "score-90-m",
        "score-90-z",
        "score-40",
        "score-00",
    ]
    assert len(page_ids) == len(set(page_ids))
    assert all(record["person"]["person_id"] != "missing-score" for record in pages)


_FILTERED_PARITY_FIXTURE = """
CREATE (address:Address {
  address_id: 'address-sg', city: 'Singapore', _person_list_test_run: $test_run_id
})
CREATE (entity:Entity {entity_key: 'entity-a', _person_list_test_run: $test_run_id})
CREATE (source:SourceSystem {source_key: 'source-a', _person_list_test_run: $test_run_id})
WITH address, entity, source
UNWIND [
  {person_id: 'matched-scored', profile_completeness_score: 0.8},
  {person_id: 'matched-missing'},
  {person_id: 'other-city', profile_completeness_score: 0.6}
] AS row
CREATE (person:Person {status: 'active', _person_list_test_run: $test_run_id})
SET person += row
WITH DISTINCT address, entity, source
MATCH (matched:Person {person_id: 'matched-scored'})
WHERE matched._person_list_test_run = $test_run_id
MATCH (missing:Person {person_id: 'matched-missing'})
WHERE missing._person_list_test_run = $test_run_id
MATCH (other:Person {person_id: 'other-city'})
WHERE other._person_list_test_run = $test_run_id
CREATE (matched)-[:LIVES_AT {is_active: true}]->(address)
CREATE (matched)-[:LIVES_AT {is_active: true}]->(address)
CREATE (missing)-[:LIVES_AT {is_active: true}]->(address)
CREATE (other_address:Address {
  address_id: 'address-my', city: 'Kuala Lumpur', _person_list_test_run: $test_run_id
})
CREATE (other)-[:LIVES_AT {is_active: true}]->(other_address)
CREATE (matched_record:SourceRecord {
  source_record_pk: 'matched-record', lifecycle_status: 'active', is_latest: true,
  _person_list_test_run: $test_run_id
})
CREATE (missing_record:SourceRecord {
  source_record_pk: 'missing-record', lifecycle_status: 'active', is_latest: true,
  _person_list_test_run: $test_run_id
})
CREATE (matched_record)-[:LINKED_TO {is_active: true}]->(matched)
CREATE (matched_record)-[:OWNED_BY]->(entity)
CREATE (matched_record)-[:FROM_SOURCE]->(source)
CREATE (missing_record)-[:LINKED_TO {is_active: true}]->(missing)
CREATE (missing_record)-[:OWNED_BY]->(entity)
CREATE (missing_record)-[:FROM_SOURCE]->(source)
"""


def _filtered_person_ids(
    test_graph: _TestGraph,
    *,
    sort_by: str,
    active_filters: frozenset[str],
    parameters: dict[str, object],
) -> tuple[list[str], int]:
    with test_graph.driver.session() as session:
        rows = session.run(
            build_list_persons_query(sort_by, "desc", has_q=False, active_filters=active_filters),
            skip=0,
            limit=10,
            **parameters,
        )
        person_ids = [record["person"]["person_id"] for record in rows]
        total = session.run(
            build_count_persons_query(
                sort_by,
                "desc",
                has_q=False,
                active_filters=active_filters,
            ),
            **parameters,
        ).single(strict=True)["total"]
    return person_ids, total


def test_completeness_list_and_count_keep_filtered_row_sets_aligned(
    neo4j_driver: _TestGraph,
) -> None:
    with neo4j_driver.driver.session() as session:
        session.run(_FILTERED_PARITY_FIXTURE, test_run_id=neo4j_driver.run_id).consume()
    cases = (
        (
            frozenset({"addr_city", "entity_keys"}),
            {"addr_city": "Singapore", "entity_keys": ["entity-a"]},
        ),
        (frozenset({"source_keys"}), {"source_keys": ["source-a"]}),
    )
    for filters, parameters in cases:
        completeness_ids, completeness_total = _filtered_person_ids(
            neo4j_driver,
            sort_by="profile_completeness_score",
            active_filters=filters,
            parameters=parameters,
        )
        name_ids, name_total = _filtered_person_ids(
            neo4j_driver,
            sort_by="preferred_full_name",
            active_filters=filters,
            parameters=parameters,
        )
        assert completeness_ids == ["matched-scored"]
        assert completeness_total == 1
        assert set(name_ids) == {"matched-missing", "matched-scored"}
        assert name_total == 2
