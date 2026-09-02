"""Narrow non-allocating service boundary for guarded CRM repair rollback."""

from __future__ import annotations

from typing import Protocol

from src.crm_deal_identity_repair.rollback_models import (
    RepairRollbackCommand,
    RepairRollbackResult,
    RepairRollbackStatus,
)


class AtomicCrmDealRollbackCommitter(Protocol):
    """Owns the one transaction which validates, compares, and restores a bundle."""

    def commit_atomic_rollback(self, command: RepairRollbackCommand) -> RepairRollbackResult: ...

    def get_rollback_status(self, command: RepairRollbackCommand) -> RepairRollbackStatus: ...


class CrmDealIdentityRepairRollbackService:
    """Delegate only an already-authorized transition; no allocation or dispatch exists here."""

    def __init__(self, repository: AtomicCrmDealRollbackCommitter) -> None:
        self._repository = repository

    def execute(self, command: RepairRollbackCommand) -> RepairRollbackResult:
        return self._repository.commit_atomic_rollback(command)

    def status(self, command: RepairRollbackCommand) -> RepairRollbackStatus:
        return self._repository.get_rollback_status(command)
