"""API-backed connector for selected Bitrix Open Lines conversations."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from typing import Protocol

from src.connectors.base import SourceConnector
from src.connectors.bitrix.connector import (
    BitrixChatConnector,
    _AgentMember,
    _ChatBundle,
)
from src.connectors.bitrix_openlines.discovery import discover_chats
from src.connectors.bitrix_openlines.models import (
    ChatReference,
    DialogMetadata,
    OpenLineConfig,
    OpenLineMessage,
)
from src.connectors.bitrix_openlines.selection import classify_channel, mapped_entity
from src.connectors.chat_helpers import run_extraction_batch
from src.ingestion_config import BitrixOpenLinesConfig
from src.models import JsonValue

logger = logging.getLogger(__name__)


class OpenLinesClient(Protocol):
    def list_active_configs(self) -> list[OpenLineConfig]: ...
    def iter_crm_chat_refs(self) -> Iterable[ChatReference]: ...
    def iter_recent_chat_refs(self, page_size: int) -> Iterable[ChatReference]: ...
    def get_dialog(self, chat_id: int) -> DialogMetadata: ...
    def get_messages(self, chat_id: int) -> list[OpenLineMessage]: ...
    def close(self) -> None: ...


class WatermarkStore(Protocol):
    def get(self, *, overlap_seconds: int) -> datetime | None: ...
    def set(self, value: datetime) -> None: ...
    def close(self) -> None: ...


class BitrixOpenLinesConnector(SourceConnector):
    def __init__(
        self,
        client: OpenLinesClient,
        watermark: WatermarkStore,
        config: BitrixOpenLinesConfig,
        *,
        mode: str,
    ) -> None:
        self._client = client
        self._watermark = watermark
        self._config = config
        self._mode = mode
        self._pending_watermark: datetime | None = None
        self._builder = BitrixChatConnector()

    def get_source_key(self) -> str:
        return "bitrix_openlines"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        line_names = {item.id: item.line_name for item in self._client.list_active_configs()}
        committed_watermark = (
            self._watermark.get(overlap_seconds=0) if self._mode == "api" else None
        )
        since = (
            committed_watermark - timedelta(seconds=self._config.incremental_overlap_seconds)
            if committed_watermark is not None
            else None
        )
        if committed_watermark is not None:
            self._track_watermark_candidate(committed_watermark)
        for reference in discover_chats(
            self._client,
            recent_page_size=self._config.recent_page_size,
        ):
            if since is not None and reference.changed_at is not None:
                if reference.changed_at < since:
                    continue
            dialog = self._client.get_dialog(reference.chat_id)
            if dialog.config_id not in line_names:
                logger.warning(
                    "Skipping Bitrix Open Lines chat %s: config %s is inactive or unavailable",
                    reference.chat_id,
                    dialog.config_id,
                )
                continue
            channel_type = classify_channel(dialog.connector_id)
            entity = mapped_entity(dialog.config_id, channel_type, self._config)
            if entity is None:
                logger.warning(
                    "Skipping Bitrix Open Lines chat %s: config %s is not selected or mapped",
                    reference.chat_id,
                    dialog.config_id,
                )
                continue
            messages = self._client.get_messages(reference.chat_id)
            if not messages:
                continue
            last_message_at = max(message.date for message in messages)
            effective_changed_at = reference.changed_at or last_message_at
            if since is not None and effective_changed_at < since:
                continue
            transcript = _format_messages(messages)
            extraction = run_extraction_batch([transcript])[0]
            if extraction is None:
                raise RuntimeError(
                    f"Bitrix Open Lines extraction failed for chat {reference.chat_id}"
                )
            agents = _agent_members(messages)
            bundle = _ChatBundle(
                chat_id=reference.chat_id,
                deal_id=None,
                bitrix_chat_id=f"chat{reference.chat_id}",
                last_message_at=last_message_at,
                created_at=messages[0].date,
                category_name=line_names.get(dialog.config_id, ""),
                entity=entity,
                conv_text=transcript,
                deal=None,
                agents=agents,
            )
            discovery_methods: list[JsonValue] = []
            discovery_methods.extend(reference.discovery.split(","))
            extra: dict[str, JsonValue] = {
                "openline_config_id": dialog.config_id,
                "openline_name": line_names.get(dialog.config_id, ""),
                "channel_type": channel_type,
                "connector_id": dialog.connector_id,
                "discovery_methods": discovery_methods,
            }
            yield from self._builder._build_envelopes(
                bundle=bundle,
                extraction=extraction,
                source_record_prefix="bitrix-openlines-chat",
                platform="bitrix_openlines",
                extra_raw_payload=extra,
            )
            self._track_watermark(reference, last_message_at)

    def commit_watermark(self) -> None:
        if self._mode == "api" and self._pending_watermark is not None:
            self._watermark.set(self._pending_watermark)
            self._pending_watermark = None

    def close(self) -> None:
        self._client.close()
        self._watermark.close()

    def _track_watermark(self, reference: ChatReference, last_message_at: datetime) -> None:
        candidate = reference.changed_at or last_message_at
        self._track_watermark_candidate(candidate)

    def _track_watermark_candidate(self, candidate: datetime) -> None:
        if self._pending_watermark is None or candidate > self._pending_watermark:
            self._pending_watermark = candidate


def _format_messages(messages: list[OpenLineMessage]) -> str:
    return "\n".join(
        f"[{item.date.strftime('%Y-%m-%d %H:%M:%S')}] {item.author_name}: {item.text.strip()}"
        for item in messages
        if item.text.strip()
    )


def _agent_members(messages: list[OpenLineMessage]) -> list[_AgentMember]:
    by_id = {
        item.author_id: _AgentMember(str(item.author_id), item.author_name, True)
        for item in messages
        if item.is_agent and item.author_id != 0
    }
    return list(by_id.values())
