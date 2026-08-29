from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import httpx
import pytest
from src.connectors.bitrix_openlines.client import (
    BitrixHttpCallIntent,
    BitrixHttpCallMetadata,
    BitrixHttpCallReservationHook,
    BitrixOpenLinesClient,
)
from src.standalone_crm_census_http import StandaloneCrmCensusHttpReservationHook
from src.standalone_crm_census_models import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
    StandaloneCrmCallIntent,
    StandaloneCrmCallOutcome,
    StandaloneCrmCensusRequest,
)


@dataclass
class _Hook:
    permit: bool = True
    fail_outcomes: bool = False
    intents: list[BitrixHttpCallIntent] = field(default_factory=list)
    outcomes: list[tuple[str, str, str | None]] = field(default_factory=list)
    bounds: list[tuple[str, int]] = field(default_factory=list)

    def reserve(self, intent: BitrixHttpCallIntent) -> bool:
        self.intents.append(intent)
        return self.permit

    def record_outcome(
        self,
        intent: BitrixHttpCallIntent,
        state: Literal["succeeded", "failed", "unknown"],
        error_code: str | None = None,
    ) -> None:
        if self.fail_outcomes:
            raise RuntimeError("durable outcome unavailable")
        self.outcomes.append((intent.intent_id, state, error_code))

    def record_probe_upper_bound(self, intent: BitrixHttpCallIntent, upper_id: int) -> None:
        if self.fail_outcomes:
            raise RuntimeError("durable outcome unavailable")
        self.bounds.append((intent.intent_id, upper_id))


def _client(
    handler: httpx.MockTransport,
    hook: BitrixHttpCallReservationHook | None = None,
    attempts: int = 2,
) -> BitrixOpenLinesClient:
    return BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest",
        timeout_seconds=5,
        max_attempts=attempts,
        request_delay_seconds=0,
        http=httpx.Client(transport=handler),
        reservation_hook=hook,
    )


def test_reservation_refusal_performs_zero_source_io() -> None:
    posts: list[httpx.Request] = []
    hook = _Hook(permit=False)

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200, json={"result": []})

    client = _client(httpx.MockTransport(respond), hook)

    with pytest.raises(RuntimeError, match="not reserved"):
        client.probe_crm_contact_upper_id()

    assert len(hook.intents) == 1
    assert posts == []
    assert hook.outcomes == []


def test_each_retry_reserves_a_new_intent_and_transport_failure_is_known() -> None:
    hook = _Hook()
    requests = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json={"result": [{"ID": "7"}]})

    assert _client(httpx.MockTransport(respond), hook).probe_crm_contact_upper_id() == 7

    assert [intent.retry_ordinal for intent in hook.intents] == [0, 1]
    assert hook.intents[0].intent_id != hook.intents[1].intent_id
    assert hook.outcomes == [(hook.intents[0].intent_id, "failed", "transport_error")]
    assert hook.bounds == [(hook.intents[1].intent_id, 7)]


@pytest.mark.parametrize(
    ("payload", "expected"), [({"result": []}, 0), ({"result": [{"ID": "9"}]}, 9)]
)
def test_usable_probe_result_persists_zero_and_positive_bounds(
    payload: dict[str, object], expected: int
) -> None:
    hook = _Hook()
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json=payload)), hook)

    assert client.probe_crm_lead_upper_id() == expected
    assert hook.outcomes == []
    assert hook.bounds == [(hook.intents[0].intent_id, expected)]


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        (b"not-json", "invalid_json"),
        ({"unexpected": True}, "invalid_envelope"),
        ({"result": [{"ID": "bad"}]}, "invalid_probe_result"),
    ],
)
def test_protocol_failures_are_known_failures_after_reservation(
    payload: bytes | dict[str, object], error_code: str
) -> None:
    hook = _Hook()
    response = (
        httpx.Response(200, content=payload)
        if isinstance(payload, bytes)
        else httpx.Response(200, json=payload)
    )
    client = _client(httpx.MockTransport(lambda _: response), hook)

    with pytest.raises(RuntimeError):
        client.probe_crm_company_upper_id()

    assert hook.outcomes == [(hook.intents[0].intent_id, "failed", error_code)]


def test_outcome_write_failure_leaves_a_consumed_unresolved_intent_without_reuse() -> None:
    hook = _Hook(fail_outcomes=True)
    posts: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200, json={"result": [{"ID": "3"}]})

    client = _client(httpx.MockTransport(respond), hook)

    with pytest.raises(RuntimeError, match="durable outcome unavailable"):
        client.probe_crm_contact_upper_id()
    assert len(posts) == 1
    assert len(hook.intents) == 1

    hook.fail_outcomes = False
    assert client.probe_crm_contact_upper_id() == 3
    assert len(posts) == 2
    assert len(hook.intents) == 2
    assert hook.intents[0].intent_id != hook.intents[1].intent_id


def test_legacy_no_hook_probe_behavior_is_unchanged() -> None:
    client = _client(
        httpx.MockTransport(lambda _: httpx.Response(200, json={"result": [{"ID": "12"}]}))
    )

    assert client.probe_crm_contact_upper_id() == 12
    assert client.request_count == 1


def test_accepted_http_status_persists_durable_success_before_returning_not_found() -> None:
    hook = _Hook()
    client = _client(
        httpx.MockTransport(
            lambda request: httpx.Response(
                404,
                json={"error": "ERROR_NOT_FOUND", "error_description": "not found"},
                request=request,
            )
        ),
        hook,
    )

    assert client.get_deal_or_none(7) is None
    assert hook.outcomes == [(hook.intents[0].intent_id, "succeeded", None)]


def test_accepted_response_stops_when_its_durable_outcome_cannot_be_persisted() -> None:
    hook = _Hook(fail_outcomes=True)
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json={"result": []})), hook)

    with pytest.raises(RuntimeError, match="durable outcome unavailable"):
        client.list_crm_contacts_keyset(greater_than_id=None, less_than_or_equal_to_id=3)

    assert len(hook.intents) == 1


@dataclass
class _ReservationRepository:
    intents: list[StandaloneCrmCallIntent] = field(default_factory=list)
    outcomes: list[StandaloneCrmCallOutcome] = field(default_factory=list)

    def reserve_call_with_sequence(
        self, intent: StandaloneCrmCallIntent, fence_token: int, request: StandaloneCrmCensusRequest
    ) -> int | None:
        assert fence_token == 11
        assert request.occurrence_key == "occurrence"
        self.intents.append(intent)
        return 17

    def record_call_outcome(self, outcome: StandaloneCrmCallOutcome) -> bool:
        self.outcomes.append(outcome)
        return True


def _request() -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        "source",
        "control",
        "occurrence",
        ("contact",),
        StandaloneCrmBudget(2, 2, 20, 4, 4, 3, "2026-08-29T00:00:00Z"),
        "policy",
        "association",
        "sha256:" + "a" * 64,
        SourceSyncAuthority("mapping", "sha256:" + "b" * 64, "projection", "sha256:" + "c" * 64),
    )


def test_production_adapter_binds_fence_budget_and_persists_parsed_probe_outcome() -> None:
    repository = _ReservationRepository()
    hook = StandaloneCrmCensusHttpReservationHook(repository, _request(), "census", 4, 11)
    http_intent = BitrixHttpCallIntent(
        "intent", "crm.contact.list", 0, BitrixHttpCallMetadata("probe", "contact")
    )

    assert hook.reserve(http_intent)
    hook.record_probe_upper_bound(http_intent, 0)

    assert repository.intents[0].generation == 4
    assert repository.intents[0].sequence == 1
    assert hook._reserved[http_intent.intent_id].sequence == 17
    assert repository.outcomes[0].state == "succeeded"
    assert repository.outcomes[0].upper_id == 0


def test_non_probe_call_without_parent_issued_task_id_is_denied_before_io() -> None:
    repository = _ReservationRepository()
    hook = StandaloneCrmCensusHttpReservationHook(repository, _request(), "census", 4, 11)
    posts: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200, json={"result": []})

    client = _client(httpx.MockTransport(respond), hook)
    with pytest.raises(RuntimeError, match="not reserved"):
        client.list_crm_contacts_keyset(greater_than_id=None, less_than_or_equal_to_id=4)

    assert posts == []
    assert repository.intents == []


def test_page_and_binding_reservations_keep_the_exact_parent_issued_task_identity() -> None:
    repository = _ReservationRepository()
    hook = StandaloneCrmCensusHttpReservationHook(
        repository, _request(), "census", 4, 11, "published-child-task"
    )
    page = BitrixHttpCallIntent(
        "page-intent", "crm.contact.list", 0, BitrixHttpCallMetadata("page", "contact", 0)
    )
    binding = BitrixHttpCallIntent(
        "binding-intent",
        "crm.contact.company.items.get",
        0,
        BitrixHttpCallMetadata("company_binding", "contact", 42, 42),
    )

    assert hook.reserve(page)
    assert hook.reserve(binding)

    assert [(intent.call_kind, intent.task_id) for intent in repository.intents] == [
        ("page", "published-child-task"),
        ("company_binding", "published-child-task"),
    ]
    assert repository.intents[1].cursor == 42
    assert repository.intents[1].subject_id == 42
    assert hook._reserved["page-intent"].sequence == 17
    assert hook._reserved["binding-intent"].sequence == 17


def test_completed_intent_receipt_returns_only_the_durable_successful_exact_call() -> None:
    repository = _ReservationRepository()
    hook = StandaloneCrmCensusHttpReservationHook(
        repository, _request(), "census", 4, 11, "published-child-task"
    )
    page = BitrixHttpCallIntent(
        "page-intent", "crm.contact.list", 0, BitrixHttpCallMetadata("page", "contact", 5)
    )

    assert hook.reserve(page)
    with pytest.raises(RuntimeError, match="no durable successful"):
        hook.completed_intent_id("page", 5, None)
    hook.record_outcome(page, "succeeded")

    assert hook.completed_intent_id("page", 5, None) == "page-intent"


def test_binding_client_metadata_has_a_durable_subject_cursor() -> None:
    hook = _Hook()
    client = _client(httpx.MockTransport(lambda _: httpx.Response(200, json={"result": []})), hook)

    assert client.get_contact_company_bindings("42") == ()

    metadata = hook.intents[0].metadata
    assert metadata is not None
    assert metadata.call_kind == "company_binding"
    assert metadata.stream_kind == "contact"
    assert metadata.cursor == 42
    assert metadata.subject_id == 42
