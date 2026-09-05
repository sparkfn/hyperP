"""Driver, repository, and command helpers for Issue #305 disposable Neo4j coverage."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from _crm_tenant_projection_neo4j_seed import (
    _DIGEST,
    _mapping_manifest,
    _mapping_revision_id,
    _scope,
)
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.crm_tenant_mapping_contracts import CrmTenantMappingManifest
from src.crm_tenant_mapping_identity import mapping_head_id
from src.crm_tenant_mapping_models import CrmTenantMappingExpectedHeadBoundary
from src.crm_tenant_projection_models import (
    CrmTenantProjectionMaterializationCommand,
    CrmTenantProjectionReleaseSummary,
)
from src.graph import crm_tenant_projection as projection_graph
from src.graph.client import Neo4jClient
from src.graph.queries.standalone_crm_census import (
    CREATE_STANDALONE_CRM_CENSUS_CONSTRAINTS,
)
from src.graph.queries.standalone_crm_lane_a_contracts import (
    CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS,
)

T = TypeVar("T")
_LABELS = (
    "StandaloneCrmCensus",
    "StandaloneCrmCensusUnit",
    "StandaloneCrmCensusCheckpoint",
    "StandaloneCrmCensusFence",
    "StandaloneCrmChildPublication",
    "CrmCompanyMembershipHead",
    "CrmCompanyMembershipSnapshot",
    "CrmCompanyMembershipObservation",
    "CrmCompanyReference",
    "CrmTenantMappingScopeCounter",
    "CrmTenantMappingRevision",
    "CrmTenantMappingEntry",
    "CrmTenantMappingTarget",
    "CrmTenantMappingActiveHead",
    "CrmTenantProjectionScopeCounter",
    "CrmTenantProjectionRelease",
    "CrmTenantProjectionInput",
    "CrmTenantProjectionDecision",
    "CrmTenantProjectionAssociation",
    "CrmTenantProjectionSupport",
    "CrmTenantProjectionActiveHead",
)
_CONSTRAINTS = tuple(
    statement
    for statement in CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS
    if "crm_tenant_" in statement or "crm_company_membership_" in statement
) + tuple(
    statement
    for statement in CREATE_STANDALONE_CRM_CENSUS_CONSTRAINTS
    if "standalone_crm_checkpoint_unique" in statement
)


@dataclass(frozen=True)
class _MappingActiveHeadDriftParameters:
    source_key: str
    source_instance_id: str
    control_instance_id: str
    head_id: str
    active_revision_id: str
    active_revision_number: int
    active_manifest_digest: str


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


_module_driver: Driver | None = None


def _open_schema_driver() -> Driver:
    uri = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_URI")
    password = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_PASSWORD")
    if uri is None or password is None:
        pytest.skip("disposable standalone CRM Lane A Neo4j database is not configured")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    service_host = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_SERVICE_HOST")
    if service_host:
        allowed_hosts.add(service_host)
    if urlparse(uri).hostname not in allowed_hosts:
        pytest.fail("projection Neo4j tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(
        uri, auth=(os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_USER", "neo4j"), password)
    )
    try:
        driver.verify_connectivity()
        _reset(driver)
        _create_schema(driver)
    except BaseException:
        with suppress(Exception):
            _reset(driver)
        driver.close()
        raise
    return driver


def _close_schema_driver() -> None:
    global _module_driver
    driver = _module_driver
    _module_driver = None
    if driver is None:
        return
    try:
        _reset(driver)
    finally:
        driver.close()


def _module_schema_driver(request: pytest.FixtureRequest) -> Driver:
    global _module_driver
    if _module_driver is None:
        _module_driver = _open_schema_driver()
        try:
            request.node.parent.addfinalizer(_close_schema_driver)
        except BaseException:
            _close_schema_driver()
            raise
    return _module_driver


@pytest.fixture
def neo4j_driver(request: pytest.FixtureRequest) -> Iterator[Driver]:
    driver = _module_schema_driver(request)
    _clear_data(driver)
    try:
        yield driver
    finally:
        try:
            _clear_data(driver)
        finally:
            _restore_schema(driver)


def _reset(driver: Driver) -> None:
    _drop_schema(driver)
    _clear_data(driver)


def _create_schema(driver: Driver) -> None:
    with driver.session() as session:
        for statement in _CONSTRAINTS:
            session.run(statement).consume()


def _restore_schema(driver: Driver) -> None:
    with driver.session() as session:
        existing_names = set(session.run("SHOW CONSTRAINTS YIELD name RETURN name").value("name"))
        existing_names.update(session.run("SHOW INDEXES YIELD name RETURN name").value("name"))
        for statement in _CONSTRAINTS:
            if statement.split()[2] not in existing_names:
                session.run(statement).consume()


def _drop_schema(driver: Driver) -> None:
    with driver.session() as session:
        for statement in _CONSTRAINTS:
            name = statement.split()[2]
            kind = "CONSTRAINT" if "CONSTRAINT" in statement else "INDEX"
            session.run(f"DROP {kind} {name} IF EXISTS").consume()


def _clear_data(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (node) WHERE any(label IN labels(node) WHERE label IN $labels) "
            "DETACH DELETE node",
            labels=_LABELS,
        ).consume()
        session.run(
            "MATCH (entity:Entity) WHERE entity.entity_key STARTS WITH $prefix "
            "DETACH DELETE entity",
            prefix="issue-305-",
        ).consume()


def _command(
    request_id: str = "issue-305-request",
    manifest: CrmTenantMappingManifest | None = None,
) -> CrmTenantProjectionMaterializationCommand:
    scope = _scope()
    effective_manifest = _mapping_manifest() if manifest is None else manifest
    return CrmTenantProjectionMaterializationCommand(
        scope,
        request_id,
        "issue-305-census",
        _DIGEST,
        _mapping_revision_id(),
        effective_manifest.digest,
        CrmTenantMappingExpectedHeadBoundary(
            scope.mapping_scope, mapping_head_id(scope.mapping_scope), None
        ),
        None,
        1,
    )


def _mapping_active_head_drift_parameters() -> _MappingActiveHeadDriftParameters:
    scope = _scope()
    return _MappingActiveHeadDriftParameters(
        scope.source_key,
        scope.source_instance_id,
        scope.control_instance_id,
        _command().expected_mapping_head_id,
        "other",
        1,
        _DIGEST,
    )


def _repository(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> projection_graph.Neo4jCrmTenantProjectionRepository:
    monkeypatch.setattr(
        projection_graph, "assert_standalone_crm_lane_a_ready", lambda _client: None
    )
    return projection_graph.Neo4jCrmTenantProjectionRepository(cast(Neo4jClient, _Client(driver)))


def _drive_to_projection_complete(
    repository: projection_graph.Neo4jCrmTenantProjectionRepository,
    command: CrmTenantProjectionMaterializationCommand,
) -> CrmTenantProjectionReleaseSummary:
    release = repository.allocate_or_replay(command)
    while release.phase == "capture":
        release = repository.capture_page(release.release_id, release.release_fingerprint, 1)
    while release.phase == "projection":
        release = repository.project_page(release.release_id, release.release_fingerprint, 1)
    return release
