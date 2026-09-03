"""One guarded service that composes unchanged #309--#312 operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from src.crm_deal_identity_repair.execution_models import (
    RepairFence,
    RepairQualificationRun,
    RepairUnit,
)
from src.crm_deal_identity_repair.integration_models import (
    RepairIntegrationReceipt,
    RepairIntegrationRequest,
)
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair.mutation_models import RepairMutationCommand
from src.crm_deal_identity_repair.mutation_service import CrmDealIdentityRepairMutationService
from src.crm_deal_identity_repair.rollback_models import (
    RepairRollbackAuthorization,
    RepairRollbackCommand,
)
from src.crm_deal_identity_repair.rollback_service import CrmDealIdentityRepairRollbackService
from src.crm_deal_identity_repair.verification_equations import RepairRunEquationCommand
from src.crm_deal_identity_repair.verification_models import RepairVerificationCommand
from src.crm_deal_identity_repair.verification_service import RepairVerificationService


@dataclass(frozen=True)
class RepairIntegrationAuthority:
    """Authenticated allocation and dispatch facts rebound in every CAS."""

    completion_id: str
    overlay_digest: str
    allocation_digest: str
    allocation_unit_set_digest: str
    allocation_request_digest: str
    allocation_origin_key_id: str
    allocation_origin_hmac: str
    allocation_receipt_digest: str
    sealed_boundary_digest: str
    allocation_revision: int


@dataclass(frozen=True)
class RepairIntegrationContext:
    """Purely verified artifact context plus database-authenticated authority."""

    run: RepairQualificationRun
    inventory: tuple[RepairInventoryItem, ...]
    authority: RepairIntegrationAuthority


class RepairIntegrationRepository(Protocol):
    def allocated_unit(
        self, request: RepairIntegrationRequest, context: RepairIntegrationContext
    ) -> RepairUnit: ...

    def claim_or_read_fence(
        self,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
        unit: RepairUnit,
    ) -> RepairFence: ...

    def read_fence(
        self,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
    ) -> tuple[RepairUnit, RepairFence]: ...

    def create_rollback_authorization(
        self,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
        unit: RepairUnit,
        fence: RepairFence,
        authorization_token_digest: str,
        policy: str,
    ) -> RepairRollbackAuthorization: ...

    def store_rollback_receipt(
        self,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
        authorization: RepairRollbackAuthorization,
        status_digest: str,
    ) -> None: ...

    def release_terminal_fence(
        self,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
        authorization: RepairRollbackAuthorization,
        result_digest: str,
    ) -> None: ...

    def accept(
        self,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
        equation_command: RepairRunEquationCommand,
    ) -> None: ...

    def acceptance(self, context: RepairIntegrationContext) -> tuple[str, str]: ...

    def release_dispatch(
        self,
        request: RepairIntegrationRequest,
        context: RepairIntegrationContext,
        fence_set_digest: str,
        acceptance_digest: str,
    ) -> None: ...


class CrmDealIdentityRepairIntegrationService:
    """The single six-command integration surface; it never chains another command."""

    def __init__(
        self,
        repository: RepairIntegrationRepository,
        context_loader: Callable[[RepairIntegrationRequest], RepairIntegrationContext],
        mutation: CrmDealIdentityRepairMutationService,
        verification: RepairVerificationService,
        rollback: CrmDealIdentityRepairRollbackService,
        rollback_token_digest: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._context_loader = context_loader
        self._mutation = mutation
        self._verification = verification
        self._rollback = rollback
        self._rollback_token_digest = rollback_token_digest

    def execute(self, request: RepairIntegrationRequest) -> RepairIntegrationReceipt:
        context = self._context_loader(request)
        self._assert_request_scope(request, context)
        if request.operation == "apply":
            return self._apply(request, context)
        if request.operation == "verify":
            return self._verify(request, context)
        if request.operation == "rollback-status":
            return self._rollback_status(request, context)
        if request.operation == "rollback":
            return self._rollback_execute(request, context)
        if request.operation == "accept":
            return self._accept(request, context)
        return self._release_dispatch(request, context)

    @staticmethod
    def _assert_request_scope(
        request: RepairIntegrationRequest, context: RepairIntegrationContext
    ) -> None:
        if (context.run.repair_id, context.run.run_id) != (
            request.control.repair_id,
            request.control.run_id,
        ):
            raise RuntimeError("repair command does not bind the qualified run")

    def _apply(
        self, request: RepairIntegrationRequest, context: RepairIntegrationContext
    ) -> RepairIntegrationReceipt:
        unit = self._repository.allocated_unit(request, context)
        fence = self._repository.claim_or_read_fence(request, context, unit)
        result = self._mutation.execute(
            RepairMutationCommand(
                unit,
                fence,
                _executable_inventory(context.inventory, unit),
                context.run.source_instance_id,
                context.run.control_instance_id,
            )
        )
        return RepairIntegrationReceipt.create("apply", request.request_digest, result.decision)

    def _verify(
        self, request: RepairIntegrationRequest, context: RepairIntegrationContext
    ) -> RepairIntegrationReceipt:
        unit, fence, inventory = self._claimed(request, context)
        result = self._verification.verify(
            RepairVerificationCommand(
                unit,
                fence,
                inventory,
                context.run.source_instance_id,
                context.run.control_instance_id,
                request.control.owner_id,
                request.request_digest,
            )
        )
        return RepairIntegrationReceipt.create("verify", request.request_digest, result.decision)

    def _rollback_status(
        self, request: RepairIntegrationRequest, context: RepairIntegrationContext
    ) -> RepairIntegrationReceipt:
        command = self._rollback_command(request, context)
        status = self._rollback.status(command)
        self._repository.store_rollback_receipt(
            request, context, command.authorization, status.status_digest
        )
        return RepairIntegrationReceipt.create(
            "rollback-status", request.request_digest, status.image_state
        )

    def _rollback_execute(
        self, request: RepairIntegrationRequest, context: RepairIntegrationContext
    ) -> RepairIntegrationReceipt:
        command = self._rollback_command(request, context)
        result = self._rollback.execute(command)
        if result.decision in {"restored", "reviewed_compensation_required", "replayed"}:
            self._repository.release_terminal_fence(
                request, context, command.authorization, result.result_digest
            )
        return RepairIntegrationReceipt.create("rollback", request.request_digest, result.decision)

    def _accept(
        self, request: RepairIntegrationRequest, context: RepairIntegrationContext
    ) -> RepairIntegrationReceipt:
        equation_command = RepairRunEquationCommand(
            context.run.repair_id,
            context.run.run_id,
            context.run.boundary_digest,
            context.inventory,
            context.run.inventory_digest,
            context.run.source_instance_id,
            context.run.control_instance_id,
            request.request_digest,
        )
        self._repository.accept(request, context, equation_command)
        return RepairIntegrationReceipt.create("accept", request.request_digest, "accepted")

    def _release_dispatch(
        self, request: RepairIntegrationRequest, context: RepairIntegrationContext
    ) -> RepairIntegrationReceipt:
        acceptance_digest, fences = self._repository.acceptance(context)
        self._repository.release_dispatch(request, context, fences, acceptance_digest)
        return RepairIntegrationReceipt.create(
            "release-dispatch", request.request_digest, "released"
        )

    def _claimed(
        self, request: RepairIntegrationRequest, context: RepairIntegrationContext
    ) -> tuple[RepairUnit, RepairFence, RepairInventoryItem]:
        unit, fence = self._repository.read_fence(request, context)
        return unit, fence, _executable_inventory(context.inventory, unit)

    def _rollback_command(
        self, request: RepairIntegrationRequest, context: RepairIntegrationContext
    ) -> RepairRollbackCommand:
        if self._rollback_token_digest is None:
            raise RuntimeError("repair rollback credential is unavailable for this operation")
        unit, fence, _ = self._claimed(request, context)
        authorization = self._repository.create_rollback_authorization(
            request,
            context,
            unit,
            fence,
            self._rollback_token_digest(),
            context.run.manifest.rollback_authority_policy,
        )
        return RepairRollbackCommand(authorization)


def _executable_inventory(
    inventory: tuple[RepairInventoryItem, ...], unit: RepairUnit
) -> RepairInventoryItem:
    item = next((row for row in inventory if row.inventory_key == unit.inventory_key), None)
    if item is None or item.partition == "negative_control":
        raise RuntimeError("repair allocated inventory binding is absent or not executable")
    return item
