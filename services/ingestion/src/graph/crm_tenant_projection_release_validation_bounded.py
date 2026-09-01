"""Bounded repository-read validation for immutable projection releases."""

from __future__ import annotations

from collections.abc import Mapping

from neo4j import ManagedTransaction

from src.crm_tenant_projection_identity import (
    empty_capture_boundary_digest,
    extend_capture_boundary_digest,
    projection_input_id,
)
from src.crm_tenant_projection_models import (
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionReleaseSummary,
)
from src.crm_tenant_projection_records import _digest
from src.graph.client import Neo4jClient
from src.graph.crm_tenant_projection_mapping_validation import (
    _validate_mapping_topology_fingerprint_bounded,
)
from src.graph.crm_tenant_projection_release_ledger_validation import (
    _validate_associations_bounded,
    _validate_decisions_bounded,
    _validate_supports_bounded,
)
from src.graph.crm_tenant_projection_snapshot_validation import (
    _validate_input_snapshot_contents_bounded,
)
from src.graph.crm_tenant_projection_values import (
    _mapping_int,
    _mapping_string,
    _required_mapping,
    _subject_kind_value,
    _subject_numeric_id,
    _summary_from_record,
)
from src.graph.queries.crm_tenant_projection_release_pages import (
    READ_INPUT_PAGE,
    READ_RELEASE_GUARDS,
    READ_VALIDATION_RELEASE,
)

_VALIDATION_PAGE_LIMIT = 200


def _validate_release_topology_bounded(
    client: Neo4jClient,
    release: CrmTenantProjectionReleaseSummary,
) -> None:
    """Use one read transaction for every release child keyset page."""

    def read_release(tx: ManagedTransaction) -> Mapping[str, object] | None:
        return tx.run(READ_VALIDATION_RELEASE, release_id=release.release_id).single()

    record = client.execute_read(read_release)
    if record is None or _summary_from_record(record) != release:
        raise CrmTenantProjectionIntegrityError(
            "projection release topology changed during validation"
        )
    revision_number = _mapping_int(_required_mapping(record, "release"), "mapping_revision_number")
    _validate_mapping_topology_fingerprint_bounded(
        client, release.release_id, release.release_fingerprint
    )
    input_count, contact_count, digest = _validate_inputs_bounded(client, release)
    decision_count = _validate_decisions_bounded(client, release)
    association_count = _validate_associations_bounded(client, release)
    support_count = _validate_supports_bounded(client, release, revision_number)
    _validate_final_counts_bounded(
        client,
        release,
        input_count,
        contact_count,
        decision_count,
        association_count,
        support_count,
        digest,
    )


def _validate_inputs_bounded(
    client: Neo4jClient,
    release: CrmTenantProjectionReleaseSummary,
) -> tuple[int, int, str]:
    kind: str | None = None
    subject_id: int | None = None
    input_cursor: str | None = None
    count = 0
    contact_count = 0
    digest = empty_capture_boundary_digest()
    while True:

        def read_page(
            tx: ManagedTransaction,
            kind: str | None = kind,
            subject_id: int | None = subject_id,
            input_cursor: str | None = input_cursor,
        ) -> list[Mapping[str, object]]:
            return _input_page(tx, release.release_id, kind, subject_id, input_cursor)

        rows = client.execute_read(read_page)
        for row in rows:
            input_node = _required_mapping(row, "input")
            snapshot = _required_mapping(row, "snapshot")
            if _mapping_int(row, "owner_count") != 1 or _mapping_int(row, "snapshot_count") != 1:
                raise CrmTenantProjectionIntegrityError("projection input ownership is malformed")
            input_id = _mapping_string(input_node, "input_id")
            input_kind = _subject_kind_value(_mapping_string(input_node, "subject_kind"))
            input_subject_id = _mapping_string(input_node, "subject_id")
            snapshot_id = _mapping_string(input_node, "snapshot_id")
            _validate_input_values(
                release, input_node, snapshot, input_id, input_kind, input_subject_id
            )
            _validate_input_snapshot_contents_bounded(
                client, release, input_id, snapshot_id, input_kind, input_subject_id
            )
            digest = extend_capture_boundary_digest(
                digest, input_id, _mapping_string(input_node, "input_digest")
            )
            count += 1
            contact_count += input_kind == "contact"
            kind = input_kind
            subject_id = _subject_numeric_id(input_subject_id)
            input_cursor = input_id
        if len(rows) < _VALIDATION_PAGE_LIMIT:
            return count, contact_count, digest


def _input_page(
    tx: ManagedTransaction,
    release_id: str,
    kind: str | None,
    subject_id: int | None,
    input_cursor: str | None,
) -> list[Mapping[str, object]]:
    return list(
        tx.run(
            READ_INPUT_PAGE,
            release_id=release_id,
            cursor_kind=kind,
            cursor_subject_id=subject_id,
            cursor_input_id=input_cursor,
            contact_kind="contact",
            page_limit=_VALIDATION_PAGE_LIMIT,
        )
    )


def _validate_input_values(
    release: CrmTenantProjectionReleaseSummary,
    input_node: Mapping[str, object],
    snapshot: Mapping[str, object],
    input_id: str,
    input_kind: str,
    input_subject_id: str,
) -> None:
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
        or _mapping_string(snapshot, "source_instance_id") != release.scope.source_instance_id
        or _mapping_string(snapshot, "control_instance_id") != release.scope.control_instance_id
        or _mapping_string(snapshot, "subject_kind") != input_kind
        or _mapping_string(snapshot, "subject_id") != input_subject_id
    ):
        raise CrmTenantProjectionIntegrityError("projection input is malformed")


def _validate_final_counts_bounded(
    client: Neo4jClient,
    release: CrmTenantProjectionReleaseSummary,
    input_count: int,
    contact_count: int,
    decision_count: int,
    association_count: int,
    support_count: int,
    digest: str,
) -> None:
    client.execute_read(
        lambda tx: _validate_final_counts(
            tx,
            release,
            input_count,
            contact_count,
            decision_count,
            association_count,
            support_count,
            digest,
        )
    )


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
    if digest != release.capture_boundary_digest:
        raise CrmTenantProjectionIntegrityError("projection capture boundary digest is malformed")
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
    ):
        raise CrmTenantProjectionIntegrityError("projection release aggregate counts are malformed")
