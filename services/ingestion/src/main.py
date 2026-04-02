"""CLI entry point for the ingestion service."""

from __future__ import annotations

import argparse
import logging
import sys

from src.config import get_settings
from src.connectors.base import SourceConnector
from src.connectors.sample import SampleConnector
from src.graph import queries
from src.graph.client import Neo4jClient
from src.models import SourceRecordEnvelope
from src.pipeline import IngestPipeline


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _get_connector(source_key: str) -> SourceConnector:
    """Return the appropriate connector for the given source key."""
    if source_key == "sample_pos":
        return SampleConnector()
    raise ValueError(f"Unknown source key: {source_key!r}. Available: sample_pos")


def _seed_source_system(client: Neo4jClient, source_key: str) -> None:
    """Ensure the SourceSystem node exists in Neo4j."""

    import json as _json

    trust = _json.dumps({
        "phone": "tier_2",
        "email": "tier_3",
        "full_name": "tier_3",
        "dob": "tier_4",
        "address": "tier_4",
    })

    def _work(tx):  # type: ignore[no-untyped-def]
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="profile-unifier-ingestion",
        description="Ingestion service for the profile unification platform",
    )
    parser.add_argument(
        "--source-key",
        required=True,
        help="Source system key (must match a SourceSystem node in Neo4j)",
    )
    parser.add_argument(
        "--mode",
        choices=["batch", "backfill"],
        default="batch",
        help="Ingestion mode (default: batch)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    _setup_logging(settings.log_level)
    logger = logging.getLogger(__name__)

    logger.info("Starting ingestion service")
    logger.info("  source-key : %s", args.source_key)
    logger.info("  mode       : %s", args.mode)
    logger.info("  batch-size : %d", settings.batch_size)
    logger.info("  neo4j-uri  : %s", settings.neo4j_uri)

    client = Neo4jClient(settings)
    try:
        client.verify_connectivity()
        logger.info("Neo4j connection verified")

        # Seed the SourceSystem node if it doesn't exist
        _seed_source_system(client, args.source_key)

        pipeline = IngestPipeline(client)
        connector = _get_connector(args.source_key)
        logger.info("Using connector: %s", type(connector).__name__)

        # Create IngestRun node before processing records
        def _create_run(tx):  # type: ignore[no-untyped-def]
            result = tx.run(
                queries.CREATE_INGEST_RUN,
                source_key=args.source_key,
                run_type=args.mode,
            )
            record = result.single()
            return record["ingest_run_id"]

        with client.session() as session:
            ingest_run_id = session.execute_write(_create_run)
        logger.info("Created IngestRun %s", ingest_run_id)

        success = 0
        errors = 0
        skipped = 0
        for raw_record in connector.fetch_records():
            envelope = SourceRecordEnvelope(
                source_system=connector.get_source_key(),
                **raw_record,
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

        # Update IngestRun with final status
        final_status = "completed" if errors == 0 else "completed_with_errors"

        def _update_run(tx):  # type: ignore[no-untyped-def]
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
            success, errors, skipped,
        )

    except Exception:
        logger.exception("Fatal error during ingestion startup")
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
