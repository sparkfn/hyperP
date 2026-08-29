"""Terminal snapshot and mapping-proof corruption cases for Issue #305 Neo4j tests."""

from __future__ import annotations

import pytest
from _crm_tenant_projection_neo4j_helpers import (
    _command,
    _drive_to_projection_complete,
    _repository,
)
from _crm_tenant_projection_neo4j_seed import (
    _contact_snapshot_id,
    _mapping_revision_id,
    _observation_id,
    _scope,
    _seed,
)
from neo4j import Driver
from src.crm_tenant_projection_models import CrmTenantProjectionIntegrityError


@pytest.mark.parametrize("mutation", ("replacement", "addition", "removal"))
def test_real_neo4j_terminal_snapshot_content_corruption_rejects_completion(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    release = _drive_to_projection_complete(repository, _command())
    snapshot_id = _contact_snapshot_id()
    with neo4j_driver.session() as session:
        if mutation == "replacement":
            session.run(
                "MATCH (observation:CrmCompanyMembershipObservation {fixture_snapshot_id: "
                "'issue-305-contact-snapshot'})-[:REFERENCES_COMPANY]->(reference) "
                "SET observation.company_id = $company_id, "
                "observation.observation_id = $observation_id, "
                "reference.company_id = $company_id",
                company_id="404",
                observation_id=_observation_id(snapshot_id, "404", None, None, True),
            ).consume()
        elif mutation == "addition":
            scope = _scope()
            session.run(
                "MATCH (snapshot:CrmCompanyMembershipSnapshot {snapshot_id: $snapshot_id}) "
                "CREATE (reference:CrmCompanyReference {source_key: $source_key, "
                "source_instance_id: $source_instance_id, "
                "control_instance_id: $control_instance_id, "
                "company_id: '404'}) "
                "CREATE (observation:CrmCompanyMembershipObservation {snapshot_id: $snapshot_id, "
                "company_id: '404', observation_id: $observation_id, subject_kind: 'contact', "
                "subject_id: '101', is_primary: false}) "
                "CREATE (snapshot)-[:HAS_MEMBERSHIP_OBSERVATION]->(observation) "
                "CREATE (observation)-[:REFERENCES_COMPANY]->(reference)",
                snapshot_id=snapshot_id,
                source_key=scope.source_key,
                source_instance_id=scope.source_instance_id,
                control_instance_id=scope.control_instance_id,
                observation_id=_observation_id(snapshot_id, "404", None, None, False),
            ).consume()
        else:
            session.run(
                "MATCH (:CrmCompanyMembershipSnapshot {snapshot_id: $snapshot_id})"
                "-[link:HAS_MEMBERSHIP_OBSERVATION]->() DELETE link",
                snapshot_id=snapshot_id,
            ).consume()

    with pytest.raises(CrmTenantProjectionIntegrityError, match="membership snapshot"):
        repository.complete(release.release_id, release.release_fingerprint)


def test_real_neo4j_completed_reader_rejects_snapshot_content_corruption(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    projected = _drive_to_projection_complete(repository, _command())
    completed = repository.complete(projected.release_id, projected.release_fingerprint)
    snapshot_id = _contact_snapshot_id()
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (observation:CrmCompanyMembershipObservation {fixture_snapshot_id: "
            "'issue-305-contact-snapshot'})-[:REFERENCES_COMPANY]->(reference) "
            "SET observation.company_id = $company_id, "
            "observation.observation_id = $observation_id, "
            "reference.company_id = $company_id",
            company_id="404",
            observation_id=_observation_id(snapshot_id, "404", None, None, True),
        ).consume()

    with pytest.raises(CrmTenantProjectionIntegrityError, match="membership snapshot"):
        repository.get_completed(
            _scope(),
            completed.release_id,
            completed.release_fingerprint,
        )


def test_real_neo4j_foreign_mapping_entry_link_rejects_completion(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    release = _drive_to_projection_complete(repository, _command())
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id}) "
            "CREATE (entry:CrmTenantMappingEntry {revision_id: 'foreign-revision', "
            "entry_id: 'foreign-entry', company_id: '404'}) "
            "CREATE (revision)-[:HAS_MAPPING_ENTRY]->(entry)",
            revision_id=_mapping_revision_id(),
        ).consume()

    with pytest.raises(CrmTenantProjectionIntegrityError, match="mapping entry revision"):
        repository.complete(release.release_id, release.release_fingerprint)


@pytest.mark.parametrize("mutation", ("duplicate", "wrong_entity"))
def test_real_neo4j_mapping_target_entity_topology_rejects_completion(
    neo4j_driver: Driver,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _seed(neo4j_driver)
    repository = _repository(neo4j_driver, monkeypatch)
    release = _drive_to_projection_complete(repository, _command())
    with neo4j_driver.session() as session:
        if mutation == "duplicate":
            session.run(
                "MATCH (:CrmTenantMappingTarget)-[:TARGETS_ENTITY]->(entity:Entity) "
                "MATCH (target:CrmTenantMappingTarget)-[:TARGETS_ENTITY]->(entity) "
                "CREATE (target)-[:TARGETS_ENTITY]->(entity)"
            ).consume()
        else:
            session.run(
                "MATCH (target:CrmTenantMappingTarget)-[:TARGETS_ENTITY]->() "
                "CREATE (entity:Entity {entity_key: 'issue-305-wrong-entity'}) "
                "CREATE (target)-[:TARGETS_ENTITY]->(entity)"
            ).consume()

    with pytest.raises(CrmTenantProjectionIntegrityError, match="mapping topology"):
        repository.complete(release.release_id, release.release_fingerprint)
