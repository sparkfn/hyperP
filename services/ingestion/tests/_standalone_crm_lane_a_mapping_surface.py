"""A-M1 compile surface limited to immutable mapping contracts."""

from __future__ import annotations

from src.crm_tenant_mapping_contracts import CrmTenantActiveMappingHead, CrmTenantMappingManifest


def active_mapping_head_id(
    head: CrmTenantActiveMappingHead,
    manifest: CrmTenantMappingManifest,
) -> str:
    """Prove A-M1 reads mapping contracts without source or runtime imports."""
    if head.active_manifest_digest != manifest.digest:
        raise ValueError("mapping head must use the supplied immutable manifest")
    return head.head_id
