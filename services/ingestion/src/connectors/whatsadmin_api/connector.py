"""API-backed WhatsAdmin chat connector."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol

from src.connectors.base import SourceConnector
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
    def iter_sessions(self) -> Iterator[SessionRow]: ...
    def iter_chat_pages(self, session_id: str, changed_since: str | None) -> Iterator[ChatPage]: ...
    def close(self) -> None: ...


class WhatsAdminChatApiConnector(SourceConnector):
    def __init__(self, client: WhatsAdminClient, watermark: WatermarkStore) -> None:
        self._client = client
        self._watermark = watermark
        self._pending_watermarks: dict[str, datetime] = {}

    def get_source_key(self) -> str:
        return "whatsapp_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        for session in self._client.iter_sessions():
            tenant = ORG_TO_ENTITY.get(session.org_name)
            if tenant is None:
                continue
            watermark = self._watermark.get(session.id)
            changed_since = watermark.isoformat() if watermark is not None else None
            for page in self._client.iter_chat_pages(session.id, changed_since):
                if page.meta.snapshot_at is None:
                    raise RuntimeError("WhatsAdmin chat page omitted snapshotAt")
                prior = self._pending_watermarks.get(session.id)
                if prior is not None and prior != page.meta.snapshot_at:
                    raise RuntimeError("WhatsAdmin snapshotAt changed during pagination")
                self._pending_watermarks[session.id] = page.meta.snapshot_at
                bundles = self._bundles(session, tenant, page)
                yield from process_whatsapp_bundles(
                    bundles,
                    fail_on_extraction_error=True,
                )

    def commit_watermark(self) -> None:
        for session_id, value in self._pending_watermarks.items():
            self._watermark.set(session_id, value)
        self._pending_watermarks.clear()

    def close(self) -> None:
        self._client.close()
        self._watermark.close()

    def _bundles(self, session: SessionRow, tenant: str, page: ChatPage) -> list[_ChatBundle]:
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
                    source_id_scope=session.id,
                )
            )
        return result
