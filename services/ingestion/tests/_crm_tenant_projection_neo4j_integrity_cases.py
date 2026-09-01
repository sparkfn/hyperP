"""Integrity and terminal-state cases for Issue #305 disposable Neo4j coverage."""

from __future__ import annotations

import pytest
from _crm_tenant_projection_neo4j_helpers import (
    _DIGEST,
    _command,
    _drive_to_projection_complete,
    _mapping_active_head_drift_parameters,
    _repository,
)
from _crm_tenant_projection_neo4j_seed import _scope, _seed
from neo4j import Driver
from src.crm_tenant_projection_identity import projection_release_id
from src.crm_tenant_projection_models import (
    CrmTenantProjectionCancelledError,
    CrmTenantProjectionConflictError,
    CrmTenantProjectionIntegrityError,
)


@pytest.mark.parametrize("mutation", ("selected_id", "order", "duplicate_edge", "numeric"))
def test_real_neo4j_membership_selection_corruption_fails_closed(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        if mutation == "selected_id":
            session.run(
                "MATCH (head:CrmCompanyMembershipHead {subject_id: '101'}) "
                "SET head.selected_snapshot_id = 'wrong-snapshot'"
            ).consume()
        elif mutation == "order":
            session.run(
                "MATCH (head:CrmCompanyMembershipHead {subject_id: '101'}) "
                "SET head.source_record_version = 2"
            ).consume()
        elif mutation == "duplicate_edge":
            session.run(
                "MATCH (head:CrmCompanyMembershipHead {subject_id: '101'})"
                "-[edge:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot) "
                "CREATE (head)-[:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot)"
            ).consume()
        else:
            session.run(
                "MATCH (head:CrmCompanyMembershipHead {subject_id: '101'}) "
                "SET head.subject_id = 'bad'"
            ).consume()
            session.run(
                "MATCH (snapshot:CrmCompanyMembershipSnapshot {subject_id: '101'}) "
                "SET snapshot.subject_id = 'bad'"
            ).consume()
    repository = _repository(neo4j_driver, monkeypatch)
    release = repository.allocate_or_replay(_command())
    with pytest.raises(CrmTenantProjectionConflictError, match="capture boundary"):
        repository.capture_page(release.release_id, release.release_fingerprint, 10)


@pytest.mark.parametrize("mutation", ("state", "digest", "active_head"))
def test_real_neo4j_mapping_state_digest_and_head_drift_fail_before_capture(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        if mutation == "state":
            session.run(
                "MATCH (revision:CrmTenantMappingRevision) SET revision.state = 'active'"
            ).consume()
        elif mutation == "digest":
            session.run(
                "MATCH (revision:CrmTenantMappingRevision) SET revision.manifest_digest = $digest",
                digest="sha256:" + "b" * 64,
            ).consume()
        else:
            parameters = _mapping_active_head_drift_parameters()
            session.run(
                "CREATE (:CrmTenantMappingActiveHead {source_key: $source_key, "
                "source_instance_id: $source_instance_id, "
                "control_instance_id: $control_instance_id, "
                "head_id: $head_id, active_revision_id: 'other', active_revision_number: 1, "
                "active_manifest_digest: $active_manifest_digest})",
                source_key=parameters.source_key,
                source_instance_id=parameters.source_instance_id,
                control_instance_id=parameters.control_instance_id,
                head_id=parameters.head_id,
                active_revision_id=parameters.active_revision_id,
                active_revision_number=parameters.active_revision_number,
                active_manifest_digest=parameters.active_manifest_digest,
            ).consume()
    with pytest.raises((CrmTenantProjectionConflictError, CrmTenantProjectionIntegrityError)):
        _repository(neo4j_driver, monkeypatch).allocate_or_replay(_command())


def test_real_neo4j_support_digest_and_authority_edge_corruption_are_rejected(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    release = _drive_to_projection_complete(repository, _command())
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (release:CrmTenantProjectionRelease {release_id: $release_id})"
            "-[:MATERIALIZES_SOURCE_CENSUS]->(census) "
            "CREATE (release)-[:MATERIALIZES_SOURCE_CENSUS]->(census)",
            release_id=release.release_id,
        ).consume()
        session.run(
            "MATCH (:CrmTenantProjectionRelease {release_id: $release_id})"
            "-[:HAS_PROJECTION_INPUT]->()-[:HAS_PROJECTION_ASSOCIATION]->()"
            "-[:HAS_PROJECTION_SUPPORT]->(support) SET support.support_digest = $digest",
            release_id=release.release_id,
            digest=_DIGEST,
        ).consume()
    with pytest.raises(CrmTenantProjectionIntegrityError):
        repository.complete(release.release_id, release.release_fingerprint)


@pytest.mark.parametrize(
    ("property_name", "value", "message"),
    (
        ("capture_boundary_digest", _DIGEST, "capture boundary digest"),
        ("release_fingerprint", "sha256:" + "b" * 64, "release fingerprint"),
    ),
)
def test_real_neo4j_capture_digest_and_terminal_fingerprint_corruption_are_rejected(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
    property_name: str,
    value: str,
    message: str,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    release = _drive_to_projection_complete(repository, _command())
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (release:CrmTenantProjectionRelease {release_id: $release_id}) "
            "SET release[$property_name] = $value",
            release_id=release.release_id,
            property_name=property_name,
            value=value,
        ).consume()
    with pytest.raises(CrmTenantProjectionIntegrityError, match=message):
        repository.complete(release.release_id, release.release_fingerprint)


def test_real_neo4j_direct_completed_replay_rejects_corrupted_topology(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    release = _drive_to_projection_complete(repository, _command())
    completed = repository.complete(release.release_id, release.release_fingerprint)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (:CrmTenantProjectionRelease {release_id: $release_id})"
            "-[:HAS_PROJECTION_INPUT]->()-[:HAS_PROJECTION_ASSOCIATION]->()"
            "-[:HAS_PROJECTION_SUPPORT]->(support) SET support.support_digest = $digest",
            release_id=completed.release_id,
            digest=_DIGEST,
        ).consume()
    with pytest.raises(CrmTenantProjectionIntegrityError, match="support deterministic"):
        repository.complete(completed.release_id, completed.release_fingerprint)


def test_real_neo4j_cancellation_after_capture_is_invisible_and_nonresumable(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    release = repository.allocate_or_replay(_command())
    captured = repository.capture_page(release.release_id, release.release_fingerprint, 1)
    cancelled = repository.cancel(captured.release_id, captured.release_fingerprint)

    assert cancelled.state == "cancelled"
    with pytest.raises(CrmTenantProjectionCancelledError):
        repository.capture_page(cancelled.release_id, cancelled.release_fingerprint, 1)
    assert (
        repository.get_completed(_scope(), cancelled.release_id, cancelled.release_fingerprint)
        is None
    )


def test_real_neo4j_cancellation_during_projection_is_invisible_and_nonresumable(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    release = repository.allocate_or_replay(_command())
    while release.phase == "capture":
        release = repository.capture_page(release.release_id, release.release_fingerprint, 1)
    projected = repository.project_page(release.release_id, release.release_fingerprint, 1)
    cancelled = repository.cancel(projected.release_id, projected.release_fingerprint)

    assert cancelled.state == "cancelled"
    with pytest.raises(CrmTenantProjectionCancelledError):
        repository.project_page(cancelled.release_id, cancelled.release_fingerprint, 1)
    assert (
        repository.get_completed(_scope(), cancelled.release_id, cancelled.release_fingerprint)
        is None
    )


def test_real_neo4j_deterministic_release_collision_is_rejected(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    scope = _scope()
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:CrmTenantProjectionRelease {release_id: $release_id})",
            release_id=projection_release_id(scope, 1),
        ).consume()
    with pytest.raises(CrmTenantProjectionConflictError, match="collides"):
        _repository(neo4j_driver, monkeypatch).allocate_or_replay(_command())
