"""Pure deterministic allocation from an approved #310 overlay."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from typing import cast

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.approval_overlay import ApprovalOverlay, ApprovalRow
from src.crm_deal_identity_repair.control_models import RepairAllocationCompletion
from src.crm_deal_identity_repair.digests import (
    CanonicalObjectDigest,
    canonical_json_line,
    inventory_binding_digest,
    object_digest,
)
from src.crm_deal_identity_repair.execution_records import RepairUnit
from src.crm_deal_identity_repair.models import RepairInventoryItem, RepairPartition
from src.models import JsonValue

_ALLOCATION_DOMAIN = b"crm-deal-identity-repair-allocation-v1\x00"
_ALLOCATION_ORIGIN_HMAC_DOMAIN = b"crm-deal-identity-repair-allocation-origin-v1\x00"


@dataclass(frozen=True)
class AllocationPlan:
    units: tuple[RepairUnit, ...]
    completion: RepairAllocationCompletion


def allocation_origin_hmac(
    *,
    secret: bytes,
    key_id: str,
    control_instance_id: str,
    run_id: str,
    owner_id: str,
    token_digest: str,
    revision: int,
    boundary_digest: str,
    sealed_boundary_digest: str,
    completion_id: str,
    overlay_digest: str,
    allocation_digest: str,
    unit_count: int,
    unit_set_digest: str,
    request_digest: str,
) -> str:
    """Authenticate immutable allocation-origin evidence with the approval key."""
    if not secret or not key_id:
        raise ValueError("allocation origin signing configuration is missing")
    payload: dict[str, JsonValue] = {
        "key_id": key_id,
        "control_instance_id": control_instance_id,
        "run_id": run_id,
        "owner_id": owner_id,
        "token_digest": token_digest,
        "revision": revision,
        "boundary_digest": boundary_digest,
        "sealed_boundary_digest": sealed_boundary_digest,
        "completion_id": completion_id,
        "overlay_digest": overlay_digest,
        "allocation_digest": allocation_digest,
        "unit_count": unit_count,
        "unit_set_digest": unit_set_digest,
        "request_digest": request_digest,
    }
    return hmac.new(
        secret,
        _ALLOCATION_ORIGIN_HMAC_DOMAIN + canonical_json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


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
    binding = inventory_binding_digest(item)
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


@dataclass(frozen=True)
class RebaseAllocationEvidence:
    """Compact, replayable expected-unit evidence for boundary rebasing."""

    completion: RepairAllocationCompletion
    unit_set_digest: str
    unit_ids: tuple[str, ...]
    _unit_batches: Callable[[], Iterator[list[dict[str, JsonValue]]]]

    def unit_batches(self) -> Iterator[list[dict[str, JsonValue]]]:
        """Re-read authenticated inventory and yield bounded expected-unit batches."""
        yield from self._unit_batches()


def stream_rebase_allocation_evidence(
    *,
    run_id: str,
    boundary_digest: str,
    overlay: ApprovalOverlay,
    inventory_lines: Callable[[], Iterator[bytes]],
    batch_size: int = 500,
) -> RebaseAllocationEvidence:
    """Derive legacy-identical allocation identities without retaining payloads or units."""
    if batch_size < 1:
        raise ValueError("rebase unit batch size must be positive")
    rows = {row.inventory_key: row for row in overlay.rows}
    if len(rows) != len(overlay.rows):
        raise ValueError("approval overlay has duplicate row identities")
    unit_ids = _stream_selected_unit_ids(run_id, boundary_digest, rows, inventory_lines)
    count = len(unit_ids)
    if count > overlay.unit_ceiling:
        raise ValueError("approval overlay executable ceiling is exceeded")
    allocation_digest = _stream_allocation_digest(
        run_id, boundary_digest, overlay.overlay_digest, unit_ids
    )
    completion = RepairAllocationCompletion(
        run_id,
        str(uuid.uuid5(uuid.NAMESPACE_URL, allocation_digest)),
        boundary_digest,
        overlay.overlay_digest,
        allocation_digest,
        count,
    )
    unit_set_digest = _stream_unit_set_digest(run_id, boundary_digest, rows, inventory_lines)

    def batches() -> Iterator[list[dict[str, JsonValue]]]:
        yield from _stream_unit_batches(run_id, boundary_digest, rows, inventory_lines, batch_size)

    return RebaseAllocationEvidence(completion, unit_set_digest, unit_ids, batches)


def _stream_selected_unit_ids(
    run_id: str,
    boundary_digest: str,
    rows: dict[str, ApprovalRow],
    inventory_lines: Callable[[], Iterator[bytes]],
) -> tuple[str, ...]:
    seen: set[str] = set()
    unit_ids: list[str] = []
    sequence = 0
    for item in _compact_inventory_items(inventory_lines):
        row = _validated_overlay_row(rows, item)
        if item.inventory_key in seen:
            raise ValueError("qualified inventory contains duplicate identities")
        seen.add(item.inventory_key)
        if row.disposition == "executable":
            if item.partition == "negative_control":
                raise ValueError("negative-control inventory rows are never executable")
            unit_ids.append(_unit(run_id, boundary_digest, item, sequence).unit_id)
            sequence += 1
    if seen != set(rows):
        raise ValueError("approval overlay row coverage is incomplete or changed")
    if len(unit_ids) != len(set(unit_ids)):
        raise RuntimeError("rebase allocation unit IDs are not unique")
    return tuple(unit_ids)


def _stream_allocation_digest(
    run_id: str,
    boundary_digest: str,
    overlay_digest: str,
    unit_ids: tuple[str, ...],
) -> str:
    digest = CanonicalObjectDigest(_ALLOCATION_DOMAIN)
    digest.value("boundary_digest", boundary_digest)
    digest.value("overlay_digest", overlay_digest)
    digest.value("run_id", run_id)
    digest.value("unit_count", len(unit_ids))
    digest.begin_array("unit_ids")
    for unit_id in unit_ids:
        digest.array_value(canonical_json_line(unit_id))
    digest.end_array()
    return digest.finish()


def _stream_unit_set_digest(
    run_id: str,
    boundary_digest: str,
    rows: dict[str, ApprovalRow],
    inventory_lines: Callable[[], Iterator[bytes]],
) -> str:
    digest = CanonicalObjectDigest(b"crm-deal-identity-repair-allocation-unit-set-v1\x00")
    digest.begin_array("units")
    sequence = 0
    for item in _compact_inventory_items(inventory_lines):
        row = _validated_overlay_row(rows, item)
        if row.disposition != "executable":
            continue
        unit = _unit(run_id, boundary_digest, item, sequence)
        digest.array_value(canonical_json_bytes(_unit_json(unit)))
        sequence += 1
    digest.end_array()
    return digest.finish()


def _stream_unit_batches(
    run_id: str,
    boundary_digest: str,
    rows: dict[str, ApprovalRow],
    inventory_lines: Callable[[], Iterator[bytes]],
    batch_size: int,
) -> Iterator[list[dict[str, JsonValue]]]:
    batch: list[dict[str, JsonValue]] = []
    sequence = 0
    for item in _compact_inventory_items(inventory_lines):
        row = _validated_overlay_row(rows, item)
        if row.disposition != "executable":
            continue
        batch.append(_unit_json(_unit(run_id, boundary_digest, item, sequence)))
        sequence += 1
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _compact_inventory_items(
    inventory_lines: Callable[[], Iterator[bytes]],
) -> Iterator[RepairInventoryItem]:
    for line in inventory_lines():
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError("qualified inventory row is malformed")
        values = cast(dict[str, JsonValue], raw)
        required = (
            "source_system",
            "source_record_id",
            "source_record_pk",
            "deal_id",
            "partition",
            "graph_fingerprint",
            "stored_payload_fingerprint",
        )
        if any(not isinstance(values.get(key), str) for key in required):
            raise ValueError("qualified inventory row is malformed")
        conditions = values.get("repair_conditions")
        if not isinstance(conditions, list) or not all(
            isinstance(item, str) for item in conditions
        ):
            raise ValueError("qualified inventory row is malformed")
        partition = cast(RepairPartition, values["partition"])
        item = RepairInventoryItem(
            source_system=cast(str, values["source_system"]),
            source_record_id=cast(str, values["source_record_id"]),
            source_record_pk=cast(str, values["source_record_pk"]),
            deal_id=cast(str, values["deal_id"]),
            partition=partition,
            repair_conditions=cast(tuple[RepairPartition, ...], tuple(conditions)),
            graph_fingerprint=cast(str, values["graph_fingerprint"]),
            stored_payload_fingerprint=cast(str, values["stored_payload_fingerprint"]),
            payload={},
        )
        yield item


def _validated_overlay_row(rows: dict[str, ApprovalRow], item: RepairInventoryItem) -> ApprovalRow:
    row = rows.get(item.inventory_key)
    if row is None:
        raise ValueError("approval overlay row coverage is incomplete or changed")
    if (
        row.source_record_pk != item.source_record_pk
        or row.graph_fingerprint != item.graph_fingerprint
        or row.stored_payload_fingerprint != item.stored_payload_fingerprint
    ):
        raise ValueError("approval overlay row fingerprint binding changed")
    return row


def _unit_json(unit: RepairUnit) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], asdict(unit))
