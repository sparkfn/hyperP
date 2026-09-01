"""Atomic strict freshness validation for CRM tenant mapping authorities."""

from __future__ import annotations

from neo4j import ManagedTransaction, Record

from src.crm_tenant_mapping_contracts import CrmTenantActiveMappingHead, CrmTenantMappingScope
from src.crm_tenant_mapping_models import (
    CrmTenantMappingConflictError,
    CrmTenantMappingIntegrityError,
    CrmTenantMappingRevisionSnapshot,
)
from src.graph.crm_tenant_mapping_graph_values import _scope_parameters
from src.graph.crm_tenant_mapping_read import _read_by_id, _read_snapshot
from src.graph.crm_tenant_mapping_read_boundaries import _read_active_head
from src.graph.queries.crm_tenant_mapping import (
    VALIDATE_MAPPING_PREPARE_AT_LINEARIZATION,
    VALIDATE_MAPPING_ROLLBACK_AT_LINEARIZATION,
    VALIDATE_SOURCE_SYNC_AT_LINEARIZATION,
)
from src.standalone_crm_census_requests import (
    MappingPrepareAuthority,
    MappingRollbackAuthority,
    SourceSyncAuthority,
)


def _read_active_head_and_revision(
    tx: ManagedTransaction, scope: CrmTenantMappingScope
) -> tuple[CrmTenantActiveMappingHead | None, CrmTenantMappingRevisionSnapshot | None]:
    head = _read_active_head(tx, scope)
    if head is None:
        return None, None
    snapshot = _read_snapshot(tx, scope, head.active_revision_id, head.active_manifest_digest)
    if snapshot is None:
        raise CrmTenantMappingIntegrityError("active mapping head references a missing revision")
    if (
        snapshot.revision.state != "active"
        or snapshot.revision.revision_number != head.active_revision_number
    ):
        raise CrmTenantMappingIntegrityError("active mapping head revision is malformed")
    return head, snapshot


def prevalidate_source_sync(
    tx: ManagedTransaction, scope: CrmTenantMappingScope, authority: SourceSyncAuthority
) -> None:
    head, active = _read_active_head_and_revision(tx, scope)
    if head is None or (head.head_id, head.active_manifest_digest) != (
        authority.mapping_head_id,
        authority.mapping_head_digest,
    ):
        raise CrmTenantMappingConflictError("source-sync mapping authority is stale")
    if authority.mapping_active_revision_id is not None and (
        head.active_revision_id,
        head.active_revision_number,
    ) != (
        authority.mapping_active_revision_id,
        authority.mapping_active_revision_number,
    ):
        raise CrmTenantMappingConflictError("source-sync mapping head snapshot is stale")
    if active is None:
        raise CrmTenantMappingIntegrityError("active mapping head has no active revision")


def validate_source_sync_at_linearization(
    tx: ManagedTransaction, scope: CrmTenantMappingScope, authority: SourceSyncAuthority
) -> None:
    records = list(
        tx.run(
            VALIDATE_SOURCE_SYNC_AT_LINEARIZATION,
            **_scope_parameters(scope),
            head_id=authority.mapping_head_id,
            mapping_head_digest=authority.mapping_head_digest,
        )
    )
    _require_one_match(records, "source-sync mapping authority is stale")


def prevalidate_mapping_prepare(
    tx: ManagedTransaction, scope: CrmTenantMappingScope, authority: MappingPrepareAuthority
) -> CrmTenantMappingRevisionSnapshot:
    snapshot = _require_prepared(
        tx, scope, authority.prepared_revision_id, authority.prepared_revision_digest
    )
    if snapshot.expected_head_boundary.head_id != authority.expected_current_head_id:
        raise CrmTenantMappingConflictError("mapping prepare authority head ID conflicts")
    return snapshot


def prevalidate_mapping_rollback(
    tx: ManagedTransaction, scope: CrmTenantMappingScope, authority: MappingRollbackAuthority
) -> CrmTenantMappingRevisionSnapshot:
    snapshot = _require_prepared(
        tx, scope, authority.rollback_head_id, authority.rollback_head_digest
    )
    provenance = snapshot.revision.rollback_provenance
    if provenance is None or (
        provenance.rollback_of_revision_id,
        provenance.rollback_of_manifest_digest,
    ) != (authority.target_revision_id, authority.target_revision_digest):
        raise CrmTenantMappingConflictError("mapping rollback provenance conflicts")
    if snapshot.expected_head_boundary.head_id != authority.expected_current_head_id:
        raise CrmTenantMappingConflictError("mapping rollback authority head ID conflicts")
    expected = snapshot.expected_head_boundary.expected_head
    if expected is None:
        raise CrmTenantMappingIntegrityError(
            "rollback prepared revision lacks current-head boundary"
        )
    historical = _read_snapshot(
        tx,
        scope,
        provenance.rollback_of_revision_id,
        provenance.rollback_of_manifest_digest,
    )
    if historical is None or historical.revision.state not in {"active", "superseded"}:
        raise CrmTenantMappingConflictError(
            "mapping rollback target is not prior effective history"
        )
    if (
        historical.revision.revision_number != provenance.rollback_of_revision_number
        or historical.revision.revision_number >= expected.active_revision_number
    ):
        raise CrmTenantMappingConflictError("mapping rollback provenance revision is malformed")
    return snapshot


def _require_prepared(
    tx: ManagedTransaction,
    scope: CrmTenantMappingScope,
    revision_id: str,
    digest: str | None,
) -> CrmTenantMappingRevisionSnapshot:
    result = _read_by_id(tx, scope, revision_id)
    if (
        result is None
        or result.revision.state != "prepared"
        or (digest is not None and result.revision.manifest_digest != digest)
    ):
        raise CrmTenantMappingConflictError("exact prepared mapping revision is unavailable")
    return result


def validate_mapping_prepare_at_linearization(
    tx: ManagedTransaction, snapshot: CrmTenantMappingRevisionSnapshot
) -> None:
    boundary = snapshot.expected_head_boundary
    expected = boundary.expected_head
    records = list(
        tx.run(
            VALIDATE_MAPPING_PREPARE_AT_LINEARIZATION,
            **_scope_parameters(snapshot.revision.scope),
            revision_id=snapshot.revision.revision_id,
            manifest_digest=snapshot.revision.manifest_digest,
            expected_head_id=boundary.head_id,
            expected_head_present=expected is not None,
            expected_active_revision_id=None if expected is None else expected.active_revision_id,
            expected_active_revision_number=None
            if expected is None
            else expected.active_revision_number,
            expected_active_manifest_digest=None
            if expected is None
            else expected.active_manifest_digest,
        )
    )
    _require_one_match(records, "mapping prepare authority is stale")


def validate_mapping_rollback_at_linearization(
    tx: ManagedTransaction, snapshot: CrmTenantMappingRevisionSnapshot
) -> None:
    provenance = snapshot.revision.rollback_provenance
    expected = snapshot.expected_head_boundary.expected_head
    if provenance is None or expected is None:
        raise CrmTenantMappingIntegrityError("rollback strict snapshot lacks required provenance")
    records = list(
        tx.run(
            VALIDATE_MAPPING_ROLLBACK_AT_LINEARIZATION,
            **_scope_parameters(snapshot.revision.scope),
            revision_id=snapshot.revision.revision_id,
            manifest_digest=snapshot.revision.manifest_digest,
            rollback_of_revision_id=provenance.rollback_of_revision_id,
            rollback_of_revision_number=provenance.rollback_of_revision_number,
            rollback_of_manifest_digest=provenance.rollback_of_manifest_digest,
            expected_head_id=snapshot.expected_head_boundary.head_id,
            expected_active_revision_id=expected.active_revision_id,
            expected_active_revision_number=expected.active_revision_number,
            expected_active_manifest_digest=expected.active_manifest_digest,
        )
    )
    _require_one_match(records, "mapping rollback authority is stale")


def _require_one_match(records: list[Record], conflict_message: str) -> None:
    if not records:
        raise CrmTenantMappingConflictError(conflict_message)
    if len(records) != 1:
        raise CrmTenantMappingIntegrityError("mapping freshness linearization is not unique")
