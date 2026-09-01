"""Narrow orchestration seam for #311 verification repositories."""

from __future__ import annotations

from src.crm_deal_identity_repair.execution_protocols import RepairVerificationRepository
from src.crm_deal_identity_repair.verification_models import (
    RepairAtomicVerificationResult,
    RepairRunEquationCommand,
    RepairRunEquationResult,
    RepairVerificationCommand,
)


class RepairVerificationService:
    """Keeps callers bound to the repository protocol rather than Neo4j directly."""

    def __init__(self, repository: RepairVerificationRepository) -> None:
        self._repository = repository

    def verify(self, command: RepairVerificationCommand) -> RepairAtomicVerificationResult:
        return self._repository.verify_and_reconcile_unit(command)

    def read_run_equation(self, command: RepairRunEquationCommand) -> RepairRunEquationResult:
        return self._repository.read_run_equation(command)
