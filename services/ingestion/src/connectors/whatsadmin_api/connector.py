"""API-backed WhatsAdmin chat connector."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol

from src.connectors.base import SourceConnector
from src.connectors.whatsadmin_api.credentials import WhatsAdminEntity
from src.connectors.whatsadmin_api.models import ChatPage, SessionRow
from src.connectors.whatsadmin_api.watermark import WatermarkStore
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


class WhatsAdminClient(Protocol):
    @property
    def entity_key(self) -> WhatsAdminEntity: ...
    def iter_sessions(self) -> Iterator[SessionRow]: ...
    def iter_chat_pages(self, session_id: str, changed_since: str | None) -> Iterator[ChatPage]: ...
    def close(self) -> None: ...


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
            yield from self._fetch_session(client, session, changed_since)

    def _fetch_session(
        self,
        client: WhatsAdminClient,
        session: SessionRow,
        changed_since: str | None,
    ) -> Iterator[dict[str, JsonValue]]:
        state_key = (client.entity_key, session.id)
        for page in client.iter_chat_pages(session.id, changed_since):
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
                fail_on_extraction_error=True,
            )

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
