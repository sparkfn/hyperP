"""Direct proposed-proxy client for fixture-backed bounded Bitrix ingestion."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
from pydantic.types import JsonValue

from src.bounded_ingestion_models import SourceBackoffError
from src.connectors.bitrix_openlines.bounded_contract import (
    CAPABILITY_ENDPOINT,
    CHANGED_CONVERSATIONS_ENDPOINT,
    CHANGED_DEALS_ENDPOINT,
    BitrixBoundedContractError,
    CapabilitySnapshot,
    ChangedConversationsPage,
    ChangedDealsPage,
    parse_capability_snapshot,
    parse_changed_conversations_page,
    parse_changed_deals_page,
)


class BitrixBoundedClient:
    """Call only the proposed direct proxy contract; never CRM activity endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        max_response_bytes: int,
        http: httpx.Client | None = None,
    ) -> None:
        if not base_url.strip() or timeout_seconds <= 0 or max_response_bytes < 1:
            raise ValueError("Bitrix bounded client configuration is invalid")
        self._base_url = base_url.rstrip("/")
        self._max_response_bytes = max_response_bytes
        self._http = http or httpx.Client(timeout=timeout_seconds)
        self._request_count = 0
        self._last_response_bytes = 0

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def last_response_bytes(self) -> int:
        return self._last_response_bytes

    def negotiate_capability(self, *, source_scope: str, snapshot_id: str) -> CapabilitySnapshot:
        response = self._post(
            CAPABILITY_ENDPOINT,
            {"source_scope": source_scope, "snapshot_id": snapshot_id},
        )
        return parse_capability_snapshot(response)

    def changed_deals(
        self,
        *,
        source_scope: str,
        snapshot_id: str,
        cursor: str | None,
        max_changes: int,
    ) -> ChangedDealsPage:
        response = self._post(
            CHANGED_DEALS_ENDPOINT,
            {
                "source_scope": source_scope,
                "snapshot_id": snapshot_id,
                "cursor": cursor,
                "limit": max_changes,
            },
        )
        return parse_changed_deals_page(
            response,
            expected_snapshot_id=snapshot_id,
            max_changes=max_changes,
        )

    def changed_conversations(
        self,
        *,
        source_scope: str,
        snapshot_id: str,
        cursor: str | None,
        max_changes: int,
    ) -> ChangedConversationsPage:
        response = self._post(
            CHANGED_CONVERSATIONS_ENDPOINT,
            {
                "source_scope": source_scope,
                "snapshot_id": snapshot_id,
                "cursor": cursor,
                "limit": max_changes,
            },
        )
        return parse_changed_conversations_page(
            response,
            expected_snapshot_id=snapshot_id,
            max_changes=max_changes,
        )

    def close(self) -> None:
        self._http.close()

    def _post(self, endpoint: str, body: dict[str, JsonValue]) -> JsonValue:
        if endpoint not in {
            CAPABILITY_ENDPOINT,
            CHANGED_DEALS_ENDPOINT,
            CHANGED_CONVERSATIONS_ENDPOINT,
        }:
            raise BitrixBoundedContractError("Bitrix bounded endpoint is not approved")
        self._request_count += 1
        with self._http.stream("POST", f"{self._base_url}/{endpoint}", json=body) as response:
            if response.status_code == 429:
                raise SourceBackoffError(
                    _retry_at(response), "Bitrix bounded source requested backoff"
                )
            if response.status_code >= 400:
                raise BitrixBoundedContractError("Bitrix bounded source request failed")
            content_length = response.headers.get("Content-Length")
            if content_length is not None and _content_length_exceeds(
                content_length, self._max_response_bytes
            ):
                raise BitrixBoundedContractError("Bitrix bounded response exceeded byte limit")
            body_bytes = bytearray()
            for chunk in response.iter_bytes():
                body_bytes.extend(chunk)
                if len(body_bytes) > self._max_response_bytes:
                    raise BitrixBoundedContractError("Bitrix bounded response exceeded byte limit")
        self._last_response_bytes = len(body_bytes)
        try:
            decoded = json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BitrixBoundedContractError("Bitrix bounded response was not valid JSON") from exc
        return cast(JsonValue, decoded)


def _retry_at(response: httpx.Response) -> datetime:
    raw = response.headers.get("Retry-After")
    try:
        seconds = float(raw) if raw is not None else 0.0
    except ValueError:
        seconds = 0.0
    return datetime.now(UTC) + timedelta(seconds=max(seconds, 0.0))


def _content_length_exceeds(value: str, maximum: int) -> bool:
    try:
        return int(value) > maximum
    except ValueError:
        return True
