"""Disposable-Neo4j CAS coverage for the #313 guarded integration boundary."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from threading import Event, Thread
from typing import TypeVar, cast
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

import pytest
from neo4j import Driver, GraphDatabase, ManagedTransaction, Session
from src.crm_deal_identity_repair.control_models import RepairControlRequest
from src.crm_deal_identity_repair.execution_models import RepairQualificationRun
from src.crm_deal_identity_repair.integration_models import (
    IntegrationOperation,
    RepairIntegrationRequest,
    rollback_status_receipt_digest,
)
from src.crm_deal_identity_repair.integration_service import (
    CrmDealIdentityRepairIntegrationService,
    RepairIntegrationAuthority,
    RepairIntegrationContext,
)
from src.crm_deal_identity_repair.mutation_service import CrmDealIdentityRepairMutationService
from src.crm_deal_identity_repair.rollback_models import (
    build_rollback_result_digest,
    build_rollback_status_digest,
)
from src.crm_deal_identity_repair.rollback_service import CrmDealIdentityRepairRollbackService
from src.crm_deal_identity_repair.verification_service import RepairVerificationService
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_integration import CrmDealRepairIntegrationRepository
from src.graph.crm_deal_identity_repair_mutation import CrmDealIdentityRepairMutationRepository
from src.graph.crm_deal_identity_repair_rollback import CrmDealIdentityRepairRollbackRepository
from src.graph.crm_deal_identity_repair_verification import (
    CrmDealIdentityRepairVerificationRepository,
)
from src.graph.queries import crm_deal_identity_repair_integration as queries
from src.graph.queries import crm_deal_identity_repair_rollback as rollback_queries
from test_crm_deal_identity_repair_mutation_neo4j import (
    _CONTROL,
    _deactivate_child_contamination,
    _inventory,
    _seed_authority,
    _seed_domain,
)
from test_crm_deal_identity_repair_mutation_neo4j import (
    _reset as _mutation_reset,
)
from test_crm_deal_identity_repair_rollback_neo4j import (
    _canonical_qualification_manifest,
    _seed_canonical_qualification_manifest,
)

_DIGEST = "sha256:" + "a" * 64


T = TypeVar("T")


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
    if os.getenv("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_SERVICE_HOST") == "neo4j":
        allowed.add("neo4j")
    if urlparse(uri).hostname not in allowed:
        pytest.fail("CRM repair integration tests require an explicitly disposable Neo4j host")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    for _ in range(15):
        try:
            driver.verify_connectivity()
            break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    else:
        pytest.fail("disposable CRM repair Neo4j database did not become ready")
    _mutation_reset(driver)
    try:
        yield driver
    finally:
        _mutation_reset(driver)
        driver.close()


def _params() -> dict[str, str | int]:
    return {
        "repair_id": "repair-313",
        "run_id": "run-313",
        "owner_id": "owner-313",
        "token_digest": _DIGEST,
        "revision": 1,
        "boundary_digest": _DIGEST,
        "qualification_identity": _DIGEST,
        "manifest_digest": _DIGEST,
        "artifact_id": "artifact-313",
        "artifact_manifest_hmac": "b" * 64,
        "manifest_json": "{}",
        "inventory_digest": _DIGEST,
        "inventory_row_count": 0,
        "eligible_unit_count": 0,
        "negative_control_count": 0,
        "source_instance_id": "source-313",
        "control_instance_id": "control-313",
        "completion_id": "completion-313",
        "overlay_digest": _DIGEST,
        "allocation_digest": _DIGEST,
        "allocation_unit_set_digest": _DIGEST,
        "allocation_request_digest": _DIGEST,
        "allocation_origin_key_id": "key-313",
        "allocation_origin_hmac": "c" * 64,
        "allocation_receipt_digest": _DIGEST,
        "allocation_revision": 1,
        "sealed_boundary_digest": _DIGEST,
        "request_digest": _DIGEST,
        "unit_set_digest": _DIGEST,
        "fence_set_digest": _DIGEST,
        "equation_digest": _DIGEST,
        "acceptance_request_digest": _DIGEST,
        "acceptance_receipt_digest": _DIGEST,
        "computed_allocation_unit_set_digest": _DIGEST,
        "expected_secondary_count": 0,
        "observed_secondary_count": 0,
        "reconciled_secondaries": 0,
        "review_required_secondaries": 0,
        "failed_secondaries": 0,
        "pending_secondaries": 0,
        "receipt_bindings": [],
    }


def _seed_zero_unit_run(driver: Driver) -> None:
    values = _params()
    with driver.session() as session:
        session.run(
            """
            CREATE (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id,
              qualification_identity: $qualification_identity, manifest_digest: $manifest_digest,
              artifact_id: $artifact_id, artifact_manifest_hmac: $artifact_manifest_hmac,
              manifest_json: $manifest_json, inventory_digest: $inventory_digest,
              inventory_row_count: $inventory_row_count,
              eligible_unit_count: $eligible_unit_count,
              negative_control_count: $negative_control_count,
              boundary_digest: $boundary_digest,
              source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
              status: 'qualified', execution_allowed: false})
            CREATE (boundary:RepairExecutionBoundary {manifest_digest: $manifest_digest,
              artifact_id: $artifact_id, artifact_manifest_hmac: $artifact_manifest_hmac,
              manifest_json: $manifest_json, inventory_digest: $inventory_digest,
              inventory_row_count: $inventory_row_count,
              eligible_unit_count: $eligible_unit_count,
              negative_control_count: $negative_control_count,
              boundary_digest: $boundary_digest,
              source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
              execution_allowed: false})
            CREATE (run)-[:QUALIFIED_WITH]->(boundary)
            CREATE (:CrmDealRepairControl {repair_id: $repair_id, run_id: $run_id,
              owner_id: $owner_id, token_digest: $token_digest, revision: $revision,
              boundary_digest: $boundary_digest, state: 'allocated', sealed_revision: $revision,
              sealed_boundary_digest: $sealed_boundary_digest,
              sealed_inventory_digest: $inventory_digest})
            CREATE (:BitrixDispatchControl {source_key: 'bitrix_chat',
              control_instance_id: $control_instance_id, blocked: true,
              block_reason: 'crm_deal_identity_repair_quiesce', repair_run_id: $run_id,
              repair_owner_id: $owner_id, repair_token_digest: $token_digest,
              repair_revision: $revision})
            CREATE (:CrmDealRepairAllocationCompletion {run_id: $run_id,
              completion_id: $completion_id, boundary_digest: $boundary_digest,
              overlay_digest: $overlay_digest, allocation_digest: $allocation_digest,
              unit_set_digest: $allocation_unit_set_digest,
              request_digest: $allocation_request_digest, unit_count: 0, unit_ids: [],
              allocation_origin_key_id: $allocation_origin_key_id,
              allocation_origin_hmac: $allocation_origin_hmac,
              receipt_digest: $allocation_receipt_digest,
              allocation_control_instance_id: $control_instance_id,
              allocation_revision: $revision, allocation_state: 'allocated',
              allocation_sealed_boundary_digest: $sealed_boundary_digest,
              receipt_control_instance_id: $control_instance_id, receipt_run_id: $run_id,
              receipt_owner_id: $owner_id, receipt_token_digest: $token_digest,
              receipt_revision: $revision, receipt_state: 'allocated',
              receipt_boundary_digest: $boundary_digest,
              receipt_sealed_boundary_digest: $sealed_boundary_digest})
            """,
            **values,
        ).consume()


def test_zero_unit_acceptance_is_atomic_and_dispatch_neutral(neo4j_driver: Driver) -> None:
    _seed_zero_unit_run(neo4j_driver)
    values = _params()
    with neo4j_driver.session() as session:
        accepted = session.execute_write(
            lambda tx: tx.run(queries.ACCEPT_AND_RELEASE, **values).single(strict=True)
        )
        assert accepted["receipt_digest"] == _DIGEST
        wrong_bindings = values | {
            "receipt_bindings": [{"receipt_id": "wrong-receipt", "receipt_digest": _DIGEST}]
        }
        rejected = session.execute_write(
            lambda tx: tx.run(queries.ACCEPT_AND_RELEASE, **wrong_bindings).single()
        )
        assert rejected is None
        dispatch = session.run(
            "MATCH (d:BitrixDispatchControl) RETURN d.blocked AS blocked, d.block_reason AS reason"
        ).single(strict=True)
        assert dict(dispatch) == {"blocked": True, "reason": "crm_deal_identity_repair_quiesce"}
        session.execute_write(
            lambda tx: tx.run(queries.RELEASE_DISPATCH, **values).single(strict=True)
        )
        released = session.run(
            "MATCH (d:BitrixDispatchControl) RETURN d.blocked AS blocked"
        ).single(strict=True)
        assert released["blocked"] is False


def test_acceptance_lock_requires_every_allocated_unit(neo4j_driver: Driver) -> None:
    _seed_zero_unit_run(neo4j_driver)
    values = _params()
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id}) "
            "SET completion.unit_count = 1, completion.unit_ids = ['missing-unit']",
            **values,
        ).consume()
        locked = session.execute_write(
            lambda tx: tx.run(queries.LOCK_ACCEPTANCE_SCOPE, **values).single()
        )
    assert locked is None


def test_admission_requires_verified_status_for_every_prior_sequence(neo4j_driver: Driver) -> None:
    _seed_zero_unit_run(neo4j_driver)
    values: dict[str, object] = _params() | {
        "unit_id": "unit-b",
        "generation": 1,
        "sequence": 1,
        "attempt": 1,
        "inventory_fingerprint": _DIGEST,
        "inventory_binding_digest": _DIGEST,
        "fence_id": "fence-b",
        "fence_fingerprint": _DIGEST,
    }
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id})
            SET completion.unit_count = 2, completion.unit_ids = ['unit-a', 'unit-b']
            CREATE (:CrmDealRepairUnit {run_id: $run_id, unit_id: 'unit-a', generation: 1,
              sequence: 0, attempt: 1, boundary_digest: $boundary_digest,
              inventory_fingerprint: $inventory_digest, inventory_binding_digest: $inventory_digest,
              state: 'allocated'})
            CREATE (:CrmDealRepairUnit {run_id: $run_id, unit_id: 'unit-b', generation: 1,
              sequence: 1, attempt: 1, boundary_digest: $boundary_digest,
              inventory_fingerprint: $inventory_digest, inventory_binding_digest: $inventory_digest,
              state: 'allocated'})
            """,
            **values,
        ).consume()
        assert (
            session.execute_write(
                lambda tx: tx.run(queries.CLAIM_ADMITTED_FENCE, **values).single()
            )
            is None
        )
        session.run(
            "MATCH (unit:CrmDealRepairUnit {run_id: $run_id, unit_id: 'unit-a'}) "
            "SET unit.state = 'applied'",
            **values,
        ).consume()
        assert (
            session.execute_write(
                lambda tx: tx.run(queries.CLAIM_ADMITTED_FENCE, **values).single()
            )
            is None
        )
        session.run(
            """
            CREATE (:CrmDealRepairVerification {run_id: $run_id, unit_id: 'unit-a', generation: 1,
              sequence: 0, attempt: 1, owner_id: $owner_id, fence_token: $token_digest,
              boundary_digest: $boundary_digest, outcome: 'verified'})
            """,
            **values,
        ).consume()
        assert (
            session.execute_write(
                lambda tx: tx.run(queries.CLAIM_ADMITTED_FENCE, **values).single()
            )
            is None
        )
        session.run(
            """
            CREATE (:CrmDealRepairFence {run_id: $run_id, unit_id: 'unit-a', fence_id: 'fence-a',
              generation: 1, sequence: 0, attempt: 1, owner_id: $owner_id,
              token: $token_digest, boundary_digest: $boundary_digest,
              fence_fingerprint: $inventory_digest, state: 'claimed'})
            CREATE (:CrmDealRepairMutationResult {run_id: $run_id, unit_id: 'unit-a',
              mutation_id: 'mutation-a', rollback_image_id: 'image-a',
              rollback_image_digest: $inventory_digest, generation: 1, sequence: 0, attempt: 1,
              owner_id: $owner_id, fence_token: $token_digest, boundary_digest: $boundary_digest})
            CREATE (:CrmDealRepairRollbackImage {run_id: $run_id, unit_id: 'unit-a',
              rollback_image_id: 'image-a', image_digest: $inventory_digest, generation: 1,
              sequence: 0, attempt: 1, owner_id: $owner_id, fence_token: $token_digest,
              boundary_digest: $boundary_digest, state: 'available'})
            CREATE (:CrmDealRepairRollbackAuthorization {run_id: $run_id, unit_id: 'unit-a',
              authorization_transition_id: 'authorization-a',
              authorization_digest: $inventory_digest, mutation_id: 'mutation-a',
              rollback_image_id: 'image-a', image_digest: $inventory_digest, generation: 1,
              sequence: 0, attempt: 1, owner_id: $owner_id, fence_token: $token_digest,
              boundary_digest: $boundary_digest, fence_id: 'fence-a',
              predecessor_transition_id: 'mutation-a:applied:image-a', state: 'approved',
              consumable: true})
            CREATE (:CrmDealRepairRollbackReceipt {run_id: $run_id, receipt_id: 'receipt-foreign',
              unit_id: 'unit-a', fence_id: 'foreign-fence', mutation_id: 'mutation-a',
              image_digest: $inventory_digest, authorization_transition_id: 'authorization-a',
              authorization_digest: $inventory_digest, generation: 1, sequence: 0, attempt: 1,
              control_revision: $revision, allocation_revision: $allocation_revision,
              completion_id: $completion_id, status_digest: $inventory_digest, state: 'available'})
            """,
            **values,
        ).consume()
        assert (
            session.execute_write(
                lambda tx: tx.run(queries.CLAIM_ADMITTED_FENCE, **values).single()
            )
            is None
        )
        session.run(
            "MATCH (receipt:CrmDealRepairRollbackReceipt {run_id: $run_id, "
            "receipt_id: 'receipt-foreign'}) DELETE receipt",
            **values,
        ).consume()
        session.run(
            """
            CREATE (:CrmDealRepairRollbackReceipt {run_id: $run_id, receipt_id: 'receipt-a',
              unit_id: 'unit-a', fence_id: 'fence-a', mutation_id: 'mutation-a',
              image_digest: $inventory_digest, authorization_transition_id: 'authorization-a',
              authorization_digest: $inventory_digest, generation: 1, sequence: 0, attempt: 1,
              control_revision: $revision, allocation_revision: $allocation_revision,
              completion_id: $completion_id, status_digest: $inventory_digest, state: 'available'})
            """,
            **values,
        ).consume()
        admitted = session.execute_write(
            lambda tx: tx.run(queries.CLAIM_ADMITTED_FENCE, **values).single(strict=True)
        )
    assert admitted["fence"]["unit_id"] == "unit-b"


def test_dispatch_release_replay_does_not_clear_a_replaced_block(neo4j_driver: Driver) -> None:
    _seed_zero_unit_run(neo4j_driver)
    values = _params()
    with neo4j_driver.session() as session:
        session.execute_write(
            lambda tx: tx.run(queries.ACCEPT_AND_RELEASE, **values).single(strict=True)
        )
        session.execute_write(
            lambda tx: tx.run(queries.RELEASE_DISPATCH, **values).single(strict=True)
        )
        session.run(
            "MATCH (d:BitrixDispatchControl) SET d.blocked = true, d.block_reason = 'other', "
            "d.repair_run_id = 'other-run'"
        ).consume()
        session.execute_write(
            lambda tx: tx.run(queries.RELEASE_DISPATCH, **values).single(strict=True)
        )
        row = session.run(
            "MATCH (d:BitrixDispatchControl) "
            "RETURN d.blocked AS blocked, d.block_reason AS reason, d.repair_run_id AS run_id"
        ).single(strict=True)
    assert dict(row) == {"blocked": True, "reason": "other", "run_id": "other-run"}


def test_release_rejects_tampered_completion_without_clearing_dispatch(
    neo4j_driver: Driver,
) -> None:
    _seed_zero_unit_run(neo4j_driver)
    values = _params()
    with neo4j_driver.session() as session:
        session.execute_write(
            lambda tx: tx.run(queries.ACCEPT_AND_RELEASE, **values).single(strict=True)
        )
        session.run(
            "MATCH (completion:CrmDealRepairAllocationCompletion) "
            "SET completion.allocation_digest = $replacement_digest",
            replacement_digest="sha256:" + "b" * 64,
        ).consume()
        rejected = session.execute_write(
            lambda tx: tx.run(queries.RELEASE_DISPATCH, **values).single()
        )
        dispatch = session.run(
            "MATCH (d:BitrixDispatchControl) RETURN d.blocked AS blocked"
        ).single(strict=True)
    assert rejected is None
    assert dispatch["blocked"] is True


def test_queries_keep_acceptance_dispatch_neutral_and_release_exact() -> None:
    assert "SET dispatch.blocked = false" not in queries.ACCEPT_AND_RELEASE
    for predicate in (
        "dispatch.block_reason = 'crm_deal_identity_repair_quiesce'",
        "dispatch.repair_run_id = $run_id",
        "dispatch.repair_owner_id = $owner_id",
        "dispatch.repair_token_digest = $token_digest",
        "dispatch.repair_revision = $revision",
    ):
        assert predicate in queries.RELEASE_DISPATCH


def _seed_one_unit_acceptance(driver: Driver) -> dict[str, object]:
    _seed_zero_unit_run(driver)
    receipt_digest = rollback_status_receipt_digest(
        run_id="run-313",
        unit_id="unit-a",
        receipt_id="receipt-a",
        fence_id="fence-a",
        mutation_id="mutation-a",
        image_digest=_DIGEST,
        authorization_transition_id="authorization-a",
        authorization_digest=_DIGEST,
        status_digest=_DIGEST,
        control_revision=1,
        allocation_revision=1,
        completion_id="completion-313",
        generation=1,
        sequence=0,
        attempt=1,
    )
    values: dict[str, object] = _params() | {
        "unit_set_digest": _DIGEST,
        "computed_allocation_unit_set_digest": _DIGEST,
        "receipt_bindings": [{"receipt_id": "receipt-a", "receipt_digest": receipt_digest}],
        "rollback_receipt_digest": receipt_digest,
    }

    with driver.session() as session:
        session.run(
            """
            MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id})
            SET completion.unit_count = 1, completion.unit_ids = ['unit-a']
            CREATE (:CrmDealRepairUnit {run_id: $run_id, unit_id: 'unit-a', generation: 1,
              sequence: 0, attempt: 1, boundary_digest: $boundary_digest,
              inventory_fingerprint: $inventory_digest, state: 'applied'})
            CREATE (:CrmDealRepairFence {run_id: $run_id, unit_id: 'unit-a', fence_id: 'fence-a',
              generation: 1, sequence: 0, attempt: 1, owner_id: $owner_id,
              token: $token_digest, boundary_digest: $boundary_digest,
              fence_fingerprint: $inventory_digest, state: 'claimed'})
            CREATE (:CrmDealRepairMutationResult {run_id: $run_id, unit_id: 'unit-a',
              mutation_id: 'mutation-a', rollback_image_id: 'image-a',
              rollback_image_digest: $inventory_digest, generation: 1, sequence: 0, attempt: 1,
              owner_id: $owner_id, fence_token: $token_digest, boundary_digest: $boundary_digest})
            CREATE (:CrmDealRepairRollbackImage {run_id: $run_id, unit_id: 'unit-a',
              rollback_image_id: 'image-a', image_digest: $inventory_digest, generation: 1,
              sequence: 0, attempt: 1, owner_id: $owner_id, fence_token: $token_digest,
              boundary_digest: $boundary_digest, state: 'available'})
            CREATE (:CrmDealRepairVerification {run_id: $run_id, unit_id: 'unit-a', generation: 1,
              sequence: 0, attempt: 1, owner_id: $owner_id, fence_token: $token_digest,
              boundary_digest: $boundary_digest, outcome: 'verified'})
            CREATE (:CrmDealRepairRollbackAuthorization {run_id: $run_id, unit_id: 'unit-a',
              authorization_transition_id: 'authorization-a',
              authorization_digest: $inventory_digest, mutation_id: 'mutation-a',
              rollback_image_id: 'image-a', image_digest: $inventory_digest,
              generation: 1, sequence: 0, attempt: 1, boundary_digest: $boundary_digest,
              owner_id: $owner_id, fence_token: $token_digest, fence_id: 'fence-a',
              state: 'approved', consumable: true})
            CREATE (:CrmDealRepairRollbackReceipt {run_id: $run_id, receipt_id: 'receipt-a',
              unit_id: 'unit-a', fence_id: 'fence-a', image_digest: $inventory_digest,
              mutation_id: 'mutation-a', authorization_transition_id: 'authorization-a',
              authorization_digest: $inventory_digest, generation: 1, sequence: 0, attempt: 1,
              control_revision: $revision, allocation_revision: $revision,
              completion_id: $completion_id, status_digest: $inventory_digest,
              receipt_digest: $rollback_receipt_digest, state: 'available'})
            """,
            **values,
        ).consume()
    return values


@pytest.mark.parametrize(
    "corruption",
    (
        "CREATE (:CrmDealRepairRollbackReceipt {run_id: $run_id, receipt_id: 'receipt-extra'})",
        "MATCH (receipt:CrmDealRepairRollbackReceipt {run_id: $run_id}) "
        "SET receipt.authorization_digest = $replacement_digest",
        "MATCH (receipt:CrmDealRepairRollbackReceipt {run_id: $run_id}) "
        "SET receipt.status_digest = 'invalid'",
    ),
)
def test_acceptance_rejects_corrupt_or_duplicate_rollback_receipts(
    neo4j_driver: Driver, corruption: str
) -> None:
    values = _seed_one_unit_acceptance(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(corruption, **(values | {"replacement_digest": "sha256:" + "b" * 64})).consume()
        rejected = session.execute_write(
            lambda tx: tx.run(queries.ACCEPT_AND_RELEASE, **values).single()
        )
        fence = session.run(
            "MATCH (f:CrmDealRepairFence {run_id: $run_id}) RETURN f.state AS state", **values
        ).single(strict=True)
    assert rejected is None
    assert fence["state"] == "claimed"


def test_acceptance_allows_balanced_successful_secondary_dispositions(
    neo4j_driver: Driver,
) -> None:
    values = _seed_one_unit_acceptance(neo4j_driver) | {
        "expected_secondary_count": 1,
        "observed_secondary_count": 1,
        "reconciled_secondaries": 1,
    }
    with neo4j_driver.session() as session:
        session.run(
            """
            CREATE (:CrmDealRepairSecondaryDisposition {run_id: $run_id, unit_id: 'unit-a',
              disposition_id: 'secondary-a', generation: 1, sequence: 0, attempt: 1,
              owner_id: $owner_id, control_token: $token_digest, boundary_digest: $boundary_digest,
              subject_fingerprint: $inventory_digest, evidence_digest: $inventory_digest,
              payload_digest: $inventory_digest, outcome: 'reconciled'})
            """,
            **values,
        ).consume()
        accepted = session.execute_write(
            lambda tx: tx.run(queries.ACCEPT_AND_RELEASE, **values).single(strict=True)
        )
    assert accepted["receipt_digest"] == _DIGEST


def test_acceptance_rejects_outside_secondary_disposition(neo4j_driver: Driver) -> None:
    values = _seed_one_unit_acceptance(neo4j_driver)
    with neo4j_driver.session() as session:
        session.run(
            """
            CREATE (:CrmDealRepairSecondaryDisposition {run_id: $run_id, unit_id: 'outside-unit',
              disposition_id: 'outside-secondary', generation: 1, sequence: 99, attempt: 1,
              owner_id: $owner_id, control_token: $token_digest, boundary_digest: $boundary_digest,
              subject_fingerprint: $inventory_digest, evidence_digest: $inventory_digest,
              payload_digest: $inventory_digest, outcome: 'reconciled'})
            """,
            **values,
        ).consume()
        rejected = session.execute_write(
            lambda tx: tx.run(queries.ACCEPT_AND_RELEASE, **values).single()
        )
        fence = session.run(
            "MATCH (f:CrmDealRepairFence {run_id: $run_id, fence_id: 'fence-a'}) "
            "RETURN f.state AS state",
            **values,
        ).single(strict=True)
    assert rejected is None
    assert fence["state"] == "claimed"


def _create_outside_unit_and_fence(session: Session, values: dict[str, object]) -> None:
    session.run(
        """
        CREATE (:CrmDealRepairUnit {run_id: $run_id, unit_id: 'outside-unit', generation: 1,
          sequence: 99, attempt: 1, boundary_digest: $boundary_digest, state: 'allocated'})
        CREATE (:CrmDealRepairFence {run_id: $run_id, unit_id: 'outside-unit',
          fence_id: 'outside-fence', generation: 1, sequence: 99, attempt: 1,
          owner_id: $owner_id, token: $token_digest, boundary_digest: $boundary_digest,
          fence_fingerprint: $inventory_digest, state: 'claimed'})
        """,
        **values,
    ).consume()


def test_zero_unit_acceptance_rejects_outside_run_scoped_records(neo4j_driver: Driver) -> None:
    _seed_zero_unit_run(neo4j_driver)
    values = _params()
    with neo4j_driver.session() as session:
        _create_outside_unit_and_fence(session, values)
        rejected = session.execute_write(
            lambda tx: tx.run(queries.ACCEPT_AND_RELEASE, **values).single()
        )
        row = session.run(
            "MATCH (d:BitrixDispatchControl) "
            "OPTIONAL MATCH (a:CrmDealRepairAcceptance) "
            "RETURN d.blocked AS blocked, count(a) AS acceptances"
        ).single(strict=True)
    assert dict(row) == {"blocked": True, "acceptances": 0}
    assert rejected is None


def test_partial_allocation_acceptance_rejects_outside_run_scoped_records(
    neo4j_driver: Driver,
) -> None:
    values = _seed_one_unit_acceptance(neo4j_driver)
    with neo4j_driver.session() as session:
        _create_outside_unit_and_fence(session, values)
        rejected = session.execute_write(
            lambda tx: tx.run(queries.ACCEPT_AND_RELEASE, **values).single()
        )
        row = session.run(
            "MATCH (f:CrmDealRepairFence {run_id: $run_id, fence_id: 'fence-a'}) "
            "RETURN f.state AS state",
            **values,
        ).single(strict=True)
    assert rejected is None
    assert row["state"] == "claimed"


def test_multi_unit_acceptance_rejects_outside_run_scoped_records(neo4j_driver: Driver) -> None:
    values = _seed_one_unit_acceptance(neo4j_driver)
    receipt_b_digest = rollback_status_receipt_digest(
        run_id="run-313",
        unit_id="unit-b",
        receipt_id="receipt-b",
        fence_id="fence-b",
        mutation_id="mutation-b",
        image_digest=_DIGEST,
        authorization_transition_id="authorization-b",
        authorization_digest=_DIGEST,
        status_digest=_DIGEST,
        control_revision=1,
        allocation_revision=1,
        completion_id="completion-313",
        generation=1,
        sequence=1,
        attempt=1,
    )
    multi_values = values | {
        "receipt_bindings": [
            {"receipt_id": "receipt-a", "receipt_digest": values["rollback_receipt_digest"]},
            {"receipt_id": "receipt-b", "receipt_digest": receipt_b_digest},
        ],
        "rollback_receipt_b_digest": receipt_b_digest,
    }
    with neo4j_driver.session() as session:
        session.run(
            """
            MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id})
            SET completion.unit_count = 2, completion.unit_ids = ['unit-a', 'unit-b']
            CREATE (:CrmDealRepairUnit {run_id: $run_id, unit_id: 'unit-b', generation: 1,
              sequence: 1, attempt: 1, boundary_digest: $boundary_digest,
              inventory_fingerprint: $inventory_digest, state: 'applied'})
            CREATE (:CrmDealRepairFence {run_id: $run_id, unit_id: 'unit-b', fence_id: 'fence-b',
              generation: 1, sequence: 1, attempt: 1, owner_id: $owner_id,
              token: $token_digest, boundary_digest: $boundary_digest,
              fence_fingerprint: $inventory_digest, state: 'claimed'})
            CREATE (:CrmDealRepairMutationResult {run_id: $run_id, unit_id: 'unit-b',
              mutation_id: 'mutation-b', rollback_image_id: 'image-b',
              rollback_image_digest: $inventory_digest, generation: 1, sequence: 1, attempt: 1,
              owner_id: $owner_id, fence_token: $token_digest, boundary_digest: $boundary_digest})
            CREATE (:CrmDealRepairRollbackImage {run_id: $run_id, unit_id: 'unit-b',
              rollback_image_id: 'image-b', image_digest: $inventory_digest, generation: 1,
              sequence: 1, attempt: 1, owner_id: $owner_id, fence_token: $token_digest,
              boundary_digest: $boundary_digest, state: 'available'})
            CREATE (:CrmDealRepairVerification {run_id: $run_id, unit_id: 'unit-b', generation: 1,
              sequence: 1, attempt: 1, owner_id: $owner_id, fence_token: $token_digest,
              boundary_digest: $boundary_digest, outcome: 'verified'})
            CREATE (:CrmDealRepairRollbackAuthorization {run_id: $run_id, unit_id: 'unit-b',
              authorization_transition_id: 'authorization-b',
              authorization_digest: $inventory_digest, mutation_id: 'mutation-b',
              rollback_image_id: 'image-b', image_digest: $inventory_digest,
              generation: 1, sequence: 1, attempt: 1, boundary_digest: $boundary_digest,
              owner_id: $owner_id, fence_token: $token_digest, fence_id: 'fence-b',
              state: 'approved', consumable: true})
            CREATE (:CrmDealRepairRollbackReceipt {run_id: $run_id, receipt_id: 'receipt-b',
              unit_id: 'unit-b', fence_id: 'fence-b', image_digest: $inventory_digest,
              mutation_id: 'mutation-b', authorization_transition_id: 'authorization-b',
              authorization_digest: $inventory_digest, generation: 1, sequence: 1, attempt: 1,
              control_revision: $revision, allocation_revision: $revision,
              completion_id: $completion_id, status_digest: $inventory_digest,
              receipt_digest: $rollback_receipt_b_digest, state: 'available'})
            """,
            **multi_values,
        ).consume()
        _create_outside_unit_and_fence(session, multi_values)
        rejected = session.execute_write(
            lambda tx: tx.run(queries.ACCEPT_AND_RELEASE, **multi_values).single()
        )
        rows = session.run(
            "MATCH (f:CrmDealRepairFence {run_id: $run_id}) "
            "WHERE f.unit_id IN ['unit-a', 'unit-b'] "
            "WITH f ORDER BY f.unit_id RETURN collect(f.state) AS states",
            **multi_values,
        ).single(strict=True)
    assert rejected is None
    assert rows["states"] == ["claimed", "claimed"]


def _service(
    driver: Driver,
) -> tuple[CrmDealIdentityRepairIntegrationService, RepairIntegrationContext]:
    """Seed #309 authority, then compose #313 around the real component repositories."""
    _seed_domain(driver, independent_support=True)
    with driver.session() as session:
        session.run(
            "MATCH (person:Person) SET person.crm_deal_count = coalesce(person.crm_deal_count, 0), "
            "person.analysis_input_revision = coalesce(person.analysis_input_revision, 0), "
            "person.golden_profile_version = coalesce(person.golden_profile_version, 0), "
            "person.profile_completeness_score = coalesce(person.profile_completeness_score, 0)"
        ).consume()
    _deactivate_child_contamination(driver)
    item, negative = _inventory(driver)
    manifest = _canonical_qualification_manifest("reviewed-312", "reviewed_rollback_v1")
    run_id = str(uuid5(NAMESPACE_URL, manifest.qualification_identity))
    mutation_command = _seed_authority(driver, item, run_id=run_id)
    _seed_canonical_qualification_manifest(driver, mutation_command, manifest)
    control = RepairControlRequest(manifest.repair_id, run_id, "worker-a", "operator-secret", 1)
    from src.crm_deal_identity_repair.digests import object_digest

    unit_set_digest = object_digest(
        b"crm-deal-identity-repair-allocation-unit-set-v1\x00",
        {"units": [asdict(mutation_command.unit)]},
    )
    authority = RepairIntegrationAuthority(
        "completion-313",
        _DIGEST,
        _DIGEST,
        unit_set_digest,
        _DIGEST,
        "key-313",
        "c" * 64,
        _DIGEST,
        _DIGEST,
        1,
    )
    with driver.session() as session:
        session.run(
            "MATCH (f:CrmDealRepairFence {run_id: $run_id}) DELETE f", run_id=run_id
        ).consume()
        session.run(
            "MATCH (d:BitrixDispatchControl {control_instance_id: $control}) "
            "SET d.block_reason = 'crm_deal_identity_repair_quiesce', d.repair_run_id = $run_id, "
            "d.repair_owner_id = $owner, d.repair_token_digest = $token, d.repair_revision = 1",
            control=_CONTROL,
            run_id=run_id,
            owner=control.owner_id,
            token=control.token_digest,
        ).consume()
        session.run(
            "CREATE (:CrmDealRepairControl {repair_id: $repair_id, run_id: $run_id, "
            "owner_id: $owner, token_digest: $token, revision: 1, boundary_digest: $boundary, "
            "state: 'allocated', sealed_revision: 1, sealed_boundary_digest: $boundary, "
            "sealed_inventory_digest: $inventory})",
            repair_id=manifest.repair_id,
            run_id=run_id,
            owner=control.owner_id,
            token=control.token_digest,
            boundary=manifest.graph_boundary_digest,
            inventory=manifest.inventory_digest,
        ).consume()
        session.run(
            "CREATE (:CrmDealRepairAllocationCompletion "
            "{run_id: $run_id, completion_id: 'completion-313', "
            "boundary_digest: $boundary, overlay_digest: $overlay, "
            "allocation_digest: $allocation, unit_set_digest: $unit_set, "
            "request_digest: $request, unit_count: 1, unit_ids: ['unit-a'], "
            "allocation_origin_key_id: 'key-313', allocation_origin_hmac: $origin, "
            "receipt_digest: $receipt, allocation_control_instance_id: $control, "
            "allocation_revision: 1, allocation_state: 'allocated', "
            "allocation_sealed_boundary_digest: $boundary, "
            "receipt_control_instance_id: $control, receipt_run_id: $run_id, "
            "receipt_owner_id: $owner, receipt_token_digest: $token, "
            "receipt_revision: 1, receipt_state: 'allocated', "
            "receipt_boundary_digest: $boundary, "
            "receipt_sealed_boundary_digest: $boundary})",
            run_id=run_id,
            boundary=manifest.graph_boundary_digest,
            overlay=_DIGEST,
            allocation=_DIGEST,
            unit_set=unit_set_digest,
            request=_DIGEST,
            origin="c" * 64,
            receipt=_DIGEST,
            control=_CONTROL,
            owner=control.owner_id,
            token=control.token_digest,
        ).consume()
    context = RepairIntegrationContext(
        RepairQualificationRun(
            manifest.repair_id,
            run_id,
            manifest.qualification_identity,
            manifest,
            manifest.graph_boundary_digest,
            "qualified",
        ),
        tuple(sorted((item, negative), key=lambda row: row.inventory_key)),
        authority,
    )
    client = cast(Neo4jClient, _Client(driver))
    service = CrmDealIdentityRepairIntegrationService(
        CrmDealRepairIntegrationRepository(client),
        lambda _: context,
        CrmDealIdentityRepairMutationService(CrmDealIdentityRepairMutationRepository(client)),
        RepairVerificationService(CrmDealIdentityRepairVerificationRepository(client)),
        CrmDealIdentityRepairRollbackService(CrmDealIdentityRepairRollbackRepository(client)),
        lambda: "sha256:" + "d" * 64,
    )
    return service, context


def _request(
    operation: IntegrationOperation, context: RepairIntegrationContext
) -> RepairIntegrationRequest:
    control = RepairControlRequest(
        context.run.repair_id, context.run.run_id, "worker-a", "operator-secret", 1
    )
    kwargs: dict[str, str] = {}
    if operation in {"rollback-status", "rollback"}:
        kwargs = {"authorization_reference": "reviewed-312", "predecessor_transition_id": "ignored"}
    return RepairIntegrationRequest(
        operation,
        control,
        "approval-313",
        "unit-a" if operation not in {"accept", "release-dispatch"} else None,
        **kwargs,
    )


def _rollback_request(
    driver: Driver,
    operation: IntegrationOperation,
    context: RepairIntegrationContext,
    authorization_reference: str = "reviewed-312",
) -> RepairIntegrationRequest:
    with driver.session() as session:
        row = session.run(
            """
            MATCH (result:CrmDealRepairMutationResult {run_id: $run_id, unit_id: 'unit-a'})
            RETURN result.mutation_id AS mutation_id, result.rollback_image_id AS rollback_image_id
            """,
            run_id=context.run.run_id,
        ).single(strict=True)
    predecessor = f"{row['mutation_id']}:applied:{row['rollback_image_id']}"
    return RepairIntegrationRequest(
        operation,
        RepairControlRequest(
            context.run.repair_id, context.run.run_id, "worker-a", "operator-secret", 1
        ),
        "approval-313",
        "unit-a",
        authorization_reference=authorization_reference,
        predecessor_transition_id=predecessor,
    )


def test_shared_service_real_apply_replay_verify_and_rollback_status(neo4j_driver: Driver) -> None:
    service, context = _service(neo4j_driver)
    applied = service.execute(_request("apply", context))
    replay = service.execute(_request("apply", context))
    verified = service.execute(_request("verify", context))
    status = service.execute(_rollback_request(neo4j_driver, "rollback-status", context))
    assert applied.state in {"committed", "replayed"}
    assert replay.state == "replayed"
    assert verified.state in {"committed", "replayed"}
    assert status.state == "available"


def test_shared_service_real_rollback_after_verification(neo4j_driver: Driver) -> None:
    service, context = _service(neo4j_driver)
    service.execute(_request("apply", context))
    service.execute(_request("verify", context))
    service.execute(_rollback_request(neo4j_driver, "rollback-status", context))
    result = service.execute(_rollback_request(neo4j_driver, "rollback", context))
    replay = service.execute(_rollback_request(neo4j_driver, "rollback", context))
    assert result.state in {"restored", "reviewed_compensation_required", "replayed"}
    assert replay.state == "replayed"
    with pytest.raises(RuntimeError, match="fence"):
        service.execute(
            _rollback_request(
                neo4j_driver, "rollback", context, authorization_reference="different"
            )
        )


def test_shared_service_rejects_verify_before_apply(neo4j_driver: Driver) -> None:
    service, context = _service(neo4j_driver)
    with pytest.raises(RuntimeError, match="fence"):
        service.execute(_request("verify", context))


def test_shared_service_real_rollback_before_verification(neo4j_driver: Driver) -> None:
    service, context = _service(neo4j_driver)
    service.execute(_request("apply", context))
    service.execute(_rollback_request(neo4j_driver, "rollback-status", context))
    result = service.execute(_rollback_request(neo4j_driver, "rollback", context))
    assert result.state in {"restored", "reviewed_compensation_required", "replayed"}


def test_shared_service_accept_and_release_after_verified_happy_path(neo4j_driver: Driver) -> None:
    service, context = _service(neo4j_driver)
    service.execute(_request("apply", context))
    service.execute(_request("verify", context))
    service.execute(_rollback_request(neo4j_driver, "rollback-status", context))
    accepted = service.execute(_request("accept", context))
    released = service.execute(_request("release-dispatch", context))
    assert accepted.state == "accepted"
    assert released.state == "released"
    assert service.execute(_request("release-dispatch", context)).state == "released"


def test_acceptance_waits_for_rollback_lock_and_rejects_terminal_rollback(
    neo4j_driver: Driver,
) -> None:
    service, context = _service(neo4j_driver)
    service.execute(_request("apply", context))
    service.execute(_request("verify", context))
    service.execute(_rollback_request(neo4j_driver, "rollback-status", context))
    rollback_request = _rollback_request(neo4j_driver, "rollback", context)
    command = service._rollback_command(rollback_request, context)
    authorization = command.authorization
    rollback_params = authorization.to_dict() | {
        "unit_fingerprint": authorization.unit.inventory_fingerprint,
        "rollback_request_digest": command.request_digest,
    }
    result_digest = build_rollback_result_digest(command, "restored", "restored", None)
    status_digest = build_rollback_status_digest(
        command,
        "restored",
        command.disposition_id,
        "restored",
        result_digest,
        None,
    )
    terminal_params = rollback_params | {
        "disposition_id": command.disposition_id,
        "evidence_digest": authorization.image.evidence_digest,
        "payload_digest": authorization.image.payload_digest,
        "result_digest": result_digest,
        "image_state": "restored",
        "unit_state": "rolled_back",
        "outcome": "reconciled",
        "drift_total_mismatch_count": 0,
        "drift_summaries_json": "[]",
        "drift_complete_digest": None,
        "status_digest": status_digest,
    }
    rollback_locked = Event()
    acceptance_started = Event()
    acceptance_finished = Event()
    rollback_errors: list[BaseException] = []
    acceptance_errors: list[BaseException] = []

    def rollback() -> None:
        transaction = None
        try:
            with neo4j_driver.session() as session:
                transaction = session.begin_transaction()
                locked = transaction.run(
                    rollback_queries.LOCK_AND_READ_ROLLBACK_BUNDLE, **rollback_params
                ).single(strict=True)
                assert locked["unit"]["unit_id"] == authorization.unit.unit_id
                rollback_locked.set()
                assert acceptance_started.wait(timeout=10)
                assert not acceptance_finished.wait(timeout=2)
                terminal = transaction.run(
                    rollback_queries.PERSIST_ROLLBACK_TERMINAL, **terminal_params
                ).single(strict=True)
                assert terminal["disposition"]["disposition_id"] == command.disposition_id
                transaction.commit()
        except BaseException as exc:  # noqa: BLE001
            if transaction is not None:
                transaction.rollback()
            rollback_errors.append(exc)

    def accept() -> None:
        try:
            assert rollback_locked.wait(timeout=10)
            acceptance_started.set()
            service.execute(_request("accept", context))
        except BaseException as exc:  # noqa: BLE001
            acceptance_errors.append(exc)
        finally:
            acceptance_finished.set()

    threads = (Thread(target=rollback), Thread(target=accept))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert not rollback_errors
    assert len(acceptance_errors) == 1
    with neo4j_driver.session() as session:
        row = session.run(
            """
            OPTIONAL MATCH (acceptance:CrmDealRepairAcceptance {run_id: $run_id})
            OPTIONAL MATCH (image:CrmDealRepairRollbackImage {run_id: $run_id})
            OPTIONAL MATCH (disposition:CrmDealRepairSecondaryDisposition {run_id: $run_id})
            RETURN count(DISTINCT acceptance) AS acceptances,
              count(DISTINCT image) AS images,
              count(DISTINCT disposition) AS dispositions
            """,
            run_id=context.run.run_id,
        ).single(strict=True)
    assert dict(row) == {"acceptances": 0, "images": 1, "dispositions": 1}


def test_dispatch_replacement_wins_against_waiting_release(neo4j_driver: Driver) -> None:
    _seed_zero_unit_run(neo4j_driver)
    values = _params()
    with neo4j_driver.session() as session:
        session.execute_write(
            lambda tx: tx.run(queries.ACCEPT_AND_RELEASE, **values).single(strict=True)
        )
    locked = Event()
    release_started = Event()
    release_results: list[bool] = []
    errors: list[BaseException] = []

    def replace_block() -> None:
        try:
            with neo4j_driver.session() as session:
                transaction = session.begin_transaction()
                transaction.run(
                    """
                    MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
                      control_instance_id: $control_instance_id})
                    SET dispatch.control_instance_id = dispatch.control_instance_id
                    """,
                    **values,
                ).consume()
                locked.set()
                assert release_started.wait(timeout=10)
                transaction.run(
                    """
                    MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
                      control_instance_id: $control_instance_id})
                    SET dispatch.blocked = true, dispatch.block_reason = 'replacement',
                      dispatch.repair_run_id = 'replacement-run',
                      dispatch.repair_owner_id = 'other',
                      dispatch.repair_token_digest = 'other-token', dispatch.repair_revision = 99,
                      dispatch.replacement_marker = 'replacement-marker',
                      dispatch.replacement_generation = 7
                    """,
                    **values,
                ).consume()
                transaction.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def release_dispatch() -> None:
        try:
            assert locked.wait(timeout=10)
            release_started.set()
            with neo4j_driver.session() as session:
                result = session.execute_write(
                    lambda tx: tx.run(queries.RELEASE_DISPATCH, **values).single()
                )
            release_results.append(result is None)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = (Thread(target=replace_block), Thread(target=release_dispatch))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    assert release_results == [True]
    with neo4j_driver.session() as session:
        row = session.run(
            """
            MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
              control_instance_id: $control_instance_id})
            OPTIONAL MATCH (release:CrmDealRepairDispatchRelease {run_id: $run_id})
            RETURN dispatch.blocked AS blocked, dispatch.block_reason AS reason,
              dispatch.repair_run_id AS run_id, dispatch.replacement_marker AS marker,
              dispatch.replacement_generation AS generation,
              dispatch.integration_release_lock_id AS lock_marker, count(release) AS releases
            """,
            **values,
        ).single(strict=True)
    assert dict(row) == {
        "blocked": True,
        "reason": "replacement",
        "run_id": "replacement-run",
        "marker": "replacement-marker",
        "generation": 7,
        "lock_marker": None,
        "releases": 0,
    }
