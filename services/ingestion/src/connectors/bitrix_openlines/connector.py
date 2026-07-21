"""API-backed connector for selected Bitrix Open Lines conversations."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
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
from src.connectors.chat_helpers import (
    chat_batch_max_chars,
    chat_batch_size,
    run_extraction_batch,
)
from src.exclusion_config import ExclusionFile
from src.ingestion_config import BitrixOpenLinesConfig
from src.models import JsonValue

logger = logging.getLogger(__name__)


class OpenLinesClient(Protocol):
    def list_active_configs(self) -> list[OpenLineConfig]: ...
    def iter_crm_chat_refs(self) -> Iterable[ChatReference]: ...
    def iter_recent_chat_refs(self, page_size: int) -> Iterable[ChatReference]: ...
    def get_dialog(self, chat_id: int) -> DialogMetadata: ...
    def get_messages(self, chat_id: int) -> list[OpenLineMessage]: ...
    def get_history(self, chat_id: int) -> list[OpenLineMessage]: ...
    def close(self) -> None: ...


class WatermarkStore(Protocol):
    def get(self, *, overlap_seconds: int) -> datetime | None: ...
    def set(self, value: datetime) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class _PreparedChat:
    reference: ChatReference
    bundle: _ChatBundle
    extra_raw_payload: dict[str, JsonValue]


class BitrixOpenLinesConnector(SourceConnector):
    def __init__(
        self,
        client: OpenLinesClient,
        watermark: WatermarkStore,
        config: BitrixOpenLinesConfig,
        *,
        mode: str,
        company_mobile_numbers: list[str] | None = None,
        company_email_addresses: list[str] | None = None,
        internal_person_names: list[str] | None = None,
        file_exclusions: ExclusionFile | None = None,
    ) -> None:
        self._client = client
        self._watermark = watermark
        self._config = config
        self._mode = mode
        self._pending_watermark: datetime | None = None
        self._builder = BitrixChatConnector()
        self._company_mobile_numbers = list(company_mobile_numbers or [])
        self._company_email_addresses = list(company_email_addresses or [])
        self._internal_person_names = list(internal_person_names or [])
        self._file_exclusions = file_exclusions or ExclusionFile()

    def get_source_key(self) -> str:
        return "bitrix_chat"

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
        max_chars = chat_batch_max_chars()
        max_count = chat_batch_size()
        prepared_batch: list[_PreparedChat] = []
        batch_chars = 0
        for reference in discover_chats(
            self._client,
            recent_page_size=self._config.recent_page_size,
        ):
            if len(prepared_batch) >= max_count:
                yield from self._extract_batch(prepared_batch)
                prepared_batch = []
                batch_chars = 0
            prepared = self._prepare_chat(reference, line_names, since)
            if prepared is None:
                continue
            prepared_chars = len(prepared.bundle.conv_text)
            if prepared_batch and batch_chars + prepared_chars > max_chars:
                yield from self._extract_batch(prepared_batch)
                prepared_batch = []
                batch_chars = 0
            prepared_batch.append(prepared)
            batch_chars += prepared_chars
        if prepared_batch:
            yield from self._extract_batch(prepared_batch)

    def _prepare_chat(
        self,
        reference: ChatReference,
        line_names: dict[str, str],
        since: datetime | None,
    ) -> _PreparedChat | None:
        dialog = self._dialog_for(reference)
        if dialog.config_id not in line_names:
            logger.warning(
                "Skipping Bitrix Open Lines chat %s: config %s is inactive or unavailable",
                reference.chat_id,
                dialog.config_id,
            )
            return None
        channel_type = classify_channel(dialog.connector_id)
        entity = mapped_entity(dialog.config_id, channel_type, self._config)
        if entity is None:
            logger.warning(
                "Skipping Bitrix Open Lines chat %s: config %s is not selected or mapped",
                reference.chat_id,
                dialog.config_id,
            )
            return None
        messages = self._messages_for(reference)
        if not messages:
            return None
        last_message_at = max(message.date for message in messages)
        effective_changed_at = (
            max(reference.changed_at, last_message_at)
            if reference.changed_at is not None
            else last_message_at
        )
        if since is not None and effective_changed_at < since:
            return None
        bundle = _ChatBundle(
            chat_id=reference.chat_id,
            deal_id=None,
            bitrix_chat_id=f"chat{reference.chat_id}",
            last_message_at=last_message_at,
            created_at=min(message.date for message in messages),
            category_name=line_names.get(dialog.config_id, ""),
            entity=entity,
            conv_text=_format_messages(messages),
            deal=None,
            agents=_agent_members(messages),
        )
        return _PreparedChat(
            reference=reference,
            bundle=bundle,
            extra_raw_payload=_raw_provenance(
                reference,
                dialog,
                line_names.get(dialog.config_id, ""),
                channel_type,
                messages,
            ),
        )

    def _extract_batch(
        self,
        batch: list[_PreparedChat],
    ) -> Iterator[dict[str, JsonValue]]:
        texts = [prepared.bundle.conv_text for prepared in batch]
        results = run_extraction_batch(texts)
        for prepared, extraction in zip(batch, results, strict=True):
            if extraction is None:
                self._pending_watermark = None
                raise RuntimeError(
                    f"Bitrix Open Lines extraction failed for chat {prepared.reference.chat_id}"
                )
            yield from self._builder._build_envelopes(
                bundle=prepared.bundle,
                extraction=extraction,
                company_mobile_numbers=self._company_mobile_numbers,
                company_email_addresses=self._company_email_addresses,
                internal_person_names=self._internal_person_names,
                file_exclusions=self._file_exclusions,
                source_record_prefix="bitrix-openlines-chat",
                platform="bitrix_openlines",
                extra_raw_payload=prepared.extra_raw_payload,
            )
            prepared_last_message_at = prepared.bundle.last_message_at
            assert prepared_last_message_at is not None
            self._track_watermark(prepared.reference, prepared_last_message_at)

    def _messages_for(self, reference: ChatReference) -> list[OpenLineMessage]:
        resource = "message"
        try:
            discoveries = set(reference.discovery.split(","))
            if discoveries == {"crm_activity"}:
                resource = "history"
                return self._client.get_history(reference.chat_id)
            return self._client.get_messages(reference.chat_id)
        except Exception:  # noqa: BLE001 -- fail safely without exposing upstream payload text
            self._pending_watermark = None
            raise _retrieval_error(reference, resource) from None

    def _dialog_for(self, reference: ChatReference) -> DialogMetadata:
        try:
            return self._client.get_dialog(reference.chat_id)
        except Exception:  # noqa: BLE001 -- fail safely without exposing upstream payload text
            self._pending_watermark = None
            raise _retrieval_error(reference, "dialog") from None

    def commit_watermark(self) -> None:
        if self._mode == "api" and self._pending_watermark is not None:
            self._watermark.set(self._pending_watermark)
            self._pending_watermark = None

    def close(self) -> None:
        self._client.close()
        self._watermark.close()

    def _track_watermark(self, reference: ChatReference, last_message_at: datetime) -> None:
        candidate = (
            max(reference.changed_at, last_message_at)
            if reference.changed_at is not None
            else last_message_at
        )
        self._track_watermark_candidate(candidate)

    def _track_watermark_candidate(self, candidate: datetime) -> None:
        if self._pending_watermark is None or candidate > self._pending_watermark:
            self._pending_watermark = candidate


def _raw_provenance(
    reference: ChatReference,
    dialog: DialogMetadata,
    line_name: str,
    channel_type: str,
    messages: list[OpenLineMessage],
) -> dict[str, JsonValue]:
    discovery_methods: list[JsonValue] = list(reference.discovery.split(","))
    owner_references: list[JsonValue] = [
        {"owner_type": item.owner_type, "owner_id": item.owner_id}
        for item in reference.crm_owner_references
    ]
    return {
        "bitrix_chat_id_numeric": reference.chat_id,
        "openline_config_id": dialog.config_id,
        "openline_name": line_name,
        "channel_type": channel_type,
        "connector_id": dialog.connector_id,
        "discovery_methods": discovery_methods,
        "crm_activity_ids": list(reference.activity_ids),
        "crm_owner_references": owner_references,
        "crm_provider_references": list(reference.provider_references),
        "first_message_at": min(message.date for message in messages).isoformat(),
        "last_message_at": max(message.date for message in messages).isoformat(),
    }


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


def _retrieval_error(reference: ChatReference, resource: str) -> RuntimeError:
    return RuntimeError(
        f"Bitrix Open Lines chat {reference.chat_id} discovered via "
        f"{reference.discovery}: {resource} retrieval failed"
    )
