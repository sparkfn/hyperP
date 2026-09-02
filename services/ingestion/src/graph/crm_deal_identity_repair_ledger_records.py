"""Strict canonical readback of immutable CRM repair ledger records."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from neo4j import Record

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.execution_models import (
    RepairExecutionBoundaryManifest,
    RepairQualificationRun,
)
from src.models import JsonValue


@dataclass(frozen=True)
class StoredQualification:
    """One fully cross-checked immutable ledger record."""

    run: RepairQualificationRun
    manifest: RepairExecutionBoundaryManifest
    source_record_pks: tuple[str, ...]


def stored_qualification_from_record(repair_id: str, record: Record) -> StoredQualification:
    return stored_qualification_from_values(repair_id, _record_json_dict(record))


def stored_qualification_from_values(
    repair_id: str,
    values: Mapping[str, JsonValue],
) -> StoredQualification:
    """Decode one canonical qualification, permitting only absent legacy projections."""
    manifest = _manifest_from_stored_json(values)
    source_record_pks = _source_record_pks_from_stored_json(values)
    _assert_run_and_boundary_properties(values, repair_id, manifest, source_record_pks)
    run = _qualification_run(repair_id, values, manifest)
    _assert_uuid5_run_id(run)
    _assert_single_matching_boundary(values, manifest, source_record_pks, run)
    return StoredQualification(run, manifest, source_record_pks)


def canonical_json_text(value: dict[str, JsonValue], label: str) -> str:
    try:
        return canonical_json_bytes(value).decode("utf-8")
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"repair ledger {label} cannot be canonicalized") from exc


def _qualification_run(
    repair_id: str,
    values: Mapping[str, JsonValue],
    manifest: RepairExecutionBoundaryManifest,
) -> RepairQualificationRun:
    try:
        return RepairQualificationRun(
            repair_id=repair_id,
            run_id=_required_string(values, "run_id"),
            qualification_identity=_required_string(values, "qualification_identity"),
            manifest=manifest,
            boundary_digest=_required_string(values, "boundary_digest"),
            status=_qualification_status(values),
        )
    except ValueError as exc:
        raise RuntimeError("repair ledger run identity is invalid") from exc


def _manifest_from_stored_json(values: Mapping[str, JsonValue]) -> RepairExecutionBoundaryManifest:
    payload = _canonical_stored_json(values, "manifest_json")
    if set(payload) != _MANIFEST_KEYS:
        raise RuntimeError("repair ledger manifest schema is invalid")
    stop_conditions = payload["stop_conditions"]
    if not isinstance(stop_conditions, list) or not all(
        isinstance(item, str) for item in stop_conditions
    ):
        raise RuntimeError("repair ledger manifest stop conditions are invalid")
    try:
        return RepairExecutionBoundaryManifest(
            repair_id=_json_string(payload, "repair_id"),
            artifact_id=_json_string(payload, "artifact_id"),
            artifact_manifest_hmac=_json_string(payload, "artifact_manifest_hmac"),
            inventory_digest=_json_string(payload, "inventory_digest"),
            repository_sha=_json_string(payload, "repository_sha"),
            image_digest=_json_string(payload, "image_digest"),
            configuration_digest=_json_string(payload, "configuration_digest"),
            source_contract_uuid=_json_string(payload, "source_contract_uuid"),
            environment=_staging_environment(payload),
            approval_reference=_json_string(payload, "approval_reference"),
            unit_ceiling=_json_int(payload, "unit_ceiling"),
            stop_conditions=tuple(cast(str, item) for item in stop_conditions),
            source_instance_id=_json_string(payload, "source_instance_id"),
            control_instance_id=_json_string(payload, "control_instance_id"),
            rollback_authority_reference=_json_string(payload, "rollback_authority_reference"),
            rollback_authority_policy=_json_string(payload, "rollback_authority_policy"),
            graph_boundary_digest=_json_string(payload, "graph_boundary_digest"),
            inventory_row_count=_json_int(payload, "inventory_row_count"),
            eligible_unit_count=_json_int(payload, "eligible_unit_count"),
            negative_control_count=_json_int(payload, "negative_control_count"),
            execution_allowed=_execution_allowed(payload),
        )
    except ValueError as exc:
        raise RuntimeError("repair ledger manifest is invalid") from exc


def _source_record_pks_from_stored_json(values: Mapping[str, JsonValue]) -> tuple[str, ...]:
    payload = _canonical_stored_json(values, "source_record_pks_json")
    pks = payload.get("source_record_pks")
    if set(payload) != {"source_record_pks"} or not isinstance(pks, list):
        raise RuntimeError("repair ledger source record boundary schema is invalid")
    if not all(isinstance(value, str) for value in pks):
        raise RuntimeError("repair ledger source record boundary is invalid")
    result = tuple(cast(str, value) for value in pks)
    if not result or tuple(sorted(result)) != result or len(set(result)) != len(result):
        raise RuntimeError("repair ledger source record boundary is invalid")
    return result


def _assert_run_and_boundary_properties(
    values: Mapping[str, JsonValue],
    repair_id: str,
    manifest: RepairExecutionBoundaryManifest,
    source_record_pks: tuple[str, ...],
) -> None:
    for key, value in _run_properties(manifest).items():
        if not _matches_manifest_property(values, key, value):
            raise RuntimeError(f"repair ledger persisted {key} differs from manifest")
    if _required_string(values, "boundary_digest") != manifest.graph_boundary_digest:
        raise RuntimeError("repair ledger persisted boundary digest differs from manifest")
    if _json_string(_canonical_stored_json(values, "manifest_json"), "repair_id") != repair_id:
        raise RuntimeError("repair ledger repair ID differs from manifest")
    if len(source_record_pks) != manifest.inventory_row_count:
        raise RuntimeError("repair ledger source record count differs from manifest")


def _assert_single_matching_boundary(
    values: Mapping[str, JsonValue],
    manifest: RepairExecutionBoundaryManifest,
    source_record_pks: tuple[str, ...],
    run: RepairQualificationRun,
) -> None:
    boundaries = values.get("boundaries")
    if _required_int(values, "qualification_link_count") != 1:
        raise RuntimeError("repair ledger qualification boundary link is invalid")
    if (
        not isinstance(boundaries, list)
        or len(boundaries) != 1
        or not isinstance(boundaries[0], dict)
    ):
        raise RuntimeError("repair ledger qualification boundary is invalid")
    expected = _boundary_properties(manifest, source_record_pks, run)
    boundary = boundaries[0]
    if set(boundary) - set(expected) or any(
        not _matches_manifest_property(boundary, key, value) for key, value in expected.items()
    ):
        raise RuntimeError("repair ledger boundary differs from immutable run")


def _matches_manifest_property(
    values: Mapping[str, JsonValue], key: str, expected: JsonValue
) -> bool:
    """Allow only the two omitted materialized manifest projections from #300/#309."""
    actual = values.get(key)
    if key in _LEGACY_OPTIONAL_MANIFEST_PROPERTIES and actual is None:
        return True
    return actual == expected


def _run_properties(manifest: RepairExecutionBoundaryManifest) -> dict[str, JsonValue]:
    return {
        "manifest_digest": manifest.manifest_digest,
        "artifact_id": manifest.artifact_id,
        "artifact_manifest_hmac": manifest.artifact_manifest_hmac,
        "inventory_digest": manifest.inventory_digest,
        "source_instance_id": manifest.source_instance_id,
        "control_instance_id": manifest.control_instance_id,
        "inventory_row_count": manifest.inventory_row_count,
        "eligible_unit_count": manifest.eligible_unit_count,
        "negative_control_count": manifest.negative_control_count,
        "rollback_authority_reference": manifest.rollback_authority_reference,
        "rollback_authority_policy": manifest.rollback_authority_policy,
        "execution_allowed": manifest.execution_allowed,
    }


def _boundary_properties(
    manifest: RepairExecutionBoundaryManifest,
    source_record_pks: tuple[str, ...],
    run: RepairQualificationRun,
) -> dict[str, JsonValue]:
    return {
        **_run_properties(manifest),
        "boundary_digest": run.boundary_digest,
        "manifest_json": canonical_json_text(manifest.to_dict(), "manifest"),
        "source_record_pks_json": canonical_json_text(
            {"source_record_pks": list(source_record_pks)}, "source record identities"
        ),
    }


def _assert_uuid5_run_id(run: RepairQualificationRun) -> None:
    expected = str(uuid.uuid5(uuid.NAMESPACE_URL, run.qualification_identity))
    if run.run_id != expected:
        raise RuntimeError("repair ledger run ID is inconsistent")


def _canonical_stored_json(values: Mapping[str, JsonValue], key: str) -> dict[str, JsonValue]:
    raw = _required_string(values, key)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"repair ledger {key} is unreadable") from exc
    if (
        not isinstance(parsed, dict)
        or canonical_json_text(cast(dict[str, JsonValue], parsed), key) != raw
    ):
        raise RuntimeError(f"repair ledger {key} is not canonical")
    return cast(dict[str, JsonValue], parsed)


def _record_json_dict(record: Record) -> dict[str, JsonValue]:
    return {key: cast(JsonValue, record[key]) for key in record.keys()}


def _required_string(values: Mapping[str, JsonValue], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise RuntimeError(f"repair ledger {key} is invalid")
    return value


def _required_int(values: Mapping[str, JsonValue], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"repair ledger {key} is invalid")
    return value


def _json_string(values: Mapping[str, JsonValue], key: str) -> str:
    return _required_string(values, key)


def _json_int(values: Mapping[str, JsonValue], key: str) -> int:
    return _required_int(values, key)


def _staging_environment(values: Mapping[str, JsonValue]) -> Literal["staging"]:
    if values.get("environment") != "staging":
        raise RuntimeError("repair ledger environment is invalid")
    return "staging"


def _execution_allowed(values: Mapping[str, JsonValue]) -> Literal[False]:
    if values.get("execution_allowed") is not False:
        raise RuntimeError("repair ledger execution permission is invalid")
    return False


def _qualification_status(values: Mapping[str, JsonValue]) -> Literal["qualified"]:
    if values.get("status") != "qualified":
        raise RuntimeError("repair ledger run status is invalid")
    return "qualified"


_LEGACY_OPTIONAL_MANIFEST_PROPERTIES = frozenset(
    {"rollback_authority_reference", "rollback_authority_policy"}
)

_MANIFEST_KEYS = frozenset(
    {
        "repair_id",
        "artifact_id",
        "artifact_manifest_hmac",
        "inventory_digest",
        "repository_sha",
        "image_digest",
        "configuration_digest",
        "source_contract_uuid",
        "environment",
        "approval_reference",
        "unit_ceiling",
        "stop_conditions",
        "source_instance_id",
        "control_instance_id",
        "rollback_authority_reference",
        "rollback_authority_policy",
        "graph_boundary_digest",
        "inventory_row_count",
        "eligible_unit_count",
        "negative_control_count",
        "execution_allowed",
    }
)
