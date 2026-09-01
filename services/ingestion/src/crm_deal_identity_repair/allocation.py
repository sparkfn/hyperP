"""Pure deterministic allocation from an approved #310 overlay."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from src.crm_deal_identity_repair.approval_overlay import ApprovalOverlay
from src.crm_deal_identity_repair.control_models import RepairAllocationCompletion
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_records import RepairUnit
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.models import JsonValue

_ALLOCATION_DOMAIN = b"crm-deal-identity-repair-allocation-v1\x00"


@dataclass(frozen=True)
class AllocationPlan:
    units: tuple[RepairUnit, ...]
    completion: RepairAllocationCompletion


def plan_allocation(
    *,
    run_id: str,
    boundary_digest: str,
    inventory: tuple[RepairInventoryItem, ...],
    overlay: ApprovalOverlay,
) -> AllocationPlan:
    """Select exactly executable non-negative rows in canonical identity order."""
    if not inventory:
        raise ValueError("zero-unit allocation still requires a non-empty qualified inventory")
    by_key = {item.inventory_key: item for item in inventory}
    if len(by_key) != len(inventory):
        raise ValueError("qualified inventory contains duplicate identities")
    if set(by_key) != {row.inventory_key for row in overlay.rows}:
        raise ValueError("approval overlay row coverage is incomplete or changed")
    selected: list[RepairInventoryItem] = []
    for row in overlay.rows:
        item = by_key[row.inventory_key]
        if (item.source_record_pk, item.graph_fingerprint, item.stored_payload_fingerprint) != (
            row.source_record_pk,
            row.graph_fingerprint,
            row.stored_payload_fingerprint,
        ):
            raise ValueError("approval overlay row fingerprint binding changed")
        if row.disposition == "executable":
            if item.partition == "negative_control":
                raise ValueError("negative-control inventory rows are never executable")
            selected.append(item)
    selected.sort(key=lambda item: item.inventory_key)
    if len(selected) > overlay.unit_ceiling:
        raise ValueError("approval overlay executable ceiling is exceeded")
    units = tuple(
        _unit(run_id, boundary_digest, item, index) for index, item in enumerate(selected)
    )
    digest_payload: dict[str, JsonValue] = {
        "run_id": run_id,
        "boundary_digest": boundary_digest,
        "overlay_digest": overlay.overlay_digest,
        "unit_ids": [unit.unit_id for unit in units],
        "unit_count": len(units),
    }
    allocation_digest = object_digest(_ALLOCATION_DOMAIN, digest_payload)
    completion = RepairAllocationCompletion(
        run_id,
        str(uuid.uuid5(uuid.NAMESPACE_URL, allocation_digest)),
        boundary_digest,
        overlay.overlay_digest,
        allocation_digest,
        len(units),
    )
    return AllocationPlan(units, completion)


def _unit(
    run_id: str, boundary_digest: str, item: RepairInventoryItem, sequence: int
) -> RepairUnit:
    binding = object_digest(
        _ALLOCATION_DOMAIN,
        {
            "inventory_key": item.inventory_key,
            "source_record_pk": item.source_record_pk,
            "graph_fingerprint": item.graph_fingerprint,
            "stored_payload_fingerprint": item.stored_payload_fingerprint,
        },
    )
    unit_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{binding}"))
    return RepairUnit(
        run_id,
        unit_id,
        1,
        sequence,
        1,
        boundary_digest,
        binding,
        "allocated",
        item.inventory_key,
        item.source_record_pk,
        item.graph_fingerprint,
        item.stored_payload_fingerprint,
        binding,
    )
