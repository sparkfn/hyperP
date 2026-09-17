"""Neo4j integration coverage for #272 control migration.

The fixture only permits an explicitly configured disposable local database.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session, unit_of_work
from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedUnit,
    OccurrenceContext,
    RetryObligation,
    RetryResolution,
    RunScope,
    UnitApplyResult,
    Usage,
)
from src.graph.bitrix_source_instances import (
    BitrixControlAdmissionError,
    BitrixSourceInstanceConflictError,
    BitrixSourceInstanceRepository,
)
from src.graph.bootstrap import bootstrap_legacy_bitrix_source_instance
from src.graph.bounded_ingestion_control import BoundedIngestionControl
from src.graph.bounded_ingestion_schema import CREATE_BOUNDED_INGESTION_SCHEMA
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
from src.resumable import CheckpointDescriptor, IngestionUnit

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
    "ingestion_reset_generation_unique",
    "bounded_ingestion_scope_key_unique",
    "bounded_ingestion_logical_key_unique",
    "bounded_ingestion_global_slot_unique",
    "bounded_ingestion_reservation_identity_unique",
    "bounded_ingestion_receipt_attempt_identity_unique",
    "bounded_ingestion_retry_identity_unique",
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

    def execute_write(
        self,
        work: Callable[[ManagedTransaction], T],
        *,
        transaction_timeout_seconds: float | None = None,
    ) -> T:
        transaction_work = work
        if transaction_timeout_seconds is not None:
            transaction_work = unit_of_work(timeout=transaction_timeout_seconds)(work)
        with self._driver.session() as session:
            return session.execute_write(transaction_work)


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


class _BoundedWriter:
    def __init__(self, *, retry: bool = False, resolve: bool = False) -> None:
        self.retry = retry
        self.resolve = resolve
        self.calls = 0

    def apply(
        self,
        tx: ManagedTransaction,
        _context: AttemptContext,
        bounded_unit: BoundedUnit,
    ) -> UnitApplyResult:
        self.calls += 1
        if not self.retry:
            for record in bounded_unit.unit.records:
                tx.run(
                    "MERGE (output:BoundedTestOutput {record_id: $record_id}) "
                    "SET output.version = $version",
                    record_id=record["id"],
                    version=record["version"],
                ).consume()
        if self.retry:
            return UnitApplyResult(
                dispositions=("durable_retry",),
                retry_obligations=(
                    RetryObligation(
                        replay_id=bounded_unit.replay_id,
                        source_record_id="record-1",
                        source_version="v1",
                        category="fixture",
                        attempt_count=1,
                        eligible_at=datetime(2026, 9, 24, 1, tzinfo=UTC),
                    ),
                ),
            )
        if self.resolve:
            return UnitApplyResult(
                dispositions=("committed",),
                resolved_retries=(
                    RetryResolution(replay_id="page-0", source_record_id="record-1"),
                ),
            )
        return UnitApplyResult(dispositions=("committed",))


def _bounded_scope(source_key: str, window: str = "window-1") -> RunScope:
    return RunScope(
        environment="test",
        reset_generation=1,
        source_key=source_key,
        control_instance_id="bounded-control",
        entity_key=None,
        stream_key=None,
        mode="delta",
        configuration_fingerprint="sha256:stable-config",
        connector_version="fixture-v1",
        checkpoint_schema_version=1,
        source_window={"window": window},
    )


def _bounded_occurrence(week: int = 0) -> OccurrenceContext:
    start = datetime.now(UTC) - timedelta(seconds=1) + timedelta(days=7 * week)
    return OccurrenceContext(
        occurrence_id=f"week-{week}",
        starts_at=start,
        drain_starts_at=start + timedelta(hours=13, minutes=55),
        cutoff_at=start + timedelta(hours=14),
        next_eligible_at=start + timedelta(days=7),
        scheduled=False,
    )


def _bounded_checkpoint(window: str = "window-1", page: int = 0) -> CheckpointDescriptor:
    return CheckpointDescriptor(
        phase="records",
        cursor={"page": page},
        source_window={"window": window},
        last_committed_record_id=None,
        connector_version="fixture-v1",
        schema_version=1,
        replay_boundary="page",
    )


def _bounded_unit(*, terminal: bool = True) -> BoundedUnit:
    before = _bounded_checkpoint()
    after = _bounded_checkpoint(page=1)
    return BoundedUnit(
        unit=IngestionUnit(
            before,
            after,
            ({"id": "record-1", "version": "v1"},),
        ),
        replay_id="page-0",
        usage=Usage(records=1, source_requests=1, pages=1, bytes_read=32),
        terminal=terminal,
    )


def _prepare_bounded_graph(driver: Driver, *source_keys: str) -> None:
    with driver.session() as session:
        for query in CREATE_BOUNDED_INGESTION_SCHEMA:
            session.run(query).consume()
        for source_key in source_keys:
            session.run(
                "MERGE (:SourceSystem {source_key: $source_key, is_active: true})",
                source_key=source_key,
            ).consume()
        session.run(
            "CREATE (:IngestionResetGeneration {environment: 'test', generation: 1, "
            "status: 'active'})"
        ).consume()


def test_bounded_queries_commit_and_finalize_against_disposable_neo4j(
    neo4j_driver: Driver,
) -> None:
    _prepare_bounded_graph(neo4j_driver, "fixture")
    control = BoundedIngestionControl(cast(Neo4jClient, _Client(neo4j_driver)))
    occurrence = _bounded_occurrence()
    context = control.admit_or_resume(
        scope=_bounded_scope("fixture"),
        occurrence=occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="task-1",
        now=occurrence.starts_at,
        max_graph_writers=1,
    )
    assert isinstance(context, AttemptContext)
    writer = _BoundedWriter()
    result = control.commit_unit(context, _bounded_unit(), writer)
    assert result is not None
    assert control.finalize(context) is True
    status = control.status(context.logical_run_id)
    assert status is not None
    assert status.status == "completed"
    assert status.checkpoint_cursor_present is True
    assert status.attempt_generation == 1
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (output:BoundedTestOutput) "
            "MATCH (receipt:BoundedIngestionReceipt {status: 'committed'}) "
            "RETURN count(output) AS outputs, count(receipt) AS receipts"
        ).single(strict=True)
    assert dict(row) == {"outputs": 1, "receipts": 1}
    assert writer.calls == 1

    second_scope = _bounded_scope("fixture", window="window-2")
    second = control.admit_or_resume(
        scope=second_scope,
        occurrence=_bounded_occurrence(week=1),
        initial_checkpoint=_bounded_checkpoint(window="window-2"),
        worker_task_id="task-2",
        now=_bounded_occurrence(week=1).starts_at,
        max_graph_writers=1,
    )
    assert isinstance(second, AttemptContext)
    assert second.logical_run_id != context.logical_run_id


def test_bounded_retry_backlog_blocks_terminal_completion_in_neo4j(
    neo4j_driver: Driver,
) -> None:
    _prepare_bounded_graph(neo4j_driver, "fixture")
    control = BoundedIngestionControl(cast(Neo4jClient, _Client(neo4j_driver)))
    occurrence = _bounded_occurrence()
    context = control.admit_or_resume(
        scope=_bounded_scope("fixture"),
        occurrence=occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="task-retry",
        now=occurrence.starts_at,
    )
    assert isinstance(context, AttemptContext)
    retry_writer = _BoundedWriter(retry=True)
    result = control.commit_unit(context, _bounded_unit(), retry_writer)
    assert result is not None
    replay = control.commit_unit(context, _bounded_unit(), retry_writer)
    assert replay is not None
    assert retry_writer.calls == 1
    assert control.finalize(context) is False
    status = control.status(context.logical_run_id)
    assert status is not None
    assert status.status == "running"
    assert status.retry_backlog == 1
    assert status.usage.records == 1
    assert status.checkpoint_cursor_present is True
    with neo4j_driver.session() as session:
        checkpoint = session.run(
            "MATCH (checkpoint:IngestionCheckpoint {logical_run_id: $logical_run_id}) "
            "RETURN checkpoint.cursor_json AS cursor, checkpoint.terminal_committed AS terminal",
            logical_run_id=context.logical_run_id,
        ).single(strict=True)
    assert '"page":0' in checkpoint["cursor"]
    assert checkpoint["terminal"] is False
    assert control.pause(context, "source_backoff", occurrence.next_eligible_at) is True

    next_occurrence = _bounded_occurrence(week=1)
    resumed = control.admit_or_resume(
        scope=_bounded_scope("fixture"),
        occurrence=next_occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="task-retry-resolved",
        now=next_occurrence.starts_at,
    )
    assert isinstance(resumed, AttemptContext)
    resolved = control.commit_unit(
        resumed,
        _bounded_unit(),
        _BoundedWriter(resolve=True),
    )
    assert resolved is not None
    assert control.finalize(resumed) is True
    final_status = control.status(resumed.logical_run_id)
    assert final_status is not None
    assert final_status.status == "completed"
    assert final_status.retry_backlog == 0


def test_active_reset_and_global_slot_fence_other_bounded_writers(
    neo4j_driver: Driver,
) -> None:
    _prepare_bounded_graph(neo4j_driver, "fixture-a", "fixture-b")
    control = BoundedIngestionControl(cast(Neo4jClient, _Client(neo4j_driver)))
    occurrence = _bounded_occurrence()
    first = control.admit_or_resume(
        scope=_bounded_scope("fixture-a"),
        occurrence=occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="task-a",
        now=occurrence.starts_at,
        max_graph_writers=1,
    )
    assert isinstance(first, AttemptContext)
    second = control.admit_or_resume(
        scope=_bounded_scope("fixture-b"),
        occurrence=occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="task-b",
        now=occurrence.starts_at,
        max_graph_writers=1,
    )
    assert second is None
    assert (
        control.compare_and_advance_reset_generation(
            environment="test",
            expected_generation=1,
            actor="test",
            authorization_reference="issue-430",
        )
        == 2
    )
    writer = _BoundedWriter()
    assert control.commit_unit(first, _bounded_unit(), writer) is None
    assert writer.calls == 0
    assert (
        control.compare_and_advance_reset_generation(
            environment="test",
            expected_generation=1,
            actor="test",
            authorization_reference="issue-430",
        )
        is None
    )


def test_expired_recovery_claim_uses_durable_identity_and_terminal_evidence(
    neo4j_driver: Driver,
) -> None:
    _prepare_bounded_graph(neo4j_driver, "fixture")
    control = BoundedIngestionControl(cast(Neo4jClient, _Client(neo4j_driver)))
    occurrence = _bounded_occurrence()
    initial_lease_seconds = 300
    lease_expires_at = occurrence.starts_at + timedelta(seconds=initial_lease_seconds)
    first = control.admit_or_resume(
        scope=_bounded_scope("fixture"),
        occurrence=occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="recovery-first",
        now=occurrence.starts_at,
        lease_seconds=initial_lease_seconds,
    )
    assert isinstance(first, AttemptContext)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (logical:IngestionLogicalRun {logical_run_id: $logical_run_id}) "
            "SET logical.publication_intent = true, logical.recovery_authorized = true",
            logical_run_id=first.logical_run_id,
        ).consume()
    assert control.commit_unit(first, _bounded_unit(), _BoundedWriter()) is not None
    leased = control.recovery_state(
        first.logical_run_id,
        "fixture",
        "bounded-control",
        1,
        lease_expires_at - timedelta(seconds=1),
    )
    assert leased == lease_expires_at
    recovery_at = lease_expires_at + timedelta(seconds=1)
    recovered = control.recovery_state(
        first.logical_run_id,
        "fixture",
        "bounded-control",
        1,
        recovery_at,
    )
    assert recovered is not None
    second = control.admit_or_resume(
        scope=recovered.scope,
        occurrence=recovered.occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="recovery-second",
        now=recovery_at,
        lease_seconds=initial_lease_seconds,
    )
    assert isinstance(second, AttemptContext)
    assert second.attempt_generation == 2
    assert second.terminal_observed is True
    assert second.terminal_checkpoint_committed is True
    assert control.finalize(second) is True
    assert (
        control.recovery_state(
            first.logical_run_id,
            "fixture",
            "bounded-control",
            1,
            recovery_at + timedelta(seconds=1),
        )
        == "completed"
    )


def test_later_source_retry_eligibility_rejects_an_intervening_weekly_rebind(
    neo4j_driver: Driver,
) -> None:
    _prepare_bounded_graph(neo4j_driver, "fixture")
    control = BoundedIngestionControl(cast(Neo4jClient, _Client(neo4j_driver)))
    first_occurrence = _bounded_occurrence()
    first = control.admit_or_resume(
        scope=_bounded_scope("fixture"),
        occurrence=first_occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="failed-first",
        now=first_occurrence.starts_at,
    )
    assert isinstance(first, AttemptContext)
    intervening_occurrence = _bounded_occurrence(week=1)
    source_retry_at = intervening_occurrence.starts_at + timedelta(days=1)
    later_occurrence = _bounded_occurrence(week=2)
    assert source_retry_at < later_occurrence.starts_at
    assert (
        control.fail(
            first,
            "source",
            "fixture failure",
            later_occurrence.starts_at,
        )
        is True
    )

    # A scheduled delivery before the persisted later eligibility cannot claim
    # an attempt, so it cannot create or call a source connector.
    intervening = control.admit_or_resume(
        scope=_bounded_scope("fixture"),
        occurrence=intervening_occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="intervening-week",
        now=intervening_occurrence.starts_at,
    )
    assert intervening is None

    resumed = control.admit_or_resume(
        scope=_bounded_scope("fixture"),
        occurrence=later_occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="later-week",
        now=later_occurrence.starts_at,
    )
    assert isinstance(resumed, AttemptContext)
    assert resumed.logical_run_id == first.logical_run_id
    assert resumed.attempt_generation == 2


def test_unfinished_scope_rejects_a_different_source_window(
    neo4j_driver: Driver,
) -> None:
    _prepare_bounded_graph(neo4j_driver, "fixture")
    control = BoundedIngestionControl(cast(Neo4jClient, _Client(neo4j_driver)))
    occurrence = _bounded_occurrence()
    first = control.admit_or_resume(
        scope=_bounded_scope("fixture", window="window-1"),
        occurrence=occurrence,
        initial_checkpoint=_bounded_checkpoint(window="window-1"),
        worker_task_id="window-one",
        now=occurrence.starts_at,
    )
    assert isinstance(first, AttemptContext)
    assert control.pause(first, "budget", occurrence.next_eligible_at) is True
    second = control.admit_or_resume(
        scope=_bounded_scope("fixture", window="window-2"),
        occurrence=_bounded_occurrence(week=1),
        initial_checkpoint=_bounded_checkpoint(window="window-2"),
        worker_task_id="window-two",
        now=_bounded_occurrence(week=1).starts_at,
    )
    assert second is None


def test_queued_run_from_slot_contention_rebinds_next_week(
    neo4j_driver: Driver,
) -> None:
    _prepare_bounded_graph(neo4j_driver, "fixture-a", "fixture-b")
    control = BoundedIngestionControl(cast(Neo4jClient, _Client(neo4j_driver)))
    first_occurrence = _bounded_occurrence()
    owner = control.admit_or_resume(
        scope=_bounded_scope("fixture-a"),
        occurrence=first_occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="slot-owner",
        now=first_occurrence.starts_at,
        max_graph_writers=1,
    )
    assert isinstance(owner, AttemptContext)
    blocked = control.admit_or_resume(
        scope=_bounded_scope("fixture-b"),
        occurrence=first_occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="queued-first-week",
        now=first_occurrence.starts_at,
        max_graph_writers=1,
    )
    assert blocked is None
    assert control.pause(owner, "budget", first_occurrence.next_eligible_at) is True
    next_occurrence = _bounded_occurrence(week=1)
    resumed = control.admit_or_resume(
        scope=_bounded_scope("fixture-b"),
        occurrence=next_occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="queued-next-week",
        now=next_occurrence.starts_at,
        max_graph_writers=1,
    )
    assert isinstance(resumed, AttemptContext)
    assert resumed.attempt_generation == 1


def test_bounded_bitrix_successor_retires_only_its_own_active_predecessor(
    neo4j_driver: Driver,
) -> None:
    from src.graph.queries.bounded_ingestion_control import RETIRE_OWNED_BITRIX_PREDECESSOR

    with neo4j_driver.session() as session:
        session.run(
            "CREATE (owned:BitrixIngestionStream {source_key: 'bitrix_chat', "
            "control_instance_id: 'bounded-control', stream_key: 'crm_deals', "
            "logical_run_id: 'logical-owned', attempt_generation: 1, status: 'active'}), "
            "(unrelated:BitrixIngestionStream {source_key: 'bitrix_chat', "
            "control_instance_id: 'other-control', stream_key: 'crm_deals', "
            "logical_run_id: 'logical-other', attempt_generation: 1, status: 'active'})"
        ).consume()
        retired = session.run(
            RETIRE_OWNED_BITRIX_PREDECESSOR,
            control_instance_id="bounded-control",
            stream_key="crm_deals",
            logical_run_id="logical-owned",
            attempt_generation=2,
        ).single(strict=True)
        states = session.run(
            "MATCH (stream:BitrixIngestionStream) "
            "RETURN stream.control_instance_id AS control_instance_id, stream.status AS status "
            "ORDER BY control_instance_id"
        ).data()

    assert retired["logical_run_id"] == "logical-owned"
    assert states == [
        {"control_instance_id": "bounded-control", "status": "superseded"},
        {"control_instance_id": "other-control", "status": "active"},
    ]


def test_queued_slot_contended_run_is_recoverable_without_lease_metadata(
    neo4j_driver: Driver,
) -> None:
    _prepare_bounded_graph(neo4j_driver, "fixture-a", "fixture-b")
    control = BoundedIngestionControl(cast(Neo4jClient, _Client(neo4j_driver)))
    occurrence = _bounded_occurrence()
    owner = control.admit_or_resume(
        scope=_bounded_scope("fixture-a"),
        occurrence=occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="slot-owner",
        now=occurrence.starts_at,
        max_graph_writers=1,
    )
    assert isinstance(owner, AttemptContext)
    blocked = control.admit_or_resume(
        scope=_bounded_scope("fixture-b"),
        occurrence=occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="queued-recovery",
        now=occurrence.starts_at,
        max_graph_writers=1,
    )
    assert blocked is None
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (logical:IngestionLogicalRun {source_key: 'fixture-b'}) "
            "SET logical.recovery_authorized = true, logical.publication_intent = true "
            "RETURN logical.logical_run_id AS logical_run_id, logical.lease_expires_at AS lease"
        ).single(strict=True)
    assert row["lease"] is None
    recovery = control.recovery_state(
        row["logical_run_id"],
        "fixture-b",
        "bounded-control",
        1,
        occurrence.starts_at,
    )
    assert recovery is not None
    assert recovery != "completed"
    assert not isinstance(recovery, datetime)


def test_bounded_bitrix_conflict_fails_attempt_without_mutating_unrelated_stream(
    neo4j_driver: Driver,
) -> None:
    _prepare_bounded_graph(neo4j_driver, "bitrix_chat")
    control = BoundedIngestionControl(cast(Neo4jClient, _Client(neo4j_driver)))
    first_occurrence = _bounded_occurrence()
    scope = RunScope(
        environment="test",
        reset_generation=1,
        source_key="bitrix_chat",
        control_instance_id="bounded-control",
        entity_key=None,
        stream_key="crm_deals",
        mode="delta",
        configuration_fingerprint="sha256:bitrix-fixture",
        connector_version="fixture-v1",
        checkpoint_schema_version=1,
        source_window={"window": "bitrix"},
    )
    first = control.admit_or_resume(
        scope=scope,
        occurrence=first_occurrence,
        initial_checkpoint=_bounded_checkpoint(),
        worker_task_id="bitrix-first",
        now=first_occurrence.starts_at,
    )
    assert isinstance(first, AttemptContext)
    assert control.pause(first, "budget", first_occurrence.next_eligible_at) is True
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (stream:BitrixIngestionStream {source_key: 'bitrix_chat', "
            "control_instance_id: 'bounded-control', stream_key: 'crm_deals'}) "
            "SET stream.logical_run_id = 'unrelated-logical', "
            "stream.ingest_run_id = 'unrelated-run', "
            "stream.attempt_generation = 1, stream.stream_generation = 41, "
            "stream.fencing_token = 43, "
            "stream.worker_task_id = 'unrelated-worker', "
            "stream.status = 'active', "
            "stream.bounded_takeover_lock_version = 47"
        ).consume()
    next_occurrence = _bounded_occurrence(week=1)
    with pytest.raises(ValueError, match="Bitrix stream admission did not return a record"):
        control.admit_or_resume(
            scope=scope,
            occurrence=next_occurrence,
            initial_checkpoint=_bounded_checkpoint(),
            worker_task_id="bitrix-second",
            now=next_occurrence.starts_at,
        )
    with neo4j_driver.session() as session:
        stream = session.run(
            "MATCH (stream:BitrixIngestionStream {source_key: 'bitrix_chat', "
            "control_instance_id: 'bounded-control', stream_key: 'crm_deals'}) "
            "RETURN stream.logical_run_id AS logical_run_id, "
            "stream.ingest_run_id AS ingest_run_id, "
            "stream.attempt_generation AS attempt_generation, "
            "stream.stream_generation AS stream_generation, "
            "stream.fencing_token AS fencing_token, "
            "stream.worker_task_id AS worker_task_id, stream.status AS status, "
            "stream.bounded_takeover_lock_version AS lock_version"
        ).single(strict=True)
        logical = session.run(
            "MATCH (logical:IngestionLogicalRun {logical_run_id: $logical_run_id}) "
            "OPTIONAL MATCH (logical)-[:ACTIVE_ATTEMPT]->(attempt:IngestRun) "
            "OPTIONAL MATCH (slot:BoundedIngestionGlobalSlot {"
            "owner_logical_run_id: logical.logical_run_id}) "
            "RETURN logical.bounded_status AS status, "
            "logical.failure_category AS failure_category, "
            "logical.failure_message AS failure_message, "
            "count(attempt) AS active_attempts, count(slot) AS owned_slots",
            logical_run_id=first.logical_run_id,
        ).single(strict=True)
    assert dict(stream) == {
        "logical_run_id": "unrelated-logical",
        "ingest_run_id": "unrelated-run",
        "attempt_generation": 1,
        "stream_generation": 41,
        "fencing_token": 43,
        "worker_task_id": "unrelated-worker",
        "status": "active",
        "lock_version": 47,
    }
    assert dict(logical) == {
        "status": "failed",
        "failure_category": "lease",
        "failure_message": "bitrix_stream_admission_conflict",
        "active_attempts": 0,
        "owned_slots": 0,
    }
