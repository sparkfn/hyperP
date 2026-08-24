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
            WITH source
            UNWIND [
              {person_id: 'count-0', status: 'active', score: 0.9, count: 0},
              {person_id: 'count-1', status: 'active', score: 0.8, count: 1},
              {person_id: 'count-2-a', status: 'active', score: 0.7, count: 2},
              {person_id: 'count-2-b', status: 'active', score: 0.6, count: 2},
              {person_id: 'count-3', status: 'suppressed', score: 0.5, count: 3},
              {person_id: 'count-4-merged', status: 'merged', score: 1.0, count: 4}
            ] AS row
            CREATE (person:Person {
              person_id: row.person_id, status: row.status,
              profile_completeness_score: row.score, crm_deal_count: row.count,
              _person_list_test_run: $test_run_id
            })
            WITH DISTINCT source
            MATCH (count_one:Person {person_id: 'count-1'})
            MATCH (count_two_a:Person {person_id: 'count-2-a'})
            MATCH (count_two_b:Person {person_id: 'count-2-b'})
            MATCH (count_three:Person {person_id: 'count-3'})
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
            CREATE (deal_one)-[:LINKED_TO {is_active: true}]->(count_one)
            CREATE (deal_two)-[:LINKED_TO {is_active: true}]->(count_one)
            CREATE (count_one)-[:KNOWS {is_active: true}]->(count_two_a)
            CREATE (count_one)-[:KNOWS {is_active: true}]->(count_two_b)
            CREATE (count_two_a)-[:KNOWS {is_active: true}]->(count_three)
            """,
            test_run_id=test_graph.run_id,
        ).consume()


def _list_rows(
    test_graph: _TestGraph,
    query: str,
    *,
    skip: int = 0,
    limit: int = 10,
    **parameters: int,
) -> list[dict[str, object]]:
    with test_graph.driver.session() as session:
        result = session.run(query, skip=skip, limit=limit, **parameters)
        return [dict(record) for record in result]


def _count_rows(test_graph: _TestGraph, query: str, **parameters: int) -> int:
    with test_graph.driver.session() as session:
        record = session.run(query, **parameters).single(strict=True)
    return int(record["total"])


def _person_ids(rows: list[dict[str, object]]) -> list[str]:
    return [str(row["person"]["person_id"]) for row in rows]  # type: ignore[index]


def test_crm_deal_list_queries_read_stored_counts_and_keep_parity(
    neo4j_driver: _TestGraph,
) -> None:
    _seed_graph(neo4j_driver)
    ascending = _list_rows(
        neo4j_driver,
        build_list_persons_query("crm_deal_count", "asc", has_q=False),
    )
    descending = _list_rows(
        neo4j_driver,
        build_list_persons_query("crm_deal_count", "desc", has_q=False),
    )
    assert [(row["person"]["person_id"], row["crm_deal_count"]) for row in ascending] == [
        ("count-0", 0),
        ("count-1", 1),
        ("count-2-a", 2),
        ("count-2-b", 2),
        ("count-3", 3),
    ]
    assert _person_ids(descending) == [
        "count-3",
        "count-2-a",
        "count-2-b",
        "count-1",
        "count-0",
    ]
    assert (
        next(row for row in ascending if row["person"]["person_id"] == "count-1")["crm_deal_count"]
        == 1
    )

    cases = (
        (
            frozenset({"crm_deal_count_min", "crm_deal_count_max"}),
            {"crm_deal_count_min": 0, "crm_deal_count_max": 0},
            ["count-0"],
        ),
        (
            frozenset({"crm_deal_count_min"}),
            {"crm_deal_count_min": 1},
            ["count-1", "count-2-a", "count-2-b", "count-3"],
        ),
        (
            frozenset({"crm_deal_count_min", "crm_deal_count_max"}),
            {"crm_deal_count_min": 1, "crm_deal_count_max": 2},
            ["count-1", "count-2-a", "count-2-b"],
        ),
    )
    for active_filters, parameters, expected_ids in cases:
        list_query = build_list_persons_query(
            "crm_deal_count",
            "asc",
            has_q=False,
            active_filters=active_filters,
        )
        count_query = build_count_persons_query(
            "crm_deal_count",
            "asc",
            has_q=False,
            active_filters=active_filters,
        )
        rows = _list_rows(neo4j_driver, list_query, **parameters)
        assert _person_ids(rows) == expected_ids
        assert _count_rows(neo4j_driver, count_query, **parameters) == len(rows)


def test_crm_deal_count_sort_keeps_page_boundaries_stable(
    neo4j_driver: _TestGraph,
) -> None:
    _seed_graph(neo4j_driver)
    for direction, expected in (
        ("asc", ["count-0", "count-1", "count-2-a", "count-2-b", "count-3"]),
        ("desc", ["count-3", "count-2-a", "count-2-b", "count-1", "count-0"]),
    ):
        query = build_list_persons_query("crm_deal_count", direction, has_q=False)
        pages = [_list_rows(neo4j_driver, query, skip=skip, limit=2) for skip in range(0, 6, 2)]
        assert [person_id for page in pages for person_id in _person_ids(page)] == expected


def test_connection_count_deduplicates_high_cardinality_paths(
    neo4j_driver: _TestGraph,
) -> None:
    with neo4j_driver.driver.session() as session:
        session.run(
            """
            CREATE (target:Person {
              person_id: 'connections-target', status: 'active', crm_deal_count: 10,
              _person_list_test_run: $test_run_id
            })
            CREATE (address_a:Address {
              address_id: 'connections-a', _person_list_test_run: $test_run_id
            })
            CREATE (address_b:Address {
              address_id: 'connections-b', _person_list_test_run: $test_run_id
            })
            CREATE (address_only:Person {
              person_id: 'address-only', status: 'active', crm_deal_count: 4,
              _person_list_test_run: $test_run_id
            })
            CREATE (overlap:Person {
              person_id: 'address-and-knows', status: 'active', crm_deal_count: 3,
              _person_list_test_run: $test_run_id
            })
            CREATE (knows_only:Person {
              person_id: 'knows-only', status: 'active', crm_deal_count: 2,
              _person_list_test_run: $test_run_id
            })
            CREATE (inactive_only:Person {
              person_id: 'inactive-only', status: 'active', crm_deal_count: 1,
              _person_list_test_run: $test_run_id
            })
            CREATE (merged:Person {
              person_id: 'merged-connection', status: 'merged', crm_deal_count: 99,
              _person_list_test_run: $test_run_id
            })
            WITH target, address_a, address_b, address_only, overlap, knows_only,
                 inactive_only, merged
            UNWIND range(1, 20) AS duplicate
            CREATE (target)-[:LIVES_AT {is_active: true, duplicate: duplicate}]->(address_a)
            CREATE (address_only)-[:LIVES_AT {is_active: true, duplicate: duplicate}]->(address_a)
            CREATE (overlap)-[:LIVES_AT {is_active: true, duplicate: duplicate}]->(address_a)
            CREATE (target)-[:LIVES_AT {is_active: true, duplicate: duplicate}]->(address_b)
            CREATE (overlap)-[:LIVES_AT {is_active: true, duplicate: duplicate}]->(address_b)
            CREATE (target)-[:KNOWS {is_active: true, duplicate: duplicate}]->(overlap)
            CREATE (target)-[:KNOWS {is_active: true, duplicate: duplicate}]->(knows_only)
            CREATE (target)-[:KNOWS {is_active: false, duplicate: duplicate}]->(inactive_only)
            CREATE (target)-[:KNOWS {is_active: true, duplicate: duplicate}]->(merged)
            """,
            test_run_id=neo4j_driver.run_id,
        ).consume()

    query = build_list_persons_query("crm_deal_count", "desc", has_q=False)
    first_page = _list_rows(neo4j_driver, query, skip=0, limit=3)
    second_page = _list_rows(neo4j_driver, query, skip=3, limit=3)
    repeated_first_page = _list_rows(neo4j_driver, query, skip=0, limit=3)

    assert _person_ids(first_page) == ["connections-target", "address-only", "address-and-knows"]
    assert _person_ids(second_page) == ["knows-only", "inactive-only"]
    assert _person_ids(repeated_first_page) == _person_ids(first_page)
    target = first_page[0]
    assert target["connection_count"] == 3


def test_crm_deal_count_filter_composes_with_computed_sort(
    neo4j_driver: _TestGraph,
) -> None:
    _seed_graph(neo4j_driver)
    active_filters = frozenset({"crm_deal_count_min", "crm_deal_count_max"})
    parameters = {"crm_deal_count_min": 1, "crm_deal_count_max": 2}
    rows = _list_rows(
        neo4j_driver,
        build_list_persons_query(
            "connection_count",
            "desc",
            has_q=False,
            active_filters=active_filters,
        ),
        **parameters,
    )
    assert set(_person_ids(rows)) == {"count-1", "count-2-a", "count-2-b"}
    assert all(1 <= int(row["crm_deal_count"]) <= 2 for row in rows)
    total = _count_rows(
        neo4j_driver,
        build_count_persons_query(
            "connection_count",
            "desc",
            has_q=False,
            active_filters=active_filters,
        ),
        **parameters,
    )
    assert total == len(rows)


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
