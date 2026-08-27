"""Disposable Neo4j support for standalone CRM census integration coverage."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase
from src.config import Settings
from src.graph.bitrix_source_instances import BitrixSourceInstanceRepository
from src.graph.bootstrap import bootstrap_legacy_bitrix_source_instance
from src.graph.client import Neo4jClient
from src.graph.ingestion_control_instance_migration import migrate_ingestion_control_instances
from src.graph.queries.bitrix_source_instances import CREATE_BITRIX_SOURCE_INSTANCE_CONSTRAINTS
from src.graph.queries.standalone_crm_census import (
    CREATE_STANDALONE_CRM_CENSUS_CONSTRAINTS,
    CREATE_STANDALONE_CRM_CENSUS_INDEXES,
)
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.graph.standalone_crm_census_migration import migrate_standalone_crm_census_control
from src.graph.standalone_crm_census_schema import assert_standalone_census_schema

_CENSUS_CONSTRAINTS = (
    "standalone_crm_census_id_unique",
    "standalone_crm_census_occurrence_unique",
    "standalone_crm_census_scope_lock_unique",
    "standalone_crm_census_attempt_unique",
    "standalone_crm_census_unit_unique",
    "standalone_crm_census_checkpoint_unique",
    "standalone_crm_census_publication_unique",
    "standalone_crm_census_publication_id_unique",
    "standalone_crm_census_call_intent_unique",
    "standalone_crm_census_fence_unique",
)
_CENSUS_INDEXES = (
    "standalone_crm_census_recovery_scan",
    "standalone_crm_census_publication_scan",
    "standalone_crm_census_call_scan",
    "standalone_crm_census_fence_scan",
)

# The #272 migration normally follows ``apply_schema`` in production. Census
# tests deliberately install #273 DDL only when a test asks for it, so restore
# just the #272 migration's required base schema after the control-instance
# suite drops its test constraints.
CONTROL_MIGRATION_BASE_CONSTRAINTS: tuple[str, ...] = (
    """CREATE CONSTRAINT data_migration_key_unique IF NOT EXISTS
FOR (migration:DataMigration)
REQUIRE migration.migration_key IS UNIQUE""",
    *CREATE_BITRIX_SOURCE_INSTANCE_CONSTRAINTS,
)


@dataclass(frozen=True)
class CensusNeo4j:
    """A test-owned client, raw driver, and active #272 source/control pair."""

    driver: Driver
    client: Neo4jClient
    source_instance_id: str

    @property
    def repository(self) -> StandaloneCrmCensusRepository:
        return StandaloneCrmCensusRepository(self.client)


def disposable_census_neo4j() -> CensusNeo4j:
    """Open the explicitly configured CI-only Neo4j service or skip locally."""
    uri = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST_URI")
    password = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST_PASSWORD")
    if uri is None or password is None:
        pytest.skip("standalone CRM census Neo4j test environment is not configured")
    host = urlparse(uri).hostname
    service_host = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST_SERVICE_HOST")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if service_host == "neo4j":
        allowed_hosts.add("neo4j")
    if host not in allowed_hosts:
        pytest.fail("standalone CRM census tests require an explicitly disposable Neo4j host")
    user = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST_USER", "neo4j")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    for _ in range(15):
        try:
            driver.verify_connectivity()
            break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    else:
        driver.close()
        pytest.fail("standalone CRM census Neo4j service did not become ready")
    client = Neo4jClient(Settings(neo4j_uri=uri, neo4j_user=user, neo4j_password=password))
    return CensusNeo4j(driver, client, f"census-test-{uuid.uuid4().hex}")


def drop_census_schema(env: CensusNeo4j) -> None:
    """Remove only named #273 DDL so a test can assert partial readiness."""
    with env.driver.session() as session:
        for name in _CENSUS_INDEXES:
            session.run(f"DROP INDEX {name} IF EXISTS").consume()
        for name in _CENSUS_CONSTRAINTS:
            session.run(f"DROP CONSTRAINT {name} IF EXISTS").consume()


def install_census_schema(env: CensusNeo4j) -> None:
    with env.driver.session() as session:
        for statement in CREATE_STANDALONE_CRM_CENSUS_CONSTRAINTS:
            session.run(statement).consume()
        for statement in CREATE_STANDALONE_CRM_CENSUS_INDEXES:
            session.run(statement).consume()


def prepare_272(env: CensusNeo4j) -> None:
    """Use #272's real migration and registry operations, not surrogate topology."""
    with env.driver.session() as session:
        for statement in CONTROL_MIGRATION_BASE_CONSTRAINTS:
            session.run(statement).consume()
        session.run(
            "MERGE (source:SourceSystem {source_key: 'bitrix_chat'}) SET source.is_active = true"
        ).consume()
    migrate_ingestion_control_instances(
        env.client,
        ensure_legacy_registration=lambda: bootstrap_legacy_bitrix_source_instance(env.client),
    )


def prepare_ready(env: CensusNeo4j) -> StandaloneCrmCensusRepository:
    prepare_272(env)
    install_census_schema(env)
    migrate_standalone_crm_census_control(env.client)
    assert_standalone_census_schema(env.client)
    registry = BitrixSourceInstanceRepository(env.client)
    registry.register("bitrix_chat", env.source_instance_id)
    registry.admit(
        control_instance_id=env.source_instance_id,
        source_instance_id=env.source_instance_id,
    )
    return env.repository


def cleanup_census_env(env: CensusNeo4j) -> None:
    """Delete only labels/markers and source instances owned by this test fixture."""
    try:
        with env.driver.session() as session:
            session.run(
                "MATCH (census:StandaloneCrmCensus {source_instance_id: $source_instance_id}) "
                "WITH collect(census.census_id) AS census_ids "
                "MATCH (node) WHERE node.census_id IN census_ids DETACH DELETE node",
                source_instance_id=env.source_instance_id,
            ).consume()
            session.run(
                "MATCH (scope:StandaloneCrmCensusScopeLock "
                "{source_instance_id: $source_instance_id}) DELETE scope",
                source_instance_id=env.source_instance_id,
            ).consume()
            session.run(
                "MATCH (node:BitrixSourceInstance {source_instance_id: $source_instance_id}) "
                "DETACH DELETE node",
                source_instance_id=env.source_instance_id,
            ).consume()
            session.run(
                "MATCH (node:DataMigration {migration_key: 'standalone_crm_census_control_v1'}) "
                "DETACH DELETE node"
            ).consume()
    finally:
        drop_census_schema(env)
        env.client.close()
        env.driver.close()
