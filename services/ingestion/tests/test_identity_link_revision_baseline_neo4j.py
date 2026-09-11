"""Neo4j 5.26 integration coverage for identity-link baseline completion."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction

from src.graph.client import Neo4jClient
from src.graph.migrations import migrate_identity_link_revision_baseline
from src.graph.queries.identity_link_revision_migrations import (
    BASELINE_MIGRATION_KEY,
    COMPLETE_IDENTITY_LINK_BASELINE,
)
from src.identity_link_revisions import identity_link_key

T = TypeVar("T")
_ENV_PREFIX = "HYPERP_NEO4J_CONTROL_MIGRATION_TEST"
_STREAM_KEY = "identity_link_revision_stream_v1"
_OWNER_ID = "baseline-owner"


@dataclass(frozen=True)
class _ReadinessState:
    migration_completed_at: str | None
    provenance_completed_at: str | None
    lease_owner: str | None
    lease_until: str | None
    counter_completed_at: str | None
    current_revision: int
    revision_count: int
    head_count: int


class _Client:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        with self._driver.session() as session:
            return session.execute_write(work)


def _configuration() -> tuple[str, str, str, str] | None:
    uri = os.getenv(f"{_ENV_PREFIX}_URI")
    user = os.getenv(f"{_ENV_PREFIX}_USER")
    password = os.getenv(f"{_ENV_PREFIX}_PASSWORD")
    service_host = os.getenv(f"{_ENV_PREFIX}_SERVICE_HOST")
    values = (uri, user, password, service_host)
    if all(value is None for value in values):
        return None
    if any(value is None or not value.strip() for value in values):
        pytest.fail("identity-link baseline tests require complete disposable Neo4j configuration")
    assert uri is not None
    assert user is not None
    assert password is not None
    assert service_host is not None
    return uri, user, password, service_host


def _validate_target(uri: str, service_host: str) -> None:
    parsed = urlparse(uri)
    allowed_hosts = {"localhost", "127.0.0.1", "::1", service_host}
    if service_host != service_host.strip():
        pytest.fail("identity-link baseline tests require a valid disposable service host")
    if parsed.scheme != "bolt":
        pytest.fail("identity-link baseline tests require a direct Bolt URI")
    if parsed.hostname not in allowed_hosts:
        pytest.fail("identity-link baseline tests require an explicitly disposable Neo4j host")
    if parsed.username is not None or parsed.password is not None:
        pytest.fail("identity-link baseline tests must not embed credentials in the URI")


def _require_neo4j_526(driver: Driver) -> None:
    with driver.session() as session:
        result = session.run(
            "CALL dbms.components() YIELD versions RETURN versions[0] AS version"
        ).single(strict=True)
    version = result["version"]
    if not isinstance(version, str) or version.split(".")[:2] != ["5", "26"]:
        pytest.fail("identity-link baseline tests require a Neo4j 5.26 server")


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
            pytest.fail("identity-link baseline test database must be empty")
        cleanup_owned = True
        yield driver
    finally:
        try:
            if cleanup_owned:
                with driver.session() as session:
                    session.run("MATCH (node) DETACH DELETE node").consume()
        finally:
            driver.close()


def _migrate(driver: Driver) -> int:
    return migrate_identity_link_revision_baseline(cast(Neo4jClient, _Client(driver)))


def _optional_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise AssertionError(f"expected string or null, got {value!r}")


def _required_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise AssertionError(f"expected integer, got {value!r}")


def _readiness_state(driver: Driver) -> _ReadinessState:
    with driver.session() as session:
        result = session.run(
            "MATCH (migration:DataMigration {migration_key: $migration_key}) "
            "MATCH (counter:IdentityLinkRevisionCounter {stream_key: $stream_key}) "
            "OPTIONAL MATCH (revision:IdentityLinkRevision) "
            "WITH migration, counter, count(revision) AS revision_count "
            "OPTIONAL MATCH (head:IdentityLinkHead) "
            "RETURN toString(migration.completed_at) AS migration_completed_at, "
            "toString(migration.provenance_completed_at) AS provenance_completed_at, "
            "migration.lease_owner AS lease_owner, "
            "toString(migration.lease_until) AS lease_until, "
            "toString(counter.baseline_completed_at) AS counter_completed_at, "
            "counter.current_revision AS current_revision, revision_count, "
            "count(head) AS head_count",
            migration_key=BASELINE_MIGRATION_KEY,
            stream_key=_STREAM_KEY,
        ).single(strict=True)
    return _ReadinessState(
        migration_completed_at=_optional_string(result["migration_completed_at"]),
        provenance_completed_at=_optional_string(result["provenance_completed_at"]),
        lease_owner=_optional_string(result["lease_owner"]),
        lease_until=_optional_string(result["lease_until"]),
        counter_completed_at=_optional_string(result["counter_completed_at"]),
        current_revision=_required_int(result["current_revision"]),
        revision_count=_required_int(result["revision_count"]),
        head_count=_required_int(result["head_count"]),
    )


def _seed_baseline_record(driver: Driver) -> None:
    key = identity_link_key(
        "bitrix_chat",
        "legacy-default",
        "contact",
        "42",
        "crm_contact_identity_v1",
    )
    with driver.session() as session:
        session.run(
            "CREATE (source:SourceSystem {source_key: 'bitrix_chat'}), "
            "(record:SourceRecord {source_record_pk: 'baseline-record-1', "
            "source_record_id: 'bitrix-crm-contact-42', source_record_version: '1', "
            "source_instance_id: 'legacy-default', source_entity_type: 'contact', "
            "source_entity_id: '42', identity_policy_version: 'crm_contact_identity_v1', "
            "identity_link_key: $identity_link_key, record_type: 'identity', "
            "lifecycle_status: 'active', link_status: 'unresolved', ingested_at: datetime()}) "
            "CREATE (record)-[:FROM_SOURCE]->(source)",
            identity_link_key=key,
        ).consume()


def _seed_rejected_completion_state(driver: Driver, state: str) -> str:
    lease_owner = _OWNER_ID if state != "different-owner" else "other-owner"
    completed_clause = "" if state != "already-completed" else ", completed_at: datetime()"
    lease_until = (
        "datetime() - duration({seconds: 1})"
        if state == "expired-lease"
        else "datetime() + duration({seconds: 60})"
    )
    with driver.session() as session:
        session.run(
            "CREATE (migration:DataMigration {migration_key: $migration_key, "
            "lease_owner: $lease_owner, lease_until: "
            + lease_until
            + completed_clause
            + "}), (counter:IdentityLinkRevisionCounter {stream_key: $stream_key, "
            "current_revision: 17, baseline_completed_at: datetime() - duration({seconds: 30})})",
            migration_key=BASELINE_MIGRATION_KEY,
            lease_owner=lease_owner,
            stream_key=_STREAM_KEY,
        ).consume()
    return _OWNER_ID


def test_baseline_completion_is_ready_and_full_rerun_is_idempotent(neo4j_driver: Driver) -> None:
    _seed_baseline_record(neo4j_driver)

    assert _migrate(neo4j_driver) == 1
    completed = _readiness_state(neo4j_driver)
    assert completed.migration_completed_at is not None
    assert completed.provenance_completed_at is not None
    assert completed.lease_owner is None
    assert completed.lease_until is None
    assert completed.counter_completed_at is not None
    assert completed.current_revision == 1
    assert completed.revision_count == 1
    assert completed.head_count == 1

    assert _migrate(neo4j_driver) == 0
    assert _readiness_state(neo4j_driver) == completed


@pytest.mark.parametrize("state", ("expired-lease", "different-owner", "already-completed"))
def test_completion_query_rejects_lost_or_completed_lease_states(
    neo4j_driver: Driver,
    state: str,
) -> None:
    owner_id = _seed_rejected_completion_state(neo4j_driver, state)
    before = _readiness_state(neo4j_driver)

    with neo4j_driver.session() as session:
        completion = session.run(
            COMPLETE_IDENTITY_LINK_BASELINE,
            migration_key=BASELINE_MIGRATION_KEY,
            owner_id=owner_id,
        ).single()

    assert completion is None
    assert _readiness_state(neo4j_driver) == before
