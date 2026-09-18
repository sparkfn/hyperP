"""In-memory fakes and payload builders for bounded WhatsAdmin adapter tests.

The fixtures define the required upstream contract: a requested-as-of snapshot
that every page echoes, stable session and chat cursors, and one explicit
processed outcome per chat version.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from neo4j import ManagedTransaction
from pydantic.types import JsonValue
from src.bounded_ingestion_models import (
    AttemptContext,
    OccurrenceContext,
    RunScope,
    UnitApplyResult,
    Usage,
)
from src.connectors.chat_helpers import ExtractionFailure, ExtractionFailureCode
from src.connectors.whatsadmin_api.bounded_connector import WhatsAdminBoundedConnector
from src.connectors.whatsadmin_api.bounded_writer import WhatsAdminBoundedWriter
from src.connectors.whatsadmin_api.credentials import WhatsAdminEntity
from src.connectors.whatsadmin_api.models import (
    ChatPage,
    PreparedChat,
    SessionPage,
    StoredChatPage,
)
from src.connectors.whatsadmin_api.watermark import bounded_entry_key, committed_version_key
from src.connectors.whatsapp.connector import _ChatBundle
from src.llm import LlmAttemptControl
from src.models import IngestResult
from src.resumable import CheckpointDescriptor

ENTITY: WhatsAdminEntity = "eko"
CONTRACT_VERSION = "whatsadmin-hyperp-extraction-v1"
WINDOW_ID = "eko-window-2026-09-17"
LOWER_BOUND = "2026-07-10T00:00:00+00:00"
UPPER_BOUND = "2026-09-17T06:00:00+00:00"
SNAPSHOT_AT = "2026-09-17T05:30:00+00:00"
SESSION_ID = "ses_1"
WHATSAPP_USER = "6590000000@c.us"
CREDENTIAL = "hk_eko_fixture_secret"
SOURCE_KEY = "whatsapp_chat"


def source_window(**overrides: JsonValue) -> dict[str, JsonValue]:
    """Return the immutable bounded WhatsAdmin window payload."""
    window: dict[str, JsonValue] = {
        "contract_version": CONTRACT_VERSION,
        "entity_key": ENTITY,
        "window_id": WINDOW_ID,
        "completed_lower_bound": LOWER_BOUND,
        "requested_as_of_upper_bound": UPPER_BOUND,
    }
    window.update(overrides)
    return window


def session_page(
    session_ids: tuple[str, ...],
    *,
    has_more: bool = False,
    next_cursor: str | None = None,
    snapshot_at: str | None = SNAPSHOT_AT,
    org_name: str = "EkoLife SG",
) -> SessionPage:
    meta: dict[str, object] = {
        "timestamp": "2026-09-17T05:30:00Z",
        "requestId": "req_sessions",
        "pagination": {"hasMore": has_more, "nextCursor": next_cursor},
    }
    if snapshot_at is not None:
        meta["snapshotAt"] = snapshot_at
    return SessionPage.model_validate(
        {
            "success": True,
            "data": [
                {
                    "id": session_id,
                    "orgId": "org_1",
                    "orgName": org_name,
                    "whatsappUserId": WHATSAPP_USER,
                    "expectedPhoneNumber": "6590000000",
                    "updatedAt": "2026-09-17T05:00:00Z",
                }
                for session_id in session_ids
            ],
            "meta": meta,
        }
    )


def chat_payload(
    chat_id: str,
    *,
    body: str = "Hello",
    session_id: str = SESSION_ID,
    whatsapp_user_id: str = WHATSAPP_USER,
) -> dict[str, object]:
    """Return one denormalized chat bundle payload."""
    return {
        "chatId": chat_id,
        "chatName": f"Chat {chat_id}",
        "sessionId": session_id,
        "whatsappUserId": whatsapp_user_id,
        "changedAt": "2026-09-17T05:20:00Z",
        "participants": [
            {
                "jid": chat_id,
                "phone": "6581111111",
                "name": "Alice",
                "role": "chat",
            }
        ],
        "messages": [
            {
                "fromId": chat_id,
                "toId": whatsapp_user_id,
                "authorId": None,
                "body": body,
                "timestamp": "2026-09-17T05:20:00Z",
                "fromMe": False,
            }
        ],
    }


def chat_page(
    chat_ids: tuple[str, ...],
    *,
    has_more: bool = False,
    next_cursor: str | None = None,
    snapshot_at: str | None = SNAPSHOT_AT,
    body: str = "Hello",
    bodies: dict[str, str] | None = None,
    session_id: str = SESSION_ID,
) -> ChatPage:
    meta: dict[str, object] = {
        "timestamp": "2026-09-17T05:30:00Z",
        "requestId": "req_chats",
        "pagination": {"hasMore": has_more, "nextCursor": next_cursor},
    }
    if snapshot_at is not None:
        meta["snapshotAt"] = snapshot_at
    return ChatPage.model_validate(
        {
            "success": True,
            "data": [
                chat_payload(
                    chat_id,
                    body=(bodies or {}).get(chat_id, body),
                    session_id=session_id,
                )
                for chat_id in chat_ids
            ],
            "meta": meta,
        }
    )


@dataclass
class ClientCall:
    """One recorded bounded read."""

    resource: str
    cursor: str | None
    session_id: str | None
    snapshot_at: str | None
    changed_since: str | None
    deadline_monotonic: float | None


@dataclass
class FakeBoundedClient:
    """Scripted one-page reads that record every request binding."""

    session_pages: list[SessionPage] = field(default_factory=lambda: [session_page((SESSION_ID,))])
    chat_pages: dict[str, list[ChatPage]] = field(default_factory=dict)
    entity_key: WhatsAdminEntity = ENTITY
    calls: list[ClientCall] = field(default_factory=list)
    closed: bool = False
    session_index: int = 0

    def read_session_page(
        self,
        *,
        cursor: str | None,
        deadline_monotonic: float | None = None,
    ) -> SessionPage:
        self.calls.append(ClientCall("sessions", cursor, None, None, None, deadline_monotonic))
        page = self.session_pages[self.session_index]
        self.session_index += 1
        return page

    def read_chat_page(
        self,
        *,
        session_id: str,
        changed_since: str | None = None,
        snapshot_at: str | None = None,
        cursor: str | None = None,
        deadline_monotonic: float | None = None,
    ) -> ChatPage:
        self.calls.append(
            ClientCall(
                "chats",
                cursor,
                session_id,
                snapshot_at,
                changed_since,
                deadline_monotonic,
            )
        )
        pages = self.chat_pages[session_id]
        index = 0 if cursor is None else int(cursor.rsplit("-", maxsplit=1)[1])
        return pages[index]

    def close(self) -> None:
        self.closed = True


class FakeCheckpointView:
    """Transaction view enforcing the real watermark authorization rule."""

    def __init__(self, state: FakeState, *, terminal_authorized: bool) -> None:
        self._state = state
        self._terminal_authorized = terminal_authorized

    def get(self, name: str) -> str | None:
        return self._state.entries.get(name)

    def set(self, name: str, value: str, *, status: str = "resume") -> None:
        if status == "completed" and not self._terminal_authorized:
            raise RuntimeError("completed checkpoint writes require terminal authorization")
        self._state.entries[name] = value
        self._state.writes.append((name, value, status))

    def delete(self, name: str) -> None:
        self._state.entries.pop(name, None)


@dataclass
class FakeState:
    """In-memory durable entries mirroring WhatsAdminBoundedState keys."""

    entries: dict[str, str] = field(default_factory=dict)
    reads: list[tuple[str, str]] = field(default_factory=list)
    writes: list[tuple[str, str, str]] = field(default_factory=list)
    closed: bool = False

    def stage_entry(
        self,
        kind: str,
        entity_key: WhatsAdminEntity,
        session_id: str,
        digest: str,
        payload: str,
    ) -> None:
        # Validate through the durable models so a malformed payload cannot be
        # staged where the strict store would reject it.
        if kind == "bundles":
            StoredChatPage.model_validate_json(payload)
        else:
            PreparedChat.model_validate_json(payload)
        self.entries[bounded_entry_key(kind, entity_key, session_id, digest)] = payload

    def read_entry(
        self,
        kind: str,
        entity_key: WhatsAdminEntity,
        session_id: str,
        digest: str,
    ) -> str | None:
        self.reads.append((kind, digest))
        return self.entries.get(bounded_entry_key(kind, entity_key, session_id, digest))

    def read_bundles(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
        digest: str,
    ) -> StoredChatPage | None:
        raw = self.read_entry("bundles", entity_key, session_id, digest)
        return None if raw is None else StoredChatPage.model_validate_json(raw)

    def read_prepared(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
        digest: str,
    ) -> PreparedChat | None:
        raw = self.read_entry("prepared", entity_key, session_id, digest)
        return None if raw is None else PreparedChat.model_validate_json(raw)

    def committed_version(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
        chat_id: str,
    ) -> str | None:
        self.reads.append(("version", chat_id))
        return self.entries.get(committed_version_key(entity_key, session_id, chat_id))

    def bind_transaction(
        self,
        _tx: ManagedTransaction,
        *,
        terminal_authorized: bool = False,
    ) -> FakeCheckpointView:
        return FakeCheckpointView(self, terminal_authorized=terminal_authorized)

    def close(self) -> None:
        self.closed = True


@dataclass
class FakePipeline:
    """Record every envelope handed to the bounded transaction hook."""

    envelopes: list[dict[str, JsonValue]] = field(default_factory=list)
    transactions: list[object] = field(default_factory=list)
    duplicate_ids: set[str] = field(default_factory=set)
    dropped_ids: set[str] = field(default_factory=set)
    error_ids: set[str] = field(default_factory=set)
    raise_on_ids: set[str] = field(default_factory=set)

    def ingest_in_transaction(
        self,
        tx: ManagedTransaction,
        envelope: object,
        ingest_run_id: str | None = None,
        exclusion_context: object | None = None,
    ) -> IngestResult:
        _ = ingest_run_id, exclusion_context
        payload = envelope.model_dump()
        assert isinstance(payload, dict)
        source_record_id = str(payload["source_record_id"])
        if source_record_id in self.raise_on_ids:
            self.raise_on_ids.discard(source_record_id)
            raise RuntimeError("simulated graph write failure")
        self.envelopes.append(payload)
        self.transactions.append(tx)
        return IngestResult(
            source_record_id=source_record_id,
            skipped_duplicate=source_record_id in self.duplicate_ids,
            dropped=source_record_id in self.dropped_ids,
            errors=["boom"] if source_record_id in self.error_ids else [],
        )


@dataclass
class RecordingBundleExtraction:
    """Replacement for ``process_whatsapp_bundles`` in adapter tests."""

    envelopes: dict[str, list[dict[str, JsonValue]]] = field(default_factory=dict)
    failures: dict[str, ExtractionFailureCode] = field(default_factory=dict)
    cancelled: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    call_controls: list[object] = field(default_factory=list)

    def __call__(
        self,
        bundles: list[_ChatBundle],
        *,
        fail_on_extraction_error: bool = False,
        on_extraction_failure: object = None,
        call_control: LlmAttemptControl | None = None,
    ) -> Iterator[dict[str, JsonValue]]:
        _ = fail_on_extraction_error
        from src.llm import LlmCallCancelledError

        if call_control is not None:
            call_control.before_attempt()
        self.call_controls.append(call_control)
        bundle = bundles[0]
        self.calls.append(bundle.chat_id)
        if bundle.chat_id in self.cancelled:
            raise LlmCallCancelledError("bounded extraction budget exhausted")
        failure = self.failures.get(bundle.chat_id)
        if failure is not None:
            assert callable(on_extraction_failure)
            on_extraction_failure(bundle, ExtractionFailure(failure, 4))
            return iter(())
        return iter(self.envelopes.get(bundle.chat_id, []))


def envelope(chat_id: str, index: int = 1) -> dict[str, JsonValue]:
    """Return one prepared conversation envelope for a chat."""
    scoped = f"{ENTITY}-{SESSION_ID}"
    return {
        "source_record_id": f"whatsapp-chat-{scoped}-{chat_id}-person-{index}",
        "observed_at": "2026-09-17T05:20:00+00:00",
        "record_hash": f"sha256:fixture-{chat_id}-{index}",
        "identifiers": [{"type": "phone", "value": "+6581111111", "is_verified": False}],
        "attributes": {"full_name": "Alice"},
        "raw_payload": {"chat_id": chat_id, "messages_text": "Hello"},
        "record_type": "conversation",
        "extraction_confidence": 0.9,
        "extraction_method": "llm:fixture",
    }


def scope(**overrides: object) -> RunScope:
    """Return the fixture run scope with optional overrides."""
    base = RunScope(
        environment="test",
        reset_generation=1,
        source_key=SOURCE_KEY,
        control_instance_id="whatsadmin-control",
        entity_key=ENTITY,
        stream_key=None,
        mode="delta",
        configuration_fingerprint="fixture-fingerprint",
        connector_version="whatsadmin-bounded-v1",
        checkpoint_schema_version=1,
        source_window=source_window(),
    )
    return replace(base, **overrides)


def occurrence() -> OccurrenceContext:
    """Return the approved Thursday 09:00-23:00 Asia/Singapore occurrence."""
    starts_at = datetime(2026, 9, 17, 1, tzinfo=UTC)
    return OccurrenceContext(
        occurrence_id="thu-2026-09-17",
        starts_at=starts_at,
        drain_starts_at=starts_at.replace(hour=14),
        cutoff_at=starts_at.replace(hour=15),
        next_eligible_at=starts_at.replace(day=24),
    )


def context(
    checkpoint: CheckpointDescriptor,
    *,
    attempt_generation: int = 1,
    run_scope: RunScope | None = None,
    deadline: datetime | None = datetime(2026, 9, 17, 14, 55, tzinfo=UTC),
) -> AttemptContext:
    """Return one bounded attempt sharing the fixture run scope."""
    return AttemptContext(
        logical_run_id="logical-whatsadmin",
        ingest_run_id=f"attempt-{attempt_generation}",
        worker_task_id=f"task-{attempt_generation}",
        attempt_generation=attempt_generation,
        fencing_token=attempt_generation,
        lease_token=f"lease-{attempt_generation}",
        global_slot_index=0,
        global_slot_fencing_token=attempt_generation,
        scope=run_scope or scope(),
        occurrence=occurrence(),
        checkpoint=checkpoint,
        usage=Usage(),
        reserved_usage=Usage(),
        operation_deadline_at=deadline,
    )


@dataclass
class WalkStep:
    """One applied bounded unit of a driven walk."""

    subphase: str
    replay_id: str
    records: int
    dispositions: tuple[str, ...]
    obligations: tuple[str, ...]
    resolutions: tuple[str, ...]
    terminal: bool
    usage: Usage


@dataclass
class WalkResult:
    """The durable outcome of one driven walk."""

    steps: list[WalkStep] = field(default_factory=list)
    checkpoint: CheckpointDescriptor | None = None
    finished: bool = False
    dispositions: tuple[str, ...] = ()
    extraction_calls: int = 0

    def subphases(self) -> list[str]:
        return [step.subphase for step in self.steps]

    def replay_ids(self) -> list[str]:
        return [step.replay_id for step in self.steps]


def cursor_subphase(checkpoint: CheckpointDescriptor) -> str:
    """Return the typed cursor subphase recorded in a checkpoint."""
    value = checkpoint.cursor.get("subphase")
    assert isinstance(value, str)
    return value


def drive(
    connector: WhatsAdminBoundedConnector,
    writer: WhatsAdminBoundedWriter,
    *,
    checkpoint: CheckpointDescriptor,
    generation: int = 1,
    run_scope: RunScope | None = None,
    max_units: int = 60,
    stop_on_obligation: bool = False,
) -> WalkResult:
    """Drive units through one writer until the walk terminates or stalls."""
    result = WalkResult()
    dispositions: list[str] = []
    current = checkpoint
    tx = FakeTx()
    for _ in range(max_units):
        before_subphase = cursor_subphase(current)
        attempt = context(current, attempt_generation=generation, run_scope=run_scope)
        unit = connector.fetch_one_unit(current, attempt)
        applied: UnitApplyResult = writer.apply(tx, attempt, unit)
        result.steps.append(
            WalkStep(
                subphase=before_subphase,
                replay_id=unit.replay_id,
                records=len(unit.unit.records),
                dispositions=tuple(applied.dispositions),
                obligations=tuple(item.replay_id for item in applied.retry_obligations),
                resolutions=tuple(item.replay_id for item in applied.resolved_retries),
                terminal=unit.terminal,
                usage=unit.usage,
            )
        )
        dispositions.extend(applied.dispositions)
        result.extraction_calls += unit.usage.extraction_calls
        current = unit.unit.checkpoint_after
        if unit.terminal:
            result.finished = True
            break
        if stop_on_obligation and applied.retry_obligations:
            break
    result.checkpoint = current
    result.dispositions = tuple(dispositions)
    return result


def drive_until(
    connector: WhatsAdminBoundedConnector,
    writer: WhatsAdminBoundedWriter,
    *,
    checkpoint: CheckpointDescriptor,
    subphase: str,
    generation: int = 1,
    run_scope: RunScope | None = None,
    max_units: int = 60,
) -> CheckpointDescriptor:
    """Apply units until the cursor reaches ``subphase`` and stop there."""
    current = checkpoint
    tx = FakeTx()
    for _ in range(max_units):
        if cursor_subphase(current) == subphase:
            return current
        attempt = context(current, attempt_generation=generation, run_scope=run_scope)
        unit = connector.fetch_one_unit(current, attempt)
        writer.apply(tx, attempt, unit)
        current = unit.unit.checkpoint_after
    raise AssertionError(f"walk did not reach {subphase}")


def apply_one(
    connector: WhatsAdminBoundedConnector,
    writer: WhatsAdminBoundedWriter,
    checkpoint: CheckpointDescriptor,
    *,
    generation: int = 1,
    run_scope: RunScope | None = None,
) -> tuple[UnitApplyResult, CheckpointDescriptor]:
    """Apply exactly one unit and return its result and next checkpoint."""
    attempt = context(checkpoint, attempt_generation=generation, run_scope=run_scope)
    unit = connector.fetch_one_unit(checkpoint, attempt)
    applied = writer.apply(FakeTx(), attempt, unit)
    return applied, unit.unit.checkpoint_after


class FakeTx:
    """Minimal transaction stand-in for writer tests."""
