"""Bounded immutable capture, ledger projection, and terminal writes."""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.crm_tenant_projection_identity import (
    extend_capture_boundary_digest,
    projection_association_id,
    projection_input_id,
    projection_support_digest,
    projection_support_id,
)
from src.crm_tenant_projection_models import (
    CrmTenantProjectionConflictError,
    CrmTenantProjectionCursor,
    CrmTenantProjectionFailureCode,
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionReleaseSummary,
    validate_failure_code,
)
from src.crm_tenant_projection_records import CRM_TENANT_PROJECTION_CONTRACT_VERSION, _digest
from src.graph.crm_tenant_projection_boundaries import _validate_release_boundary
from src.graph.crm_tenant_projection_integrity import _validate_release_topology
from src.graph.crm_tenant_projection_observations import _validated_observation_id
from src.graph.crm_tenant_projection_values import (
    _optional_string,
    _read_release,
    _require_building,
    _required_int,
    _required_string,
    _required_subject_kind,
    _subject_numeric_id,
    _SubjectKind,
    _summary_from_record,
)
from src.graph.queries.crm_tenant_projection import (
    ADVANCE_CAPTURE,
    CAPTURE_CANDIDATES,
    READ_CAPTURE_COUNTS,
    WRITE_INPUTS,
)
from src.graph.queries.crm_tenant_projection_integrity import (
    CANCEL_RELEASE,
    COMPLETE_RELEASE,
    FAIL_RELEASE,
)
from src.graph.queries.crm_tenant_projection_projection import (
    ADVANCE_PROJECTION,
    READ_INPUT_SUPPORTS,
    READ_PROJECTION_INPUTS,
    WRITE_ASSOCIATIONS,
    WRITE_DECISION,
)


def _capture_page(
    tx: ManagedTransaction,
    release_id: str,
    release_fingerprint: str,
    page_limit: int,
) -> CrmTenantProjectionReleaseSummary:
    release = _read_release(tx, release_id)
    _require_building(release, release_fingerprint, "capture")
    _validate_release_boundary(tx, release_id, release_fingerprint)
    rows = list(tx.run(CAPTURE_CANDIDATES, release_id=release_id, page_limit=page_limit))
    inputs: list[dict[str, object]] = []
    cursor = release.capture_cursor
    boundary_digest = release.capture_boundary_digest
    for row in rows:
        subject_kind = _required_subject_kind(row, "subject_kind")
        subject_id = _required_string(row, "subject_id")
        numeric_subject_id = _subject_numeric_id(subject_id)
        snapshot_id = _required_string(row, "snapshot_id")
        snapshot_digest = _required_string(row, "snapshot_digest")
        input_id = projection_input_id(release_id, subject_kind, subject_id)
        input_digest = _digest(
            "crm-tenant-projection-input-v1",
            [release_id, input_id, subject_kind, subject_id, snapshot_id],
        )
        inputs.append(
            {
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "snapshot_id": snapshot_id,
                "snapshot_digest": snapshot_digest,
                "input_id": input_id,
                "input_digest": input_digest,
            }
        )
        cursor = CrmTenantProjectionCursor(subject_kind, numeric_subject_id)
        boundary_digest = extend_capture_boundary_digest(boundary_digest, input_id, input_digest)
    if inputs:
        stored = tx.run(
            WRITE_INPUTS,
            release_id=release_id,
            release_fingerprint=release_fingerprint,
            inputs=inputs,
        ).single()
        if stored is None or _required_int(stored, "input_count") != len(inputs):
            raise CrmTenantProjectionIntegrityError("projection input persistence conflicted")
    contact_delta = sum(item["subject_kind"] == "contact" for item in inputs)
    lead_delta = len(inputs) - contact_delta
    count_record = tx.run(READ_CAPTURE_COUNTS, release_id=release_id).single()
    if count_record is None:
        raise CrmTenantProjectionConflictError("projection capture counts are missing")
    counts = (
        _required_int(count_record, "contact_input_count"),
        _required_int(count_record, "lead_input_count"),
    )
    done = len(inputs) < page_limit
    advanced = tx.run(
        ADVANCE_CAPTURE,
        release_id=release_id,
        release_fingerprint=release_fingerprint,
        cursor_kind=None if cursor is None else cursor.subject_kind,
        cursor_subject_id=None if cursor is None else cursor.subject_id,
        prior_input_count=release.input_count,
        prior_contact_input_count=counts[0],
        prior_lead_input_count=counts[1],
        input_count=release.input_count + len(inputs),
        contact_input_count=counts[0] + contact_delta,
        lead_input_count=counts[1] + lead_delta,
        capture_boundary_digest=boundary_digest,
        done=done,
    ).single()
    if advanced is None:
        raise CrmTenantProjectionConflictError("projection capture boundary became stale")
    return _summary_from_record(advanced)


def _project_page(
    tx: ManagedTransaction,
    release_id: str,
    release_fingerprint: str,
    page_limit: int,
) -> CrmTenantProjectionReleaseSummary:
    release = _read_release(tx, release_id)
    _require_building(release, release_fingerprint, "projection")
    _validate_release_boundary(tx, release_id, release_fingerprint)
    rows = list(
        tx.run(
            READ_PROJECTION_INPUTS,
            release_id=release_id,
            release_fingerprint=release_fingerprint,
            page_limit=page_limit,
        )
    )
    cursor = release.projection_cursor
    association_delta = 0
    support_delta = 0
    for row in rows:
        input_id = _required_string(row, "input_id")
        subject_kind = _required_subject_kind(row, "subject_kind")
        subject_id = _required_string(row, "subject_id")
        snapshot_id = _required_string(row, "snapshot_id")
        associations, supports = _project_one_input(
            tx, release, input_id, subject_kind, subject_id, snapshot_id
        )
        association_delta += associations
        support_delta += supports
        cursor = CrmTenantProjectionCursor(subject_kind, _subject_numeric_id(subject_id))
    done = len(rows) < page_limit
    advanced = tx.run(
        ADVANCE_PROJECTION,
        release_id=release_id,
        release_fingerprint=release_fingerprint,
        cursor_kind=None if cursor is None else cursor.subject_kind,
        cursor_subject_id=None if cursor is None else cursor.subject_id,
        prior_decision_count=release.decision_count,
        prior_association_count=release.association_count,
        prior_support_count=release.support_count,
        decision_count=release.decision_count + len(rows),
        association_count=release.association_count + association_delta,
        support_count=release.support_count + support_delta,
        done=done,
    ).single()
    if advanced is None:
        raise CrmTenantProjectionConflictError("projection ledger boundary became stale")
    return _summary_from_record(advanced)


def _project_one_input(
    tx: ManagedTransaction,
    release: CrmTenantProjectionReleaseSummary,
    input_id: str,
    subject_kind: _SubjectKind,
    subject_id: str,
    snapshot_id: str,
) -> tuple[int, int]:
    rows = list(
        tx.run(
            READ_INPUT_SUPPORTS,
            release_id=release.release_id,
            mapping_revision_id=release.mapping_revision_id,
            input_id=input_id,
            snapshot_id=snapshot_id,
        )
    )
    if not rows:
        raise CrmTenantProjectionIntegrityError("captured input snapshot is missing")
    binding_count = _required_int(rows[0], "binding_count")
    observation_ids: set[str] = set()
    observation_nodes: dict[str, str] = {}
    supports_by_id: dict[str, dict[str, str]] = {}
    association_ids: set[str] = set()
    for row in rows:
        observation_id = _validated_observation_id(
            row,
            snapshot_id,
            subject_kind,
            subject_id,
            release.scope.source_key,
            release.scope.source_instance_id,
            release.scope.control_instance_id,
            observation_nodes,
        )
        target_id = _optional_string(row, "mapping_target_id")
        entity_key = _optional_string(row, "entity_key")
        relationship_kind = _optional_string(row, "relationship_kind")
        if observation_id is not None:
            observation_ids.add(observation_id)
        if observation_id is None:
            if any(value is not None for value in (target_id, entity_key, relationship_kind)):
                raise CrmTenantProjectionIntegrityError("membership support row is malformed")
            continue
        if target_id is None and entity_key is None and relationship_kind is None:
            continue
        if target_id is None or entity_key is None or relationship_kind is None:
            raise CrmTenantProjectionIntegrityError("mapping target row is malformed")
        if relationship_kind != "tenant_member":
            raise CrmTenantProjectionIntegrityError("mapping target relationship kind is invalid")
        association_id = projection_association_id(
            release.release_id,
            input_id,
            subject_kind,
            subject_id,
            entity_key,
            relationship_kind,
        )
        support_id = projection_support_id(association_id, observation_id, target_id)
        association_ids.add(association_id)
        supports_by_id[support_id] = {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "entity_key": entity_key,
            "relationship_kind": relationship_kind,
            "association_id": association_id,
            "observation_id": observation_id,
            "mapping_target_id": target_id,
            "support_id": support_id,
            "support_digest": projection_support_digest(
                release.release_id, association_id, observation_id, target_id
            ),
        }
    if len(observation_ids) != binding_count:
        raise CrmTenantProjectionIntegrityError("membership snapshot binding topology is malformed")
    supports = list(supports_by_id.values())
    if supports:
        result = tx.run(
            WRITE_ASSOCIATIONS,
            release_id=release.release_id,
            release_fingerprint=release.release_fingerprint,
            input_id=input_id,
            supports=supports,
        ).single()
        if result is None:
            raise CrmTenantProjectionIntegrityError("projection association persistence failed")
        if _required_int(result, "associations") != len(association_ids):
            raise CrmTenantProjectionIntegrityError("projection association replay is malformed")
        if _required_int(result, "supports") != len(supports):
            raise CrmTenantProjectionIntegrityError("projection support replay is malformed")
        decision = "associated"
        reason: str | None = None
    else:
        decision = "zero_target"
        reason = "empty_membership" if binding_count == 0 else "no_mapped_targets"
    decision_digest = _digest(
        "crm-tenant-projection-decision-v1",
        [release.release_id, input_id, decision, reason],
    )
    persisted = tx.run(
        WRITE_DECISION,
        release_id=release.release_id,
        release_fingerprint=release.release_fingerprint,
        input_id=input_id,
        decision=decision,
        zero_target_reason=reason,
        decision_digest=decision_digest,
    ).single()
    if persisted is None or _required_string(persisted, "input_id") != input_id:
        raise CrmTenantProjectionIntegrityError("projection decision persistence failed")
    return len(association_ids), len(supports)


def _complete_release(
    tx: ManagedTransaction,
    release_id: str,
    release_fingerprint: str,
) -> CrmTenantProjectionReleaseSummary:
    current = _read_release(tx, release_id)
    if current.release_fingerprint != release_fingerprint:
        raise CrmTenantProjectionConflictError("projection release fingerprint conflicts")
    if current.state == "completed":
        result = _validate_release_boundary(tx, release_id, release_fingerprint)
        _validate_release_topology(tx, result)
        return result
    _require_building(current, release_fingerprint, "complete")
    _validate_release_boundary(tx, release_id, release_fingerprint)
    _validate_release_topology(tx, current)
    record = tx.run(
        COMPLETE_RELEASE,
        release_id=release_id,
        release_fingerprint=release_fingerprint,
        contract_version=CRM_TENANT_PROJECTION_CONTRACT_VERSION,
    ).single()
    if record is None:
        raise CrmTenantProjectionConflictError("projection completion conflicts")
    return _summary_from_record(record)


def _cancel_release(
    tx: ManagedTransaction,
    release_id: str,
    release_fingerprint: str,
) -> CrmTenantProjectionReleaseSummary:
    current = _read_release(tx, release_id)
    if current.release_fingerprint != release_fingerprint:
        raise CrmTenantProjectionConflictError("projection release fingerprint conflicts")
    if current.state == "cancelled":
        return current
    if current.terminal:
        raise CrmTenantProjectionConflictError("projection terminal transition conflicts")
    record = tx.run(
        CANCEL_RELEASE, release_id=release_id, release_fingerprint=release_fingerprint
    ).single()
    if record is None:
        raise CrmTenantProjectionConflictError("projection cancellation conflicts")
    return _summary_from_record(record)


def _fail_release(
    tx: ManagedTransaction,
    release_id: str,
    release_fingerprint: str,
    failure_code: CrmTenantProjectionFailureCode,
) -> CrmTenantProjectionReleaseSummary:
    failure_code = validate_failure_code(failure_code)
    current = _read_release(tx, release_id)
    if current.release_fingerprint != release_fingerprint:
        raise CrmTenantProjectionConflictError("projection release fingerprint conflicts")
    if current.state == "failed":
        return current
    if current.terminal:
        raise CrmTenantProjectionConflictError("projection terminal transition conflicts")
    record = tx.run(
        FAIL_RELEASE,
        release_id=release_id,
        release_fingerprint=release_fingerprint,
        failure_code=failure_code,
    ).single()
    if record is None:
        raise CrmTenantProjectionConflictError("projection failure transition conflicts")
    return _summary_from_record(record)
