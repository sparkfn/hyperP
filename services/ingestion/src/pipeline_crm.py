"""Persistence for immutable CRM activity and universal call source records."""

from __future__ import annotations

import json

from neo4j import ManagedTransaction, Record

from src.bitrix_backfill_models import CoverageDisposition
from src.bitrix_backfill_runtime import record_terminal_unit
from src.bitrix_ingestion_models import ExecutionContext, FenceContext
from src.crm_history_contract import generic_activity_properties
from src.graph import queries
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import assert_active_bitrix_fence
from src.graph.queries.bitrix_backfill import (
    RECORD_BITRIX_ACTIVITY_OWNER_RETRY,
    RESOLVE_BITRIX_ACTIVITY_OWNER_RETRY,
)
from src.graph.queries.bitrix_deal_scope import GET_CURRENT_DEAL_SCOPE_BATCH
from src.models import IngestResult, RecordType, SourceRecordEnvelope
from src.record_lifecycle import load_locked_source_state
from src.source_version_keys import encode_source_version_key

_LEGACY_CRM_ACTIVITY_FAMILY = "crm_activity"
_BITRIX_ACTIVITY_HISTORY_SOURCE = "bitrix_crm_activity"
_LEGACY_BITRIX_ACTIVITY_PROJECTION_SOURCE = "bitrix_crm_activity_v1"


class UnresolvedActivityOwnerError(RuntimeError):
    """Owner scope must be reviewed before this activity cursor can advance."""


def ingest_crm_history_record(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    *,
    ingest_run_id: str,
    fence_context: FenceContext | None = None,
    execution_context: ExecutionContext | None = None,
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
    active_fence = (
        execution_context.fence_context if execution_context is not None else fence_context
    )

    def _work(tx: ManagedTransaction) -> IngestResult:
        if active_fence is not None:
            assert_active_bitrix_fence(tx, active_fence)
        owner_scope = _owner_scope_or_retry(tx, envelope, execution_context)
        if owner_scope is None:
            result = IngestResult(
                source_record_id=envelope.source_record_id,
                dropped=True,
                ingest_run_id=ingest_run_id,
            )
            _record_activity_unit(
                tx,
                execution_context,
                envelope,
                result,
                disposition="quarantined_owner_unresolved",
                scope_state="indeterminate",
            )
            return result
        if owner_scope == "out_of_scope":
            result = IngestResult(
                source_record_id=envelope.source_record_id,
                dropped=True,
                ingest_run_id=ingest_run_id,
            )
            _record_activity_unit(
                tx,
                execution_context,
                envelope,
                result,
                disposition="excluded_out_of_scope",
                scope_state=owner_scope,
            )
            return result
        load_locked_source_state(tx, envelope.source_system, envelope.source_record_id)
        existing = tx.run(
            queries.FIND_ANY_SOURCE_RECORD,
            source_system=envelope.source_system,
            source_record_id=envelope.source_record_id,
        ).single()
        if existing is not None:
            _validate_existing_hash(existing, envelope.record_hash)
            updated = _rematerialize_activity_projection(tx, existing, envelope)
            result = IngestResult(
                source_record_id=envelope.source_record_id,
                source_record_pk=str(existing["source_record_pk"]),
                skipped_duplicate=True,
                ingest_run_id=ingest_run_id,
            )
            _record_activity_unit(
                tx,
                execution_context,
                envelope,
                result,
                disposition="updated_projection" if updated else "existing_same_hash",
                scope_state=owner_scope,
            )
            return result
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
            result = IngestResult(
                source_record_id=envelope.source_record_id,
                dropped=True,
                ingest_run_id=ingest_run_id,
            )
            _record_activity_unit(
                tx,
                execution_context,
                envelope,
                result,
                disposition="conflict",
                scope_state=owner_scope,
            )
            return result
        source_record_pk = str(created["source_record_pk"])
        if ingest_run_id:
            tx.run(
                queries.LINK_SOURCE_RECORD_TO_RUN,
                source_record_pk=source_record_pk,
                ingest_run_id=ingest_run_id,
            )
        result = IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=source_record_pk,
            ingest_run_id=ingest_run_id,
        )
        _link_history_to_conversations_in_transaction(tx, envelope, source_record_pk)
        _record_activity_unit(
            tx,
            execution_context,
            envelope,
            result,
            disposition="created",
            scope_state=owner_scope,
        )
        return result

    with client.session() as session:
        return session.execute_write(_work)


def ingest_call_record(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    *,
    ingest_run_id: str,
    fence_context: FenceContext | None = None,
    execution_context: ExecutionContext | None = None,
) -> IngestResult:
    """Create a call only when its immutable history parent has person context."""
    if envelope.record_type != RecordType.CALL or envelope.parent_ref is None:
        raise ValueError("call ingestion requires a call envelope with parent_ref")
    parent_ref = envelope.parent_ref
    crm_activity_id = envelope.raw_payload.get("crm_activity_id")
    if not isinstance(crm_activity_id, str) or not crm_activity_id:
        raise ValueError("call source records require raw_payload.crm_activity_id")
    active_fence = (
        execution_context.fence_context if execution_context is not None else fence_context
    )

    def _work(tx: ManagedTransaction) -> IngestResult:
        if active_fence is not None:
            assert_active_bitrix_fence(tx, active_fence)
        owner_scope = _owner_scope_or_retry(tx, envelope, execution_context)
        if owner_scope is None:
            result = IngestResult(
                source_record_id=envelope.source_record_id,
                dropped=True,
                ingest_run_id=ingest_run_id,
            )
            _record_activity_unit(
                tx,
                execution_context,
                envelope,
                result,
                disposition="quarantined_owner_unresolved",
                scope_state="indeterminate",
            )
            return result
        if owner_scope == "out_of_scope":
            result = IngestResult(
                source_record_id=envelope.source_record_id,
                dropped=True,
                ingest_run_id=ingest_run_id,
            )
            _record_activity_unit(
                tx,
                execution_context,
                envelope,
                result,
                disposition="excluded_out_of_scope",
                scope_state=owner_scope,
            )
            return result
        load_locked_source_state(tx, envelope.source_system, envelope.source_record_id)
        existing = tx.run(
            queries.FIND_ANY_SOURCE_RECORD,
            source_system=envelope.source_system,
            source_record_id=envelope.source_record_id,
        ).single()
        if existing is not None:
            _validate_existing_hash(existing, envelope.record_hash)
            result = IngestResult(
                source_record_id=envelope.source_record_id,
                source_record_pk=str(existing["source_record_pk"]),
                skipped_duplicate=True,
                ingest_run_id=ingest_run_id,
            )
            _record_activity_unit(
                tx,
                execution_context,
                envelope,
                result,
                disposition="existing_same_hash",
                scope_state=owner_scope,
            )
            return result
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
            result = IngestResult(
                source_record_id=envelope.source_record_id,
                dropped=True,
                ingest_run_id=ingest_run_id,
            )
            _record_activity_unit(
                tx,
                execution_context,
                envelope,
                result,
                disposition="conflict",
                scope_state=owner_scope,
            )
            return result
        source_record_pk = str(created["source_record_pk"])
        person_id = str(created["person_id"])
        if ingest_run_id:
            tx.run(
                queries.LINK_SOURCE_RECORD_TO_RUN,
                source_record_pk=source_record_pk,
                ingest_run_id=ingest_run_id,
            )
        result = IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=source_record_pk,
            person_id=person_id,
            ingest_run_id=ingest_run_id,
        )
        _record_activity_unit(
            tx,
            execution_context,
            envelope,
            result,
            disposition="created",
            scope_state=owner_scope,
        )
        return result

    with client.session() as session:
        return session.execute_write(_work)


def _validate_existing_hash(existing: Record, incoming_hash: str) -> None:
    existing_hash: object = existing["record_hash"]
    if not isinstance(existing_hash, str) or existing_hash != incoming_hash:
        raise ValueError("CRM immutable source ID was observed with a different record hash")


def _require_activity_owner_scope(
    tx: ManagedTransaction,
    envelope: SourceRecordEnvelope,
) -> str:
    deal_id = _activity_owner_deal_id(envelope)
    record = tx.run(
        GET_CURRENT_DEAL_SCOPE_BATCH,
        source_key="bitrix_chat",
        deal_ids=[deal_id],
    ).single()
    if record is None or record["scope_state"] is None:
        raise UnresolvedActivityOwnerError(f"activity owner {deal_id} is missing from scope")
    state: object = record["scope_state"]
    if state == "out_of_scope":
        return "out_of_scope"
    if state in {"indeterminate", "missing"}:
        raise UnresolvedActivityOwnerError(f"activity owner {deal_id} requires review")
    if state != "in_scope":
        raise RuntimeError("activity owner scope returned an invalid state")
    return "in_scope"


def _owner_scope_or_retry(
    tx: ManagedTransaction,
    envelope: SourceRecordEnvelope,
    context: ExecutionContext | None,
) -> str | None:
    if context is None:
        return "in_scope"
    try:
        return _require_activity_owner_scope(tx, envelope)
    except UnresolvedActivityOwnerError as exc:
        generation = context.generation_context
        if generation is None:
            raise
        owner_deal_id = _activity_owner_deal_id(envelope)
        record = tx.run(
            RECORD_BITRIX_ACTIVITY_OWNER_RETRY,
            generation_id=generation.generation_id,
            logical_run_id=context.fence_context.logical_run_id,
            ingest_run_id=context.fence_context.ingest_run_id,
            attempt_generation=context.fence_context.attempt_generation,
            stream_generation=context.fence_context.stream_generation,
            fencing_token=context.fence_context.fencing_token,
            source_identity=envelope.source_record_id,
            source_boundary=f"{generation.boundary_digest}:{context.checkpoint.phase}",
            owner_deal_id=owner_deal_id,
            owner_state="missing_or_indeterminate",
        ).single()
        if record is None:
            raise RuntimeError("activity owner retry evidence was not persisted") from exc
        return _reviewed_owner_scope(record["status"])


def _reviewed_owner_scope(status: object) -> str | None:
    if status == "reviewed_excluded":
        return "out_of_scope"
    if status != "retryable":
        raise RuntimeError("activity owner retry returned an invalid review status")
    return None


def _activity_owner_deal_id(envelope: SourceRecordEnvelope) -> str:
    if envelope.record_type == RecordType.CALL:
        raw_owner_id = envelope.raw_payload.get("owner_id")
        if not isinstance(raw_owner_id, str) or not raw_owner_id.strip():
            raise ValueError("Bitrix call requires raw_payload.owner_id")
        return raw_owner_id.strip()
    if envelope.parent_ref is None:
        raise ValueError("Bitrix activity requires a parent deal")
    return envelope.parent_ref.parent_source_record_id.rsplit("-", maxsplit=1)[-1]


def _record_activity_unit(
    tx: ManagedTransaction,
    context: ExecutionContext | None,
    envelope: SourceRecordEnvelope,
    result: IngestResult,
    *,
    disposition: CoverageDisposition,
    scope_state: str,
) -> None:
    if context is None:
        return
    generation = context.generation_context
    if generation is not None:
        tx.run(
            RESOLVE_BITRIX_ACTIVITY_OWNER_RETRY,
            generation_id=generation.generation_id,
            source_identity=envelope.source_record_id,
            source_boundary=f"{generation.boundary_digest}:{context.checkpoint.phase}",
            resolution=(
                "quarantined_owner_unresolved"
                if disposition == "quarantined_owner_unresolved"
                else "reviewed_excluded"
                if scope_state == "out_of_scope"
                else "resolved_in_scope"
            ),
        ).consume()
    if (
        envelope.record_type == RecordType.CRM_HISTORY
        and envelope.raw_payload.get("has_call_record") is True
    ):
        # The companion call is the terminal unit for this source activity.
        # If the worker dies between the two writes, the unchanged activity
        # cursor causes the history to replay idempotently before the call.
        return
    resolved = disposition
    if scope_state == "out_of_scope":
        resolved = "excluded_out_of_scope"
        if not result.dropped:
            raise RuntimeError("out-of-scope activity attempted to persist")
    record_terminal_unit(
        tx,
        context=context,
        envelope=envelope,
        result=result,
        disposition=resolved,
        scope_state=scope_state,
    )


def _link_history_to_conversations_in_transaction(
    tx: ManagedTransaction,
    envelope: SourceRecordEnvelope,
    history_source_record_pk: str,
) -> None:
    chat_id = envelope.raw_payload.get("bitrix_chat_id_numeric")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return
    tx.run(
        queries.LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS,
        history_source_record_pk=history_source_record_pk,
        source_system=envelope.source_system,
        bitrix_chat_id_numeric=chat_id,
    ).consume()


def _rematerialize_activity_projection(
    tx: ManagedTransaction,
    existing: Record,
    envelope: SourceRecordEnvelope,
) -> bool:
    version = envelope.projection_version
    if version is None:
        return False
    existing_version = existing["projection_version"]
    legacy_bitrix_alias = (
        existing["history_family"] == _LEGACY_CRM_ACTIVITY_FAMILY
        and existing["history_source"] == _BITRIX_ACTIVITY_HISTORY_SOURCE
        and existing["projection_source"] == _LEGACY_BITRIX_ACTIVITY_PROJECTION_SOURCE
    )
    if existing_version == version and not legacy_bitrix_alias:
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
        return False
    if isinstance(existing_version, int) and existing_version > version:
        return False
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
    return True


def link_conversation_to_crm_history(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    source_record_pk: str,
    *,
    fence_context: FenceContext | None = None,
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
        if fence_context is not None:
            assert_active_bitrix_fence(tx, fence_context)
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
    *,
    fence_context: FenceContext | None = None,
) -> bool:
    """Link a CRM activity to every current conversation for its Bitrix chat."""
    chat_id = envelope.raw_payload.get("bitrix_chat_id_numeric")
    activity_id = envelope.raw_payload.get("crm_activity_id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return False
    if not isinstance(activity_id, str) or not activity_id:
        return False

    def _work(tx: ManagedTransaction) -> bool:
        if fence_context is not None:
            assert_active_bitrix_fence(tx, fence_context)
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
