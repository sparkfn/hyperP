"""Apply bounded WhatsAdmin chat work inside the fenced commit transaction.

The writer performs deterministic domain writes only. Every source read and
every LLM call happened before this transaction, so a graph failure can be
retried without repeating either.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from neo4j import ManagedTransaction
from pydantic.types import JsonValue

from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedUnit,
    RecordDisposition,
    RetryObligation,
    RetryResolution,
    UnitApplyResult,
)
from src.connectors.whatsadmin_api.bounded_state import (
    WhatsAdminBoundedState,
    WhatsAdminCursor,
    bounded_graph_client,
)
from src.connectors.whatsadmin_api.credentials import WHATSADMIN_ENTITIES, WhatsAdminEntity
from src.connectors.whatsadmin_api.watermark import (
    committed_version_key,
    session_watermark_key,
)
from src.graph.incremental_checkpoints import Neo4jCheckpointRedis
from src.models import IngestResult, SourceRecordEnvelope
from src.pipeline import IngestPipeline

logger = logging.getLogger(__name__)

SOURCE_KEY = "whatsapp_chat"


class WhatsAdminBoundedWriter:
    """Commit prepared envelopes, chat versions, and terminal watermarks."""

    def __init__(
        self,
        *,
        state_factory: Callable[[AttemptContext], WhatsAdminBoundedState] | None = None,
        pipeline_factory: Callable[[str], IngestPipeline] | None = None,
    ) -> None:
        """Bind the writer to lazily resolved state and pipeline seams.

        Both seams are resolved per commit, so module import never opens a graph
        connection and a caller can substitute its own store.
        """
        self._state_factory = state_factory or _default_state_factory
        self._pipeline_factory = pipeline_factory or _default_pipeline_factory

    def apply(
        self,
        tx: ManagedTransaction,
        context: AttemptContext,
        unit: BoundedUnit,
    ) -> UnitApplyResult:
        """Apply one unit's deterministic writes; never call a source or LLM."""
        before = WhatsAdminCursor.from_payload(unit.unit.checkpoint_before.cursor)
        result = self._apply_unit(tx, context, unit, before)
        if unit.terminal:
            return self._terminal_watermarks(tx, context, unit, result)
        return result

    def _apply_unit(
        self,
        tx: ManagedTransaction,
        context: AttemptContext,
        unit: BoundedUnit,
        before: WhatsAdminCursor,
    ) -> UnitApplyResult:
        if before.subphase == "extract":
            if unit.unit.records:
                return self._retry(unit, before)
            return self._resolved(before)
        if before.subphase != "commit":
            return UnitApplyResult(dispositions=())
        return self._commit(tx, context, unit, before)

    # Extraction outcomes --------------------------------------------------

    @staticmethod
    def _retry(unit: BoundedUnit, before: WhatsAdminCursor) -> UnitApplyResult:
        """Persist the durable retry obligation of an exhausted extraction."""
        marker = unit.unit.records[0]
        replay_id = _text(marker, "replay_id")
        source_record_id = _text(marker, "source_record_id")
        source_version = _text(marker, "source_version")
        category = _text(marker, "category")
        attempt_count = _positive_attempt_count(marker.get("attempt_count"))
        if replay_id != unit.replay_id:
            raise RuntimeError("bounded retry marker does not match its unit")
        if before.retry_replay_id is not None and replay_id != before.retry_replay_id:
            raise RuntimeError("bounded retry marker does not match its checkpoint")
        obligation = RetryObligation(
            replay_id=replay_id,
            source_record_id=source_record_id,
            source_version=source_version,
            category=category,
            attempt_count=attempt_count,
            eligible_at=None,
        )
        return UnitApplyResult(
            dispositions=("durable_retry",),
            retry_obligations=(obligation,),
        )

    @staticmethod
    def _resolved(before: WhatsAdminCursor) -> UnitApplyResult:
        """Resolve the logical-run obligation a successful extraction satisfied."""
        if before.retry_replay_id is None or before.retry_source_record_id is None:
            return UnitApplyResult(dispositions=())
        return UnitApplyResult(
            dispositions=(),
            resolved_retries=(
                RetryResolution(
                    replay_id=before.retry_replay_id,
                    source_record_id=before.retry_source_record_id,
                ),
            ),
        )

    # Graph commit ---------------------------------------------------------

    def _commit(
        self,
        tx: ManagedTransaction,
        context: AttemptContext,
        unit: BoundedUnit,
        before: WhatsAdminCursor,
    ) -> UnitApplyResult:
        session = before.current_session()
        if session is None:
            raise RuntimeError("bounded commit unit has no current session")
        entity_key = _entity_key(context, session.session_id)
        pipeline = self._pipeline_factory(context.scope.control_instance_id)
        dispositions: list[RecordDisposition] = []
        for record in unit.unit.records:
            if record.get("source_system", SOURCE_KEY) != SOURCE_KEY:
                raise RuntimeError("bounded envelope declares a foreign source system")
            envelope = SourceRecordEnvelope.model_validate(
                {"source_system": SOURCE_KEY, **record},
            )
            # The bounded path has no IngestRun row: its provenance is the
            # bounded receipt and checkpoint, not a run-to-record link.
            result = pipeline.ingest_in_transaction(tx, envelope, ingest_run_id=None)
            dispositions.append(_disposition(result, envelope.source_record_id))
        if before.source_version is not None and before.chat_id is not None:
            view = self._state_factory(context).bind_transaction(
                tx,
                terminal_authorized=unit.terminal,
            )
            view.set(
                committed_version_key(entity_key, session.session_id, before.chat_id),
                before.source_version,
            )
        return UnitApplyResult(
            dispositions=tuple(dispositions),
            resolved_retries=self._resolved(before).resolved_retries,
        )

    def _terminal_watermarks(
        self,
        tx: ManagedTransaction,
        context: AttemptContext,
        unit: BoundedUnit,
        result: UnitApplyResult,
    ) -> UnitApplyResult:
        """Publish completed per-session watermarks for a terminal unit.

        Watermarks are published only after the whole walk finished and no retry
        obligation remains, which is exactly when the runner may finalize.
        """
        if result.retry_obligations:
            return result
        after = WhatsAdminCursor.from_payload(unit.unit.checkpoint_after.cursor)
        entity_key = _entity_key(context, None)
        view = self._state_factory(context).bind_transaction(tx, terminal_authorized=True)
        for session_id in after.completed_sessions:
            view.set(
                session_watermark_key(entity_key, session_id),
                after.upper_bound,
                status="completed",
            )
        logger.info(
            "Bounded WhatsAdmin window published entity=%s window=%s sessions=%d",
            entity_key,
            after.window_id,
            len(after.completed_sessions),
        )
        return result


def _default_state_factory(context: AttemptContext) -> WhatsAdminBoundedState:
    """Build this run's generation-scoped durable state (no I/O yet)."""
    scope = context.scope
    return WhatsAdminBoundedState(
        Neo4jCheckpointRedis(
            bounded_graph_client(),
            scope.source_key,
            control_instance_id=scope.control_instance_id,
            reset_generation=scope.reset_generation,
        )
    )


def _default_pipeline_factory(control_instance_id: str) -> IngestPipeline:
    return IngestPipeline(bounded_graph_client(), control_instance_id=control_instance_id)


def _disposition(result: IngestResult, source_record_id: str) -> RecordDisposition:
    """Map one pipeline result onto the durable disposition it earned."""
    if result.errors:
        raise RuntimeError(f"bounded chat envelope failed to ingest {source_record_id}")
    if result.skipped_duplicate:
        return "duplicate"
    if result.dropped:
        return "policy_dropped"
    return "committed"


def _entity_key(context: AttemptContext, session_id: str | None) -> WhatsAdminEntity:
    value = context.scope.entity_key
    if value not in WHATSADMIN_ENTITIES:
        detail = f" for session {session_id}" if session_id is not None else ""
        raise RuntimeError(f"bounded WhatsAdmin scope has no entity{detail}")
    entity: WhatsAdminEntity = value
    return entity


def _positive_attempt_count(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError("bounded retry marker requires a positive attempt count")
    return value


def _text(record: Mapping[str, JsonValue], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"bounded retry marker requires {key}")
    return value
