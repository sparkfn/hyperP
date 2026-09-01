"""Canonical payload, snapshot, guard, and ledger helpers for CRM-deal repair."""

from __future__ import annotations

import json
from typing import NotRequired, TypedDict, cast

from neo4j import ManagedTransaction, Record

from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.mutation_models import (
    RepairMutationCommand,
    RepairMutationPlan,
    RepairRollbackPayload,
)
from src.graph.crm_deal_identity_repair_mutation_errors import (
    RepairMutationAuthorityError,
    RepairMutationDriftError,
)
from src.graph.crm_deal_identity_repair_mutation_state import (
    canonical_state_rows,
    normalize_repaired_state,
)
from src.graph.crm_deal_identity_repair_mutation_structures import (
    _created_object_specifications,
)
from src.graph.queries.crm_deal_identity_repair_mutation import (
    READ_MUTATION_GRAPH_SNAPSHOT,
    READ_REPAIR_IDENTIFIER_PREEXISTENCE,
    VERIFY_REPAIRED_MUTATION_POSTCONDITIONS,
)
from src.identifier_scopes import identifier_scope
from src.models import JsonValue, SourceRecordEnvelope
from src.pipeline_crm_identity import projected_identifiers
from src.pipeline_normalization import normalize_envelope_identifiers
from src.source_version_keys import encode_source_version_key


class _GuardParameters(TypedDict):
    run_id: str
    unit_id: str
    generation: int
    sequence: int
    attempt: int
    boundary_digest: str
    unit_fingerprint: str
    fence_id: str
    owner_id: str
    fence_token: str
    source_instance_id: str
    control_instance_id: str
    source_record_pk: str
    source_record_id: str
    inventory_key: str
    inventory_graph_fingerprint: str
    inventory_stored_payload_fingerprint: str
    inventory_binding_digest: str
    mutation_id: str
    quoted_source_record_pk: str
    new_source_record_pk: NotRequired[str]
    new_lifecycle_status: NotRequired[str]


class _SourceParameters(TypedDict):
    old_source_record_pk: str
    new_source_record_pk: str
    source_record_id: str
    source_instance_id: str
    source_record_version: str
    source_version_key: str
    entity_key: str
    observed_at: str
    record_hash: str
    raw_payload: str
    normalized_payload: str
    deal_id: str
    identity_link_key: str
    link_status: str
    mutation_id: str


class _LedgerParameters(TypedDict):
    run_id: str
    unit_id: str
    generation: int
    sequence: int
    attempt: int
    owner_id: str
    fence_id: str
    fence_token: str
    boundary_digest: str
    unit_fingerprint: str
    expected_unit_state: str
    unit_state: str
    mutation_id: str
    rollback_image_id: str
    checkpoint_id: str
    outbox_event_id: str
    source_fingerprint: str
    image_digest: str
    repaired_state_digest: str
    evidence_digest: str
    payload_digest: str
    rollback_payload_json: str
    result_digest: str
    request_digest: str
    checkpoint_digest: str
    outbox_payload_digest: str
    outcome: str
    control_instance_id: str
    new_source_record_pk: str


def _snapshot(
    tx: ManagedTransaction,
    request: RepairMutationCommand,
    retired_source_record_pks: tuple[str, ...],
    envelope: SourceRecordEnvelope | None,
) -> dict[str, JsonValue]:
    row = tx.run(
        READ_MUTATION_GRAPH_SNAPSHOT,
        source_record_pk=request.inventory.source_record_pk,
        retired_source_record_pks=list(retired_source_record_pks),
    ).single()
    if row is None:
        raise RepairMutationDriftError("repair rollback snapshot source is missing")
    snapshot = {key: _json_value(row[key]) for key in row.keys()}
    relationships = snapshot.get("relationships")
    if isinstance(relationships, list):
        snapshot["relationships"] = _ordinal_relationships(relationships)
    snapshot["created_identifier_candidates"] = _identifier_preexistence(tx, envelope)
    return snapshot


def _identifier_preexistence(
    tx: ManagedTransaction, envelope: SourceRecordEnvelope | None
) -> list[JsonValue]:
    if envelope is None:
        return []
    identifiers = projected_identifiers(envelope, normalize_envelope_identifiers(envelope))
    rows = [
        {
            "identifier_type": item.identifier_type,
            "identifier_scope": identifier_scope(item.identifier_type, item.source_instance_id),
            "normalized_value": item.normalized_value,
        }
        for item in identifiers
        if item.quality_flag.value != "invalid_format"
    ]
    return [
        {key: _json_value(row[key]) for key in row.keys()}
        for row in tx.run(READ_REPAIR_IDENTIFIER_PREEXISTENCE, identifiers=rows)
    ]


def _ordinal_relationships(rows: list[JsonValue]) -> list[JsonValue]:
    canonical_rows: list[tuple[str, dict[str, JsonValue]]] = []
    for value in rows:
        if not isinstance(value, dict):
            raise RuntimeError("rollback relationship snapshot is malformed")
        row = dict(value)
        row["left_identity"] = _endpoint_identity(
            row.get("left_labels"), row.get("left_properties")
        )
        row["right_identity"] = _endpoint_identity(
            row.get("right_labels"), row.get("right_properties")
        )
        canonical_rows.append((_canonical_json(row), row))
    canonical_rows.sort(key=lambda item: item[0])
    result: list[JsonValue] = []
    counts: dict[str, int] = {}
    for key, row in canonical_rows:
        ordinal = counts.get(key, 0)
        counts[key] = ordinal + 1
        row["multiplicity_ordinal"] = ordinal
        result.append(row)
    return result


def _endpoint_identity(labels: JsonValue | None, properties: JsonValue | None) -> JsonValue:
    if not isinstance(labels, list) or not isinstance(properties, dict):
        raise RuntimeError("rollback relationship endpoint is malformed")
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
            return {"labels": labels, "key": key, "value": value}
    return {
        "labels": labels,
        "properties_digest": object_digest(b"graph-endpoint-v1\x00", properties),
    }


def _rollback_payload(
    request: RepairMutationCommand,
    plan: RepairMutationPlan,
    snapshot: dict[str, JsonValue],
    expected_state: dict[str, JsonValue],
    envelope: SourceRecordEnvelope | None,
) -> RepairRollbackPayload:
    created_specs = _created_object_specifications(request, plan, snapshot, envelope)
    return RepairRollbackPayload(
        payload={
            "contract_version": request.mutation_contract_version,
            "request": request.to_dict(),
            "authority_context": {
                "current_owner_ids": list(plan.current_owner_ids),
                "authority_digest": plan.authority_digest,
                "external_authority_digest": plan.external_authority_digest,
            },
            "desired_state": plan.desired_state(),
            "pre_state": snapshot,
            "created_object_specifications": created_specs,
            "rollback_operations": [
                {
                    "operation": "delete_created_relationships_by_repair_mutation_id",
                    "repair_mutation_id": request.mutation_id,
                    "relationship_types": [
                        "LINKED_TO",
                        "ABOUT_LEFT",
                        "ABOUT_RIGHT",
                        "FOR_DECISION",
                        "IDENTIFIED_BY",
                        "HAS_FACT",
                        "FROM_SOURCE",
                        "PREVIOUS_VERSION_OF",
                        "OWNED_BY",
                    ],
                },
                {
                    "operation": "delete_created_nodes_and_identifiers",
                    "source_record_pk": plan.source_record_pk,
                    "match_decision_id": request.mutation_id + ":decision",
                    "review_case_id": request.mutation_id + ":review",
                    "identifier_repair_mutation_id": request.mutation_id,
                    "identifier_candidates": snapshot.get("created_identifier_candidates", []),
                    "created_object_specifications": created_specs,
                    "delete_identifier_only_when_preexisting_is_false": True,
                },
                {
                    "operation": "restore_source_and_relationship_properties",
                    "source_record_pk": request.inventory.source_record_pk,
                    "relationships": snapshot.get("relationships", []),
                },
            ],
        },
        expected_repaired_state=expected_state,
    )


def _postcondition_state(tx: ManagedTransaction, source_record_pk: str) -> dict[str, JsonValue]:
    row = tx.run(
        VERIFY_REPAIRED_MUTATION_POSTCONDITIONS,
        new_source_record_pk=source_record_pk,
    ).single()
    if row is None:
        raise RuntimeError("repair postcondition readback is missing")
    return normalize_repaired_state(
        {
            "nodes": canonical_state_rows(_object_rows(row["nodes"])),
            "relationships": canonical_state_rows(_object_rows(row["relationships"])),
        }
    )


def _source_values(
    request: RepairMutationCommand,
    plan: RepairMutationPlan,
) -> _SourceParameters:
    payload = plan.source_record_payload
    if payload is None:
        raise RepairMutationAuthorityError("repair source payload is unavailable")
    source_record_id = _payload_string(payload, "source_record_id")
    raw_payload = _payload_object(payload, "raw_payload")
    normalized_payload: dict[str, JsonValue] = {
        "identifiers": _json_value(payload.get("identifiers", [])),
        "addresses": _json_value(payload.get("addresses", [])),
        "attributes": _json_value(payload.get("attributes", {})),
    }
    return {
        "old_source_record_pk": request.inventory.source_record_pk,
        "new_source_record_pk": plan.source_record_pk,
        "source_record_id": source_record_id,
        "source_instance_id": request.source_instance_id,
        "source_record_version": str(plan.source_record_version),
        "source_version_key": encode_source_version_key(
            "bitrix_chat",
            source_record_id,
            str(plan.source_record_version),
            source_instance_id=request.source_instance_id,
        ),
        "entity_key": _payload_string(payload, "entity_key"),
        "observed_at": _payload_string(payload, "observed_at"),
        "record_hash": _payload_string(payload, "record_hash"),
        "raw_payload": _canonical_json(raw_payload),
        "normalized_payload": _canonical_json(normalized_payload),
        "deal_id": request.inventory.deal_id,
        "identity_link_key": _payload_string(payload, "identity_link_key"),
        "link_status": "linked" if plan.disposition == "applied" else "pending_review",
        "mutation_id": request.mutation_id,
    }


def _ledger_parameters(
    request: RepairMutationCommand,
    plan: RepairMutationPlan,
    rollback: RepairRollbackPayload,
    result_digest: str,
    outbox_digest: str,
) -> _LedgerParameters:
    checkpoint_digest = object_digest(
        b"crm-deal-identity-repair-checkpoint-v1\x00",
        {"result_digest": result_digest},
    )
    return {
        "run_id": request.unit.run_id,
        "unit_id": request.unit.unit_id,
        "generation": request.unit.generation,
        "sequence": request.unit.sequence,
        "attempt": request.unit.attempt,
        "owner_id": request.fence.owner_id,
        "fence_id": request.fence.fence_id,
        "fence_token": request.fence.token,
        "boundary_digest": request.unit.boundary_digest,
        "unit_fingerprint": request.unit.inventory_fingerprint,
        "expected_unit_state": request.unit.state,
        "unit_state": plan.disposition,
        "mutation_id": request.mutation_id,
        "rollback_image_id": request.rollback_image_id,
        "checkpoint_id": request.checkpoint_id,
        "outbox_event_id": request.outbox_event_id,
        "source_fingerprint": request.inventory.graph_fingerprint,
        "image_digest": rollback.image_digest,
        "repaired_state_digest": rollback.expected_repaired_digest,
        "evidence_digest": plan.authority_digest,
        "payload_digest": rollback.image_digest,
        "rollback_payload_json": _canonical_json(rollback.to_dict()),
        "result_digest": result_digest,
        "request_digest": request.request_digest,
        "checkpoint_digest": checkpoint_digest,
        "outbox_payload_digest": outbox_digest,
        "outcome": plan.disposition,
        "control_instance_id": request.control_instance_id,
        "new_source_record_pk": plan.source_record_pk,
    }


def _guard_parameters(request: RepairMutationCommand) -> _GuardParameters:
    return {
        "run_id": request.unit.run_id,
        "unit_id": request.unit.unit_id,
        "generation": request.unit.generation,
        "sequence": request.unit.sequence,
        "attempt": request.unit.attempt,
        "boundary_digest": request.unit.boundary_digest,
        "unit_fingerprint": request.unit.inventory_fingerprint,
        "fence_id": request.fence.fence_id,
        "owner_id": request.fence.owner_id,
        "fence_token": request.fence.token,
        "source_instance_id": request.source_instance_id,
        "control_instance_id": request.control_instance_id,
        "source_record_pk": request.inventory.source_record_pk,
        "source_record_id": request.inventory.source_record_id,
        "inventory_key": request.unit.inventory_key or "",
        "inventory_graph_fingerprint": request.unit.inventory_graph_fingerprint or "",
        "inventory_stored_payload_fingerprint": (
            request.unit.inventory_stored_payload_fingerprint or ""
        ),
        "inventory_binding_digest": request.unit.inventory_binding_digest or "",
        "mutation_id": request.mutation_id,
        "quoted_source_record_pk": json.dumps(request.inventory.source_record_pk),
    }


def _record_object(row: Record, key: str) -> dict[str, JsonValue]:
    value = row[key]
    if not isinstance(value, dict):
        raise RuntimeError("repair mutation readback is malformed: " + key)
    return {cast(str, item_key): _json_value(item) for item_key, item in value.items()}


def _required_record_string(row: Record, key: str) -> str:
    value = row[key]
    if not isinstance(value, str) or not value:
        raise RuntimeError("repair graph row is malformed: " + key)
    return value


def _record_int(row: Record, key: str) -> int:
    value: object = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("repair graph count is malformed: " + key)
    return value


def _object_rows(value: object) -> list[dict[str, JsonValue]]:
    if not isinstance(value, list):
        raise RuntimeError("repair authority rows are malformed")
    rows: list[dict[str, JsonValue]] = []
    for item in value:
        if item is None:
            continue
        converted = _json_value(item)
        if not isinstance(converted, dict):
            raise RuntimeError("repair authority row is malformed")
        rows.append(converted)
    return rows


def _payload_object(payload: dict[str, JsonValue], key: str) -> dict[str, JsonValue]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RepairMutationAuthorityError("repair source payload is malformed: " + key)
    return value


def _payload_string(payload: dict[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RepairMutationAuthorityError("repair source payload is malformed: " + key)
    return value


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise RuntimeError("repair graph object has non-string keys")
        return {cast(str, key): _json_value(item) for key, item in value.items()}
    if hasattr(value, "iso_format"):
        formatted = value.iso_format()
        if isinstance(formatted, str):
            return formatted
    raise RuntimeError("repair graph value is not JSON serializable")


def _canonical_json(value: dict[str, JsonValue]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
