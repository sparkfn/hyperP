"""Strict immutable topology reconstruction for CRM tenant projection releases."""

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
from src.graph.crm_tenant_projection_integrity_values import (
    _child_node,
    _decision_matches,
    _mapping_list,
    _mapping_node_strings,
    _node_rows,
    _require_exact_strings,
    _validate_authority,
)
from src.graph.crm_tenant_projection_values import (
    _mapping_int,
    _mapping_optional_string,
    _mapping_string,
    _required_mapping,
    _subject_kind_value,
    _subject_numeric_id,
    _summary_from_record,
)
from src.graph.queries.crm_tenant_projection_integrity import READ_RELEASE_TOPOLOGY


def _validate_release_topology(
    tx: ManagedTransaction,
    release: CrmTenantProjectionReleaseSummary,
) -> None:
    """Fail closed unless every persisted child and proof matches the immutable ledger."""
    record = tx.run(READ_RELEASE_TOPOLOGY, release_id=release.release_id).single()
    if record is None:
        raise CrmTenantProjectionIntegrityError("projection release topology is missing")
    persisted = _summary_from_record(record)
    if persisted != release:
        raise CrmTenantProjectionIntegrityError(
            "projection release topology changed during validation"
        )
    _validate_authority(record, release)
    inputs = _node_rows(record, "inputs")
    decisions = _node_rows(record, "decisions")
    associations = _node_rows(record, "associations")
    supports = _node_rows(record, "supports")
    input_by_id = _validate_inputs(release, inputs)
    association_by_id = _validate_associations(release, associations, input_by_id)
    _validate_decisions(release, decisions, input_by_id, association_by_id)
    release_values = _required_mapping(record, "release")
    mapping_revision_number = _mapping_int(release_values, "mapping_revision_number")
    _validate_supports(release, supports, input_by_id, association_by_id, mapping_revision_number)
    _validate_counts(release, inputs, decisions, associations, supports, release_values)
    _validate_capture_digest(release, input_by_id)


def _validate_inputs(
    release: CrmTenantProjectionReleaseSummary,
    rows: list[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for row in rows:
        node = _child_node(row, "input")
        input_id = _mapping_string(node, "input_id")
        subject_kind = _subject_kind_value(_mapping_string(node, "subject_kind"))
        subject_id = _mapping_string(node, "subject_id")
        if input_id != projection_input_id(release.release_id, subject_kind, subject_id):
            raise CrmTenantProjectionIntegrityError(
                "projection input deterministic identity is malformed"
            )
        if _mapping_string(node, "release_id") != release.release_id:
            raise CrmTenantProjectionIntegrityError(
                "projection input release identity is malformed"
            )
        snapshot_id = _mapping_string(node, "snapshot_id")
        if _mapping_string(node, "input_digest") != _digest(
            "crm-tenant-projection-input-v1",
            [release.release_id, input_id, subject_kind, subject_id, snapshot_id],
        ):
            raise CrmTenantProjectionIntegrityError("projection input digest is malformed")
        _require_exact_strings(
            row, "release_owner_ids", release.release_id, "projection input owner"
        )
        snapshots = _mapping_list(row, "snapshots", "projection input snapshots")
        if len(snapshots) != 1:
            raise CrmTenantProjectionIntegrityError(
                "projection input snapshot ownership is malformed"
            )
        snapshot = snapshots[0]
        if (
            _mapping_string(snapshot, "snapshot_id") != snapshot_id
            or _mapping_string(snapshot, "snapshot_digest")
            != _mapping_string(node, "snapshot_digest")
            or _mapping_string(snapshot, "source_instance_id") != release.scope.source_instance_id
            or _mapping_string(snapshot, "control_instance_id") != release.scope.control_instance_id
            or _mapping_string(snapshot, "subject_kind") != subject_kind
            or _mapping_string(snapshot, "subject_id") != subject_id
        ):
            raise CrmTenantProjectionIntegrityError(
                "projection input snapshot boundary is malformed"
            )
        _mapping_int(snapshot, "binding_count")
        if input_id in result:
            raise CrmTenantProjectionIntegrityError("projection input is duplicated")
        result[input_id] = row
    return result


def _validate_associations(
    release: CrmTenantProjectionReleaseSummary,
    rows: list[Mapping[str, object]],
    inputs: Mapping[str, Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for row in rows:
        node = _child_node(row, "association")
        association_id = _mapping_string(node, "association_id")
        input_id = _mapping_string(node, "input_id")
        input_row = inputs.get(input_id)
        if input_row is None:
            raise CrmTenantProjectionIntegrityError("projection association input is missing")
        input_node = _child_node(input_row, "input")
        subject_kind = _subject_kind_value(_mapping_string(node, "subject_kind"))
        subject_id = _mapping_string(node, "subject_id")
        entity_key = _mapping_string(node, "entity_key")
        relationship_kind = _mapping_string(node, "relationship_kind")
        if relationship_kind != "tenant_member":
            raise CrmTenantProjectionIntegrityError(
                "projection association relationship is malformed"
            )
        if (
            _mapping_string(node, "release_id") != release.release_id
            or subject_kind != _mapping_string(input_node, "subject_kind")
            or subject_id != _mapping_string(input_node, "subject_id")
            or association_id
            != projection_association_id(
                release.release_id,
                input_id,
                subject_kind,
                subject_id,
                entity_key,
                relationship_kind,
            )
        ):
            raise CrmTenantProjectionIntegrityError("projection association identity is malformed")
        _require_exact_strings(row, "input_owner_ids", input_id, "projection association owner")
        entities = _mapping_list(row, "entities", "projection association entities")
        if len(entities) != 1 or _mapping_string(entities[0], "entity_key") != entity_key:
            raise CrmTenantProjectionIntegrityError(
                "projection association entity proof is malformed"
            )
        if association_id in result:
            raise CrmTenantProjectionIntegrityError("projection association is duplicated")
        result[association_id] = row
    for input_id, row in inputs.items():
        expected = sorted(
            association_id
            for association_id, association in result.items()
            if _mapping_string(_child_node(association, "association"), "input_id") == input_id
        )
        actual = sorted(
            _mapping_node_strings(
                row, "associations", "association_id", "projection input associations"
            )
        )
        if actual != expected:
            raise CrmTenantProjectionIntegrityError(
                "projection input association ownership is malformed"
            )
    return result


def _validate_decisions(
    release: CrmTenantProjectionReleaseSummary,
    rows: list[Mapping[str, object]],
    inputs: Mapping[str, Mapping[str, object]],
    associations: Mapping[str, Mapping[str, object]],
) -> None:
    seen: set[str] = set()
    for row in rows:
        node = _child_node(row, "decision")
        input_id = _mapping_string(node, "input_id")
        input_row = inputs.get(input_id)
        if input_row is None or input_id in seen:
            raise CrmTenantProjectionIntegrityError("projection decision ownership is malformed")
        seen.add(input_id)
        decision = _mapping_string(node, "decision")
        reason = _mapping_optional_string(node, "zero_target_reason")
        associated_count = sum(
            _mapping_string(_child_node(item, "association"), "input_id") == input_id
            for item in associations.values()
        )
        snapshot = _mapping_list(input_row, "snapshots", "projection input snapshots")[0]
        binding_count = _mapping_int(snapshot, "binding_count")
        if (
            _mapping_string(node, "release_id") != release.release_id
            or _mapping_string(node, "decision_digest")
            != _digest(
                "crm-tenant-projection-decision-v1",
                [release.release_id, input_id, decision, reason],
            )
            or not _decision_matches(decision, reason, associated_count, binding_count)
        ):
            raise CrmTenantProjectionIntegrityError("projection decision is malformed")
        _require_exact_strings(row, "input_owner_ids", input_id, "projection decision owner")
    if seen != set(inputs):
        raise CrmTenantProjectionIntegrityError("projection inputs require exactly one decision")
    for input_id, row in inputs.items():
        actual = _mapping_list(row, "decisions", "projection input decisions")
        if len(actual) != 1 or _mapping_string(actual[0], "input_id") != input_id:
            raise CrmTenantProjectionIntegrityError(
                "projection input decision ownership is malformed"
            )


def _validate_supports(
    release: CrmTenantProjectionReleaseSummary,
    rows: list[Mapping[str, object]],
    inputs: Mapping[str, Mapping[str, object]],
    associations: Mapping[str, Mapping[str, object]],
    mapping_revision_number: int,
) -> None:
    seen: set[str] = set()
    by_association: dict[str, list[str]] = {key: [] for key in associations}
    for row in rows:
        node = _child_node(row, "support")
        support_id = _mapping_string(node, "support_id")
        association_id = _mapping_string(node, "association_id")
        association_row = associations.get(association_id)
        if association_row is None or support_id in seen:
            raise CrmTenantProjectionIntegrityError("projection support association is malformed")
        seen.add(support_id)
        association = _child_node(association_row, "association")
        input_id = _mapping_string(association, "input_id")
        input_row = inputs[input_id]
        snapshot = _mapping_list(input_row, "snapshots", "projection input snapshots")[0]
        observation_id = _mapping_string(node, "membership_observation_id")
        target_id = _mapping_string(node, "mapping_target_id")
        if (
            _mapping_string(node, "release_id") != release.release_id
            or support_id != projection_support_id(association_id, observation_id, target_id)
            or _mapping_string(node, "support_digest")
            != projection_support_digest(
                release.release_id, association_id, observation_id, target_id
            )
        ):
            raise CrmTenantProjectionIntegrityError(
                "projection support deterministic identity is malformed"
            )
        _require_exact_strings(
            row, "association_owner_ids", association_id, "projection support owner"
        )
        observations = _mapping_list(row, "observations", "projection support observations")
        targets = _mapping_list(row, "targets", "projection support targets")
        if len(observations) != 1 or len(targets) != 1:
            raise CrmTenantProjectionIntegrityError(
                "projection support proof multiplicity is malformed"
            )
        observation = _child_node(observations[0], "observation")
        snapshots = _mapping_list(observations[0], "snapshots", "support observation snapshots")
        if (
            _mapping_string(observation, "observation_id") != observation_id
            or _mapping_string(observation, "snapshot_id")
            != _mapping_string(snapshot, "snapshot_id")
            or _mapping_string(observation, "subject_kind")
            != _mapping_string(_child_node(input_row, "input"), "subject_kind")
            or _mapping_string(observation, "subject_id")
            != _mapping_string(_child_node(input_row, "input"), "subject_id")
            or len(snapshots) != 1
            or _mapping_string(snapshots[0], "snapshot_id")
            != _mapping_string(snapshot, "snapshot_id")
        ):
            raise CrmTenantProjectionIntegrityError(
                "projection support snapshot proof is malformed"
            )
        target = _child_node(targets[0], "target")
        if (
            _mapping_string(target, "target_id") != target_id
            or _mapping_string(target, "entity_key") != _mapping_string(association, "entity_key")
            or _mapping_string(target, "relationship_kind")
            != _mapping_string(association, "relationship_kind")
        ):
            raise CrmTenantProjectionIntegrityError("projection support target proof is malformed")
        entities = _mapping_list(targets[0], "entities", "support target entities")
        entries = _mapping_list(targets[0], "entries", "support mapping entries")
        if len(entities) != 1 or _mapping_string(entities[0], "entity_key") != _mapping_string(
            association, "entity_key"
        ):
            raise CrmTenantProjectionIntegrityError("projection support Entity proof is malformed")
        _validate_mapping_entry(release, observation, target, entries, mapping_revision_number)
        by_association[association_id].append(support_id)
    for association_id, association_row in associations.items():
        actual = sorted(
            _mapping_node_strings(association_row, "supports", "support_id", "association supports")
        )
        if actual != sorted(by_association[association_id]) or not actual:
            raise CrmTenantProjectionIntegrityError(
                "projection association support ownership is malformed"
            )


def _validate_mapping_entry(
    release: CrmTenantProjectionReleaseSummary,
    observation: Mapping[str, object],
    target: Mapping[str, object],
    entries: list[Mapping[str, object]],
    mapping_revision_number: int,
) -> None:
    if len(entries) != 1:
        raise CrmTenantProjectionIntegrityError("projection mapping entry proof is malformed")
    entry = _child_node(entries[0], "entry")
    revisions = _mapping_list(entries[0], "revisions", "mapping entry revisions")
    if (
        _mapping_string(entry, "revision_id") != release.mapping_revision_id
        or _mapping_string(entry, "company_id") != _mapping_string(observation, "company_id")
        or _mapping_string(target, "entry_id") != _mapping_string(entry, "entry_id")
        or len(revisions) != 1
        or _mapping_string(revisions[0], "revision_id") != release.mapping_revision_id
        or _mapping_int(revisions[0], "revision_number") != mapping_revision_number
        or _mapping_string(revisions[0], "manifest_digest") != release.mapping_manifest_digest
        or _mapping_string(revisions[0], "state") != "prepared"
    ):
        raise CrmTenantProjectionIntegrityError("projection mapping revision proof is malformed")


def _validate_counts(
    release: CrmTenantProjectionReleaseSummary,
    inputs: list[Mapping[str, object]],
    decisions: list[Mapping[str, object]],
    associations: list[Mapping[str, object]],
    supports: list[Mapping[str, object]],
    release_values: Mapping[str, object],
) -> None:
    if (
        release.input_count != len(inputs)
        or release.decision_count != len(decisions)
        or release.association_count != len(associations)
        or release.support_count != len(supports)
        or release.decision_count != release.input_count
    ):
        raise CrmTenantProjectionIntegrityError("projection release aggregate counts are malformed")
    contact_count = sum(
        _mapping_string(_child_node(row, "input"), "subject_kind") == "contact" for row in inputs
    )
    lead_count = len(inputs) - contact_count
    if (
        contact_count != _mapping_int(release_values, "contact_input_count")
        or lead_count != _mapping_int(release_values, "lead_input_count")
        or contact_count != _mapping_int(release_values, "contact_expected_input_count")
        or lead_count != _mapping_int(release_values, "lead_expected_input_count")
    ):
        raise CrmTenantProjectionIntegrityError("projection release capture counts are malformed")


def _validate_capture_digest(
    release: CrmTenantProjectionReleaseSummary,
    inputs: Mapping[str, Mapping[str, object]],
) -> None:
    ordered = sorted(
        (
            _subject_kind_value(_mapping_string(_child_node(row, "input"), "subject_kind")),
            _subject_numeric_id(_mapping_string(_child_node(row, "input"), "subject_id")),
            input_id,
            _mapping_string(_child_node(row, "input"), "input_digest"),
        )
        for input_id, row in inputs.items()
    )
    digest = empty_capture_boundary_digest()
    for _, _, input_id, input_digest in ordered:
        digest = extend_capture_boundary_digest(digest, input_id, input_digest)
    if digest != release.capture_boundary_digest:
        raise CrmTenantProjectionIntegrityError("projection capture boundary digest is malformed")
