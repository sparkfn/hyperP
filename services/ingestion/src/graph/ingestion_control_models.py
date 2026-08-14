"""Typed records and serialization helpers for ingestion control state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, NoReturn, cast

from neo4j import Record

from src.bitrix_ingestion_models import BitrixStreamKey, FenceContext
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
    committed_count: int
    duplicate_count: int
    excluded_count: int
    retry_count: int
    checkpointed_at: str | None


BitrixStreamAdmissionOutcome = Literal["admitted", "coalesced", "replaced"]


@dataclass(frozen=True)
class BitrixStreamAdmission:
    """Durable ownership result consumed by every fenced stream mutation."""

    outcome: BitrixStreamAdmissionOutcome
    fence_context: FenceContext
    worker_task_id: str


def bitrix_stream_admission(record: Record | None) -> BitrixStreamAdmission:
    """Map the active Bitrix stream control row returned by admission."""
    if record is None:
        raise ValueError("Bitrix stream admission did not return a record")
    outcome = _bitrix_stream_admission_outcome(record)
    fence_context = FenceContext(
        logical_run_id=_required_str(record, "logical_run_id"),
        ingest_run_id=_required_str(record, "ingest_run_id"),
        source_key=_required_str(record, "source_key"),
        stream_key=_bitrix_stream_key(record),
        stream_generation=_required_positive_int(record, "stream_generation"),
        fencing_token=_required_positive_int(record, "fencing_token"),
        attempt_generation=_required_positive_int(record, "attempt_generation"),
    )
    return BitrixStreamAdmission(
        outcome=outcome,
        fence_context=fence_context,
        worker_task_id=_required_str(record, "worker_task_id"),
    )


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
        committed_count=_required_non_negative_int(record, "committed_count"),
        duplicate_count=_required_non_negative_int(record, "duplicate_count"),
        excluded_count=_required_non_negative_int(record, "excluded_count"),
        retry_count=_required_non_negative_int(record, "retry_count"),
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


def _required_non_negative_int(record: Record, key: str) -> int:
    value = _required_int(record, key)
    if value < 0:
        raise ValueError(f"Expected a non-negative integer for {key}")
    return value


def _required_positive_int(record: Record, key: str) -> int:
    value = _required_int(record, key)
    if value < 1:
        raise ValueError(f"Expected a positive integer for {key}")
    return value


def _required_bool(record: Record, key: str) -> bool:
    value: object = record[key]
    if not isinstance(value, bool):
        raise ValueError(f"Expected a boolean for {key}")
    return value


def _bitrix_stream_key(record: Record) -> BitrixStreamKey:
    value = _required_str(record, "stream_key")
    if value == "crm_deals":
        return "crm_deals"
    if value == "crm_activities":
        return "crm_activities"
    if value == "openlines_conversations":
        return "openlines_conversations"
    if value == "crm_stage_history":
        return "crm_stage_history"
    raise ValueError(f"Unexpected Bitrix stream key: {value}")


def _bitrix_stream_admission_outcome(record: Record) -> BitrixStreamAdmissionOutcome:
    value = _required_str(record, "admission_outcome")
    if value not in {"admitted", "coalesced", "replaced"}:
        raise ValueError(f"Unexpected Bitrix stream admission outcome: {value}")
    return cast(BitrixStreamAdmissionOutcome, value)
