"""Separate Neo4j repository for #310 repair control metadata only."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from typing import Literal, cast

from neo4j import ManagedTransaction

from src.crm_deal_identity_repair.allocation import AllocationPlan
from src.crm_deal_identity_repair.control_models import (
    RepairControlRequest,
    RepairControlState,
    RepairControlStatus,
    RepairDispatchLease,
    RepairPublicationReservation,
    RepairPublicationState,
)
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.task_inspection import (
    TaskAbsenceEvidence,
    verify_absence_evidence,
)
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_ledger import (
    _control_row,
    _current_inventory_boundary,
    _CurrentInventoryBoundary,
    _snapshot_from_transaction,
    _source_rows,
)
from src.graph.crm_deal_identity_repair_ledger_records import stored_qualification_from_record
from src.graph.queries.crm_deal_identity_repair_control import (
    ALLOCATE_REPAIR_UNITS,
    CLAIM_REPAIR_DISPATCH,
    COMPLETE_QUIESCENCE,
    CONFIRM_REPAIR_AWARE_PUBLICATION,
    MARK_REPAIR_AWARE_PUBLISHING,
    PAUSE_REPAIR_CONTROL,
    PREPARE_REPAIR_AWARE_PUBLICATION,
    READ_REPAIR_CONTROL_PROOF,
    READ_REPAIR_CONTROL_STATUS,
    READ_REPAIR_TOPOLOGY_CAPTURE,
    READ_REPAIR_TOPOLOGY_SNAPSHOT,
    REQUEST_REPAIR_TOPOLOGY_STOP,
    RESUME_REPAIR_CONTROL,
    STORE_REPAIR_TOPOLOGY_CAPTURE,
    SUPERSEDE_REPAIR_TOPOLOGY,
)
from src.graph.queries.crm_deal_identity_repair_ledger import GET_REPAIR_RUN
from src.models import JsonValue


class CrmDealRepairControlRepository:
    """All writes target repair/control labels and exact dispatch metadata only."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def claim(
        self, request: RepairControlRequest, *, boundary_digest: str, control_instance_id: str
    ) -> RepairDispatchLease:
        return self._write_lease(
            CLAIM_REPAIR_DISPATCH, request, boundary_digest, control_instance_id
        )

    def complete_quiescence(
        self,
        request: RepairControlRequest,
        *,
        boundary_digest: str,
        control_instance_id: str,
        topology_digest: str,
        evidence: TaskAbsenceEvidence,
        proof_secret: bytes,
        stale_run_id: str,
    ) -> RepairDispatchLease:
        from datetime import UTC, datetime

        if not verify_absence_evidence(evidence, secret=proof_secret, now=datetime.now(UTC)):
            raise RuntimeError("repair task absence evidence failed final authentication")
        payload = json.dumps(evidence.payload(), sort_keys=True, separators=(",", ":"))

        def work(tx: ManagedTransaction) -> RepairDispatchLease:
            captured = tx.run(
                READ_REPAIR_TOPOLOGY_CAPTURE,
                run_id=request.run_id,
                control_instance_id=control_instance_id,
                topology_digest=topology_digest,
            ).single()
            if captured is None or not isinstance(captured["captures_json"], str):
                raise RuntimeError("repair topology capture is missing")
            captures_json = captured["captures_json"]
            decoded = json.loads(captures_json)
            if not isinstance(decoded, dict) or not isinstance(decoded.get("captures"), list):
                raise RuntimeError("repair topology capture is malformed")
            captures = decoded["captures"]
            publications = decoded.get("publications")
            stale_snapshot = decoded.get("stale")
            if not isinstance(publications, list) or not isinstance(stale_snapshot, dict):
                raise RuntimeError("repair topology capture is incomplete")
            captured_boundary = self._read_actual_boundary(tx, request.repair_id)
            stored_boundary = decoded.get("boundary_digest")
            if not isinstance(stored_boundary, str) or stored_boundary != captured_boundary:
                raise RuntimeError("repair topology boundary is stale or malformed")
            current_boundary = self._read_actual_boundary(tx, request.repair_id)
            if current_boundary != captured_boundary:
                raise RuntimeError("repair boundary changed during final revalidation")
            record = tx.run(
                COMPLETE_QUIESCENCE,
                repair_id=request.repair_id,
                run_id=request.run_id,
                owner_id=request.owner_id,
                token=request.token,
                expected_revision=request.expected_revision,
                boundary_digest=boundary_digest,
                control_instance_id=control_instance_id,
                topology_digest=topology_digest,
                topology_json=captures_json,
                captures=captures,
                publications=publications,
                stale_snapshot=stale_snapshot,
                stale_run_id=stale_run_id,
                proof_payload_json=payload,
                proof_digest=evidence.payload_digest,
                proof_hmac=evidence.hmac_hex,
                proof_expires_at=evidence.expires_at,
            ).single()
            if record is None:
                raise RuntimeError("repair quiescence final compare-and-set was rejected")
            if (
                record["proof_payload_json"],
                record["proof_digest"],
                record["proof_hmac"],
                record["proof_expires_at"],
            ) != (payload, evidence.payload_digest, evidence.hmac_hex, evidence.expires_at):
                raise RuntimeError("repair quiescence proof durable readback differs")
            return _lease(record)

        return self._client.execute_write(work)

    def pause(self, request: RepairControlRequest) -> RepairDispatchLease:
        return self._lease_only(PAUSE_REPAIR_CONTROL, request)

    def resume(self, request: RepairControlRequest) -> RepairDispatchLease:
        return self._lease_only(RESUME_REPAIR_CONTROL, request)

    def allocate(
        self,
        request: RepairControlRequest,
        *,
        boundary_digest: str,
        proof_digest: str,
        plan: AllocationPlan,
    ) -> RepairDispatchLease:
        units = [asdict(unit) for unit in plan.units]

        def work(tx: ManagedTransaction) -> RepairDispatchLease:
            inventory = self._read_allocation_boundary(tx, request.repair_id)
            record = tx.run(
                ALLOCATE_REPAIR_UNITS,
                repair_id=request.repair_id,
                run_id=request.run_id,
                owner_id=request.owner_id,
                token=request.token,
                expected_revision=request.expected_revision,
                boundary_digest=boundary_digest,
                proof_digest=proof_digest,
                completion_id=plan.completion.completion_id,
                overlay_digest=plan.completion.overlay_digest,
                allocation_digest=plan.completion.allocation_digest,
                unit_count=plan.completion.unit_count,
                units=units,
                unit_ids=[unit.unit_id for unit in plan.units],
                actual_inventory_digest=inventory.inventory_digest,
                actual_inventory_row_count=inventory.inventory_row_count,
                actual_eligible_unit_count=inventory.eligible_unit_count,
                actual_negative_control_count=inventory.negative_control_count,
            ).single()
            return _lease(record)

        return self._client.execute_write(work)

    @staticmethod
    def _read_allocation_boundary(
        tx: ManagedTransaction, repair_id: str
    ) -> _CurrentInventoryBoundary:
        record = tx.run(GET_REPAIR_RUN, repair_id=repair_id).single()
        if record is None:
            raise RuntimeError("repair qualification boundary is missing")
        stored = stored_qualification_from_record(repair_id, record)
        inventory = _current_inventory_boundary(tx, stored.source_record_pks)
        _source_rows(tx, stored.source_record_pks, stored.run.source_instance_id)
        _control_row(tx, stored.run.source_instance_id, stored.run.control_instance_id)
        if (
            inventory.inventory_digest,
            inventory.inventory_row_count,
            inventory.eligible_unit_count,
            inventory.negative_control_count,
        ) != (
            stored.run.inventory_digest,
            stored.run.inventory_row_count,
            stored.run.eligible_unit_count,
            stored.run.negative_control_count,
        ):
            raise RuntimeError("repair allocation inventory boundary became stale")
        return inventory

    def status(self, repair_id: str) -> RepairControlStatus:
        def work(tx: ManagedTransaction) -> RepairControlStatus:
            record = tx.run(READ_REPAIR_CONTROL_STATUS, repair_id=repair_id).single()
            if record is None or record["run_id"] is None:
                return RepairControlStatus(
                    repair_id, "not_qualified", None, None, None, None, None, None, None
                )
            state_value = record["control_state"]
            state = _control_state(state_value) if state_value is not None else None
            return RepairControlStatus(
                repair_id,
                "qualified",
                state,
                _optional_bool(record["dispatch_blocked"]),
                _optional_int(record["dispatch_revision"]),
                _optional_quiescence_state(record["quiescence_state"]),
                _optional_allocation_state(record["allocation_state"]),
                _optional_paused_from_state(record["paused_from_state"]),
                _optional_int(record["allocated_unit_count"]),
            )

        return self._client.execute_read(work)

    def proof_digest(self, request: RepairControlRequest) -> str:
        def work(tx: ManagedTransaction) -> str:
            record = tx.run(
                READ_REPAIR_CONTROL_PROOF,
                run_id=request.run_id,
                owner_id=request.owner_id,
                token=request.token,
                revision=request.expected_revision,
            ).single()
            if record is None or not isinstance(record["proof_digest"], str):
                raise RuntimeError("repair quiescence proof is absent or stale")
            return record["proof_digest"]

        return self._client.execute_read(work)

    def request_stop_topology(
        self, *, control_instance_id: str, run_id: str, owner_id: str, stale_run_id: str
    ) -> str:
        def work(tx: ManagedTransaction) -> str:
            stopped = tx.run(
                REQUEST_REPAIR_TOPOLOGY_STOP,
                control_instance_id=control_instance_id,
                owner_id=owner_id,
            ).single()
            if stopped is None:
                raise RuntimeError("repair topology stop boundary is missing")
            snapshot = tx.run(
                READ_REPAIR_TOPOLOGY_SNAPSHOT,
                control_instance_id=control_instance_id,
                stale_run_id=stale_run_id,
            ).single()
            if snapshot is None:
                raise RuntimeError("repair topology snapshot is missing")
            captures = snapshot["captures"]
            publications = snapshot["publications"]
            stale = snapshot["stale"]
            if not isinstance(captures, list) or not isinstance(publications, list):
                raise RuntimeError("repair topology snapshot is malformed")
            if not isinstance(stale, dict) or not isinstance(stale.get("state"), str):
                raise RuntimeError("repair stale topology snapshot is malformed")
            if stale["state"] == "ambiguous":
                raise RuntimeError("repair stale run ownership or orphan proof is ambiguous")
            boundary_digest = self._read_actual_boundary(tx, run_id=run_id)
            topology: dict[str, JsonValue] = {
                "boundary_digest": boundary_digest,
                "captures": cast(JsonValue, captures),
                "publications": cast(JsonValue, publications),
                "stale": cast(JsonValue, stale),
            }
            digest = object_digest(
                b"crm-deal-identity-repair-topology-v1\x00",
                {
                    "run_id": run_id,
                    "control_instance_id": control_instance_id,
                    "topology": topology,
                },
            )
            encoded = json.dumps(topology, sort_keys=True, separators=(",", ":"))
            saved = tx.run(
                STORE_REPAIR_TOPOLOGY_CAPTURE,
                run_id=run_id,
                control_instance_id=control_instance_id,
                topology_digest=digest,
                captures_json=encoded,
            ).single()
            if saved is None:
                raise RuntimeError("repair topology capture could not be persisted")
            return digest

        return self._client.execute_write(work)

    @staticmethod
    def _read_actual_boundary(
        tx: ManagedTransaction, repair_id: str | None = None, *, run_id: str | None = None
    ) -> str:
        if (repair_id is None) == (run_id is None):
            raise ValueError("exactly one repair boundary identity is required")
        if repair_id is not None:
            resolved_repair_id = repair_id
        else:
            record = tx.run(
                "MATCH (run:CrmDealRepairRun {run_id: $run_id}) RETURN run.repair_id AS repair_id",
                run_id=run_id,
            ).single()
            if record is None or not isinstance(record["repair_id"], str):
                raise RuntimeError("repair qualification boundary is missing")
            resolved_repair_id = record["repair_id"]
        record = tx.run(GET_REPAIR_RUN, repair_id=resolved_repair_id).single()
        if record is None:
            raise RuntimeError("repair qualification boundary is missing")
        stored = stored_qualification_from_record(resolved_repair_id, record)
        snapshot = _snapshot_from_transaction(
            tx,
            stored.run.source_instance_id,
            stored.run.control_instance_id,
            stored.source_record_pks,
        )
        return snapshot.boundary_digest

    def supersede_captured_topology(
        self, *, control_instance_id: str, run_id: str, topology_digest: str
    ) -> None:
        def work(tx: ManagedTransaction) -> None:
            stored = tx.run(
                READ_REPAIR_TOPOLOGY_CAPTURE,
                run_id=run_id,
                control_instance_id=control_instance_id,
                topology_digest=topology_digest,
            ).single()
            if stored is None or not isinstance(stored["captures_json"], str):
                raise RuntimeError("repair topology capture is missing")
            decoded = json.loads(stored["captures_json"])
            if not isinstance(decoded, dict) or not isinstance(decoded.get("captures"), list):
                raise RuntimeError("repair topology capture is malformed")
            captures = decoded["captures"]
            record = tx.run(
                SUPERSEDE_REPAIR_TOPOLOGY,
                control_instance_id=control_instance_id,
                captures=captures,
            ).single()
            if record is None or int(record["superseded_count"]) != len(captures):
                raise RuntimeError("repair topology changed before exact supersession")

        self._client.execute_write(work)

    def supersede_topology_captures(
        self, *, control_instance_id: str, captures: list[dict[str, JsonValue]]
    ) -> None:
        def work(tx: ManagedTransaction) -> None:
            record = tx.run(
                SUPERSEDE_REPAIR_TOPOLOGY,
                control_instance_id=control_instance_id,
                captures=captures,
            ).single()
            if record is None or int(record["superseded_count"]) != len(captures):
                raise RuntimeError("repair topology changed before exact supersession")

        self._client.execute_write(work)

    def prepare_publication(
        self, control_instance_id: str, publication_key: str
    ) -> RepairPublicationReservation:
        reservation_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{control_instance_id}:{publication_key}")
        )

        def work(tx: ManagedTransaction) -> RepairPublicationReservation:
            return _reservation(
                tx.run(
                    PREPARE_REPAIR_AWARE_PUBLICATION,
                    control_instance_id=control_instance_id,
                    publication_key=publication_key,
                    reservation_id=reservation_id,
                ).single()
            )

        return self._client.execute_write(work)

    def mark_publishing(
        self, reservation: RepairPublicationReservation
    ) -> RepairPublicationReservation:
        def work(tx: ManagedTransaction) -> RepairPublicationReservation:
            return _reservation(
                tx.run(
                    MARK_REPAIR_AWARE_PUBLISHING,
                    control_instance_id=reservation.control_instance_id,
                    publication_key=reservation.publication_key,
                    reservation_id=reservation.reservation_id,
                    expected_revision=reservation.revision,
                ).single()
            )

        return self._client.execute_write(work)

    def confirm_publication(
        self, reservation: RepairPublicationReservation, workflow_task_id: str
    ) -> RepairPublicationReservation:
        def work(tx: ManagedTransaction) -> RepairPublicationReservation:
            return _reservation(
                tx.run(
                    CONFIRM_REPAIR_AWARE_PUBLICATION,
                    control_instance_id=reservation.control_instance_id,
                    publication_key=reservation.publication_key,
                    reservation_id=reservation.reservation_id,
                    expected_revision=reservation.revision,
                    workflow_task_id=workflow_task_id,
                ).single()
            )

        return self._client.execute_write(work)

    def _write_lease(
        self,
        query: str,
        request: RepairControlRequest,
        boundary_digest: str,
        control_instance_id: str,
    ) -> RepairDispatchLease:
        def work(tx: ManagedTransaction) -> RepairDispatchLease:
            return _lease(
                tx.run(
                    query,
                    repair_id=request.repair_id,
                    run_id=request.run_id,
                    owner_id=request.owner_id,
                    token=request.token,
                    expected_revision=request.expected_revision,
                    boundary_digest=boundary_digest,
                    control_instance_id=control_instance_id,
                ).single()
            )

        return self._client.execute_write(work)

    def _lease_only(self, query: str, request: RepairControlRequest) -> RepairDispatchLease:
        def work(tx: ManagedTransaction) -> RepairDispatchLease:
            return _lease(
                tx.run(
                    query,
                    run_id=request.run_id,
                    owner_id=request.owner_id,
                    token=request.token,
                    expected_revision=request.expected_revision,
                ).single()
            )

        return self._client.execute_write(work)


def _lease(record: object) -> RepairDispatchLease:
    if record is None:
        raise RuntimeError("repair dispatch compare-and-set was rejected")
    value = cast(Mapping[str, object], record)
    return RepairDispatchLease(
        str(value["control_instance_id"]),
        str(value["run_id"]),
        str(value["owner_id"]),
        str(value["token"]),
        _required_int(value["revision"], "dispatch revision"),
        _control_state(value["state"]),
        str(value["boundary_digest"]),
    )


def _reservation(record: object) -> RepairPublicationReservation:
    if record is None:
        raise RuntimeError("repair-aware publication reservation was rejected")
    value = cast(Mapping[str, object], record)
    return RepairPublicationReservation(
        str(value["reservation_id"]),
        str(value["control_instance_id"]),
        str(value["publication_key"]),
        _publication_state(value["state"]),
        _required_int(value["revision"], "publication revision"),
    )


def _control_state(value: object) -> RepairControlState:
    if value not in {"qualified", "quiescing", "quiesced", "allocated", "paused", "lost"}:
        raise RuntimeError("repair control state is invalid")
    return cast(RepairControlState, value)


def _publication_state(value: object) -> RepairPublicationState:
    if value not in {"preparing", "publishing", "confirmed"}:
        raise RuntimeError("repair publication state is invalid")
    return cast(RepairPublicationState, value)


def _required_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"{label} is invalid")
    return value


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_quiescence_state(
    value: object,
) -> Literal["not_quiesced", "quiesced"] | None:
    return (
        cast(Literal["not_quiesced", "quiesced"], value)
        if value in {"not_quiesced", "quiesced"}
        else None
    )


def _optional_allocation_state(
    value: object,
) -> Literal["not_allocated", "allocated"] | None:
    return (
        cast(Literal["not_allocated", "allocated"], value)
        if value in {"not_allocated", "allocated"}
        else None
    )


def _optional_paused_from_state(
    value: object,
) -> Literal["quiesced", "allocated"] | None:
    return (
        cast(Literal["quiesced", "allocated"], value)
        if value in {"quiesced", "allocated"}
        else None
    )
