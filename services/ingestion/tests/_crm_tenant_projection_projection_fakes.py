"""Issue #305-only projection release fixtures."""

from __future__ import annotations

from _standalone_crm_lane_a_fakes import (
    mapping_manifest,
    membership_head,
    membership_observation,
    prepared_mapping_revision,
    projection_scope,
)
from src.crm_tenant_mapping_contracts import CrmTenantMappingEntry, CrmTenantMappingEntryTarget
from src.crm_tenant_projection_contracts import (
    CrmTenantProjectionAssociation,
    CrmTenantProjectionDecision,
    CrmTenantProjectionInput,
    CrmTenantProjectionMembershipHeadBoundary,
    CrmTenantProjectionRelease,
    CrmTenantProjectionSupport,
)


def prepared_projection_release() -> CrmTenantProjectionRelease:
    release_id = "projection-release-prepared-1"
    head = membership_head()
    boundary = CrmTenantProjectionMembershipHeadBoundary(projection_scope(), (head,))
    input_item = CrmTenantProjectionInput(release_id, head)
    association = CrmTenantProjectionAssociation(
        release_id,
        input_item.input_id,
        input_item.subject_type,
        input_item.subject_id,
        "entity-a",
    )
    manifest_entry = mapping_manifest().entries[0]
    prepared = prepared_mapping_revision()
    persisted_entry = CrmTenantMappingEntry(prepared.revision_id, manifest_entry)
    support = CrmTenantProjectionSupport(
        release_id,
        association,
        membership_observation(),
        CrmTenantMappingEntryTarget(persisted_entry, manifest_entry.targets[0]),
    )
    return CrmTenantProjectionRelease(
        projection_scope(),
        release_id,
        2,
        "projection-request-prepared-1",
        "sha256:" + "a" * 64,
        "census-a",
        "sha256:" + "a" * 64,
        boundary,
        prepared,
        None,
        "completed",
        (input_item,),
        (CrmTenantProjectionDecision(release_id, input_item.input_id, "associated"),),
        (association,),
        (support,),
    )
