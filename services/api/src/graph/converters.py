"""Helpers to convert Neo4j driver values to Python primitives.

Centralised so route handlers stay free of `Any`-typed conversion logic.
"""

from __future__ import annotations

import base64
from datetime import datetime

from neo4j.time import DateTime as Neo4jDateTime

# Neo4j returns a heterogeneous mix of primitives, so we constrain to a tagged
# union of the value shapes we accept and emit. We deliberately exclude `Any`.
type GraphScalar = str | int | float | bool | None
type GraphValue = (
    GraphScalar
    | Neo4jDateTime
    | datetime
    | list[GraphValue]
    | dict[str, GraphValue]
)
type GraphRecord = dict[str, GraphValue]


def to_iso_or_none(value: GraphValue) -> str | None:
    """Convert a Neo4j temporal value to an ISO 8601 string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Neo4jDateTime):
        return value.to_native().isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def to_iso_or_empty(value: GraphValue) -> str:
    """Convert a Neo4j temporal value to ISO 8601, defaulting to empty string."""
    return to_iso_or_none(value) or ""


def to_int(value: GraphValue, default: int = 0) -> int:
    """Convert a graph value to int with a safe default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    return default


def to_float(value: GraphValue, default: float = 0.0) -> float:
    """Convert a graph value to float with a safe default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return float(value)
    return default


def to_str(value: GraphValue, default: str = "") -> str:
    """Convert a graph value to str with a safe default."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def to_optional_str(value: GraphValue) -> str | None:
    """Convert to str or None when input is None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def to_str_list(value: GraphValue) -> list[str]:
    """Convert a graph value to a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [to_str(v) for v in value]
    return []


def encode_cursor(offset: int) -> str:
    """Encode an integer offset to an opaque base64 cursor."""
    return base64.b64encode(str(offset).encode("ascii")).decode("ascii")


def decode_cursor(cursor: str | None) -> int:
    """Decode an opaque base64 cursor to an integer offset."""
    if not cursor:
        return 0
    try:
        return int(base64.b64decode(cursor.encode("ascii")).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return 0
