"""Hybrid discovery of Bitrix Open Lines conversations."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Protocol

from src.bitrix_ingestion_models import CRM_ACTIVITY_SOURCE_ACCESS_RETIRED_REASON
from src.connectors.bitrix_openlines.models import ChatReference


class DiscoveryClient(Protocol):
    def iter_crm_chat_refs(self) -> Iterable[ChatReference]: ...
    def iter_recent_chat_refs(self, page_size: int) -> Iterable[ChatReference]: ...


class PagedDiscoveryClient(Protocol):
    def iter_crm_chat_ref_pages(self) -> Iterable[list[ChatReference]]: ...
    def iter_recent_chat_refs(self, page_size: int) -> Iterable[ChatReference]: ...


def stream_chats(
    client: PagedDiscoveryClient,
    *,
    recent_page_size: int,
) -> Iterator[ChatReference]:
    """Reject retired activity-based discovery before any source I/O."""
    raise RuntimeError(CRM_ACTIVITY_SOURCE_ACCESS_RETIRED_REASON)


def discover_chats(client: DiscoveryClient, *, recent_page_size: int) -> list[ChatReference]:
    """Reject retired activity-based discovery before any source I/O."""
    raise RuntimeError(CRM_ACTIVITY_SOURCE_ACCESS_RETIRED_REASON)
