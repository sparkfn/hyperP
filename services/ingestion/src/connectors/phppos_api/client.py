"""Authenticated, retrying client for PHPPOS HyperP extraction endpoints."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict

from src.connectors.phppos_api.models import CustomerPage, CustomerRow, SaleRow, SalesPage


@dataclass(frozen=True)
class ApiCredentials:
    base_url: str
    client_id: str
    client_secret: str
    tenant_id: str
    page_size: int
    scopes: tuple[str, ...]


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
        max_attempts: int = 3,
    ) -> None:
        self._credentials = credentials
        self._http = http or httpx.Client(timeout=30.0)
        self._sleeper = sleeper
        self._clock = clock
        self._max_attempts = max_attempts
        self._access_token: str | None = None
        self._access_expires_at = 0.0
        self._closed = False

    def iter_customers(self) -> Iterator[CustomerRow]:
        cursor: str | None = None
        while True:
            page = CustomerPage.model_validate(self._get_page("customers", cursor))
            yield from page.data
            if not page.pagination.has_more:
                return
            cursor = page.pagination.next_cursor

    def iter_sales(self) -> Iterator[SaleRow]:
        cursor: str | None = None
        while True:
            page = SalesPage.model_validate(self._get_page("sales", cursor))
            yield from page.data
            if not page.pagination.has_more:
                return
            cursor = page.pagination.next_cursor

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._http.close()

    def _get_page(self, resource: str, cursor: str | None) -> object:
        params: dict[str, str | int] = {"limit": self._credentials.page_size}
        if cursor is not None:
            params["cursor"] = cursor
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

    def _local_access_valid(self) -> bool:
        return self._access_token is not None and self._clock() < self._access_expires_at

    def _invalidate_access_token(self) -> None:
        self._access_token = None
        self._access_expires_at = 0.0
