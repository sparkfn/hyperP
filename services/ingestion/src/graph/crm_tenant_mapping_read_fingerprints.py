"""Canonical persisted command-fingerprint verification for mapping strict reads."""

from __future__ import annotations

from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingManifest,
    CrmTenantMappingRevision,
)
from src.crm_tenant_mapping_models import (
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingIntegrityError,
    CrmTenantMappingPrepareCommand,
    CrmTenantMappingRejectCommand,
    CrmTenantMappingRejection,
    CrmTenantMappingRollbackCommand,
)


def _assert_persisted_fingerprints(
    revision: CrmTenantMappingRevision,
    manifest: CrmTenantMappingManifest,
    boundary: CrmTenantMappingExpectedHeadBoundary,
    created_at: str,
    request_fingerprint: str,
    rejection: CrmTenantMappingRejection | None,
    rejected_at: str | None,
    rejection_authorization: CrmTenantMappingAuthorization | None,
    rejection_request_fingerprint: str | None,
) -> None:
    expected_request = _expected_request_fingerprint(revision, manifest, boundary, created_at)
    if request_fingerprint != expected_request:
        raise CrmTenantMappingIntegrityError("mapping preparation fingerprint conflicts")
    if revision.state != "rejected":
        return
    if rejection is None or rejected_at is None or rejection_authorization is None:
        raise CrmTenantMappingIntegrityError("mapping rejection fingerprint lacks metadata")
    expected_rejection = CrmTenantMappingRejectCommand(
        revision.scope,
        revision.revision_id,
        revision.manifest_digest,
        rejection,
        rejection_authorization,
        rejected_at,
    ).request_fingerprint
    if rejection_request_fingerprint != expected_rejection:
        raise CrmTenantMappingIntegrityError("mapping rejection fingerprint conflicts")


def _expected_request_fingerprint(
    revision: CrmTenantMappingRevision,
    manifest: CrmTenantMappingManifest,
    boundary: CrmTenantMappingExpectedHeadBoundary,
    created_at: str,
) -> str:
    provenance = revision.rollback_provenance
    if provenance is None:
        return CrmTenantMappingPrepareCommand(
            revision.scope,
            revision.preparation_request_id,
            manifest,
            boundary,
            revision.authorization,
            created_at,
        ).request_fingerprint
    return CrmTenantMappingRollbackCommand(
        revision.scope,
        revision.preparation_request_id,
        provenance.rollback_of_revision_id,
        provenance.rollback_of_manifest_digest,
        boundary,
        revision.authorization,
        created_at,
    ).request_fingerprint
