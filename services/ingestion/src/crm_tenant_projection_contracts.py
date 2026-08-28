"""Public immutable contract surface for standalone CRM tenant projections."""

from __future__ import annotations

from src.crm_tenant_projection_records import (
    CRM_TENANT_PROJECTION_CONTRACT_VERSION,
    CrmTenantProjectionAssociation,
    CrmTenantProjectionDecision,
    CrmTenantProjectionDecisionKind,
    CrmTenantProjectionExpectedHead,
    CrmTenantProjectionInput,
    CrmTenantProjectionMembershipHeadBoundary,
    CrmTenantProjectionReleaseState,
    CrmTenantProjectionScope,
    CrmTenantProjectionSupport,
    CrmTenantProjectionZeroTargetReason,
)
from src.crm_tenant_projection_release_contracts import (
    CrmTenantProjectionActiveHead,
    CrmTenantProjectionRelease,
)

__all__ = (
    "CRM_TENANT_PROJECTION_CONTRACT_VERSION",
    "CrmTenantProjectionActiveHead",
    "CrmTenantProjectionAssociation",
    "CrmTenantProjectionDecision",
    "CrmTenantProjectionDecisionKind",
    "CrmTenantProjectionExpectedHead",
    "CrmTenantProjectionInput",
    "CrmTenantProjectionMembershipHeadBoundary",
    "CrmTenantProjectionRelease",
    "CrmTenantProjectionReleaseState",
    "CrmTenantProjectionScope",
    "CrmTenantProjectionSupport",
    "CrmTenantProjectionZeroTargetReason",
)
