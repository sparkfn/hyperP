"""Focused lifecycle tests for the closed standalone CRM source-child runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
    canonical_request_payload,
)
from src.standalone_crm_source_child_runtime import (
    SOURCE_CHILD_TASK_NAME,
    StandaloneCrmSourceChildClaim,
    StandaloneCrmSourceChildRegistry,
    StandaloneCrmSourceChildRuntime,
)


def _request() -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        "source-a",
        "control-a",
        "occurrence-a",
        ("contact", "lead", "company"),
        StandaloneCrmBudget(8, 8, 30, 16, 16, 2, "2099-01-01T00:00:00Z"),
        "policy-a",
        "association-a",
        "sha256:" + "a" * 64,
        SourceSyncAuthority(
            "mapping-a",
            "sha256:" + "b" * 64,
            "projection-a",
            "sha256:" + "c" * 64,
        ),
    )


def _payload(kind: str = "contact", bound: int = 2) -> dict[str, object]:
    return {
        "census_id": "census-a",
        "generation": 1,
        "stream_kind": kind,
        "frozen_upper_id": bound,
        "revision_id": None,
        "task_name": SOURCE_CHILD_TASK_NAME,
        "task_id": "task-a",
        "queue": "ingestion",
        "payload_version": "standalone-crm-child-v1",
    }


def _row(cursor: int = 0, processed: int = 0) -> dict[str, object]:
    return {
        "fence_token": 4,
        "fence_owner_id": "task-a",
        "last_committed_id": cursor,
        "binding_subject_id": None,
        "binding_offset": None,
        "processed_rows": processed,
        "skipped_rows": 0,
        "attempt_deadline": "2099-01-01T00:00:00Z",
        "available_at": "2026-01-01T00:00:00Z",
        "request_json": canonical_request_payload(_request()),
    }


@dataclass
class _Repository:
    initial: Mapping[str, object] | None
    refreshed: list[Mapping[str, object] | None] = field(default_factory=list)
    claimed: int = 0
    refreshed_calls: list[tuple[str, int, str]] = field(default_factory=list)
    renewals: list[tuple[str, int, str, int, str]] = field(default_factory=list)
    settlements: list[tuple[str, int, str, int, str, bool]] = field(default_factory=list)
    pauses: list[tuple[str, int, str, str]] = field(default_factory=list)
    converged_occurrences: list[tuple[str, int]] = field(default_factory=list)
    lease_held: bool = False
    preconfirm_pending: bool = False

    def claim_published_child(self, *_: object, **__: object) -> Mapping[str, object] | None:
        self.claimed += 1
        return self.initial

    def published_child_lease_held(self, *_: object, **__: object) -> bool:
        return self.lease_held

    def published_child_preconfirm_pending(self, *_: object, **__: object) -> bool:
        return self.preconfirm_pending

    def refresh_published_child(
        self,
        _: object,
        *,
        owner_id: str,
        fence_token: int,
        payload_json: str,
    ) -> Mapping[str, object] | None:
        self.refreshed_calls.append((owner_id, fence_token, payload_json))
        return self.refreshed.pop(0)

    def renew_unit_fence(
        self,
        census_id: str,
        generation: int,
        stream_kind: str,
        fence_token: int,
        owner_id: str,
    ) -> bool:
        self.renewals.append((census_id, generation, stream_kind, fence_token, owner_id))
        return True

    def settle_unit(
        self,
        census_id: str,
        generation: int,
        stream_kind: str,
        fence_token: int,
        state: str,
        *,
        no_work: bool,
    ) -> bool:
        self.settlements.append((census_id, generation, stream_kind, fence_token, state, no_work))
        return True

    def pause(self, census_id: str, generation: int, code: str, detail: str) -> bool:
        self.pauses.append((census_id, generation, code, detail))
        return True

    def pause_claimed_unit(
        self,
        census_id: str,
        generation: int,
        _: str,
        __: int,
        ___: str,
        ____: str,
        _____: str,
        ______: str,
        _______: int,
        ________: object,
        code: str,
        detail: str,
    ) -> bool:
        self.pauses.append((census_id, generation, code, detail))
        return True

    def converge_occurrence_exhaustion(self, census_id: str, generation: int) -> bool:
        self.converged_occurrences.append((census_id, generation))
        return True


@dataclass
class _Client:
    closed: bool = False

    def close(self) -> None:
        self.closed = True


@dataclass
class _Factory:
    created: list[StandaloneCrmSourceChildClaim] = field(default_factory=list)
    client: _Client = field(default_factory=_Client)

    def create(self, claim: StandaloneCrmSourceChildClaim) -> _Client:
        self.created.append(claim)
        return self.client


@dataclass
class _Handler:
    results: list[str]
    calls: list[int] = field(default_factory=list)

    def run(self, claim: StandaloneCrmSourceChildClaim, _: _Client) -> str:
        self.calls.append(claim.checkpoint.last_committed_id)
        return self.results.pop(0)


class _FailingHandler:
    def run(self, _: StandaloneCrmSourceChildClaim, __: _Client) -> str:
        raise RuntimeError("reserved source effect failed")


class _MissingRegistry:
    def has_task_handler(self, _: str) -> bool:
        return False

    def handler_for(self, _: str) -> _Handler:
        raise AssertionError("missing handler must fail before source construction")


def _runtime(
    repository: _Repository,
    handler: _Handler,
    factory: _Factory | None = None,
) -> tuple[StandaloneCrmSourceChildRuntime, _Factory]:
    used_factory = _Factory() if factory is None else factory
    registry = StandaloneCrmSourceChildRegistry(
        {"contact": handler, "lead": handler, "company": handler}
    )
    return StandaloneCrmSourceChildRuntime(repository, registry, used_factory), used_factory


def test_closed_registry_requires_all_three_source_handlers() -> None:
    handler = _Handler(["no_contact_row"])

    with pytest.raises(ValueError, match="exactly contact, lead, and company"):
        StandaloneCrmSourceChildRegistry({"contact": handler})


def test_missing_handler_and_zero_bound_fail_before_client_construction() -> None:
    repository = _Repository(_row())
    factory = _Factory()
    runtime = StandaloneCrmSourceChildRuntime(repository, _MissingRegistry(), factory)

    with pytest.raises(RuntimeError, match="not registered"):
        runtime.run(_payload(), worker_id="task-a")
    with pytest.raises(ValueError, match="invalid bound or stream"):
        runtime.run(_payload(bound=0), worker_id="task-a")

    assert repository.claimed == 0
    assert factory.created == []


def test_broker_task_identity_must_equal_the_exact_durable_publication() -> None:
    repository = _Repository(_row())
    runtime, factory = _runtime(repository, _Handler(["no_contact_row"]))

    with pytest.raises(RuntimeError, match="identity does not match publication"):
        runtime.run(_payload(), worker_id="other-task")

    assert repository.claimed == 0
    assert factory.created == []


def test_raw_or_underspecified_child_payload_cannot_reach_claim_or_source_setup() -> None:
    repository = _Repository(_row())
    runtime, factory = _runtime(repository, _Handler(["no_contact_row"]))
    missing_queue = _payload()
    del missing_queue["queue"]
    raw_row_payload = _payload()
    raw_row_payload["rows"] = []

    with pytest.raises(ValueError, match="exact stored v1 publication"):
        runtime.run(missing_queue, worker_id="task-a")
    with pytest.raises(ValueError, match="exact stored v1 publication"):
        runtime.run(raw_row_payload, worker_id="task-a")

    assert repository.claimed == 0
    assert factory.created == []


@pytest.mark.parametrize("claim_denial", ["stale_authority", "duplicate_delivery", "cancelled"])
def test_denied_stale_duplicate_or_cancelled_claim_does_not_construct_a_source_client(
    claim_denial: str,
) -> None:
    repository = _Repository(None)
    runtime, factory = _runtime(repository, _Handler(["no_contact_row"]))

    assert runtime.run(_payload(), worker_id="task-a") == "terminal_denied"
    assert claim_denial in {"stale_authority", "duplicate_delivery", "cancelled"}
    assert repository.claimed == 1
    assert factory.created == []


def test_exact_active_lease_is_retryable_without_constructing_a_source_client() -> None:
    repository = _Repository(None, lease_held=True)
    runtime, factory = _runtime(repository, _Handler(["no_contact_row"]))

    assert runtime.run(_payload(), worker_id="task-a") == "lease_held_retryable"
    assert repository.claimed == 1
    assert factory.created == []


def test_exact_broker_delivery_before_parent_confirmation_is_retryable_without_source_io() -> None:
    repository = _Repository(None, preconfirm_pending=True)
    runtime, factory = _runtime(repository, _Handler(["no_contact_row"]))

    assert runtime.run(_payload(), worker_id="task-a") == "publication_pending_retryable"
    assert repository.claimed == 1
    assert factory.created == []


def test_runtime_consumes_one_fenced_unit_until_bounded_no_row_then_settles() -> None:
    repository = _Repository(_row(), [_row(cursor=1, processed=1)])
    handler = _Handler(["contact_completed", "no_contact_row"])
    runtime, factory = _runtime(repository, handler)

    assert runtime.run(_payload(), worker_id="task-a") == "unit_completed"
    assert handler.calls == [0, 1]
    assert repository.renewals == [("census-a", 1, "contact", 4, "task-a")]
    assert len(repository.refreshed_calls) == 1
    assert repository.settlements == [("census-a", 1, "contact", 4, "completed", False)]
    assert factory.client.closed is True


def test_empty_bounded_unit_is_settled_as_no_work() -> None:
    repository = _Repository(_row())
    runtime, _ = _runtime(repository, _Handler(["no_company_row"]))

    assert runtime.run(_payload("company"), worker_id="task-a") == "unit_no_work"
    assert repository.settlements == [("census-a", 1, "company", 4, "no_work", True)]


def test_attempt_exhaustion_and_source_failure_pause_at_the_current_checkpoint() -> None:
    exhausted = _Repository(_row())
    runtime, _ = _runtime(exhausted, _Handler(["attempt_exhausted"]))
    failed = _Repository(_row())
    failed_runtime, _ = _runtime(failed, _FailingHandler())

    assert runtime.run(_payload(), worker_id="task-a") == "paused_with_checkpoint"
    assert failed_runtime.run(_payload(), worker_id="task-a") == "paused_with_checkpoint"
    assert exhausted.pauses[0][2] == "attempt_budget_exhausted"
    assert failed.pauses[0][2] == "source_effect_failed"
    assert exhausted.settlements == []
    assert failed.settlements == []


def test_occurrence_exhaustion_uses_the_existing_terminal_convergence_operation() -> None:
    repository = _Repository(_row())
    runtime, _ = _runtime(repository, _Handler(["occurrence_exhausted"]))

    assert runtime.run(_payload(), worker_id="task-a") == "occurrence_exhausted"
    assert repository.converged_occurrences == [("census-a", 1)]
    assert repository.pauses == []
    assert repository.settlements == []
