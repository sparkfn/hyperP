"""Duplicate-safe, fixed-locator restoration rows for #312."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import cast

from src.crm_deal_identity_repair.digests import object_digest
from src.graph.crm_deal_identity_repair_rollback_image import RepairRollbackIntegrityError
from src.models import JsonValue


def restore_rows(rows: tuple[dict[str, JsonValue], ...]) -> list[dict[str, JsonValue]]:
    """Build one assignment group per safe endpoint/provenance locator.

    #309's ordinal is for a complete canonical row.  Property-distinct rows in
    one locator group can both be zero, so a fresh group ordinal is assigned.
    """
    grouped: dict[str, list[dict[str, JsonValue]]] = defaultdict(list)
    for row in rows:
        left = _locator(row, "left")
        right = _locator(row, "right")
        properties = _properties(row)
        source_pk = properties.get("source_record_pk")
        if source_pk is not None and not isinstance(source_pk, str):
            raise RepairRollbackIntegrityError("rollback relationship source identity is malformed")
        relationship_type = _required_string(row, "relationship_type")
        group = _group_key(relationship_type, left, right, source_pk)
        grouped[group].append(
            {
                "properties": properties,
                "image_multiplicity_ordinal": _image_ordinal(row),
            }
        )
    result: list[dict[str, JsonValue]] = []
    for group, assignments in sorted(grouped.items()):
        ordered = sorted(assignments, key=_canonical_json)
        for ordinal, assignment in enumerate(ordered):
            assignment["restore_ordinal"] = ordinal
        relationship_type, left, right, source_pk = _group_parts(group)
        result.append(
            {
                "restore_group": group,
                "relationship_type": relationship_type,
                "left_mode": left["mode"],
                "left_value": left.get("value"),
                "left_value_2": left.get("value_2"),
                "left_value_3": left.get("value_3"),
                "right_mode": right["mode"],
                "right_value": right.get("value"),
                "right_value_2": right.get("value_2"),
                "right_value_3": right.get("value_3"),
                "source_record_pk": source_pk,
                "group_size": len(ordered),
                "assignments": cast(JsonValue, ordered),
            }
        )
    return result


def _locator(row: dict[str, JsonValue], side: str) -> dict[str, str]:
    identity = row.get(side + "_identity")
    labels = row.get(side + "_labels")
    properties = row.get(side + "_properties")
    if not isinstance(identity, dict):
        raise RepairRollbackIntegrityError("rollback relationship endpoint is malformed")
    label_values = _labels(identity, labels)
    key = identity.get("key")
    value = identity.get("value")
    if isinstance(key, str) and isinstance(value, str) and value:
        mode = _simple_mode(key, label_values)
        if mode is not None:
            return {"mode": mode, "value": value}
    if not isinstance(properties, dict):
        raise RepairRollbackIntegrityError("rollback digest endpoint properties are missing")
    _validate_digest_identity(identity, properties)
    identifier_type = properties.get("identifier_type")
    identifier_scope = properties.get("identifier_scope")
    normalized_value = properties.get("normalized_value")
    if "Identifier" in label_values and all(
        isinstance(item, str) and item
        for item in (identifier_type, identifier_scope, normalized_value)
    ):
        return {
            "mode": "identifier_composite",
            "value": cast(str, identifier_type),
            "value_2": cast(str, identifier_scope),
            "value_3": cast(str, normalized_value),
        }
    source_key = properties.get("source_key")
    if "SourceSystem" in label_values and isinstance(source_key, str) and source_key:
        return {"mode": "source_system", "value": source_key}
    raise RepairRollbackIntegrityError(
        "rollback relationship endpoint lacks a supported fixed locator"
    )


def _labels(identity: dict[str, JsonValue], labels: JsonValue | None) -> tuple[str, ...]:
    if isinstance(labels, list):
        label_values = tuple(sorted(label for label in labels if isinstance(label, str)))
        if len(label_values) != len(labels):
            raise RepairRollbackIntegrityError(
                "rollback relationship endpoint labels are malformed"
            )
        return label_values
    key = identity.get("key")
    if not isinstance(key, str):
        raise RepairRollbackIntegrityError("rollback digest endpoint labels are missing")
    inferred = {
        "source_record_pk": "SourceRecord",
        "person_id": "Person",
        "match_decision_id": "MatchDecision",
        "review_case_id": "ReviewCase",
        "identifier_key": "Identifier",
        "address_id": "Address",
        "fact_id": "Fact",
        "entity_key": "Entity",
        "source_key": "SourceSystem",
    }.get(key)
    if inferred is None:
        raise RepairRollbackIntegrityError("rollback digest endpoint labels are missing")
    return (inferred,)


def _validate_digest_identity(
    identity: dict[str, JsonValue], properties: dict[str, JsonValue]
) -> None:
    digest = identity.get("properties_digest")
    if digest is not None and (
        not isinstance(digest, str) or digest != object_digest(b"graph-endpoint-v1\x00", properties)
    ):
        raise RepairRollbackIntegrityError("rollback endpoint properties digest differs")


def _simple_mode(key: str, labels: tuple[str, ...]) -> str | None:
    modes = {
        "source_record_pk": ("source_record_pk", "SourceRecord"),
        "person_id": ("person_id", "Person"),
        "match_decision_id": ("match_decision_id", "MatchDecision"),
        "review_case_id": ("review_case_id", "ReviewCase"),
        "identifier_key": ("identifier_key", "Identifier"),
        "address_id": ("address_id", "Address"),
        "fact_id": ("fact_id", "Fact"),
        "entity_key": ("entity_key", "Entity"),
        "source_key": ("source_system", "SourceSystem"),
    }
    entry = modes.get(key)
    return entry[0] if entry is not None and entry[1] in labels else None


def _properties(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
    value = row.get("relationship_properties")
    if not isinstance(value, dict):
        raise RepairRollbackIntegrityError("rollback relationship properties are malformed")
    return value


def _image_ordinal(row: dict[str, JsonValue]) -> int:
    value = row.get("multiplicity_ordinal")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RepairRollbackIntegrityError("rollback relationship multiplicity is malformed")
    return value


def _group_key(
    relationship_type: str,
    left: dict[str, str],
    right: dict[str, str],
    source_pk: JsonValue | None,
) -> str:
    return _canonical_json(
        {
            "relationship_type": relationship_type,
            "left": cast(JsonValue, left),
            "right": cast(JsonValue, right),
            "source_record_pk": source_pk,
        }
    )


def _group_parts(group: str) -> tuple[str, dict[str, str], dict[str, str], JsonValue | None]:
    decoded: object = json.loads(group)
    if not isinstance(decoded, dict):
        raise RepairRollbackIntegrityError("rollback restore group is malformed")
    relationship_type = decoded.get("relationship_type")
    left = _string_locator(decoded.get("left"))
    right = _string_locator(decoded.get("right"))
    if not isinstance(relationship_type, str):
        raise RepairRollbackIntegrityError("rollback restore group is malformed")
    source_pk = decoded.get("source_record_pk")
    if source_pk is not None and not isinstance(source_pk, str):
        raise RepairRollbackIntegrityError("rollback restore group source is malformed")
    return relationship_type, left, right, cast(JsonValue | None, source_pk)


def _string_locator(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RepairRollbackIntegrityError("rollback restore locator is malformed")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not item:
            raise RepairRollbackIntegrityError("rollback restore locator is malformed")
        result[key] = item
    if "mode" not in result:
        raise RepairRollbackIntegrityError("rollback restore locator is malformed")
    return result


def _canonical_json(value: dict[str, JsonValue]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _required_string(values: dict[str, JsonValue], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise RepairRollbackIntegrityError("rollback relationship property is malformed: " + key)
    return value
