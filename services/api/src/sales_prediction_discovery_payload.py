"""Defensive private JSON parsing for CRM-WON discovery."""

from __future__ import annotations

import json

type DiscoveryScalar = str | int | float | bool | None
type DiscoveryRow = dict[str, DiscoveryScalar]


def parse_payload(row: DiscoveryRow) -> dict[str, object] | None:
    """Parse a source payload without allowing malformed depth to abort a run."""
    raw = row.get("raw_payload")
    if not isinstance(raw, str):
        return None
    try:
        parsed: object = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def nested_object(value: dict[str, object] | None, key: str) -> dict[str, object] | None:
    nested = value.get(key) if value is not None else None
    return nested if isinstance(nested, dict) else None


def first_value(
    payload: dict[str, object] | None, nested: dict[str, object] | None, *keys: str
) -> object:
    for container in (payload, nested):
        if container is not None:
            for key in keys:
                if key in container:
                    return container[key]
    return None


def payload_status(row: DiscoveryRow) -> str:
    raw = row.get("raw_payload")
    if raw is None or raw == "":
        return "missing"
    return "valid_object" if parse_payload(row) is not None else "invalid"
