"""Bounded canonical JSON readers for immutable model workflow evidence."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import cast

from intelligence.artifacts import canonical_json
from intelligence.datasets.bounds import ReadBudget, read_bytes

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

MAX_JSON_DEPTH = 32
MAX_JSON_VALUES = 100_000
MAX_JSON_OBJECT_ENTRIES = 4_096


def canonical_object(
    path: Path,
    budget: ReadBudget,
    *,
    maximum_file_bytes: int,
    maximum_depth: int = MAX_JSON_DEPTH,
    maximum_values: int = MAX_JSON_VALUES,
    maximum_object_entries: int = MAX_JSON_OBJECT_ENTRIES,
) -> dict[str, JsonValue]:
    """Read one bounded, finite, duplicate-free canonical JSON object."""
    raw = read_bytes(path, budget, maximum_file_bytes=maximum_file_bytes)
    value = _decode(raw, maximum_depth, maximum_values, maximum_object_entries)
    if not isinstance(value, dict) or raw != canonical_json(value).encode("utf-8"):
        raise ValueError("model JSON evidence is noncanonical")
    return value


def canonical_ndjson(
    path: Path,
    budget: ReadBudget,
    *,
    maximum_file_bytes: int,
    maximum_rows: int,
    maximum_depth: int = MAX_JSON_DEPTH,
    maximum_values: int = MAX_JSON_VALUES,
    maximum_object_entries: int = MAX_JSON_OBJECT_ENTRIES,
) -> tuple[dict[str, JsonValue], ...]:
    """Read LF-only canonical object rows while charging a fixed row ceiling."""
    raw = read_bytes(path, budget, maximum_file_bytes=maximum_file_bytes)
    if raw and not raw.endswith(b"\n"):
        raise ValueError("model NDJSON evidence must end with LF")
    lines = () if not raw else tuple(raw[:-1].split(b"\n"))
    if len(lines) > maximum_rows:
        raise RuntimeError("model NDJSON evidence exceeds row ceiling")
    budget.rows(len(lines))
    values: list[dict[str, JsonValue]] = []
    for line in lines:
        if not line:
            raise ValueError("model NDJSON evidence contains an empty row")
        value = _decode(line, maximum_depth, maximum_values, maximum_object_entries)
        if not isinstance(value, dict) or line != canonical_json(value).encode("utf-8"):
            raise ValueError("model NDJSON evidence is noncanonical")
        values.append(value)
    return tuple(values)


def _decode(
    raw: bytes,
    maximum_depth: int,
    maximum_values: int,
    maximum_object_entries: int,
) -> JsonValue:
    _encoded_depth(raw, maximum_depth)
    pair_count = 0

    def object_pairs(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        nonlocal pair_count
        pair_count += len(pairs)
        if pair_count > maximum_object_entries or len(pairs) > maximum_object_entries:
            raise ValueError("model JSON evidence exceeds object entry ceiling")
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("model JSON evidence has duplicate object keys")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            object_pairs_hook=object_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ValueError("model JSON evidence is invalid") from error
    _value_count(value, maximum_values)
    return cast(JsonValue, value)


def _reject_constant(_: str) -> JsonValue:
    raise ValueError("non-finite JSON value")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON value")
    return parsed


def _encoded_depth(raw: bytes, maximum_depth: int) -> None:
    depth = 0
    quoted = False
    escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
            continue
        if byte == 34:
            quoted = True
        elif byte in {91, 123}:
            depth += 1
            if depth > maximum_depth:
                raise ValueError("model JSON evidence exceeds nesting ceiling")
        elif byte in {93, 125}:
            depth -= 1
            if depth < 0:
                raise ValueError("model JSON evidence is invalid")
    if quoted or escaped or depth != 0:
        raise ValueError("model JSON evidence is invalid")


def _value_count(value: JsonValue, maximum: int) -> None:
    count = 0
    pending: list[JsonValue] = [value]
    while pending:
        current = pending.pop()
        count += 1
        if count > maximum:
            raise ValueError("model JSON evidence exceeds value ceiling")
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
