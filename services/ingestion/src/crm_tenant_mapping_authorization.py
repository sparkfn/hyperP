"""Fail-closed authorization boundary for immutable CRM tenant mapping mutations."""

from __future__ import annotations

from typing import Protocol

from src.crm_tenant_mapping_models import (
    CrmTenantMappingAuthorizationError,
    CrmTenantMappingAuthorizationRequest,
)


class CrmTenantMappingAuthorizer(Protocol):
    """Authorizes one complete canonical mapping mutation or raises fail-closed."""

    def authorize(self, request: CrmTenantMappingAuthorizationRequest) -> None: ...


class UnavailableCrmTenantMappingAuthorizer:
    """Production-safe default until a separately-owned authority adapter exists."""

    def authorize(self, request: CrmTenantMappingAuthorizationRequest) -> None:
        del request
        raise CrmTenantMappingAuthorizationError(
            "CRM tenant mapping authorization is unavailable; refusing mutation"
        )
