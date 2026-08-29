"""Disposable real-Neo4j lifecycle coverage for Issue #304."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from _crm_tenant_mapping_neo4j_helpers import (
    _DIGEST,
    _command,
    _command_for_manifest,
    _concurrent_reject,
    _counts,
    _head_boundary,
    _mark_active,
    _neo4j_driver,
    _repository,
    _scope,
    _set_active_head,
)
from neo4j import Driver
from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingManifest,
    CrmTenantMappingRollbackProvenance,
)
from src.crm_tenant_mapping_models import (
    CrmTenantMappingConflictError,
    CrmTenantMappingIntegrityError,
    CrmTenantMappingRejectCommand,
    CrmTenantMappingRejection,
    CrmTenantMappingRevisionSnapshot,
    CrmTenantMappingRollbackCommand,
)


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    yield from _neo4j_driver()


def test_empty_omission_rejection_and_active_invisibility_are_strict(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    explicit = CrmTenantMappingManifest(_scope(), (CrmTenantMappingCompanyEntry("10", ()),))
    omitted = CrmTenantMappingManifest(_scope(), ())
    explicit_snapshot = repository.prepare(_command_for_manifest("explicit-empty", explicit))
    omitted_snapshot = repository.prepare(_command_for_manifest("omitted", omitted))

    assert explicit.digest != omitted.digest
    assert explicit_snapshot.manifest.targets_for("10") == ()
    assert len(explicit_snapshot.entries) == 1
    assert omitted_snapshot.manifest.targets_for("10") == ()
    assert omitted_snapshot.entries == ()
    assert repository.get_active_revision(_scope()) is None
    _set_active_head(neo4j_driver, explicit_snapshot)
    with pytest.raises(CrmTenantMappingIntegrityError):
        repository.get_active_revision(_scope())

    rejection = CrmTenantMappingRejectCommand(
        _scope(),
        explicit_snapshot.revision.revision_id,
        explicit_snapshot.revision.manifest_digest,
        CrmTenantMappingRejection("reviewer", "case", "bad"),
        CrmTenantMappingAuthorization(
            "reviewer", "approval", _DIGEST, "2026-08-29T00:00:00Z", "2026-08-30T00:00:00Z"
        ),
        "2026-08-29T12:00:00Z",
    )
    before = _counts(neo4j_driver)
    head_before = repository.get_active_head(_scope())
    rejected = repository.reject(rejection)
    assert repository.reject(rejection) == rejected
    changed = CrmTenantMappingRejectCommand(
        _scope(),
        rejection.revision_id,
        rejection.manifest_digest,
        rejection.rejection,
        CrmTenantMappingAuthorization(
            "reviewer", "changed", _DIGEST, "2026-08-29T00:00:00Z", "2026-08-30T00:00:00Z"
        ),
        rejection.operation_time,
    )
    with pytest.raises(CrmTenantMappingConflictError):
        repository.reject(changed)
    assert rejected.manifest == explicit
    assert _counts(neo4j_driver) == before
    assert repository.get_active_head(_scope()) == head_before
    with pytest.raises(CrmTenantMappingIntegrityError):
        repository.get_active_revision(_scope())

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.state = 'activation_failed'",
            revision_id=omitted_snapshot.revision.revision_id,
        ).consume()
    _set_active_head(neo4j_driver, omitted_snapshot)
    with pytest.raises(CrmTenantMappingIntegrityError):
        repository.get_active_revision(_scope())
    with pytest.raises(CrmTenantMappingConflictError, match="only prepared"):
        repository.reject(
            CrmTenantMappingRejectCommand(
                _scope(),
                omitted_snapshot.revision.revision_id,
                omitted_snapshot.revision.manifest_digest,
                CrmTenantMappingRejection("reviewer", "case", "late"),
                rejection.authorization,
                rejection.operation_time,
            )
        )


def test_rollback_copies_effective_history_without_moving_current_head(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
    historical = repository.prepare(_command("historical", "issue-304-entity-a"))
    current = repository.prepare(_command("current", "issue-304-entity-a"))
    _mark_active(neo4j_driver, historical)
    _mark_active(neo4j_driver, current)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.state = 'superseded'",
            revision_id=historical.revision.revision_id,
        ).consume()
    _set_active_head(neo4j_driver, current)
    rollback = CrmTenantMappingRollbackCommand(
        _scope(),
        "rollback",
        historical.revision.revision_id,
        historical.revision.manifest_digest,
        _head_boundary(current),
        CrmTenantMappingAuthorization(
            "reviewer", "approval", _DIGEST, "2026-08-29T00:00:00Z", "2026-08-30T00:00:00Z"
        ),
        "2026-08-29T12:00:00Z",
    )

    prepared = repository.rollback(rollback)
    assert prepared.revision.state == "prepared"
    assert prepared.revision.revision_number == 3
    assert prepared.manifest == historical.manifest
    assert prepared.revision.rollback_provenance == CrmTenantMappingRollbackProvenance(
        historical.revision.revision_id,
        historical.revision.revision_number,
        historical.revision.manifest_digest,
    )
    assert repository.get_active_head(_scope()).active_revision_id == current.revision.revision_id
    assert (
        repository.get_revision(
            _scope(), historical.revision.revision_id, historical.revision.manifest_digest
        ).revision.state
        == "superseded"
    )


def test_concurrent_rejection_replays_exactly_and_rejects_conflicting_input(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    prepared = repository.prepare(
        _command_for_manifest("reject-exact", CrmTenantMappingManifest(_scope(), ()))
    )
    exact = CrmTenantMappingRejectCommand(
        _scope(),
        prepared.revision.revision_id,
        prepared.revision.manifest_digest,
        CrmTenantMappingRejection("reviewer", "case", "bad target"),
        CrmTenantMappingAuthorization(
            "reviewer", "approval", _DIGEST, "2026-08-29T00:00:00Z", "2026-08-30T00:00:00Z"
        ),
        "2026-08-29T12:00:00Z",
    )
    first, second = _concurrent_reject(repository, (exact, exact))
    assert isinstance(first, CrmTenantMappingRevisionSnapshot)
    assert isinstance(second, CrmTenantMappingRevisionSnapshot)
    assert first.revision.state == "rejected"
    assert second.revision.revision_id == first.revision.revision_id
    assert second.rejection_request_fingerprint == exact.request_fingerprint

    conflicting = repository.prepare(
        _command_for_manifest("reject-conflict", CrmTenantMappingManifest(_scope(), ()))
    )
    first_command = CrmTenantMappingRejectCommand(
        _scope(),
        conflicting.revision.revision_id,
        conflicting.revision.manifest_digest,
        CrmTenantMappingRejection("reviewer", "case", "bad target"),
        CrmTenantMappingAuthorization(
            "reviewer", "approval-a", _DIGEST, "2026-08-29T00:00:00Z", "2026-08-30T00:00:00Z"
        ),
        "2026-08-29T12:00:00Z",
    )
    second_command = CrmTenantMappingRejectCommand(
        _scope(),
        conflicting.revision.revision_id,
        conflicting.revision.manifest_digest,
        first_command.rejection,
        CrmTenantMappingAuthorization(
            "reviewer", "approval-b", _DIGEST, "2026-08-29T00:00:00Z", "2026-08-30T00:00:00Z"
        ),
        first_command.operation_time,
    )
    before = _counts(neo4j_driver)
    head_before = repository.get_active_head(_scope())
    first, second = _concurrent_reject(repository, (first_command, second_command))
    outcomes = (first, second)
    successful = next(
        outcome for outcome in outcomes if isinstance(outcome, CrmTenantMappingRevisionSnapshot)
    )
    assert sum(isinstance(outcome, CrmTenantMappingRevisionSnapshot) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, CrmTenantMappingConflictError) for outcome in outcomes) == 1
    assert successful.manifest == conflicting.manifest
    assert _counts(neo4j_driver) == before
    assert repository.get_active_head(_scope()) == head_before
