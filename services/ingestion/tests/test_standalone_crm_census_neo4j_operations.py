"""Real-Neo4j #273 call, child, cancellation, and terminal operations coverage."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from src.graph.standalone_crm_census_types import (
    StandaloneCrmCensusConflictError,
    StandaloneCrmCensusStaleError,
)
from src.standalone_crm_census_models import StandaloneCrmCheckpoint
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

    def reserve(payload: str) -> str:
        return repository.reserve_publication(
            admission=census.admission,
            attempt=census.attempt,
            unit_kind="contact",
            sequence=1,
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
    assert accounting.skipped_units == 1

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
    assert repository.request_cancel(published.admission, actor="operator", reason="stop") == 0
    published_token = repository.claim_child_fence(
        published.admission,
        published_envelope,
        worker_task_id="published-child",
        lease_seconds=60,
    )
    repository.settle_child(
        published.admission,
        _checkpoint_for(
            published,
            child_fence_token=published_token,
            child_task_id="published-child",
        ),
        terminal_state="cancelled",
        expected_version=1,
        max_rows_per_attempt=20,
        max_rows_per_occurrence=20,
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

    active = _claim_source(census_neo4j, "terminal-active")
    _freeze(repository, active, (("contact", 1),))
    envelope, _publication_id = _reserve_published_contact(repository, active)
    repository.claim_child_fence(
        active.admission, envelope, worker_task_id="active-child", lease_seconds=60
    )
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.reconcile_terminal(active.admission, active.attempt)

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
