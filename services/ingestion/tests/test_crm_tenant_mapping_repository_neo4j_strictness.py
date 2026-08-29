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
from src.crm_tenant_mapping_contracts import CrmTenantMappingAuthorization
from src.crm_tenant_mapping_models import (
    CrmTenantMappingConflictError,
    CrmTenantMappingIntegrityError,
    CrmTenantMappingRejectCommand,
    CrmTenantMappingRejection,
    CrmTenantMappingRollbackCommand,
)
from src.graph import crm_tenant_mapping as mapping_graph
from src.graph.client import Neo4jClient
from src.standalone_crm_census_requests import (
    MappingPrepareAuthority,
    MappingRollbackAuthority,
    SourceSyncAuthority,
)

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


def test_strict_reads_reject_rollback_provenance_number_identity_corruption(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
    historical = repository.prepare(_command("provenance-history", "issue-304-entity-a"))
    current = repository.prepare(_command("provenance-current", "issue-304-entity-a"))
    _mark_active(neo4j_driver, historical)
    _mark_active(neo4j_driver, current)
    _set_active_head(neo4j_driver, current)
    rollback_command = CrmTenantMappingRollbackCommand(
        _scope(),
        "provenance-rollback",
        historical.revision.revision_id,
        historical.revision.manifest_digest,
        _head_boundary(current),
        _authorization(),
        "2026-08-29T12:00:00Z",
    )
    rollback = repository.rollback(rollback_command)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.rollback_of_revision_number = $revision_number",
            revision_id=rollback.revision.revision_id,
            revision_number=historical.revision.revision_number + 1,
        ).consume()
    with pytest.raises(CrmTenantMappingIntegrityError, match="provenance revision ID"):
        repository.get_revision(
            _scope(), rollback.revision.revision_id, rollback.revision.manifest_digest
        )
    with pytest.raises(CrmTenantMappingIntegrityError, match="provenance revision ID"):
        repository.rollback(rollback_command)


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
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-b'})").consume()
    current = repository.prepare(_command("atomic-source-current", "issue-304-entity-a"))
    replacement = repository.prepare(_command("atomic-source-replacement", "issue-304-entity-b"))
    assert replacement.revision.manifest_digest != current.revision.manifest_digest
    _mark_active(neo4j_driver, current)
    _mark_active(neo4j_driver, replacement)
    _set_active_head(neo4j_driver, current)
    client = _CountingClient(
        neo4j_driver,
        after_first_read=lambda: _set_active_head(neo4j_driver, replacement),
    )
    atomic_repository = mapping_graph.Neo4jCrmTenantMappingRepository(cast(Neo4jClient, client))
    monkeypatch.setattr(mapping_graph, "assert_standalone_crm_lane_a_ready", lambda _client: None)
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
    assert client.read_calls == 2


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

    def transition_prepared() -> None:
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
                "SET revision.state = 'activation_failed'",
                revision_id=prepared.revision.revision_id,
            ).consume()

    client = _CountingClient(neo4j_driver, after_first_read=transition_prepared)
    atomic_repository = mapping_graph.Neo4jCrmTenantMappingRepository(cast(Neo4jClient, client))
    monkeypatch.setattr(mapping_graph, "assert_standalone_crm_lane_a_ready", lambda _client: None)
    with pytest.raises(CrmTenantMappingConflictError):
        atomic_repository.validate_mapping_prepare(
            _scope(),
            MappingPrepareAuthority(
                prepared.revision.revision_id,
                prepared.revision.manifest_digest,
                _head_boundary(current).head_id,
            ),
        )
    assert client.read_calls == 2


def test_rollback_final_statement_rejects_interleaved_candidate_history_and_head_changes(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
    historical = repository.prepare(_command("atomic-rollback-history", "issue-304-entity-a"))
    current = repository.prepare(_command("atomic-rollback-current", "issue-304-entity-a"))
    replacement = repository.prepare(_command("atomic-rollback-replacement", "issue-304-entity-a"))
    _mark_active(neo4j_driver, historical)
    _mark_active(neo4j_driver, current)
    _mark_active(neo4j_driver, replacement)
    _set_active_head(neo4j_driver, current)
    rollback = repository.rollback(
        CrmTenantMappingRollbackCommand(
            _scope(),
            "atomic-rollback-candidate",
            historical.revision.revision_id,
            historical.revision.manifest_digest,
            _head_boundary(current),
            _authorization(),
            "2026-08-29T12:00:00Z",
        )
    )

    def transition_rollback_inputs() -> None:
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (candidate:CrmTenantMappingRevision {revision_id: $candidate_id}) "
                "MATCH (historical:CrmTenantMappingRevision {revision_id: $historical_id}) "
                "SET candidate.state = 'activation_failed', historical.state = 'activation_failed'",
                candidate_id=rollback.revision.revision_id,
                historical_id=historical.revision.revision_id,
            ).consume()
        _set_active_head(neo4j_driver, replacement)

    client = _CountingClient(neo4j_driver, after_first_read=transition_rollback_inputs)
    atomic_repository = mapping_graph.Neo4jCrmTenantMappingRepository(cast(Neo4jClient, client))
    monkeypatch.setattr(mapping_graph, "assert_standalone_crm_lane_a_ready", lambda _client: None)
    with pytest.raises(CrmTenantMappingConflictError):
        atomic_repository.validate_mapping_rollback(
            _scope(),
            MappingRollbackAuthority(
                historical.revision.revision_id,
                historical.revision.manifest_digest,
                _head_boundary(current).head_id,
                rollback.revision.revision_id,
            ),
        )
    assert client.read_calls == 2


class _CountingClient:
    def __init__(self, driver: Driver, after_first_read: Callable[[], None] | None = None) -> None:
        self._delegate = _Client(driver)
        self.read_calls = 0
        self._after_first_read = after_first_read

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        self.read_calls += 1
        result = self._delegate.execute_read(work)
        if self.read_calls == 1 and self._after_first_read is not None:
            self._after_first_read()
        return result
