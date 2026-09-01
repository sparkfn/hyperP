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
_NODE_KINDS = frozenset({"SourceRecord", "MatchDecision", "ReviewCase", "Identifier"})


def expected_repaired_state(
    request: RepairMutationCommand,
    envelope: SourceRecordEnvelope | None,
    plan: RepairMutationPlan,
    snapshot: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Build the complete mutation-owned domain projection from immutable specs."""
    specifications = _created_object_specifications(request, plan, snapshot, envelope)
    nodes = [_specified_node_row(item) for item in specifications if _is_created_node(item)]
    relationships = [
        _specified_relationship_row(item)
        for item in specifications
        if _is_created_relationship(item)
    ]
    return normalize_repaired_state(
        {
            "nodes": canonical_state_rows(nodes),
            "relationships": canonical_state_rows(relationships),
        }
    )


def canonical_state_rows(rows: list[dict[str, JsonValue]]) -> list[JsonValue]:
    """Canonically order exact graph rows while retaining duplicate multiplicity."""
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


def _is_created_node(specification: JsonValue) -> bool:
    if not isinstance(specification, dict):
        return False
    object_kind = specification.get("object_kind")
    if object_kind not in _NODE_KINDS or specification.get("preexisting") is not False:
        return False
    return specification.get("write_mode") == "created"


def _is_created_relationship(specification: JsonValue) -> bool:
    if not isinstance(specification, dict):
        return False
    return (
        isinstance(specification.get("object_kind"), str)
        and "left_endpoint" in specification
        and "right_endpoint" in specification
        and specification.get("preexisting") is False
        and specification.get("write_mode") == "created"
    )


def _specified_node_row(specification: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(specification, dict):
        raise RuntimeError("repair created-object node specification is malformed")
    object_kind = specification.get("object_kind")
    identity = specification.get("identity")
    if not isinstance(object_kind, str) or not isinstance(identity, dict):
        raise RuntimeError("repair created-object node identity is malformed")
    properties = specification.get("properties")
    if object_kind == "Identifier":
        on_create_properties = specification.get("on_create_properties")
        if not isinstance(on_create_properties, dict):
            raise RuntimeError("repair created-object identifier properties are malformed")
        properties = {**identity, **on_create_properties}
    if not isinstance(properties, dict):
        raise RuntimeError("repair created-object node properties are malformed")
    return {
        "object_kind": object_kind,
        "identity": dict(identity),
        "properties": dict(properties),
    }


def _specified_relationship_row(specification: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(specification, dict):
        raise RuntimeError("repair created-object relationship specification is malformed")
    object_kind = specification.get("object_kind")
    left_endpoint = specification.get("left_endpoint")
    right_endpoint = specification.get("right_endpoint")
    properties = specification.get("properties")
    if (
        not isinstance(object_kind, str)
        or not isinstance(left_endpoint, dict)
        or not isinstance(right_endpoint, dict)
        or not isinstance(properties, dict)
    ):
        raise RuntimeError("repair created-object relationship fields are malformed")
    return {
        "object_kind": object_kind,
        "direction": "outgoing",
        "left_endpoint": dict(left_endpoint),
        "right_endpoint": dict(right_endpoint),
        "properties": dict(properties),
    }


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
