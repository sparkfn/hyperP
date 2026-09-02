"""Canonical current-state comparison for the #309 rollback image."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.rollback_models import RepairRollbackDrift
from src.graph.crm_deal_identity_repair_mutation_state import (
    is_transaction_datetime,
    normalize_repaired_state,
)
from src.graph.crm_deal_identity_repair_rollback_image import RollbackImageBundle
from src.graph.crm_deal_identity_repair_rollback_postcondition import (
    postcondition_history_matches,
)
from src.models import JsonValue

__all__ = (
    "compare_current_state",
    "desired_post_rollback_state",
    "expected_current_state",
    "normalize_post_rollback_state",
    "postcondition_history_matches",
    "restoration_ambiguity",
)


def expected_current_state(bundle: RollbackImageBundle) -> dict[str, JsonValue]:
    """Build the complete pre-existing retired plus mutation-created CAS multiset."""
    pre_nodes: list[dict[str, JsonValue]] = []
    changed = dict(bundle.source_properties)
    changed["lifecycle_status"] = "superseded"
    changed["is_latest"] = False
    changed["superseded_at"] = {"dynamic": "transaction_datetime"}
    pre_nodes.append(
        {
            "object_kind": "SourceRecord",
            "identity": {"source_record_pk": bundle.source_record_pk},
            "properties": changed,
        }
    )
    pre_nodes.extend(
        {
            "object_kind": "SourceRecord",
            "identity": {"source_record_pk": source_record_pk},
            "properties": properties,
        }
        for source_record_pk, properties in bundle.descendant_properties
    )
    retired = [
        _retired_row(row, bundle.retired_source_record_pks) for row in bundle.relationship_rows
    ]
    created = bundle.expected_repaired_state
    raw_nodes = created.get("nodes")
    raw_relationships = created.get("relationships")
    nodes: list[dict[str, JsonValue]] = (
        [dict(item) for item in raw_nodes if isinstance(item, dict)]
        if isinstance(raw_nodes, list)
        else []
    )
    relationships: list[dict[str, JsonValue]] = (
        [dict(item) for item in raw_relationships if isinstance(item, dict)]
        if isinstance(raw_relationships, list)
        else []
    )
    nodes.extend(pre_nodes)
    relationships.extend(retired)
    return _normalize_rollback_state(
        _state_rows(
            {"nodes": cast(JsonValue, nodes), "relationships": cast(JsonValue, relationships)}
        )
    )


def desired_post_rollback_state(
    bundle: RollbackImageBundle, rollback_image_id: str
) -> dict[str, JsonValue]:
    """Build the authoritative pre-state multiset and inert replacement invariants."""
    nodes: list[dict[str, JsonValue]] = [
        {
            "object_kind": "SourceRecord",
            "identity": {"source_record_pk": bundle.source_record_pk},
            "properties": bundle.source_properties,
        }
    ]
    nodes.extend(
        {
            "object_kind": "SourceRecord",
            "identity": {"source_record_pk": source_pk},
            "properties": properties,
        }
        for source_pk, properties in bundle.descendant_properties
    )
    return _normalize_rollback_state(
        _state_rows(
            {
                "nodes": cast(JsonValue, nodes),
                "relationships": cast(JsonValue, [dict(row) for row in bundle.relationship_rows]),
            }
        )
    )


def normalize_post_rollback_state(state: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Normalize observed post-rollback rows into the v1 image canonical schema."""
    return _normalize_rollback_state(_state_rows(state))


def compare_current_state(
    bundle: RollbackImageBundle, current: Mapping[str, JsonValue]
) -> RepairRollbackDrift | None:
    """Compare exact canonical CAS projections, exposing only stable IDs and reason codes."""
    expected = expected_current_state(bundle)
    observed = _normalize_rollback_state(_state_rows(dict(current)), bundle.mutation_id)
    if expected == observed:
        return None
    rows = _mismatch_rows(expected, observed)
    return RepairRollbackDrift.from_rows(tuple(rows))


def _mismatch_rows(
    expected: dict[str, JsonValue], observed: dict[str, JsonValue]
) -> list[tuple[str, str]]:
    mismatches: list[tuple[str, str]] = []
    expected_rows = _rows(expected)
    observed_rows = _rows(observed)
    for identity in sorted(set(expected_rows) | set(observed_rows)):
        if identity not in observed_rows:
            mismatches.append((identity, "missing"))
        elif identity not in expected_rows:
            mismatches.append((identity, "unexpected"))
        elif expected_rows[identity] != observed_rows[identity]:
            mismatches.append((identity, "properties_or_multiplicity_changed"))
    return mismatches or [("rollback-image", "state_digest_changed")]


def _normalize_rollback_state(
    state: dict[str, JsonValue], mutation_id: str | None = None
) -> dict[str, JsonValue]:
    """Extend #309 normalization for the root supersession write it performs."""
    normalized = normalize_repaired_state(state)
    result = _normalize_mutation_dynamic_values(normalized, mutation_id=mutation_id)
    if not isinstance(result, dict):
        raise RuntimeError("rollback normalized state is malformed")
    return result


def _normalize_mutation_dynamic_values(
    value: JsonValue, key: str | None = None, mutation_id: str | None = None
) -> JsonValue:
    if key == "superseded_at":
        if is_transaction_datetime(value):
            return {"dynamic": "transaction_datetime"}
        return value
    if key == "retired_by_repair_mutation_id" and (isinstance(value, dict) or value == mutation_id):
        return {"dynamic": "repair_mutation_id"}
    if isinstance(value, dict):
        return {
            item_key: _normalize_mutation_dynamic_values(item, item_key, mutation_id)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_mutation_dynamic_values(item, mutation_id=mutation_id) for item in value]
    return value


def _rows(value: dict[str, JsonValue]) -> dict[str, str]:
    rows: dict[str, str] = {}
    payload = value.get("expected_repaired_state", value)
    if not isinstance(payload, dict):
        return {"rollback-image": _json(payload)}
    for group in ("nodes", "relationships"):
        entries = payload.get(group, [])
        if not isinstance(entries, list):
            return {"rollback-image": "invalid-" + group}
        for ordinal, entry in enumerate(entries):
            identity = _identity(entry, group, ordinal)
            rows[identity] = _json(entry)
    return rows


def _state_rows(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    state: dict[str, JsonValue] = {}
    for group in ("nodes", "relationships"):
        entries = value.get(group)
        if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
            raise RuntimeError("rollback current-state rows are malformed")
        object_entries = [item for item in entries if isinstance(item, dict)]
        normalized_entries: list[dict[str, JsonValue]] = []
        for item in object_entries:
            row = dict(item)
            # Image ordinals order complete canonical rows; they are not state.
            row.pop("multiplicity_ordinal", None)
            normalized_entries.append(_canonical_row(row, group))
        ordered = sorted(normalized_entries, key=_json)
        counts: dict[str, int] = {}
        rows: list[JsonValue] = []
        for row in ordered:
            key = _json(row)
            ordinal = counts.get(key, 0)
            counts[key] = ordinal + 1
            row["multiplicity_ordinal"] = ordinal
            rows.append(row)
        state[group] = rows
    return state


def _canonical_row(row: dict[str, JsonValue], group: str) -> dict[str, JsonValue]:
    """Reduce every #309 relationship shape to one endpoint/property schema."""
    if group != "relationships":
        return row
    if "relationship_properties" in row:
        relationship_type = row.get("relationship_type")
        properties = row.get("relationship_properties")
        if not isinstance(relationship_type, str) or not isinstance(properties, dict):
            raise RuntimeError("rollback snapshot relationship shape is malformed")
        return {
            "object_kind": relationship_type,
            "direction": row.get("direction", "outgoing"),
            "left_endpoint": _canonical_endpoint(
                row.get("left_identity"), row.get("left_labels"), row.get("left_properties")
            ),
            "right_endpoint": _canonical_endpoint(
                row.get("right_identity"), row.get("right_labels"), row.get("right_properties")
            ),
            "properties": properties,
        }
    relation_type = row.get("object_kind")
    properties = row.get("properties")
    left = row.get("left_endpoint")
    right = row.get("right_endpoint")
    if (
        not isinstance(relation_type, str)
        or not isinstance(properties, dict)
        or not isinstance(left, dict)
        or not isinstance(right, dict)
    ):
        raise RuntimeError("rollback observed relationship shape is malformed")
    return {
        "object_kind": relation_type,
        "direction": row.get("direction", "outgoing"),
        "left_endpoint": _canonical_endpoint(left, None, None),
        "right_endpoint": _canonical_endpoint(right, None, None),
        "properties": properties,
    }


def _canonical_endpoint(
    identity: JsonValue | None, labels: JsonValue | None, properties: JsonValue | None
) -> dict[str, JsonValue]:
    if not isinstance(identity, dict):
        if isinstance(labels, list) and isinstance(properties, dict):
            return _canonical_endpoint(
                _snapshot_endpoint_identity(labels, properties), labels, properties
            )
        raise RuntimeError("rollback relationship endpoint is malformed")
    key = identity.get("key")
    value = identity.get("value")
    if isinstance(key, str) and isinstance(value, str) and value:
        return {"key": key, "value": value}
    for candidate in (
        "source_record_pk",
        "person_id",
        "match_decision_id",
        "review_case_id",
        "identifier_key",
        "address_id",
        "fact_id",
        "entity_key",
        "source_key",
    ):
        candidate_value = identity.get(candidate)
        if isinstance(candidate_value, str) and candidate_value:
            return {"key": candidate, "value": candidate_value}
    composite = ("identifier_type", "identifier_scope", "normalized_value")
    if all(isinstance(identity.get(key), str) and identity.get(key) for key in composite):
        return {key: identity[key] for key in composite}
    if "properties_digest" in identity:
        if not isinstance(labels, list) or not isinstance(properties, dict):
            raise RuntimeError("rollback digest endpoint lacks snapshot properties")
        if identity["properties_digest"] != object_digest(b"graph-endpoint-v1\x00", properties):
            raise RuntimeError("rollback endpoint properties digest differs")
        if "SourceSystem" in labels:
            source_key = properties.get("source_key")
            if isinstance(source_key, str) and source_key:
                return {"key": "source_key", "value": source_key}
        composite = ("identifier_type", "identifier_scope", "normalized_value")
        if "Identifier" in labels and all(
            isinstance(properties.get(key), str) and properties.get(key) for key in composite
        ):
            return {key: properties[key] for key in composite}
        return {
            "labels": cast(JsonValue, sorted(label for label in labels if isinstance(label, str))),
            "properties_digest": identity["properties_digest"],
        }
    if isinstance(labels, list) and isinstance(properties, dict):
        snapshot = _snapshot_endpoint_identity(labels, properties)
        return _canonical_endpoint(snapshot, labels, properties)
    raise RuntimeError("rollback relationship endpoint lacks canonical identity")


def _identity(value: JsonValue, group: str, ordinal: int) -> str:
    if not isinstance(value, dict):
        return group + ":invalid:" + str(ordinal)
    for key in ("identity", "left_endpoint"):
        item = value.get(key)
        if isinstance(item, dict):
            for identifier in (
                "source_record_pk",
                "person_id",
                "match_decision_id",
                "review_case_id",
            ):
                candidate = item.get(identifier)
                if isinstance(candidate, str) and candidate:
                    return group + ":" + identifier + ":" + candidate + ":" + str(ordinal)
    kind = value.get("object_kind")
    return group + ":" + (kind if isinstance(kind, str) else "unknown") + ":" + str(ordinal)


def _json(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def restoration_ambiguity(bundle: RollbackImageBundle) -> RepairRollbackDrift | None:
    """Retain the legacy hook while restoration validates graph cardinality in-transaction.

    #312 assigns canonical rows to transaction-local relationship instances only
    after it has locked and cardinality-checked each restore group.  A v1 image
    therefore does not need a persistent relationship ID merely because property
    rows differ; only an observed cardinality/identity failure is compensable.
    """
    del bundle
    return None


def _retired_row(
    row: dict[str, JsonValue], retired_source_record_pks: tuple[str, ...]
) -> dict[str, JsonValue]:
    current = dict(row)
    # The ordinal is a canonical-image ordering aid, not a stored relationship
    # locator.  It cannot distinguish current duplicate graph relationships.
    current.pop("multiplicity_ordinal", None)
    properties = current.get("relationship_properties")
    relation_type = current.get("relationship_type")
    if not isinstance(properties, dict) or not isinstance(relation_type, str):
        return current
    source_pk = properties.get("source_record_pk")
    left = current.get("left_identity")
    left_value = left.get("value") if isinstance(left, dict) else None
    retired = source_pk in retired_source_record_pks or left_value in retired_source_record_pks
    projection = relation_type in {
        "LINKED_TO",
        "IDENTIFIED_BY",
        "LIVES_AT",
        "HAS_FACT",
        "DESCRIBES_ADDRESS",
    }
    if retired and projection and properties.get("is_active", True) is True:
        changed = dict(properties)
        changed["is_active"] = False
        changed["retired_by_repair_mutation_id"] = {"dynamic": "repair_mutation_id"}
        changed["updated_at"] = {"dynamic": "transaction_datetime"}
        current["relationship_properties"] = changed
    return current


def _snapshot_endpoint_identity(
    labels: JsonValue | None, properties: JsonValue | None
) -> dict[str, JsonValue]:
    if not isinstance(labels, list) or not isinstance(properties, dict):
        raise RuntimeError("rollback snapshot endpoint is malformed")
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
