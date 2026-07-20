"""Read-only Bitrix REST client for Open Lines ingestion."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime
from math import isfinite
from typing import cast

import httpx

from src.connectors.bitrix_openlines.models import (
    ChatReference,
    DialogMetadata,
    OpenLineConfig,
    OpenLineMessage,
)
from src.models import JsonValue


class BitrixOpenLinesClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        max_attempts: int,
        request_delay_seconds: float = 0.0,
        http: httpx.Client | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("Bitrix Open Lines API base URL is required")
        self._base_url = base_url.strip().rstrip("/")
        self._max_attempts = max_attempts
        self._request_delay_seconds = request_delay_seconds
        self._last_request_at = 0.0
        self._http = http or httpx.Client(timeout=timeout_seconds)

    def list_active_configs(self) -> list[OpenLineConfig]:
        result = self._call("imopenlines.config.list.get", {})
        if not isinstance(result, list):
            raise RuntimeError("Bitrix config list returned an invalid result")
        configs: list[OpenLineConfig] = []
        for item in result:
            if not isinstance(item, dict):
                raise RuntimeError("Bitrix config list contained an invalid item")
            config_id = item.get("ID")
            line_name = item.get("LINE_NAME")
            active = item.get("ACTIVE")
            if not isinstance(config_id, str) or not isinstance(line_name, str):
                raise RuntimeError("Bitrix config list omitted required fields")
            if active == "Y":
                configs.append(OpenLineConfig(config_id, line_name))
        return configs

    def get_dialog(self, chat_id: int) -> DialogMetadata:
        result = self._call("im.dialog.get", {"DIALOG_ID": f"chat{chat_id}"})
        if not isinstance(result, dict):
            raise RuntimeError("Bitrix dialog returned an invalid result")
        entity_link = result.get("entity_link")
        if not isinstance(entity_link, dict):
            raise RuntimeError("Bitrix dialog omitted its Open Lines origin")
        origin = entity_link.get("id")
        if not isinstance(origin, str):
            raise RuntimeError("Bitrix dialog omitted its Open Lines origin ID")
        parts = origin.split("|")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise RuntimeError("Bitrix dialog returned an invalid Open Lines origin ID")
        return DialogMetadata(chat_id, parts[1], parts[0])

    def iter_crm_chat_refs(self) -> list[ChatReference]:
        refs: list[ChatReference] = []
        start = 0
        while True:
            payload = self._request(
                "crm.activity.list",
                {
                    "filter": {"PROVIDER_ID": "IMOPENLINES_SESSION"},
                    "select": [
                        "ID",
                        "OWNER_TYPE_ID",
                        "OWNER_ID",
                        "PROVIDER_PARAMS",
                        "LAST_UPDATED",
                    ],
                    "order": {"ID": "ASC"},
                    "start": start,
                },
            )
            result = payload.get("result")
            if not isinstance(result, list):
                raise RuntimeError("Bitrix CRM activities returned an invalid result")
            refs.extend(self._crm_references(result))
            next_start = _numeric_chat_id(payload.get("next"))
            if next_start is None:
                return refs
            start = next_start
        return refs

    def _crm_references(self, result: list[JsonValue]) -> list[ChatReference]:
        refs: list[ChatReference] = []
        resolved_owners: set[tuple[str, int]] = set()
        for item in result:
            if not isinstance(item, dict):
                continue
            changed_at = _optional_datetime(item.get("LAST_UPDATED"))
            chat_id = _provider_chat_id(item.get("PROVIDER_PARAMS"))
            if chat_id is not None:
                refs.append(ChatReference(chat_id, changed_at, "crm_activity"))
                continue
            owner_type = _crm_owner_type(item.get("OWNER_TYPE_ID"))
            owner_id = _numeric_chat_id(item.get("OWNER_ID"))
            if owner_type is None or owner_id is None:
                continue
            owner = (owner_type, owner_id)
            if owner in resolved_owners:
                continue
            resolved_owners.add(owner)
            for resolved_chat_id in self._crm_chat_ids(owner_type, owner_id):
                refs.append(ChatReference(resolved_chat_id, changed_at, "crm_activity"))
        return refs

    def _crm_chat_ids(self, owner_type: str, owner_id: int) -> list[int]:
        result = self._call(
            "imopenlines.crm.chat.get",
            {
                "CRM_ENTITY_TYPE": owner_type,
                "CRM_ENTITY": owner_id,
                "ACTIVE_ONLY": "N",
            },
        )
        if not isinstance(result, list):
            raise RuntimeError("Bitrix CRM chat lookup returned an invalid result")
        chat_ids: list[int] = []
        for item in result:
            if isinstance(item, dict):
                chat_id = _numeric_chat_id(item.get("CHAT_ID"))
                if chat_id is not None:
                    chat_ids.append(chat_id)
        return chat_ids

    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        refs: list[ChatReference] = []
        offset = 0
        while True:
            result = self._call(
                "im.recent.list",
                {"LIMIT": page_size, "OFFSET": offset, "GET_ORIGINAL_TEXT": "Y"},
            )
            if isinstance(result, dict):
                raw_items = result.get("items")
                has_more = result.get("hasMore") is True
            else:
                raw_items = result
                has_more = isinstance(result, list) and len(result) >= page_size
            if not isinstance(raw_items, list):
                raise RuntimeError("Bitrix recent dialogs returned an invalid result")
            refs.extend(_recent_references(raw_items))
            if not has_more:
                return refs
            if not raw_items:
                raise RuntimeError("Bitrix recent-dialog pagination did not advance")
            offset += len(raw_items)

    def get_messages(self, chat_id: int) -> list[OpenLineMessage]:
        messages: dict[int, OpenLineMessage] = {}
        last_id: int | None = None
        while True:
            params: dict[str, JsonValue] = {"DIALOG_ID": f"chat{chat_id}", "LIMIT": 50}
            if last_id is not None:
                params["LAST_ID"] = last_id
            result = self._call("im.dialog.messages.get", params)
            page = _message_page(result)
            for message in page:
                messages[message.id] = message
            if len(page) < 50:
                return sorted(messages.values(), key=lambda item: (item.date, item.id))
            next_last_id = min(item.id for item in page)
            if next_last_id == last_id:
                raise RuntimeError("Bitrix message pagination did not advance")
            last_id = next_last_id

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, params: Mapping[str, JsonValue]) -> JsonValue:
        return self._request(method, params)["result"]

    def _request(self, method: str, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        for attempt in range(1, self._max_attempts + 1):
            try:
                elapsed = time.monotonic() - self._last_request_at
                if elapsed < self._request_delay_seconds:
                    time.sleep(self._request_delay_seconds - elapsed)
                response = self._http.post(f"{self._base_url}/{method}", json=dict(params))
                self._last_request_at = time.monotonic()
                response.raise_for_status()
                payload = cast(object, response.json())
                if not isinstance(payload, dict) or "result" not in payload:
                    raise RuntimeError(f"Bitrix method {method} returned an invalid envelope")
                return cast(dict[str, JsonValue], payload)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429 and exc.response.status_code < 500:
                    raise RuntimeError(
                        f"Bitrix method {method} failed with HTTP {exc.response.status_code}"
                    ) from None
                if attempt == self._max_attempts:
                    raise RuntimeError(f"Bitrix method {method} request failed") from None
                delay = _retry_delay(exc.response, attempt)
                time.sleep(delay)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == self._max_attempts:
                    raise RuntimeError(f"Bitrix method {method} request failed") from None
                time.sleep(min(2 ** (attempt - 1), 8))
        raise AssertionError("unreachable")


def _numeric_chat_id(raw: object) -> int | None:
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str):
        return None
    normalized = raw.removeprefix("chat")
    return int(normalized) if normalized.isdigit() else None


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    fallback = float(min(2 ** (attempt - 1), 8))
    if response.status_code != 429:
        return fallback
    raw = response.headers.get("Retry-After")
    if raw is None:
        return fallback
    try:
        delay = float(raw)
    except ValueError:
        return fallback
    return delay if isfinite(delay) and delay >= 0 else fallback


def _optional_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _provider_chat_id(raw: object) -> int | None:
    if not isinstance(raw, dict):
        return None
    chat_id = _numeric_chat_id(raw.get("CHAT_ID"))
    if chat_id is not None:
        return chat_id
    raw_bindings = raw.get("IM")
    if not isinstance(raw_bindings, list):
        return None
    for binding in raw_bindings:
        if isinstance(binding, dict):
            chat_id = _numeric_chat_id(binding.get("id"))
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


def _recent_references(result: list[JsonValue]) -> list[ChatReference]:
    refs: list[ChatReference] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "chat":
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
        chat_id = _numeric_chat_id(item.get("chat_id"))
        message = item.get("message")
        raw_date = item.get("date_update")
        if raw_date is None and isinstance(message, dict):
            raw_date = message.get("date")
        if chat_id is not None:
            refs.append(ChatReference(chat_id, _optional_datetime(raw_date), "recent_dialog"))
    return refs


def _message_page(result: JsonValue) -> list[OpenLineMessage]:
    if not isinstance(result, dict):
        raise RuntimeError("Bitrix messages returned an invalid result")
    raw_users = result.get("users")
    raw_messages = result.get("messages")
    if not isinstance(raw_users, list) or not isinstance(raw_messages, list):
        raise RuntimeError("Bitrix messages omitted messages or users")
    users: dict[int, tuple[str, bool]] = {}
    for raw_user in raw_users:
        if not isinstance(raw_user, dict):
            continue
        user_id = raw_user.get("id")
        name = raw_user.get("name")
        user_type = raw_user.get("type")
        if isinstance(user_id, int) and isinstance(name, str):
            users[user_id] = (name, user_type != "extranet")
    messages: list[OpenLineMessage] = []
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            raise RuntimeError("Bitrix messages contained an invalid message")
        message_id = raw_message.get("id")
        author_id = raw_message.get("author_id")
        text = raw_message.get("text")
        raw_date = raw_message.get("date")
        if (
            not isinstance(message_id, int)
            or not isinstance(author_id, int)
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
