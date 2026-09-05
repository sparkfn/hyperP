"""Disposable real-Neo4j readiness coverage for standalone CRM Lane A schema."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from neo4j.exceptions import ServiceUnavailable
from src.graph import standalone_crm_lane_a_migration as lane_a_migration
from src.graph.bootstrap import bootstrap_legacy_bitrix_source_instance
from src.graph.client import Neo4jClient
from src.graph.ingestion_control_instance_migration import migrate_ingestion_control_instances
from src.graph.queries.standalone_crm_lane_a_contracts import (
    CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS,
)
from src.graph.standalone_crm_census_migration import ensure_standalone_crm_census_ready
from tests.test_standalone_crm_source_fact_neo4j import run_source_fact_neo4j_cases

T = TypeVar("T")

_MIGRATION_CONSTRAINT = (
    "CREATE CONSTRAINT data_migration_key_unique IF NOT EXISTS "
    "FOR (migration:DataMigration) REQUIRE migration.migration_key IS UNIQUE"
)
_REGISTRY_CONSTRAINT = (
    "CREATE CONSTRAINT bitrix_source_instance_identity_unique IF NOT EXISTS "
    "FOR (instance:BitrixSourceInstance) "
    "REQUIRE (instance.source_key, instance.source_instance_id) IS UNIQUE"
)
_LANE_A_SCHEMA_NAMES = tuple(
    statement.split()[2] for statement in CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS
)
_LANE_A_DOMAIN_LABELS = (
    "CrmCompanyReference",
    "CrmCompanyDescriptionObservation",
    "CrmCompanyDescriptionHead",
    "CrmCompanyMembershipSnapshot",
    "CrmCompanyMembershipObservation",
    "CrmCompanyMembershipHead",
    "CrmTenantMappingScopeCounter",
    "CrmTenantMappingRevision",
    "CrmTenantMappingEntry",
    "CrmTenantMappingTarget",
    "CrmTenantMappingActiveHead",
    "CrmTenantProjectionScopeCounter",
    "CrmTenantProjectionRelease",
    "CrmTenantProjectionInput",
    "CrmTenantProjectionDecision",
    "CrmTenantProjectionAssociation",
    "CrmTenantProjectionSupport",
    "CrmTenantProjectionActiveHead",
)


class _Client:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._driver.session() as session:
            yield session

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        with self._driver.session() as session:
            return session.execute_read(work)

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        with self._driver.session() as session:
            return session.execute_write(work)


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    uri = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_URI")
    password = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_PASSWORD")
    if uri is None or password is None:
        pytest.skip("disposable standalone CRM Lane A Neo4j database is not configured")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    service_host = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_SERVICE_HOST")
    if service_host:
        allowed_hosts.add(service_host)
    if urlparse(uri).hostname not in allowed_hosts:
        pytest.fail("standalone CRM Lane A tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_USER", "neo4j"), password),
    )
    ready = False
    try:
        _wait_for_driver(driver)
        ready = True
        _delete_lane_a_artifacts(driver)
        yield driver
    finally:
        try:
            if ready:
                _delete_lane_a_artifacts(driver)
        finally:
            driver.close()


def _wait_for_driver(driver: Driver) -> None:
    for _ in range(15):
        try:
            driver.verify_connectivity()
            return
        except ServiceUnavailable:
            time.sleep(1)
    pytest.fail("disposable standalone CRM Lane A Neo4j database did not become ready")


def _delete_lane_a_artifacts(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            "DROP CONSTRAINT "
            + lane_a_migration.LEGACY_MEMBERSHIP_SNAPSHOT_SCOPE_DIGEST_CONSTRAINT
            + " IF EXISTS"
        ).consume()
        for statement in CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS:
            schema_kind = "CONSTRAINT" if "CONSTRAINT" in statement else "INDEX"
            session.run(f"DROP {schema_kind} {statement.split()[2]} IF EXISTS").consume()
        session.run(
            "MATCH (migration:DataMigration {migration_key: $migration_key}) "
            "DETACH DELETE migration",
            migration_key=lane_a_migration.MIGRATION_KEY,
        ).consume()
        session.run(
            "MATCH (node) WHERE any(label IN labels(node) WHERE label IN $labels) "
            "DETACH DELETE node",
            labels=_LANE_A_DOMAIN_LABELS,
        ).consume()


def _install_census_prerequisite(driver: Driver) -> _Client:
    client = _Client(driver)
    with driver.session() as session:
        session.run(_MIGRATION_CONSTRAINT).consume()
        session.run(_REGISTRY_CONSTRAINT).consume()
        session.run(
            "MERGE (source:SourceSystem {source_key: 'bitrix_chat'}) SET source.is_active = true"
        ).consume()
    typed_client = cast(Neo4jClient, client)
    migrate_ingestion_control_instances(
        typed_client,
        ensure_legacy_registration=lambda: bootstrap_legacy_bitrix_source_instance(typed_client),
    )
    ensure_standalone_crm_census_ready(typed_client)
    return client


def _marker_is_complete(driver: Driver) -> bool:
    with driver.session() as session:
        record = session.run(
            "MATCH (migration:DataMigration {migration_key: $migration_key}) "
            "RETURN migration.completed_at IS NOT NULL AS ready",
            migration_key=lane_a_migration.MIGRATION_KEY,
        ).single()
    return record is not None and record["ready"] is True


def _lane_a_schema_names(driver: Driver) -> set[str]:
    with driver.session() as session:
        constraints = session.run("SHOW CONSTRAINTS YIELD name RETURN name")
        indexes = session.run("SHOW INDEXES YIELD name RETURN name")
        return {str(row["name"]) for row in constraints} | {str(row["name"]) for row in indexes}


def test_readiness_requires_273_before_schema_or_marker(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(neo4j_driver)

    def _reject_prerequisite(_client: Neo4jClient) -> None:
        raise RuntimeError("#273 is incomplete")

    monkeypatch.setattr(
        lane_a_migration,
        "assert_standalone_crm_census_ready",
        _reject_prerequisite,
    )

    with pytest.raises(RuntimeError, match="#273 is incomplete"):
        lane_a_migration.ensure_standalone_crm_lane_a_ready(cast(Neo4jClient, client))
    with pytest.raises(RuntimeError, match="#273 is incomplete"):
        lane_a_migration.assert_standalone_crm_lane_a_ready(cast(Neo4jClient, client))

    assert not (_lane_a_schema_names(neo4j_driver) & set(_LANE_A_SCHEMA_NAMES))
    assert _marker_is_complete(neo4j_driver) is False


def test_readiness_installs_exact_schema_reruns_and_creates_no_domain_rows(
    neo4j_driver: Driver,
) -> None:
    client = _install_census_prerequisite(neo4j_driver)
    typed_client = cast(Neo4jClient, client)

    lane_a_migration.ensure_standalone_crm_lane_a_ready(typed_client)
    lane_a_migration.ensure_standalone_crm_lane_a_ready(typed_client)
    lane_a_migration.assert_standalone_crm_lane_a_ready(typed_client)

    expected = lane_a_migration._expected_schema()
    with client.session() as session:
        constraints = lane_a_migration._schema_rows(session, "SHOW CONSTRAINTS")
        indexes = lane_a_migration._schema_rows(session, "SHOW INDEXES")
        domain_rows = session.run(
            "MATCH (node) WHERE any(label IN labels(node) WHERE label IN $labels) "
            "RETURN count(node) AS count",
            labels=_LANE_A_DOMAIN_LABELS,
        ).single(strict=True)

    actual = {
        name: (constraints if definition[0] == "UNIQUENESS" else indexes)[name]
        for name, definition in expected.items()
    }
    assert actual == expected
    assert len(expected) == 29
    assert _marker_is_complete(neo4j_driver) is True
    assert domain_rows["count"] == 0


def test_marker_is_not_written_until_exact_schema_postvalidation_passes(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_census_prerequisite(neo4j_driver)

    def _reject_schema(_client: Neo4jClient) -> None:
        raise RuntimeError("postvalidation failed")

    monkeypatch.setattr(lane_a_migration, "assert_standalone_crm_lane_a_schema", _reject_schema)

    with pytest.raises(RuntimeError, match="postvalidation failed"):
        lane_a_migration.ensure_standalone_crm_lane_a_ready(cast(Neo4jClient, client))

    assert set(_LANE_A_SCHEMA_NAMES) <= _lane_a_schema_names(neo4j_driver)
    assert _marker_is_complete(neo4j_driver) is False


def test_readiness_removes_exact_legacy_membership_snapshot_constraint(
    neo4j_driver: Driver,
) -> None:
    client = _install_census_prerequisite(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE CONSTRAINT "
            + lane_a_migration.LEGACY_MEMBERSHIP_SNAPSHOT_SCOPE_DIGEST_CONSTRAINT
            + " IF NOT EXISTS FOR (n:CrmCompanyMembershipSnapshot) "
            + "REQUIRE (n.source_instance_id, n.subject_kind, n.subject_id, "
            + "n.snapshot_digest) IS UNIQUE"
        ).consume()

    lane_a_migration.ensure_standalone_crm_lane_a_ready(cast(Neo4jClient, client))

    assert (
        lane_a_migration.LEGACY_MEMBERSHIP_SNAPSHOT_SCOPE_DIGEST_CONSTRAINT
        not in _lane_a_schema_names(neo4j_driver)
    )
    lane_a_migration.assert_standalone_crm_lane_a_ready(cast(Neo4jClient, client))


def test_readiness_does_not_drop_malformed_legacy_constraint(
    neo4j_driver: Driver,
) -> None:
    client = _install_census_prerequisite(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE CONSTRAINT "
            + lane_a_migration.LEGACY_MEMBERSHIP_SNAPSHOT_SCOPE_DIGEST_CONSTRAINT
            + " IF NOT EXISTS FOR (n:MalformedMembershipSnapshot) "
            + "REQUIRE n.invalid_digest IS UNIQUE"
        ).consume()

    with pytest.raises(RuntimeError, match="legacy membership snapshot constraint is malformed"):
        lane_a_migration.ensure_standalone_crm_lane_a_ready(cast(Neo4jClient, client))

    assert (
        lane_a_migration.LEGACY_MEMBERSHIP_SNAPSHOT_SCOPE_DIGEST_CONSTRAINT
        in _lane_a_schema_names(neo4j_driver)
    )
    assert _marker_is_complete(neo4j_driver) is False


@pytest.mark.parametrize(
    "malformed_statement",
    (
        "CREATE CONSTRAINT crm_company_reference_scope_unique IF NOT EXISTS "
        "FOR (n:MalformedCompanyReference) REQUIRE n.invalid_id IS UNIQUE",
        "CREATE INDEX crm_company_description_observation_order IF NOT EXISTS "
        "FOR (n:MalformedCompanyDescriptionObservation) ON (n.invalid_order)",
    ),
)
def test_malformed_same_name_schema_fails_closed_without_marker(
    neo4j_driver: Driver,
    malformed_statement: str,
) -> None:
    client = _install_census_prerequisite(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(malformed_statement).consume()

    with pytest.raises(RuntimeError, match="standalone CRM Lane A schema is malformed"):
        lane_a_migration.ensure_standalone_crm_lane_a_ready(cast(Neo4jClient, client))

    assert _marker_is_complete(neo4j_driver) is False


def test_source_fact_page_cas_hook_executes_owned_cases(neo4j_driver: Driver) -> None:
    """Keep #302 executable when CI invokes the established Lane A suite only."""
    run_source_fact_neo4j_cases(neo4j_driver)
