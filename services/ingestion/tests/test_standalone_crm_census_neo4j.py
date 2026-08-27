"""Real-Neo4j repository coverage for #273 standalone CRM census control."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

import pytest
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.graph.standalone_crm_census_migration import (
    assert_standalone_crm_census_ready,
    migrate_standalone_crm_census_control,
)
from src.graph.standalone_crm_census_types import (
    StandaloneCrmCensusAdmission,
    StandaloneCrmCensusConflictError,
    StandaloneCrmCensusStaleError,
)
from src.standalone_crm_census_models import (
    FrozenSourceWindow,
    StandaloneCrmAttempt,
    StandaloneCrmBudgetSnapshot,
    StandaloneCrmCallIntent,
    StandaloneCrmCheckpoint,
    StandaloneCrmChildEnvelope,
    StandaloneCrmFreshness,
)
from src.standalone_crm_census_requests import (
    MappingPrepareCensusRequest,
    SourceSyncAuthoritySnapshot,
    SourceSyncCensusRequest,
)
from standalone_crm_census_neo4j_support import (
    CensusNeo4j,
    cleanup_census_env,
    disposable_census_neo4j,
    install_census_schema,
    prepare_272,
    prepare_ready,
)


@dataclass(frozen=True)
class _ReadyCensus:
    admission: StandaloneCrmCensusAdmission
    attempt: StandaloneCrmAttempt
    request: SourceSyncCensusRequest


@pytest.fixture
def census_neo4j() -> Iterator[CensusNeo4j]:
    env = disposable_census_neo4j()
    try:
        yield env
    finally:
        cleanup_census_env(env)


def _budget(*, calls: int = 4, attempts: int = 4) -> StandaloneCrmBudgetSnapshot:
    return StandaloneCrmBudgetSnapshot(calls, 20, 120.0, calls, 20, attempts, 600.0)


def _source_request(
    env: CensusNeo4j,
    occurrence: str,
    kinds: tuple[Literal["contact", "lead", "company"], ...] = ("contact",),
    *,
    configuration_digest: str = "config-a",
    budget: StandaloneCrmBudgetSnapshot | None = None,
) -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        env.source_instance_id,
        env.source_instance_id,
        occurrence,
        "operator",
        kinds,
        "policy",
        "association",
        configuration_digest,
        budget or _budget(),
    )


def _authority() -> SourceSyncAuthoritySnapshot:
    return SourceSyncAuthoritySnapshot("mapping-head", "mapping-digest", "projection-head")


def _admit_source(
    env: CensusNeo4j,
    occurrence: str,
    kinds: tuple[Literal["contact", "lead", "company"], ...] = ("contact",),
    *,
    configuration_digest: str = "config-a",
    budget: StandaloneCrmBudgetSnapshot | None = None,
) -> tuple[StandaloneCrmCensusRepository, StandaloneCrmCensusAdmission, SourceSyncCensusRequest]:
    repository = prepare_ready(env)
    request = _source_request(
        env, occurrence, kinds, configuration_digest=configuration_digest, budget=budget
    )
    return repository, repository.admit(request, authority=_authority()), request


def _claim_source(
    env: CensusNeo4j,
    occurrence: str,
    kinds: tuple[Literal["contact", "lead", "company"], ...] = ("contact",),
    *,
    budget: StandaloneCrmBudgetSnapshot | None = None,
) -> _ReadyCensus:
    repository, admission, request = _admit_source(env, occurrence, kinds, budget=budget)
    attempt = repository.claim_attempt(admission, request, task_id=f"parent-{occurrence}")
    return _ReadyCensus(admission, attempt, request)


def _freeze(
    repository: StandaloneCrmCensusRepository,
    census: _ReadyCensus,
    bounds: tuple[tuple[Literal["contact", "lead", "company"], int], ...],
) -> None:
    selected = tuple(kind for kind, _upper in bounds)
    repository.freeze_source_window(
        census.admission, census.attempt, FrozenSourceWindow(selected, bounds)
    )


def _freshness(admission: StandaloneCrmCensusAdmission) -> StandaloneCrmFreshness:
    return StandaloneCrmFreshness(
        admission.census_id,
        admission.fingerprint,
        admission.authority_digest,
        admission.source_instance_id,
        admission.control_instance_id,
    )


def _call_intent(
    census: _ReadyCensus,
    intent_id: str,
    sequence: int,
) -> StandaloneCrmCallIntent:
    return StandaloneCrmCallIntent(
        census.admission.census_id,
        census.attempt.generation,
        census.attempt.parent_fence_token,
        _freshness(census.admission),
        intent_id,
        sequence,
        "page",
        "contact",
        sequence - 1,
        f"sha256:metadata-{sequence}",
        cursor_id=sequence - 1,
        upper_id=1,
    )


def _reserve_published_contact(
    repository: StandaloneCrmCensusRepository,
    census: _ReadyCensus,
    *,
    upper_id: int = 1,
) -> tuple[StandaloneCrmChildEnvelope, str]:
    publication = repository.reserve_publication(
        admission=census.admission,
        attempt=census.attempt,
        unit_kind="contact",
        sequence=1,
        publication_id=f"{census.admission.census_id}:{census.attempt.generation}:contact:1",
        task_id=f"child-{census.admission.census_id}",
        task_name="src.standalone_crm_source_child.run",
        queue="ingestion",
        payload_json='{"unit_kind":"contact"}',
        payload_digest="sha256:contact-payload",
    )
    repository.confirm_publication(census.admission, census.attempt, publication.publication_id)
    envelope = StandaloneCrmChildEnvelope(
        census.admission.census_id,
        census.attempt.generation,
        census.attempt.parent_fence_token,
        "contact",
        upper_id,
        None,
        publication.publication_id,
        publication.task_id,
        publication.payload_digest,
        census.admission.source_instance_id,
        census.admission.control_instance_id,
    )
    return envelope, publication.publication_id


def test_readiness_requires_272_and_exact_273_schema_then_is_rerunnable(
    census_neo4j: CensusNeo4j,
) -> None:
    with pytest.raises(RuntimeError):
        migrate_standalone_crm_census_control(census_neo4j.client)
    prepare_272(census_neo4j)
    with pytest.raises(RuntimeError, match="standalone CRM census constraint"):
        migrate_standalone_crm_census_control(census_neo4j.client)
    install_census_schema(census_neo4j)
    migrate_standalone_crm_census_control(census_neo4j.client)
    migrate_standalone_crm_census_control(census_neo4j.client)
    assert_standalone_crm_census_ready(census_neo4j.client)
    with census_neo4j.driver.session() as session:
        session.run(
            "DROP CONSTRAINT standalone_crm_census_publication_id_unique IF EXISTS"
        ).consume()
    with pytest.raises(RuntimeError, match="standalone CRM census constraint"):
        assert_standalone_crm_census_ready(census_neo4j.client)
    install_census_schema(census_neo4j)
    with census_neo4j.driver.session() as session:
        session.run("DROP INDEX standalone_crm_census_call_scan IF EXISTS").consume()
    with pytest.raises(RuntimeError, match="standalone CRM census index"):
        assert_standalone_crm_census_ready(census_neo4j.client)
    install_census_schema(census_neo4j)
    assert_standalone_crm_census_ready(census_neo4j.client)


def test_admission_is_idempotent_conflicts_on_fingerprint_and_excludes_active_scope(
    census_neo4j: CensusNeo4j,
) -> None:
    repository = prepare_ready(census_neo4j)
    request = _source_request(census_neo4j, "same-occurrence")
    first = repository.admit(request, authority=_authority())
    second = repository.admit(request, authority=_authority())
    assert first.census_id == second.census_id
    assert first.created is True
    assert second.created is False
    with pytest.raises(StandaloneCrmCensusConflictError):
        repository.admit(
            _source_request(census_neo4j, "same-occurrence", configuration_digest="config-b"),
            authority=_authority(),
        )
    with pytest.raises(StandaloneCrmCensusConflictError):
        repository.admit(_source_request(census_neo4j, "other-occurrence"), authority=_authority())


def test_concurrent_active_scope_admission_has_exactly_one_owner(census_neo4j: CensusNeo4j) -> None:
    repository = prepare_ready(census_neo4j)

    def admit(occurrence: str) -> bool:
        try:
            repository.admit(_source_request(census_neo4j, occurrence), authority=_authority())
        except StandaloneCrmCensusConflictError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(admit, ("concurrent-a", "concurrent-b")))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1


@pytest.mark.parametrize(
    ("kinds", "bounds"),
    (
        (("company", "contact", "lead"), (("company", 1), ("contact", 3), ("lead", 2))),
        (("company", "contact"), (("company", 4), ("contact", 0))),
        (("lead",), (("lead", 0),)),
    ),
)
def test_source_window_freeze_is_atomic_for_full_subset_and_mixed_zero(
    census_neo4j: CensusNeo4j,
    kinds: tuple[Literal["contact", "lead", "company"], ...],
    bounds: tuple[tuple[Literal["contact", "lead", "company"], int], ...],
) -> None:
    census = _claim_source(census_neo4j, f"freeze-{len(kinds)}-{bounds[-1][1]}", kinds)
    repository = census_neo4j.repository
    _freeze(repository, census, bounds)
    status = repository.status(census.admission.census_id)
    assert status is not None
    assert {(unit["unit_kind"], unit["upper_id"]) for unit in status.units} == set(bounds)
    assert len(status.units) == len(bounds)
    with pytest.raises(StandaloneCrmCensusStaleError):
        _freeze(repository, census, bounds)


def test_mapping_no_source_window_rejects_http_reservation(census_neo4j: CensusNeo4j) -> None:
    repository = prepare_ready(census_neo4j)
    request = MappingPrepareCensusRequest(
        "bitrix_chat",
        census_neo4j.source_instance_id,
        census_neo4j.source_instance_id,
        "mapping-window",
        "operator",
        "policy",
        "association",
        "config",
        _budget(),
        "revision-1",
        "revision-digest",
        None,
    )
    admission = repository.admit(request, authority=None)
    attempt = repository.claim_attempt(admission, request, task_id="mapping-parent")
    repository.freeze_no_source_window(
        admission, attempt, unit_kind="mapping_prepare", revision_id="revision-1"
    )
    intent = StandaloneCrmCallIntent(
        admission.census_id,
        attempt.generation,
        attempt.parent_fence_token,
        _freshness(admission),
        "mapping-http-intent",
        1,
        "probe",
        "contact",
        0,
        "sha256:mapping-http",
    )
    assert (
        repository.reserve_call(
            intent=intent, budget_calls_per_attempt=4, budget_calls_per_occurrence=4
        )
        is False
    )


def test_mapping_cancellation_before_publication_reconciles_without_a_fence(
    census_neo4j: CensusNeo4j,
) -> None:
    repository = prepare_ready(census_neo4j)
    request = MappingPrepareCensusRequest(
        "bitrix_chat",
        census_neo4j.source_instance_id,
        census_neo4j.source_instance_id,
        "mapping-cancel-before-publication",
        "operator",
        "policy",
        "association",
        "config",
        _budget(),
        "revision-1",
        "revision-digest",
        None,
    )
    admission = repository.admit(request, authority=None)
    attempt = repository.claim_attempt(admission, request, task_id="mapping-cancel-parent")
    repository.freeze_no_source_window(
        admission, attempt, unit_kind="mapping_prepare", revision_id="revision-1"
    )
    assert repository.request_cancel(admission, actor="operator", reason="stop") == 1
    state, accounting = repository.reconcile_terminal(admission, attempt)
    assert state == "cancelled_with_checkpoint"
    assert accounting.skipped_units == 0
    status = repository.status(admission.census_id)
    assert status is not None
    assert status.publications == ()
    assert status.fences == ()


def test_attempt_redelivery_exclusion_and_expired_recovery(census_neo4j: CensusNeo4j) -> None:
    census = _claim_source(census_neo4j, "attempts")
    repository = census_neo4j.repository
    redelivery = repository.claim_attempt(
        census.admission, census.request, task_id="parent-attempts"
    )
    assert redelivery.generation == census.attempt.generation
    assert redelivery.parent_fence_token == census.attempt.parent_fence_token
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.claim_attempt(census.admission, census.request, task_id="competing-parent")
    with census_neo4j.driver.session() as session:
        session.run(
            "MATCH (attempt:StandaloneCrmCensusAttempt "
            "{census_id: $census_id, generation: $generation}) "
            "SET attempt.lease_until = datetime() - duration({seconds: 1})",
            census_id=census.admission.census_id,
            generation=census.attempt.generation,
        ).consume()
    repository.recover_expired_attempt(census.admission, census.attempt)
    successor = repository.claim_attempt(
        census.admission, census.request, task_id="recovered-parent"
    )
    assert successor.generation == census.attempt.generation + 1
    assert successor.parent_fence_token > census.attempt.parent_fence_token


def test_continuation_advances_only_a_paused_generation_and_preserves_history(
    census_neo4j: CensusNeo4j,
) -> None:
    census = _claim_source(census_neo4j, "continuation")
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 2),))
    first_envelope, _first_publication_id = _reserve_published_contact(
        repository, census, upper_id=2
    )
    first_token = repository.claim_child_fence(
        census.admission, first_envelope, worker_task_id="first-child", lease_seconds=60
    )
    first_checkpoint = StandaloneCrmCheckpoint(
        census.admission.census_id,
        "contact",
        2,
        1,
        None,
        1,
        0,
        0,
        0,
        census.attempt.generation,
        census.attempt.parent_fence_token,
        first_token,
        "first-child",
    )
    repository.checkpoint_child(
        census.admission,
        first_checkpoint,
        expected_version=1,
        max_rows_per_attempt=20,
        max_rows_per_occurrence=20,
    )
    repository.release_child_fence(census.admission, first_checkpoint)
    repository.pause(census.admission, census.attempt, reason="worker_paused")
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.continue_attempt(census.admission, census.request, task_id="parent-continuation")

    successor = repository.continue_attempt(
        census.admission, census.request, task_id="continuation-parent"
    )
    assert successor.generation == census.attempt.generation + 1
    assert successor.parent_fence_token > census.attempt.parent_fence_token
    assert (
        repository.continue_attempt(census.admission, census.request, task_id="continuation-parent")
        == successor
    )

    successor_census = _ReadyCensus(census.admission, successor, census.request)
    successor_envelope, _publication_id = _reserve_published_contact(
        repository, successor_census, upper_id=2
    )
    successor_token = repository.claim_child_fence(
        census.admission, successor_envelope, worker_task_id="second-child", lease_seconds=60
    )
    repository.settle_child(
        census.admission,
        StandaloneCrmCheckpoint(
            census.admission.census_id,
            "contact",
            2,
            2,
            None,
            2,
            0,
            0,
            0,
            successor.generation,
            successor.parent_fence_token,
            successor_token,
            "second-child",
        ),
        terminal_state="completed",
        expected_version=2,
        max_rows_per_attempt=20,
        max_rows_per_occurrence=20,
    )
    assert repository.reconcile_terminal(census.admission, successor)[0] == "completed"


@pytest.mark.parametrize("budget_field", ["attempt", "calls", "rows", "deadline"])
def test_continuation_exhaustion_fails_instead_of_creating_a_generation(
    census_neo4j: CensusNeo4j, budget_field: str
) -> None:
    budget = _budget(calls=1, attempts=1)
    census = _claim_source(census_neo4j, f"continuation-exhausted-{budget_field}", budget=budget)
    repository = census_neo4j.repository
    repository.pause(census.admission, census.attempt, reason="paused")
    if budget_field == "calls":
        query, params = (
            "MATCH (c:StandaloneCrmCensus {census_id: $id}) SET c.call_count = 1",
            {"id": census.admission.census_id},
        )
    elif budget_field == "rows":
        query, params = (
            "MATCH (c:StandaloneCrmCensus {census_id: $id}) SET c.row_count = 20",
            {"id": census.admission.census_id},
        )
    elif budget_field == "deadline":
        query, params = (
            "MATCH (c:StandaloneCrmCensus {census_id: $id}) "
            "SET c.occurrence_deadline_at = datetime() - duration({seconds: 1})",
            {"id": census.admission.census_id},
        )
    else:
        query, params = (
            "MATCH (c:StandaloneCrmCensus {census_id: $id}) SET c.attempt_count = 1",
            {"id": census.admission.census_id},
        )
    with census_neo4j.driver.session() as session:
        session.run(query, **params).consume()
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.continue_attempt(census.admission, census.request, task_id="next-parent")
    status = repository.status(census.admission.census_id)
    assert status is not None
    assert status.census["terminal_state"] == "failed"


def test_continuation_rejects_cancelled_and_stale_admission(census_neo4j: CensusNeo4j) -> None:
    census = _claim_source(census_neo4j, "continuation-stale")
    repository = census_neo4j.repository
    repository.pause(census.admission, census.attempt, reason="paused")
    stale = StandaloneCrmCensusAdmission(
        census.admission.census_id,
        census.admission.state,
        census.admission.fingerprint,
        "other-authority",
        census.admission.source_instance_id,
        census.admission.control_instance_id,
        False,
    )
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.continue_attempt(stale, census.request, task_id="stale-parent")
    repository.request_cancel(census.admission, actor="operator", reason="stop")
    with pytest.raises(StandaloneCrmCensusStaleError):
        repository.continue_attempt(census.admission, census.request, task_id="next-parent")


def test_unknown_call_classification_is_one_way_and_requires_a_new_intent(
    census_neo4j: CensusNeo4j,
) -> None:
    census = _claim_source(census_neo4j, "unknown-call", budget=_budget(calls=3))
    repository = census_neo4j.repository
    _freeze(repository, census, (("contact", 1),))
    intent = _call_intent(census, "unknown-intent", 1)
    assert repository.reserve_call(
        intent=intent, budget_calls_per_attempt=3, budget_calls_per_occurrence=3
    )
    assert repository.classify_current_reserved_call_unknown(
        census.admission, intent_id="unknown-intent"
    )
    assert not repository.classify_current_reserved_call_unknown(
        census.admission, intent_id="unknown-intent"
    )
    assert not repository.record_call_outcome(intent, "failed")
    retry = _call_intent(census, "new-intent", 2)
    assert repository.reserve_call(
        intent=retry, budget_calls_per_attempt=3, budget_calls_per_occurrence=3
    )
