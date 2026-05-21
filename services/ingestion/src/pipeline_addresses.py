"""Address-only ingestion for source records that do not represent people."""

from __future__ import annotations

import logging

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient
from src.models import IngestResult, MatchDecision, MatchResult, SourceRecordEnvelope
from src.pipeline import IngestPipeline
from src.pipeline_normalization import normalize_envelope_attributes
from src.pipeline_writes import (
    link_source_record_to_address,
    persist_source_record,
)

logger = logging.getLogger(__name__)


def ingest_address_record(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    ingest_run_id: str | None = None,
) -> IngestResult:
    """Persist a source record and attach it to a shared Address node."""
    existing_pk = IngestPipeline(client)._check_idempotency(envelope)
    if existing_pk is not None:
        logger.info(
            "Duplicate address source record %s (hash=%s) — skipping",
            envelope.source_record_id,
            envelope.record_hash,
        )
        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=existing_pk,
            skipped_duplicate=True,
            ingest_run_id=ingest_run_id,
        )

    attributes = normalize_envelope_attributes(envelope)

    def _tx(tx: ManagedTransaction) -> IngestResult:
        source_record_pk = persist_source_record(
            tx,
            envelope=envelope,
            identifiers=[],
            address=None,
            attributes=attributes,
            match_result=MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                reasons=["address_inventory_record"],
            ),
            is_new_person=True,
            ingest_run_id=ingest_run_id,
        )
        link_source_record_to_address(
            tx,
            envelope=envelope,
            source_record_pk=source_record_pk,
        )
        logger.info(
            "Ingested %s -> address postal_code=%s",
            envelope.source_record_id,
            envelope.attributes.get("postal_code"),
        )
        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=source_record_pk,
            ingest_run_id=ingest_run_id,
        )

    with client.session() as session:
        return session.execute_write(_tx)
