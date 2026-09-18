"""Fixture-backed contract validation for the proposed bounded Bitrix proxy API."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from pydantic.types import JsonValue

BitrixBoundedStream = Literal[
    "crm_deals",
    "openlines_conversations",
    "crm_stage_history",
]
DealChangeKind = Literal["upsert", "tombstone"]
ConversationChangeKind = Literal["upsert", "tombstone"]

CONTRACT_VERSION = "bitrix_bounded_v1"
CAPABILITY_ENDPOINT = "hyperp.capabilities.get"
CHANGED_DEALS_ENDPOINT = "hyperp.deals.changed.list"
CHANGED_CONVERSATIONS_ENDPOINT = "hyperp.openlines.changed.list"

_REQUIRED_CAPABILITY: dict[BitrixBoundedStream, str] = {
    "crm_deals": "changed_deals_v1",
    "openlines_conversations": "changed_conversations_v1",
    "crm_stage_history": "stage_history_artifact_v1",
}


class BitrixBoundedContractError(ValueError):
    """A synthetic or upstream bounded response violates the frozen contract."""


class BitrixBoundedCapabilityError(RuntimeError):
    """A required bounded upstream capability is absent or does not match scope."""


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """A capability response bound to one source scope and frozen snapshot."""

    source_scope: str
    snapshot_id: str
    capabilities: frozenset[str]
    cursor_ttl_seconds: int


@dataclass(frozen=True, order=True, slots=True)
class DealChangeOrder:
    """The required stable total order for changed-deal pages."""

    change_version: int
    deal_id: int
    event_id: str


@dataclass(frozen=True, slots=True)
class DealChange:
    """One upsert or tombstone in the proposed changed-deal feed."""

    order: DealChangeOrder
    kind: DealChangeKind
    revision: str
    category_id: str | None
    payload: dict[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class ChangedDealsPage:
    """One bounded changed-deal page under a single frozen snapshot."""

    snapshot_id: str
    next_cursor: str | None
    changes: tuple[DealChange, ...]


@dataclass(frozen=True, order=True, slots=True)
class ConversationChangeOrder:
    """The required stable total order for changed-conversation pages."""

    change_version: int
    chat_id: int
    event_id: str


@dataclass(frozen=True, slots=True)
class ConversationChange:
    """One changed conversation, including its immutable message revision."""

    order: ConversationChangeOrder
    kind: ConversationChangeKind
    revision: str
    message_cursor: str | None
    payload: dict[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class ChangedConversationsPage:
    """One bounded changed-conversation page under a single frozen snapshot."""

    snapshot_id: str
    next_cursor: str | None
    changes: tuple[ConversationChange, ...]


def required_capability(stream_key: BitrixBoundedStream) -> str:
    """Return the exact proxy capability required by a bounded Bitrix stream."""
    capability = _REQUIRED_CAPABILITY.get(stream_key)
    if capability is None:
        raise BitrixBoundedContractError("Bitrix bounded stream is unsupported")
    return capability


def parse_capability_snapshot(value: JsonValue) -> CapabilitySnapshot:
    """Parse a capability response without accepting undeclared or loose fields."""
    payload = _mapping(value, "capability response")
    _require_exact_keys(
        payload,
        {"contract_version", "source_scope", "snapshot_id", "capabilities", "cursor_ttl_seconds"},
        "capability response",
    )
    if _text(payload["contract_version"], "contract_version") != CONTRACT_VERSION:
        raise BitrixBoundedContractError("capability response has an unsupported contract version")
    capabilities = _text_set(payload["capabilities"], "capabilities")
    return CapabilitySnapshot(
        source_scope=_text(payload["source_scope"], "source_scope"),
        snapshot_id=_text(payload["snapshot_id"], "snapshot_id"),
        capabilities=capabilities,
        cursor_ttl_seconds=_positive_int(payload["cursor_ttl_seconds"], "cursor_ttl_seconds"),
    )


def validate_capability_snapshot(
    snapshot: CapabilitySnapshot,
    *,
    source_scope: str,
    snapshot_id: str,
    stream_key: BitrixBoundedStream,
) -> None:
    """Require the negotiated capability response to match the frozen run boundary."""
    if snapshot.source_scope != source_scope:
        raise BitrixBoundedCapabilityError("Bitrix bounded capability source scope mismatch")
    if snapshot.snapshot_id != snapshot_id:
        raise BitrixBoundedCapabilityError("Bitrix bounded capability snapshot mismatch")
    if required_capability(stream_key) not in snapshot.capabilities:
        raise BitrixBoundedCapabilityError(
            f"Bitrix bounded capability missing for stream {stream_key}"
        )


def parse_changed_deals_page(
    value: JsonValue,
    *,
    expected_snapshot_id: str,
    max_changes: int,
) -> ChangedDealsPage:
    """Parse one ordered changed-deal page and reject mutable-offset semantics."""
    payload = _page_mapping(value, "changed-deal page")
    snapshot_id, next_cursor, changes_value = _page_fields(payload, "changed-deal page")
    if snapshot_id != expected_snapshot_id:
        raise BitrixBoundedContractError("changed-deal page snapshot mismatch")
    changes = tuple(
        _parse_deal_change(item) for item in _list(changes_value, "changed-deal changes")
    )
    _validate_page_size(changes, max_changes, "changed-deal page")
    _validate_strict_order((item.order for item in changes), "changed-deal page")
    return ChangedDealsPage(snapshot_id, next_cursor, changes)


def parse_changed_conversations_page(
    value: JsonValue,
    *,
    expected_snapshot_id: str,
    max_changes: int,
) -> ChangedConversationsPage:
    """Parse one ordered changed-conversation page and preserve message revisions."""
    payload = _page_mapping(value, "changed-conversation page")
    snapshot_id, next_cursor, changes_value = _page_fields(payload, "changed-conversation page")
    if snapshot_id != expected_snapshot_id:
        raise BitrixBoundedContractError("changed-conversation page snapshot mismatch")
    changes = tuple(
        _parse_conversation_change(item)
        for item in _list(changes_value, "changed-conversation changes")
    )
    _validate_page_size(changes, max_changes, "changed-conversation page")
    _validate_strict_order((item.order for item in changes), "changed-conversation page")
    return ChangedConversationsPage(snapshot_id, next_cursor, changes)


def _parse_deal_change(value: JsonValue) -> DealChange:
    payload = _mapping(value, "changed-deal item")
    _require_exact_keys(
        payload,
        {"change_version", "deal_id", "event_id", "kind", "revision", "category_id", "payload"},
        "changed-deal item",
    )
    kind = _change_kind(payload["kind"], "changed-deal item")
    detail = _optional_mapping(payload["payload"], "changed-deal payload")
    category_id = _optional_text(payload["category_id"], "changed-deal category_id")
    _validate_change_body(kind, detail, category_id, "changed-deal item")
    return DealChange(
        order=DealChangeOrder(
            _positive_int(payload["change_version"], "changed-deal change_version"),
            _positive_int(payload["deal_id"], "changed-deal deal_id"),
            _text(payload["event_id"], "changed-deal event_id"),
        ),
        kind=kind,
        revision=_text(payload["revision"], "changed-deal revision"),
        category_id=category_id,
        payload=detail,
    )


def _parse_conversation_change(value: JsonValue) -> ConversationChange:
    payload = _mapping(value, "changed-conversation item")
    _require_exact_keys(
        payload,
        {"change_version", "chat_id", "event_id", "kind", "revision", "message_cursor", "payload"},
        "changed-conversation item",
    )
    kind = _change_kind(payload["kind"], "changed-conversation item")
    detail = _optional_mapping(payload["payload"], "changed-conversation payload")
    message_cursor = _optional_text(
        payload["message_cursor"], "changed-conversation message_cursor"
    )
    _validate_change_body(kind, detail, message_cursor, "changed-conversation item")
    return ConversationChange(
        order=ConversationChangeOrder(
            _positive_int(payload["change_version"], "changed-conversation change_version"),
            _positive_int(payload["chat_id"], "changed-conversation chat_id"),
            _text(payload["event_id"], "changed-conversation event_id"),
        ),
        kind=kind,
        revision=_text(payload["revision"], "changed-conversation revision"),
        message_cursor=message_cursor,
        payload=detail,
    )


def _page_mapping(value: JsonValue, name: str) -> dict[str, JsonValue]:
    payload = _mapping(value, name)
    _require_exact_keys(payload, {"snapshot_id", "changes", "has_more", "next_cursor"}, name)
    return payload


def _page_fields(payload: dict[str, JsonValue], name: str) -> tuple[str, str | None, JsonValue]:
    has_more = payload["has_more"]
    if not isinstance(has_more, bool):
        raise BitrixBoundedContractError(f"{name} has_more must be boolean")
    next_cursor = _optional_text(payload["next_cursor"], f"{name} next_cursor")
    if has_more != (next_cursor is not None):
        raise BitrixBoundedContractError(f"{name} continuation fields disagree")
    return _text(payload["snapshot_id"], f"{name} snapshot_id"), next_cursor, payload["changes"]


def _validate_change_body(
    kind: DealChangeKind | ConversationChangeKind,
    payload: dict[str, JsonValue] | None,
    companion_value: str | None,
    name: str,
) -> None:
    """Require a tombstone to be bare and an upsert to be fully pinned.

    ``companion_value`` is the change's non-``payload`` positional field:
    ``category_id`` for a deal and ``message_cursor`` for a conversation. Both a
    tombstone's ``payload`` and its companion field must be absent, because a
    tombstone reports only the removal, never the removed data. ``revision`` is
    validated separately and is required in both cases.
    """
    if kind == "tombstone":
        if payload is not None or companion_value is not None:
            raise BitrixBoundedContractError(f"{name} tombstone must not include current data")
        return
    if payload is None or companion_value is None:
        raise BitrixBoundedContractError(f"{name} upsert omitted its pinned current data")


def _change_kind(value: JsonValue, name: str) -> DealChangeKind:
    if value == "upsert":
        return "upsert"
    if value == "tombstone":
        return "tombstone"
    raise BitrixBoundedContractError(f"{name} has an invalid kind")


def _validate_page_size(items: tuple[object, ...], max_items: int, name: str) -> None:
    if max_items < 1:
        raise ValueError("maximum bounded page size must be positive")
    if len(items) > max_items:
        raise BitrixBoundedContractError(f"{name} exceeded its declared page size")


def _validate_strict_order(items: Iterable[object], name: str) -> None:
    previous: object | None = None
    for item in items:
        if previous is not None and item <= previous:  # type: ignore[operator]
            raise BitrixBoundedContractError(f"{name} order did not strictly advance")
        previous = item


def _mapping(value: JsonValue, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise BitrixBoundedContractError(f"{name} must be an object")
    return dict(value)


def _optional_mapping(value: JsonValue, name: str) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    return _mapping(value, name)


def _list(value: JsonValue, name: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise BitrixBoundedContractError(f"{name} must be a list")
    return list(value)


def _text_set(value: JsonValue, name: str) -> frozenset[str]:
    values = _list(value, name)
    parsed = frozenset(_text(item, name) for item in values)
    if len(parsed) != len(values):
        raise BitrixBoundedContractError(f"{name} contains duplicates")
    return parsed


def _text(value: JsonValue, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BitrixBoundedContractError(f"{name} must be a non-empty string")
    return value


def _optional_text(value: JsonValue, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _positive_int(value: JsonValue, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BitrixBoundedContractError(f"{name} must be a positive integer")
    return value


def _require_exact_keys(payload: dict[str, JsonValue], keys: set[str], name: str) -> None:
    if set(payload) != keys:
        raise BitrixBoundedContractError(f"{name} has unexpected or missing fields")
