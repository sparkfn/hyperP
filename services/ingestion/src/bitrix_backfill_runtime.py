"""Per-source-unit corrective accounting inside fenced domain transactions."""

from __future__ import annotations

import json

from neo4j import ManagedTransaction

from src.bitrix_backfill_models import CoverageDisposition, CoverageEntry
from src.bitrix_ingestion_models import ExecutionContext
from src.graph.bitrix_backfill import BitrixBackfillRepository
from src.graph.queries.ingestion_control import ADVANCE_BITRIX_UNIT_CHECKPOINT
from src.models import IngestResult, JsonValue, SourceRecordEnvelope


def record_terminal_unit(
    tx: ManagedTransaction,
    *,
    context: ExecutionContext,
    envelope: SourceRecordEnvelope,
    result: IngestResult,
    disposition: CoverageDisposition | None = None,
    scope_state: str | None = None,
    detail: str | None = None,
) -> None:
    """Commit coverage and cursor advancement after a terminal domain outcome."""
    generation = context.generation_context
    if generation is None:
        return
    resolved_disposition = disposition or _result_disposition(result)
    raw_deal_id = envelope.raw_payload.get("ID")
    deal_id = str(raw_deal_id) if raw_deal_id is not None else _parent_deal_id(envelope)
    category_id = source_lineage_text(envelope.raw_payload, "category_id", "CATEGORY_ID")
    stage_id = source_lineage_text(envelope.raw_payload, "stage_id", "STAGE_ID")
    census_epoch = _census_epoch(context)
    entry = CoverageEntry(
        source_identity=envelope.source_record_id,
        source_boundary=f"{generation.boundary_digest}:{context.checkpoint.phase}",
        disposition=resolved_disposition,
        source_observation_hash=envelope.record_hash,
        deal_id=deal_id,
        scope_state=scope_state,
        entity_key=envelope.entity_key,
        category_id=category_id,
        stage_id=stage_id,
        census_epoch=census_epoch,
        detail=detail,
    )
    BitrixBackfillRepository.record_coverage_in_transaction(
        tx,
        generation_id=generation.generation_id,
        stream_key=context.fence_context.stream_key,
        fence_context=context.fence_context,
        entry=entry,
    )
    cursor = _cursor_after(context, envelope)
    deltas = _counter_deltas(resolved_disposition)
    record = tx.run(
        ADVANCE_BITRIX_UNIT_CHECKPOINT,
        source_key=context.fence_context.source_key,
        stream_key=context.fence_context.stream_key,
        logical_run_id=context.fence_context.logical_run_id,
        ingest_run_id=context.fence_context.ingest_run_id,
        attempt_generation=context.fence_context.attempt_generation,
        stream_generation=context.fence_context.stream_generation,
        fencing_token=context.fence_context.fencing_token,
        phase=context.checkpoint.phase,
        cursor_json=json.dumps(cursor, sort_keys=True, separators=(",", ":")),
        source_window_json=json.dumps(
            context.checkpoint.source_window,
            sort_keys=True,
            separators=(",", ":"),
        ),
        connector_version=context.checkpoint.connector_version,
        checkpoint_schema_version=context.checkpoint.schema_version,
        last_committed_record_id=envelope.source_record_id,
        committed_delta=deltas["committed_delta"],
        duplicate_delta=deltas["duplicate_delta"],
        excluded_delta=deltas["excluded_delta"],
        retry_delta=deltas["retry_delta"],
    ).single()
    if record is None:
        raise RuntimeError("Bitrix checkpoint did not advance with its terminal coverage row")
    if record["stop_requested"] is True:
        raise RuntimeError("Bitrix logical run stop was requested at a committed unit boundary")


def source_lineage_text(
    raw_payload: dict[str, JsonValue],
    canonical_key: str,
    source_key: str,
) -> str | None:
    """Read canonical envelope lineage without changing the record hash contract."""
    canonical = _optional_text(raw_payload.get(canonical_key))
    return canonical if canonical is not None else _optional_text(raw_payload.get(source_key))


def _result_disposition(result: IngestResult) -> CoverageDisposition:
    if result.skipped_duplicate:
        return "existing_same_hash"
    if result.dropped:
        return "conflict"
    return "created"


def _cursor_after(
    context: ExecutionContext,
    envelope: SourceRecordEnvelope,
) -> dict[str, JsonValue]:
    cursor = dict(context.checkpoint.cursor)
    source_id = _numeric_suffix(envelope.source_record_id)
    if context.fence_context.stream_key == "crm_deals":
        cursor_key = (
            "last_known_deal_id"
            if context.checkpoint.phase == "known_owner_refresh_v1"
            else "last_deal_id"
        )
        cursor[cursor_key] = source_id
    elif context.fence_context.stream_key == "crm_activities":
        cursor["last_activity_id"] = source_id
    else:
        crm_start = envelope.raw_payload.get("crm_start")
        if isinstance(crm_start, int) and not isinstance(crm_start, bool):
            cursor["crm_start"] = crm_start
    return cursor


def _counter_deltas(disposition: CoverageDisposition) -> dict[str, int]:
    return {
        "committed_delta": int(disposition in {"created", "updated_projection"}),
        "duplicate_delta": int(disposition in {"existing_same_hash", "scope_unchanged"}),
        "excluded_delta": int(
            disposition in {"excluded_out_of_scope", "quarantined_owner_unresolved", "conflict"}
        ),
        "retry_delta": int(disposition == "failed"),
    }


def _numeric_suffix(source_record_id: str) -> str:
    value = source_record_id.rsplit("-", maxsplit=1)[-1]
    if not value.isdigit():
        raise ValueError("Bitrix split source identity must end in a numeric ID")
    return value


def _parent_deal_id(envelope: SourceRecordEnvelope) -> str | None:
    if envelope.parent_ref is None:
        return None
    return envelope.parent_ref.parent_source_record_id.rsplit("-", maxsplit=1)[-1]


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _census_epoch(context: ExecutionContext) -> int | None:
    value = context.checkpoint.cursor.get("census_epoch")
    return value if isinstance(value, int) and not isinstance(value, bool) else None
