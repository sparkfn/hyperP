"""Thin application service for the single atomic activation operation."""

from __future__ import annotations

from src.crm_tenant_activation_contracts import (
    CrmTenantActivationCommand,
    CrmTenantActivationRepository,
    CrmTenantActivationResult,
)


class CrmTenantActivationService:
    """Delegate immutable activation commands to the transaction repository."""

    def __init__(self, repository: CrmTenantActivationRepository) -> None:
        self._repository = repository

    def activate(self, command: CrmTenantActivationCommand) -> CrmTenantActivationResult:
        """Activate once or return the durable receipt for an exact replay."""
        return self._repository.activate(command)
