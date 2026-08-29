"""Opt-in disposable Neo4j acceptance coverage for #310 topology supersession."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction

from src.crm_deal_identity_repair.control_models import RepairControlLease
from src.graph import crm_deal_identity_repair_control as control_repository
from src.graph.crm_deal_identity_repair_control import CrmDealRepairControlRepository
from src.graph.queries.crm_deal_identity_repair_ledger import (
    ALLOCATE_REPAIR_UNITS,
    SUPERSEDE_CAPTURED_REPAIR_TOPOLOGY,
    TERMINALIZE_STALE_REPAIR_RUN,
    PERSIST_REPAIR_BOUNDARY_COMPONENT_PROOF,
    PERSIST_REPAIR_TRANSACTION_AUTHORIZATION,
    BEGIN_REPAIR_PUBLICATION,
    PUBLISH_REPAIR_PUBLICATION,
    RESERVE_REPAIR_PUBLICATION,
)

_DIGEST = "sha256:" + "a" * 64
_CONTROL = "repair-310-test-control"
_OTHER_CONTROL = "repair-310-test-other-control"
_RUN = "repair-310-test-run"
_OWNER = "repair-310-test-owner"
_TOKEN = "repair-310-test-token"


class _ControlClient:
    """Minimal transaction adapter for repository acceptance coverage."""

    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def execute_write(self, work: Callable[[ManagedTransaction], object]) -> object:
        with self._driver.session() as session:
            return session.execute_write(work)

    def execute_read(self, work: Callable[[ManagedTransaction], object]) -> object:
        with self._driver.session() as session:
            return session.execute_read(work)


@pytest.fixture
def disposable_neo4j_driver() -> Iterator[Driver]:
    """Use only an explicitly configured localhost disposable Neo4j database."""
    uri = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_URI")
    user = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_USER")
    password = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_PASSWORD")
    if uri is None or user is None or password is None:
        pytest.skip("disposable CRM repair Neo4j database is not configured")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_SERVICE_HOST") == "neo4j":
        allowed_hosts.add("neo4j")
    if urlparse(uri).hostname not in allowed_hosts:
        pytest.fail("#310 topology tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    connected = False
    try:
        for _ in range(15):
            try:
                driver.verify_connectivity()
                connected = True
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        if not connected:
            pytest.fail("disposable #310 Neo4j database did not become ready")
        _clear(driver)
        yield driver
    finally:
        try:
            if connected:
                _clear(driver)
        finally:
            driver.close()


def _clear(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (node) WHERE node.control_instance_id IN $controls DETACH DELETE node",
            controls=[_CONTROL, _OTHER_CONTROL],
        ).consume()
        session.run(
            "MATCH (run:CrmDealRepairRun {run_id: $run_id}) DETACH DELETE run", run_id=_RUN
        ).consume()
        session.run(
            "MATCH (control:CrmDealRepairControl {run_id: $run_id}) DETACH DELETE control",
            run_id=_RUN,
        ).consume()
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: 'repair-310-test-domain'}) "
            "DETACH DELETE record"
        ).consume()
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: 'repair-310-allocation-domain'}) "
            "DETACH DELETE record"
        ).consume()
        session.run(
            "MATCH (source:SourceSystem {repair_test_marker: $run_id}) DETACH DELETE source",
            run_id=_RUN,
        ).consume()


def _seed_control(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            "CREATE (:CrmDealRepairRun {run_id: $run_id, status: 'qualified', "
            "boundary_digest: $boundary_digest, execution_allowed: false, "
            "control_instance_id: $control_instance_id}) "
            "CREATE (:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token: $token, "
            "revision: 1, state: 'quiescing', boundary_digest: $boundary_digest}) "
            "CREATE (:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, blocked: true, "
            "repair_control_run_id: $run_id, repair_control_owner_id: $owner_id, "
            "repair_control_token: $token, repair_control_revision: 1, "
            "repair_control_state: 'quiescing', block_reason: 'crm_deal_repair_quiescence'}) "
            "CREATE (:SourceRecord {source_record_pk: 'repair-310-test-domain', marker: 'before'})",
            run_id=_RUN,
            owner_id=_OWNER,
            token=_TOKEN,
            boundary_digest=_DIGEST,
            control_instance_id=_CONTROL,
        ).consume()


def _seed_full_topology(driver: Driver, *, add_other_control: bool = False) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (run:CrmDealRepairRun {run_id: $run_id}) "
            "CREATE (logical:IngestionLogicalRun {control_instance_id: $control_instance_id, "
            "logical_run_id: 'logical-1', source_key: 'bitrix_chat', "
            "bitrix_stream_key: 'crm_deals', "
            "status: 'running'}) "
            "CREATE (attempt:IngestRun {control_instance_id: $control_instance_id, "
            "ingest_run_id: 'attempt-1', logical_run_id: 'logical-1', generation: 2, "
            "status: 'started'}) "
            "CREATE (logical)-[:HAS_ATTEMPT]->(attempt) "
            "CREATE (checkpoint:IngestionCheckpoint {control_instance_id: $control_instance_id, "
            "logical_run_id: 'logical-1', phase: 'read', generation: 2, status: 'active'}) "
            "CREATE (checkpoint)-[:CHECKPOINT_FOR]->(logical) "
            "CREATE (stream:BitrixIngestionStream {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, stream_key: 'crm_deals', "
            "logical_run_id: 'logical-1', ingest_run_id: 'attempt-1', attempt_generation: 2, "
            "stream_generation: 4, fencing_token: 8, fence_lock_version: 3, status: 'active'}) "
            "CREATE (generation:BitrixBackfillGeneration "
            "{control_instance_id: $control_instance_id, "
            "generation_id: 'generation-1', status: 'backfilling'}) "
            "CREATE (generation)-[:HAS_LOGICAL_RUN]->(logical) "
            "CREATE (generation)-[:HAS_STREAM]->(stream) "
            "CREATE (:BitrixBackfillDispatchOutbox {control_instance_id: $control_instance_id, "
            "successor_generation_id: 'generation-1', evidence_digest: 'evidence-1', "
            "occurrence: '2026-08-29T00:00:00Z', status: 'pending'})",
            run_id=_RUN,
            control_instance_id=_CONTROL,
        ).consume()
        if add_other_control:
            session.run(
                "CREATE (:IngestionLogicalRun {control_instance_id: $control_instance_id, "
                "logical_run_id: 'other-logical', source_key: 'bitrix_chat', "
                "bitrix_stream_key: 'crm_deals', status: 'running'}) "
                "CREATE (:BitrixIngestionStream {source_key: 'bitrix_chat', "
                "control_instance_id: $control_instance_id, stream_key: 'crm_deals', "
                "logical_run_id: 'other-logical', ingest_run_id: 'other-attempt', "
                "attempt_generation: 1, stream_generation: 1, fencing_token: 1, status: 'active'})",
                control_instance_id=_OTHER_CONTROL,
            ).consume()


def _parameters() -> dict[str, object]:
    return {
        "run_id": _RUN,
        "owner_id": _OWNER,
        "token": _TOKEN,
        "expected_revision": 1,
        "next_revision": 2,
        "boundary_digest": _DIGEST,
        "logical_run_ids": [{"logical_run_id": "logical-1", "status": "running"}],
        "ingest_run_ids": [{"ingest_run_id": "attempt-1", "status": "started", "generation": 2}],
        "checkpoint_ids": [
            {"logical_run_id": "logical-1", "phase": "read", "generation": 2, "status": "active"}
        ],
        "stream_ids": [
            {
                "stream_key": "crm_deals",
                "logical_run_id": "logical-1",
                "ingest_run_id": "attempt-1",
                "attempt_generation": 2,
                "stream_generation": 4,
                "fencing_token": 8,
                "status": "active",
            }
        ],
        "generation_ids": [{"generation_id": "generation-1", "status": "backfilling"}],
        "publication_ids": [
            {
                "successor_generation_id": "generation-1",
                "evidence_digest": "evidence-1",
                "occurrence": "2026-08-29T00:00:00Z",
                "status": "pending",
            }
        ],
    }


def _execute(driver: Driver, parameters: dict[str, object]) -> dict[str, object] | None:
    with driver.session() as session:
        record = session.run(SUPERSEDE_CAPTURED_REPAIR_TOPOLOGY, **parameters).single()
        return None if record is None else dict(record)


def _state(driver: Driver) -> dict[str, object]:
    with driver.session() as session:
        record = session.run(
            "MATCH (control:CrmDealRepairControl {run_id: $run_id}) "
            "MATCH (logical:IngestionLogicalRun {control_instance_id: $control_instance_id, "
            "logical_run_id: 'logical-1'}) "
            "MATCH (attempt:IngestRun {control_instance_id: $control_instance_id, "
            "ingest_run_id: 'attempt-1'}) "
            "MATCH (checkpoint:IngestionCheckpoint {control_instance_id: $control_instance_id, "
            "logical_run_id: 'logical-1', phase: 'read', generation: 2}) "
            "MATCH (stream:BitrixIngestionStream {control_instance_id: $control_instance_id, "
            "stream_key: 'crm_deals'}) "
            "MATCH (generation:BitrixBackfillGeneration "
            "{control_instance_id: $control_instance_id, "
            "generation_id: 'generation-1'}) "
            "MATCH (publication:BitrixBackfillDispatchOutbox "
            "{control_instance_id: $control_instance_id, "
            "successor_generation_id: 'generation-1'}) "
            "MATCH (record:SourceRecord {source_record_pk: 'repair-310-test-domain'}) "
            "RETURN control.state AS control_state, control.revision AS revision, "
            "logical.stop_requested AS stop_requested, attempt.status AS attempt_status, "
            "checkpoint.status AS checkpoint_status, stream.status AS stream_status, "
            "stream.fencing_token AS fencing_token, stream.stream_generation AS stream_generation, "
            "generation.status AS generation_status, publication.status AS publication_status, "
            "record.marker AS domain_marker",
            run_id=_RUN,
            control_instance_id=_CONTROL,
        ).single()
        assert record is not None
        return dict(record)


def test_all_empty_capture_quiesces_without_row_loss(disposable_neo4j_driver: Driver) -> None:
    _seed_control(disposable_neo4j_driver)
    parameters = _parameters()
    for category in (
        "logical_run_ids",
        "ingest_run_ids",
        "checkpoint_ids",
        "stream_ids",
        "generation_ids",
        "publication_ids",
    ):
        parameters[category] = []

    record = _execute(disposable_neo4j_driver, parameters)

    assert record == {"revision": 2}


def test_mixed_empty_capture_quiesces_without_row_loss(disposable_neo4j_driver: Driver) -> None:
    _seed_control(disposable_neo4j_driver)
    with disposable_neo4j_driver.session() as session:
        session.run(
            "CREATE (:IngestionLogicalRun {control_instance_id: $control_instance_id, "
            "logical_run_id: 'logical-1', source_key: 'bitrix_chat', "
            "bitrix_stream_key: 'crm_deals', "
            "status: 'running'})",
            control_instance_id=_CONTROL,
        ).consume()
    parameters = _parameters()
    parameters["ingest_run_ids"] = []
    parameters["checkpoint_ids"] = []
    parameters["stream_ids"] = []
    parameters["generation_ids"] = []
    parameters["publication_ids"] = []

    record = _execute(disposable_neo4j_driver, parameters)

    assert record == {"revision": 2}


def test_exact_capture_supersedes_and_invalidates_fence_without_domain_mutation(
    disposable_neo4j_driver: Driver,
) -> None:
    _seed_control(disposable_neo4j_driver)
    _seed_full_topology(disposable_neo4j_driver, add_other_control=True)

    assert _execute(disposable_neo4j_driver, _parameters()) == {"revision": 2}
    state = _state(disposable_neo4j_driver)
    with disposable_neo4j_driver.session() as session:
        other = session.run(
            "MATCH (stream:BitrixIngestionStream {control_instance_id: $control_instance_id, "
            "stream_key: 'crm_deals'}) RETURN stream.status AS status, "
            "stream.fencing_token AS fencing_token",
            control_instance_id=_OTHER_CONTROL,
        ).single()

    assert state == {
        "control_state": "quiesced",
        "revision": 2,
        "stop_requested": True,
        "attempt_status": "failed",
        "checkpoint_status": "superseded",
        "stream_status": "superseded",
        "fencing_token": 9,
        "stream_generation": 5,
        "generation_status": "superseded",
        "publication_status": "superseded",
        "domain_marker": "before",
    }
    assert other is not None
    assert dict(other) == {"status": "active", "fencing_token": 1}


@pytest.mark.parametrize("failure", ["stale_fence", "missing_identity"])
def test_capture_mismatch_rolls_back_every_prior_mutation(
    disposable_neo4j_driver: Driver, failure: str
) -> None:
    _seed_control(disposable_neo4j_driver)
    _seed_full_topology(disposable_neo4j_driver)
    parameters = _parameters()
    if failure == "stale_fence":
        streams = parameters["stream_ids"]
        assert isinstance(streams, list)
        stream = streams[0]
        assert isinstance(stream, dict)
        stream["fencing_token"] = 7
    else:
        checkpoints = parameters["checkpoint_ids"]
        assert isinstance(checkpoints, list)
        checkpoint = checkpoints[0]
        assert isinstance(checkpoint, dict)
        checkpoint["phase"] = "missing"

    assert _execute(disposable_neo4j_driver, parameters) is None
    state = _state(disposable_neo4j_driver)

    assert state == {
        "control_state": "quiescing",
        "revision": 1,
        "stop_requested": None,
        "attempt_status": "started",
        "checkpoint_status": "active",
        "stream_status": "active",
        "fencing_token": 8,
        "stream_generation": 4,
        "generation_status": "backfilling",
        "publication_status": "pending",
        "domain_marker": "before",
    }


def _allocation_parameters() -> dict[str, object]:
    return {
        "run_id": "repair-310-allocation-run",
        "owner_id": _OWNER,
        "token": _TOKEN,
        "expected_revision": 1,
        "next_revision": 2,
        "boundary_digest": _DIGEST,
        "manifest_digest": "sha256:" + "b" * 64,
        "artifact_id": "a" * 32,
        "artifact_manifest_hmac": "c" * 64,
        "inventory_digest": "sha256:" + "d" * 64,
        "source_instance_id": "source-310",
        "control_instance_id": "repair-310-allocation-control",
        "manifest_json": "{}",
        "inventory_row_count": 2,
        "eligible_unit_count": 2,
        "negative_control_count": 0,
        "manifest_unit_ceiling": 2,
        "unit_ceiling": 2,
        "generation": 1,
        "units": [
            {
                "run_id": "repair-310-allocation-run",
                "unit_id": "unit-1",
                "generation": 1,
                "sequence": 0,
                "attempt": 1,
                "boundary_digest": _DIGEST,
                "inventory_fingerprint": "sha256:" + "e" * 64,
                "state": "allocated",
            }
        ],
        "approved_rows": [
            {
                "inventory_key": "inventory-1",
                "source_record_pk": "source-1",
                "inventory_fingerprint": "sha256:" + "e" * 64,
                "disposition": "executable",
            },
            {
                "inventory_key": "inventory-2",
                "source_record_pk": "source-2",
                "inventory_fingerprint": "sha256:" + "a" * 64,
                "disposition": "blocked",
            },
        ],
        "qualified_source_record_pks": ["source-1", "source-2"],
        "allocation_digest": "sha256:" + "f" * 64,
        "overlay_digest": "sha256:" + "9" * 64,
        "approval_reference": "approval-310",
    }


def _seed_allocation(driver: Driver, *, state: str = "quiesced", owner: str = _OWNER) -> None:
    parameters = _allocation_parameters()
    with driver.session() as session:
        session.run(
            "MATCH (node) WHERE node.control_instance_id = $control_instance_id DETACH DELETE node",
            control_instance_id=parameters["control_instance_id"],
        ).consume()
        session.run(
            "MATCH (node {run_id: $run_id}) DETACH DELETE node", run_id=parameters["run_id"]
        ).consume()
        session.run(
            "MATCH (record:SourceRecord {source_record_pk: 'repair-310-allocation-domain'}) "
            "DETACH DELETE record"
        ).consume()
        session.run(
            "MATCH (source:SourceSystem {repair_test_marker: $run_id}) DETACH DELETE source",
            run_id=_RUN,
        ).consume()
        seed_parameters = dict(parameters)
        seed_parameters["owner_id"] = owner
        seed_parameters["state"] = state
        session.run(
            "CREATE (run:CrmDealRepairRun {run_id: $run_id, status: 'qualified', "
            "manifest_digest: $manifest_digest, artifact_id: $artifact_id, "
            "artifact_manifest_hmac: $artifact_manifest_hmac, inventory_digest: $inventory_digest, "
            "boundary_digest: $boundary_digest, source_instance_id: $source_instance_id, "
            "control_instance_id: $control_instance_id, manifest_json: $manifest_json, "
            "source_record_pks_json: '{\"source_record_pks\":[]}', "
            "inventory_row_count: 2, eligible_unit_count: 2, negative_control_count: 0, "
            "execution_allowed: false}) "
            "CREATE (boundary:RepairExecutionBoundary {manifest_digest: $manifest_digest, "
            "artifact_id: $artifact_id, artifact_manifest_hmac: $artifact_manifest_hmac, "
            "inventory_digest: $inventory_digest, boundary_digest: $boundary_digest, "
            "source_instance_id: $source_instance_id, control_instance_id: $control_instance_id, "
            "manifest_json: $manifest_json, source_record_pks_json: '{\"source_record_pks\":[]}', "
            "inventory_row_count: 2, eligible_unit_count: 2, "
            "negative_control_count: 0, execution_allowed: false}) "
            "CREATE (run)-[:QUALIFIED_WITH]->(boundary) "
            "CREATE (:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token: $token, "
            "revision: 1, state: $state, boundary_digest: $boundary_digest}) "
            "CREATE (:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, blocked: true, "
            "repair_control_run_id: $run_id, repair_control_owner_id: $owner_id, "
            "repair_control_token: $token, repair_control_revision: 1, "
            "repair_control_state: $state}) "
            "CREATE (:SourceRecord {source_record_pk: 'repair-310-allocation-domain', "
            "marker: 'before'})",
            **seed_parameters,
        ).consume()


def _execute_allocation(driver: Driver, parameters: dict[str, object]) -> dict[str, object] | None:
    with driver.session() as session:
        record = session.run(ALLOCATE_REPAIR_UNITS, **parameters).single()
        return None if record is None else dict(record)


def _allocation_state(driver: Driver) -> dict[str, object]:
    parameters = _allocation_parameters()
    with driver.session() as session:
        record = session.run(
            "MATCH (control:CrmDealRepairControl {run_id: $run_id}) "
            "OPTIONAL MATCH (unit:CrmDealRepairUnit {run_id: $run_id}) "
            "OPTIONAL MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id}) "
            "MATCH (record:SourceRecord {source_record_pk: 'repair-310-allocation-domain'}) "
            "RETURN control.state AS state, control.revision AS revision, "
            "count(unit) AS unit_count, "
            "completion.unit_count AS completion_count, record.marker AS marker",
            run_id=parameters["run_id"],
        ).single()
        assert record is not None
        return dict(record)


def test_allocation_atomic_success_zero_and_exact_replay(disposable_neo4j_driver: Driver) -> None:
    _seed_allocation(disposable_neo4j_driver)
    parameters = _allocation_parameters()
    assert _execute_allocation(disposable_neo4j_driver, parameters) == {
        "allocation_digest": parameters["allocation_digest"],
        "executable_count": 1,
        "unit_count": 1,
        "revision": 2,
        "replayed": False,
    }
    parameters["expected_revision"] = 2
    parameters["next_revision"] = 3
    assert _execute_allocation(disposable_neo4j_driver, parameters) == {
        "allocation_digest": parameters["allocation_digest"],
        "executable_count": 1,
        "unit_count": 1,
        "revision": 2,
        "replayed": True,
    }
    assert _allocation_state(disposable_neo4j_driver) == {
        "state": "allocated", "revision": 2, "unit_count": 1,
        "completion_count": 1, "marker": "before",
    }

    _seed_allocation(disposable_neo4j_driver)
    zero = _allocation_parameters()
    zero["units"] = []
    zero["approved_rows"] = [
        {
            "inventory_key": "inventory-1",
            "source_record_pk": "source-1",
            "inventory_fingerprint": "sha256:" + "e" * 64,
            "disposition": "blocked",
        },
        {
            "inventory_key": "inventory-2",
            "source_record_pk": "source-2",
            "inventory_fingerprint": "sha256:" + "a" * 64,
            "disposition": "investigate",
        },
    ]
    zero["allocation_digest"] = "sha256:" + "0" * 64
    assert _execute_allocation(disposable_neo4j_driver, zero) is not None
    assert _allocation_state(disposable_neo4j_driver)["unit_count"] == 0


@pytest.mark.parametrize(
    "failure",
    [
        "changed_replay",
        "duplicate",
        "ordinal",
        "ceiling",
        "boundary",
        "owner",
        "revision",
        "state",
    ],
)
def test_allocation_rejections_create_nothing_and_preserve_domain(
    disposable_neo4j_driver: Driver, failure: str
) -> None:
    state = "quiesced" if failure != "state" else "paused"
    owner = _OWNER if failure != "owner" else "other-owner"
    _seed_allocation(disposable_neo4j_driver, state=state, owner=owner)
    parameters = _allocation_parameters()
    if failure == "changed_replay":
        assert _execute_allocation(disposable_neo4j_driver, parameters) is not None
        parameters["expected_revision"] = 2
        parameters["next_revision"] = 3
        parameters["allocation_digest"] = "sha256:" + "1" * 64
    elif failure == "duplicate":
        units = parameters["units"]
        assert isinstance(units, list)
        units.append(dict(units[0]))
        rows = parameters["approved_rows"]
        assert isinstance(rows, list)
        parameters["approved_rows"] = [rows[0], rows[0]]
    elif failure == "ordinal":
        units = parameters["units"]
        assert isinstance(units, list)
        unit = units[0]
        assert isinstance(unit, dict)
        unit["sequence"] = 1
    elif failure == "ceiling":
        parameters["unit_ceiling"] = 0
    elif failure == "boundary":
        parameters["boundary_digest"] = "sha256:" + "2" * 64
    elif failure == "revision":
        parameters["expected_revision"] = 0

    assert _execute_allocation(disposable_neo4j_driver, parameters) is None
    expected_state = "allocated" if failure == "changed_replay" else state
    expected_revision = 2 if failure == "changed_replay" else 1
    expected_units = 1 if failure == "changed_replay" else 0
    assert _allocation_state(disposable_neo4j_driver) == {
        "state": expected_state, "revision": expected_revision, "unit_count": expected_units,
        "completion_count": expected_units if failure == "changed_replay" else None,
        "marker": "before",
    }


def _seed_stale_run(driver: Driver, *, orphan: bool = False, ambiguous: bool = False) -> None:
    _clear(driver)
    _seed_control(driver)
    with driver.session() as session:
        session.run(
            "MATCH (control:CrmDealRepairControl {run_id: $run_id}) "
            "MATCH (dispatch:BitrixDispatchControl {control_instance_id: $control_instance_id}) "
            "SET control.state = 'quiesced', control.revision = 2, "
            "dispatch.repair_control_state = 'quiesced', dispatch.repair_control_revision = 2 "
            "CREATE (source:SourceSystem {source_key: 'bitrix_chat', repair_test_marker: $run_id}) "
            "CREATE (stale:IngestRun {ingest_run_id: 'stale-attempt', "
            "control_instance_id: $control_instance_id, source_key: 'bitrix_chat', status: 'started'}) "
            "CREATE (stale)-[:FROM_SOURCE]->(source)",
            run_id=_RUN, control_instance_id=_CONTROL,
        ).consume()
        if not orphan:
            session.run(
                "MATCH (stale:IngestRun {ingest_run_id: 'stale-attempt'}) "
                "CREATE (logical:IngestionLogicalRun {control_instance_id: $control_instance_id, "
                "logical_run_id: 'stale-logical', source_key: 'bitrix_chat', "
                "bitrix_stream_key: 'crm_deals', status: 'running'}) "
                "CREATE (logical)-[:HAS_ATTEMPT]->(stale) "
                "CREATE (checkpoint:IngestionCheckpoint {control_instance_id: $control_instance_id, "
                "logical_run_id: 'stale-logical', phase: 'read', generation: 1, status: 'active'}) "
                "CREATE (checkpoint)-[:CHECKPOINT_FOR]->(logical) "
                "CREATE (:BitrixIngestionStream {source_key: 'bitrix_chat', "
                "control_instance_id: $control_instance_id, stream_key: 'crm_deals', "
                "logical_run_id: 'stale-logical', ingest_run_id: 'stale-attempt', "
                "attempt_generation: 1, stream_generation: 1, fencing_token: 1, status: 'active'})",
                control_instance_id=_CONTROL,
            ).consume()
        if ambiguous:
            session.run(
                "MATCH (stale:IngestRun {ingest_run_id: 'stale-attempt'}) "
                "CREATE (logical:IngestionLogicalRun {control_instance_id: $control_instance_id, "
                "logical_run_id: 'stale-logical-other', source_key: 'bitrix_chat', "
                "bitrix_stream_key: 'crm_deals', status: 'running'}) "
                "CREATE (logical)-[:HAS_ATTEMPT]->(stale)",
                control_instance_id=_CONTROL,
            ).consume()


def _stale_parameters(*, orphan: bool = False) -> dict[str, object]:
    return {
        "run_id": _RUN, "owner_id": _OWNER, "token": _TOKEN, "expected_revision": 2,
        "boundary_digest": _DIGEST, "stale_run_id": "stale-attempt",
        "stale_control_instance_id": _CONTROL, "stale_source_key": "bitrix_chat",
        "stale_status": "started",
        "logical_run_ids": [] if orphan else ["stale-logical"],
        "checkpoint_ids": [] if orphan else ["stale-logical|read|1"],
        "stream_keys": [] if orphan else ["crm_deals"],
    }


def _stale_state(driver: Driver) -> dict[str, object]:
    with driver.session() as session:
        record = session.run(
            "MATCH (stale:IngestRun {ingest_run_id: 'stale-attempt'}) "
            "MATCH (record:SourceRecord {source_record_pk: 'repair-310-test-domain'}) "
            "MATCH (dispatch:BitrixDispatchControl {control_instance_id: $control_instance_id}) "
            "RETURN stale.status AS status, stale.failure_category AS failure_category, "
            "dispatch.blocked AS blocked, dispatch.repair_control_owner_id AS owner, "
            "record.marker AS marker",
            control_instance_id=_CONTROL,
        ).single()
        assert record is not None
        return dict(record)


def test_stale_run_exact_owner_and_orphan_terminalize_without_crm_mutation(
    disposable_neo4j_driver: Driver,
) -> None:
    _seed_stale_run(disposable_neo4j_driver)
    with disposable_neo4j_driver.session() as session:
        assert dict(session.run(TERMINALIZE_STALE_REPAIR_RUN, **_stale_parameters()).single()) == {
            "ingest_run_id": "stale-attempt"
        }
    assert _stale_state(disposable_neo4j_driver) == {
        "status": "failed", "failure_category": "crm_deal_repair_stale_run",
        "blocked": True, "owner": _OWNER, "marker": "before",
    }

    _seed_stale_run(disposable_neo4j_driver, orphan=True)
    with disposable_neo4j_driver.session() as session:
        assert dict(session.run(TERMINALIZE_STALE_REPAIR_RUN, **_stale_parameters(orphan=True)).single()) == {
            "ingest_run_id": "stale-attempt"
        }
    assert _stale_state(disposable_neo4j_driver)["marker"] == "before"


@pytest.mark.parametrize("ambiguity", ["parent", "checkpoint", "stream", "boundary", "owner"])
def test_stale_run_ambiguity_or_stale_lease_rejects_without_partial_write(
    disposable_neo4j_driver: Driver, ambiguity: str
) -> None:
    _seed_stale_run(disposable_neo4j_driver, ambiguous=ambiguity == "parent")
    parameters = _stale_parameters()
    if ambiguity == "checkpoint":
        parameters["checkpoint_ids"] = ["stale-logical|other|1"]
    elif ambiguity == "stream":
        parameters["stream_keys"] = ["crm_activities"]
    elif ambiguity == "boundary":
        parameters["boundary_digest"] = "sha256:" + "b" * 64
    elif ambiguity == "owner":
        parameters["owner_id"] = "other-owner"
    with disposable_neo4j_driver.session() as session:
        assert session.run(TERMINALIZE_STALE_REPAIR_RUN, **parameters).single() is None
    assert _stale_state(disposable_neo4j_driver) == {
        "status": "started", "failure_category": None,
        "blocked": True, "owner": _OWNER, "marker": "before",
    }


def _boundary_proof_parameters() -> dict[str, object]:
    digest = "sha256:" + "a" * 64
    return {
        "run_id": _RUN, "owner_id": _OWNER, "token": _TOKEN, "expected_revision": 1,
        "boundary_digest": _DIGEST,
        "baseline_source_instance_id": "source-310",
        "baseline_control_instance_id": _CONTROL,
        "baseline_inventory_digest": digest, "baseline_inventory_row_count": 1,
        "baseline_eligible_unit_count": 1, "baseline_negative_control_count": 0,
        "baseline_source_records_digest": "sha256:" + "b" * 64,
        "baseline_source_instance_digest": "sha256:" + "c" * 64,
        "baseline_control_digest": "sha256:" + "d" * 64,
        "baseline_stale_run_evidence_digest": "sha256:" + "e" * 64,
        "authorized_control_digest": "sha256:" + "f" * 64,
        "authorized_stale_run_evidence_digest": "sha256:" + "0" * 64,
    }


def test_boundary_component_proof_persists_exact_baseline_and_authorized_post_state(
    disposable_neo4j_driver: Driver,
) -> None:
    _clear(disposable_neo4j_driver)
    _seed_control(disposable_neo4j_driver)
    parameters = _boundary_proof_parameters()
    with disposable_neo4j_driver.session() as session:
        assert dict(session.run(PERSIST_REPAIR_BOUNDARY_COMPONENT_PROOF, **parameters).single()) == {
            "run_id": _RUN
        }
        stored = session.run(
            "MATCH (control:CrmDealRepairControl {run_id: $run_id}) "
            "RETURN control.baseline_inventory_digest AS baseline, "
            "control.authorized_control_digest AS authorized",
            run_id=_RUN,
        ).single()
        assert stored is not None
        assert dict(stored) == {
            "baseline": parameters["baseline_inventory_digest"],
            "authorized": parameters["authorized_control_digest"],
        }
        changed = dict(parameters)
        changed["baseline_inventory_digest"] = "sha256:" + "9" * 64
        assert session.run(PERSIST_REPAIR_BOUNDARY_COMPONENT_PROOF, **changed).single() is None


def test_existing_unblocked_dispatch_is_atomically_closed_on_claim(
    disposable_neo4j_driver: Driver,
) -> None:
    _clear(disposable_neo4j_driver)
    with disposable_neo4j_driver.session() as session:
        session.run(
            "CREATE (:CrmDealRepairRun {run_id: $run_id, status: 'qualified', "
            "boundary_digest: $boundary_digest, execution_allowed: false}) "
            "CREATE (:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, blocked: false})",
            run_id=_RUN,
            boundary_digest=_DIGEST,
            control_instance_id=_CONTROL,
        ).consume()
        record = session.run(
            control_repository._WRITE_REPAIR_CONTROL,
            run_id=_RUN, owner_id=_OWNER, token=_TOKEN, expected_revision=0,
            revision=1, state="quiescing", prior_state=None, boundary_digest=_DIGEST,
            control_instance_id=_CONTROL, creating=True,
        ).single()
        assert record is not None
        dispatch = session.run(
            "MATCH (dispatch:BitrixDispatchControl {control_instance_id: $control_instance_id}) "
            "RETURN dispatch.blocked AS blocked, dispatch.block_reason AS reason, "
            "dispatch.repair_control_revision AS revision",
            control_instance_id=_CONTROL,
        ).single()
    assert dispatch is not None
    assert dict(dispatch) == {
        "blocked": True,
        "reason": "crm_deal_repair_quiescence",
        "revision": 1,
    }


def test_unexpected_drift_between_write_and_proof_rolls_back_all_control_changes(
    disposable_neo4j_driver: Driver,
) -> None:
    _clear(disposable_neo4j_driver)
    _seed_control(disposable_neo4j_driver)
    _seed_full_topology(disposable_neo4j_driver)
    parameters = _parameters()

    def work(tx: ManagedTransaction) -> None:
        assert tx.run(SUPERSEDE_CAPTURED_REPAIR_TOPOLOGY, **parameters).single() is not None
        tx.run(
            "MATCH (dispatch:BitrixDispatchControl {control_instance_id: $control_instance_id}) "
            "SET dispatch.repair_control_state = 'lost'",
            control_instance_id=_CONTROL,
        ).consume()
        proof = tx.run(
            PERSIST_REPAIR_TRANSACTION_AUTHORIZATION,
            run_id=_RUN, owner_id=_OWNER, token=_TOKEN, revision=2, state="quiesced",
            boundary_digest=_DIGEST, control_instance_id=_CONTROL,
            operation="supersede_topology", authorization_digest="sha256:" + "b" * 64,
            operation_capture_digest="sha256:" + "c" * 64,
            pre_control_digest="sha256:" + "d" * 64,
            post_control_digest="sha256:" + "e" * 64,
            pre_stale_run_evidence_digest="sha256:" + "f" * 64,
            post_stale_run_evidence_digest="sha256:" + "0" * 64,
        ).single()
        if proof is None:
            raise RuntimeError("injected dispatch drift rejected the authorization proof")

    with pytest.raises(RuntimeError, match="injected dispatch drift"):
        with disposable_neo4j_driver.session() as session:
            session.execute_write(work)
    with disposable_neo4j_driver.session() as session:
        record = session.run(
            "MATCH (control:CrmDealRepairControl {run_id: $run_id}) "
            "MATCH (stream:BitrixIngestionStream {control_instance_id: $control_instance_id}) "
            "MATCH (dispatch:BitrixDispatchControl {control_instance_id: $control_instance_id}) "
            "RETURN control.state AS state, control.revision AS revision, "
            "stream.status AS stream_status, stream.fencing_token AS fence, "
            "dispatch.repair_control_state AS dispatch_state",
            run_id=_RUN, control_instance_id=_CONTROL,
        ).single()
    assert record is not None
    assert dict(record) == {
        "state": "quiescing", "revision": 1, "stream_status": "active",
        "fence": 8, "dispatch_state": "quiescing",
    }



def test_publication_reservation_token_cas_and_repair_claim_exclusion(
    disposable_neo4j_driver: Driver,
) -> None:
    _clear(disposable_neo4j_driver)
    with disposable_neo4j_driver.session() as session:
        session.run(
            "CREATE (:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, blocked: false})",
            control_instance_id=_CONTROL,
        ).consume()
        parameters = {
            "control_instance_id": _CONTROL,
            "stream_scope": "crm_deals,crm_activities",
            "routing_identity_digest": "sha256:" + "1" * 64,
            "occurrence_generation_identity": "generation-1:2026-08-29",
            "reservation_token": "reservation-token-310",
        }
        pending = session.run(RESERVE_REPAIR_PUBLICATION, **parameters).single()
        assert pending is not None and dict(pending) == {
            "reservation_token": "reservation-token-310",
            "status": "pending",
            "publication_id": None,
            "is_exact_replay": False,
        }
        assert session.run(BEGIN_REPAIR_PUBLICATION, **{
            **parameters, "reservation_token": "wrong-token"
        }).single() is None
        assert session.run(BEGIN_REPAIR_PUBLICATION, **parameters).single() is not None
        assert session.run(PUBLISH_REPAIR_PUBLICATION, **{
            "control_instance_id": _CONTROL, "reservation_token": "wrong-token",
            "publication_id": "canvas-310"
        }).single() is None
        assert session.run(PUBLISH_REPAIR_PUBLICATION, **{
            "control_instance_id": _CONTROL, "reservation_token": "reservation-token-310",
            "publication_id": "canvas-310"
        }).single() is not None
        replay = session.run(
            RESERVE_REPAIR_PUBLICATION,
            **{**parameters, "reservation_token": "new-token-must-not-replace"},
        ).single()
        assert replay is not None and dict(replay) == {
            "reservation_token": "reservation-token-310",
            "status": "published",
            "publication_id": "canvas-310",
            "is_exact_replay": True,
        }
        state = session.run(
            "MATCH (reservation:BitrixRepairPublicationReservation {reservation_token: $token}) "
            "RETURN reservation.status AS status, reservation.canvas_or_workflow_id AS publication_id",
            token="reservation-token-310",
        ).single()
    assert state is not None and dict(state) == {"status": "published", "publication_id": "canvas-310"}


def test_pending_publication_reservation_rejects_repair_claim_without_touching_dispatch(
    disposable_neo4j_driver: Driver,
) -> None:
    _clear(disposable_neo4j_driver)
    with disposable_neo4j_driver.session() as session:
        session.run(
            "CREATE (:CrmDealRepairRun {run_id: $run_id, status: 'qualified', "
            "boundary_digest: $boundary_digest, execution_allowed: false, "
            "control_instance_id: $control_instance_id}) "
            "CREATE (:BitrixDispatchControl {source_key: 'bitrix_chat', "
            "control_instance_id: $control_instance_id, blocked: false}) "
            "CREATE (:BitrixRepairPublicationReservation {control_instance_id: $control_instance_id, "
            "routing_identity_digest: $digest, occurrence_generation_identity: 'pending-1', "
            "reservation_token: 'pending-token', stream_scope: 'crm_deals', status: 'pending', "
            "execution_allowed: false})",
            run_id=_RUN, boundary_digest=_DIGEST, control_instance_id=_CONTROL,
            digest="sha256:" + "2" * 64,
        ).consume()
        claim = session.run(
            control_repository._WRITE_REPAIR_CONTROL,
            run_id=_RUN, owner_id=_OWNER, token=_TOKEN, expected_revision=0, revision=1,
            state="quiescing", prior_state=None, boundary_digest=_DIGEST,
            control_instance_id=_CONTROL, creating=True,
        ).single()
        dispatch = session.run(
            "MATCH (dispatch:BitrixDispatchControl {control_instance_id: $control_instance_id}) "
            "RETURN dispatch.blocked AS blocked", control_instance_id=_CONTROL
        ).single()
    assert claim is None
    assert dispatch is not None and dispatch["blocked"] is False
