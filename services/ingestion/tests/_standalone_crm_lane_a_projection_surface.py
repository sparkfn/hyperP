"""A-P1 compile surface limited to membership-read, mapping, and projection contracts."""

from __future__ import annotations

from src.crm_company_contracts import CrmCompanyMembershipHead
from src.crm_tenant_mapping_contracts import CrmTenantActiveMappingHead
from src.crm_tenant_projection_contracts import CrmTenantProjectionActiveHead


def active_projection_head_id(
    membership_head: CrmCompanyMembershipHead,
    mapping_head: CrmTenantActiveMappingHead,
    projection_head: CrmTenantProjectionActiveHead,
) -> str:
    """Prove A-P1 reads frozen heads without activating or materializing anything."""
    release = projection_head.active_release
    if membership_head.scope.source_instance_id != projection_head.scope.source_instance_id:
        raise ValueError("membership head must use the projection source scope")
    if release.mapping_revision_id != mapping_head.active_revision_id:
        raise ValueError("projection release must use the supplied active mapping revision")
    return projection_head.head_id
