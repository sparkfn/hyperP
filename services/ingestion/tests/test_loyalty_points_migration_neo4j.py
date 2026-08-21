"""Disposable Neo4j coverage for the PHPPOS loyalty property repair."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase
from src.graph.client import Neo4jClient
from src.graph.loyalty_points_migration import (
    count_invalid_loyalty_points,
    repair_loyalty_points,
)


@dataclass(frozen=True)
class _TestGraph:
    driver: Driver


@pytest.fixture
def neo4j_driver() -> Iterator[_TestGraph]:
    uri = os.getenv("HYPERP_NEO4J_LOYALTY_POINTS_TEST_URI")
    if uri is None:
        pytest.skip("disposable loyalty-points Neo4j test database is not configured")
    host = urlparse(uri).hostname
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    service_host = os.getenv("HYPERP_NEO4J_LOYALTY_POINTS_TEST_SERVICE_HOST")
    if service_host is not None:
        allowed_hosts.add(service_host)
    if host not in allowed_hosts:
        pytest.fail("loyalty-points tests require an explicitly configured disposable host")
    password = os.getenv("HYPERP_NEO4J_LOYALTY_POINTS_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_LOYALTY_POINTS_TEST_PASSWORD is required")
    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_LOYALTY_POINTS_TEST_USER", "neo4j"), password),
    )
    try:
        for _ in range(15):
            try:
                driver.verify_connectivity()
                break
            except Exception:  # noqa: BLE001 - disposable service readiness retry
                time.sleep(1)
        else:
            pytest.fail("disposable loyalty-points Neo4j database did not become ready")
        with driver.session() as session:
            existing = session.run("MATCH (node) RETURN count(node) AS total").single(strict=True)
            if existing["total"] != 0:
                pytest.fail("loyalty-points integration test requires an empty database")
            session.run(
                """CREATE CONSTRAINT data_migration_key_unique IF NOT EXISTS
                FOR (migration:DataMigration) REQUIRE migration.migration_key IS UNIQUE"""
            ).consume()
        yield _TestGraph(driver)
    finally:
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


def _seed(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            """
            UNWIND [
              {source: 'eko_phppos:sales', id: '1', used: '14000.0000000000', gained: '5'},
              {source: 'eko_phppos:sales', id: '2', used: '20.5', gained: 7},
              {source: 'speedzone_phppos:sales', id: '3', used: true, gained: 'bad'},
              {source: 'speedzone_phppos:sales', id: '4', used: 20.0, gained: null},
              {source: 'fundbox', id: '5', used: '99.000', gained: 'bad'},
              {source: 'eko_phppos:sales', id: '6', used: 9, gained: null}
            ] AS row
            CREATE (o:Order {source_system_key: row.source, source_order_id: row.id})
            SET o.order_id = randomUUID(), o.points_used = row.used, o.points_gained = row.gained
            """
        ).consume()


def _values(driver: Driver) -> dict[str, tuple[object, object]]:
    with driver.session() as session:
        rows = session.run(
            """MATCH (o:Order) RETURN o.source_order_id AS id,
            o.points_used AS used, o.points_gained AS gained ORDER BY id"""
        )
        return {str(row["id"]): (row["used"], row["gained"]) for row in rows}


def test_repair_is_targeted_idempotent_and_verifies_zero_invalid(
    neo4j_driver: _TestGraph,
) -> None:
    _seed(neo4j_driver.driver)
    client = cast(Neo4jClient, _Client(neo4j_driver.driver))

    before = count_invalid_loyalty_points(client)
    assert before.invalid_order_count == 4
    assert before.invalid_points_used_count == 4
    assert before.invalid_points_gained_count == 2

    assert repair_loyalty_points(client, batch_size=2) == 6
    assert count_invalid_loyalty_points(client).invalid_order_count == 0
    assert repair_loyalty_points(client, batch_size=2) == 0

    assert _values(neo4j_driver.driver) == {
        "1": (14000, 5),
        "2": (None, 7),
        "3": (None, None),
        "4": (20, None),
        "5": ("99.000", "bad"),
        "6": (9, None),
    }
