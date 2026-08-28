"""Real-Neo4j coverage for standalone CRM census CAS boundaries."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.graph.bitrix_source_instances import BitrixSourceInstanceRepository
from src.graph.bootstrap import bootstrap_legacy_bitrix_source_instance
from src.graph.client import Neo4jClient
from src.graph.ingestion_control_instance_migration import (
    migrate_ingestion_control_instances,
)
from src.graph.queries.standalone_crm_census import CREATE_STANDALONE_CRM_CENSUS_SCHEMA
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.standalone_crm_census_models import (
    CensusConflictError,
    CensusKind,
    HttpCallState,
    ParentState,
)

T = TypeVar("T")

_MIGRATION_CONSTRAINT = (
    "CREATE CONSTRAINT data_migration_key_unique IF NOT EXISTS "
    "FOR (n:DataMigration) REQUIRE n.migration_key IS UNIQUE"
)
_REGISTRY_CONSTRAINT = (
    "CREATE CONSTRAINT bitrix_source_instance_identity_unique IF NOT EXISTS "
    "FOR (instance:BitrixSourceInstance) "
    "REQUIRE (instance.source_key, instance.source_instance_id) IS UNIQUE"
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


@pytest.fixture
def census_client() -> Iterator[tuple[Driver, Neo4jClient]]:
    uri = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST_URI")
    password = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST_PASSWORD")
    if uri is None or password is None:
        pytest.skip("disposable Neo4j standalone CRM census database is not configured")
    host = urlparse(uri).hostname
    service_host = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST_SERVICE_HOST")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if service_host == "neo4j":
        allowed_hosts.add("neo4j")
    if host not in allowed_hosts:
        pytest.fail(
            "standalone CRM census test requires an explicitly configured disposable Neo4j host"
        )
    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST_USER", "neo4j"), password),
    )
    prepared = False
    try:
        for _ in range(15):
            try:
                driver.verify_connectivity()
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        else:
            pytest.fail("disposable standalone CRM census Neo4j database did not become ready")
        with driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n").consume()
            session.run(_MIGRATION_CONSTRAINT).consume()
            session.run(_REGISTRY_CONSTRAINT).consume()
            session.run(
                "CREATE (:SourceSystem {source_key: 'bitrix_chat', is_active: true})"
            ).consume()
            for statement in CREATE_STANDALONE_CRM_CENSUS_SCHEMA:
                session.run(statement).consume()
        migrate_ingestion_control_instances(
            cast(Neo4jClient, _Client(driver)),
            ensure_legacy_registration=lambda: bootstrap_legacy_bitrix_source_instance(
                cast(Neo4jClient, _Client(driver))
            ),
        )
        client = cast(Neo4jClient, _Client(driver))
        BitrixSourceInstanceRepository(client).register("bitrix_chat", "bitrix-primary")
        BitrixSourceInstanceRepository(client).admit(
            control_instance_id="legacy-default", source_instance_id="bitrix-primary"
        )
        prepared = True
        yield driver, client
    finally:
        try:
            if prepared:
                with driver.session() as session:
                    session.run("MATCH (n) DETACH DELETE n").consume()
                    for statement in CREATE_STANDALONE_CRM_CENSUS_SCHEMA:
                        if not statement.startswith("CREATE INDEX"):
                            name = statement.split("CREATE CONSTRAINT ", 1)[1].split(" ", 1)[0]
                            session.run(f"DROP CONSTRAINT {name} IF EXISTS").consume()
                    session.run("DROP CONSTRAINT data_migration_key_unique IF EXISTS").consume()
                    session.run(
                        "DROP CONSTRAINT bitrix_source_instance_identity_unique IF EXISTS"
                    ).consume()
        finally:
            driver.close()


def _admit(client: Neo4jClient, *, fingerprint: str = "fingerprint") -> str:
    repository = StandaloneCrmCensusRepository(client)
    census_id, _created = repository.admit(
        source_instance_id="bitrix-primary",
        control_instance_id="legacy-default",
        census_kind=CensusKind.SOURCE_SYNC,
        occurrence_key="occurrence",
        fingerprint=fingerprint,
        request_json="{}",
        budget_json="{}",
        heads_json="{}",
        occurrence_deadline=datetime.now(UTC) + timedelta(hours=1),
        occurrence_calls=100,
        occurrence_rows=100,
        attempt_calls=10,
        attempt_rows=10,
        attempt_runtime_seconds=60.0,
        max_attempts=3,
    )
    return census_id


def test_census_admission_is_idempotent_and_fingerprint_conflict_fail_closed(
    census_client: tuple[Driver, Neo4jClient],
) -> None:
    _driver, client = census_client
    repository = StandaloneCrmCensusRepository(client)
    first_id, first_created = repository.admit(
        source_instance_id="bitrix-primary",
        control_instance_id="legacy-default",
        census_kind=CensusKind.SOURCE_SYNC,
        occurrence_key="occurrence",
        fingerprint="fingerprint",
        request_json="{}",
        budget_json="{}",
        heads_json="{}",
        occurrence_deadline=datetime.now(UTC) + timedelta(hours=1),
        occurrence_calls=100,
        occurrence_rows=100,
        attempt_calls=10,
        attempt_rows=10,
        attempt_runtime_seconds=60.0,
        max_attempts=3,
    )
    second_id, second_created = repository.admit(
        source_instance_id="bitrix-primary",
        control_instance_id="legacy-default",
        census_kind=CensusKind.SOURCE_SYNC,
        occurrence_key="occurrence",
        fingerprint="fingerprint",
        request_json="{}",
        budget_json="{}",
        heads_json="{}",
        occurrence_deadline=datetime.now(UTC) + timedelta(hours=1),
        occurrence_calls=100,
        occurrence_rows=100,
        attempt_calls=10,
        attempt_rows=10,
        attempt_runtime_seconds=60.0,
        max_attempts=3,
    )

    assert first_created is True
    assert second_created is False
    assert first_id == second_id
    with pytest.raises(CensusConflictError, match="different fingerprint"):
        repository.admit(
            source_instance_id="bitrix-primary",
            control_instance_id="legacy-default",
            census_kind=CensusKind.SOURCE_SYNC,
            occurrence_key="occurrence",
            fingerprint="changed",
            request_json="{}",
            budget_json="{}",
            heads_json="{}",
            occurrence_deadline=datetime.now(UTC) + timedelta(hours=1),
            occurrence_calls=100,
            occurrence_rows=100,
            attempt_calls=10,
            attempt_rows=10,
            attempt_runtime_seconds=60.0,
            max_attempts=3,
        )


def test_attempt_claim_reservation_and_atomic_window_are_fenced(
    census_client: tuple[Driver, Neo4jClient],
) -> None:
    _driver, client = census_client
    repository = StandaloneCrmCensusRepository(client)
    census_id = _admit(client)
    generation, fence_token = repository.claim_attempt(
        census_id=census_id,
        fingerprint="fingerprint",
        attempt_deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    intent_id = "intent-one"
    assert (
        repository.reserve_http_call(
            census_id=census_id,
            fingerprint="fingerprint",
            fence_token=fence_token,
            intent_id=intent_id,
            call_kind="probe",
            unit_kind="contact",
            frozen_upper_id=None,
            cursor=None,
            retry_ordinal=1,
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        )
        is True
    )
    assert (
        repository.reserve_http_call(
            census_id=census_id,
            fingerprint="fingerprint",
            fence_token=fence_token,
            intent_id=intent_id,
            call_kind="probe",
            unit_kind="contact",
            frozen_upper_id=None,
            cursor=None,
            retry_ordinal=1,
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        )
        is False
    )
    repository.record_http_outcome(
        census_id=census_id,
        intent_id=intent_id,
        outcome=HttpCallState.SUCCEEDED,
        outcome_detail="probe",
    )
    repository.commit_source_window(
        census_id=census_id,
        fingerprint="fingerprint",
        selected_kinds=["contact", "lead", "company"],
        bounds_json='{"company": 3, "contact": 10, "lead": 0}',
    )
    units = repository.allocate_source_units(
        census_id=census_id,
        fingerprint="fingerprint",
        units=[
            {
                "unit_kind": "contact",
                "frozen_upper_id": 10,
                "revision_id": "",
                "state": "pending_publication",
                "fence_token": fence_token,
                "expected_rows": 10,
            },
            {
                "unit_kind": "company",
                "frozen_upper_id": 3,
                "revision_id": "",
                "state": "pending_publication",
                "fence_token": fence_token,
                "expected_rows": 3,
            },
            {
                "unit_kind": "lead",
                "frozen_upper_id": 0,
                "revision_id": "",
                "state": "completed",
                "fence_token": fence_token,
                "expected_rows": 0,
            },
        ],
    )

    assert generation == 1
    assert fence_token
    assert sorted(units) == ["company", "contact", "lead"]


def test_publication_checkpoint_and_terminal_accounting_settle(
    census_client: tuple[Driver, Neo4jClient],
) -> None:
    _driver, client = census_client
    repository = StandaloneCrmCensusRepository(client)
    census_id = _admit(client)
    _generation, fence_token = repository.claim_attempt(
        census_id=census_id,
        fingerprint="fingerprint",
        attempt_deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    repository.commit_source_window(
        census_id=census_id,
        fingerprint="fingerprint",
        bounds_json='{"company": 3, "contact": 10, "lead": 0}',
        selected_kinds=["contact", "company", "lead"],
    )
    repository.allocate_source_units(
        census_id=census_id,
        fingerprint="fingerprint",
        units=[
            {
                "unit_kind": "contact",
                "frozen_upper_id": 10,
                "revision_id": "",
                "state": "pending_publication",
                "fence_token": fence_token,
                "expected_rows": 10,
            },
            {
                "unit_kind": "company",
                "frozen_upper_id": 3,
                "revision_id": "",
                "state": "pending_publication",
                "fence_token": fence_token,
                "expected_rows": 3,
            },
            {
                "unit_kind": "lead",
                "frozen_upper_id": 0,
                "revision_id": "",
                "state": "completed",
                "fence_token": fence_token,
                "expected_rows": 0,
            },
        ],
    )
    for unit_kind, upper_id in (("company", 3), ("contact", 10)):
        task_id = repository.reserve_publication(
            census_id=census_id,
            fingerprint="fingerprint",
            fence_token=fence_token,
            unit_kind=unit_kind,
            publication_sequence=1 if unit_kind == "company" else 2,
            task_name=f"src.standalone_crm_census_tasks.run_{unit_kind}_child_task",
            task_id=f"task-{unit_kind}",
            queue="ingestion",
            payload_version="v1",
            payload_digest=f"digest-{unit_kind}",
            payload_json=f'{{"unit_kind":"{unit_kind}"}}',
        )
        assert task_id == f"task-{unit_kind}"
        repository.confirm_publication(census_id=census_id, task_id=task_id)
        repository.claim_child(
            census_id=census_id,
            fingerprint="fingerprint",
            fence_token=fence_token,
            unit_kind=unit_kind,
        )
        repository.advance_checkpoint(
            census_id=census_id,
            fingerprint="fingerprint",
            fence_token=fence_token,
            unit_kind=unit_kind,
            last_id=upper_id,
            rows_processed=upper_id,
            binding_position=0,
        )
        repository.mark_child_terminal(
            census_id=census_id,
            fingerprint="fingerprint",
            fence_token=fence_token,
            unit_kind=unit_kind,
            terminal_state=ParentState.COMPLETED,
            reason="complete",
        )

    repository.finalize(
        census_id=census_id,
        fingerprint="fingerprint",
        terminal_state=ParentState.COMPLETED,
        reason="complete",
        allow_paused=False,
    )

    status = repository.status(census_id)
    assert status is not None
    assert status["state"] == "completed"
    assert status["terminal_rows_processed"] == 13


def test_mapping_only_zero_call_window_and_cancellation(
    census_client: tuple[Driver, Neo4jClient],
) -> None:
    _driver, client = census_client
    repository = StandaloneCrmCensusRepository(client)
    census_id, _created = repository.admit(
        source_instance_id="bitrix-primary",
        control_instance_id="legacy-default",
        census_kind=CensusKind.MAPPING_PREPARE,
        occurrence_key="mapping-occurrence",
        fingerprint="mapping-fingerprint",
        request_json="{}",
        budget_json="{}",
        heads_json="{}",
        occurrence_deadline=datetime.now(UTC) + timedelta(hours=1),
        occurrence_calls=100,
        occurrence_rows=100,
        attempt_calls=10,
        attempt_rows=10,
        attempt_runtime_seconds=60.0,
        max_attempts=3,
    )
    repository.claim_attempt(
        census_id=census_id,
        fingerprint="mapping-fingerprint",
        attempt_deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    repository.commit_no_source_window(census_id=census_id, fingerprint="mapping-fingerprint")
    assert (
        repository.cancel(census_id=census_id, fingerprint="mapping-fingerprint", actor="operator")
        is ParentState.CANCEL_REQUESTED
    )
    repository.finalize(
        census_id=census_id,
        fingerprint="mapping-fingerprint",
        terminal_state=ParentState.CANCELLED_WITH_CHECKPOINT,
        reason="cancelled",
        allow_paused=True,
    )
    status = repository.status(census_id)
    assert status is not None
    assert status["state"] == ParentState.CANCELLED_WITH_CHECKPOINT.value
