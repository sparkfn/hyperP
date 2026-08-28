"""Read-only boundary snapshots and immutable #300 qualification writes."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from neo4j import ManagedTransaction, Record

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.digests import inventory_digest, object_digest
from src.crm_deal_identity_repair.execution_models import (
    RepairBoundaryDriftReason,
    RepairBoundarySnapshot,
    RepairExecutionBoundaryManifest,
    RepairQualificationRun,
    RepairRunStatus,
)
from src.crm_deal_identity_repair.inventory import (
    RepairInventoryReadClient,
    collect_repair_inventory,
)
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_ledger_records import (
    StoredQualification as _StoredQualification,
)
from src.graph.crm_deal_identity_repair_ledger_records import (
    canonical_json_text as _canonical_json_text,
)
from src.graph.crm_deal_identity_repair_ledger_records import (
    stored_qualification_from_record as _stored_qualification_from_record,
)
from src.graph.queries.crm_deal_identity_repair_ledger import (
    GET_REPAIR_RUN,
    QUALIFY_REPAIR_RUN,
    READ_INSTANCE_CONTROL_BOUNDARY,
    READ_SOURCE_RECORD_BOUNDARY,
    READ_STALE_RUN_CONTROL_EVIDENCE,
)
from src.models import JsonValue

_SOURCE_ROWS_DOMAIN = b"crm-deal-identity-repair-source-record-boundary-v1\x00"
_INSTANCE_DOMAIN = b"crm-deal-identity-repair-source-instance-boundary-v1\x00"
_STALE_RUN_DOMAIN = b"crm-deal-identity-repair-stale-run-boundary-v1\x00"
_CONTROL_DOMAIN = b"crm-deal-identity-repair-control-boundary-v1\x00"
T = TypeVar("T")


class ExpectedRepairBoundaryDriftError(Exception):
    """Expected persisted graph evidence drift, safe only for read-only status."""

    def __init__(self, reason: RepairBoundaryDriftReason) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class _QualificationParameters:
    repair_id: str
    run_id: str
    qualification_identity: str
    manifest_digest: str
    artifact_id: str
    artifact_manifest_hmac: str
    inventory_digest: str
    boundary_digest: str
    source_instance_id: str
    control_instance_id: str
    manifest_json: str
    source_record_pks_json: str
    inventory_row_count: int
    eligible_unit_count: int
    negative_control_count: int
    execution_allowed: bool


@dataclass(frozen=True)
class _CurrentInventoryBoundary:
    source_record_pks: tuple[str, ...]
    inventory_digest: str
    inventory_row_count: int
    eligible_unit_count: int
    negative_control_count: int
    stale_run_evidence: dict[str, JsonValue]


class _TransactionInventoryReader(RepairInventoryReadClient):
    def __init__(self, transaction: ManagedTransaction) -> None:
        self._transaction = transaction

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        return work(self._transaction)


class CrmDealRepairLedgerRepository:
    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def snapshot(
        self,
        *,
        source_instance_id: str,
        control_instance_id: str,
        source_record_pks: tuple[str, ...],
    ) -> RepairBoundarySnapshot:
        return self._client.execute_read(
            lambda tx: _snapshot_from_transaction(
                tx, source_instance_id, control_instance_id, source_record_pks
            )
        )

    def qualify(
        self,
        manifest: RepairExecutionBoundaryManifest,
        snapshot: RepairBoundarySnapshot,
    ) -> RepairQualificationRun:
        _assert_requested_boundary(manifest, snapshot)
        return self._client.execute_write(lambda tx: _qualify_transaction(tx, manifest, snapshot))

    def get_qualification(self, repair_id: str) -> RepairQualificationRun | None:
        stored = self._get_stored_qualification(repair_id)
        return None if stored is None else stored.run

    def get_execution_manifest(self, repair_id: str) -> RepairExecutionBoundaryManifest | None:
        stored = self._get_stored_qualification(repair_id)
        return None if stored is None else stored.manifest

    def source_record_pks(self, repair_id: str) -> tuple[str, ...]:
        stored = self._get_stored_qualification(repair_id)
        if stored is None:
            raise RuntimeError("repair ledger source record boundary is missing")
        return stored.source_record_pks

    def get_status(
        self,
        repair_id: str,
        snapshot: RepairBoundarySnapshot | None = None,
        drift_reason: RepairBoundaryDriftReason | None = None,
    ) -> RepairRunStatus:
        stored = self._get_stored_qualification(repair_id)
        if stored is None:
            return RepairRunStatus.not_qualified(repair_id)
        if drift_reason is not None:
            return RepairRunStatus.drifted(stored.run, drift_reason)
        if snapshot is None:
            return RepairRunStatus.drifted(stored.run, "persisted_boundary_change")
        if snapshot.boundary_digest != stored.run.boundary_digest:
            return RepairRunStatus.drifted(
                stored.run,
                "persisted_boundary_change",
                observed_boundary_digest=snapshot.boundary_digest,
            )
        return RepairRunStatus.admissible(stored.run, snapshot.boundary_digest)

    def _get_stored_qualification(self, repair_id: str) -> _StoredQualification | None:
        record = self._client.execute_read(
            lambda tx: tx.run(GET_REPAIR_RUN, repair_id=repair_id).single()
        )
        return None if record is None else _stored_qualification_from_record(repair_id, record)


def _snapshot_from_transaction(
    tx: ManagedTransaction,
    source_instance_id: str,
    control_instance_id: str,
    source_record_pks: tuple[str, ...],
) -> RepairBoundarySnapshot:
    inventory = _current_inventory_boundary(tx, source_record_pks)
    rows = _source_rows(tx, source_record_pks, source_instance_id)
    control = _control_row(tx, source_instance_id, control_instance_id)
    return RepairBoundarySnapshot(
        source_instance_id=source_instance_id,
        control_instance_id=control_instance_id,
        inventory_source_record_pks=inventory.source_record_pks,
        inventory_digest=inventory.inventory_digest,
        inventory_row_count=inventory.inventory_row_count,
        eligible_unit_count=inventory.eligible_unit_count,
        negative_control_count=inventory.negative_control_count,
        source_records_digest=object_digest(_SOURCE_ROWS_DOMAIN, {"rows": rows}),
        source_instance_digest=object_digest(_INSTANCE_DOMAIN, _instance_digest_value(control)),
        stale_run_evidence_digest=object_digest(_STALE_RUN_DOMAIN, inventory.stale_run_evidence),
        control_digest=object_digest(_CONTROL_DOMAIN, _control_digest_value(control)),
    )


def _current_inventory_boundary(
    tx: ManagedTransaction, expected_source_record_pks: tuple[str, ...]
) -> _CurrentInventoryBoundary:
    inventory = collect_repair_inventory(_TransactionInventoryReader(tx))
    source_record_pks = tuple(sorted(item.source_record_pk for item in inventory.items))
    if source_record_pks != expected_source_record_pks:
        raise ExpectedRepairBoundaryDriftError("persisted_boundary_change")
    negative_control_count = sum(item.partition == "negative_control" for item in inventory.items)
    stale_record = tx.run(
        READ_STALE_RUN_CONTROL_EVIDENCE,
        stale_run_id=inventory.stale_run_evidence["stale_run_id"],
    ).single()
    if stale_record is None:
        raise RuntimeError("repair stale-run control evidence readback is missing")
    return _CurrentInventoryBoundary(
        source_record_pks,
        inventory_digest(inventory.items),
        len(inventory.items),
        len(inventory.items) - negative_control_count,
        negative_control_count,
        {
            "inventory_evidence": _canonical_boundary_evidence(inventory.stale_run_evidence),
            "persisted_associations": _canonical_boundary_evidence(_record_json_dict(stale_record)),
        },
    )


def _source_rows(
    tx: ManagedTransaction,
    source_record_pks: tuple[str, ...],
    source_instance_id: str,
) -> list[JsonValue]:
    rows = [
        _record_json_dict(record)
        for record in tx.run(READ_SOURCE_RECORD_BOUNDARY, source_record_pks=list(source_record_pks))
    ]
    if len(rows) != len(source_record_pks) or any(row["source_record_id"] is None for row in rows):
        raise ExpectedRepairBoundaryDriftError("missing_source_record")
    if any(row["source_instance_id"] != source_instance_id for row in rows):
        raise ExpectedRepairBoundaryDriftError("source_instance_mismatch")
    return [row for row in rows]


def _control_row(
    tx: ManagedTransaction, source_instance_id: str, control_instance_id: str
) -> dict[str, JsonValue]:
    record = tx.run(
        READ_INSTANCE_CONTROL_BOUNDARY,
        source_instance_id=source_instance_id,
        control_instance_id=control_instance_id,
    ).single()
    if record is None:
        raise ExpectedRepairBoundaryDriftError("missing_control_evidence")
    control = _canonical_boundary_evidence(_record_json_dict(record))
    if not isinstance(control, dict):
        raise RuntimeError("repair control boundary must be a JSON object")
    _validate_control_boundary(control, source_instance_id, control_instance_id)
    return control


def _validate_control_boundary(
    control: Mapping[str, JsonValue], source_instance_id: str, control_instance_id: str
) -> None:
    source_active = _is_active_registration(control, "source")
    control_active = _is_active_registration(control, "control")
    if not source_active or not control_active:
        raise ExpectedRepairBoundaryDriftError("source_instance_disabled")
    if control["binding_count"] == 0:
        raise ExpectedRepairBoundaryDriftError("missing_binding")
    if control["dispatch_count"] != 1:
        raise ExpectedRepairBoundaryDriftError("missing_control_evidence")
    if (
        control["binding_count"] != 1
        or control["binding_ownership_count"] != 1
        or control["requested_binding_count"] != 1
        or control["requested_ownership_count"] != 1
        or control["binding_source_instance_ids"] != [source_instance_id]
        or control["binding_owner_instance_ids"] != [source_instance_id]
        or control["owned_binding_source_instance_ids"] != [source_instance_id]
        or control_instance_id == ""
    ):
        raise ExpectedRepairBoundaryDriftError("binding_mismatch")


def _is_active_registration(control: Mapping[str, JsonValue], prefix: str) -> bool:
    return (
        control[f"{prefix}_registration_count"] == 1
        and control[f"{prefix}_instance_of_count"] == 1
        and control[f"{prefix}_active_instance_of_count"] == 1
        and control[f"{prefix}_statuses"] == ["active"]
    )


def _instance_digest_value(control: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "source_registration_count": control["source_registration_count"],
        "source_instance_of_count": control["source_instance_of_count"],
        "source_active_instance_of_count": control["source_active_instance_of_count"],
        "source_statuses": control["source_statuses"],
        "control_registration_count": control["control_registration_count"],
        "control_instance_of_count": control["control_instance_of_count"],
        "control_active_instance_of_count": control["control_active_instance_of_count"],
        "control_statuses": control["control_statuses"],
    }


def _control_digest_value(control: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "binding_count": control["binding_count"],
        "binding_ownership_count": control["binding_ownership_count"],
        "requested_binding_count": control["requested_binding_count"],
        "requested_ownership_count": control["requested_ownership_count"],
        "binding_source_instance_ids": control["binding_source_instance_ids"],
        "binding_owner_instance_ids": control["binding_owner_instance_ids"],
        "owned_binding_source_instance_ids": control["owned_binding_source_instance_ids"],
        "dispatch_count": control["dispatch_count"],
        "dispatch_evidence": control["dispatch_evidence"],
        "control_nodes": control["control_nodes"],
        "control_relationships": control["control_relationships"],
    }


def _assert_requested_boundary(
    manifest: RepairExecutionBoundaryManifest, snapshot: RepairBoundarySnapshot
) -> None:
    if manifest.graph_boundary_digest != snapshot.boundary_digest:
        raise ExpectedRepairBoundaryDriftError("persisted_boundary_change")
    if (manifest.source_instance_id, manifest.control_instance_id) != (
        snapshot.source_instance_id,
        snapshot.control_instance_id,
    ):
        raise ExpectedRepairBoundaryDriftError("binding_mismatch")
    if (
        manifest.inventory_digest,
        manifest.inventory_row_count,
        manifest.eligible_unit_count,
        manifest.negative_control_count,
    ) != (
        snapshot.inventory_digest,
        snapshot.inventory_row_count,
        snapshot.eligible_unit_count,
        snapshot.negative_control_count,
    ):
        raise ExpectedRepairBoundaryDriftError("persisted_boundary_change")


def _qualify_transaction(
    tx: ManagedTransaction,
    manifest: RepairExecutionBoundaryManifest,
    snapshot: RepairBoundarySnapshot,
) -> RepairQualificationRun:
    admission_snapshot = _snapshot_from_transaction(
        tx,
        manifest.source_instance_id,
        manifest.control_instance_id,
        snapshot.inventory_source_record_pks,
    )
    if admission_snapshot != snapshot:
        raise ExpectedRepairBoundaryDriftError("persisted_boundary_change")
    _assert_requested_boundary(manifest, admission_snapshot)
    parameters = _qualification_parameters(manifest, admission_snapshot)
    record = tx.run(
        QUALIFY_REPAIR_RUN,
        repair_id=parameters.repair_id,
        run_id=parameters.run_id,
        qualification_identity=parameters.qualification_identity,
        manifest_digest=parameters.manifest_digest,
        artifact_id=parameters.artifact_id,
        artifact_manifest_hmac=parameters.artifact_manifest_hmac,
        inventory_digest=parameters.inventory_digest,
        boundary_digest=parameters.boundary_digest,
        source_instance_id=parameters.source_instance_id,
        control_instance_id=parameters.control_instance_id,
        manifest_json=parameters.manifest_json,
        source_record_pks_json=parameters.source_record_pks_json,
        inventory_row_count=parameters.inventory_row_count,
        eligible_unit_count=parameters.eligible_unit_count,
        negative_control_count=parameters.negative_control_count,
        execution_allowed=parameters.execution_allowed,
    ).single()
    if record is None or record["status"] != "qualified":
        raise RuntimeError("repair qualification conflicts with immutable ledger state")
    readback = tx.run(GET_REPAIR_RUN, repair_id=manifest.repair_id).single()
    if readback is None:
        raise RuntimeError("repair qualification readback is missing")
    stored = _stored_qualification_from_record(manifest.repair_id, readback)
    if (
        stored.manifest != manifest
        or stored.source_record_pks != snapshot.inventory_source_record_pks
    ):
        raise RuntimeError("repair qualification readback differs from requested boundary")
    return stored.run


def _qualification_parameters(
    manifest: RepairExecutionBoundaryManifest, snapshot: RepairBoundarySnapshot
) -> _QualificationParameters:
    return _QualificationParameters(
        repair_id=manifest.repair_id,
        run_id=str(uuid.uuid5(uuid.NAMESPACE_URL, manifest.qualification_identity)),
        qualification_identity=manifest.qualification_identity,
        manifest_digest=manifest.manifest_digest,
        artifact_id=manifest.artifact_id,
        artifact_manifest_hmac=manifest.artifact_manifest_hmac,
        inventory_digest=manifest.inventory_digest,
        boundary_digest=snapshot.boundary_digest,
        source_instance_id=manifest.source_instance_id,
        control_instance_id=manifest.control_instance_id,
        manifest_json=_canonical_json_text(manifest.to_dict(), "manifest"),
        source_record_pks_json=_canonical_json_text(
            {"source_record_pks": list(snapshot.inventory_source_record_pks)},
            "source record identities",
        ),
        inventory_row_count=manifest.inventory_row_count,
        eligible_unit_count=manifest.eligible_unit_count,
        negative_control_count=manifest.negative_control_count,
        execution_allowed=manifest.execution_allowed,
    )


def _record_json_dict(record: Record) -> dict[str, JsonValue]:
    return {key: record[key] for key in record.keys()}


def _canonical_boundary_evidence(value: JsonValue) -> JsonValue:
    """Normalize unordered read-only graph evidence before it is digested."""
    if isinstance(value, dict):
        return {key: _canonical_boundary_evidence(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        normalized = [_canonical_boundary_evidence(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: canonical_json_bytes({"value": item}),
        )
    return value
