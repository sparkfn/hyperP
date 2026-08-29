"""Private disposable-Neo4j fixtures and concurrency helpers for Issue #304 tests."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingExpectedHead,
    CrmTenantMappingManifest,
    CrmTenantMappingScope,
    CrmTenantMappingTarget,
)
from src.crm_tenant_mapping_models import (
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingPrepareCommand,
    CrmTenantMappingRejectCommand,
    CrmTenantMappingRevisionSnapshot,
    mapping_head_id,
)
from src.graph import crm_tenant_mapping as mapping_graph
from src.graph.client import Neo4jClient
from src.graph.queries.standalone_crm_lane_a_contracts import (
    CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS,
)

T = TypeVar("T")
_DIGEST = "sha256:" + "a" * 64
_TEST_ENTITY_PREFIX = "issue-304-"
_LABELS = [
    "CrmTenantMappingScopeCounter",
    "CrmTenantMappingRevision",
    "CrmTenantMappingEntry",
    "CrmTenantMappingTarget",
    "CrmTenantMappingActiveHead",
]
_MAPPING_CONSTRAINTS = tuple(
    statement
    for statement in CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS
    if "crm_tenant_mapping_" in statement
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


def _neo4j_driver() -> Iterator[Driver]:
    uri = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_URI")
    password = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_PASSWORD")
    if uri is None or password is None:
        pytest.skip("disposable standalone CRM Lane A Neo4j database is not configured")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_SERVICE_HOST") == "neo4j":
        allowed_hosts.add("neo4j")
    if urlparse(uri).hostname not in allowed_hosts:
        pytest.fail("mapping Neo4j tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(
        uri, auth=(os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_USER", "neo4j"), password)
    )
    try:
        driver.verify_connectivity()
        _cleanup(driver)
        yield driver
    finally:
        try:
            _cleanup(driver)
        finally:
            driver.close()


def _cleanup(driver: Driver) -> None:
    with driver.session() as session:
        for statement in _MAPPING_CONSTRAINTS:
            session.run(
                f"DROP {'CONSTRAINT' if 'CONSTRAINT' in statement else 'INDEX'} "
                f"{statement.split()[2]} IF EXISTS"
            ).consume()
        session.run(
            "MATCH (node) "
            "WHERE any(label IN labels(node) WHERE label IN $labels) "
            "DETACH DELETE node",
            labels=_LABELS,
        ).consume()
        session.run(
            "MATCH (entity:Entity) "
            "WHERE entity.entity_key STARTS WITH $entity_key_prefix "
            "DETACH DELETE entity",
            entity_key_prefix=_TEST_ENTITY_PREFIX,
        ).consume()


def _scope() -> CrmTenantMappingScope:
    return CrmTenantMappingScope("bitrix_chat", "portal-a", "control-a")


def _command(request_id: str, entity_key: str) -> CrmTenantMappingPrepareCommand:
    scope = _scope()
    manifest = CrmTenantMappingManifest(
        scope, (CrmTenantMappingCompanyEntry("10", (CrmTenantMappingTarget(entity_key),)),)
    )
    return _command_for_manifest(request_id, manifest)


def _command_for_manifest(
    request_id: str,
    manifest: CrmTenantMappingManifest,
    boundary: CrmTenantMappingExpectedHeadBoundary | None = None,
) -> CrmTenantMappingPrepareCommand:
    scope = manifest.scope
    return CrmTenantMappingPrepareCommand(
        scope,
        request_id,
        manifest,
        boundary or CrmTenantMappingExpectedHeadBoundary(scope, mapping_head_id(scope), None),
        CrmTenantMappingAuthorization(
            "reviewer", "approval", _DIGEST, "2026-08-29T00:00:00Z", "2026-08-30T00:00:00Z"
        ),
        "2026-08-29T12:00:00Z",
    )


def _counts(driver: Driver) -> tuple[int, int, int, int]:
    with driver.session() as session:
        record = session.run(
            "OPTIONAL MATCH (counter:CrmTenantMappingScopeCounter) "
            "OPTIONAL MATCH (revision:CrmTenantMappingRevision) "
            "OPTIONAL MATCH (entry:CrmTenantMappingEntry) "
            "OPTIONAL MATCH (target:CrmTenantMappingTarget) "
            "RETURN coalesce(max(counter.next_revision_number), 0) AS counter, "
            "count(DISTINCT revision) AS revisions, count(DISTINCT entry) AS entries, "
            "count(DISTINCT target) AS targets"
        ).single(strict=True)
    return record["counter"], record["revisions"], record["entries"], record["targets"]


def _mark_active(driver: Driver, snapshot: CrmTenantMappingRevisionSnapshot) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.state = 'active'",
            revision_id=snapshot.revision.revision_id,
        ).consume()


def _set_active_head(driver: Driver, snapshot: CrmTenantMappingRevisionSnapshot) -> None:
    scope = snapshot.revision.scope
    with driver.session() as session:
        session.run(
            "MERGE (head:CrmTenantMappingActiveHead {source_key: $source_key, "
            "source_instance_id: $source_instance_id, control_instance_id: $control_instance_id}) "
            "SET head.head_id = $head_id, head.active_revision_id = $revision_id, "
            "head.active_revision_number = $revision_number, "
            "head.active_manifest_digest = $manifest_digest, "
            "head.effective_at = '2026-08-29T12:00:00Z'",
            source_key=scope.source_key,
            source_instance_id=scope.source_instance_id,
            control_instance_id=scope.control_instance_id,
            head_id=mapping_head_id(scope),
            revision_id=snapshot.revision.revision_id,
            revision_number=snapshot.revision.revision_number,
            manifest_digest=snapshot.revision.manifest_digest,
        ).consume()


def _head_boundary(
    snapshot: CrmTenantMappingRevisionSnapshot,
) -> CrmTenantMappingExpectedHeadBoundary:
    scope = snapshot.revision.scope
    return CrmTenantMappingExpectedHeadBoundary(
        scope,
        mapping_head_id(scope),
        CrmTenantMappingExpectedHead(
            mapping_head_id(scope),
            snapshot.revision.revision_id,
            snapshot.revision.revision_number,
            snapshot.revision.manifest_digest,
        ),
    )


def _repository(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> mapping_graph.Neo4jCrmTenantMappingRepository:
    monkeypatch.setattr(mapping_graph, "assert_standalone_crm_lane_a_ready", lambda _client: None)
    with driver.session() as session:
        for statement in _MAPPING_CONSTRAINTS:
            session.run(statement).consume()
    return mapping_graph.Neo4jCrmTenantMappingRepository(cast(Neo4jClient, _Client(driver)))


def _concurrent_prepare(
    repository: mapping_graph.Neo4jCrmTenantMappingRepository,
    commands: tuple[CrmTenantMappingPrepareCommand, CrmTenantMappingPrepareCommand],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, object]:
    original = mapping_graph._validate_entities
    barrier = threading.Barrier(2)
    counter_lock = threading.Lock()
    calls = 0

    def synchronized_validation(tx: ManagedTransaction, entity_keys: tuple[str, ...]) -> None:
        nonlocal calls
        original(tx, entity_keys)
        with counter_lock:
            calls += 1
            should_wait = calls <= 2
        if should_wait:
            barrier.wait(timeout=10)

    monkeypatch.setattr(mapping_graph, "_validate_entities", synchronized_validation)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(executor.submit(repository.prepare, command) for command in commands)
        results: list[object] = []
        for future in futures:
            try:
                results.append(future.result(timeout=30))
            except Exception as exc:
                results.append(exc)
    return results[0], results[1]


def _concurrent_reject(
    repository: mapping_graph.Neo4jCrmTenantMappingRepository,
    commands: tuple[CrmTenantMappingRejectCommand, CrmTenantMappingRejectCommand],
) -> tuple[object, object]:
    barrier = threading.Barrier(2)

    def reject(command: CrmTenantMappingRejectCommand) -> CrmTenantMappingRevisionSnapshot:
        barrier.wait(timeout=10)
        return repository.reject(command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(executor.submit(reject, command) for command in commands)
        results: list[object] = []
        for future in futures:
            try:
                results.append(future.result(timeout=30))
            except Exception as exc:
                results.append(exc)
    return results[0], results[1]
