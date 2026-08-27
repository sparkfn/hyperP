"""Typed fake transaction coverage for child terminal settlement ordering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar, cast

import pytest
from neo4j import ManagedTransaction, Record
from src.graph.client import Neo4jClient
from src.graph.queries import standalone_crm_census as queries
from src.graph.standalone_crm_census_checkpoint_ops import StandaloneCrmCheckpointOperations
from src.graph.standalone_crm_census_core_ops import StandaloneCrmCensusCoreOperations
from src.graph.standalone_crm_census_types import (
    StandaloneCrmCensusAdmission,
    StandaloneCrmCensusStaleError,
)
from src.standalone_crm_census_models import (
    StandaloneCrmAttempt,
    StandaloneCrmCallIntent,
    StandaloneCrmCheckpoint,
    StandaloneCrmFreshness,
)

_T = TypeVar("_T")


class _Result:
    def __init__(self, record: dict[str, object] | None) -> None:
        self._record = record

    def single(self) -> Record | None:
        return cast(Record | None, self._record)


class _Transaction:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.queries: list[str] = []

    def run(self, query: str, **_params: object) -> _Result:
        self.queries.append(query)
        if query == queries.VALIDATE_SETTLE_UNIT:
            return _Result({"version": 1} if self.valid else None)
        if query == queries.SETTLE_UNIT:
            return _Result({"version": 2})
        raise AssertionError("unexpected query")


class _Client:
    def __init__(self, transaction: _Transaction) -> None:
        self._transaction = transaction

    def execute_write(self, work: Callable[[ManagedTransaction], _T]) -> _T:
        return work(cast(ManagedTransaction, self._transaction))


class _Operations(StandaloneCrmCheckpointOperations):
    def __init__(self, transaction: _Transaction) -> None:
        self._client = cast(Neo4jClient, _Client(transaction))


def _admission() -> StandaloneCrmCensusAdmission:
    return StandaloneCrmCensusAdmission(
        "census", "running", "fingerprint", "authority", "source", "control", False
    )


def _checkpoint() -> StandaloneCrmCheckpoint:
    return StandaloneCrmCheckpoint(
        "census",
        "contact",
        2,
        1,
        None,
        1,
        0,
        0,
        0,
        1,
        1,
        2,
        "child",
    )


def test_settlement_validates_then_runs_callback_then_commits() -> None:
    transaction = _Transaction()
    operations = _Operations(transaction)
    events: list[str] = []

    def work(_tx: ManagedTransaction) -> None:
        assert transaction.queries == [queries.VALIDATE_SETTLE_UNIT]
        events.append("callback")

    version = operations.settle_child_with_work(
        _admission(),
        _checkpoint(),
        terminal_state="completed",
        expected_version=1,
        max_rows_per_attempt=3,
        max_rows_per_occurrence=4,
        work=work,
    )

    assert version == 2
    assert events == ["callback"]
    assert transaction.queries == [queries.VALIDATE_SETTLE_UNIT, queries.SETTLE_UNIT]


def test_settlement_invalid_guard_or_callback_error_never_commits() -> None:
    invalid = _Transaction(valid=False)
    operations = _Operations(invalid)
    called = False

    def unexpected(_tx: ManagedTransaction) -> None:
        nonlocal called
        called = True

    with pytest.raises(StandaloneCrmCensusStaleError):
        operations.settle_child_with_work(
            _admission(),
            _checkpoint(),
            terminal_state="cancelled",
            expected_version=1,
            max_rows_per_attempt=3,
            max_rows_per_occurrence=4,
            work=unexpected,
        )
    assert not called
    assert invalid.queries == [queries.VALIDATE_SETTLE_UNIT]

    failed_callback = _Transaction()
    callback_operations = _Operations(failed_callback)
    with pytest.raises(RuntimeError, match="domain failure"):
        callback_operations.settle_child_with_work(
            _admission(),
            _checkpoint(),
            terminal_state="completed",
            expected_version=1,
            max_rows_per_attempt=3,
            max_rows_per_occurrence=4,
            work=lambda _tx: (_ for _ in ()).throw(RuntimeError("domain failure")),
        )
    assert failed_callback.queries == [queries.VALIDATE_SETTLE_UNIT]


@dataclass
class _CancellationFake:
    unit_state: str
    publication_state: str | None
    fence_state: str | None
    checkpoint_version: int
    checkpoint_count: int
    cancel_requested: bool = False
    fence_owner: str | None = None

    def cancel(self) -> int:
        if self.cancel_requested:
            return 0
        self.cancel_requested = True
        if self.unit_state in {"pending_publication", "queued", "paused"}:
            self.unit_state = "cancelled"
            self.checkpoint_count = max(self.checkpoint_count, 1)
            return 1
        if self.publication_state in {"reserved", "publishing", "ambiguous"}:
            self.publication_state = "retired"
            self.unit_state = "cancelled"
            self.checkpoint_count = max(self.checkpoint_count, 1)
            return 1
        if self.fence_state == "active":
            self.fence_state = "cancel_requested"
        return 0

    def settle_cancelled(self, owner: str, committed_count: int) -> None:
        if not self.cancel_requested or self.fence_state != "cancel_requested":
            raise RuntimeError("cancellation settlement is not authorized")
        if owner != self.fence_owner or committed_count < self.checkpoint_count:
            raise RuntimeError("stale owner or checkpoint regression")
        self.checkpoint_count = committed_count
        self.checkpoint_version += 1
        self.unit_state = "cancelled"
        self.fence_state = "released"

    def reconcile(self) -> str:
        if not self.cancel_requested:
            raise RuntimeError("cancellation has not settled")
        if self.publication_state == "published" and (
            self.fence_state != "released" or self.checkpoint_version <= 1
        ):
            raise RuntimeError("published child remains unsettled")
        if self.unit_state != "cancelled":
            raise RuntimeError("cancellation has not settled")
        if self.publication_state in {None, "retired"}:
            if self.fence_state is not None:
                raise RuntimeError("unstarted cancellation has a fence")
        elif self.publication_state == "published":
            if self.fence_state != "released" or self.checkpoint_version <= 1:
                raise RuntimeError("published child remains unsettled")
        else:
            raise RuntimeError("publication remains unresolved")
        if self.checkpoint_count != 1:
            raise RuntimeError("terminal accounting is not balanced")
        return "cancelled_with_checkpoint"


@pytest.mark.parametrize(
    ("publication_state", "unit_state"),
    [(None, "pending_publication"), ("reserved", "publishing"), ("publishing", "publishing")],
)
def test_fake_cancellation_retires_unstarted_work_and_reconciles(
    publication_state: str | None, unit_state: str
) -> None:
    lifecycle = _CancellationFake(unit_state, publication_state, None, 1, 0)
    assert lifecycle.cancel() == 1
    assert lifecycle.cancel() == 0
    assert lifecycle.publication_state in {None, "retired"}
    assert lifecycle.reconcile() == "cancelled_with_checkpoint"


def test_fake_active_partial_checkpoint_requires_exact_owner_settlement() -> None:
    lifecycle = _CancellationFake("running", "published", "active", 2, 1, fence_owner="child")
    assert lifecycle.cancel() == 0
    with pytest.raises(RuntimeError, match="stale owner"):
        lifecycle.settle_cancelled("other", 1)
    with pytest.raises(RuntimeError, match="checkpoint regression"):
        lifecycle.settle_cancelled("child", 0)
    lifecycle.settle_cancelled("child", 1)
    assert lifecycle.reconcile() == "cancelled_with_checkpoint"


def test_fake_published_unclaimed_child_must_claim_and_settle_before_terminal() -> None:
    lifecycle = _CancellationFake("publishing", "published", None, 1, 0)
    assert lifecycle.cancel() == 0
    with pytest.raises(RuntimeError, match="published child remains unsettled"):
        lifecycle.reconcile()
    lifecycle.fence_state = "cancel_requested"
    lifecycle.fence_owner = "child"
    lifecycle.settle_cancelled("child", 1)
    assert lifecycle.reconcile() == "cancelled_with_checkpoint"


class _UnknownCallTransaction:
    def __init__(self, outcomes: list[bool]) -> None:
        self._outcomes = outcomes
        self.params: list[dict[str, object]] = []
        self.queries: list[str] = []

    def run(self, query: str, **params: object) -> _Result:
        assert query in {
            queries.CLASSIFY_RESERVED_HTTP_CALL_UNKNOWN,
            queries.CLASSIFY_CURRENT_RESERVED_HTTP_CALL_UNKNOWN,
        }
        self.queries.append(query)
        self.params.append(params)
        return _Result({"intent_id": "intent"} if self._outcomes.pop(0) else None)


class _UnknownCallClient:
    def __init__(self, transaction: _UnknownCallTransaction) -> None:
        self._transaction = transaction

    def execute_write(self, work: Callable[[ManagedTransaction], _T]) -> _T:
        return work(cast(ManagedTransaction, self._transaction))


class _UnknownCallOperations(StandaloneCrmCensusCoreOperations):
    def __init__(self, transaction: _UnknownCallTransaction) -> None:
        self._client = cast(Neo4jClient, _UnknownCallClient(transaction))


def _call_intent() -> StandaloneCrmCallIntent:
    now = datetime.now(UTC)
    attempt = StandaloneCrmAttempt(
        "census",
        2,
        "parent",
        "running",
        3,
        now + timedelta(seconds=60),
        now + timedelta(seconds=90),
    )
    return StandaloneCrmCallIntent(
        "census",
        attempt.generation,
        attempt.parent_fence_token,
        StandaloneCrmFreshness("census", "fingerprint", "authority", "source", "control"),
        "intent",
        1,
        "page",
        "contact",
        0,
        "sha256:metadata",
        cursor_id=0,
        upper_id=1,
    )


def test_unknown_call_classification_is_one_way_and_fenced_in_the_repository() -> None:
    transaction = _UnknownCallTransaction([True, False])
    operations = _UnknownCallOperations(transaction)
    intent = _call_intent()
    assert operations.classify_reserved_call_unknown(intent)
    assert not operations.classify_reserved_call_unknown(intent)
    assert [params["intent_id"] for params in transaction.params] == ["intent", "intent"]
    assert all(params["generation"] == 2 for params in transaction.params)
    assert all(params["parent_fence_token"] == 3 for params in transaction.params)


def test_operator_unknown_call_classification_derives_current_authority_from_admission() -> None:
    transaction = _UnknownCallTransaction([True, False])
    operations = _UnknownCallOperations(transaction)
    admission = StandaloneCrmCensusAdmission(
        "census", "running", "fingerprint", "authority", "source", "control", False
    )
    assert operations.classify_current_reserved_call_unknown(admission, intent_id="intent")
    assert not operations.classify_current_reserved_call_unknown(admission, intent_id="intent")
    assert transaction.queries == [
        queries.CLASSIFY_CURRENT_RESERVED_HTTP_CALL_UNKNOWN,
        queries.CLASSIFY_CURRENT_RESERVED_HTTP_CALL_UNKNOWN,
    ]
    assert all(params["fingerprint"] == "fingerprint" for params in transaction.params)
    assert all("generation" not in params for params in transaction.params)
    assert all("parent_fence_token" not in params for params in transaction.params)
