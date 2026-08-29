"""Repository protocols for immutable CRM tenant mapping authority and strict readers."""

from __future__ import annotations

from typing import Protocol

from src.crm_tenant_mapping_contracts import CrmTenantActiveMappingHead, CrmTenantMappingScope
from src.crm_tenant_mapping_models import (
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingPrepareCommand,
    CrmTenantMappingRejectCommand,
    CrmTenantMappingRevisionSnapshot,
    CrmTenantMappingRollbackCommand,
)
from src.standalone_crm_census_requests import (
    MappingPrepareAuthority,
    MappingRollbackAuthority,
    SourceSyncAuthority,
)


class CrmTenantMappingCommandRepository(Protocol):
    """Atomic immutable mapping command boundary."""

    def find_by_preparation_request(
        self, scope: CrmTenantMappingScope, preparation_request_id: str
    ) -> CrmTenantMappingRevisionSnapshot | None: ...

    def prepare(
        self, command: CrmTenantMappingPrepareCommand
    ) -> CrmTenantMappingRevisionSnapshot: ...

    def rollback(
        self, command: CrmTenantMappingRollbackCommand
    ) -> CrmTenantMappingRevisionSnapshot: ...

    def reject(
        self, command: CrmTenantMappingRejectCommand
    ) -> CrmTenantMappingRevisionSnapshot: ...


class CrmTenantMappingRevisionReader(Protocol):
    """Strict immutable mapping revision and active-head reads."""

    def get_revision(
        self, scope: CrmTenantMappingScope, revision_id: str, manifest_digest: str
    ) -> CrmTenantMappingRevisionSnapshot | None: ...

    def get_active_head(
        self, scope: CrmTenantMappingScope
    ) -> CrmTenantActiveMappingHead | None: ...

    def get_active_revision(
        self, scope: CrmTenantMappingScope
    ) -> CrmTenantMappingRevisionSnapshot | None: ...


class CrmTenantMappingLifecycleRepository(
    CrmTenantMappingCommandRepository, CrmTenantMappingRevisionReader, Protocol
):
    """Combined command/read seam needed by the lifecycle application service."""


class CrmTenantMappingMaterializationReader(Protocol):
    """Strict activation-candidate reader; it intentionally accepts only prepared revisions."""

    def require_prepared_for_materialization(
        self,
        scope: CrmTenantMappingScope,
        revision_id: str,
        manifest_digest: str,
        expected_head_boundary: CrmTenantMappingExpectedHeadBoundary,
    ) -> CrmTenantMappingRevisionSnapshot: ...


class CrmTenantMappingFreshnessReader(Protocol):
    """Read-only #273/#307 authority validation seam; it never activates a mapping."""

    def validate_source_sync(
        self, scope: CrmTenantMappingScope, authority: SourceSyncAuthority
    ) -> None: ...

    def validate_mapping_prepare(
        self, scope: CrmTenantMappingScope, authority: MappingPrepareAuthority
    ) -> None: ...

    def validate_mapping_rollback(
        self, scope: CrmTenantMappingScope, authority: MappingRollbackAuthority
    ) -> None: ...
