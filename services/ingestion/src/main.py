"""CLI entry point and reusable runner for the ingestion service."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TypedDict

from neo4j import ManagedTransaction

from src.config import get_settings
from src.connectors.base import SourceConnector
from src.connectors.bitrix import BitrixChatConnector
from src.connectors.dumps.connectors import get_dump_connector
from src.connectors.dumps.reader import resolve_dump_path
from src.connectors.eko import EkoConnector, EkoSalesConnector
from src.connectors.fundbox import (
    FundboxConnector,
    FundboxContactsConnector,
    FundboxLegacyConnector,
    FundboxMergedUsersConnector,
    FundboxSalesConnector,
)
from src.connectors.speedzone import SpeedZoneConnector, SpeedZoneSalesConnector
from src.connectors.whatsapp import WhatsAppChatConnector
from src.exclusions import (
    ExclusionContext,
    build_exclusion_context,
    is_excluded_email,
    is_excluded_phone,
    is_excluded_source_id,
)
from src.graph import queries
from src.graph.bootstrap import bootstrap_entities_and_sources
from src.graph.client import Neo4jClient
from src.graph.migrations import apply_data_migrations
from src.graph.schema_init import apply_schema
from src.ingestion_config import get_ingestion_config
from src.models import IngestResult, RecordType, SourceRecordEnvelope
from src.pipeline import IngestPipeline
from src.pipeline_addresses import ingest_address_record
from src.pipeline_knows import (
    materialize_knows_from_chat_relationships,
    materialize_knows_from_contacts,
)
from src.pipeline_sales import (
    drain_pending_customer_sales,
    ingest_sales_record,
    propose_machine_unit_matches_for_pending_sales,
)

logger = logging.getLogger(__name__)


# Registry of available connectors keyed by source_key. New sources only need
# to add an entry here; the CLI and the Celery task share the same registry.
_CONNECTOR_REGISTRY: dict[str, type[SourceConnector]] = {
    "fundbox_consumer_backend": FundboxConnector,
    "fundbox_consumer_backend:contacts": FundboxContactsConnector,
    "fundbox_consumer_backend:legacy": FundboxLegacyConnector,
    "fundbox_consumer_backend:merged": FundboxMergedUsersConnector,
    "fundbox_consumer_backend:sales": FundboxSalesConnector,
    "speedzone_phppos": SpeedZoneConnector,
    "speedzone_phppos:sales": SpeedZoneSalesConnector,
    "eko_phppos": EkoConnector,
    "eko_phppos:sales": EkoSalesConnector,
    "whatsapp_chat": WhatsAppChatConnector,
    "bitrix_chat": BitrixChatConnector,
    # SG government registers (sgbankruptcy, sgrentalflats) are dump-only — they
    # have no live source, so they are reached via get_dump_connector with an
    # explicit dump_path passed in the task call (mode="dump"), not registered
    # here for batch mode.
}

_ADDRESS_ONLY_SOURCES = frozenset({"sgrentalflats"})


def _is_address_only_source(source_key: str) -> bool:
    return source_key in _ADDRESS_ONLY_SOURCES


def _mark_run_failed(
    client: Neo4jClient,
    ingest_run_id: str,
    record_count: int,
    rejected_count: int,
) -> None:
    """Best-effort finaliser that records a run as ``completed_with_errors``.

    Swallows any secondary failure so the original exception propagates to
    the Celery task handler.
    """
    try:

        def _work(tx: ManagedTransaction) -> None:
            tx.run(
                queries.UPDATE_INGEST_RUN,
                ingest_run_id=ingest_run_id,
                status="completed_with_errors",
                record_count=record_count,
                rejected_count=rejected_count,
            )

        with client.session() as session:
            session.execute_write(_work)
        logger.warning("Marked IngestRun %s -> completed_with_errors", ingest_run_id)
    except Exception:
        logger.exception("Failed to mark IngestRun %s as failed", ingest_run_id)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)


def get_connector(source_key: str, dump_path: str | None = None) -> SourceConnector:
    """Return the appropriate connector for the given source key."""
    if dump_path is not None:
        settings = get_settings()
        resolved_dump_path = resolve_dump_path(dump_path, settings.dumps_root)
        return get_dump_connector(source_key, resolved_dump_path)
    try:
        return _CONNECTOR_REGISTRY[source_key]()
    except KeyError as exc:
        available = ", ".join(sorted(_CONNECTOR_REGISTRY))
        raise ValueError(f"Unknown source key: {source_key!r}. Available: {available}") from exc


class IngestionSummary(TypedDict):
    """Summary returned by :func:`run_ingestion`. Celery serializes this as JSON."""

    ingest_run_id: str
    status: str
    succeeded: int
    errors: int
    skipped: int
    source_key: str
    mode: str
    dump_path: str | None


def _create_ingest_run(client: Neo4jClient, source_key: str, mode: str) -> str:
    """Create an IngestRun node and return its ID."""

    def _tx(tx: ManagedTransaction) -> str:
        result = tx.run(
            queries.CREATE_INGEST_RUN,
            source_key=source_key,
            run_type=mode,
        )
        record = result.single()
        assert record is not None, "CREATE_INGEST_RUN must return a row"
        run_id_value = record["ingest_run_id"]
        assert isinstance(run_id_value, str)
        return run_id_value

    with client.session() as session:
        return session.execute_write(_tx)


def _finalize_ingest_run(
    client: Neo4jClient,
    ingest_run_id: str,
    status: str,
    record_count: int,
    rejected_count: int,
) -> None:
    """Update the IngestRun with final status and counts."""

    def _tx(tx: ManagedTransaction) -> None:
        tx.run(
            queries.UPDATE_INGEST_RUN,
            ingest_run_id=ingest_run_id,
            status=status,
            record_count=record_count,
            rejected_count=rejected_count,
        )

    with client.session() as session:
        session.execute_write(_tx)


def _process_record(
    client: Neo4jClient,
    pipeline: IngestPipeline,
    envelope: SourceRecordEnvelope,
    ingest_run_id: str,
    exclusion_context: ExclusionContext,
) -> IngestResult:
    """Route a single envelope to the correct ingestion pipeline."""
    if envelope.record_type == RecordType.SALES:
        return ingest_sales_record(
            client,
            envelope,
            ingest_run_id=ingest_run_id,
            exclusion_context=exclusion_context,
        )
    if _is_address_only_source(envelope.source_system):
        return ingest_address_record(client, envelope, ingest_run_id=ingest_run_id)
    return pipeline.ingest(
        envelope,
        ingest_run_id=ingest_run_id,
        exclusion_context=exclusion_context,
    )


def _record_is_excluded(envelope: SourceRecordEnvelope, context: ExclusionContext) -> bool:
    if is_excluded_source_id(envelope.source_record_id, context):
        return True
    for identifier in envelope.identifiers:
        if identifier.type == "phone" and is_excluded_phone(identifier.value, context):
            return True
        if identifier.type == "email" and is_excluded_email(identifier.value, context):
            return True
    return False


def _load_exclusion_context() -> ExclusionContext:
    settings = get_settings()
    return build_exclusion_context(
        company_mobile_numbers=settings.company_mobile_numbers,
        company_email_addresses=settings.company_email_addresses,
        internal_person_names=settings.internal_person_names,
        file_exclusions=get_ingestion_config().exclusions,
    )


def _ingest_all_records(
    client: Neo4jClient,
    pipeline: IngestPipeline,
    connector: SourceConnector,
    ingest_run_id: str,
    exclusion_context: ExclusionContext | None = None,
) -> tuple[int, int, int]:
    """Process every record from the connector. Returns (success, errors, skipped)."""
    success = errors = skipped = 0
    active_exclusion_context = (
        exclusion_context if exclusion_context is not None else _load_exclusion_context()
    )
    for raw_record in connector.fetch_records():
        envelope = SourceRecordEnvelope.model_validate(
            {"source_system": connector.get_source_key(), **raw_record},
        )
        if _record_is_excluded(envelope, active_exclusion_context):
            skipped += 1
            logger.info("  %s -> excluded", envelope.source_record_id)
            continue
        result = _process_record(
            client,
            pipeline,
            envelope,
            ingest_run_id,
            active_exclusion_context,
        )
        if result.skipped_duplicate:
            skipped += 1
        elif result.errors:
            errors += 1
        else:
            success += 1
        if result.person_id is None:
            logger.info(
                "  %s -> address-only%s",
                result.source_record_id,
                " (DUPLICATE)" if result.skipped_duplicate else "",
            )
        else:
            logger.info(
                "  %s -> person=%s new=%s decision=%s candidates=%d%s",
                result.source_record_id,
                result.person_id,
                result.is_new_person,
                result.match_decision,
                result.candidate_count,
                " (DUPLICATE)" if result.skipped_duplicate else "",
            )
    return success, errors, skipped


def initialize_ingestion_graph() -> None:
    settings = get_settings()
    client = Neo4jClient(settings)
    try:
        client.verify_connectivity()
        apply_schema(client)
        bootstrap_entities_and_sources(client)
        apply_data_migrations(client)
    finally:
        client.close()


def run_ingestion(
    source_key: str,
    mode: str = "batch",
    dump_path: str | None = None,
    *,
    initialize_graph: bool = True,
) -> IngestionSummary:
    """Execute one ingestion run end-to-end."""
    settings = get_settings()
    if mode == "dump" and dump_path is None:
        raise ValueError("dump_path is required when mode='dump'")
    if mode != "dump" and dump_path is not None:
        raise ValueError("dump_path is only valid when mode='dump'")
    logger.info("Starting ingestion: source=%s mode=%s", source_key, mode)

    if initialize_graph:
        initialize_ingestion_graph()

    client = Neo4jClient(settings)
    try:
        if not initialize_graph:
            client.verify_connectivity()

        pipeline = IngestPipeline(client)
        connector = get_connector(source_key, dump_path if mode == "dump" else None)
        ingest_run_id = _create_ingest_run(client, source_key, mode)
        logger.info("IngestRun %s created, connector=%s", ingest_run_id, type(connector).__name__)

        try:
            exclusion_context = _load_exclusion_context()
            success, errors, skipped = _ingest_all_records(
                client,
                pipeline,
                connector,
                ingest_run_id,
                exclusion_context,
            )
            drained = drain_pending_customer_sales(
                client,
                exclusion_context=exclusion_context,
            )
            if drained:
                logger.info("Drained %d pending sales records", drained)
            proposed = propose_machine_unit_matches_for_pending_sales(client)
            if proposed:
                logger.info("Proposed %d machine-unit review cases for pending sales", proposed)
            chat_knows_linked = materialize_knows_from_chat_relationships(client)
            if chat_knows_linked:
                logger.info(
                    "Materialized %d KNOWS edges from chat relationships",
                    chat_knows_linked,
                )
            knows_linked = materialize_knows_from_contacts(client)
            if knows_linked:
                logger.info("Materialized %d KNOWS edges from contacts", knows_linked)
        except Exception:
            _mark_run_failed(client, ingest_run_id, 0, 0)
            raise

        final_status = "completed" if errors == 0 else "completed_with_errors"
        _finalize_ingest_run(
            client,
            ingest_run_id,
            final_status,
            success + errors + skipped,
            errors,
        )
        logger.info(
            "Ingestion complete: %d succeeded, %d errors, %d skipped",
            success,
            errors,
            skipped,
        )
        return {
            "ingest_run_id": ingest_run_id,
            "status": final_status,
            "succeeded": success,
            "errors": errors,
            "skipped": skipped,
            "source_key": source_key,
            "mode": mode,
            "dump_path": dump_path,
        }
    finally:
        client.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="profile-unifier-ingestion",
        description="Ingestion service for the profile unification platform",
    )
    parser.add_argument("--source-key", required=True)
    parser.add_argument("--mode", choices=["batch", "backfill", "dump"], default="batch")
    parser.add_argument("--dump-path", default=None)
    args = parser.parse_args(argv)

    setup_logging(get_settings().log_level)
    try:
        run_ingestion(args.source_key, args.mode, args.dump_path)
    except Exception:
        logger.exception("Fatal error during ingestion")
        sys.exit(1)


if __name__ == "__main__":
    main()
