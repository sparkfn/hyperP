"""Read-only verification adapters for receipt-bound protected evidence."""

from __future__ import annotations

from intelligence.crm.activities.cleanup.receipt import CleanupReceipt
from intelligence.repositories.protocols.crm_activity_cleanup import CrmActivityCleanupRepository


def verify_protected_evidence(
    repository: CrmActivityCleanupRepository, receipt: CleanupReceipt
) -> None:
    """Require every receipt-serialized protected relationship to remain exact and present."""
    missing = repository.verify_protected(receipt.protected_evidence)
    if missing:
        raise RuntimeError("cleanup protected evidence changed")
