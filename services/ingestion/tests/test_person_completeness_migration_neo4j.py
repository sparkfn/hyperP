"""Disposable Neo4j behavior coverage for the Person completeness repair."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from neo4j import Driver, GraphDatabase
from src.graph import migrations
from src.graph.client import Neo4jClient


@dataclass(frozen=True)
class _TestGraph:
    driver: Driver
    run_id: str


@pytest.fixture
def neo4j_driver() -> Iterator[_TestGraph]:
    uri = os.getenv("HYPERP_NEO4J_PERSON_COMPLETENESS_TEST_URI")
    if uri is None:
        pytest.skip("disposable person-completeness Neo4j test database is not configured")
    host = urlparse(uri).hostname
    service_host = os.getenv("HYPERP_NEO4J_PERSON_COMPLETENESS_TEST_SERVICE_HOST")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if service_host is not None:
        allowed_hosts.add(service_host)
    if host not in allowed_hosts:
        pytest.fail("person-completeness tests only accept an explicitly configured Neo4j host")
    password = os.getenv("HYPERP_NEO4J_PERSON_COMPLETENESS_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_PERSON_COMPLETENESS_TEST_PASSWORD is required")

    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_PERSON_COMPLETENESS_TEST_USER", "neo4j"), password),
    )
    test_graph: _TestGraph | None = None
    try:
        for _ in range(15):
            try:
                driver.verify_connectivity()
                break
            except Exception:  # noqa: BLE001 - disposable service readiness retry
                time.sleep(1)
        else:
            pytest.fail("disposable person-completeness Neo4j database did not become ready")
        with driver.session() as session:
            existing = session.run("MATCH (node) RETURN count(node) AS total").single(strict=True)
        if existing["total"] != 0:
            pytest.fail("person-completeness integration test requires an empty database")
        with driver.session() as session:
            session.run(
                """CREATE CONSTRAINT person_id_unique IF NOT EXISTS
                FOR (person:Person) REQUIRE person.person_id IS UNIQUE"""
            ).consume()
            session.run(
                """CREATE CONSTRAINT data_migration_key_unique IF NOT EXISTS
                FOR (migration:DataMigration) REQUIRE migration.migration_key IS UNIQUE"""
            ).consume()
        test_graph = _TestGraph(driver=driver, run_id=uuid4().hex)
        yield test_graph
    finally:
        if test_graph is not None:
            with driver.session() as session:
                session.run("MATCH (node) DETACH DELETE node").consume()
        driver.close()


class _Client:
    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    def execute_read(self, work: object) -> object:
        with self.driver.session() as session:
            return session.execute_read(cast("object", work))  # type: ignore[arg-type]

    def execute_write(self, work: object) -> object:
        with self.driver.session() as session:
            return session.execute_write(cast("object", work))  # type: ignore[arg-type]


class _InterruptAfterFirstBatchClient(_Client):
    def __init__(self, driver: Driver) -> None:
        super().__init__(driver)
        self._write_count = 0

    def execute_write(self, work: object) -> object:
        result = super().execute_write(work)
        self._write_count += 1
        if self._write_count == 2:
            raise RuntimeError("simulated interruption after first committed batch")
        return result


def _seed_graph(test_graph: _TestGraph) -> None:
    with test_graph.driver.session() as session:
        session.run(
            """
            UNWIND [
              {person_id: 'zero', status: 'active'},
              {person_id: 'one', status: 'active', preferred_full_name: 'One'},
              {
                person_id: 'three', status: 'active', preferred_full_name: 'Three',
                preferred_phone: '+6500000000', preferred_email: 'three@example.com'
              },
              {
                person_id: 'five', status: 'active', preferred_full_name: 'Five',
                preferred_phone: '+6500000001', preferred_email: 'five@example.com',
                preferred_dob: '2000-01-02', preferred_address_id: 'address-five'
              },
              {person_id: 'suppressed', status: 'suppressed', preferred_phone: '+6500000002'},
              {person_id: 'existing', status: 'active', profile_completeness_score: 0.8},
              {
                person_id: 'invalid-string', status: 'active',
                profile_completeness_score: 'bad', preferred_dob: '2001-01-01'
              },
              {
                person_id: 'invalid-high', status: 'active',
                profile_completeness_score: 2.0, preferred_email: 'high@example.com'
              },
              {
                person_id: 'invalid-bool', status: 'active',
                profile_completeness_score: true, preferred_phone: '+6500000003'
              },
              {
                person_id: 'invalid-low', status: 'active',
                profile_completeness_score: -0.1, preferred_address_id: 'address-low'
              },
              {
                person_id: 'invalid-nan', status: 'active',
                profile_completeness_score: $nan_score, preferred_full_name: 'NaN'
              },
              {person_id: 'merged', status: 'merged', profile_completeness_score: 'bad'}
            ] AS row
            CREATE (p:Person)
            SET p += row, p._person_completeness_test_run = $test_run_id
            """,
            test_run_id=test_graph.run_id,
            nan_score=float("nan"),
        ).consume()


def _scores(test_graph: _TestGraph) -> dict[str, float | str | bool | None]:
    with test_graph.driver.session() as session:
        rows = session.run(
            """
            MATCH (p:Person {_person_completeness_test_run: $test_run_id})
            RETURN p.person_id AS person_id, p.profile_completeness_score AS score
            """,
            test_run_id=test_graph.run_id,
        )
        return {str(row["person_id"]): row["score"] for row in rows}


def test_backfill_repairs_invalid_non_merged_scores_and_is_idempotent(
    neo4j_driver: _TestGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_graph(neo4j_driver)
    monkeypatch.setattr(migrations, "PERSON_COMPLETENESS_MIGRATION_BATCH_SIZE", 2)
    interrupted_client = cast(
        Neo4jClient,
        _InterruptAfterFirstBatchClient(neo4j_driver.driver),
    )
    client = cast(Neo4jClient, _Client(neo4j_driver.driver))

    assert migrations.count_missing_person_completeness_scores(client) == 10
    with pytest.raises(RuntimeError, match="simulated interruption"):
        migrations.backfill_missing_person_completeness_scores(interrupted_client)
    assert migrations.count_missing_person_completeness_scores(client) == 8
    assert migrations.backfill_missing_person_completeness_scores(client) == 8
    assert migrations.count_missing_person_completeness_scores(client) == 0
    assert migrations.backfill_missing_person_completeness_scores(client) == 0

    with neo4j_driver.driver.session() as session:
        session.run(
            """
            CREATE (:Person {
              person_id: 'late-invalid', status: 'active', preferred_email: 'late@example.com',
              _person_completeness_test_run: $test_run_id
            })
            """,
            test_run_id=neo4j_driver.run_id,
        ).consume()

    assert migrations.backfill_missing_person_completeness_scores(client) == 0
    assert migrations.count_missing_person_completeness_scores(client) == 1
    assert (
        migrations.backfill_missing_person_completeness_scores(
            client,
            skip_if_completed=False,
        )
        == 1
    )
    assert migrations.count_missing_person_completeness_scores(client) == 0

    assert _scores(neo4j_driver) == {
        "existing": 0.8,
        "five": 1.0,
        "invalid-bool": 0.2,
        "invalid-high": 0.2,
        "invalid-low": 0.2,
        "invalid-nan": 0.2,
        "invalid-string": 0.2,
        "late-invalid": 0.2,
        "merged": "bad",
        "one": 0.2,
        "suppressed": 0.2,
        "three": 0.6,
        "zero": 0.0,
    }
