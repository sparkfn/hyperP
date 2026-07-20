"""Authenticated cursor client for Fundbox source-shaped ingestion records."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from urllib.parse import urlparse

import httpx

from src.connectors.fundbox_api.models import (
    IngestionPage,
    validate_source_records,
)
from src.errors import SourceNotConfiguredError
from src.models import JsonValue

_RESOURCES: frozenset[str] = frozenset({"users", "contacts", "sales"})
_MAX_RETRY_DELAY_SECONDS = 60.0


@dataclass(frozen=True)
class FundboxApiCredentials:
    base_url: str
    username: str
    password: str
    page_size: int

    def __post_init__(self) -> None:
        # Empty config is a normal pre-provisioning state, not a startup error:
        # reject it here (at dispatch time) with an actionable message so the
        # Celery task can log a clean warning and reject the run rather than
        # crash-looping. ``SourceNotConfiguredError`` is caught specifically by
        # the ingestion task and logged at WARNING (no traceback).
        if not self.base_url.strip() or not self.username.strip() or not self.password.strip():
            raise SourceNotConfiguredError(
                "Fundbox API ingestion is not configured: set FUNDBOX_API_BASE_URL, "
                "FUNDBOX_API_USERNAME and FUNDBOX_API_PASSWORD before dispatching "
                "fundbox API ingestion."
            )
        if not self.base_url.startswith("https://"):
            raise SourceNotConfiguredError("Fundbox API base URL must use HTTPS")
        if not urlparse(self.base_url).netloc:
            raise SourceNotConfiguredError("Fundbox API base URL is missing a host")


class FundboxApiClient:
    def __init__(
        self,
        credentials: FundboxApiCredentials,
        *,
        http: httpx.Client | None = None,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._credentials = credentials
        self._http = http or httpx.Client(timeout=30.0)
        self._max_attempts = max_attempts
        self._sleeper = sleeper

    def iter_source(
        self,
        resource: str,
        *,
        updated_since: str | None = None,
    ) -> Iterator[dict[str, JsonValue]]:
        if resource not in _RESOURCES:
            raise ValueError(f"Unsupported Fundbox API resource: {resource!r}")
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, str | int] = {"limit": self._credentials.page_size}
            if cursor is not None:
                params["cursor"] = cursor
            elif updated_since is not None:
                params["updated_since"] = updated_since
            response = self._request(resource, params)
            page = IngestionPage.model_validate(response.json())
            records = validate_source_records(resource, page.data)
            next_cursor = page.meta.next_cursor
            if page.meta.has_more:
                if next_cursor is None:
                    raise RuntimeError("Fundbox API pagination metadata is inconsistent")
                if next_cursor in seen_cursors:
                    raise ValueError("Fundbox API returned a repeated cursor")
                seen_cursors.add(next_cursor)
            yield from records
            if not page.meta.has_more:
                break
            cursor = next_cursor

    def close(self) -> None:
        self._http.close()

    def _request(
        self,
        resource: str,
        params: dict[str, str | int],
    ) -> httpx.Response:
        url = f"{self._credentials.base_url.rstrip('/')}/hyperp/ingestion/{resource}"
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._http.get(
                    url,
                    params=params,
                    auth=(self._credentials.username, self._credentials.password),
                )
            except httpx.TransportError:
                if attempt == self._max_attempts:
                    raise
                self._sleeper(self._exponential_delay(attempt))
                continue
            else:
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or attempt == self._max_attempts:
                    response.raise_for_status()
                    return response
            self._sleeper(self._retry_delay(response, attempt))
        raise RuntimeError("Fundbox API retry loop exhausted")

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(retry_after)
                except (TypeError, ValueError):
                    pass
                else:
                    if parsed.tzinfo is None:
                        return FundboxApiClient._exponential_delay(attempt)
                    retry_at = parsed.astimezone(UTC)
                    delay = (retry_at - datetime.now(UTC)).total_seconds()
                    if isfinite(delay):
                        return min(_MAX_RETRY_DELAY_SECONDS, max(0.0, delay))
            else:
                if isfinite(delay):
                    return min(_MAX_RETRY_DELAY_SECONDS, max(0.0, delay))
        return FundboxApiClient._exponential_delay(attempt)

    @staticmethod
    def _exponential_delay(attempt: int) -> float:
        return min(_MAX_RETRY_DELAY_SECONDS, float(2 ** (attempt - 1)))
