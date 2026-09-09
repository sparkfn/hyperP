"""Receipt and archive evidence adapters for CRM activity cleanup commands."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, cast

from intelligence.crm.activities.cleanup.admission import AdmittedArchive
from intelligence.crm.activities.cleanup.config import CleanupConfig
from intelligence.crm.activities.cleanup.models import CleanupRequest
from intelligence.crm.activities.cleanup.quiescence import admit_published_quiescence
from intelligence.crm.activities.cleanup.receipt import CleanupReceipt, admit_published_receipt
from intelligence.crm.activities.cleanup.types import (
    AuthorizedCompanionRelationship,
    CleanupAuthorization,
    CleanupIdentity,
    CleanupTarget,
    ProtectedSourceEndpointEvidence,
    QuiescenceEvidence,
    ResourceCeilings,
    canonical_digest,
)
from intelligence.repositories.protocols.crm_activity_cleanup import (
    CleanupPlan,
    ExactRecordInspection,
    ExpectedDeletionFact,
    IncidentRelationship,
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
        source_record_pk=_required(value, "source_record_pk"),
        record_type=cast(Literal["crm_history", "call"], record_type),
        source_instance_id=_required(value, "source_instance_id"),
        source_record_version=_required(value, "source_record_version"),
        record_hash=_required(value, "record_hash"),
        lifecycle_status=_optional(value.get("lifecycle_status"), "lifecycle_status"),
        history_family=_optional(value.get("history_family"), "history_family"),
        stored_parent=ParentIdentity(
            _optional(parent.get("source_record_pk"), "parent source_record_pk"),
            _optional(parent.get("source_instance_id"), "parent source_instance_id"),
            _optional(parent.get("source_record_id"), "parent source_record_id"),
            _optional(parent.get("record_type"), "parent record_type"),
            _optional(parent.get("source_system"), "parent source_system"),
        ),
        source_key=_required(value, "source_key"),
        child_parent_source_record_pks=_parent_pks(value.get("child_parents"), "child_parents"),
        details_parent_source_record_pks=_parent_pks(
            value.get("details_parents"), "details_parents"
        ),
        source_record_id=_optional(value.get("source_record_id"), "source_record_id"),
        source_version_key=_optional(value.get("source_version_key"), "source_version_key"),
        history_source=_optional(value.get("history_source"), "history_source"),
        projection_source=_optional(value.get("projection_source"), "projection_source"),
        projection_version=_optional(value.get("projection_version"), "projection_version"),
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
    quiescence: QuiescenceEvidence,
) -> CleanupReceipt:
    ceilings = ResourceCeilings(
        config.archive.max_checkpoint_bytes,
        config.archive.max_checkpoint_entries,
        config.archive.max_rows,
        1_000,
    )
    companions = _authorized_companion_relationships(admitted, plan, inspections)
    source_endpoints = _protected_source_endpoints(inspections, quiescence)
    return CleanupReceipt.create(
        cleanup_run_id,
        authorization,
        target,
        batch_size,
        ceilings,
        "crm-activities-cleanup-v1",
        quiescence,
        _inspection_counts(inspections, len(plan.protected_evidence)),
        _receipt_identities(admitted, plan, inspections, companions),
        plan.protected_evidence,
        source_endpoints,
        companions,
    )


def _quiescence(
    state: State,
    quiescence_run_id: str | None,
    quiescence_digest: str | None,
    authorization: CleanupAuthorization,
    target: CleanupTarget,
) -> QuiescenceEvidence:
    """Admit the one State-registered operational proof bound to this cleanup request."""
    if not isinstance(quiescence_run_id, str) or not isinstance(quiescence_digest, str):
        raise RuntimeError("cleanup requires State quiescence run ID and evidence digest")
    evidence = admit_published_quiescence(
        state.workspace, state, quiescence_run_id, quiescence_digest
    )
    try:
        evidence.require_matches(authorization, target)
    except ValueError as error:
        raise RuntimeError(
            "cleanup quiescence does not bind admitted authorization and target"
        ) from error
    return evidence


def _protected_source_endpoints(
    inspections: Sequence[ExactRecordInspection],
    quiescence: QuiescenceEvidence,
) -> tuple[ProtectedSourceEndpointEvidence, ...]:
    """Capture one exact preserved FROM_SOURCE endpoint per manifest-selected identity."""
    result: list[ProtectedSourceEndpointEvidence] = []
    for inspection in inspections:
        if inspection.matching_node_count == 0:
            continue
        if inspection.matching_node_count != 1:
            raise RuntimeError("protected source endpoint identity is ambiguous")
        candidates = tuple(
            relationship
            for relationship in inspection.incident_relationships
            if relationship.relationship_type == "FROM_SOURCE"
            and relationship.direction == "outbound"
            and "SourceSystem" in relationship.other_endpoint.labels
            and relationship.other_endpoint.source_key == quiescence.source_key
        )
        if len(candidates) != 1:
            raise RuntimeError("cleanup identity lacks one exact preserved FROM_SOURCE endpoint")
        endpoint = candidates[0].other_endpoint
        result.append(
            ProtectedSourceEndpointEvidence(
                inspection.source_record_pk,
                candidates[0].relationship_element_id,
                "FROM_SOURCE",
                "outbound",
                endpoint.element_id,
                endpoint.labels,
                quiescence.source_key,
            )
        )
    return tuple(sorted(result, key=ProtectedSourceEndpointEvidence.key))


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
    authorized_companions: Sequence[AuthorizedCompanionRelationship] = (),
    successful_prior_call_pks: frozenset[str] = frozenset(),
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
        relations, virtual_count = _relationship_digest_rows(
            key,
            inspection.incident_relationships,
            authorized_companions,
            successful_prior_call_pks,
        )
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
                inspection.incident_relationship_count + virtual_count,
                canonical_digest(relations),
                len(dependencies),
                canonical_digest(dependencies),
            )
        )
    return tuple(result)


def _relationship_digest_rows(
    selected_source_record_pk: str,
    relationships: Sequence[IncidentRelationship],
    authorized_companions: Sequence[AuthorizedCompanionRelationship],
    successful_prior_call_pks: frozenset[str],
) -> tuple[list[object], int]:
    relevant = {
        item.relationship_element_id: item
        for item in authorized_companions
        if selected_source_record_pk in {item.call_source_record_pk, item.activity_source_record_pk}
    }
    observed_ids: set[str] = set()
    rows: list[object] = []
    for relationship in relationships:
        companion = relevant.get(relationship.relationship_element_id)
        if companion is None:
            rows.append({"relationship": relationship.key()})
            continue
        if not _matches_selected_companion(selected_source_record_pk, relationship, companion):
            raise RuntimeError("authorized companion relationship evidence drifted")
        observed_ids.add(companion.relationship_element_id)
        rows.append(
            {
                "authorized_companion": companion.as_dict(),
                "selected_source_record_pk": selected_source_record_pk,
            }
        )
    virtual = tuple(
        item
        for item in relevant.values()
        if item.relationship_element_id not in observed_ids
        and item.call_source_record_pk in successful_prior_call_pks
    )
    rows.extend(
        {
            "authorized_companion": item.as_dict(),
            "selected_source_record_pk": selected_source_record_pk,
        }
        for item in virtual
    )
    return sorted(rows, key=canonical_digest), len(virtual)


def _matches_selected_companion(
    selected_source_record_pk: str,
    relationship: IncidentRelationship,
    companion: AuthorizedCompanionRelationship,
) -> bool:
    if selected_source_record_pk == companion.call_source_record_pk:
        return (
            relationship.direction == companion.call_direction
            and relationship.other_endpoint.source_record_pk == companion.activity_source_record_pk
            and relationship.relationship_type == companion.relationship_type
        )
    return (
        selected_source_record_pk == companion.activity_source_record_pk
        and relationship.direction == companion.activity_direction
        and relationship.other_endpoint.source_record_pk == companion.call_source_record_pk
        and relationship.relationship_type == companion.relationship_type
    )


def _authorized_companion_relationships(
    admitted: AdmittedArchive,
    plan: CleanupPlan,
    inspections: tuple[ExactRecordInspection, ...],
) -> tuple[AuthorizedCompanionRelationship, ...]:
    """Capture only dry-run-owned call-to-activity parent edges as receipt evidence."""
    records = {_required(item, "source_record_pk"): item for item in admitted.identities}
    observed = {item.source_record_pk: item for item in inspections}
    result: list[AuthorizedCompanionRelationship] = []
    for fact in plan.expected_deletions:
        if fact.target.record_type != "call":
            continue
        call_pk = fact.target.source_record_pk
        for relationship in fact.incident_relationships:
            if relationship.relationship_element_id not in fact.owned_relationship_element_ids:
                continue
            activity_pk = relationship.other_endpoint.source_record_pk
            if (
                relationship.direction != "outbound"
                or relationship.relationship_type not in {"CHILD_OF", "DETAILS_HISTORY_ITEM"}
                or activity_pk is None
                or _record_type(records.get(activity_pk)) != "crm_history"
            ):
                continue
            activity = observed.get(activity_pk)
            if activity is None or not _has_inverse_companion(activity, relationship, call_pk):
                raise RuntimeError(
                    "owned companion relationship lacks exact authorized activity endpoint"
                )
            result.append(
                AuthorizedCompanionRelationship(
                    relationship.relationship_element_id,
                    cast(
                        Literal["CHILD_OF", "DETAILS_HISTORY_ITEM"],
                        relationship.relationship_type,
                    ),
                    call_pk,
                    activity_pk,
                    "outbound",
                    "inbound",
                )
            )
    return tuple(sorted(set(result), key=AuthorizedCompanionRelationship.key))


def _reconcile_activity_relationships_after_successful_calls(
    receipt: CleanupReceipt,
    activity_source_record_pk: str,
    expected: Sequence[IncidentRelationship],
    observed: Sequence[IncidentRelationship],
    successful_prior_call_pks: frozenset[str],
) -> tuple[IncidentRelationship, ...]:
    """Allow only receipt-authorized parent edges removed by proven prior call outcomes."""
    identities = {item.source_record_pk: item.record_type for item in receipt.identities}
    if identities.get(activity_source_record_pk) != "crm_history":
        raise RuntimeError("activity relationship adjustment is outside receipt authorization")
    if any(identities.get(call_pk) != "call" for call_pk in successful_prior_call_pks):
        raise RuntimeError("successful prior call is outside receipt authorization")
    expected_by_id = _relationships_by_id(expected, "expected")
    observed_by_id = _relationships_by_id(observed, "observed")
    if any(
        key not in expected_by_id or expected_by_id[key] != relationship
        for key, relationship in observed_by_id.items()
    ):
        raise RuntimeError("activity relationship changed outside receipt authorization")
    authorized = tuple(
        item
        for item in receipt.authorized_companion_relationships
        if item.activity_source_record_pk == activity_source_record_pk
        and item.call_source_record_pk in successful_prior_call_pks
    )
    allowed_ids = {item.relationship_element_id for item in authorized}
    missing_ids = set(expected_by_id) - set(observed_by_id)
    if missing_ids != allowed_ids:
        raise RuntimeError(
            "activity relationship difference is not an authorized prior call removal"
        )
    for item in authorized:
        relationship = expected_by_id.get(item.relationship_element_id)
        if relationship is None or not _matches_activity_companion(relationship, item):
            raise RuntimeError("authorized companion relationship does not match activity evidence")
    return tuple(sorted(observed_by_id.values(), key=IncidentRelationship.key))


def _has_inverse_companion(
    activity: ExactRecordInspection, call_relationship: IncidentRelationship, call_pk: str
) -> bool:
    matches = tuple(
        item
        for item in activity.incident_relationships
        if item.relationship_element_id == call_relationship.relationship_element_id
    )
    return len(matches) == 1 and _matches_activity_companion(
        matches[0],
        AuthorizedCompanionRelationship(
            call_relationship.relationship_element_id,
            cast(
                Literal["CHILD_OF", "DETAILS_HISTORY_ITEM"],
                call_relationship.relationship_type,
            ),
            call_pk,
            activity.source_record_pk,
            "outbound",
            "inbound",
        ),
    )


def _matches_activity_companion(
    relationship: IncidentRelationship, authorized: AuthorizedCompanionRelationship
) -> bool:
    return (
        relationship.relationship_element_id == authorized.relationship_element_id
        and relationship.relationship_type == authorized.relationship_type
        and relationship.direction == authorized.activity_direction
        and relationship.other_endpoint.source_record_pk == authorized.call_source_record_pk
    )


def _relationships_by_id(
    relationships: Sequence[IncidentRelationship], field: str
) -> dict[str, IncidentRelationship]:
    result = {item.relationship_element_id: item for item in relationships}
    if len(result) != len(relationships):
        raise RuntimeError(f"{field} activity relationships contain duplicate element identities")
    return result


def _record_type(value: Mapping[str, object] | None) -> str | None:
    if value is None:
        return None
    raw = value.get("record_type")
    return raw if isinstance(raw, str) else None


def _receipt(
    state: State,
    receipt_run_id: str | None,
    receipt_digest: str | None,
    cleanup_run_id: str,
    authorization: CleanupAuthorization,
    target: CleanupTarget,
    request: CleanupRequest,
    quiescence: QuiescenceEvidence,
) -> CleanupReceipt:
    if not isinstance(receipt_run_id, str) or not isinstance(receipt_digest, str):
        raise RuntimeError("cleanup requires State receipt run ID and logical digest")
    result = admit_published_receipt(state.workspace, state, receipt_run_id, receipt_digest)
    if (
        result.cleanup_run_id != cleanup_run_id
        or result.authorization != authorization
        or result.target != target
        or result.quiescence_run_id != quiescence.quiescence_run_id
        or result.quiescence_evidence_digest != quiescence.evidence_digest
        or result.quiescence_identity_digest != quiescence.identity_digest
        or result.quiescence_source_key != quiescence.source_key
        or result.quiescence_source_instance_id != quiescence.source_instance_id
    ):
        raise RuntimeError(
            "cleanup receipt does not bind admitted authorization, target, and quiescence"
        )
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
    processed_successful: frozenset[str] = frozenset(),
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
        if key in processed_successful:
            rebound.append(outcome)
            continue
        if outcome.reason_code == _READY and current_by_id[key] != receipt_by_id[key]:
            rebound.append(RecordOutcome(key, "conflict", "receipt_graph_evidence_drift"))
            continue
        rebound.append(outcome)
        if outcome.reason_code == _READY:
            deletions.append(expected[key])
    return type(plan)(
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
