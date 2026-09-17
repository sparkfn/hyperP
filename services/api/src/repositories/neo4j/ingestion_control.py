"""Neo4j implementation of bounded logical-run operator controls."""

from __future__ import annotations

from neo4j import AsyncManagedTransaction, AsyncResult

from src.graph.client import get_session
from src.graph.converters import GraphRecord, GraphValue, to_iso_or_none, to_optional_str
from src.graph.queries.ingestion_control import (
    GET_BOUNDED_LOGICAL_RUN,
    PAUSE_BOUNDED_LOGICAL_RUN,
    RESUME_BOUNDED_LOGICAL_RUN,
)
from src.repositories.neo4j._utils import record_to_dict
from src.repositories.protocols.ingestion_control import (
    BoundedLogicalRunControlResult,
    BoundedLogicalRunStatusRecord,
    BoundedLogicalRunUsageRecord,
)


class Neo4jIngestionControlRepository:
    """Persist and read bounded operator intents through exact run identity."""

    async def get_bounded_run(
        self,
        logical_run_id: str,
    ) -> BoundedLogicalRunStatusRecord | None:
        async with get_session() as session:
            record = await session.execute_read(_get_bounded_run_tx, logical_run_id)
        return _status_from_record(record) if record is not None else None

    async def pause_bounded_run(
        self,
        logical_run_id: str,
        source_key: str,
        control_instance_id: str,
        reset_generation: int,
        reason: str,
    ) -> BoundedLogicalRunControlResult:
        async with get_session(write=True) as session:
            record = await session.execute_write(
                _pause_bounded_run_tx,
                logical_run_id,
                source_key,
                control_instance_id,
                reset_generation,
                reason,
            )
        return _control_result(record, publish_recovery=False)

    async def resume_bounded_run(
        self,
        logical_run_id: str,
        source_key: str,
        control_instance_id: str,
        reset_generation: int,
    ) -> BoundedLogicalRunControlResult:
        async with get_session(write=True) as session:
            record = await session.execute_write(
                _resume_bounded_run_tx,
                logical_run_id,
                source_key,
                control_instance_id,
                reset_generation,
            )
        return _control_result(record, publish_recovery=True)


async def _get_bounded_run_tx(
    tx: AsyncManagedTransaction,
    logical_run_id: str,
) -> GraphRecord | None:
    result = await tx.run(GET_BOUNDED_LOGICAL_RUN, logical_run_id=logical_run_id)
    return await _single_record(result)


async def _pause_bounded_run_tx(
    tx: AsyncManagedTransaction,
    logical_run_id: str,
    source_key: str,
    control_instance_id: str,
    reset_generation: int,
    reason: str,
) -> GraphRecord | None:
    result = await tx.run(
        PAUSE_BOUNDED_LOGICAL_RUN,
        logical_run_id=logical_run_id,
        source_key=source_key,
        control_instance_id=control_instance_id,
        reset_generation=reset_generation,
        reason=reason,
    )
    return await _single_record(result)


async def _resume_bounded_run_tx(
    tx: AsyncManagedTransaction,
    logical_run_id: str,
    source_key: str,
    control_instance_id: str,
    reset_generation: int,
) -> GraphRecord | None:
    result = await tx.run(
        RESUME_BOUNDED_LOGICAL_RUN,
        logical_run_id=logical_run_id,
        source_key=source_key,
        control_instance_id=control_instance_id,
        reset_generation=reset_generation,
    )
    return await _single_record(result)


async def _single_record(result: AsyncResult) -> GraphRecord | None:
    """Convert one Neo4j result row at the driver boundary."""
    record = await result.single()
    if record is None:
        return None
    keys = record.keys()
    values = record.values()
    return record_to_dict(list(keys), list(values))


def _control_result(
    record: GraphRecord | None,
    *,
    publish_recovery: bool,
) -> BoundedLogicalRunControlResult:
    if record is None:
        return BoundedLogicalRunControlResult(outcome="not_found")
    outcome = _required_text(record, "outcome")
    if outcome == "not_found":
        return BoundedLogicalRunControlResult(outcome="not_found")
    if outcome == "conflict":
        return BoundedLogicalRunControlResult(outcome="conflict")
    if outcome != "updated":
        raise RuntimeError("bounded control returned an invalid mutation outcome")
    return BoundedLogicalRunControlResult(
        outcome="updated",
        status=_status_from_record(record),
        publish_recovery=publish_recovery,
    )


def _status_from_record(record: GraphRecord) -> BoundedLogicalRunStatusRecord:
    return BoundedLogicalRunStatusRecord(
        logical_run_id=_required_text(record, "logical_run_id"),
        source_key=_required_text(record, "source_key"),
        control_instance_id=_required_text(record, "control_instance_id"),
        entity_key=to_optional_str(record.get("entity_key")),
        status=_required_text(record, "status"),
        pause_reason=to_optional_str(record.get("pause_reason")),
        occurrence_id=to_optional_str(record.get("occurrence_id")),
        timezone=to_optional_str(record.get("timezone")),
        starts_at=to_iso_or_none(record.get("starts_at")),
        drain_starts_at=to_iso_or_none(record.get("drain_starts_at")),
        cutoff_at=to_iso_or_none(record.get("cutoff_at")),
        next_eligible_at=to_iso_or_none(record.get("next_eligible_at")),
        usage=BoundedLogicalRunUsageRecord(
            records=_nonnegative_int(record.get("records")),
            source_requests=_nonnegative_int(record.get("source_requests")),
            pages=_nonnegative_int(record.get("pages")),
            bytes_read=_nonnegative_int(record.get("bytes_read")),
            extraction_calls=_nonnegative_int(record.get("extraction_calls")),
        ),
        reserved_usage=BoundedLogicalRunUsageRecord(
            records=_nonnegative_int(record.get("reserved_records")),
            source_requests=_nonnegative_int(record.get("reserved_source_requests")),
            pages=_nonnegative_int(record.get("reserved_pages")),
            bytes_read=_nonnegative_int(record.get("reserved_bytes_read")),
            extraction_calls=_nonnegative_int(record.get("reserved_extraction_calls")),
        ),
        attempt_generation=_nonnegative_int(record.get("attempt_generation")),
        source_window_fingerprint=_required_text(record, "source_window_fingerprint"),
        checkpoint_cursor_present=record.get("checkpoint_cursor_present") is True,
        phase=to_optional_str(record.get("phase")),
        checkpointed_at=to_iso_or_none(record.get("checkpointed_at")),
        retry_backlog=_nonnegative_int(record.get("retry_backlog")),
        retry_oldest_at=to_iso_or_none(record.get("retry_oldest_at")),
        failure_category=to_optional_str(record.get("failure_category")),
    )


def _required_text(record: GraphRecord, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"bounded control returned an invalid {key}")
    return value


def _nonnegative_int(value: GraphValue | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value
