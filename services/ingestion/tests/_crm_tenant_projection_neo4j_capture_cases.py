"""Capture, projection, and replay cases for Issue #305 disposable Neo4j coverage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from _crm_tenant_projection_neo4j_helpers import (
    _DIGEST,
    _add_membership_observation,
    _command,
    _drive_to_projection_complete,
    _mapping_manifest,
    _repository,
    _scope,
    _seed,
)
from neo4j import Driver, ManagedTransaction
from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingTarget,
)
from src.crm_tenant_projection_models import (
    CrmTenantProjectionConflictError,
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionMaterializationCommand,
    CrmTenantProjectionReleaseSummary,
)
from src.graph import crm_tenant_projection as projection_graph
from src.graph.crm_tenant_projection_census import _CensusBoundary


def test_real_neo4j_capture_projection_zero_target_and_exact_replay(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    command = _command()

    release = repository.allocate_or_replay(command)
    assert release.state == "building"
    assert release.phase == "capture"
    while release.phase == "capture":
        release = repository.capture_page(release.release_id, release.release_fingerprint, 1)
    while release.phase == "projection":
        release = repository.project_page(release.release_id, release.release_fingerprint, 1)
    completed = repository.complete(release.release_id, release.release_fingerprint)

    assert completed.state == "completed"
    assert completed.input_count == 2
    assert completed.decision_count == 2
    assert completed.association_count == 1
    assert completed.support_count == 1
    assert (
        repository.get_completed(_scope(), completed.release_id, completed.release_fingerprint)
        == completed
    )
    assert repository.allocate_or_replay(command) == completed
    with neo4j_driver.session() as session:
        counts = session.run(
            "MATCH (release:CrmTenantProjectionRelease) "
            "OPTIONAL MATCH (head:CrmTenantProjectionActiveHead) "
            "RETURN count(DISTINCT release) AS releases, count(DISTINCT head) AS heads"
        ).single(strict=True)
    assert counts["releases"] == 1
    assert counts["heads"] == 0


def test_real_neo4j_conflicting_request_and_cancellation_are_invisible(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    allocated = repository.allocate_or_replay(_command())

    cancelled = repository.cancel(allocated.release_id, allocated.release_fingerprint)

    assert cancelled.state == "cancelled"
    assert (
        repository.get_completed(_scope(), cancelled.release_id, cancelled.release_fingerprint)
        is None
    )
    with pytest.raises(CrmTenantProjectionConflictError, match="different immutable input"):
        repository.allocate_or_replay(
            replace(_command(), mapping_manifest_digest="sha256:" + "b" * 64)
        )


def test_real_neo4j_concurrent_duplicate_allocation_replays_one_release(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    command = _command()

    def allocate() -> CrmTenantProjectionReleaseSummary:
        return repository.allocate_or_replay(command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = tuple(executor.map(lambda _value: allocate(), range(2)))

    assert first == second
    assert (first.release_number, first.state, first.phase) == (1, "building", "capture")
    with neo4j_driver.session() as session:
        count = session.run("MATCH (:CrmTenantProjectionRelease) RETURN count(*) AS count").single(
            strict=True
        )
    assert count["count"] == 1


def test_real_neo4j_concurrent_conflicting_request_reuse_allocates_one_release(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    first_command = _command()
    conflicting_command = replace(
        first_command,
        mapping_manifest_digest="sha256:" + "b" * 64,
    )
    canonical_boundary_reached = Event()
    conflicting_call_started = Event()
    validate_source_census = projection_graph._validate_source_census

    def hold_canonical_scope_lock(
        tx: ManagedTransaction,
        command: CrmTenantProjectionMaterializationCommand,
    ) -> _CensusBoundary:
        if command == first_command:
            canonical_boundary_reached.set()
            assert conflicting_call_started.wait(timeout=5)
        return validate_source_census(tx, command)

    def allocate_conflicting_command() -> CrmTenantProjectionReleaseSummary:
        conflicting_call_started.set()
        return repository.allocate_or_replay(conflicting_command)

    monkeypatch.setattr(
        projection_graph,
        "_validate_source_census",
        hold_canonical_scope_lock,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(repository.allocate_or_replay, first_command)
        assert canonical_boundary_reached.wait(timeout=5)
        conflicting_future = executor.submit(allocate_conflicting_command)
        first = first_future.result()
        with pytest.raises(CrmTenantProjectionConflictError, match="different immutable input"):
            conflicting_future.result()

    assert first.release_number == 1
    with neo4j_driver.session() as session:
        releases = session.run(
            "MATCH (release:CrmTenantProjectionRelease) "
            "RETURN count(release) AS count, collect(release.release_number) AS numbers"
        ).single(strict=True)
    assert releases["count"] == 1
    assert releases["numbers"] == [1]


def test_real_neo4j_out_of_bound_heads_fail_closed(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (head:CrmCompanyMembershipHead {subject_id: '101'}) SET head.subject_id = '999'"
        ).consume()
        session.run(
            "MATCH (snapshot:CrmCompanyMembershipSnapshot {subject_id: '101'}) "
            "SET snapshot.subject_id = '999'"
        ).consume()
    repository = _repository(neo4j_driver, monkeypatch)

    release = repository.allocate_or_replay(_command())
    with pytest.raises(CrmTenantProjectionConflictError, match="capture boundary"):
        repository.capture_page(release.release_id, release.release_fingerprint, 10)


def test_real_neo4j_canonical_no_work_zero_input_completes(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (unit:StandaloneCrmCensusUnit) "
            "SET unit.state = 'no_work', unit.generation = 1, unit.frozen_upper_id = 0"
        ).consume()
        session.run(
            "MATCH (census:StandaloneCrmCensus {census_id: 'issue-305-census'}) "
            "SET census.completed_units = 0, census.no_work_units = 3, "
            "census.processed_rows = 0, census.skipped_rows = 0"
        ).consume()
        session.run("MATCH (checkpoint:StandaloneCrmCensusCheckpoint) DELETE checkpoint").consume()
        session.run("MATCH (head:CrmCompanyMembershipHead) DETACH DELETE head").consume()
        session.run(
            "MATCH (snapshot:CrmCompanyMembershipSnapshot) DETACH DELETE snapshot"
        ).consume()
    repository = _repository(neo4j_driver, monkeypatch)

    release = _drive_to_projection_complete(repository, _command())
    completed = repository.complete(release.release_id, release.release_fingerprint)

    assert completed.state == "completed"
    assert (completed.input_count, completed.decision_count) == (0, 0)
    assert (completed.association_count, completed.support_count) == (0, 0)


def test_real_neo4j_duplicate_checkpoint_after_allocation_fails_closed(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    release = repository.allocate_or_replay(_command())
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:StandaloneCrmCensusCheckpoint {census_id: 'issue-305-census', "
            "stream_kind: 'contact', generation: 1, frozen_upper_id: 101, "
            "last_committed_id: 101, processed_rows: 1, skipped_rows: 0})"
        ).consume()
    with pytest.raises(CrmTenantProjectionConflictError, match="authority boundary"):
        repository.capture_page(release.release_id, release.release_fingerprint, 1)


def test_real_neo4j_deduplicates_company_paths_and_retains_each_support(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _mapping_manifest(
        (
            CrmTenantMappingCompanyEntry("303", (CrmTenantMappingTarget("issue-305-entity"),)),
            CrmTenantMappingCompanyEntry("404", (CrmTenantMappingTarget("issue-305-entity"),)),
        )
    )
    _seed(neo4j_driver, manifest)
    with neo4j_driver.session() as session:
        _add_membership_observation(
            session,
            "issue-305-contact-snapshot",
            "contact",
            "101",
            "404",
            False,
        )
    repository = _repository(neo4j_driver, monkeypatch)
    release = _drive_to_projection_complete(repository, _command(manifest=manifest))
    completed = repository.complete(release.release_id, release.release_fingerprint)

    assert (completed.association_count, completed.support_count) == (1, 2)


def test_real_neo4j_acknowledgement_loss_replay_converges_without_duplicate_supports(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _mapping_manifest(
        (
            CrmTenantMappingCompanyEntry("303", (CrmTenantMappingTarget("issue-305-entity"),)),
            CrmTenantMappingCompanyEntry("404", (CrmTenantMappingTarget("issue-305-entity"),)),
        )
    )
    _seed(neo4j_driver, manifest)
    with neo4j_driver.session() as session:
        _add_membership_observation(
            session,
            "issue-305-contact-snapshot",
            "contact",
            "101",
            "404",
            False,
        )
    repository = _repository(neo4j_driver, monkeypatch)
    release = repository.allocate_or_replay(_command(manifest=manifest))

    repository.capture_page(release.release_id, release.release_fingerprint, 1)
    captured = repository.capture_page(release.release_id, release.release_fingerprint, 1)
    while captured.phase == "capture":
        captured = repository.capture_page(captured.release_id, captured.release_fingerprint, 1)
    repository.project_page(captured.release_id, captured.release_fingerprint, 1)
    projected = repository.project_page(captured.release_id, captured.release_fingerprint, 1)
    while projected.phase == "projection":
        projected = repository.project_page(projected.release_id, projected.release_fingerprint, 1)
    completed = repository.complete(projected.release_id, projected.release_fingerprint)

    assert (completed.input_count, completed.decision_count) == (2, 2)
    assert (completed.association_count, completed.support_count) == (1, 2)
    with neo4j_driver.session() as session:
        counts = session.run(
            "MATCH (:CrmTenantProjectionRelease {release_id: $release_id})"
            "-[:HAS_PROJECTION_INPUT]->(input) "
            "OPTIONAL MATCH (input)-[:HAS_PROJECTION_DECISION]->(decision) "
            "OPTIONAL MATCH (input)-[:HAS_PROJECTION_ASSOCIATION]->(association)"
            "-[:HAS_PROJECTION_SUPPORT]->(support) "
            "RETURN count(DISTINCT input) AS inputs, count(DISTINCT decision) AS decisions, "
            "count(DISTINCT association) AS associations, count(DISTINCT support) AS supports",
            release_id=completed.release_id,
        ).single(strict=True)
    assert dict(counts) == {"inputs": 2, "decisions": 2, "associations": 1, "supports": 2}


def test_real_neo4j_completion_and_reader_reject_orphan_release_children(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    release = _drive_to_projection_complete(repository, _command())
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:CrmTenantProjectionDecision {release_id: $release_id, input_id: 'orphan', "
            "decision: 'zero_target', zero_target_reason: 'empty_membership', "
            "decision_digest: $digest})",
            release_id=release.release_id,
            digest=_DIGEST,
        ).consume()
    with pytest.raises(CrmTenantProjectionIntegrityError, match="aggregate counts"):
        repository.complete(release.release_id, release.release_fingerprint)


def test_real_neo4j_page_replay_and_stale_projection_head_are_safe(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    release = repository.allocate_or_replay(_command())
    first = repository.capture_page(release.release_id, release.release_fingerprint, 1)
    captured = repository.capture_page(first.release_id, first.release_fingerprint, 1)
    assert captured.input_count == 2
    with neo4j_driver.session() as session:
        count = session.run(
            "MATCH (:CrmTenantProjectionRelease {release_id: $release_id})"
            "-[:HAS_PROJECTION_INPUT]->(input) RETURN count(input) AS count",
            release_id=release.release_id,
        ).single(strict=True)
        assert count["count"] == 2
    while captured.phase == "capture":
        captured = repository.capture_page(captured.release_id, captured.release_fingerprint, 1)
    while captured.phase == "projection":
        captured = repository.project_page(captured.release_id, captured.release_fingerprint, 1)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (:CrmTenantProjectionActiveHead {source_key: $source_key, "
            "source_instance_id: $source_instance_id, control_instance_id: $control_instance_id, "
            "head_id: $head_id})",
            source_key=captured.scope.source_key,
            source_instance_id=captured.scope.source_instance_id,
            control_instance_id=captured.scope.control_instance_id,
            head_id=_command().projection_head_id,
        ).consume()
    with pytest.raises(CrmTenantProjectionConflictError, match="authority boundary"):
        repository.complete(captured.release_id, captured.release_fingerprint)


@pytest.mark.parametrize("mapping_kind", ("omit", "empty_entry"))
def test_real_neo4j_valid_omitted_and_explicit_empty_mapping_are_no_mapped_targets(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
    mapping_kind: str,
) -> None:
    entries = () if mapping_kind == "omit" else (CrmTenantMappingCompanyEntry("303", ()),)
    manifest = _mapping_manifest(entries)
    _seed(neo4j_driver, manifest)
    repository = _repository(neo4j_driver, monkeypatch)
    release = _drive_to_projection_complete(repository, _command(manifest=manifest))
    completed = repository.complete(release.release_id, release.release_fingerprint)

    assert (completed.association_count, completed.support_count) == (0, 0)
    with neo4j_driver.session() as session:
        decision = session.run(
            "MATCH (:CrmTenantProjectionRelease {release_id: $release_id})"
            "-[:HAS_PROJECTION_INPUT]->(:CrmTenantProjectionInput {subject_kind: 'contact'})"
            "-[:HAS_PROJECTION_DECISION]->(decision) RETURN decision.zero_target_reason AS reason",
            release_id=completed.release_id,
        ).single(strict=True)
    assert decision["reason"] == "no_mapped_targets"
