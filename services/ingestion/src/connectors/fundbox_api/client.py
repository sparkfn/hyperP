"""Authenticated cursor client for Fundbox source-shaped ingestion records."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from math import isfinite
from urllib.parse import urlparse

import httpx

from src.bounded_ingestion_models import AttemptContext, SourceBackoffError
from src.connectors.fundbox_api.models import (
    MAX_CURSOR_LENGTH,
    MAX_SNAPSHOT_ID_LENGTH,
    BoundedIngestionPage,
    IngestionPage,
    validate_source_records,
)
from src.errors import SourceNotConfiguredError
from src.models import JsonValue

_RESOURCES: frozenset[str] = frozenset({"users", "contacts", "sales"})
_MAX_RETRY_DELAY_SECONDS = 60.0


def _declared_content_length(response: httpx.Response) -> int | None:
    """Return the response's declared body length when it is a plain integer."""
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


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


class FundboxCursorExpiredError(RuntimeError):
    """The persisted continuation cannot be resumed by the source."""


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

    def fetch_bounded_page(
        self,
        resource: str,
        *,
        snapshot_id: str,
        lower_change_version: int,
        upper_change_version: int,
        cursor: str | None,
        context: AttemptContext,
        max_bytes: int,
    ) -> BoundedIngestionPage:
        """Fetch exactly one frozen-window page without iterator prefetch.

        Retriable upstream responses become a durable bounded backoff. The
        runner, not this client, decides when another admitted attempt may run.
        The body is streamed and abandoned as soon as it passes ``max_bytes``,
        so an oversized upstream page is never buffered whole.
        """
        if resource not in _RESOURCES:
            raise ValueError(f"Unsupported Fundbox API resource: {resource!r}")
        if not snapshot_id.strip() or len(snapshot_id) > MAX_SNAPSHOT_ID_LENGTH:
            raise ValueError("Fundbox snapshot ID is missing or oversized")
        if lower_change_version < 0 or upper_change_version < lower_change_version:
            raise ValueError("Fundbox frozen change window is invalid")
        if cursor is not None and (not cursor.strip() or len(cursor) > MAX_CURSOR_LENGTH):
            raise ValueError("Fundbox continuation cursor is invalid or oversized")
        if max_bytes < 1:
            raise ValueError("Fundbox bounded response limit must be positive")
        self._assert_request_allowed(context)
        params: dict[str, str | int] = {
            "limit": self._credentials.page_size,
            "snapshot_id": snapshot_id,
            "after_change_version": lower_change_version,
            "through_change_version": upper_change_version,
        }
        if cursor is not None:
            params["cursor"] = cursor
        page, response_bytes = self._request_bounded(resource, params, context, max_bytes)
        if page.meta.snapshot_id != snapshot_id:
            raise ValueError("Fundbox response changed the frozen snapshot")
        if (
            page.meta.lower_change_version != lower_change_version
            or page.meta.upper_change_version != upper_change_version
        ):
            raise ValueError("Fundbox response changed the frozen change window")
        validated = []
        for change in page.data:
            if change.composite is None:
                validated.append(change)
                continue
            composite = validate_source_records(resource, [change.composite])[0]
            validated.append(change.model_copy(update={"composite": composite}))
        return page.model_copy(update={"data": validated, "response_bytes": response_bytes})

    def _request_bounded(
        self,
        resource: str,
        params: dict[str, str | int],
        context: AttemptContext,
        max_bytes: int,
    ) -> tuple[BoundedIngestionPage, int]:
        """Read one bounded response page, aborting an oversized streamed body."""
        url = f"{self._credentials.base_url.rstrip('/')}/hyperp/ingestion/{resource}"
        try:
            with self._http.stream(
                "GET",
                url,
                params=params,
                auth=(self._credentials.username, self._credentials.password),
            ) as response:
                self._raise_for_bounded_status(response)
                self._assert_request_allowed(context)
                declared = _declared_content_length(response)
                if declared is not None and declared > max_bytes:
                    raise ValueError("Fundbox bounded response exceeds byte limit")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError("Fundbox bounded response exceeds byte limit")
        except httpx.TransportError as exc:
            raise SourceBackoffError(
                datetime.now(UTC) + timedelta(seconds=self._exponential_delay(1)),
                "fundbox_transport_unavailable",
            ) from exc
        return BoundedIngestionPage.model_validate(json.loads(body)), len(body)

    @staticmethod
    def _raise_for_bounded_status(response: httpx.Response) -> None:
        if response.status_code == 410:
            raise FundboxCursorExpiredError("fundbox_cursor_expired")
        if response.status_code in {401, 403}:
            raise PermissionError("fundbox_auth_or_scope_rejected")
        if response.status_code == 429 or response.status_code >= 500:
            raise SourceBackoffError(
                datetime.now(UTC) + timedelta(seconds=FundboxApiClient._retry_delay(response, 1)),
                "fundbox_source_backoff",
            )
        response.raise_for_status()

    @staticmethod
    def _assert_request_allowed(context: AttemptContext) -> None:
        cancellation = context.cancellation
        if cancellation is not None and cancellation.requested():
            raise TimeoutError("Fundbox bounded request cancelled")
        context.require_operation_budget(datetime.now(UTC), 0.001)

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
