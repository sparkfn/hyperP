"""Immutable structural created-object specifications for CRM-deal repair."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import cast

from src.crm_deal_identity_repair.mutation_models import RepairMutationCommand, RepairMutationPlan
from src.graph.crm_deal_identity_repair_mutation_errors import (
    RepairMutationAuthorityError,
    RepairMutationDriftError,
)
from src.models import JsonValue, SourceRecordEnvelope
from src.pipeline_crm_identity import projected_identifiers
from src.pipeline_normalization import normalize_envelope_attributes, normalize_envelope_identifiers
from src.source_version_keys import encode_source_version_key


def _created_object_specifications(
    request: RepairMutationCommand,
    plan: RepairMutationPlan,
    snapshot: dict[str, JsonValue],
    envelope: SourceRecordEnvelope | None,
) -> list[JsonValue]:
    """Describe every node and relationship created by this mutation."""
    specifications = _structural_object_specifications(request, plan, snapshot, envelope)
    if plan.disposition != "applied" or envelope is None:
        return specifications
    candidates = snapshot.get("created_identifier_candidates")
    if not isinstance(candidates, list) or plan.selected_person_id is None:
        raise RepairMutationDriftError("repair identifier pre-state is malformed")
    for ordinal, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise RepairMutationDriftError("repair identifier candidate is malformed")
        identifier_type = candidate.get("identifier_type")
        identifier_scope = candidate.get("identifier_scope")
        normalized_value = candidate.get("normalized_value")
        preexisting = candidate.get("preexisting")
        if not isinstance(identifier_type, str) or not isinstance(identifier_scope, str):
            raise RepairMutationDriftError("repair identifier candidate identity is malformed")
        if not isinstance(normalized_value, str) or not isinstance(preexisting, bool):
            raise RepairMutationDriftError("repair identifier candidate state is malformed")
        identifier: dict[str, JsonValue] = {
            "identifier_type": identifier_type,
            "identifier_scope": identifier_scope,
            "normalized_value": normalized_value,
        }
        identifier_input = _staging_identifier_input(envelope, identifier_type, normalized_value)
        source_instance_id = identifier_input["source_instance_id"]
        is_verified = identifier_input["is_verified"]
        quality_flag = identifier_input["quality_flag"]
        if source_instance_id is not None and not isinstance(source_instance_id, str):
            raise RepairMutationDriftError("repair identifier source instance is malformed")
        if not isinstance(is_verified, bool) or not isinstance(quality_flag, str):
            raise RepairMutationDriftError("repair identifier staging properties are malformed")
        transaction_datetime: dict[str, JsonValue] = {"dynamic": "transaction_datetime"}
        specifications.extend(
            [
                {
                    "object_kind": "Identifier",
                    "identity": identifier,
                    "preexisting": preexisting,
                    "write_mode": "preserved" if preexisting else "created",
                    "on_create_properties": (
                        {}
                        if preexisting
                        else _persisted_properties(
                            {
                                "source_instance_id": source_instance_id,
                                "created_at": transaction_datetime,
                                "repair_mutation_id": request.mutation_id,
                            }
                        )
                    ),
                    "multiplicity_ordinal": ordinal,
                },
                _relationship_specification(
                    "IDENTIFIED_BY",
                    {"person_id": plan.selected_person_id},
                    identifier,
                    {
                        "source_system_key": "bitrix_chat",
                        "source_record_pk": plan.source_record_pk,
                        "is_verified": is_verified,
                        "verification_method": None,
                        "is_active": True,
                        "quality_flag": quality_flag,
                        "first_seen_at": transaction_datetime,
                        "last_seen_at": transaction_datetime,
                        "last_confirmed_at": transaction_datetime,
                        "repair_mutation_id": request.mutation_id,
                    },
                    ordinal,
                ),
            ]
        )
    _, facts = _staging_projection(envelope)
    for ordinal, fact in enumerate(facts):
        specifications.append(
            _relationship_specification(
                "HAS_FACT",
                {"person_id": plan.selected_person_id},
                {"source_record_pk": plan.source_record_pk},
                {
                    "attribute_name": fact["attribute_name"],
                    "attribute_value": fact["attribute_value"],
                    "source_record_pk": plan.source_record_pk,
                    "source_trust_tier": 2,
                    "confidence": 1.0,
                    "quality_flag": fact["quality_flag"],
                    "is_active": True,
                    "is_current_hint": False,
                    "observed_at": envelope.observed_at,
                    "created_at": {"dynamic": "transaction_datetime"},
                    "repair_mutation_id": request.mutation_id,
                },
                ordinal,
            )
        )
    return specifications


def _structural_object_specifications(
    request: RepairMutationCommand,
    plan: RepairMutationPlan,
    snapshot: dict[str, JsonValue],
    envelope: SourceRecordEnvelope | None,
) -> list[JsonValue]:
    source_properties = _replacement_source_properties(request, plan, snapshot, envelope)
    source_endpoint = {"source_record_pk": plan.source_record_pk}
    decision_endpoint = {"match_decision_id": request.mutation_id + ":decision"}
    specifications: list[JsonValue] = [
        _node_specification("SourceRecord", source_endpoint, source_properties),
        _relationship_specification(
            "FROM_SOURCE",
            source_endpoint,
            {"source_key": "bitrix_chat"},
            {"repair_mutation_id": request.mutation_id},
            0,
        ),
        _relationship_specification(
            "PREVIOUS_VERSION_OF",
            {"source_record_pk": request.inventory.source_record_pk},
            source_endpoint,
            {"repair_mutation_id": request.mutation_id},
            0,
        ),
        _node_specification(
            "MatchDecision",
            decision_endpoint,
            {
                "match_decision_id": request.mutation_id + ":decision",
                "engine_type": "deterministic",
                "engine_version": "crm_deal_identity_repair_v1",
                "decision": "merge" if plan.disposition == "applied" else "review",
                "confidence": 1.0,
                "reasons": list(plan.reason_codes),
                "blocking_conflicts": [],
                "review_candidate_person_ids": list(plan.current_owner_ids),
                "feature_snapshot": _canonical_json(
                    {
                        "authority_digest": plan.authority_digest,
                        "repair_mutation_id": request.mutation_id,
                    }
                ),
                "policy_version": "crm_deal_identity_v2",
                "repair_mutation_id": request.mutation_id,
                "created_at": {"dynamic": "transaction_datetime"},
                "retention_expires_at": None,
            },
        ),
        _relationship_specification(
            "ABOUT_LEFT",
            decision_endpoint,
            source_endpoint,
            {"entity_type": "source_record", "repair_mutation_id": request.mutation_id},
            0,
        ),
    ]
    specifications.extend(_owned_by_specifications(request, snapshot, source_endpoint))
    if plan.disposition == "applied":
        assert plan.selected_person_id is not None
        person_endpoint = {"person_id": plan.selected_person_id}
        specifications.extend(
            [
                _relationship_specification(
                    "ABOUT_RIGHT",
                    decision_endpoint,
                    person_endpoint,
                    {"entity_type": "person", "repair_mutation_id": request.mutation_id},
                    0,
                ),
                _relationship_specification(
                    "LINKED_TO",
                    source_endpoint,
                    person_endpoint,
                    {
                        "is_active": True,
                        "provisional": False,
                        "authoritative": True,
                        "source_record_pk": plan.source_record_pk,
                        "repair_mutation_id": request.mutation_id,
                        "linked_at": {"dynamic": "transaction_datetime"},
                    },
                    0,
                ),
            ]
        )
        return specifications
    if plan.provisional_person_id is not None:
        person_endpoint = {"person_id": plan.provisional_person_id}
        specifications.extend(
            [
                _relationship_specification(
                    "ABOUT_RIGHT",
                    decision_endpoint,
                    person_endpoint,
                    {"entity_type": "person", "repair_mutation_id": request.mutation_id},
                    0,
                ),
                _relationship_specification(
                    "LINKED_TO",
                    source_endpoint,
                    person_endpoint,
                    {
                        "is_active": False,
                        "provisional": True,
                        "authoritative": False,
                        "source_record_pk": plan.source_record_pk,
                        "repair_mutation_id": request.mutation_id,
                        "linked_at": {"dynamic": "transaction_datetime"},
                    },
                    0,
                ),
            ]
        )
    review_endpoint = {"review_case_id": request.mutation_id + ":review"}
    sla_due_at = None
    if envelope is not None and envelope.observed_at is not None:
        observed = datetime.fromisoformat(envelope.observed_at.replace("Z", "+00:00"))
        sla_due_at = (observed + timedelta(days=7)).isoformat()
    specifications.extend(
        [
            _node_specification(
                "ReviewCase",
                review_endpoint,
                {
                    "review_case_id": request.mutation_id + ":review",
                    "priority": 100,
                    "queue_state": "open",
                    "assigned_to": None,
                    "follow_up_at": None,
                    "sla_due_at": sla_due_at,
                    "resolution": None,
                    "resolved_at": None,
                    "actions": [],
                    "created_at": {"dynamic": "transaction_datetime"},
                    "updated_at": {"dynamic": "transaction_datetime"},
                    "repair_mutation_id": request.mutation_id,
                },
            ),
            _relationship_specification(
                "FOR_DECISION",
                review_endpoint,
                decision_endpoint,
                {"repair_mutation_id": request.mutation_id},
                0,
            ),
        ]
    )
    return specifications


def _replacement_source_properties(
    request: RepairMutationCommand,
    plan: RepairMutationPlan,
    snapshot: dict[str, JsonValue],
    envelope: SourceRecordEnvelope | None,
) -> dict[str, JsonValue]:
    source = snapshot.get("source")
    if not isinstance(source, dict):
        raise RepairMutationDriftError("repair source snapshot is malformed")
    properties = dict(source)
    dynamic: dict[str, JsonValue] = {"dynamic": "transaction_datetime"}
    if envelope is None:
        properties.update(
            {
                "source_record_pk": plan.source_record_pk,
                "source_record_version": str(plan.source_record_version),
                "source_version_key": encode_source_version_key(
                    "bitrix_chat",
                    request.inventory.source_record_id,
                    str(plan.source_record_version),
                    source_instance_id=request.source_instance_id,
                ),
                "expected_active_source_record_pk": request.inventory.source_record_pk,
                "lifecycle_status": "pending_review",
                "is_latest": True,
                "link_status": "pending_review",
                "ingested_at": dynamic,
                "repair_mutation_id": request.mutation_id,
                "repair_reconstruction_status": "unreconstructable_review_only",
                "review_staged_at": dynamic,
            }
        )
        return properties
    values = _source_values(request, plan)
    properties.update(
        {
            "source_record_pk": plan.source_record_pk,
            "source_record_id": values["source_record_id"],
            "source_instance_id": values["source_instance_id"],
            "source_record_version": values["source_record_version"],
            "source_version_key": values["source_version_key"],
            "entity_key": values["entity_key"],
            "expected_active_source_record_pk": request.inventory.source_record_pk,
            "lifecycle_status": "active" if plan.disposition == "applied" else "pending_review",
            "is_latest": True,
            "link_status": values["link_status"],
            "record_type": "crm_deal",
            "observed_at": values["observed_at"],
            "ingested_at": dynamic,
            "record_hash": values["record_hash"],
            "raw_payload": values["raw_payload"],
            "normalized_payload": values["normalized_payload"],
            "source_entity_type": "deal",
            "source_entity_id": values["deal_id"],
            "identity_policy_version": "crm_deal_identity_v2",
            "identity_link_key": values["identity_link_key"],
            "repair_mutation_id": request.mutation_id,
            "activated_at" if plan.disposition == "applied" else "review_staged_at": dynamic,
        }
    )
    _apply_replacement_source_derived_properties(properties)
    return properties


def _apply_replacement_source_derived_properties(properties: dict[str, JsonValue]) -> None:
    """Mirror the source-record MERGE map's only derived, potentially new property."""
    crm_deal_stage_id = properties.get("crm_deal_stage_id")
    if crm_deal_stage_id is None:
        crm_deal_stage_id = properties.get("stage_id")
    if crm_deal_stage_id is None:
        properties.pop("crm_deal_stage_id", None)
    else:
        properties["crm_deal_stage_id"] = crm_deal_stage_id


def _owned_by_specifications(
    request: RepairMutationCommand,
    snapshot: dict[str, JsonValue],
    source_endpoint: Mapping[str, JsonValue],
) -> list[JsonValue]:
    relationships = snapshot.get("relationships")
    if not isinstance(relationships, list):
        raise RepairMutationDriftError("repair relationship snapshot is malformed")
    specifications: list[JsonValue] = []
    for relation in relationships:
        if not isinstance(relation, dict) or relation.get("relationship_type") != "OWNED_BY":
            continue
        expected_left = {
            "labels": ["SourceRecord"],
            "key": "source_record_pk",
            "value": request.inventory.source_record_pk,
        }
        if relation.get("left_identity") != expected_left:
            continue
        right_identity = relation.get("right_identity")
        if not isinstance(right_identity, dict) or right_identity.get("key") != "entity_key":
            raise RepairMutationDriftError("repair ownership snapshot is malformed")
        entity_key = right_identity.get("value")
        if not isinstance(entity_key, str) or not entity_key:
            raise RepairMutationDriftError("repair ownership identity is malformed")
        specifications.append(
            _relationship_specification(
                "OWNED_BY",
                source_endpoint,
                {"entity_key": entity_key},
                {"repair_mutation_id": request.mutation_id},
                len(specifications),
            )
        )
    return specifications


def _node_specification(
    object_kind: str,
    identity: Mapping[str, JsonValue],
    properties: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    return {
        "object_kind": object_kind,
        "identity": dict(identity),
        "properties": _persisted_properties(properties),
        "preexisting": False,
        "write_mode": "created",
        "multiplicity_ordinal": 0,
    }


def _relationship_specification(
    relationship_type: str,
    left_endpoint: Mapping[str, JsonValue],
    right_endpoint: Mapping[str, JsonValue],
    properties: Mapping[str, JsonValue],
    ordinal: int,
) -> dict[str, JsonValue]:
    return {
        "object_kind": relationship_type,
        "preexisting": False,
        "write_mode": "created",
        "direction": "outgoing",
        "left_endpoint": dict(left_endpoint),
        "right_endpoint": dict(right_endpoint),
        "properties": _persisted_properties(properties),
        "multiplicity_ordinal": ordinal,
    }


def _persisted_properties(properties: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: value for key, value in properties.items() if value is not None}


def _staging_identifier_input(
    envelope: SourceRecordEnvelope, identifier_type: str, normalized_value: str
) -> dict[str, JsonValue]:
    identifiers, _ = _staging_projection(envelope)
    candidates = [
        item
        for item in identifiers
        if item["identifier_type"] == identifier_type
        and item["normalized_value"] == normalized_value
    ]
    if len(candidates) != 1:
        raise RepairMutationDriftError("repair identifier payload does not match staged identity")
    return candidates[0]


def _staging_projection(
    envelope: SourceRecordEnvelope,
) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
    identifiers: list[dict[str, JsonValue]] = [
        {
            "identifier_type": item.identifier_type,
            "normalized_value": item.normalized_value,
            "source_instance_id": item.source_instance_id,
            "is_verified": item.is_verified,
            "quality_flag": item.quality_flag.value,
        }
        for item in projected_identifiers(envelope, normalize_envelope_identifiers(envelope))
        if item.quality_flag.value != "invalid_format"
    ]
    facts: list[dict[str, JsonValue]] = [
        {
            "attribute_name": item.attribute_name,
            "attribute_value": item.attribute_value,
            "quality_flag": item.quality_flag.value,
        }
        for item in normalize_envelope_attributes(envelope)
        if item.quality_flag.value != "invalid_format"
    ]
    return identifiers, facts


def _source_values(
    request: RepairMutationCommand,
    plan: RepairMutationPlan,
) -> dict[str, str]:
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
