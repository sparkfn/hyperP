"""Strict graph-record conversion for rollback terminal evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from src.crm_deal_identity_repair.digests import rollback_drift_digest
from src.crm_deal_identity_repair.execution_models import (
    RepairFence,
    RepairMutationResult,
    RepairRollbackImage,
    RepairSecondaryDisposition,
    RepairUnit,
)
from src.crm_deal_identity_repair.execution_records import (
    RepairFenceState,
    RepairMutationOutcome,
    RepairRollbackState,
    RepairSecondaryOutcome,
    RepairUnitState,
)
from src.crm_deal_identity_repair.rollback_models import RepairRollbackDrift
from src.models import JsonValue


class RepairRollbackRecordError(RuntimeError):
    """Stored rollback ledger values do not satisfy frozen execution contracts."""


def unit_from_properties(values: Mapping[str, JsonValue]) -> RepairUnit:
    return RepairUnit(
        _string(values, "run_id"),
        _string(values, "unit_id"),
        _integer(values, "generation"),
        _integer(values, "sequence"),
        _integer(values, "attempt"),
        _string(values, "boundary_digest"),
        _string(values, "inventory_fingerprint"),
        cast(
            RepairUnitState,
            _state(
                values,
                "state",
                {"allocated", "quiesced", "applied", "review_required", "failed", "rolled_back"},
            ),
        ),
        _optional_string(values, "inventory_key"),
        _optional_string(values, "source_record_pk"),
        _optional_string(values, "inventory_graph_fingerprint"),
        _optional_string(values, "inventory_stored_payload_fingerprint"),
        _optional_string(values, "inventory_binding_digest"),
    )


def fence_from_properties(values: Mapping[str, JsonValue]) -> RepairFence:
    return RepairFence(
        _string(values, "run_id"),
        _string(values, "unit_id"),
        _string(values, "fence_id"),
        _integer(values, "generation"),
        _integer(values, "sequence"),
        _integer(values, "attempt"),
        _string(values, "owner_id"),
        _string(values, "token"),
        _string(values, "boundary_digest"),
        _string(values, "fence_fingerprint"),
        cast(RepairFenceState, _state(values, "state", {"claimed", "released", "lost"})),
    )


def mutation_from_properties(values: Mapping[str, JsonValue]) -> RepairMutationResult:
    return RepairMutationResult(
        _string(values, "run_id"),
        _string(values, "unit_id"),
        _string(values, "mutation_id"),
        _integer(values, "generation"),
        _integer(values, "sequence"),
        _integer(values, "attempt"),
        _string(values, "owner_id"),
        _string(values, "fence_token"),
        _string(values, "boundary_digest"),
        _string(values, "unit_fingerprint"),
        _string(values, "result_digest"),
        _string(values, "rollback_image_digest"),
        _string(values, "evidence_digest"),
        _string(values, "payload_digest"),
        cast(
            RepairMutationOutcome,
            _state(values, "outcome", {"applied", "review_required", "no_op", "drifted", "failed"}),
        ),
    )


def image_from_properties(values: Mapping[str, JsonValue]) -> RepairRollbackImage:
    return RepairRollbackImage(
        _string(values, "run_id"),
        _string(values, "unit_id"),
        _string(values, "rollback_image_id"),
        _integer(values, "generation"),
        _integer(values, "sequence"),
        _integer(values, "attempt"),
        _string(values, "owner_id"),
        _string(values, "fence_token"),
        _string(values, "boundary_digest"),
        _string(values, "source_fingerprint"),
        _string(values, "image_digest"),
        _string(values, "expected_repaired_digest"),
        _string(values, "evidence_digest"),
        _string(values, "payload_digest"),
        cast(
            RepairRollbackState,
            _state(values, "state", {"available", "restored", "review_required"}),
        ),
    )


def disposition_from_properties(values: Mapping[str, JsonValue]) -> RepairSecondaryDisposition:
    return RepairSecondaryDisposition(
        _string(values, "run_id"),
        _string(values, "unit_id"),
        _string(values, "disposition_id"),
        _integer(values, "generation"),
        _integer(values, "sequence"),
        _integer(values, "attempt"),
        _string(values, "owner_id"),
        _string(values, "control_token"),
        _string(values, "boundary_digest"),
        _string(values, "subject_fingerprint"),
        _string(values, "evidence_digest"),
        _string(values, "payload_digest"),
        cast(
            RepairSecondaryOutcome,
            _state(values, "outcome", {"pending", "reconciled", "review_required", "failed"}),
        ),
    )


def property_map(value: object, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RepairRollbackRecordError("rollback record is malformed: " + name)
    return {cast(str, key): _json_value(item) for key, item in value.items()}


def payload_json(values: Mapping[str, JsonValue]) -> str:
    value = values.get("payload_json")
    if not isinstance(value, str) or not value:
        raise RepairRollbackRecordError("rollback image payload is missing")
    return value


def drift_summaries_json(drift: tuple[tuple[str, str], ...]) -> str:
    return json.dumps(
        [{"identity": identity, "reason": reason} for identity, reason in drift],
        sort_keys=True,
        separators=(",", ":"),
    )


def drift_from_properties(
    values: Mapping[str, JsonValue], *, required: bool
) -> RepairRollbackDrift | None:
    """Decode bounded terminal drift evidence without treating it as graph state."""
    count_value = values.get("drift_total_mismatch_count")
    summaries_value = values.get("drift_summaries_json")
    digest_value = values.get("drift_complete_digest")
    if not required:
        if count_value not in {None, 0}:
            raise RepairRollbackRecordError("restored disposition has drift count")
        if summaries_value not in {None, "[]"}:
            raise RepairRollbackRecordError("restored disposition has drift summaries")
        if digest_value is not None:
            raise RepairRollbackRecordError("restored disposition has drift digest")
        return None
    if isinstance(count_value, bool) or not isinstance(count_value, int) or count_value < 1:
        raise RepairRollbackRecordError("rollback drift total is invalid")
    if not isinstance(summaries_value, str) or not summaries_value:
        raise RepairRollbackRecordError("rollback drift summaries are missing")
    if not isinstance(digest_value, str) or not digest_value:
        raise RepairRollbackRecordError("rollback drift digest is missing")
    summaries = _drift_summaries(summaries_value)
    if len(summaries) > count_value:
        raise RepairRollbackRecordError("rollback drift summary count exceeds total")
    if count_value > len(summaries) and len(summaries) != 20:
        raise RepairRollbackRecordError("rollback drift summary truncation is invalid")
    drift = RepairRollbackDrift(count_value, summaries, digest_value)
    if count_value == len(summaries):
        expected = rollback_drift_digest(
            {
                "mismatches": [
                    {"identity": identity, "reason": reason} for identity, reason in summaries
                ]
            }
        )
        if expected != digest_value:
            raise RepairRollbackRecordError("rollback drift digest differs")
    return drift


def _drift_summaries(value: str) -> tuple[tuple[str, str], ...]:
    try:
        decoded: object = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RepairRollbackRecordError("rollback drift summaries are unreadable") from exc
    if not isinstance(decoded, list) or len(decoded) > 20:
        raise RepairRollbackRecordError("rollback drift summaries are invalid")
    rows: list[tuple[str, str]] = []
    for item in decoded:
        if not isinstance(item, dict) or set(item) != {"identity", "reason"}:
            raise RepairRollbackRecordError("rollback drift summary row is invalid")
        identity = item.get("identity")
        reason = item.get("reason")
        if (
            not isinstance(identity, str)
            or not identity
            or not isinstance(reason, str)
            or not reason
        ):
            raise RepairRollbackRecordError("rollback drift summary value is invalid")
        rows.append((identity, reason))
    summaries = tuple(rows)
    if summaries != tuple(sorted(set(summaries))) or drift_summaries_json(summaries) != value:
        raise RepairRollbackRecordError("rollback drift summaries are not canonical")
    return summaries


def _string(values: Mapping[str, JsonValue], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise RepairRollbackRecordError("rollback property is invalid: " + key)
    return value


def _optional_string(values: Mapping[str, JsonValue], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RepairRollbackRecordError("rollback optional property is invalid: " + key)
    return value


def _integer(values: Mapping[str, JsonValue], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RepairRollbackRecordError("rollback property is invalid: " + key)
    return value


def _state(values: Mapping[str, JsonValue], key: str, allowed: set[str]) -> str:
    value = _string(values, key)
    if value not in allowed:
        raise RepairRollbackRecordError("rollback state is invalid: " + key)
    return value


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {cast(str, key): _json_value(item) for key, item in value.items()}
    if hasattr(value, "iso_format"):
        formatted = value.iso_format()
        if isinstance(formatted, str):
            return formatted
    raise RepairRollbackRecordError("rollback property is not JSON-compatible")
