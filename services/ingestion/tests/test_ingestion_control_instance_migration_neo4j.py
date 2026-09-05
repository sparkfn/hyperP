"""Neo4j integration coverage for #272 control migration.

The fixture only permits an explicitly configured disposable local database.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.graph.bitrix_source_instances import (
    BitrixControlAdmissionError,
    BitrixSourceInstanceConflictError,
    BitrixSourceInstanceRepository,
)
from src.graph.bootstrap import bootstrap_legacy_bitrix_source_instance
from src.graph.client import Neo4jClient
from src.graph.ingestion_control_instance_migration import (
    assert_ingestion_control_ready,
    migrate_ingestion_control_instances,
)
from src.graph.queries.bitrix_backfill import ATTACH_BACKFILL_LOGICAL_RUN
from src.graph.queries.ingestion_control_instance_migration import (
    LEGACY_CONSTRAINT_SPECS,
    NEW_CONSTRAINT_SPECS,
)
from src.graph.queries.stage_history_ingestion import (
    CLAIM_STAGE_HISTORY_RETRY,
    RESOLVE_STAGE_HISTORY_RETRY,
    UPSERT_STAGE_HISTORY_RETRY,
)
from src.models import (
    MatchDecision,
    MatchResult,
    NormalizedIdentifier,
    RecordType,
    SourceRecordEnvelope,
    SourceRecordLifecycleStatus,
)
from src.pipeline_writes import (
    create_person,
    link_record_to_graph,
    persist_source_record,
    upsert_nodes,
)

T = TypeVar("T")

_MIGRATION_CONSTRAINT = (
    "CREATE CONSTRAINT data_migration_key_unique IF NOT EXISTS "
    "FOR (n:DataMigration) REQUIRE n.migration_key IS UNIQUE"
)
_REGISTRY_CONSTRAINT = (
    "CREATE CONSTRAINT bitrix_source_instance_identity_unique IF NOT EXISTS "
    "FOR (instance:BitrixSourceInstance) "
    "REQUIRE (instance.source_key, instance.source_instance_id) IS UNIQUE"
)
_SEED = "\n".join(
    (
        "MATCH (source:SourceSystem {source_key: 'bitrix_chat', is_active: true})",
        "CREATE (logical:IngestionLogicalRun {logical_run_id: 'logical',",
        "  source_key: 'bitrix_chat', idempotency_key: 'key'})-[:FOR_SOURCE]->(source)",
        "CREATE (run:IngestRun {ingest_run_id: 'run', worker_task_id: 'worker',",
        "  source_key: 'bitrix_chat', idempotency_key: 'key'})-[:FROM_SOURCE]->(source)",
        "CREATE (logical)-[:HAS_ATTEMPT]->(run)",
        "CREATE (logical)-[:ACTIVE_ATTEMPT]->(run)",
        "CREATE (checkpoint:IngestionCheckpoint {logical_run_id: 'logical', phase: 'phase'})",
        "CREATE (checkpoint)-[:CHECKPOINT_FOR]->(logical)",
        "CREATE (checkpoint)-[:PRODUCED_BY]->(run)",
        "CREATE (stream:BitrixIngestionStream {source_key: 'bitrix_chat',",
        "  stream_key: 'crm_deals'})",
        "CREATE (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat'})",
        "CREATE (generation:BitrixBackfillGeneration "
        "{generation_id: 'generation', status: 'accepted'})",
        "CREATE (successor:BitrixBackfillGeneration "
        "{generation_id: 'successor', status: 'allocated'})",
        "CREATE (generation)-[:HAS_SUCCESSOR]->(successor)",
        "CREATE (outbox:BitrixBackfillDispatchOutbox {successor_generation_id: 'successor'})",
    )
)
_SCOPED_COUNT = "\n".join(
    (
        "MATCH (n)",
        "WHERE n:IngestRun OR n:IngestionLogicalRun OR n:IngestionCheckpoint",
        "   OR n:BitrixIngestionStream OR n:BitrixDispatchControl",
        "   OR n:BitrixBackfillGeneration OR n:BitrixBackfillDispatchOutbox",
        "RETURN count(n) AS total,",
        "count(CASE WHEN n.control_instance_id = 'legacy-default' THEN n END) AS scoped",
    )
)
_COMPLETE_MARKER = (
    "MATCH (m:DataMigration {migration_key: 'bitrix_control_instance_v1'}) "
    "RETURN m.completed_at IS NOT NULL AS complete"
)
_AMBIGUOUS_SOURCE = "\n".join(
    (
        "CREATE (a:SourceSystem {source_key: 'a'}), (b:SourceSystem {source_key: 'b'})",
        "CREATE (r:IngestRun {ingest_run_id: 'bad'})-[:FROM_SOURCE]->(a)",
        "CREATE (r)-[:FROM_SOURCE]->(b)",
    )
)
_BLOCKED_MARKER = (
    "MATCH (m:DataMigration {migration_key: 'bitrix_control_instance_v1'}) "
    "RETURN m.completed_at IS NULL AS blocked"
)

_SUITE_CONSTRAINT_NAMES = (
    "data_migration_key_unique",
    "bitrix_source_instance_identity_unique",
    *(spec[0] for spec in LEGACY_CONSTRAINT_SPECS),
    *(spec[0] for spec in NEW_CONSTRAINT_SPECS),
    "unexpected_control_identity",
    "unexpected_legacy_ingest_run_identity",
    "identifier_identity_scope_unique",
)


def _drop_suite_constraints(driver: Driver) -> None:
    with driver.session() as session:
        for name in _SUITE_CONSTRAINT_NAMES:
            session.run(f"DROP CONSTRAINT {name} IF EXISTS").consume()


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


def _migrate(driver: Driver) -> _Client:
    client = _Client(driver)
    migrate_ingestion_control_instances(
        cast(Neo4jClient, client),
        ensure_legacy_registration=lambda: bootstrap_legacy_bitrix_source_instance(
            cast(Neo4jClient, client)
        ),
    )
    return client


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
            "control migration test requires an explicitly configured disposable Neo4j host"
        )
    driver = GraphDatabase.driver(
        uri, auth=(os.getenv("HYPERP_NEO4J_CONTROL_MIGRATION_TEST_USER", "neo4j"), password)
    )
    connected = False
    prepared = False
    try:
        for _ in range(15):
            try:
                driver.verify_connectivity()
                connected = True
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        else:
            pytest.fail("disposable control migration Neo4j database did not become ready")
        _drop_suite_constraints(driver)
        with driver.session() as session:
            if session.run("MATCH (n) RETURN count(n) AS count").single(strict=True)["count"] != 0:
                pytest.fail("control migration test database must be empty")
            session.run(
                "CREATE (:SourceSystem {source_key: 'bitrix_chat', is_active: true})"
            ).consume()
        prepared = True
        yield driver
    finally:
        try:
            if prepared:
                with driver.session() as session:
                    session.run("MATCH (n) DETACH DELETE n").consume()
            if connected:
                _drop_suite_constraints(driver)
        finally:
            driver.close()


def _seed(driver: Driver) -> None:
    with driver.session() as session:
        session.run(_MIGRATION_CONSTRAINT).consume()
        session.run(_REGISTRY_CONSTRAINT).consume()
        session.run(_SEED).consume()


def test_fresh_migration_without_reserved_registration_callback_remains_incomplete(
    neo4j_driver: Driver,
) -> None:
    with neo4j_driver.session() as session:
        session.run(_MIGRATION_CONSTRAINT).consume()
        session.run(_REGISTRY_CONSTRAINT).consume()
    with pytest.raises(RuntimeError, match="reserved legacy Bitrix source registration"):
        migrate_ingestion_control_instances(cast(Neo4jClient, _Client(neo4j_driver)))
    with neo4j_driver.session() as session:
        marker = session.run(_BLOCKED_MARKER).single(strict=True)
    assert marker["blocked"] is True


def test_fixture_starts_without_any_migration_owned_constraint(neo4j_driver: Driver) -> None:
    with neo4j_driver.session() as session:
        names = {row["name"] for row in session.run("SHOW CONSTRAINTS YIELD name RETURN name")}
    assert not names.intersection(_SUITE_CONSTRAINT_NAMES)


def test_fresh_install_creates_only_replacement_constraints_and_completion_marker(
    neo4j_driver: Driver,
) -> None:
    with neo4j_driver.session() as session:
        session.run(_MIGRATION_CONSTRAINT).consume()
        session.run(_REGISTRY_CONSTRAINT).consume()
    client = _Client(neo4j_driver)
    _migrate(neo4j_driver)
    assert_ingestion_control_ready(cast(Neo4jClient, client))
    with neo4j_driver.session() as session:
        marker = session.run(_COMPLETE_MARKER).single(strict=True)
        dispatch_count = session.run(
            "MATCH (:BitrixDispatchControl) RETURN count(*) AS count"
        ).single(strict=True)
    assert marker["complete"] is True
    assert dispatch_count["count"] == 0


def test_fresh_upgrade_is_idempotent_and_preserves_selective_blocking(neo4j_driver: Driver) -> None:
    _seed(neo4j_driver)
    client = _Client(neo4j_driver)
    _migrate(neo4j_driver)
    _migrate(neo4j_driver)
    assert_ingestion_control_ready(cast(Neo4jClient, client))
    with neo4j_driver.session() as session:
        result = session.run(_SCOPED_COUNT).single(strict=True)
        marker = session.run(_COMPLETE_MARKER).single(strict=True)
    assert result["total"] == result["scoped"]
    assert marker["complete"] is True


def test_upgrade_blocks_an_existing_source_only_dispatch_under_legacy_constraint(
    neo4j_driver: Driver,
) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE CONSTRAINT bitrix_dispatch_control_source_unique IF NOT EXISTS "
            "FOR (control:BitrixDispatchControl) REQUIRE control.source_key IS UNIQUE"
        ).consume()
    _migrate(neo4j_driver)
    with neo4j_driver.session() as session:
        control = session.run(
            "MATCH (control:BitrixDispatchControl {source_key: 'bitrix_chat'}) "
            "RETURN count(control) AS count, "
            "control.control_instance_id AS control_instance_id, "
            "control.blocked AS blocked"
        ).single(strict=True)
        legacy = session.run(
            "SHOW CONSTRAINTS YIELD name "
            "WHERE name = 'bitrix_dispatch_control_source_unique' RETURN count(*) AS count"
        ).single(strict=True)
    assert control["count"] == 1
    assert control["control_instance_id"] == "legacy-default"
    assert control["blocked"] is False
    assert legacy["count"] == 0


def test_ambiguous_source_ownership_remains_blocked(neo4j_driver: Driver) -> None:
    with neo4j_driver.session() as session:
        session.run(_MIGRATION_CONSTRAINT).consume()
        session.run(_REGISTRY_CONSTRAINT).consume()
        session.run(_AMBIGUOUS_SOURCE).consume()
    with pytest.raises(RuntimeError, match="ambiguities"):
        _migrate(neo4j_driver)
    with neo4j_driver.session() as session:
        row = session.run(_BLOCKED_MARKER).single(strict=True)
    assert row["blocked"] is True


def test_scoped_backfill_topology_allows_overlapping_ids_and_rejects_cross_instance_attachment(
    neo4j_driver: Driver,
) -> None:
    _seed(neo4j_driver)
    _migrate(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE "
            "(first:BitrixBackfillGeneration {control_instance_id: 'portal-one', "
            " generation_id: 'same', status: 'allocated', boundary_digest: 'b', "
            " configuration_digest: 'c'}), "
            "(second:BitrixBackfillGeneration {control_instance_id: 'portal-two', "
            " generation_id: 'same', status: 'allocated', boundary_digest: 'b', "
            " configuration_digest: 'c'}), "
            "(first_set:BitrixKnownOwnerRefreshSet {control_instance_id: 'portal-one', "
            " generation_id: 'same', membership_set_id: 'owners'}), "
            "(second_set:BitrixKnownOwnerRefreshSet {control_instance_id: 'portal-two', "
            " generation_id: 'same', membership_set_id: 'owners'}), "
            "(first_member:BitrixKnownOwnerRefreshMember {control_instance_id: 'portal-one', "
            " generation_id: 'same', membership_set_id: 'owners', deal_id: '42'}), "
            "(second_member:BitrixKnownOwnerRefreshMember {control_instance_id: 'portal-two', "
            " generation_id: 'same', membership_set_id: 'owners', deal_id: '42'}), "
            "(first_coverage:BitrixBackfillCoverage {control_instance_id: 'portal-one', "
            " generation_id: 'same', stream_key: 'crm_deals', source_identity: 'crm-42', "
            " source_boundary: 'window'}), "
            "(second_coverage:BitrixBackfillCoverage {control_instance_id: 'portal-two', "
            " generation_id: 'same', stream_key: 'crm_deals', source_identity: 'crm-42', "
            " source_boundary: 'window'}), "
            "(first)-[:HAS_KNOWN_OWNER_SET]->(first_set), "
            "(second)-[:HAS_KNOWN_OWNER_SET]->(second_set), "
            "(first_set)-[:HAS_MEMBER]->(first_member), "
            "(second_set)-[:HAS_MEMBER]->(second_member), "
            "(first)-[:HAS_COVERAGE]->(first_coverage), "
            "(second)-[:HAS_COVERAGE]->(second_coverage)"
        ).consume()
        result = session.run(
            "MATCH (generation:BitrixBackfillGeneration {generation_id: 'same'}) "
            "MATCH (generation)-[:HAS_KNOWN_OWNER_SET]->(:BitrixKnownOwnerRefreshSet) "
            "MATCH (generation)-[:HAS_COVERAGE]->(:BitrixBackfillCoverage) "
            "RETURN count(generation) AS count"
        ).single(strict=True)
        session.run(
            "CREATE (logical:IngestionLogicalRun {control_instance_id: 'portal-two', "
            " logical_run_id: 'portal-two-logical'}), "
            "(stream:BitrixIngestionStream {source_key: 'bitrix_chat', "
            " control_instance_id: 'portal-two', stream_key: 'crm_deals', "
            " logical_run_id: 'portal-two-logical'})"
        ).consume()
        cross = session.run(
            ATTACH_BACKFILL_LOGICAL_RUN,
            control_instance_id="portal-one",
            generation_id="same",
            stream_key="crm_deals",
            logical_run_id="portal-two-logical",
            boundary_digest="b",
            configuration_digest="c",
        ).single()
    assert result["count"] == 2
    assert cross is None


_LEGACY_DDL = (
    "CREATE CONSTRAINT ingest_run_worker_task_id_unique IF NOT EXISTS "
    "FOR (run:IngestRun) REQUIRE run.worker_task_id IS UNIQUE",
    "CREATE CONSTRAINT ingest_run_source_idempotency_unique IF NOT EXISTS "
    "FOR (run:IngestRun) REQUIRE (run.source_key, run.idempotency_key) IS UNIQUE",
    "CREATE CONSTRAINT ingestion_checkpoint_identity_unique IF NOT EXISTS "
    "FOR (checkpoint:IngestionCheckpoint) "
    "REQUIRE (checkpoint.logical_run_id, checkpoint.phase) IS UNIQUE",
)


def test_upgrade_replaces_verified_pair_and_global_constraints(neo4j_driver: Driver) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        for statement in _LEGACY_DDL:
            session.run(statement).consume()
    _migrate(neo4j_driver)
    with neo4j_driver.session() as session:
        names = {row["name"] for row in session.run("SHOW CONSTRAINTS YIELD name RETURN name")}
    assert (
        not {
            "ingest_run_worker_task_id_unique",
            "ingest_run_source_idempotency_unique",
            "ingestion_checkpoint_identity_unique",
        }
        & names
    )
    assert {
        "ingest_run_worker_task_control_unique",
        "ingest_run_source_control_idempotency_unique",
        "ingestion_checkpoint_control_logical_phase_unique",
    } <= names


def test_partial_ddl_phase_resumes_and_completes(neo4j_driver: Driver) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (node) WHERE node:IngestRun OR node:IngestionLogicalRun "
            "OR node:IngestionCheckpoint OR node:BitrixIngestionStream "
            "OR node:BitrixDispatchControl OR node:BitrixBackfillGeneration "
            "OR node:BitrixBackfillDispatchOutbox "
            "SET node.control_instance_id = 'legacy-default'"
        ).consume()
        session.run(
            "CREATE (migration:DataMigration {migration_key: 'bitrix_control_instance_v1', "
            "phase: 'create_instance_constraints', cursor: '', progress_count: 0})"
        ).consume()
        session.run(
            "CREATE CONSTRAINT ingest_run_worker_task_control_unique IF NOT EXISTS "
            "FOR (run:IngestRun) REQUIRE (run.control_instance_id, run.worker_task_id) IS UNIQUE"
        ).consume()
    _migrate(neo4j_driver)
    assert_ingestion_control_ready(cast(Neo4jClient, _Client(neo4j_driver)))


@pytest.mark.parametrize(
    "seed_statement",
    (
        "CREATE (bad:IngestionLogicalRun {source_key: 'bitrix_chat', "
        "idempotency_key: 'bad', control_instance_id: 'bad id'})",
        "CREATE (first:IngestRun {source_key: 'bitrix_chat', idempotency_key: 'collision'}), "
        "(second:IngestRun {source_key: 'bitrix_chat', idempotency_key: 'collision'})",
        "CREATE CONSTRAINT unexpected_control_identity IF NOT EXISTS "
        "FOR (run:IngestRun) REQUIRE (run.source_key, run.idempotency_key) IS UNIQUE",
    ),
)
def test_upgrade_validation_failure_stays_blocked(
    neo4j_driver: Driver, seed_statement: str
) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(seed_statement).consume()
    with pytest.raises(RuntimeError):
        _migrate(neo4j_driver)
    with neo4j_driver.session() as session:
        marker = session.run(_BLOCKED_MARKER).single(strict=True)
        dispatch = session.run(
            "MATCH (control:BitrixDispatchControl {source_key: 'bitrix_chat'}) "
            "RETURN control.blocked AS blocked"
        ).single(strict=True)
    assert marker["blocked"] is True
    assert dispatch["blocked"] is True


def _write_registered_crm_contact(
    client: _Client,
    source_instance_id: str,
) -> None:
    BitrixSourceInstanceRepository(cast(Neo4jClient, client)).register(
        "bitrix_chat", source_instance_id
    )
    identifier = NormalizedIdentifier(
        identifier_type="crm_contact_id",
        normalized_value="42",
        source_instance_id=source_instance_id,
        is_verified=True,
    )
    envelope = SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_instance_id=source_instance_id,
        source_record_id="bitrix-crm-contact-42",
        source_record_version="1",
        record_type=RecordType.IDENTITY,
        observed_at="2026-08-27T00:00:00+00:00",
        record_hash=f"crm-contact-42-{source_instance_id}",
        raw_payload={"crm_contact_id": "42"},
    )

    def _write(tx: ManagedTransaction) -> None:
        upsert_nodes(tx, [identifier], [])
        source_record_pk = persist_source_record(
            tx,
            envelope=envelope,
            identifiers=[identifier],
            addresses=[],
            attributes=[],
            match_result=MatchResult(decision=MatchDecision.NO_MATCH),
            is_new_person=True,
            ingest_run_id=None,
            lifecycle_status=SourceRecordLifecycleStatus.ACTIVE,
            expected_active_source_record_pk=None,
        )
        person_id = create_person(tx)
        link_record_to_graph(
            tx,
            envelope=envelope,
            identifiers=[identifier],
            addresses=[],
            attributes=[],
            person_id=person_id,
            source_record_pk=source_record_pk,
        )

    client.execute_write(_write)


def test_two_registered_portals_keep_same_crm_contact_id_isolated(neo4j_driver: Driver) -> None:
    with neo4j_driver.session() as session:
        session.run(_REGISTRY_CONSTRAINT).consume()
        session.run(
            "CREATE CONSTRAINT identifier_identity_scope_unique IF NOT EXISTS "
            "FOR (identifier:Identifier) "
            "REQUIRE (identifier.identifier_type, identifier.identifier_scope, "
            "identifier.normalized_value) IS UNIQUE"
        ).consume()
    client = _Client(neo4j_driver)
    _write_registered_crm_contact(client, "portal-one")
    _write_registered_crm_contact(client, "portal-two")
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (record:SourceRecord {source_record_id: 'bitrix-crm-contact-42'}) "
            "-[:LINKED_TO]->(:Person)-[link:IDENTIFIED_BY]->"
            "(identifier:Identifier {identifier_type: 'crm_contact_id', normalized_value: '42'}) "
            "WHERE link.source_record_pk = record.source_record_pk "
            "RETURN count(DISTINCT record) AS records, "
            "count(DISTINCT identifier) AS identifiers, "
            "count(DISTINCT identifier.identifier_scope) AS scopes, "
            "count(CASE WHEN record.source_instance_id <> identifier.identifier_scope "
            "THEN 1 END) AS cross_instance_links"
        ).single(strict=True)
    assert row["records"] == 2
    assert row["identifiers"] == 2
    assert row["scopes"] == 2
    assert row["cross_instance_links"] == 0


def test_stage_retry_creation_claim_resolution_and_cross_instance_reuse_are_scoped(
    neo4j_driver: Driver,
) -> None:
    with neo4j_driver.session() as session:
        session.run(
            "CREATE "
            "(logical:IngestionLogicalRun {control_instance_id: 'portal-one', "
            "logical_run_id: 'portal-one-logical', active_generation: 1, "
            "mode: 'parent_reconcile', status: 'running'}), "
            "(attempt:IngestRun {control_instance_id: 'portal-one', "
            "ingest_run_id: 'portal-one-run', generation: 1}), "
            "(logical)-[:ACTIVE_ATTEMPT]->(attempt), "
            "(stream:BitrixIngestionStream {source_key: 'bitrix_chat', "
            "control_instance_id: 'portal-one', stream_key: 'crm_stage_history', "
            "logical_run_id: 'portal-one-logical', ingest_run_id: 'portal-one-run', "
            "attempt_generation: 1, stream_generation: 1, fencing_token: 1, "
            "status: 'active'}), "
            "(occurrence:StageHistoryOccurrence {occurrence_id: 'stage-occurrence-one', "
            "control_instance_id: 'portal-one', logical_run_id: 'portal-one-logical'})"
        ).consume()
        created = session.run(
            UPSERT_STAGE_HISTORY_RETRY,
            source_key="bitrix_chat",
            control_instance_id="portal-one",
            logical_run_id="portal-one-logical",
            ingest_run_id="portal-one-run",
            attempt_generation=1,
            stream_generation=1,
            fencing_token=1,
            required_run_type="parent_reconcile",
            occurrence_id="stage-occurrence-one",
            retry_sequence=1,
            retry_id="stage-retry-one",
            reason_code="canonical_pending_parent",
            max_attempts=3,
            review_command_id=None,
            next_attempt_at="2020-01-01T00:00:00+00:00",
        ).single(strict=True)
        claimed = session.run(
            CLAIM_STAGE_HISTORY_RETRY,
            source_key="bitrix_chat",
            control_instance_id="portal-one",
            logical_run_id="portal-one-logical",
            ingest_run_id="portal-one-run",
            attempt_generation=1,
            stream_generation=1,
            fencing_token=1,
            required_run_type="parent_reconcile",
            occurrence_id="stage-occurrence-one",
            retry_sequence=1,
            lease_owner="worker-one",
            lease_expires_at="2030-01-01T00:00:00+00:00",
        ).single(strict=True)
        resolved = session.run(
            RESOLVE_STAGE_HISTORY_RETRY,
            source_key="bitrix_chat",
            control_instance_id="portal-one",
            logical_run_id="portal-one-logical",
            ingest_run_id="portal-one-run",
            attempt_generation=1,
            stream_generation=1,
            fencing_token=1,
            required_run_type="parent_reconcile",
            occurrence_id="stage-occurrence-one",
            retry_sequence=1,
            lease_owner="worker-one",
            lease_expires_at="2030-01-01T00:00:00+00:00",
            resolution="resolved",
            resolution_decision_id="decision-one",
        ).single(strict=True)
        retry = session.run(
            "MATCH (retry:StageHistoryRetry {retry_id: 'stage-retry-one'}) "
            "RETURN retry.control_instance_id AS control_instance_id, retry.status AS status"
        ).single(strict=True)
        session.run(
            "MATCH (retry:StageHistoryRetry {retry_id: 'stage-retry-one'}) "
            "SET retry.control_instance_id = 'portal-two'"
        ).consume()
        reused = session.run(
            UPSERT_STAGE_HISTORY_RETRY,
            source_key="bitrix_chat",
            control_instance_id="portal-one",
            logical_run_id="portal-one-logical",
            ingest_run_id="portal-one-run",
            attempt_generation=1,
            stream_generation=1,
            fencing_token=1,
            required_run_type="parent_reconcile",
            occurrence_id="stage-occurrence-one",
            retry_sequence=1,
            retry_id="stage-retry-one",
            reason_code="canonical_pending_parent",
            max_attempts=3,
            review_command_id=None,
            next_attempt_at="2020-01-01T00:00:00+00:00",
        ).single()
    assert created["status"] == "pending"
    assert claimed["attempt_count"] == 1
    assert resolved["status"] == "resolved"
    assert retry["control_instance_id"] == "portal-one"
    assert retry["status"] == "resolved"
    assert reused is None


@pytest.mark.parametrize(
    "registry_statement",
    (
        None,
        "CREATE CONSTRAINT bitrix_source_instance_identity_unique IF NOT EXISTS "
        "FOR (source:SourceSystem) REQUIRE source.source_key IS UNIQUE",
    ),
)
def test_readiness_fails_closed_without_exact_registry_constraint(
    neo4j_driver: Driver, registry_statement: str | None
) -> None:
    with neo4j_driver.session() as session:
        session.run(_MIGRATION_CONSTRAINT).consume()
        if registry_statement is not None:
            session.run(registry_statement).consume()
    with pytest.raises(RuntimeError):
        _migrate(neo4j_driver)


def test_upgrade_blocks_checkpoint_key_without_any_source_ownership(neo4j_driver: Driver) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run("CREATE (:IngestionCheckpoint {checkpoint_key: 'orphan-checkpoint'})").consume()
    with pytest.raises(RuntimeError, match="ambiguities"):
        _migrate(neo4j_driver)
    with neo4j_driver.session() as session:
        marker = session.run(_BLOCKED_MARKER).single(strict=True)
    assert marker["blocked"] is True


def test_upgrade_allows_multiple_historical_checkpoint_producers(neo4j_driver: Driver) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (logical:IngestionLogicalRun {logical_run_id: 'logical'}), "
            "(checkpoint:IngestionCheckpoint {logical_run_id: 'logical'}) "
            "MATCH (source:SourceSystem {source_key: 'bitrix_chat'}) "
            "CREATE (attempt:IngestRun {ingest_run_id: 'run-2', worker_task_id: 'worker-2', "
            "source_key: 'bitrix_chat', logical_run_id: 'logical'})-[:FROM_SOURCE]->(source), "
            "(logical)-[:HAS_ATTEMPT]->(attempt), (checkpoint)-[:PRODUCED_BY]->(attempt)"
        ).consume()
    _migrate(neo4j_driver)
    assert_ingestion_control_ready(cast(Neo4jClient, _Client(neo4j_driver)))


def test_upgrade_blocks_mismatched_historical_checkpoint_producer(neo4j_driver: Driver) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (checkpoint:IngestionCheckpoint {logical_run_id: 'logical'}), "
            "(source:SourceSystem {source_key: 'bitrix_chat'}) "
            "CREATE (attempt:IngestRun {ingest_run_id: 'bad-run', worker_task_id: 'bad-worker', "
            "source_key: 'bitrix_chat', logical_run_id: 'other'})-[:FROM_SOURCE]->(source), "
            "(checkpoint)-[:PRODUCED_BY]->(attempt)"
        ).consume()
    with pytest.raises(RuntimeError, match="ambiguities"):
        _migrate(neo4j_driver)
    with neo4j_driver.session() as session:
        marker = session.run(_BLOCKED_MARKER).single(strict=True)
    assert marker["blocked"] is True


def test_disable_portal_registration_rejects_legacy_control_execution(neo4j_driver: Driver) -> None:
    _seed(neo4j_driver)
    client = _migrate(neo4j_driver)
    repository = BitrixSourceInstanceRepository(cast(Neo4jClient, client))
    repository.register("bitrix_chat", "portal-a")
    repository.admit(control_instance_id="legacy-default", source_instance_id="portal-a")
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:IngestRun {ingest_run_id: 'portal-a-running', "
            "source_key: 'bitrix_chat', control_instance_id: 'legacy-default', "
            "status: 'running'})"
        ).consume()
    with pytest.raises(BitrixSourceInstanceConflictError):
        repository.disable("bitrix_chat", "portal-a", "operator", "retire")
    with neo4j_driver.session() as session:
        status = session.run(
            "MATCH (instance:BitrixSourceInstance {source_key: 'bitrix_chat', "
            "source_instance_id: 'portal-a'}) RETURN instance.status AS status"
        ).single(strict=True)
    assert status["status"] == "active"


def test_aliased_retired_constraint_blocks_dispatch_before_inventory_failure(
    neo4j_driver: Driver,
) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE CONSTRAINT unexpected_legacy_ingest_run_identity IF NOT EXISTS "
            "FOR (run:IngestRun) REQUIRE (run.source_key, run.idempotency_key) IS UNIQUE"
        ).consume()
    with pytest.raises(RuntimeError, match="unrecognized constraint"):
        _migrate(neo4j_driver)
    with neo4j_driver.session() as session:
        marker = session.run(_BLOCKED_MARKER).single(strict=True)
        dispatch = session.run(
            "MATCH (control:BitrixDispatchControl {source_key: 'bitrix_chat'}) "
            "RETURN control.blocked AS blocked"
        ).single(strict=True)
    assert marker["blocked"] is True
    assert dispatch["blocked"] is True


@pytest.mark.parametrize(
    "seed_statement",
    (
        "MATCH (source:SourceSystem {source_key: 'bitrix_chat'}) "
        "CREATE (instance:BitrixSourceInstance {source_key: 'bitrix_chat', "
        "source_instance_id: 'portal-disabled', status: 'disabled'})"
        "-[:INSTANCE_OF]->(source)",
        "CREATE (:BitrixSourceInstance {source_key: 'bitrix_chat', "
        "source_instance_id: 'portal-unlinked', status: 'active'})",
        "MATCH (source:SourceSystem {source_key: 'bitrix_chat'}) "
        "CREATE (other:SourceSystem {source_key: 'other', is_active: true}), "
        "(instance:BitrixSourceInstance {source_key: 'bitrix_chat', "
        "source_instance_id: 'portal-multi', status: 'active'}), "
        "(instance)-[:INSTANCE_OF]->(source), "
        "(instance)-[:INSTANCE_OF]->(other)",
    ),
)
def test_invalid_existing_registration_is_not_mutated_before_rejection(
    neo4j_driver: Driver, seed_statement: str
) -> None:
    with neo4j_driver.session() as session:
        session.run(_REGISTRY_CONSTRAINT).consume()
        session.run(seed_statement).consume()
        before = session.run(
            "MATCH (instance:BitrixSourceInstance) "
            "OPTIONAL MATCH (instance)-[relationship:INSTANCE_OF]->() "
            "RETURN count(instance) AS instances, count(relationship) AS relationships"
        ).single(strict=True)
    repository = BitrixSourceInstanceRepository(cast(Neo4jClient, _Client(neo4j_driver)))
    suffix = (
        "portal-disabled"
        if "portal-disabled" in seed_statement
        else "portal-unlinked"
        if "portal-unlinked" in seed_statement
        else "portal-multi"
    )
    with pytest.raises(BitrixSourceInstanceConflictError):
        repository.register("bitrix_chat", suffix)
    with neo4j_driver.session() as session:
        after = session.run(
            "MATCH (instance:BitrixSourceInstance) "
            "OPTIONAL MATCH (instance)-[relationship:INSTANCE_OF]->() "
            "RETURN count(instance) AS instances, count(relationship) AS relationships"
        ).single(strict=True)
    assert dict(after) == dict(before)


def test_conflicting_control_binding_admission_leaves_graph_unchanged(
    neo4j_driver: Driver,
) -> None:
    _seed(neo4j_driver)
    client = _migrate(neo4j_driver)
    repository = BitrixSourceInstanceRepository(cast(Neo4jClient, client))
    repository.register("bitrix_chat", "portal-a")
    repository.register("bitrix_chat", "portal-b")
    repository.admit(control_instance_id="legacy-default", source_instance_id="portal-a")

    with neo4j_driver.session() as session:
        before = session.run(
            "MATCH (binding:BitrixExecutionSourceBinding {source_key: 'bitrix_chat', "
            "control_instance_id: 'legacy-default'}) "
            "OPTIONAL MATCH (owner:BitrixSourceInstance)-[:OWNS_BITRIX_CONTROL]->(binding) "
            "RETURN count(DISTINCT binding) AS binding_count, "
            "collect(DISTINCT binding.source_instance_id) AS bound_sources, "
            "collect(DISTINCT owner.source_instance_id) AS owners"
        ).single(strict=True)

    with pytest.raises(BitrixControlAdmissionError):
        repository.admit(control_instance_id="legacy-default", source_instance_id="portal-b")

    with neo4j_driver.session() as session:
        after = session.run(
            "MATCH (binding:BitrixExecutionSourceBinding {source_key: 'bitrix_chat', "
            "control_instance_id: 'legacy-default'}) "
            "OPTIONAL MATCH (owner:BitrixSourceInstance)-[:OWNS_BITRIX_CONTROL]->(binding) "
            "RETURN count(DISTINCT binding) AS binding_count, "
            "collect(DISTINCT binding.source_instance_id) AS bound_sources, "
            "collect(DISTINCT owner.source_instance_id) AS owners"
        ).single(strict=True)
        portal_b_bindings = session.run(
            "MATCH (portal:BitrixSourceInstance {source_key: 'bitrix_chat', "
            "source_instance_id: 'portal-b'}) "
            "OPTIONAL MATCH (portal)-[:OWNS_BITRIX_CONTROL]->(binding) "
            "RETURN count(binding) AS binding_count"
        ).single(strict=True)

    assert dict(after) == dict(before)
    assert dict(after) == {
        "binding_count": 1,
        "bound_sources": ["portal-a"],
        "owners": ["portal-a"],
    }
    assert portal_b_bindings["binding_count"] == 0


def test_disable_ignores_work_bound_to_a_different_registered_portal(neo4j_driver: Driver) -> None:
    _seed(neo4j_driver)
    client = _migrate(neo4j_driver)
    repository = BitrixSourceInstanceRepository(cast(Neo4jClient, client))
    repository.register("bitrix_chat", "portal-a")
    repository.register("bitrix_chat", "portal-b")
    repository.admit(control_instance_id="legacy-default", source_instance_id="portal-a")
    repository.admit(control_instance_id="portal-b", source_instance_id="portal-b")
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (generation:BitrixBackfillGeneration {control_instance_id: 'legacy-default'}) "
            "SET generation.status = 'completed'"
        ).consume()
        session.run(
            "CREATE (:IngestRun {ingest_run_id: 'portal-b-running', "
            "source_key: 'bitrix_chat', control_instance_id: 'portal-b', status: 'running'})"
        ).consume()
    repository.disable("bitrix_chat", "portal-a", "operator", "retire")
    with neo4j_driver.session() as session:
        status = session.run(
            "MATCH (instance:BitrixSourceInstance {source_key: 'bitrix_chat', "
            "source_instance_id: 'portal-a'}) RETURN instance.status AS status"
        ).single(strict=True)
    assert status["status"] == "disabled"
