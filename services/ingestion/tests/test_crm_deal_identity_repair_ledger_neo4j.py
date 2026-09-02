"""Disposable persisted-evidence acceptance coverage for the #300 repair ledger.

This suite is deliberately opt-in.  It uses only a dedicated disposable Neo4j
database and never contacts Bitrix.  The fixture creates the persisted #272
source/control evidence that the ledger reads; qualification itself must create
only its repair metadata.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.crm_deal_identity_repair.allocation import AllocationPlan
from src.crm_deal_identity_repair.control_models import (
    RepairControlRequest,
    control_token_digest,
)
from src.crm_deal_identity_repair.execution_models import (
    RepairBoundarySnapshot,
    RepairExecutionBoundaryManifest,
    RepairQualificationRun,
)
from src.crm_deal_identity_repair.task_inspection import BrokerInspection, TaskAbsenceEvidence
from src.graph import crm_deal_identity_repair_control as control_repository_module
from src.graph import crm_deal_identity_repair_ledger_migration as ledger_migration
from src.graph.bitrix_source_instances import BitrixSourceInstanceRepository
from src.graph.bootstrap import bootstrap_legacy_bitrix_source_instance
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_control import CrmDealRepairControlRepository
from src.graph.crm_deal_identity_repair_ledger import (
    CrmDealRepairLedgerRepository,
    ExpectedRepairBoundaryDriftError,
)
from src.graph.crm_deal_identity_repair_ledger_migration import (
    MIGRATION_KEY,
    REQUIRED_CONSTRAINTS,
    REQUIRED_INDEXES,
    assert_crm_deal_repair_ledger_ready,
    ensure_crm_deal_repair_ledger_ready,
)
from src.graph.ingestion_control_instance_migration import migrate_ingestion_control_instances

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
_TEST_SOURCE_INSTANCE_ID = "repair-test-source"
_TEST_CONTROL_INSTANCE_ID = "repair-test-control"
_TEST_SOURCE_RECORD_PK = "repair-test-crm-deal-001"
_REPAIR_RANGE_INDEX_INVENTORY = (
    "SHOW INDEXES YIELD name, type, owningConstraint "
    "WHERE name STARTS WITH 'crm_deal_repair_' AND type = 'RANGE' "
    "AND owningConstraint IS NULL RETURN name ORDER BY name"
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


def _drop_repair_schema(driver: Driver) -> None:
    with driver.session() as session:
        for name in REQUIRED_CONSTRAINTS:
            session.run(f"DROP CONSTRAINT {name} IF EXISTS").consume()
        for name in REQUIRED_INDEXES:
            session.run(f"DROP INDEX {name} IF EXISTS").consume()


def _clear_repair_metadata(driver: Driver) -> None:
    labels = (
        "CrmDealRepairRun",
        "RepairExecutionBoundary",
        "CrmDealRepairQuiescence",
        "CrmDealRepairUnit",
        "CrmDealRepairCheckpoint",
        "CrmDealRepairFence",
        "CrmDealRepairMutationResult",
        "CrmDealRepairRollbackImage",
        "CrmDealRepairSecondaryDisposition",
        "CrmDealRepairVerification",
        "CrmDealRepairOutbox",
    )
    with driver.session() as session:
        for label in labels:
            session.run(f"MATCH (node:{label}) DETACH DELETE node").consume()
        session.run(
            "MATCH (marker:DataMigration {migration_key: $migration_key}) DETACH DELETE marker",
            migration_key=MIGRATION_KEY,
        ).consume()
        session.run(
            "MATCH (record:SourceRecord) WHERE record.source_record_pk STARTS WITH 'repair-test-' "
            "DETACH DELETE record"
        ).consume()
        session.run(
            "MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) DETACH DELETE dispatch",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
        session.run(
            "MATCH (binding:BitrixExecutionSourceBinding {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) DETACH DELETE binding",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
        session.run(
            "MATCH (instance:BitrixSourceInstance {source_key: 'bitrix_chat'}) "
            "WHERE instance.source_instance_id STARTS WITH 'repair-test-' "
            "OR instance.source_instance_id IN $instance_ids DETACH DELETE instance",
            instance_ids=[_TEST_SOURCE_INSTANCE_ID, _TEST_CONTROL_INSTANCE_ID],
        ).consume()
        session.run(
            "MATCH (node) WHERE node.control_instance_id = $control_instance_id DETACH DELETE node",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
        session.run(
            "MATCH (run:IngestRun {ingest_run_id: $stale_run_id}) DETACH DELETE run",
            stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
        ).consume()
        session.run(
            "MATCH (node) WHERE node.control_instance_id = 'other-control' DETACH DELETE node"
        ).consume()


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    uri = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_URI")
    user = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_USER")
    password = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_PASSWORD")
    if uri is None or user is None or password is None:
        pytest.skip("disposable CRM repair ledger Neo4j database is not configured")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_SERVICE_HOST") == "neo4j":
        allowed_hosts.add("neo4j")
    if urlparse(uri).hostname not in allowed_hosts:
        pytest.fail("CRM repair ledger tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(uri, auth=(user, password))
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
            pytest.fail("disposable CRM repair ledger Neo4j database did not become ready")
        _clear_repair_metadata(driver)
        _drop_repair_schema(driver)
        prepared = True
        yield driver
    finally:
        try:
            if prepared:
                _clear_repair_metadata(driver)
            if connected:
                _drop_repair_schema(driver)
        finally:
            driver.close()


def _client(driver: Driver) -> _Client:
    return _Client(driver)


def _repository(driver: Driver) -> CrmDealRepairLedgerRepository:
    client = _client(driver)
    with driver.session() as session:
        session.run(_MIGRATION_CONSTRAINT).consume()
        session.run(_REGISTRY_CONSTRAINT).consume()
        session.run(
            "MERGE (source:SourceSystem {source_key: 'bitrix_chat'}) SET source.is_active = true"
        ).consume()
    migrate_ingestion_control_instances(
        cast(Neo4jClient, client),
        ensure_legacy_registration=lambda: bootstrap_legacy_bitrix_source_instance(
            cast(Neo4jClient, client)
        ),
    )
    ensure_crm_deal_repair_ledger_ready(cast(Neo4jClient, client))
    return CrmDealRepairLedgerRepository(cast(Neo4jClient, client))


def _persist_evidence(
    driver: Driver,
    *,
    source_instance_id: str = _TEST_SOURCE_INSTANCE_ID,
    control_instance_id: str = _TEST_CONTROL_INSTANCE_ID,
    source_record_pk: str = _TEST_SOURCE_RECORD_PK,
) -> None:
    client = _client(driver)
    instances = BitrixSourceInstanceRepository(cast(Neo4jClient, client))
    instances.register("bitrix_chat", source_instance_id)
    if control_instance_id != source_instance_id:
        instances.register("bitrix_chat", control_instance_id)
    instances.admit(
        source_instance_id=source_instance_id,
        control_instance_id=control_instance_id,
    )
    with driver.session() as session:
        session.run(
            "MERGE (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) "
            "SET dispatch.blocked = false, dispatch.updated_at = datetime()",
            control_instance_id=control_instance_id,
        ).consume()
        session.run(
            "MATCH (source:SourceSystem {source_key: 'bitrix_chat'}) "
            "CREATE (record:SourceRecord {source_record_pk: $source_record_pk, "
            "source_record_id: 'bitrix-crm-deal-' + $source_record_pk, source_record_version: '1', "
            "source_version_key: $source_record_pk + ':1', record_hash: 'sha256:source-hash', "
            "lifecycle_status: 'active', is_latest: true, record_type: 'crm_deal', "
            "source_instance_id: $source_instance_id, "
            'raw_payload: \'{"crm_deal_identity_policy_version":"legacy"}\', '
            "normalized_payload: '{}'}) "
            "CREATE (record)-[:FROM_SOURCE]->(source)",
            source_record_pk=source_record_pk,
            source_instance_id=source_instance_id,
        ).consume()


def _snapshot(
    repository: CrmDealRepairLedgerRepository,
    *,
    source_instance_id: str = _TEST_SOURCE_INSTANCE_ID,
    control_instance_id: str = _TEST_CONTROL_INSTANCE_ID,
    source_record_pk: str = _TEST_SOURCE_RECORD_PK,
) -> RepairBoundarySnapshot:
    return repository.snapshot(
        source_instance_id=source_instance_id,
        control_instance_id=control_instance_id,
        source_record_pks=(source_record_pk,),
    )


def _manifest(
    snapshot: RepairBoundarySnapshot,
    *,
    repair_id: str = "repair-300",
    artifact_id: str = "a" * 32,
    source_instance_id: str = _TEST_SOURCE_INSTANCE_ID,
    control_instance_id: str = _TEST_CONTROL_INSTANCE_ID,
    approval_reference: str = "approval-300",
) -> RepairExecutionBoundaryManifest:
    return RepairExecutionBoundaryManifest(
        repair_id=repair_id,
        artifact_id=artifact_id,
        artifact_manifest_hmac="b" * 64,
        inventory_digest=snapshot.inventory_digest,
        repository_sha="d" * 40,
        image_digest="sha256:" + "e" * 64,
        configuration_digest="sha256:" + "f" * 64,
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        environment="staging",
        approval_reference=approval_reference,
        unit_ceiling=1,
        stop_conditions=("boundary_drift",),
        source_instance_id=source_instance_id,
        control_instance_id=control_instance_id,
        rollback_authority_reference="rollback-300",
        rollback_authority_policy="reviewed-only",
        graph_boundary_digest=snapshot.boundary_digest,
        inventory_row_count=snapshot.inventory_row_count,
        eligible_unit_count=snapshot.eligible_unit_count,
        negative_control_count=snapshot.negative_control_count,
    )


def _domain_state(
    driver: Driver, *, source_instance_id: str, control_instance_id: str
) -> dict[str, tuple[str, ...]]:
    with driver.session() as session:
        nodes = session.run(
            "MATCH (node) WHERE (node:SourceRecord AND node.source_record_pk = $source_record_pk) "
            "OR (node:BitrixSourceInstance AND node.source_key = 'bitrix_chat' "
            "AND node.source_instance_id IN $instance_ids) "
            "OR (node:BitrixExecutionSourceBinding AND node.source_key = 'bitrix_chat' "
            "AND node.control_instance_id = $control_instance_id) "
            "OR (node:BitrixDispatchControl AND node.source_key = 'bitrix_chat' "
            "AND node.control_instance_id = $control_instance_id) "
            "RETURN labels(node) AS labels, properties(node) AS properties",
            source_record_pk=_TEST_SOURCE_RECORD_PK,
            instance_ids=[source_instance_id, control_instance_id],
            control_instance_id=control_instance_id,
        ).data()
        relationships = session.run(
            "MATCH (left)-[relationship]->(right) "
            "WHERE (left:SourceRecord AND left.source_record_pk = $source_record_pk) "
            "OR (right:SourceRecord AND right.source_record_pk = $source_record_pk) "
            "OR (left:BitrixExecutionSourceBinding AND left.source_key = 'bitrix_chat' "
            "AND left.control_instance_id = $control_instance_id) "
            "OR (right:BitrixExecutionSourceBinding AND right.source_key = 'bitrix_chat' "
            "AND right.control_instance_id = $control_instance_id) "
            "RETURN labels(left) AS left_labels, properties(left) AS left_properties, "
            "type(relationship) AS relationship_type, "
            "properties(relationship) AS relationship_properties, "
            "labels(right) AS right_labels, properties(right) AS right_properties",
            source_record_pk=_TEST_SOURCE_RECORD_PK,
            control_instance_id=control_instance_id,
        ).data()
    return {
        "nodes": _canonical_domain_rows(nodes),
        "relationships": _canonical_domain_rows(relationships),
    }


def _canonical_domain_rows(rows: list[dict[str, object]]) -> tuple[str, ...]:
    return tuple(
        sorted(json.dumps(row, default=str, separators=(",", ":"), sort_keys=True) for row in rows)
    )


def _repair_metadata_counts(driver: Driver) -> dict[str, int]:
    with driver.session() as session:
        row = session.run(
            "MATCH (run:CrmDealRepairRun) "
            "OPTIONAL MATCH (run)-[link:QUALIFIED_WITH]->(boundary:RepairExecutionBoundary) "
            "WITH count(DISTINCT run) AS runs, count(DISTINCT boundary) AS boundaries, "
            "count(link) AS links "
            "CALL { MATCH (node) WHERE any(label IN labels(node) "
            "WHERE label STARTS WITH 'CrmDealRepair') RETURN count(node) AS extra } "
            "RETURN runs, boundaries, links, extra"
        ).single(strict=True)
    return {
        "runs": row["runs"],
        "boundaries": row["boundaries"],
        "links": row["links"],
        "extra": row["extra"],
    }


def test_readiness_requires_272_dependency(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(neo4j_driver)

    def _reject_dependency(_: Neo4jClient) -> None:
        raise RuntimeError("#272 is incomplete")

    monkeypatch.setattr(ledger_migration, "assert_ingestion_control_ready", _reject_dependency)
    with pytest.raises(RuntimeError, match="#272 is incomplete"):
        ensure_crm_deal_repair_ledger_ready(cast(Neo4jClient, client))


def test_readiness_is_fresh_idempotent_exact_and_rejects_malformed_schema(
    neo4j_driver: Driver,
) -> None:
    _repository(neo4j_driver)
    client = _client(neo4j_driver)
    ensure_crm_deal_repair_ledger_ready(cast(Neo4jClient, client))
    assert_crm_deal_repair_ledger_ready(cast(Neo4jClient, client))
    with neo4j_driver.session() as session:
        constraints = session.run(
            "SHOW CONSTRAINTS YIELD name WHERE name STARTS WITH 'crm_deal_repair_' "
            "RETURN name ORDER BY name"
        ).value("name")
        indexes = session.run(_REPAIR_RANGE_INDEX_INVENTORY).value("name")
    assert constraints == sorted(REQUIRED_CONSTRAINTS)
    assert indexes == sorted(REQUIRED_INDEXES)
    with neo4j_driver.session() as session:
        session.run("DROP CONSTRAINT crm_deal_repair_run_id_unique IF EXISTS").consume()
    with pytest.raises(RuntimeError, match="crm_deal_repair_run_id_unique"):
        assert_crm_deal_repair_ledger_ready(cast(Neo4jClient, client))
    ensure_crm_deal_repair_ledger_ready(cast(Neo4jClient, client))
    with neo4j_driver.session() as session:
        session.run("DROP INDEX crm_deal_repair_run_status IF EXISTS").consume()
        session.run(
            "CREATE INDEX crm_deal_repair_run_status IF NOT EXISTS "
            "FOR (boundary:RepairExecutionBoundary) ON (boundary.artifact_id)"
        ).consume()
    with pytest.raises(RuntimeError, match="crm_deal_repair_run_status"):
        assert_crm_deal_repair_ledger_ready(cast(Neo4jClient, client))


def test_repair_index_inventory_excludes_constraint_backing_indexes() -> None:
    assert "type = 'RANGE'" in _REPAIR_RANGE_INDEX_INVENTORY
    assert "owningConstraint IS NULL" in _REPAIR_RANGE_INDEX_INVENTORY


def test_exact_and_concurrent_replay_use_persisted_evidence_without_domain_mutation(
    neo4j_driver: Driver,
) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    snapshot = _snapshot(repository)
    manifest = _manifest(snapshot)
    before = _domain_state(
        neo4j_driver,
        source_instance_id=_TEST_SOURCE_INSTANCE_ID,
        control_instance_id=_TEST_CONTROL_INSTANCE_ID,
    )
    first = repository.qualify(manifest, snapshot)
    replay = repository.qualify(manifest, snapshot)
    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent = list(pool.map(lambda _: repository.qualify(manifest, snapshot), range(2)))
    assert {first.run_id, replay.run_id, *(run.run_id for run in concurrent)} == {first.run_id}
    assert (
        repository.get_status(manifest.repair_id, _snapshot(repository)).admissibility
        == "admissible"
    )
    assert _repair_metadata_counts(neo4j_driver) == {
        "runs": 1,
        "boundaries": 1,
        "links": 1,
        "extra": 1,
    }
    assert (
        _domain_state(
            neo4j_driver,
            source_instance_id=_TEST_SOURCE_INSTANCE_ID,
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        )
        == before
    )


def test_concurrent_conflicting_rebind_leaves_no_orphan_repair_metadata(
    neo4j_driver: Driver,
) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    snapshot = _snapshot(repository)
    manifest = _manifest(snapshot)
    conflicting = _manifest(snapshot, approval_reference="other-approved-review")

    def _qualify(candidate: RepairExecutionBoundaryManifest) -> bool:
        try:
            repository.qualify(candidate, snapshot)
        except RuntimeError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_qualify, (manifest, conflicting)))
    assert sorted(results) == [False, True]
    assert _repair_metadata_counts(neo4j_driver) == {
        "runs": 1,
        "boundaries": 1,
        "links": 1,
        "extra": 1,
    }


def test_persisted_source_drift_is_read_only_status(neo4j_driver: Driver) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    snapshot = _snapshot(repository)
    manifest = _manifest(snapshot)
    repository.qualify(manifest, snapshot)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: $source_record_pk}) "
            "SET record.record_hash = 'sha256:changed-source-hash'",
            source_record_pk=_TEST_SOURCE_RECORD_PK,
        ).consume()
    status = repository.get_status(manifest.repair_id, _snapshot(repository))
    assert (status.admissibility, status.reason_code, status.execution_allowed) == (
        "drifted",
        "persisted_boundary_change",
        False,
    )
    assert _repair_metadata_counts(neo4j_driver) == {
        "runs": 1,
        "boundaries": 1,
        "links": 1,
        "extra": 1,
    }


def test_same_count_control_state_transition_is_persisted_boundary_drift(
    neo4j_driver: Driver,
) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:IngestionLogicalRun {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, logical_run_id: 'repair-test-logical', "
            "status: 'queued'})",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    snapshot = _snapshot(repository)
    manifest = _manifest(snapshot, repair_id="repair-control-state", artifact_id="c" * 32)
    repository.qualify(manifest, snapshot)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (logical:IngestionLogicalRun {control_instance_id: $control_instance_id, "
            "logical_run_id: 'repair-test-logical'}) SET logical.status = 'running'",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    observed = _snapshot(repository)
    assert observed.control_digest != snapshot.control_digest
    assert repository.get_status(manifest.repair_id, observed).admissibility == "drifted"


def test_stage_history_stable_ids_and_backfill_provenance_are_control_boundary_evidence(
    neo4j_driver: Driver,
) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:StageHistoryUnit {control_instance_id: $control_instance_id, "
            "unit_id: 'repair-stage-unit-a', state: 'pending'}) "
            "CREATE (:StageHistoryUnit {control_instance_id: $control_instance_id, "
            "unit_id: 'repair-stage-unit-b', state: 'pending'})",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    stage_snapshot = _snapshot(repository)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (unit:StageHistoryUnit {control_instance_id: $control_instance_id, "
            "unit_id: 'repair-stage-unit-b'}) DELETE unit",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    assert _snapshot(repository).control_digest != stage_snapshot.control_digest

    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:BitrixBackfillGeneration {control_instance_id: $control_instance_id, "
            "generation_id: 'repair-backfill-a', "
            "repository_sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "image_digest: 'sha256:image-a', configuration_digest: 'sha256:config-a', "
            "source_contract_uuid: '12345678-1234-5678-9234-567812345678', "
            "source_boundary: 'boundary-a'})",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    backfill_snapshot = _snapshot(repository)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (generation:BitrixBackfillGeneration "
            "{control_instance_id: $control_instance_id, generation_id: 'repair-backfill-a'}) "
            "SET generation.source_boundary = 'boundary-b'",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    assert _snapshot(repository).control_digest != backfill_snapshot.control_digest


def test_control_relationship_multiplicity_and_properties_are_boundary_evidence(
    neo4j_driver: Driver,
) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (unit:StageHistoryUnit {control_instance_id: $control_instance_id, "
            "unit_id: 'repair-relationship-unit'}) "
            "CREATE (occurrence:StageHistoryOccurrence {control_instance_id: $control_instance_id, "
            "occurrence_id: 'repair-relationship-occurrence'}) "
            "CREATE (unit)-[:CONTAINS_STAGE_HISTORY_OCCURRENCE {attempt: 1}]->(occurrence)",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    baseline = _snapshot(repository)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (unit:StageHistoryUnit {unit_id: 'repair-relationship-unit'}) "
            "MATCH (occurrence:StageHistoryOccurrence "
            "{occurrence_id: 'repair-relationship-occurrence'}) "
            "CREATE (unit)-[:CONTAINS_STAGE_HISTORY_OCCURRENCE {attempt: 1}]->(occurrence)",
        ).consume()
    with_duplicate = _snapshot(repository)
    assert with_duplicate.control_digest != baseline.control_digest
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (:StageHistoryUnit {unit_id: 'repair-relationship-unit'})"
            "-[relationship:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(:StageHistoryOccurrence {"
            "occurrence_id: 'repair-relationship-occurrence'}) "
            "WITH relationship LIMIT 1 DELETE relationship"
        ).consume()
    after_removal = _snapshot(repository)
    assert after_removal.control_digest != with_duplicate.control_digest
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (unit:StageHistoryUnit {unit_id: 'repair-relationship-unit'}) "
            "MATCH (occurrence:StageHistoryOccurrence "
            "{occurrence_id: 'repair-relationship-occurrence'}) "
            "CREATE (unit)-[:CONTAINS_STAGE_HISTORY_OCCURRENCE {attempt: 2}]->(occurrence)",
        ).consume()
    assert _snapshot(repository).control_digest != after_removal.control_digest


def test_unrelated_part_of_run_source_record_is_not_control_boundary_evidence(
    neo4j_driver: Driver,
) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (run:IngestRun {control_instance_id: $control_instance_id, "
            "ingest_run_id: 'repair-topology-run', status: 'queued'}) "
            "CREATE (logical:IngestionLogicalRun {control_instance_id: $control_instance_id, "
            "logical_run_id: 'repair-topology-logical'})"
            "-[:HAS_ATTEMPT]->(run) "
            "CREATE (unit:StageHistoryUnit {control_instance_id: $control_instance_id, "
            "unit_id: 'repair-topology-unit'}) "
            "CREATE (logical)-[:HAS_STAGE_HISTORY_UNIT {generation: 1}]->(unit)",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    baseline = _snapshot(repository)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (run:IngestRun {ingest_run_id: 'repair-topology-run'}) "
            "CREATE (record:SourceRecord {source_record_pk: 'repair-test-unrelated-record', "
            "raw_payload: 'sensitive-domain-payload', normalized_payload: 'domain-value'})"
            "-[:PART_OF_RUN]->(run)"
        ).consume()
    with_unrelated_record = _snapshot(repository)
    assert with_unrelated_record.control_digest == baseline.control_digest
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: 'repair-test-unrelated-record'}) "
            "SET record.raw_payload = 'changed-sensitive-domain-payload'"
        ).consume()
    assert _snapshot(repository).control_digest == baseline.control_digest
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (logical:IngestionLogicalRun {logical_run_id: 'repair-topology-logical'})"
            "-[relationship:HAS_STAGE_HISTORY_UNIT]->"
            "(:StageHistoryUnit {unit_id: 'repair-topology-unit'}) "
            "SET relationship.generation = 2"
        ).consume()
    assert _snapshot(repository).control_digest != baseline.control_digest


def test_stale_run_association_identity_drift_is_persisted_boundary_drift(
    neo4j_driver: Driver,
) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    stale_run_id = "e5deb1d6-7333-4660-be4f-c44fcf5af686"
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (source:SourceSystem {source_key: 'bitrix_chat'}) "
            "CREATE (run:IngestRun {ingest_run_id: $stale_run_id, source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, status: 'failed'})"
            "-[:FROM_SOURCE]->(source) "
            "CREATE (logical:IngestionLogicalRun {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, logical_run_id: 'repair-test-stale-a', "
            "status: 'failed'})-[:HAS_ATTEMPT]->(run) "
            "CREATE (checkpoint:IngestionCheckpoint {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, checkpoint_key: 'repair-test-checkpoint', "
            "logical_run_id: 'repair-test-stale-a', phase: 'repair'})"
            "-[:CHECKPOINT_FOR]->(logical) "
            "CREATE (checkpoint)-[:PRODUCED_BY]->(run)",
            stale_run_id=stale_run_id,
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    snapshot = _snapshot(repository)
    manifest = _manifest(snapshot, repair_id="repair-stale-association", artifact_id="d" * 32)
    repository.qualify(manifest, snapshot)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (checkpoint:IngestionCheckpoint {control_instance_id: $control_instance_id, "
            "checkpoint_key: 'repair-test-checkpoint'})-[relation:CHECKPOINT_FOR]->() "
            "DELETE relation",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    observed = _snapshot(repository)
    assert observed.stale_run_evidence_digest != snapshot.stale_run_evidence_digest
    assert repository.get_status(manifest.repair_id, observed).admissibility == "drifted"
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (checkpoint:IngestionCheckpoint {control_instance_id: $control_instance_id, "
            "checkpoint_key: 'repair-test-checkpoint'}), "
            "(logical:IngestionLogicalRun {control_instance_id: $control_instance_id, "
            "logical_run_id: 'repair-test-stale-a'}) "
            "CREATE (checkpoint)-[:CHECKPOINT_FOR]->(logical)",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    assert _snapshot(repository) == snapshot
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (checkpoint:IngestionCheckpoint {control_instance_id: $control_instance_id, "
            "checkpoint_key: 'repair-test-checkpoint'}), "
            "(logical:IngestionLogicalRun {control_instance_id: $control_instance_id, "
            "logical_run_id: 'repair-test-stale-a'}) "
            "CREATE (checkpoint)-[:CHECKPOINT_FOR]->(logical)",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    with_duplicate_checkpoint = _snapshot(repository)
    assert with_duplicate_checkpoint.stale_run_evidence_digest != snapshot.stale_run_evidence_digest
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (:IngestionCheckpoint {control_instance_id: $control_instance_id, "
            "checkpoint_key: 'repair-test-checkpoint'})-[relation:CHECKPOINT_FOR]->() "
            "WITH relation LIMIT 1 DELETE relation",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    after_checkpoint_removal = _snapshot(repository)
    assert (
        after_checkpoint_removal.stale_run_evidence_digest
        != with_duplicate_checkpoint.stale_run_evidence_digest
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (checkpoint:IngestionCheckpoint {control_instance_id: $control_instance_id, "
            "checkpoint_key: 'repair-test-checkpoint'})-[relation:PRODUCED_BY]->(run:IngestRun {"
            "ingest_run_id: $stale_run_id}) DELETE relation",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
            stale_run_id=stale_run_id,
        ).consume()
    observed = _snapshot(repository)
    assert observed.stale_run_evidence_digest != snapshot.stale_run_evidence_digest
    assert repository.get_status(manifest.repair_id, observed).admissibility == "drifted"


def test_absent_stale_run_is_valid_evidence_and_later_appearance_drifts(
    neo4j_driver: Driver,
) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    snapshot = _snapshot(repository)
    manifest = _manifest(snapshot, repair_id="repair-absent-stale", artifact_id="e" * 32)
    repository.qualify(manifest, snapshot)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:IngestRun {ingest_run_id: $stale_run_id, source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, status: 'failed'})",
            stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    observed = _snapshot(repository)
    assert observed.stale_run_evidence_digest != snapshot.stale_run_evidence_digest
    assert repository.get_status(manifest.repair_id, observed).admissibility == "drifted"


def test_qualification_revalidates_full_inventory_before_admission(neo4j_driver: Driver) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    snapshot = _snapshot(repository)
    manifest = _manifest(snapshot, repair_id="repair-preflight-race", artifact_id="c" * 32)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: $source_record_pk}) "
            "SET record.raw_payload = "
            '\'{"crm_deal_identity_policy_version":"crm_deal_identity_v2"}\'',
            source_record_pk=_TEST_SOURCE_RECORD_PK,
        ).consume()
    with pytest.raises(ExpectedRepairBoundaryDriftError) as drift:
        repository.qualify(manifest, snapshot)
    assert drift.value.reason == "persisted_boundary_change"
    assert _repair_metadata_counts(neo4j_driver) == {
        "runs": 0,
        "boundaries": 0,
        "links": 0,
        "extra": 0,
    }


def test_legacy_default_and_distinct_source_control_ids_are_exact(neo4j_driver: Driver) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    distinct_snapshot = _snapshot(repository)
    assert (distinct_snapshot.source_instance_id, distinct_snapshot.control_instance_id) == (
        _TEST_SOURCE_INSTANCE_ID,
        _TEST_CONTROL_INSTANCE_ID,
    )
    distinct_run = repository.qualify(
        _manifest(distinct_snapshot, repair_id="repair-distinct"),
        distinct_snapshot,
    )
    assert (distinct_run.source_instance_id, distinct_run.control_instance_id) == (
        _TEST_SOURCE_INSTANCE_ID,
        _TEST_CONTROL_INSTANCE_ID,
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: $source_record_pk}) "
            "DETACH DELETE record",
            source_record_pk=_TEST_SOURCE_RECORD_PK,
        ).consume()
        session.run(
            "MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) DETACH DELETE dispatch",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
        session.run(
            "MATCH (binding:BitrixExecutionSourceBinding {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) DETACH DELETE binding",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
        session.run(
            "MATCH (instance:BitrixSourceInstance {source_key: 'bitrix_chat', "
            "source_instance_id: $source_instance_id}) DETACH DELETE instance",
            source_instance_id=_TEST_SOURCE_INSTANCE_ID,
        ).consume()
        session.run(
            "MATCH (instance:BitrixSourceInstance {source_key: 'bitrix_chat', "
            "source_instance_id: $source_instance_id}) DETACH DELETE instance",
            source_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    _persist_evidence(
        neo4j_driver,
        source_instance_id="legacy-default",
        control_instance_id="legacy-default",
        source_record_pk="repair-test-legacy-crm-deal-001",
    )
    legacy_snapshot = _snapshot(
        repository,
        source_instance_id="legacy-default",
        control_instance_id="legacy-default",
        source_record_pk="repair-test-legacy-crm-deal-001",
    )
    legacy_manifest = _manifest(
        legacy_snapshot,
        repair_id="repair-legacy-default",
        artifact_id="b" * 32,
        source_instance_id="legacy-default",
        control_instance_id="legacy-default",
    )
    run = repository.qualify(legacy_manifest, legacy_snapshot)
    assert (run.source_instance_id, run.control_instance_id) == ("legacy-default", "legacy-default")


def test_missing_binding_and_control_evidence_are_typed_read_only_drift(
    neo4j_driver: Driver,
) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    snapshot = _snapshot(repository)
    manifest = _manifest(snapshot)
    repository.qualify(manifest, snapshot)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (binding:BitrixExecutionSourceBinding {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) DETACH DELETE binding",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    with pytest.raises(ExpectedRepairBoundaryDriftError) as missing_binding:
        _snapshot(repository)
    assert missing_binding.value.reason == "missing_binding"
    status = repository.get_status(manifest.repair_id, drift_reason=missing_binding.value.reason)
    assert (status.admissibility, status.reason_code, status.execution_allowed) == (
        "drifted",
        "missing_binding",
        False,
    )


def test_legacy_qualified_manifest_without_materialized_rollback_authority_replays(
    neo4j_driver: Driver,
) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    snapshot = _snapshot(repository)
    manifest = _manifest(snapshot, repair_id="repair-legacy-rollback-authority")
    initial = repository.qualify(manifest, snapshot)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (run:CrmDealRepairRun {run_id: $run_id})-[qualification:QUALIFIED_WITH]->
              (boundary:RepairExecutionBoundary)
            REMOVE run.rollback_authority_reference, run.rollback_authority_policy,
              boundary.rollback_authority_reference, boundary.rollback_authority_policy
            """,
            run_id=initial.run_id,
        ).consume()

    replay = repository.qualify(manifest, snapshot)
    assert replay == initial
    instances = BitrixSourceInstanceRepository(cast(Neo4jClient, _client(neo4j_driver)))
    instances.admit(
        source_instance_id=_TEST_SOURCE_INSTANCE_ID,
        control_instance_id=_TEST_CONTROL_INSTANCE_ID,
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) DETACH DELETE dispatch",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    with pytest.raises(ExpectedRepairBoundaryDriftError) as missing_control:
        _snapshot(repository)
    assert missing_control.value.reason == "missing_control_evidence"
    status = repository.get_status(manifest.repair_id, drift_reason=missing_control.value.reason)
    assert (status.admissibility, status.reason_code, status.execution_allowed) == (
        "drifted",
        "missing_control_evidence",
        False,
    )


def test_distinct_control_registration_and_ownership_are_exact(neo4j_driver: Driver) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (control:BitrixSourceInstance {source_key: 'bitrix_chat', "
            "source_instance_id: $control_instance_id}) SET control.status = 'disabled'",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    with pytest.raises(ExpectedRepairBoundaryDriftError) as disabled:
        _snapshot(repository)
    assert disabled.value.reason == "source_instance_disabled"

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (control:BitrixSourceInstance {source_key: 'bitrix_chat', "
            "source_instance_id: $control_instance_id}) SET control.status = 'active'",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
        session.run(
            "MATCH (owner:BitrixSourceInstance {source_key: 'bitrix_chat', "
            "source_instance_id: $source_instance_id}), "
            "(binding:BitrixExecutionSourceBinding {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) "
            "CREATE (owner)-[:OWNS_BITRIX_CONTROL]->(binding)",
            source_instance_id=_TEST_SOURCE_INSTANCE_ID,
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    with pytest.raises(ExpectedRepairBoundaryDriftError) as duplicate_owner:
        _snapshot(repository)
    assert duplicate_owner.value.reason == "binding_mismatch"

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (owner:BitrixSourceInstance {source_key: 'bitrix_chat', "
            "source_instance_id: $source_instance_id})-[ownership:OWNS_BITRIX_CONTROL]->"
            "(binding:BitrixExecutionSourceBinding {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) DELETE ownership",
            source_instance_id=_TEST_SOURCE_INSTANCE_ID,
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
        session.run(
            "MATCH (source:SourceSystem {source_key: 'bitrix_chat'}), "
            "(binding:BitrixExecutionSourceBinding {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) "
            "CREATE (other:BitrixSourceInstance {source_key: 'bitrix_chat', "
            "source_instance_id: 'repair-test-other', status: 'active'})-[:INSTANCE_OF]->(source) "
            "CREATE (other)-[:OWNS_BITRIX_CONTROL]->(binding)",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    with pytest.raises(ExpectedRepairBoundaryDriftError) as redirected:
        _snapshot(repository)
    assert redirected.value.reason == "binding_mismatch"


def test_deleted_distinct_control_registration_is_read_only_drift(neo4j_driver: Driver) -> None:
    repository = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (control:BitrixSourceInstance {source_key: 'bitrix_chat', "
            "source_instance_id: $control_instance_id}) DETACH DELETE control",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    with pytest.raises(ExpectedRepairBoundaryDriftError) as deleted:
        _snapshot(repository)
    assert deleted.value.reason == "source_instance_disabled"


# #310 disposable real-Neo4j acceptance: these assertions exercise only the
# repair/control metadata labels plus the dispatch fence. They never mutate CRM
# SourceRecords or relationships.
def _qualified_control_repository(
    driver: Driver, *, repair_id: str
) -> tuple[CrmDealRepairLedgerRepository, CrmDealRepairControlRepository, RepairQualificationRun]:
    ledger = _repository(driver)
    _persist_evidence(driver)
    snapshot = _snapshot(ledger)
    run = ledger.qualify(_manifest(snapshot, repair_id=repair_id), snapshot)
    return ledger, CrmDealRepairControlRepository(cast(Neo4jClient, _client(driver))), run


def test_310_dispatch_cas_concurrency_and_no_crm_mutation(neo4j_driver: Driver) -> None:
    _, control, run = _qualified_control_repository(neo4j_driver, repair_id="repair-310-cas")
    before = _crm_domain_snapshot(neo4j_driver)

    def claim(owner: str) -> bool:
        try:
            control.claim(
                RepairControlRequest("repair-310-cas", run.run_id, owner, owner + "-token", 0),
                boundary_digest=run.boundary_digest,
                control_instance_id=run.control_instance_id,
            )
        except RuntimeError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ("owner-a", "owner-b")))
    assert sorted(outcomes) == [False, True]
    assert _crm_domain_snapshot(neo4j_driver) == before


def _crm_domain_snapshot(driver: Driver) -> tuple[str, ...]:
    """Capture all fixture CRM nodes and incident facts, excluding control metadata."""
    with driver.session() as session:
        rows = session.run(
            "MATCH path=(record:SourceRecord {source_record_pk: $pk})-[relationship*0..1]-(other) "
            "RETURN labels(record) AS record_labels, properties(record) AS record_properties, "
            "[item IN relationships(path) | {type: type(item), properties: properties(item)}] "
            "AS relationships, labels(other) AS other_labels, "
            "properties(other) AS other_properties",
            pk=_TEST_SOURCE_RECORD_PK,
        ).data()
    return _canonical_domain_rows(rows)


def test_310_same_owner_renewal_and_exact_replay_reject_foreign_expired_owner(
    neo4j_driver: Driver,
) -> None:
    _, control, run = _qualified_control_repository(neo4j_driver, repair_id="repair-310-owner")
    first = control.claim(
        RepairControlRequest("repair-310-owner", run.run_id, "owner-a", "token-a", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    replay = control.claim(
        RepairControlRequest("repair-310-owner", run.run_id, "owner-a", "token-a", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    assert replay == first
    renewed = control.claim(
        RepairControlRequest("repair-310-owner", run.run_id, "owner-a", "token-a", first.revision),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    assert (first.owner_id, first.token_digest, first.revision, renewed.revision) == (
        "owner-a",
        control_token_digest("token-a"),
        1,
        2,
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) "
            "SET dispatch.repair_expires_at = datetime() - duration('PT1S')",
            control_instance_id=run.control_instance_id,
        ).consume()
    with pytest.raises(RuntimeError, match="compare-and-set"):
        control.claim(
            RepairControlRequest(
                "repair-310-owner", run.run_id, "owner-b", "token-b", renewed.revision
            ),
            boundary_digest=run.boundary_digest,
            control_instance_id=run.control_instance_id,
        )
    with neo4j_driver.session() as session:
        owner = session.run(
            "MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) "
            "RETURN dispatch.repair_owner_id AS owner, "
            "dispatch.repair_token_digest AS token_digest, dispatch.repair_revision AS revision",
            control_instance_id=run.control_instance_id,
        ).single(strict=True)
    assert dict(owner) == {
        "owner": "owner-a",
        "token_digest": control_token_digest("token-a"),
        "revision": 2,
    }


def test_310_completed_claim_replays_only_the_original_claim_identity(neo4j_driver: Driver) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-complete-claim"
    )
    _seed_quiesced_allocation_control(neo4j_driver, run)

    with neo4j_driver.session() as session:
        before = session.run(
            "MATCH (control:CrmDealRepairControl {run_id: $run_id}) "
            "MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) "
            "RETURN properties(control) AS control, properties(dispatch) AS dispatch",
            run_id=run.run_id,
            control_instance_id=run.control_instance_id,
        ).single(strict=True)
    replay = control.claim(
        RepairControlRequest("repair-310-complete-claim", run.run_id, "owner", "token", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    with neo4j_driver.session() as session:
        after = session.run(
            "MATCH (control:CrmDealRepairControl {run_id: $run_id}) "
            "MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) "
            "RETURN properties(control) AS control, properties(dispatch) AS dispatch",
            run_id=run.run_id,
            control_instance_id=run.control_instance_id,
        ).single(strict=True)
    assert dict(after) == dict(before)
    assert (replay.state, replay.revision) == ("quiesced", 1)
    with pytest.raises(RuntimeError, match="compare-and-set"):
        control.claim(
            RepairControlRequest("repair-310-complete-claim", run.run_id, "owner", "token", 1),
            boundary_digest=run.boundary_digest,
            control_instance_id=run.control_instance_id,
        )
    with pytest.raises(RuntimeError, match="compare-and-set"):
        control.claim(
            RepairControlRequest(
                "repair-310-complete-claim", run.run_id, "other", "other-token", 0
            ),
            boundary_digest=run.boundary_digest,
            control_instance_id=run.control_instance_id,
        )
    with pytest.raises(RuntimeError, match="compare-and-set"):
        control.claim(
            RepairControlRequest("repair-310-complete-claim", run.run_id, "owner", "token", 0),
            boundary_digest="sha256:" + "f" * 64,
            control_instance_id=run.control_instance_id,
        )


def test_310_unsettled_publication_blocks_claim_and_is_fail_closed(neo4j_driver: Driver) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-publication"
    )
    reservation = control.prepare_publication(run.control_instance_id, "generation-310")
    assert reservation.state == "preparing"
    with pytest.raises(RuntimeError, match="compare-and-set"):
        control.claim(
            RepairControlRequest("repair-310-publication", run.run_id, "owner", "token", 0),
            boundary_digest=run.boundary_digest,
            control_instance_id=run.control_instance_id,
        )
    # A preparation reservation cannot be reinterpreted as a successful publish.
    assert control.prepare_publication(run.control_instance_id, "generation-310") == reservation


def test_310_publication_reservation_and_repair_claim_race_are_mutually_exclusive(
    neo4j_driver: Driver,
) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-publication-race"
    )

    def reserve() -> bool:
        try:
            control.prepare_publication(run.control_instance_id, "legacy:scheduled:race")
        except RuntimeError:
            return False
        return True

    def claim() -> bool:
        try:
            control.claim(
                RepairControlRequest(
                    "repair-310-publication-race", run.run_id, "owner", "token", 0
                ),
                boundary_digest=run.boundary_digest,
                control_instance_id=run.control_instance_id,
            )
        except RuntimeError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda operation: operation(), (reserve, claim)))
    assert sorted(outcomes) == [False, True]


def test_310_schema_parity_includes_control_metadata(neo4j_driver: Driver) -> None:
    _repository(neo4j_driver)
    with neo4j_driver.session() as session:
        constraints = set(
            session.run(
                "SHOW CONSTRAINTS YIELD name WHERE name STARTS WITH 'crm_deal_repair_' RETURN name"
            ).value("name")
        )
    assert {
        "crm_deal_repair_control_run_unique",
        "crm_deal_repair_publication_reservation_unique",
        "crm_deal_repair_allocation_completion_unique",
        "crm_deal_repair_unit_unique",
    } <= constraints
    runtime_names = REQUIRED_CONSTRAINTS | REQUIRED_INDEXES
    init_schema = Path("infra/neo4j/init.cypher").read_text(encoding="utf-8")
    assert all(name in init_schema for name in runtime_names)


def _absence_evidence(
    control: CrmDealRepairControlRepository,
    run: RepairQualificationRun,
    *,
    owner: str,
    token_digest: str,
    revision: int,
    topology_digest: str,
    captured_topology_digest: str | None = None,
) -> TaskAbsenceEvidence:
    from src.crm_deal_identity_repair.task_inspection import (
        BrokerInspector,
        WorkerInspector,
        collect_absence_evidence,
    )

    captured_tasks = control.captured_task_identities(
        run_id=run.run_id,
        control_instance_id=run.control_instance_id,
        topology_digest=captured_topology_digest or topology_digest,
    )

    class _Workers:
        def inspect(
            self, timeout_seconds: int
        ) -> dict[str, dict[str, tuple[dict[str, object], ...]]]:
            assert timeout_seconds == 10
            return {"worker-a": {"active": (), "reserved": (), "scheduled": ()}}

    class _Broker:
        def inspect(self, selectors: tuple[str, ...]) -> BrokerInspection:
            assert selectors == tuple(sorted(identity.selector() for identity in captured_tasks))
            priority_keys = [
                "ingestion",
                *(f"ingestion\x06\x16{priority}" for priority in range(1, 10)),
            ]
            return BrokerInspection(
                {
                    "ready_priority_keys": priority_keys,
                    "unacked_hash": "unacked",
                    "unacked_index": "unacked_index",
                    "unacked_wrapper": "kombu-redis-json-v1",
                },
                {"ready": (), "unacked": ()},
            )

    return collect_absence_evidence(
        worker=cast(WorkerInspector, _Workers()),
        broker=cast(BrokerInspector, _Broker()),
        run_id=run.run_id,
        captured_tasks=captured_tasks,
        boundary_digest=run.boundary_digest,
        owner_id=owner,
        token_digest=control_token_digest(token_digest),
        dispatch_revision=revision,
        topology_digest=topology_digest,
        expected_workers=("worker-a",),
        timeout_seconds=10,
        max_age_seconds=60,
        key_id="key-1",
        secret=b"secret",
        now=datetime.now(UTC),
    )


def _seed_complete_task_topology(driver: Driver, control_instance_id: str) -> None:
    """Seed one complete active logical/attempt/stream/fence capture for #310."""
    with driver.session() as session:
        session.run(
            "CREATE (attempt:IngestRun {ingest_run_id: 'repair-attempt', "
            "control_instance_id: $control_instance_id, generation: 1, status: 'running'}) "
            "CREATE (logical:IngestionLogicalRun {source_key: 'bitrix_chat', "
            "logical_run_id: 'repair-logical', control_instance_id: $control_instance_id, "
            "status: 'running'})"
            "-[:HAS_ATTEMPT]->(attempt) "
            "CREATE (stream:BitrixIngestionStream {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, stream_key: 'crm_deals', "
            "logical_run_id: 'repair-logical', ingest_run_id: 'repair-attempt', "
            "attempt_generation: 1, stream_generation: 1, fencing_token: 'repair-fence', "
            "status: 'active'}) "
            "CREATE (generation:BitrixBackfillGeneration {control_instance_id: "
            "$control_instance_id, generation_id: 'repair-generation'})"
            "-[:HAS_STREAM {stream_generation: 1, "
            "fencing_token: 'repair-fence'}]->(stream) "
            "CREATE (generation)-[:HAS_LOGICAL_RUN {stream_key: 'crm_deals'}]->(logical)",
            control_instance_id=control_instance_id,
        ).consume()


def test_310_zero_capture_quiescence_fails_closed(neo4j_driver: Driver) -> None:
    _, control, run = _qualified_control_repository(neo4j_driver, repair_id="repair-310-empty")
    before = _crm_domain_snapshot(neo4j_driver)
    lease = control.claim(
        RepairControlRequest("repair-310-empty", run.run_id, "owner", "token", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    topology_digest = control.request_stop_topology(
        control_instance_id=run.control_instance_id,
        run_id=run.run_id,
        owner_id="owner",
        stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
    )

    with pytest.raises(RuntimeError, match="task topology capture is incomplete"):
        _absence_evidence(
            control,
            run,
            owner="owner",
            token_digest="token",
            revision=lease.revision,
            topology_digest=topology_digest,
        )
    assert _crm_domain_snapshot(neo4j_driver) == before


def test_310_unadmitted_logical_topology_is_not_silently_omitted(
    neo4j_driver: Driver,
) -> None:
    """A queued affected logical run without an attempt must fail the topology proof."""
    _, control, run = _qualified_control_repository(neo4j_driver, repair_id="repair-310-unadmitted")
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (logical:IngestionLogicalRun {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, logical_run_id: 'unadmitted-logical', "
            "status: 'queued'}) "
            "CREATE (:BitrixBackfillGeneration {control_instance_id: $control_instance_id, "
            "generation_id: 'unadmitted-generation'})"
            "-[:HAS_LOGICAL_RUN {stream_key: 'crm_deals'}]->(logical)",
            control_instance_id=run.control_instance_id,
        ).consume()

    lease = control.claim(
        RepairControlRequest("repair-310-unadmitted", run.run_id, "owner", "token", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    topology_digest = control.request_stop_topology(
        control_instance_id=run.control_instance_id,
        run_id=run.run_id,
        owner_id="owner",
        stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
    )
    assert lease.state == "quiescing"

    with pytest.raises(RuntimeError, match="task topology capture is incomplete"):
        control.captured_task_identities(
            run_id=run.run_id,
            control_instance_id=run.control_instance_id,
            topology_digest=topology_digest,
        )


def test_310_duplicate_attempt_relationships_produce_one_capture(neo4j_driver: Driver) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-duplicate-attempt"
    )
    _seed_complete_task_topology(neo4j_driver, run.control_instance_id)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (logical:IngestionLogicalRun {logical_run_id: 'repair-logical'})"
            "-[:HAS_ATTEMPT]->(attempt:IngestRun {ingest_run_id: 'repair-attempt'}) "
            "CREATE (logical)-[:ACTIVE_ATTEMPT]->(attempt)"
        ).consume()
    lease = control.claim(
        RepairControlRequest("repair-310-duplicate-attempt", run.run_id, "owner", "token", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    topology = control.request_stop_topology(
        control_instance_id=run.control_instance_id,
        run_id=run.run_id,
        owner_id="owner",
        stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
    )
    assert (
        len(
            control.captured_task_identities(
                run_id=run.run_id,
                control_instance_id=run.control_instance_id,
                topology_digest=topology,
            )
        )
        == 1
    )
    evidence = _absence_evidence(
        control,
        run,
        owner="owner",
        token_digest="token",
        revision=lease.revision,
        topology_digest=topology,
    )
    assert (
        control.complete_quiescence(
            RepairControlRequest(
                "repair-310-duplicate-attempt", run.run_id, "owner", "token", lease.revision
            ),
            boundary_digest=run.boundary_digest,
            control_instance_id=run.control_instance_id,
            topology_digest=topology,
            evidence=evidence,
            proof_secret=b"secret",
            stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
        ).state
        == "quiesced"
    )


def test_310_ambiguous_current_attempts_fail_closed(neo4j_driver: Driver) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-ambiguous-attempt"
    )
    _seed_complete_task_topology(neo4j_driver, run.control_instance_id)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (logical:IngestionLogicalRun {logical_run_id: 'repair-logical'}) "
            "CREATE (other:IngestRun {ingest_run_id: 'repair-other-attempt', "
            "control_instance_id: $control_instance_id, generation: 2, status: 'running'}) "
            "CREATE (logical)-[:ACTIVE_ATTEMPT]->(other)",
            control_instance_id=run.control_instance_id,
        ).consume()
    control.claim(
        RepairControlRequest("repair-310-ambiguous-attempt", run.run_id, "owner", "token", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    topology = control.request_stop_topology(
        control_instance_id=run.control_instance_id,
        run_id=run.run_id,
        owner_id="owner",
        stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
    )
    with pytest.raises(RuntimeError, match="task topology capture is incomplete"):
        control.captured_task_identities(
            run_id=run.run_id,
            control_instance_id=run.control_instance_id,
            topology_digest=topology,
        )


def test_310_paused_checkpoint_unit_is_stopped_and_quiesced_without_losing_checkpoint(
    neo4j_driver: Driver,
) -> None:
    _, control, run = _qualified_control_repository(neo4j_driver, repair_id="repair-310-paused")
    _seed_complete_task_topology(neo4j_driver, run.control_instance_id)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (logical:IngestionLogicalRun {logical_run_id: 'repair-logical'}) "
            "SET logical.status = 'paused_with_checkpoint' "
            "CREATE (:IngestionCheckpoint {control_instance_id: $control_instance_id, "
            "logical_run_id: 'repair-logical', checkpoint_id: 'paused-checkpoint'})",
            control_instance_id=run.control_instance_id,
        ).consume()
    lease = control.claim(
        RepairControlRequest("repair-310-paused", run.run_id, "owner", "token", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    topology = control.request_stop_topology(
        control_instance_id=run.control_instance_id,
        run_id=run.run_id,
        owner_id="owner",
        stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
    )
    evidence = _absence_evidence(
        control,
        run,
        owner="owner",
        token_digest="token",
        revision=lease.revision,
        topology_digest=topology,
    )
    completed = control.complete_quiescence(
        RepairControlRequest("repair-310-paused", run.run_id, "owner", "token", lease.revision),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
        topology_digest=topology,
        evidence=evidence,
        proof_secret=b"secret",
        stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
    )
    assert completed.state == "quiesced"
    with neo4j_driver.session() as session:
        checkpoint_count = session.run(
            "MATCH (:IngestionCheckpoint {control_instance_id: $control_instance_id, "
            "logical_run_id: 'repair-logical', checkpoint_id: 'paused-checkpoint'}) "
            "RETURN count(*) AS count",
            control_instance_id=run.control_instance_id,
        ).single(strict=True)["count"]
    assert checkpoint_count == 1


def test_310_final_commit_rejects_boundary_drift_and_preserves_crm_domain(
    neo4j_driver: Driver,
) -> None:
    _, control, run = _qualified_control_repository(neo4j_driver, repair_id="repair-310-boundary")
    _seed_complete_task_topology(neo4j_driver, run.control_instance_id)
    before = _crm_domain_snapshot(neo4j_driver)
    lease = control.claim(
        RepairControlRequest("repair-310-boundary", run.run_id, "owner", "token", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    topology = control.request_stop_topology(
        control_instance_id=run.control_instance_id,
        run_id=run.run_id,
        owner_id="owner",
        stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
    )
    evidence = _absence_evidence(
        control,
        run,
        owner="owner",
        token_digest="token",
        revision=lease.revision,
        topology_digest=topology,
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: $pk}) "
            "SET record.record_hash = 'sha256:boundary-drift'",
            pk=_TEST_SOURCE_RECORD_PK,
        ).consume()
    with pytest.raises(RuntimeError, match="boundary"):
        control.complete_quiescence(
            RepairControlRequest(
                "repair-310-boundary", run.run_id, "owner", "token", lease.revision
            ),
            boundary_digest=run.boundary_digest,
            control_instance_id=run.control_instance_id,
            topology_digest=topology,
            evidence=evidence,
            proof_secret=b"secret",
            stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
        )
    assert _crm_domain_snapshot(neo4j_driver) != before
    with neo4j_driver.session() as session:
        state = session.run(
            "MATCH (control:CrmDealRepairControl {run_id: $run_id}) RETURN control.state AS state",
            run_id=run.run_id,
        ).single(strict=True)["state"]
    assert state == "quiescing"


def test_310_final_commit_rejects_valid_evidence_for_a_different_topology(
    neo4j_driver: Driver,
) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-proof-topology"
    )
    _seed_complete_task_topology(neo4j_driver, run.control_instance_id)
    lease = control.claim(
        RepairControlRequest("repair-310-proof-topology", run.run_id, "owner", "token", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    topology = control.request_stop_topology(
        control_instance_id=run.control_instance_id,
        run_id=run.run_id,
        owner_id="owner",
        stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
    )
    other_topology = "sha256:" + "b" * 64
    evidence = _absence_evidence(
        control,
        run,
        owner="owner",
        token_digest="token",
        revision=lease.revision,
        topology_digest=other_topology,
        captured_topology_digest=topology,
    )

    with pytest.raises(RuntimeError, match="topology does not match"):
        control.complete_quiescence(
            RepairControlRequest(
                "repair-310-proof-topology", run.run_id, "owner", "token", lease.revision
            ),
            boundary_digest=run.boundary_digest,
            control_instance_id=run.control_instance_id,
            topology_digest=topology,
            evidence=evidence,
            proof_secret=b"secret",
            stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
        )

    with neo4j_driver.session() as session:
        state = session.run(
            "MATCH (control:CrmDealRepairControl {run_id: $run_id}) RETURN control.state AS state",
            run_id=run.run_id,
        ).single(strict=True)["state"]
    assert state == "quiescing"


def test_310_stale_orphan_and_cross_control_ambiguity_are_exact(neo4j_driver: Driver) -> None:
    _, control, run = _qualified_control_repository(neo4j_driver, repair_id="repair-310-stale")
    _seed_complete_task_topology(neo4j_driver, run.control_instance_id)
    lease = control.claim(
        RepairControlRequest("repair-310-stale", run.run_id, "owner", "token", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    stale_run_id = "e5deb1d6-7333-4660-be4f-c44fcf5af686"
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:IngestRun {ingest_run_id: $run_id, status: 'queued'})", run_id=stale_run_id
        ).consume()
    topology = control.request_stop_topology(
        control_instance_id=run.control_instance_id,
        run_id=run.run_id,
        owner_id="owner",
        stale_run_id=stale_run_id,
    )
    completed = control.complete_quiescence(
        RepairControlRequest("repair-310-stale", run.run_id, "owner", "token", lease.revision),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
        topology_digest=topology,
        evidence=_absence_evidence(
            control,
            run,
            owner="owner",
            token_digest="token",
            revision=lease.revision,
            topology_digest=topology,
        ),
        proof_secret=b"secret",
        stale_run_id=stale_run_id,
    )
    assert completed.state == "quiesced"
    with neo4j_driver.session() as session:
        stale = session.run(
            "MATCH (run:IngestRun {ingest_run_id: $run_id}) "
            "RETURN run.status AS status, run.repair_control_run_id AS repair_control_run_id",
            run_id=stale_run_id,
        ).single(strict=True)
    assert dict(stale) == {"status": "failed", "repair_control_run_id": run.run_id}

    with neo4j_driver.session() as session:
        session.run(
            "CREATE (run:IngestRun {ingest_run_id: $run_id, control_instance_id: 'other-control', "
            "status: 'queued'}) "
            "CREATE (logical:IngestionLogicalRun {logical_run_id: 'foreign-stale-logical', "
            "control_instance_id: 'other-control'})-[:HAS_ATTEMPT]->(run)",
            run_id=stale_run_id,
        ).consume()
    with pytest.raises(RuntimeError, match="ambiguous"):
        control.request_stop_topology(
            control_instance_id=run.control_instance_id,
            run_id=run.run_id,
            owner_id="owner",
            stale_run_id=stale_run_id,
        )


def test_310_stale_owned_topology_is_terminalized_only_when_exact(neo4j_driver: Driver) -> None:
    ledger = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    stale_run_id = "e5deb1d6-7333-4660-be4f-c44fcf5af686"
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (stale:IngestRun {ingest_run_id: $stale_run_id, "
            "control_instance_id: $control_instance_id, status: 'queued'}) "
            "CREATE (logical:IngestionLogicalRun {logical_run_id: 'owned-stale-logical', "
            "control_instance_id: $control_instance_id, status: 'queued'})-[:HAS_ATTEMPT]->(stale) "
            "CREATE (checkpoint:IngestionCheckpoint {checkpoint_id: 'owned-stale-checkpoint', "
            "control_instance_id: $control_instance_id})-[:PRODUCED_BY]->(stale)",
            stale_run_id=stale_run_id,
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    snapshot = _snapshot(ledger)
    run = ledger.qualify(_manifest(snapshot, repair_id="repair-310-stale-owned"), snapshot)
    control = CrmDealRepairControlRepository(cast(Neo4jClient, _client(neo4j_driver)))
    _seed_complete_task_topology(neo4j_driver, run.control_instance_id)
    lease = control.claim(
        RepairControlRequest("repair-310-stale-owned", run.run_id, "owner", "token", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    topology = control.request_stop_topology(
        control_instance_id=run.control_instance_id,
        run_id=run.run_id,
        owner_id="owner",
        stale_run_id=stale_run_id,
    )
    completed = control.complete_quiescence(
        RepairControlRequest(
            "repair-310-stale-owned", run.run_id, "owner", "token", lease.revision
        ),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
        topology_digest=topology,
        evidence=_absence_evidence(
            control,
            run,
            owner="owner",
            token_digest="token",
            revision=lease.revision,
            topology_digest=topology,
        ),
        proof_secret=b"secret",
        stale_run_id=stale_run_id,
    )
    assert completed.state == "quiesced"
    with neo4j_driver.session() as session:
        stale = session.run(
            "MATCH (stale:IngestRun {ingest_run_id: $stale_run_id}) "
            "MATCH (logical:IngestionLogicalRun {logical_run_id: 'owned-stale-logical'}) "
            "RETURN stale.status AS status, stale.repair_control_run_id AS repair_control_run_id, "
            "logical.status AS logical_status",
            stale_run_id=stale_run_id,
        ).single(strict=True)
    assert dict(stale) == {
        "status": "failed",
        "repair_control_run_id": run.run_id,
        "logical_status": "queued",
    }


def test_310_worker_fence_race_rejects_final_quiescence(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalization must not sample a boundary before it has every worker fence lock."""
    _, control, run = _qualified_control_repository(neo4j_driver, repair_id="repair-310-fence-race")
    _seed_complete_task_topology(neo4j_driver, run.control_instance_id)
    lease = control.claim(
        RepairControlRequest("repair-310-fence-race", run.run_id, "owner", "token", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    topology = control.request_stop_topology(
        control_instance_id=run.control_instance_id,
        run_id=run.run_id,
        owner_id="owner",
        stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
    )
    evidence = _absence_evidence(
        control,
        run,
        owner="owner",
        token_digest="token",
        revision=lease.revision,
        topology_digest=topology,
    )
    worker_locked = Event()
    mutate_worker = Event()
    finalizer_started = Event()
    boundary_read = Event()
    original_snapshot_from_transaction = control_repository_module._snapshot_from_transaction

    def observe_post_lock_snapshot(*args: object, **kwargs: object) -> RepairBoundarySnapshot:
        boundary_read.set()
        return original_snapshot_from_transaction(*args, **kwargs)

    monkeypatch.setattr(
        control_repository_module,
        "_snapshot_from_transaction",
        observe_post_lock_snapshot,
    )

    def mutate_while_holding_fence() -> None:
        with neo4j_driver.session() as session:
            transaction = session.begin_transaction()
            try:
                transaction.run(
                    "MATCH (stream:BitrixIngestionStream {logical_run_id: 'repair-logical'}) "
                    "SET stream.worker_fence_race_lock = 1"
                ).consume()
                worker_locked.set()
                assert mutate_worker.wait(timeout=10)
                transaction.run(
                    "MATCH (record:SourceRecord {source_record_pk: $pk}) "
                    "SET record.record_hash = 'sha256:worker-boundary-drift'",
                    pk=_TEST_SOURCE_RECORD_PK,
                ).consume()
                transaction.commit()
            except BaseException:
                transaction.rollback()
                raise

    def finalize() -> None:
        finalizer_started.set()
        control.complete_quiescence(
            RepairControlRequest(
                "repair-310-fence-race", run.run_id, "owner", "token", lease.revision
            ),
            boundary_digest=run.boundary_digest,
            control_instance_id=run.control_instance_id,
            topology_digest=topology,
            evidence=evidence,
            proof_secret=b"secret",
            stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        worker = executor.submit(mutate_while_holding_fence)
        assert worker_locked.wait(timeout=10)
        finalizer = executor.submit(finalize)
        assert finalizer_started.wait(timeout=10)
        # The finalizer must be blocked at LOCK_REPAIR_TOPOLOGY, not invoking
        # the actual post-lock full-boundary reread while the worker owns it.
        assert not boundary_read.wait(timeout=0.25)
        mutate_worker.set()
        worker.result(timeout=10)
        with pytest.raises(RuntimeError, match="boundary"):
            finalizer.result(timeout=10)

    assert boundary_read.is_set()
    with neo4j_driver.session() as session:
        status = session.run(
            "MATCH (stream:BitrixIngestionStream {logical_run_id: 'repair-logical'}) "
            "RETURN stream.status AS status"
        ).single(strict=True)["status"]
    assert status == "active"


def test_310_topology_capture_supersedes_only_exact_target_and_retains_controls(
    neo4j_driver: Driver,
) -> None:
    ledger = _repository(neo4j_driver)
    _persist_evidence(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (attempt:IngestRun {ingest_run_id: 'target-attempt', "
            "control_instance_id: $control_instance_id, generation: 7, status: 'running'}) "
            "CREATE (logical:IngestionLogicalRun {source_key: 'bitrix_chat', "
            "logical_run_id: 'target-logical', control_instance_id: $control_instance_id, "
            "status: 'running'})"
            "-[:HAS_ATTEMPT]->(attempt) "
            "CREATE (stream:BitrixIngestionStream {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, stream_key: 'crm_deals', "
            "logical_run_id: 'target-logical', ingest_run_id: 'target-attempt', "
            "attempt_generation: 7, stream_generation: 11, fencing_token: 'target-fence', "
            "status: 'active'}) "
            "CREATE (checkpoint:IngestionCheckpoint {control_instance_id: $control_instance_id, "
            "logical_run_id: 'target-logical', checkpoint_id: 'target-checkpoint'}) "
            "CREATE (logical)-[:HAS_CONTINUATION]->(:IngestionLogicalRun {"
            "logical_run_id: 'target-continuation', control_instance_id: $control_instance_id}) "
            "CREATE (generation:BitrixBackfillGeneration {"
            "control_instance_id: $control_instance_id, "
            "generation_id: 'target-generation'})-[:HAS_STREAM {stream_generation: 11, "
            "fencing_token: 'target-fence'}]->(stream) "
            "CREATE (generation)-[:HAS_LOGICAL_RUN {stream_key: 'crm_deals'}]->(logical) "
            "CREATE (:StageHistoryUnit {control_instance_id: $control_instance_id, "
            "unit_id: 'retained-history', state: 'complete'}) "
            "CREATE (other_attempt:IngestRun {ingest_run_id: 'other-attempt', "
            "control_instance_id: 'other-control', generation: 1, status: 'running'}) "
            "CREATE (other_logical:IngestionLogicalRun {logical_run_id: 'other-logical', "
            "control_instance_id: 'other-control', status: 'running'})"
            "-[:HAS_ATTEMPT]->(other_attempt) "
            "CREATE (:BitrixIngestionStream {source_key: 'bitrix_chat', "
            "control_instance_id: 'other-control', stream_key: 'crm_deals', "
            "logical_run_id: 'other-logical', ingest_run_id: 'other-attempt', "
            "attempt_generation: 1, stream_generation: 1, fencing_token: 'other-fence', "
            "status: 'active'})",
            control_instance_id=_TEST_CONTROL_INSTANCE_ID,
        ).consume()
    snapshot = _snapshot(ledger)
    run = ledger.qualify(_manifest(snapshot, repair_id="repair-310-topology"), snapshot)
    control = CrmDealRepairControlRepository(cast(Neo4jClient, _client(neo4j_driver)))
    control.claim(
        RepairControlRequest("repair-310-topology", run.run_id, "owner", "token", 0),
        boundary_digest=run.boundary_digest,
        control_instance_id=run.control_instance_id,
    )
    topology = control.request_stop_topology(
        control_instance_id=run.control_instance_id,
        run_id=run.run_id,
        owner_id="owner",
        stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
    )
    with neo4j_driver.session() as session:
        capture = session.run(
            "MATCH (capture:CrmDealRepairTopologyCapture {run_id: $run_id, "
            "topology_digest: $topology_digest}) RETURN capture.captures_json AS captures_json",
            run_id=run.run_id,
            topology_digest=topology,
        ).single(strict=True)["captures_json"]
    decoded = json.loads(cast(str, capture))
    captured = decoded["captures"]
    assert captured == [
        {
            "attempt_generation": 7,
            "attempt_status": "running",
            "checkpoint_ids": ["target-checkpoint"],
            "continuation_ids": ["target-continuation"],
            "fences": [
                {
                    "fencing_token": "target-fence",
                    "generation_id": "target-generation",
                    "stream_generation": 11,
                }
            ],
            "fencing_token": "target-fence",
            "ingest_run_id": "target-attempt",
            "logical_run_id": "target-logical",
            "stream_generation": 11,
            "stream_key": "crm_deals",
        }
    ]
    control.supersede_captured_topology(
        control_instance_id=run.control_instance_id, run_id=run.run_id, topology_digest=topology
    )
    with neo4j_driver.session() as session:
        rows = session.run(
            "MATCH (target:BitrixIngestionStream {logical_run_id: 'target-logical'}) "
            "MATCH (other:BitrixIngestionStream {logical_run_id: 'other-logical'}) "
            "MATCH (history:StageHistoryUnit {unit_id: 'retained-history'}) "
            "RETURN target.status AS target, other.status AS other, history.state AS history"
        ).single(strict=True)
    assert dict(rows) == {"target": "superseded", "other": "active", "history": "complete"}


def test_310_real_neo4j_authoritative_reader_excludes_retired_link_and_audit_retains_it(
    neo4j_driver: Driver,
) -> None:
    """Exercise real Cypher reader behavior, not only source classification."""
    from src.graph.queries.crm_deal_identity_repair_verification import (
        READ_RETIRED_RELATIONSHIP_SNAPSHOTS,
    )
    from src.graph.queries.sales import RESOLVE_SALES_CUSTOMER

    with neo4j_driver.session() as session:
        session.run(
            "CREATE (sales:SourceRecord {source_record_pk: 'reader-sales'})"
            "-[:FOR_CUSTOMER_RECORD]->(identity:SourceRecord {source_record_pk: 'reader-identity', "
            "lifecycle_status: 'active'}) "
            "CREATE (person:Person {person_id: 'reader-person', status: 'active'}) "
            "CREATE (identity)-[:LINKED_TO {is_active: false, retired_at: datetime(), "
            "retired_by_repair_id: 'repair-310-reader'}]->(person)"
        ).consume()
        authoritative = session.run(
            RESOLVE_SALES_CUSTOMER, sales_source_record_pk="reader-sales"
        ).single()
        audit = list(
            session.run(
                READ_RETIRED_RELATIONSHIP_SNAPSHOTS,
                retired_source_record_pks=["reader-identity"],
            )
        )
    assert authoritative is None
    assert len(audit) == 1
    properties = audit[0]["relationship_properties"]
    assert properties["is_active"] is False
    assert properties["retired_by_repair_id"] == "repair-310-reader"


def test_310_real_neo4j_sales_materializers_preserve_retired_active_projections(
    neo4j_driver: Driver,
) -> None:
    """Current sales writers create one active edge without reviving retired history."""
    from src.graph.queries.sales import LINK_PERSON_PURCHASED_ORDER
    from src.graph.queries.vehicle import LINK_PERSON_BOUGHT_VEHICLE

    parameters = {
        "person_id": "repair-test-materializer-person",
        "source_system_key": "repair-test-materializer-source",
        "source_order_id": "repair-test-materializer-order",
        "source_record_pk": "repair-test-materializer-record",
        "vehicle_id": "repair-test-materializer-vehicle",
        "raw_context": "repair-test",
        "observed_at": "2026-09-02T00:00:00Z",
        "confidence": 1.0,
        "quality_flag": "verified",
        "is_active": True,
    }
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (person:Person {person_id: $person_id}) "
            "CREATE (sale_order:Order {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id}) "
            "CREATE (vehicle:Vehicle {vehicle_id: $vehicle_id}) "
            "CREATE (person)-[:PURCHASED {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id, is_active: true, "
            "retired_at: datetime()}]->(sale_order) "
            "CREATE (person)-[:PURCHASED {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id, is_active: false, "
            "retired_at: datetime()}]->(sale_order) "
            "CREATE (person)-[:BOUGHT_VEHICLE {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id, is_active: true, "
            "retired_at: datetime()}]->(vehicle) "
            "CREATE (person)-[:BOUGHT_VEHICLE {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id, is_active: false, "
            "retired_at: datetime()}]->(vehicle)",
            **parameters,
        ).consume()
        for _ in range(2):
            session.run(LINK_PERSON_PURCHASED_ORDER, **parameters).consume()
            session.run(LINK_PERSON_BOUGHT_VEHICLE, **parameters).consume()
        purchase_counts = session.run(
            "MATCH (:Person {person_id: $person_id})-[rel:PURCHASED]->"
            "(:Order {source_system_key: $source_system_key, source_order_id: $source_order_id}) "
            "RETURN count(rel) AS total, "
            "count(CASE WHEN rel.is_active = true THEN rel END) AS active, "
            "count(CASE WHEN rel.is_active = false THEN rel END) AS retired, "
            "count(CASE WHEN rel.is_active = true AND rel.retired_at IS NOT NULL THEN rel END) "
            "AS contradictory",
            **parameters,
        ).single(strict=True)
        vehicle_counts = session.run(
            "MATCH (:Person {person_id: $person_id})-[rel:BOUGHT_VEHICLE]->"
            "(:Vehicle {vehicle_id: $vehicle_id}) "
            "RETURN count(rel) AS total, "
            "count(CASE WHEN rel.is_active = true THEN rel END) AS active, "
            "count(CASE WHEN rel.is_active = false THEN rel END) AS retired, "
            "count(CASE WHEN rel.is_active = true AND rel.retired_at IS NOT NULL THEN rel END) "
            "AS contradictory",
            **parameters,
        ).single(strict=True)
    assert dict(purchase_counts) == {"total": 2, "active": 1, "retired": 1, "contradictory": 0}
    assert dict(vehicle_counts) == {"total": 2, "active": 1, "retired": 1, "contradictory": 0}


def test_310_real_neo4j_sales_materializers_normalize_legacy_active_edges(
    neo4j_driver: Driver,
) -> None:
    """Adding is_active to a MERGE key cannot duplicate an old current relationship."""
    from src.graph.queries.sales import LINK_PERSON_PURCHASED_ORDER
    from src.graph.queries.vehicle import LINK_PERSON_BOUGHT_VEHICLE

    parameters = {
        "person_id": "repair-test-legacy-materializer-person",
        "source_system_key": "repair-test-legacy-materializer-source",
        "source_order_id": "repair-test-legacy-materializer-order",
        "source_record_pk": "repair-test-legacy-materializer-record",
        "vehicle_id": "repair-test-legacy-materializer-vehicle",
        "raw_context": "repair-test",
        "observed_at": "2026-09-02T00:00:00Z",
        "confidence": 1.0,
        "quality_flag": "verified",
        "is_active": True,
    }
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (person:Person {person_id: $person_id}) "
            "CREATE (sale_order:Order {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id}) "
            "CREATE (vehicle:Vehicle {vehicle_id: $vehicle_id}) "
            "CREATE (person)-[:PURCHASED {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id}]->(sale_order) "
            "CREATE (person)-[:BOUGHT_VEHICLE {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id}]->(vehicle)",
            **parameters,
        ).consume()
        for _ in range(2):
            session.run(LINK_PERSON_PURCHASED_ORDER, **parameters).consume()
            session.run(LINK_PERSON_BOUGHT_VEHICLE, **parameters).consume()
        purchase_counts = session.run(
            "MATCH (:Person {person_id: $person_id})-[rel:PURCHASED]->"
            "(:Order {source_system_key: $source_system_key, "
            "source_order_id: $source_order_id}) "
            "RETURN count(rel) AS total, "
            "count(CASE WHEN rel.is_active = true THEN rel END) AS active",
            **parameters,
        ).single(strict=True)
        vehicle_counts = session.run(
            "MATCH (:Person {person_id: $person_id})-[rel:BOUGHT_VEHICLE]->"
            "(:Vehicle {vehicle_id: $vehicle_id}) "
            "RETURN count(rel) AS total, "
            "count(CASE WHEN rel.is_active = true THEN rel END) AS active",
            **parameters,
        ).single(strict=True)
    assert dict(purchase_counts) == {"total": 1, "active": 1}
    assert dict(vehicle_counts) == {"total": 1, "active": 1}


def _allocation_plan_for_test(run_id: str, boundary_digest: str, unit_count: int) -> AllocationPlan:
    from src.crm_deal_identity_repair.control_models import RepairAllocationCompletion
    from src.crm_deal_identity_repair.execution_records import RepairUnit

    digest = "sha256:" + "a" * 64
    units = tuple(
        RepairUnit(
            run_id,
            f"allocation-unit-{index}",
            1,
            index,
            1,
            boundary_digest,
            digest,
            "allocated",
            f"inventory-{index}",
            f"source-{index}",
            digest,
            digest,
            digest,
        )
        for index in range(unit_count)
    )
    return AllocationPlan(
        units,
        RepairAllocationCompletion(
            run_id,
            f"allocation-completion-{unit_count}",
            boundary_digest,
            digest,
            digest,
            unit_count,
        ),
    )


def _seed_quiesced_allocation_control(
    driver: Driver,
    run: RepairQualificationRun,
    *,
    owner: str = "owner",
    token_digest: str = "token",
) -> None:
    durable_token_digest = control_token_digest(token_digest)
    with driver.session() as session:
        session.run(
            "CREATE (:CrmDealRepairControl {repair_id: $repair_id, run_id: $run_id, "
            "control_instance_id: $control_instance_id, owner_id: $owner, "
            "token_digest: $token_digest, revision: 1, claim_expected_revision: 0, "
            "state: 'quiesced', boundary_digest: $boundary_digest, proof_digest: 'proof', "
            "proof_expires_at: datetime() + duration('PT5M')}) "
            "WITH 1 AS ignored "
            "MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) "
            "SET dispatch.blocked = true, dispatch.repair_run_id = $run_id, "
            "dispatch.repair_owner_id = $owner, dispatch.repair_token_digest = $token_digest, "
            "dispatch.repair_revision = 1",
            repair_id=run.repair_id,
            run_id=run.run_id,
            control_instance_id=run.control_instance_id,
            owner=owner,
            token_digest=durable_token_digest,
            boundary_digest=run.boundary_digest,
        ).consume()
    sealed = _snapshot(_repository(driver))
    with driver.session() as session:
        session.run(
            "MATCH (control:CrmDealRepairControl {run_id: $run_id}) "
            "SET control.sealed_boundary_digest = $sealed_boundary_digest, "
            "control.sealed_source_records_digest = $sealed_source_records_digest, "
            "control.sealed_source_instance_digest = $sealed_source_instance_digest, "
            "control.sealed_stale_run_evidence_digest = $sealed_stale_run_evidence_digest, "
            "control.sealed_control_digest = $sealed_control_digest, "
            "control.sealed_inventory_digest = $sealed_inventory_digest, "
            "control.sealed_inventory_row_count = $sealed_inventory_row_count, "
            "control.sealed_eligible_unit_count = $sealed_eligible_unit_count, "
            "control.sealed_negative_control_count = $sealed_negative_control_count",
            run_id=run.run_id,
            sealed_boundary_digest=sealed.boundary_digest,
            sealed_source_records_digest=sealed.source_records_digest,
            sealed_source_instance_digest=sealed.source_instance_digest,
            sealed_stale_run_evidence_digest=sealed.stale_run_evidence_digest,
            sealed_control_digest=sealed.control_digest,
            sealed_inventory_digest=sealed.inventory_digest,
            sealed_inventory_row_count=sealed.inventory_row_count,
            sealed_eligible_unit_count=sealed.eligible_unit_count,
            sealed_negative_control_count=sealed.negative_control_count,
        ).consume()


def test_310_pause_resume_exact_replay_rejects_conflicts_and_stale_revisions(
    neo4j_driver: Driver,
) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-pause-replay"
    )
    _seed_quiesced_allocation_control(neo4j_driver, run, owner="owner-a", token_digest="token-a")
    pause = RepairControlRequest("repair-310-pause-replay", run.run_id, "owner-a", "token-a", 1)
    paused = control.pause(pause)
    assert (paused.state, paused.revision) == ("paused", 2)
    assert control.pause(pause) == paused
    with pytest.raises(RuntimeError, match="compare-and-set"):
        control.pause(
            RepairControlRequest("repair-310-pause-replay", run.run_id, "owner-b", "token-b", 1)
        )
    with pytest.raises(RuntimeError, match="compare-and-set"):
        control.pause(
            RepairControlRequest("repair-310-pause-replay", run.run_id, "owner-a", "token-a", 0)
        )
    resume = RepairControlRequest("repair-310-pause-replay", run.run_id, "owner-a", "token-a", 2)
    resumed = control.resume(resume)
    assert (resumed.state, resumed.revision) == ("quiesced", 3)
    assert control.resume(resume) == resumed
    with pytest.raises(RuntimeError, match="compare-and-set"):
        control.resume(
            RepairControlRequest("repair-310-pause-replay", run.run_id, "owner-a", "token-a", 1)
        )
    allocated = control.allocate(
        RepairControlRequest("repair-310-pause-replay", run.run_id, "owner-a", "token-a", 3),
        boundary_digest=run.boundary_digest,
        proof_digest="proof",
        plan=_allocation_plan_for_test(run.run_id, run.boundary_digest, 1),
    )
    assert (allocated.state, allocated.revision) == ("allocated", 4)


def test_310_pause_rejects_unrelated_control_drift_before_resealing(
    neo4j_driver: Driver,
) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-pause-drift"
    )
    _seed_quiesced_allocation_control(neo4j_driver, run)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (dispatch:BitrixDispatchControl {control_instance_id: $control_instance_id}) "
            "SET dispatch.unrelated_control_drift = true",
            control_instance_id=run.control_instance_id,
        ).consume()

    with pytest.raises(RuntimeError, match="full boundary became stale"):
        control.pause(
            RepairControlRequest("repair-310-pause-drift", run.run_id, "owner", "token", 1)
        )


def test_310_allocated_pause_resume_reseals_and_allocation_replay_is_exact(
    neo4j_driver: Driver,
) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-allocated-pause"
    )
    _seed_quiesced_allocation_control(neo4j_driver, run)
    allocation_request = RepairControlRequest(
        "repair-310-allocated-pause", run.run_id, "owner", "token", 1
    )
    plan = _allocation_plan_for_test(run.run_id, run.boundary_digest, 1)
    allocated = control.allocate(
        allocation_request,
        boundary_digest=run.boundary_digest,
        proof_digest="proof",
        plan=plan,
    )
    assert (allocated.state, allocated.revision) == ("allocated", 2)
    pause = RepairControlRequest("repair-310-allocated-pause", run.run_id, "owner", "token", 2)
    paused = control.pause(pause)
    assert (paused.state, paused.revision) == ("paused", 3)
    assert control.pause(pause) == paused
    with pytest.raises(RuntimeError, match="compare-and-set"):
        control.pause(
            RepairControlRequest("repair-310-allocated-pause", run.run_id, "other", "token", 2)
        )
    resume = RepairControlRequest("repair-310-allocated-pause", run.run_id, "owner", "token", 3)
    resumed = control.resume(resume)
    assert (resumed.state, resumed.revision) == ("allocated", 4)
    assert control.resume(resume) == resumed
    replay = control.allocate(
        allocation_request,
        boundary_digest=run.boundary_digest,
        proof_digest="proof",
        plan=plan,
    )
    assert replay == allocated
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: 'allocation-unit-0'}) "
            "SET unit.inventory_key = 'tampered-after-resume'",
            run_id=run.run_id,
        ).consume()
    # The exact original request/revision remains a replay request after resume;
    # it must reject corrupt persisted allocation evidence, not fall through to
    # a fresh CAS at the resumed revision.
    with pytest.raises(RuntimeError):
        control.allocate(
            allocation_request,
            boundary_digest=run.boundary_digest,
            proof_digest="proof",
            plan=plan,
        )


def test_310_allocation_persists_exact_multi_unit_set_and_replay_conflicts(
    neo4j_driver: Driver,
) -> None:
    _, control, run = _qualified_control_repository(neo4j_driver, repair_id="repair-310-allocation")
    _seed_quiesced_allocation_control(neo4j_driver, run)
    plan = _allocation_plan_for_test(run.run_id, run.boundary_digest, 2)
    allocated = control.allocate(
        RepairControlRequest("repair-310-allocation", run.run_id, "owner", "token", 1),
        boundary_digest=run.boundary_digest,
        proof_digest="proof",
        plan=plan,
    )
    assert allocated.state == "allocated"
    replay_request = RepairControlRequest("repair-310-allocation", run.run_id, "owner", "token", 1)
    replay = control.allocate(
        replay_request,
        boundary_digest=run.boundary_digest,
        proof_digest=control.proof_digest(replay_request),
        plan=plan,
    )
    assert replay == allocated
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: 'allocation-unit-0'}) "
            "SET unit.inventory_key = 'tampered'",
            run_id=run.run_id,
        ).consume()
    with pytest.raises(RuntimeError):
        control.allocate(
            replay_request,
            boundary_digest=run.boundary_digest,
            proof_digest=control.proof_digest(replay_request),
            plan=plan,
        )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (control:CrmDealRepairControl {run_id: $run_id}) "
            "SET control.proof_expires_at = datetime() - duration('PT1S')",
            run_id=run.run_id,
        ).consume()
    with pytest.raises(RuntimeError, match="absent or stale"):
        control.proof_digest(replay_request)


def test_310_exact_allocation_replay_rejects_a_corrupted_current_seal(
    neo4j_driver: Driver,
) -> None:
    """An immutable receipt is unusable when its current lifecycle seal is corrupt."""
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-seal-replay"
    )
    _seed_quiesced_allocation_control(neo4j_driver, run)
    request = RepairControlRequest("repair-310-seal-replay", run.run_id, "owner", "token", 1)
    plan = _allocation_plan_for_test(run.run_id, run.boundary_digest, 1)
    control.allocate(
        request,
        boundary_digest=run.boundary_digest,
        proof_digest="proof",
        plan=plan,
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (control:CrmDealRepairControl {run_id: $run_id}) "
            "SET control.sealed_boundary_digest = 'tampered-seal'",
            run_id=run.run_id,
        ).consume()
    with pytest.raises(RuntimeError):
        control.allocate(
            request,
            boundary_digest=run.boundary_digest,
            proof_digest=control.proof_digest(request),
            plan=plan,
        )


@pytest.mark.parametrize(
    ("property_name", "value"),
    (
        ("receipt_control_instance_id", "tampered-control"),
        ("receipt_run_id", "tampered-run"),
        ("receipt_owner_id", "tampered-owner"),
        ("receipt_token_digest", "tampered-token"),
        ("receipt_revision", 999),
        ("receipt_state", "paused"),
        ("receipt_boundary_digest", "tampered-boundary"),
        ("receipt_sealed_boundary_digest", "tampered-seal"),
        ("receipt_digest", "sha256:tampered"),
    ),
)
def test_310_exact_allocation_replay_rejects_tampered_immutable_receipt(
    neo4j_driver: Driver, property_name: str, value: str | int
) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id=f"repair-310-receipt-{property_name}"
    )
    _seed_quiesced_allocation_control(neo4j_driver, run)
    request = RepairControlRequest(
        f"repair-310-receipt-{property_name}", run.run_id, "owner", "token", 1
    )
    plan = _allocation_plan_for_test(run.run_id, run.boundary_digest, 1)
    control.allocate(request, boundary_digest=run.boundary_digest, proof_digest="proof", plan=plan)
    with neo4j_driver.session() as session:
        session.run(
            f"MATCH (completion:CrmDealRepairAllocationCompletion {{run_id: $run_id}}) "
            f"SET completion.{property_name} = $value",
            run_id=run.run_id,
            value=value,
        ).consume()
    with pytest.raises(RuntimeError):
        control.allocate(
            request, boundary_digest=run.boundary_digest, proof_digest="proof", plan=plan
        )


@pytest.mark.parametrize(
    ("mutation_query", "mutation_params"),
    (
        (
            "MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id}) "
            "SET dispatch.unrelated_control_drift = 'changed'",
            ("control_instance_id",),
        ),
        (
            "MERGE (stale:IngestRun {ingest_run_id: $stale_run_id}) "
            "SET stale.unrelated_stale_drift = 'changed'",
            ("stale_run_id",),
        ),
    ),
)
def test_310_allocation_rejects_any_post_quiescence_control_or_stale_drift(
    neo4j_driver: Driver,
    mutation_query: str,
    mutation_params: tuple[str, ...],
) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-full-boundary"
    )
    _seed_quiesced_allocation_control(neo4j_driver, run)
    parameters = {
        "control_instance_id": run.control_instance_id,
        "stale_run_id": "e5deb1d6-7333-4660-be4f-c44fcf5af686",
    }
    with neo4j_driver.session() as session:
        session.run(
            mutation_query,
            **{key: parameters[key] for key in mutation_params},
        ).consume()

    with pytest.raises(RuntimeError, match="full boundary became stale"):
        control.allocate(
            RepairControlRequest("repair-310-full-boundary", run.run_id, "owner", "token", 1),
            boundary_digest=run.boundary_digest,
            proof_digest="proof",
            plan=_allocation_plan_for_test(run.run_id, run.boundary_digest, 1),
        )
    with neo4j_driver.session() as session:
        count = session.run(
            "MATCH (:CrmDealRepairAllocationCompletion {run_id: $run_id}) RETURN count(*) AS count",
            run_id=run.run_id,
        ).single(strict=True)["count"]
    assert count == 0


def test_310_allocation_persists_zero_unit_completion_without_a_unit(neo4j_driver: Driver) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-zero-allocation"
    )
    _seed_quiesced_allocation_control(neo4j_driver, run)
    plan = _allocation_plan_for_test(run.run_id, run.boundary_digest, 0)
    allocated = control.allocate(
        RepairControlRequest("repair-310-zero-allocation", run.run_id, "owner", "token", 1),
        boundary_digest=run.boundary_digest,
        proof_digest="proof",
        plan=plan,
    )
    assert allocated.state == "allocated"
    assert (
        control.allocate(
            RepairControlRequest("repair-310-zero-allocation", run.run_id, "owner", "token", 1),
            boundary_digest=run.boundary_digest,
            proof_digest="proof",
            plan=plan,
        )
        == allocated
    )
    with pytest.raises(RuntimeError):
        control.allocate(
            RepairControlRequest("repair-310-zero-allocation", run.run_id, "owner", "token", 2),
            boundary_digest=run.boundary_digest,
            proof_digest="proof",
            plan=_allocation_plan_for_test(run.run_id, run.boundary_digest, 1),
        )
    with neo4j_driver.session() as session:
        assert (
            session.run(
                "MATCH (:CrmDealRepairUnit {run_id: $run_id}) RETURN count(*) AS count",
                run_id=run.run_id,
            ).single(strict=True)["count"]
            == 0
        )


def test_310_allocation_conflict_rolls_back_multi_unit_completion(neo4j_driver: Driver) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-allocation-rollback"
    )
    _seed_quiesced_allocation_control(neo4j_driver, run)
    plan = _allocation_plan_for_test(run.run_id, run.boundary_digest, 2)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:CrmDealRepairUnit {run_id: $run_id, unit_id: 'allocation-unit-0', "
            "generation: 1, sequence: 0, attempt: 1, boundary_digest: $boundary_digest, "
            "inventory_fingerprint: $digest, state: 'allocated', inventory_key: 'conflict', "
            "source_record_pk: 'source-0', inventory_graph_fingerprint: $digest, "
            "inventory_stored_payload_fingerprint: $digest, inventory_binding_digest: $digest})",
            run_id=run.run_id,
            boundary_digest=run.boundary_digest,
            digest="sha256:" + "a" * 64,
        ).consume()
    with pytest.raises(RuntimeError):
        control.allocate(
            RepairControlRequest("repair-310-allocation-rollback", run.run_id, "owner", "token", 1),
            boundary_digest=run.boundary_digest,
            proof_digest="proof",
            plan=plan,
        )
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (unit:CrmDealRepairUnit {run_id: $run_id}) "
            "WITH count(unit) AS units "
            "OPTIONAL MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id}) "
            "RETURN units, count(completion) AS completions",
            run_id=run.run_id,
        ).single(strict=True)
    assert dict(row) == {"units": 1, "completions": 0}


def test_310_allocation_concurrent_replay_persists_one_complete_unit_set(
    neo4j_driver: Driver,
) -> None:
    _, control, run = _qualified_control_repository(
        neo4j_driver, repair_id="repair-310-allocation-concurrent"
    )
    _seed_quiesced_allocation_control(neo4j_driver, run)
    plan = _allocation_plan_for_test(run.run_id, run.boundary_digest, 2)

    def allocate() -> bool:
        try:
            control.allocate(
                RepairControlRequest(
                    "repair-310-allocation-concurrent", run.run_id, "owner", "token", 1
                ),
                boundary_digest=run.boundary_digest,
                proof_digest="proof",
                plan=plan,
            )
        except RuntimeError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: allocate(), range(2)))
    assert sorted(outcomes) == [False, True]
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (unit:CrmDealRepairUnit {run_id: $run_id}) "
            "WITH count(unit) AS units "
            "MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id}) "
            "RETURN units, count(completion) AS completions",
            run_id=run.run_id,
        ).single(strict=True)
    assert dict(row) == {"units": 2, "completions": 1}
