"""Boundary validation for immutable staged-sales promotion blueprints."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from src.graph.loyalty_points import convert_integral, warn_invalid_loyalty_once


class InvalidSalesStageError(ValueError):
    """Raised when staged scalar data does not match its canonical hashes."""


@dataclass(frozen=True)
class ValidatedSalesStage:
    source_lock_version: int
    lock_version: int
    line_count: int
    observation_count: int
    stage_hash: str
    points_used: int | None
    points_gained: int | None


def canonical_staging_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidSalesStageError(f"{name} is not a mapping")
    return {str(key): item for key, item in value.items()}


def _stored_hash(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidSalesStageError(f"{name} is invalid")
    return value


def validate_sales_stage(record: Mapping[str, object]) -> ValidatedSalesStage:
    """Recompute all canonical hashes returned by the lock-acquiring precheck."""
    order = _mapping(record.get("order"), "order")
    order_hash = _stored_hash(record.get("order_hash"), "order_hash")
    if canonical_staging_hash(order) != order_hash:
        raise InvalidSalesStageError("order hash mismatch")
    raw_lines = record.get("lines")
    raw_observations = record.get("observations")
    if not isinstance(raw_lines, list) or not isinstance(raw_observations, list):
        raise InvalidSalesStageError("stage children are not lists")
    lines: list[dict[str, object]] = []
    source_line_item_ids: set[str] = set()
    for index, raw_line in enumerate(raw_lines):
        line = _mapping(raw_line, f"line {index}")
        line_hash = _stored_hash(line.pop("line_hash", None), f"line {index} hash")
        if canonical_staging_hash(line) != line_hash:
            raise InvalidSalesStageError(f"line {index} hash mismatch")
        source_line_item_id = line.get("source_line_item_id")
        if not isinstance(source_line_item_id, str) or not source_line_item_id:
            raise InvalidSalesStageError(f"line {index} source id is invalid")
        if source_line_item_id in source_line_item_ids:
            raise InvalidSalesStageError("duplicate staged source line id")
        source_line_item_ids.add(source_line_item_id)
        line["line_hash"] = line_hash
        lines.append(line)
    observations: list[dict[str, object]] = []
    for index, raw_observation in enumerate(raw_observations):
        observation = _mapping(raw_observation, f"observation {index}")
        observation_hash = _stored_hash(
            observation.pop("observation_hash", None), f"observation {index} hash"
        )
        if canonical_staging_hash(observation) != observation_hash:
            raise InvalidSalesStageError(f"observation {index} hash mismatch")
        observation["observation_hash"] = observation_hash
        observations.append(observation)
    stage_hash = _stored_hash(record.get("stage_hash"), "stage_hash")
    if (
        canonical_staging_hash({"order": order, "lines": lines, "observations": observations})
        != stage_hash
    ):
        raise InvalidSalesStageError("aggregate stage hash mismatch")
    lock_version = record.get("lock_version")
    if not isinstance(lock_version, int) or isinstance(lock_version, bool) or lock_version < 1:
        raise InvalidSalesStageError("lock version is invalid")
    source_lock_version = record.get("source_lock_version")
    if (
        not isinstance(source_lock_version, int)
        or isinstance(source_lock_version, bool)
        or source_lock_version < 1
    ):
        raise InvalidSalesStageError("source lock version is invalid")
    expected_lines = record.get("expected_line_count")
    expected_observations = record.get("expected_observation_count")
    if expected_lines != len(lines) or expected_observations != len(observations):
        raise InvalidSalesStageError("stage count mismatch")
    points_used = convert_integral(order.get("points_used"))
    points_gained = convert_integral(order.get("points_gained"))
    source = record.get("source_system_key")
    source_order_id = order.get("source_order_id")
    normalized_source = source if isinstance(source, str) else None
    normalized_order_id = source_order_id if isinstance(source_order_id, str) else None
    warn_invalid_loyalty_once(
        source=normalized_source,
        source_order_id=normalized_order_id,
        field="points_used",
        conversion=points_used,
        raw_value=order.get("points_used"),
    )
    warn_invalid_loyalty_once(
        source=normalized_source,
        source_order_id=normalized_order_id,
        field="points_gained",
        conversion=points_gained,
        raw_value=order.get("points_gained"),
    )
    return ValidatedSalesStage(
        source_lock_version=source_lock_version,
        lock_version=lock_version,
        line_count=len(lines),
        observation_count=len(observations),
        stage_hash=stage_hash,
        points_used=points_used.value,
        points_gained=points_gained.value,
    )
