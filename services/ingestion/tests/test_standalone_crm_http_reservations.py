"""Durable reservation behavior at the Bitrix physical-attempt boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import pytest
from src.connectors.bitrix_openlines.client import BitrixOpenLinesClient
from src.graph.queries import standalone_crm_census as census_queries
from src.standalone_crm_census_models import (
    StandaloneCrmAttempt,
    StandaloneCrmCallIntent,
    StandaloneCrmCallOutcome,
    StandaloneCrmFreshness,
)
from src.standalone_crm_http_calls import (
    BitrixHttpAttempt,
    BitrixHttpCallContext,
    BitrixHttpOutcome,
    StandaloneCrmHttpReservationAdapter,
)


class _Hook:
    def __init__(self, allow: bool, events: list[str]) -> None:
        self._allow = allow
        self._events = events
        self.attempts: list[BitrixHttpAttempt] = []

    def reserve(self, attempt: BitrixHttpAttempt) -> bool:
        self.attempts.append(attempt)
        self._events.append(f"reserve:{attempt.retry_ordinal}")
        return self._allow

    def record_outcome(
        self,
        attempt: BitrixHttpAttempt,
        outcome: BitrixHttpOutcome,
        *,
        numeric_result: int | None = None,
    ) -> None:
        self._events.append(f"outcome:{attempt.retry_ordinal}:{outcome}:{numeric_result}")


class _Repository:
    def __init__(self, *, accept_outcomes: bool = True) -> None:
        self.accept_outcomes = accept_outcomes
        self.intents: list[StandaloneCrmCallIntent] = []
        self.outcomes: list[
            tuple[StandaloneCrmCallIntent, StandaloneCrmCallOutcome, int | None]
        ] = []

    def reserve_call(
        self,
        *,
        intent: StandaloneCrmCallIntent,
        budget_calls_per_attempt: int,
        budget_calls_per_occurrence: int,
    ) -> bool:
        assert budget_calls_per_attempt == 4
        assert budget_calls_per_occurrence == 8
        self.intents.append(intent)
        return True

    def record_call_outcome(
        self,
        intent: StandaloneCrmCallIntent,
        outcome: StandaloneCrmCallOutcome,
        *,
        numeric_result: int | None = None,
        result_digest: str = "",
    ) -> bool:
        assert numeric_result is None or result_digest.startswith("sha256:")
        self.outcomes.append((intent, outcome, numeric_result))
        return self.accept_outcomes


class _CapturingAdapterHook:
    def __init__(self, adapter: StandaloneCrmHttpReservationAdapter) -> None:
        self.adapter = adapter
        self.attempts: list[BitrixHttpAttempt] = []

    def reserve(self, attempt: BitrixHttpAttempt) -> bool:
        self.attempts.append(attempt)
        return self.adapter.reserve(attempt)

    def record_outcome(
        self,
        attempt: BitrixHttpAttempt,
        outcome: BitrixHttpOutcome,
        *,
        numeric_result: int | None = None,
    ) -> None:
        self.adapter.record_outcome(attempt, outcome, numeric_result=numeric_result)


class _DeadlineAdvancingHook:
    def __init__(self, adapter: StandaloneCrmHttpReservationAdapter) -> None:
        self._adapter = adapter
        self.expire_after_reservation = True

    def reserve(self, attempt: BitrixHttpAttempt) -> bool:
        reserved = self._adapter.reserve(attempt)
        if self.expire_after_reservation:
            self.now = 2.0
        return reserved

    def record_outcome(
        self,
        attempt: BitrixHttpAttempt,
        outcome: BitrixHttpOutcome,
        *,
        numeric_result: int | None = None,
    ) -> None:
        self._adapter.record_outcome(attempt, outcome, numeric_result=numeric_result)

    now = 0.0


def _adapter(repository: _Repository) -> StandaloneCrmHttpReservationAdapter:
    now = datetime.now(UTC)
    attempt = StandaloneCrmAttempt(
        "census",
        1,
        "parent",
        "running",
        1,
        now + timedelta(minutes=1),
        now + timedelta(minutes=2),
    )
    freshness = StandaloneCrmFreshness("census", "fingerprint", "authority", "source", "control")
    return StandaloneCrmHttpReservationAdapter(
        repository=repository,
        attempt=attempt,
        freshness=freshness,
        max_calls_per_attempt=4,
        max_calls_per_occurrence=8,
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_attempts: int = 1,
    hook: _Hook | _CapturingAdapterHook | _DeadlineAdvancingHook | None = None,
    max_request_count: int | None = None,
    deadline_monotonic: float | None = None,
) -> BitrixOpenLinesClient:
    return BitrixOpenLinesClient(
        base_url="https://example.invalid",
        timeout_seconds=1.0,
        max_attempts=max_attempts,
        request_delay_seconds=0.0,
        max_request_count=max_request_count,
        deadline_monotonic=deadline_monotonic,
        reservation_hook=hook,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _probe_success() -> httpx.Response:
    return httpx.Response(200, json={"result": [{"ID": "9"}]})


def test_reservation_precedes_http_and_success_is_recorded() -> None:
    events: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        events.append("http")
        return httpx.Response(200, json={"result": []})

    client = _client(handler, hook=_Hook(True, events))
    try:
        assert client.probe_crm_contact_upper_id() == 0
    finally:
        client.close()
    assert events == ["reserve:0", "http", "outcome:0:succeeded:0"]


def test_rejected_reservation_performs_no_http() -> None:
    events: list[str] = []
    client = _client(lambda _request: pytest.fail("unexpected HTTP"), hook=_Hook(False, events))
    try:
        with pytest.raises(RuntimeError, match="reservation was rejected"):
            client.probe_crm_contact_upper_id()
    finally:
        client.close()
    assert events == ["reserve:0"]


@pytest.mark.parametrize("failure", ["rate_limit", "server", "envelope", "transport"])
def test_hooked_retryable_failures_reserve_a_fresh_intent_per_physical_io(
    monkeypatch: pytest.MonkeyPatch,
    failure: Literal["rate_limit", "server", "envelope", "transport"],
) -> None:
    repository = _Repository()
    adapter = _adapter(repository)
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        if posts == 1:
            if failure == "rate_limit":
                return httpx.Response(429, request=request)
            if failure == "server":
                return httpx.Response(503, request=request)
            if failure == "envelope":
                return httpx.Response(200, json={"error": "QUERY_LIMIT_EXCEEDED"})
            raise httpx.ConnectError("offline", request=request)
        return _probe_success()

    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.sleep", lambda _delay: None)
    client = _client(handler, max_attempts=2, hook=_CapturingAdapterHook(adapter))
    try:
        assert client.probe_crm_contact_upper_id() == 9
    finally:
        client.close()

    assert posts == 2
    assert len(repository.intents) == 2
    assert len({intent.intent_id for intent in repository.intents}) == 2
    assert [intent.retry_ordinal for intent in repository.intents] == [0, 1]
    assert [outcome for _intent, outcome, _result in repository.outcomes] == ["failed", "succeeded"]
    assert [result for _intent, _outcome, result in repository.outcomes] == [None, 9]


@pytest.mark.parametrize("failure", ["http", "envelope"])
def test_hooked_nonretryable_failures_record_once_and_do_not_retry(
    failure: Literal["http", "envelope"],
) -> None:
    repository = _Repository()
    adapter = _adapter(repository)
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        if failure == "http":
            return httpx.Response(400, request=request)
        return httpx.Response(200, json={"error": "ACCESS_DENIED"})

    client = _client(handler, max_attempts=3, hook=_CapturingAdapterHook(adapter))
    try:
        with pytest.raises(RuntimeError):
            client.probe_crm_contact_upper_id()
    finally:
        client.close()

    assert posts == 1
    assert len(repository.intents) == 1
    assert [outcome for _intent, outcome, _result in repository.outcomes] == ["failed"]


@pytest.mark.parametrize("failure", ["rate_limit", "server", "envelope", "transport"])
def test_legacy_no_hook_retry_behavior_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    failure: Literal["rate_limit", "server", "envelope", "transport"],
) -> None:
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        if posts == 1:
            if failure == "rate_limit":
                return httpx.Response(429, request=request)
            if failure == "server":
                return httpx.Response(503, request=request)
            if failure == "envelope":
                return httpx.Response(200, json={"error": "QUERY_LIMIT_EXCEEDED"})
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={"result": []})

    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.sleep", lambda _delay: None)
    client = _client(handler, max_attempts=2)
    try:
        assert client.probe_crm_contact_upper_id() == 0
    finally:
        client.close()
    assert posts == 2
    assert client.request_count == 2


@pytest.mark.parametrize("failure", ["http", "envelope"])
def test_legacy_no_hook_permanent_failures_are_not_retried(
    failure: Literal["http", "envelope"],
) -> None:
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        if failure == "http":
            return httpx.Response(400, request=request)
        return httpx.Response(200, json={"error": "ACCESS_DENIED"})

    client = _client(handler, max_attempts=3)
    try:
        with pytest.raises(RuntimeError):
            client.probe_crm_contact_upper_id()
    finally:
        client.close()
    assert posts == 1
    assert client.request_count == 1


def test_outcome_persistence_failure_after_io_leaves_intent_consumed() -> None:
    repository = _Repository(accept_outcomes=False)
    adapter = _adapter(repository)
    hook = _CapturingAdapterHook(adapter)
    posts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return _probe_success()

    client = _client(handler, hook=hook)
    try:
        with pytest.raises(RuntimeError, match="outcome persistence was rejected"):
            client.probe_crm_contact_upper_id()
    finally:
        client.close()

    assert posts == 1
    assert len(repository.intents) == 1
    assert len(repository.outcomes) == 1
    assert len(hook.attempts) == 1
    assert adapter.reserve(hook.attempts[0]) is False
    assert len(repository.intents) == 1


def test_deadline_after_reservation_causes_zero_io_and_new_delivery_uses_new_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    adapter = _adapter(repository)
    hook = _DeadlineAdvancingHook(adapter)
    posts = 0

    def monotonic() -> float:
        return hook.now

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return _probe_success()

    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.monotonic", monotonic)
    client = _client(handler, hook=hook, deadline_monotonic=1.0)
    try:
        with pytest.raises(RuntimeError, match="runtime ceiling"):
            client.probe_crm_contact_upper_id()
        assert posts == 0
        assert len(repository.intents) == 1
        assert repository.outcomes == []

        hook.now = 0.0
        hook.expire_after_reservation = False
        assert client.probe_crm_contact_upper_id() == 9
    finally:
        client.close()

    assert posts == 1
    assert len(repository.intents) == 2
    assert repository.intents[0].intent_id != repository.intents[1].intent_id
    assert client.request_count == 1


def test_source_page_and_company_binding_calls_persist_exact_sanitized_metadata() -> None:
    repository = _Repository()
    adapter = _adapter(repository)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/crm.contact.list"):
            return httpx.Response(200, json={"result": []})
        if request.url.path.endswith("/crm.contact.company.items.get"):
            return httpx.Response(200, json={"result": []})
        pytest.fail(f"unexpected Bitrix method: {request.url.path}")

    client = _client(handler, hook=_CapturingAdapterHook(adapter))
    try:
        assert (
            client.list_crm_contacts_keyset(greater_than_id=7, less_than_or_equal_to_id=9).records
            == ()
        )
        assert client.get_contact_company_bindings("42") == ()
    finally:
        client.close()

    page, binding = repository.intents
    assert (page.call_kind, page.unit_kind, page.cursor_id, page.upper_id, page.subject_id) == (
        "page",
        "contact",
        7,
        9,
        None,
    )
    assert (
        binding.call_kind,
        binding.unit_kind,
        binding.cursor_id,
        binding.upper_id,
        binding.subject_id,
    ) == ("company_binding", "contact", None, None, "42")


def test_mapping_only_or_untyped_calls_are_rejected_without_io() -> None:
    repository = _Repository()
    adapter = _adapter(repository)
    client = _client(
        lambda _request: pytest.fail("unexpected HTTP"), hook=_CapturingAdapterHook(adapter)
    )
    try:
        with pytest.raises(RuntimeError, match="typed source call metadata"):
            client.list_active_configs()
    finally:
        client.close()

    mapping_attempt = BitrixHttpAttempt(
        "crm.contact.list",
        0,
        BitrixHttpCallContext(call_kind="probe", unit_kind=None),
    )
    with pytest.raises(RuntimeError, match="typed source call metadata"):
        adapter.reserve(mapping_attempt)

    assert repository.intents == []
    assert "census.census_kind = 'source_sync'" in census_queries.RESERVE_HTTP_CALL


def test_hooked_client_request_count_is_diagnostic_not_durable_budget_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    adapter = _adapter(repository)
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        if posts == 1:
            return httpx.Response(503, request=request)
        return _probe_success()

    monkeypatch.setattr("src.connectors.bitrix_openlines.client.time.sleep", lambda _delay: None)
    client = _client(
        handler,
        max_attempts=2,
        max_request_count=1,
        hook=_CapturingAdapterHook(adapter),
    )
    try:
        assert client.probe_crm_contact_upper_id() == 9
    finally:
        client.close()

    assert posts == 2
    assert client.request_count == 2
    assert len(repository.intents) == 2


def test_adapter_allows_distinct_physical_attempts_for_repeated_identical_calls() -> None:
    repository = _Repository()
    adapter = _adapter(repository)
    responses = iter((_probe_success(), _probe_success()))

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = _client(handler, hook=_CapturingAdapterHook(adapter))
    try:
        assert client.probe_crm_contact_upper_id() == 9
        assert client.probe_crm_contact_upper_id() == 9
    finally:
        client.close()

    assert len(repository.intents) == 2
    assert len({intent.intent_id for intent in repository.intents}) == 2
    assert [intent.retry_ordinal for intent in repository.intents] == [0, 0]


def test_invalid_json_and_invalid_probe_result_record_failed_outcomes_after_io() -> None:
    invalid_json_repository = _Repository()
    invalid_json_adapter = _adapter(invalid_json_repository)
    invalid_json_client = _client(
        lambda _request: httpx.Response(200, content=b"not-json"),
        hook=_CapturingAdapterHook(invalid_json_adapter),
    )
    try:
        with pytest.raises(RuntimeError, match="invalid JSON"):
            invalid_json_client.probe_crm_contact_upper_id()
    finally:
        invalid_json_client.close()
    assert [outcome for _intent, outcome, _result in invalid_json_repository.outcomes] == ["failed"]

    invalid_probe_repository = _Repository()
    invalid_probe_adapter = _adapter(invalid_probe_repository)
    invalid_probe_client = _client(
        lambda _request: httpx.Response(200, json={"result": [{"ID": "not-an-id"}]}),
        hook=_CapturingAdapterHook(invalid_probe_adapter),
    )
    try:
        with pytest.raises(RuntimeError, match="numeric ID"):
            invalid_probe_client.probe_crm_contact_upper_id()
    finally:
        invalid_probe_client.close()
    assert [outcome for _intent, outcome, _result in invalid_probe_repository.outcomes] == [
        "failed"
    ]
