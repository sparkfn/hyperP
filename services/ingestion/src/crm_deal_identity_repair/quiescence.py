"""Fail-closed #310 quiescence, pause, and resume orchestration; never dispatches work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.crm_deal_identity_repair.control_models import (
    RepairBoundaryComponentProof,
    RepairControlLease,
    RepairStaleRunProof,
    RepairTopologyCapture,
)
from src.crm_deal_identity_repair.execution_models import (
    RepairBoundarySnapshot,
    RepairQualificationRun,
)
from src.crm_deal_identity_repair.task_inspection import (
    RepairBrokerInspector,
    RepairTaskIdentity,
    RepairTaskInspector,
)


class RepairControlRepository(Protocol):
    """CAS-only metadata repository. It must never mutate CRM domain evidence."""

    def claim(self, lease: RepairControlLease, expected_revision: int) -> RepairControlLease: ...

    def transition(
        self, lease: RepairControlLease, expected_revision: int
    ) -> RepairControlLease: ...

    def inventory_topology(self, lease: RepairControlLease) -> RepairTopologyCapture: ...

    def supersede_topology(
        self, lease: RepairControlLease, expected_revision: int, topology: RepairTopologyCapture
    ) -> RepairControlLease: ...

    def verify_quiesced_topology(
        self, lease: RepairControlLease, topology: RepairTopologyCapture
    ) -> bool: ...

    def inventory_stale_run(
        self, lease: RepairControlLease, stale_run_id: str
    ) -> RepairStaleRunProof: ...

    def terminalize_stale_run(
        self, lease: RepairControlLease, expected_revision: int, proof: RepairStaleRunProof
    ) -> None: ...

    def read(self, run_id: str) -> RepairControlLease | None: ...

    def record_task_proof(
        self, lease: RepairControlLease, proof_state: str, stop_reason: str | None
    ) -> None: ...

    def read_boundary_component_proof(
        self, run_id: str
    ) -> tuple[RepairBoundaryComponentProof, str, str] | None: ...


class RepairBoundaryRepository(Protocol):
    """#300 immutable qualification and snapshot boundary, read only in #310."""

    def get_qualification(self, repair_id: str) -> RepairQualificationRun | None: ...

    def source_record_pks(self, repair_id: str) -> tuple[str, ...]: ...

    def snapshot(
        self,
        *,
        source_instance_id: str,
        control_instance_id: str,
        source_record_pks: tuple[str, ...],
    ) -> RepairBoundarySnapshot: ...


@dataclass(frozen=True)
class RepairQuiescenceRequest:
    """Bounded evidence inputs; inspectors are injected and never contact live services here."""

    repair_id: str
    lease: RepairControlLease
    expected_revision: int
    expected_workers: tuple[str, ...]
    tasks: tuple[RepairTaskIdentity, ...]
    timeout_seconds: float
    stale_run_id: str | None = None


class RepairQuiescenceService:
    """Coordinates exact ownership proof, topology supersession, and absence proof only."""

    def __init__(
        self,
        repository: RepairControlRepository,
        boundary: RepairBoundaryRepository,
        inspector: RepairTaskInspector,
        broker: RepairBrokerInspector,
    ) -> None:
        self._repository = repository
        self._boundary = boundary
        self._inspector = inspector
        self._broker = broker

    def quiesce(self, request: RepairQuiescenceRequest) -> RepairControlLease:
        """Quiesce frozen topology and bind permitted evidence evolution to the owner lease."""
        if request.lease.state != "quiescing":
            raise ValueError("quiescence must begin in quiescing state")
        self._initial_boundary(request.repair_id, request.lease)
        claimed = self._repository.claim(request.lease, request.expected_revision)
        try:
            topology = self._repository.inventory_topology(claimed)
            self._assert_authorized_boundary(request.repair_id, claimed)
            quiesced = self._repository.supersede_topology(claimed, claimed.revision, topology)
            inspection = self._inspector.inspect(
                request.expected_workers, request.tasks, request.timeout_seconds
            )
            if not inspection.proves_absence(
                expected_workers=request.expected_workers,
                broker=self._broker,
                tasks=request.tasks,
                timeout_seconds=request.timeout_seconds,
            ):
                self._repository.record_task_proof(quiesced, "failed", "task_absence_not_proven")
                return self._lost(request.repair_id, quiesced, "task_absence_not_proven")
            self._repository.record_task_proof(quiesced, "absent", None)
            if request.stale_run_id is not None:
                proof = self._repository.inventory_stale_run(quiesced, request.stale_run_id)
                self._repository.terminalize_stale_run(quiesced, quiesced.revision, proof)
            if not self._repository.verify_quiesced_topology(quiesced, topology):
                return self._lost(
                    request.repair_id,
                    quiesced,
                    "topology_readback_changed",
                )
            self._assert_authorized_boundary(request.repair_id, quiesced)
            readback = self._repository.read(quiesced.run_id)
            if readback != quiesced:
                return self._lost(request.repair_id, quiesced, "control_readback_changed")
            return quiesced
        except Exception:
            current = self._repository.read(claimed.run_id)
            if current is not None and self._same_owner(claimed, current):
                self._repository.record_task_proof(current, "failed", "quiescence_proof_lost")
                self._lost(request.repair_id, current, "quiescence_proof_lost")
            raise

    def pause(
        self, repair_id: str, lease: RepairControlLease, expected_revision: int
    ) -> RepairControlLease:
        """Pause owned control without releasing dispatch or scheduling work."""
        if expected_revision != lease.revision:
            raise RuntimeError("repair control revision was lost before pause")
        self._assert_authorized_boundary(repair_id, lease)
        if lease.state == "paused":
            return lease
        if lease.state not in {"quiesced", "allocated"}:
            raise ValueError("only quiesced or allocated repair control can pause")
        paused = RepairControlLease(
            lease.run_id, lease.owner_id, lease.token, expected_revision + 1, "paused",
            lease.boundary_digest, prior_state=lease.state,
        )
        return self._repository.transition(paused, expected_revision)

    def resume(
        self,
        repair_id: str,
        lease: RepairControlLease,
        expected_revision: int,
        expected_workers: tuple[str, ...],
        tasks: tuple[RepairTaskIdentity, ...],
        timeout_seconds: float,
    ) -> RepairControlLease:
        """Restore only a persisted state after a fresh absence proof; never restart ingestion."""
        if expected_revision != lease.revision:
            raise RuntimeError("repair control revision was lost before resume")
        self._assert_authorized_boundary(repair_id, lease)
        if lease.state not in {"paused", "quiesced", "allocated"}:
            raise ValueError("only a paused repair control can resume")
        inspection = self._inspector.inspect(expected_workers, tasks, timeout_seconds)
        if not inspection.proves_absence(
            expected_workers=expected_workers, broker=self._broker, tasks=tasks,
            timeout_seconds=timeout_seconds,
        ):
            self._repository.record_task_proof(lease, "failed", "resume_task_absence_not_proven")
            return self._lost(repair_id, lease, "resume_task_absence_not_proven")
        self._repository.record_task_proof(lease, "absent", None)
        if lease.state in {"quiesced", "allocated"}:
            return lease
        if lease.prior_state is None:
            raise RuntimeError("paused repair control has no resumable state")
        resumed = RepairControlLease(
            lease.run_id, lease.owner_id, lease.token, expected_revision + 1, lease.prior_state,
            lease.boundary_digest,
        )
        return self._repository.transition(resumed, expected_revision)

    def _initial_boundary(
        self, repair_id: str, lease: RepairControlLease
    ) -> RepairBoundaryComponentProof:
        run = self._qualified_run(repair_id, lease)
        snapshot = self._boundary.snapshot(
            source_instance_id=run.source_instance_id,
            control_instance_id=run.control_instance_id,
            source_record_pks=self._boundary.source_record_pks(repair_id),
        )
        observed = getattr(snapshot, "boundary_digest", None)
        if not isinstance(observed, str) or observed != lease.boundary_digest:
            raise RuntimeError("repair boundary drift prevents initial control claim")
        return RepairBoundaryComponentProof.from_snapshot(snapshot)

    def _current_components(self, repair_id: str) -> RepairBoundaryComponentProof:
        run = self._boundary.get_qualification(repair_id)
        if run is None:
            raise RuntimeError("repair qualification is missing")
        snapshot = self._boundary.snapshot(
            source_instance_id=run.source_instance_id,
            control_instance_id=run.control_instance_id,
            source_record_pks=self._boundary.source_record_pks(repair_id),
        )
        return RepairBoundaryComponentProof.from_snapshot(snapshot)

    def _assert_authorized_boundary(
        self, repair_id: str, lease: RepairControlLease
    ) -> RepairBoundaryComponentProof:
        self._qualified_run(repair_id, lease)
        stored = self._repository.read_boundary_component_proof(lease.run_id)
        if stored is None:
            raise RuntimeError("repair boundary component proof is missing")
        baseline, authorized_control_digest, authorized_stale_digest = stored
        current = self._current_components(repair_id)
        if not baseline.immutable_matches(current):
            raise RuntimeError("immutable repair boundary component drift was detected")
        if (
            current.control_digest != authorized_control_digest
            or current.stale_run_evidence_digest != authorized_stale_digest
        ):
            raise RuntimeError("unaccounted repair control or stale-run change was detected")
        return baseline

    def _qualified_run(self, repair_id: str, lease: RepairControlLease) -> RepairQualificationRun:
        run = self._boundary.get_qualification(repair_id)
        if (
            run is None
            or run.run_id != lease.run_id
            or run.boundary_digest != lease.boundary_digest
        ):
            raise RuntimeError("qualified repair boundary no longer matches the control lease")
        return run

    def _lost(
        self,
        repair_id: str,
        lease: RepairControlLease,
        reason: str,
    ) -> RepairControlLease:
        self._repository.record_task_proof(lease, "lost", reason)
        # transition() seals the authorized lost post-state in the same Neo4j transaction.
        return self._repository.transition(
            RepairControlLease(
                lease.run_id, lease.owner_id, lease.token, lease.revision + 1, "lost",
                lease.boundary_digest,
            ),
            lease.revision,
        )

    @staticmethod
    def _same_owner(left: RepairControlLease, right: RepairControlLease) -> bool:
        return left.owner_id == right.owner_id and left.token == right.token
