"""Neo4j 5.26 regression and feasibility coverage for #416's schema transition."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, Session
from neo4j.exceptions import ClientError
from src import main
from src.config import Settings
from src.graph.client import Neo4jClient
from src.graph.identifier_scope_schema import (
    BRIDGE_INDEX_NAME,
    IDENTIFIER_SCOPE_CONSTRAINT_NAME,
    LEGACY_INDEX_NAME,
    apply_identifier_scope_schema_transition,
)

_ENV_PREFIX = "HYPERP_NEO4J_CONTROL_MIGRATION_TEST"
_SCHEMA_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class _PlanLike(Protocol):
    operator_type: str
    arguments: dict[str, object]
    children: list[_PlanLike]


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
        pytest.fail("identifier schema tests require complete disposable Neo4j configuration")
    return _Configuration(
        uri=cast(str, values["uri"]),
        user=cast(str, values["user"]),
        password=cast(str, values["password"]),
        service_host=cast(str, values["service_host"]),
    )


def _validate_target(configuration: _Configuration) -> None:
    parsed = urlparse(configuration.uri)
    if parsed.scheme != "bolt":
        pytest.fail("identifier schema tests require a direct Bolt URI")
    if parsed.username is not None or parsed.password is not None:
        pytest.fail("identifier schema tests must not embed credentials in the URI")
    if configuration.service_host != configuration.service_host.strip():
        pytest.fail("identifier schema tests require a valid disposable service host")
    allowed_hosts = {"localhost", "127.0.0.1", "::1", configuration.service_host}
    if parsed.hostname not in allowed_hosts:
        pytest.fail("identifier schema tests require an explicitly disposable Neo4j host")


def _require_neo4j_526(driver: Driver) -> None:
    with driver.session() as session:
        row = session.run(
            "CALL dbms.components() YIELD versions RETURN versions[0] AS version"
        ).single(strict=True)
    version = row["version"]
    if not isinstance(version, str) or version.split(".")[:2] != ["5", "26"]:
        pytest.fail("identifier schema tests require Neo4j 5.26")


def _prepare_empty_owned_schema(driver: Driver) -> None:
    """Claim only a validated target with no graph data, then reset its schema.

    Earlier suites in the shared CI shard deliberately leave schema behind.
    A zero-node guard runs before any mutation so this fixture cannot erase
    data from an accidentally shared target. Default LOOKUP indexes remain.
    """
    _assert_no_nodes(driver)
    _clear_non_lookup_schema(driver)
    _assert_schema_clean(driver)


def _assert_no_nodes(driver: Driver) -> None:
    with driver.session() as session:
        node_count = session.run("MATCH (node) RETURN count(node) AS count").single(strict=True)[
            "count"
        ]
    if node_count != 0:
        pytest.fail("identifier schema tests require a zero-node disposable Neo4j database")


def _assert_schema_clean(driver: Driver) -> None:
    with driver.session() as session:
        constraint_count = session.run(
            "SHOW CONSTRAINTS YIELD name RETURN count(name) AS count"
        ).single(strict=True)["count"]
        index_count = session.run(
            "SHOW INDEXES YIELD type WHERE type <> 'LOOKUP' RETURN count(*) AS count"
        ).single(strict=True)["count"]
    if constraint_count != 0 or index_count != 0:
        pytest.fail("identifier schema tests could not establish clean schema ownership")


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    configuration = _configuration()
    if configuration is None:
        pytest.skip("disposable Neo4j 5.26 control migration database is not configured")
    _validate_target(configuration)
    driver = GraphDatabase.driver(
        configuration.uri, auth=(configuration.user, configuration.password)
    )
    cleanup_owned = False
    try:
        driver.verify_connectivity()
        _require_neo4j_526(driver)
        _prepare_empty_owned_schema(driver)
        cleanup_owned = True
        yield driver
    finally:
        try:
            if cleanup_owned:
                _cleanup(driver)
        finally:
            driver.close()


def _cleanup(driver: Driver) -> None:
    with driver.session() as session:
        session.run("MATCH (node) DETACH DELETE node").consume()
    _clear_non_lookup_schema(driver)


def _clear_non_lookup_schema(driver: Driver) -> None:
    with driver.session() as session:
        constraint_names = [
            row["name"] for row in session.run("SHOW CONSTRAINTS YIELD name RETURN name")
        ]
        for name in constraint_names:
            _drop_schema_name(session, "CONSTRAINT", name)
        index_rows = session.run("SHOW INDEXES YIELD name, type RETURN name, type")
        index_names = [row["name"] for row in index_rows if row["type"] != "LOOKUP"]
        for name in index_names:
            _drop_schema_name(session, "INDEX", name)


def _drop_schema_name(session: Session, kind: str, name: object) -> None:
    if not isinstance(name, str) or _SCHEMA_NAME.fullmatch(name) is None:
        raise AssertionError(f"unexpected schema name during owned cleanup: {name!r}")
    session.run(f"DROP {kind} {name} IF EXISTS").consume()


def _await_indexes(driver: Driver) -> None:
    with driver.session() as session:
        session.run("CALL db.awaitIndexes(120)").consume()


def _create_legacy_index(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            "CREATE INDEX idx_identifier_type_scope_norm IF NOT EXISTS "
            "FOR (id:Identifier) ON (id.identifier_type, id.identifier_scope, id.normalized_value)"
        ).consume()
    _await_indexes(driver)


def _create_bridge_index(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            "CREATE INDEX idx_identifier_scope_norm_type_bridge IF NOT EXISTS "
            "FOR (id:Identifier) ON (id.identifier_scope, id.normalized_value, id.identifier_type)"
        ).consume()
    _await_indexes(driver)


def _schema_rows(
    driver: Driver,
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    with driver.session() as session:
        indexes = {
            cast(str, row["name"]): dict(row)
            for row in session.run(
                "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, state, "
                "owningConstraint RETURN name, type, entityType, labelsOrTypes, properties, "
                "state, owningConstraint"
            )
        }
        constraints = {
            cast(str, row["name"]): dict(row)
            for row in session.run(
                "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties, "
                "ownedIndex RETURN name, type, entityType, labelsOrTypes, properties, ownedIndex"
            )
        }
    return indexes, constraints


def _seed_identifier(driver: Driver, *, scope: str = "portal-a", value: str = "42") -> None:
    with driver.session() as session:
        session.run(
            "CREATE (person:Person {person_id: 'person-1'}), "
            "(identifier:Identifier {identifier_id: 'identifier-1', "
            "identifier_type: 'crm_contact_id', "
            "identifier_scope: $scope, normalized_value: $value}), "
            "(person)-[:IDENTIFIED_BY {source_record_pk: 'provenance-1'}]->(identifier)",
            scope=scope,
            value=value,
        ).consume()


def _plan_text(driver: Driver) -> str:
    with driver.session() as session:
        summary = session.run(
            "EXPLAIN MATCH (identifier:Identifier) "
            "WHERE identifier.identifier_type = $identifier_type "
            "AND identifier.identifier_scope = $identifier_scope "
            "AND identifier.normalized_value = $normalized_value RETURN identifier",
            identifier_type="crm_contact_id",
            identifier_scope="portal-a",
            normalized_value="42",
        ).consume()
    return _render_plan(cast(_PlanLike, summary.plan))


def _render_plan(plan: _PlanLike) -> str:
    return " ".join(
        [
            plan.operator_type,
            str(plan.arguments),
            *[_render_plan(child) for child in plan.children],
        ]
    )


def _transition(driver: Driver) -> int:
    return apply_identifier_scope_schema_transition(cast(Neo4jClient, _DriverClient(driver)))


class _DriverClient:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def execute_read(self, work: object) -> object:
        with self._driver.session() as session:
            return cast(object, work)(session)  # type: ignore[operator]

    def session(self) -> object:
        return self._driver.session()


def test_neo4j_526_bridge_feasibility_gate_proves_safe_ddl_and_lookup(
    neo4j_driver: Driver,
) -> None:
    _seed_identifier(neo4j_driver)
    _create_legacy_index(neo4j_driver)
    _create_bridge_index(neo4j_driver)

    indexes, constraints = _schema_rows(neo4j_driver)
    assert indexes[LEGACY_INDEX_NAME]["state"] == "ONLINE"
    assert indexes[BRIDGE_INDEX_NAME]["state"] == "ONLINE"
    assert IDENTIFIER_SCOPE_CONSTRAINT_NAME not in constraints

    with neo4j_driver.session() as session:
        session.run("DROP INDEX idx_identifier_type_scope_norm IF EXISTS").consume()
    bridge_plan = _plan_text(neo4j_driver)
    assert "NodeIndexSeek" in bridge_plan
    assert "identifier_scope" in bridge_plan
    assert "normalized_value" in bridge_plan
    assert "identifier_type" in bridge_plan

    with neo4j_driver.session() as session:
        session.run(
            "CREATE CONSTRAINT identifier_identity_scope_unique IF NOT EXISTS "
            "FOR (id:Identifier) REQUIRE "
            "(id.identifier_type, id.identifier_scope, id.normalized_value) IS UNIQUE"
        ).consume()
    _await_indexes(neo4j_driver)
    indexes, constraints = _schema_rows(neo4j_driver)
    assert LEGACY_INDEX_NAME not in indexes
    assert indexes[BRIDGE_INDEX_NAME]["state"] == "ONLINE"
    assert constraints[IDENTIFIER_SCOPE_CONSTRAINT_NAME]["ownedIndex"] in indexes


def test_complete_initialization_transitions_legacy_schema_and_full_rerun_is_idempotent(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = _configuration()
    assert configuration is not None
    _seed_identifier(neo4j_driver)
    _create_legacy_index(neo4j_driver)
    settings = Settings(
        neo4j_uri=configuration.uri,
        neo4j_user=configuration.user,
        neo4j_password=configuration.password,
        _env_file=None,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    main.initialize_ingestion_graph()
    first_indexes, first_constraints = _schema_rows(neo4j_driver)
    main.initialize_ingestion_graph()
    second_indexes, second_constraints = _schema_rows(neo4j_driver)

    assert LEGACY_INDEX_NAME not in second_indexes
    assert BRIDGE_INDEX_NAME not in second_indexes
    backing_index = first_constraints[IDENTIFIER_SCOPE_CONSTRAINT_NAME]["ownedIndex"]
    assert isinstance(backing_index, str)
    assert first_indexes[backing_index]["state"] == "ONLINE"
    assert (
        second_constraints[IDENTIFIER_SCOPE_CONSTRAINT_NAME]
        == first_constraints[IDENTIFIER_SCOPE_CONSTRAINT_NAME]
    )
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (:Person {person_id: 'person-1'})-[relationship:IDENTIFIED_BY]->"
            "(identifier:Identifier {identifier_id: 'identifier-1'}) "
            "RETURN relationship.source_record_pk AS provenance, count(identifier) AS count"
        ).single(strict=True)
    assert row["provenance"] == "provenance-1"
    assert row["count"] == 1


def test_duplicate_tuple_rejection_preserves_bridge_lookup_coverage(neo4j_driver: Driver) -> None:
    _seed_identifier(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:Identifier {identifier_id: 'identifier-2', "
            "identifier_type: 'crm_contact_id', "
            "identifier_scope: 'portal-a', normalized_value: '42'})"
        ).consume()
    _create_legacy_index(neo4j_driver)

    with pytest.raises(ClientError):
        _transition(neo4j_driver)

    indexes, constraints = _schema_rows(neo4j_driver)
    assert LEGACY_INDEX_NAME not in indexes
    assert BRIDGE_INDEX_NAME in indexes
    assert IDENTIFIER_SCOPE_CONSTRAINT_NAME not in constraints
    assert "NodeIndexSeek" in _plan_text(neo4j_driver)


def test_scoped_constraint_keeps_distinct_scopes_and_rejects_duplicates(
    neo4j_driver: Driver,
) -> None:
    _seed_identifier(neo4j_driver, scope="portal-a", value="42")
    _create_legacy_index(neo4j_driver)
    assert _transition(neo4j_driver) == 1

    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:Identifier {identifier_id: 'identifier-2', "
            "identifier_type: 'crm_contact_id', "
            "identifier_scope: 'portal-b', normalized_value: '42'})"
        ).consume()
        with pytest.raises(ClientError):
            session.run(
                "CREATE (:Identifier {identifier_id: 'identifier-3', "
                "identifier_type: 'crm_contact_id', "
                "identifier_scope: 'portal-a', normalized_value: '42'})"
            ).consume()


def test_unexpected_legacy_definition_fails_without_removing_lookup(neo4j_driver: Driver) -> None:
    _seed_identifier(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE INDEX idx_identifier_type_scope_norm IF NOT EXISTS "
            "FOR (id:Identifier) ON (id.identifier_type, id.normalized_value)"
        ).consume()
    _await_indexes(neo4j_driver)

    with pytest.raises(RuntimeError, match="legacy scoped Identifier index has an unexpected"):
        _transition(neo4j_driver)

    indexes, _constraints = _schema_rows(neo4j_driver)
    assert LEGACY_INDEX_NAME in indexes
    assert BRIDGE_INDEX_NAME not in indexes


@pytest.mark.parametrize("state", ("bridge_only", "constraint_cleanup"))
def test_crash_retry_states_converge(neo4j_driver: Driver, state: str) -> None:
    _seed_identifier(neo4j_driver)
    _create_bridge_index(neo4j_driver)
    if state == "constraint_cleanup":
        with neo4j_driver.session() as session:
            session.run(
                "CREATE CONSTRAINT identifier_identity_scope_unique IF NOT EXISTS "
                "FOR (id:Identifier) REQUIRE "
                "(id.identifier_type, id.identifier_scope, id.normalized_value) IS UNIQUE"
            ).consume()
        _await_indexes(neo4j_driver)

    assert _transition(neo4j_driver) == 1
    indexes, constraints = _schema_rows(neo4j_driver)
    assert LEGACY_INDEX_NAME not in indexes
    assert BRIDGE_INDEX_NAME not in indexes
    assert IDENTIFIER_SCOPE_CONSTRAINT_NAME in constraints
