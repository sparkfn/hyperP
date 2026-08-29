"""Projection observation and terminal replay cases for disposable Neo4j coverage."""

from __future__ import annotations

from dataclasses import replace

import pytest
from _crm_tenant_projection_neo4j_helpers import (
    _command,
    _repository,
    _scope,
    _seed,
)
from neo4j import Driver
from src.crm_tenant_mapping_contracts import CrmTenantMappingCompanyEntry, CrmTenantMappingManifest
from src.crm_tenant_projection_models import (
    CrmTenantProjectionConflictError,
    CrmTenantProjectionIntegrityError,
)


@pytest.mark.parametrize("terminal_state", ("cancelled", "failed"))
@pytest.mark.parametrize("drift", ("mapping", "census", "projection_head"))
def test_real_neo4j_terminal_failed_or_cancelled_replay_is_stable_after_drift(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
    terminal_state: str,
    drift: str,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    command = _command()
    allocated = repository.allocate_or_replay(command)
    if terminal_state == "cancelled":
        terminal = repository.cancel(allocated.release_id, allocated.release_fingerprint)
    else:
        terminal = repository.fail(
            allocated.release_id, allocated.release_fingerprint, "boundary_conflict"
        )
    with neo4j_driver.session() as session:
        if drift == "mapping":
            session.run(
                "MATCH (revision:CrmTenantMappingRevision) SET revision.state = 'active'"
            ).consume()
        elif drift == "census":
            session.run(
                "MATCH (census:StandaloneCrmCensus {census_id: 'issue-305-census'}) "
                "SET census.processed_rows = 3"
            ).consume()
        else:
            scope = _scope()
            session.run(
                "CREATE (:CrmTenantProjectionActiveHead {source_key: $source_key, "
                "source_instance_id: $source_instance_id, "
                "control_instance_id: $control_instance_id, "
                "head_id: $head_id})",
                source_key=scope.source_key,
                source_instance_id=scope.source_instance_id,
                control_instance_id=scope.control_instance_id,
                head_id=command.projection_head_id,
            ).consume()

    assert repository.allocate_or_replay(command) == terminal
    with pytest.raises(CrmTenantProjectionConflictError, match="different immutable input"):
        repository.allocate_or_replay(
            replace(command, mapping_manifest_digest="sha256:" + "b" * 64)
        )


@pytest.mark.parametrize("mapping_kind", ("omitted", "empty_entry"))
@pytest.mark.parametrize(
    "mutation", ("cross_subject", "duplicate_snapshot_reference", "cross_snapshot")
)
def test_real_neo4j_unmapped_or_empty_entry_observation_topology_fails_closed(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
    mapping_kind: str,
    mutation: str,
) -> None:
    scope = _scope()
    entries = ()
    if mapping_kind == "empty_entry":
        entries = (CrmTenantMappingCompanyEntry("303", ()),)
    manifest = CrmTenantMappingManifest(scope.mapping_scope, entries)
    _seed(neo4j_driver, manifest)
    with neo4j_driver.session() as session:
        if mutation == "cross_subject":
            session.run(
                "MATCH (observation:CrmCompanyMembershipObservation {snapshot_id: "
                "'issue-305-contact-snapshot'}) SET observation.subject_id = '999'"
            ).consume()
        elif mutation == "duplicate_snapshot_reference":
            session.run(
                "MATCH (snapshot:CrmCompanyMembershipSnapshot {snapshot_id: "
                "'issue-305-contact-snapshot'})-[:HAS_MEMBERSHIP_OBSERVATION]->(observation) "
                "CREATE (snapshot)-[:HAS_MEMBERSHIP_OBSERVATION]->(observation)"
            ).consume()
        else:
            session.run(
                "MATCH (observation:CrmCompanyMembershipObservation {"
                "snapshot_id: 'issue-305-contact-snapshot'}) CREATE "
                "(:CrmCompanyMembershipSnapshot {snapshot_id: 'other-owner'})"
                "-[:HAS_MEMBERSHIP_OBSERVATION]->(observation)"
            ).consume()
    repository = _repository(neo4j_driver, monkeypatch)
    release = repository.allocate_or_replay(_command(manifest=manifest))
    while release.phase == "capture":
        release = repository.capture_page(release.release_id, release.release_fingerprint, 1)

    with pytest.raises(CrmTenantProjectionIntegrityError, match="membership observation"):
        repository.project_page(release.release_id, release.release_fingerprint, 1)


@pytest.mark.parametrize("mapping_kind", ("omitted", "empty_entry"))
@pytest.mark.parametrize(
    "mutation",
    ("missing", "duplicate", "wrong_company", "wrong_scope", "wrong_source_key"),
)
def test_real_neo4j_unmapped_company_reference_topology_fails_closed(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
    mapping_kind: str,
    mutation: str,
) -> None:
    scope = _scope()
    entries = ()
    if mapping_kind == "empty_entry":
        entries = (CrmTenantMappingCompanyEntry("303", ()),)
    manifest = CrmTenantMappingManifest(scope.mapping_scope, entries)
    _seed(neo4j_driver, manifest)
    with neo4j_driver.session() as session:
        if mutation == "missing":
            session.run(
                "MATCH (:CrmCompanyMembershipObservation {snapshot_id: "
                "'issue-305-contact-snapshot'})-[edge:REFERENCES_COMPANY]->() DELETE edge"
            ).consume()
        elif mutation == "duplicate":
            session.run(
                "MATCH (observation:CrmCompanyMembershipObservation {snapshot_id: "
                "'issue-305-contact-snapshot'})-[:REFERENCES_COMPANY]->(reference) "
                "CREATE (observation)-[:REFERENCES_COMPANY]->(reference)"
            ).consume()
        else:
            company_id = "404" if mutation == "wrong_company" else "303"
            source_key = "other-source" if mutation == "wrong_source_key" else scope.source_key
            source_instance_id = (
                "other-portal" if mutation == "wrong_scope" else scope.source_instance_id
            )
            session.run(
                "MATCH (observation:CrmCompanyMembershipObservation {snapshot_id: "
                "'issue-305-contact-snapshot'})-[edge:REFERENCES_COMPANY]->() DELETE edge "
                "CREATE (reference:CrmCompanyReference {source_key: $source_key, "
                "source_instance_id: $source_instance_id, "
                "control_instance_id: $control_instance_id, "
                "company_id: $company_id}) CREATE (observation)-[:REFERENCES_COMPANY]->(reference)",
                source_key=source_key,
                source_instance_id=source_instance_id,
                control_instance_id=scope.control_instance_id,
                company_id=company_id,
            ).consume()
    repository = _repository(neo4j_driver, monkeypatch)
    release = repository.allocate_or_replay(_command(manifest=manifest))
    while release.phase == "capture":
        release = repository.capture_page(release.release_id, release.release_fingerprint, 1)

    with pytest.raises(CrmTenantProjectionIntegrityError, match="company reference"):
        repository.project_page(release.release_id, release.release_fingerprint, 1)


@pytest.mark.parametrize("edge", ("entry", "target"))
def test_real_neo4j_mapping_topology_drift_is_rejected(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
    edge: str,
) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        if edge == "entry":
            session.run(
                "MATCH (:CrmTenantMappingRevision {revision_id: 'issue-305-mapping'})"
                "-[link:HAS_MAPPING_ENTRY]->() DELETE link"
            ).consume()
        else:
            session.run(
                "MATCH (:CrmTenantMappingEntry {revision_id: 'issue-305-mapping'})"
                "-[link:HAS_MAPPING_TARGET]->() DELETE link"
            ).consume()
    with pytest.raises(CrmTenantProjectionIntegrityError, match="prepared mapping topology"):
        _repository(neo4j_driver, monkeypatch).allocate_or_replay(_command())


@pytest.mark.parametrize(
    "node_label",
    ("StandaloneCrmChildPublication", "StandaloneCrmCensusFence"),
)
def test_real_neo4j_unresolved_source_census_control_is_rejected(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
    node_label: str,
) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        if node_label == "StandaloneCrmChildPublication":
            session.run(
                "CREATE (:StandaloneCrmChildPublication {census_id: 'issue-305-census', "
                "generation: 1, stream_kind: 'company', status: 'pending'})"
            ).consume()
        else:
            session.run(
                "CREATE (:StandaloneCrmCensusFence {census_id: 'issue-305-census', "
                "generation: 1, stream_kind: 'company', status: 'active'})"
            ).consume()
    with pytest.raises(CrmTenantProjectionConflictError):
        _repository(neo4j_driver, monkeypatch).allocate_or_replay(_command())


def test_real_neo4j_selected_company_failure_is_rejected(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (unit:StandaloneCrmCensusUnit {census_id: 'issue-305-census', "
            "stream_kind: 'company'}) SET unit.state = 'failed'"
        ).consume()
    with pytest.raises(CrmTenantProjectionConflictError, match="incomplete"):
        _repository(neo4j_driver, monkeypatch).allocate_or_replay(_command())


@pytest.mark.parametrize(
    "create_query",
    (
        "CREATE (:StandaloneCrmChildPublication {census_id: 'issue-305-census', "
        "generation: 1, stream_kind: 'company', status: 'pending'})",
        "CREATE (:StandaloneCrmCensusFence {census_id: 'issue-305-census', "
        "generation: 1, stream_kind: 'company', status: 'active'})",
    ),
)
def test_real_neo4j_release_boundary_revalidates_source_census_controls(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
    create_query: str,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    release = repository.allocate_or_replay(_command())
    with neo4j_driver.session() as session:
        session.run(create_query).consume()
    with pytest.raises(CrmTenantProjectionConflictError):
        repository.capture_page(release.release_id, release.release_fingerprint, 1)


def test_real_neo4j_cross_source_instance_membership_is_not_captured(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "CREATE (head:CrmCompanyMembershipHead {source_instance_id: 'other-portal', "
            "control_instance_id: $control_instance_id, subject_kind: 'contact', "
            "subject_id: '101', "
            "available_at: datetime($available_at)}) "
            "CREATE (snapshot:CrmCompanyMembershipSnapshot {snapshot_id: 'other-snapshot', "
            "snapshot_digest: $digest, source_instance_id: 'other-portal', "
            "control_instance_id: $control_instance_id, subject_kind: 'contact', "
            "subject_id: '101', "
            "available_at: datetime($available_at), binding_count: 0}) "
            "CREATE (head)-[:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot)",
            control_instance_id=_scope().control_instance_id,
            available_at="2026-08-29T00:00:00Z",
            digest="sha256:" + "a" * 64,
        ).consume()
    repository = _repository(neo4j_driver, monkeypatch)
    release = repository.allocate_or_replay(_command())
    while release.phase == "capture":
        release = repository.capture_page(release.release_id, release.release_fingerprint, 1)

    assert release.input_count == 2
