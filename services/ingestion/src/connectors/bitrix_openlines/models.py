"""Typed values returned by the Bitrix Open Lines REST boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.models import JsonValue


@dataclass(frozen=True)
class OpenLineConfig:
    id: str
    line_name: str


@dataclass(frozen=True)
class CrmOwnerReference:
    owner_type: str
    owner_id: int


@dataclass(frozen=True)
class ChatReference:
    chat_id: int
    changed_at: datetime | None
    discovery: str
    activity_ids: tuple[str, ...] = ()
    crm_owner_references: tuple[CrmOwnerReference, ...] = ()
    provider_references: tuple[dict[str, JsonValue], ...] = ()


@dataclass(frozen=True)
class CrmDiscoveryPage:
    references: list[ChatReference]
    next_start: int | None


@dataclass(frozen=True)
class DialogMetadata:
    chat_id: int
    config_id: str
    connector_id: str


@dataclass(frozen=True)
class OpenLineMessage:
    id: int
    author_id: int
    author_name: str
    text: str
    date: datetime
    is_agent: bool


def merge_chat_references(first: ChatReference, second: ChatReference) -> ChatReference:
    """Merge two discoveries without dropping their typed provenance."""
    changed_at = _latest_changed_at(first.changed_at, second.changed_at)
    discoveries = sorted(set(first.discovery.split(",") + second.discovery.split(",")))
    return ChatReference(
        chat_id=first.chat_id,
        changed_at=changed_at,
        discovery=",".join(discoveries),
        activity_ids=tuple(dict.fromkeys((*first.activity_ids, *second.activity_ids))),
        crm_owner_references=tuple(
            dict.fromkeys((*first.crm_owner_references, *second.crm_owner_references))
        ),
        provider_references=_unique_provider_references(
            (*first.provider_references, *second.provider_references)
        ),
    )


def _latest_changed_at(first: datetime | None, second: datetime | None) -> datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


def _unique_provider_references(
    references: tuple[dict[str, JsonValue], ...],
) -> tuple[dict[str, JsonValue], ...]:
    unique: list[dict[str, JsonValue]] = []
    for reference in references:
        if reference not in unique:
            unique.append(reference)
    return tuple(unique)
