"""Disposable persisted-evidence acceptance coverage for the #300 repair ledger.

This suite is deliberately opt-in.  It uses only a dedicated disposable Neo4j
database and never contacts Bitrix.  The fixture creates the persisted #272
source/control evidence that the ledger reads; qualification itself must create
only its repair metadata.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.crm_deal_identity_repair.execution_models import (
    RepairBoundarySnapshot,
    RepairExecutionBoundaryManifest,
)
from src.graph import crm_deal_identity_repair_ledger_migration as ledger_migration
from src.graph.bitrix_source_instances import BitrixSourceInstanceRepository
from src.graph.bootstrap import bootstrap_legacy_bitrix_source_instance
from src.graph.client import Neo4jClient
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
            "WHERE instance.source_instance_id IN $instance_ids DETACH DELETE instance",
            instance_ids=[_TEST_SOURCE_INSTANCE_ID, _TEST_CONTROL_INSTANCE_ID],
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
) -> dict[str, object]:
    with driver.session() as session:
        nodes = session.run(
            "MATCH (node) WHERE (node:SourceRecord AND node.source_record_pk = $source_record_pk) "
            "OR (node:BitrixSourceInstance AND node.source_key = 'bitrix_chat' "
            "AND node.source_instance_id IN $instance_ids) "
            "OR (node:BitrixExecutionSourceBinding AND node.source_key = 'bitrix_chat' "
            "AND node.control_instance_id = $control_instance_id) "
            "OR (node:BitrixDispatchControl AND node.source_key = 'bitrix_chat' "
            "AND node.control_instance_id = $control_instance_id) "
            "RETURN labels(node) AS labels, properties(node) AS properties "
            "ORDER BY labels, toString(properties)",
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
            "labels(right) AS right_labels, properties(right) AS right_properties "
            "ORDER BY left_labels, toString(left_properties), relationship_type, "
            "right_labels, toString(right_properties)",
            source_record_pk=_TEST_SOURCE_RECORD_PK,
            control_instance_id=control_instance_id,
        ).data()
    return {"nodes": nodes, "relationships": relationships}


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
