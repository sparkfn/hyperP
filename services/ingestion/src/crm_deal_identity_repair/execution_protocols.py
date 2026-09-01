"""Granular, non-executable seams reserved for repair issues #309 through #313."""

from __future__ import annotations

from typing import Protocol

from src.crm_deal_identity_repair.control_models import (
    RepairControlRequest,
    RepairControlStatus,
    RepairDispatchLease,
)
from src.crm_deal_identity_repair.execution_models import (
    RepairBoundaryDriftReason,
    RepairBoundarySnapshot,
    RepairCheckpoint,
    RepairExecutionBoundaryManifest,
    RepairFence,
    RepairMutationResult,
    RepairOutboxEvent,
    RepairQualificationRun,
    RepairQuiescence,
    RepairRollbackImage,
    RepairRunStatus,
    RepairSecondaryDisposition,
    RepairUnit,
    RepairVerificationResult,
)
from src.crm_deal_identity_repair.mutation_models import (
    RepairAtomicMutationResult,
    RepairMutationCommand,
)
from src.crm_deal_identity_repair.verification_models import (
    RepairAtomicVerificationResult,
    RepairRunEquationCommand,
    RepairRunEquationResult,
    RepairVerificationCommand,
)


class RepairQualificationRepository(Protocol):
    """Persists and reads only the immutable qualification boundary."""

    def qualify(
        self,
        manifest: RepairExecutionBoundaryManifest,
        snapshot: RepairBoundarySnapshot,
    ) -> RepairQualificationRun: ...

    def get_qualification(self, repair_id: str) -> RepairQualificationRun | None: ...

    def get_execution_manifest(
        self,
        repair_id: str,
    ) -> RepairExecutionBoundaryManifest | None: ...

    def source_record_pks(self, repair_id: str) -> tuple[str, ...]: ...


class RepairBoundaryReader(Protocol):
    def snapshot(
        self,
        *,
        source_instance_id: str,
        control_instance_id: str,
        source_record_pks: tuple[str, ...],
    ) -> RepairBoundarySnapshot: ...


class RepairStatusReader(Protocol):
    def get_status(
        self,
        repair_id: str,
        snapshot: RepairBoundarySnapshot | None = None,
        drift_reason: RepairBoundaryDriftReason | None = None,
    ) -> RepairRunStatus: ...


class RepairQuiescenceRepository(Protocol):
    """Future #309 quiescence ownership; it does not dispatch work."""

    def claim_quiescence(self, request: RepairQuiescence) -> RepairQuiescence: ...

    def read_quiescence(
        self,
        run_id: str,
        quiescence_id: str,
    ) -> RepairQuiescence | None: ...

    def release_quiescence(self, release: RepairQuiescence) -> RepairQuiescence: ...


class RepairControlRepository(Protocol):
    """#310 metadata-only CAS control; it never releases dispatch or executes CRM work."""

    def claim(
        self, request: RepairControlRequest, *, boundary_digest: str, control_instance_id: str
    ) -> RepairDispatchLease: ...

    def pause(self, request: RepairControlRequest) -> RepairDispatchLease: ...

    def resume(self, request: RepairControlRequest) -> RepairDispatchLease: ...

    def status(self, repair_id: str) -> RepairControlStatus: ...


class RepairAllocationRepository(Protocol):
    """Future #310 allocation, fences, and checkpoints."""

    def reserve_unit(self, unit: RepairUnit, fence: RepairFence) -> RepairUnit: ...

    def read_unit(
        self,
        run_id: str,
        unit_id: str,
        generation: int,
    ) -> RepairUnit | None: ...

    def read_fence(
        self,
        run_id: str,
        fence_id: str,
    ) -> RepairFence | None: ...

    def append_checkpoint(self, checkpoint: RepairCheckpoint) -> RepairCheckpoint: ...


class RepairMutationRepository(Protocol):
    """Future #311 append-only mutation evidence and rollback images."""

    def store_rollback_image(self, image: RepairRollbackImage) -> RepairRollbackImage: ...

    def commit_atomic_mutation(
        self,
        request: RepairMutationCommand,
    ) -> RepairAtomicMutationResult:
        """Commit one fenced domain mutation and all ledger effects, or none."""

    def append_mutation_result(self, result: RepairMutationResult) -> RepairMutationResult: ...

    def read_mutation_result(
        self,
        run_id: str,
        mutation_id: str,
    ) -> RepairMutationResult | None: ...


class RepairVerificationRepository(Protocol):
    """Future #312 verification and secondary reconciliation evidence."""

    def append_verification(
        self,
        result: RepairVerificationResult,
    ) -> RepairVerificationResult: ...

    def append_secondary_disposition(
        self,
        disposition: RepairSecondaryDisposition,
    ) -> RepairSecondaryDisposition: ...

    def list_unit_verifications(
        self,
        run_id: str,
        unit_id: str,
        generation: int,
    ) -> tuple[RepairVerificationResult, ...]: ...

    def verify_and_reconcile_unit(
        self, command: RepairVerificationCommand
    ) -> RepairAtomicVerificationResult: ...

    def read_run_equation(self, command: RepairRunEquationCommand) -> RepairRunEquationResult: ...


class RepairRollbackRepository(Protocol):
    """Future #313 guarded rollback ledger seam; never invokes a CRM mutation."""

    def get_rollback_image(
        self,
        run_id: str,
        rollback_image_id: str,
    ) -> RepairRollbackImage | None: ...

    def append_rollback_disposition(
        self,
        disposition: RepairSecondaryDisposition,
    ) -> RepairSecondaryDisposition: ...


class RepairIntegrationRepository(Protocol):
    """Future outbox ownership and acknowledgement without dispatch or release authority."""

    def append_outbox_event(self, event: RepairOutboxEvent) -> RepairOutboxEvent: ...

    def claim_outbox_event(
        self,
        run_id: str,
        event_id: str,
        owner_id: str,
        delivery_token: str,
    ) -> RepairOutboxEvent | None: ...

    def acknowledge_outbox_event(self, event: RepairOutboxEvent) -> RepairOutboxEvent: ...


class RepairAcceptanceStatusReader(Protocol):
    """Future acceptance and release observability; it grants no authority."""

    def read_acceptance_status(self, repair_id: str) -> RepairRunStatus: ...

    def read_release_status(self, repair_id: str) -> RepairRunStatus: ...
