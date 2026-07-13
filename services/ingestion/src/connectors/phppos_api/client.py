"""Authenticated, retrying client for PHPPOS HyperP extraction endpoints."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from json import JSONDecodeError, dumps, loads
from math import ceil
from typing import Protocol
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict
from redis import Redis

from src.connectors.phppos_api.models import CustomerPage, CustomerRow, SaleRow, SalesPage


@dataclass(frozen=True)
class ApiCredentials:
    base_url: str
    client_id: str
    client_secret: str
    bootstrap_refresh_token: str
    tenant_id: str
    page_size: int


@dataclass(frozen=True)
class CredentialBundle:
    access_token: str | None
    access_expires_at: float
    refresh_token: str
    pending_idempotency_key: str | None = None


class TokenStore(Protocol):
    def get_credential_bundle(self) -> CredentialBundle | None: ...
    def set_credential_bundle(self, value: CredentialBundle) -> None: ...
    def rotation_lock(self) -> AbstractContextManager[None]: ...
    def close(self) -> None: ...


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: str
    refresh_token: str
    expires_in: int


class RedisTokenStore:
    """Persists the latest single-use refresh token and serializes rotation."""

    def __init__(self, redis: Redis, client_id: str, lock_timeout_seconds: int) -> None:
        self._redis = redis
        self._key = f"hyperp:phppos-api:{client_id}:credentials"
        self._lock_timeout_seconds = lock_timeout_seconds

    def get_credential_bundle(self) -> CredentialBundle | None:
        value = self._redis.get(self._key)
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode()
        if not isinstance(value, str):
            raise RuntimeError("Redis returned an invalid POS OAuth credential bundle")
        try:
            payload = loads(value)
        except JSONDecodeError as exc:
            raise RuntimeError("Redis returned invalid POS OAuth credential JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Redis returned an invalid POS OAuth credential payload")
        access_token = payload.get("access_token")
        access_expires_at = payload.get("access_expires_at")
        refresh_token = payload.get("refresh_token")
        pending_idempotency_key = payload.get("pending_idempotency_key")
        if (
            access_token is not None
            and not isinstance(access_token, str)
            or not isinstance(access_expires_at, int | float)
            or not isinstance(refresh_token, str)
            or pending_idempotency_key is not None
            and not isinstance(pending_idempotency_key, str)
        ):
            raise RuntimeError("Redis returned invalid POS OAuth credential fields")
        return CredentialBundle(
            access_token,
            float(access_expires_at),
            refresh_token,
            pending_idempotency_key,
        )

    def set_credential_bundle(self, value: CredentialBundle) -> None:
        self._redis.set(
            self._key,
            dumps(
                {
                    "access_token": value.access_token,
                    "access_expires_at": value.access_expires_at,
                    "refresh_token": value.refresh_token,
                    "pending_idempotency_key": value.pending_idempotency_key,
                },
                separators=(",", ":"),
            ),
        )

    def close(self) -> None:
        self._redis.close()

    @contextmanager
    def rotation_lock(self) -> Iterator[None]:
        lock = self._redis.lock(
            f"{self._key}:lock",
            timeout=self._lock_timeout_seconds,
            blocking_timeout=self._lock_timeout_seconds,
        )
        acquired = lock.acquire()
        if not acquired:
            raise RuntimeError("Timed out acquiring POS OAuth refresh lock")
        try:
            yield
        finally:
            lock.release()


class PhpposApiClient:
    def __init__(
        self,
        credentials: ApiCredentials,
        *,
        token_store: TokenStore,
        http: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ) -> None:
        self._credentials = credentials
        self._tokens = token_store
        self._http = http or httpx.Client(timeout=30.0)
        self._sleeper = sleeper
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
        self._tokens.close()

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

    def _request(self, method: str, path: str, *, params: dict[str, str | int]) -> httpx.Response:
        refresh_required = False
        for attempt in range(self._max_attempts):
            token = self._access_token_value(force_refresh=refresh_required)
            refresh_required = False
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
                if attempt + 1 >= self._max_attempts:
                    raise
                self._sleeper(float(2**attempt))
                continue
            if response.status_code == 401 and attempt + 1 < self._max_attempts:
                refresh_required = True
                self._access_token = None
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < self._max_attempts:
                    self._sleeper(float(2**attempt))
                    continue
            response.raise_for_status()
            return response
        raise RuntimeError("POS API retry loop exhausted")

    def _access_token_value(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self._local_access_valid():
            assert self._access_token is not None
            return self._access_token
        observed = self._tokens.get_credential_bundle()
        if not force_refresh and _bundle_access_valid(observed):
            return self._use_bundle(observed)
        with self._tokens.rotation_lock():
            latest = self._tokens.get_credential_bundle()
            newer_bundle = observed is not None and latest != observed
            if _bundle_access_valid(latest) and (not force_refresh or newer_bundle):
                return self._use_bundle(latest)
            refresh_token = (
                latest.refresh_token
                if latest is not None
                else self._credentials.bootstrap_refresh_token
            )
            idempotency_key = (
                latest.pending_idempotency_key
                if latest is not None and latest.pending_idempotency_key is not None
                else str(uuid4())
            )
            pending = CredentialBundle(
                latest.access_token if latest is not None else None,
                latest.access_expires_at if latest is not None else 0,
                refresh_token,
                idempotency_key,
            )
            self._tokens.set_credential_bundle(pending)
            response = self._request_token(refresh_token, idempotency_key)
            token = TokenResponse.model_validate(response.json())
            bundle = CredentialBundle(
                token.access_token,
                time.time() + max(0, token.expires_in - 30),
                token.refresh_token,
            )
            self._tokens.set_credential_bundle(bundle)
            return self._use_bundle(bundle)

    def _request_token(self, refresh_token: str, idempotency_key: str) -> httpx.Response:
        for attempt in range(self._max_attempts):
            try:
                response = self._http.post(
                    f"{self._credentials.base_url.rstrip('/')}/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "idempotency_key": idempotency_key,
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
        return self._access_token is not None and time.time() < self._access_expires_at

    def _use_bundle(self, bundle: CredentialBundle | None) -> str:
        if not _bundle_access_valid(bundle):
            raise RuntimeError("POS OAuth credential bundle has no valid access token")
        assert bundle is not None and bundle.access_token is not None
        self._access_token = bundle.access_token
        self._access_expires_at = bundle.access_expires_at
        return bundle.access_token


def token_rotation_lock_seconds(timeout_seconds: float, max_attempts: int) -> int:
    """Return a lease covering all token attempts, backoffs, and cleanup margin."""
    attempts = max(1, max_attempts)
    backoff_seconds = sum(2**attempt for attempt in range(attempts - 1))
    cleanup_margin_seconds = 30
    http_timeout_phases = 4  # pool, connect, write, and read
    request_budget = timeout_seconds * http_timeout_phases * attempts
    return ceil(request_budget + backoff_seconds + cleanup_margin_seconds)


def _bundle_access_valid(bundle: CredentialBundle | None) -> bool:
    return (
        bundle is not None
        and bundle.access_token is not None
        and time.time() < bundle.access_expires_at
    )
