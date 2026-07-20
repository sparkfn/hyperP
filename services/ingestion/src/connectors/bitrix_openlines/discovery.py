"""Hybrid discovery of Bitrix Open Lines conversations."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from src.connectors.bitrix_openlines.models import ChatReference


class DiscoveryClient(Protocol):
    def iter_crm_chat_refs(self) -> Iterable[ChatReference]: ...
    def iter_recent_chat_refs(self, page_size: int) -> Iterable[ChatReference]: ...


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
        changed_at = _latest_changed_at(prior.changed_at, item.changed_at)
        discoveries = sorted(set(prior.discovery.split(",") + item.discovery.split(",")))
        merged[item.chat_id] = ChatReference(item.chat_id, changed_at, ",".join(discoveries))
    return [merged[chat_id] for chat_id in sorted(merged)]


def _latest_changed_at(first: datetime | None, second: datetime | None) -> datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)
