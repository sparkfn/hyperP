"""Hybrid discovery of Bitrix Open Lines conversations."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Protocol

from src.connectors.bitrix_openlines.models import ChatReference, merge_chat_references


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
    """Yield hybrid discovery page-by-page without materialising all CRM activity."""
    recent = {item.chat_id: item for item in client.iter_recent_chat_refs(recent_page_size)}
    seen: set[int] = set()
    for page in client.iter_crm_chat_ref_pages():
        for item in page:
            if item.chat_id in seen:
                continue
            seen.add(item.chat_id)
            recent_item = recent.pop(item.chat_id, None)
            yield item if recent_item is None else merge_chat_references(item, recent_item)
    for chat_id in sorted(recent):
        yield recent[chat_id]


def discover_chats(client: DiscoveryClient, *, recent_page_size: int) -> list[ChatReference]:
    """Return the de-duplicated union of CRM and recent-dialog discovery."""
    merged: dict[int, ChatReference] = {}
    for item in [
        *client.iter_crm_chat_refs(),
        *client.iter_recent_chat_refs(recent_page_size),
    ]:
        prior = merged.get(item.chat_id)
        if prior is None:
            merged[item.chat_id] = item
            continue
        merged[item.chat_id] = merge_chat_references(prior, item)
    return [merged[chat_id] for chat_id in sorted(merged)]
