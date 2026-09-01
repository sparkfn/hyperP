"""Typed Neo4j boundary conversion and persistence parameters for verification."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol, TypedDict, cast, runtime_checkable

from neo4j import Record

from src.crm_deal_identity_repair.digests import object_digest, verification_result_digest
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
from src.graph.crm_deal_identity_repair_verification_records import VerificationBundle
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


class RetirementRequirement(TypedDict):
    """Exact authenticated pre/post relationship requirement, including duplicates."""

    relationship_type: str
    left_identity: Mapping[str, JsonValue]
    right_identity: Mapping[str, JsonValue]
    properties: Mapping[str, JsonValue]
    frozen_active: bool
    multiplicity_ordinal: int


class RetirementSnapshot(TypedDict):
    relationship_type: str
    left_identity: Mapping[str, JsonValue]
    right_identity: Mapping[str, JsonValue]
    properties: Mapping[str, JsonValue]
    mutation_timestamp_present: bool | None


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
    """Return only frozen root/descendant source identities in canonical order."""
    pks = {command.inventory.source_record_pk}
    for descendant in _payload_rows(command, "descendants"):
        pks.add(_required_payload_string(descendant, "source_record_pk", "descendant"))
    return tuple(sorted(pks))


def _payload_rows(
    command: RepairVerificationCommand,
    key: str,
) -> tuple[Mapping[str, JsonValue], ...]:
    value = command.inventory.payload.get(key)
    if not isinstance(value, list):
        raise RepairVerificationDriftError("verification frozen inventory list is malformed")
    return tuple(_required_payload_mapping_value(item, "inventory row") for item in value)


def _required_payload_mapping(
    row: Mapping[str, JsonValue],
    key: str,
    label: str,
) -> Mapping[str, JsonValue]:
    value = row.get(key)
    if not isinstance(value, dict) or not all(isinstance(item_key, str) for item_key in value):
        raise RepairVerificationDriftError(f"verification frozen {label} is malformed")
    return value


def _required_payload_string(
    row: Mapping[str, JsonValue],
    key: str,
    label: str,
) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise RepairVerificationDriftError(f"verification frozen {label} is malformed")
    return value


def retirement_requirements(
    command: RepairVerificationCommand,
    bundle: VerificationBundle,
) -> tuple[RetirementRequirement, ...]:
    """Return #309-authenticated exact retirement rows, preserving duplicate order."""
    body = _required_payload_mapping(bundle.rollback_payload, "payload", "rollback payload")
    pre_state = _required_payload_mapping(body, "pre_state", "rollback payload")
    rows = pre_state.get("relationships")
    if not isinstance(rows, list):
        raise RepairVerificationDriftError("verification rollback relationships are malformed")
    retired = set(retired_source_record_pks(command))
    requirements: list[RetirementRequirement] = []
    for row_value in rows:
        row = _required_payload_mapping_value(row_value, "rollback relationship")
        relationship_type = _required_payload_string(row, "relationship_type", "relationship")
        if relationship_type not in {
            "LINKED_TO",
            "IDENTIFIED_BY",
            "LIVES_AT",
            "HAS_FACT",
            "DESCRIBES_ADDRESS",
        }:
            continue
        properties = _required_payload_mapping(row, "relationship_properties", "relationship")
        left_identity = _required_payload_mapping(row, "left_identity", "relationship")
        right_identity = _required_payload_mapping(row, "right_identity", "relationship")
        if not _is_retired_relationship(relationship_type, left_identity, properties, retired):
            continue
        requirements.append(
            {
                "relationship_type": relationship_type,
                "left_identity": left_identity,
                "right_identity": right_identity,
                "properties": properties,
                "frozen_active": _relationship_active(properties),
                "multiplicity_ordinal": _required_payload_ordinal(row),
            }
        )
    expected = tuple(sorted(requirements, key=_retirement_requirement_sort_key))
    if len({_retirement_requirement_key(item) for item in expected}) != len(expected):
        raise RepairVerificationDriftError("verification rollback relationship ordinal differs")
    return expected


def _required_payload_mapping_value(value: JsonValue, label: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RepairVerificationDriftError(f"verification frozen {label} is malformed")
    return value


def _is_retired_relationship(
    relationship_type: str,
    left_identity: Mapping[str, JsonValue],
    properties: Mapping[str, JsonValue],
    retired: set[str],
) -> bool:
    if relationship_type in {"LINKED_TO", "DESCRIBES_ADDRESS"}:
        return _identity_value(left_identity) in retired
    source_pk = properties.get("source_record_pk")
    return isinstance(source_pk, str) and source_pk in retired


def _identity_value(identity: Mapping[str, JsonValue]) -> str | None:
    value = identity.get("value")
    return value if isinstance(value, str) else None


def _relationship_active(properties: Mapping[str, JsonValue]) -> bool:
    value = properties.get("is_active", True)
    if not isinstance(value, bool):
        raise RepairVerificationDriftError("verification frozen relationship activity is malformed")
    return value


def _required_payload_ordinal(row: Mapping[str, JsonValue]) -> int:
    value = row.get("multiplicity_ordinal")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RepairVerificationDriftError(
            "verification rollback relationship ordinal is malformed"
        )
    return value


def _retirement_requirement_key(item: RetirementRequirement) -> tuple[str, str, str, int]:
    return (
        item["relationship_type"],
        _canonical_json_value(item["left_identity"]),
        _canonical_json_value(item["right_identity"]),
        item["multiplicity_ordinal"],
    )


def _retirement_requirement_sort_key(item: RetirementRequirement) -> tuple[str, str, str, int]:
    return _retirement_requirement_key(item)


def _canonical_json_value(value: Mapping[str, JsonValue]) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def retirement_snapshot_matches(
    requirements: tuple[RetirementRequirement, ...],
    current: tuple[RetirementSnapshot, ...],
    mutation_id: str,
) -> bool:
    """Compare exact endpoints/properties and duplicate multiplicity without leaking values."""
    expected = tuple(_expected_retirement_snapshot(item, mutation_id) for item in requirements)
    canonical_expected = sorted(_snapshot_key(item) for item in expected)
    canonical_current = sorted(_snapshot_key(item) for item in current)
    return canonical_expected == canonical_current


def _expected_retirement_snapshot(
    requirement: RetirementRequirement, mutation_id: str
) -> RetirementSnapshot:
    properties = dict(requirement["properties"])
    if requirement["frozen_active"]:
        properties["is_active"] = False
        properties["retired_by_repair_mutation_id"] = mutation_id
        properties.pop("updated_at", None)
    return {
        "relationship_type": requirement["relationship_type"],
        "left_identity": requirement["left_identity"],
        "right_identity": requirement["right_identity"],
        "properties": properties,
        "mutation_timestamp_present": True if requirement["frozen_active"] else None,
    }


def _snapshot_key(item: RetirementSnapshot) -> tuple[str, str, str, str, str]:
    return (
        item["relationship_type"],
        _canonical_json_value(item["left_identity"]),
        _canonical_json_value(item["right_identity"]),
        _canonical_json_value(item["properties"]),
        str(item["mutation_timestamp_present"]),
    )


def retirement_snapshot_from_row(row: Record, mutation_id: str) -> RetirementSnapshot:
    properties = dict(json_mapping(row, "relationship_properties"))
    active = properties.get("is_active", True)
    if not isinstance(active, bool):
        raise RepairVerificationDriftError(
            "verification retired relationship activity is malformed"
        )
    mutation_timestamp_present: bool | None = None
    if not active and properties.get("retired_by_repair_mutation_id") == mutation_id:
        # #309 owns this timestamp only for rows it retired.  A pre-existing
        # inactive row remains a complete immutable property-map comparison.
        mutation_timestamp_present = properties.get("updated_at") is not None
        properties.pop("updated_at", None)
    return {
        "relationship_type": required_row_string(row, "relationship_type"),
        "left_identity": _snapshot_endpoint_identity(row, "left"),
        "right_identity": _snapshot_endpoint_identity(row, "right"),
        "properties": properties,
        "mutation_timestamp_present": mutation_timestamp_present,
    }


def _snapshot_endpoint_identity(row: Record, side: str) -> Mapping[str, JsonValue]:
    identity = row[side + "_identity"]
    if isinstance(identity, dict):
        return json_object(identity)
    labels_value = json_value(row[side + "_labels"])
    properties = json_mapping(row, side + "_properties")
    if not isinstance(labels_value, list) or not all(
        isinstance(label, str) for label in labels_value
    ):
        raise RepairVerificationDriftError(
            "verification relationship endpoint labels are malformed"
        )
    for key in (
        "source_record_pk",
        "person_id",
        "match_decision_id",
        "review_case_id",
        "identifier_key",
        "address_id",
        "fact_id",
        "entity_key",
    ):
        value = properties.get(key)
        if isinstance(value, str) and value:
            return {"labels": labels_value, "key": key, "value": value}
    return {
        "labels": labels_value,
        "properties_digest": object_digest(b"graph-endpoint-v1\x00", dict(properties)),
    }


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


def build_replay_unit_equation(
    outcome: str,
    active_links: int,
    provisional_links: int,
    forbidden: int,
    records: tuple[RepairSecondaryDisposition, ...],
) -> RepairUnitEquation:
    committed = build_unit_equation(outcome, active_links, provisional_links, forbidden, records)
    values = dict(committed.__dict__)
    values["first_commit_attempt_count"] = 0
    values["replay_no_op_count"] = 1
    return RepairUnitEquation(**values)


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
            required_row_string(row, "link_status") == "linked"
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
