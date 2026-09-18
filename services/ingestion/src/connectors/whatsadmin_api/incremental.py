"""IncrementalConnector adapter for WhatsAdmin API chat ingestion."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.connectors.whatsadmin_api.connector import WhatsAdminClient
from src.connectors.whatsadmin_api.credentials import WhatsAdminEntity
from src.connectors.whatsadmin_api.models import ChatPage, SessionRow
from src.connectors.whatsapp.connector import (
    ORG_TO_ENTITY,
    _ChatBundle,
    _format_messages,
    _latest_message_timestamp,
    _message_endpoints,
    _Participant,
    process_whatsapp_bundles,
)
from src.incremental_connector import IncrementalPage
from src.models import JsonValue

if TYPE_CHECKING:
    from src.connectors.chat_helpers import ExtractionFailure

logger = logging.getLogger(__name__)


class WhatsAdminIncrementalConnector:
    """IncrementalConnector for WhatsAdmin API chat ingestion."""

    def __init__(
        self,
        clients: tuple[WhatsAdminClient, ...],
        *,
        legacy_entity: WhatsAdminEntity | None = None,
    ) -> None:
        self._clients = clients
        self._legacy_entity = legacy_entity
        self._page_iter: Iterator[IncrementalPage] | None = None
        self._buffered: IncrementalPage | None = None
        self._updated_since: datetime | None = None

    def open_query(self, updated_since: datetime | None) -> None:
        """Build and prime the page iterator."""
        self._updated_since = updated_since
        self._page_iter = self._all_pages(updated_since)
        self._buffered = next(self._page_iter, None)

    def fetch_next_page(self) -> IncrementalPage:
        """Return the next page; authoritative has_more via peeking buffer."""
        if self._buffered is None:
            return IncrementalPage(
                records=(),
                has_more=False,
                max_updated_at=self._updated_since or datetime.now(tz=UTC),
            )
        current = self._buffered
        assert self._page_iter is not None
        next_page = next(self._page_iter, None)
        if next_page is not None:
            self._buffered = next_page
            return dataclasses.replace(current, has_more=True)
        self._buffered = None
        return dataclasses.replace(current, has_more=False)

    def get_source_key(self) -> str:
        return "whatsapp_chat"

    def close(self) -> None:
        for client in self._clients:
            client.close()

    # ------------------------------------------------------------------
    # Internal iteration
    # ------------------------------------------------------------------

    def _all_pages(
        self,
        updated_since: datetime | None,
    ) -> Iterator[IncrementalPage]:
        changed_since = updated_since.isoformat() if updated_since is not None else None
        for client in self._clients:
            yield from self._client_pages(client, changed_since)

    def _client_pages(
        self,
        client: WhatsAdminClient,
        changed_since: str | None,
    ) -> Iterator[IncrementalPage]:
        entity_key = client.entity_key
        for session in client.iter_sessions():
            if ORG_TO_ENTITY.get(session.org_name) != entity_key:
                raise RuntimeError(
                    "WhatsAdmin session organization does not match credential entity"
                )
            for page in client.iter_chat_pages(session.id, changed_since):
                if page.meta.snapshot_at is None:
                    raise RuntimeError("WhatsAdmin chat page omitted snapshotAt")
                self._validate_chat_identities(session, page)
                bundles = self._bundles(session, entity_key, page)
                records: list[dict[str, JsonValue]] = list(
                    process_whatsapp_bundles(
                        bundles,
                        on_extraction_failure=self._log_extraction_failure,
                    )
                )
                yield IncrementalPage(
                    records=tuple(records),
                    has_more=True,
                    max_updated_at=page.meta.snapshot_at,
                )

    @staticmethod
    def _validate_chat_identities(
        session: SessionRow,
        page: ChatPage,
    ) -> None:
        for chat in page.data:
            if chat.session_id != session.id:
                raise RuntimeError("WhatsAdmin chat session does not match requested session")
            if chat.whatsapp_user_id != session.whatsapp_user_id:
                raise RuntimeError("WhatsAdmin chat WhatsApp user does not match requested session")

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
                    msg_text=_format_messages(
                        messages,
                        participants,
                        chat.chat_name,
                    ),
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

    @staticmethod
    def _log_extraction_failure(
        bundle: _ChatBundle,
        failure: ExtractionFailure,
    ) -> None:
        logger.warning(
            "WhatsAdmin incremental extraction failure entity=%s session=%s chat=%s",
            bundle.tenant,
            bundle.session_id,
            bundle.chat_id,
        )
