"""Typed Neo4j record decoding and release-state helpers for CRM projection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

from neo4j import ManagedTransaction

from src.crm_tenant_projection_identity import (
    materialized_release_fingerprint,
    projection_release_id,
)
from src.crm_tenant_projection_models import (
    CrmTenantProjectionCancelledError,
    CrmTenantProjectionConflictError,
    CrmTenantProjectionCursor,
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionReleaseSummary,
)
from src.crm_tenant_projection_records import CrmTenantProjectionScope
from src.graph.queries.crm_tenant_projection import READ_RELEASE

type _Phase = Literal["capture", "projection", "complete"]
type _SubjectKind = Literal["contact", "lead"]


class _RecordValue(Protocol):
    def __getitem__(self, key: str) -> object: ...


def _read_release(tx: ManagedTransaction, release_id: str) -> CrmTenantProjectionReleaseSummary:
    record = tx.run(READ_RELEASE, release_id=release_id).single()
    if record is None:
        raise CrmTenantProjectionConflictError("projection release is missing")
    return _summary_from_record(record)


def _require_building(
    release: CrmTenantProjectionReleaseSummary,
    fingerprint: str,
    phase: _Phase,
) -> None:
    if release.release_fingerprint != fingerprint:
        raise CrmTenantProjectionConflictError("projection release fingerprint conflicts")
    if release.state == "cancelled":
        raise CrmTenantProjectionCancelledError("projection release was cancelled")
    if release.state != "building" or release.phase != phase:
        raise CrmTenantProjectionConflictError("projection release is not resumable in this phase")


def _require_page_limit(page_limit: int) -> None:
    if (
        isinstance(page_limit, bool)
        or not isinstance(page_limit, int)
        or not 1 <= page_limit <= 500
    ):
        raise ValueError("page_limit must be between 1 and 500")


def _summary_from_record(record: _RecordValue) -> CrmTenantProjectionReleaseSummary:
    values = _required_mapping(record, "release")
    state = _release_state(_mapping_string(values, "state"))
    phase = _phase_value(_mapping_string(values, "phase"))
    scope = CrmTenantProjectionScope(
        _mapping_string(values, "source_key"),
        _mapping_string(values, "source_instance_id"),
        _mapping_string(values, "control_instance_id"),
    )
    release_number = _mapping_int(values, "release_number")
    release_id = _mapping_string(values, "release_id")
    if release_id != projection_release_id(scope, release_number):
        raise CrmTenantProjectionIntegrityError(
            "projection release deterministic identity is malformed"
        )
    if _mapping_string(values, "release_fingerprint") != _materialized_fingerprint_from_values(
        values
    ):
        raise CrmTenantProjectionIntegrityError("projection release fingerprint is malformed")
    return CrmTenantProjectionReleaseSummary(
        scope=scope,
        release_id=release_id,
        release_number=release_number,
        request_id=_mapping_string(values, "request_id"),
        release_fingerprint=_mapping_string(values, "release_fingerprint"),
        source_census_id=_mapping_string(values, "source_census_id"),
        mapping_revision_id=_mapping_string(values, "mapping_revision_id"),
        mapping_manifest_digest=_mapping_string(values, "mapping_manifest_digest"),
        state=state,
        phase=phase,
        capture_cursor=_cursor(values, "capture_cursor_kind", "capture_cursor_subject_id"),
        projection_cursor=_cursor(values, "projection_cursor_kind", "projection_cursor_subject_id"),
        input_count=_mapping_int(values, "input_count"),
        decision_count=_mapping_int(values, "decision_count"),
        association_count=_mapping_int(values, "association_count"),
        support_count=_mapping_int(values, "support_count"),
        capture_boundary_digest=_mapping_string(values, "capture_boundary_digest"),
        failure_code=_mapping_optional_string(values, "failure_code"),
    )


def _materialized_fingerprint_from_values(values: Mapping[str, object]) -> str:
    scope = CrmTenantProjectionScope(
        _mapping_string(values, "source_key"),
        _mapping_string(values, "source_instance_id"),
        _mapping_string(values, "control_instance_id"),
    )
    return materialized_release_fingerprint(
        scope,
        _mapping_string(values, "release_id"),
        _mapping_int(values, "release_number"),
        _mapping_string(values, "request_fingerprint"),
        _mapping_string(values, "source_census_id"),
        _mapping_string(values, "source_census_fingerprint"),
        _census_fingerprint_payload(values, "contact"),
        _census_fingerprint_payload(values, "lead"),
        _mapping_string(values, "mapping_revision_id"),
        _mapping_int(values, "mapping_revision_number"),
        _mapping_string(values, "mapping_manifest_digest"),
        _mapping_int(values, "mapping_entry_count"),
        _mapping_int(values, "mapping_target_count"),
        _mapping_string(values, "mapping_topology_fingerprint"),
        _mapping_head_fingerprint_payload(values),
        _prior_head_fingerprint_payload(values),
        _mapping_string(values, "projection_head_id"),
        _mapping_string(values, "contract_version"),
    )


def _census_fingerprint_payload(
    values: Mapping[str, object], prefix: str
) -> dict[str, str | int | bool | None]:
    return {
        "state": _mapping_string(values, f"{prefix}_unit_state"),
        "generation": _mapping_int(values, f"{prefix}_unit_generation"),
        "checkpoint_present": _mapping_bool(values, f"{prefix}_checkpoint_present"),
        "checkpoint_generation": _mapping_optional_int(values, f"{prefix}_checkpoint_generation"),
        "processed_rows": _mapping_int(values, f"{prefix}_processed_rows"),
        "skipped_rows": _mapping_int(values, f"{prefix}_skipped_rows"),
        "expected_input_count": _mapping_int(values, f"{prefix}_expected_input_count"),
        "frozen_upper_id": _mapping_int(values, f"{prefix}_frozen_upper_id"),
    }


def _mapping_head_fingerprint_payload(
    values: Mapping[str, object],
) -> dict[str, str | int | bool | None]:
    return {
        "head_id": _mapping_string(values, "expected_mapping_head_id"),
        "digest": _mapping_string(values, "expected_mapping_head_digest"),
        "present": _mapping_bool(values, "expected_mapping_head_present"),
        "revision_id": _mapping_optional_string(values, "expected_mapping_active_revision_id"),
        "revision_number": _mapping_optional_int(values, "expected_mapping_active_revision_number"),
    }


def _prior_head_fingerprint_payload(
    values: Mapping[str, object],
) -> dict[str, str | int | bool | None]:
    return {
        "present": _mapping_bool(values, "expected_prior_head_present"),
        "head_id": _mapping_optional_string(values, "expected_prior_head_id"),
        "release_id": _mapping_optional_string(values, "expected_prior_release_id"),
        "release_number": _mapping_optional_int(values, "expected_prior_release_number"),
        "release_fingerprint": _mapping_optional_string(
            values, "expected_prior_release_fingerprint"
        ),
    }


def _cursor(
    values: Mapping[str, object],
    kind_key: str,
    id_key: str,
) -> CrmTenantProjectionCursor | None:
    kind = _mapping_optional_string(values, kind_key)
    identifier = values.get(id_key)
    if kind is None and identifier is None:
        return None
    if identifier is None:
        raise CrmTenantProjectionIntegrityError("projection cursor is malformed")
    return CrmTenantProjectionCursor(
        _subject_kind_value(kind), _nonnegative_int(identifier, "cursor")
    )


def _required_mapping(record: _RecordValue, key: str) -> Mapping[str, object]:
    return _object_mapping(record[key], f"persisted {key}")


def _object_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CrmTenantProjectionIntegrityError(f"{label} is malformed")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise CrmTenantProjectionIntegrityError(f"{label} is malformed")
        result[key] = item
    return result


def _json_object(value: object) -> Mapping[str, object]:
    return _object_mapping(value, "source census request")


def _required_string(record: _RecordValue, key: str) -> str:
    value: object = record[key]
    if not isinstance(value, str) or not value:
        raise CrmTenantProjectionIntegrityError(f"persisted {key} is malformed")
    return value


def _optional_string(record: _RecordValue, key: str) -> str | None:
    value: object = record[key]
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CrmTenantProjectionIntegrityError(f"persisted {key} is malformed")
    return value


def _required_int(record: _RecordValue, key: str) -> int:
    return _nonnegative_int(record[key], f"persisted {key}")


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CrmTenantProjectionIntegrityError(f"{label} is malformed")
    return value


def _required_subject_kind(record: _RecordValue, key: str) -> _SubjectKind:
    return _subject_kind_value(_required_string(record, key))


def _subject_kind_value(value: object) -> _SubjectKind:
    if value == "contact":
        return "contact"
    if value == "lead":
        return "lead"
    raise CrmTenantProjectionIntegrityError("projection subject kind is malformed")


def _release_state(value: str) -> Literal["building", "completed", "failed", "cancelled"]:
    if value == "building":
        return "building"
    if value == "completed":
        return "completed"
    if value == "failed":
        return "failed"
    if value == "cancelled":
        return "cancelled"
    raise CrmTenantProjectionIntegrityError("projection release state is invalid")


def _phase_value(value: str) -> _Phase:
    if value == "capture":
        return "capture"
    if value == "projection":
        return "projection"
    if value == "complete":
        return "complete"
    raise CrmTenantProjectionIntegrityError("projection release phase is invalid")


def _mapping_string(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise CrmTenantProjectionIntegrityError(f"persisted release {key} is malformed")
    return value


def _mapping_optional_string(values: Mapping[str, object], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CrmTenantProjectionIntegrityError(f"persisted release {key} is malformed")
    return value


def _mapping_int(values: Mapping[str, object], key: str) -> int:
    return _nonnegative_int(values.get(key), f"persisted release {key}")


def _mapping_optional_int(values: Mapping[str, object], key: str) -> int | None:
    value = values.get(key)
    return None if value is None else _nonnegative_int(value, f"persisted release {key}")


def _mapping_bool(values: Mapping[str, object], key: str) -> bool:
    value = values.get(key)
    if not isinstance(value, bool):
        raise CrmTenantProjectionIntegrityError(f"persisted release {key} is malformed")
    return value


def _subject_numeric_id(value: str) -> int:
    try:
        numeric = int(value)
    except ValueError as exc:
        raise CrmTenantProjectionIntegrityError("projection subject ID is malformed") from exc
    if numeric < 1 or str(numeric) != value:
        raise CrmTenantProjectionIntegrityError("projection subject ID is malformed")
    return numeric
