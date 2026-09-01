"""Disposable real-Neo4j coverage for the #307 atomic activation CAS."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar, cast
from urllib.parse import urlparse

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.crm_tenant_activation_contracts import (
    CrmTenantActivationCandidate,
    CrmTenantActivationCommand,
    CrmTenantActivationRelease,
)
from src.crm_tenant_activation_models import CrmTenantActivationConflictError
from src.crm_tenant_mapping_contracts import CrmTenantMappingScope
from src.crm_tenant_mapping_identity import mapping_head_id
from src.crm_tenant_projection_records import CrmTenantProjectionScope
from src.graph import crm_tenant_activation as activation_graph
from src.graph.client import Neo4jClient

T = TypeVar("T")
_SCOPE = CrmTenantProjectionScope("bitrix_chat", "issue-307-portal", "issue-307-control")
_MAPPING_DIGEST = "sha256:" + "a" * 64
_RELEASE_FINGERPRINT = "sha256:" + "b" * 64


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
        pytest.fail("activation tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(
        uri, auth=(os.getenv("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_USER", "neo4j"), password)
    )
    try:
        driver.verify_connectivity()
        _reset(driver)
        yield driver
    finally:
        _reset(driver)
        driver.close()


def _reset(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (node) WHERE node.source_instance_id = $source_instance_id "
            "AND any(label IN labels(node) WHERE label IN $labels) DETACH DELETE node",
            source_instance_id=_SCOPE.source_instance_id,
            labels=[
                "CrmTenantMappingScopeCounter",
                "CrmTenantMappingRevision",
                "CrmTenantMappingActiveHead",
                "CrmTenantProjectionScopeCounter",
                "CrmTenantProjectionRelease",
                "CrmTenantProjectionActiveHead",
            ],
        ).consume()


def _repository(
    driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> activation_graph.Neo4jCrmTenantActivationRepository:
    monkeypatch.setattr(
        activation_graph, "assert_standalone_crm_lane_a_ready", lambda _client: None
    )
    return activation_graph.Neo4jCrmTenantActivationRepository(cast(Neo4jClient, _Client(driver)))


def _command() -> CrmTenantActivationCommand:
    return CrmTenantActivationCommand(
        CrmTenantMappingScope(
            *(_SCOPE.source_key, _SCOPE.source_instance_id, _SCOPE.control_instance_id)
        ),
        _SCOPE,
        CrmTenantActivationCandidate("issue-307-revision", _MAPPING_DIGEST),
        CrmTenantActivationRelease("issue-307-release", _RELEASE_FINGERPRINT),
        None,
        None,
        "issue-307-census",
        1,
        "issue-307-task",
    )


def _seed_generation_zero(driver: Driver) -> None:
    with driver.session() as session:
        session.run(
            "CREATE (:CrmTenantMappingRevision {source_key: $source_key, "
            "source_instance_id: $source_instance_id, control_instance_id: $control_instance_id, "
            "revision_id: $revision_id, manifest_digest: $manifest_digest, revision_number: 1, "
            "state: 'prepared', expected_head_id: $mapping_head_id, expected_head_present: false}) "
            "CREATE (:CrmTenantProjectionRelease {source_key: $source_key, "
            "source_instance_id: $source_instance_id, control_instance_id: $control_instance_id, "
            "release_id: $release_id, release_fingerprint: $release_fingerprint, "
            "release_number: 1, "
            "mapping_revision_id: $revision_id, mapping_manifest_digest: $manifest_digest, "
            "state: 'completed', expected_prior_head_present: false})",
            source_key=_SCOPE.source_key,
            source_instance_id=_SCOPE.source_instance_id,
            control_instance_id=_SCOPE.control_instance_id,
            revision_id="issue-307-revision",
            manifest_digest=_MAPPING_DIGEST,
            release_id="issue-307-release",
            release_fingerprint=_RELEASE_FINGERPRINT,
            mapping_head_id=mapping_head_id(_SCOPE.mapping_scope),
        ).consume()


def test_real_neo4j_generation_zero_activation_is_atomic_and_exactly_replayable(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_generation_zero(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    first = repository.activate(_command())
    replay = repository.activate(_command())
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.receipt == first.receipt
    with neo4j_driver.session() as session:
        record = session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "MATCH (release:CrmTenantProjectionRelease {release_id: $release_id}) "
            "MATCH (mapping_head:CrmTenantMappingActiveHead) "
            "WHERE mapping_head.source_instance_id = $source_instance_id "
            "MATCH (projection_head:CrmTenantProjectionActiveHead) "
            "WHERE projection_head.source_instance_id = $source_instance_id "
            "RETURN revision.state AS revision_state, release.state AS release_state, "
            "mapping_head.effective_at AS mapping_at, "
            "projection_head.effective_at AS projection_at, "
            "release.activation_activated_at AS release_at",
            revision_id="issue-307-revision",
            release_id="issue-307-release",
            source_instance_id=_SCOPE.source_instance_id,
        ).single()
    assert record is not None
    assert record["revision_state"] == "active"
    assert record["release_state"] == "published"
    assert record["mapping_at"] == record["projection_at"] == record["release_at"]


def test_real_neo4j_stale_candidate_boundary_cannot_partially_publish(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_generation_zero(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision) SET revision.expected_head_present = true"
        ).consume()
    with pytest.raises(CrmTenantActivationConflictError):
        _repository(neo4j_driver, monkeypatch).activate(_command())
    with neo4j_driver.session() as session:
        record = session.run(
            "MATCH (revision:CrmTenantMappingRevision) MATCH (release:CrmTenantProjectionRelease) "
            "RETURN revision.state AS revision_state, release.state AS release_state"
        ).single()
    assert record is not None
    assert record["revision_state"] == "prepared"
    assert record["release_state"] == "completed"


def test_real_neo4j_replacement_requires_full_predecessor_fixture(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_generation_zero(neo4j_driver)
    first = _repository(neo4j_driver, monkeypatch).activate(_command())
    assert first.replayed is False
    with neo4j_driver.session() as session:
        count = session.run(
            "MATCH (:CrmTenantMappingActiveHead) MATCH (:CrmTenantProjectionActiveHead) "
            "RETURN count(*) AS count"
        ).single()
    assert count is not None and count["count"] == 1
