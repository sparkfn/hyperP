"""Bounded change-feed conformance tests for Fundbox users, contacts and sales.

These tests are the synthetic-source contract for the durable bounded path: one
frozen-window page per unit, replay-safe checkpoints, explicit upstream failure
outcomes, transactional writes, and cutoff-bounded execution that never makes an
unfiltered population pass.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from _bounded_ingestion_fixture import FakeClock, FakeShutdown, context
from neo4j import ManagedTransaction
from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedMode,
    BoundedUnit,
    FailureCategory,
    OccurrenceContext,
    PauseReason,
    RunScope,
    SourceBackoffError,
    UnitApplyResult,
    Usage,
)
from src.bounded_ingestion_runner import BoundedIngestionRunner
from src.config import Settings
from src.connectors.fundbox_api import bounded
from src.connectors.fundbox_api.bounded import (
    FundboxBoundedConnector,
    FundboxBoundedDescriptor,
    FundboxBoundedWriter,
)
from src.connectors.fundbox_api.client import (
    FundboxApiClient,
    FundboxApiCredentials,
    FundboxCursorExpiredError,
)
from src.connectors.fundbox_api.connectors import (
    FundboxApiConnector,
    FundboxContactsApiConnector,
    FundboxSalesApiConnector,
    FundboxUsersApiConnector,
)
from src.connectors.fundbox_api.models import BoundedIngestionPage, validate_source_records
from src.connectors.registry import BoundedConnectorRegistry
from src.exclusions import ExclusionContext, normalized_phone_set
from src.models import JsonValue, SourceRecordEnvelope
from src.pipeline import IngestPipeline
from src.resumable import CheckpointDescriptor, IngestionUnit

_CONTRACT_VERSION = "fundbox-change-feed-v1"
_CAPABILITY_EVIDENCE = "verified_upstream_contract"
_SNAPSHOT_ID = "snap-2026-09-17"
_EFFECTIVE_AT = "2026-09-17T06:00:00Z"
# Far enough in the future that a resumed checkpoint stays usable regardless of
# when the suite runs; expiry itself is asserted with an explicit past instant.
_CURSOR_EXPIRES_AT = "2030-01-01T00:00:00Z"
_EXPIRED_AT = "2020-01-01T00:00:00Z"


# --- synthetic source fixtures ---------------------------------------------


def _window(
    resource: str = "users",
    *,
    snapshot_id: str = _SNAPSHOT_ID,
    lower: int = 0,
    upper: int = 9,
    retention_days: int = 30,
    capability_evidence: str = _CAPABILITY_EVIDENCE,
) -> dict[str, JsonValue]:
    """Return the frozen change-feed window the descriptor must resume against."""

    return {
        "contract_version": _CONTRACT_VERSION,
        "resource": resource,
        "snapshot_id": snapshot_id,
        "lower_change_version": lower,
        "upper_change_version": upper,
        "cursor_retention_days": retention_days,
        "capability_evidence": capability_evidence,
    }


def _checkpoint(
    *,
    window: dict[str, JsonValue],
    continuation: str | None = None,
    last_position: list[JsonValue] | None = None,
    terminal: bool = False,
    expires_at: str | None = None,
    connector_version: str = "fundbox-bounded-v1",
    schema_version: int = 1,
) -> CheckpointDescriptor:
    """Return a durable continuation position for one Fundbox change window."""

    return CheckpointDescriptor(
        phase="change_window",
        cursor={
            "continuation": continuation,
            "last_position": last_position,
            "cursor_expires_at": expires_at,
            "terminal": terminal,
        },
        source_window=window,
        last_committed_record_id=None,
        connector_version=connector_version,
        schema_version=schema_version,
        replay_boundary="fundbox:test",
    )


def _run_scope(
    *,
    source_key: str = "fundbox",
    window: dict[str, JsonValue] | None = None,
    mode: BoundedMode = "delta",
) -> RunScope:
    """Return one exact bounded scope for a Fundbox change-feed run."""

    return RunScope(
        environment="test",
        reset_generation=1,
        source_key=source_key,
        control_instance_id="fundbox-control",
        entity_key=None,
        stream_key=None,
        mode=mode,
        configuration_fingerprint="fundbox-fingerprint",
        connector_version="fundbox-bounded-v1",
        checkpoint_schema_version=1,
        source_window=window if window is not None else _window(),
    )


def _contact_composite(contact_id: int) -> dict[str, JsonValue]:
    """Return one contract-validated contact composite as the client emits it."""

    return validate_source_records(
        "contacts",
        [
            {
                "effective_updated_at": _EFFECTIVE_AT,
                "contact": {
                    "id": contact_id,
                    "user_id": 2,
                    "mobile_number": "81234567",
                    "full_name": "Grace",
                    "relationship": "sister",
                    "created_at": None,
                    "updated_at": _EFFECTIVE_AT,
                },
            }
        ],
    )[0]


def _address_payload(address_line_1: str) -> dict[str, JsonValue]:
    """Return one joined address child as the source would report it."""

    return {"id": 5, "user_id": 7, "address_type": "home", "address_line_1": address_line_1}


def _user_composite(
    user_id: int,
    *,
    addresses: list[dict[str, JsonValue]] | None = None,
) -> dict[str, JsonValue]:
    """Return one contract-validated user composite as the client emits it."""

    return validate_source_records(
        "users",
        [
            {
                "effective_updated_at": _EFFECTIVE_AT,
                "user": {
                    "id": user_id,
                    "email": "ada@example.com",
                    "mobile_number": "81234567",
                    "created_at": None,
                    "updated_at": _EFFECTIVE_AT,
                },
                "basic_profile": {
                    "id": 1,
                    "user_id": user_id,
                    "nric": None,
                    "full_name": "Ada Rider",
                    "date_of_birth": None,
                    "gender": None,
                    "nationality": None,
                    "race": None,
                    "email": None,
                    "mobile_number": None,
                    "created_at": None,
                    "updated_at": _EFFECTIVE_AT,
                },
                "basic_plus_profile": None,
                "addresses": addresses if addresses is not None else [],
                "social_accounts": [],
                "device_ids": [],
                "last_login": None,
            }
        ],
    )[0]


def _sales_composite(order_id: int) -> dict[str, JsonValue]:
    """Return one contract-validated sales composite as the client emits it."""

    return validate_source_records(
        "sales",
        [
            {
                "effective_updated_at": _EFFECTIVE_AT,
                "order": {
                    "id": order_id,
                    "user_id": 2,
                    "merchant_id": 3,
                    "merchant_staff_id": None,
                    "order_no": "FB-11",
                    "status": "completed",
                    "total_amount": "100.00",
                    "total_items": 1,
                    "transaction_reference": "tx",
                    "release_date": None,
                    "expiry_at": None,
                    "created_at": None,
                    "updated_at": _EFFECTIVE_AT,
                },
                "merchant": {"id": 3, "name": "Cycles", "official_name": None},
                "items": [],
                "customer": None,
            }
        ],
    )[0]


def _upsert(
    change_version: int,
    root_id: int,
    composite: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Return one effective composite upsert at a change-feed position."""

    return {
        "kind": "upsert",
        "change_version": change_version,
        "root_id": root_id,
        "effective_updated_at": _EFFECTIVE_AT,
        "composite": composite,
    }


def _tombstone(change_version: int, root_id: int, reason: str = "deleted") -> dict[str, JsonValue]:
    """Return one explicit root tombstone at a change-feed position."""

    return {
        "kind": "tombstone",
        "change_version": change_version,
        "root_id": root_id,
        "effective_updated_at": _EFFECTIVE_AT,
        "tombstone_reason": reason,
    }


def _page(
    changes: list[dict[str, JsonValue]],
    *,
    terminal: bool,
    next_cursor: str | None = None,
    snapshot_id: str = _SNAPSHOT_ID,
    lower: int = 0,
    upper: int = 9,
    cursor_expires_at: str = _CURSOR_EXPIRES_AT,
    response_bytes: int | None = None,
) -> BoundedIngestionPage:
    """Return one source page exactly as the bounded client would validate it."""

    return BoundedIngestionPage.model_validate(
        {
            "data": changes,
            "meta": {
                "snapshot_id": snapshot_id,
                "lower_change_version": lower,
                "upper_change_version": upper,
                "next_cursor": next_cursor,
                "terminal": terminal,
                "cursor_expires_at": cursor_expires_at,
            },
            "response_bytes": response_bytes,
        }
    )


def _page_payload(
    changes: list[dict[str, JsonValue]] | None = None,
    *,
    next_cursor: str | None = "cursor-2",
    terminal: bool = False,
    snapshot_id: str = _SNAPSHOT_ID,
    lower: int = 0,
    upper: int = 9,
    cursor_expires_at: str = _CURSOR_EXPIRES_AT,
) -> dict[str, JsonValue]:
    """Return one wire-shaped bounded page payload."""

    return {
        "data": changes if changes is not None else [_upsert(3, 7, _contact_composite(7))],
        "meta": {
            "snapshot_id": snapshot_id,
            "lower_change_version": lower,
            "upper_change_version": upper,
            "next_cursor": next_cursor,
            "terminal": terminal,
            "cursor_expires_at": cursor_expires_at,
        },
    }


def _contact_envelope(contact_id: int) -> dict[str, JsonValue]:
    """Return the relationship envelope the contact mapper produces."""

    return FundboxContactsApiConnector(_StubBoundedClient([])).build_record(
        _contact_composite(contact_id)
    )


def _sales_envelope(order_id: int) -> dict[str, JsonValue]:
    """Return the sales envelope the sales mapper produces."""

    return FundboxSalesApiConnector(_StubBoundedClient([])).build_record(
        _sales_composite(order_id)
    )


def _tombstone_record(contact_id: int) -> dict[str, JsonValue]:
    """Return the retirement evidence the connector emits for a tombstone."""

    return {
        "_fundbox_tombstone": {
            "source_record_id": f"fundbox-contact-{contact_id}",
            "retired_at": "2026-09-17T06:00:00+00:00",
            "snapshot_id": _SNAPSHOT_ID,
            "reason": "deleted",
        }
    }


class _StubBoundedClient:
    """Scripted bounded client that records requests and refuses to stream."""

    def __init__(self, responses: list[BoundedIngestionPage | Exception]) -> None:
        self.responses = responses
        self.resources: list[str] = []
        self.snapshots: list[str] = []
        self.cursors: list[str | None] = []
        self.windows: list[tuple[int, int]] = []
        self.contexts: list[AttemptContext] = []
        self.max_bytes: list[int] = []
        self.stream_calls = 0
        self.closed = False

    @property
    def calls(self) -> int:
        return len(self.resources)

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
        self.resources.append(resource)
        self.snapshots.append(snapshot_id)
        self.cursors.append(cursor)
        self.windows.append((lower_change_version, upper_change_version))
        self.contexts.append(context)
        self.max_bytes.append(max_bytes)
        if self.calls > len(self.responses):
            raise AssertionError("bounded connector fetched more pages than the test scripted")
        response = self.responses[self.calls - 1]
        if isinstance(response, Exception):
            raise response
        return response

    def iter_source(
        self,
        resource: str,
        *,
        updated_since: str | None = None,
    ) -> Iterator[dict[str, JsonValue]]:
        self.stream_calls += 1
        raise AssertionError("bounded ingestion must never stream a population pass")

    def close(self) -> None:
        self.closed = True


def _connector(
    client: _StubBoundedClient,
    *,
    resource: str = "users",
    mapper_type: type[FundboxApiConnector] = FundboxUsersApiConnector,
    max_records: int = 500,
    max_bytes: int = 2_000_000,
) -> FundboxBoundedConnector:
    """Return a bounded connector wired to the scripted client."""

    return FundboxBoundedConnector(
        source_key="fundbox",
        resource=resource,
        mapper_type=mapper_type,
        client=client,
        max_records=max_records,
        max_bytes=max_bytes,
    )


def _http_client(handler: Callable[[httpx.Request], httpx.Response]) -> FundboxApiClient:
    """Return a bounded client whose inline retry path fails the test."""

    return FundboxApiClient(
        FundboxApiCredentials("https://fundbox.test/api/v1", "u", "p", 100),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=3,
        sleeper=lambda _seconds: pytest.fail("bounded fetch must not retry inline"),
    )


def _open_occurrence() -> OccurrenceContext:
    """Return an occurrence that is admitting work right now.

    The connector and the bounded client both refuse source work at or after the
    operation deadline, so unit-level tests need a window that is open against
    the wall clock the client actually checks.
    """

    now = datetime.now(UTC)
    return OccurrenceContext(
        occurrence_id="fundbox-open-window",
        starts_at=now - timedelta(minutes=1),
        drain_starts_at=now + timedelta(minutes=1),
        cutoff_at=now + timedelta(hours=2),
        next_eligible_at=now + timedelta(days=7),
        scheduled=False,
    )


def _open_attempt(*, occurrence_context: OccurrenceContext | None = None) -> AttemptContext:
    """Return an admitted attempt inside an open drain window."""

    return context(
        0,
        occurrence_context=(
            occurrence_context if occurrence_context is not None else _open_occurrence()
        ),
    )


def _fetch(
    client: FundboxApiClient,
    *,
    resource: str = "contacts",
    snapshot_id: str = _SNAPSHOT_ID,
    lower: int = 0,
    upper: int = 9,
    cursor: str | None = None,
    attempt: AttemptContext | None = None,
    max_bytes: int = 2_000_000,
) -> BoundedIngestionPage:
    """Fetch one bounded page through the real client with overridable arguments."""

    return client.fetch_bounded_page(
        resource,
        snapshot_id=snapshot_id,
        lower_change_version=lower,
        upper_change_version=upper,
        cursor=cursor,
        context=attempt if attempt is not None else _open_attempt(),
        max_bytes=max_bytes,
    )


# --- single-page, deadline-aware client ------------------------------------


def test_bounded_client_reads_exactly_one_frozen_window_page() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page_payload())

    page = _fetch(_http_client(handler))

    assert len(requests) == 1
    params = requests[0].url.params
    assert params["snapshot_id"] == _SNAPSHOT_ID
    assert params["after_change_version"] == "0"
    assert params["through_change_version"] == "9"
    assert params["limit"] == "100"
    assert params.get("updated_since") is None
    assert params.get("cursor") is None
    assert page.meta.next_cursor == "cursor-2"
    assert page.meta.terminal is False
    assert page.data[0].composite is not None
    assert page.data[0].composite["contact"]["id"] == 7


def test_bounded_client_sends_the_persisted_continuation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page_payload(next_cursor=None, terminal=True))

    page = _fetch(_http_client(handler), cursor="cursor-1")

    assert requests[0].url.params["cursor"] == "cursor-1"
    assert len(requests) == 1
    assert page.meta.terminal is True
    assert page.meta.next_cursor is None


def _assert_bounded_fetch_rejects(
    payload: dict[str, JsonValue],
    message: str,
    *,
    max_bytes: int = 2_000_000,
) -> None:
    client = _http_client(lambda _request: httpx.Response(200, json=payload))

    with pytest.raises(ValueError, match=message):
        _fetch(client, max_bytes=max_bytes)


def test_bounded_client_rejects_response_drift_and_oversized_bodies() -> None:
    _assert_bounded_fetch_rejects(_page_payload(snapshot_id="snap-other"), "frozen snapshot")
    _assert_bounded_fetch_rejects(_page_payload(upper=10), "frozen change window")
    _assert_bounded_fetch_rejects(_page_payload(), "byte limit", max_bytes=8)


def test_bounded_client_rejects_invalid_frozen_window_arguments_before_any_request() -> None:
    client = _http_client(lambda _request: pytest.fail("no source call for invalid arguments"))

    with pytest.raises(ValueError, match="Unsupported Fundbox API resource"):
        _fetch(client, resource="orders")
    with pytest.raises(ValueError, match="snapshot ID"):
        _fetch(client, snapshot_id="  ")
    with pytest.raises(ValueError, match="frozen change window"):
        _fetch(client, lower=9, upper=3)
    with pytest.raises(ValueError, match="continuation cursor"):
        _fetch(client, cursor="x" * 2049)
    with pytest.raises(ValueError, match="response limit"):
        _fetch(client, max_bytes=0)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(410, FundboxCursorExpiredError), (401, PermissionError), (403, PermissionError)],
)
def test_bounded_client_classifies_terminal_source_responses(
    status: int,
    expected: type[Exception],
) -> None:
    client = _http_client(lambda request: httpx.Response(status, request=request))

    with pytest.raises(expected):
        _fetch(client)


@pytest.mark.parametrize("status", [429, 503])
def test_bounded_client_turns_retriable_responses_into_durable_backoff(status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, request=request, headers={"Retry-After": "5"})

    client = _http_client(handler)
    observed_at = datetime.now(UTC)

    with pytest.raises(SourceBackoffError) as caught:
        _fetch(client)

    assert len(requests) == 1
    assert caught.value.safe_message == "fundbox_source_backoff"
    assert caught.value.retry_at >= observed_at + timedelta(seconds=5)


def test_bounded_client_aborts_a_streamed_body_over_the_byte_limit() -> None:
    body = json.dumps(_page_payload()).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        # A chunked response carries no Content-Length, so the streamed
        # accumulator is what has to stop the oversized body.
        return httpx.Response(200, stream=httpx.ByteStream(body))

    client = _http_client(handler)

    with pytest.raises(ValueError, match="byte limit"):
        _fetch(client, max_bytes=32)


def test_bounded_client_rejects_a_declared_body_over_the_byte_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "9000000"}, content=b"{}")

    client = _http_client(handler)

    with pytest.raises(ValueError, match="byte limit"):
        _fetch(client, max_bytes=1024)


def test_bounded_client_reports_the_measured_response_size() -> None:
    body = json.dumps(_page_payload()).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(body))

    page = _fetch(_http_client(handler))

    assert page.response_bytes == len(body)


def test_bounded_client_turns_a_source_outage_into_durable_backoff() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("source is down", request=request)

    client = _http_client(handler)

    with pytest.raises(SourceBackoffError) as caught:
        _fetch(client)

    assert caught.value.safe_message == "fundbox_transport_unavailable"


def test_bounded_client_makes_no_upstream_call_at_or_after_the_operation_deadline() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page_payload())

    client = _http_client(handler)
    expired = replace(
        _open_attempt(),
        operation_deadline_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    with pytest.raises(TimeoutError, match="cutoff"):
        _fetch(client, attempt=expired)

    assert requests == []


def test_bounded_client_makes_no_upstream_call_once_cancellation_is_requested() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page_payload())

    client = _http_client(handler)
    cancelled = replace(_open_attempt(), cancellation=FakeShutdown(is_requested=True))

    with pytest.raises(TimeoutError, match="cancelled"):
        _fetch(client, attempt=cancelled)

    assert requests == []


# --- one page in, one atomic unit out --------------------------------------


def test_bounded_connector_yields_one_unit_per_page_with_bounded_usage() -> None:
    stub = _StubBoundedClient(
        [
            _page(
                [_upsert(3, 7, _contact_composite(7))],
                terminal=False,
                next_cursor="cursor-2",
                response_bytes=512,
            )
        ]
    )
    connector = _connector(stub, resource="contacts", mapper_type=FundboxContactsApiConnector)
    attempt = _open_attempt()

    unit = connector.fetch_one_unit(_checkpoint(window=_window("contacts")), attempt)

    assert stub.resources == ["contacts"]
    assert stub.snapshots == [_SNAPSHOT_ID]
    assert stub.cursors == [None]
    assert stub.windows == [(0, 9)]
    assert stub.stream_calls == 0
    assert stub.contexts == [attempt]
    assert unit.usage == Usage(records=1, source_requests=1, pages=1, bytes_read=512)
    assert unit.terminal is False
    assert unit.unit.records[0]["source_record_id"] == "fundbox-contact-7"
    cursor = unit.unit.checkpoint_after.cursor
    assert cursor["continuation"] == "cursor-2"
    assert cursor["last_position"] == [3, 7]
    assert cursor["terminal"] is False
    assert cursor["cursor_expires_at"] == "2030-01-01T00:00:00+00:00"
    assert unit.unit.checkpoint_after.last_committed_record_id == "fundbox-contact-7"


def test_bounded_connector_freezes_the_window_and_forwards_its_source_limits() -> None:
    stub = _StubBoundedClient([_page([_upsert(1, 7, _user_composite(7))], terminal=True)])
    connector = _connector(stub, max_bytes=1_500_000, max_records=7)

    unit = connector.fetch_one_unit(_checkpoint(window=_window()), _open_attempt())

    assert stub.snapshots == [_SNAPSHOT_ID]
    assert stub.max_bytes == [1_500_000]
    assert unit.unit.checkpoint_after.source_window == _window()
    assert unit.terminal is True


def test_bounded_connector_advances_to_terminal_for_an_empty_window() -> None:
    stub = _StubBoundedClient([_page([], terminal=True)])
    connector = _connector(stub)

    unit = connector.fetch_one_unit(_checkpoint(window=_window()), _open_attempt())

    assert unit.terminal is True
    assert unit.unit.records == ()
    assert unit.usage == Usage(records=0, source_requests=1, pages=1, bytes_read=0)
    assert unit.unit.checkpoint_after != unit.unit.checkpoint_before
    assert unit.unit.checkpoint_after.cursor == {
        "continuation": None,
        "last_position": None,
        "cursor_expires_at": "2030-01-01T00:00:00+00:00",
        "terminal": True,
    }


def test_bounded_connector_upserts_a_joined_child_only_change() -> None:
    stub = _StubBoundedClient(
        [
            _page(
                [
                    _upsert(3, 7, _user_composite(7)),
                    _upsert(4, 7, _user_composite(7, addresses=[_address_payload("12 New Road")])),
                ],
                terminal=False,
                next_cursor="cursor-2",
            )
        ]
    )
    connector = _connector(stub)

    unit = connector.fetch_one_unit(_checkpoint(window=_window()), _open_attempt())

    assert [record["source_record_id"] for record in unit.unit.records] == [
        "fundbox-user-7",
        "fundbox-user-7",
    ]
    assert unit.unit.records[1]["raw_payload"]["addresses"][0]["address_line_1"] == "12 New Road"
    assert unit.unit.records[1]["attributes"]["address"] == "12 New Road"
    assert unit.unit.checkpoint_after.cursor["last_position"] == [4, 7]
    assert unit.unit.checkpoint_after.last_committed_record_id == "fundbox-user-7"


def test_bounded_connector_rejects_a_repeated_continuation_that_did_not_advance() -> None:
    stub = _StubBoundedClient(
        [_page([_upsert(3, 7, _contact_composite(7))], terminal=False, next_cursor="cursor-1")]
    )
    connector = _connector(stub, resource="contacts", mapper_type=FundboxContactsApiConnector)

    with pytest.raises(ValueError, match="did not progress"):
        connector.fetch_one_unit(
            _checkpoint(window=_window("contacts"), continuation="cursor-1"),
            _open_attempt(),
        )


def test_bounded_connector_rejects_a_source_order_regression() -> None:
    stub = _StubBoundedClient(
        [_page([_upsert(3, 7, _contact_composite(7))], terminal=False, next_cursor="cursor-2")]
    )
    connector = _connector(stub, resource="contacts", mapper_type=FundboxContactsApiConnector)

    with pytest.raises(ValueError, match="did not progress"):
        connector.fetch_one_unit(
            _checkpoint(window=_window("contacts"), continuation="cursor-1", last_position=[3, 7]),
            _open_attempt(),
        )


def test_bounded_connector_accepts_an_equal_timestamp_that_advances_the_position() -> None:
    stub = _StubBoundedClient(
        [_page([_upsert(3, 8, _contact_composite(8))], terminal=False, next_cursor="cursor-3")]
    )
    connector = _connector(stub, resource="contacts", mapper_type=FundboxContactsApiConnector)

    unit = connector.fetch_one_unit(
        _checkpoint(window=_window("contacts"), continuation="cursor-2", last_position=[3, 7]),
        _open_attempt(),
    )

    assert unit.unit.checkpoint_after.cursor["last_position"] == [3, 8]


def test_bounded_connector_enforces_page_and_nested_child_caps() -> None:
    crowded = _StubBoundedClient(
        [
            _page(
                [_upsert(1, 7, _contact_composite(7)), _upsert(2, 8, _contact_composite(8))],
                terminal=True,
            )
        ]
    )
    connector = _connector(
        crowded,
        resource="contacts",
        mapper_type=FundboxContactsApiConnector,
        max_records=1,
    )

    with pytest.raises(ValueError, match="record cap"):
        connector.fetch_one_unit(_checkpoint(window=_window("contacts")), _open_attempt())

    oversized = _StubBoundedClient(
        [_page([_upsert(1, 7, {"addresses": [{} for _ in range(101)]})], terminal=True)]
    )

    with pytest.raises(ValueError, match="addresses exceeds bounded child limit"):
        _connector(oversized).fetch_one_unit(_checkpoint(window=_window()), _open_attempt())


def test_bounded_connector_maps_tombstones_to_retirement_evidence() -> None:
    stub = _StubBoundedClient([_page([_tombstone(4, 9, "ineligible")], terminal=True)])
    connector = _connector(stub, resource="contacts", mapper_type=FundboxContactsApiConnector)

    unit = connector.fetch_one_unit(_checkpoint(window=_window("contacts")), _open_attempt())

    assert len(unit.unit.records) == 1
    tombstone = unit.unit.records[0].get("_fundbox_tombstone")
    assert isinstance(tombstone, dict)
    assert tombstone["source_record_id"] == "fundbox-contact-9"
    assert tombstone["retired_at"] == "2026-09-17T06:00:00+00:00"
    assert tombstone["snapshot_id"] == _SNAPSHOT_ID
    assert tombstone["reason"] == "ineligible"
    assert unit.unit.checkpoint_after.last_committed_record_id == "fundbox-contact-9"


def test_bounded_connector_closes_the_source_client_once() -> None:
    stub = _StubBoundedClient([_page([], terminal=True)])
    connector = _connector(stub)

    connector.cancel()
    connector.close()

    assert stub.closed is True
    assert stub.stream_calls == 0


# --- checkpoint compatibility ----------------------------------------------


def test_bounded_connector_accepts_its_own_frozen_window_checkpoint() -> None:
    connector = _connector(_StubBoundedClient([]))

    assert connector.validate_checkpoint(_checkpoint(window=_window())) == "compatible"


def test_bounded_connector_reports_version_drift_as_incompatible() -> None:
    connector = _connector(_StubBoundedClient([]))

    drifted = _checkpoint(window=_window(), connector_version="fundbox-bounded-v2")
    schema_drift = _checkpoint(window=_window(), schema_version=2)

    assert connector.validate_checkpoint(drifted) == "incompatible"
    assert connector.validate_checkpoint(schema_drift) == "incompatible"


def test_bounded_connector_reports_expired_foreign_and_malformed_checkpoints() -> None:
    connector = _connector(_StubBoundedClient([]))

    expired = _checkpoint(window=_window(), expires_at=_EXPIRED_AT)

    assert connector.validate_checkpoint(expired) == "expired"
    assert connector.validate_checkpoint(_checkpoint(window=_window("sales"))) == "corrupted"
    assert (
        connector.validate_checkpoint(_checkpoint(window=_window(), expires_at="not-a-time"))
        == "corrupted"
    )


def test_bounded_connector_rejects_unverified_capability_evidence() -> None:
    connector = _connector(_StubBoundedClient([]))

    unverified = _checkpoint(window=_window(capability_evidence="self_reported"))

    assert connector.validate_checkpoint(unverified) == "rejected"


def test_bounded_connector_rejects_a_malformed_terminal_flag() -> None:
    connector = _connector(_StubBoundedClient([]))
    malformed = CheckpointDescriptor(
        phase="change_window",
        cursor={
            "continuation": None,
            "last_position": None,
            "cursor_expires_at": None,
            "terminal": "yes",
        },
        source_window=_window(),
        last_committed_record_id=None,
        connector_version="fundbox-bounded-v1",
        schema_version=1,
        replay_boundary="fundbox:test",
    )

    assert connector.validate_checkpoint(malformed) == "corrupted"


def test_initial_checkpoint_is_side_effect_free_and_source_scoped() -> None:
    descriptor = FundboxBoundedDescriptor(
        "fundbox:contacts",
        "contacts",
        FundboxContactsApiConnector,
    )
    window = _window("contacts")

    checkpoint = descriptor.initial_checkpoint(
        _run_scope(source_key="fundbox:contacts", window=window),
        None,
    )

    assert checkpoint.phase == "change_window"
    assert checkpoint.cursor == {
        "continuation": None,
        "last_position": None,
        "cursor_expires_at": None,
        "terminal": False,
    }
    assert checkpoint.source_window == window
    assert checkpoint.replay_boundary == "fundbox:fundbox:contacts:snap-2026-09-17"
    assert checkpoint.last_committed_record_id is None
    assert checkpoint.connector_version == descriptor.connector_version
    assert checkpoint.schema_version == descriptor.checkpoint_schema_version

    with pytest.raises(ValueError, match="another source"):
        descriptor.initial_checkpoint(_run_scope(window=window), None)


# --- transactional writes ---------------------------------------------------


class _Transaction:
    """Opaque caller-owned transaction handed to the writer's hooks."""

    def run(self, query: str, **parameters: object) -> None:
        raise AssertionError("bounded writer hooks own their queries")


class _IngestOutcome:
    """Minimal ``IngestResult`` surface the writer classifies."""

    skipped_duplicate = False
    dropped = False
    retry_pending = False


class _RecordingPipeline:
    """Stand-in for the graph pipeline that must never open its own session."""

    instances: list[_RecordingPipeline] = []

    def __init__(self, client: object, *, control_instance_id: str) -> None:
        self.client = client
        self.control_instance_id = control_instance_id
        self.records: list[tuple[object, object]] = []
        _RecordingPipeline.instances.append(self)

    def ingest_in_transaction(
        self,
        tx: ManagedTransaction,
        envelope: object,
        *,
        ingest_run_id: str | None = None,
        exclusion_context: ExclusionContext | None = None,
    ) -> object:
        self.records.append((tx, envelope))
        return _IngestOutcome()


class _RecordingSalesHook:
    """Records the caller transaction the sales hook is applied in."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, object, str | None, str | None]] = []

    def __call__(
        self,
        tx: ManagedTransaction,
        envelope: object,
        *,
        ingest_run_id: str | None,
        exclusion_context: ExclusionContext | None,
        control_instance_id: str | None,
    ) -> object:
        self.calls.append((tx, envelope, ingest_run_id, control_instance_id))
        return _IngestOutcome()


class _RecordingRetirementHook:
    """Records the caller transaction the retirement hook is applied in."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, str, str, str]] = []

    def __call__(
        self,
        tx: ManagedTransaction,
        source_system: str,
        source_record_id: str,
        retired_at: str,
        reconciliation_snapshot_at: str,
    ) -> int:
        self.calls.append((tx, source_record_id, retired_at, reconciliation_snapshot_at))
        return 1


def _writer_unit(
    records: tuple[dict[str, JsonValue], ...],
    *,
    window: dict[str, JsonValue] | None = None,
) -> BoundedUnit:
    checkpoint = _checkpoint(window=window if window is not None else _window())
    return BoundedUnit(
        unit=IngestionUnit(checkpoint, checkpoint, records),
        replay_id="fundbox:writer-unit",
        usage=Usage(records=len(records), source_requests=1, pages=1),
        terminal=True,
    )


def _writer_attempt() -> AttemptContext:
    """Return a Fundbox-scoped attempt for the transactional writer."""

    return context(
        0,
        run_scope=_run_scope(source_key="fundbox:contacts", window=_window("contacts")),
    )


def test_bounded_writer_applies_upserts_and_tombstones_in_the_callers_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingPipeline.instances.clear()
    sales_hook = _RecordingSalesHook()
    retirement_hook = _RecordingRetirementHook()
    monkeypatch.setattr(bounded, "IngestPipeline", _RecordingPipeline)
    monkeypatch.setattr(bounded, "ingest_sales_record_in_transaction", sales_hook)
    monkeypatch.setattr(bounded, "retire_source_evidence_in_transaction", retirement_hook)

    contact = _contact_envelope(9)
    sale = _sales_envelope(11)
    tx = cast(ManagedTransaction, _Transaction())
    attempt = _writer_attempt()
    unit = _writer_unit((contact, sale, _tombstone_record(4)), window=_window("contacts"))

    result = FundboxBoundedWriter().apply(tx, attempt, unit)

    assert result.dispositions == ("committed", "committed", "committed")
    assert len(_RecordingPipeline.instances) == 1
    pipeline = _RecordingPipeline.instances[0]
    assert pipeline.client is None
    assert pipeline.control_instance_id == attempt.scope.control_instance_id
    assert [
        cast(SourceRecordEnvelope, envelope).source_record_id
        for _tx, envelope in pipeline.records
    ] == ["fundbox-contact-9"]
    assert pipeline.records[0][0] is tx
    assert sales_hook.calls[0][0] is tx
    assert cast(SourceRecordEnvelope, sales_hook.calls[0][1]).source_record_id == (
        "fundbox-order-11"
    )
    assert cast(SourceRecordEnvelope, sales_hook.calls[0][1]).record_type == "sales"
    assert sales_hook.calls[0][2] == attempt.ingest_run_id
    assert sales_hook.calls[0][3] == attempt.scope.control_instance_id
    assert retirement_hook.calls == [
        (tx, "fundbox-contact-4", "2026-09-17T06:00:00+00:00", _SNAPSHOT_ID)
    ]


def test_transaction_only_pipeline_cannot_open_a_graph_session() -> None:
    with pytest.raises(RuntimeError, match="transaction-only pipeline"):
        IngestPipeline(None)._require_client()


def test_bounded_writer_excludes_configured_source_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingPipeline.instances.clear()
    monkeypatch.setattr(bounded, "IngestPipeline", _RecordingPipeline)
    monkeypatch.setattr(
        bounded,
        "_exclusions",
        lambda: ExclusionContext(phones=normalized_phone_set(["81234567"])),
    )

    unit = _writer_unit((_contact_envelope(9),), window=_window("contacts"))

    result = FundboxBoundedWriter().apply(
        cast(ManagedTransaction, _Transaction()),
        _writer_attempt(),
        unit,
    )

    assert result.dispositions == ("excluded",)
    assert _RecordingPipeline.instances[0].records == []


# --- runner-level completion and cutoff -------------------------------------


class _RecordingControl:
    """Minimal commit store that records bounded terminal decisions."""

    def __init__(self) -> None:
        self.reservations: list[Usage] = []
        self.pauses: list[tuple[PauseReason, datetime]] = []
        self.failures: list[tuple[FailureCategory, str]] = []
        self.units: list[BoundedUnit] = []
        self.finalized = 0

    def reserve_usage(
        self,
        context: AttemptContext,
        requested: Usage,
        budget: BoundedIngestionBudget,
    ) -> bool:
        self.reservations.append(requested)
        return True

    def commit_unit(
        self,
        context: AttemptContext,
        unit: BoundedUnit,
        writer: object,
    ) -> UnitApplyResult:
        self.units.append(unit)
        return UnitApplyResult(tuple("committed" for _ in unit.unit.records))

    def pause(
        self,
        context: AttemptContext,
        reason: PauseReason,
        next_eligible_at: datetime,
    ) -> bool:
        self.pauses.append((reason, next_eligible_at))
        return True

    def fail(
        self,
        context: AttemptContext,
        category: FailureCategory,
        safe_message: str,
        next_eligible_at: datetime,
    ) -> bool:
        self.failures.append((category, safe_message))
        return True

    def finalize(self, context: AttemptContext) -> bool:
        self.finalized += 1
        return True


def _settings() -> Settings:
    return Settings(
        neo4j_password="test",
        fundbox_api_base_url="https://fundbox.test/api/v1",
        fundbox_api_username="hyperp",
        fundbox_api_password="secret",
        _env_file=None,
    )


def _runner_attempt(
    descriptor: FundboxBoundedDescriptor,
    occurrence_context: OccurrenceContext,
) -> AttemptContext:
    """Return an admitted attempt for the descriptor's frozen change window."""

    scope = _run_scope(window=_window())
    return replace(
        context(0, run_scope=scope, occurrence_context=occurrence_context),
        checkpoint=descriptor.initial_checkpoint(scope, None),
    )


def _install_stub_source(monkeypatch: pytest.MonkeyPatch, stub: _StubBoundedClient) -> None:
    """Wire the descriptor factory to the scripted bounded client."""

    monkeypatch.setattr(bounded, "get_settings", _settings)
    monkeypatch.setattr(bounded, "FundboxApiClient", lambda *_args, **_kwargs: stub)


def test_runner_completes_only_the_terminal_window_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubBoundedClient(
        [
            _page([_upsert(3, 7, _user_composite(7))], terminal=False, next_cursor="cursor-2"),
            _page([_upsert(4, 8, _user_composite(8))], terminal=True),
        ]
    )
    _install_stub_source(monkeypatch, stub)

    descriptor = FundboxBoundedDescriptor("fundbox", "users", FundboxUsersApiConnector)
    budget = BoundedIngestionBudget()
    window = _open_occurrence()
    clock = FakeClock(datetime.now(UTC))
    control = _RecordingControl()
    runner = BoundedIngestionRunner(budget, clock, FakeShutdown())
    attempt = _runner_attempt(descriptor, window)

    first = runner.run_one(descriptor, attempt, control)

    assert first.status == "paused_with_checkpoint"
    assert control.finalized == 0
    assert [reason for reason, _at in control.pauses] == ["budget"]
    assert control.units[0].terminal is False
    assert stub.calls == 1
    assert stub.max_bytes == [descriptor.max_bytes_per_unit]
    assert len(stub.contexts) == 1
    assert stub.contexts[0].cancellation is not None
    assert stub.contexts[0].operation_deadline_at == clock.now + timedelta(
        seconds=budget.max_unit_seconds
    )
    assert stub.contexts[0].operation_deadline_at < window.cutoff_at

    second = runner.run_one(
        descriptor,
        replace(attempt, checkpoint=control.units[0].unit.checkpoint_after),
        control,
    )

    assert second.status == "completed"
    assert control.finalized == 1
    assert stub.calls == 2
    assert stub.cursors == [None, "cursor-2"]


def test_runner_pauses_with_a_bounded_source_backoff_on_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _open_occurrence()
    clock = FakeClock(datetime.now(UTC))
    retry_at = clock.now + timedelta(seconds=30)
    stub = _StubBoundedClient([SourceBackoffError(retry_at, "fundbox_source_backoff")])
    _install_stub_source(monkeypatch, stub)

    descriptor = FundboxBoundedDescriptor("fundbox", "users", FundboxUsersApiConnector)
    control = _RecordingControl()
    runner = BoundedIngestionRunner(BoundedIngestionBudget(), clock, FakeShutdown())

    result = runner.run_one(descriptor, _runner_attempt(descriptor, window), control)

    assert result.status == "paused_with_checkpoint"
    assert result.pause_reason == "source_backoff"
    assert control.failures == []
    assert control.pauses == [("source_backoff", retry_at)]
    assert retry_at < window.drain_starts_at
    assert stub.calls == 1
    assert stub.stream_calls == 0


# --- trusted registration ---------------------------------------------------


def test_registry_discovers_the_adapter_local_fundbox_descriptors() -> None:
    discovered = BoundedConnectorRegistry(auto_discover=True)

    assert {"fundbox", "fundbox:contacts", "fundbox:sales"} <= set(
        discovered.registered_sources()
    )
    for source_key in ("fundbox", "fundbox:contacts", "fundbox:sales"):
        descriptor = discovered.require(source_key, "delta")
        assert isinstance(descriptor.writer, FundboxBoundedWriter)
        assert descriptor.max_source_requests_per_unit == 1
        assert descriptor.supports_bootstrap is True
        assert descriptor.supports_one_time is False

    with pytest.raises(LookupError, match="requested mode"):
        discovered.require("fundbox:sales", "one_time")
