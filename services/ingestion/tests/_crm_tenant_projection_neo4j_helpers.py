"""Fixtures and seed helpers for Issue #305 disposable Neo4j coverage."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingManifest,
    CrmTenantMappingTarget,
)
from src.crm_tenant_mapping_identity import mapping_head_id
from src.crm_tenant_mapping_models import (
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingPrepareCommand,
)
from src.crm_tenant_projection_models import (
    CrmTenantProjectionMaterializationCommand,
    CrmTenantProjectionReleaseSummary,
)
from src.crm_tenant_projection_records import CrmTenantProjectionScope
from src.crm_tenant_projection_records import _digest as _projection_digest
from src.graph import crm_tenant_projection as projection_graph
from src.graph.client import Neo4jClient
from src.graph.crm_tenant_mapping_write import _persistence_components, _revision_properties
from src.graph.queries.standalone_crm_lane_a_contracts import (
    CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS,
)
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
    canonical_request_payload,
)

T = TypeVar("T")
_DIGEST = "sha256:" + "a" * 64
_AVAILABLE_AT = "2026-08-29T00:00:00Z"
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


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    uri = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_URI")
    password = os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_PASSWORD")
    if uri is None or password is None:
        pytest.skip("disposable standalone CRM Lane A Neo4j database is not configured")
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_SERVICE_HOST") == "neo4j":
        allowed_hosts.add("neo4j")
    if urlparse(uri).hostname not in allowed_hosts:
        pytest.fail("projection Neo4j tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_USER", "neo4j"), password),
    )
    try:
        driver.verify_connectivity()
        _reset(driver)
        with driver.session() as session:
            for statement in _CONSTRAINTS:
                session.run(statement).consume()
        yield driver
    finally:
        _reset(driver)
        driver.close()


def _reset(driver: Driver) -> None:
    with driver.session() as session:
        for statement in _CONSTRAINTS:
            name = statement.split()[2]
            kind = "CONSTRAINT" if "CONSTRAINT" in statement else "INDEX"
            session.run(f"DROP {kind} {name} IF EXISTS").consume()
        session.run(
            "MATCH (node) WHERE any(label IN labels(node) WHERE label IN $labels) "
            "DETACH DELETE node",
            labels=_LABELS,
        ).consume()
        session.run(
            "MATCH (entity:Entity) WHERE entity.entity_key STARTS WITH 'issue-305-' "
            "DETACH DELETE entity"
        ).consume()


def _scope() -> CrmTenantProjectionScope:
    return CrmTenantProjectionScope("bitrix_chat", "issue-305-portal", "issue-305-control")


def _request_json() -> str:
    scope = _scope()
    return canonical_request_payload(
        SourceSyncCensusRequest(
            scope.source_key,
            scope.source_instance_id,
            scope.control_instance_id,
            "issue-305-occurrence",
            ("contact", "lead", "company"),
            StandaloneCrmBudget(2, 10, 60, 2, 10, 1, "2026-08-30T00:00:00Z"),
            "policy-a",
            "association-a",
            "configuration-a",
            SourceSyncAuthority(
                "mapping-head", "mapping-digest", "projection-head", "projection-digest"
            ),
        )
    )


def _mapping_manifest(
    entries: tuple[CrmTenantMappingCompanyEntry, ...] | None = None,
) -> CrmTenantMappingManifest:
    return CrmTenantMappingManifest(
        _scope().mapping_scope,
        entries
        if entries is not None
        else (CrmTenantMappingCompanyEntry("303", (CrmTenantMappingTarget("issue-305-entity"),)),),
    )


def _observation_id(
    snapshot_id: str,
    company_id: str,
    sort: int | None,
    role_id: str | None,
    is_primary: bool,
) -> str:
    return _projection_digest(
        "crm-company-membership-observation-v1",
        [snapshot_id, company_id, sort, role_id, is_primary],
    )


def _mapping_properties(
    manifest: CrmTenantMappingManifest | None = None,
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    scope = _scope()
    effective_manifest = _mapping_manifest() if manifest is None else manifest
    command = CrmTenantMappingPrepareCommand(
        scope.mapping_scope,
        "issue-305-mapping-prepare",
        effective_manifest,
        CrmTenantMappingExpectedHeadBoundary(
            scope.mapping_scope, mapping_head_id(scope.mapping_scope), None
        ),
        CrmTenantMappingAuthorization(
            "issue-305-reviewer",
            "issue-305-approval",
            _DIGEST,
            _AVAILABLE_AT,
            "2026-08-30T00:00:00Z",
        ),
        _AVAILABLE_AT,
    )
    entries, targets = _persistence_components("issue-305-mapping", effective_manifest)
    return (
        _revision_properties(command, effective_manifest, "issue-305-mapping", 1, None),
        entries,
        targets,
    )


def _seed(driver: Driver, manifest: CrmTenantMappingManifest | None = None) -> None:
    scope = _scope()
    mapping_properties, entries, targets = _mapping_properties(manifest)
    with driver.session() as session:
        session.run(
            """
            CREATE (census:StandaloneCrmCensus {census_id: 'issue-305-census',
              source_key: $source_key, source_instance_id: $source_instance_id,
              control_instance_id: $control_instance_id, census_kind: 'source_sync',
              status: 'completed', fingerprint: $digest, request_json: $request_json,
              expected_units: 3, completed_units: 2, failed_units: 0, cancelled_units: 0,
              no_work_units: 1, processed_rows: 2, skipped_rows: 0,
              created_at: datetime($available_at)})
            CREATE (:StandaloneCrmCensusUnit {census_id: 'issue-305-census', stream_kind: 'contact',
              state: 'completed', generation: 1, frozen_upper_id: 101})
            CREATE (:StandaloneCrmCensusUnit {census_id: 'issue-305-census', stream_kind: 'lead',
              state: 'completed', generation: 1, frozen_upper_id: 102})
            CREATE (:StandaloneCrmCensusUnit {census_id: 'issue-305-census', stream_kind: 'company',
              state: 'no_work', generation: 1, frozen_upper_id: 0})
            CREATE (:StandaloneCrmCensusCheckpoint {
              census_id: 'issue-305-census', stream_kind: 'contact',
              generation: 1, frozen_upper_id: 101, last_committed_id: 101,
              processed_rows: 1, skipped_rows: 0})
            CREATE (:StandaloneCrmCensusCheckpoint {
              census_id: 'issue-305-census', stream_kind: 'lead',
              generation: 1, frozen_upper_id: 102, last_committed_id: 102,
              processed_rows: 1, skipped_rows: 0})
            CREATE (revision:CrmTenantMappingRevision $mapping_properties)
            CREATE (entity:Entity {entity_key: 'issue-305-entity'})
            WITH revision, entity
            UNWIND $entries AS item
            CREATE (entry:CrmTenantMappingEntry {revision_id: revision.revision_id,
              entry_id: item.entry_id, company_id: item.company_id})
            CREATE (revision)-[:HAS_MAPPING_ENTRY]->(entry)
            WITH entity
            UNWIND $targets AS item
            MATCH (entry:CrmTenantMappingEntry {entry_id: item.entry_id})
            CREATE (target:CrmTenantMappingTarget {entry_id: item.entry_id,
              target_id: item.target_id, entity_key: item.entity_key,
              relationship_kind: item.relationship_kind})
            CREATE (entry)-[:HAS_MAPPING_TARGET]->(target)-[:TARGETS_ENTITY]->(entity)
            """,
            source_key=scope.source_key,
            source_instance_id=scope.source_instance_id,
            control_instance_id=scope.control_instance_id,
            request_json=_request_json(),
            available_at=_AVAILABLE_AT,
            digest=_DIGEST,
            mapping_properties=mapping_properties,
            entries=entries,
            targets=targets,
        ).consume()
        _seed_snapshot(session, "contact", "101", "issue-305-contact", True)
        _seed_snapshot(session, "lead", "102", "issue-305-lead", False)


def _seed_snapshot(
    session: Session,
    subject_kind: str,
    subject_id: str,
    prefix: str,
    with_company: bool,
) -> None:
    scope = _scope()
    session.run(
        """
        CREATE (head:CrmCompanyMembershipHead {source_instance_id: $source_instance_id,
          control_instance_id: $control_instance_id, subject_kind: $subject_kind,
          subject_id: $subject_id, selected_snapshot_id: $snapshot_id,
          available_at: datetime($available_at), source_record_version: 1, source_record_pk: 1})
        CREATE (snapshot:CrmCompanyMembershipSnapshot {snapshot_id: $snapshot_id,
          snapshot_digest: $digest, source_instance_id: $source_instance_id,
          control_instance_id: $control_instance_id, subject_kind: $subject_kind,
          subject_id: $subject_id, source_record_version: 1, source_record_pk: 1,
          available_at: datetime($available_at), binding_count: 0})
        CREATE (head)-[:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot)
        """,
        source_instance_id=scope.source_instance_id,
        control_instance_id=scope.control_instance_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
        snapshot_id=f"{prefix}-snapshot",
        digest=_DIGEST,
        available_at=_AVAILABLE_AT,
    ).consume()
    if with_company:
        _add_membership_observation(
            session,
            f"{prefix}-snapshot",
            subject_kind,
            subject_id,
            "303",
            True,
        )


def _add_membership_observation(
    session: Session,
    snapshot_id: str,
    subject_kind: str,
    subject_id: str,
    company_id: str,
    is_primary: bool,
) -> None:
    scope = _scope()
    session.run(
        """
        MATCH (snapshot:CrmCompanyMembershipSnapshot {snapshot_id: $snapshot_id})
        CREATE (reference:CrmCompanyReference {source_key: $source_key,
          source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
          company_id: $company_id})
        CREATE (observation:CrmCompanyMembershipObservation {snapshot_id: $snapshot_id,
          company_id: $company_id, observation_id: $observation_id, subject_kind: $subject_kind,
          subject_id: $subject_id, is_primary: $is_primary})
        CREATE (snapshot)-[:HAS_MEMBERSHIP_OBSERVATION]->(observation)
        CREATE (observation)-[:REFERENCES_COMPANY]->(reference)
        SET snapshot.binding_count = snapshot.binding_count + 1
        """,
        snapshot_id=snapshot_id,
        source_key=scope.source_key,
        source_instance_id=scope.source_instance_id,
        control_instance_id=scope.control_instance_id,
        company_id=company_id,
        observation_id=_observation_id(snapshot_id, company_id, None, None, is_primary),
        subject_kind=subject_kind,
        subject_id=subject_id,
        is_primary=is_primary,
    ).consume()


def _command(
    request_id: str = "issue-305-request",
    manifest: CrmTenantMappingManifest | None = None,
) -> CrmTenantProjectionMaterializationCommand:
    scope = _scope()
    return CrmTenantProjectionMaterializationCommand(
        scope,
        request_id,
        "issue-305-census",
        _DIGEST,
        "issue-305-mapping",
        (_mapping_manifest() if manifest is None else manifest).digest,
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
    driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
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
