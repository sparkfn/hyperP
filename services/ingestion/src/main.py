"""CLI entry point and reusable runner for the ingestion service."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import TypedDict

from neo4j import ManagedTransaction

from src.config import get_settings
from src.connectors.base import SourceConnector
from src.connectors.eko import EkoConnector
from src.connectors.fundbox import (
    FundboxConnector,
    FundboxContactsConnector,
    FundboxLegacyConnector,
    FundboxMergedUsersConnector,
)
from src.connectors.speedzone import SpeedZoneConnector
from src.graph import queries
from src.graph.client import Neo4jClient
from src.graph.schema_init import apply_schema
from src.models import SourceRecordEnvelope
from src.pipeline import IngestPipeline

logger = logging.getLogger(__name__)


# Registry of available connectors keyed by source_key. New sources only need
# to add an entry here; the CLI and the Celery task share the same registry.
_CONNECTOR_REGISTRY: dict[str, type[SourceConnector]] = {
    "fundbox": FundboxConnector,
    "fundbox:contacts": FundboxContactsConnector,
    "fundbox:legacy": FundboxLegacyConnector,
    "fundbox:merged": FundboxMergedUsersConnector,
    "speedzone": SpeedZoneConnector,
    "eko": EkoConnector,
}


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


def _seed_source_system(client: Neo4jClient, source_key: str) -> None:
    """Ensure the SourceSystem node exists in Neo4j."""
    trust = json.dumps(
        {
            "phone": "tier_2",
            "email": "tier_3",
            "full_name": "tier_3",
            "dob": "tier_4",
            "address": "tier_4",
        }
    )

    def _work(tx: ManagedTransaction) -> None:
        tx.run(
            """
            MERGE (ss:SourceSystem {source_key: $source_key})
            ON CREATE SET
                ss.source_system_id = randomUUID(),
                ss.display_name = $display_name,
                ss.system_type = 'pos',
                ss.is_active = true,
                ss.field_trust = $field_trust,
                ss.created_at = datetime(),
                ss.updated_at = datetime()
            """,
            source_key=source_key,
            display_name=source_key.replace("_", " ").title(),
            field_trust=trust,
        )

    with client.session() as session:
        session.execute_write(_work)


class IngestionSummary(TypedDict):
    """Summary returned by :func:`run_ingestion`. Celery serializes this as JSON."""

    ingest_run_id: str
    status: str
    succeeded: int
    errors: int
    skipped: int
    source_key: str
    mode: str


def run_ingestion(source_key: str, mode: str = "batch") -> IngestionSummary:
    """Execute one ingestion run end-to-end.

    Shared by both the CLI entry point and the Celery task.
    """
    settings = get_settings()
    logger.info("Starting ingestion run")
    logger.info("  source-key : %s", source_key)
    logger.info("  mode       : %s", mode)
    logger.info("  batch-size : %d", settings.batch_size)
    logger.info("  neo4j-uri  : %s", settings.neo4j_uri)

    client = Neo4jClient(settings)
    try:
        client.verify_connectivity()
        logger.info("Neo4j connection verified")

        # Apply constraints + indexes before any data work. Idempotent — every
        # statement is `IF NOT EXISTS`, so this is a no-op on warm databases
        # and a critical fix on cold ones (missing indexes turn candidate
        # generation into a full label scan that gets linearly slower).
        apply_schema(client)

        _seed_source_system(client, source_key)

        pipeline = IngestPipeline(client)
        connector = get_connector(source_key)
        logger.info("Using connector: %s", type(connector).__name__)

        def _create_run(tx: ManagedTransaction) -> str:
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
            ingest_run_id = session.execute_write(_create_run)
        logger.info("Created IngestRun %s", ingest_run_id)

        success = errors = skipped = 0
        for raw_record in connector.fetch_records():
            envelope = SourceRecordEnvelope.model_validate(
                {"source_system": connector.get_source_key(), **raw_record},
            )
            result = pipeline.ingest(envelope, ingest_run_id=ingest_run_id)
            if result.skipped_duplicate:
                skipped += 1
            elif result.errors:
                errors += 1
            else:
                success += 1
            logger.info(
                "  %s -> person=%s new=%s decision=%s candidates=%d%s",
                result.source_record_id,
                result.person_id,
                result.is_new_person,
                result.match_decision,
                result.candidate_count,
                " (DUPLICATE)" if result.skipped_duplicate else "",
            )

        final_status = "completed" if errors == 0 else "completed_with_errors"

        def _update_run(tx: ManagedTransaction) -> None:
            tx.run(
                queries.UPDATE_INGEST_RUN,
                ingest_run_id=ingest_run_id,
                status=final_status,
                record_count=success + errors + skipped,
                rejected_count=errors,
            )

        with client.session() as session:
            session.execute_write(_update_run)
        logger.info("Updated IngestRun %s -> %s", ingest_run_id, final_status)
        logger.info(
            "Ingestion complete: %d succeeded, %d errors, %d skipped duplicates",
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
