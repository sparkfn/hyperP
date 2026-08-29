"""Disposable real-Neo4j fingerprint, ownership, and freshness-race tests for Issue #304."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TypeVar, cast

import pytest
from _crm_tenant_mapping_neo4j_helpers import (
    _DIGEST,
    _Client,
    _command,
    _command_for_manifest,
    _head_boundary,
    _mark_active,
    _neo4j_driver,
    _repository,
    _scope,
    _set_active_head,
)
from neo4j import Driver, ManagedTransaction
from src.crm_tenant_mapping_contracts import CrmTenantMappingAuthorization, CrmTenantMappingScope
from src.crm_tenant_mapping_models import (
    CrmTenantMappingConflictError,
    CrmTenantMappingIntegrityError,
    CrmTenantMappingRejectCommand,
    CrmTenantMappingRejection,
    CrmTenantMappingRevisionSnapshot,
    CrmTenantMappingRollbackCommand,
)
from src.graph import crm_tenant_mapping as mapping_graph
from src.graph import crm_tenant_mapping_freshness as mapping_freshness
from src.graph.client import Neo4jClient
from src.standalone_crm_census_requests import MappingPrepareAuthority, SourceSyncAuthority

T = TypeVar("T")


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    yield from _neo4j_driver()


def _authorization() -> CrmTenantMappingAuthorization:
    return CrmTenantMappingAuthorization(
        "reviewer", "approval", _DIGEST, "2026-08-29T00:00:00Z", "2026-08-30T00:00:00Z"
    )


def test_strict_reads_recompute_prepare_rollback_and_rejection_fingerprints(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
    prepared = repository.prepare(_command("fingerprint-prepare", "issue-304-entity-a"))
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.request_fingerprint = $fingerprint",
            revision_id=prepared.revision.revision_id,
            fingerprint="sha256:" + "b" * 64,
        ).consume()
    with pytest.raises(CrmTenantMappingIntegrityError, match="preparation fingerprint"):
        repository.get_revision(
            _scope(), prepared.revision.revision_id, prepared.revision.manifest_digest
        )
    with pytest.raises(CrmTenantMappingIntegrityError, match="preparation fingerprint"):
        repository.prepare(_command("fingerprint-prepare", "issue-304-entity-a"))

    rejected = repository.prepare(_command("fingerprint-reject", "issue-304-entity-a"))
    rejection = CrmTenantMappingRejectCommand(
        _scope(),
        rejected.revision.revision_id,
        rejected.revision.manifest_digest,
        CrmTenantMappingRejection("reviewer", "case", "bad target"),
        _authorization(),
        "2026-08-29T12:00:00Z",
    )
    repository.reject(rejection)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.rejection_request_fingerprint = $fingerprint",
            revision_id=rejected.revision.revision_id,
            fingerprint="sha256:" + "d" * 64,
        ).consume()
    with pytest.raises(CrmTenantMappingIntegrityError, match="rejection fingerprint"):
        repository.reject(rejection)

    history = repository.prepare(_command("fingerprint-history", "issue-304-entity-a"))
    current = repository.prepare(_command("fingerprint-current", "issue-304-entity-a"))
    _mark_active(neo4j_driver, history)
    _mark_active(neo4j_driver, current)
    _set_active_head(neo4j_driver, current)
    rollback = repository.rollback(
        CrmTenantMappingRollbackCommand(
            _scope(),
            "fingerprint-rollback",
            history.revision.revision_id,
            history.revision.manifest_digest,
            _head_boundary(current),
            _authorization(),
            "2026-08-29T12:00:00Z",
        )
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.request_fingerprint = $fingerprint",
            revision_id=rollback.revision.revision_id,
            fingerprint="sha256:" + "c" * 64,
        ).consume()
    with pytest.raises(CrmTenantMappingIntegrityError, match="preparation fingerprint"):
        repository.get_revision(
            _scope(), rollback.revision.revision_id, rollback.revision.manifest_digest
        )


def test_foreign_entry_and_target_owners_fail_closed(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
    entry_snapshot = repository.prepare(_command("foreign-entry", "issue-304-entity-a"))
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (:CrmTenantMappingRevision {revision_id: $revision_id})"
            "-[:HAS_MAPPING_ENTRY]->(entry:CrmTenantMappingEntry) "
            "CREATE (:ForeignMappingOwner)-[:HAS_MAPPING_ENTRY]->(entry)",
            revision_id=entry_snapshot.revision.revision_id,
        ).consume()
    with pytest.raises(CrmTenantMappingIntegrityError, match="unexpected topology"):
        repository.get_revision(
            _scope(), entry_snapshot.revision.revision_id, entry_snapshot.revision.manifest_digest
        )

    target_snapshot = repository.prepare(_command("foreign-target", "issue-304-entity-a"))
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (:CrmTenantMappingRevision {revision_id: $revision_id})"
            "-[:HAS_MAPPING_ENTRY]->(:CrmTenantMappingEntry)"
            "-[:HAS_MAPPING_TARGET]->(target:CrmTenantMappingTarget) "
            "CREATE (:ForeignMappingOwner)-[:HAS_MAPPING_TARGET]->(target)",
            revision_id=target_snapshot.revision.revision_id,
        ).consume()
    with pytest.raises(CrmTenantMappingIntegrityError, match="unexpected topology"):
        repository.get_revision(
            _scope(), target_snapshot.revision.revision_id, target_snapshot.revision.manifest_digest
        )


def test_source_sync_final_statement_rejects_interleaved_head_swap(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
    current = repository.prepare(_command("atomic-source-current", "issue-304-entity-a"))
    replacement = repository.prepare(_command("atomic-source-replacement", "issue-304-entity-a"))
    _mark_active(neo4j_driver, current)
    _mark_active(neo4j_driver, replacement)
    _set_active_head(neo4j_driver, current)
    client = _CountingClient(neo4j_driver)
    atomic_repository = mapping_graph.Neo4jCrmTenantMappingRepository(cast(Neo4jClient, client))
    monkeypatch.setattr(mapping_graph, "assert_standalone_crm_lane_a_ready", lambda _client: None)
    original = mapping_freshness._validate_source_sync_at_linearization

    def interleave(
        tx: ManagedTransaction, scope: CrmTenantMappingScope, authority: SourceSyncAuthority
    ) -> None:
        _set_active_head(neo4j_driver, replacement)
        original(tx, scope, authority)

    monkeypatch.setattr(mapping_freshness, "_validate_source_sync_at_linearization", interleave)
    with pytest.raises(CrmTenantMappingConflictError):
        atomic_repository.validate_source_sync(
            _scope(),
            SourceSyncAuthority(
                _head_boundary(current).head_id,
                current.revision.manifest_digest,
                "projection-head",
                "projection-digest",
            ),
        )
    assert client.read_calls == 1


def test_prepare_final_statement_rejects_interleaved_prepared_state_transition(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
    current = repository.prepare(_command("atomic-current", "issue-304-entity-a"))
    _mark_active(neo4j_driver, current)
    _set_active_head(neo4j_driver, current)
    prepared = repository.prepare(
        _command_for_manifest("atomic-prepared", current.manifest, _head_boundary(current))
    )
    client = _CountingClient(neo4j_driver)
    atomic_repository = mapping_graph.Neo4jCrmTenantMappingRepository(cast(Neo4jClient, client))
    monkeypatch.setattr(mapping_graph, "assert_standalone_crm_lane_a_ready", lambda _client: None)
    original = mapping_freshness._validate_prepare_at_linearization

    def interleave(
        tx: ManagedTransaction,
        snapshot: CrmTenantMappingRevisionSnapshot,
    ) -> None:
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
                "SET revision.state = 'activation_failed'",
                revision_id=snapshot.revision.revision_id,
            ).consume()
        original(tx, snapshot)

    monkeypatch.setattr(mapping_freshness, "_validate_prepare_at_linearization", interleave)
    with pytest.raises(CrmTenantMappingConflictError):
        atomic_repository.validate_mapping_prepare(
            _scope(),
            MappingPrepareAuthority(
                prepared.revision.revision_id,
                prepared.revision.manifest_digest,
                _head_boundary(current).head_id,
            ),
        )
    assert client.read_calls == 1


class _CountingClient:
    def __init__(self, driver: Driver) -> None:
        self._delegate = _Client(driver)
        self.read_calls = 0

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        self.read_calls += 1
        return self._delegate.execute_read(work)
