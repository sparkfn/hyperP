"""API-backed WhatsAdmin chat connector."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from src.connectors.base import SourceConnector
from src.connectors.chat_helpers import ExtractionFailure
from src.connectors.whatsadmin_api.credentials import WhatsAdminEntity
from src.connectors.whatsadmin_api.models import ChatPage, SessionRow
from src.connectors.whatsadmin_api.retry_queue import (
    bundle_entity_key,
    deserialize_retry_bundle,
    retry_matches_bundle,
    serialize_retry_bundle,
)
from src.connectors.whatsadmin_api.watermark import (
    ExtractionRetryStore,
    PageCheckpoint,
    PageCheckpointStore,
    WatermarkStore,
)
from src.connectors.whatsapp.connector import (
    ORG_TO_ENTITY,
    _ChatBundle,
    _format_messages,
    _latest_message_timestamp,
    _message_endpoints,
    _Participant,
    process_whatsapp_bundles,
)
from src.models import JsonValue

logger = logging.getLogger(__name__)


class WhatsAdminClient(Protocol):
    @property
    def entity_key(self) -> WhatsAdminEntity: ...
    def iter_sessions(self) -> Iterator[SessionRow]: ...
    def iter_chat_pages(
        self,
        session_id: str,
        changed_since: str | None,
        cursor: str | None = None,
    ) -> Iterator[ChatPage]: ...
    def close(self) -> None: ...


@runtime_checkable
class FailureContextReporter(Protocol):
    def failure_context(self) -> dict[str, JsonValue]: ...


class WhatsAdminChatApiConnector(SourceConnector):
    def __init__(
        self,
        clients: tuple[WhatsAdminClient, ...],
        watermark: WatermarkStore,
        *,
        legacy_entity: WhatsAdminEntity | None = None,
    ) -> None:
        self._clients = clients
        self._watermark = watermark
        self._legacy_entity = legacy_entity
        self._pending_watermarks: dict[tuple[WhatsAdminEntity, str], datetime] = {}
        self._active_checkpoint: dict[str, JsonValue] = {}
        self._checkpoint_store = watermark if isinstance(watermark, PageCheckpointStore) else None
        self._retry_store = watermark if isinstance(watermark, ExtractionRetryStore) else None
        self._touched_checkpoints: set[tuple[WhatsAdminEntity, str]] = set()
        self._record_errors = False
        self._downstream_errors = False
        self._active_retry_downstream_error = False
        self._extraction_failures: list[dict[str, JsonValue]] = []

    def get_source_key(self) -> str:
        return "whatsapp_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        for client in self._clients:
            yield from self._fetch_client(client)

    def _fetch_client(self, client: WhatsAdminClient) -> Iterator[dict[str, JsonValue]]:
        entity_key = client.entity_key
        for session in client.iter_sessions():
            if ORG_TO_ENTITY.get(session.org_name) != entity_key:
                raise RuntimeError(
                    "WhatsAdmin session organization does not match credential entity"
                )
            watermark = self._watermark.get(entity_key, session.id)
            changed_since = watermark.isoformat() if watermark is not None else None
            self._active_checkpoint = {
                "entity_key": entity_key,
                "session_id": session.id,
                "changed_since": changed_since,
                "cursor": "first",
            }
            checkpoint = self._load_checkpoint(entity_key, session.id, changed_since)
            yield from self._retry_pending_extractions(entity_key, session.id)
            if checkpoint is not None and checkpoint.complete:
                self._pending_watermarks[(entity_key, session.id)] = checkpoint.snapshot_at
                self._active_checkpoint.update(
                    {
                        "snapshot_at": checkpoint.snapshot_at.isoformat(),
                        "cursor": "complete",
                    }
                )
                continue
            yield from self._fetch_session(client, session, changed_since, checkpoint)

    def _retry_pending_extractions(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
    ) -> Iterator[dict[str, JsonValue]]:
        if self._retry_store is None:
            return
        retries = self._retry_store.get_extraction_retries(entity_key, session_id)
        if not retries:
            return
        for retry in retries:
            bundle = deserialize_retry_bundle(retry)
            failures_before = len(self._extraction_failures)
            self._active_retry_downstream_error = False
            yield from process_whatsapp_bundles(
                [bundle],
                on_extraction_failure=self._record_extraction_failure,
            )
            extraction_failed = len(self._extraction_failures) != failures_before
            if not extraction_failed and not self._active_retry_downstream_error:
                self._clear_extraction_retry(bundle)

    def _load_checkpoint(
        self,
        entity_key: WhatsAdminEntity,
        session_id: str,
        changed_since: str | None,
    ) -> PageCheckpoint | None:
        if self._checkpoint_store is None:
            return None
        checkpoint = self._checkpoint_store.get_checkpoint(entity_key, session_id)
        if checkpoint is None:
            return None
        if checkpoint.changed_since != changed_since:
            self._checkpoint_store.delete_checkpoint(entity_key, session_id)
            return None
        self._touched_checkpoints.add((entity_key, session_id))
        return checkpoint

    def _fetch_session(
        self,
        client: WhatsAdminClient,
        session: SessionRow,
        changed_since: str | None,
        checkpoint: PageCheckpoint | None,
    ) -> Iterator[dict[str, JsonValue]]:
        state_key = (client.entity_key, session.id)
        if checkpoint is not None:
            self._pending_watermarks[state_key] = checkpoint.snapshot_at
        cursor = checkpoint.cursor if checkpoint is not None else None
        pages = (
            client.iter_chat_pages(session.id, changed_since, cursor)
            if cursor is not None
            else client.iter_chat_pages(session.id, changed_since)
        )
        for page in pages:
            if page.meta.snapshot_at is None:
                raise RuntimeError("WhatsAdmin chat page omitted snapshotAt")
            self._validate_chat_identities(session, page)
            prior = self._pending_watermarks.get(state_key)
            if prior is not None and prior != page.meta.snapshot_at:
                raise RuntimeError("WhatsAdmin snapshotAt changed during pagination")
            self._pending_watermarks[state_key] = page.meta.snapshot_at
            bundles = self._bundles(session, client.entity_key, page)
            yield from process_whatsapp_bundles(
                bundles,
                on_extraction_failure=self._record_extraction_failure,
            )
            next_cursor = page.meta.pagination.next_cursor
            if page.meta.pagination.has_more and next_cursor is None:
                raise RuntimeError("WhatsAdmin chat page omitted nextCursor")
            saved = PageCheckpoint(
                changed_since=changed_since,
                cursor=next_cursor,
                snapshot_at=page.meta.snapshot_at,
                complete=not page.meta.pagination.has_more,
            )
            if not self._record_errors:
                self._save_checkpoint(state_key, saved)
            self._active_checkpoint.update(
                {
                    "request_id": page.meta.request_id,
                    "snapshot_at": page.meta.snapshot_at.isoformat(),
                    "cursor": next_cursor or "complete",
                }
            )

    def _save_checkpoint(
        self,
        state_key: tuple[WhatsAdminEntity, str],
        checkpoint: PageCheckpoint,
    ) -> None:
        if self._checkpoint_store is None:
            return
        self._checkpoint_store.set_checkpoint(*state_key, checkpoint)
        self._touched_checkpoints.add(state_key)

    @staticmethod
    def _validate_chat_identities(session: SessionRow, page: ChatPage) -> None:
        for chat in page.data:
            if chat.session_id != session.id:
                raise RuntimeError("WhatsAdmin chat session does not match requested session")
            if chat.whatsapp_user_id != session.whatsapp_user_id:
                raise RuntimeError("WhatsAdmin chat WhatsApp user does not match requested session")

    def commit_watermark(self) -> None:
        for (entity_key, session_id), value in self._pending_watermarks.items():
            self._watermark.set(entity_key, session_id, value)
        self._pending_watermarks.clear()
        self._delete_touched_checkpoints()

    def record_processed(self, *, succeeded: bool) -> None:
        if not succeeded:
            self._record_errors = True
            self._downstream_errors = True
            self._active_retry_downstream_error = True

    def commit_progress_with_errors(self) -> bool:
        """Allow watermark progress only when every isolated failure is durable."""
        return self._retry_store is not None and not self._downstream_errors

    def connector_error_count(self) -> int:
        """Return chats isolated after bounded extraction retries."""
        return len(self._extraction_failures)

    def _record_extraction_failure(
        self,
        bundle: _ChatBundle,
        failure: ExtractionFailure,
    ) -> None:
        """Record safe diagnostics and durably queue the source bundle for retry."""
        self._record_errors = True
        details: dict[str, JsonValue] = {
            "entity_key": bundle.tenant,
            "session_id": bundle.session_id,
            "chat_id": bundle.chat_id,
            "observed_at": bundle.observed_at,
            "failure_code": failure.code,
            "attempts": failure.attempts,
        }
        self._extraction_failures.append(details)
        self._save_extraction_retry(bundle, details)
        self._active_checkpoint["extraction_failures"] = list(self._extraction_failures)
        logger.warning(
            "Recorded WhatsAdmin chat extraction failure entity=%s session=%s chat=%s "
            "code=%s attempts=%d",
            bundle.tenant,
            bundle.session_id,
            bundle.chat_id,
            failure.code,
            failure.attempts,
        )

    def _save_extraction_retry(self, bundle: _ChatBundle, details: dict[str, JsonValue]) -> None:
        if self._retry_store is None:
            return
        entity_key = bundle_entity_key(bundle)
        retries = self._retry_store.get_extraction_retries(entity_key, bundle.session_id)
        serialized = serialize_retry_bundle(bundle, details)
        retries = [item for item in retries if not retry_matches_bundle(item, bundle)]
        retries.append(serialized)
        self._retry_store.set_extraction_retries(entity_key, bundle.session_id, retries)

    def _clear_extraction_retry(self, bundle: _ChatBundle) -> None:
        if self._retry_store is None:
            return
        entity_key = bundle_entity_key(bundle)
        retries = self._retry_store.get_extraction_retries(entity_key, bundle.session_id)
        remaining = [item for item in retries if not retry_matches_bundle(item, bundle)]
        self._retry_store.set_extraction_retries(entity_key, bundle.session_id, remaining)

    def _delete_touched_checkpoints(self) -> None:
        if self._checkpoint_store is not None:
            for state_key in self._touched_checkpoints:
                self._checkpoint_store.delete_checkpoint(*state_key)
        self._touched_checkpoints.clear()

    def failure_checkpoint(self) -> dict[str, JsonValue]:
        checkpoint = dict(self._active_checkpoint)
        for client in self._clients:
            if isinstance(client, FailureContextReporter):
                checkpoint.update(client.failure_context())
        return checkpoint

    def close(self) -> None:
        for client in self._clients:
            client.close()
        self._watermark.close()

    def _bundles(
        self,
        session: SessionRow,
        tenant: WhatsAdminEntity,
        page: ChatPage,
    ) -> list[_ChatBundle]:
        result: list[_ChatBundle] = []
        for chat in page.data:
            messages: list[dict[str, object]] = [
                {
                    "from_id": message.from_id,
                    "to_id": message.to_id,
                    "author_id": message.author_id,
                    "body": message.body,
                    "timestamp": message.timestamp,
                    "from_me": message.from_me,
                }
                for message in chat.messages
            ]
            if not messages:
                continue
            participants = [
                _Participant(item.jid, item.phone, item.name, item.role)
                for item in chat.participants
            ]
            result.append(
                _ChatBundle(
                    chat_id=chat.chat_id,
                    chat_name=chat.chat_name,
                    session_id=session.id,
                    whatsapp_user_id=session.whatsapp_user_id,
                    tenant=tenant,
                    msg_text=_format_messages(messages, participants, chat.chat_name),
                    observed_at=_latest_message_timestamp(messages),
                    participants=participants,
                    message_endpoints=_message_endpoints(messages),
                    session_phone=session.expected_phone_number,
                    source_id_scope=(
                        session.id if tenant == self._legacy_entity else f"{tenant}-{session.id}"
                    ),
                )
            )
        return result
