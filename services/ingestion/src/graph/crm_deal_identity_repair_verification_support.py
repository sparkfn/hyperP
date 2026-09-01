"""Typed Neo4j boundary conversion and persistence parameters for verification."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol, TypedDict, cast, runtime_checkable

from neo4j import Record

from src.crm_deal_identity_repair.digests import verification_result_digest
from src.crm_deal_identity_repair.execution_models import (
    RepairOutboxEvent,
    RepairSecondaryDisposition,
    RepairVerificationResult,
)
from src.crm_deal_identity_repair.execution_records import RepairSecondaryOutcome
from src.crm_deal_identity_repair.verification_models import (
    RepairSecondaryDispositionDetail,
    RepairUnitEquation,
    RepairVerificationCommand,
)
from src.graph.crm_deal_identity_repair_verification_errors import RepairVerificationDriftError
from src.models import JsonValue


class _BundleParameters(TypedDict):
    run_id: str
    unit_id: str
    generation: int
    sequence: int
    attempt: int
    boundary_digest: str
    unit_fingerprint: str
    inventory_binding_digest: str
    fence_id: str
    owner_id: str
    fence_token: str
    source_instance_id: str
    control_instance_id: str
    mutation_id: str
    rollback_image_id: str
    checkpoint_id: str
    outbox_event_id: str


class _OutboxParameters(_BundleParameters):
    event_id: str
    outbox_payload_digest: str
    outbox_evidence_digest: str
    claim_token: str
    claim_digest: str


class _PersistParameters(_OutboxParameters):
    verification_id: str
    verification_digest: str
    subject_fingerprint: str
    evidence_digest: str
    payload_digest: str
    expected_disposition_count: int
    dispositions: list[dict[str, JsonValue]]
    request_digest: str


def bundle_parameters(command: RepairVerificationCommand) -> _BundleParameters:
    return {
        "run_id": command.unit.run_id,
        "unit_id": command.unit.unit_id,
        "generation": command.unit.generation,
        "sequence": command.unit.sequence,
        "attempt": command.unit.attempt,
        "boundary_digest": command.unit.boundary_digest,
        "unit_fingerprint": command.unit.inventory_fingerprint,
        "inventory_binding_digest": command.unit.inventory_binding_digest or "",
        "fence_id": command.fence.fence_id,
        "owner_id": command.owner_id,
        "fence_token": command.fence.token,
        "source_instance_id": command.source_instance_id,
        "control_instance_id": command.control_instance_id,
        "mutation_id": command.mutation_id,
        "rollback_image_id": command.rollback_image_id,
        "checkpoint_id": command.checkpoint_id,
        "outbox_event_id": command.outbox_event_id,
    }


def outbox_parameters(
    command: RepairVerificationCommand, outbox: RepairOutboxEvent
) -> _OutboxParameters:
    return {
        **bundle_parameters(command),
        "event_id": outbox.event_id,
        "outbox_payload_digest": outbox.payload_digest,
        "outbox_evidence_digest": outbox.evidence_digest,
        "claim_token": command.claim_token,
        "claim_digest": command.claim_digest,
    }


def persist_parameters(
    command: RepairVerificationCommand,
    verification: RepairVerificationResult,
    details: list[RepairSecondaryDispositionDetail],
    dispositions: tuple[RepairSecondaryDisposition, ...],
    outbox: RepairOutboxEvent,
) -> _PersistParameters:
    items: list[dict[str, JsonValue]] = [
        {
            "disposition_id": record.disposition_id,
            "subject_fingerprint": record.subject_fingerprint,
            "evidence_digest": record.evidence_digest,
            "payload_digest": record.payload_digest,
            "outcome": record.outcome,
            "action": detail.action,
            "subject_kind": detail.subject.kind,
            "subject_stable_id": detail.subject.stable_id,
        }
        for detail, record in zip(details, dispositions, strict=True)
    ]
    return {
        **outbox_parameters(command, outbox),
        "verification_id": verification.verification_id,
        "verification_digest": verification.verification_digest,
        "subject_fingerprint": verification.subject_fingerprint,
        "evidence_digest": verification.evidence_digest,
        "payload_digest": verification.payload_digest,
        "expected_disposition_count": len(items),
        "dispositions": items,
        "request_digest": command.request_digest,
        "fence_token": command.fence.token,
    }


def retired_source_record_pks(command: RepairVerificationCommand) -> tuple[str, ...]:
    payload = command.inventory.payload
    descendants = payload.get("descendants")
    pks = [command.inventory.source_record_pk]
    if isinstance(descendants, list):
        for row in descendants:
            if isinstance(row, dict) and isinstance(row.get("source_record_pk"), str):
                pks.append(cast(str, row["source_record_pk"]))
    return tuple(sorted(set(pks)))


def postcondition_closure_source_record_pks(
    command: RepairVerificationCommand,
    replacement_pk: str,
) -> tuple[str, ...]:
    """Return retired PKs plus only the replacement for forbidden-projection checks."""
    return tuple(sorted((*retired_source_record_pks(command), replacement_pk)))


def build_verification_record(
    command: RepairVerificationCommand, evidence_digest: str, state: str
) -> RepairVerificationResult:
    digest = verification_result_digest(
        {"request_digest": command.request_digest, "derived_state_digest": state}
    )
    return RepairVerificationResult(
        command.unit.run_id,
        command.unit.unit_id,
        command.verification_id,
        command.unit.generation,
        command.unit.sequence,
        command.unit.attempt,
        command.owner_id,
        command.fence.token,
        command.unit.boundary_digest,
        command.unit.inventory_fingerprint,
        digest,
        evidence_digest,
        state,
        "verified",
    )


def build_unit_equation(
    outcome: str,
    active_links: int,
    provisional_links: int,
    forbidden: int,
    records: tuple[RepairSecondaryDisposition, ...],
) -> RepairUnitEquation:
    reconciled = sum(item.outcome == "reconciled" for item in records)
    review = sum(item.outcome == "review_required" for item in records)
    return RepairUnitEquation(
        1,
        int(outcome == "applied"),
        int(outcome == "review_required"),
        0,
        1,
        0,
        0,
        0,
        int(outcome == "applied"),
        active_links,
        provisional_links,
        forbidden,
        len(records),
        len(records),
        reconciled,
        review,
        0,
        0,
        0,
    )


def json_mapping(row: Record, key: str) -> Mapping[str, JsonValue]:
    value = row[key]
    if not isinstance(value, dict):
        raise RepairVerificationDriftError("verification graph JSON record is malformed")
    converted: dict[str, JsonValue] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str):
            raise RepairVerificationDriftError("verification graph JSON key is malformed")
        converted[item_key] = json_value(item_value)
    return converted


def json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        converted: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RepairVerificationDriftError("verification graph key is malformed")
            converted[key] = json_value(item)
        return converted
    if isinstance(value, _IsoFormatValue):
        return value.iso_format()
    raise RepairVerificationDriftError("verification graph value is not JSON-safe")


@runtime_checkable
class _IsoFormatValue(Protocol):
    def iso_format(self) -> str: ...


def nonnegative_row_count(row: Record, key: str) -> int:
    value: object = row[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RepairVerificationDriftError("negative-control snapshot count is malformed")
    return value


def required_record_int(row: Record, key: str) -> int:
    value: object = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RepairVerificationDriftError("verification graph count is malformed")
    return value


def mapping(row: Record, key: str) -> Mapping[str, JsonValue]:
    return json_mapping(row, key)


def json_object(value: object) -> dict[str, JsonValue]:
    converted = json_value(value)
    if not isinstance(converted, dict):
        raise RepairVerificationDriftError("verification graph object is malformed")
    return converted


def json_list(row: Record, key: str) -> list[JsonValue]:
    value = json_value(row[key])
    if not isinstance(value, list):
        raise RepairVerificationDriftError("verification graph list is malformed")
    return value


def list_mappings(row: Record, key: str) -> list[Mapping[str, JsonValue]]:
    value = row[key]
    if not isinstance(value, list):
        raise RepairVerificationDriftError("verification graph list is malformed")
    if any(not isinstance(item, dict) for item in value):
        raise RepairVerificationDriftError("verification graph list item is malformed")
    return [json_object(item) for item in value]


def strings(row: Record, key: str) -> tuple[str, ...]:
    value = row[key]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RepairVerificationDriftError("verification replacement sources are malformed")
    return tuple(cast(str, item) for item in value)


def verification_from_properties(values: Mapping[str, JsonValue]) -> RepairVerificationResult:
    return RepairVerificationResult(
        required_str(values, "run_id"),
        required_str(values, "unit_id"),
        required_str(values, "verification_id"),
        required_int(values, "generation"),
        required_int(values, "sequence"),
        required_int(values, "attempt"),
        required_str(values, "owner_id"),
        required_str(values, "fence_token"),
        required_str(values, "boundary_digest"),
        required_str(values, "subject_fingerprint"),
        required_str(values, "verification_digest"),
        required_str(values, "evidence_digest"),
        required_str(values, "payload_digest"),
        cast(Literal["pending", "verified", "drifted", "failed"], required_str(values, "outcome")),
    )


def disposition_from_properties(values: Mapping[str, JsonValue]) -> RepairSecondaryDisposition:
    return RepairSecondaryDisposition(
        required_str(values, "run_id"),
        required_str(values, "unit_id"),
        required_str(values, "disposition_id"),
        required_int(values, "generation"),
        required_int(values, "sequence"),
        required_int(values, "attempt"),
        required_str(values, "owner_id"),
        required_str(values, "control_token"),
        required_str(values, "boundary_digest"),
        required_str(values, "subject_fingerprint"),
        required_str(values, "evidence_digest"),
        required_str(values, "payload_digest"),
        cast(RepairSecondaryOutcome, required_str(values, "outcome")),
    )


def required_str(values: Mapping[str, JsonValue], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise RepairVerificationDriftError("verification record string is invalid")
    return value


def required_int(values: Mapping[str, JsonValue], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RepairVerificationDriftError("verification record integer is invalid")
    return value


def json_scalar(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise RepairVerificationDriftError("verification aggregate is not JSON scalar")


def required_row_string(row: Record, key: str) -> str:
    value: object = row[key]
    if not isinstance(value, str) or not value:
        raise RepairVerificationDriftError("verification graph string is malformed")
    return value


def optional_nonnegative_int(row: Record, key: str) -> int:
    value: object = row[key]
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RepairVerificationDriftError("verification graph counter is malformed")
    return value


def primary_matches(row: Record, outcome: str) -> bool:
    """Validate the post-mutation link/review/projection invariants."""
    active_links = required_record_int(row, "active_links")
    forbidden = required_record_int(row, "forbidden_projection_count")
    retired = required_record_int(row, "retirement_stamp_failure_count")
    if forbidden != 0 or retired != 0:
        return False
    if outcome == "applied":
        return (
            required_row_string(row, "link_status") == "applied"
            and active_links == 1
            and required_record_int(row, "active_any_links") == 1
            and required_record_int(row, "provisional_links") == 0
            and required_record_int(row, "repair_review_count") == 0
            and required_record_int(row, "repair_decision_count") == 0
        )
    return (
        required_row_string(row, "link_status") == "pending_review"
        and active_links == 0
        and required_record_int(row, "active_any_links") == 0
        and required_record_int(row, "active_new_evidence") == 0
        and required_record_int(row, "repair_review_count") == 1
        and required_record_int(row, "repair_decision_count") == 1
        and required_record_int(row, "all_links") == required_record_int(row, "provisional_links")
        and required_record_int(row, "provisional_links") <= 1
    )
