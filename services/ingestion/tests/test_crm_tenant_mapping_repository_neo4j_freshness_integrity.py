"""Disposable real-Neo4j mapping integrity and freshness coverage for Issue #304."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from _crm_tenant_mapping_neo4j_helpers import (
    _DIGEST,
    _command,
    _command_for_manifest,
    _head_boundary,
    _mark_active,
    _neo4j_driver,
    _repository,
    _scope,
    _set_active_head,
)
from neo4j import Driver
from src.crm_tenant_mapping_contracts import CrmTenantMappingAuthorization
from src.crm_tenant_mapping_models import (
    CrmTenantMappingConflictError,
    CrmTenantMappingIntegrityError,
    CrmTenantMappingRollbackCommand,
    mapping_head_id,
)
from src.standalone_crm_census_requests import (
    MappingPrepareAuthority,
    MappingRollbackAuthority,
    SourceSyncAuthority,
)


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    yield from _neo4j_driver()


def test_malformed_digest_counts_and_links_fail_closed_in_strict_reads(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
    digest_snapshot = repository.prepare(_command("malformed-digest", "issue-304-entity-a"))
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.manifest_digest = $manifest_digest",
            revision_id=digest_snapshot.revision.revision_id,
            manifest_digest="sha256:" + "d" * 64,
        ).consume()
    with pytest.raises(CrmTenantMappingIntegrityError):
        repository.get_revision(
            _scope(), digest_snapshot.revision.revision_id, "sha256:" + "d" * 64
        )

    count_snapshot = repository.prepare(_command("malformed-count", "issue-304-entity-a"))
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.company_entry_count = 99",
            revision_id=count_snapshot.revision.revision_id,
        ).consume()
    with pytest.raises(CrmTenantMappingIntegrityError):
        repository.get_revision(
            _scope(), count_snapshot.revision.revision_id, count_snapshot.revision.manifest_digest
        )

    target_snapshot = repository.prepare(_command("malformed-target", "issue-304-entity-a"))
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (:CrmTenantMappingRevision {revision_id: $revision_id})"
            "-[:HAS_MAPPING_ENTRY]->(:CrmTenantMappingEntry)"
            "-[:HAS_MAPPING_TARGET]->(target:CrmTenantMappingTarget) "
            "SET target.target_id = 'malformed-target-id'",
            revision_id=target_snapshot.revision.revision_id,
        ).consume()
    with pytest.raises(CrmTenantMappingIntegrityError):
        repository.get_revision(
            _scope(), target_snapshot.revision.revision_id, target_snapshot.revision.manifest_digest
        )

    link_snapshot = repository.prepare(_command("malformed-link", "issue-304-entity-a"))
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (:CrmTenantMappingRevision {revision_id: $revision_id})"
            "-[:HAS_MAPPING_ENTRY]->(:CrmTenantMappingEntry)"
            "-[:HAS_MAPPING_TARGET]->(target:CrmTenantMappingTarget) "
            "MATCH (target)-[link:TARGETS_ENTITY]->() DELETE link",
            revision_id=link_snapshot.revision.revision_id,
        ).consume()
    with pytest.raises(CrmTenantMappingIntegrityError):
        repository.get_revision(
            _scope(), link_snapshot.revision.revision_id, link_snapshot.revision.manifest_digest
        )


def test_freshness_readers_require_exact_prepared_rollback_and_active_boundaries(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
    historical = repository.prepare(_command("fresh-history", "issue-304-entity-a"))
    current = repository.prepare(_command("fresh-current", "issue-304-entity-a"))
    _mark_active(neo4j_driver, historical)
    _mark_active(neo4j_driver, current)
    _set_active_head(neo4j_driver, current)
    boundary = _head_boundary(current)
    prepared = repository.prepare(
        _command_for_manifest("fresh-prepare", current.manifest, boundary)
    )
    rollback = repository.rollback(
        CrmTenantMappingRollbackCommand(
            _scope(),
            "fresh-rollback",
            historical.revision.revision_id,
            historical.revision.manifest_digest,
            boundary,
            CrmTenantMappingAuthorization(
                "reviewer",
                "approval",
                _DIGEST,
                "2026-08-29T00:00:00Z",
                "2026-08-30T00:00:00Z",
            ),
            "2026-08-29T12:00:00Z",
        )
    )

    repository.validate_source_sync(
        _scope(),
        SourceSyncAuthority(
            mapping_head_id(_scope()),
            current.revision.manifest_digest,
            "projection-head",
            "projection-digest",
        ),
    )
    repository.validate_mapping_prepare(
        _scope(),
        MappingPrepareAuthority(
            prepared.revision.revision_id,
            prepared.revision.manifest_digest,
            mapping_head_id(_scope()),
        ),
    )
    repository.validate_mapping_rollback(
        _scope(),
        MappingRollbackAuthority(
            historical.revision.revision_id,
            historical.revision.manifest_digest,
            mapping_head_id(_scope()),
            rollback.revision.revision_id,
        ),
    )
    with pytest.raises(CrmTenantMappingConflictError):
        repository.validate_source_sync(
            _scope(),
            SourceSyncAuthority(
                mapping_head_id(_scope()),
                "sha256:" + "b" * 64,
                "projection-head",
                "projection-digest",
            ),
        )
    with pytest.raises(CrmTenantMappingConflictError):
        repository.validate_mapping_prepare(
            _scope(),
            MappingPrepareAuthority(
                prepared.revision.revision_id,
                prepared.revision.manifest_digest,
                "wrong-head",
            ),
        )
    with pytest.raises(CrmTenantMappingConflictError):
        repository.validate_mapping_rollback(
            _scope(),
            MappingRollbackAuthority(
                historical.revision.revision_id,
                "sha256:" + "c" * 64,
                mapping_head_id(_scope()),
                rollback.revision.revision_id,
            ),
        )


def test_rollback_freshness_rejects_corrupt_provenance_and_stale_current_head(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
    historical = repository.prepare(_command("rollback-history", "issue-304-entity-a"))
    current = repository.prepare(_command("rollback-current", "issue-304-entity-a"))
    _mark_active(neo4j_driver, historical)
    _mark_active(neo4j_driver, current)
    _set_active_head(neo4j_driver, current)
    rollback = repository.rollback(
        CrmTenantMappingRollbackCommand(
            _scope(),
            "rollback-freshness",
            historical.revision.revision_id,
            historical.revision.manifest_digest,
            _head_boundary(current),
            CrmTenantMappingAuthorization(
                "reviewer",
                "approval",
                _DIGEST,
                "2026-08-29T00:00:00Z",
                "2026-08-30T00:00:00Z",
            ),
            "2026-08-29T12:00:00Z",
        )
    )
    authority = MappingRollbackAuthority(
        historical.revision.revision_id,
        historical.revision.manifest_digest,
        mapping_head_id(_scope()),
        rollback.revision.revision_id,
    )

    repository.validate_mapping_rollback(_scope(), authority)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.rollback_of_revision_id = 'dangling-revision-id'",
            revision_id=rollback.revision.revision_id,
        ).consume()
    with pytest.raises(CrmTenantMappingConflictError):
        repository.validate_mapping_rollback(_scope(), authority)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.rollback_of_revision_id = $historical_id",
            revision_id=rollback.revision.revision_id,
            historical_id=historical.revision.revision_id,
        ).consume()
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.rollback_of_manifest_digest = $manifest_digest",
            revision_id=rollback.revision.revision_id,
            manifest_digest="sha256:" + "b" * 64,
        ).consume()
    with pytest.raises(CrmTenantMappingConflictError):
        repository.validate_mapping_rollback(_scope(), authority)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.rollback_of_manifest_digest = $manifest_digest",
            revision_id=rollback.revision.revision_id,
            manifest_digest=historical.revision.manifest_digest,
        ).consume()
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.state = 'activation_failed'",
            revision_id=historical.revision.revision_id,
        ).consume()
    with pytest.raises(CrmTenantMappingConflictError):
        repository.validate_mapping_rollback(_scope(), authority)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.state = 'active'",
            revision_id=historical.revision.revision_id,
        ).consume()
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.rollback_of_revision_number = 99",
            revision_id=rollback.revision.revision_id,
        ).consume()
    with pytest.raises(CrmTenantMappingConflictError):
        repository.validate_mapping_rollback(_scope(), authority)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.rollback_of_revision_id = $current_id, "
            "revision.rollback_of_revision_number = $current_number, "
            "revision.rollback_of_manifest_digest = $current_digest",
            revision_id=rollback.revision.revision_id,
            current_id=current.revision.revision_id,
            current_number=current.revision.revision_number,
            current_digest=current.revision.manifest_digest,
        ).consume()
    with pytest.raises(CrmTenantMappingConflictError):
        repository.validate_mapping_rollback(_scope(), authority)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "SET revision.rollback_of_revision_id = $historical_id, "
            "revision.rollback_of_revision_number = $historical_number, "
            "revision.rollback_of_manifest_digest = $historical_digest",
            revision_id=rollback.revision.revision_id,
            historical_id=historical.revision.revision_id,
            historical_number=historical.revision.revision_number,
            historical_digest=historical.revision.manifest_digest,
        ).consume()
        session.run(
            "MATCH (head:CrmTenantMappingActiveHead {head_id: $head_id}) "
            "SET head.active_manifest_digest = $manifest_digest",
            head_id=mapping_head_id(_scope()),
            manifest_digest="sha256:" + "c" * 64,
        ).consume()
    with pytest.raises(CrmTenantMappingConflictError):
        repository.validate_mapping_rollback(_scope(), authority)


def test_orphan_entries_and_targets_fail_closed_in_strict_reads(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
    entry_snapshot = repository.prepare(_command("orphan-entry", "issue-304-entity-a"))
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:CrmTenantMappingEntry {revision_id: $revision_id, "
            "entry_id: 'orphan-entry-id', company_id: '999'})",
            revision_id=entry_snapshot.revision.revision_id,
        ).consume()
    with pytest.raises(CrmTenantMappingIntegrityError):
        repository.get_revision(
            _scope(), entry_snapshot.revision.revision_id, entry_snapshot.revision.manifest_digest
        )

    target_snapshot = repository.prepare(_command("orphan-target", "issue-304-entity-a"))
    entry_id = target_snapshot.entries[0].entry_id
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:CrmTenantMappingTarget {entry_id: $entry_id, "
            "target_id: 'orphan-target-id', entity_key: 'orphan-entity', "
            "relationship_kind: 'tenant_member'})",
            entry_id=entry_id,
        ).consume()
    with pytest.raises(CrmTenantMappingIntegrityError):
        repository.get_revision(
            _scope(), target_snapshot.revision.revision_id, target_snapshot.revision.manifest_digest
        )
