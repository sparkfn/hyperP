"""Typed raw-record helpers for strict CRM projection topology validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from src.crm_tenant_projection_identity import projection_head_id
from src.crm_tenant_projection_models import (
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionReleaseSummary,
)
from src.crm_tenant_projection_records import CRM_TENANT_PROJECTION_CONTRACT_VERSION
from src.graph.crm_tenant_projection_values import (
    _mapping_string,
    _object_mapping,
    _required_mapping,
)


class _RecordValue(Protocol):
    def __getitem__(self, key: str) -> object: ...


def _decision_matches(decision: str, reason: str | None, associations: int, bindings: int) -> bool:
    if decision == "associated":
        return associations > 0 and reason is None
    if decision != "zero_target" or associations != 0:
        return False
    return (bindings == 0 and reason == "empty_membership") or (
        bindings > 0 and reason == "no_mapped_targets"
    )


def _node_rows(record: _RecordValue, key: str) -> list[Mapping[str, object]]:
    raw = record[key]
    if not isinstance(raw, list):
        raise CrmTenantProjectionIntegrityError("projection topology rows are malformed")
    return [_object_mapping(item, "projection topology row") for item in raw]


def _child_node(row: Mapping[str, object], label: str) -> Mapping[str, object]:
    value = row.get("node")
    if value is None:
        raise CrmTenantProjectionIntegrityError(f"{label} node is malformed")
    return _object_mapping(value, label)


def _mapping_list(row: Mapping[str, object], key: str, label: str) -> list[Mapping[str, object]]:
    value = row.get(key)
    if not isinstance(value, list):
        raise CrmTenantProjectionIntegrityError(f"{label} are malformed")
    return [_object_mapping(item, label) for item in value]


def _mapping_node_strings(
    row: Mapping[str, object],
    key: str,
    value_key: str,
    label: str,
) -> list[str]:
    values = _mapping_list(row, key, label)
    return [_mapping_string(value, value_key) for value in values]


def _require_exact_strings(row: Mapping[str, object], key: str, expected: str, label: str) -> None:
    value = row.get(key)
    if not isinstance(value, list) or value != [expected]:
        raise CrmTenantProjectionIntegrityError(f"{label} is malformed")


def _validate_authority(
    record: Mapping[str, object], release: CrmTenantProjectionReleaseSummary
) -> None:
    source_ids = record.get("source_census_ids")
    mapping_ids = record.get("mapping_revision_ids")
    if source_ids != [release.source_census_id] or mapping_ids != [release.mapping_revision_id]:
        raise CrmTenantProjectionIntegrityError(
            "projection release authority topology is malformed"
        )
    values = _required_mapping(record, "release")
    if (
        _mapping_string(values, "projection_head_id") != projection_head_id(release.scope)
        or _mapping_string(values, "contract_version") != CRM_TENANT_PROJECTION_CONTRACT_VERSION
    ):
        raise CrmTenantProjectionIntegrityError(
            "projection release authority metadata is malformed"
        )
