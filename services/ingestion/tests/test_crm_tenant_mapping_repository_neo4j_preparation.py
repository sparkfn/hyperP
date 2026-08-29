"""Disposable real-Neo4j preparation and concurrency coverage for Issue #304."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from _crm_tenant_mapping_neo4j_helpers import (
    _command,
    _command_for_manifest,
    _concurrent_prepare,
    _counts,
    _mark_active,
    _neo4j_driver,
    _repository,
    _scope,
    _set_active_head,
)
from neo4j import Driver
from src.crm_tenant_mapping_contracts import CrmTenantMappingExpectedHead
from src.crm_tenant_mapping_models import (
    CrmTenantMappingConflictError,
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingRevisionSnapshot,
    mapping_head_id,
    mapping_revision_id,
)


@pytest.fixture
def neo4j_driver() -> Iterator[Driver]:
    yield from _neo4j_driver()


def test_preparation_is_atomic_idempotent_and_does_not_create_missing_entities(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()

    command = _command("request-a", "issue-304-entity-a")
    first = repository.prepare(command)
    replay = repository.prepare(command)
    before_missing_target = _counts(neo4j_driver)
    with pytest.raises(CrmTenantMappingConflictError, match="Entity"):
        repository.prepare(_command("request-b", "issue-304-missing"))
    assert _counts(neo4j_driver) == before_missing_target
    second = repository.prepare(_command("request-c", "issue-304-entity-a"))

    assert replay.revision.revision_id == first.revision.revision_id
    assert first.revision.revision_number == 1
    assert second.revision.revision_number == 2
    assert repository.get_active_revision(_scope()) is None
    with neo4j_driver.session() as session:
        missing_count = session.run(
            "MATCH (:Entity {entity_key: 'issue-304-missing'}) RETURN count(*) AS count"
        ).single(strict=True)["count"]
        revision_count = session.run(
            "MATCH (:CrmTenantMappingRevision) RETURN count(*) AS count"
        ).single(strict=True)["count"]
    assert missing_count == 0
    assert revision_count == 2


def test_deterministic_revision_id_collision_fails_without_counter_gap(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    collision_id = mapping_revision_id(_scope(), 1)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
        session.run(
            "CREATE (:CrmTenantMappingRevision {revision_id: $revision_id})",
            revision_id=collision_id,
        ).consume()

    with pytest.raises(CrmTenantMappingConflictError, match="collides"):
        repository.prepare(_command("colliding", "issue-304-entity-a"))
    assert _counts(neo4j_driver) == (0, 1, 0, 0)

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) DELETE revision",
            revision_id=collision_id,
        ).consume()
    prepared = repository.prepare(_command("after-collision", "issue-304-entity-a"))
    assert prepared.revision.revision_number == 1


def test_concurrent_preparation_replays_conflicts_and_monotonic_allocation(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-b'})").consume()

    exact = _command("same-request", "issue-304-entity-a")
    first, second = _concurrent_prepare(repository, (exact, exact), monkeypatch)
    assert isinstance(first, CrmTenantMappingRevisionSnapshot)
    assert isinstance(second, CrmTenantMappingRevisionSnapshot)
    assert first.revision.revision_number == 1
    assert second.revision.revision_id == first.revision.revision_id

    conflict_a = _command("conflicting-request", "issue-304-entity-a")
    conflict_b = _command("conflicting-request", "issue-304-entity-b")
    first, second = _concurrent_prepare(repository, (conflict_a, conflict_b), monkeypatch)
    outcomes = (first, second)
    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, CrmTenantMappingConflictError) for outcome in outcomes) == 1

    next_revision = repository.prepare(_command("after-conflict", "issue-304-entity-a"))
    assert next_revision.revision.revision_number == 3

    distinct_a = _command("distinct-a", "issue-304-entity-a")
    distinct_b = _command("distinct-b", "issue-304-entity-b")
    first, second = _concurrent_prepare(repository, (distinct_a, distinct_b), monkeypatch)
    assert isinstance(first, CrmTenantMappingRevisionSnapshot)
    assert isinstance(second, CrmTenantMappingRevisionSnapshot)
    numbers = {
        first.revision.revision_number,
        second.revision.revision_number,
    }
    assert numbers == {4, 5}


def test_stale_absent_and_present_heads_fail_without_authority_writes(
    neo4j_driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(neo4j_driver, monkeypatch)
    with neo4j_driver.session() as session:
        session.run("CREATE (:Entity {entity_key: 'issue-304-entity-a'})").consume()
    first = repository.prepare(_command("first", "issue-304-entity-a"))
    _mark_active(neo4j_driver, first)
    _set_active_head(neo4j_driver, first)
    before = _counts(neo4j_driver)

    with pytest.raises(CrmTenantMappingConflictError, match="unexpectedly exists"):
        repository.prepare(_command("stale-absent", "issue-304-entity-a"))
    wrong = CrmTenantMappingExpectedHeadBoundary(
        _scope(),
        mapping_head_id(_scope()),
        CrmTenantMappingExpectedHead(
            mapping_head_id(_scope()),
            first.revision.revision_id,
            first.revision.revision_number + 1,
            first.revision.manifest_digest,
        ),
    )
    with pytest.raises(CrmTenantMappingConflictError, match="stale"):
        repository.prepare(_command_for_manifest("stale-present", first.manifest, wrong))

    assert _counts(neo4j_driver) == before
