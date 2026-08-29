"""Strict bounded membership-observation validation for tenant projection."""

from __future__ import annotations

from src.crm_tenant_projection_models import CrmTenantProjectionIntegrityError
from src.crm_tenant_projection_records import _digest
from src.graph.crm_tenant_projection_values import (
    _optional_string,
    _RecordValue,
    _required_int,
    _required_string,
    _required_subject_kind,
    _SubjectKind,
)


def _validated_observation_id(
    row: _RecordValue,
    snapshot_id: str,
    subject_kind: _SubjectKind,
    subject_id: str,
    source_key: str,
    source_instance_id: str,
    control_instance_id: str,
    observation_nodes: dict[str, str],
) -> str | None:
    observation_id = _optional_string(row, "observation_id")
    snapshot_count = _required_int(row, "snapshot_reference_count")
    owner_count = _required_int(row, "observation_owner_count")
    company_reference_count = _required_int(row, "company_reference_count")
    if observation_id is None:
        _validate_absent_observation(row, snapshot_count, owner_count, company_reference_count)
        return None
    node_id = _required_string(row, "observation_node_id")
    observed_snapshot_id = _required_string(row, "observation_snapshot_id")
    observed_subject_kind = _required_subject_kind(row, "observation_subject_kind")
    observed_subject_id = _required_string(row, "observation_subject_id")
    company_id = _required_string(row, "company_id")
    sort = _optional_nonnegative_int(row, "observation_sort")
    role_id = _optional_string(row, "observation_role_id")
    is_primary = _required_bool(row, "observation_is_primary")
    if (
        snapshot_count != 1
        or owner_count != 1
        or observed_snapshot_id != snapshot_id
        or observed_subject_kind != subject_kind
        or observed_subject_id != subject_id
    ):
        raise CrmTenantProjectionIntegrityError("membership observation topology is malformed")
    _validate_company_reference(
        row,
        company_reference_count,
        company_id,
        source_key,
        source_instance_id,
        control_instance_id,
    )
    expected_id = _digest(
        "crm-company-membership-observation-v1",
        [observed_snapshot_id, company_id, sort, role_id, is_primary],
    )
    if observation_id != expected_id:
        raise CrmTenantProjectionIntegrityError("membership observation identity is malformed")
    existing_node = observation_nodes.setdefault(observation_id, node_id)
    if existing_node != node_id:
        raise CrmTenantProjectionIntegrityError("membership observation identity is duplicated")
    return observation_id


def _validate_absent_observation(
    row: _RecordValue,
    snapshot_count: int,
    owner_count: int,
    company_reference_count: int,
) -> None:
    fields = (
        "observation_node_id",
        "observation_snapshot_id",
        "observation_subject_kind",
        "observation_subject_id",
        "company_id",
        "observation_sort",
        "observation_role_id",
        "observation_is_primary",
        "reference_company_id",
        "reference_source_key",
        "reference_source_instance_id",
        "reference_control_instance_id",
    )
    if (
        snapshot_count != 0
        or owner_count != 0
        or company_reference_count != 0
        or any(row[field] is not None for field in fields)
    ):
        raise CrmTenantProjectionIntegrityError("membership observation row is malformed")


def _validate_company_reference(
    row: _RecordValue,
    reference_count: int,
    company_id: str,
    source_key: str,
    source_instance_id: str,
    control_instance_id: str,
) -> None:
    if (
        reference_count != 1
        or _required_string(row, "reference_company_id") != company_id
        or _required_string(row, "reference_source_key") != source_key
        or _required_string(row, "reference_source_instance_id") != source_instance_id
        or _required_string(row, "reference_control_instance_id") != control_instance_id
    ):
        raise CrmTenantProjectionIntegrityError(
            "membership company reference topology is malformed"
        )


def _optional_nonnegative_int(row: _RecordValue, key: str) -> int | None:
    value = row[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CrmTenantProjectionIntegrityError(f"persisted {key} is malformed")
    return value


def _required_bool(row: _RecordValue, key: str) -> bool:
    value = row[key]
    if not isinstance(value, bool):
        raise CrmTenantProjectionIntegrityError(f"persisted {key} is malformed")
    return value
