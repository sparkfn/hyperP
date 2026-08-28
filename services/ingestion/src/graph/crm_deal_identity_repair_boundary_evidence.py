"""Strict canonical conversion for read-only CRM repair graph evidence."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time
from typing import Protocol, runtime_checkable

from neo4j import Record
from neo4j.spatial import CartesianPoint, Point, WGS84Point

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.models import JsonValue


@runtime_checkable
class _IsoFormatValue(Protocol):
    def iso_format(self) -> str: ...


def record_json_dict(record: Record) -> dict[str, JsonValue]:
    """Convert one Neo4j record to canonical JSON-compatible evidence."""
    return {key: neo4j_json_value(record[key]) for key in record.keys()}


def canonical_evidence_rows(records: Iterable[Record]) -> list[JsonValue]:
    """Convert, retain multiplicity, and deterministically order streamed rows."""
    rows = [record_json_dict(record) for record in records]
    normalized: list[JsonValue] = []
    for row in rows:
        evidence_row = canonical_boundary_evidence(row)
        if not isinstance(evidence_row, dict):
            raise RuntimeError("repair boundary evidence rows must be JSON objects")
        normalized.append(evidence_row)
    return sorted(normalized, key=_json_sort_key)


def canonical_boundary_evidence(
    value: JsonValue,
    *,
    field_name: str | None = None,
    preserve_property_order: bool = False,
) -> JsonValue:
    """Normalize unordered graph evidence without discarding duplicate rows."""
    if isinstance(value, dict):
        property_map = preserve_property_order or field_name in _PROPERTY_MAP_FIELDS
        return {
            key: canonical_boundary_evidence(
                value[key],
                field_name=key,
                preserve_property_order=property_map,
            )
            for key in sorted(value)
        }
    if isinstance(value, list):
        normalized = [
            canonical_boundary_evidence(item, preserve_property_order=preserve_property_order)
            for item in value
        ]
        return normalized if preserve_property_order else sorted(normalized, key=_json_sort_key)
    return value


def neo4j_json_value(value: object) -> JsonValue:
    """Fail closed unless a Neo4j value has an exact canonical JSON image."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeError("repair boundary evidence contains a non-finite float")
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, _IsoFormatValue):
        return value.iso_format()
    if isinstance(value, (CartesianPoint, WGS84Point)):
        return {
            "coordinates": [neo4j_json_value(coordinate) for coordinate in value],
            "srid": value.srid,
        }
    if isinstance(value, Point):
        raise RuntimeError("repair boundary evidence Point is missing an SRID")
    if isinstance(value, Mapping):
        converted: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeError("repair boundary evidence object keys must be strings")
            converted[key] = neo4j_json_value(item)
        return converted
    if isinstance(value, (list, tuple)):
        return [neo4j_json_value(item) for item in value]
    raise RuntimeError(f"unsupported repair boundary Neo4j value: {type(value).__name__}")


def _json_sort_key(value: JsonValue) -> bytes:
    return canonical_json_bytes({"value": value})


_PROPERTY_MAP_FIELDS = frozenset(
    {"properties", "left_properties", "right_properties", "relationship_properties"}
)
