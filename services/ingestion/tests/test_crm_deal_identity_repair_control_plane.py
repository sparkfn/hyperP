"""Focused #310 topology-quiescence contract coverage; no Neo4j service is contacted."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar, cast

from src.crm_deal_identity_repair.control_models import (
    RepairBoundaryComponentProof,
    RepairControlLease,
)
from src.crm_deal_identity_repair.quiescence import RepairQuiescenceRequest, RepairQuiescenceService
from src.crm_deal_identity_repair.task_inspection import (
    RepairObservedTask,
    RepairTaskIdentity,
    RepairTaskInspection,
)
from src.graph import crm_deal_identity_repair_control as control_repository
from src.graph.client import Neo4jClient
from src.graph.queries import crm_deal_identity_repair_ledger as queries

_DIGEST = "sha256:" + "a" * 64
T = TypeVar("T")


@dataclass
class _Result:
    record: dict[str, object] | None

    def single(self) -> dict[str, object] | None:
        return self.record


class _Transaction:
    def __init__(self, record: dict[str, object] | None) -> None:
        self.record = record
        self.query = ""
        self.parameters: dict[str, object] = {}

    def run(self, query: str, **parameters: object) -> _Result:
        self.query = query
        self.parameters = parameters
        return _Result(self.record)


class _Client:
    def __init__(
        self,
        read_record: dict[str, object] | None,
        write_record: dict[str, object] | None,
    ) -> None:
        self.read_transaction = _Transaction(read_record)
        self.write_transaction = _Transaction(write_record)

    def execute_read(self, work: Callable[[_Transaction], T]) -> T:
        return work(self.read_transaction)

    def execute_write(self, work: Callable[[_Transaction], T]) -> T:
        return work(self.write_transaction)


def _lease() -> RepairControlLease:
    return RepairControlLease("run-310", "owner-310", "token-310", 1, "quiescing", _DIGEST)


def _inventory_record() -> dict[str, object]:
    return {
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


def test_captured_topology_repository_preserves_all_exact_identity_and_fence_values() -> None:
    client = _Client(_inventory_record(), {"revision": 2})
    repository = control_repository.CrmDealRepairControlRepository(
        cast(Neo4jClient, client), "control-310"
    )
    topology = repository.inventory_topology(_lease())

    assert topology.as_parameters() == _inventory_record()
    assert client.read_transaction.query == queries.INVENTORY_REPAIR_TOPOLOGY
    assert client.read_transaction.parameters["owner_id"] == "owner-310"
    assert client.read_transaction.parameters["expected_revision"] == 1

    parameters = topology.as_parameters()
    assert parameters["stream_ids"] == _inventory_record()["stream_ids"]
    assert parameters["checkpoint_ids"] == _inventory_record()["checkpoint_ids"]
    assert "repair_control_revision = $expected_revision" in queries.SUPERSEDE_CAPTURED_REPAIR_TOPOLOGY


def test_supersede_query_preserves_a_row_for_every_empty_capture_category() -> None:
    query = queries.SUPERSEDE_CAPTURED_REPAIR_TOPOLOGY
    for category in (
        "logical_run_ids",
        "ingest_run_ids",
        "checkpoint_ids",
        "stream_ids",
        "generation_ids",
        "publication_ids",
    ):
        assert f"UNWIND CASE WHEN size(${category}) = 0 THEN [NULL] ELSE ${category} END" in query
    assert "UNWIND $logical_run_ids" not in query
    assert "UNWIND $ingest_run_ids" not in query
    assert "UNWIND $checkpoint_ids" not in query
    assert "UNWIND $stream_ids" not in query
    assert "UNWIND $generation_ids" not in query
    assert "UNWIND $publication_ids" not in query


def test_supersede_query_validates_every_capture_before_any_mutation_and_cas_transition() -> None:
    query = queries.SUPERSEDE_CAPTURED_REPAIR_TOPOLOGY
    first_write = query.index("SET logical.stop_requested")
    for count in (
        "logical_count = size($logical_run_ids)",
        "ingest_count = size($ingest_run_ids)",
        "checkpoint_count = size($checkpoint_ids)",
        "stream_count = size($stream_ids)",
        "generation_count = size($generation_ids)",
        "publication_count = size($publication_ids)",
    ):
        assert query.index(count) < first_write
    for guard in (
        "repair_control_owner_id: $owner_id",
        "repair_control_token: $token",
        "repair_control_revision: $expected_revision",
        "state: 'quiescing'",
        "boundary_digest: $boundary_digest",
        "stream.fencing_token = captured.fencing_token",
        "stream.stream_generation = captured.stream_generation",
        "stream.fencing_token = captured.fencing_token + 1",
    ):
        assert guard in query


def test_supersede_query_scopes_only_captured_control_metadata_and_excludes_stage_history() -> None:
    query = queries.SUPERSEDE_CAPTURED_REPAIR_TOPOLOGY
    assert "crm_stage_history" not in query
    assert "SourceRecord" not in query
    assert "Person" not in query
    assert "control_instance_id: run.control_instance_id" in query
    assert "BitrixBackfillGeneration" in query
    assert "BitrixBackfillDispatchOutbox" in query
    assert "IngestionCheckpoint" in query



def test_allocation_query_requires_full_approved_boundary_and_exact_replay_guards() -> None:
    query = queries.ALLOCATE_REPAIR_UNITS
    for required in (
        "QUALIFIED_WITH",
        "artifact_manifest_hmac: $artifact_manifest_hmac",
        "manifest_json: $manifest_json",
        "approved_rows",
        "qualified_source_record_pks",
        "source_record_pk IN $qualified_source_record_pks",
        "distinct_inventory_key_count",
        "distinct_fingerprint_count",
        "$units[index].sequence = index",
        "persisted_units = $units",
        "overlay_digest = $overlay_digest",
        "approval_reference = $approval_reference",
        "control.state = 'quiesced'",
        "control.state = 'allocated'",
        "execution_allowed: false",
    ):
        assert required in query
    assert "SourceRecord" not in query
    assert "Person" not in query


def test_allocation_query_validates_before_creating_any_unit_or_completion() -> None:
    query = queries.ALLOCATE_REPAIR_UNITS
    first_write = query.index("CREATE (:CrmDealRepairUnit")
    for guard in (
        "size($approved_rows) = run.inventory_row_count",
        "size($approved_rows) = size($qualified_source_record_pks)",
        "size($units) = size([row IN $approved_rows WHERE row.disposition = 'executable'])",
        "size($units) <= $unit_ceiling",
        "size($units) <= run.eligible_unit_count",
        "size($approved_rows) = distinct_inventory_key_count",
        "size($approved_rows) = distinct_fingerprint_count",
        "size($units) = distinct_unit_count",
        "size(persisted_units) = 0",
    ):
        assert query.index(guard) < first_write


class _Broker:
    def __init__(self, queued: bool | None) -> None:
        self.queued = queued

    def has_queued_delivery(
        self, _tasks: tuple[RepairTaskIdentity, ...], _timeout_seconds: float
    ) -> bool | None:
        return self.queued


def _task() -> RepairTaskIdentity:
    return RepairTaskIdentity("task-310", "src.bitrix_backfill", "ingestion", "sha256:kwargs")


def test_task_absence_proof_rejects_every_runtime_state_and_missing_or_unknown_replies() -> None:
    task = _task()
    clean = RepairTaskInspection(responders=("worker-a", "worker-b"))
    assert clean.proves_absence(
        expected_workers=("worker-a", "worker-b"), broker=_Broker(False), tasks=(task,),
        timeout_seconds=1.0,
    )
    for field in ("active", "reserved", "scheduled", "queued"):
        inspection = RepairTaskInspection(
            responders=("worker-a", "worker-b"),
            **{field: (RepairObservedTask(task.task_id, None, None, None),)},
        )
        assert not inspection.proves_absence(
            expected_workers=("worker-a", "worker-b"), broker=_Broker(False), tasks=(task,),
            timeout_seconds=1.0,
        )
    for inspection in (
        RepairTaskInspection(responders=("worker-a",)),
        RepairTaskInspection(responders=("worker-a", "worker-b"), timed_out=True),
        RepairTaskInspection(responders=("worker-a", "worker-b"), inspection_failed=True),
        RepairTaskInspection(responders=("worker-a", "worker-b"), unknown_task_ids=("unknown",)),
        RepairTaskInspection(
            responders=("worker-a", "worker-b"),
            active=(RepairObservedTask(None, "src.unknown", None, None),),
        ),
        RepairTaskInspection(responders=("worker-a", "worker-b"), reply_errors=("worker error",)),
    ):
        assert not inspection.proves_absence(
            expected_workers=("worker-a", "worker-b"), broker=_Broker(False), tasks=(task,),
            timeout_seconds=1.0,
        )
    assert not clean.proves_absence(
        expected_workers=("worker-a", "worker-b"), broker=_Broker(None), tasks=(task,),
        timeout_seconds=1.0,
    )


def test_quiesced_topology_readback_revalidates_each_captured_post_state_and_fence() -> None:
    query = queries.VERIFY_QUIESCED_REPAIR_TOPOLOGY
    for category in (
        "logical_run_ids", "ingest_run_ids", "checkpoint_ids", "stream_ids",
        "generation_ids", "publication_ids",
    ):
        assert f"size(${category})" in query
    for required in (
        "state: 'quiesced'", "repair_control_revision: $expected_revision",
        "stop_requested: true", "stream_generation: captured.stream_generation + 1",
        "fencing_token: captured.fencing_token + 1", "status: 'superseded'",
    ):
        assert required in query


def test_stale_run_query_requires_exact_source_owner_checkpoint_stream_and_cas() -> None:
    query = queries.TERMINALIZE_STALE_REPAIR_RUN
    for required in (
        "FROM_SOURCE",
        "control_instance_id: $stale_control_instance_id",
        "source_key: $stale_source_key",
        "status: $stale_status",
        "size(actual_logical_run_ids) <= 1",
        "size(produced_checkpoint_ids) = 0",
        "size(logical_checkpoint_ids) = size($checkpoint_ids)",
        "size(actual_stream_keys) = size($stream_keys)",
        "repair_control_owner_id: $owner_id",
        "repair_control_token: $token",
        "repair_control_revision: $expected_revision",
        "failure_category = 'crm_deal_repair_stale_run'",
    ):
        assert required in query
    assert "SourceRecord" not in query
    assert "Person" not in query


def test_combined_status_query_is_read_only_and_keeps_qualification_separate() -> None:
    query = queries.READ_REPAIR_CONTROL_STATUS
    assert "SET " not in query
    assert "CREATE " not in query
    for required in (
        "allocation_unit_count", "dispatch_blocked", "task_proof_state",
        "stale_run_proof_count", "topology_active_count", "topology_superseded_count",
    ):
        assert required in query


def test_control_write_preserves_an_unrelated_block_and_never_merges_before_ownership_guard() -> None:
    query = control_repository._WRITE_REPAIR_CONTROL
    assert query.index("WHERE (") < query.index("MERGE (next")
    assert "repair_control_run_id = $run_id" in query
    assert "repair_control_token = $token" in query
    assert "dispatch.blocked = false" in query
    assert query.index("MERGE (dispatch_write") > query.index("WHERE (")


def test_every_mutating_operation_uses_the_single_transaction_proof_wrapper() -> None:
    source = Path(control_repository.__file__).read_text(encoding="utf-8")
    for operation in (
        'operation="supersede_topology"',
        'operation="terminalize_stale_run"',
        'operation=f"record_task_proof:{proof_state}"',
        'operation="allocate"',
        'operation="claim" if creating else "transition"', 
    ):
        assert operation in source
    assert source.count("self._execute_proven_write(") >= 5
    assert "pre = repair_boundary_snapshot_for_run_transaction" in source
    assert "post = repair_boundary_snapshot_for_run_transaction" in source
    assert "immutable_matches(post_proof)" in source
    assert "PERSIST_REPAIR_TRANSACTION_AUTHORIZATION" in source


def test_proven_write_contract_revalidates_exact_operation_post_states() -> None:
    source = Path(control_repository.__file__).read_text(encoding="utf-8")
    for verifier in (
        "VERIFY_QUIESCED_REPAIR_TOPOLOGY",
        "_VERIFY_TERMINALIZED_STALE_RUN",
        "_VERIFY_REPAIR_ALLOCATION",
        "_VERIFY_REPAIR_CONTROL_POST_STATE",
    ):
        assert verifier in source
    assert "expected_control_change" not in source
    assert "expected_stale_change" not in source


def test_service_cannot_bless_a_later_arbitrary_boundary_snapshot() -> None:
    source = Path("services/ingestion/src/crm_deal_identity_repair/quiescence.py").read_text(
        encoding="utf-8"
    )
    assert "_persist_authorized_boundary" not in source
    assert "persist_boundary_component_proof" not in source
    assert "read_boundary_component_proof" in source


class _Inspector:
    def __init__(self, inspection: RepairTaskInspection) -> None:
        self.inspection = inspection
        self.calls = 0

    def inspect(
        self, _workers: tuple[str, ...], _tasks: tuple[RepairTaskIdentity, ...], _timeout: float
    ) -> RepairTaskInspection:
        self.calls += 1
        return self.inspection


class _Boundary:
    def __init__(self, boundary_digest: str, *, drift_after: int | None = None) -> None:
        self.boundary_digest = boundary_digest
        self.calls = 0
        self.drift_after = drift_after

    def get_qualification(self, _repair_id: str) -> object:
        return SimpleNamespace(
            run_id="run-310", boundary_digest=self.boundary_digest,
            source_instance_id="source-310", control_instance_id="control-310",
        )

    def source_record_pks(self, _repair_id: str) -> tuple[str, ...]:
        return ("source-record-310",)

    def snapshot(self, **_kwargs: object) -> object:
        self.calls += 1
        digest = self.boundary_digest
        inventory_digest = "sha256:" + "a" * 64
        if self.drift_after is not None and self.calls > self.drift_after:
            digest = "sha256:" + "d" * 64
            inventory_digest = "sha256:" + "f" * 64
        return SimpleNamespace(
            boundary_digest=digest,
            source_instance_id="source-310",
            control_instance_id="control-310",
            inventory_digest=inventory_digest,
            inventory_row_count=1,
            eligible_unit_count=1,
            negative_control_count=0,
            source_records_digest="sha256:" + "b" * 64,
            source_instance_digest="sha256:" + "d" * 64,
            control_digest="sha256:" + "c" * 64,
            stale_run_evidence_digest="sha256:" + "e" * 64,
        )


class _Control:
    def __init__(self, lease: RepairControlLease) -> None:
        self.lease = lease
        self.transitions: list[RepairControlLease] = []
        self.proofs: list[tuple[str, str | None]] = []
        self.superseded = 0
        self.boundary_proof: tuple[RepairBoundaryComponentProof, str, str] = (
            RepairBoundaryComponentProof(
                source_instance_id="source-310",
                control_instance_id="control-310",
                inventory_digest="sha256:" + "a" * 64,
                inventory_row_count=1,
                eligible_unit_count=1,
                negative_control_count=0,
                source_records_digest="sha256:" + "b" * 64,
                source_instance_digest="sha256:" + "d" * 64,
                control_digest="sha256:" + "c" * 64,
                stale_run_evidence_digest="sha256:" + "e" * 64,
            ),
            "sha256:" + "c" * 64,
            "sha256:" + "e" * 64,
        )

    def claim(self, lease: RepairControlLease, expected_revision: int) -> RepairControlLease:
        assert expected_revision == 0
        self.lease = lease
        return lease

    def transition(self, lease: RepairControlLease, expected_revision: int) -> RepairControlLease:
        assert expected_revision == self.lease.revision
        self.lease = lease
        self.transitions.append(lease)
        return lease

    def inventory_topology(self, _lease: RepairControlLease) -> object:
        from src.crm_deal_identity_repair.control_models import RepairTopologyCapture
        return RepairTopologyCapture((), (), (), (), (), ())

    def supersede_topology(
        self, lease: RepairControlLease, expected_revision: int, _topology: object
    ) -> RepairControlLease:
        assert expected_revision == lease.revision
        self.superseded += 1
        self.lease = RepairControlLease(
            lease.run_id, lease.owner_id, lease.token, lease.revision + 1, "quiesced",
            lease.boundary_digest,
        )
        return self.lease

    def verify_quiesced_topology(self, _lease: RepairControlLease, _topology: object) -> bool:
        return True

    def inventory_stale_run(self, _lease: RepairControlLease, _stale_run_id: str) -> object:
        raise AssertionError("stale proof is not expected in this focused control test")

    def terminalize_stale_run(self, *_args: object) -> None:
        raise AssertionError("stale terminalization is not expected in this focused control test")

    def read(self, _run_id: str) -> RepairControlLease:
        return self.lease

    def record_task_proof(self, _lease: RepairControlLease, state: str, reason: str | None) -> None:
        self.proofs.append((state, reason))

    def read_boundary_component_proof(
        self, _run_id: str
    ) -> tuple[RepairBoundaryComponentProof, str, str]:
        return self.boundary_proof



def test_quiesce_and_pause_resume_fail_closed_without_any_ingestion_restart() -> None:
    task = _task()
    clean = RepairTaskInspection(responders=("worker-a",))
    control = _Control(RepairControlLease("run-310", "owner-310", "token-310", 1, "quiescing", _DIGEST))
    service = RepairQuiescenceService(control, _Boundary(_DIGEST), _Inspector(clean), _Broker(False))
    quiesced = service.quiesce(RepairQuiescenceRequest(
        repair_id="repair-310", lease=control.lease, expected_revision=0,
        expected_workers=("worker-a",), tasks=(task,), timeout_seconds=1.0,
    ))
    assert quiesced.state == "quiesced"
    assert control.superseded == 1
    assert control.proofs == [("absent", None)]
    paused = service.pause("repair-310", quiesced, quiesced.revision)
    assert paused.state == "paused" and paused.prior_state == "quiesced"
    assert service.pause("repair-310", paused, paused.revision) == paused
    resumed = service.resume("repair-310", paused, paused.revision, ("worker-a",), (task,), 1.0)
    assert resumed.state == "quiesced"
    assert service.resume("repair-310", resumed, resumed.revision, ("worker-a",), (task,), 1.0) == resumed
    assert not hasattr(control, "restart_ingestion")


def test_quiesce_boundary_drift_after_topology_proof_transitions_to_lost_with_block_retained() -> None:
    task = _task()
    control = _Control(RepairControlLease("run-310", "owner-310", "token-310", 1, "quiescing", _DIGEST))
    service = RepairQuiescenceService(
        control, _Boundary(_DIGEST, drift_after=4),
        _Inspector(RepairTaskInspection(responders=("worker-a",))), _Broker(False),
    )
    try:
        service.quiesce(RepairQuiescenceRequest(
            repair_id="repair-310", lease=control.lease, expected_revision=0,
            expected_workers=("worker-a",), tasks=(task,), timeout_seconds=1.0,
        ))
    except RuntimeError as error:
        assert "boundary drift" in str(error)
    else:
        raise AssertionError("boundary drift must fail closed")
    assert control.lease.state == "lost"
    assert control.proofs[-1][0] == "lost"


def test_boundary_component_proof_query_binds_full_baseline_and_only_authorized_post_digests() -> None:
    query = queries.PERSIST_REPAIR_BOUNDARY_COMPONENT_PROOF
    for required in (
        "baseline_inventory_digest", "baseline_source_records_digest",
        "baseline_source_instance_digest", "baseline_control_digest",
        "baseline_stale_run_evidence_digest", "authorized_control_digest",
        "authorized_stale_run_evidence_digest", "repair_control_owner_id: $owner_id",
        "repair_control_revision: $expected_revision",
    ):
        assert required in query
    assert "SourceRecord" not in query
    assert "Person" not in query


def test_allocation_query_requires_full_classified_overlay_and_only_allocates_executable_rows() -> None:
    query = queries.ALLOCATE_REPAIR_UNITS
    assert "row.disposition IN ['executable', 'blocked', 'investigate']" in query
    assert "single(row IN $approved_rows WHERE row.source_record_pk = source_record_pk)" in query
    assert "row.disposition <> 'executable'" in query
    assert "WHERE row.disposition = 'executable' | row.inventory_fingerprint" in query


def test_control_plane_queries_never_write_crm_domain_labels() -> None:
    writes = (
        control_repository.CrmDealRepairControlRepository._write,
        queries.SUPERSEDE_CAPTURED_REPAIR_TOPOLOGY,
        queries.TERMINALIZE_STALE_REPAIR_RUN,
        queries.ALLOCATE_REPAIR_UNITS,
    )
    source = "\n".join(
        item if isinstance(item, str) else item.__doc__ or "" for item in writes
    )
    for label in ("Person", "SourceRecord", "Identifier", "LINKED_TO", "IDENTIFIED_BY"):
        assert label not in source


def test_dispatch_claim_closes_existing_unblocked_row_and_rejects_stale_or_illegal_state() -> None:
    query = control_repository._WRITE_REPAIR_CONTROL
    for expected in (
        "dispatch.blocked = false",
        "dispatch.repair_control_run_id IS NULL",
        "dispatch_write.blocked = true",
        "dispatch_write.block_reason = 'crm_deal_repair_quiescence'",
        "dispatch.repair_control_revision = $expected_revision",
        "dispatch.repair_control_state = control.state",
        "control.state = 'quiescing' AND $state = 'quiesced'",
        "control.state = 'paused' AND $state = control.prior_state",
        "$state = 'lost'",
    ):
        assert expected in query
    assert "control.state = 'lost' AND" not in query


def test_topology_supersession_is_active_only_and_preserves_prior_evidence() -> None:
    query = queries.SUPERSEDE_CAPTURED_REPAIR_TOPOLOGY
    for expected in (
        "logical.status IN ['active', 'running', 'started', 'queued', 'pending', 'backfilling']",
        "attempt.repair_prior_status = attempt.status",
        "checkpoint.repair_prior_status = checkpoint.status",
        "stream.repair_prior_fencing_token = stream.fencing_token",
        "stream.repair_prior_stream_generation = stream.stream_generation",
        "generation.repair_prior_status = generation.status",
        "publication.repair_prior_status = publication.status",
    ):
        assert expected in query
    verify = queries.VERIFY_QUIESCED_REPAIR_TOPOLOGY
    assert "repair_prior_fencing_token: captured.fencing_token" in verify
    assert "repair_prior_status: captured.status" in verify


def test_publication_reservation_queries_are_exact_token_cas_and_block_repair_claim() -> None:
    reserve = queries.RESERVE_REPAIR_PUBLICATION
    begin = queries.BEGIN_REPAIR_PUBLICATION
    publish = queries.PUBLISH_REPAIR_PUBLICATION
    claim = control_repository._WRITE_REPAIR_CONTROL
    for required in (
        "dispatch.blocked = false",
        "reservation_token = $reservation_token",
        "status: 'pending'",
        "status: 'publishing'",
        "is_exact_replay",
        "publication_id",
        "execution_allowed = false",
    ):
        assert required in "\n".join((reserve, begin, publish))
    assert "uncertain_reservation_count = 0" in claim
    assert "reservation.status IN ['pending', 'publishing']" in claim


def test_publication_ordering_reserves_before_markers_windows_canvas_and_broker_apply() -> None:
    scheduled = Path("services/ingestion/src/scheduled_ingestion_tasks.py").read_text(
        encoding="utf-8"
    )
    backfill = Path("services/ingestion/src/bitrix_backfill_tasks.py").read_text(
        encoding="utf-8"
    )
    legacy_dispatch = scheduled[scheduled.index("def dispatch_ingestion_group_task("):]
    assert legacy_dispatch.index("_reserve_legacy_bitrix_publication(") < legacy_dispatch.index(
        "_claim_dispatch(marker_key, task_id)"
    )
    assert "_read_legacy_bitrix_publication" in legacy_dispatch
    assert "_reconcile_legacy_marker" in legacy_dispatch
    assert backfill.index("reserve_generation_publication(") < backfill.index(
        "canvas = build_generation_canvas("
    )
    assert backfill.index("_begin_generation_publication") < backfill.index(
        "canvas.apply_async()"
    )
    assert backfill.index("canvas.apply_async()") < backfill.index(
        "_mark_generation_published"
    )


def test_qualification_executes_manifest_write_before_exact_admission_row_persistence() -> None:
    source = Path("services/ingestion/src/graph/crm_deal_identity_repair_ledger.py").read_text(
        encoding="utf-8"
    )
    qualification = source[source.index("def _qualify_transaction("):source.index("def _qualification_parameters(")]
    assert qualification.index("tx.run(\n        QUALIFY_REPAIR_RUN") < qualification.index(
        "tx.run(\n        PERSIST_QUALIFIED_INVENTORY_ROWS"
    )
    assert "admission_inventory = collect_repair_inventory" in qualification
    assert "inventory_digest(admission_items) != admission_snapshot.inventory_digest" in qualification
    assert "for item in admission_items" in qualification


def test_stage_history_only_generation_bypasses_repair_reservation_and_mixed_scope_is_bound() -> None:
    from src.bitrix_backfill_tasks import _repair_stream_scope

    class _Entry:
        def __init__(self, stream_key: str) -> None:
            self.stream_key = stream_key
            self.executes = True

    assert _repair_stream_scope((_Entry("crm_stage_history"),)) == ""
    assert _repair_stream_scope((_Entry("crm_stage_history"), _Entry("crm_deals"))) == "crm_deals"
