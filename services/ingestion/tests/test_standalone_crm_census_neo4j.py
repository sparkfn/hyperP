"""Disposable real-Neo4j acceptance coverage for standalone CRM census control.

The suite is deliberately opt-in.  It only accepts the dedicated disposable
``HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST_*`` database and never contacts a
Bitrix endpoint.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.graph import standalone_crm_census_migration as standalone_census_migration
from src.graph.bitrix_source_instances import (
    BitrixSourceInstanceConflictError,
    BitrixSourceInstanceRepository,
)
from src.graph.bootstrap import bootstrap_legacy_bitrix_source_instance
from src.graph.client import Neo4jClient
from src.graph.ingestion_control_instance_migration import migrate_ingestion_control_instances
from src.graph.queries.standalone_crm_census import (
    FREEZE_NO_SOURCE_WINDOW,
    FREEZE_SOURCE_WINDOW,
)
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.graph.standalone_crm_census_migration import (
    MIGRATION_KEY,
    assert_standalone_crm_census_ready,
    ensure_standalone_crm_census_ready,
)
from src.standalone_crm_census_models import (
    MappingPrepareAuthority,
    MappingPrepareCensusRequest,
    NoSourceWindow,
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    SourceWindow,
    StandaloneCrmBudget,
    StandaloneCrmCallIntent,
    StandaloneCrmCallOutcome,
    StandaloneCrmCensusConflictError,
    StandaloneCrmCensusUnit,
    StandaloneCrmCheckpoint,
    StandaloneCrmChildEnvelope,
    StandaloneCrmPublication,
    StandaloneCrmReason,
    StandaloneCrmStreamKind,
)

T = TypeVar("T")

_MIGRATION_CONSTRAINT = (
    "CREATE CONSTRAINT data_migration_key_unique IF NOT EXISTS "
    "FOR (migration:DataMigration) REQUIRE migration.migration_key IS UNIQUE"
)
_REGISTRY_CONSTRAINT = (
    "CREATE CONSTRAINT bitrix_source_instance_identity_unique IF NOT EXISTS "
    "FOR (instance:BitrixSourceInstance) "
    "REQUIRE (instance.source_key, instance.source_instance_id) IS UNIQUE"
)
_STANDALONE_CONSTRAINTS = (
    "standalone_crm_census_id_unique",
    "standalone_crm_census_occurrence_unique",
    "standalone_crm_attempt_identity_unique",
    "standalone_crm_unit_identity_unique",
    "standalone_crm_call_intent_unique",
    "standalone_crm_call_sequence_unique",
    "standalone_crm_publication_unique",
)


class _Client:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._driver.session() as session:
            yield session

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        with self._driver.session() as session:
            return session.execute_read(work)

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        with self._driver.session() as session:
            return session.execute_write(work)


def _drop_suite_constraints(driver: Driver) -> None:
    with driver.session() as session:
        for name in _STANDALONE_CONSTRAINTS:
            session.run(f"DROP CONSTRAINT {name} IF EXISTS").consume()


def _delete_standalone_artifacts(driver: Driver) -> None:
    labels = (
        "StandaloneCrmCensus",
        "StandaloneCrmCensusActiveScope",
        "StandaloneCrmCensusAttempt",
        "StandaloneCrmCensusUnit",
        "StandaloneCrmCensusCheckpoint",
        "StandaloneCrmCensusContinuation",
        "StandaloneCrmCensusFence",
        "StandaloneCrmChildPublication",
        "StandaloneCrmHttpCallReservation",
    )
    with driver.session() as session:
        for label in labels:
            session.run(f"MATCH (node:{label}) DETACH DELETE node").consume()
        session.run(
            "MATCH (migration:DataMigration {migration_key: $key}) DETACH DELETE migration",
            key=MIGRATION_KEY,
        ).consume()


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    uri = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST_URI")
    password = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST_PASSWORD")
    if uri is None or password is None:
        pytest.skip("disposable standalone CRM census Neo4j database is not configured")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if os.getenv("HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST_SERVICE_HOST") == "neo4j":
        allowed_hosts.add("neo4j")
    if urlparse(uri).hostname not in allowed_hosts:
        pytest.fail("standalone CRM census tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST_USER", "neo4j"), password),
    )
    connected = False
    prepared = False
    try:
        for _ in range(15):
            try:
                driver.verify_connectivity()
                connected = True
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        else:
            pytest.fail("disposable standalone CRM census Neo4j database did not become ready")
        _delete_standalone_artifacts(driver)
        prepared = True
        yield driver
    finally:
        try:
            if prepared:
                _delete_standalone_artifacts(driver)
            if connected:
                _drop_suite_constraints(driver)
        finally:
            driver.close()


def _install_control_dependency(driver: Driver) -> _Client:
    client = _Client(driver)
    with driver.session() as session:
        session.run(_MIGRATION_CONSTRAINT).consume()
        session.run(_REGISTRY_CONSTRAINT).consume()
        session.run(
            "MERGE (source:SourceSystem {source_key: 'bitrix_chat'}) SET source.is_active = true"
        ).consume()
    migrate_ingestion_control_instances(
        cast(Neo4jClient, client),
        ensure_legacy_registration=lambda: bootstrap_legacy_bitrix_source_instance(
            cast(Neo4jClient, client)
        ),
    )
    return client


def _prepare_repository(driver: Driver) -> StandaloneCrmCensusRepository:
    client = _install_control_dependency(driver)
    ensure_standalone_crm_census_ready(cast(Neo4jClient, client))
    return StandaloneCrmCensusRepository(cast(Neo4jClient, client))


def _budget(
    calls: int = 4,
    *,
    attempts: int = 4,
    deadline: str = "2099-01-01T00:00:00Z",
    max_attempt_rows: int = 20,
    max_occurrence_rows: int = 100,
) -> StandaloneCrmBudget:
    return StandaloneCrmBudget(
        calls, max_attempt_rows, 60, calls, max_occurrence_rows, attempts, deadline
    )


def _source_request(
    *,
    occurrence: str = "occurrence-a",
    source_instance: str = "portal-a",
    control_instance: str = "portal-a",
    calls: int = 4,
    attempts: int = 4,
    deadline: str = "2099-01-01T00:00:00Z",
    authority_mapping_digest: str = "digest-a",
    max_attempt_rows: int = 20,
    max_occurrence_rows: int = 100,
) -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        source_instance,
        control_instance,
        occurrence,
        ("contact", "lead"),
        _budget(
            calls,
            attempts=attempts,
            deadline=deadline,
            max_attempt_rows=max_attempt_rows,
            max_occurrence_rows=max_occurrence_rows,
        ),
        "policy-v1",
        "association-v1",
        "sha256:" + "a" * 64,
        SourceSyncAuthority("mapping-a", authority_mapping_digest, "projection-a", "digest-b"),
    )


def _prepare_request() -> MappingPrepareCensusRequest:
    return MappingPrepareCensusRequest(
        "bitrix_chat",
        "portal-a",
        "portal-a",
        "mapping-occurrence",
        ("contact",),
        _budget(),
        "policy-v1",
        "association-v1",
        "sha256:" + "a" * 64,
        MappingPrepareAuthority("prepared-a", "digest-a", "head-a"),
    )


def _register(driver: Driver, source_instance: str = "portal-a") -> None:
    client = _Client(driver)
    BitrixSourceInstanceRepository(cast(Neo4jClient, client)).register(
        "bitrix_chat", source_instance
    )


def _claim(
    repository: StandaloneCrmCensusRepository,
    census_id: str,
    request: SourceSyncCensusRequest | MappingPrepareCensusRequest,
    generation: int = 1,
) -> None:
    assert repository.claim_attempt(census_id, generation, generation, request) is True


def _intent(census_id: str, intent_id: str, generation: int = 1) -> StandaloneCrmCallIntent:
    return StandaloneCrmCallIntent(
        census_id,
        generation,
        intent_id,
        1,
        "probe",
        "contact",
        0,
        "2099-01-01T00:00:00Z",
    )


def _freeze_source_window(
    repository: StandaloneCrmCensusRepository,
    census_id: str,
    request: SourceSyncCensusRequest,
    bounds: tuple[tuple[StandaloneCrmStreamKind, int], ...],
    *,
    generation: int = 1,
) -> None:
    for stream_kind, upper_id in bounds:
        intent_id = f"{census_id}-{generation}-{stream_kind}-probe"
        intent = StandaloneCrmCallIntent(
            census_id,
            generation,
            intent_id,
            1,
            "probe",
            stream_kind,
            0,
            "2099-01-01T00:00:00Z",
        )
        assert repository.reserve_call(intent, generation, request) is True
        assert (
            repository.record_call_outcome(
                StandaloneCrmCallOutcome(
                    intent_id, "probe", "succeeded", "2099-01-01T00:00:00Z", upper_id
                )
            )
            is True
        )
    assert repository.freeze_source_window(census_id, generation, SourceWindow(bounds)) is True


def _freeze_mapping_window(
    repository: StandaloneCrmCensusRepository,
    census_id: str,
    generation: int = 1,
) -> None:
    assert (
        repository.freeze_no_source_window(
            census_id, generation, NoSourceWindow("prepared-a", "digest-a")
        )
        is True
    )


def test_readiness_requires_272_and_is_idempotent(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _Client(neo4j_driver)
    monkeypatch.setattr(
        standalone_census_migration,
        "assert_ingestion_control_ready",
        lambda _client: (_ for _ in ()).throw(RuntimeError("#272 is incomplete")),
    )
    with pytest.raises(RuntimeError):
        ensure_standalone_crm_census_ready(cast(Neo4jClient, client))

    monkeypatch.undo()

    _install_control_dependency(neo4j_driver)
    ensure_standalone_crm_census_ready(cast(Neo4jClient, client))
    ensure_standalone_crm_census_ready(cast(Neo4jClient, client))
    assert_standalone_crm_census_ready(cast(Neo4jClient, client))

    with neo4j_driver.session() as session:
        marker = session.run(
            "MATCH (migration:DataMigration {migration_key: $key}) "
            "RETURN migration.completed_at IS NOT NULL AS ready",
            key=MIGRATION_KEY,
        ).single(strict=True)
        constraints = {
            row["name"] for row in session.run("SHOW CONSTRAINTS YIELD name RETURN name")
        }
    assert marker["ready"] is True
    assert set(_STANDALONE_CONSTRAINTS) <= constraints


def test_readiness_installs_exact_census_constraints_and_lookup_indexes(
    neo4j_driver: Driver,
) -> None:
    _prepare_repository(neo4j_driver)
    expected_constraints = {
        "standalone_crm_census_id_unique": ("StandaloneCrmCensus", ("census_id",)),
        "standalone_crm_census_occurrence_unique": (
            "StandaloneCrmCensus",
            (
                "source_key",
                "source_instance_id",
                "control_instance_id",
                "census_kind",
                "occurrence_key",
            ),
        ),
        "standalone_crm_attempt_identity_unique": (
            "StandaloneCrmCensusAttempt",
            ("census_id", "generation"),
        ),
        "standalone_crm_unit_identity_unique": (
            "StandaloneCrmCensusUnit",
            ("census_id", "stream_kind"),
        ),
        "standalone_crm_call_intent_unique": ("StandaloneCrmHttpCallReservation", ("intent_id",)),
        "standalone_crm_call_sequence_unique": (
            "StandaloneCrmHttpCallReservation",
            ("census_id", "call_sequence"),
        ),
        "standalone_crm_publication_unique": (
            "StandaloneCrmChildPublication",
            ("census_id", "generation", "stream_kind"),
        ),
        "standalone_crm_checkpoint_unique": (
            "StandaloneCrmCensusCheckpoint",
            ("census_id", "stream_kind"),
        ),
        "standalone_crm_fence_unique": (
            "StandaloneCrmCensusFence",
            ("census_id", "generation", "stream_kind"),
        ),
        "standalone_crm_active_scope_unique": ("StandaloneCrmCensusActiveScope", ("scope_key",)),
    }
    expected_indexes = {
        "standalone_crm_census_active_scope": (
            "StandaloneCrmCensus",
            ("source_key", "source_instance_id", "control_instance_id", "status"),
        ),
        "standalone_crm_attempt_lease": (
            "StandaloneCrmCensusAttempt",
            ("census_id", "status", "lease_until"),
        ),
        "standalone_crm_unit_status": (
            "StandaloneCrmCensusUnit",
            ("census_id", "generation", "state"),
        ),
        "standalone_crm_publication_status": (
            "StandaloneCrmChildPublication",
            ("census_id", "generation", "status"),
        ),
    }
    with neo4j_driver.session() as session:
        constraints = {
            str(row["name"]): (tuple(row["labelsOrTypes"]), tuple(row["properties"]))
            for row in session.run(
                "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties "
                "RETURN name, labelsOrTypes, properties"
            )
        }
        indexes = {
            str(row["name"]): (tuple(row["labelsOrTypes"]), tuple(row["properties"]))
            for row in session.run(
                "SHOW INDEXES YIELD name, labelsOrTypes, properties "
                "WHERE labelsOrTypes IS NOT NULL AND properties IS NOT NULL "
                "RETURN name, labelsOrTypes, properties"
            )
        }
    assert {
        name: (values[0][0], values[1])
        for name, values in constraints.items()
        if name in expected_constraints
    } == expected_constraints
    assert {
        name: (values[0][0], values[1])
        for name, values in indexes.items()
        if name in expected_indexes
    } == expected_indexes


def test_concurrent_replay_is_singleton_and_conflicting_admission_is_rejected(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request()

    with ThreadPoolExecutor(max_workers=2) as pool:
        admissions = list(pool.map(lambda _: repository.admit(request), range(2)))

    assert len({admission.census_id for admission in admissions}) == 1
    assert sorted(admission.replayed for admission in admissions) == [False, True]
    conflicting = _source_request(calls=3)
    with pytest.raises(StandaloneCrmCensusConflictError):
        repository.admit(conflicting)
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (census:StandaloneCrmCensus {occurrence_key: 'occurrence-a'}) "
            "RETURN count(census) AS count"
        ).single(strict=True)
    assert row["count"] == 1


def test_fresh_admission_has_a_canonical_zero_generation_and_runnable_snapshot(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request(occurrence="fresh-generation")
    admission = repository.admit(request)
    status = repository.status(admission.census_id)
    snapshot = repository.runtime_snapshot(admission.census_id)
    assert status is not None
    assert snapshot is not None
    assert (status.generation, snapshot.generation, snapshot.request) == (0, 0, request)
    assert repository.claim_attempt(admission.census_id, 1, 1, request) is True


def test_reservation_race_enforces_an_atomic_occurrence_and_attempt_budget(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request(calls=1)
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reserved = list(
            pool.map(
                lambda intent_id: repository.reserve_call(
                    _intent(admission.census_id, intent_id), 1, _source_request(calls=1)
                ),
                ("intent-a", "intent-b"),
            )
        )

    assert reserved.count(True) == 1
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "OPTIONAL MATCH (call:StandaloneCrmHttpCallReservation {census_id: $census_id}) "
            "RETURN census.occurrence_calls AS occurrence_calls, count(call) AS calls",
            census_id=admission.census_id,
        ).single(strict=True)
    assert row["occurrence_calls"] == 1
    assert row["calls"] == 1


def test_source_and_no_source_windows_are_one_shot_and_preserve_full_boundaries(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    source_request = _source_request()
    source = repository.admit(source_request)
    _claim(repository, source.census_id, source_request)
    source_window = SourceWindow((("contact", 7), ("lead", 9)))
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "SET census.status = 'freezing'",
            census_id=source.census_id,
        ).consume()
    assert repository.freeze_source_window(source.census_id, 1, source_window) is True
    assert repository.freeze_source_window(source.census_id, 1, source_window) is False
    status = repository.status(source.census_id)
    assert status is not None
    assert status.state == "frozen"
    assert status.window_frozen is True
    with neo4j_driver.session() as session:
        stored = session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "RETURN census.window_json AS window_json",
            census_id=source.census_id,
        ).single(strict=True)
    assert json.loads(stored["window_json"])["selected_bounds"] == [["contact", 7], ["lead", 9]]

    mapping_request = _prepare_request()
    mapping = repository.admit(mapping_request)
    _claim(repository, mapping.census_id, mapping_request)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "SET census.status = 'freezing'",
            census_id=mapping.census_id,
        ).consume()
    no_source_window = NoSourceWindow("prepared-a", "digest-a")
    assert repository.freeze_no_source_window(mapping.census_id, 1, no_source_window) is True
    assert repository.freeze_no_source_window(mapping.census_id, 1, no_source_window) is False


def test_legacy_named_freeze_queries_are_compiled_by_real_neo4j(neo4j_driver: Driver) -> None:
    """Keep direct execution coverage while repository code uses the unified freeze CAS."""
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    source_request = _source_request(occurrence="direct-freeze-source")
    source = repository.admit(source_request)
    _claim(repository, source.census_id, source_request)
    mapping_request = _prepare_request()
    mapping = repository.admit(mapping_request)
    _claim(repository, mapping.census_id, mapping_request)

    with neo4j_driver.session() as session:
        source_result = session.run(
            FREEZE_SOURCE_WINDOW,
            census_id=source.census_id,
            generation=1,
            window_json='{"direct":true}',
        )
        source_result.consume()
        mapping_result = session.run(
            FREEZE_NO_SOURCE_WINDOW,
            census_id=mapping.census_id,
            generation=1,
            window_json='{"direct":true}',
        )
        mapping_result.consume()


def test_next_generation_rejects_stale_fence_reservations(neo4j_driver: Driver) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request()
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: 1}) "
            "SET attempt.lease_until = datetime() - duration({seconds: 1})",
            census_id=admission.census_id,
        ).consume()
    takeover = repository.recover_or_take_over_attempt(admission.census_id, 1, 2, 2)
    assert takeover is not None
    assert (takeover.generation, takeover.fence_token) == (2, 2)

    assert (
        repository.reserve_call(
            _intent(admission.census_id, "stale-intent", generation=1), 1, _source_request()
        )
        is False
    )
    assert (
        repository.reserve_call(
            _intent(admission.census_id, "fresh-intent", generation=2), 2, _source_request()
        )
        is True
    )
    with neo4j_driver.session() as session:
        attempts = session.run(
            "MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id}) "
            "RETURN attempt.generation AS generation, attempt.status AS status "
            "ORDER BY generation",
            census_id=admission.census_id,
        )
        states = [(row["generation"], row["status"]) for row in attempts]
    assert states == [(1, "superseded"), (2, "running")]


def test_attempt_limits_deadlines_snapshots_and_durable_call_outcomes(neo4j_driver: Driver) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request(calls=2, attempts=1)
    admission = repository.admit(request)
    assert repository.claim_attempt(admission.census_id, 2, 2, request) is False
    _claim(repository, admission.census_id, request)
    snapshot = repository.runtime_snapshot(admission.census_id)
    assert snapshot is not None
    assert (snapshot.generation, snapshot.state, snapshot.request) == (1, "freezing", request)

    first = _intent(admission.census_id, "outcome-a")
    second = _intent(admission.census_id, "outcome-b")
    assert repository.reserve_call(first, 1, request) is True
    assert (
        repository.record_call_outcome(
            StandaloneCrmCallOutcome(
                first.intent_id, "probe", "succeeded", "2099-01-01T00:00:00Z", 0
            )
        )
        is True
    )
    assert repository.reserve_call(second, 1, request) is True
    assert repository.classify_unresolved_calls(admission.census_id) == 1
    assert (
        repository.record_call_outcome(
            StandaloneCrmCallOutcome(
                second.intent_id, "probe", "unknown", "2099-01-01T00:00:00Z", None, "lost"
            )
        )
        is False
    )

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "SET census.status = 'paused_with_checkpoint'",
            census_id=admission.census_id,
        ).consume()
    assert repository.claim_attempt(admission.census_id, 2, 2, request) is False
    expired = _source_request(occurrence="expired", deadline="2000-01-01T00:00:00Z")
    expired_admission = repository.admit(expired)
    assert repository.claim_attempt(expired_admission.census_id, 1, 1, expired) is False


def test_continuation_pause_resume_and_pre_window_freeze_failure(neo4j_driver: Driver) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request()
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)
    assert (
        repository.fail_freeze(
            admission.census_id, 1, StandaloneCrmReason("freeze_failed", "probe failed")
        )
        is True
    )
    assert repository.resume(admission.census_id) is False

    continued = repository.admit(_source_request(occurrence="continued"))
    continued_request = _source_request(occurrence="continued")
    _claim(repository, continued.census_id, continued_request)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "SET census.status = 'paused_with_checkpoint'",
            census_id=continued.census_id,
        ).consume()
    assert repository.create_continuation(continued.census_id, 1, continued_request) == 2
    assert repository.resume(continued.census_id) is False

    paused = repository.admit(_source_request(occurrence="paused"))
    paused_request = _source_request(occurrence="paused")
    _claim(repository, paused.census_id, paused_request)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "SET census.status = 'running'",
            census_id=paused.census_id,
        ).consume()
    assert repository.pause(paused.census_id, 1, "budget_exhausted", "attempt exhausted") is True
    assert repository.resume(paused.census_id) is True


def test_publication_confirmation_and_terminalization_leave_no_active_fence(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request()
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "SET census.status = 'freezing'",
            census_id=admission.census_id,
        ).consume()
    assert (
        repository.freeze_source_window(
            admission.census_id, 1, SourceWindow((("contact", 3), ("lead", 5)))
        )
        is True
    )

    publication = StandaloneCrmPublication(
        admission.census_id,
        1,
        "contact",
        "child-task-a",
        "sha256:" + "f" * 64,
        "pending",
    )
    conflicting_task = StandaloneCrmPublication(
        admission.census_id,
        1,
        "contact",
        "child-task-b",
        "sha256:" + "e" * 64,
        "pending",
    )
    reason = StandaloneCrmReason("call_failed", "all work settled")
    authority_revision = "digest-a:digest-b"

    assert repository.reserve_publication(publication) is True
    assert repository.reserve_publication(conflicting_task) is False
    assert (
        repository.terminalize(admission.census_id, 1, "completed", reason, authority_revision)
        is False
    )
    assert repository.confirm_publication(publication) is True
    assert (
        repository.terminalize(admission.census_id, 1, "completed", reason, authority_revision)
        is False
    )

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: 1}) "
            "SET attempt.status = 'completed'",
            census_id=admission.census_id,
        ).consume()
    assert (
        repository.terminalize(admission.census_id, 1, "completed", reason, authority_revision)
        is True
    )
    assert (
        repository.terminalize(admission.census_id, 1, "completed", reason, authority_revision)
        is False
    )

    with neo4j_driver.session() as session:
        terminal = session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "OPTIONAL MATCH (active:StandaloneCrmCensusAttempt {census_id: $census_id, "
            "status: 'running'}) "
            "OPTIONAL MATCH (unsettled:StandaloneCrmChildPublication {census_id: $census_id}) "
            "WHERE unsettled.status IN ['pending', 'publishing'] "
            "RETURN census.status AS status, census.terminal_reason AS terminal_reason, "
            "count(DISTINCT active) AS active_fences, count(DISTINCT unsettled) AS unsettled",
            census_id=admission.census_id,
        ).single(strict=True)
    assert dict(terminal) == {
        "status": "completed",
        "terminal_reason": "call_failed",
        "active_fences": 0,
        "unsettled": 0,
    }


def test_units_fences_checkpoints_and_publication_repair_execute_recovery_paths(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request(occurrence="work-path")
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)
    assert (
        repository.freeze_source_window(admission.census_id, 1, SourceWindow((("contact", 5),)))
        is True
    )
    unit = StandaloneCrmCensusUnit(
        admission.census_id, 1, "contact", "pending_publication", 5, None
    )
    assert repository.allocate_units(admission.census_id, 1, (unit,)) == 1

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "MATCH (unit:StandaloneCrmCensusUnit {census_id: $census_id, stream_kind: 'contact'}) "
            "SET census.status = 'running', unit.state = 'queued'",
            census_id=admission.census_id,
        ).consume()
    first_fence = repository.acquire_unit_fence(admission.census_id, 1, "contact", "worker-a")
    assert first_fence is not None
    assert (
        repository.renew_unit_fence(admission.census_id, 1, "contact", first_fence, "worker-a")
        is True
    )
    checkpoint = StandaloneCrmCheckpoint(
        admission.census_id, "contact", 5, None, 3, None, None, 3, 0, 1, first_fence
    )
    assert repository.store_checkpoint(checkpoint, attempt_rows=3, occurrence_rows=3) is True
    regressing = StandaloneCrmCheckpoint(
        admission.census_id, "contact", 5, None, 2, None, None, 2, 0, 1, first_fence
    )
    assert repository.store_checkpoint(regressing, attempt_rows=2, occurrence_rows=2) is False

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (fence:StandaloneCrmCensusFence {census_id: $census_id, "
            "stream_kind: 'contact'}) "
            "SET fence.lease_until = datetime() - duration({seconds: 1})",
            census_id=admission.census_id,
        ).consume()
    second_fence = repository.acquire_unit_fence(admission.census_id, 1, "contact", "worker-b")
    assert second_fence is not None and second_fence > first_fence
    assert (
        repository.settle_unit(admission.census_id, 1, "contact", first_fence, "completed") is False
    )
    assert (
        repository.settle_unit(admission.census_id, 1, "contact", second_fence, "completed") is True
    )

    repair_admission = repository.admit(_source_request(occurrence="repair-path"))
    repair_request = _source_request(occurrence="repair-path")
    _claim(repository, repair_admission.census_id, repair_request)
    assert (
        repository.freeze_source_window(
            repair_admission.census_id, 1, SourceWindow((("contact", 1),))
        )
        is True
    )
    envelope = StandaloneCrmChildEnvelope(
        repair_admission.census_id, 1, "contact", 1, None, "child.task", "repair-task", "ingestion"
    )
    assert repository.reserve_child_envelope(envelope) is True
    publication = StandaloneCrmPublication(
        repair_admission.census_id,
        1,
        "contact",
        "repair-task",
        envelope.payload_digest(),
        "pending",
    )
    assert repository.mark_publication_publishing(publication) is not None
    repairs = repository.repair_publications(repair_admission.census_id)
    assert [(repair.task_id, repair.state) for repair in repairs] == [("repair-task", "publishing")]
    assert repository.confirm_publication(publication) is True


def test_child_page_and_binding_calls_require_the_exact_published_task_and_active_fence(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request(occurrence="child-call-task-identity")
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)
    _freeze_source_window(repository, admission.census_id, request, (("contact", 5), ("lead", 0)))
    units = (
        StandaloneCrmCensusUnit(admission.census_id, 1, "contact", "pending_publication", 5, None),
        StandaloneCrmCensusUnit(admission.census_id, 1, "lead", "no_work", 0, None),
    )
    assert repository.allocate_units(admission.census_id, 1, units) == 2
    envelope = StandaloneCrmChildEnvelope(
        admission.census_id, 1, "contact", 5, None, "child.task", "child-task-a", "ingestion"
    )
    assert repository.reserve_child_envelope(envelope) is True
    publication = StandaloneCrmPublication(
        admission.census_id, 1, "contact", "child-task-a", envelope.payload_digest(), "pending"
    )
    assert repository.confirm_publication(publication) is True
    fence = repository.acquire_unit_fence(admission.census_id, 1, "contact", "worker-a")
    assert fence is not None

    page = StandaloneCrmCallIntent(
        admission.census_id,
        1,
        "child-page-a",
        1,
        "page",
        "contact",
        0,
        "2099-01-01T00:00:00Z",
        0,
        None,
        "child-task-a",
    )
    binding = StandaloneCrmCallIntent(
        admission.census_id,
        1,
        "child-binding-a",
        1,
        "company_binding",
        "contact",
        0,
        "2099-01-01T00:00:00Z",
        42,
        42,
        "child-task-a",
    )
    wrong_task = StandaloneCrmCallIntent(
        admission.census_id,
        1,
        "child-page-wrong-task",
        1,
        "page",
        "contact",
        0,
        "2099-01-01T00:00:00Z",
        0,
        None,
        "wrong-task",
    )

    assert repository.reserve_call(page, fence, request) is True
    assert repository.reserve_call(binding, fence, request) is True
    assert repository.reserve_call(wrong_task, fence, request) is False
    with pytest.raises(ValueError, match="child task_id"):
        StandaloneCrmCallIntent(
            admission.census_id,
            1,
            "child-page-missing-task",
            1,
            "page",
            "contact",
            0,
            "2099-01-01T00:00:00Z",
            0,
        )
    with neo4j_driver.session() as session:
        calls = session.run(
            "MATCH (call:StandaloneCrmHttpCallReservation {census_id: $census_id}) "
            "WHERE call.call_kind IN ['page', 'company_binding'] "
            "RETURN call.call_kind AS kind, call.task_id AS task_id, call.cursor AS cursor, "
            "call.subject_id AS subject_id ORDER BY kind",
            census_id=admission.census_id,
        )
        values = [dict(record) for record in calls]
    assert values == [
        {"kind": "company_binding", "task_id": "child-task-a", "cursor": 42, "subject_id": 42},
        {"kind": "page", "task_id": "child-task-a", "cursor": 0, "subject_id": None},
    ]


def test_terminalization_requires_retired_fences_and_settled_selected_units(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request(occurrence="terminal-fence")
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)
    assert (
        repository.freeze_source_window(admission.census_id, 1, SourceWindow((("contact", 1),)))
        is True
    )
    unit = StandaloneCrmCensusUnit(
        admission.census_id, 1, "contact", "pending_publication", 1, None
    )
    assert repository.allocate_units(admission.census_id, 1, (unit,)) == 1
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "MATCH (unit:StandaloneCrmCensusUnit {census_id: $census_id, stream_kind: 'contact'}) "
            "MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: 1}) "
            "SET census.status = 'running', unit.state = 'queued', attempt.status = 'completed'",
            census_id=admission.census_id,
        ).consume()
    fence = repository.acquire_unit_fence(admission.census_id, 1, "contact", "worker")
    assert fence is not None
    reason = StandaloneCrmReason("call_failed", "fence must settle")
    assert (
        repository.terminalize(admission.census_id, 1, "completed", reason, "digest-a:digest-b")
        is False
    )
    assert repository.settle_unit(admission.census_id, 1, "contact", fence, "completed") is True
    assert (
        repository.terminalize(admission.census_id, 1, "completed", reason, "digest-a:digest-b")
        is True
    )
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "OPTIONAL MATCH (fence:StandaloneCrmCensusFence {census_id: $census_id, "
            "status: 'active'}) "
            "RETURN census.expected_units AS expected_units, "
            "census.completed_units AS completed_units, "
            "count(fence) AS active_fences",
            census_id=admission.census_id,
        ).single(strict=True)
    assert dict(row) == {"expected_units": 1, "completed_units": 1, "active_fences": 0}


def test_all_zero_selected_window_settles_attempt_without_child_publication(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request(occurrence="all-zero", calls=1)
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)
    assert repository.reserve_call(_intent(admission.census_id, "zero-probe"), 1, request) is True
    assert (
        repository.record_call_outcome(
            StandaloneCrmCallOutcome("zero-probe", "probe", "succeeded", "2099-01-01T00:00:00Z", 0)
        )
        is True
    )
    assert (
        repository.freeze_source_window(
            admission.census_id, 1, SourceWindow((("contact", 0), ("lead", 0)))
        )
        is True
    )
    zero_units = (
        StandaloneCrmCensusUnit(admission.census_id, 1, "contact", "no_work", 0, None),
        StandaloneCrmCensusUnit(admission.census_id, 1, "lead", "no_work", 0, None),
    )
    assert repository.allocate_units(admission.census_id, 1, zero_units) == 2
    assert repository.settle_attempt(admission.census_id, 1) is True
    assert (
        repository.terminalize(
            admission.census_id,
            1,
            "completed",
            StandaloneCrmReason("freeze_incomplete", "zero"),
            "digest-a:digest-b",
        )
        is True
    )
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "OPTIONAL MATCH (publication:StandaloneCrmChildPublication {census_id: $census_id}) "
            "RETURN census.status AS status, census.expected_units AS expected_units, "
            "count(publication) AS publications",
            census_id=admission.census_id,
        ).single(strict=True)
    assert dict(row) == {"status": "completed", "expected_units": 2, "publications": 0}


def test_cancellation_retires_publications_requests_stops_and_settles_fences(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request(occurrence="cancel-path")
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)
    assert repository.request_cancellation(admission.census_id, "operator", "before window") is True
    assert (
        repository.fail_freeze(
            admission.census_id, 1, StandaloneCrmReason("cancelled", "cancelled before window")
        )
        is True
    )

    settled = repository.admit(_source_request(occurrence="settle-cancel"))
    settled_request = _source_request(occurrence="settle-cancel")
    _claim(repository, settled.census_id, settled_request)
    assert (
        repository.freeze_source_window(settled.census_id, 1, SourceWindow((("contact", 2),)))
        is True
    )
    assert (
        repository.reserve_child_envelope(
            StandaloneCrmChildEnvelope(
                settled.census_id, 1, "contact", 2, None, "child.task", "cancel-task", "ingestion"
            )
        )
        is True
    )
    assert repository.request_cancellation(settled.census_id, "operator", "after window") is True
    assert repository.settle_cancellation(settled.census_id, 1) is True
    with neo4j_driver.session() as session:
        state = session.run(
            "MATCH (publication:StandaloneCrmChildPublication {census_id: $census_id}) "
            "RETURN publication.status AS status",
            census_id=settled.census_id,
        ).single(strict=True)
    assert state["status"] == "retired"


def test_active_census_prevents_source_disable_and_legacy_default_remains_compatible(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    repository.admit(_source_request())
    client = _Client(neo4j_driver)
    source_instances = BitrixSourceInstanceRepository(cast(Neo4jClient, client))
    with pytest.raises(BitrixSourceInstanceConflictError):
        source_instances.disable("bitrix_chat", "portal-a", "operator", "active census")

    legacy = repository.admit(
        _source_request(
            occurrence="legacy-occurrence",
            source_instance="legacy-default",
            control_instance="legacy-default",
        )
    )
    assert legacy.replayed is False
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "RETURN census.source_instance_id AS source_instance_id, "
            "census.control_instance_id AS control_instance_id",
            census_id=legacy.census_id,
        ).single(strict=True)
    assert dict(row) == {
        "source_instance_id": "legacy-default",
        "control_instance_id": "legacy-default",
    }


def test_reservation_guards_reject_stale_control_state_without_partial_mutation(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)

    def assert_empty_reservation_state(census_id: str) -> None:
        with neo4j_driver.session() as session:
            row = session.run(
                "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
                "OPTIONAL MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id}) "
                "OPTIONAL MATCH (call:StandaloneCrmHttpCallReservation {census_id: $census_id}) "
                "RETURN census.occurrence_calls AS calls, attempt.call_count AS attempt_calls, "
                "count(call) AS reservations",
                census_id=census_id,
            ).single(strict=True)
        assert dict(row) == {"calls": 0, "attempt_calls": 0, "reservations": 0}

    authority_request = _source_request(occurrence="stale-authority")
    authority_admission = repository.admit(authority_request)
    _claim(repository, authority_admission.census_id, authority_request)
    assert (
        repository.reserve_call(
            _intent(authority_admission.census_id, "stale-authority"),
            1,
            _source_request(
                occurrence="stale-authority", authority_mapping_digest="different-digest"
            ),
        )
        is False
    )
    assert_empty_reservation_state(authority_admission.census_id)
    assert (
        repository.fail_freeze(
            authority_admission.census_id, 1, StandaloneCrmReason("guarded", "authority")
        )
        is True
    )

    deadline_request = _source_request(occurrence="stale-deadline")
    deadline_admission = repository.admit(deadline_request)
    _claim(repository, deadline_admission.census_id, deadline_request)
    assert (
        repository.reserve_call(
            StandaloneCrmCallIntent(
                deadline_admission.census_id,
                1,
                "stale-deadline",
                1,
                "probe",
                "contact",
                0,
                "2000-01-01T00:00:00Z",
            ),
            1,
            deadline_request,
        )
        is False
    )
    assert_empty_reservation_state(deadline_admission.census_id)
    assert (
        repository.fail_freeze(
            deadline_admission.census_id, 1, StandaloneCrmReason("guarded", "deadline")
        )
        is True
    )

    cancelled_request = _source_request(occurrence="stale-cancellation")
    cancelled_admission = repository.admit(cancelled_request)
    _claim(repository, cancelled_admission.census_id, cancelled_request)
    assert (
        repository.request_cancellation(cancelled_admission.census_id, "operator", "stop") is True
    )
    assert (
        repository.reserve_call(
            _intent(cancelled_admission.census_id, "stale-cancellation"), 1, cancelled_request
        )
        is False
    )
    assert_empty_reservation_state(cancelled_admission.census_id)
    assert (
        repository.fail_freeze(
            cancelled_admission.census_id, 1, StandaloneCrmReason("cancelled", "stop")
        )
        is True
    )

    binding_request = _source_request(occurrence="stale-binding")
    binding_admission = repository.admit(binding_request)
    _claim(repository, binding_admission.census_id, binding_request)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (binding:BitrixExecutionSourceBinding {source_key: 'bitrix_chat', "
            "source_instance_id: 'portal-a', control_instance_id: 'portal-a'}) DELETE binding"
        ).consume()
    assert (
        repository.reserve_call(
            _intent(binding_admission.census_id, "stale-binding"), 1, binding_request
        )
        is False
    )
    assert_empty_reservation_state(binding_admission.census_id)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:BitrixExecutionSourceBinding {source_key: 'bitrix_chat', "
            "source_instance_id: 'portal-a', control_instance_id: 'portal-a'})"
        ).consume()
    assert (
        repository.fail_freeze(
            binding_admission.census_id, 1, StandaloneCrmReason("guarded", "binding")
        )
        is True
    )

    source_request = _source_request(occurrence="stale-source")
    source_admission = repository.admit(source_request)
    _claim(repository, source_admission.census_id, source_request)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (instance:BitrixSourceInstance {source_key: 'bitrix_chat', "
            "source_instance_id: 'portal-a'}) SET instance.status = 'disabled'"
        ).consume()
    assert (
        repository.reserve_call(
            _intent(source_admission.census_id, "stale-source"), 1, source_request
        )
        is False
    )
    assert_empty_reservation_state(source_admission.census_id)


def test_call_sequence_is_occurrence_wide_across_concurrent_reservations_and_continuation(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request(occurrence="sequence-wide", calls=4)
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda intent_id: repository.reserve_call(
                    _intent(admission.census_id, intent_id), 1, request
                ),
                ("sequence-a", "sequence-b"),
            )
        )
    assert outcomes == [True, True]
    assert repository.pause(admission.census_id, 1, "attempt_budget", "continue") is True
    assert repository.create_continuation(admission.census_id, 1, request) == 2
    assert (
        repository.reserve_call(
            _intent(admission.census_id, "sequence-c", generation=2), 2, request
        )
        is True
    )
    with neo4j_driver.session() as session:
        rows = session.run(
            "MATCH (call:StandaloneCrmHttpCallReservation {census_id: $census_id}) "
            "RETURN call.generation AS generation, call.call_sequence AS sequence "
            "ORDER BY sequence",
            census_id=admission.census_id,
        )
        sequences = [(row["generation"], row["sequence"]) for row in rows]
    assert sequences == [(1, 1), (1, 2), (2, 3)]


def test_terminalization_accounts_for_orphan_publications_in_all_generations(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request(occurrence="terminal-orphan")
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: 1}) "
            "SET census.status = 'running', attempt.status = 'completed' "
            "CREATE (:StandaloneCrmChildPublication {census_id: $census_id, generation: 0, "
            "stream_kind: 'company', status: 'pending'})",
            census_id=admission.census_id,
        ).consume()
    reason = StandaloneCrmReason("accounting", "orphan publication")
    assert (
        repository.terminalize(admission.census_id, 1, "completed", reason, "digest-a:digest-b")
        is False
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (publication:StandaloneCrmChildPublication "
            "{census_id: $census_id, generation: 0}) "
            "SET publication.status = 'retired'",
            census_id=admission.census_id,
        ).consume()
    assert (
        repository.terminalize(admission.census_id, 1, "completed", reason, "digest-a:digest-b")
        is True
    )


def test_cancellation_preserves_completed_work_and_terminalizes_partial_completion(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request(occurrence="partial-cancellation")
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)
    _freeze_source_window(repository, admission.census_id, request, (("contact", 5), ("lead", 5)))
    units = (
        StandaloneCrmCensusUnit(admission.census_id, 1, "contact", "queued", 5, None),
        StandaloneCrmCensusUnit(admission.census_id, 1, "lead", "queued", 5, None),
    )
    assert repository.allocate_units(admission.census_id, 1, units) == 2
    fence = repository.acquire_unit_fence(admission.census_id, 1, "contact", "worker")
    assert fence is not None
    assert repository.store_checkpoint(
        StandaloneCrmCheckpoint(
            admission.census_id, "contact", 5, None, 5, None, None, 5, 0, 1, fence
        ),
        attempt_rows=5,
        occurrence_rows=5,
    ).stored
    assert repository.settle_unit(admission.census_id, 1, "contact", fence, "completed") is True
    assert repository.request_cancellation(admission.census_id, "operator", "partial") is True
    assert repository.settle_cancellation(admission.census_id, 1) is True
    assert (
        repository.terminalize(
            admission.census_id,
            1,
            "cancelled_with_checkpoint",
            StandaloneCrmReason("cancelled", "partial"),
            "digest-a:digest-b",
        )
        is True
    )
    with neo4j_driver.session() as session:
        rows = session.run(
            "MATCH (unit:StandaloneCrmCensusUnit {census_id: $census_id}) "
            "RETURN unit.stream_kind AS stream_kind, unit.state AS state ORDER BY stream_kind",
            census_id=admission.census_id,
        )
        states = [(row["stream_kind"], row["state"]) for row in rows]
    assert states == [("contact", "completed"), ("lead", "cancelled")]


def test_checkpoint_boundaries_pause_attempts_without_advancing_durable_state(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request(
        occurrence="checkpoint-attempt-boundary", max_attempt_rows=3, max_occurrence_rows=5
    )
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "SET census.status = 'frozen'",
            census_id=admission.census_id,
        ).consume()
    unit = StandaloneCrmCensusUnit(admission.census_id, 1, "contact", "queued", 5, None)
    assert repository.allocate_units(admission.census_id, 1, (unit,)) == 1
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "SET census.status = 'running'",
            census_id=admission.census_id,
        ).consume()
    fence = repository.acquire_unit_fence(admission.census_id, 1, "contact", "worker")
    assert fence is not None
    stored = StandaloneCrmCheckpoint(
        admission.census_id, "contact", 5, None, 3, 31, 7, 3, 0, 1, fence
    )
    assert (
        repository.store_checkpoint(stored, attempt_rows=3, occurrence_rows=3).decision == "stored"
    )
    exhausted = StandaloneCrmCheckpoint(
        admission.census_id, "contact", 5, None, 4, 32, 0, 4, 0, 1, fence
    )
    result = repository.store_checkpoint(exhausted, attempt_rows=4, occurrence_rows=4)
    assert result.decision == "attempt_exhausted"
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "MATCH (attempt:StandaloneCrmCensusAttempt {census_id: $census_id, generation: 1}) "
            "MATCH (checkpoint:StandaloneCrmCensusCheckpoint {census_id: $census_id, "
            "stream_kind: 'contact'}) "
            "RETURN census.occurrence_rows AS occurrence_rows, census.status AS census_status, "
            "attempt.row_count AS attempt_rows, attempt.status AS attempt_status, "
            "checkpoint.last_committed_id AS last_committed_id, "
            "checkpoint.binding_subject_id AS binding_subject_id, "
            "checkpoint.binding_offset AS binding_offset",
            census_id=admission.census_id,
        ).single(strict=True)
    assert dict(row) == {
        "occurrence_rows": 3,
        "census_status": "paused_with_checkpoint",
        "attempt_rows": 3,
        "attempt_status": "paused_with_checkpoint",
        "last_committed_id": 3,
        "binding_subject_id": 31,
        "binding_offset": 7,
    }


def test_occurrence_exhaustion_converges_to_a_terminal_census_and_releases_scope(
    neo4j_driver: Driver,
) -> None:
    repository = _prepare_repository(neo4j_driver)
    _register(neo4j_driver)
    request = _source_request(
        occurrence="checkpoint-occurrence-exhaustion", max_attempt_rows=3, max_occurrence_rows=3
    )
    admission = repository.admit(request)
    _claim(repository, admission.census_id, request)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "SET census.status = 'frozen'",
            census_id=admission.census_id,
        ).consume()
    unit = StandaloneCrmCensusUnit(admission.census_id, 1, "contact", "queued", 5, None)
    assert repository.allocate_units(admission.census_id, 1, (unit,)) == 1
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "SET census.status = 'running'",
            census_id=admission.census_id,
        ).consume()
    fence = repository.acquire_unit_fence(admission.census_id, 1, "contact", "worker")
    assert fence is not None
    assert (
        repository.store_checkpoint(
            StandaloneCrmCheckpoint(
                admission.census_id, "contact", 5, None, 3, None, None, 3, 0, 1, fence
            ),
            attempt_rows=3,
            occurrence_rows=3,
        ).decision
        == "stored"
    )
    assert (
        repository.store_checkpoint(
            StandaloneCrmCheckpoint(
                admission.census_id, "contact", 5, None, 4, None, None, 4, 0, 1, fence
            ),
            attempt_rows=4,
            occurrence_rows=4,
        ).decision
        == "occurrence_exhausted"
    )
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: $census_id}) "
            "OPTIONAL MATCH (:StandaloneCrmCensusActiveScope)-[active:HAS_ACTIVE_CENSUS]->(census) "
            "RETURN census.status AS status, census.terminal_reason AS terminal_reason, "
            "count(active) AS active_scopes",
            census_id=admission.census_id,
        ).single(strict=True)
    assert dict(row) == {
        "status": "failed",
        "terminal_reason": "occurrence_budget_exhausted",
        "active_scopes": 0,
    }
