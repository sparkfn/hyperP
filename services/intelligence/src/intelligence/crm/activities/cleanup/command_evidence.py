"""Receipt and archive evidence adapters for CRM activity cleanup commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from intelligence.crm.activities.cleanup.admission import AdmittedArchive
from intelligence.crm.activities.cleanup.config import CleanupConfig
from intelligence.crm.activities.cleanup.models import CleanupRequest
from intelligence.crm.activities.cleanup.receipt import CleanupReceipt, admit_published_receipt
from intelligence.crm.activities.cleanup.types import (
    CleanupAuthorization,
    CleanupIdentity,
    CleanupTarget,
    ResourceCeilings,
    canonical_digest,
)
from intelligence.repositories.protocols.crm_activity_cleanup import (
    CleanupPlan,
    ExactRecordInspection,
    ExpectedDeletionFact,
    LiveTargetIdentity,
    ParentIdentity,
    RecordOutcome,
)
from intelligence.state import State

_READY = "ready_for_batch_mutation"


def _authorization(admitted: AdmittedArchive) -> CleanupAuthorization:
    item = admitted.descriptor
    return CleanupAuthorization(
        item.checkpoint_id,
        item.run_id,
        item.snapshot_id,
        item.manifest_digest,
        item.boundary_digest,
        item.cleanup_identity_digest,
        item.request.database_identity,
    )


def _live_identities(admitted: AdmittedArchive) -> tuple[LiveTargetIdentity, ...]:
    values = tuple(_live_identity(record) for record in admitted.identities)
    if tuple(item.source_record_pk for item in values) != tuple(
        sorted(item.source_record_pk for item in values)
    ):
        raise RuntimeError("accepted cleanup identities are not canonical")
    return values


def _live_identity(value: Mapping[str, object]) -> LiveTargetIdentity:
    record_type = _required(value, "record_type")
    if record_type not in {"crm_history", "call"}:
        raise RuntimeError("accepted cleanup identity has unsupported record type")
    parent = _mapping(value.get("stored_parent"), "stored_parent")
    return LiveTargetIdentity(
        _required(value, "source_record_pk"),
        cast(Literal["crm_history", "call"], record_type),
        _required(value, "source_instance_id"),
        _required(value, "source_record_version"),
        _required(value, "record_hash"),
        _optional(value.get("lifecycle_status"), "lifecycle_status"),
        _optional(value.get("history_family"), "history_family"),
        ParentIdentity(
            _optional(parent.get("source_record_pk"), "parent source_record_pk"),
            _optional(parent.get("source_instance_id"), "parent source_instance_id"),
            _optional(parent.get("source_record_id"), "parent source_record_id"),
            _optional(parent.get("record_type"), "parent record_type"),
            _optional(parent.get("source_system"), "parent source_system"),
        ),
        _required(value, "source_key"),
        _parent_pks(value.get("child_parents"), "child_parents"),
        _parent_pks(value.get("details_parents"), "details_parents"),
    )


def _parent_pks(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RuntimeError(f"accepted cleanup identity {field} is invalid")
    result = tuple(sorted(_required(_mapping(item, field), "source_record_pk") for item in value))
    if len(set(result)) != len(result):
        raise RuntimeError(f"accepted cleanup identity {field} is duplicated")
    return result


def _create_receipt(
    cleanup_run_id: str,
    authorization: CleanupAuthorization,
    target: CleanupTarget,
    batch_size: int,
    config: CleanupConfig,
    admitted: AdmittedArchive,
    plan: CleanupPlan,
    inspections: tuple[ExactRecordInspection, ...],
) -> CleanupReceipt:
    ceilings = ResourceCeilings(
        config.archive.max_checkpoint_bytes,
        config.archive.max_checkpoint_entries,
        config.archive.max_rows,
        1_000,
    )
    return CleanupReceipt.create(
        cleanup_run_id,
        authorization,
        target,
        batch_size,
        ceilings,
        "crm-activities-cleanup-v1",
        _inspection_counts(inspections, len(plan.protected_evidence)),
        _receipt_identities(admitted, plan, inspections),
        plan.protected_evidence,
    )


def _inspection_counts(
    inspections: tuple[ExactRecordInspection, ...], protected_count: int
) -> dict[str, int]:
    return {
        "authorized_identity_count": len(inspections),
        "present_identity_count": sum(item.matching_node_count == 1 for item in inspections),
        "absent_identity_count": sum(item.matching_node_count == 0 for item in inspections),
        "duplicate_identity_count": sum(item.matching_node_count > 1 for item in inspections),
        "incident_relationship_count": sum(
            item.incident_relationship_count for item in inspections
        ),
        "protected_evidence_count": protected_count,
    }


def _receipt_identities(
    admitted: AdmittedArchive,
    plan: CleanupPlan,
    inspections: tuple[ExactRecordInspection, ...],
) -> tuple[CleanupIdentity, ...]:
    records = {_required(item, "source_record_pk"): item for item in admitted.identities}
    observed = {item.source_record_pk: item for item in inspections}
    protected: dict[str, list[object]] = {}
    for item in plan.protected_evidence:
        protected.setdefault(item.selected_source_record_pk, []).append(item.relationship.key())
    result: list[CleanupIdentity] = []
    for key in sorted(records):
        record, inspection = records[key], observed.get(key)
        if inspection is None:
            raise RuntimeError("receipt inspection does not cover accepted identity")
        record_type = _required(record, "record_type")
        if record_type not in {"crm_history", "call"}:
            raise RuntimeError("accepted cleanup identity has unsupported record type")
        relations = [item.key() for item in inspection.incident_relationships]
        dependencies = protected.get(key, [])
        result.append(
            CleanupIdentity(
                key,
                cast(Literal["crm_history", "call"], record_type),
                _required(record, "source_instance_id"),
                _required(record, "source_record_version"),
                _required(record, "record_hash"),
                canonical_digest(record),
                _required(record, "reference_fingerprint"),
                inspection.incident_relationship_count,
                canonical_digest(relations),
                len(dependencies),
                canonical_digest(dependencies),
            )
        )
    return tuple(result)


def _receipt(
    state: State,
    receipt_run_id: str | None,
    receipt_digest: str | None,
    cleanup_run_id: str,
    authorization: CleanupAuthorization,
    target: CleanupTarget,
    request: CleanupRequest,
) -> CleanupReceipt:
    if not isinstance(receipt_run_id, str) or not isinstance(receipt_digest, str):
        raise RuntimeError("cleanup requires State receipt run ID and logical digest")
    result = admit_published_receipt(state.workspace, state, receipt_run_id, receipt_digest)
    if (
        result.cleanup_run_id != cleanup_run_id
        or result.authorization != authorization
        or result.target != target
    ):
        raise RuntimeError("cleanup receipt does not bind admitted authorization and target")
    if result.batch_size != request.batch_size:
        raise RuntimeError("cleanup request cannot override receipt batch size")
    return result


def _require_receipt_identities(
    receipt: CleanupReceipt, identities: tuple[LiveTargetIdentity, ...]
) -> None:
    actual = tuple(
        (
            item.source_record_pk,
            item.record_type,
            item.source_instance_id,
            item.source_record_version,
            item.record_hash,
        )
        for item in receipt.identities
    )
    expected = tuple(
        (
            item.source_record_pk,
            item.record_type,
            item.source_instance_id,
            item.source_record_version,
            item.record_hash,
        )
        for item in identities
    )
    if tuple(sorted(actual)) != tuple(sorted(expected)):
        raise RuntimeError("cleanup receipt does not bind the exact admitted target identities")


def _bind_plan_to_receipt(
    receipt: CleanupReceipt,
    current_identities: tuple[CleanupIdentity, ...],
    plan: CleanupPlan,
) -> CleanupPlan:
    """Prevent live graph drift from expanding the receipt-authorized deletion plan."""
    receipt_by_id = {item.source_record_pk: item for item in receipt.identities}
    current_by_id = {item.source_record_pk: item for item in current_identities}
    outcomes = {item.source_record_pk: item for item in plan.outcomes}
    expected = {item.target.source_record_pk: item for item in plan.expected_deletions}
    rebound: list[RecordOutcome] = []
    deletions: list[ExpectedDeletionFact] = []
    for key in sorted(receipt_by_id):
        outcome = outcomes[key]
        if outcome.reason_code == _READY and current_by_id[key] != receipt_by_id[key]:
            rebound.append(RecordOutcome(key, "conflict", "receipt_graph_evidence_drift"))
            continue
        rebound.append(outcome)
        if outcome.reason_code == _READY:
            deletions.append(expected[key])
    return CleanupPlan(
        tuple(sorted(deletions, key=lambda item: item.target.source_record_pk)),
        plan.protected_evidence,
        tuple(rebound),
    )


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RuntimeError(f"{field} is invalid")
    return value


def _required(value: Mapping[str, object], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise RuntimeError(f"{field} is invalid")
    return result


def _optional(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{field} is invalid")
    return value
