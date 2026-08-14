"""Strict typed boundary for read-only Bitrix stage-history evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from src.models import JsonValue

TraversalOutcome = Literal["verified_keyset", "bounded_spool_reconcile", "unsupported"]
StageHistoryRowErrorCode = Literal[
    "invalid_row_shape",
    "missing_history_id",
    "blank_history_id",
    "invalid_history_id",
    "missing_owner_id",
    "blank_owner_id",
    "invalid_owner_id",
    "missing_created_time",
    "blank_created_time",
    "invalid_created_time",
    "created_time_without_timezone",
    "invalid_type_id",
    "invalid_category_id",
    "invalid_stage_semantic_id",
    "invalid_stage_id",
]


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
class StageHistoryRawPage:
    """Validated response envelope retaining every source row as raw JSON."""

    items: tuple[JsonValue, ...]
    next_start: int | None
    total: int | None
    operating: float | None
    operating_reset_at: float | None


@dataclass(frozen=True)
class DecodedStageHistoryRow:
    """A valid typed row alongside the exact JSON value supplied to the decoder."""

    raw: JsonValue
    item: StageHistoryItem


@dataclass(frozen=True)
class MalformedStageHistoryRow:
    """A malformed row retained without inventing a canonical stage identity."""

    raw: JsonValue
    error_code: StageHistoryRowErrorCode


StageHistoryRowDecodeResult = DecodedStageHistoryRow | MalformedStageHistoryRow


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
    raw_page = parse_stage_history_raw_page(payload, current_start=current_start)
    items = tuple(_parse_stage_history_item(item, entity_type_id) for item in raw_page.items)
    return StageHistoryPage(
        items=items,
        next_start=raw_page.next_start,
        total=raw_page.total,
        operating=raw_page.operating,
        operating_reset_at=raw_page.operating_reset_at,
    )


def parse_stage_history_raw_page(
    payload: dict[str, JsonValue],
    *,
    current_start: int,
) -> StageHistoryRawPage:
    """Validate page-level metadata without interpreting individual source rows."""
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Bitrix stage history returned an invalid result")
    raw_items = result.get("items")
    if not isinstance(raw_items, list):
        raise RuntimeError("Bitrix stage history omitted result.items")
    next_start = _optional_non_negative_int(payload, "next")
    if next_start is not None and next_start <= current_start:
        raise RuntimeError("Bitrix stage-history pagination did not advance")
    total = _optional_non_negative_int(payload, "total")
    timing_value = payload.get("time")
    if "time" in payload and timing_value is not None and not isinstance(timing_value, dict):
        raise RuntimeError("Bitrix stage history returned an invalid time")
    timing = timing_value if isinstance(timing_value, dict) else {}
    return StageHistoryRawPage(
        items=tuple(raw_items),
        next_start=next_start,
        total=total,
        operating=_optional_finite_number(timing, "operating"),
        operating_reset_at=_optional_finite_number(timing, "operating_reset_at"),
    )


def decode_stage_history_item(
    raw: JsonValue,
    *,
    entity_type_id: str,
) -> StageHistoryRowDecodeResult:
    """Decode one row without allowing malformed source data to abort its page."""
    parse_positive_numeric_id(entity_type_id, field_name="entity_type_id")
    try:
        return DecodedStageHistoryRow(
            raw=raw,
            item=_parse_stage_history_item(raw, entity_type_id),
        )
    except _StageHistoryRowError as exc:
        return MalformedStageHistoryRow(raw=raw, error_code=exc.error_code)


def parse_positive_history_id(value: str) -> int:
    """Parse the canonical ASCII decimal representation used by keyset traversal."""
    return parse_positive_numeric_id(value, field_name="stage history ID")


def parse_positive_numeric_id(value: str, *, field_name: str) -> int:
    """Parse one canonical positive ASCII-decimal source identifier."""
    if not value or not value.isascii() or not value.isdecimal() or value.startswith("0"):
        raise ValueError(f"{field_name} must be a canonical positive ASCII decimal")
    return int(value)


def _parse_stage_history_item(raw: JsonValue, entity_type_id: str) -> StageHistoryItem:
    if not isinstance(raw, dict):
        raise _StageHistoryRowError(
            "invalid_row_shape", "Bitrix stage history contained an invalid item"
        )
    history_id = _required_source_text(
        raw,
        "ID",
        missing_code="missing_history_id",
        blank_code="blank_history_id",
        invalid_code="invalid_history_id",
    )
    try:
        parse_positive_history_id(history_id)
    except ValueError as exc:
        raise _StageHistoryRowError(
            "invalid_history_id",
            "Bitrix stage history contained a noncanonical ID",
        ) from exc
    owner_id = _required_source_text(
        raw,
        "OWNER_ID",
        missing_code="missing_owner_id",
        blank_code="blank_owner_id",
        invalid_code="invalid_owner_id",
    )
    try:
        parse_positive_numeric_id(owner_id, field_name="owner ID")
    except ValueError as exc:
        raise _StageHistoryRowError(
            "invalid_owner_id",
            "Bitrix stage history contained a noncanonical OWNER_ID",
        ) from exc
    created_source = _required_source_text(
        raw,
        "CREATED_TIME",
        missing_code="missing_created_time",
        blank_code="blank_created_time",
        invalid_code="invalid_created_time",
    )
    try:
        created_time = datetime.fromisoformat(created_source.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _StageHistoryRowError(
            "invalid_created_time",
            "Bitrix stage history contained an invalid CREATED_TIME",
        ) from exc
    if created_time.tzinfo is None or created_time.utcoffset() is None:
        raise _StageHistoryRowError(
            "created_time_without_timezone",
            "Bitrix stage history CREATED_TIME must include a timezone",
        )
    return StageHistoryItem(
        history_id=history_id,
        entity_type_id=entity_type_id,
        owner_id=owner_id,
        type_id=_optional_source_text(raw, "TYPE_ID", "invalid_type_id"),
        created_time=created_time,
        created_time_source=created_source,
        category_id=_optional_source_text(raw, "CATEGORY_ID", "invalid_category_id"),
        stage_semantic_id=_optional_source_text(
            raw,
            "STAGE_SEMANTIC_ID",
            "invalid_stage_semantic_id",
        ),
        stage_id=_optional_source_text(raw, "STAGE_ID", "invalid_stage_id"),
        raw_payload=dict(raw),
    )


def _required_source_text(
    payload: dict[str, JsonValue],
    field_name: str,
    *,
    missing_code: StageHistoryRowErrorCode,
    blank_code: StageHistoryRowErrorCode,
    invalid_code: StageHistoryRowErrorCode,
) -> str:
    if field_name not in payload or payload[field_name] is None:
        raise _StageHistoryRowError(missing_code, f"Bitrix stage history omitted {field_name}")
    parsed = _optional_source_text(payload, field_name, invalid_code)
    if parsed is None:
        raise _StageHistoryRowError(missing_code, f"Bitrix stage history omitted {field_name}")
    if not parsed.strip():
        raise _StageHistoryRowError(
            blank_code, f"Bitrix stage history contained a blank {field_name}"
        )
    return parsed


def _optional_source_text(
    payload: dict[str, JsonValue],
    field_name: str,
    error_code: StageHistoryRowErrorCode,
) -> str | None:
    if field_name not in payload or payload[field_name] is None:
        return None
    value = payload[field_name]
    if isinstance(value, bool):
        raise _StageHistoryRowError(
            error_code, f"Bitrix stage history contained an invalid {field_name}"
        )
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    raise _StageHistoryRowError(
        error_code, f"Bitrix stage history contained an invalid {field_name}"
    )


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
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        if value != "0" and value.startswith("0"):
            raise RuntimeError(f"Bitrix stage history returned an invalid {field_name}")
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


class _StageHistoryRowError(RuntimeError):
    def __init__(self, error_code: StageHistoryRowErrorCode, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
