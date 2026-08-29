"""Separate Neo4j metadata repository for #310 repair dispatch control."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypeVar

from neo4j import ManagedTransaction

from src.crm_deal_identity_repair.approval_overlay import VerifiedApprovalOverlay
from src.crm_deal_identity_repair.control_models import (
    RepairAllocationCompletion,
    RepairBoundaryComponentProof,
    RepairCheckpointCapture,
    RepairControlLease,
    RepairControlStatus,
    RepairGenerationCapture,
    RepairIngestRunCapture,
    RepairLogicalRunCapture,
    RepairOverlayRow,
    RepairPublicationCapture,
    RepairStaleRunProof,
    RepairStreamCapture,
    RepairTopologyCapture,
)
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_boundary_models import (
    RepairBoundarySnapshot,
    RepairExecutionBoundaryManifest,
)
from src.crm_deal_identity_repair.execution_records import RepairUnit
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_ledger import repair_boundary_snapshot_for_run_transaction
from src.models import JsonValue

T = TypeVar("T")

_WRITE_REPAIR_CONTROL = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, status: 'qualified', execution_allowed: false})
OPTIONAL MATCH (control:CrmDealRepairControl {run_id: $run_id})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {
  source_key: 'bitrix_chat', control_instance_id: $control_instance_id
})
CALL {
  WITH run
  OPTIONAL MATCH (reservation:BitrixRepairPublicationReservation {
    control_instance_id: run.control_instance_id
  })
  WHERE reservation.status IN ['pending', 'publishing']
  RETURN count(reservation) AS uncertain_reservation_count
}
WITH run, control, dispatch, uncertain_reservation_count
WHERE uncertain_reservation_count = 0 AND (
  dispatch IS NULL
  OR (dispatch.blocked = false
      AND dispatch.repair_control_run_id IS NULL
      AND dispatch.repair_control_owner_id IS NULL
      AND dispatch.repair_control_token IS NULL
      AND dispatch.repair_control_revision IS NULL
      AND dispatch.repair_control_state IS NULL)
  OR (dispatch.blocked = true
      AND dispatch.block_reason = 'crm_deal_repair_quiescence'
      AND dispatch.repair_control_run_id = $run_id
      AND dispatch.repair_control_owner_id = $owner_id
      AND dispatch.repair_control_token = $token
      AND dispatch.repair_control_revision = $expected_revision
      AND dispatch.repair_control_state = control.state)
) AND (
  (control IS NULL AND $creating AND $expected_revision = 0 AND $revision = 1
      AND $state = 'quiescing')
  OR (control IS NOT NULL AND control.owner_id = $owner_id AND control.token = $token
      AND control.revision = $expected_revision AND control.boundary_digest = $boundary_digest
      AND (
        (control.state = 'quiescing' AND $state = 'quiesced' AND $revision = $expected_revision + 1)
        OR (control.state = 'quiesced' AND $state IN ['allocated', 'paused', 'lost']
            AND $revision = $expected_revision + 1)
        OR (control.state = 'allocated' AND $state IN ['paused', 'lost']
            AND $revision = $expected_revision + 1)
        OR (control.state = 'paused' AND $state = control.prior_state
            AND $revision = $expected_revision + 1)
        OR (control.state IN ['quiescing', 'quiesced', 'allocated', 'paused']
            AND $state = 'lost' AND $revision = $expected_revision + 1)
      )
  )
)
MERGE (next:CrmDealRepairControl {run_id: $run_id})
ON CREATE SET next.owner_id = $owner_id, next.token = $token, next.revision = $revision,
  next.state = $state, next.boundary_digest = $boundary_digest, next.prior_state = $prior_state,
  next.execution_allowed = false, next.created_at = datetime()
ON MATCH SET next.revision = $revision, next.state = $state, next.prior_state = $prior_state,
  next.updated_at = datetime()
WITH next, dispatch
WHERE next.owner_id = $owner_id AND next.token = $token AND next.revision = $revision
  AND next.boundary_digest = $boundary_digest
MERGE (dispatch_write:BitrixDispatchControl {
  source_key: 'bitrix_chat', control_instance_id: $control_instance_id
})
ON CREATE SET dispatch_write.created_at = datetime()
SET dispatch_write.blocked = true,
    dispatch_write.block_reason = 'crm_deal_repair_quiescence',
    dispatch_write.repair_control_run_id = $run_id,
    dispatch_write.repair_control_owner_id = $owner_id,
    dispatch_write.repair_control_token = $token,
    dispatch_write.repair_control_state = $state,
    dispatch_write.repair_control_revision = $revision,
    dispatch_write.updated_at = datetime()
RETURN next.run_id AS run_id, next.owner_id AS owner_id, next.token AS token,
  next.revision AS revision, next.state AS state, next.boundary_digest AS boundary_digest,
  next.prior_state AS prior_state
"""

_VERIFY_REPAIR_CONTROL_POST_STATE = """
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token: $token,
  revision: $revision, state: $state, boundary_digest: $boundary_digest,
  execution_allowed: false})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id, blocked: true,
  block_reason: 'crm_deal_repair_quiescence', repair_control_run_id: $run_id,
  repair_control_owner_id: $owner_id, repair_control_token: $token,
  repair_control_revision: $revision, repair_control_state: $state})
WHERE (control.prior_state = $prior_state OR (control.prior_state IS NULL AND $prior_state IS NULL))
  AND ($task_proof_state IS NULL OR control.task_proof_state = $task_proof_state)
  AND ((control.stop_reason IS NULL AND $stop_reason IS NULL) OR control.stop_reason = $stop_reason)
RETURN control.run_id AS run_id
"""


_VERIFY_TERMINALIZED_STALE_RUN = """
MATCH (run:CrmDealRepairRun {run_id: $run_id, boundary_digest: $boundary_digest,
  status: 'qualified', execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token: $token,
  revision: $expected_revision, boundary_digest: $boundary_digest})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: run.control_instance_id, blocked: true,
  block_reason: 'crm_deal_repair_quiescence', repair_control_run_id: $run_id,
  repair_control_owner_id: $owner_id, repair_control_token: $token,
  repair_control_revision: $expected_revision})
MATCH (stale:IngestRun {ingest_run_id: $stale_run_id,
  control_instance_id: $stale_control_instance_id, source_key: $stale_source_key,
  status: 'failed', failure_category: 'crm_deal_repair_stale_run',
  repair_control_run_id: $run_id, repair_control_revision: $expected_revision,
  repair_control_evidence: 'exact_owner_or_orphan'})
WHERE stale.failure_message = 'terminalized by exact repair control proof'
RETURN stale.ingest_run_id AS ingest_run_id
"""

_VERIFY_REPAIR_ALLOCATION = """
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token: $token,
  revision: $revision, state: 'allocated', boundary_digest: $boundary_digest})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', blocked: true,
  block_reason: 'crm_deal_repair_quiescence', repair_control_run_id: $run_id,
  repair_control_owner_id: $owner_id, repair_control_token: $token,
  repair_control_revision: $revision, repair_control_state: 'allocated'})
MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id,
  allocation_digest: $allocation_digest, overlay_digest: $overlay_digest,
  approval_reference: $approval_reference, execution_allowed: false})
CALL {
  WITH control
  OPTIONAL MATCH (unit:CrmDealRepairUnit {run_id: control.run_id})
  WITH unit ORDER BY unit.sequence
  RETURN collect(CASE WHEN unit IS NULL THEN NULL ELSE {
    run_id: unit.run_id, unit_id: unit.unit_id, generation: unit.generation,
    sequence: unit.sequence, attempt: unit.attempt, boundary_digest: unit.boundary_digest,
    inventory_fingerprint: unit.inventory_fingerprint, state: unit.state
  } END) AS persisted_units
}
WITH completion, [unit IN persisted_units WHERE unit IS NOT NULL] AS persisted_units
WHERE persisted_units = $units
  AND completion.unit_count = size($units)
RETURN completion.run_id AS run_id
"""


class CrmDealRepairControlRepository:
    """CAS-controls only repair metadata and the exact Bitrix dispatch block."""

    def __init__(self, client: Neo4jClient, control_instance_id: str) -> None:
        self._client = client
        self._control_instance_id = control_instance_id

    def claim(
        self, lease: RepairControlLease, expected_revision: int
    ) -> RepairControlLease:
        """Create or renew only the same owner/token; competing ownership fails closed."""
        return self._write(lease, expected_revision, creating=True)

    def transition(
        self, lease: RepairControlLease, expected_revision: int
    ) -> RepairControlLease:
        """Advance only a same-owner/token control revision; never clear the block."""
        return self._write(lease, expected_revision, creating=False)

    def _write(
        self,
        lease: RepairControlLease,
        expected_revision: int,
        *,
        creating: bool,
    ) -> RepairControlLease:
        """Perform the dispatch/control CAS and prove its exact post-state transactionally."""
        if expected_revision < 0:
            raise ValueError("repair control expected revision is invalid")

        def _mutate(tx: ManagedTransaction) -> RepairControlLease:
            record = tx.run(
                _WRITE_REPAIR_CONTROL,
                run_id=lease.run_id,
                owner_id=lease.owner_id,
                token=lease.token,
                expected_revision=expected_revision,
                revision=lease.revision,
                state=lease.state,
                prior_state=lease.prior_state,
                boundary_digest=lease.boundary_digest,
                control_instance_id=self._control_instance_id,
                creating=creating,
            ).single()
            if record is None:
                raise RuntimeError("repair control ownership or revision was rejected")
            prior = record["prior_state"]
            return RepairControlLease(
                run_id=str(record["run_id"]),
                owner_id=str(record["owner_id"]),
                token=str(record["token"]),
                revision=int(record["revision"]),
                state=str(record["state"]),
                boundary_digest=str(record["boundary_digest"]),
                prior_state=None if prior is None else str(prior),
            )

        def _validate(
            tx: ManagedTransaction, result: RepairControlLease
        ) -> Mapping[str, object]:
            return self._verify_control_post_state(tx, result)

        return self._execute_proven_write(
            lease_after=lease,
            operation="claim" if creating else "transition",
            capture={
                "creating": creating,
                "expected_revision": expected_revision,
                "expected_state": lease.state,
                "expected_prior_state": lease.prior_state,
            },
            mutate=_mutate,
            validate=_validate,
        )

    def _execute_proven_write(
        self,
        *,
        lease_after: RepairControlLease,
        operation: str,
        capture: Mapping[str, object],
        mutate: Callable[[ManagedTransaction], T],
        validate: Callable[[ManagedTransaction, T], Mapping[str, object]],
    ) -> T:
        """Capture, mutate, validate exact post-state, and seal one #310 write atomically."""
        def _work(tx: ManagedTransaction) -> T:
            pre = repair_boundary_snapshot_for_run_transaction(tx, lease_after.run_id)
            result = mutate(tx)
            operation_capture = validate(tx, result)
            post = repair_boundary_snapshot_for_run_transaction(tx, lease_after.run_id)
            pre_proof = RepairBoundaryComponentProof.from_snapshot(pre)
            post_proof = RepairBoundaryComponentProof.from_snapshot(post)
            if not pre_proof.immutable_matches(post_proof):
                raise RuntimeError(
                    "immutable boundary drift occurred inside repair control transaction"
                )
            self._persist_transaction_proof(
                tx=tx,
                lease=lease_after,
                operation=operation,
                pre=pre,
                post=post,
                capture=capture,
                operation_capture=operation_capture,
            )
            return result

        return self._client.execute_write(_work)

    def _persist_transaction_proof(
        self,
        *,
        tx: ManagedTransaction,
        lease: RepairControlLease,
        operation: str,
        pre: RepairBoundarySnapshot,
        post: RepairBoundarySnapshot,
        capture: Mapping[str, object],
        operation_capture: Mapping[str, object],
    ) -> None:
        """Persist canonical baseline/post components and one immutable operation authorization."""
        from src.graph.queries.crm_deal_identity_repair_ledger import (
            PERSIST_REPAIR_BOUNDARY_COMPONENT_PROOF,
            PERSIST_REPAIR_TRANSACTION_AUTHORIZATION,
        )

        pre_snapshot = pre
        post_snapshot = post
        pre_proof = RepairBoundaryComponentProof.from_snapshot(pre_snapshot)
        post_proof = RepairBoundaryComponentProof.from_snapshot(post_snapshot)
        capture_digest = object_digest(
            b"crm-deal-identity-repair-operation-capture-v1\x00",
            {
                "requested": _json_mapping(capture),
                "readback": _json_mapping(operation_capture),
            },
        )
        authorization_digest = object_digest(
            b"crm-deal-identity-repair-transaction-authorization-v2\x00",
            {
                "operation": operation,
                "run_id": lease.run_id,
                "revision": lease.revision,
                "pre": _component_payload(pre_proof),
                "post": _component_payload(post_proof),
                "operation_capture_digest": capture_digest,
            },
        )
        baseline = tx.run(
            PERSIST_REPAIR_BOUNDARY_COMPONENT_PROOF,
            run_id=lease.run_id,
            owner_id=lease.owner_id,
            token=lease.token,
            expected_revision=lease.revision,
            boundary_digest=lease.boundary_digest,
            **_boundary_parameters("baseline", pre_proof),
            authorized_control_digest=post_snapshot.control_digest,
            authorized_stale_run_evidence_digest=post_snapshot.stale_run_evidence_digest,
        ).single()
        if baseline is None:
            raise RuntimeError("repair baseline/post proof was rejected inside write transaction")
        proof = tx.run(
            PERSIST_REPAIR_TRANSACTION_AUTHORIZATION,
            run_id=lease.run_id,
            owner_id=lease.owner_id,
            token=lease.token,
            revision=lease.revision,
            state=lease.state,
            boundary_digest=lease.boundary_digest,
            control_instance_id=self._control_instance_id,
            operation=operation,
            authorization_digest=authorization_digest,
            operation_capture_digest=capture_digest,
            pre_control_digest=pre_snapshot.control_digest,
            post_control_digest=post_snapshot.control_digest,
            pre_stale_run_evidence_digest=pre_snapshot.stale_run_evidence_digest,
            post_stale_run_evidence_digest=post_snapshot.stale_run_evidence_digest,
        ).single()
        if proof is None:
            raise RuntimeError("repair transaction authorization proof was rejected")

    def _verify_control_post_state(
        self,
        tx: ManagedTransaction,
        lease: RepairControlLease,
        *,
        task_proof_state: str | None = None,
        stop_reason: str | None = None,
    ) -> Mapping[str, object]:
        """Read back the exact same control and dispatch ownership after a write."""
        record = tx.run(
            _VERIFY_REPAIR_CONTROL_POST_STATE,
            run_id=lease.run_id,
            owner_id=lease.owner_id,
            token=lease.token,
            revision=lease.revision,
            state=lease.state,
            prior_state=lease.prior_state,
            boundary_digest=lease.boundary_digest,
            control_instance_id=self._control_instance_id,
            task_proof_state=task_proof_state,
            stop_reason=stop_reason,
        ).single()
        if record is None:
            raise RuntimeError("repair control post-state differs from the authorized transition")
        return {
            "control": {
                "run_id": lease.run_id,
                "owner_id": lease.owner_id,
                "token": lease.token,
                "revision": lease.revision,
                "state": lease.state,
                "prior_state": lease.prior_state,
                "task_proof_state": task_proof_state,
                "stop_reason": stop_reason,
            },
            "dispatch": {
                "control_instance_id": self._control_instance_id,
                "blocked": True,
                "block_reason": "crm_deal_repair_quiescence",
                "repair_control_revision": lease.revision,
                "repair_control_state": lease.state,
            },
        }

    def read(self, run_id: str) -> RepairControlLease | None:
        """Read control status without granting a write capability."""
        def _work(tx: ManagedTransaction) -> RepairControlLease | None:
            record = tx.run(
                """
MATCH (control:CrmDealRepairControl {run_id: $run_id})
RETURN control.run_id AS run_id, control.owner_id AS owner_id, control.token AS token,
  control.revision AS revision, control.state AS state, control.boundary_digest AS boundary_digest,
  control.prior_state AS prior_state
""",
                run_id=run_id,
            ).single()
            if record is None:
                return None
            prior = record["prior_state"]
            return RepairControlLease(
                run_id=str(record["run_id"]), owner_id=str(record["owner_id"]),
                token=str(record["token"]), revision=int(record["revision"]),
                state=str(record["state"]), boundary_digest=str(record["boundary_digest"]),
                prior_state=None if prior is None else str(prior),
            )

        return self._client.execute_read(_work)

    def inventory_topology(self, lease: RepairControlLease) -> RepairTopologyCapture:
        """Freeze exact affected control identities and their state/fence evidence."""
        from src.graph.queries.crm_deal_identity_repair_ledger import (
            INVENTORY_REPAIR_TOPOLOGY,
        )

        def _work(tx: ManagedTransaction) -> RepairTopologyCapture:
            record = tx.run(
                INVENTORY_REPAIR_TOPOLOGY,
                run_id=lease.run_id,
                owner_id=lease.owner_id,
                token=lease.token,
                expected_revision=lease.revision,
                boundary_digest=lease.boundary_digest,
            ).single()
            if record is None:
                raise RuntimeError("repair dispatch ownership is not active for topology capture")
            return RepairTopologyCapture(
                logical_run_ids=tuple(
                    RepairLogicalRunCapture(
                        logical_run_id=_required_text(item, "logical_run_id"),
                        status=_required_text(item, "status"),
                    )
                    for item in _captured_maps(record["logical_run_ids"])
                ),
                ingest_run_ids=tuple(
                    RepairIngestRunCapture(
                        ingest_run_id=_required_text(item, "ingest_run_id"),
                        status=_required_text(item, "status"),
                        generation=_required_int(item, "generation"),
                    )
                    for item in _captured_maps(record["ingest_run_ids"])
                ),
                checkpoint_ids=tuple(
                    RepairCheckpointCapture(
                        logical_run_id=_required_text(item, "logical_run_id"),
                        phase=_required_text(item, "phase"),
                        generation=_required_int(item, "generation"),
                        status=_required_text(item, "status"),
                    )
                    for item in _captured_maps(record["checkpoint_ids"])
                ),
                stream_ids=tuple(
                    RepairStreamCapture(
                        stream_key=_required_text(item, "stream_key"),
                        logical_run_id=_required_text(item, "logical_run_id"),
                        ingest_run_id=_required_text(item, "ingest_run_id"),
                        attempt_generation=_required_int(item, "attempt_generation"),
                        stream_generation=_required_int(item, "stream_generation"),
                        fencing_token=_required_int(item, "fencing_token"),
                        status=_required_text(item, "status"),
                    )
                    for item in _captured_maps(record["stream_ids"])
                ),
                generation_ids=tuple(
                    RepairGenerationCapture(
                        generation_id=_required_text(item, "generation_id"),
                        status=_required_text(item, "status"),
                    )
                    for item in _captured_maps(record["generation_ids"])
                ),
                publication_ids=tuple(
                    RepairPublicationCapture(
                        successor_generation_id=_required_text(item, "successor_generation_id"),
                        evidence_digest=_required_text(item, "evidence_digest"),
                        occurrence=_required_text(item, "occurrence"),
                        status=_required_text(item, "status"),
                    )
                    for item in _captured_maps(record["publication_ids"])
                ),
            )

        return self._client.execute_read(_work)

    def supersede_topology(
        self,
        lease: RepairControlLease,
        expected_revision: int,
        topology: RepairTopologyCapture,
    ) -> RepairControlLease:
        """Supersede only a frozen exact topology, then CAS the owned block to quiesced."""
        from src.graph.queries.crm_deal_identity_repair_ledger import (
            SUPERSEDE_CAPTURED_REPAIR_TOPOLOGY,
            VERIFY_QUIESCED_REPAIR_TOPOLOGY,
        )

        if lease.revision != expected_revision:
            raise ValueError("repair topology revision must match its captured lease")
        parameters = topology.as_parameters()

        def _mutate(tx: ManagedTransaction) -> RepairControlLease:
            record = tx.run(
                SUPERSEDE_CAPTURED_REPAIR_TOPOLOGY,
                run_id=lease.run_id,
                owner_id=lease.owner_id,
                token=lease.token,
                expected_revision=expected_revision,
                next_revision=expected_revision + 1,
                boundary_digest=lease.boundary_digest,
                **parameters,
            ).single()
            if record is None:
                raise RuntimeError(
                    "repair topology changed, fence is stale, or control ownership was lost"
                )
            return RepairControlLease(
                lease.run_id,
                lease.owner_id,
                lease.token,
                int(record["revision"]),
                "quiesced",
                lease.boundary_digest,
            )

        def _validate(
            tx: ManagedTransaction, result: RepairControlLease
        ) -> Mapping[str, object]:
            verification = tx.run(
                VERIFY_QUIESCED_REPAIR_TOPOLOGY,
                run_id=result.run_id,
                owner_id=result.owner_id,
                token=result.token,
                expected_revision=result.revision,
                boundary_digest=result.boundary_digest,
                **parameters,
            ).single()
            if verification is None or verification["verified"] is not True:
                raise RuntimeError("captured repair topology post-state is not exact")
            return {
                "topology": topology.as_parameters(),
                "control": self._verify_control_post_state(tx, result),
            }

        return self._execute_proven_write(
            lease_after=RepairControlLease(
                lease.run_id,
                lease.owner_id,
                lease.token,
                expected_revision + 1,
                "quiesced",
                lease.boundary_digest,
            ),
            operation="supersede_topology",
            capture=parameters,
            mutate=_mutate,
            validate=_validate,
        )

    def verify_quiesced_topology(
        self, lease: RepairControlLease, topology: RepairTopologyCapture
    ) -> bool:
        """Reread every captured identity/fence after supersession without writing any graph row."""
        from src.graph.queries.crm_deal_identity_repair_ledger import (
            VERIFY_QUIESCED_REPAIR_TOPOLOGY,
        )

        parameters = topology.as_parameters()

        def _work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                VERIFY_QUIESCED_REPAIR_TOPOLOGY,
                run_id=lease.run_id,
                owner_id=lease.owner_id,
                token=lease.token,
                expected_revision=lease.revision,
                boundary_digest=lease.boundary_digest,
                **parameters,
            ).single()
            return record is not None and record["verified"] is True

        return self._client.execute_read(_work)

    def inventory_stale_run(
        self,
        lease: RepairControlLease,
        stale_run_id: str,
    ) -> RepairStaleRunProof:
        """Capture one exact stale-run owner/orphan proof before terminalizing it."""
        from src.graph.queries.crm_deal_identity_repair_ledger import (
            INVENTORY_STALE_REPAIR_RUN_PROOF,
        )

        def _work(tx: ManagedTransaction) -> RepairStaleRunProof:
            record = tx.run(
                INVENTORY_STALE_REPAIR_RUN_PROOF,
                run_id=lease.run_id,
                owner_id=lease.owner_id,
                token=lease.token,
                expected_revision=lease.revision,
                boundary_digest=lease.boundary_digest,
                stale_run_id=stale_run_id,
            ).single()
            if record is None:
                raise RuntimeError("stale-run ownership proof is missing or dispatch was lost")
            if int(record["produced_checkpoint_count"]) != 0:
                raise RuntimeError("stale-run checkpoint ownership is ambiguous")
            if int(record["logical_checkpoint_count"]) != len(record["checkpoint_ids"]):
                raise RuntimeError("stale-run checkpoint proof is ambiguous")
            return RepairStaleRunProof(
                ingest_run_id=str(record["ingest_run_id"]),
                control_instance_id=str(record["control_instance_id"]),
                source_key=str(record["source_key"]),
                status=str(record["status"]),
                logical_run_ids=_required_text_list(record["logical_run_ids"], "logical run"),
                checkpoint_ids=_required_text_list(record["checkpoint_ids"], "checkpoint"),
                stream_keys=_required_text_list(record["stream_keys"], "stream"),
            )

        return self._client.execute_read(_work)

    def terminalize_stale_run(
        self,
        lease: RepairControlLease,
        expected_revision: int,
        proof: RepairStaleRunProof,
    ) -> None:
        """Fail only the exact captured stale run; all owner/orphan evidence is rechecked."""
        from src.graph.queries.crm_deal_identity_repair_ledger import (
            TERMINALIZE_STALE_REPAIR_RUN,
        )

        if expected_revision != lease.revision:
            raise ValueError("stale-run terminalization revision must match the captured lease")
        parameters = proof.as_parameters()

        def _mutate(tx: ManagedTransaction) -> None:
            record = tx.run(
                TERMINALIZE_STALE_REPAIR_RUN,
                run_id=lease.run_id,
                owner_id=lease.owner_id,
                token=lease.token,
                expected_revision=expected_revision,
                boundary_digest=lease.boundary_digest,
                **parameters,
            ).single()
            if record is None:
                raise RuntimeError("stale-run ownership is ambiguous, stale, or changed")

        def _validate(tx: ManagedTransaction, _result: None) -> Mapping[str, object]:
            record = tx.run(
                _VERIFY_TERMINALIZED_STALE_RUN,
                run_id=lease.run_id,
                owner_id=lease.owner_id,
                token=lease.token,
                expected_revision=lease.revision,
                boundary_digest=lease.boundary_digest,
                **parameters,
            ).single()
            if record is None:
                raise RuntimeError("terminalized stale-run evidence differs from the exact proof")
            return {
                "stale_run": dict(parameters),
                "control": self._verify_control_post_state(tx, lease),
            }

        self._execute_proven_write(
            lease_after=lease,
            operation="terminalize_stale_run",
            capture=parameters,
            mutate=_mutate,
            validate=_validate,
        )

    def read_boundary_component_proof(
        self, run_id: str
    ) -> tuple[RepairBoundaryComponentProof, str, str] | None:
        """Read the persisted baseline plus exact authorized control/stale digests."""
        from src.graph.queries.crm_deal_identity_repair_ledger import (
            READ_REPAIR_BOUNDARY_COMPONENT_PROOF,
        )

        def _work(
            tx: ManagedTransaction,
        ) -> tuple[RepairBoundaryComponentProof, str, str] | None:
            record = tx.run(READ_REPAIR_BOUNDARY_COMPONENT_PROOF, run_id=run_id).single()
            if record is None:
                return None
            baseline = RepairBoundaryComponentProof(
                source_instance_id=str(record["baseline_source_instance_id"]),
                control_instance_id=str(record["baseline_control_instance_id"]),
                inventory_digest=str(record["baseline_inventory_digest"]),
                inventory_row_count=int(record["baseline_inventory_row_count"]),
                eligible_unit_count=int(record["baseline_eligible_unit_count"]),
                negative_control_count=int(record["baseline_negative_control_count"]),
                source_records_digest=str(record["baseline_source_records_digest"]),
                source_instance_digest=str(record["baseline_source_instance_digest"]),
                control_digest=str(record["baseline_control_digest"]),
                stale_run_evidence_digest=str(record["baseline_stale_run_evidence_digest"]),
            )
            control_digest = _required_text_value(record["authorized_control_digest"], "control")
            stale_digest = _required_text_value(
                record["authorized_stale_run_evidence_digest"], "stale-run"
            )
            return baseline, control_digest, stale_digest

        return self._client.execute_read(_work)

    def record_task_proof(
        self,
        lease: RepairControlLease,
        proof_state: str,
        stop_reason: str | None,
    ) -> None:
        """Persist only fail-closed task-proof metadata under the current owned CAS lease."""
        from src.graph.queries.crm_deal_identity_repair_ledger import (
            RECORD_REPAIR_TASK_PROOF,
        )

        if proof_state not in {"absent", "failed", "lost"}:
            raise ValueError("repair task proof state is invalid")

        def _mutate(tx: ManagedTransaction) -> None:
            record = tx.run(
                RECORD_REPAIR_TASK_PROOF,
                run_id=lease.run_id,
                owner_id=lease.owner_id,
                token=lease.token,
                expected_revision=lease.revision,
                boundary_digest=lease.boundary_digest,
                proof_state=proof_state,
                stop_reason=stop_reason,
            ).single()
            if record is None:
                raise RuntimeError("repair task proof ownership or revision was lost")

        def _validate(tx: ManagedTransaction, _result: None) -> Mapping[str, object]:
            return self._verify_control_post_state(
                tx,
                lease,
                task_proof_state=proof_state,
                stop_reason=stop_reason,
            )

        self._execute_proven_write(
            lease_after=lease,
            operation=f"record_task_proof:{proof_state}",
            capture={"proof_state": proof_state, "stop_reason": stop_reason},
            mutate=_mutate,
            validate=_validate,
        )

    def read_status(self, run_id: str) -> RepairControlStatus:
        """Read combined control/allocation/topology evidence without issuing a capability."""
        from src.graph.queries.crm_deal_identity_repair_ledger import (
            READ_REPAIR_CONTROL_STATUS,
        )

        def _work(tx: ManagedTransaction) -> RepairControlStatus:
            record = tx.run(READ_REPAIR_CONTROL_STATUS, run_id=run_id).single()
            if record is None:
                raise RuntimeError("qualified repair run is missing")
            state_value = record["state"]
            prior_value = record["prior_state"]
            revision_value = record["revision"]
            completion_count = record["completion_unit_count"]
            return RepairControlStatus(
                run_id=str(record["run_id"]),
                boundary_digest=str(record["boundary_digest"]),
                owner_id=_optional_text(record["owner_id"]),
                revision=None if revision_value is None else int(revision_value),
                state=None if state_value is None else str(state_value),
                prior_state=None if prior_value is None else str(prior_value),
                allocation_digest=_optional_text(record["allocation_digest"]),
                allocation_unit_count=int(record["allocation_unit_count"]),
                completion_unit_count=None if completion_count is None else int(completion_count),
                dispatch_blocked=(
                    None
                    if record["dispatch_blocked"] is None
                    else bool(record["dispatch_blocked"])
                ),
                dispatch_owner_id=_optional_text(record["dispatch_owner_id"]),
                topology_active_count=int(record["topology_active_count"]),
                topology_superseded_count=int(record["topology_superseded_count"]),
                stale_run_proof_count=int(record["stale_run_proof_count"]),
                task_proof_state=_optional_text(record["task_proof_state"]),
                stop_reason=_optional_text(record["stop_reason"]),
            )

        return self._client.execute_read(_work)

    def allocate(
        self,
        lease: RepairControlLease,
        expected_revision: int,
        units: tuple[RepairUnit, ...],
        completion: RepairAllocationCompletion,
        overlay: VerifiedApprovalOverlay,
        manifest: RepairExecutionBoundaryManifest,
        qualified_source_record_pks: tuple[str, ...],
    ) -> RepairAllocationCompletion:
        """Persist one exact approved allocation or return its exact sealed replay."""
        import json

        from src.graph.queries.crm_deal_identity_repair_ledger import (
            ALLOCATE_REPAIR_UNITS,
        )

        if lease.revision != expected_revision:
            raise ValueError("repair allocation revision must match its captured lease")
        if manifest.graph_boundary_digest != lease.boundary_digest:
            raise ValueError("repair allocation manifest boundary does not match the control lease")
        if manifest.execution_allowed is not False:
            raise ValueError("repair allocation manifest must remain non-executable")
        if overlay.approval_reference != manifest.approval_reference:
            raise ValueError("repair allocation overlay approval reference changed")
        executable_rows = tuple(row for row in overlay.rows if row.disposition == "executable")
        _validate_allocation_inputs(units, executable_rows, completion, manifest, lease)
        _validate_overlay_rows(overlay.rows, manifest, qualified_source_record_pks)
        unit_parameters = [_unit_parameters(unit) for unit in units]
        row_parameters = [_approved_row_parameters(row) for row in overlay.rows]
        manifest_json = json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":"))

        def _mutate(tx: ManagedTransaction) -> RepairAllocationCompletion:
            record = tx.run(
                ALLOCATE_REPAIR_UNITS,
                run_id=lease.run_id,
                owner_id=lease.owner_id,
                token=lease.token,
                expected_revision=expected_revision,
                next_revision=expected_revision + 1,
                boundary_digest=lease.boundary_digest,
                manifest_digest=manifest.manifest_digest,
                artifact_id=manifest.artifact_id,
                artifact_manifest_hmac=manifest.artifact_manifest_hmac,
                inventory_digest=manifest.inventory_digest,
                source_instance_id=manifest.source_instance_id,
                control_instance_id=manifest.control_instance_id,
                manifest_json=manifest_json,
                inventory_row_count=manifest.inventory_row_count,
                eligible_unit_count=manifest.eligible_unit_count,
                negative_control_count=manifest.negative_control_count,
                manifest_unit_ceiling=manifest.unit_ceiling,
                unit_ceiling=manifest.unit_ceiling,
                generation=_allocation_generation(units),
                units=unit_parameters,
                approved_rows=row_parameters,
                qualified_source_record_pks=list(qualified_source_record_pks),
                allocation_digest=completion.allocation_digest,
                overlay_digest=overlay.overlay_digest,
                approval_reference=overlay.approval_reference,
            ).single()
            if record is None:
                raise RuntimeError(
                    "repair allocation conflicts with control, boundary, or persisted replay"
                )
            result = RepairAllocationCompletion(
                run_id=lease.run_id,
                allocation_digest=str(record["allocation_digest"]),
                executable_count=int(record["executable_count"]),
                unit_count=int(record["unit_count"]),
            )
            if result != completion:
                raise RuntimeError("repair allocation readback differs from the sealed request")
            return result

        allocated_lease = (
            lease
            if lease.state == "allocated"
            else RepairControlLease(
                lease.run_id,
                lease.owner_id,
                lease.token,
                expected_revision + 1,
                "allocated",
                lease.boundary_digest,
            )
        )

        def _validate(
            tx: ManagedTransaction, result: RepairAllocationCompletion
        ) -> Mapping[str, object]:
            record = tx.run(
                _VERIFY_REPAIR_ALLOCATION,
                run_id=lease.run_id,
                owner_id=lease.owner_id,
                token=lease.token,
                revision=allocated_lease.revision,
                boundary_digest=lease.boundary_digest,
                allocation_digest=completion.allocation_digest,
                overlay_digest=overlay.overlay_digest,
                approval_reference=overlay.approval_reference,
                units=unit_parameters,
            ).single()
            if record is None:
                raise RuntimeError("repair allocation post-state differs from sealed allocation")
            return {
                "allocation": {
                    "allocation_digest": result.allocation_digest,
                    "executable_count": result.executable_count,
                    "unit_count": result.unit_count,
                    "overlay_digest": overlay.overlay_digest,
                    "approval_reference": overlay.approval_reference,
                    "units": unit_parameters,
                },
                "control": self._verify_control_post_state(tx, allocated_lease),
            }

        return self._execute_proven_write(
            lease_after=allocated_lease,
            operation="allocate",
            capture={
                "allocation_digest": completion.allocation_digest,
                "overlay_digest": overlay.overlay_digest,
                "approval_reference": overlay.approval_reference,
                "units": unit_parameters,
            },
            mutate=_mutate,
            validate=_validate,
        )


def _json_mapping(value: Mapping[str, object]) -> dict[str, JsonValue]:
    """Validate operation captures before they enter canonical authorization evidence."""
    return {key: _json_value(item) for key, item in value.items()}


def _json_value(value: object) -> JsonValue:
    """Convert only JSON-shaped locally captured metadata; no broad coercion is permitted."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise RuntimeError("repair operation capture contains a non-JSON value")


def _component_payload(proof: RepairBoundaryComponentProof) -> dict[str, JsonValue]:
    """Encode every canonical component so an authorization digest cannot bless a later snapshot."""
    return {
        "source_instance_id": proof.source_instance_id,
        "control_instance_id": proof.control_instance_id,
        "inventory_digest": proof.inventory_digest,
        "inventory_row_count": proof.inventory_row_count,
        "eligible_unit_count": proof.eligible_unit_count,
        "negative_control_count": proof.negative_control_count,
        "source_records_digest": proof.source_records_digest,
        "source_instance_digest": proof.source_instance_digest,
        "control_digest": proof.control_digest,
        "stale_run_evidence_digest": proof.stale_run_evidence_digest,
    }


def _validate_allocation_inputs(
    units: tuple[RepairUnit, ...],
    rows: tuple[RepairOverlayRow, ...],
    completion: RepairAllocationCompletion,
    manifest: RepairExecutionBoundaryManifest,
    lease: RepairControlLease,
) -> None:
    """Reject altered, duplicate, gapped, or non-approved allocation input locally."""
    if completion.run_id != lease.run_id:
        raise ValueError("repair allocation completion belongs to a different run")
    if completion.executable_count != len(rows) or completion.unit_count != len(units):
        raise ValueError("repair allocation completion count differs from the approved rows")
    if len(units) > manifest.unit_ceiling:
        raise ValueError("repair allocation exceeds the qualified unit ceiling")
    if len({unit.unit_id for unit in units}) != len(units):
        raise ValueError("repair allocation contains duplicate units")
    if len({row.inventory_key for row in rows}) != len(rows):
        raise ValueError("repair allocation contains duplicate approved inventory keys")
    if len({row.inventory_fingerprint for row in rows}) != len(rows):
        raise ValueError("repair allocation requires unique approved row fingerprints")
    if tuple(unit.sequence for unit in units) != tuple(range(len(units))):
        raise ValueError("repair allocation unit ordinals must be contiguous from zero")
    expected_fingerprints = {row.inventory_fingerprint for row in rows}
    actual_fingerprints = {unit.inventory_fingerprint for unit in units}
    if actual_fingerprints != expected_fingerprints:
        raise ValueError("repair allocation units differ from approved executable rows")
    if any(
        unit.run_id != lease.run_id
        or unit.boundary_digest != lease.boundary_digest
        or unit.generation < 1
        or unit.attempt != 1
        or unit.state != "allocated"
        for unit in units
    ):
        raise ValueError("repair allocation unit boundary or state is invalid")


def _validate_overlay_rows(
    rows: tuple[RepairOverlayRow, ...],
    manifest: RepairExecutionBoundaryManifest,
    qualified_source_record_pks: tuple[str, ...],
) -> None:
    """Require the HMAC-verified overlay to classify every immutable #300 inventory row."""
    if len(rows) != manifest.inventory_row_count:
        raise ValueError("repair allocation overlay does not cover the qualified inventory")
    if tuple(sorted(row.source_record_pk for row in rows)) != qualified_source_record_pks:
        raise ValueError(
            "repair allocation overlay does not match qualified source-record identities"
        )


def _unit_parameters(unit: RepairUnit) -> dict[str, str | int]:
    """Encode one immutable unit for parameterized Cypher."""
    return {
        "run_id": unit.run_id,
        "unit_id": unit.unit_id,
        "generation": unit.generation,
        "sequence": unit.sequence,
        "attempt": unit.attempt,
        "boundary_digest": unit.boundary_digest,
        "inventory_fingerprint": unit.inventory_fingerprint,
        "state": unit.state,
    }


def _approved_row_parameters(row: RepairOverlayRow) -> dict[str, str]:
    """Encode one HMAC-verified executable row without retaining its source payload."""
    return {
        "inventory_key": row.inventory_key,
        "source_record_pk": row.source_record_pk,
        "inventory_fingerprint": row.inventory_fingerprint,
        "disposition": row.disposition,
    }


def _allocation_generation(units: tuple[RepairUnit, ...]) -> int:
    """Use the sealed unit generation; zero allocations retain generation one by convention."""
    if not units:
        return 1
    generations = {unit.generation for unit in units}
    if len(generations) != 1:
        raise ValueError("repair allocation units must use one generation")
    return next(iter(generations))


def _captured_maps(value: object) -> tuple[Mapping[str, object], ...]:
    """Validate Neo4j map projections before they become a quiescence capability."""
    if not isinstance(value, list):
        raise RuntimeError("repair topology inventory is not a list")
    captures: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise RuntimeError("repair topology inventory contains an invalid identity")
        captures.append(item)
    return tuple(captures)


def _boundary_parameters(
    prefix: str, proof: RepairBoundaryComponentProof
) -> dict[str, object]:
    """Encode only canonical #300 component evidence for the #310-derived proof record."""
    return {
        f"{prefix}_source_instance_id": proof.source_instance_id,
        f"{prefix}_control_instance_id": proof.control_instance_id,
        f"{prefix}_inventory_digest": proof.inventory_digest,
        f"{prefix}_inventory_row_count": proof.inventory_row_count,
        f"{prefix}_eligible_unit_count": proof.eligible_unit_count,
        f"{prefix}_negative_control_count": proof.negative_control_count,
        f"{prefix}_source_records_digest": proof.source_records_digest,
        f"{prefix}_source_instance_digest": proof.source_instance_digest,
        f"{prefix}_control_digest": proof.control_digest,
        f"{prefix}_stale_run_evidence_digest": proof.stale_run_evidence_digest,
    }


def _required_text_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"repair boundary proof {label} digest is invalid")
    return value


def _required_text_list(value: object, label: str) -> tuple[str, ...]:
    """Read a unique list of proof identities without coercing graph values."""
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise RuntimeError(f"repair stale-run {label} proof is invalid")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise RuntimeError(f"repair stale-run {label} proof is duplicated")
    return result


def _optional_text(value: object) -> str | None:
    """Preserve optional graph text without broad coercion."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RuntimeError("repair control status contains invalid text")
    return value


def _required_text(item: Mapping[str, object], key: str) -> str:
    """Read a required non-empty captured string without coercing database values."""
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"repair topology {key} is invalid")
    return value


def _required_int(item: Mapping[str, object], key: str) -> int:
    """Read a required non-negative captured counter without accepting booleans."""
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"repair topology {key} is invalid")
    return value
