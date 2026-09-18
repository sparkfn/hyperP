"""Authenticated, retrying client for PHPPOS HyperP extraction endpoints."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from json import JSONDecodeError, loads

import httpx
from pydantic import BaseModel, ConfigDict

from src.bounded_ingestion_models import AttemptContext, SourceBackoffError, Usage
from src.connectors.phppos_api.models import (
    BoundedPage,
    BoundedWindow,
    CustomerPage,
    CustomerRow,
    SaleRow,
    SalesPage,
)


@dataclass(frozen=True)
class ApiCredentials:
    base_url: str
    client_id: str
    client_secret: str
    tenant_id: str
    page_size: int
    scopes: tuple[str, ...]
    principal_tenant_id: str | None = None


@dataclass
class BoundedRequestBudget:
    """Adapter-local accounting for every bounded OAuth and source request."""

    max_requests: int
    max_rows: int
    max_bytes: int
    usage: Usage = Usage()

    def remaining_requests(self) -> int:
        return max(0, self.max_requests - self.usage.source_requests)

    def reserve_request(self) -> None:
        if self.remaining_requests() < 1:
            raise PhpposBoundedTransportError("bounded PHPPOS request allowance exhausted")
        self.usage = self.usage.add(Usage(source_requests=1))

    def reserve_bytes(self, amount: int) -> None:
        if amount < 0:
            raise PhpposBoundedTransportError("bounded PHPPOS byte accounting is invalid")
        self.usage = self.usage.add(Usage(bytes_read=amount))
        if self.usage.bytes_read > self.max_bytes:
            raise PhpposBoundedTransportError("bounded PHPPOS response exceeds byte allowance")

    def record_page(self, rows: int) -> None:
        self.usage = self.usage.add(Usage(records=rows, pages=1))
        if self.usage.records > self.max_rows:
            raise PhpposBoundedTransportError("bounded PHPPOS page exceeds adapter limits")


@dataclass(frozen=True)
class BoundedPageResult:
    page: BoundedPage
    usage: Usage


class PhpposBoundedTransportError(RuntimeError):
    """Sanitized bounded transport failure with no source payload or token details."""


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: str
    expires_in: int


class PhpposApiClient:
    def __init__(
        self,
        credentials: ApiCredentials,
        *,
        http: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        wall_clock: Callable[[], datetime] | None = None,
        max_attempts: int = 3,
    ) -> None:
        self._credentials = credentials
        self._http = http or httpx.Client(timeout=30.0)
        self._sleeper = sleeper
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._max_attempts = max_attempts
        self._access_token: str | None = None
        self._access_expires_at = 0.0
        self._closed = False

    def iter_customers(self, *, updated_since: str | None = None) -> Iterator[CustomerRow]:
        cursor: str | None = None
        while True:
            page = CustomerPage.model_validate(self._get_page("customers", cursor, updated_since))
            yield from page.data
            if not page.pagination.has_more:
                return
            cursor = page.pagination.next_cursor

    def iter_sales(self, *, updated_since: str | None = None) -> Iterator[SaleRow]:
        cursor: str | None = None
        while True:
            page = SalesPage.model_validate(self._get_page("sales", cursor, updated_since))
            yield from page.data
            if not page.pagination.has_more:
                return
            cursor = page.pagination.next_cursor

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._http.close()

    @property
    def page_size(self) -> int:
        """Return the page capacity this client requests and admits per page."""
        return self._credentials.page_size

    def fetch_bounded_page(
        self,
        resource: str,
        *,
        cursor: str | None,
        window: BoundedWindow,
        context: AttemptContext,
        budget: BoundedRequestBudget,
    ) -> BoundedPageResult:
        """Fetch exactly one source-bound page without invoking legacy traversal."""
        if resource not in {"customers", "sales"}:
            raise PhpposBoundedTransportError("bounded PHPPOS resource is unsupported")
        if self._credentials.principal_tenant_id != self._credentials.tenant_id:
            raise PhpposBoundedTransportError("independent PHPPOS tenant principal is unavailable")
        self._require_operation(context, 0.0)
        params: dict[str, str | int] = {
            "limit": self._credentials.page_size,
            "snapshot_id": window.snapshot_id,
            "upper_change_version": window.upper_change_version,
        }
        if cursor is not None:
            params["cursor"] = cursor
        response_bytes = self._bounded_request(
            "GET",
            f"/api/v1/custom/hyperp/{resource}",
            params=params,
            context=context,
            budget=budget,
        )
        try:
            payload = loads(response_bytes)
        except (JSONDecodeError, UnicodeDecodeError) as exc:
            raise PhpposBoundedTransportError("bounded PHPPOS response is malformed") from exc
        if not isinstance(payload, dict):
            raise PhpposBoundedTransportError("bounded PHPPOS response is malformed")
        try:
            page = BoundedPage.model_validate(payload)
        except ValueError as exc:
            raise PhpposBoundedTransportError(
                "bounded PHPPOS response violates the contract"
            ) from exc
        if page.tenant_id != self._credentials.tenant_id or page.resource != resource:
            raise PhpposBoundedTransportError("bounded PHPPOS response crossed tenant or resource")
        if page.window != window:
            raise PhpposBoundedTransportError("bounded PHPPOS response changed its frozen window")
        budget.record_page(rows=len(page.data))
        return BoundedPageResult(page, budget.usage)

    def _get_page(self, resource: str, cursor: str | None, updated_since: str | None) -> object:
        params: dict[str, str | int] = {"limit": self._credentials.page_size}
        if cursor is not None:
            params["cursor"] = cursor
        if updated_since is not None:
            params["updated_since"] = updated_since
        response = self._request(
            "GET",
            f"/api/v1/custom/hyperp/{resource}",
            params=params,
        )
        return response.json()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int],
    ) -> httpx.Response:
        retried_unauthorized = False
        transient_attempts = 0
        while transient_attempts < self._max_attempts:
            token = self._access_token_value()
            try:
                response = self._http.request(
                    method,
                    f"{self._credentials.base_url.rstrip('/')}{path}",
                    headers={
                        "authorization": f"Bearer {token}",
                        "x-pos-tenant-id": self._credentials.tenant_id,
                    },
                    params=params,
                )
            except httpx.TransportError:
                transient_attempts += 1
                if transient_attempts >= self._max_attempts:
                    raise
                self._sleeper(float(2 ** (transient_attempts - 1)))
                continue
            if response.status_code == 401:
                self._invalidate_access_token()
                if retried_unauthorized:
                    response.raise_for_status()
                retried_unauthorized = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                transient_attempts += 1
                if transient_attempts < self._max_attempts:
                    self._sleeper(float(2 ** (transient_attempts - 1)))
                    continue
            response.raise_for_status()
            return response
        raise RuntimeError("POS API retry loop exhausted")

    def _access_token_value(self) -> str:
        if self._local_access_valid():
            assert self._access_token is not None
            return self._access_token
        response = self._request_token()
        token = TokenResponse.model_validate(response.json())
        self._access_token = token.access_token
        self._access_expires_at = self._clock() + max(0, token.expires_in - 30)
        return token.access_token

    def _request_token(self) -> httpx.Response:
        for attempt in range(self._max_attempts):
            try:
                response = self._http.post(
                    f"{self._credentials.base_url.rstrip('/')}/oauth/token",
                    data={
                        "grant_type": "client_credentials",
                        "scope": " ".join(self._credentials.scopes),
                    },
                    auth=(self._credentials.client_id, self._credentials.client_secret),
                )
            except httpx.TransportError:
                if attempt + 1 >= self._max_attempts:
                    raise
                self._sleeper(float(2**attempt))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < self._max_attempts:
                    self._sleeper(float(2**attempt))
                    continue
            response.raise_for_status()
            return response
        raise RuntimeError("POS OAuth retry loop exhausted")

    def _bounded_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int],
        context: AttemptContext,
        budget: BoundedRequestBudget,
    ) -> bytes:
        retried_unauthorized = False
        for attempt in range(self._attempt_allowance(budget)):
            self._require_operation(context, 0.0)
            token = self._bounded_access_token(context, budget)
            budget.reserve_request()
            try:
                with self._http.stream(
                    method,
                    f"{self._credentials.base_url.rstrip('/')}{path}",
                    headers={
                        "authorization": f"Bearer {token}",
                        "x-pos-tenant-id": self._credentials.tenant_id,
                    },
                    params=params,
                    timeout=self._bounded_timeout(context),
                ) as response:
                    response_bytes = self._read_bounded_response(response, context, budget)
                    status_code = response.status_code
                    retry_after = response.headers.get("retry-after")
            except httpx.TransportError as exc:
                if attempt + 1 >= self._max_attempts:
                    raise PhpposBoundedTransportError("bounded PHPPOS transport failed") from exc
                self._bounded_backoff(attempt, context)
                continue
            if status_code == 401:
                self._invalidate_access_token()
                if retried_unauthorized:
                    raise PhpposBoundedTransportError("bounded PHPPOS authorization was rejected")
                retried_unauthorized = True
                continue
            if status_code == 429:
                raise SourceBackoffError(
                    self._retry_at(retry_after),
                    "bounded PHPPOS source requested backoff",
                )
            if status_code >= 500:
                if attempt + 1 >= self._max_attempts:
                    raise PhpposBoundedTransportError("bounded PHPPOS source is unavailable")
                self._bounded_backoff(attempt, context)
                continue
            if status_code < 200 or status_code >= 300:
                raise PhpposBoundedTransportError("bounded PHPPOS source rejected request")
            return response_bytes
        raise PhpposBoundedTransportError("bounded PHPPOS retry allowance exhausted")

    def _bounded_access_token(
        self,
        context: AttemptContext,
        budget: BoundedRequestBudget,
    ) -> str:
        if self._local_access_valid():
            assert self._access_token is not None
            return self._access_token
        for attempt in range(self._attempt_allowance(budget)):
            self._require_operation(context, 0.0)
            budget.reserve_request()
            try:
                with self._http.stream(
                    "POST",
                    f"{self._credentials.base_url.rstrip('/')}/oauth/token",
                    data={
                        "grant_type": "client_credentials",
                        "scope": " ".join(self._credentials.scopes),
                    },
                    auth=(self._credentials.client_id, self._credentials.client_secret),
                    timeout=self._bounded_timeout(context),
                ) as response:
                    token_bytes = self._read_bounded_response(response, context, budget)
                    status_code = response.status_code
                    retry_after = response.headers.get("retry-after")
            except httpx.TransportError as exc:
                if attempt + 1 >= self._max_attempts:
                    raise PhpposBoundedTransportError(
                        "bounded PHPPOS authentication failed"
                    ) from exc
                self._bounded_backoff(attempt, context)
                continue
            if status_code == 429:
                raise SourceBackoffError(
                    self._retry_at(retry_after),
                    "bounded PHPPOS authentication requested backoff",
                )
            if status_code >= 500:
                if attempt + 1 >= self._max_attempts:
                    raise PhpposBoundedTransportError(
                        "bounded PHPPOS authentication is unavailable"
                    )
                self._bounded_backoff(attempt, context)
                continue
            if status_code < 200 or status_code >= 300:
                raise PhpposBoundedTransportError("bounded PHPPOS authentication was rejected")
            try:
                token = TokenResponse.model_validate(loads(token_bytes))
            except (ValueError, UnicodeDecodeError) as exc:
                raise PhpposBoundedTransportError(
                    "bounded PHPPOS token response is malformed"
                ) from exc
            self._access_token = token.access_token
            self._access_expires_at = self._clock() + max(0, token.expires_in - 30)
            return token.access_token
        raise PhpposBoundedTransportError("bounded PHPPOS authentication retry allowance exhausted")

    def _read_bounded_response(
        self,
        response: httpx.Response,
        context: AttemptContext,
        budget: BoundedRequestBudget,
    ) -> bytes:
        chunks: list[bytes] = []
        for chunk in response.iter_bytes():
            self._require_operation(context, 0.0)
            budget.reserve_bytes(len(chunk))
            chunks.append(chunk)
        return b"".join(chunks)

    def _attempt_allowance(self, budget: BoundedRequestBudget) -> int:
        return min(self._max_attempts, budget.remaining_requests())

    def _bounded_backoff(self, attempt: int, context: AttemptContext) -> None:
        seconds = float(2**attempt)
        self._require_operation(context, seconds)
        self._sleeper(seconds)
        self._require_operation(context, 0.0)

    def _require_operation(self, context: AttemptContext, worst_case_seconds: float) -> None:
        if context.cancellation is not None and context.cancellation.requested():
            raise PhpposBoundedTransportError("bounded PHPPOS operation was cancelled")
        try:
            context.require_operation_budget(self._wall_clock(), worst_case_seconds)
        except TimeoutError as exc:
            raise PhpposBoundedTransportError(
                "bounded PHPPOS operation reached its deadline"
            ) from exc

    def _bounded_timeout(self, context: AttemptContext) -> float:
        remaining = context.remaining_seconds(self._wall_clock())
        if remaining is None:
            return 30.0
        if remaining <= 0:
            raise PhpposBoundedTransportError("bounded PHPPOS operation reached its deadline")
        return min(30.0, remaining)

    def _retry_at(self, retry_after: str | None) -> datetime:
        if retry_after is not None:
            try:
                seconds = float(retry_after)
            except ValueError:
                seconds = 0.0
            if seconds > 0:
                return self._wall_clock() + timedelta(seconds=seconds)
        return self._wall_clock() + timedelta(seconds=1)

    def _local_access_valid(self) -> bool:
        return self._access_token is not None and self._clock() < self._access_expires_at

    def _invalidate_access_token(self) -> None:
        self._access_token = None
        self._access_expires_at = 0.0
