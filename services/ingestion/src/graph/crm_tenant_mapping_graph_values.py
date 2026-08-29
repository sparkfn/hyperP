"""Typed Neo4j record and scope parameter boundary helpers for CRM tenant mappings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict, cast

from neo4j import Record

from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingRevisionState,
    CrmTenantMappingScope,
    CrmTenantRelationshipKind,
)
from src.crm_tenant_mapping_models import CrmTenantMappingIntegrityError


class ScopeParameters(TypedDict):
    source_key: str
    source_instance_id: str
    control_instance_id: str


def _scope_parameters(scope: CrmTenantMappingScope) -> ScopeParameters:
    """Build primitive query parameters for the neo4j driver's dynamic kwargs API."""
    return {
        "source_key": scope.source_key,
        "source_instance_id": scope.source_instance_id,
        "control_instance_id": scope.control_instance_id,
    }


def _assert_scope_values(values: Mapping[str, object], scope: CrmTenantMappingScope) -> None:
    if (
        values.get("source_key"),
        values.get("source_instance_id"),
        values.get("control_instance_id"),
    ) != (scope.source_key, scope.source_instance_id, scope.control_instance_id):
        raise CrmTenantMappingIntegrityError("mapping persisted scope conflicts")


def _record_values(record: Record | None) -> dict[str, object]:
    if record is None:
        raise CrmTenantMappingIntegrityError("mapping graph read is unexpectedly empty")
    return {str(key): record[key] for key in record.keys()}


def _mapping_value(values: Mapping[str, object], key: str) -> Mapping[str, object]:
    return _mapping(values.get(key), key)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CrmTenantMappingIntegrityError(f"mapping {label} is malformed")
    return value


def _required_str(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise CrmTenantMappingIntegrityError(f"mapping persisted {key} is malformed")
    return value


def _required_int(values: Mapping[str, object], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CrmTenantMappingIntegrityError(f"mapping persisted {key} is malformed")
    return value


def _relationship_kind(values: Mapping[str, object]) -> CrmTenantRelationshipKind:
    value = _required_str(values, "relationship_kind")
    if value != "tenant_member":
        raise CrmTenantMappingIntegrityError("mapping relationship kind is malformed")
    return "tenant_member"


def _revision_state(values: Mapping[str, object]) -> CrmTenantMappingRevisionState:
    value = _required_str(values, "state")
    if value not in {"prepared", "active", "superseded", "rejected", "activation_failed"}:
        raise CrmTenantMappingIntegrityError("mapping revision state is malformed")
    return cast(CrmTenantMappingRevisionState, value)
