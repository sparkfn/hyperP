"""Typed records and serialization helpers for ingestion control state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import NoReturn, cast

from neo4j import Record

from src.models import JsonValue
from src.resumable import LogicalRunStatus


@dataclass(frozen=True)
class LogicalRunAttempt:
    """The active immutable attempt for one logical ingestion request."""

    logical_run_id: str
    ingest_run_id: str
    worker_task_id: str
    generation: int
    logical_status: LogicalRunStatus
    created: bool


@dataclass(frozen=True)
class LogicalRunState:
    """Safe, operator-facing state for the active logical run."""

    logical_run_id: str
    status: LogicalRunStatus
    generation: int
    source_key: str
    mode: str
    dump_path: str | None
    entity_key: str | None
    stop_requested: bool
    stop_reason: str | None
    ingest_run_id: str | None
    phase: str | None
    cursor: dict[str, JsonValue] | None
    checkpointed_at: str | None


def logical_attempt(record: Record | None) -> LogicalRunAttempt:
    if record is None:
        raise ValueError("Logical-run creation did not return a record")
    return LogicalRunAttempt(
        logical_run_id=_required_str(record, "logical_run_id"),
        ingest_run_id=_required_str(record, "ingest_run_id"),
        worker_task_id=_required_str(record, "worker_task_id"),
        generation=_required_int(record, "generation"),
        logical_status=_logical_status(record, "logical_status"),
        created=_required_bool(record, "created"),
    )


def resumed_attempt(record: Record) -> LogicalRunAttempt:
    """Map a newly created resume attempt returned by the generation CAS."""
    return LogicalRunAttempt(
        logical_run_id=_required_str(record, "logical_run_id"),
        ingest_run_id=_required_str(record, "ingest_run_id"),
        worker_task_id=_required_str(record, "worker_task_id"),
        generation=_required_int(record, "generation"),
        logical_status="queued",
        created=True,
    )


def logical_state(record: Record) -> LogicalRunState:
    cursor_json = _optional_str(record, "cursor_json")
    return LogicalRunState(
        logical_run_id=_required_str(record, "logical_run_id"),
        status=_logical_status(record, "status"),
        generation=_required_int(record, "generation"),
        source_key=_required_str(record, "source_key"),
        mode=_required_str(record, "mode"),
        dump_path=_optional_str(record, "dump_path"),
        entity_key=_optional_str(record, "entity_key"),
        stop_requested=record["stop_requested"] is True,
        stop_reason=_optional_str(record, "stop_reason"),
        ingest_run_id=_optional_str(record, "ingest_run_id"),
        phase=_optional_str(record, "phase"),
        cursor=decode_json_object(cursor_json) if cursor_json is not None else None,
        checkpointed_at=_optional_str(record, "checkpointed_at"),
    )


def encode_json(value: dict[str, JsonValue]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def decode_json_object(value: str) -> dict[str, JsonValue]:
    try:
        parsed = cast(object, json.loads(value, parse_constant=_reject_json_constant))
    except json.JSONDecodeError as exc:
        raise ValueError("Checkpoint JSON is corrupted") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Checkpoint JSON is corrupted")
    result: dict[str, JsonValue] = {}
    for raw_key, raw_value in cast(dict[object, object], parsed).items():
        if not isinstance(raw_key, str):
            raise ValueError("Checkpoint JSON is corrupted")
        result[raw_key] = _validate_json_value(raw_value)
    return result


def validate_counts(*counts: int) -> None:
    if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in counts):
        raise ValueError("Ingestion counters must be non-negative integers")


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"Checkpoint JSON contains invalid constant {value}")


def _validate_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_validate_json_value(item) for item in cast(list[object], value)]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for raw_key, raw_value in cast(dict[object, object], value).items():
            if not isinstance(raw_key, str):
                raise ValueError("Checkpoint JSON is corrupted")
            result[raw_key] = _validate_json_value(raw_value)
        return result
    raise ValueError("Checkpoint JSON is corrupted")


def _logical_status(record: Record, key: str) -> LogicalRunStatus:
    value = _required_str(record, key)
    allowed: frozenset[str] = frozenset(
        {
            "queued",
            "running",
            "stop_requested",
            "paused_with_checkpoint",
            "completed",
            "completed_with_errors",
            "failed",
        }
    )
    if value not in allowed:
        raise ValueError(f"Unexpected logical-run status: {value}")
    return cast(LogicalRunStatus, value)


def _required_str(record: Record, key: str) -> str:
    value: object = record[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"Expected a non-empty string for {key}")
    return value


def _optional_str(record: Record, key: str) -> str | None:
    value: object = record[key]
    return value if isinstance(value, str) and value else None


def _required_int(record: Record, key: str) -> int:
    value: object = record[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Expected an integer for {key}")
    return value


def _required_bool(record: Record, key: str) -> bool:
    value: object = record[key]
    if not isinstance(value, bool):
        raise ValueError(f"Expected a boolean for {key}")
    return value
