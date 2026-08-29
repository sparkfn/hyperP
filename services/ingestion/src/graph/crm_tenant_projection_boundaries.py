"""Mapping, replay, and active-head boundary validation for CRM projection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from neo4j import ManagedTransaction

from src.crm_tenant_projection_identity import empty_capture_boundary_digest
from src.crm_tenant_projection_models import (
    CrmTenantProjectionConflictError,
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionMaterializationCommand,
    CrmTenantProjectionReleaseSummary,
)
from src.graph.crm_tenant_mapping_read import _read_snapshot
from src.graph.crm_tenant_projection_census import (
    _CensusBoundary,
    _CensusUnitBoundary,
    _read_source_census_boundary,
)
from src.graph.crm_tenant_projection_values import (
    _mapping_string,
    _materialized_fingerprint_from_values,
    _nonnegative_int,
    _object_mapping,
    _required_mapping,
    _summary_from_record,
)
from src.graph.queries.crm_tenant_projection import FIND_BY_REQUEST, READ_MAPPING_BOUNDARY
from src.graph.queries.crm_tenant_projection_boundaries import VALIDATE_RELEASE_BOUNDARY


def _validate_mapping_boundary(
    tx: ManagedTransaction,
    command: CrmTenantProjectionMaterializationCommand,
) -> int:
    expected = command.expected_mapping_head_boundary.expected_head
    record = tx.run(
        READ_MAPPING_BOUNDARY,
        source_key=command.scope.source_key,
        source_instance_id=command.scope.source_instance_id,
        control_instance_id=command.scope.control_instance_id,
        mapping_revision_id=command.mapping_revision_id,
        mapping_manifest_digest=command.mapping_manifest_digest,
        expected_mapping_head_id=command.expected_mapping_head_id,
        expected_mapping_head_digest=command.expected_mapping_head_digest,
        expected_mapping_head_present=expected is not None,
        expected_mapping_active_revision_id=None
        if expected is None
        else expected.active_revision_id,
        expected_mapping_active_revision_number=None
        if expected is None
        else expected.active_revision_number,
    ).single()
    if record is None:
        raise CrmTenantProjectionConflictError("prepared mapping boundary conflicts")
    try:
        snapshot = _read_snapshot(
            tx,
            command.scope.mapping_scope,
            command.mapping_revision_id,
            command.mapping_manifest_digest,
        )
    except RuntimeError as exc:
        raise CrmTenantProjectionIntegrityError("prepared mapping topology is malformed") from exc
    if snapshot is None or snapshot.revision.state != "prepared":
        raise CrmTenantProjectionConflictError("prepared mapping snapshot conflicts")
    if snapshot.expected_head_boundary != command.expected_mapping_head_boundary:
        raise CrmTenantProjectionConflictError("prepared mapping expected head conflicts")
    return snapshot.revision.revision_number


def _validate_release_boundary(
    tx: ManagedTransaction,
    release_id: str,
    release_fingerprint: str,
) -> CrmTenantProjectionReleaseSummary:
    record = tx.run(
        VALIDATE_RELEASE_BOUNDARY,
        release_id=release_id,
        release_fingerprint=release_fingerprint,
    ).single()
    if record is None:
        raise CrmTenantProjectionConflictError("projection release authority boundary became stale")
    summary = _summary_from_record(record)
    release_values = _required_mapping(record, "release")
    if record["source_census_ids"] != [summary.source_census_id] or record[
        "mapping_revision_ids"
    ] != [summary.mapping_revision_id]:
        raise CrmTenantProjectionIntegrityError(
            "projection release authority topology is malformed"
        )
    census_boundary = _read_source_census_boundary(
        tx,
        summary.source_census_id,
        _mapping_string(release_values, "source_census_fingerprint"),
        summary.scope,
    )
    if not (
        _unit_matches_release(release_values, census_boundary.contact, "contact")
        and _unit_matches_release(release_values, census_boundary.lead, "lead")
    ):
        raise CrmTenantProjectionConflictError("projection source census boundary became stale")
    try:
        snapshot = _read_snapshot(
            tx,
            summary.scope.mapping_scope,
            summary.mapping_revision_id,
            summary.mapping_manifest_digest,
        )
    except RuntimeError as exc:
        raise CrmTenantProjectionIntegrityError("projection mapping topology is malformed") from exc
    if snapshot is None or snapshot.revision.state != "prepared":
        raise CrmTenantProjectionConflictError("projection prepared mapping became stale")
    return summary


def _unit_matches_release(
    release: Mapping[str, object],
    unit: _CensusUnitBoundary,
    prefix: Literal["contact", "lead"],
) -> bool:
    return (
        _mapping_string(release, f"{prefix}_unit_state") == unit.state
        and _nonnegative_int(release.get(f"{prefix}_unit_generation"), "release generation")
        == unit.generation
        and _nonnegative_int(release.get(f"{prefix}_frozen_upper_id"), "release bound")
        == unit.frozen_upper_id
        and release.get(f"{prefix}_checkpoint_generation") == unit.checkpoint_generation
        and release.get(f"{prefix}_checkpoint_present") == unit.checkpoint_present
        and _nonnegative_int(release.get(f"{prefix}_processed_rows"), "release processed rows")
        == unit.processed_rows
        and _nonnegative_int(release.get(f"{prefix}_skipped_rows"), "release skipped rows")
        == unit.skipped_rows
    )


def _release_properties(
    command: CrmTenantProjectionMaterializationCommand,
    boundary: _CensusBoundary,
    mapping_revision_number: int,
    release_id: str,
    release_number: int,
) -> dict[str, object]:
    prior = command.expected_prior_head
    properties: dict[str, object] = {
        "source_key": command.scope.source_key,
        "source_instance_id": command.scope.source_instance_id,
        "control_instance_id": command.scope.control_instance_id,
        "release_id": release_id,
        "release_number": release_number,
        "request_id": command.request_id,
        "request_fingerprint": command.request_fingerprint,
        "release_fingerprint": "sha256:" + "0" * 64,
        "source_census_id": command.source_census_id,
        "source_census_fingerprint": command.source_census_fingerprint,
        "contact_unit_state": boundary.contact.state,
        "contact_unit_generation": boundary.contact.generation,
        "contact_checkpoint_generation": boundary.contact.checkpoint_generation,
        "contact_checkpoint_present": boundary.contact.checkpoint_present,
        "contact_processed_rows": boundary.contact.processed_rows,
        "contact_skipped_rows": boundary.contact.skipped_rows,
        "contact_expected_input_count": boundary.contact.processed_rows
        - boundary.contact.skipped_rows,
        "contact_frozen_upper_id": boundary.contact.frozen_upper_id,
        "lead_unit_state": boundary.lead.state,
        "lead_unit_generation": boundary.lead.generation,
        "lead_checkpoint_generation": boundary.lead.checkpoint_generation,
        "lead_checkpoint_present": boundary.lead.checkpoint_present,
        "lead_processed_rows": boundary.lead.processed_rows,
        "lead_skipped_rows": boundary.lead.skipped_rows,
        "lead_expected_input_count": boundary.lead.processed_rows - boundary.lead.skipped_rows,
        "lead_frozen_upper_id": boundary.lead.frozen_upper_id,
        "mapping_revision_id": command.mapping_revision_id,
        "mapping_revision_number": mapping_revision_number,
        "mapping_manifest_digest": command.mapping_manifest_digest,
        "projection_head_id": command.projection_head_id,
        "expected_mapping_head_id": command.expected_mapping_head_id,
        "expected_mapping_head_digest": command.expected_mapping_head_digest,
        "expected_mapping_head_present": (
            command.expected_mapping_head_boundary.expected_head is not None
        ),
        "expected_mapping_active_revision_id": (
            None
            if command.expected_mapping_head_boundary.expected_head is None
            else command.expected_mapping_head_boundary.expected_head.active_revision_id
        ),
        "expected_mapping_active_revision_number": (
            None
            if command.expected_mapping_head_boundary.expected_head is None
            else command.expected_mapping_head_boundary.expected_head.active_revision_number
        ),
        "expected_prior_head_present": prior is not None,
        "expected_prior_head_id": None if prior is None else prior.head_id,
        "expected_prior_release_id": None if prior is None else prior.active_release_id,
        "expected_prior_release_number": None if prior is None else prior.active_release_number,
        "expected_prior_release_fingerprint": (
            None if prior is None else prior.active_release_fingerprint
        ),
        "contract_version": command.contract_version,
        "state": "building",
        "phase": "capture",
        "capture_complete": False,
        "projection_complete": False,
        "capture_boundary_digest": empty_capture_boundary_digest(),
        "input_count": 0,
        "contact_input_count": 0,
        "lead_input_count": 0,
        "decision_count": 0,
        "association_count": 0,
        "support_count": 0,
        "created_at": None,
    }
    properties["release_fingerprint"] = _materialized_fingerprint_from_values(
        _object_mapping(properties, "projection release properties")
    )
    return properties


def _find_by_request(
    tx: ManagedTransaction,
    command: CrmTenantProjectionMaterializationCommand,
) -> CrmTenantProjectionReleaseSummary | None:
    rows = list(
        tx.run(
            FIND_BY_REQUEST,
            source_key=command.scope.source_key,
            source_instance_id=command.scope.source_instance_id,
            control_instance_id=command.scope.control_instance_id,
            request_id=command.request_id,
        )
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise CrmTenantProjectionIntegrityError("projection request ID is not unique")
    summary = _summary_from_record(rows[0])
    request_fingerprint = _mapping_string(
        _required_mapping(rows[0], "release"), "request_fingerprint"
    )
    if request_fingerprint != command.request_fingerprint:
        raise CrmTenantProjectionConflictError(
            "projection request ID was reused with different immutable input"
        )
    return summary
