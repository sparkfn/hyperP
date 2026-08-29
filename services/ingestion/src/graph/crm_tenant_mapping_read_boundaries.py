"""Strict active-head and Entity validation helpers for CRM mapping readers."""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.crm_tenant_mapping_contracts import CrmTenantActiveMappingHead, CrmTenantMappingScope
from src.crm_tenant_mapping_identity import mapping_head_id
from src.crm_tenant_mapping_models import (
    CrmTenantMappingConflictError,
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingIntegrityError,
)
from src.graph.crm_tenant_mapping_graph_values import (
    _assert_scope_values,
    _mapping_value,
    _record_values,
    _required_int,
    _required_str,
    _scope_parameters,
)
from src.graph.queries.crm_tenant_mapping import READ_ACTIVE_HEAD, VALIDATE_ENTITIES


def _read_active_head(
    tx: ManagedTransaction, scope: CrmTenantMappingScope
) -> CrmTenantActiveMappingHead | None:
    rows = list(tx.run(READ_ACTIVE_HEAD, **_scope_parameters(scope)))
    if not rows:
        return None
    if len(rows) != 1:
        raise CrmTenantMappingIntegrityError("mapping active head is not unique")
    values = _mapping_value(_record_values(rows[0]), "head")
    _assert_scope_values(values, scope)
    if _required_str(values, "head_id") != mapping_head_id(scope):
        raise CrmTenantMappingIntegrityError("mapping active head ID is not deterministic")
    try:
        return CrmTenantActiveMappingHead(
            scope,
            _required_str(values, "head_id"),
            _required_str(values, "active_revision_id"),
            _required_int(values, "active_revision_number"),
            _required_str(values, "active_manifest_digest"),
            _required_str(values, "effective_at"),
            None,
        )
    except ValueError as exc:
        raise CrmTenantMappingIntegrityError("mapping active head is malformed") from exc


def _assert_expected_head(
    tx: ManagedTransaction,
    scope: CrmTenantMappingScope,
    boundary: CrmTenantMappingExpectedHeadBoundary,
) -> None:
    current = _read_active_head(tx, scope)
    expected = boundary.expected_head
    if expected is None:
        if current is not None:
            raise CrmTenantMappingConflictError("mapping active head unexpectedly exists")
        return
    if current is None or (
        current.head_id,
        current.active_revision_id,
        current.active_revision_number,
        current.active_manifest_digest,
    ) != (
        expected.head_id,
        expected.active_revision_id,
        expected.active_revision_number,
        expected.active_manifest_digest,
    ):
        raise CrmTenantMappingConflictError("mapping expected active head is stale")


def _validate_entities(tx: ManagedTransaction, entity_keys: tuple[str, ...]) -> None:
    if not entity_keys:
        return
    rows = list(tx.run(VALIDATE_ENTITIES, entity_keys=list(entity_keys)))
    if len(rows) != len(entity_keys):
        raise CrmTenantMappingConflictError("mapping Entity validation is incomplete")
    for row, entity_key in zip(rows, entity_keys, strict=True):
        values = _record_values(row)
        if values.get("entity_key") != entity_key or _required_int(values, "entity_count") != 1:
            raise CrmTenantMappingConflictError(
                "mapping target must reference exactly one existing Entity"
            )
