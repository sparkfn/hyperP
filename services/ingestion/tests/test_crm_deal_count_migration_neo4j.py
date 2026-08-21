"""Disposable Neo4j coverage for the Person CRM deal-count projection."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, Session
from src.graph.client import Neo4jClient
from src.graph.crm_deal_count import inspect_crm_deal_count_invariant, repair_crm_deal_counts


@dataclass(frozen=True)
class _TestGraph:
    driver: Driver


@pytest.fixture
def neo4j_driver() -> Iterator[_TestGraph]:
    uri = os.getenv("HYPERP_NEO4J_CRM_DEAL_COUNT_TEST_URI")
    if uri is None:
        pytest.skip("disposable CRM deal-count Neo4j test database is not configured")
    host = urlparse(uri).hostname
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    service_host = os.getenv("HYPERP_NEO4J_CRM_DEAL_COUNT_TEST_SERVICE_HOST")
    if service_host is not None:
        allowed_hosts.add(service_host)
    if host not in allowed_hosts:
        pytest.fail("CRM deal-count tests require an explicitly configured disposable host")
    password = os.getenv("HYPERP_NEO4J_CRM_DEAL_COUNT_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_CRM_DEAL_COUNT_TEST_PASSWORD is required")
    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_CRM_DEAL_COUNT_TEST_USER", "neo4j"), password),
    )
    try:
        for _ in range(15):
            try:
                driver.verify_connectivity()
                break
            except Exception:  # noqa: BLE001 - disposable service readiness retry
                time.sleep(1)
        else:
            pytest.fail("disposable CRM deal-count Neo4j database did not become ready")
        with driver.session() as session:
            existing = session.run("MATCH (node) RETURN count(node) AS total").single(strict=True)
            if existing["total"] != 0:
                pytest.fail("CRM deal-count integration test requires an empty database")
            session.run(
                """CREATE CONSTRAINT data_migration_key_unique IF NOT EXISTS
                FOR (migration:DataMigration) REQUIRE migration.migration_key IS UNIQUE"""
            ).consume()
        yield _TestGraph(driver)
    finally:
        with driver.session() as session:
            session.run("DROP INDEX idx_person_crm_deal_count IF EXISTS").consume()
            session.run("MATCH (node) DETACH DELETE node").consume()
        driver.close()


class _Client:
    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    @contextmanager
    def session(self, **kwargs: Any) -> Iterator[Session]:
        with self.driver.session(**kwargs) as session:
            yield session

    def execute_read(self, work: object) -> object:
        with self.driver.session() as session:
            return session.execute_read(cast("object", work))  # type: ignore[arg-type]

    def execute_write(self, work: object) -> object:
        with self.driver.session() as session:
            return session.execute_write(cast("object", work))  # type: ignore[arg-type]


def _seed(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            """
            CREATE (source:SourceSystem {source_key: 'bitrix_chat'})
            CREATE (missing:Person {person_id: 'missing', status: 'active'})
            CREATE (stale:Person {person_id: 'stale', status: 'active', crm_deal_count: 9})
            CREATE (zero:Person {person_id: 'zero', status: 'active', crm_deal_count: 0})
            CREATE (active:SourceRecord {
              source_record_pk: 'active', record_type: 'crm_deal', lifecycle_status: 'active'
            })-[:FROM_SOURCE]->(source)
            CREATE (legacy:SourceRecord {
              source_record_pk: 'legacy', record_type: 'crm_deal', is_latest: true
            })-[:FROM_SOURCE]->(source)
            CREATE (ignored:SourceRecord {
              source_record_pk: 'ignored', record_type: 'crm_deal', lifecycle_status: 'superseded'
            })-[:FROM_SOURCE]->(source)
            CREATE (active)-[:LINKED_TO {is_active: true}]->(missing)
            CREATE (legacy)-[:LINKED_TO {is_active: true}]->(missing)
            CREATE (ignored)-[:LINKED_TO {is_active: true}]->(missing)
            CREATE (active)-[:LINKED_TO {is_active: true}]->(stale)
            """
        ).consume()


def _counts(driver: Driver) -> dict[str, int]:
    with driver.session() as session:
        rows = session.run(
            "MATCH (person:Person) RETURN person.person_id AS id, "
            "person.crm_deal_count AS count ORDER BY id"
        )
        return {str(row["id"]): int(row["count"]) for row in rows}


def test_backfill_repairs_missing_and_stale_counts_and_is_idempotent(
    neo4j_driver: _TestGraph,
) -> None:
    _seed(neo4j_driver.driver)
    client = cast(Neo4jClient, _Client(neo4j_driver.driver))

    before = inspect_crm_deal_count_invariant(client)
    assert before.invalid_person_count == 1
    assert before.drifted_person_count == 1
    assert before.index_online is False

    assert repair_crm_deal_counts(client, batch_size=2) == 3
    assert inspect_crm_deal_count_invariant(client).valid is True
    assert _counts(neo4j_driver.driver) == {"missing": 2, "stale": 1, "zero": 0}
    assert repair_crm_deal_counts(client, batch_size=2, force=False) == 0

    with neo4j_driver.driver.session() as session:
        session.run("CREATE (:Person {person_id: 'uninitialized', status: 'active'})").consume()
    after_new_person = inspect_crm_deal_count_invariant(client)
    assert after_new_person.invalid_person_count == 1
    assert after_new_person.valid is False
