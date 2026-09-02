"""Service delegation remains non-allocating and repository-owned."""

from __future__ import annotations

import inspect

from src.crm_deal_identity_repair.rollback_service import (
    CrmDealIdentityRepairRollbackService,
)


def test_service_has_only_repository_delegation() -> None:
    source = inspect.getsource(CrmDealIdentityRepairRollbackService)
    assert "commit_atomic_rollback" in source
    assert "get_rollback_status" in source
    assert "allocate" not in source.lower()
