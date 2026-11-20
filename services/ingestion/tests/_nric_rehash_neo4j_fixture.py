"""Disposable Neo4j fixture and client helpers for NRIC rehash tests."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from _nric_rehash_neo4j_seed import _Dataset
from neo4j import Driver, GraphDatabase, ManagedTransaction, Record
from src.graph.client import Neo4jClient
from src.graph.schema_init import apply_deferred_identifier_scope_constraints, apply_schema
from src.nric_rehash_apply import run_nric_rehash_apply

_ENV_PREFIX = "HYPERP_NEO4J_CONTROL_MIGRATION_TEST"
_TestT = TypeVar("_TestT")


@dataclass(frozen=True)
class _Configuration:
    uri: str
    user: str
    password: str
    service_host: str


def _configuration() -> _Configuration | None:
    values = {
        "uri": os.getenv(f"{_ENV_PREFIX}_URI"),
        "user": os.getenv(f"{_ENV_PREFIX}_USER"),
        "password": os.getenv(f"{_ENV_PREFIX}_PASSWORD"),
        "service_host": os.getenv(f"{_ENV_PREFIX}_SERVICE_HOST"),
    }
    if all(value is None for value in values.values()):
        return None
    if any(value is None or not value.strip() for value in values.values()):
        pytest.fail("NRIC rehash integration requires complete disposable Neo4j settings")
    return _Configuration(
        uri=cast(str, values["uri"]),
        user=cast(str, values["user"]),
        password=cast(str, values["password"]),
        service_host=cast(str, values["service_host"]),
    )


def _validate_target(configuration: _Configuration) -> None:
    parsed = urlparse(configuration.uri)
    allowed_hosts = {"localhost", "127.0.0.1", "::1", configuration.service_host}
    if parsed.scheme != "bolt" or parsed.hostname not in allowed_hosts:
        pytest.fail("NRIC rehash integration requires an explicitly disposable Bolt target")
    if parsed.username is not None or parsed.password is not None:
        pytest.fail("NRIC rehash integration URI must not embed credentials")


def _require_neo4j_526(driver: Driver) -> None:
    with driver.session() as session:
        row = session.run(
            "CALL dbms.components() YIELD versions RETURN versions[0] AS version"
        ).single(strict=True)
    version = row["version"]
    if not isinstance(version, str) or version.split(".")[:2] != ["5", "26"]:
        pytest.fail("NRIC rehash integration requires Neo4j 5.26")


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    configuration = _configuration()
    if configuration is None:
        pytest.skip("disposable Neo4j control migration database is not configured")
    _validate_target(configuration)
    driver = GraphDatabase.driver(
        configuration.uri, auth=(configuration.user, configuration.password)
    )
    cleanup_owned = False
    try:
        driver.verify_connectivity()
        _require_neo4j_526(driver)
        with driver.session() as session:
            nodes = session.run(
                "MATCH (node) WHERE NOT node:PersonListSummaryCounter RETURN count(node) AS count"
            ).single(strict=True)["count"]
        if nodes != 0:
            pytest.fail("NRIC rehash integration database must be empty")
        cleanup_owned = True
        client = _Client(driver)
        apply_schema(client)
        apply_deferred_identifier_scope_constraints(client)
        yield driver
    finally:
        try:
            if cleanup_owned:
                with driver.session() as session:
                    session.run(
                        "MATCH (node) WHERE NOT node:PersonListSummaryCounter DETACH DELETE node"
                    ).consume()
        finally:
            driver.close()


class _Client(Neo4jClient):
    def __init__(self, driver: Driver) -> None:
        self._driver = driver
        self._bounded_timeout_seconds = None


class _StealingClient(_Client):
    def __init__(self, driver: Driver, replacement_owner: str) -> None:
        super().__init__(driver)
        self.write_count = 0
        self.replacement_owner = replacement_owner

    def execute_write(self, work: Callable[[ManagedTransaction], _TestT]) -> _TestT:
        result = super().execute_write(work)
        self.write_count += 1
        if self.write_count == 2:
            with self._driver.session() as session:
                session.run(
                    "MATCH (migration:DataMigration {migration_key: $migration_key}) "
                    "SET migration.lock_version = migration.lock_version + 1, "
                    "migration.lease_owner = $owner, "
                    "migration.lease_until = datetime() + duration({seconds: 300})",
                    migration_key="nric_hmac_rehash_v1",
                    owner=self.replacement_owner,
                ).consume()
        return result


def _database_name(driver: Driver) -> str:
    with driver.session() as session:
        row = session.run("CALL db.info() YIELD name RETURN name").single(strict=True)
    name = row["name"]
    if not isinstance(name, str):
        raise AssertionError("database name result was malformed")
    return name


def _counter(driver: Driver, query: str, **parameters: object) -> int:
    with driver.session() as session:
        row: Record = session.run(query, **parameters).single(strict=True)
    value: object = row["count"]
    if not isinstance(value, int) or isinstance(value, bool):
        raise AssertionError("count result was malformed")
    return value


def _counter_record(driver: Driver) -> dict[str, object]:
    with driver.session() as session:
        row = session.run(
            "MATCH (migration:DataMigration {migration_key: $key}) "
            "RETURN properties(migration) AS properties",
            key="nric_hmac_rehash_v1",
        ).single(strict=True)
    properties = row["properties"]
    if not isinstance(properties, dict):
        raise AssertionError("migration properties were malformed")
    return dict(properties)


def _clear_owned_graph(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (node) WHERE NOT node:PersonListSummaryCounter DETACH DELETE node"
        ).consume()


def _apply(
    driver: Driver,
    dataset: _Dataset,
    *,
    batch_size: int = 2,
    expected_database: str | None = None,
    client: Neo4jClient | None = None,
) -> tuple[dict[str, object], int]:
    report, status = run_nric_rehash_apply(
        client or _Client(driver),
        batch_size=batch_size,
        key=dataset.key,
        key_version=dataset.key_version,
        expected_database=expected_database or _database_name(driver),
        acknowledge_offline=True,
    )
    return cast(dict[str, object], report), status
