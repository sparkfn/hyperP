"""CLI entry point and reusable runner for the ingestion service."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TypedDict

from neo4j import ManagedTransaction

from src.config import get_settings
from src.connectors.base import SourceConnector
from src.connectors.eko import EkoConnector, EkoSalesConnector
from src.connectors.fundbox import (
    FundboxConnector,
    FundboxContactsConnector,
    FundboxLegacyConnector,
    FundboxMergedUsersConnector,
    FundboxSalesConnector,
)
from src.connectors.speedzone import SpeedZoneConnector, SpeedZoneSalesConnector
from src.graph import queries
from src.graph.bootstrap import bootstrap_entities_and_sources
from src.graph.client import Neo4jClient
from src.graph.schema_init import apply_schema
from src.models import IngestResult, RecordType, SourceRecordEnvelope
from src.pipeline import IngestPipeline
from src.pipeline_knows import materialize_knows_from_contacts
from src.pipeline_sales import drain_pending_customer_sales, ingest_sales_record

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
}


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


def get_connector(source_key: str) -> SourceConnector:
    """Return the appropriate connector for the given source key."""
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


def _create_ingest_run(
    client: Neo4jClient, source_key: str, mode: str
) -> str:
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
) -> IngestResult:
    """Route a single envelope to the sales or identity pipeline."""
    if envelope.record_type == RecordType.SALES:
        return ingest_sales_record(client, envelope, ingest_run_id=ingest_run_id)
    return pipeline.ingest(envelope, ingest_run_id=ingest_run_id)


def _ingest_all_records(
    client: Neo4jClient,
    pipeline: IngestPipeline,
    connector: SourceConnector,
    ingest_run_id: str,
) -> tuple[int, int, int]:
    """Process every record from the connector. Returns (success, errors, skipped)."""
    success = errors = skipped = 0
    for raw_record in connector.fetch_records():
        envelope = SourceRecordEnvelope.model_validate(
            {"source_system": connector.get_source_key(), **raw_record},
        )
        result = _process_record(client, pipeline, envelope, ingest_run_id)
        if result.skipped_duplicate:
            skipped += 1
        elif result.errors:
            errors += 1
        else:
            success += 1
        logger.info(
            "  %s -> person=%s new=%s decision=%s candidates=%d%s",
            result.source_record_id, result.person_id, result.is_new_person,
            result.match_decision, result.candidate_count,
            " (DUPLICATE)" if result.skipped_duplicate else "",
        )
    return success, errors, skipped


def run_ingestion(source_key: str, mode: str = "batch") -> IngestionSummary:
    """Execute one ingestion run end-to-end."""
    settings = get_settings()
    logger.info("Starting ingestion: source=%s mode=%s", source_key, mode)

    client = Neo4jClient(settings)
    try:
        client.verify_connectivity()
        apply_schema(client)
        bootstrap_entities_and_sources(client)

        pipeline = IngestPipeline(client)
        connector = get_connector(source_key)
        ingest_run_id = _create_ingest_run(client, source_key, mode)
        logger.info("IngestRun %s created, connector=%s", ingest_run_id, type(connector).__name__)

        try:
            success, errors, skipped = _ingest_all_records(
                client, pipeline, connector, ingest_run_id,
            )
            drained = drain_pending_customer_sales(client)
            if drained:
                logger.info("Drained %d pending sales records", drained)
            knows_linked = materialize_knows_from_contacts(client)
            if knows_linked:
                logger.info("Materialized %d KNOWS edges from contacts", knows_linked)
        except Exception:
            _mark_run_failed(client, ingest_run_id, 0, 0)
            raise

        final_status = "completed" if errors == 0 else "completed_with_errors"
        _finalize_ingest_run(
            client, ingest_run_id, final_status, success + errors + skipped, errors,
        )
        logger.info(
            "Ingestion complete: %d succeeded, %d errors, %d skipped",
            success, errors, skipped,
        )
        return {
            "ingest_run_id": ingest_run_id, "status": final_status,
            "succeeded": success, "errors": errors, "skipped": skipped,
            "source_key": source_key, "mode": mode,
        }
    finally:
        client.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="profile-unifier-ingestion",
        description="Ingestion service for the profile unification platform",
    )
    parser.add_argument("--source-key", required=True)
    parser.add_argument("--mode", choices=["batch", "backfill"], default="batch")
    args = parser.parse_args(argv)

    setup_logging(get_settings().log_level)
    try:
        run_ingestion(args.source_key, args.mode)
    except Exception:
        logger.exception("Fatal error during ingestion")
        sys.exit(1)


if __name__ == "__main__":
    main()
