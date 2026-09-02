"""Ordered #310 quiescence orchestration over injected metadata seams."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from src.crm_deal_identity_repair.control_models import (
    CapturedTaskTopologyIdentity,
    RepairControlCommand,
    RepairDispatchLease,
    _trusted_request_from_durable_digest,
)
from src.crm_deal_identity_repair.task_inspection import (
    BrokerInspector,
    TaskAbsenceEvidence,
    WorkerInspector,
    collect_absence_evidence,
    verify_absence_evidence,
)


class QuiescenceRepository(Protocol):
    def claim(
        self, request: RepairControlCommand, *, boundary_digest: str, control_instance_id: str
    ) -> RepairDispatchLease: ...
    def request_stop_topology(
        self, *, control_instance_id: str, run_id: str, owner_id: str, stale_run_id: str
    ) -> str: ...
    def captured_task_identities(
        self, *, run_id: str, control_instance_id: str, topology_digest: str
    ) -> tuple[CapturedTaskTopologyIdentity, ...]: ...
    def complete_quiescence(
        self,
        request: RepairControlCommand,
        *,
        boundary_digest: str,
        control_instance_id: str,
        topology_digest: str,
        evidence: TaskAbsenceEvidence,
        proof_secret: bytes,
        stale_run_id: str,
    ) -> RepairDispatchLease: ...


@dataclass(frozen=True)
class QuiescenceResult:
    lease: RepairDispatchLease
    evidence: TaskAbsenceEvidence
    execution_allowed: bool = False


class RepairQuiescenceService:
    """Claim, request stop, prove fresh absence, then atomically final-CAS commit."""

    def __init__(
        self, repository: QuiescenceRepository, worker: WorkerInspector, broker: BrokerInspector
    ) -> None:
        self._repository = repository
        self._worker = worker
        self._broker = broker

    def quiesce(
        self,
        *,
        request: RepairControlCommand,
        boundary_digest: str,
        control_instance_id: str,
        expected_workers: tuple[str, ...],
        timeout_seconds: int,
        max_age_seconds: int,
        proof_key_id: str,
        proof_secret: bytes,
        stale_run_id: str,
    ) -> QuiescenceResult:
        lease = self._repository.claim(
            request, boundary_digest=boundary_digest, control_instance_id=control_instance_id
        )
        topology_digest = self._repository.request_stop_topology(
            control_instance_id=control_instance_id,
            run_id=request.run_id,
            owner_id=request.owner_id,
            stale_run_id=stale_run_id,
        )
        captured_tasks = self._repository.captured_task_identities(
            run_id=request.run_id,
            control_instance_id=control_instance_id,
            topology_digest=topology_digest,
        )
        evidence = collect_absence_evidence(
            worker=self._worker,
            broker=self._broker,
            run_id=request.run_id,
            captured_tasks=captured_tasks,
            boundary_digest=boundary_digest,
            owner_id=request.owner_id,
            token_digest=request.token_digest,
            dispatch_revision=lease.revision,
            topology_digest=topology_digest,
            expected_workers=expected_workers,
            timeout_seconds=timeout_seconds,
            max_age_seconds=max_age_seconds,
            key_id=proof_key_id,
            secret=proof_secret,
        )
        if not verify_absence_evidence(evidence, secret=proof_secret, now=datetime.now(UTC)):
            raise RuntimeError("repair task absence evidence is not authentic and fresh")
        final_request = _trusted_request_from_durable_digest(
            request.repair_id,
            request.run_id,
            request.owner_id,
            request.token_digest,
            lease.revision,
        )
        final = self._repository.complete_quiescence(
            final_request,
            boundary_digest=boundary_digest,
            control_instance_id=control_instance_id,
            topology_digest=topology_digest,
            evidence=evidence,
            proof_secret=proof_secret,
            stale_run_id=stale_run_id,
        )
        return QuiescenceResult(final, evidence)
