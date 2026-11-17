"""Parse Person.golden_top from stored golden_json / golden_conflicts."""

from __future__ import annotations

import json

from src.types import GoldenTopBlock, GoldenTopConflict, GoldenTopValue

_DISPLAY_TYPES: tuple[str, str, str] = ("phone", "email", "nric")

_MAX_VALUES_PER_TYPE = 3


def parse_golden_top(
    golden_json: str | None, golden_conflicts: str | None
) -> GoldenTopBlock | None:
    """Parse stored golden state into a GoldenTopBlock; None when absent/malformed.

    NRIC values arrive masked from the ingestion writer and are preserved
    verbatim. Never raises: unparseable documents map to None, while
    individual malformed entries are dropped.
    """
    if golden_json is None:
        return None
    raw: object = _safe_loads(golden_json)
    if not isinstance(raw, dict):
        return None
    values: dict[str, list[GoldenTopValue]] = {"phone": [], "email": [], "nric": []}
    for identifier_type in _DISPLAY_TYPES:
        entry = raw.get(identifier_type)
        if not isinstance(entry, dict):
            continue
        value = _parse_value(entry)
        if value is not None:
            values[identifier_type].append(value)
    conflicts = _parse_conflicts(golden_conflicts)
    if conflicts is None:
        return None
    if not any(values.values()) and not conflicts:
        return None
    return GoldenTopBlock(
        phone=values["phone"][:_MAX_VALUES_PER_TYPE],
        email=values["email"][:_MAX_VALUES_PER_TYPE],
        nric=values["nric"][:_MAX_VALUES_PER_TYPE],
        conflicts=conflicts,
    )


def _safe_loads(raw: str) -> object:
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        return None
    return parsed


def _parse_value(entry: dict[object, object]) -> GoldenTopValue | None:
    value = entry.get("value")
    if not isinstance(value, str) or not value:
        return None
    rank = entry.get("rank")
    score = entry.get("score")
    verified = entry.get("verified")
    last_confirmed_at = entry.get("last_confirmed_at")
    source_count = entry.get("source_count")
    return GoldenTopValue(
        value=value,
        rank=rank if isinstance(rank, int) and not isinstance(rank, bool) else 1,
        score=float(score)
        if isinstance(score, (int, float)) and not isinstance(score, bool)
        else 0.0,
        verified=verified is True,
        last_confirmed_at=last_confirmed_at if isinstance(last_confirmed_at, str) else None,
        source_count=source_count
        if isinstance(source_count, int) and not isinstance(source_count, bool)
        else 0,
    )


def _parse_conflicts(raw: str | None) -> list[GoldenTopConflict | str] | None:
    if raw is None:
        return []
    parsed: object = _safe_loads(raw)
    if not isinstance(parsed, list):
        return None
    conflicts: list[GoldenTopConflict | str] = []
    for item in parsed:
        if isinstance(item, str):
            conflicts.append(item)
        elif isinstance(item, dict):
            conflict = _parse_conflict(item)
            if conflict is not None:
                conflicts.append(conflict)
    return conflicts


def _parse_conflict(item: dict[object, object]) -> GoldenTopConflict | None:
    type_value = item.get("type")
    reason = item.get("reason")
    values = item.get("values")
    if not isinstance(type_value, str) or not isinstance(reason, str):
        return None
    if not isinstance(values, list):
        return None
    clean: list[str | None] = []
    for value in values:
        if value is None or isinstance(value, str):
            clean.append(value)
        else:
            return None
    return GoldenTopConflict(type=type_value, reason=reason, values=clean)
