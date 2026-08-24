"""Possible-match cardinality regression coverage on disposable Neo4j."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from neo4j import Driver, GraphDatabase
from src.graph.queries.persons import COUNT_PERSON_SHARED_IDENTIFIERS
from src.graph.queries.persons_list import build_list_persons_query


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
            remaining = session.run(
                """
                MATCH (node {_person_list_test_run: $test_run_id})
                RETURN count(node) AS total
                """,
                test_run_id=test_graph.run_id,
            ).single(strict=True)["total"]
            assert remaining == 0
        driver.close()


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


def _row_person_id(row: Mapping[str, object]) -> str:
    person = row.get("person")
    if not isinstance(person, Mapping):
        raise AssertionError("Person-list row must contain a person mapping")
    person_id = person.get("person_id")
    if not isinstance(person_id, str):
        raise AssertionError("Person-list row must contain a string person_id")
    return person_id


def _person_ids(rows: list[dict[str, object]]) -> list[str]:
    return [_row_person_id(row) for row in rows]


@dataclass(frozen=True)
class _PossibleMatchFixture:
    person_ids: Mapping[str, str]
    expected_candidates: Mapping[str, tuple[str, ...]]
    relationship_batch_counts: Mapping[str, int]


def _seed_possible_match_cardinality_graph(test_graph: _TestGraph) -> _PossibleMatchFixture:
    person_ids = {
        "candidate-a": f"{test_graph.run_id}-pm-a-candidate",
        "target": f"{test_graph.run_id}-pm-b-target",
        "candidate-b": f"{test_graph.run_id}-pm-c-suppressed",
        "candidate-c": f"{test_graph.run_id}-pm-d-missing-active",
        "candidate-d": f"{test_graph.run_id}-pm-e-inactive-candidate",
        "candidate-e": f"{test_graph.run_id}-pm-f-inactive-target",
        "no-match": f"{test_graph.run_id}-pm-g-no-match",
        "merged": f"{test_graph.run_id}-pm-h-merged",
    }
    identifier_ids = {
        "phone": f"{test_graph.run_id}-pm-phone",
        "email": f"{test_graph.run_id}-pm-email",
        "inactive": f"{test_graph.run_id}-pm-inactive",
    }
    persons = (
        ("candidate-a", "active", 2_000),
        ("target", "active", 1_999),
        ("candidate-b", "suppressed", 1_998),
        ("candidate-c", "active", 1_997),
        ("candidate-d", "active", 1_996),
        ("candidate-e", "active", 1_995),
        ("no-match", "active", 1_994),
        ("merged", "merged", 1_993),
    )
    relationship_batches = (
        ("target_phone_active_parallel", "target", "phone", 100, True),
        ("candidate_a_phone_active_parallel", "candidate-a", "phone", 90, True),
        ("candidate_b_phone_active_parallel", "candidate-b", "phone", 80, True),
        ("merged_phone_active_parallel", "merged", "phone", 70, True),
        ("target_email_missing_active", "target", "email", 1, None),
        ("candidate_a_email_missing_active", "candidate-a", "email", 1, None),
        ("candidate_c_email_missing_active", "candidate-c", "email", 1, None),
        ("candidate_d_phone_inactive", "candidate-d", "phone", 2, False),
        ("target_inactive_identifier", "target", "inactive", 1, False),
        ("candidate_e_active_to_inactive_target_identifier", "candidate-e", "inactive", 1, True),
    )
    expected_candidates = {
        person_ids["target"]: tuple(
            sorted(
                (person_ids["candidate-a"], person_ids["candidate-b"], person_ids["candidate-c"])
            )
        ),
        person_ids["candidate-a"]: tuple(
            sorted((person_ids["target"], person_ids["candidate-b"], person_ids["candidate-c"]))
        ),
        person_ids["candidate-b"]: tuple(sorted((person_ids["candidate-a"], person_ids["target"]))),
        person_ids["candidate-c"]: tuple(sorted((person_ids["candidate-a"], person_ids["target"]))),
        person_ids["candidate-d"]: (),
        person_ids["candidate-e"]: (),
        person_ids["no-match"]: (),
    }
    with test_graph.driver.session() as session:
        session.run(
            """
            UNWIND $persons AS row
            CREATE (:Person {
              person_id: row.person_id,
              status: row.status,
              crm_deal_count: row.crm_deal_count,
              profile_completeness_score: 0.5,
              _person_list_test_run: $test_run_id
            })
            """,
            persons=[
                {"person_id": person_ids[name], "status": status, "crm_deal_count": crm_count}
                for name, status, crm_count in persons
            ],
            test_run_id=test_graph.run_id,
        ).consume()
        session.run(
            """
            UNWIND $identifiers AS row
            CREATE (:Identifier {
              identifier_id: row.identifier_id,
              identifier_type: row.identifier_type,
              normalized_value: row.normalized_value,
              _person_list_test_run: $test_run_id
            })
            """,
            identifiers=[
                {
                    "identifier_id": identifier_id,
                    "identifier_type": "phone" if name == "phone" else "email",
                    "normalized_value": identifier_id,
                }
                for name, identifier_id in identifier_ids.items()
            ],
            test_run_id=test_graph.run_id,
        ).consume()
        for batch, person_name, identifier_name, count, is_active in relationship_batches:
            session.run(
                """
                MATCH (person:Person {
                  person_id: $person_id, _person_list_test_run: $test_run_id
                })
                MATCH (identifier:Identifier {
                  identifier_id: $identifier_id, _person_list_test_run: $test_run_id
                })
                UNWIND range(1, $relationship_count) AS ignored
                CREATE (person)-[rel:IDENTIFIED_BY {
                  _person_list_test_run: $test_run_id,
                  fixture_batch: $fixture_batch
                }]->(identifier)
                SET rel.is_active = $is_active
                """,
                person_id=person_ids[person_name],
                identifier_id=identifier_ids[identifier_name],
                relationship_count=count,
                fixture_batch=batch,
                is_active=is_active,
                test_run_id=test_graph.run_id,
            ).consume()
    return _PossibleMatchFixture(
        person_ids=person_ids,
        expected_candidates=expected_candidates,
        relationship_batch_counts={batch: count for batch, _, _, count, _ in relationship_batches},
    )


def _assert_possible_match_fixture_seed(
    test_graph: _TestGraph,
    fixture: _PossibleMatchFixture,
) -> None:
    with test_graph.driver.session() as session:
        node_counts = session.run(
            """
            CALL {
              MATCH (:Person {_person_list_test_run: $test_run_id})
              RETURN count(*) AS person_count
            }
            CALL {
              MATCH (:Identifier {_person_list_test_run: $test_run_id})
              RETURN count(*) AS identifier_count
            }
            RETURN person_count, identifier_count
            """,
            test_run_id=test_graph.run_id,
        ).single(strict=True)
        batch_rows = session.run(
            """
            MATCH ()-[rel:IDENTIFIED_BY {_person_list_test_run: $test_run_id}]->()
            RETURN rel.fixture_batch AS fixture_batch, count(rel) AS relationship_count
            ORDER BY fixture_batch
            """,
            test_run_id=test_graph.run_id,
        )
        relationship_counts = {
            str(row["fixture_batch"]): int(row["relationship_count"]) for row in batch_rows
        }
    assert int(node_counts["person_count"]) == 8
    assert int(node_counts["identifier_count"]) == 3
    assert relationship_counts == dict(fixture.relationship_batch_counts)


def _possible_match_candidate_sets(
    test_graph: _TestGraph,
    fixture: _PossibleMatchFixture,
) -> dict[str, tuple[str, ...]]:
    candidate_sets = {person_id: () for person_id in fixture.expected_candidates}
    with test_graph.driver.session() as session:
        rows = session.run(
            """
            MATCH (p:Person {_person_list_test_run: $test_run_id})
            MATCH (other:Person {_person_list_test_run: $test_run_id})
            WHERE p.status <> 'merged'
              AND other.person_id <> p.person_id
              AND other.status <> 'merged'
              AND EXISTS {
                MATCH (p)-[p_rel:IDENTIFIED_BY]->(:Identifier {
                  _person_list_test_run: $test_run_id
                })<-[other_rel:IDENTIFIED_BY]-(other)
                WHERE coalesce(p_rel.is_active, true) = true
                  AND coalesce(other_rel.is_active, true) = true
              }
            RETURN p.person_id AS person_id, collect(other.person_id) AS candidate_ids
            """,
            test_run_id=test_graph.run_id,
        )
        for row in rows:
            candidate_sets[str(row["person_id"])] = tuple(
                sorted(str(item) for item in row["candidate_ids"])
            )
    return candidate_sets


def test_possible_match_count_deduplicates_parallel_identifier_paths(
    neo4j_driver: _TestGraph,
) -> None:
    fixture = _seed_possible_match_cardinality_graph(neo4j_driver)
    _assert_possible_match_fixture_seed(neo4j_driver, fixture)
    candidate_sets = _possible_match_candidate_sets(neo4j_driver, fixture)
    assert candidate_sets == dict(fixture.expected_candidates)

    filters = frozenset({"crm_deal_count_min"})
    parameters = {"crm_deal_count_min": 1_000}
    crm_rows = _list_rows(
        neo4j_driver,
        build_list_persons_query(
            "crm_deal_count",
            "desc",
            has_q=False,
            active_filters=filters,
        ),
        **parameters,
    )
    listed_counts = {
        _row_person_id(row): int(row["possible_match_count"])
        for row in crm_rows
        if _row_person_id(row) in fixture.expected_candidates
    }
    assert listed_counts == {
        person_id: len(candidate_ids)
        for person_id, candidate_ids in fixture.expected_candidates.items()
    }

    for person_id, expected_candidates in fixture.expected_candidates.items():
        with neo4j_driver.driver.session() as session:
            legacy_count = session.run(
                COUNT_PERSON_SHARED_IDENTIFIERS,
                person_id=person_id,
            ).single(strict=True)["total"]
        assert int(legacy_count) == len(expected_candidates)

    sort_query = build_list_persons_query(
        "possible_match_count",
        "desc",
        has_q=False,
        active_filters=filters,
    )
    pages = [
        _list_rows(neo4j_driver, sort_query, skip=skip, limit=2, **parameters)
        for skip in range(0, 8, 2)
    ]
    all_page_ids = [person_id for page in pages for person_id in _person_ids(page)]
    fixture_id_set = set(fixture.person_ids.values())
    returned_fixture_ids = [person_id for person_id in all_page_ids if person_id in fixture_id_set]
    expected_non_merged_ids = set(fixture.expected_candidates)
    assert fixture.person_ids["merged"] not in returned_fixture_ids
    assert set(returned_fixture_ids) == expected_non_merged_ids
    assert len(returned_fixture_ids) == len(set(returned_fixture_ids))

    expected_order = sorted(
        fixture.expected_candidates,
        key=lambda person_id: (-len(fixture.expected_candidates[person_id]), person_id),
    )
    assert returned_fixture_ids == expected_order
