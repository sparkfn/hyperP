"""Cursor-drain-then-advance loop for incremental (watermark) ingestion."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypedDict

import redis
from neo4j import ManagedTransaction

from src.exclusions import ExclusionContext, build_exclusion_context
from src.graph import queries
from src.graph.client import Neo4jClient
from src.incremental_connector import IncrementalConnector
from src.ingest_skips import SkipLedger, SkipReporter
from src.ingestion_config import get_ingestion_config
from src.models import JsonValue, SourceRecordEnvelope
from src.pipeline import IngestPipeline
from src.source_instances import LEGACY_DEFAULT_CONTROL_INSTANCE_ID
from src.watermark_store import (
    IngestionWatermark,
    load_watermark,
    save_watermark,
)

logger = logging.getLogger(__name__)


class IncrementalRunSummary(TypedDict):
    """Result returned by :func:`run_incremental`."""

    ingest_run_id: str
    status: str
    records_processed: int
    pages_processed: int
    watermark_start: str | None
    watermark_end: str | None
    source_key: str
    entity_key: str | None


def _load_exclusion_context() -> ExclusionContext:
    from src.config import get_settings

    settings = get_settings()
    return build_exclusion_context(
        company_mobile_numbers=settings.company_mobile_numbers,
        company_email_addresses=settings.company_email_addresses,
        internal_person_names=settings.internal_person_names,
        file_exclusions=get_ingestion_config().exclusions,
    )


def _create_watermark_ingest_run(
    client: Neo4jClient,
    source_key: str,
    mode: str,
    *,
    control_instance_id: str,
    watermark_start: str | None,
) -> str:
    """Create an IngestRun node for a watermark-driven run."""

    def _tx(tx: ManagedTransaction) -> str:
        result = tx.run(
            queries.CREATE_INGEST_RUN,
            source_key=source_key,
            control_instance_id=control_instance_id,
            run_type=mode,
            mode=mode,
            watermark_start=watermark_start,
            watermark_end=None,
        )
        record = result.single()
        assert record is not None, "CREATE_INGEST_RUN must return a row"
        run_id_value = record["ingest_run_id"]
        assert isinstance(run_id_value, str)
        return run_id_value

    with client.session() as session:
        return session.execute_write(_tx)


def _finalize_run(
    client: Neo4jClient,
    ingest_run_id: str,
    status: str,
    record_count: int,
    *,
    control_instance_id: str,
    watermark_end: str | None,
) -> None:
    """Finalize an IngestRun with status and optional watermark_end."""

    def _tx(tx: ManagedTransaction) -> None:
        tx.run(
            queries.UPDATE_INGEST_RUN,
            ingest_run_id=ingest_run_id,
            control_instance_id=control_instance_id,
            status=status,
            record_count=record_count,
            rejected_count=0,
            watermark_end=watermark_end,
        )

    with client.session() as session:
        session.execute_write(_tx)


def _process_page_records(
    client: Neo4jClient,
    pipeline: IngestPipeline,
    source_key: str,
    records: tuple[dict[str, JsonValue], ...],
    ingest_run_id: str,
    exclusion_context: ExclusionContext,
    control_instance_id: str,
    ledger: SkipLedger | None = None,
) -> tuple[int, int]:
    """Process records from one page. Returns (succeeded, errors)."""
    from src.main import _exclusion_reason, _process_record

    succeeded = 0
    errors = 0
    for raw_record in records:
        if ledger is not None:
            ledger.checkpoint()
            ledger.emitted += 1
        envelope = SourceRecordEnvelope.model_validate(
            {"source_system": source_key, **raw_record},
        )
        exclusion_reason = _exclusion_reason(envelope, exclusion_context)
        if exclusion_reason is not None:
            if ledger is not None:
                ledger.add_pipeline_skip(exclusion_reason, envelope)
            continue
        result = _process_record(
            client,
            pipeline,
            envelope,
            ingest_run_id,
            exclusion_context,
            control_instance_id=control_instance_id,
        )
        if result.excluded:
            if ledger is not None:
                ledger.add_pipeline_skip(result.skip_reason or "excluded", envelope)
        elif result.skipped_duplicate:
            if ledger is not None:
                ledger.duplicates += 1
        elif result.errors:
            errors += 1
            if ledger is not None:
                ledger.record_errors += 1
        else:
            succeeded += 1
            if ledger is not None:
                ledger.persisted += 1
    return succeeded, errors


def run_incremental(
    connector: IncrementalConnector,
    redis_client: redis.Redis,
    graph_client: Neo4jClient,
    *,
    entity_key: str | None = None,
    shutdown_signal: Callable[[], bool],
    time_window_closing: Callable[[], bool],
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
) -> IncrementalRunSummary:
    """Run one watermark-driven ingestion cycle for a single source."""
    source_key = connector.get_source_key()
    watermark = load_watermark(redis_client, source_key, entity_key)
    mode = "bootstrap" if watermark.updated_at is None else "delta"
    watermark_start_iso = (
        watermark.updated_at.isoformat() if watermark.updated_at is not None else None
    )

    ingest_run_id = _create_watermark_ingest_run(
        graph_client,
        source_key,
        mode,
        control_instance_id=control_instance_id,
        watermark_start=watermark_start_iso,
    )
    logger.info(
        "Watermark run %s started: source=%s mode=%s watermark=%s",
        ingest_run_id,
        source_key,
        mode,
        watermark_start_iso,
    )

    pipeline = IngestPipeline(graph_client, control_instance_id=control_instance_id)
    exclusion_context = _load_exclusion_context()
    max_updated_at = watermark.updated_at
    records_processed = 0
    pages_processed = 0
    ledger = SkipLedger(graph_client, ingest_run_id, source_key, control_instance_id)

    try:
        connector.open_query(updated_since=watermark.updated_at)

        while True:
            if isinstance(connector, SkipReporter):
                ledger.add_connector_skips(connector.drain_skips())
            if time_window_closing() or shutdown_signal():
                ledger.reconcile()
                _finalize_run(
                    graph_client,
                    ingest_run_id,
                    "yielded",
                    records_processed,
                    control_instance_id=control_instance_id,
                    watermark_end=None,
                )
                return IncrementalRunSummary(
                    ingest_run_id=ingest_run_id,
                    status="yielded",
                    records_processed=records_processed,
                    pages_processed=pages_processed,
                    watermark_start=watermark_start_iso,
                    watermark_end=None,
                    source_key=source_key,
                    entity_key=entity_key,
                )

            page = connector.fetch_next_page()
            if isinstance(connector, SkipReporter):
                ledger.add_connector_skips(connector.drain_skips())
            _process_page_records(
                graph_client,
                pipeline,
                source_key,
                page.records,
                ingest_run_id,
                exclusion_context,
                control_instance_id,
                ledger=ledger,
            )
            records_processed += len(page.records)
            pages_processed += 1

            if max_updated_at is None or page.max_updated_at > max_updated_at:
                max_updated_at = page.max_updated_at

            if not page.has_more:
                if isinstance(connector, SkipReporter):
                    ledger.add_connector_skips(connector.drain_skips())
                ledger.reconcile()
                watermark_end_iso = (
                    max_updated_at.isoformat() if max_updated_at is not None else None
                )
                if max_updated_at is not None:
                    save_watermark(
                        redis_client,
                        IngestionWatermark(
                            updated_at=max_updated_at,
                            source_key=source_key,
                            entity_key=entity_key,
                        ),
                    )
                _finalize_run(
                    graph_client,
                    ingest_run_id,
                    "caught_up",
                    records_processed,
                    control_instance_id=control_instance_id,
                    watermark_end=watermark_end_iso,
                )
                return IncrementalRunSummary(
                    ingest_run_id=ingest_run_id,
                    status="caught_up",
                    records_processed=records_processed,
                    pages_processed=pages_processed,
                    watermark_start=watermark_start_iso,
                    watermark_end=watermark_end_iso,
                    source_key=source_key,
                    entity_key=entity_key,
                )
    except Exception:
        try:
            if isinstance(connector, SkipReporter):
                ledger.add_connector_skips(connector.drain_skips())
            ledger.flush_quietly()
            _finalize_run(
                graph_client,
                ingest_run_id,
                "failed",
                records_processed,
                control_instance_id=control_instance_id,
                watermark_end=None,
            )
        except Exception:
            logger.exception("Failed to finalize IngestRun %s as failed", ingest_run_id)
        raise
    finally:
        connector.close()
