"""Repository contracts for implemented standalone CRM census persistence."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

import pytest
from src.graph import standalone_crm_census_admission as repository_module
from src.graph.client import Neo4jClient
from src.graph.queries.standalone_crm_census import (
    ADMIT_CENSUS,
    CLAIM_ATTEMPT,
    CLAIM_PUBLISHED_CHILD,
    CLOSE_CONTACT_BINDING_POSITION,
    CONVERGE_LIMIT_DENIAL,
    CREATE_CONTINUATION,
    FAIL_AFTER_WINDOW_AUTHORITY,
    LEASE_HELD_PUBLISHED_CHILD,
    PAUSE_CLAIMED_UNIT,
    PRECONFIRM_PUBLISHED_CHILD,
    RECORD_CALL_OUTCOME,
    REFRESH_PUBLISHED_CHILD,
    REQUEST_CANCELLATION,
    REQUEST_UNIT_STOPS,
    RESERVE_CALL,
    STORE_CHECKPOINT,
)
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.graph.standalone_crm_census_records import (
    StandaloneCrmAttemptTakeover,
    StandaloneCrmRuntimeSnapshot,
    authority_context,
    authority_revision,
)
from src.standalone_crm_census_models import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
    StandaloneCrmCallIntent,
    StandaloneCrmCallOutcome,
    StandaloneCrmCensusConflictError,
    StandaloneCrmCheckpoint,
    StandaloneCrmChildEnvelope,
    StandaloneCrmReason,
    canonical_request_payload,
    census_fingerprint,
)


@dataclass(frozen=True)
class _Run:
    query: str
    parameters: dict[str, object]


class _Result:
    def __init__(self, record: dict[str, object] | None) -> None:
        self._record = record

    def single(self) -> dict[str, object] | None:
        return self._record

    def consume(self) -> None:
        return None


class _Transaction:
    def __init__(self, records: list[dict[str, object] | None]) -> None:
        self._records = records
        self.runs: list[_Run] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.runs.append(_Run(query, parameters))
        return _Result(self._records.pop(0))


class _Client:
    def __init__(self, records: list[dict[str, object] | None]) -> None:
        self.transaction = _Transaction(records)
        self.write_calls = 0
        self.read_calls = 0

    def execute_write(self, work: Callable[[_Transaction], object]) -> object:
        self.write_calls += 1
        return work(self.transaction)

    def execute_read(self, work: Callable[[_Transaction], object]) -> object:
        self.read_calls += 1
        return work(self.transaction)


class _SourceAdmissions:
    calls: list[tuple[object, str, str]] = []

    def __init__(self, client: object) -> None:
        self._client = client

    def admit(self, *, control_instance_id: str, source_instance_id: str) -> None:
        self.calls.append((self._client, control_instance_id, source_instance_id))


def _repository(client: _Client) -> StandaloneCrmCensusRepository:
    return StandaloneCrmCensusRepository(cast(Neo4jClient, client))


def _request() -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        "portal-a",
        "control-a",
        "occurrence-a",
        ("contact", "lead"),
        StandaloneCrmBudget(2, 3, 4, 5, 6, 7, "2099-01-02T00:00:00Z"),
        "policy-v1",
        "association-v1",
        "sha256:" + "a" * 64,
        SourceSyncAuthority(
            "mapping-a", "sha256:" + "b" * 64, "projection-a", "sha256:" + "c" * 64
        ),
    )


def _intent() -> StandaloneCrmCallIntent:
    return StandaloneCrmCallIntent(
        "census-a",
        3,
        "intent-a",
        8,
        "page",
        "lead",
        2,
        "2099-01-02T00:00:00Z",
        17,
        None,
        "published-task-a",
    )


def _snapshot(
    *, generation: int = 3, cancel_requested: bool = False
) -> StandaloneCrmRuntimeSnapshot:
    return StandaloneCrmRuntimeSnapshot(_request(), generation, "running", cancel_requested)


def _source_envelope() -> StandaloneCrmChildEnvelope:
    return StandaloneCrmChildEnvelope(
        "census-a",
        3,
        "contact",
        19,
        None,
        "src.standalone_crm_census_tasks.run_standalone_crm_census_unit",
        "task-a",
        "ingestion",
    )


def test_refresh_published_child_reasserts_exact_active_task_fence_and_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(
        [
            {
                "fence_token": 12,
                "fence_owner_id": "task-a",
                "last_committed_id": 7,
                "binding_subject_id": None,
                "binding_offset": None,
                "processed_rows": 7,
                "skipped_rows": 0,
                "attempt_deadline": "2099-01-02T00:00:00Z",
                "available_at": "2026-08-29T00:00:00Z",
                "request_json": canonical_request_payload(_request()),
            }
        ]
    )
    repository = _repository(client)
    envelope = _source_envelope()
    monkeypatch.setattr(repository, "runtime_snapshot", lambda _census_id: _snapshot())

    refreshed = repository.refresh_published_child(
        envelope,
        owner_id="task-a",
        fence_token=12,
        payload_json='{"frozen":true}',
    )

    assert refreshed is not None
    assert client.write_calls == 0
    assert client.read_calls == 1
    run = client.transaction.runs[0]
    assert run.query == REFRESH_PUBLISHED_CHILD
    assert run.parameters["task_name"] == envelope.task_name
    assert run.parameters["task_id"] == envelope.task_id
    assert run.parameters["payload_digest"] == envelope.payload_digest()
    assert run.parameters["payload_json"] == '{"frozen":true}'
    assert run.parameters["fence_token"] == 12
    assert run.parameters["owner_id"] == "task-a"
    assert run.parameters["frozen_upper_id"] == 19


def test_lease_held_classification_reasserts_current_admission_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client([{"owner_id": "other-worker"}])
    repository = _repository(client)
    envelope = _source_envelope()
    monkeypatch.setattr(repository, "runtime_snapshot", lambda _census_id: _snapshot())

    assert repository.published_child_lease_held(
        envelope, owner_id="task-a", payload_json='{"frozen":true}'
    )

    assert client.write_calls == 0
    assert client.read_calls == 1
    run = client.transaction.runs[0]
    assert run.query == LEASE_HELD_PUBLISHED_CHILD
    assert run.parameters["request_json"] == canonical_request_payload(_request())
    assert run.parameters["authority_revision"] == authority_revision(_request())
    assert run.parameters["authority_json"] == authority_context(_request())
    for term in (
        "attempt_deadline",
        "occurrence_call_limit",
        "attempt_call_limit",
        "unit.state IN ['queued', 'running', 'paused']",
        "BitrixSourceInstance",
        "BitrixExecutionSourceBinding",
    ):
        assert term in LEASE_HELD_PUBLISHED_CHILD
    assert "fence.owner_id <> $owner_id" not in LEASE_HELD_PUBLISHED_CHILD


def test_preconfirmation_delivery_is_read_only_retryable_for_the_exact_publishing_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client([{"pending": 1}])
    repository = _repository(client)
    envelope = _source_envelope()
    monkeypatch.setattr(repository, "runtime_snapshot", lambda _census_id: _snapshot())

    assert repository.published_child_preconfirm_pending(envelope, payload_json='{"frozen":true}')

    assert client.write_calls == 0
    assert client.read_calls == 1
    run = client.transaction.runs[0]
    assert run.query == PRECONFIRM_PUBLISHED_CHILD
    assert run.parameters["task_id"] == envelope.task_id
    assert run.parameters["payload_digest"] == envelope.payload_digest()
    assert "status: 'publishing'" in PRECONFIRM_PUBLISHED_CHILD
    assert "RETURN 1 AS pending" in PRECONFIRM_PUBLISHED_CHILD


def test_child_claim_rebinds_only_a_compatible_checkpoint_fence_token() -> None:
    assert "checkpoint.generation = $generation" in CLAIM_PUBLISHED_CHILD
    assert "checkpoint.frozen_upper_id = $frozen_upper_id" in CLAIM_PUBLISHED_CHILD
    assert "checkpoint.revision_id IS NULL" in CLAIM_PUBLISHED_CHILD
    assert "StandaloneCrmCensusContinuation" in CLAIM_PUBLISHED_CHILD
    assert "SET checkpoint.generation = $generation" in CLAIM_PUBLISHED_CHILD
    assert CLAIM_PUBLISHED_CHILD.index("OPTIONAL MATCH (checkpoint") < CLAIM_PUBLISHED_CHILD.index(
        "MERGE (fence"
    )
    rebind = CLAIM_PUBLISHED_CHILD.split("FOREACH (_ IN CASE WHEN checkpoint IS NULL", 1)[1]
    assert "checkpoint.fence_token = fence.token" in rebind
    for preserved in (
        "checkpoint.last_committed_id",
        "checkpoint.processed_rows",
        "checkpoint.skipped_rows",
        "checkpoint.binding_subject_id",
        "checkpoint.binding_offset",
    ):
        assert f"SET {preserved}" not in rebind


def test_durable_claimed_pause_creates_or_preserves_exact_checkpoint_before_resumable_status() -> (
    None
):
    for term in (
        "MERGE (stored:StandaloneCrmCensusCheckpoint",
        "unit.state = 'paused'",
        "attempt.status = 'paused_with_checkpoint'",
        "census.status = 'paused_with_checkpoint'",
        "fence_token = $fence_token",
        "task_name: $task_name",
        "payload_digest: $payload_digest",
    ):
        assert term in PAUSE_CLAIMED_UNIT


@pytest.fixture
def census_prerequisites(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, str, str]]:
    _SourceAdmissions.calls = []
    monkeypatch.setattr(
        repository_module, "assert_standalone_crm_census_ready", lambda _client: None
    )
    monkeypatch.setattr(repository_module, "BitrixSourceInstanceRepository", _SourceAdmissions)
    return _SourceAdmissions.calls


def test_admit_writes_canonical_identity_and_immutable_request_fingerprint(
    census_prerequisites: list[tuple[object, str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _Client(
        [
            {
                "census_id": "census-a",
                "status": "allocated",
                "replayed": False,
                "fingerprint_match": True,
            }
        ]
    )
    request = _request()
    monkeypatch.setattr(
        "src.graph.standalone_crm_census_admission.uuid.uuid4",
        lambda: type("Id", (), {"hex": "allocated-id"})(),
    )

    admission = _repository(client).admit(request)

    assert (admission.census_id, admission.status, admission.replayed) == (
        "census-a",
        "allocated",
        False,
    )
    assert census_prerequisites == [(client, "control-a", "portal-a")]
    assert client.write_calls == 1
    assert client.transaction.runs == [
        _Run(
            ADMIT_CENSUS,
            {
                "census_id": "allocated-id",
                "source_key": "bitrix_chat",
                "source_instance_id": "portal-a",
                "control_instance_id": "control-a",
                "census_kind": "source_sync",
                "occurrence_key": "occurrence-a",
                "scope_key": "bitrix_chat\x1fportal-a\x1fcontrol-a\x1fsource_sync",
                "fingerprint": census_fingerprint(request),
                "authority_revision": "sha256:" + "b" * 64 + ":sha256:" + "c" * 64,
                "authority_json": authority_context(request),
                "request_json": canonical_request_payload(request),
            },
        )
    ]


@pytest.mark.parametrize(
    "record",
    [
        None,
        {
            "census_id": "census-a",
            "status": "allocated",
            "replayed": False,
            "fingerprint_match": False,
        },
    ],
)
def test_admit_rejects_ambiguous_or_conflicting_occurrences(
    census_prerequisites: list[tuple[object, str, str]], record: dict[str, object] | None
) -> None:
    client = _Client([record])

    with pytest.raises(StandaloneCrmCensusConflictError, match="occurrence conflicts"):
        _repository(client).admit(_request())

    assert client.write_calls == 1
    assert census_prerequisites == [(client, "control-a", "portal-a")]


@pytest.mark.parametrize("record, expected", [({"generation": 4}, True), (None, False)])
def test_claim_attempt_carries_generation_fence_and_lease(
    record: dict[str, object] | None, expected: bool
) -> None:
    client = _Client([record] if record is not None else [None, None])

    assert _repository(client).claim_attempt("census-a", 4, 19, _request()) is expected
    expected_runs = [
        _Run(
            CLAIM_ATTEMPT,
            {
                "census_id": "census-a",
                "generation": 4,
                "fence_token": 19,
                "attempt_task_id": "standalone-crm-parent:census-a:4",
                "lease_seconds": 120,
                "max_attempts": 7,
                "occurrence_deadline": "2099-01-02T00:00:00Z",
                "attempt_runtime_seconds": 4,
                "authority_revision": "sha256:" + "b" * 64 + ":sha256:" + "c" * 64,
                "authority_json": authority_context(_request()),
            },
        )
    ]
    if record is None:
        expected_runs.append(
            _Run(
                CONVERGE_LIMIT_DENIAL,
                {
                    "census_id": "census-a",
                    "generation": 4,
                    "reason_code": "attempts_exhausted",
                    "max_attempts": 7,
                    "occurrence_deadline": "2099-01-02T00:00:00Z",
                    "authority_revision": "sha256:" + "b" * 64 + ":sha256:" + "c" * 64,
                    "authority_json": authority_context(_request()),
                },
            )
        )
    assert client.transaction.runs == expected_runs


def test_continuation_uses_captured_authority_and_immutable_occurrence_budget() -> None:
    client = _Client([{"generation": 5}])

    assert _repository(client).create_continuation("census-a", 4, _request()) == 5
    assert client.transaction.runs == [
        _Run(
            CREATE_CONTINUATION,
            {
                "census_id": "census-a",
                "generation": 4,
                "next_generation": 5,
                "attempt_task_id": "standalone-crm-parent:census-a:5",
                "authority_revision": "sha256:" + "b" * 64 + ":sha256:" + "c" * 64,
                "authority_json": authority_context(_request()),
                "max_attempts": 7,
                "occurrence_deadline": "2099-01-02T00:00:00Z",
                "attempt_runtime_seconds": 4,
                "lease_seconds": 120,
            },
        )
    ]


def test_expired_attempt_takeover_only_advances_the_current_generation_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client([{"generation": 5, "fence_token": 41}])
    repository = _repository(client)
    monkeypatch.setattr(repository, "runtime_snapshot", lambda _census_id: _snapshot(generation=4))

    takeover = repository.recover_or_take_over_attempt("census-a", 4, 5, 41)

    assert takeover == StandaloneCrmAttemptTakeover(5, 41)
    assert client.write_calls == 1
    assert client.transaction.runs[0].parameters == {
        "census_id": "census-a",
        "prior_generation": 4,
        "next_generation": 5,
        "fence_token": 41,
        "attempt_task_id": "standalone-crm-parent:census-a:5",
        "lease_seconds": 120,
        "authority_revision": "sha256:" + "b" * 64 + ":sha256:" + "c" * 64,
        "authority_json": authority_context(_request()),
        "max_attempts": 7,
        "occurrence_deadline": "2099-01-02T00:00:00Z",
        "attempt_runtime_seconds": 4,
    }


def test_expired_attempt_takeover_refuses_a_stale_parent_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client([])
    repository = _repository(client)
    monkeypatch.setattr(repository, "runtime_snapshot", lambda _census_id: _snapshot(generation=3))

    assert repository.recover_or_take_over_attempt("census-a", 4, 5, 41) is None
    assert client.write_calls == 0
    assert client.transaction.runs == []


@pytest.mark.parametrize(
    "record, expected", [({"intent_id": "intent-a", "call_sequence": 1}, True), (None, False)]
)
def test_reserve_call_uses_an_atomic_occurrence_wide_sequence_and_structured_authority_context(
    record: dict[str, object] | None, expected: bool
) -> None:
    client = _Client([record])

    assert _repository(client).reserve_call(_intent(), 29, _request()) is expected
    assert client.transaction.runs == [
        _Run(
            RESERVE_CALL,
            {
                "census_id": "census-a",
                "generation": 3,
                "fence_token": 29,
                "intent_id": "intent-a",
                "call_kind": "page",
                "stream_kind": "lead",
                "retry_ordinal": 2,
                "cursor": 17,
                "subject_id": None,
                "deadline": "2099-01-02T00:00:00Z",
                "effective_deadline": "2099-01-02T00:00:00Z",
                "task_id": "published-task-a",
                "occurrence_call_limit": 5,
                "attempt_call_limit": 2,
                "authority_revision": "sha256:" + "b" * 64 + ":sha256:" + "c" * 64,
                "authority_json": authority_context(_request()),
            },
        )
    ]
    assert "$call_sequence" not in RESERVE_CALL
    assert "SET census.call_sequence = coalesce(census.call_sequence, 0) + 1" in RESERVE_CALL
    assert "call_sequence: census.call_sequence" in RESERVE_CALL
    stored_authority = client.transaction.runs[0].parameters["authority_json"]
    assert isinstance(stored_authority, str)
    assert json.loads(stored_authority) == {
        "mapping_head_digest": "sha256:" + "b" * 64,
        "mapping_head_id": "mapping-a",
        "projection_head_digest": "sha256:" + "c" * 64,
        "projection_head_id": "projection-a",
    }


def test_page_and_binding_reservations_use_child_fences_not_the_parent_attempt_fence() -> None:
    client = _Client(
        [
            {"intent_id": "page-intent", "call_sequence": 1},
            {"intent_id": "binding-intent", "call_sequence": 2},
        ]
    )
    page = StandaloneCrmCallIntent(
        "census-a",
        3,
        "page-intent",
        1,
        "page",
        "contact",
        0,
        "2099-01-02T00:00:00Z",
        17,
        None,
        "published-child-task",
    )
    binding = StandaloneCrmCallIntent(
        "census-a",
        3,
        "binding-intent",
        1,
        "company_binding",
        "contact",
        0,
        "2099-01-02T00:00:00Z",
        17,
        42,
        "published-child-task",
    )

    assert _repository(client).reserve_call_with_sequence(page, 29, _request()) == 1
    assert _repository(client).reserve_call_with_sequence(binding, 47, _request()) == 2

    page_parameters, binding_parameters = (run.parameters for run in client.transaction.runs)
    assert page_parameters["fence_token"] == 29
    assert binding_parameters["fence_token"] == 47
    assert page_parameters["task_id"] == binding_parameters["task_id"] == "published-child-task"
    assert page_parameters["call_kind"] == "page"
    assert binding_parameters["call_kind"] == "company_binding"


def test_post_window_authority_failure_is_one_atomic_repository_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client([{"census_id": "census-a"}])
    repository = _repository(client)
    monkeypatch.setattr(
        repository,
        "runtime_snapshot",
        lambda _census_id: StandaloneCrmRuntimeSnapshot(_request(), 3, "running", False, True),
    )

    assert repository.fail_after_window_authority(
        "census-a", 3, StandaloneCrmReason("authority_stale", "authority changed")
    )
    assert client.write_calls == 1
    assert [run.query for run in client.transaction.runs] == [FAIL_AFTER_WINDOW_AUTHORITY]
    assert client.transaction.runs[0].parameters["reason_code"] == "authority_stale"


def test_stale_generation_refuses_checkpoint_before_any_mutating_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client([])
    repository = _repository(client)
    monkeypatch.setattr(repository, "runtime_snapshot", lambda _census_id: _snapshot(generation=2))
    checkpoint = StandaloneCrmCheckpoint("census-a", "lead", 17, None, 17, None, None, 4, 0, 3, 29)

    result = repository.store_checkpoint(checkpoint, attempt_rows=4, occurrence_rows=4)

    assert result.decision == "stale_or_conflict"
    assert client.write_calls == 0
    assert client.transaction.runs == []


def test_stale_checkpoint_decision_has_one_atomic_query_and_no_partial_mutation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client([{"decision": "stale_or_conflict"}])
    repository = _repository(client)
    monkeypatch.setattr(repository, "runtime_snapshot", lambda _census_id: _snapshot())
    checkpoint = StandaloneCrmCheckpoint("census-a", "lead", 17, None, 17, None, None, 4, 0, 3, 29)

    result = repository.store_checkpoint(checkpoint, attempt_rows=4, occurrence_rows=4)

    assert result.decision == "stale_or_conflict"
    assert client.transaction.runs == [
        _Run(
            STORE_CHECKPOINT,
            {
                "census_id": "census-a",
                "generation": 3,
                "fence_token": 29,
                "stream_kind": "lead",
                "last_committed_id": 17,
                "binding_subject_id": None,
                "binding_offset": None,
                "processed_rows": 4,
                "skipped_rows": 0,
                "attempt_row_limit": 3,
                "occurrence_row_limit": 6,
                "authority_revision": "sha256:" + "b" * 64 + ":sha256:" + "c" * 64,
                "authority_json": authority_context(_request()),
                "allow_cancel_checkpoint": False,
                "occurrence_deadline": "2099-01-02T00:00:00Z",
            },
        )
    ]
    assert "CASE WHEN NOT valid OR row_delta < 0 THEN 'stale_or_conflict'" in STORE_CHECKPOINT
    stored_foreach = "FOREACH (_ IN CASE WHEN decision = 'stored' THEN [1] ELSE [] END | "
    assert stored_foreach in STORE_CHECKPOINT
    assert (
        "census.occurrence_rows = coalesce(census.occurrence_rows, 0) + row_delta"
        in (STORE_CHECKPOINT.split(stored_foreach, 1)[1])
    )


def test_stale_cancellation_does_not_request_unit_stops_after_failed_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client([None])
    repository = _repository(client)
    monkeypatch.setattr(repository, "runtime_snapshot", lambda _census_id: _snapshot())

    assert repository.request_cancellation("census-a", "operator", "no longer needed") is False
    assert client.transaction.runs == [
        _Run(
            REQUEST_CANCELLATION,
            {
                "census_id": "census-a",
                "actor": "operator",
                "reason": "no longer needed",
                "authority_revision": "sha256:" + "b" * 64 + ":sha256:" + "c" * 64,
                "authority_json": authority_context(_request()),
            },
        )
    ]
    assert all(run.query != REQUEST_UNIT_STOPS for run in client.transaction.runs)


@pytest.mark.parametrize(
    "state, error_code, upper_id",
    [("succeeded", None, 91), ("failed", "timeout", None)],
)
def test_record_call_outcome_persists_one_final_success_or_failure(
    state: Literal["succeeded", "failed"],
    error_code: str | None,
    upper_id: int | None,
) -> None:
    client = _Client([{"intent_id": "intent-a"}])
    outcome = StandaloneCrmCallOutcome(
        "intent-a", "probe", state, "2026-08-28T00:00:00Z", upper_id, error_code
    )

    assert _repository(client).record_call_outcome(outcome)
    assert client.transaction.runs == [
        _Run(
            RECORD_CALL_OUTCOME,
            {
                "intent_id": "intent-a",
                "status": state,
                "upper_id": upper_id,
                "error_code": error_code,
            },
        )
    ]


def test_contact_binding_position_queries_are_fenced_and_do_not_account_rows() -> None:
    query = CLOSE_CONTACT_BINDING_POSITION
    assert "owner_id: $owner_id" in query
    assert "token: $fence_token" in query
    assert "task_id: $task_id" in query
    assert "task_name: $task_name" in query
    assert "payload_digest: $payload_digest" in query
    assert "coalesce(census.cancel_requested, false) = false" in query
    assert "occurrence_rows" not in query
    assert "attempt.row_count" not in query
    assert "binding_offset: $binding_count" in CLOSE_CONTACT_BINDING_POSITION
    assert "stored.last_committed_id = $contact_id" in CLOSE_CONTACT_BINDING_POSITION
