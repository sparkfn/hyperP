"""Read-only Bitrix REST client for Open Lines ingestion."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import cast

import httpx

from src.connectors.bitrix_openlines.models import (
    ChatReference,
    DialogMetadata,
    OpenLineConfig,
    OpenLineMessage,
)
from src.connectors.bitrix_openlines.response_helpers import (
    RETRYABLE_ERRORS,
    activity_ids,
    deduplicate_references,
    envelope_retry_delay,
    history_message_page,
    message_page,
    next_start,
    numeric_chat_id,
    optional_datetime,
    owner_reference,
    provider_chat_id,
    provider_references,
    recent_references,
    retry_delay,
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
        configs: list[OpenLineConfig] = []
        start = 0
        while True:
            payload = self._request("imopenlines.config.list.get", {"start": start})
            result = payload.get("result")
            if not isinstance(result, list):
                raise RuntimeError("Bitrix config list returned an invalid result")
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
            next_page = next_start(payload, start)
            if next_page is None:
                return configs
            start = next_page

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
        chat_ids_by_owner: dict[tuple[str, int], list[int]] = {}
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
            refs.extend(self._crm_references(result, chat_ids_by_owner))
            next_page = next_start(payload, start)
            if next_page is None:
                return deduplicate_references(refs)
            start = next_page

    def _crm_references(
        self,
        result: list[JsonValue],
        chat_ids_by_owner: dict[tuple[str, int], list[int]],
    ) -> list[ChatReference]:
        refs: list[ChatReference] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            changed_at = optional_datetime(item.get("LAST_UPDATED"))
            activity_id_values = activity_ids(item.get("ID"))
            owner = owner_reference(item)
            owner_references = () if owner is None else (owner,)
            provider_reference_values = provider_references(item.get("PROVIDER_PARAMS"))
            chat_id = provider_chat_id(item.get("PROVIDER_PARAMS"))
            if chat_id is not None:
                refs.append(
                    ChatReference(
                        chat_id,
                        changed_at,
                        "crm_activity",
                        activity_id_values,
                        owner_references,
                        provider_reference_values,
                    )
                )
                continue
            if owner is None:
                continue
            owner_key = (owner.owner_type, owner.owner_id)
            if owner_key not in chat_ids_by_owner:
                chat_ids_by_owner[owner_key] = self._crm_chat_ids(*owner_key)
            for resolved_chat_id in chat_ids_by_owner[owner_key]:
                refs.append(
                    ChatReference(
                        resolved_chat_id,
                        changed_at,
                        "crm_activity",
                        activity_id_values,
                        owner_references,
                        provider_reference_values,
                    )
                )
        return refs

    def _crm_chat_ids(self, owner_type: str, owner_id: int) -> list[int]:
        chat_ids: list[int] = []
        start = 0
        while True:
            payload = self._request(
                "imopenlines.crm.chat.get",
                {
                    "CRM_ENTITY_TYPE": owner_type,
                    "CRM_ENTITY": owner_id,
                    "ACTIVE_ONLY": "N",
                    "start": start,
                },
            )
            result = payload.get("result")
            if not isinstance(result, list):
                raise RuntimeError("Bitrix CRM chat lookup returned an invalid result")
            for item in result:
                if isinstance(item, dict):
                    chat_id = numeric_chat_id(item.get("CHAT_ID"))
                    if chat_id is not None:
                        chat_ids.append(chat_id)
            next_page = next_start(payload, start)
            if next_page is None:
                return chat_ids
            start = next_page

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
            refs.extend(recent_references(raw_items))
            if not has_more:
                return refs
            if not raw_items:
                raise RuntimeError("Bitrix recent-dialog pagination did not advance")
            offset += page_size

    def get_messages(self, chat_id: int) -> list[OpenLineMessage]:
        return self._get_message_collection(
            "im.dialog.messages.get",
            {"DIALOG_ID": f"chat{chat_id}"},
        )

    def get_history(self, chat_id: int) -> list[OpenLineMessage]:
        """Return history for a CRM-only Open Lines chat using its numeric ID."""
        result = self._call("imopenlines.session.history.get", {"CHAT_ID": chat_id})
        return sorted(
            history_message_page(result),
            key=lambda item: (item.date, item.id),
        )

    def _get_message_collection(
        self,
        method: str,
        base_params: Mapping[str, JsonValue],
    ) -> list[OpenLineMessage]:
        messages: dict[int, OpenLineMessage] = {}
        last_id: int | None = None
        while True:
            params = dict(base_params)
            params["LIMIT"] = 50
            if last_id is not None:
                params["LAST_ID"] = last_id
            result = self._call(method, params)
            page = message_page(result)
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
                if not isinstance(payload, dict):
                    raise RuntimeError(f"Bitrix method {method} returned an invalid envelope")
                typed_payload = cast(dict[str, JsonValue], payload)
                error = typed_payload.get("error")
                if isinstance(error, str):
                    if error not in RETRYABLE_ERRORS:
                        raise RuntimeError(f"Bitrix method {method} failed with {error}")
                    if attempt == self._max_attempts:
                        raise RuntimeError(f"Bitrix method {method} failed with {error}")
                    time.sleep(envelope_retry_delay(typed_payload, attempt))
                    continue
                if "result" not in typed_payload:
                    raise RuntimeError(f"Bitrix method {method} returned an invalid envelope")
                return typed_payload
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429 and exc.response.status_code < 500:
                    raise RuntimeError(
                        f"Bitrix method {method} failed with HTTP {exc.response.status_code}"
                    ) from None
                if attempt == self._max_attempts:
                    raise RuntimeError(f"Bitrix method {method} request failed") from None
                delay = retry_delay(exc.response, attempt)
                time.sleep(delay)
            except httpx.TransportError:
                if attempt == self._max_attempts:
                    raise RuntimeError(f"Bitrix method {method} request failed") from None
                time.sleep(min(2 ** (attempt - 1), 8))
        raise AssertionError("unreachable")
