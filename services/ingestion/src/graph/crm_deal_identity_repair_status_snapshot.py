"""Bounded transaction-local current-boundary calculation for repair status."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.bounded import CanonicalByteSorter
from src.crm_deal_identity_repair.digests import inventory_digest_from_parts, object_digest
from src.crm_deal_identity_repair.execution_models import (
    RepairBoundaryDriftReason,
    RepairBoundarySnapshot,
)
from src.crm_deal_identity_repair.inventory import (
    REPAIR_INVENTORY_PAGE_SIZE,
    iter_repair_inventory_pages,
    page_inventory_item,
    page_projection_rows,
    stale_run_evidence_from_record,
    validate_repair_inventory_keys,
)
from src.graph.crm_deal_identity_repair_boundary_evidence import (
    canonical_boundary_evidence,
    record_json_dict,
)
from src.graph.crm_deal_identity_repair_status_evidence import (
    CanonicalObjectDigest as _CanonicalObjectDigest,
    canonical_json_line as _canonical_line,
    spool_evidence as _spool_evidence,
)
from src.graph.queries.crm_deal_identity_repair import INVENTORY_STALE_RUN_CONTROL_PLANE
from src.graph.queries.crm_deal_identity_repair_ledger import (
    READ_CONTROL_DISPATCH_EVIDENCE,
    READ_CONTROL_NODES,
    READ_CONTROL_RELATIONSHIPS,
    READ_INSTANCE_CONTROL_BOUNDARY,
    READ_SOURCE_RECORD_BOUNDARY,
    READ_STALE_RUN_ASSOCIATIONS,
    READ_STALE_RUN_CONTROL_EVIDENCE,
)
from src.models import JsonValue

_SOURCE_ROWS_DOMAIN = b"crm-deal-identity-repair-source-record-boundary-v1\x00"
_INSTANCE_DOMAIN = b"crm-deal-identity-repair-source-instance-boundary-v1\x00"
_STALE_RUN_DOMAIN = b"crm-deal-identity-repair-stale-run-boundary-v1\x00"
_CONTROL_DOMAIN = b"crm-deal-identity-repair-control-boundary-v1\x00"


class ExpectedRepairBoundaryDriftError(Exception):
    """Expected persisted graph evidence drift, safe only for read-only status."""

    def __init__(self, reason: RepairBoundaryDriftReason) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class _InventoryBoundary:
    inventory_digest: str
    inventory_row_count: int
    eligible_unit_count: int
    negative_control_count: int
    stale_run_evidence_digest: str


def status_snapshot_from_transaction(
    tx: ManagedTransaction,
    source_instance_id: str,
    control_instance_id: str,
    source_record_pks: tuple[str, ...],
) -> RepairBoundarySnapshot:
    """Read every current boundary component in one bounded managed transaction."""
    inventory = _current_inventory_boundary(tx, source_record_pks)
    source_records_digest = _source_records_digest(tx, source_record_pks, source_instance_id)
    source_instance_digest, control_digest = _control_digests(
        tx,
        source_instance_id,
        control_instance_id,
    )
    return RepairBoundarySnapshot(
        source_instance_id=source_instance_id,
        control_instance_id=control_instance_id,
        inventory_source_record_pks=source_record_pks,
        inventory_digest=inventory.inventory_digest,
        inventory_row_count=inventory.inventory_row_count,
        eligible_unit_count=inventory.eligible_unit_count,
        negative_control_count=inventory.negative_control_count,
        source_records_digest=source_records_digest,
        source_instance_digest=source_instance_digest,
        stale_run_evidence_digest=inventory.stale_run_evidence_digest,
        control_digest=control_digest,
    )


def _current_inventory_boundary(
    tx: ManagedTransaction,
    expected_source_record_pks: tuple[str, ...],
) -> _InventoryBoundary:
    validate_repair_inventory_keys(tx, "bitrix_chat")
    expected_index = 0
    inventory_count = 0
    negative_control_count = 0
    with CanonicalByteSorter(unique_keys=True) as sorter:
        for page in iter_repair_inventory_pages(tx, source_system="bitrix_chat"):
            page_pks = tuple(_record_string(record, "source_record_pk") for record in page)
            _advance_expected_inventory(page_pks, expected_source_record_pks, expected_index)
            expected_index += len(page_pks)
            projections = page_projection_rows(
                tx,
                source_system="bitrix_chat",
                source_record_pks=page_pks,
            )
            for record in page:
                source_record_pk = _record_string(record, "source_record_pk")
                item = page_inventory_item(record, projections.get(source_record_pk, []))
                sorter.add(item.inventory_key.encode("utf-8"), canonical_json_bytes(item.to_dict()))
                inventory_count += 1
                if item.partition == "negative_control":
                    negative_control_count += 1
            del projections
        if expected_index != len(expected_source_record_pks):
            raise ExpectedRepairBoundaryDriftError("persisted_boundary_change")
        stale_run_evidence = _stale_run_evidence(tx)
        stale_run_evidence_digest = _stale_run_evidence_digest(tx, stale_run_evidence)
        inventory_digest = inventory_digest_from_parts(sorter.values())
    return _InventoryBoundary(
        inventory_digest=inventory_digest,
        inventory_row_count=inventory_count,
        eligible_unit_count=inventory_count - negative_control_count,
        negative_control_count=negative_control_count,
        stale_run_evidence_digest=stale_run_evidence_digest,
    )


def _advance_expected_inventory(
    observed: tuple[str, ...],
    expected: tuple[str, ...],
    expected_index: int,
) -> None:
    for offset, source_record_pk in enumerate(observed):
        index = expected_index + offset
        if index >= len(expected) or source_record_pk != expected[index]:
            raise ExpectedRepairBoundaryDriftError("persisted_boundary_change")


def _stale_run_evidence(tx: ManagedTransaction) -> dict[str, JsonValue]:
    result = tx.run(
        INVENTORY_STALE_RUN_CONTROL_PLANE,
        source_system="bitrix_chat",
        stale_run_id="e5deb1d6-7333-4660-be4f-c44fcf5af686",
    )
    records = tuple(result)
    result.consume()
    if len(records) != 1:
        raise ValueError("repair stale-run graph evidence is unavailable")
    return stale_run_evidence_from_record(records[0])


def _stale_run_evidence_digest(
    tx: ManagedTransaction,
    inventory_evidence: dict[str, JsonValue],
) -> str:
    stale_run_id = inventory_evidence["stale_run_id"]
    if not isinstance(stale_run_id, str):
        raise RuntimeError("repair stale-run inventory evidence is malformed")
    with ExitStack() as stack:
        persisted_run, persisted_run_count = _spool_evidence(
            stack,
            tx,
            READ_STALE_RUN_CONTROL_EVIDENCE,
            stale_run_id=stale_run_id,
        )
        if persisted_run_count == 0:
            raise RuntimeError("repair stale-run control evidence readback is missing")
        associations, _ = _spool_evidence(
            stack,
            tx,
            READ_STALE_RUN_ASSOCIATIONS,
            stale_run_id=stale_run_id,
        )
        digest = _CanonicalObjectDigest(_STALE_RUN_DOMAIN)
        normalized = canonical_boundary_evidence(inventory_evidence)
        if not isinstance(normalized, dict):
            raise RuntimeError("repair stale-run inventory evidence is malformed")
        digest.value("inventory_evidence", normalized)
        digest.array("persisted_associations", associations.values())
        digest.array("persisted_run", persisted_run.values())
        return digest.finish()


def _source_records_digest(
    tx: ManagedTransaction,
    source_record_pks: tuple[str, ...],
    source_instance_id: str,
) -> str:
    digest = _CanonicalObjectDigest(_SOURCE_ROWS_DOMAIN)
    missing_source_record = False
    source_instance_mismatch = False
    digest.begin_array("rows")
    for offset in range(0, len(source_record_pks), REPAIR_INVENTORY_PAGE_SIZE):
        page_pks = source_record_pks[offset : offset + REPAIR_INVENTORY_PAGE_SIZE]
        result = tx.run(READ_SOURCE_RECORD_BOUNDARY, source_record_pks=list(page_pks))
        records = tuple(result)
        result.consume()
        if len(records) != len(page_pks):
            missing_source_record = True
            continue
        for expected_pk, record in zip(page_pks, records, strict=True):
            row = record_json_dict(record)
            if row.get("source_record_pk") != expected_pk or row.get("source_record_id") is None:
                missing_source_record = True
            if row.get("source_instance_id") != source_instance_id:
                source_instance_mismatch = True
            digest.array_value(_canonical_line(row))
    if missing_source_record:
        raise ExpectedRepairBoundaryDriftError("missing_source_record")
    if source_instance_mismatch:
        raise ExpectedRepairBoundaryDriftError("source_instance_mismatch")
    digest.end_array()
    return digest.finish()


def _control_digests(
    tx: ManagedTransaction,
    source_instance_id: str,
    control_instance_id: str,
) -> tuple[str, str]:
    result = tx.run(
        READ_INSTANCE_CONTROL_BOUNDARY,
        source_instance_id=source_instance_id,
        control_instance_id=control_instance_id,
    )
    records = tuple(result)
    result.consume()
    if len(records) != 1:
        raise ExpectedRepairBoundaryDriftError("missing_control_evidence")
    control_value = canonical_boundary_evidence(record_json_dict(records[0]))
    if not isinstance(control_value, dict):
        raise RuntimeError("repair control boundary must be a JSON object")
    with ExitStack() as stack:
        dispatch, dispatch_count = _spool_evidence(
            stack,
            tx,
            READ_CONTROL_DISPATCH_EVIDENCE,
            control_instance_id=control_instance_id,
        )
        control_nodes, _ = _spool_evidence(
            stack,
            tx,
            READ_CONTROL_NODES,
            control_instance_id=control_instance_id,
        )
        relationships, _ = _spool_evidence(
            stack,
            tx,
            READ_CONTROL_RELATIONSHIPS,
            control_instance_id=control_instance_id,
        )
        control_value["dispatch_count"] = dispatch_count
        _validate_control_boundary(control_value, source_instance_id, control_instance_id)
        instance_digest = object_digest(_INSTANCE_DOMAIN, _instance_digest_value(control_value))
        digest = _CanonicalObjectDigest(_CONTROL_DOMAIN)
        _write_control_digest(
            digest,
            control_value,
            dispatch.values(),
            control_nodes.values(),
            relationships.values(),
        )
        return instance_digest, digest.finish()


def _write_control_digest(
    digest: _CanonicalObjectDigest,
    control: dict[str, JsonValue],
    dispatch: Iterable[bytes],
    control_nodes: Iterable[bytes],
    relationships: Iterable[bytes],
) -> None:
    for key in (
        "binding_count",
        "binding_owner_instance_ids",
        "binding_ownership_count",
        "binding_source_instance_ids",
    ):
        digest.value(key, _control_value(control, key))
    digest.array("control_nodes", control_nodes)
    digest.array("control_relationships", relationships)
    digest.value("dispatch_count", _control_value(control, "dispatch_count"))
    digest.array("dispatch_evidence", dispatch)
    for key in (
        "owned_binding_source_instance_ids",
        "requested_binding_count",
        "requested_ownership_count",
    ):
        digest.value(key, _control_value(control, key))


def _control_value(control: dict[str, JsonValue], key: str) -> JsonValue:
    if key not in control:
        raise RuntimeError(f"repair control boundary omitted {key}")
    return control[key]


def _instance_digest_value(control: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "source_registration_count": _control_value(control, "source_registration_count"),
        "source_instance_of_count": _control_value(control, "source_instance_of_count"),
        "source_active_instance_of_count": _control_value(
            control,
            "source_active_instance_of_count",
        ),
        "source_statuses": _control_value(control, "source_statuses"),
        "control_registration_count": _control_value(control, "control_registration_count"),
        "control_instance_of_count": _control_value(control, "control_instance_of_count"),
        "control_active_instance_of_count": _control_value(
            control,
            "control_active_instance_of_count",
        ),
        "control_statuses": _control_value(control, "control_statuses"),
    }


def _validate_control_boundary(
    control: dict[str, JsonValue],
    source_instance_id: str,
    control_instance_id: str,
) -> None:
    source_active = _is_active_registration(control, "source")
    control_active = _is_active_registration(control, "control")
    if not source_active or not control_active:
        raise ExpectedRepairBoundaryDriftError("source_instance_disabled")
    if control.get("binding_count") == 0:
        raise ExpectedRepairBoundaryDriftError("missing_binding")
    if control.get("dispatch_count") != 1:
        raise ExpectedRepairBoundaryDriftError("missing_control_evidence")
    if (
        control.get("binding_count") != 1
        or control.get("binding_ownership_count") != 1
        or control.get("requested_binding_count") != 1
        or control.get("requested_ownership_count") != 1
        or control.get("binding_source_instance_ids") != [source_instance_id]
        or control.get("binding_owner_instance_ids") != [source_instance_id]
        or control.get("owned_binding_source_instance_ids") != [source_instance_id]
        or control_instance_id == ""
    ):
        raise ExpectedRepairBoundaryDriftError("binding_mismatch")


def _is_active_registration(control: dict[str, JsonValue], prefix: str) -> bool:
    return (
        control.get(f"{prefix}_registration_count") == 1
        and control.get(f"{prefix}_instance_of_count") == 1
        and control.get(f"{prefix}_active_instance_of_count") == 1
        and control.get(f"{prefix}_statuses") == ["active"]
    )


def _record_string(record: Record, key: str) -> str:
    value = record[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"repair inventory {key} must be a non-empty string")
    return value
