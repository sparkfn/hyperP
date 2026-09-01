"""Canonical repaired-state projections for CRM-deal repair exact replay."""

from __future__ import annotations

import json
from datetime import datetime

from src.crm_deal_identity_repair.mutation_models import (
    RepairMutationCommand,
    RepairMutationPlan,
)
from src.graph.crm_deal_identity_repair_mutation_structures import (
    _created_object_specifications,
)
from src.models import JsonValue, SourceRecordEnvelope

_TRANSACTION_DYNAMIC_KEYS = frozenset(
    {
        "ingested_at",
        "activated_at",
        "review_staged_at",
        "created_at",
        "updated_at",
        "linked_at",
        "first_seen_at",
        "last_seen_at",
        "last_confirmed_at",
    }
)


def expected_repaired_state(
    request: RepairMutationCommand,
    envelope: SourceRecordEnvelope | None,
    plan: RepairMutationPlan,
    snapshot: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Build the exact persisted replacement-source and active-evidence projection."""
    specifications = _created_object_specifications(request, plan, snapshot, envelope)
    source_properties = _specified_node_properties(specifications, "SourceRecord")
    state: dict[str, JsonValue] = {
        "source_properties": source_properties,
        "links": specified_relationship_rows(specifications, "LINKED_TO", "person_id"),
        "identified_by": specified_relationship_rows(
            specifications,
            "IDENTIFIED_BY",
            "identifier",
        ),
        "lives_at": specified_relationship_rows(specifications, "LIVES_AT", "address"),
        "has_fact": specified_relationship_rows(specifications, "HAS_FACT", "source_record_pk"),
        "describes_address": specified_relationship_rows(
            specifications,
            "DESCRIBES_ADDRESS",
            "address",
        ),
    }
    return normalize_repaired_state(state)


def specified_relationship_rows(
    specifications: list[JsonValue],
    object_kind: str,
    endpoint_kind: str,
) -> list[JsonValue]:
    """Reduce rollback structural specifications to replay-relevant relationship rows."""
    rows: list[dict[str, JsonValue]] = []
    for item in specifications:
        if not isinstance(item, dict) or item.get("object_kind") != object_kind:
            continue
        properties = item.get("properties")
        right_endpoint = item.get("right_endpoint")
        if not isinstance(properties, dict) or not isinstance(right_endpoint, dict):
            raise RuntimeError("repair created-object relationship specification is malformed")
        endpoint = _relationship_endpoint(right_endpoint, endpoint_kind)
        left_endpoint = item.get("left_endpoint")
        if not isinstance(left_endpoint, dict):
            raise RuntimeError("repair created-object relationship left endpoint is malformed")
        person_id = left_endpoint.get("person_id")
        row: dict[str, JsonValue] = {"properties": dict(properties), "endpoint": endpoint}
        if isinstance(person_id, str) and person_id:
            row["person_id"] = person_id
        rows.append(row)
    return canonical_state_rows(rows)


def canonical_state_rows(rows: list[dict[str, JsonValue]]) -> list[JsonValue]:
    """Canonically order exact relationship rows while retaining duplicates."""
    ordered = sorted((dict(row) for row in rows), key=_canonical_json)
    result: list[JsonValue] = []
    counts: dict[str, int] = {}
    for row in ordered:
        key = _canonical_json(row)
        ordinal = counts.get(key, 0)
        counts[key] = ordinal + 1
        row["multiplicity_ordinal"] = ordinal
        result.append(row)
    return result


def normalize_repaired_state(state: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Normalize transaction timestamps and Neo4j temporal strings for stable digests."""
    normalized = _normalize_repaired_value(state)
    if not isinstance(normalized, dict):
        raise RuntimeError("repair repaired-state projection is malformed")
    return normalized


def _specified_node_properties(
    specifications: list[JsonValue], object_kind: str
) -> dict[str, JsonValue]:
    matches = [
        item
        for item in specifications
        if isinstance(item, dict) and item.get("object_kind") == object_kind
    ]
    if len(matches) != 1:
        raise RuntimeError("repair created-object source specification is malformed")
    properties = matches[0].get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("repair created-object source properties are malformed")
    return dict(properties)


def _relationship_endpoint(
    right_endpoint: dict[str, JsonValue], endpoint_kind: str
) -> dict[str, JsonValue]:
    if endpoint_kind == "identifier":
        endpoint = {
            key: right_endpoint[key]
            for key in ("identifier_type", "identifier_scope", "normalized_value")
            if key in right_endpoint
        }
    elif endpoint_kind == "address":
        endpoint = dict(right_endpoint)
    else:
        endpoint = {endpoint_kind: right_endpoint.get(endpoint_kind)}
    if not endpoint or any(value is None for value in endpoint.values()):
        raise RuntimeError("repair created-object relationship endpoint is malformed")
    return endpoint


def _normalize_repaired_value(value: JsonValue, key: str | None = None) -> JsonValue:
    if key in _TRANSACTION_DYNAMIC_KEYS:
        return {"dynamic": "transaction_datetime"}
    if isinstance(value, list):
        return [_normalize_repaired_value(item) for item in value]
    if isinstance(value, dict):
        return {
            item_key: _normalize_repaired_value(item, item_key) for item_key, item in value.items()
        }
    if isinstance(value, str):
        return _normalized_iso(value)
    return value


def _normalized_iso(value: str) -> str:
    if "T" not in value:
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


def _canonical_json(value: dict[str, JsonValue]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
