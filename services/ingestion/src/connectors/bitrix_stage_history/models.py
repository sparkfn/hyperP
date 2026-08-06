"""Strict typed boundary for read-only Bitrix stage-history evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from src.models import JsonValue

TraversalOutcome = Literal["verified_keyset", "bounded_spool_reconcile", "unsupported"]


@dataclass(frozen=True)
class StageHistoryItem:
    """One unmodified source observation returned by ``crm.stagehistory.list``."""

    history_id: str
    entity_type_id: str
    owner_id: str
    type_id: str | None
    created_time: datetime
    created_time_source: str
    category_id: str | None
    stage_semantic_id: str | None
    stage_id: str | None
    raw_payload: dict[str, JsonValue]


@dataclass(frozen=True)
class StageHistoryPage:
    """One validated stage-history response page with safe pagination metadata."""

    items: tuple[StageHistoryItem, ...]
    next_start: int | None
    total: int | None
    operating: float | None
    operating_reset_at: float | None


@dataclass(frozen=True)
class ProbeLimits:
    """Mandatory bounded-resource limits for a capability run."""

    max_calls: int
    max_rows: int
    max_spool_bytes: int
    max_runtime_seconds: float
    max_passes: int
    required_identical_passes: int

    def __post_init__(self) -> None:
        if not _is_int(self.max_calls) or self.max_calls < 1:
            raise ValueError("max_calls must be positive")
        if not _is_int(self.max_rows) or self.max_rows < 1:
            raise ValueError("max_rows must be positive")
        if not _is_int(self.max_spool_bytes) or self.max_spool_bytes < 1:
            raise ValueError("max_spool_bytes must be positive")
        if (
            isinstance(self.max_runtime_seconds, bool)
            or not math.isfinite(self.max_runtime_seconds)
            or self.max_runtime_seconds <= 0
        ):
            raise ValueError("max_runtime_seconds must be positive and finite")
        if not _is_int(self.max_passes) or self.max_passes < 1:
            raise ValueError("max_passes must be positive")
        if not _is_int(self.required_identical_passes) or self.required_identical_passes < 2:
            raise ValueError("required_identical_passes must be at least two")
        if self.required_identical_passes > self.max_passes:
            raise ValueError("required_identical_passes cannot exceed max_passes")


def parse_stage_history_page(
    payload: dict[str, JsonValue],
    *,
    entity_type_id: str,
    current_start: int,
) -> StageHistoryPage:
    """Parse Bitrix's nested ``result.items`` stage-history envelope."""
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Bitrix stage history returned an invalid result")
    raw_items = result.get("items")
    if not isinstance(raw_items, list):
        raise RuntimeError("Bitrix stage history omitted result.items")
    items = tuple(_parse_stage_history_item(item, entity_type_id) for item in raw_items)
    next_start = _optional_non_negative_int(payload, "next")
    if next_start is not None and next_start <= current_start:
        raise RuntimeError("Bitrix stage-history pagination did not advance")
    total = _optional_non_negative_int(payload, "total")
    timing_value = payload.get("time")
    if "time" in payload and timing_value is not None and not isinstance(timing_value, dict):
        raise RuntimeError("Bitrix stage history returned an invalid time")
    timing = timing_value if isinstance(timing_value, dict) else {}
    return StageHistoryPage(
        items=items,
        next_start=next_start,
        total=total,
        operating=_optional_finite_number(timing, "operating"),
        operating_reset_at=_optional_finite_number(timing, "operating_reset_at"),
    )


def _parse_stage_history_item(raw: JsonValue, entity_type_id: str) -> StageHistoryItem:
    if not isinstance(raw, dict):
        raise RuntimeError("Bitrix stage history contained an invalid item")
    history_id = _required_source_text(raw, "ID")
    owner_id = _required_source_text(raw, "OWNER_ID")
    created_source = _required_source_text(raw, "CREATED_TIME")
    try:
        created_time = datetime.fromisoformat(created_source.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("Bitrix stage history contained an invalid CREATED_TIME") from exc
    if created_time.tzinfo is None:
        raise RuntimeError("Bitrix stage history CREATED_TIME must include a timezone")
    return StageHistoryItem(
        history_id=history_id,
        entity_type_id=entity_type_id,
        owner_id=owner_id,
        type_id=_optional_source_text(raw, "TYPE_ID"),
        created_time=created_time,
        created_time_source=created_source,
        category_id=_optional_source_text(raw, "CATEGORY_ID"),
        stage_semantic_id=_optional_source_text(raw, "STAGE_SEMANTIC_ID"),
        stage_id=_optional_source_text(raw, "STAGE_ID"),
        raw_payload=dict(raw),
    )


def _required_source_text(payload: dict[str, JsonValue], field_name: str) -> str:
    parsed = _optional_source_text(payload, field_name)
    if parsed is None:
        raise RuntimeError(f"Bitrix stage history omitted {field_name}")
    if not parsed.strip():
        raise RuntimeError(f"Bitrix stage history contained a blank {field_name}")
    return parsed


def _optional_source_text(payload: dict[str, JsonValue], field_name: str) -> str | None:
    if field_name not in payload or payload[field_name] is None:
        return None
    value = payload[field_name]
    if isinstance(value, bool):
        raise RuntimeError(f"Bitrix stage history contained an invalid {field_name}")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    raise RuntimeError(f"Bitrix stage history contained an invalid {field_name}")


def _optional_non_negative_int(payload: dict[str, JsonValue], field_name: str) -> int | None:
    if field_name not in payload or payload[field_name] is None:
        return None
    value = payload[field_name]
    if isinstance(value, bool):
        raise RuntimeError(f"Bitrix stage history returned an invalid {field_name}")
    if isinstance(value, int):
        if value >= 0:
            return value
        raise RuntimeError(f"Bitrix stage history returned an invalid {field_name}")
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise RuntimeError(f"Bitrix stage history returned an invalid {field_name}")


def _optional_finite_number(payload: dict[str, JsonValue], field_name: str) -> float | None:
    if field_name not in payload or payload[field_name] is None:
        return None
    value = payload[field_name]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuntimeError(f"Bitrix stage history returned an invalid time.{field_name}")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise RuntimeError(f"Bitrix stage history returned an invalid time.{field_name}")
    return parsed


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
