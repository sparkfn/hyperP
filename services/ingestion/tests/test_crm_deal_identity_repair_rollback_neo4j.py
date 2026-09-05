# ruff: noqa: E501 -- acceptance Cypher fixtures retain their graph shape.
"""Disposable-Neo4j acceptance tests for #312 rollback through the real repository."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from typing import TypeVar, cast
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

import pytest
import test_crm_deal_identity_repair_mutation_neo4j as mutation
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.execution_models import RepairExecutionBoundaryManifest
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair.mutation_models import (
    RepairAtomicMutationResult,
    RepairMutationCommand,
)
from src.crm_deal_identity_repair.rollback_models import (
    RepairRollbackAuthorization,
    RepairRollbackCommand,
    RollbackFailureStage,
)
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_rollback import (
    CrmDealIdentityRepairRollbackRepository,
    RepairRollbackAuthorityError,
    RepairRollbackDriftError,
)
from src.graph.crm_deal_identity_repair_verification_run import canonical_source_record_pks_json

T = TypeVar("T")
_FAILURE_STAGES: tuple[RollbackFailureStage, ...] = (
    "after_guard",
    "after_lock",
    "after_compare",
    "after_restore",
    "after_postcondition",
    "after_ledger",
)


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
    uri = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_URI")
    user = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_USER")
    password = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_PASSWORD")
    if uri is None or user is None or password is None:
        pytest.skip("disposable CRM repair Neo4j database is not configured")
    allowed = {"localhost", "127.0.0.1", "::1"}
    service_host = os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_SERVICE_HOST")
    if service_host:
        allowed.add(service_host)
    if urlparse(uri).hostname not in allowed:
        pytest.fail("CRM repair rollback tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    for _ in range(15):
        try:
            driver.verify_connectivity()
            break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    else:
        pytest.fail("disposable CRM repair Neo4j database did not become ready")
    try:
        mutation._reset(driver)
        yield driver
    finally:
        mutation._reset(driver)
        driver.close()


def _rollback_command(driver: Driver) -> RepairRollbackCommand:
    """Create the canonical #309 bundle first, then consume it through #312."""
    mutation._seed_domain(driver, independent_support=True)
    mutation._deactivate_child_contamination(driver)
    item, _ = mutation._inventory(driver)
    policy = "reviewed_rollback_v1"
    reference = "reviewed-312"
    manifest = _canonical_qualification_manifest(reference, policy)
    mutation_command = mutation._seed_authority(
        driver,
        item,
        run_id=str(uuid5(NAMESPACE_URL, manifest.qualification_identity)),
    )
    committed = mutation._repository(driver).commit_atomic_mutation(mutation_command)
    assert committed.mutation is not None
    assert committed.rollback_image is not None
    _seed_canonical_qualification_manifest(driver, mutation_command, manifest)
    authorization = RepairRollbackAuthorization(
        replace(mutation_command.unit, state="applied"),
        mutation_command.fence,
        committed.mutation,
        committed.rollback_image,
        reference,
        "sha256:" + "b" * 64,
        committed.mutation.mutation_id + ":applied:" + committed.rollback_image.rollback_image_id,
        policy,
        "approval-transition-312",
    )
    command = RepairRollbackCommand(authorization)
    _seed_persisted_authorization(driver, command)
    return command


def _canonical_qualification_manifest(
    reference: str, policy: str
) -> RepairExecutionBoundaryManifest:
    return RepairExecutionBoundaryManifest(
        repair_id="repair-312",
        artifact_id="a" * 32,
        artifact_manifest_hmac="b" * 64,
        inventory_digest="sha256:" + "c" * 64,
        repository_sha="d" * 40,
        image_digest="sha256:" + "e" * 64,
        configuration_digest="sha256:" + "f" * 64,
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        environment="staging",
        approval_reference="approval-312",
        unit_ceiling=1,
        stop_conditions=("boundary_drift",),
        source_instance_id=mutation._SOURCE,
        control_instance_id=mutation._CONTROL,
        rollback_authority_reference=reference,
        rollback_authority_policy=policy,
        graph_boundary_digest=mutation._DIGEST,
        inventory_row_count=1,
        eligible_unit_count=1,
        negative_control_count=0,
    )


def _seed_canonical_qualification_manifest(
    driver: Driver,
    mutation_command: RepairMutationCommand,
    manifest: RepairExecutionBoundaryManifest,
    *,
    inventory: tuple[RepairInventoryItem, ...] | None = None,
) -> None:
    """Upgrade the #309 fixture to the canonical #300 qualified-run evidence shape."""
    manifest_json = canonical_json_bytes(manifest.to_dict()).decode("utf-8")
    canonical_inventory = (mutation_command.inventory,) if inventory is None else inventory
    source_record_pks_json = canonical_source_record_pks_json(canonical_inventory)
    values = {
        "manifest_digest": manifest.manifest_digest,
        "artifact_id": manifest.artifact_id,
        "artifact_manifest_hmac": manifest.artifact_manifest_hmac,
        "inventory_digest": manifest.inventory_digest,
        "boundary_digest": manifest.graph_boundary_digest,
        "source_instance_id": manifest.source_instance_id,
        "control_instance_id": manifest.control_instance_id,
        "source_record_pks_json": source_record_pks_json,
        "manifest_json": manifest_json,
        "inventory_row_count": manifest.inventory_row_count,
        "eligible_unit_count": manifest.eligible_unit_count,
        "negative_control_count": manifest.negative_control_count,
        "rollback_authority_reference": manifest.rollback_authority_reference,
        "rollback_authority_policy": manifest.rollback_authority_policy,
        "execution_allowed": manifest.execution_allowed,
    }
    legacy_values = {
        key: value
        for key, value in values.items()
        if key not in {"rollback_authority_reference", "rollback_authority_policy"}
    }
    with driver.session() as session:
        session.run(
            """
            MATCH (run:CrmDealRepairRun {run_id: $run_id})
            SET run += $legacy_values, run.repair_id = $repair_id,
              run.qualification_identity = $qualification_identity
            REMOVE run.rollback_authority_reference, run.rollback_authority_policy
            MERGE (boundary:RepairExecutionBoundary {manifest_digest: $manifest_digest})
            SET boundary += $legacy_values
            MERGE (run)-[:QUALIFIED_WITH]->(boundary)
            """,
            run_id=mutation_command.unit.run_id,
            repair_id=manifest.repair_id,
            qualification_identity=manifest.qualification_identity,
            manifest_digest=manifest.manifest_digest,
            legacy_values=legacy_values,
        ).consume()


def _seed_persisted_authorization(driver: Driver, command: RepairRollbackCommand) -> None:
    auth = command.authorization
    with driver.session() as session:
        session.run(
            """
            CREATE (:CrmDealRepairRollbackAuthorization {
              run_id: $run_id, unit_id: $unit_id,
              authorization_transition_id: $authorization_transition_id,
              authorization_reference: $authorization_reference,
              authorization_token_digest: $authorization_token_digest,
              predecessor_transition_id: $predecessor_transition_id,
              authorization_policy: $authorization_policy,
              generation: $generation, sequence: $sequence, attempt: $attempt,
              boundary_digest: $boundary_digest, fence_id: $fence_id, owner_id: $owner_id,
              fence_token: $fence_token, mutation_id: $mutation_id,
              rollback_image_id: $rollback_image_id, image_digest: $image_digest,
              state: 'approved', consumable: true
            })
            """,
            **auth.to_dict(),
        ).consume()


def _repository(
    driver: Driver, failpoint: Callable[[RollbackFailureStage], None] | None = None
) -> CrmDealIdentityRepairRollbackRepository:
    return CrmDealIdentityRepairRollbackRepository(
        cast(Neo4jClient, _Client(driver)), failpoint=failpoint
    )


def _state(driver: Driver) -> tuple[str, ...]:
    return mutation._graph_state(driver)


def _terminal_counts(driver: Driver, run_id: str) -> dict[str, int]:
    with driver.session() as session:
        row = session.run(
            """
            MATCH (image:CrmDealRepairRollbackImage {run_id: $run_id})
            OPTIONAL MATCH (disposition:CrmDealRepairSecondaryDisposition {run_id: $run_id})
            RETURN count(DISTINCT image) AS images, count(DISTINCT disposition) AS dispositions
            """,
            run_id=run_id,
        ).single(strict=True)
    return {"images": row["images"], "dispositions": row["dispositions"]}


def _rollback_evidence_state(
    driver: Driver, command: RepairRollbackCommand, conflicting_disposition_id: str
) -> dict[str, object]:
    with driver.session() as session:
        row = session.run(
            """
            MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: $unit_id})
            MATCH (image:CrmDealRepairRollbackImage {run_id: $run_id,
              rollback_image_id: $rollback_image_id})
            OPTIONAL MATCH (candidate:CrmDealRepairSecondaryDisposition {run_id: $run_id,
              rollback_image_id: image.rollback_image_id})
            OPTIONAL MATCH (expected:CrmDealRepairSecondaryDisposition {run_id: $run_id,
              disposition_id: $expected_disposition_id})
            RETURN unit.state AS unit_state, image.state AS image_state,
              image.rollback_disposition_id AS image_disposition_id,
              count(DISTINCT candidate) AS candidate_count,
              count(DISTINCT expected) AS expected_terminal_count
            """,
            run_id=command.authorization.unit.run_id,
            unit_id=command.authorization.unit.unit_id,
            rollback_image_id=command.authorization.image.rollback_image_id,
            expected_disposition_id=command.disposition_id,
        ).single(strict=True)
    return dict(row)


def test_exact_rollback_restores_saved_authoritative_root_descendant_and_relationships(
    neo4j_driver: Driver,
) -> None:
    command = _rollback_command(neo4j_driver)
    before = _state(neo4j_driver)
    result = _repository(neo4j_driver).commit_atomic_rollback(command)

    assert result.decision == "restored"
    assert result.image_state == "restored"
    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (old:SourceRecord {source_record_pk: 'deal-pk'})
            MATCH (child:SourceRecord {source_record_pk: 'child-pk'})
            MATCH (replacement:SourceRecord {repair_mutation_id: $mutation_id})
            OPTIONAL MATCH (old)-[old_link:LINKED_TO]->(:Person {person_id: 'person-a'})
            OPTIONAL MATCH (child)-[child_link:LINKED_TO]->(:Person {person_id: 'person-a'})
            OPTIONAL MATCH (replacement)-[new_link:LINKED_TO]->()
            RETURN old.lifecycle_status AS old_status, old.is_latest AS old_latest,
              child.lifecycle_status AS child_status, old_link.is_active AS old_link_active,
              child_link.is_active AS child_link_active, replacement.lifecycle_status AS replacement_status,
              replacement.is_latest AS replacement_latest, collect(DISTINCT new_link.is_active) AS new_link_active
            """,
            mutation_id=command.authorization.mutation.mutation_id,
        ).single(strict=True)
    assert dict(row) == {
        "old_status": "active",
        "old_latest": True,
        "child_status": "active",
        "old_link_active": True,
        # The immutable #309 image was captured after this fixture deliberately
        # retired its child self-support link; rollback restores that exact
        # pre-mutation value rather than an earlier seed value.
        "child_link_active": False,
        "replacement_status": "rolled_back",
        "replacement_latest": False,
        "new_link_active": [False],
    }
    assert _terminal_counts(neo4j_driver, command.authorization.unit.run_id) == {
        "images": 1,
        "dispositions": 1,
    }
    assert _state(neo4j_driver) != before


@pytest.mark.parametrize(
    "mutation",
    [
        "MATCH (:SourceRecord {source_record_pk: 'deal-pk'})-[link:LINKED_TO]->() SET link.owner_id = 'changed'",
        "MATCH (node:SourceRecord {source_record_pk: 'deal-pk'}) SET node.superseded_at = 'not-a-neo4j-datetime'",
        "MATCH (node:SourceRecord {source_record_pk: 'child-pk'}) DETACH DELETE node",
        "MATCH (node:SourceRecord {repair_mutation_id: $mutation_id}) SET node.unexpected = true",
        "MATCH (left:SourceRecord {source_record_pk: 'deal-pk'})-[link:LINKED_TO]->(right) CREATE (left)-[copy:LINKED_TO]->(right) SET copy = properties(link)",
    ],
)
def test_current_state_drift_requires_compensation_without_partial_restore(
    neo4j_driver: Driver,
    mutation: str,
) -> None:
    command = _rollback_command(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(mutation, mutation_id=command.authorization.mutation.mutation_id).consume()
    before = _state(neo4j_driver)
    result = _repository(neo4j_driver).commit_atomic_rollback(command)

    assert result.decision == "reviewed_compensation_required"
    assert result.image_state == "review_required"
    assert result.drift is not None
    assert (
        _state(neo4j_driver) != before
    )  # terminal ledger only; no attempted restoration can erase drift.
    with neo4j_driver.session() as session:
        value = session.run(
            "MATCH (old:SourceRecord {source_record_pk: 'deal-pk'}) RETURN old.lifecycle_status AS state"
        ).single(strict=True)["state"]
    assert value == "superseded"


@pytest.mark.parametrize("stage", _FAILURE_STAGES)
def test_every_rollback_failpoint_is_atomic_against_real_neo4j(
    neo4j_driver: Driver,
    stage: RollbackFailureStage,
) -> None:
    command = _rollback_command(neo4j_driver)
    before = _state(neo4j_driver)

    def fail(observed: RollbackFailureStage) -> None:
        if observed == stage:
            raise RuntimeError("injected rollback failpoint")

    with pytest.raises(RuntimeError, match="injected rollback failpoint"):
        _repository(neo4j_driver, fail).commit_atomic_rollback(command)
    assert _state(neo4j_driver) == before


def test_sequential_and_concurrent_retry_produce_one_terminal_disposition(
    neo4j_driver: Driver,
) -> None:
    command = _rollback_command(neo4j_driver)
    repository = _repository(neo4j_driver)
    first = repository.commit_atomic_rollback(command)
    replay = repository.commit_atomic_rollback(command)
    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent = list(pool.map(lambda _: repository.commit_atomic_rollback(command), range(2)))

    assert first.decision == "restored"
    assert replay.decision == "replayed"
    assert {result.decision for result in concurrent} == {"replayed"}
    assert _terminal_counts(neo4j_driver, command.authorization.unit.run_id) == {
        "images": 1,
        "dispositions": 1,
    }


@pytest.mark.parametrize("terminal_state", ("restored", "review_required"))
@pytest.mark.parametrize("fence_state", ("released", "lost"))
def test_terminal_status_and_replay_survive_completed_fence_lifecycle(
    neo4j_driver: Driver,
    terminal_state: str,
    fence_state: str,
) -> None:
    command = _rollback_command(neo4j_driver)
    if terminal_state == "review_required":
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (:SourceRecord {source_record_pk: 'deal-pk'})-[link:LINKED_TO]->() "
                "SET link.owner_id = 'changed'"
            ).consume()
    result = _repository(neo4j_driver).commit_atomic_rollback(command)
    assert result.decision == (
        "restored" if terminal_state == "restored" else "reviewed_compensation_required"
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (fence:CrmDealRepairFence {run_id: $run_id, fence_id: 'fence-a'}) "
            "SET fence.state = $fence_state",
            run_id=command.authorization.unit.run_id,
            fence_state=fence_state,
        ).consume()
    before = _state(neo4j_driver)

    status = _repository(neo4j_driver).get_rollback_status(command)
    replay = _repository(neo4j_driver).commit_atomic_rollback(command)

    assert status.image_state == terminal_state
    assert replay.decision == "replayed"
    assert replay.original_terminal_decision == (
        "restored" if terminal_state == "restored" else "reviewed_compensation_required"
    )
    assert _state(neo4j_driver) == before
    assert _terminal_counts(neo4j_driver, command.authorization.unit.run_id) == {
        "images": 1,
        "dispositions": 1,
    }


def _transition(
    command: RepairRollbackCommand,
    *,
    reference: str | None = None,
) -> RepairRollbackCommand:
    """Build a model-valid but unpersisted competing approval transition."""
    auth = command.authorization
    return RepairRollbackCommand(
        RepairRollbackAuthorization(
            auth.unit,
            auth.fence,
            auth.mutation,
            auth.image,
            reference or auth.authorization_reference,
            "sha256:" + "c" * 64,
            auth.predecessor_transition_id,
            auth.authorization_policy,
            "other-approval-transition",
        )
    )


def test_concurrent_first_execution_has_one_restore_and_one_verified_replay(
    neo4j_driver: Driver,
) -> None:
    command = _rollback_command(neo4j_driver)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: _repository(neo4j_driver).commit_atomic_rollback(command), range(2))
        )

    assert {result.decision for result in results} == {"restored", "replayed"}
    assert _terminal_counts(neo4j_driver, command.authorization.unit.run_id) == {
        "images": 1,
        "dispositions": 1,
    }


def test_foreign_fence_and_changed_transition_have_no_terminal_write(
    neo4j_driver: Driver,
) -> None:
    command = _rollback_command(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (fence:CrmDealRepairFence {run_id: $run_id, fence_id: 'fence-a'}) "
            "SET fence.token = 'foreign-fence-token'",
            run_id=command.authorization.unit.run_id,
        ).consume()
    with pytest.raises(RepairRollbackAuthorityError):
        _repository(neo4j_driver).commit_atomic_rollback(command)
    assert _terminal_counts(neo4j_driver, command.authorization.unit.run_id) == {
        "images": 1,
        "dispositions": 0,
    }
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (fence:CrmDealRepairFence {run_id: $run_id, fence_id: 'fence-a'}) "
            "SET fence.token = $token",
            run_id=command.authorization.unit.run_id,
            token=command.authorization.fence.token,
        ).consume()

    assert _repository(neo4j_driver).commit_atomic_rollback(command).decision == "restored"
    changed = _transition(command, reference="other-reviewed-transition")
    with pytest.raises(RepairRollbackAuthorityError):
        _repository(neo4j_driver).commit_atomic_rollback(changed)
    assert _terminal_counts(neo4j_driver, command.authorization.unit.run_id) == {
        "images": 1,
        "dispositions": 1,
    }


@pytest.mark.parametrize("linked_from_image", (False, True))
def test_available_image_rejects_conflicting_terminal_evidence_before_restoration(
    neo4j_driver: Driver, linked_from_image: bool
) -> None:
    command = _rollback_command(neo4j_driver)
    conflicting_disposition_id = "conflicting-rollback-terminal"
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (image:CrmDealRepairRollbackImage {run_id: $run_id,
              rollback_image_id: $rollback_image_id})
            CREATE (:CrmDealRepairSecondaryDisposition {run_id: $run_id, unit_id: $unit_id,
              disposition_id: 'verification-secondary', outcome: 'reconciled'})
            CREATE (:CrmDealRepairSecondaryDisposition {run_id: $run_id, unit_id: $unit_id,
              disposition_id: $conflicting_disposition_id,
              rollback_image_id: image.rollback_image_id, outcome: 'reconciled'})
            WITH image
            FOREACH (_ IN CASE WHEN $linked_from_image THEN [1] ELSE [] END |
              SET image.rollback_disposition_id = $conflicting_disposition_id)
            """,
            run_id=command.authorization.unit.run_id,
            unit_id=command.authorization.unit.unit_id,
            rollback_image_id=command.authorization.image.rollback_image_id,
            conflicting_disposition_id=conflicting_disposition_id,
            linked_from_image=linked_from_image,
        ).consume()
    before = _rollback_evidence_state(neo4j_driver, command, conflicting_disposition_id)

    with pytest.raises(
        RepairRollbackDriftError, match="available rollback image has terminal evidence"
    ):
        _repository(neo4j_driver).commit_atomic_rollback(command)

    assert _rollback_evidence_state(neo4j_driver, command, conflicting_disposition_id) == before
    assert before == {
        "unit_state": "applied",
        "image_state": "available",
        "image_disposition_id": conflicting_disposition_id if linked_from_image else None,
        "candidate_count": 1,
        "expected_terminal_count": 0,
    }


@pytest.mark.parametrize(
    "property_name,property_value",
    (
        ("rollback_authority_reference", "conflicting-reviewed-approval"),
        ("manifest_json", "{}"),
    ),
)
def test_legacy_qualification_manifest_corruption_fails_closed(
    neo4j_driver: Driver,
    property_name: str,
    property_value: str,
) -> None:
    command = _rollback_command(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (run:CrmDealRepairRun {run_id: $run_id}) SET run += $properties",
            run_id=command.authorization.unit.run_id,
            properties={property_name: property_value},
        ).consume()
    with pytest.raises(RepairRollbackDriftError):
        _repository(neo4j_driver).commit_atomic_rollback(command)
    assert _terminal_counts(neo4j_driver, command.authorization.unit.run_id) == {
        "images": 1,
        "dispositions": 0,
    }


def test_status_tampering_and_retargeted_person_require_safe_failure(
    neo4j_driver: Driver,
) -> None:
    command = _rollback_command(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (old:SourceRecord {source_record_pk: 'deal-pk'})-[old_link:LINKED_TO]->()
            MATCH (other:Person {person_id: 'person-negative'})
            DELETE old_link
            CREATE (old)-[retargeted:LINKED_TO]->(other)
            SET retargeted.source_record_pk = 'deal-pk', retargeted.is_active = false,
                retargeted.repair_mutation_id = $mutation_id
            """,
            mutation_id=command.authorization.mutation.mutation_id,
        ).consume()
    assert _repository(neo4j_driver).commit_atomic_rollback(command).decision == (
        "reviewed_compensation_required"
    )
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (image:CrmDealRepairRollbackImage {rollback_image_id: $image_id})
            SET image.rollback_status_digest = 'sha256:tampered'
            """,
            image_id=command.authorization.image.rollback_image_id,
        ).consume()
    with pytest.raises(RepairRollbackDriftError):
        _repository(neo4j_driver).get_rollback_status(command)


def _relationship_multiset(driver: Driver, scope: str) -> tuple[str, ...]:
    """Return a fixed-query complete property-map multiset without hidden IDs."""
    with driver.session() as session:
        rows = session.run(
            """
            MATCH (left)-[relationship]->(right)
            WHERE ($scope = 'affected' AND coalesce(relationship.repair_mutation_id, '') = '' AND (
              (left:SourceRecord AND left.source_record_pk IN ['deal-pk', 'child-pk'])
              OR (right:SourceRecord AND right.source_record_pk IN ['deal-pk', 'child-pk'])
              OR relationship.source_record_pk IN ['deal-pk', 'child-pk']
            )) OR ($scope = 'unrelated' AND (
              coalesce(relationship.source_record_pk, '') = 'unrelated'
              OR relationship.lock_id = 'unrelated-lock'
              OR relationship.merge_event_id = 'unrelated-merge'
              OR left.match_decision_id = 'unrelated-decision'
              OR right.match_decision_id = 'unrelated-decision'
            ))
            RETURN labels(left) AS left_labels, properties(left) AS left_properties,
              type(relationship) AS relationship_type,
              properties(relationship) AS relationship_properties,
              labels(right) AS right_labels, properties(right) AS right_properties
            """,
            scope=scope,
        ).data()
    return tuple(
        sorted(json.dumps(row, default=str, sort_keys=True, separators=(",", ":")) for row in rows)
    )


def test_duplicate_multiset_and_digest_located_endpoints_restore_exactly(
    neo4j_driver: Driver,
) -> None:
    mutation._seed_domain(neo4j_driver, independent_support=True)
    mutation._deactivate_child_contamination(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (deal:SourceRecord {source_record_pk: 'deal-pk'}), (person:Person {person_id: 'person-a'})
            CREATE (deal)-[:LINKED_TO {source_record_pk: 'deal-pk', is_active: true,
              owner_id: 'person-a', duplicate_kind: 'property-distinct'}]->(person)
            CREATE (deal)-[:LINKED_TO {source_record_pk: 'deal-pk', is_active: true,
              owner_id: 'person-a', duplicate_kind: 'property-identical'}]->(person)
            CREATE (deal)-[:LINKED_TO {source_record_pk: 'deal-pk', is_active: true,
              owner_id: 'person-a', duplicate_kind: 'property-identical'}]->(person)
            """
        ).consume()
    before = _relationship_multiset(neo4j_driver, "affected")
    item, _ = mutation._inventory(neo4j_driver)
    policy = "reviewed_rollback_v1"
    reference = "reviewed-312"
    manifest = _canonical_qualification_manifest(reference, policy)
    mutation_command = mutation._seed_authority(
        neo4j_driver,
        item,
        run_id=str(uuid5(NAMESPACE_URL, manifest.qualification_identity)),
    )
    committed = mutation._repository(neo4j_driver).commit_atomic_mutation(mutation_command)
    assert committed.mutation is not None and committed.rollback_image is not None
    _seed_canonical_qualification_manifest(neo4j_driver, mutation_command, manifest)
    command = _rollback_command_from_committed(neo4j_driver, mutation_command, committed)

    assert _repository(neo4j_driver).commit_atomic_rollback(command).decision == "restored"
    after = _relationship_multiset(neo4j_driver, "affected")
    assert after == before
    assert sum("property-identical" in row for row in after) == 2
    assert sum("property-distinct" in row for row in after) == 1
    assert any('"source_key":"bitrix_chat"' in row for row in after)
    assert any('"identifier_type":"phone"' in row for row in after)


def _rollback_command_from_committed(
    driver: Driver,
    mutation_command: RepairMutationCommand,
    committed: RepairAtomicMutationResult,
) -> RepairRollbackCommand:
    committed_mutation = committed.mutation
    committed_image = committed.rollback_image
    if committed_mutation is None or committed_image is None:
        raise AssertionError("mutation fixture did not produce a rollback bundle")
    authorization = RepairRollbackAuthorization(
        replace(mutation_command.unit, state="applied"),
        mutation_command.fence,
        committed_mutation,
        committed_image,
        "reviewed-312",
        "sha256:" + "b" * 64,
        committed_mutation.mutation_id + ":applied:" + committed_image.rollback_image_id,
        "reviewed_rollback_v1",
        "approval-transition-312",
    )
    command = RepairRollbackCommand(authorization)
    _seed_persisted_authorization(driver, command)
    return command


def test_rollback_preserves_unrelated_review_identifier_lock_merge_and_consumes_authorization(
    neo4j_driver: Driver,
) -> None:
    command = _rollback_command(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (person:Person {person_id: 'person-a'}), (other:Person {person_id: 'person-negative'})
            CREATE (identifier:Identifier {identifier_type: 'external', identifier_scope: 'global',
              normalized_value: 'unrelated-id'})
            CREATE (decision:MatchDecision {match_decision_id: 'unrelated-decision', decision: 'review'})
            CREATE (review:ReviewCase {review_case_id: 'unrelated-review', queue_state: 'open'})
            CREATE (decision)-[:ABOUT_RIGHT {entity_type: 'person'}]->(person)
            CREATE (review)-[:FOR_DECISION]->(decision)
            CREATE (person)-[:IDENTIFIED_BY {source_record_pk: 'unrelated'}]->(identifier)
            CREATE (person)-[:NO_MATCH_LOCK {lock_id: 'unrelated-lock'}]->(other)
            CREATE (other)-[:MERGED_INTO {merge_event_id: 'unrelated-merge'}]->(person)
            """
        ).consume()
    before = _relationship_multiset(neo4j_driver, "unrelated")
    result = _repository(neo4j_driver).commit_atomic_rollback(command)
    assert result.decision == "restored"
    after = _relationship_multiset(neo4j_driver, "unrelated")
    assert after == before
    with neo4j_driver.session() as session:
        authorization = session.run(
            """
            MATCH (authorization:CrmDealRepairRollbackAuthorization {
              authorization_transition_id: $transition_id
            })
            RETURN authorization.state AS state, authorization.consumable AS consumable,
              authorization.consumed_disposition_id AS disposition_id,
              authorization.consumed_request_digest AS request_digest
            """,
            transition_id=command.authorization.authorization_transition_id,
        ).single(strict=True)
    assert dict(authorization) == {
        "state": "consumed",
        "consumable": False,
        "disposition_id": command.disposition_id,
        "request_digest": command.request_digest,
    }
    replay = _repository(neo4j_driver).commit_atomic_rollback(command)
    assert replay.decision == "replayed"
    assert replay.original_terminal_decision == "restored"


def test_missing_changed_and_new_generation_authorization_boundaries_reject_without_terminal_write(
    neo4j_driver: Driver,
) -> None:
    command = _rollback_command(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (authorization:CrmDealRepairRollbackAuthorization) DELETE authorization"
        ).consume()
    with pytest.raises(RepairRollbackAuthorityError):
        _repository(neo4j_driver).commit_atomic_rollback(command)
    assert _terminal_counts(neo4j_driver, command.authorization.unit.run_id) == {
        "images": 1,
        "dispositions": 0,
    }

    _seed_persisted_authorization(neo4j_driver, command)
    changed = _transition(command, reference="changed-reference")
    with pytest.raises(RepairRollbackAuthorityError):
        _repository(neo4j_driver).commit_atomic_rollback(changed)
    assert _terminal_counts(neo4j_driver, command.authorization.unit.run_id) == {
        "images": 1,
        "dispositions": 0,
    }

    auth = command.authorization
    generation_two = RepairRollbackAuthorization(
        replace(auth.unit, generation=2),
        replace(auth.fence, generation=2),
        replace(auth.mutation, generation=2),
        replace(auth.image, generation=2),
        auth.authorization_reference,
        auth.authorization_token_digest,
        auth.predecessor_transition_id,
        auth.authorization_policy,
        "approval-transition-generation-two",
    )
    with pytest.raises(RepairRollbackAuthorityError):
        _repository(neo4j_driver).commit_atomic_rollback(RepairRollbackCommand(generation_two))
    assert _terminal_counts(neo4j_driver, command.authorization.unit.run_id) == {
        "images": 1,
        "dispositions": 0,
    }
