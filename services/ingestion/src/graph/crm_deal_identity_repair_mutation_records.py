"""Strict readback conversion for atomic CRM-deal repair ledger records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from neo4j import Record

from src.crm_deal_identity_repair.execution_models import (
    RepairCheckpoint,
    RepairMutationOutcome,
    RepairMutationResult,
    RepairOutboxEvent,
    RepairOutboxState,
    RepairRollbackImage,
    RepairRollbackState,
)
from src.crm_deal_identity_repair.mutation_models import RepairAtomicMutationResult
from src.models import JsonValue


def atomic_result_from_record(record: Record, *, replayed: bool) -> RepairAtomicMutationResult:
    """Decode the complete immutable bundle returned by commit or exact replay."""
    result = mutation_result_from_properties(_properties(record, "result"))
    image_values = _properties(record, "image")
    payload = canonical_payload(_string(image_values, "payload_json"))
    if _canonical_json(payload) != _string(image_values, "payload_json"):
        raise RuntimeError("repair rollback payload is not canonical JSON")
    return RepairAtomicMutationResult(
        decision="replayed" if replayed else "committed",
        mutation=result,
        rollback_image=rollback_image_from_properties(image_values),
        checkpoint=checkpoint_from_properties(_properties(record, "checkpoint")),
        outbox_event=outbox_event_from_properties(_properties(record, "outbox")),
        repaired_state_digest=_string(_properties(record, "result"), "repaired_state_digest"),
    )


def mutation_result_from_properties(values: Mapping[str, JsonValue]) -> RepairMutationResult:
    """Decode one immutable mutation result and reject loose graph properties."""
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
        _mutation_outcome(_string(values, "outcome")),
    )


def rollback_image_from_properties(values: Mapping[str, JsonValue]) -> RepairRollbackImage:
    """Decode one immutable rollback-image ledger record."""
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
        _rollback_state(_string(values, "state")),
    )


def checkpoint_from_properties(values: Mapping[str, JsonValue]) -> RepairCheckpoint:
    """Decode one immutable checkpoint record."""
    return RepairCheckpoint(
        _string(values, "run_id"),
        _string(values, "unit_id"),
        _string(values, "checkpoint_id"),
        _integer(values, "generation"),
        _integer(values, "sequence"),
        _integer(values, "attempt"),
        _string(values, "owner_id"),
        _string(values, "fence_token"),
        _string(values, "boundary_digest"),
        _string(values, "checkpoint_digest"),
        _string(values, "evidence_digest"),
        "written",
    )


def outbox_event_from_properties(values: Mapping[str, JsonValue]) -> RepairOutboxEvent:
    """Decode one bounded pending-outbox record."""
    return RepairOutboxEvent(
        _string(values, "run_id"),
        _string(values, "unit_id"),
        _string(values, "event_id"),
        _integer(values, "generation"),
        _integer(values, "sequence"),
        _integer(values, "attempt"),
        _string(values, "owner_id"),
        _string(values, "delivery_token"),
        _string(values, "boundary_digest"),
        _string(values, "payload_digest"),
        _string(values, "evidence_digest"),
        _outbox_state(_string(values, "state")),
    )


def canonical_payload(value: str) -> dict[str, JsonValue]:
    """Decode and validate the restricted rollback JSON object."""
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("repair rollback payload is unreadable") from exc
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise RuntimeError("repair rollback payload is not an object")
    return {cast(str, key): _json_value(item) for key, item in parsed.items()}


def _properties(record: Record, key: str) -> Mapping[str, JsonValue]:
    value = record[key]
    if not isinstance(value, dict):
        raise RuntimeError("repair mutation readback is malformed: " + key)
    return {cast(str, item_key): _json_value(item) for item_key, item in value.items()}


def _string(values: Mapping[str, JsonValue], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError("repair mutation property is invalid: " + key)
    return value


def _integer(values: Mapping[str, JsonValue], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("repair mutation property is invalid: " + key)
    return value


def _mutation_outcome(value: str) -> RepairMutationOutcome:
    allowed = {"applied", "review_required", "no_op", "drifted", "failed"}
    if value not in allowed:
        raise RuntimeError("repair mutation outcome is invalid")
    return cast(RepairMutationOutcome, value)


def _rollback_state(value: str) -> RepairRollbackState:
    if value not in {"available", "restored", "review_required"}:
        raise RuntimeError("repair rollback state is invalid")
    return cast(RepairRollbackState, value)


def _outbox_state(value: str) -> RepairOutboxState:
    if value not in {"pending", "published", "acknowledged", "failed"}:
        raise RuntimeError("repair outbox state is invalid")
    return cast(RepairOutboxState, value)


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise RuntimeError("repair mutation object has a non-string key")
        return {cast(str, key): _json_value(item) for key, item in value.items()}
    if hasattr(value, "iso_format"):
        formatted = value.iso_format()
        if isinstance(formatted, str):
            return formatted
    raise RuntimeError("repair mutation value is not JSON serializable")


def _canonical_json(value: dict[str, JsonValue]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
