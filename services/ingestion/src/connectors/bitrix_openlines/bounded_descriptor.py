"""Auto-discovered bounded adapter for the Bitrix chat source (issue #432).

The adapter reads only the transports frozen into the run's source window: the
capability negotiation and the ordered changed-deal feed. Every CRM activity
path is refused before source I/O, so a bounded run can never reintroduce the
retired activity family.

Unit shape: one bounded unit performs at most one approved source call. A
capability unit performs the negotiation; each subsequent ``crm_deals`` unit
performs exactly one changed-deal page and is terminal once the feed's cursor is
exhausted.

Assumed proxy payload contract (the frozen contract leaves ``payload`` opaque,
so the adapter pins the shape it consumes):

* a deal upsert payload is the upstream ``crm.deal.get`` result, keyed by the
  Bitrix field names (``ID``, ``TITLE``, ``CATEGORY_ID``, ``STAGE_ID``,
  ``DATE_MODIFY``/``DATE_CREATE``, ``CONTACT_ID``) plus a ``CONTACTS`` list of
  ``{ID, FULL_NAME, PHONES, EMAILS}`` mappings. Decoding it into the shared
  ``CrmDeal`` model lets the adapter reuse ``build_crm_deal_envelope`` verbatim,
  so source-record IDs, the deal identity policy, and the category-to-entity
  mapping are preserved unchanged.

The ``openlines_conversations`` and ``crm_stage_history`` streams pass the
capability gate and then refuse their work substage: the frozen contract exposes
no changed-message or stage-artifact transport, so the adapter will not invent
one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from neo4j import ManagedTransaction
from pydantic.types import JsonValue

from src.bitrix_ingestion_models import ExecutionContext, FenceContext
from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedUnit,
    OccurrenceContext,
    RunScope,
    UnitApplyResult,
    Usage,
    utc_now,
)
from src.config import get_settings
from src.connectors.bitrix_openlines.bounded_checkpoint import (
    BITRIX_BOUNDED_CONNECTOR_VERSION,
    BITRIX_BOUNDED_SCHEMA_VERSION,
    advance_checkpoint,
    initial_checkpoint,
    validate_checkpoint,
)
from src.connectors.bitrix_openlines.bounded_client import BitrixBoundedClient
from src.connectors.bitrix_openlines.bounded_contract import (
    BitrixBoundedCapabilityError,
    BitrixBoundedContractError,
    BitrixBoundedStream,
    DealChange,
    validate_capability_snapshot,
)
from src.connectors.bitrix_openlines.connector import (
    _validate_included_crm_category_mappings,
    build_crm_deal_envelope,
)
from src.connectors.bitrix_openlines.models import CrmContact, CrmDeal
from src.graph.client import Neo4jClient
from src.ingestion_config import BitrixOpenLinesConfig, get_ingestion_config
from src.models import SourceRecordEnvelope
from src.pipeline import IngestPipeline
from src.resumable import (
    CheckpointCompatibility,
    CheckpointDescriptor,
    IngestionUnit,
    RecordDisposition,
)
from src.retirement import retire_source_evidence_in_transaction

SOURCE_KEY = "bitrix_chat"
CONFIGURATION_VERSION = "bitrix-bounded-config-v1"
BOUNDED_BASE_URL_ENV = "HYPERP_BITRIX_BOUNDED_BASE_URL"
_BOUNDED_TIMEOUT_SECONDS = 30.0
MAX_RECORDS_PER_UNIT = 50
MAX_SOURCE_REQUESTS_PER_UNIT = 1
MAX_BYTES_PER_UNIT = 4 * 1024 * 1024
MAX_EXTRACTION_CALLS_PER_UNIT = 1
MAX_CLOSE_SECONDS = 5.0
MAX_RETRY_BACKOFF_SECONDS = 600.0

BoundedOpKind = Literal["upsert", "retire"]
_OP_KEY = "bounded_op"
_APPROVED_WORK_STREAMS: frozenset[BitrixBoundedStream] = frozenset({"crm_deals"})
_WORK_SUBSTAGE: dict[BitrixBoundedStream, str] = {
    "crm_deals": "deals",
    "openlines_conversations": "conversation_discovery",
    "crm_stage_history": "apply",
}


@dataclass(frozen=True, slots=True)
class _State:
    """A validated checkpoint's frozen capability boundary and current position."""

    stream_key: BitrixBoundedStream
    substage: str
    source_scope: str
    snapshot_id: str
    page_cursor: str | None


def _state(checkpoint: CheckpointDescriptor) -> _State:
    """Validate a bounded checkpoint once and expose its boundary and position."""
    stream_key = validate_checkpoint(checkpoint)
    substage = checkpoint.cursor["substage"]
    if not isinstance(substage, str):
        raise BitrixBoundedContractError("Bitrix bounded substage is invalid")
    page_cursor = checkpoint.cursor.get("page_cursor")
    if page_cursor is not None and not isinstance(page_cursor, str):
        raise BitrixBoundedContractError("Bitrix bounded page cursor is invalid")
    return _State(
        stream_key=stream_key,
        substage=substage,
        source_scope=cast(str, checkpoint.source_window["source_scope"]),
        snapshot_id=cast(str, checkpoint.source_window["snapshot_id"]),
        page_cursor=page_cursor,
    )


def _required_timestamp(value: str | None, name: str) -> str:
    """Narrow a decoded retirement timestamp that must be present."""
    if value is None:
        raise BitrixBoundedContractError(f"Bitrix bounded retirement lost its {name}")
    return value


def _require_scope_stream(state: _State, context: AttemptContext) -> None:
    """Require the checkpoint's stream to match the run scope's selected stream."""
    expected = context.scope.stream_key
    if expected is not None and state.stream_key != expected:
        raise BitrixBoundedContractError(
            "Bitrix bounded checkpoint stream does not match the run scope"
        )


def _replay_id(state: _State) -> str:
    """Derive a stable replay identity so a replayed unit keeps its identity."""
    return "|".join((state.stream_key, state.substage, state.page_cursor or "start"))


def _unit(
    checkpoint: CheckpointDescriptor,
    state: _State,
    after: CheckpointDescriptor,
    records: tuple[dict[str, JsonValue], ...],
    client: BitrixBoundedClient,
    *,
    terminal: bool,
) -> BoundedUnit:
    """Assemble one bounded unit whose usage reconciles with its record page."""
    return BoundedUnit(
        unit=IngestionUnit(
            checkpoint_before=checkpoint,
            checkpoint_after=after,
            records=records,
        ),
        replay_id=_replay_id(state),
        usage=Usage(
            records=len(records),
            source_requests=client.request_count,
            pages=1,
            bytes_read=client.last_response_bytes,
        ),
        terminal=terminal,
    )


def _upsert_record(envelope: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {_OP_KEY: "upsert", "envelope": envelope}


def _retire_record(
    source_record_id: str,
    *,
    retired_at: str,
) -> dict[str, JsonValue]:
    return {
        _OP_KEY: "retire",
        "source_record_id": source_record_id,
        "retired_at": retired_at,
        "reconciliation_snapshot_at": retired_at,
    }


def _deal_source_record_id(deal_id: int) -> str:
    return f"bitrix-crm-deal-{deal_id}"


def _deal_op(
    change: DealChange,
    *,
    config: BitrixOpenLinesConfig,
    included_categories: frozenset[str],
    retired_at: str,
) -> dict[str, JsonValue] | None:
    """Decode one changed deal into an upsert or retirement operation.

    Returns ``None`` for an upsert outside the configured category filter, which
    preserves the excluded-category skip the legacy deal path applied.
    """
    if change.kind == "tombstone":
        return _retire_record(
            _deal_source_record_id(change.order.deal_id),
            retired_at=retired_at,
        )
    if change.category_id is None:
        raise BitrixBoundedContractError("Bitrix bounded deal upsert omitted its category")
    if change.category_id not in included_categories:
        return None
    deal = _crm_deal_from_change(change)
    if deal.category_id != change.category_id:
        raise BitrixBoundedContractError("Bitrix bounded deal changed category during hydration")
    entity_key = config.entity_by_crm_category_id.get(change.category_id)
    if entity_key is None:
        raise BitrixBoundedContractError(
            f"Bitrix CRM deal {deal.id} category {change.category_id!r} has no entity mapping"
        )
    return _upsert_record(
        build_crm_deal_envelope(
            deal,
            entity_key,
            source_instance_id=config.source_instance_id,
        )
    )


def _crm_deal_from_change(change: DealChange) -> CrmDeal:
    if change.payload is None:
        raise BitrixBoundedContractError("Bitrix bounded deal upsert omitted its pinned payload")
    return _crm_deal_from_payload(change.order.deal_id, change.payload)


def _crm_deal_from_payload(deal_id: int, payload: dict[str, JsonValue]) -> CrmDeal:
    """Decode an upstream deal payload into the shared immutable CRM model."""
    raw_id = _positive_id(payload.get("ID"), "deal ID")
    if raw_id != str(deal_id):
        raise BitrixBoundedContractError("Bitrix bounded deal payload has a mismatched ID")
    contacts = _crm_contacts(payload.get("CONTACTS"))
    explicit_contact_id = _optional_id(payload.get("CONTACT_ID"))
    primary = next((item for item in contacts if item.id == explicit_contact_id), None)
    if primary is None and explicit_contact_id is None and len(contacts) == 1:
        primary = contacts[0]
    return CrmDeal(
        id=raw_id,
        title=_optional_text(payload.get("TITLE")) or "",
        category_id=_optional_text(payload.get("CATEGORY_ID")),
        stage_id=_optional_text(payload.get("STAGE_ID")),
        observed_at=_first_datetime(payload, "DATE_MODIFY", "DATE_CREATE"),
        primary_contact=primary,
        contacts=contacts,
        contact_count=len(contacts),
        has_ambiguous_contacts=len(contacts) > 1 and explicit_contact_id is None,
        raw_payload=payload,
    )


def _crm_contacts(value: JsonValue) -> tuple[CrmContact, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise BitrixBoundedContractError("Bitrix bounded deal contacts must be a list")
    return tuple(_crm_contact(item) for item in value)


def _crm_contact(value: JsonValue) -> CrmContact:
    if not isinstance(value, dict):
        raise BitrixBoundedContractError("Bitrix bounded deal contact must be an object")
    return CrmContact(
        id=_positive_id(value.get("ID"), "contact ID"),
        full_name=_optional_text(value.get("FULL_NAME")),
        phones=_text_tuple(value.get("PHONES"), "contact phones"),
        emails=_text_tuple(value.get("EMAILS"), "contact emails"),
    )


def _text_tuple(value: JsonValue, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise BitrixBoundedContractError(f"Bitrix bounded {name} must be a list")
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise BitrixBoundedContractError(f"Bitrix bounded {name} has an invalid entry")
        parsed.append(item)
    return tuple(parsed)


def _positive_id(value: JsonValue, name: str) -> str:
    if isinstance(value, bool):
        raise BitrixBoundedContractError(f"Bitrix bounded {name} is invalid")
    if isinstance(value, int) and value > 0:
        return str(value)
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return value
    raise BitrixBoundedContractError(f"Bitrix bounded {name} is invalid")


def _optional_id(value: JsonValue) -> str | None:
    if value is None:
        return None
    return _positive_id(value, "contact ID")


def _optional_text(value: JsonValue) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BitrixBoundedContractError("Bitrix bounded deal text field is invalid")
    return value or None


def _first_datetime(payload: dict[str, JsonValue], *keys: str) -> datetime | None:
    for key in keys:
        raw = payload.get(key)
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise BitrixBoundedContractError(f"Bitrix bounded deal {key} is invalid")
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BitrixBoundedContractError(
                f"Bitrix bounded deal {key} is not an ISO timestamp"
            ) from exc
    return None


@dataclass(frozen=True, slots=True)
class _BoundedOp:
    """One decoded bounded operation awaiting a graph write."""

    kind: BoundedOpKind
    source_record_id: str
    envelope: dict[str, JsonValue] | None
    retired_at: str | None
    reconciliation_snapshot_at: str | None


def _op_from_record(record: dict[str, JsonValue]) -> _BoundedOp:
    kind = record.get(_OP_KEY)
    if kind == "upsert":
        envelope = record.get("envelope")
        if not isinstance(envelope, dict):
            raise BitrixBoundedContractError("Bitrix bounded upsert omitted its envelope")
        source_record_id = envelope.get("source_record_id")
        if not isinstance(source_record_id, str) or not source_record_id:
            raise BitrixBoundedContractError("Bitrix bounded upsert omitted its source record ID")
        return _BoundedOp("upsert", source_record_id, dict(envelope), None, None)
    if kind == "retire":
        source_record_id = record.get("source_record_id")
        retired_at = record.get("retired_at")
        snapshot_at = record.get("reconciliation_snapshot_at")
        if not isinstance(source_record_id, str) or not source_record_id:
            raise BitrixBoundedContractError(
                "Bitrix bounded retirement omitted its source record ID"
            )
        if not isinstance(retired_at, str) or not retired_at:
            raise BitrixBoundedContractError("Bitrix bounded retirement omitted its timestamp")
        if not isinstance(snapshot_at, str) or not snapshot_at:
            raise BitrixBoundedContractError(
                "Bitrix bounded retirement omitted its reconciliation snapshot"
            )
        return _BoundedOp("retire", source_record_id, None, retired_at, snapshot_at)
    raise BitrixBoundedContractError("Bitrix bounded operation kind is unsupported")


def _source_envelope(envelope: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Bind a decoded source-record payload to this adapter's source identity.

    ``build_crm_deal_envelope`` deliberately omits ``source_system``; the legacy
    runner supplied it from ``connector.get_source_key()``. The bounded writer
    supplies the same identity so the envelope validates unchanged.
    """
    return {**envelope, "source_system": SOURCE_KEY}


class BitrixBoundedConnector:
    """Fetch exactly one approved bounded Bitrix unit."""

    def __init__(
        self,
        *,
        client: BitrixBoundedClient,
        config: BitrixOpenLinesConfig,
    ) -> None:
        self._client = client
        self._config = config
        self._cancelled = False

    def validate_checkpoint(self, checkpoint: CheckpointDescriptor) -> CheckpointCompatibility:
        try:
            validate_checkpoint(checkpoint)
        except BitrixBoundedContractError:
            return "rejected"
        return "compatible"

    def fetch_one_unit(
        self,
        checkpoint: CheckpointDescriptor,
        context: AttemptContext,
    ) -> BoundedUnit:
        """Perform at most one approved source call for the current substage."""
        self._require_uncancelled(context)
        state = _state(checkpoint)
        _require_scope_stream(state, context)
        if state.substage == "capability":
            return self._capability_unit(checkpoint, state)
        if state.stream_key == "crm_deals":
            if state.substage != "deals":
                raise BitrixBoundedContractError("Bitrix bounded deal substage is unsupported")
            return self._deal_unit(checkpoint, state)
        raise BitrixBoundedCapabilityError(
            f"Bitrix bounded stream {state.stream_key} has no approved work transport"
        )

    def cancel(self) -> None:
        """Stop before the next source call; an in-flight request is not interruptible."""
        self._cancelled = True

    def close(self) -> None:
        self._client.close()

    def _require_uncancelled(self, context: AttemptContext) -> None:
        cancellation = context.cancellation
        if self._cancelled or (cancellation is not None and cancellation.requested()):
            raise BitrixBoundedCapabilityError("Bitrix bounded unit was cancelled")

    def _capability_unit(
        self,
        checkpoint: CheckpointDescriptor,
        state: _State,
    ) -> BoundedUnit:
        snapshot = self._client.negotiate_capability(
            source_scope=state.source_scope,
            snapshot_id=state.snapshot_id,
        )
        validate_capability_snapshot(
            snapshot,
            source_scope=state.source_scope,
            snapshot_id=state.snapshot_id,
            stream_key=state.stream_key,
        )
        after = advance_checkpoint(
            checkpoint,
            substage=_WORK_SUBSTAGE[state.stream_key],
            page_cursor=None,
        )
        return _unit(checkpoint, state, after, (), self._client, terminal=False)

    def _deal_unit(
        self,
        checkpoint: CheckpointDescriptor,
        state: _State,
    ) -> BoundedUnit:
        included = frozenset(_validate_included_crm_category_mappings(self._config))
        page = self._client.changed_deals(
            source_scope=state.source_scope,
            snapshot_id=state.snapshot_id,
            cursor=state.page_cursor,
            max_changes=MAX_RECORDS_PER_UNIT,
        )
        retired_at = utc_now().isoformat()
        records: list[dict[str, JsonValue]] = []
        for change in page.changes:
            operation = _deal_op(
                change,
                config=self._config,
                included_categories=included,
                retired_at=retired_at,
            )
            if operation is not None:
                records.append(operation)
        terminal = page.next_cursor is None
        after = advance_checkpoint(
            checkpoint,
            substage="terminal" if terminal else "deals",
            page_cursor=page.next_cursor,
        )
        return _unit(checkpoint, state, after, tuple(records), self._client, terminal=terminal)


class BitrixBoundedWriter:
    """Apply one bounded Bitrix unit through the extracted transaction hooks."""

    def __init__(self) -> None:
        self._client: Neo4jClient | None = None

    def apply(
        self,
        tx: ManagedTransaction,
        context: AttemptContext,
        unit: BoundedUnit,
    ) -> UnitApplyResult:
        """Write one decoded unit; never calls a source or an LLM."""
        fence = context.bitrix_fence_context
        if fence is None:
            raise BitrixBoundedContractError("Bitrix bounded apply requires a fence context")
        pipeline = self._pipeline(
            fence=fence,
            worker_task_id=context.worker_task_id,
            checkpoint=unit.unit.checkpoint_after,
        )
        dispositions: list[RecordDisposition] = []
        for record in unit.unit.records:
            operation = _op_from_record(record)
            if operation.kind == "upsert":
                if operation.envelope is None:
                    raise BitrixBoundedContractError("Bitrix bounded upsert lost its envelope")
                result = pipeline.ingest_in_transaction(
                    tx,
                    SourceRecordEnvelope.model_validate(
                        _source_envelope(operation.envelope),
                    ),
                )
                dispositions.append("duplicate" if result.skipped_duplicate else "committed")
                continue
            retired = retire_source_evidence_in_transaction(
                tx,
                SOURCE_KEY,
                operation.source_record_id,
                _required_timestamp(operation.retired_at, "retired_at"),
                _required_timestamp(
                    operation.reconciliation_snapshot_at,
                    "reconciliation_snapshot_at",
                ),
                fence_context=fence,
            )
            dispositions.append("committed" if retired else "excluded")
        return UnitApplyResult(tuple(dispositions))

    def _pipeline(
        self,
        *,
        fence: FenceContext,
        worker_task_id: str,
        checkpoint: CheckpointDescriptor,
    ) -> IngestPipeline:
        if self._client is None:
            self._client = Neo4jClient(get_settings())
        return IngestPipeline(
            self._client,
            execution_context=ExecutionContext(
                worker_task_id=worker_task_id,
                fence_context=fence,
                checkpoint=checkpoint,
            ),
        )


class BitrixBoundedDescriptor:
    """The single auto-discovered bounded descriptor for the Bitrix chat source."""

    source_key = SOURCE_KEY
    connector_version = BITRIX_BOUNDED_CONNECTOR_VERSION
    configuration_version = CONFIGURATION_VERSION
    checkpoint_schema_version = BITRIX_BOUNDED_SCHEMA_VERSION
    supports_bootstrap = True
    supports_delta = True
    supports_one_time = False
    max_records_per_unit = MAX_RECORDS_PER_UNIT
    max_source_requests_per_unit = MAX_SOURCE_REQUESTS_PER_UNIT
    max_bytes_per_unit = MAX_BYTES_PER_UNIT
    max_extraction_calls_per_unit = MAX_EXTRACTION_CALLS_PER_UNIT
    max_close_seconds = MAX_CLOSE_SECONDS
    max_retry_backoff_seconds = MAX_RETRY_BACKOFF_SECONDS
    supports_deadline = True
    supports_cancellation = True
    writer = BitrixBoundedWriter()

    def initial_checkpoint(
        self,
        scope: RunScope,
        occurrence: OccurrenceContext | None,
    ) -> CheckpointDescriptor:
        """Create the reserved capability checkpoint for a frozen source window."""
        return initial_checkpoint(scope, occurrence)

    def create(self, context: AttemptContext) -> BitrixBoundedConnector:
        """Build a connector bound to the frozen capability boundary.

        A stream without an approved work transport is refused here, before a
        source client is constructed.
        """
        state = _state(context.checkpoint)
        _require_scope_stream(state, context)
        if state.substage != "capability" and state.stream_key not in _APPROVED_WORK_STREAMS:
            raise BitrixBoundedCapabilityError(
                f"Bitrix bounded stream {state.stream_key} has no approved work transport"
            )
        return BitrixBoundedConnector(
            client=BitrixBoundedClient(
                base_url=_bounded_base_url(),
                timeout_seconds=_bounded_source_timeout(context),
                max_response_bytes=MAX_BYTES_PER_UNIT,
            ),
            config=get_ingestion_config().bitrix_openlines,
        )


def _bounded_source_timeout(context: AttemptContext) -> float:
    """Clamp the source timeout so one unit fits inside its operation deadline."""
    remaining = context.remaining_seconds(utc_now())
    if remaining is None:
        return _BOUNDED_TIMEOUT_SECONDS
    return max(min(_BOUNDED_TIMEOUT_SECONDS, remaining - MAX_CLOSE_SECONDS), 0.1)


def _bounded_base_url() -> str:
    base_url = os.environ.get(BOUNDED_BASE_URL_ENV, "").strip()
    if not base_url:
        raise BitrixBoundedCapabilityError(
            f"Bitrix bounded proxy endpoint is not configured ({BOUNDED_BASE_URL_ENV})"
        )
    return base_url


DESCRIPTOR = BitrixBoundedDescriptor()
