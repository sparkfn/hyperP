"""Repository contracts for replay-safe immutable CRM tenant projection materialization."""

from __future__ import annotations

from typing import Protocol

from src.crm_tenant_projection_models import (
    CrmTenantProjectionFailureCode,
    CrmTenantProjectionMaterializationCommand,
    CrmTenantProjectionReleaseSummary,
)
from src.crm_tenant_projection_records import CrmTenantProjectionScope


class CrmTenantProjectionRepository(Protocol):
    """Narrow stateful boundary; it never exposes active-head mutation."""

    def allocate_or_replay(
        self, command: CrmTenantProjectionMaterializationCommand
    ) -> CrmTenantProjectionReleaseSummary: ...

    def capture_page(
        self, release_id: str, release_fingerprint: str, page_limit: int
    ) -> CrmTenantProjectionReleaseSummary: ...

    def project_page(
        self, release_id: str, release_fingerprint: str, page_limit: int
    ) -> CrmTenantProjectionReleaseSummary: ...

    def complete(
        self, release_id: str, release_fingerprint: str
    ) -> CrmTenantProjectionReleaseSummary: ...

    def cancel(
        self, release_id: str, release_fingerprint: str
    ) -> CrmTenantProjectionReleaseSummary: ...

    def fail(
        self,
        release_id: str,
        release_fingerprint: str,
        failure_code: CrmTenantProjectionFailureCode,
    ) -> CrmTenantProjectionReleaseSummary: ...

    def get_completed(
        self, scope: CrmTenantProjectionScope, release_id: str, release_fingerprint: str
    ) -> CrmTenantProjectionReleaseSummary | None: ...
