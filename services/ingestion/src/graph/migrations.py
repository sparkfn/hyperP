"""Idempotent data migrations applied on ingestion startup.

Distinct from :mod:`src.graph.schema_init` (which applies constraints/indexes)
and :mod:`src.graph.bootstrap` (which seeds entity/source-system metadata):
these rewrite existing data so it matches the current domain model. Every
migration here must be safe to run repeatedly.
"""

from __future__ import annotations

import logging

from neo4j import ManagedTransaction

from src.graph import queries
from src.graph.client import Neo4jClient

logger = logging.getLogger(__name__)


def backfill_record_type_subtypes(client: Neo4jClient) -> int:
    """Reclassify legacy ``record_type='system'`` records into subtypes.

    Maps existing SourceRecords by ``source_system`` to the three subtypes that
    replaced ``system`` (identity / public_record / relationship). Returns the
    number of records updated; ``0`` once the backfill has already run.
    """

    def _work(tx: ManagedTransaction) -> int:
        record = tx.run(queries.BACKFILL_RECORD_TYPE_SUBTYPES).single()
        return int(record["updated"]) if record is not None else 0

    updated = client.execute_write(_work)
    if updated:
        logger.info("Backfilled record_type on %d legacy 'system' source records", updated)
    return updated


def apply_data_migrations(client: Neo4jClient) -> None:
    """Run every idempotent data migration in order."""
    backfill_record_type_subtypes(client)
