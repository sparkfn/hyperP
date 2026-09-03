"""Narrow orchestration seam for #311 verification repositories."""

from __future__ import annotations

from typing import Protocol

from src.crm_deal_identity_repair.verification_models import (
    RepairAtomicVerificationResult,
    RepairRunEquationCommand,
    RepairRunEquationResult,
    RepairVerificationCommand,
)


class AtomicCrmDealVerificationReader(Protocol):
    """Narrow #311 seam used by guarded orchestration."""

    def verify_and_reconcile_unit(
        self, command: RepairVerificationCommand
    ) -> RepairAtomicVerificationResult: ...

    def read_run_equation(self, command: RepairRunEquationCommand) -> RepairRunEquationResult: ...


class RepairVerificationService:
    """Keeps callers bound to the repository protocol rather than Neo4j directly."""

    def __init__(self, repository: AtomicCrmDealVerificationReader) -> None:
        self._repository = repository

    def verify(self, command: RepairVerificationCommand) -> RepairAtomicVerificationResult:
        return self._repository.verify_and_reconcile_unit(command)

    def read_run_equation(self, command: RepairRunEquationCommand) -> RepairRunEquationResult:
        return self._repository.read_run_equation(command)
