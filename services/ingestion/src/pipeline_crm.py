"""Persistence for immutable CRM activity and universal call source records."""

from __future__ import annotations

import json

from neo4j import ManagedTransaction

from src.graph import queries
from src.graph.client import Neo4jClient
from src.models import IngestResult, RecordType, SourceRecordEnvelope
from src.record_lifecycle import load_locked_source_state
from src.source_version_keys import encode_source_version_key


def ingest_crm_history_record(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    *,
    ingest_run_id: str,
) -> IngestResult:
    """Create a first-observed CRM activity, never a replacement version."""
    if envelope.record_type != RecordType.CRM_HISTORY or envelope.parent_ref is None:
        raise ValueError("crm history ingestion requires a crm_history envelope with parent_ref")
    parent_ref = envelope.parent_ref

    def _work(tx: ManagedTransaction) -> IngestResult:
        load_locked_source_state(tx, envelope.source_system, envelope.source_record_id)
        existing = tx.run(
            queries.FIND_ANY_SOURCE_RECORD,
            source_system=envelope.source_system,
            source_record_id=envelope.source_record_id,
        ).single()
        if existing is not None:
            return IngestResult(
                source_record_id=envelope.source_record_id,
                source_record_pk=str(existing["source_record_pk"]),
                skipped_duplicate=True,
                ingest_run_id=ingest_run_id,
            )
        created = tx.run(
            queries.CREATE_CRM_HISTORY,
            source_system=envelope.source_system,
            source_record_id=envelope.source_record_id,
            source_version_key=encode_source_version_key(
                envelope.source_system, envelope.source_record_id, "1"
            ),
            parent_source_system=parent_ref.parent_source_system,
            parent_source_record_id=parent_ref.parent_source_record_id,
            observed_at=envelope.observed_at,
            record_hash=envelope.record_hash,
            raw_payload=json.dumps(envelope.raw_payload, default=str),
        ).single()
        if created is None:
            return IngestResult(
                source_record_id=envelope.source_record_id,
                dropped=True,
                ingest_run_id=ingest_run_id,
            )
        source_record_pk = str(created["source_record_pk"])
        if ingest_run_id:
            tx.run(
                queries.LINK_SOURCE_RECORD_TO_RUN,
                source_record_pk=source_record_pk,
                ingest_run_id=ingest_run_id,
            )
        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=source_record_pk,
            ingest_run_id=ingest_run_id,
        )

    with client.session() as session:
        return session.execute_write(_work)


def ingest_call_record(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    *,
    ingest_run_id: str,
) -> IngestResult:
    """Create a call only when its immutable history parent has person context."""
    if envelope.record_type != RecordType.CALL or envelope.parent_ref is None:
        raise ValueError("call ingestion requires a call envelope with parent_ref")
    parent_ref = envelope.parent_ref
    crm_activity_id = envelope.raw_payload.get("crm_activity_id")
    if not isinstance(crm_activity_id, str) or not crm_activity_id:
        raise ValueError("call source records require raw_payload.crm_activity_id")

    def _work(tx: ManagedTransaction) -> IngestResult:
        load_locked_source_state(tx, envelope.source_system, envelope.source_record_id)
        existing = tx.run(
            queries.FIND_ANY_SOURCE_RECORD,
            source_system=envelope.source_system,
            source_record_id=envelope.source_record_id,
        ).single()
        if existing is not None:
            return IngestResult(
                source_record_id=envelope.source_record_id,
                source_record_pk=str(existing["source_record_pk"]),
                skipped_duplicate=True,
                ingest_run_id=ingest_run_id,
            )
        created = tx.run(
            queries.CREATE_CALL_FROM_HISTORY,
            source_system=envelope.source_system,
            source_record_id=envelope.source_record_id,
            source_version_key=encode_source_version_key(
                envelope.source_system, envelope.source_record_id, "1"
            ),
            parent_source_system=parent_ref.parent_source_system,
            parent_source_record_id=parent_ref.parent_source_record_id,
            observed_at=envelope.observed_at,
            record_hash=envelope.record_hash,
            raw_payload=json.dumps(envelope.raw_payload, default=str),
            crm_activity_id=crm_activity_id,
        ).single()
        if created is None:
            return IngestResult(
                source_record_id=envelope.source_record_id,
                dropped=True,
                ingest_run_id=ingest_run_id,
            )
        source_record_pk = str(created["source_record_pk"])
        person_id = str(created["person_id"])
        if ingest_run_id:
            tx.run(
                queries.LINK_SOURCE_RECORD_TO_RUN,
                source_record_pk=source_record_pk,
                ingest_run_id=ingest_run_id,
            )
        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=source_record_pk,
            person_id=person_id,
            ingest_run_id=ingest_run_id,
        )

    with client.session() as session:
        return session.execute_write(_work)


def link_conversation_to_crm_history(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    source_record_pk: str,
) -> bool:
    """Link a persisted Open Lines conversation to an exact CRM activity ID."""
    if envelope.parent_ref is None:
        return False
    parent_ref = envelope.parent_ref
    crm_activity_id = envelope.raw_payload.get("crm_activity_id")
    if not isinstance(crm_activity_id, str) or not crm_activity_id:
        return False

    def _work(tx: ManagedTransaction) -> bool:
        row = tx.run(
            queries.LINK_CONVERSATION_TO_CRM_HISTORY,
            conversation_source_record_pk=source_record_pk,
            parent_source_system=parent_ref.parent_source_system,
            parent_source_record_id=parent_ref.parent_source_record_id,
            crm_activity_id=crm_activity_id,
        ).single()
        return row is not None

    with client.session() as session:
        return session.execute_write(_work)
