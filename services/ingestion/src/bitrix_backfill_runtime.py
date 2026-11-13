"""Per-source-unit corrective accounting inside fenced domain transactions."""

from __future__ import annotations

import json

from neo4j import ManagedTransaction

from src.bitrix_backfill_models import CoverageDisposition, CoverageEntry, GenerationRunContext
from src.bitrix_ingestion_models import ExecutionContext
from src.graph.bitrix_backfill import BitrixBackfillRepository
from src.graph.queries.ingest_skips import RECORD_INGEST_REFRESH_UNIT
from src.graph.queries.ingestion_control import ADVANCE_BITRIX_UNIT_CHECKPOINT
from src.models import IngestResult, JsonValue, SourceRecordEnvelope

#: Checkpoint phase that refreshes the frozen known-owner membership set.
KNOWN_OWNER_REFRESH_PHASE = "known_owner_refresh_v1"

#: Attempt key of the one known-owner refresh attempt shared by a whole run.
KNOWN_OWNER_REFRESH_ATTEMPT_KEY = "known_owner_refresh_v1"


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
        control_instance_id=context.fence_context.control_instance_id,
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
    if context.checkpoint.phase == KNOWN_OWNER_REFRESH_PHASE:
        _record_refresh_unit_accounting(
            tx,
            context=context,
            generation=generation,
            entry=entry,
            result=result,
        )


def refresh_attempt_key(context: ExecutionContext) -> str:
    """Return the attempt key of the known-owner refresh attempt of one run.

    One refresh attempt is shared by every unit of a run, so its counters accumulate
    the whole phase.  Runs without a generation context have no durable refresh
    accounting and keep the legacy empty attempt key.
    """
    if context.generation_context is None:
        return ""
    return KNOWN_OWNER_REFRESH_ATTEMPT_KEY


def _record_refresh_unit_accounting(
    tx: ManagedTransaction,
    *,
    context: ExecutionContext,
    generation: GenerationRunContext,
    entry: CoverageEntry,
    result: IngestResult,
) -> None:
    """Persist one refresh unit's attempt counters inside its own domain transaction."""
    buckets = _result_buckets(result)
    record = tx.run(
        RECORD_INGEST_REFRESH_UNIT,
        ingest_run_id=context.fence_context.ingest_run_id,
        control_instance_id=context.fence_context.control_instance_id,
        attempt_key=refresh_attempt_key(context),
        generation_id=generation.generation_id,
        stream_key=context.fence_context.stream_key,
        source_identity=entry.source_identity,
        source_boundary=entry.source_boundary,
        persisted_count=buckets["persisted"],
        duplicate_count=buckets["duplicate"],
        pipeline_skip_count=buckets["excluded"],
        record_error_count=buckets["error"],
    ).single()
    if record is None:
        raise RuntimeError("known-owner refresh accounting did not persist its terminal unit")


def _result_buckets(result: IngestResult) -> dict[str, int]:
    """Classify one terminal result into exactly one durable attempt bucket."""
    if result.excluded:
        return {"persisted": 0, "duplicate": 0, "excluded": 1, "error": 0}
    if result.skipped_duplicate:
        return {"persisted": 0, "duplicate": 1, "excluded": 0, "error": 0}
    if result.errors:
        return {"persisted": 0, "duplicate": 0, "excluded": 0, "error": 1}
    return {"persisted": 1, "duplicate": 0, "excluded": 0, "error": 0}


def source_lineage_text(
    raw_payload: dict[str, JsonValue],
    canonical_key: str,
    source_key: str,
) -> str | None:
    """Read canonical envelope lineage without changing the record hash contract."""
    canonical = _optional_text(raw_payload.get(canonical_key))
    return canonical if canonical is not None else _optional_text(raw_payload.get(source_key))


def _result_disposition(result: IngestResult) -> CoverageDisposition:
    # A parked unit is re-evaluated on every run exactly as a dropped one was, so a rerun that
    # finds its identical parked version is still "conflict", not "existing_same_hash".
    if result.parked or result.excluded:
        return "conflict"
    if result.skipped_duplicate:
        return "existing_same_hash"
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
            if context.checkpoint.phase == KNOWN_OWNER_REFRESH_PHASE
            else "last_deal_id"
        )
        cursor[cursor_key] = source_id
    elif context.fence_context.stream_key == "crm_activities":
        cursor["last_activity_id"] = source_id
    elif context.fence_context.stream_key == "openlines_conversations":
        crm_start = envelope.raw_payload.get("crm_start")
        if isinstance(crm_start, int) and not isinstance(crm_start, bool):
            cursor["crm_start"] = crm_start
    else:
        raise ValueError("stage history cannot use the backfill runtime cursor fallback")
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
