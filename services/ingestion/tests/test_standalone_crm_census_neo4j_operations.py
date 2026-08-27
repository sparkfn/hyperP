"""Real-Neo4j #273 call, child, cancellation, and terminal operations coverage."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest
from src.graph.standalone_crm_census_types import (
    StandaloneCrmCensusConflictError,
    StandaloneCrmCensusStaleError,
)
from src.standalone_crm_census_models import (
    StandaloneCrmBudgetSnapshot,
    StandaloneCrmCheckpoint,
    StandaloneCrmChildEnvelope,
)
from src.standalone_crm_census_publication_runtime import repair_publication
from src.standalone_crm_census_runtime_envelopes import canonical_json, source_envelope
from standalone_crm_census_neo4j_support import (
    CensusNeo4j,
    cleanup_census_env,
    disposable_census_neo4j,
)
from test_standalone_crm_census_neo4j import (
    _budget,
    _call_intent,
    _claim_source,
    _freeze,
    _ReadyCensus,
    _reserve_published_contact,
)


@pytest.fixture
def census_neo4j() -> Iterator[CensusNeo4j]:
    env = disposable_census_neo4j()
    try:
        yield env
    finally:
        cleanup_census_env(env)


def test_unique_call_reservations_are_concurrent_and_durably_budgeted(
    census_neo4j: CensusNeo4j,
) -> None:
    census = _claim_source(census_neo4j, "calls", budget=_budget(calls=2))
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 1),))
    duplicate = _call_intent(census, "duplicate-intent", 1)
    assert repository.reserve_call(
        intent=duplicate, budget_calls_per_attempt=2, budget_calls_per_occurrence=2
    )
    assert not repository.reserve_call(
        intent=duplicate, budget_calls_per_attempt=2, budget_calls_per_occurrence=2
    )

    def reserve(sequence: int) -> bool:
        return repository.reserve_call(
            intent=_call_intent(census, f"concurrent-intent-{sequence}", sequence + 1),
            budget_calls_per_attempt=2,
            budget_calls_per_occurrence=2,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = tuple(pool.map(reserve, (1, 2, 3)))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 2


def test_publication_reservation_is_idempotent_conflict_safe_and_concurrent(
    census_neo4j: CensusNeo4j,
) -> None:
    census = _claim_source(census_neo4j, "publication")
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 1),))

    def reserve(payload: str, publication_id: str = "publication-one") -> str:
        return repository.reserve_publication(
            admission=census.admission,
            attempt=census.attempt,
            unit_kind="contact",
            sequence=1,
            publication_id=publication_id,
            task_id="contact-child",
            task_name="src.standalone_crm_source_child.run",
            queue="ingestion",
            payload_json=payload,
            payload_digest="sha256:payload" if payload == "{}" else "sha256:other",
        ).publication_id

    first = reserve("{}")
    assert reserve("{}") == first
    with pytest.raises(StandaloneCrmCensusConflictError):
        reserve('{"changed":true}')
    with pytest.raises(StandaloneCrmCensusConflictError):
        reserve("{}", publication_id="publication-two")
    assert repository.request_cancel(census.admission, actor="operator", reason="isolate") == 1
    assert (
        repository.reconcile_terminal(census.admission, census.attempt)[0]
        == "cancelled_with_checkpoint"
    )

    census_two = _claim_source(census_neo4j, "publication-concurrent")
    _freeze(repository, census_two, (("contact", 1),))

    def concurrent_reserve(_: int) -> str:
        return repository.reserve_publication(
            admission=census_two.admission,
            attempt=census_two.attempt,
            unit_kind="contact",
            sequence=1,
            task_id="contact-child",
            task_name="src.standalone_crm_source_child.run",
            queue="ingestion",
            payload_json="{}",
            payload_digest="sha256:payload",
        ).publication_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        publication_ids = tuple(pool.map(concurrent_reserve, (1, 2)))
    assert publication_ids[0] == publication_ids[1]


def test_runtime_envelope_claims_its_exact_persisted_publication(
    census_neo4j: CensusNeo4j,
) -> None:
    census = _claim_source(census_neo4j, "envelope-publication")
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 2),))
    envelope = source_envelope(
        census.admission,
        census.attempt,
        census.request,
        "contact",
        2,
        1,
    )
    publication = repository.reserve_publication(
        admission=census.admission,
        attempt=census.attempt,
        unit_kind="contact",
        sequence=1,
        publication_id=envelope.publication_id,
        task_id=envelope.task_id,
        task_name="src.standalone_crm_source_child.run",
        queue="ingestion",
        payload_json=canonical_json(asdict(envelope)),
        payload_digest=envelope.payload_digest,
    )
    assert publication.publication_id == envelope.publication_id
    repository.confirm_publication(census.admission, census.attempt, publication.publication_id)
    assert (
        repository.claim_child_fence(
            census.admission, envelope, worker_task_id="envelope-child", lease_seconds=60
        )
        == 1
    )


@pytest.mark.parametrize("rows", [0, 1, 2, 7])
def test_terminal_classification_is_independent_of_cumulative_row_totals(
    census_neo4j: CensusNeo4j, rows: int
) -> None:
    census = _claim_source(census_neo4j, f"row-total-{rows}")
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 10),))
    envelope, _publication_id = _reserve_published_contact(repository, census, upper_id=10)
    token = repository.claim_child_fence(
        census.admission, envelope, worker_task_id=f"rows-{rows}", lease_seconds=60
    )
    repository.settle_child(
        census.admission,
        StandaloneCrmCheckpoint(
            census.admission.census_id,
            "contact",
            10,
            rows if rows > 0 else None,
            None,
            rows,
            0,
            0,
            0,
            census.attempt.generation,
            census.attempt.parent_fence_token,
            token,
            f"rows-{rows}",
        ),
        terminal_state="completed",
        expected_version=1,
        max_rows_per_attempt=20,
        max_rows_per_occurrence=20,
    )
    state, accounting = repository.reconcile_terminal(census.admission, census.attempt)
    assert state == "completed"
    assert accounting.expected_units == 1
    assert accounting.processed_units == rows


def test_child_fence_redelivery_checkpoint_and_stale_tokens(census_neo4j: CensusNeo4j) -> None:
    census = _claim_source(census_neo4j, "child")
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 2),))
    envelope, _publication_id = _reserve_published_contact(repository, census, upper_id=2)
    child_token = repository.claim_child_fence(
        census.admission, envelope, worker_task_id="child-task", lease_seconds=60
    )
    assert child_token == repository.claim_child_fence(
        census.admission, envelope, worker_task_id="child-task", lease_seconds=60
    )
    checkpoint = StandaloneCrmCheckpoint(
        census.admission.census_id,
        "contact",
        2,
        1,
        1,
        1,
        0,
        0,
        0,
        census.attempt.generation,
        census.attempt.parent_fence_token,
        child_token,
        "child-task",
    )
    assert (
        repository.checkpoint_child(
            census.admission,
            checkpoint,
            expected_version=1,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
        == 2
    )
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.checkpoint_child(
            census.admission,
            StandaloneCrmCheckpoint(
                census.admission.census_id,
                "contact",
                1,
                1,
                1,
                1,
                0,
                0,
                0,
                census.attempt.generation,
                census.attempt.parent_fence_token,
                child_token,
                "child-task",
            ),
            expected_version=2,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.checkpoint_child(
            census.admission,
            StandaloneCrmCheckpoint(
                census.admission.census_id,
                "contact",
                1,
                1,
                1,
                1,
                0,
                0,
                0,
                census.attempt.generation,
                census.attempt.parent_fence_token + 1,
                child_token,
                "child-task",
            ),
            expected_version=2,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.checkpoint_child(
            census.admission,
            StandaloneCrmCheckpoint(
                census.admission.census_id,
                "contact",
                1,
                1,
                1,
                1,
                0,
                0,
                0,
                census.attempt.generation,
                census.attempt.parent_fence_token,
                child_token + 1,
                "child-task",
            ),
            expected_version=2,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
    with census_neo4j.driver.session() as session:
        session.run(
            "MATCH (attempt:StandaloneCrmCensusAttempt "
            "{census_id: $census_id, generation: $generation}) "
            "SET attempt.lease_until = datetime() - duration({seconds: 1})",
            census_id=census.admission.census_id,
            generation=census.attempt.generation,
        ).consume()
    repository.recover_expired_attempt(census.admission, census.attempt)
    successor = repository.claim_attempt(census.admission, census.request, task_id="child-recovery")
    successor_census = _ReadyCensus(census.admission, successor, census.request)
    successor_envelope, _successor_publication_id = _reserve_published_contact(
        repository, successor_census, upper_id=2
    )
    successor_token = repository.claim_child_fence(
        census.admission,
        successor_envelope,
        worker_task_id="recovered-child",
        lease_seconds=60,
        recovery=True,
    )
    assert successor_token > child_token
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.checkpoint_child(
            census.admission,
            checkpoint,
            expected_version=2,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )


def _checkpoint_for(
    census: _ReadyCensus,
    *,
    child_fence_token: int,
    child_task_id: str,
    processed_count: int = 1,
    skipped_count: int = 0,
    failed_count: int = 0,
) -> StandaloneCrmCheckpoint:
    return StandaloneCrmCheckpoint(
        census.admission.census_id,
        "contact",
        2,
        1,
        None,
        processed_count,
        skipped_count,
        failed_count,
        0,
        census.attempt.generation,
        census.attempt.parent_fence_token,
        child_fence_token,
        child_task_id,
    )


def test_cancellation_retirement_and_active_settlement_converge(
    census_neo4j: CensusNeo4j,
) -> None:
    repository = census_neo4j.repository

    before_publication = _claim_source(census_neo4j, "cancel-before-publication")
    _freeze(repository, before_publication, (("contact", 2),))
    assert (
        repository.request_cancel(before_publication.admission, actor="operator", reason="stop")
        == 1
    )
    assert (
        repository.request_cancel(before_publication.admission, actor="retry", reason="again") == 0
    )
    state, accounting = repository.reconcile_terminal(
        before_publication.admission, before_publication.attempt
    )
    assert state == "cancelled_with_checkpoint"
    assert accounting.skipped_units == 0

    reserved = _claim_source(census_neo4j, "cancel-reserved-publication")
    _freeze(repository, reserved, (("contact", 2),))
    publication = repository.reserve_publication(
        admission=reserved.admission,
        attempt=reserved.attempt,
        unit_kind="contact",
        sequence=1,
        task_id="reserved-child",
        task_name="src.standalone_crm_source_child.run",
        queue="ingestion",
        payload_json="{}",
        payload_digest="sha256:reserved",
    )
    repository.mark_publication_publishing(
        reserved.admission, reserved.attempt, publication.publication_id
    )
    assert repository.request_cancel(reserved.admission, actor="operator", reason="stop") == 1
    reserved_status = repository.status(reserved.admission.census_id)
    assert reserved_status is not None
    assert reserved_status.publications[0]["publication_id"] == publication.publication_id
    assert reserved_status.publications[0]["status"] == "retired"
    assert (
        repository.reconcile_terminal(reserved.admission, reserved.attempt)[0]
        == "cancelled_with_checkpoint"
    )

    active = _claim_source(census_neo4j, "cancel-active-child")
    _freeze(repository, active, (("contact", 2),))
    envelope, _publication_id = _reserve_published_contact(repository, active, upper_id=2)
    child_token = repository.claim_child_fence(
        active.admission, envelope, worker_task_id="active-child", lease_seconds=60
    )
    checkpoint = _checkpoint_for(
        active, child_fence_token=child_token, child_task_id="active-child"
    )
    assert (
        repository.checkpoint_child(
            active.admission,
            checkpoint,
            expected_version=1,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
        == 2
    )
    assert repository.request_cancel(active.admission, actor="operator", reason="stop") == 0
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.checkpoint_child(
            active.admission,
            checkpoint,
            expected_version=2,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
    assert (
        repository.settle_child(
            active.admission,
            checkpoint,
            terminal_state="cancelled",
            expected_version=2,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
        == 3
    )
    assert (
        repository.reconcile_terminal(active.admission, active.attempt)[0]
        == "cancelled_with_checkpoint"
    )

    published = _claim_source(census_neo4j, "cancel-published-before-claim")
    _freeze(repository, published, (("contact", 2),))
    published_envelope, _published_id = _reserve_published_contact(
        repository, published, upper_id=2
    )
    assert repository.request_cancel(published.admission, actor="operator", reason="stop") == 1
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.claim_child_fence(
            published.admission,
            published_envelope,
            worker_task_id="published-child",
            lease_seconds=60,
        )
    assert (
        repository.reconcile_terminal(published.admission, published.attempt)[0]
        == "cancelled_with_checkpoint"
    )


def test_typed_child_settlement_derives_completed_and_failed_terminals(
    census_neo4j: CensusNeo4j,
) -> None:
    repository = census_neo4j.repository
    for occurrence, terminal_state, failed_count, expected in (
        ("settle-completed", "completed", 0, "completed"),
        ("settle-failed", "failed", 1, "failed"),
    ):
        census = _claim_source(census_neo4j, occurrence)
        _freeze(repository, census, (("contact", 2),))
        envelope, _publication_id = _reserve_published_contact(repository, census, upper_id=2)
        token = repository.claim_child_fence(
            census.admission, envelope, worker_task_id=f"{occurrence}-child", lease_seconds=60
        )
        checkpoint = _checkpoint_for(
            census,
            child_fence_token=token,
            child_task_id=f"{occurrence}-child",
            processed_count=0 if failed_count else 1,
            failed_count=failed_count,
        )
        repository.settle_child(
            census.admission,
            checkpoint,
            terminal_state=terminal_state,
            expected_version=1,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
        assert repository.reconcile_terminal(census.admission, census.attempt)[0] == expected


def test_cancellation_is_idempotent(census_neo4j: CensusNeo4j) -> None:
    census = _claim_source(census_neo4j, "cancel")
    repository = census_neo4j.repository
    assert repository.request_cancel(census.admission, actor="operator", reason="stop") == 0
    assert repository.request_cancel(census.admission, actor="another", reason="again") == 0
    status = repository.status(census.admission.census_id)
    assert status is not None
    assert status.census["state"] == "freeze_failed"


def test_terminal_reconciliation_requires_exact_publication_fence_and_balanced_accounting(
    census_neo4j: CensusNeo4j,
) -> None:
    zero = _claim_source(census_neo4j, "terminal-zero")
    repository = census_neo4j.repository
    _freeze(repository, zero, (("contact", 0),))
    state, accounting = repository.reconcile_terminal(zero.admission, zero.attempt)
    assert state == "completed"
    assert accounting.no_work_units == 1

    missing = _claim_source(census_neo4j, "terminal-missing")
    _freeze(repository, missing, (("contact", 1),))
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.reconcile_terminal(missing.admission, missing.attempt)
    assert repository.request_cancel(missing.admission, actor="operator", reason="isolate") == 1
    assert (
        repository.reconcile_terminal(missing.admission, missing.attempt)[0]
        == "cancelled_with_checkpoint"
    )

    unpublished = _claim_source(census_neo4j, "terminal-unpublished")
    _freeze(repository, unpublished, (("contact", 1),))
    repository.reserve_publication(
        admission=unpublished.admission,
        attempt=unpublished.attempt,
        unit_kind="contact",
        sequence=1,
        task_id="unpublished-child",
        task_name="src.standalone_crm_source_child.run",
        queue="ingestion",
        payload_json="{}",
        payload_digest="sha256:unpublished",
    )
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.reconcile_terminal(unpublished.admission, unpublished.attempt)
    assert repository.request_cancel(unpublished.admission, actor="operator", reason="isolate") == 1
    assert (
        repository.reconcile_terminal(unpublished.admission, unpublished.attempt)[0]
        == "cancelled_with_checkpoint"
    )

    active = _claim_source(census_neo4j, "terminal-active")
    _freeze(repository, active, (("contact", 1),))
    envelope, _publication_id = _reserve_published_contact(repository, active)
    repository.claim_child_fence(
        active.admission, envelope, worker_task_id="active-child", lease_seconds=60
    )
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.reconcile_terminal(active.admission, active.attempt)
    assert repository.request_cancel(active.admission, actor="operator", reason="isolate") == 0
    repository.settle_child(
        active.admission,
        StandaloneCrmCheckpoint(
            active.admission.census_id,
            "contact",
            1,
            None,
            None,
            0,
            0,
            0,
            0,
            active.attempt.generation,
            active.attempt.parent_fence_token,
            1,
            "active-child",
        ),
        terminal_state="cancelled",
        expected_version=1,
        max_rows_per_attempt=20,
        max_rows_per_occurrence=20,
    )
    assert (
        repository.reconcile_terminal(active.admission, active.attempt)[0]
        == "cancelled_with_checkpoint"
    )

    extra = _claim_source(census_neo4j, "terminal-extra")
    _freeze(repository, extra, (("contact", 1),))
    envelope, _publication_id = _reserve_published_contact(repository, extra)
    token = repository.claim_child_fence(
        extra.admission, envelope, worker_task_id="extra-child", lease_seconds=60
    )
    checkpoint = StandaloneCrmCheckpoint(
        extra.admission.census_id,
        "contact",
        1,
        1,
        None,
        1,
        0,
        0,
        0,
        extra.attempt.generation,
        extra.attempt.parent_fence_token,
        token,
        "extra-child",
    )
    repository.settle_child(
        extra.admission,
        checkpoint,
        terminal_state="completed",
        expected_version=1,
        max_rows_per_attempt=20,
        max_rows_per_occurrence=20,
    )
    with census_neo4j.driver.session() as session:
        session.run(
            "CREATE (:StandaloneCrmChildPublication {census_id: $census_id, "
            "generation: $generation, unit_kind: 'contact', sequence: 2, "
            "publication_id: $publication_id, task_id: 'extra', task_name: 'extra', "
            "queue: 'ingestion', payload_json: '{}', payload_digest: 'sha256:extra', "
            "status: 'published', created_at: datetime(), updated_at: datetime()})",
            census_id=extra.admission.census_id,
            generation=extra.attempt.generation,
            publication_id=f"extra-{extra.admission.census_id}",
        ).consume()
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.reconcile_terminal(extra.admission, extra.attempt)
    with census_neo4j.driver.session() as session:
        session.run(
            "MATCH (publication:StandaloneCrmChildPublication {publication_id: $publication_id}) "
            "DETACH DELETE publication",
            publication_id=f"extra-{extra.admission.census_id}",
        ).consume()
        session.run(
            "CREATE (:StandaloneCrmUnitFence {census_id: $census_id, unit_kind: 'lead', "
            "generation: $generation, state: 'released'})",
            census_id=extra.admission.census_id,
            generation=extra.attempt.generation,
        ).consume()
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.reconcile_terminal(extra.admission, extra.attempt)
    with census_neo4j.driver.session() as session:
        session.run(
            "MATCH (fence:StandaloneCrmUnitFence {census_id: $census_id, unit_kind: 'lead'}) "
            "DETACH DELETE fence",
            census_id=extra.admission.census_id,
        ).consume()
    state, accounting = repository.reconcile_terminal(extra.admission, extra.attempt)
    assert state == "completed"
    assert accounting.processed_units == 1


def test_completed_unit_cannot_create_a_second_publication(census_neo4j: CensusNeo4j) -> None:
    census = _claim_source(census_neo4j, "completed-publication")
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 1),))
    envelope, _publication_id = _reserve_published_contact(repository, census)
    token = repository.claim_child_fence(
        census.admission, envelope, worker_task_id="completed-child", lease_seconds=60
    )
    repository.settle_child(
        census.admission,
        StandaloneCrmCheckpoint(
            census.admission.census_id,
            "contact",
            1,
            1,
            None,
            1,
            0,
            0,
            0,
            census.attempt.generation,
            census.attempt.parent_fence_token,
            token,
            "completed-child",
        ),
        terminal_state="completed",
        expected_version=1,
        max_rows_per_attempt=20,
        max_rows_per_occurrence=20,
    )
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.reserve_publication(
            admission=census.admission,
            attempt=census.attempt,
            unit_kind="contact",
            sequence=2,
            task_id="duplicate-child",
            task_name="src.standalone_crm_source_child.run",
            queue="ingestion",
            payload_json="{}",
            payload_digest="sha256:duplicate",
        )
    status = repository.status(census.admission.census_id)
    assert status is not None
    assert len(status.publications) == 1


def test_checkpoint_delta_is_per_attempt_and_occurrence_accounting_is_cas_safe(
    census_neo4j: CensusNeo4j,
) -> None:
    budget = StandaloneCrmBudgetSnapshot(4, 1, 120.0, 4, 2, 4, 600.0)
    census = _claim_source(census_neo4j, "row-delta", budget=budget)
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 4),))
    envelope, _ = _reserve_published_contact(repository, census, upper_id=4)
    first_token = repository.claim_child_fence(
        census.admission, envelope, worker_task_id="rows-one", lease_seconds=60
    )
    first = StandaloneCrmCheckpoint(
        census.admission.census_id,
        "contact",
        4,
        1,
        None,
        1,
        0,
        0,
        0,
        census.attempt.generation,
        census.attempt.parent_fence_token,
        first_token,
        "rows-one",
    )
    assert (
        repository.checkpoint_child(
            census.admission,
            first,
            expected_version=1,
            max_rows_per_attempt=1,
            max_rows_per_occurrence=2,
        )
        == 2
    )
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.checkpoint_child(
            census.admission,
            StandaloneCrmCheckpoint(
                census.admission.census_id,
                "contact",
                4,
                2,
                None,
                2,
                0,
                0,
                0,
                census.attempt.generation,
                census.attempt.parent_fence_token,
                first_token,
                "rows-one",
            ),
            expected_version=2,
            max_rows_per_attempt=1,
            max_rows_per_occurrence=2,
        )
    repository.release_child_fence(census.admission, first)
    repository.pause(census.admission, census.attempt, reason="row_allowance_consumed")
    successor = repository.continue_attempt(census.admission, census.request, task_id="rows-two")
    successor_census = _ReadyCensus(census.admission, successor, census.request)
    successor_envelope, _ = _reserve_published_contact(repository, successor_census, upper_id=4)
    second_token = repository.claim_child_fence(
        census.admission, successor_envelope, worker_task_id="rows-two-child", lease_seconds=60
    )
    second = StandaloneCrmCheckpoint(
        census.admission.census_id,
        "contact",
        4,
        2,
        None,
        2,
        0,
        0,
        0,
        successor.generation,
        successor.parent_fence_token,
        second_token,
        "rows-two-child",
    )
    assert (
        repository.settle_child(
            census.admission,
            second,
            terminal_state="completed",
            expected_version=2,
            max_rows_per_attempt=1,
            max_rows_per_occurrence=2,
        )
        == 3
    )
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.settle_child(
            census.admission,
            second,
            terminal_state="completed",
            expected_version=2,
            max_rows_per_attempt=1,
            max_rows_per_occurrence=2,
        )
    status = repository.status(census.admission.census_id)
    assert status is not None
    assert status.census["row_count"] == 2
    assert repository.reconcile_terminal(census.admission, successor)[0] == "completed"


def test_historical_reserved_calls_block_paused_continuation_until_unknown_classification(
    census_neo4j: CensusNeo4j,
) -> None:
    census = _claim_source(census_neo4j, "reserved-continuation", budget=_budget(calls=3))
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 1),))
    before_io = _call_intent(census, "reserved-before-io", 1)
    after_io = _call_intent(census, "reserved-after-io", 2)
    assert repository.reserve_call(
        intent=before_io, budget_calls_per_attempt=3, budget_calls_per_occurrence=3
    )
    assert repository.reserve_call(
        intent=after_io, budget_calls_per_attempt=3, budget_calls_per_occurrence=3
    )
    repository.pause(census.admission, census.attempt, reason="reservation_recovery")
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.continue_attempt(
            census.admission, census.request, task_id="blocked-continuation"
        )
    assert repository.classify_current_reserved_call_unknown(
        census.admission, intent_id=before_io.intent_id
    )
    assert repository.classify_current_reserved_call_unknown(
        census.admission, intent_id=after_io.intent_id
    )
    assert not repository.classify_current_reserved_call_unknown(
        census.admission, intent_id=after_io.intent_id
    )
    successor = repository.continue_attempt(
        census.admission, census.request, task_id="continued-after-unknown"
    )
    assert successor.generation == census.attempt.generation + 1


def test_expiry_boundaries_reject_new_work_but_allow_exact_cancel_settlement(
    census_neo4j: CensusNeo4j,
) -> None:
    census = _claim_source(census_neo4j, "expiry-boundaries")
    repository = census_neo4j.repository
    with census_neo4j.driver.session() as session:
        session.run(
            "MATCH (attempt:StandaloneCrmCensusAttempt "
            "{census_id: $census_id, generation: $generation}) "
            "SET attempt.deadline_at = datetime() - duration({seconds: 1}), "
            "attempt.lease_until = datetime() - duration({seconds: 1})",
            census_id=census.admission.census_id,
            generation=census.attempt.generation,
        ).consume()
    with pytest.raises(StandaloneCrmCensusStaleError):
        _freeze(repository, census, (("contact", 1),))
    assert repository.request_cancel(census.admission, actor="operator", reason="expired") == 0

    reserve_expired = _claim_source(census_neo4j, "reserve-expiry")
    _freeze(repository, reserve_expired, (("contact", 1),))
    with census_neo4j.driver.session() as session:
        session.run(
            "MATCH (attempt:StandaloneCrmCensusAttempt "
            "{census_id: $census_id, generation: $generation}) "
            "SET attempt.deadline_at = datetime() - duration({seconds: 1})",
            census_id=reserve_expired.admission.census_id,
            generation=reserve_expired.attempt.generation,
        ).consume()
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.reserve_publication(
            admission=reserve_expired.admission,
            attempt=reserve_expired.attempt,
            unit_kind="contact",
            sequence=1,
            task_id="expired-child",
            task_name="src.standalone_crm_source_child.run",
            queue="ingestion",
            payload_json="{}",
            payload_digest="sha256:expired",
        )
    assert (
        repository.request_cancel(reserve_expired.admission, actor="operator", reason="expired")
        == 1
    )
    assert (
        repository.reconcile_terminal(reserve_expired.admission, reserve_expired.attempt)[0]
        == "cancelled_with_checkpoint"
    )

    active = _claim_source(census_neo4j, "expiry-settlement")
    _freeze(repository, active, (("contact", 2),))
    envelope, publication_id = _reserve_published_contact(repository, active, upper_id=2)
    token = repository.claim_child_fence(
        active.admission, envelope, worker_task_id="expired-child", lease_seconds=60
    )
    checkpoint = _checkpoint_for(active, child_fence_token=token, child_task_id="expired-child")
    assert (
        repository.checkpoint_child(
            active.admission,
            checkpoint,
            expected_version=1,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
        == 2
    )
    with census_neo4j.driver.session() as session:
        session.run(
            "MATCH (attempt:StandaloneCrmCensusAttempt "
            "{census_id: $census_id, generation: $generation}) "
            "MATCH (fence:StandaloneCrmUnitFence "
            "{census_id: $census_id, generation: $generation}) "
            "SET attempt.lease_until = datetime() - duration({seconds: 1}), "
            "attempt.deadline_at = datetime() - duration({seconds: 1}), "
            "fence.lease_until = datetime() - duration({seconds: 1})",
            census_id=active.admission.census_id,
            generation=active.attempt.generation,
        ).consume()
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.authorize_publication_broker(active.admission, active.attempt, publication_id)
    assert repository.request_cancel(active.admission, actor="operator", reason="expired") == 0
    assert (
        repository.settle_child(
            active.admission,
            checkpoint,
            terminal_state="cancelled",
            expected_version=2,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
        == 3
    )
    assert (
        repository.reconcile_terminal(active.admission, active.attempt)[0]
        == "cancelled_with_checkpoint"
    )


@pytest.mark.parametrize("mutation", ["disable", "remove_binding"])
def test_stale_authority_waits_for_settlement_before_terminal_scope_release(
    census_neo4j: CensusNeo4j, mutation: str
) -> None:
    census = _claim_source(census_neo4j, f"stale-{mutation}")
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 2),))
    envelope, _ = _reserve_published_contact(repository, census, upper_id=2)
    token = repository.claim_child_fence(
        census.admission, envelope, worker_task_id=f"{mutation}-child", lease_seconds=60
    )
    checkpoint = _checkpoint_for(census, child_fence_token=token, child_task_id=f"{mutation}-child")
    assert (
        repository.checkpoint_child(
            census.admission,
            checkpoint,
            expected_version=1,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
        == 2
    )
    with census_neo4j.driver.session() as session:
        if mutation == "disable":
            session.run(
                "MATCH (source:BitrixSourceInstance {source_instance_id: $source_instance_id}) "
                "SET source.status = 'inactive'",
                source_instance_id=census.admission.source_instance_id,
            ).consume()
        else:
            session.run(
                "MATCH (:BitrixSourceInstance {source_instance_id: $source_instance_id})"
                "-[binding:OWNS_BITRIX_CONTROL]->() DELETE binding",
                source_instance_id=census.admission.source_instance_id,
            ).consume()
    repository.mark_authority_stale(census.admission)
    status = repository.status(census.admission.census_id)
    assert status is not None
    assert status.census["terminal_state"] is None
    assert status.census["state"] == "authority_stale_pending"
    assert repository.request_cancel(census.admission, actor="operator", reason="authority") == 0
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.reconcile_terminal(census.admission, census.attempt)
    assert (
        repository.settle_child(
            census.admission,
            checkpoint,
            terminal_state="failed",
            expected_version=2,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
        == 3
    )
    assert repository.reconcile_terminal(census.admission, census.attempt)[0] == "failed"
    with census_neo4j.driver.session() as session:
        result = session.run(
            "MATCH (scope:StandaloneCrmCensusScopeLock {active_census_id: $census_id}) "
            "RETURN count(scope) AS count",
            census_id=census.admission.census_id,
        ).single()
    assert result is not None
    assert result["count"] == 0


def test_published_unclaimed_cancellation_retirement_races_late_claim(
    census_neo4j: CensusNeo4j,
) -> None:
    census = _claim_source(census_neo4j, "published-repair-race")
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 1),))
    envelope, publication_id = _reserve_published_contact(repository, census)

    def cancel() -> int:
        return repository.request_cancel(census.admission, actor="operator", reason="repair")

    def late_claim() -> bool:
        try:
            repository.claim_child_fence(
                census.admission, envelope, worker_task_id="late-child", lease_seconds=60
            )
        except StandaloneCrmCensusStaleError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled_future = pool.submit(cancel)
        claimed_future = pool.submit(late_claim)
        cancelled = cancelled_future.result()
        claimed = claimed_future.result()
    status = repository.status(census.admission.census_id)
    assert status is not None
    publication = next(
        item for item in status.publications if item["publication_id"] == publication_id
    )
    if claimed:
        assert publication["status"] == "published"
        token_value = next(item for item in status.fences if item["state"] == "active")[
            "child_fence_token"
        ]
        assert isinstance(token_value, int) and not isinstance(token_value, bool)
        checkpoint = StandaloneCrmCheckpoint(
            census.admission.census_id,
            "contact",
            1,
            None,
            None,
            0,
            0,
            0,
            0,
            census.attempt.generation,
            census.attempt.parent_fence_token,
            token_value,
            "late-child",
        )
        repository.settle_child(
            census.admission,
            checkpoint,
            terminal_state="cancelled",
            expected_version=1,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
    else:
        assert publication["status"] == "retired"
    assert cancelled in {0, 1}
    assert (
        repository.reconcile_terminal(census.admission, census.attempt)[0]
        == "cancelled_with_checkpoint"
    )


class _RepairPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def handler_available(self, census_kind: str, unit_kind: str) -> bool:
        return census_kind == "source_sync" and unit_kind == "contact"

    def publish(self, *, task_name: str, task_id: str, queue: str, payload_json: str) -> None:
        self.calls.append((task_name, task_id, queue, payload_json))


def test_published_unclaimed_repair_republishes_exact_payload_then_observes_late_claim(
    census_neo4j: CensusNeo4j,
) -> None:
    census = _claim_source(census_neo4j, "published-repair")
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 1),))
    publication = repository.reserve_publication(
        admission=census.admission,
        attempt=census.attempt,
        unit_kind="contact",
        sequence=1,
        publication_id=f"{census.admission.census_id}:{census.attempt.generation}:contact:1",
        task_id="repair-child",
        task_name="src.standalone_crm_source_child.run",
        queue="ingestion",
        payload_json='{"unit_kind":"contact"}',
        payload_digest="sha256:repair-payload",
    )
    repository.mark_publication_publishing(
        census.admission, census.attempt, publication.publication_id
    )
    repository.authorize_publication_broker(
        census.admission, census.attempt, publication.publication_id
    )
    repository.confirm_publication(census.admission, census.attempt, publication.publication_id)
    status = repository.status(census.admission.census_id)
    assert status is not None
    assert status.publications[0]["status"] == "published"
    publisher = _RepairPublisher()
    repair_publication(
        repository,
        publisher,
        publication.publication_id,
        lambda _request, _authority: None,
        lambda _admission: None,
    )
    assert publisher.calls == [
        (publication.task_name, publication.task_id, publication.queue, publication.payload_json)
    ]
    envelope = StandaloneCrmChildEnvelope(
        census.admission.census_id,
        census.attempt.generation,
        census.attempt.parent_fence_token,
        "contact",
        1,
        None,
        publication.publication_id,
        publication.task_id,
        publication.payload_digest,
        census.admission.source_instance_id,
        census.admission.control_instance_id,
    )
    assert (
        repository.claim_child_fence(
            census.admission, envelope, worker_task_id="late-repair-child", lease_seconds=60
        )
        == 1
    )
    repair_publication(
        repository,
        publisher,
        publication.publication_id,
        lambda _request, _authority: None,
        lambda _admission: None,
    )
    assert len(publisher.calls) == 1


def test_stale_authority_during_unclaimed_publication_retires_then_derives_failed(
    census_neo4j: CensusNeo4j,
) -> None:
    census = _claim_source(census_neo4j, "stale-publication")
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 1),))
    publication = repository.reserve_publication(
        admission=census.admission,
        attempt=census.attempt,
        unit_kind="contact",
        sequence=1,
        task_id="stale-publication-child",
        task_name="src.standalone_crm_source_child.run",
        queue="ingestion",
        payload_json="{}",
        payload_digest="sha256:stale-publication",
    )
    repository.mark_publication_publishing(
        census.admission, census.attempt, publication.publication_id
    )
    repository.mark_authority_stale(census.admission)
    status = repository.status(census.admission.census_id)
    assert status is not None
    assert status.census["terminal_state"] is None
    assert repository.request_cancel(census.admission, actor="operator", reason="head_changed") == 1
    status = repository.status(census.admission.census_id)
    assert status is not None
    assert status.publications[0]["status"] == "retired"
    assert repository.reconcile_terminal(census.admission, census.attempt)[0] == "failed"


def test_observed_ambiguous_publication_settles_after_authority_loss_without_broker_io(
    census_neo4j: CensusNeo4j,
) -> None:
    census = _claim_source(census_neo4j, "stale-observed-repair")
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 2),))
    envelope, publication_id = _reserve_published_contact(repository, census, upper_id=2)
    repository.mark_publication_publishing(census.admission, census.attempt, publication_id)
    repository.authorize_publication_broker(census.admission, census.attempt, publication_id)
    repository.mark_publication_ambiguous(census.admission, census.attempt, publication_id)
    token = repository.claim_child_fence(
        census.admission, envelope, worker_task_id="observed-stale-child", lease_seconds=60
    )
    checkpoint = _checkpoint_for(
        census, child_fence_token=token, child_task_id="observed-stale-child"
    )
    assert (
        repository.checkpoint_child(
            census.admission,
            checkpoint,
            expected_version=1,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
        == 2
    )
    with census_neo4j.driver.session() as session:
        session.run(
            "MATCH (source:BitrixSourceInstance {source_instance_id: $source_instance_id}) "
            "SET source.status = 'inactive'",
            source_instance_id=census.admission.source_instance_id,
        ).consume()
    repository.mark_authority_stale(census.admission)
    publisher = _RepairPublisher()

    def should_not_revalidate(_request: object, _authority: object) -> None:
        raise AssertionError("observed settlement must not invoke work authority")

    repair_publication(
        repository,
        publisher,
        publication_id,
        should_not_revalidate,
        lambda _admission: (_ for _ in ()).throw(AssertionError("already marked stale")),
    )
    status = repository.status(census.admission.census_id)
    assert status is not None
    assert status.publications[0]["status"] == "published"
    assert status.census["terminal_state"] is None
    assert publisher.calls == []
    assert repository.request_cancel(census.admission, actor="operator", reason="authority") == 0
    assert (
        repository.settle_child(
            census.admission,
            checkpoint,
            terminal_state="failed",
            expected_version=2,
            max_rows_per_attempt=20,
            max_rows_per_occurrence=20,
        )
        == 3
    )
    assert repository.reconcile_terminal(census.admission, census.attempt)[0] == "failed"
    with census_neo4j.driver.session() as session:
        result = session.run(
            "MATCH (scope:StandaloneCrmCensusScopeLock {active_census_id: $census_id}) "
            "RETURN count(scope) AS count",
            census_id=census.admission.census_id,
        ).single()
    assert result is not None
    assert result["count"] == 0
