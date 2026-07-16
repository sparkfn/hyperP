"""Address-only ingestion for source records that do not represent people."""

from __future__ import annotations

import logging

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient
from src.models import (
    IngestResult,
    MatchDecision,
    MatchResult,
    SourceRecordEnvelope,
    SourceRecordLifecycleStatus,
)
from src.pipeline_normalization import normalize_envelope_attributes
from src.pipeline_writes import (
    link_source_record_to_address,
    persist_source_record,
    retire_address_projection,
)
from src.record_lifecycle import (
    DuplicateVersion,
    activate_staged_version,
    load_locked_source_state,
    plan_incoming_version,
    reject_replaced_pending,
)

logger = logging.getLogger(__name__)


def ingest_address_record(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    ingest_run_id: str | None = None,
) -> IngestResult:
    """Persist a source record and attach it to a shared Address node."""

    def _tx(tx: ManagedTransaction) -> IngestResult:
        state = load_locked_source_state(tx, envelope.source_system, envelope.source_record_id)
        plan = plan_incoming_version(state, envelope.record_hash)
        if isinstance(plan, DuplicateVersion):
            return IngestResult(
                source_record_id=envelope.source_record_id,
                source_record_pk=plan.source_record_pk,
                skipped_duplicate=True,
                ingest_run_id=ingest_run_id,
            )
        if plan.pending_to_reject is not None:
            reject_replaced_pending(tx, plan.pending_to_reject)
        envelope.source_record_version = str(plan.version)
        attributes = normalize_envelope_attributes(envelope)
        source_record_pk = persist_source_record(
            tx,
            envelope=envelope,
            identifiers=[],
            addresses=[],
            attributes=attributes,
            match_result=MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                reasons=["address_inventory_record"],
            ),
            is_new_person=True,
            ingest_run_id=ingest_run_id,
            lifecycle_status=SourceRecordLifecycleStatus.PENDING_REVIEW,
            expected_active_source_record_pk=plan.active_source_record_pk,
        )
        link_source_record_to_address(
            tx,
            envelope=envelope,
            source_record_pk=source_record_pk,
        )
        if plan.active_source_record_pk is not None:
            retire_address_projection(tx, plan.active_source_record_pk)
        activate_staged_version(
            tx,
            source_system=envelope.source_system,
            source_record_id=envelope.source_record_id,
            old_source_record_pk=plan.active_source_record_pk,
            new_source_record_pk=source_record_pk,
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
