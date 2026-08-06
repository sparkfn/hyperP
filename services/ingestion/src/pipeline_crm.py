"""Persistence for immutable CRM activity and universal call source records."""

from __future__ import annotations

import json

from neo4j import ManagedTransaction, Record

from src.crm_history_contract import generic_activity_properties
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
    history = generic_activity_properties(envelope)
    history_family = envelope.history_family or history.history_family.value
    history_kind = envelope.history_kind or history.history_kind
    history_source = envelope.history_source or history.history_source
    event_at = envelope.event_at or history.event_at
    projection_version = envelope.projection_version or 1
    projection_source = envelope.projection_source or history.history_projection_source

    def _work(tx: ManagedTransaction) -> IngestResult:
        load_locked_source_state(tx, envelope.source_system, envelope.source_record_id)
        existing = tx.run(
            queries.FIND_ANY_SOURCE_RECORD,
            source_system=envelope.source_system,
            source_record_id=envelope.source_record_id,
        ).single()
        if existing is not None:
            _validate_existing_hash(existing, envelope.record_hash)
            _rematerialize_activity_projection(tx, existing, envelope)
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
            history_family=history_family,
            history_kind=history_kind,
            history_source=history_source,
            event_category_id=history.event_category_id,
            event_stage_id=history.event_stage_id,
            event_stage_semantic_id=history.event_stage_semantic_id,
            event_at=event_at,
            projection_version=projection_version,
            projection_source=projection_source,
            history_projection_version=history.history_projection_version,
            history_projection_source=history.history_projection_source,
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
            _validate_existing_hash(existing, envelope.record_hash)
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


def _validate_existing_hash(existing: Record, incoming_hash: str) -> None:
    existing_hash: object = existing["record_hash"]
    if not isinstance(existing_hash, str) or existing_hash != incoming_hash:
        raise ValueError("CRM immutable source ID was observed with a different record hash")


def _rematerialize_activity_projection(
    tx: ManagedTransaction,
    existing: Record,
    envelope: SourceRecordEnvelope,
) -> None:
    version = envelope.projection_version
    if version is None:
        return
    existing_version = existing["projection_version"]
    if existing_version == version:
        expected = (
            envelope.history_family,
            envelope.history_kind,
            envelope.history_source,
            envelope.event_at,
            envelope.projection_source,
        )
        actual = (
            existing["history_family"],
            existing["history_kind"],
            existing["history_source"],
            existing["event_at"],
            existing["projection_source"],
        )
        if actual != expected:
            raise ValueError("CRM activity projection conflicts with its materialized version")
        return
    if isinstance(existing_version, int) and existing_version > version:
        return
    tx.run(
        queries.REMATERIALIZE_CRM_HISTORY_PROJECTION,
        source_record_pk=existing["source_record_pk"],
        record_hash=envelope.record_hash,
        history_family=envelope.history_family,
        history_kind=envelope.history_kind,
        history_source=envelope.history_source,
        event_at=envelope.event_at,
        projection_version=version,
        projection_source=envelope.projection_source,
    ).consume()


def link_conversation_to_crm_history(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    source_record_pk: str,
) -> bool:
    """Link a persisted Open Lines conversation to all matching CRM activities."""
    raw_activity_ids = envelope.raw_payload.get("crm_activity_ids")
    if not isinstance(raw_activity_ids, list):
        return False
    activity_ids = list(
        dict.fromkeys(
            activity_id
            for activity_id in raw_activity_ids
            if isinstance(activity_id, str) and activity_id
        )
    )
    if not activity_ids:
        return False

    def _work(tx: ManagedTransaction) -> bool:
        row = tx.run(
            queries.LINK_CONVERSATION_TO_CRM_HISTORY,
            conversation_source_record_pk=source_record_pk,
            source_system=envelope.source_system,
            crm_activity_ids=activity_ids,
        ).single()
        return row is not None and int(row["linked_history_count"]) > 0

    with client.session() as session:
        return session.execute_write(_work)


def link_crm_history_to_existing_conversations(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    history_source_record_pk: str,
) -> bool:
    """Link a CRM activity to every current conversation for its Bitrix chat."""
    chat_id = envelope.raw_payload.get("bitrix_chat_id_numeric")
    activity_id = envelope.raw_payload.get("crm_activity_id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return False
    if not isinstance(activity_id, str) or not activity_id:
        return False

    def _work(tx: ManagedTransaction) -> bool:
        row = tx.run(
            queries.LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS,
            history_source_record_pk=history_source_record_pk,
            source_system=envelope.source_system,
            bitrix_chat_id=chat_id,
            crm_activity_id=activity_id,
        ).single()
        return row is not None and int(row["linked_conversation_count"]) > 0

    with client.session() as session:
        return session.execute_write(_work)
