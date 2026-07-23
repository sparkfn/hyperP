"""Pure response parsing and retry helpers for the Bitrix Open Lines client."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite

import httpx

from src.connectors.bitrix_openlines.models import (
    ChatReference,
    CrmOwnerReference,
    OpenLineMessage,
    merge_chat_references,
)
from src.models import JsonValue

RETRYABLE_ERRORS = frozenset(
    {
        "ERROR_CORE",
        "ERROR_NETWORK",
        "INTERNAL_SERVER_ERROR",
        "OPERATION_TIME_LIMIT",
        "QUERY_LIMIT_EXCEEDED",
        "RATE_LIMIT_EXCEEDED",
        "TEMPORARILY_UNAVAILABLE",
        "TOO_MANY_REQUESTS",
    }
)
_MAX_UPSTREAM_RETRY_SECONDS = 300.0


@dataclass(frozen=True)
class OpenLineOrigin:
    config_id: str | None
    connector_id: str | None


def openline_origin_id(raw: object) -> OpenLineOrigin:
    """Parse a ``connector_id|config_id`` Open Lines origin, if present."""
    if not isinstance(raw, str) or "|" not in raw:
        return OpenLineOrigin(None, None)
    parts = raw.split("|")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return OpenLineOrigin(None, None)
    return OpenLineOrigin(parts[1], parts[0])


def numeric_chat_id(raw: object) -> int | None:
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str):
        return None
    normalized = raw.removeprefix("chat")
    return int(normalized) if normalized.isdigit() else None


def next_start(payload: Mapping[str, JsonValue], current: int) -> int | None:
    next_value = numeric_chat_id(payload.get("next"))
    if next_value is None:
        return None
    if next_value <= current:
        raise RuntimeError("Bitrix collection pagination did not advance")
    return next_value


def deduplicate_references(refs: list[ChatReference]) -> list[ChatReference]:
    merged: dict[int, ChatReference] = {}
    for item in refs:
        prior = merged.get(item.chat_id)
        if prior is None:
            merged[item.chat_id] = item
        else:
            merged[item.chat_id] = merge_chat_references(prior, item)
    return [merged[chat_id] for chat_id in sorted(merged)]


def activity_ids(raw: object) -> tuple[str, ...]:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return (str(raw),)
    if isinstance(raw, str) and raw:
        return (raw,)
    return ()


def owner_reference(item: Mapping[str, JsonValue]) -> CrmOwnerReference | None:
    owner_type = _crm_owner_type(item.get("OWNER_TYPE_ID"))
    owner_id = numeric_chat_id(item.get("OWNER_ID"))
    if owner_type is None or owner_id is None:
        return None
    return CrmOwnerReference(owner_type, owner_id)


def provider_references(raw: object) -> tuple[dict[str, JsonValue], ...]:
    if not isinstance(raw, dict) or not raw:
        return ()
    reference: dict[str, JsonValue] = {}
    for key in ("CHAT_ID", "USER_CODE"):
        value = raw.get(key)
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            reference[key] = value
    raw_bindings = raw.get("IM")
    if isinstance(raw_bindings, list):
        bindings: list[JsonValue] = []
        for raw_binding in raw_bindings:
            if not isinstance(raw_binding, dict):
                continue
            binding_id = raw_binding.get("id")
            if isinstance(binding_id, (int, str)) and not isinstance(binding_id, bool):
                bindings.append({"id": binding_id})
        if bindings:
            reference["IM"] = bindings
    return (reference,) if reference else ()


def retry_delay(response: httpx.Response, attempt: int) -> float:
    fallback = float(min(2 ** (attempt - 1), 8))
    raw = response.headers.get("Retry-After")
    numeric_delay = _safe_delay(raw, -1.0)
    if numeric_delay >= 0:
        return min(numeric_delay, _MAX_UPSTREAM_RETRY_SECONDS)
    if raw is None:
        return fallback
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return fallback
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    date_delay = max(0.0, retry_at.timestamp() - time.time())
    return min(date_delay, _MAX_UPSTREAM_RETRY_SECONDS)


def envelope_retry_delay(payload: Mapping[str, JsonValue], attempt: int) -> float:
    fallback = float(min(2 ** (attempt - 1), 8))
    for key in ("retry_after", "retryAfter"):
        delay = _safe_delay(payload.get(key), -1.0)
        if delay >= 0:
            return min(delay, _MAX_UPSTREAM_RETRY_SECONDS)
    timing = payload.get("time")
    if isinstance(timing, dict):
        delay = _safe_delay(timing.get("retry_after"), -1.0)
        if delay >= 0:
            return min(delay, _MAX_UPSTREAM_RETRY_SECONDS)
        reset_at = _safe_delay(timing.get("operating_reset_at"), -1.0)
        if reset_at >= 0:
            return min(max(0.0, reset_at - time.time()), _MAX_UPSTREAM_RETRY_SECONDS)
    return fallback


def _safe_delay(raw: object, fallback: float) -> float:
    if isinstance(raw, bool):
        return fallback
    try:
        delay = float(raw) if isinstance(raw, (int, float, str)) else fallback
    except ValueError:
        return fallback
    return delay if isfinite(delay) and delay >= 0 else fallback


def optional_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def provider_chat_id(raw: object) -> int | None:
    if not isinstance(raw, dict):
        return None
    chat_id = numeric_chat_id(raw.get("CHAT_ID"))
    if chat_id is not None:
        return chat_id
    raw_bindings = raw.get("IM")
    if not isinstance(raw_bindings, list):
        return None
    for binding in raw_bindings:
        if isinstance(binding, dict):
            chat_id = numeric_chat_id(binding.get("id"))
            if chat_id is not None:
                return chat_id
    return None


def _crm_owner_type(raw: object) -> str | None:
    return {
        "1": "lead",
        "2": "deal",
        "3": "contact",
        "4": "company",
    }.get(str(raw))


def recent_references(result: list[JsonValue]) -> list[ChatReference]:
    refs: list[ChatReference] = []
    for item in result:
        if not isinstance(item, dict) or item.get("type") != "chat":
            continue
        chat = item.get("chat")
        if not isinstance(chat, dict):
            continue
        entity_link = chat.get("entity_link")
        entity_type = chat.get("entity_type")
        origin_id = entity_link.get("id") if isinstance(entity_link, dict) else None
        has_openline_origin = isinstance(origin_id, str) and "|" in origin_id
        if entity_type != "LINES" and not has_openline_origin:
            continue
        openline_origin = openline_origin_id(origin_id)
        chat_id = numeric_chat_id(item.get("chat_id"))
        message = item.get("message")
        raw_date = item.get("date_update")
        if raw_date is None and isinstance(message, dict):
            raw_date = message.get("date")
        if chat_id is not None:
            refs.append(
                ChatReference(
                    chat_id,
                    optional_datetime(raw_date),
                    "recent_dialog",
                    config_id=openline_origin.config_id,
                    connector_id=openline_origin.connector_id,
                )
            )
    return refs


def message_page(result: JsonValue) -> list[OpenLineMessage]:
    if not isinstance(result, dict):
        raise RuntimeError("Bitrix messages returned an invalid result")
    raw_users = result.get("users")
    raw_messages = result.get("messages")
    if not isinstance(raw_users, list) or not isinstance(raw_messages, list):
        raise RuntimeError("Bitrix messages omitted messages or users")
    return _parse_message_items(raw_messages, raw_users)


def history_message_page(result: JsonValue) -> list[OpenLineMessage]:
    if not isinstance(result, dict):
        raise RuntimeError("Bitrix Open Lines history returned an invalid result")
    raw_users = result.get("users")
    raw_messages = result.get("message")
    if not isinstance(raw_users, dict) or not isinstance(raw_messages, dict):
        raise RuntimeError("Bitrix Open Lines history omitted message or users")
    return _parse_message_items(
        _mapping_items(raw_messages),
        _mapping_items(raw_users),
        author_id_key="senderid",
    )


def _mapping_items(raw: Mapping[str, JsonValue]) -> list[JsonValue]:
    if "id" in raw:
        return [dict(raw)]
    items: list[JsonValue] = []
    for raw_id, value in raw.items():
        if not isinstance(value, dict):
            items.append(value)
            continue
        normalized = dict(value)
        normalized.setdefault("id", raw_id)
        items.append(normalized)
    return items


def _parse_message_items(
    raw_messages: list[JsonValue],
    raw_users: list[JsonValue],
    *,
    author_id_key: str = "author_id",
) -> list[OpenLineMessage]:
    users: dict[int, tuple[str, bool]] = {}
    for raw_user in raw_users:
        if not isinstance(raw_user, dict):
            continue
        user_id = _required_int(raw_user.get("id"))
        name = raw_user.get("name")
        user_type = raw_user.get("type")
        if user_id is not None and isinstance(name, str):
            users[user_id] = (name, user_type not in {"connector", "extranet"})
    messages: list[OpenLineMessage] = []
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            raise RuntimeError("Bitrix messages contained an invalid message")
        message_id = _required_int(raw_message.get("id"))
        author_id = _required_int(raw_message.get(author_id_key))
        text = raw_message.get("text")
        raw_date = raw_message.get("date")
        if (
            message_id is None
            or author_id is None
            or not isinstance(text, str)
            or not isinstance(raw_date, str)
        ):
            raise RuntimeError("Bitrix message omitted required fields")
        if author_id == 0:
            author_name, is_agent = "System", True
        elif author_id not in users:
            raise RuntimeError("Bitrix message author was absent from the users payload")
        else:
            author_name, is_agent = users[author_id]
        messages.append(
            OpenLineMessage(
                message_id,
                author_id,
                author_name,
                text,
                datetime.fromisoformat(raw_date.replace("Z", "+00:00")),
                is_agent,
            )
        )
    return messages


def _required_int(raw: object) -> int | None:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None
