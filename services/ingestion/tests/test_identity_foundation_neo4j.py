"""Neo4j 5.26 integration coverage for the additive identity foundation schema."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase
from neo4j.exceptions import ConstraintError
from src.graph.client import Neo4jClient
from src.graph.queries.identity_foundation_schema import CREATE_IDENTITY_FOUNDATION_SCHEMA
from src.graph.schema_init import (
    BASE_LIFECYCLE_CONSTRAINTS,
    _find_init_cypher,
    _split_statements,
    apply_deferred_identifier_scope_constraints,
    apply_schema,
)
from src.identity_assertions import audit_assertions, backfill_assertions
from src.ingest_skips import ConnectorSkip, SkipLedger
from src.source_instances import LEGACY_DEFAULT_CONTROL_INSTANCE_ID

_ENV_PREFIX = "HYPERP_NEO4J_CONTROL_MIGRATION_TEST"
_SOURCE_RECORD_PK = "identity-foundation-source-1"


def test_identity_foundation_schema_is_mirrored_in_both_init_paths() -> None:
    expected = {" ".join(statement.split()) for statement in CREATE_IDENTITY_FOUNDATION_SCHEMA}
    runtime = {" ".join(statement.split()) for statement in BASE_LIFECYCLE_CONSTRAINTS}
    canonical = {
        " ".join(statement.split())
        for statement in _split_statements(_find_init_cypher().read_text(encoding="utf-8"))
    }

    assert expected <= runtime
    assert expected <= canonical


def _configuration() -> tuple[str, str, str, str] | None:
    uri = os.getenv(f"{_ENV_PREFIX}_URI")
    user = os.getenv(f"{_ENV_PREFIX}_USER")
    password = os.getenv(f"{_ENV_PREFIX}_PASSWORD")
    service_host = os.getenv(f"{_ENV_PREFIX}_SERVICE_HOST")
    values = (uri, user, password, service_host)
    if all(value is None for value in values):
        return None
    if any(value is None or not value.strip() for value in values):
        pytest.fail("identity foundation test requires complete disposable Neo4j configuration")
    assert uri is not None
    assert user is not None
    assert password is not None
    assert service_host is not None
    return uri, user, password, service_host


def _validate_target(uri: str, service_host: str) -> None:
    parsed = urlparse(uri)
    allowed_hosts = {"localhost", "127.0.0.1", "::1", service_host}
    if parsed.scheme != "bolt" or parsed.hostname not in allowed_hosts:
        pytest.fail("identity foundation test requires an explicitly disposable Bolt target")
    if parsed.username is not None or parsed.password is not None:
        pytest.fail("identity foundation test URI must not embed credentials")


def _require_neo4j_526(driver: Driver) -> None:
    with driver.session() as session:
        row = session.run(
            "CALL dbms.components() YIELD versions RETURN versions[0] AS version"
        ).single(strict=True)
    version = row["version"]
    if not isinstance(version, str) or version.split(".")[:2] != ["5", "26"]:
        pytest.fail("identity foundation test requires Neo4j 5.26")


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    configuration = _configuration()
    if configuration is None:
        pytest.skip("disposable Neo4j control migration database is not configured")
    uri, user, password, service_host = configuration
    _validate_target(uri, service_host)
    driver = GraphDatabase.driver(uri, auth=(user, password))
    cleanup_owned = False
    try:
        driver.verify_connectivity()
        _require_neo4j_526(driver)
        with driver.session() as session:
            count = session.run("MATCH (node) RETURN count(node) AS count").single(strict=True)[
                "count"
            ]
        if count != 0:
            pytest.fail("identity foundation test database must be empty")
        cleanup_owned = True
        yield driver
    finally:
        try:
            if cleanup_owned:
                with driver.session() as session:
                    session.run("MATCH (node) DETACH DELETE node").consume()
        finally:
            driver.close()


class _Client(Neo4jClient):
    def __init__(self, driver: Driver) -> None:
        self._driver = driver
        self._bounded_timeout_seconds = None


def _seed_existing_identity(driver: Driver) -> None:
    normalized_payload = json.dumps(
        {
            "identifiers": [
                {
                    "identifier_type": "phone",
                    "normalized_value": "+6591111111",
                    "source_instance_id": None,
                    "quality_flag": "valid",
                }
            ],
            "addresses": [],
            "attributes": [],
        }
    )
    with driver.session() as session:
        session.run(
            """
            CREATE (source:SourceRecord {
                source_record_pk: $source_record_pk,
                source_record_id: 'source-1',
                source_instance_id: null,
                lifecycle_status: 'active',
                is_latest: true,
                record_hash: 'hash-1',
                normalized_payload: $normalized_payload
            })
            CREATE (person:Person {person_id: 'person-1'})
            CREATE (identifier:Identifier {
                identifier_id: 'identifier-1',
                identifier_type: 'phone',
                identifier_scope: 'global',
                normalized_value: '+6591111111',
                source_instance_id: null
            })
            CREATE (person)-[:IDENTIFIED_BY {
                source_record_pk: $source_record_pk,
                quality_flag: 'valid',
                is_active: true
            }]->(identifier)
            """,
            source_record_pk=_SOURCE_RECORD_PK,
            normalized_payload=normalized_payload,
        ).consume()


def _graph_counts(driver: Driver) -> tuple[int, int]:
    with driver.session() as session:
        row = session.run(
            "MATCH (node) WITH count(node) AS nodes "
            "MATCH ()-[relationship]->() RETURN nodes, count(relationship) AS relationships"
        ).single(strict=True)
    return int(row["nodes"]), int(row["relationships"])


def _assertion_ids(driver: Driver) -> list[str]:
    with driver.session() as session:
        return sorted(
            row["assertion_id"]
            for row in session.run(
                "MATCH (:SourceRecord {source_record_pk: $source_record_pk})"
                "-[assertion:ASSERTS]->() RETURN assertion.assertion_id AS assertion_id",
                source_record_pk=_SOURCE_RECORD_PK,
            )
        )


def _constraint_names(driver: Driver) -> set[str]:
    with driver.session() as session:
        return {row["name"] for row in session.run("SHOW CONSTRAINTS YIELD name RETURN name")}


def test_identity_foundation_schema_and_backfill_are_idempotent(neo4j_driver: Driver) -> None:
    _seed_existing_identity(neo4j_driver)
    original_counts = _graph_counts(neo4j_driver)
    client = _Client(neo4j_driver)

    apply_schema(client)
    apply_schema(client)
    apply_deferred_identifier_scope_constraints(client)
    assert _graph_counts(neo4j_driver) == original_counts
    assert {
        "source_record_assertion_id_unique",
        "ingest_skip_id_unique",
        "ingest_run_attempt_key_unique",
        "profile_fact_key_unique",
        "identifier_identity_scope_unique",
    } <= _constraint_names(neo4j_driver)

    assert backfill_assertions(client) == 1
    first_assertions = _assertion_ids(neo4j_driver)
    assert backfill_assertions(client) == 1
    assert _assertion_ids(neo4j_driver) == first_assertions
    assert len(first_assertions) == 1
    assert audit_assertions(client) == []

    with pytest.raises(ConstraintError):
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (sr:SourceRecord {source_record_pk: $source_record_pk}) "
                "MATCH (identifier:Identifier {identifier_id: 'identifier-1'}) "
                "CREATE (sr)-[:ASSERTS {assertion_id: $assertion_id}]->(identifier)",
                source_record_pk=_SOURCE_RECORD_PK,
                assertion_id=first_assertions[0],
            ).consume()


def _record_retry_attempt(
    client: _Client,
    ingest_run_id: str,
    attempt_key: str,
    *,
    persisted: int,
    duplicates: int,
) -> SkipLedger:
    ledger = SkipLedger(client, ingest_run_id, "fundbox", attempt_key=attempt_key)
    ledger.emitted = 3
    ledger.persisted = persisted
    ledger.duplicates = duplicates
    ledger.add_pipeline_skip("excluded_identity", source_record_id="rec-skip")
    ledger.add_connector_skips(
        [ConnectorSkip(reason="missing_id", source_record_id=None, locator="users#missing_id")]
    )
    ledger.flush()
    return ledger


def test_skip_ledger_reconciles_redelivered_attempts(neo4j_driver: Driver) -> None:
    ingest_run_id = "retry-safe-ingest-run"
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:IngestRun {ingest_run_id: $ingest_run_id, "
            "control_instance_id: $control_instance_id})",
            ingest_run_id=ingest_run_id,
            control_instance_id=LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
        ).consume()
    client = _Client(neo4j_driver)

    _record_retry_attempt(
        client,
        ingest_run_id,
        "attempt-one",
        persisted=1,
        duplicates=1,
    )
    attempt_two = _record_retry_attempt(
        client,
        ingest_run_id,
        "attempt-two",
        persisted=0,
        duplicates=2,
    )

    assert attempt_two.reconcile() is True
    with neo4j_driver.session() as session:
        run = session.run(
            "MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id}) "
            "RETURN ir.emitted_count AS emitted, ir.persisted_count AS persisted, "
            "ir.duplicate_count AS duplicates, ir.pipeline_skip_count AS pipeline_skips, "
            "ir.connector_skip_count AS connector_skips, "
            "ir.record_error_count AS record_errors, "
            "ir.skip_ledger_reconciled AS reconciled",
            ingest_run_id=ingest_run_id,
        ).single(strict=True)
    assert run["emitted"] == 6
    assert run["persisted"] == 1
    assert run["duplicates"] == 3
    assert run["pipeline_skips"] == 2
    assert run["connector_skips"] == 2
    assert run["record_errors"] == 0
    assert run["reconciled"] is True

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (skip:IngestSkip {ingest_run_id: $ingest_run_id}) "
            "WITH skip ORDER BY skip.skip_id LIMIT 1 DETACH DELETE skip",
            ingest_run_id=ingest_run_id,
        ).consume()
    assert attempt_two.reconcile() is False
