"""Immutable CRM tenant mapping write payload and replay helpers."""

from __future__ import annotations

from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingEntry,
    CrmTenantMappingEntryTarget,
    CrmTenantMappingManifest,
    CrmTenantMappingRollbackProvenance,
)
from src.crm_tenant_mapping_models import (
    CrmTenantMappingConflictError,
    CrmTenantMappingPrepareCommand,
    CrmTenantMappingRevisionSnapshot,
    CrmTenantMappingRollbackCommand,
)
from src.graph.crm_tenant_mapping_graph_values import _scope_parameters


def _revision_properties(
    command: CrmTenantMappingPrepareCommand | CrmTenantMappingRollbackCommand,
    manifest: CrmTenantMappingManifest,
    revision_id: str,
    revision_number: int,
    provenance: CrmTenantMappingRollbackProvenance | None,
) -> dict[str, object]:
    expected = command.expected_head_boundary.expected_head
    properties: dict[str, object] = {
        **_scope_parameters(command.scope),
        "revision_id": revision_id,
        "revision_number": revision_number,
        "manifest_digest": manifest.digest,
        "contract_version": manifest.contract_version,
        "omission_policy": manifest.omission_policy,
        "company_entry_count": len(manifest.entries),
        "target_count": sum(len(entry.targets) for entry in manifest.entries),
        "preparation_request_id": command.preparation_request_id,
        "request_fingerprint": command.request_fingerprint,
        "authorization_actor": command.authorization.actor,
        "authorization_reference": command.authorization.authorization_reference,
        "authorization_digest": command.authorization.authorization_digest,
        "authorized_at": command.authorization.authorized_at,
        "authorization_expires_at": command.authorization.expires_at,
        "expected_head_id": command.expected_head_boundary.head_id,
        "expected_head_present": expected is not None,
        "expected_active_revision_id": None if expected is None else expected.active_revision_id,
        "expected_active_revision_number": None
        if expected is None
        else expected.active_revision_number,
        "expected_active_manifest_digest": None
        if expected is None
        else expected.active_manifest_digest,
        "state": "prepared",
        "created_at": command.operation_time,
        "rollback_of_revision_id": None
        if provenance is None
        else provenance.rollback_of_revision_id,
        "rollback_of_revision_number": None
        if provenance is None
        else provenance.rollback_of_revision_number,
        "rollback_of_manifest_digest": None
        if provenance is None
        else provenance.rollback_of_manifest_digest,
    }
    return properties


def _persistence_components(
    revision_id: str, manifest: CrmTenantMappingManifest
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    entries: list[dict[str, object]] = []
    targets: list[dict[str, object]] = []
    for company_entry in manifest.entries:
        entry = CrmTenantMappingEntry(revision_id, company_entry)
        entries.append({"entry_id": entry.entry_id, "company_id": entry.company_id})
        for target in company_entry.targets:
            entry_target = CrmTenantMappingEntryTarget(entry, target)
            targets.append(
                {
                    "entry_id": entry.entry_id,
                    "target_id": entry_target.target_id,
                    "entity_key": target.entity_key,
                    "relationship_kind": target.relationship_kind,
                }
            )
    return entries, targets


def _target_keys(manifest: CrmTenantMappingManifest) -> tuple[str, ...]:
    return tuple(
        sorted({target.entity_key for entry in manifest.entries for target in entry.targets})
    )


def _require_replay(snapshot: CrmTenantMappingRevisionSnapshot, fingerprint: str) -> None:
    if snapshot.request_fingerprint != fingerprint:
        raise CrmTenantMappingConflictError(
            "preparation request ID was reused with different immutable input"
        )
