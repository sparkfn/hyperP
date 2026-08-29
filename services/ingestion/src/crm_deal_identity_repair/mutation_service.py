"""Narrow orchestration boundary for an already-approved CRM repair unit."""

from __future__ import annotations

from typing import Protocol

from src.crm_deal_identity_repair.mutation_models import (
    RepairAtomicMutationResult,
    RepairMutationCommand,
)


class AtomicCrmDealMutationCommitter(Protocol):
    """Repository seam intentionally limited to the all-or-nothing operation."""

    def commit_atomic_mutation(
        self,
        request: RepairMutationCommand,
    ) -> RepairAtomicMutationResult:
        """Commit one guarded repair unit or roll it back entirely."""


class CrmDealIdentityRepairMutationService:
    """Delegate one pre-authorized command without allocation or dispatch behavior."""

    def __init__(self, repository: AtomicCrmDealMutationCommitter) -> None:
        self._repository = repository

    def execute(self, request: RepairMutationCommand) -> RepairAtomicMutationResult:
        """Execute the repository-owned transaction for one inventory unit."""
        return self._repository.commit_atomic_mutation(request)
