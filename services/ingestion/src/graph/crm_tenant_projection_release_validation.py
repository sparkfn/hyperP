from __future__ import annotations

from collections.abc import Mapping

from neo4j import ManagedTransaction

from src.crm_tenant_projection_identity import (
    empty_capture_boundary_digest,
    extend_capture_boundary_digest,
    projection_association_id,
    projection_input_id,
    projection_support_digest,
    projection_support_id,
)
from src.crm_tenant_projection_models import (
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionReleaseSummary,
)
from src.crm_tenant_projection_records import _digest
from src.graph.crm_tenant_projection_integrity_values import _decision_matches
from src.graph.crm_tenant_projection_mapping_guard import _validate_mapping_topology_fingerprint
from src.graph.crm_tenant_projection_snapshot_validation import _validate_input_snapshot_contents
from src.graph.crm_tenant_projection_values import (
    _mapping_int,
    _mapping_optional_string,
    _mapping_string,
    _required_mapping,
    _subject_kind_value,
    _subject_numeric_id,
    _summary_from_record,
)
from src.graph.queries.crm_tenant_projection_release_pages import (
    READ_ASSOCIATION_PAGE,
    READ_DECISION_PAGE,
    READ_INPUT_PAGE,
    READ_RELEASE_GUARDS,
    READ_SUPPORT_PAGE,
    READ_VALIDATION_RELEASE,
)

_VALIDATION_PAGE_LIMIT = 200


def _validate_release_topology(
    tx: ManagedTransaction,
    release: CrmTenantProjectionReleaseSummary,
) -> None:
    record = tx.run(READ_VALIDATION_RELEASE, release_id=release.release_id).single()
    if record is None or _summary_from_record(record) != release:
        raise CrmTenantProjectionIntegrityError(
            "projection release topology changed during validation"
        )
    revision_number = _mapping_int(_required_mapping(record, "release"), "mapping_revision_number")
    _validate_mapping_topology_fingerprint(tx, release.release_id, release.release_fingerprint)
    input_count, contact_count, digest = _validate_inputs(tx, release)
    decision_count = _validate_decisions(tx, release)
    association_count = _validate_associations(tx, release)
    support_count = _validate_supports(tx, release, revision_number)
    _validate_final_counts(
        tx,
        release,
        input_count,
        contact_count,
        decision_count,
        association_count,
        support_count,
        digest,
    )


def _validate_inputs(
    tx: ManagedTransaction, release: CrmTenantProjectionReleaseSummary
) -> tuple[int, int, str]:
    kind: str | None = None
    subject_id: int | None = None
    count = 0
    contact_count = 0
    digest = empty_capture_boundary_digest()
    while True:
        rows = list(
            tx.run(
                READ_INPUT_PAGE,
                release_id=release.release_id,
                cursor_kind=kind,
                cursor_subject_id=subject_id,
                contact_kind="contact",
                page_limit=_VALIDATION_PAGE_LIMIT,
            )
        )
        for row in rows:
            input_node = _required_mapping(row, "input")
            snapshot = _required_mapping(row, "snapshot")
            if _mapping_int(row, "owner_count") != 1 or _mapping_int(row, "snapshot_count") != 1:
                raise CrmTenantProjectionIntegrityError("projection input ownership is malformed")
            input_id = _mapping_string(input_node, "input_id")
            input_kind = _subject_kind_value(_mapping_string(input_node, "subject_kind"))
            input_subject_id = _mapping_string(input_node, "subject_id")
            snapshot_id = _mapping_string(input_node, "snapshot_id")
            if (
                _mapping_string(input_node, "release_id") != release.release_id
                or input_id != projection_input_id(release.release_id, input_kind, input_subject_id)
                or _mapping_string(input_node, "input_digest")
                != _digest(
                    "crm-tenant-projection-input-v1",
                    [release.release_id, input_id, input_kind, input_subject_id, snapshot_id],
                )
                or _mapping_string(snapshot, "snapshot_id") != snapshot_id
                or _mapping_string(snapshot, "snapshot_digest")
                != _mapping_string(input_node, "snapshot_digest")
                or _mapping_string(snapshot, "source_instance_id")
                != release.scope.source_instance_id
                or _mapping_string(snapshot, "control_instance_id")
                != release.scope.control_instance_id
                or _mapping_string(snapshot, "subject_kind") != input_kind
                or _mapping_string(snapshot, "subject_id") != input_subject_id
            ):
                raise CrmTenantProjectionIntegrityError("projection input is malformed")
            _validate_input_snapshot_contents(
                tx, release, input_id, snapshot_id, input_kind, input_subject_id
            )
            digest = extend_capture_boundary_digest(
                digest, input_id, _mapping_string(input_node, "input_digest")
            )
            count += 1
            contact_count += input_kind == "contact"
            kind = input_kind
            subject_id = _subject_numeric_id(input_subject_id)
        if len(rows) < _VALIDATION_PAGE_LIMIT:
            return count, contact_count, digest


def _validate_decisions(tx: ManagedTransaction, release: CrmTenantProjectionReleaseSummary) -> int:
    cursor: str | None = None
    count = 0
    while True:
        rows = list(
            tx.run(
                READ_DECISION_PAGE,
                release_id=release.release_id,
                cursor=cursor,
                page_limit=_VALIDATION_PAGE_LIMIT,
            )
        )
        for row in rows:
            decision = _required_mapping(row, "decision")
            input_node = _required_mapping(row, "input")
            snapshot = _required_mapping(row, "snapshot")
            if (
                _mapping_int(row, "input_owner_count") != 1
                or _mapping_int(row, "snapshot_count") != 1
            ):
                raise CrmTenantProjectionIntegrityError(
                    "projection decision ownership is malformed"
                )
            input_id = _mapping_string(decision, "input_id")
            decision_kind = _mapping_string(decision, "decision")
            reason = _mapping_optional_string(decision, "zero_target_reason")
            if (
                _mapping_string(decision, "release_id") != release.release_id
                or input_id != _mapping_string(input_node, "input_id")
                or _mapping_string(decision, "decision_digest")
                != _digest(
                    "crm-tenant-projection-decision-v1",
                    [release.release_id, input_id, decision_kind, reason],
                )
                or not _decision_matches(
                    decision_kind,
                    reason,
                    _mapping_int(row, "association_count"),
                    _mapping_int(snapshot, "binding_count"),
                )
            ):
                raise CrmTenantProjectionIntegrityError("projection decision is malformed")
            cursor = input_id
            count += 1
        if len(rows) < _VALIDATION_PAGE_LIMIT:
            return count


def _validate_associations(
    tx: ManagedTransaction, release: CrmTenantProjectionReleaseSummary
) -> int:
    cursor: str | None = None
    count = 0
    while True:
        rows = list(
            tx.run(
                READ_ASSOCIATION_PAGE,
                release_id=release.release_id,
                cursor=cursor,
                page_limit=_VALIDATION_PAGE_LIMIT,
            )
        )
        for row in rows:
            association = _required_mapping(row, "association")
            input_node = _required_mapping(row, "input")
            entity = _required_mapping(row, "entity")
            if (
                _mapping_int(row, "input_owner_count") != 1
                or _mapping_int(row, "entity_count") != 1
            ):
                raise CrmTenantProjectionIntegrityError(
                    "projection association ownership is malformed"
                )
            association_id = _mapping_string(association, "association_id")
            input_id = _mapping_string(association, "input_id")
            kind = _subject_kind_value(_mapping_string(association, "subject_kind"))
            subject_id = _mapping_string(association, "subject_id")
            entity_key = _mapping_string(association, "entity_key")
            relationship_kind = _mapping_string(association, "relationship_kind")
            if (
                _mapping_string(association, "release_id") != release.release_id
                or input_id != _mapping_string(input_node, "input_id")
                or kind != _mapping_string(input_node, "subject_kind")
                or subject_id != _mapping_string(input_node, "subject_id")
                or relationship_kind != "tenant_member"
                or entity_key != _mapping_string(entity, "entity_key")
                or association_id
                != projection_association_id(
                    release.release_id, input_id, kind, subject_id, entity_key, relationship_kind
                )
                or _mapping_int(row, "support_count") < 1
            ):
                raise CrmTenantProjectionIntegrityError("projection association is malformed")
            cursor = association_id
            count += 1
        if len(rows) < _VALIDATION_PAGE_LIMIT:
            return count


def _validate_supports(
    tx: ManagedTransaction,
    release: CrmTenantProjectionReleaseSummary,
    revision_number: int,
) -> int:
    cursor: str | None = None
    count = 0
    while True:
        rows = list(
            tx.run(
                READ_SUPPORT_PAGE,
                release_id=release.release_id,
                cursor=cursor,
                page_limit=_VALIDATION_PAGE_LIMIT,
            )
        )
        for row in rows:
            support = _required_mapping(row, "support")
            association = _required_mapping(row, "association")
            input_node = _required_mapping(row, "input")
            snapshot = _required_mapping(row, "snapshot")
            observation = _required_mapping(row, "observation")
            target = _required_mapping(row, "target")
            entry = _required_mapping(row, "entry")
            revision = _required_mapping(row, "revision")
            entity = _required_mapping(row, "entity")
            multiplicities = (
                "association_owner_count",
                "input_owner_count",
                "snapshot_count",
                "observation_count",
                "target_count",
                "entry_count",
                "revision_count",
                "entity_count",
            )
            if any(_mapping_int(row, key) != 1 for key in multiplicities):
                raise CrmTenantProjectionIntegrityError(
                    "projection support proof multiplicity is malformed"
                )
            support_id = _mapping_string(support, "support_id")
            association_id = _mapping_string(support, "association_id")
            observation_id = _mapping_string(support, "membership_observation_id")
            target_id = _mapping_string(support, "mapping_target_id")
            if (
                _mapping_string(support, "release_id") != release.release_id
                or association_id != _mapping_string(association, "association_id")
                or support_id != projection_support_id(association_id, observation_id, target_id)
                or _mapping_string(support, "support_digest")
                != projection_support_digest(
                    release.release_id, association_id, observation_id, target_id
                )
                or _mapping_string(observation, "observation_id") != observation_id
                or _mapping_string(observation, "snapshot_id")
                != _mapping_string(snapshot, "snapshot_id")
                or _mapping_string(observation, "subject_kind")
                != _mapping_string(input_node, "subject_kind")
                or _mapping_string(observation, "subject_id")
                != _mapping_string(input_node, "subject_id")
                or _mapping_string(target, "target_id") != target_id
                or _mapping_string(target, "entry_id") != _mapping_string(entry, "entry_id")
                or _mapping_string(target, "entity_key") != _mapping_string(entity, "entity_key")
                or _mapping_string(target, "entity_key")
                != _mapping_string(association, "entity_key")
                or _mapping_string(target, "relationship_kind")
                != _mapping_string(association, "relationship_kind")
                or _mapping_string(entry, "revision_id") != release.mapping_revision_id
                or _mapping_string(entry, "company_id")
                != _mapping_string(observation, "company_id")
                or _mapping_string(revision, "revision_id") != release.mapping_revision_id
                or _mapping_int(revision, "revision_number") != revision_number
                or _mapping_string(revision, "manifest_digest") != release.mapping_manifest_digest
                or _mapping_string(revision, "state") != "prepared"
            ):
                raise CrmTenantProjectionIntegrityError("projection support is malformed")
            cursor = support_id
            count += 1
        if len(rows) < _VALIDATION_PAGE_LIMIT:
            return count


def _validate_final_counts(
    tx: ManagedTransaction,
    release: CrmTenantProjectionReleaseSummary,
    input_count: int,
    contact_count: int,
    decision_count: int,
    association_count: int,
    support_count: int,
    digest: str,
) -> None:
    record = tx.run(READ_RELEASE_GUARDS, release_id=release.release_id).single()
    if record is None:
        raise CrmTenantProjectionIntegrityError("projection release topology is missing")
    release_values: Mapping[str, object] = _required_mapping(record, "release")
    if (
        input_count != release.input_count
        or decision_count != release.decision_count
        or association_count != release.association_count
        or support_count != release.support_count
        or input_count != decision_count
        or input_count != _mapping_int(record, "input_count")
        or decision_count != _mapping_int(record, "decision_count")
        or association_count != _mapping_int(record, "association_count")
        or support_count != _mapping_int(record, "support_count")
        or _mapping_int(record, "integrity_count") != 0
        or contact_count != _mapping_int(release_values, "contact_input_count")
        or input_count - contact_count != _mapping_int(release_values, "lead_input_count")
        or contact_count != _mapping_int(release_values, "contact_expected_input_count")
        or input_count - contact_count != _mapping_int(release_values, "lead_expected_input_count")
        or digest != release.capture_boundary_digest
    ):
        raise CrmTenantProjectionIntegrityError("projection release aggregate counts are malformed")
