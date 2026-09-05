"""Bounded, metadata-only request-time Bitrix CRM activity reader."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Literal, cast

import httpx

from src.config import AppConfig
from src.display_format import format_display_date
from src.repositories.bitrix.activity_support import (
    Activity,
    Budget,
    CacheDisposition,
    ReadError,
    ReadState,
    aggregate_activity_metrics,
)
from src.types_crm import (
    BitrixDealScope,
    CrmActivityFailureReason,
    PersonCrmActivityMetrics,
    PersonCrmActivityMetricsComplete,
    PersonCrmActivityMetricsPartial,
    PersonCrmActivityMetricsUnavailable,
)


@dataclass
class _Flight:
    task: asyncio.Task[PersonCrmActivityMetrics]
    waiters: int = 0


_EnvelopeFailure = tuple[CrmActivityFailureReason, bool]
_TransientEnvelopeCode = Literal[
    "internal_error",
    "query_limit_exceeded",
    "service_unavailable",
    "temporary_error",
]


class BitrixCrmActivityRepository:
    """Read approved aggregate metadata without retaining upstream payloads."""

    def __init__(self, config: AppConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client
        self._cache: dict[str, tuple[float, int, PersonCrmActivityMetricsComplete]] = {}
        self._cache_sequence = 0
        self._inflight: dict[str, _Flight] = {}
        self._lock = asyncio.Lock()
        self._limiter = asyncio.Semaphore(config.bitrix_activity_max_concurrency)

    async def get_person_crm_activity_metrics(
        self, scope: BitrixDealScope
    ) -> PersonCrmActivityMetrics:
        fetched = datetime.now(UTC)
        if scope.deal_limit_exhausted:
            return self._unavailable(scope, fetched, "deal_limit", "disabled")
        if not scope.scope_valid:
            return self._unavailable(scope, fetched, "malformed_response", "disabled")
        if not scope.deal_ids:
            return self._complete(scope, fetched, Budget(0, 0, 0), [], "miss")
        if not scope.source_authorized:
            return self._unavailable(scope, fetched, "source_unavailable", "disabled")
        if not self._config.bitrix_activity_api_url:
            return self._unavailable(scope, fetched, "not_configured", "disabled")

        key = self._cache_key(scope)
        async with self._lock:
            self._purge_expired_locked()
            cached = self._cache.get(key)
            if cached is not None:
                return cached[2].model_copy(update={"cache_disposition": "hit"})
            flight = self._inflight.get(key)
            if flight is not None and flight.task.done():
                self._inflight.pop(key, None)
                flight = None
            created = flight is None
            if flight is None:
                task = asyncio.create_task(self._read(scope, fetched))
                flight = _Flight(task=task)
                self._inflight[key] = flight

                def completed_callback(
                    completed: asyncio.Task[PersonCrmActivityMetrics],
                    cache_key: str = key,
                ) -> None:
                    self._schedule_finish(cache_key, completed)

                task.add_done_callback(completed_callback)
            flight.waiters += 1

        try:
            result = await asyncio.shield(flight.task)
            if isinstance(result, PersonCrmActivityMetricsComplete):
                await self._cache_complete(key, result)
            if not created:
                return result.model_copy(update={"cache_disposition": "coalesced"})
            return result
        finally:
            await self._release_waiter(key, flight)

    async def _release_waiter(self, key: str, flight: _Flight) -> None:
        async with self._lock:
            current = self._inflight.get(key)
            if current is not flight:
                return
            flight.waiters -= 1
            if flight.waiters == 0 and not flight.task.done():
                self._inflight.pop(key, None)
                flight.task.cancel()

    def _schedule_finish(self, key: str, task: asyncio.Task[PersonCrmActivityMetrics]) -> None:
        asyncio.create_task(self._finish_flight(key, task))

    async def _cache_complete(self, key: str, result: PersonCrmActivityMetricsComplete) -> None:
        async with self._lock:
            self._store_complete_locked(key, result)

    async def _finish_flight(self, key: str, task: asyncio.Task[PersonCrmActivityMetrics]) -> None:
        async with self._lock:
            current = self._inflight.get(key)
            if current is not None and current.task is task:
                self._inflight.pop(key, None)

    def _store_complete_locked(self, key: str, result: PersonCrmActivityMetricsComplete) -> None:
        if self._config.bitrix_activity_cache_ttl_seconds == 0:
            return
        self._purge_expired_locked()
        self._cache_sequence += 1
        self._cache[key] = (
            monotonic() + self._config.bitrix_activity_cache_ttl_seconds,
            self._cache_sequence,
            result,
        )
        while len(self._cache) > self._config.bitrix_activity_cache_max_entries:
            oldest = min(self._cache, key=lambda item: self._cache[item][1])
            self._cache.pop(oldest, None)

    def _purge_expired_locked(self) -> None:
        now = monotonic()
        for key, value in list(self._cache.items()):
            if value[0] <= now:
                self._cache.pop(key, None)

    async def _read(self, scope: BitrixDealScope, fetched: datetime) -> PersonCrmActivityMetrics:
        budget = Budget(
            self._config.bitrix_activity_max_requests,
            self._config.bitrix_activity_max_pages,
            self._config.bitrix_activity_max_rows,
        )
        state = ReadState()
        deadline = monotonic() + self._config.bitrix_activity_elapsed_seconds
        tasks = [
            asyncio.create_task(self._read_batch(batch, budget, state, deadline))
            for batch in self._batches(scope.deal_ids)
        ]
        try:
            await asyncio.gather(*tasks)
        except ReadError as failure:
            await self._cancel_tasks(tasks)
            return self._incomplete(scope, fetched, budget, await state.snapshot(), failure.reason)
        except asyncio.CancelledError:
            await self._cancel_tasks(tasks)
            raise
        except httpx.HTTPError:
            await self._cancel_tasks(tasks)
            return self._incomplete(
                scope, fetched, budget, await state.snapshot(), "upstream_error"
            )
        return self._complete(scope, fetched, budget, await state.snapshot(), "miss")

    @staticmethod
    async def _cancel_tasks(tasks: list[asyncio.Task[None]]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _read_batch(
        self,
        deal_ids: tuple[str, ...],
        budget: Budget,
        state: ReadState,
        deadline: float,
    ) -> None:
        if not deal_ids:
            raise ReadError("malformed_response")
        upper_bound = await self._freeze_upper_bound(deal_ids, budget, deadline)
        if upper_bound is None:
            return
        cursor: str | None = None
        seen_cursors: set[str | None] = set()
        owners = set(deal_ids)
        while True:
            self._check_deadline(deadline)
            if cursor in seen_cursors:
                raise ReadError("non_advancing_pagination")
            seen_cursors.add(cursor)
            await budget.reserve_page()
            data = await self._request_page(deal_ids, cursor, upper_bound, budget, deadline)
            result = data.get("result")
            if not isinstance(result, list):
                raise ReadError("malformed_response")
            page_ids: list[str] = []
            for raw in result:
                self._check_deadline(deadline)
                await budget.reserve_row()
                activity = self._parse_item(raw, owners)
                if int(activity.activity_id) > int(upper_bound):
                    raise ReadError("malformed_response")
                if cursor is not None and int(activity.activity_id) < int(cursor):
                    raise ReadError("non_advancing_pagination")
                if page_ids and int(activity.activity_id) < int(page_ids[-1]):
                    raise ReadError("non_advancing_pagination")
                page_ids.append(activity.activity_id)
                await state.add(activity)
            if not page_ids:
                raise ReadError("non_advancing_pagination")
            next_cursor = page_ids[-1]
            if cursor is not None and int(next_cursor) <= int(cursor):
                raise ReadError("non_advancing_pagination")
            if data.get("next") is None:
                return
            cursor = next_cursor

    async def _request_page(
        self,
        deal_ids: tuple[str, ...],
        cursor: str | None,
        upper_bound: str,
        budget: Budget,
        deadline: float,
    ) -> dict[str, object]:
        if not deal_ids:
            raise ReadError("malformed_response")
        filters: dict[str, object] = {
            "OWNER_TYPE_ID": 2,
            "@OWNER_ID": list(deal_ids),
            "<=ID": upper_bound,
        }
        if cursor is not None:
            filters[">ID"] = cursor
        body: dict[str, object] = {
            "filter": filters,
            "select": [
                "ID",
                "OWNER_TYPE_ID",
                "OWNER_ID",
                "TYPE_ID",
                "PROVIDER_ID",
                "PROVIDER_TYPE_ID",
                "START_TIME",
                "CREATED",
                "LAST_UPDATED",
                "DIRECTION",
                "COMPLETED",
                "RESULT_STATUS",
            ],
            "order": {"ID": "ASC"},
        }
        return await self._request_body(body, budget, deadline)

    async def _request_body(
        self, body: dict[str, object], budget: Budget, deadline: float
    ) -> dict[str, object]:
        for attempt in range(self._config.bitrix_activity_max_attempts):
            self._check_deadline(deadline)
            await budget.reserve_request()
            try:
                remaining = self._remaining(deadline)
                timeout = min(self._config.bitrix_activity_timeout_seconds, remaining)
                async with asyncio.timeout(timeout):
                    acquired = False
                    await self._limiter.acquire()
                    acquired = True
                    try:
                        response = await self._post(body)
                    finally:
                        if acquired:
                            self._limiter.release()
                self._check_deadline(deadline)
                if response.status_code == 429:
                    if attempt + 1 >= self._config.bitrix_activity_max_attempts:
                        raise ReadError("rate_limited")
                    continue
                if response.status_code >= 500:
                    if attempt + 1 >= self._config.bitrix_activity_max_attempts:
                        raise ReadError("upstream_error")
                    continue
                if response.status_code >= 400:
                    raise ReadError("upstream_error")
                data = response.json()
                if not isinstance(data, dict):
                    raise ReadError("malformed_response")
                envelope_failure = self._envelope_failure(cast(dict[str, object], data))
                if envelope_failure is not None:
                    reason, retryable = envelope_failure
                    if not retryable or attempt + 1 >= self._config.bitrix_activity_max_attempts:
                        raise ReadError(reason)
                    continue
                return cast(dict[str, object], data)
            except (TimeoutError, httpx.TimeoutException):
                if monotonic() >= deadline:
                    raise ReadError("elapsed_limit") from None
                if attempt + 1 >= self._config.bitrix_activity_max_attempts:
                    raise ReadError("timeout") from None
            except httpx.RequestError:
                if attempt + 1 >= self._config.bitrix_activity_max_attempts:
                    raise ReadError("upstream_error") from None
            except ValueError as error:
                raise ReadError("malformed_response") from error
        raise ReadError("upstream_error")

    @staticmethod
    def _envelope_failure(data: dict[str, object]) -> _EnvelopeFailure | None:
        raw_code = data.get("error")
        if raw_code is None:
            return None
        if not isinstance(raw_code, str):
            return ("malformed_response", False)
        code = raw_code.strip().lower()
        if not code:
            return ("malformed_response", False)
        if code in {"query_limit_exceeded", "too_many_requests", "rate_limit_exceeded"}:
            return ("rate_limited", True)
        transient_codes: tuple[_TransientEnvelopeCode, ...] = (
            "internal_error",
            "query_limit_exceeded",
            "service_unavailable",
            "temporary_error",
        )
        if code in transient_codes:
            return ("upstream_error", True)
        return ("upstream_error", False)

    async def _post(self, body: dict[str, object]) -> httpx.Response:
        url = self._config.bitrix_activity_api_url
        if url is None:
            raise ReadError("not_configured")
        if self._client is not None:
            return await self._client.post(url, json=body)
        timeout = httpx.Timeout(self._config.bitrix_activity_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, json=body)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ReadError("elapsed_limit")
        return remaining

    def _check_deadline(self, deadline: float) -> None:
        if monotonic() >= deadline:
            raise ReadError("elapsed_limit")

    async def _freeze_upper_bound(
        self, deal_ids: tuple[str, ...], budget: Budget, deadline: float
    ) -> str | None:
        body: dict[str, object] = {
            "filter": {"OWNER_TYPE_ID": 2, "@OWNER_ID": list(deal_ids)},
            "select": ["ID"],
            "order": {"ID": "DESC"},
        }
        data = await self._request_body(body, budget, deadline)
        result = data.get("result")
        if not isinstance(result, list):
            raise ReadError("malformed_response")
        if not result:
            return None
        if not isinstance(result[0], dict):
            raise ReadError("malformed_response")
        bound = self._positive_id(result[0].get("ID"))
        if bound is None:
            raise ReadError("malformed_response")
        return bound

    def _parse_item(self, raw: object, owners: set[str]) -> Activity:
        if not isinstance(raw, dict):
            raise ReadError("malformed_response")
        identifier = self._positive_id(raw.get("ID"))
        owner_type = raw.get("OWNER_TYPE_ID")
        owner_id = raw.get("OWNER_ID")
        if (
            identifier is None
            or owner_type not in (2, "2")
            or self._positive_id(owner_id) not in owners
        ):
            raise ReadError("malformed_response")
        timestamp = next(
            (raw.get(name) for name in ("START_TIME", "CREATED", "LAST_UPDATED") if raw.get(name)),
            None,
        )
        if not isinstance(timestamp, str):
            raise ReadError("malformed_response")
        try:
            event_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if event_at.tzinfo is None:
                raise ValueError
            event_at = event_at.astimezone(UTC)
        except ValueError as error:
            raise ReadError("malformed_response") from error
        kind = self._activity_kind(
            raw.get("TYPE_ID"), raw.get("PROVIDER_ID"), raw.get("PROVIDER_TYPE_ID")
        )
        classification = self._call_classification(raw) if kind == "call" else None
        return Activity(identifier, kind, classification, event_at)

    @staticmethod
    def _activity_kind(raw_type: object, provider_id: object, provider_type: object) -> str:
        provider = provider_id.strip() if isinstance(provider_id, str) else ""
        provider_type_text = provider_type.strip() if isinstance(provider_type, str) else ""
        provider_markers = f"{provider} {provider_type_text}".upper()
        if raw_type in (2, "2", "CALL", "call") or "CALL" in provider_markers:
            return "call"
        if provider.upper() == "IMOPENLINES_SESSION":
            return "openlines_session"
        if provider_type_text:
            return provider_type_text.lower()
        if raw_type is not None and str(raw_type).strip():
            return f"activity_type_{str(raw_type).strip().lower()}"
        return "activity"

    @staticmethod
    def _positive_id(value: object) -> str | None:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        text = str(value)
        if not text.isascii() or not text.isdecimal() or text.startswith("0"):
            return None
        return text

    @staticmethod
    def _call_classification(raw: dict[object, object]) -> str:
        direction = str(raw.get("DIRECTION") or "unknown").strip().lower()
        completed = raw.get("COMPLETED")
        completion = "completed" if completed in ("Y", "y", True, 1, "1") else "unknown"
        return f"{direction}_{completion}"

    def _incomplete(
        self,
        scope: BitrixDealScope,
        fetched: datetime,
        budget: Budget,
        activities: list[Activity],
        reason: CrmActivityFailureReason,
    ) -> PersonCrmActivityMetrics:
        if not activities:
            return self._unavailable_with_budget(scope, fetched, budget, reason)
        payload = aggregate_activity_metrics(
            scope,
            fetched,
            budget,
            activities,
            "miss",
            self._config.bitrix_activity_source_instance,
        )
        payload.update({"truncated": True, "failure_reason": reason})
        return PersonCrmActivityMetricsPartial.model_validate(payload)

    def _complete(
        self,
        scope: BitrixDealScope,
        fetched: datetime,
        budget: Budget,
        activities: list[Activity],
        cache: CacheDisposition,
    ) -> PersonCrmActivityMetricsComplete:
        return PersonCrmActivityMetricsComplete.model_validate(
            aggregate_activity_metrics(
                scope,
                fetched,
                budget,
                activities,
                cache,
                self._config.bitrix_activity_source_instance,
            )
        )

    def _unavailable(
        self,
        scope: BitrixDealScope,
        fetched: datetime,
        reason: CrmActivityFailureReason,
        cache: CacheDisposition,
    ) -> PersonCrmActivityMetricsUnavailable:
        return self._unavailable_payload(scope, fetched, reason, cache, 0, 0, 0)

    def _unavailable_with_budget(
        self,
        scope: BitrixDealScope,
        fetched: datetime,
        budget: Budget,
        reason: CrmActivityFailureReason,
    ) -> PersonCrmActivityMetricsUnavailable:
        return self._unavailable_payload(
            scope, fetched, reason, "miss", budget.requests, budget.pages, budget.rows
        )

    def _unavailable_payload(
        self,
        scope: BitrixDealScope,
        fetched: datetime,
        reason: CrmActivityFailureReason,
        cache: CacheDisposition,
        requests: int,
        pages: int,
        rows: int,
    ) -> PersonCrmActivityMetricsUnavailable:
        return PersonCrmActivityMetricsUnavailable(
            source_instance=self._config.bitrix_activity_source_instance,
            fetched_at=fetched.isoformat(),
            fetched_at_display=format_display_date(fetched.isoformat()),
            cache_disposition=cache,
            truncated=False,
            queried_deal_count=len(scope.deal_ids),
            resolved_deal_count=scope.resolved_deal_count,
            request_count=requests,
            page_count=pages,
            row_count=rows,
            failure_reason=reason,
        )

    def _batches(self, ids: tuple[str, ...]) -> list[tuple[str, ...]]:
        size = self._config.bitrix_activity_owner_batch_size
        return [ids[index : index + size] for index in range(0, len(ids), size)]

    def _cache_key(self, scope: BitrixDealScope) -> str:
        digest = hashlib.sha256(",".join(sorted(scope.deal_ids)).encode()).hexdigest()
        return (
            f"{self._config.bitrix_activity_source_instance}:{scope.canonical_person_id}:{digest}"
        )
