"""
Neo4j 5.26 integration coverage for CRM identifier-scope relationship migration.

The fixture only permits an explicitly configured disposable database because it
requires an empty graph before it assumes cleanup ownership.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction
from src.graph.client import Neo4jClient
from src.graph.migrations import migrate_identifier_scopes

T = TypeVar("T")


class _Client:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        with self._driver.session() as session:
            return session.execute_write(work)


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    uri = os.getenv("HYPERP_NEO4J_CONTROL_MIGRATION_TEST_URI")
    password = os.getenv("HYPERP_NEO4J_CONTROL_MIGRATION_TEST_PASSWORD")
    if uri is None or password is None:
        pytest.skip("disposable Neo4j control migration database is not configured")

    host = urlparse(uri).hostname
    service_host = os.getenv("HYPERP_NEO4J_CONTROL_MIGRATION_TEST_SERVICE_HOST")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if service_host:
        allowed_hosts.add(service_host)
    if host not in allowed_hosts:
        pytest.fail(
            "identifier-scope migration test requires an explicitly configured disposable "
            "Neo4j host"
        )

    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_CONTROL_MIGRATION_TEST_USER", "neo4j"), password),
    )
    prepared = False
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            count = session.run("MATCH (node) RETURN count(node) AS count").single(strict=True)[
                "count"
            ]
            if count != 0:
                pytest.fail("identifier-scope migration test database must be empty")
        prepared = True
        yield driver
    finally:
        try:
            if prepared:
                with driver.session() as session:
                    session.run("MATCH (node) DETACH DELETE node").consume()
        finally:
            driver.close()


def _migrate(driver: Driver) -> int:
    return migrate_identifier_scopes(cast(Neo4jClient, _Client(driver)))


def _scoped_relationship_count(
    driver: Driver,
    person_id: str,
    identifier_type: str,
    identifier_scope: str,
    normalized_value: str,
) -> int:
    with driver.session() as session:
        result = session.run(
            (
                "MATCH (:Person {person_id: $person_id})-[:IDENTIFIED_BY]->"
                "(:Identifier {identifier_type: $identifier_type, "
                "identifier_scope: $identifier_scope, normalized_value: $normalized_value}) "
                "RETURN count(*) AS count"
            ),
            person_id=person_id,
            identifier_type=identifier_type,
            identifier_scope=identifier_scope,
            normalized_value=normalized_value,
        ).single(strict=True)
    return int(result["count"])


def test_complete_provenance_merges_relationship_deletes_legacy_and_is_idempotent(
    neo4j_driver: Driver,
) -> None:
    legacy_properties = {
        "source_system_key": "crm",
        "source_record_pk": "record-complete",
        "is_active": True,
        "is_verified": True,
        "verification_method": "legacy",
        "quality_flag": "valid",
        "first_seen_at": "2024-01-01T00:00:00Z",
        "last_seen_at": "2024-03-01T00:00:00Z",
        "last_confirmed_at": "2024-03-01T00:00:00Z",
    }
    scoped_properties = {
        "source_system_key": "crm",
        "source_record_pk": "record-complete",
        "is_active": False,
        "is_verified": False,
        "verification_method": "scoped",
        "quality_flag": "partial_parse",
        "first_seen_at": "2024-02-01T00:00:00Z",
        "last_seen_at": "2024-02-15T00:00:00Z",
        "last_confirmed_at": "2024-02-15T00:00:00Z",
    }
    with neo4j_driver.session() as session:
        session.run(
            (
                "CREATE (person:Person {person_id: $person_id}), "
                "(legacy:Identifier {identifier_type: $identifier_type, "
                "normalized_value: $normalized_value}), "
                "(scoped:Identifier {identifier_type: $identifier_type, "
                "identifier_scope: $identifier_scope, normalized_value: $normalized_value}), "
                "(source:SourceRecord {source_record_pk: $source_record_pk, "
                "source_instance_id: $identifier_scope}), "
                "(source_system:SourceSystem {source_key: $source_system_key}) "
                "CREATE (source)-[:FROM_SOURCE]->(source_system), "
                "(person)-[:IDENTIFIED_BY $legacy_properties]->(legacy), "
                "(person)-[:IDENTIFIED_BY $scoped_properties]->(scoped)"
            ),
            person_id="person-complete",
            identifier_type="crm_contact_id",
            identifier_scope="portal-a",
            normalized_value="42",
            source_record_pk="record-complete",
            source_system_key="crm",
            legacy_properties=legacy_properties,
            scoped_properties=scoped_properties,
        ).consume()

    assert _migrate(neo4j_driver) == 2

    with neo4j_driver.session() as session:
        result = session.run(
            (
                "MATCH (person:Person {person_id: $person_id}) "
                "OPTIONAL MATCH (person)-[legacy_rel:IDENTIFIED_BY]->(legacy:Identifier) "
                "WHERE legacy.identifier_type = $identifier_type "
                "AND legacy.normalized_value = $normalized_value "
                "AND legacy.identifier_scope IS NULL "
                "WITH person, count(legacy_rel) AS legacy_relationships "
                "MATCH (person)-[scoped_rel:IDENTIFIED_BY]->(scoped:Identifier { "
                "identifier_type: $identifier_type, identifier_scope: $identifier_scope, "
                "normalized_value: $normalized_value }) "
                "RETURN legacy_relationships, count(scoped_rel) AS scoped_relationships, "
                "properties(scoped_rel) AS properties"
            ),
            person_id="person-complete",
            identifier_type="crm_contact_id",
            identifier_scope="portal-a",
            normalized_value="42",
        ).single(strict=True)

    assert result["legacy_relationships"] == 0
    assert result["scoped_relationships"] == 1
    assert result["properties"] == {
        **legacy_properties,
        "verification_method": "scoped",
    }
    assert _migrate(neo4j_driver) == 0

    assert (
        _scoped_relationship_count(
            neo4j_driver,
            "person-complete",
            "crm_contact_id",
            "portal-a",
            "42",
        )
        == 1
    )


def test_incomplete_provenance_copies_properties_then_deletes_legacy_relationship(
    neo4j_driver: Driver,
) -> None:
    legacy_properties = {
        "is_active": True,
        "is_verified": False,
        "quality_flag": "partial_parse",
        "first_seen_at": "2024-04-01T00:00:00Z",
        "last_seen_at": "2024-04-02T00:00:00Z",
        "legacy_note": "missing provenance",
    }
    with neo4j_driver.session() as session:
        session.run(
            (
                "CREATE (person:Person {person_id: $person_id}), "
                "(legacy:Identifier {identifier_type: $identifier_type, "
                "normalized_value: $normalized_value}) "
                "CREATE (person)-[:IDENTIFIED_BY $legacy_properties]->(legacy)"
            ),
            person_id="person-incomplete",
            identifier_type="crm_lead_id",
            normalized_value="99",
            legacy_properties=legacy_properties,
        ).consume()

    assert _migrate(neo4j_driver) == 2

    with neo4j_driver.session() as session:
        result = session.run(
            (
                "MATCH (person:Person {person_id: $person_id}) "
                "OPTIONAL MATCH (person)-[legacy_rel:IDENTIFIED_BY]->(legacy:Identifier) "
                "WHERE legacy.identifier_type = $identifier_type "
                "AND legacy.normalized_value = $normalized_value "
                "AND legacy.identifier_scope IS NULL "
                "WITH person, count(legacy_rel) AS legacy_relationships "
                "MATCH (person)-[scoped_rel:IDENTIFIED_BY]->(scoped:Identifier { "
                "identifier_type: $identifier_type, identifier_scope: $identifier_scope, "
                "normalized_value: $normalized_value }) "
                "RETURN legacy_relationships, count(scoped_rel) AS scoped_relationships, "
                "properties(scoped_rel) AS properties"
            ),
            person_id="person-incomplete",
            identifier_type="crm_lead_id",
            identifier_scope="legacy-default",
            normalized_value="99",
        ).single(strict=True)

    assert result["legacy_relationships"] == 0
    assert result["scoped_relationships"] == 1
    assert result["properties"] == legacy_properties
    assert _migrate(neo4j_driver) == 0
