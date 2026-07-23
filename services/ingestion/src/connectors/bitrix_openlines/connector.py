"""API-backed connector for selected Bitrix Open Lines conversations."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from src.connectors.base import SourceConnector
from src.connectors.bitrix.connector import (
    BitrixChatConnector,
    _AgentMember,
    _ChatBundle,
)
from src.connectors.bitrix_openlines.dialog_cache import DialogConfigCache
from src.connectors.bitrix_openlines.discovery import stream_chats
from src.connectors.bitrix_openlines.models import (
    ChatReference,
    CrmDiscoveryPage,
    DialogMetadata,
    OpenLineConfig,
    OpenLineMessage,
    merge_chat_references,
)
from src.connectors.bitrix_openlines.selection import (
    classify_channel,
    mapped_entity,
    no_config_selectable,
)
from src.connectors.bitrix_openlines.watermark import (
    BackfillCheckpoint,
    BackfillCheckpointStore,
)
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
    def iter_crm_chat_ref_pages(self) -> Iterable[list[ChatReference]]: ...
    def iter_recent_chat_refs(self, page_size: int) -> Iterable[ChatReference]: ...
    def get_dialog(self, chat_id: int) -> DialogMetadata: ...
    def get_messages(self, chat_id: int) -> list[OpenLineMessage]: ...
    def get_history(self, chat_id: int) -> list[OpenLineMessage]: ...
    def close(self) -> None: ...


@runtime_checkable
class ResumableOpenLinesClient(Protocol):
    def iter_crm_discovery_pages(self, *, start: int = 0) -> Iterable[CrmDiscoveryPage]: ...


class WatermarkStore(Protocol):
    def get(self, *, overlap_seconds: int) -> datetime | None: ...
    def set(self, value: datetime) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class _PreparedChat:
    reference: ChatReference
    bundle: _ChatBundle
    extra_raw_payload: dict[str, JsonValue]


@dataclass
class DiscoveryCounters:
    chats_scanned: int = 0
    dialogs_requested: int = 0
    chats_skipped_by_config: int = 0
    records_emitted: int = 0


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
        dialog_cache: DialogConfigCache | None = None,
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
        self._backfill_store = watermark if isinstance(watermark, BackfillCheckpointStore) else None
        self._active_backfill_start: int | None = None
        self._record_errors = False
        self._dialog_cache = dialog_cache
        self._counters = DiscoveryCounters()
        self._no_config_selectable = no_config_selectable(config)

    def get_source_key(self) -> str:
        return "bitrix_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        self._counters = DiscoveryCounters()
        self._no_config_selectable = no_config_selectable(self._config)
        try:
            yield from self._fetch_records_inner()
        finally:
            self._log_counters()

    def _fetch_records_inner(self) -> Iterator[dict[str, JsonValue]]:
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
        if (
            self._mode == "backfill"
            and self._backfill_store is not None
            and isinstance(self._client, ResumableOpenLinesClient)
        ):
            yield from self._fetch_resumable_backfill(line_names, since)
            return
        yield from self._fetch_references(
            stream_chats(
                self._client,
                recent_page_size=self._config.recent_page_size,
            ),
            line_names,
            since,
        )

    def _log_counters(self) -> None:
        logger.info(
            "Bitrix Open Lines discovery summary mode=%s chats_scanned=%d "
            "dialogs_requested=%d chats_skipped_by_config=%d records_emitted=%d",
            self._mode,
            self._counters.chats_scanned,
            self._counters.dialogs_requested,
            self._counters.chats_skipped_by_config,
            self._counters.records_emitted,
        )

    def _fetch_resumable_backfill(
        self,
        line_names: dict[str, str],
        since: datetime | None,
    ) -> Iterator[dict[str, JsonValue]]:
        assert self._backfill_store is not None
        assert isinstance(self._client, ResumableOpenLinesClient)
        checkpoint = self._backfill_store.get_backfill_checkpoint()
        crm_start = checkpoint.crm_start if checkpoint is not None else 0
        self._active_backfill_start = crm_start
        recent = {
            item.chat_id: item
            for item in self._client.iter_recent_chat_refs(self._config.recent_page_size)
        }
        seen: set[int] = set()
        if crm_start is not None:
            for page in self._client.iter_crm_discovery_pages(start=crm_start):
                references: list[ChatReference] = []
                for item in page.references:
                    if item.chat_id in seen:
                        continue
                    seen.add(item.chat_id)
                    recent_item = recent.pop(item.chat_id, None)
                    references.append(
                        item if recent_item is None else merge_chat_references(item, recent_item)
                    )
                yield from self._fetch_references(references, line_names, since)
                self._active_backfill_start = page.next_start
                if not self._record_errors:
                    self._backfill_store.set_backfill_checkpoint(
                        BackfillCheckpoint(crm_start=page.next_start)
                    )
        yield from self._fetch_references(
            (recent[chat_id] for chat_id in sorted(recent)),
            line_names,
            since,
        )

    def _fetch_references(
        self,
        references: Iterable[ChatReference],
        line_names: dict[str, str],
        since: datetime | None,
    ) -> Iterator[dict[str, JsonValue]]:
        max_chars = chat_batch_max_chars()
        max_count = chat_batch_size()
        prepared_batch: list[_PreparedChat] = []
        batch_chars = 0
        for reference in references:
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
        self._counters.chats_scanned += 1
        if self._no_config_selectable:
            self._counters.chats_skipped_by_config += 1
            return None
        dialog, needs_caching = self._resolve_dialog(reference)
        if dialog.config_id not in line_names:
            self._counters.chats_skipped_by_config += 1
            logger.warning(
                "Skipping Bitrix Open Lines chat %s: config %s is inactive or unavailable",
                reference.chat_id,
                dialog.config_id,
            )
            self._cache_unselected(reference.chat_id, dialog, needs_caching)
            return None
        channel_type = classify_channel(dialog.connector_id)
        entity = mapped_entity(dialog.config_id, channel_type, self._config)
        if entity is None:
            self._counters.chats_skipped_by_config += 1
            logger.warning(
                "Skipping Bitrix Open Lines chat %s: config %s is not selected or mapped",
                reference.chat_id,
                dialog.config_id,
            )
            self._cache_unselected(reference.chat_id, dialog, needs_caching)
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
            for record in self._builder._build_envelopes(
                bundle=prepared.bundle,
                extraction=extraction,
                company_mobile_numbers=self._company_mobile_numbers,
                company_email_addresses=self._company_email_addresses,
                internal_person_names=self._internal_person_names,
                file_exclusions=self._file_exclusions,
                source_record_prefix="bitrix-openlines-chat",
                platform="bitrix_openlines",
                extra_raw_payload=prepared.extra_raw_payload,
            ):
                self._counters.records_emitted += 1
                yield record
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

    def _resolve_dialog(
        self,
        reference: ChatReference,
    ) -> tuple[DialogMetadata, bool]:
        """Resolve dialog metadata without a dialog lookup when possible.

        Returns the resolved metadata and whether the result is newly learned
        (from the recent-dialog origin or a fresh ``im.dialog.get`` call) and
        therefore worth persisting for later unselected-config skips.
        """
        if reference.config_id is not None and reference.connector_id is not None:
            return (
                DialogMetadata(reference.chat_id, reference.config_id, reference.connector_id),
                True,
            )
        if self._dialog_cache is not None:
            cached = self._dialog_cache.get(reference.chat_id)
            if cached is not None:
                return cached, False
        self._counters.dialogs_requested += 1
        return self._dialog_for(reference), True

    def _cache_unselected(
        self,
        chat_id: int,
        dialog: DialogMetadata,
        needs_caching: bool,
    ) -> None:
        if self._dialog_cache is not None and needs_caching:
            self._dialog_cache.set(chat_id, dialog)

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
        if self._mode == "backfill" and self._backfill_store is not None:
            self._backfill_store.clear_backfill_checkpoint()

    def record_processed(self, *, succeeded: bool) -> None:
        if not succeeded:
            self._record_errors = True

    def failure_checkpoint(self) -> dict[str, JsonValue]:
        if self._mode != "backfill":
            return {}
        return {"crm_start": self._active_backfill_start}

    def close(self) -> None:
        self._client.close()
        self._watermark.close()
        if self._dialog_cache is not None:
            self._dialog_cache.close()

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
