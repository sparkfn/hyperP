"""Bounded, session-aware WhatsAdmin chat extraction continuation.

The adapter advances one durable unit at a time through typed cursor
subphases::

    sessions -> chats -> extract -> commit -> chats/sessions -> terminal

Chat pages are stored durably before any extraction, extraction output is
stored durably before any graph write, and the checkpoint only advances inside
the bounded commit transaction. A unit therefore never reports partial work as
success, and a graph failure reuses prepared output instead of calling the LLM
again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from pydantic.types import JsonValue

from src.bounded_ingestion_models import AttemptContext, BoundedUnit, Usage, utc_now
from src.connectors.chat_helpers import ExtractionFailure
from src.connectors.whatsadmin_api.bounded_calls import BoundedChatCallControl
from src.connectors.whatsadmin_api.bounded_state import (
    WhatsAdminBoundedState,
    WhatsAdminCursor,
    WhatsAdminWindow,
    bundle_from_stored,
    bundles_digest,
    credential_fingerprint,
    extraction_replay_id,
    parse_timestamp,
    prepared_chat,
    prepared_digest,
    retry_source_record_id,
    store_bundle,
    window_from_source_window,
)
from src.connectors.whatsadmin_api.connector import build_chat_bundles
from src.connectors.whatsadmin_api.credentials import WhatsAdminEntity
from src.connectors.whatsadmin_api.models import (
    ChatPage,
    PreparedChat,
    SessionPage,
    SessionRow,
    StoredBundle,
    StoredChatPage,
    StoredSession,
)
from src.connectors.whatsadmin_api.watermark import bounded_digest
from src.connectors.whatsapp.connector import ORG_TO_ENTITY, _ChatBundle, process_whatsapp_bundles
from src.llm import LlmCallCancelledError
from src.resumable import CheckpointCompatibility, CheckpointDescriptor, IngestionUnit

logger = logging.getLogger(__name__)

#: Outer checkpoint phase; the typed continuation lives in the cursor subphase.
PHASE = "whatsadmin"
REPLAY_BOUNDARY = "session-chat-version"


class WhatsAdminBoundedClient(Protocol):
    """The one-page upstream reads the bounded adapter is allowed to issue."""

    @property
    def entity_key(self) -> WhatsAdminEntity: ...

    def read_session_page(
        self,
        *,
        cursor: str | None,
        deadline_monotonic: float | None = None,
    ) -> SessionPage: ...

    def read_chat_page(
        self,
        *,
        session_id: str,
        changed_since: str | None = None,
        snapshot_at: str | None = None,
        cursor: str | None = None,
        deadline_monotonic: float | None = None,
    ) -> ChatPage: ...

    def close(self) -> None: ...


class WhatsAdminBoundedConnector:
    """Fetch one bounded continuation unit for one WhatsAdmin entity."""

    def __init__(
        self,
        *,
        entity_key: WhatsAdminEntity,
        client: WhatsAdminBoundedClient,
        credential: str,
        state: WhatsAdminBoundedState,
        window: WhatsAdminWindow,
        legacy_entity: WhatsAdminEntity | None,
        max_records_per_unit: int,
        max_bytes_per_unit: int,
        max_extraction_calls_per_unit: int,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if client.entity_key != entity_key:
            raise ValueError("bounded WhatsAdmin client entity does not match its adapter")
        self._entity_key = entity_key
        self._client = client
        self._credential_fingerprint = credential_fingerprint(credential)
        self._state = state
        self._window = window
        self._legacy_entity = legacy_entity
        self._max_records_per_unit = max_records_per_unit
        self._max_bytes_per_unit = max_bytes_per_unit
        self._max_extraction_calls_per_unit = max_extraction_calls_per_unit
        self._clock = clock

    def validate_checkpoint(self, checkpoint: CheckpointDescriptor) -> CheckpointCompatibility:
        """Return whether the durable checkpoint can safely continue."""
        if checkpoint.phase != PHASE:
            return "incompatible"
        try:
            cursor = WhatsAdminCursor.from_payload(checkpoint.cursor)
            window = window_from_source_window(checkpoint.source_window)
        except ValueError:
            return "corrupted"
        if window != self._window or cursor.contract_version != window.contract_version:
            return "incompatible"
        if cursor.credential_fingerprint != self._credential_fingerprint:
            return "rejected"
        if not self._referenced_entries_exist(cursor):
            return "corrupted"
        return "compatible"

    def fetch_one_unit(
        self,
        checkpoint: CheckpointDescriptor,
        context: AttemptContext,
    ) -> BoundedUnit:
        """Return the next durable unit for this checkpoint."""
        cursor = WhatsAdminCursor.from_payload(checkpoint.cursor)
        if cursor.subphase == "sessions":
            return self._sessions_unit(checkpoint, cursor, context)
        if cursor.subphase == "chats":
            return self._chats_unit(checkpoint, cursor, context)
        if cursor.subphase == "extract":
            return self._extract_unit(checkpoint, cursor, context)
        if cursor.subphase == "commit":
            return self._commit_unit(checkpoint, cursor, context)
        raise RuntimeError("bounded WhatsAdmin checkpoint is already terminal")

    def cancel(self) -> None:
        """Cancellation is cooperative: the caller abandons the in-flight read."""

    def close(self) -> None:
        self._client.close()
        self._state.close()

    # Session enumeration ---------------------------------------------------

    def _sessions_unit(
        self,
        checkpoint: CheckpointDescriptor,
        cursor: WhatsAdminCursor,
        context: AttemptContext,
    ) -> BoundedUnit:
        page = self._client.read_session_page(
            cursor=cursor.sessions_cursor,
            deadline_monotonic=self._deadline_monotonic(context),
        )
        snapshot = self._bind_snapshot(cursor, page.meta.snapshot_at, required=False)
        usage = self._page_usage(page)
        replay_id = _replay_id(
            "sessions",
            self._entity_key,
            cursor.window_id,
            cursor.sessions_cursor or "first",
            str(cursor.sessions_processed),
        )
        queue = tuple(self._stored_session(row) for row in page.data)
        processed = cursor.sessions_processed + len(page.data)
        next_cursor = _next_cursor(page)
        if queue:
            located = replace(
                cursor,
                subphase="chats",
                snapshot_id=snapshot,
                session_queue=queue,
                sessions_cursor=next_cursor,
                sessions_processed=processed,
            )
            return self._unit(checkpoint, cursor, located, replay_id, usage, terminal=False)
        if next_cursor is not None:
            located = replace(
                cursor,
                snapshot_id=snapshot,
                sessions_cursor=next_cursor,
                sessions_processed=processed,
            )
            return self._unit(checkpoint, cursor, located, replay_id, usage, terminal=False)
        located = replace(cursor, subphase="terminal", snapshot_id=snapshot)
        return self._unit(checkpoint, cursor, located, replay_id, usage, terminal=True)

    def _chats_unit(
        self,
        checkpoint: CheckpointDescriptor,
        cursor: WhatsAdminCursor,
        context: AttemptContext,
    ) -> BoundedUnit:
        session = _required_session(cursor)
        page = self._client.read_chat_page(
            session_id=session.session_id,
            changed_since=cursor.lower_bound,
            snapshot_at=cursor.snapshot_id,
            cursor=cursor.chats_cursor,
            deadline_monotonic=self._deadline_monotonic(context),
        )
        snapshot = self._bind_snapshot(cursor, page.meta.snapshot_at, required=True)
        self._validate_chat_identities(session, page)
        bundles = build_chat_bundles(
            tenant=self._entity_key,
            page=page,
            session_id=session.session_id,
            whatsapp_user_id=session.whatsapp_user_id,
            expected_phone_number=session.expected_phone_number,
            legacy_entity=self._legacy_entity,
        )
        if len(bundles) > self._max_records_per_unit:
            raise RuntimeError("bounded WhatsAdmin chat page exceeds its record bound")
        digest = bundles_digest(session.session_id, cursor.chats_cursor, cursor.sessions_processed)
        if bundles:
            payload = StoredChatPage(
                session_id=session.session_id,
                whatsapp_user_id=session.whatsapp_user_id,
                bundles=[store_bundle(bundle) for bundle in bundles],
            ).model_dump_json()
            self._state.stage_entry(
                "bundles",
                self._entity_key,
                session.session_id,
                digest,
                self._bounded_payload(payload, "stored chat page"),
            )
        located = replace(
            cursor,
            snapshot_id=snapshot,
            chats_cursor=_next_cursor(page),
            bundles_digest=digest if bundles else None,
            bundle_count=len(bundles),
            bundle_index=0,
        )
        if bundles:
            after_cursor: WhatsAdminCursor = replace(located, subphase="extract")
            terminal = False
        else:
            after_cursor, terminal = self._after_commit(located)
        replay_id = _replay_id(
            "chats",
            self._entity_key,
            session.session_id,
            cursor.chats_cursor or "first",
            digest,
        )
        return self._unit(
            checkpoint,
            cursor,
            after_cursor,
            replay_id,
            self._page_usage(page),
            terminal=terminal,
        )

    # Extraction ------------------------------------------------------------

    def _extract_unit(
        self,
        checkpoint: CheckpointDescriptor,
        cursor: WhatsAdminCursor,
        context: AttemptContext,
    ) -> BoundedUnit:
        session = _required_session(cursor)
        bundle = self._current_bundle(cursor)
        replay_id = extraction_replay_id(
            self._entity_key,
            session.session_id,
            bundle.chat_id,
            bundle.source_version,
        )
        if bundle.source_version == self._state.committed_version(
            self._entity_key,
            session.session_id,
            bundle.chat_id,
        ):
            located = replace(
                cursor,
                subphase="commit",
                prepared_digest=None,
                chat_id=bundle.chat_id,
                source_version=bundle.source_version,
                retry_replay_id=None,
                retry_source_record_id=None,
                extract_attempts=0,
            )
            return self._unit(checkpoint, cursor, located, replay_id, Usage(), terminal=False)
        control = BoundedChatCallControl(
            deadline=self._operation_deadline(context),
            cancellation=context.cancellation,
            max_attempts=self._max_extraction_calls_per_unit,
            clock=self._clock,
        )
        digest = prepared_digest(session.session_id, bundle.chat_id, bundle.source_version)
        staged = self._state.read_prepared(self._entity_key, session.session_id, digest)
        if staged is not None and (
            staged.chat_id != bundle.chat_id or staged.source_version != bundle.source_version
        ):
            raise RuntimeError("bounded WhatsAdmin staged output does not match its chat version")
        if staged is None:
            try:
                envelopes, failure = self._extract(bundle, control)
            except LlmCallCancelledError as exc:
                return self._retry_unit(
                    checkpoint,
                    cursor,
                    bundle,
                    control,
                    "provider_error",
                    str(exc),
                )
            if failure is not None:
                return self._retry_unit(
                    checkpoint,
                    cursor,
                    bundle,
                    control,
                    failure.code,
                    f"extraction attempts={failure.attempts}",
                )
            if len(envelopes) > self._max_records_per_unit:
                raise RuntimeError("bounded WhatsAdmin extraction exceeds its envelope bound")
            self._stage_prepared(session.session_id, digest, prepared_chat(bundle, envelopes))
        located = replace(
            cursor,
            subphase="commit",
            prepared_digest=digest,
            chat_id=bundle.chat_id,
            source_version=bundle.source_version,
            retry_replay_id=None,
            retry_source_record_id=None,
            extract_attempts=0,
        )
        return self._unit(
            checkpoint,
            cursor,
            located,
            replay_id,
            Usage(extraction_calls=control.attempts),
            terminal=False,
        )

    def _stage_prepared(
        self,
        session_id: str,
        digest: str,
        prepared: PreparedChat,
    ) -> None:
        self._state.stage_entry(
            "prepared",
            self._entity_key,
            session_id,
            digest,
            self._bounded_payload(prepared.model_dump_json(), "prepared extraction"),
        )

    def _bounded_payload(self, payload: str, what: str) -> str:
        """Refuse an oversized durable payload rather than truncating it."""
        if len(payload.encode("utf-8")) > self._max_bytes_per_unit:
            raise RuntimeError(f"bounded WhatsAdmin {what} exceeds its byte bound")
        return payload

    def _retry_unit(
        self,
        checkpoint: CheckpointDescriptor,
        cursor: WhatsAdminCursor,
        bundle: StoredBundle,
        control: BoundedChatCallControl,
        category: str,
        reason: str,
    ) -> BoundedUnit:
        """Persist a durable retry obligation without leaving the extract subphase."""
        logger.warning(
            "Bounded WhatsAdmin extraction deferred entity=%s session=%s chat=%s "
            "category=%s attempts=%d reason=%s",
            self._entity_key,
            bundle.session_id,
            bundle.chat_id,
            category,
            control.attempts,
            reason,
        )
        replay_id = extraction_replay_id(
            self._entity_key,
            bundle.session_id,
            bundle.chat_id,
            bundle.source_version,
        )
        source_record_id = retry_source_record_id(bundle)
        attempts = cursor.extract_attempts + 1
        located = replace(
            cursor,
            retry_replay_id=replay_id,
            retry_source_record_id=source_record_id,
            extract_attempts=attempts,
        )
        marker: dict[str, JsonValue] = {
            "kind": "extraction_retry",
            "replay_id": replay_id,
            "source_record_id": source_record_id,
            "source_version": bundle.source_version,
            "category": category,
            "attempt_count": attempts,
        }
        return self._unit(
            checkpoint,
            cursor,
            located,
            replay_id,
            Usage(records=1, extraction_calls=control.attempts),
            terminal=False,
            records=(marker,),
        )

    # Commit ----------------------------------------------------------------

    def _commit_unit(
        self,
        checkpoint: CheckpointDescriptor,
        cursor: WhatsAdminCursor,
        context: AttemptContext,
    ) -> BoundedUnit:
        session = _required_session(cursor)
        envelopes = self._prepared_envelopes(cursor)
        last_record_id = _last_record_id(envelopes) or checkpoint.last_committed_record_id
        after_cursor, terminal = self._after_commit(cursor)
        replay_id = _replay_id(
            "commit",
            self._entity_key,
            session.session_id,
            cursor.chat_id or "none",
            cursor.source_version or "none",
        )
        return self._unit(
            checkpoint,
            cursor,
            after_cursor,
            replay_id,
            Usage(records=len(envelopes)),
            terminal=terminal,
            records=tuple(envelopes),
            last_record_id=last_record_id,
        )

    def _prepared_envelopes(self, cursor: WhatsAdminCursor) -> list[dict[str, JsonValue]]:
        session = _required_session(cursor)
        if cursor.prepared_digest is None:
            return []
        prepared = self._state.read_prepared(
            self._entity_key,
            session.session_id,
            cursor.prepared_digest,
        )
        if prepared is None:
            raise RuntimeError("bounded WhatsAdmin prepared output is missing")
        if prepared.chat_id != cursor.chat_id or prepared.source_version != cursor.source_version:
            raise RuntimeError("bounded WhatsAdmin prepared output does not match its cursor")
        return prepared.envelopes

    def _after_commit(self, cursor: WhatsAdminCursor) -> tuple[WhatsAdminCursor, bool]:
        """Return the cursor after the current stored chat page is fully committed."""
        session = _required_session(cursor)
        next_index = cursor.bundle_index + 1
        if next_index < cursor.bundle_count:
            return (
                replace(
                    cursor,
                    subphase="extract",
                    bundle_index=next_index,
                    prepared_digest=None,
                    chat_id=None,
                    source_version=None,
                    retry_replay_id=None,
                    retry_source_record_id=None,
                    extract_attempts=0,
                ),
                False,
            )
        cleared = replace(
            cursor,
            bundles_digest=None,
            bundle_count=0,
            bundle_index=0,
            prepared_digest=None,
            chat_id=None,
            source_version=None,
            chats_cursor=None,
            retry_replay_id=None,
            retry_source_record_id=None,
            extract_attempts=0,
        )
        if cursor.chats_cursor is not None:
            return (replace(cleared, subphase="chats"), False)
        remaining = cursor.session_queue[1:]
        completed = (*cursor.completed_sessions, session.session_id)
        if remaining:
            return (replace(cleared, subphase="chats", session_queue=remaining), False)
        if cursor.sessions_cursor is not None:
            return (
                replace(cleared, subphase="sessions", completed_sessions=completed),
                False,
            )
        return (replace(cleared, subphase="terminal", completed_sessions=completed), True)

    # Shared helpers --------------------------------------------------------

    def _unit(
        self,
        checkpoint: CheckpointDescriptor,
        before_cursor: WhatsAdminCursor,
        after_cursor: WhatsAdminCursor,
        replay_id: str,
        usage: Usage,
        *,
        terminal: bool,
        records: tuple[dict[str, JsonValue], ...] = (),
        last_record_id: str | None = None,
    ) -> BoundedUnit:
        if after_cursor.to_payload() == before_cursor.to_payload():
            raise RuntimeError("bounded WhatsAdmin unit does not advance its cursor")
        after = replace(
            checkpoint,
            cursor=after_cursor.to_payload(),
            last_committed_record_id=last_record_id or checkpoint.last_committed_record_id,
        )
        return BoundedUnit(
            unit=IngestionUnit(checkpoint, after, records),
            replay_id=replay_id,
            usage=usage,
            terminal=terminal,
        )

    def _extract(
        self,
        bundle: StoredBundle,
        control: BoundedChatCallControl,
    ) -> tuple[list[dict[str, JsonValue]], ExtractionFailure | None]:
        failures: list[ExtractionFailure] = []

        def _record(_bundle: _ChatBundle, failure: ExtractionFailure) -> None:
            failures.append(failure)

        envelopes = list(
            process_whatsapp_bundles(
                [bundle_from_stored(bundle)],
                on_extraction_failure=_record,
                call_control=control,
            )
        )
        logger.info(
            "Bounded WhatsAdmin extraction entity=%s session=%s chat=%s attempts=%d "
            "envelopes=%d version=%s",
            self._entity_key,
            bundle.session_id,
            bundle.chat_id,
            control.attempts,
            len(envelopes),
            bundle.source_version,
        )
        return envelopes, failures[0] if failures else None

    def _current_bundle(self, cursor: WhatsAdminCursor) -> StoredBundle:
        session = _required_session(cursor)
        page = self._stored_page(cursor)
        if cursor.bundle_index >= len(page.bundles):
            raise RuntimeError("bounded WhatsAdmin cursor is beyond its stored chat page")
        if page.session_id != session.session_id:
            raise RuntimeError("bounded WhatsAdmin stored chat page does not match its session")
        return page.bundles[cursor.bundle_index]

    def _stored_page(self, cursor: WhatsAdminCursor) -> StoredChatPage:
        session = _required_session(cursor)
        if cursor.bundles_digest is None:
            raise RuntimeError("bounded WhatsAdmin cursor lost its stored chat page")
        page = self._state.read_bundles(
            self._entity_key,
            session.session_id,
            cursor.bundles_digest,
        )
        if page is None:
            raise RuntimeError("bounded WhatsAdmin stored chat page is missing")
        return page

    def _referenced_entries_exist(self, cursor: WhatsAdminCursor) -> bool:
        if cursor.subphase not in {"extract", "commit"}:
            return True
        session = cursor.current_session()
        if session is None or cursor.bundles_digest is None:
            return False
        if (
            self._state.read_entry(
                "bundles",
                self._entity_key,
                session.session_id,
                cursor.bundles_digest,
            )
            is None
        ):
            return False
        if cursor.prepared_digest is None:
            return True
        return (
            self._state.read_entry(
                "prepared",
                self._entity_key,
                session.session_id,
                cursor.prepared_digest,
            )
            is not None
        )

    def _stored_session(self, row: SessionRow) -> StoredSession:
        if ORG_TO_ENTITY.get(row.org_name) != self._entity_key:
            raise RuntimeError("WhatsAdmin session organization does not match credential entity")
        if not row.whatsapp_user_id:
            raise RuntimeError("WhatsAdmin session omitted its WhatsApp user")
        return StoredSession(
            session_id=row.id,
            whatsapp_user_id=row.whatsapp_user_id,
            expected_phone_number=row.expected_phone_number,
        )

    @staticmethod
    def _validate_chat_identities(session: StoredSession, page: ChatPage) -> None:
        for chat in page.data:
            if chat.session_id != session.session_id:
                raise RuntimeError("WhatsAdmin chat session does not match requested session")
            if chat.whatsapp_user_id != session.whatsapp_user_id:
                raise RuntimeError("WhatsAdmin chat WhatsApp user does not match requested session")

    def _bind_snapshot(
        self,
        cursor: WhatsAdminCursor,
        snapshot_at: datetime | None,
        *,
        required: bool,
    ) -> str | None:
        """Bind the first observed snapshot, then refuse any drift."""
        if snapshot_at is None:
            if required or cursor.snapshot_id is None:
                raise RuntimeError("WhatsAdmin page omitted the requested-as-of snapshot")
            return cursor.snapshot_id
        if snapshot_at > parse_timestamp(cursor.upper_bound, "upper_bound"):
            raise RuntimeError("WhatsAdmin snapshot is beyond the requested-as-of upper bound")
        bound = snapshot_at.isoformat()
        if cursor.snapshot_id is not None and bound != cursor.snapshot_id:
            raise RuntimeError("WhatsAdmin snapshotAt changed during bounded extraction")
        return bound

    @staticmethod
    def _page_usage(page: SessionPage | ChatPage) -> Usage:
        return Usage(
            source_requests=1,
            pages=1,
            bytes_read=len(page.model_dump_json().encode("utf-8")),
        )

    def _deadline_monotonic(self, context: AttemptContext) -> float | None:
        remaining = context.remaining_seconds(self._clock())
        if remaining is None:
            return None
        return time.monotonic() + remaining

    @staticmethod
    def _operation_deadline(context: AttemptContext) -> datetime | None:
        if context.operation_deadline_at is not None:
            return context.operation_deadline_at
        return context.occurrence.cutoff_at if context.occurrence is not None else None


def initial_checkpoint(
    *,
    scope_window: dict[str, JsonValue],
    credential: str,
    connector_version: str,
    schema_version: int,
) -> CheckpointDescriptor:
    """Build the stable first checkpoint without performing any I/O."""
    window = window_from_source_window(scope_window)
    cursor = WhatsAdminCursor(
        subphase="sessions",
        contract_version=window.contract_version,
        credential_fingerprint=credential_fingerprint(credential),
        window_id=window.window_id,
        lower_bound=window.completed_lower_bound,
        upper_bound=window.requested_as_of_upper_bound,
        snapshot_id=None,
        sessions_cursor=None,
        session_queue=(),
        sessions_processed=0,
        chats_cursor=None,
        bundles_digest=None,
        bundle_count=0,
        bundle_index=0,
        prepared_digest=None,
        chat_id=None,
        source_version=None,
        retry_replay_id=None,
        retry_source_record_id=None,
        extract_attempts=0,
        completed_sessions=(),
    )
    return CheckpointDescriptor(
        phase=PHASE,
        cursor=cursor.to_payload(),
        source_window=dict(scope_window),
        last_committed_record_id=None,
        connector_version=connector_version,
        schema_version=schema_version,
        replay_boundary=REPLAY_BOUNDARY,
    )


def _required_session(cursor: WhatsAdminCursor) -> StoredSession:
    session = cursor.current_session()
    if session is None:
        raise RuntimeError("bounded WhatsAdmin cursor has no current session")
    return session


def _replay_id(kind: str, *parts: str) -> str:
    return "sha256:" + bounded_digest(kind, *parts)


def _next_cursor(page: SessionPage | ChatPage) -> str | None:
    if not page.meta.pagination.has_more:
        return None
    cursor = page.meta.pagination.next_cursor
    if cursor is None:
        raise RuntimeError("WhatsAdmin page omitted nextCursor")
    return cursor


def _last_record_id(envelopes: list[dict[str, JsonValue]]) -> str | None:
    if not envelopes:
        return None
    value = envelopes[-1].get("source_record_id")
    return value if isinstance(value, str) and value else None
