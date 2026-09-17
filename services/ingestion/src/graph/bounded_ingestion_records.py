"""Typed record mapping and parameter helpers for bounded graph control."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import cast

from neo4j import Record
from pydantic import TypeAdapter

from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedMode,
    BoundedRecoveryState,
    BoundedStatus,
    BoundedUnit,
    FailureCategory,
    OccurrenceContext,
    PauseReason,
    RecordDisposition,
    RunScope,
    RunStatus,
    UnitApplyResult,
    Usage,
)
from src.graph.ingestion_control_models import decode_json_object, encode_json
from src.resumable import CheckpointDescriptor

_DISPOSITIONS = TypeAdapter(list[RecordDisposition])


def attempt_from_record(
    record: Record,
    scope: RunScope,
    occurrence: OccurrenceContext,
    worker_task_id: str,
) -> AttemptContext:
    checkpoint = CheckpointDescriptor(
        phase=required_text(record, "phase"),
        cursor=decode_json_object(required_text(record, "cursor_json")),
        source_window=decode_json_object(required_text(record, "source_window_json")),
        last_committed_record_id=optional_text(record["last_committed_record_id"]),
        connector_version=required_text(record, "connector_version"),
        schema_version=required_positive_int(record, "checkpoint_schema_version"),
        replay_boundary=required_text(record, "replay_boundary"),
    )
    return AttemptContext(
        logical_run_id=required_text(record, "logical_run_id"),
        ingest_run_id=required_text(record, "ingest_run_id"),
        worker_task_id=worker_task_id,
        attempt_generation=required_positive_int(record, "attempt_generation"),
        fencing_token=required_positive_int(record, "fencing_token"),
        lease_token=required_text(record, "lease_token"),
        global_slot_index=non_negative_int(record, "global_slot_index"),
        global_slot_fencing_token=required_positive_int(
            record,
            "global_slot_fencing_token",
        ),
        scope=scope,
        occurrence=occurrence,
        checkpoint=checkpoint,
        usage=usage_from_record(record, "usage"),
        reserved_usage=usage_from_record(record, "reserved"),
        retry_backlog=non_negative_int(record, "retry_backlog"),
        terminal_observed=required_bool(record, "terminal_observed"),
        terminal_checkpoint_committed=required_bool(
            record,
            "terminal_checkpoint_committed",
        ),
    )


def status_from_record(record: Record) -> BoundedStatus:
    return BoundedStatus(
        logical_run_id=required_text(record, "logical_run_id"),
        source_key=required_text(record, "source_key"),
        control_instance_id=required_text(record, "control_instance_id"),
        entity_key=optional_text(record["entity_key"]),
        status=cast(RunStatus, required_text(record, "status")),
        pause_reason=cast(PauseReason | None, optional_text(record["pause_reason"])),
        occurrence_id=optional_text(record["occurrence_id"]),
        timezone=optional_text(record["timezone"]),
        starts_at=optional_text(record["starts_at"]),
        drain_starts_at=optional_text(record["drain_starts_at"]),
        cutoff_at=optional_text(record["cutoff_at"]),
        next_eligible_at=optional_text(record["next_eligible_at"]),
        usage=usage_from_record(record, ""),
        reserved_usage=usage_from_record(record, "reserved"),
        attempt_generation=non_negative_int(record, "attempt_generation"),
        source_window_fingerprint=required_text(
            record,
            "source_window_fingerprint",
        ),
        checkpoint_cursor_present=required_bool(
            record,
            "checkpoint_cursor_present",
        ),
        phase=optional_text(record["phase"]),
        checkpointed_at=optional_text(record["checkpointed_at"]),
        retry_backlog=non_negative_int(record, "retry_backlog"),
        retry_oldest_at=optional_text(record["retry_oldest_at"]),
        failure_category=cast(
            FailureCategory | None,
            optional_text(record["failure_category"]),
        ),
    )


def recovery_from_record(
    record: Record,
    reset_generation: int,
) -> BoundedRecoveryState:
    source_window = decode_json_object(required_text(record, "source_window_json"))
    scope = RunScope(
        environment=required_text(record, "environment"),
        reset_generation=reset_generation,
        source_key=required_text(record, "source_key"),
        control_instance_id=required_text(record, "control_instance_id"),
        entity_key=optional_text(record["entity_key"]),
        stream_key=optional_text(record["stream_key"]),
        mode=cast(BoundedMode, required_text(record, "mode")),
        configuration_fingerprint=required_text(record, "configuration_fingerprint"),
        connector_version=required_text(record, "connector_version"),
        checkpoint_schema_version=required_positive_int(
            record,
            "checkpoint_schema_version",
        ),
        source_window=source_window,
    )
    occurrence = OccurrenceContext(
        occurrence_id=required_text(record, "occurrence_id"),
        timezone=required_text(record, "timezone"),
        starts_at=required_datetime(record, "starts_at"),
        drain_starts_at=required_datetime(record, "drain_starts_at"),
        cutoff_at=required_datetime(record, "cutoff_at"),
        next_eligible_at=required_datetime(record, "next_eligible_at"),
        scheduled=required_bool(record, "scheduled"),
    )
    return BoundedRecoveryState(scope=scope, occurrence=occurrence)


def committed_receipt_result(record: Record) -> UnitApplyResult:
    if record["status"] not in {"committed", "retry_pending"}:
        raise RuntimeError("bounded receipt is not committed")
    raw = record["dispositions_json"]
    if not isinstance(raw, str):
        raise RuntimeError("bounded receipt lacks dispositions")
    return UnitApplyResult(dispositions=tuple(_DISPOSITIONS.validate_json(raw)))


def validate_apply_result(unit: BoundedUnit, result: UnitApplyResult) -> None:
    if len(result.dispositions) != len(unit.unit.records):
        raise ValueError("writer must return one disposition per source record")
    if result.dispositions.count("durable_retry") != len(result.retry_obligations):
        raise ValueError("each durable-retry disposition requires one obligation")
    if any(item.replay_id != unit.replay_id for item in result.retry_obligations):
        raise ValueError("retry obligation replay ID does not match its unit")


def logical_key(scope: RunScope, checkpoint: CheckpointDescriptor) -> str:
    digest = hashlib.sha256(encode_json(checkpoint.source_window).encode("utf-8"))
    return f"{scope.scope_key}|{digest.hexdigest()}"


def scope_parameters(scope: RunScope) -> dict[str, object]:
    return {
        "environment": scope.environment,
        "reset_generation": scope.reset_generation,
        "source_key": scope.source_key,
        "control_instance_id": scope.control_instance_id,
        "entity_key": scope.entity_key,
        "stream_key": scope.stream_key,
        "mode": scope.mode,
        "scope_key": scope.scope_key,
        "configuration_fingerprint": scope.configuration_fingerprint,
        "connector_version": scope.connector_version,
        "checkpoint_schema_version": scope.checkpoint_schema_version,
        "source_window_fingerprint": scope.source_window_fingerprint,
    }


def occurrence_parameters(occurrence: OccurrenceContext) -> dict[str, object]:
    return {
        "occurrence_id": occurrence.occurrence_id,
        "timezone": occurrence.timezone,
        "starts_at": occurrence.starts_at.isoformat(),
        "drain_starts_at": occurrence.drain_starts_at.isoformat(),
        "cutoff_at": occurrence.cutoff_at.isoformat(),
        "next_eligible_at": occurrence.next_eligible_at.isoformat(),
        "scheduled": occurrence.scheduled,
    }


def fence_parameters(context: AttemptContext) -> dict[str, object]:
    return {
        "logical_run_id": context.logical_run_id,
        "reset_generation": context.scope.reset_generation,
        "attempt_generation": context.attempt_generation,
        "fencing_token": context.fencing_token,
        "worker_task_id": context.worker_task_id,
        "lease_token": context.lease_token,
        "global_slot_index": context.global_slot_index,
        "global_slot_fencing_token": context.global_slot_fencing_token,
    }


def reservation_key(context: AttemptContext, usage: Usage) -> str:
    payload = {
        "logical_run_id": context.logical_run_id,
        "attempt_generation": context.attempt_generation,
        "phase": context.checkpoint.phase,
        "cursor": context.checkpoint.cursor,
        "usage": usage.values(),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def usage_parameters(usage: Usage) -> dict[str, object]:
    return {
        "records": usage.records,
        "source_requests": usage.source_requests,
        "pages": usage.pages,
        "bytes_read": usage.bytes_read,
        "extraction_calls": usage.extraction_calls,
    }


def usage_from_record(record: Record, prefix: str) -> Usage:
    key_prefix = f"{prefix}_" if prefix else ""
    return Usage(
        records=non_negative_int(record, f"{key_prefix}records"),
        source_requests=non_negative_int(record, f"{key_prefix}source_requests"),
        pages=non_negative_int(record, f"{key_prefix}pages"),
        bytes_read=non_negative_int(record, f"{key_prefix}bytes_read"),
        extraction_calls=non_negative_int(record, f"{key_prefix}extraction_calls"),
    )


def required_text(record: Record, key: str) -> str:
    value = record[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"bounded control returned invalid {key}")
    return value


def optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def required_positive_int(record: Record, key: str) -> int:
    value: object = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"bounded control returned invalid {key}")
    return value


def non_negative_int(record: Record, key: str) -> int:
    value: object = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"bounded control returned invalid {key}")
    return value


def required_datetime(record: Record, key: str) -> datetime:
    parsed = datetime.fromisoformat(required_text(record, key).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"bounded control returned naive {key}")
    return parsed


def required_bool(record: Record, key: str) -> bool:
    value = record[key]
    if not isinstance(value, bool):
        raise ValueError(f"bounded control returned invalid {key}")
    return value
