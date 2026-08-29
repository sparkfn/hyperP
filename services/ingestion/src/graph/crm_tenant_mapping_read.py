"""Strict Neo4j CRM tenant mapping readers and persisted-value reconstruction."""

from __future__ import annotations

from collections.abc import Mapping

from neo4j import ManagedTransaction, Record

from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingEntry,
    CrmTenantMappingEntryTarget,
    CrmTenantMappingExpectedHead,
    CrmTenantMappingManifest,
    CrmTenantMappingRevision,
    CrmTenantMappingRollbackProvenance,
    CrmTenantMappingScope,
    CrmTenantMappingTarget,
)
from src.crm_tenant_mapping_identity import mapping_revision_id
from src.crm_tenant_mapping_models import (
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingIntegrityError,
    CrmTenantMappingRejection,
    CrmTenantMappingRevisionSnapshot,
)
from src.graph.crm_tenant_mapping_graph_values import (
    _assert_scope_values,
    _mapping,
    _mapping_value,
    _record_values,
    _relationship_kind,
    _required_int,
    _required_str,
    _revision_state,
    _scope_parameters,
)
from src.graph.crm_tenant_mapping_read_fingerprints import _assert_persisted_fingerprints
from src.graph.queries.crm_tenant_mapping import (
    READ_BY_ID,
    READ_BY_REQUEST,
    READ_REVISION,
    READ_TOPOLOGY_VIOLATIONS,
)


def _find_by_request(
    tx: ManagedTransaction, scope: CrmTenantMappingScope, request_id: str
) -> CrmTenantMappingRevisionSnapshot | None:
    rows = list(
        tx.run(READ_BY_REQUEST, **_scope_parameters(scope), preparation_request_id=request_id)
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise CrmTenantMappingIntegrityError("mapping preparation request is not unique")
    values = _record_values(rows[0])
    return _read_snapshot(
        tx, scope, _required_str(values, "revision_id"), _required_str(values, "manifest_digest")
    )


def _read_by_id(
    tx: ManagedTransaction, scope: CrmTenantMappingScope, revision_id: str
) -> CrmTenantMappingRevisionSnapshot | None:
    rows = list(tx.run(READ_BY_ID, **_scope_parameters(scope), revision_id=revision_id))
    if not rows:
        return None
    if len(rows) != 1:
        raise CrmTenantMappingIntegrityError("mapping revision ID is not unique")
    values = _record_values(rows[0])
    return _read_snapshot(
        tx, scope, _required_str(values, "revision_id"), _required_str(values, "manifest_digest")
    )


def _read_snapshot(
    tx: ManagedTransaction, scope: CrmTenantMappingScope, revision_id: str, manifest_digest: str
) -> CrmTenantMappingRevisionSnapshot | None:
    rows = list(
        tx.run(
            READ_REVISION,
            **_scope_parameters(scope),
            revision_id=revision_id,
            manifest_digest=manifest_digest,
        )
    )
    if not rows:
        return None
    topology = tx.run(READ_TOPOLOGY_VIOLATIONS, revision_id=revision_id).single()
    topology_values = _record_values(topology)
    if any(
        _required_int(topology_values, key) != 0
        for key in (
            "bad_revision_links",
            "bad_entry_links",
            "bad_target_links",
            "orphan_entries",
            "orphan_targets",
            "bad_entry_owners",
            "bad_target_owners",
        )
    ):
        raise CrmTenantMappingIntegrityError("mapping revision has unexpected topology")
    revision_values = _mapping_value(_record_values(rows[0]), "revision")
    revision = _revision_from_values(revision_values, scope)
    if revision.revision_id != revision_id or revision.manifest_digest != manifest_digest:
        raise CrmTenantMappingIntegrityError(
            "mapping revision identity conflicts with requested read"
        )
    entries, targets = _components_from_rows(rows, revision)
    manifest = CrmTenantMappingManifest(
        scope,
        tuple(entry.company_entry for entry in entries),
        _required_str(revision_values, "contract_version"),
        _required_str(revision_values, "omission_policy"),
    )
    if manifest.digest != revision.manifest_digest:
        raise CrmTenantMappingIntegrityError("mapping revision manifest digest conflicts")
    boundary = _boundary_from_values(revision_values, scope)
    rejection, rejected_at, rejection_authorization, rejection_fingerprint = _rejection_from_values(
        revision_values, revision.state
    )
    created_at = _required_str(revision_values, "created_at")
    request_fingerprint = _required_str(revision_values, "request_fingerprint")
    _assert_persisted_fingerprints(
        revision,
        manifest,
        boundary,
        created_at,
        request_fingerprint,
        rejection,
        rejected_at,
        rejection_authorization,
        rejection_fingerprint,
    )
    try:
        return CrmTenantMappingRevisionSnapshot(
            revision,
            manifest,
            boundary,
            entries,
            targets,
            created_at,
            request_fingerprint,
            rejection,
            rejected_at,
            rejection_authorization,
            rejection_fingerprint,
        )
    except ValueError as exc:
        raise CrmTenantMappingIntegrityError("mapping revision snapshot is malformed") from exc


def _components_from_rows(
    rows: list[Record], revision: CrmTenantMappingRevision
) -> tuple[tuple[CrmTenantMappingEntry, ...], tuple[CrmTenantMappingEntryTarget, ...]]:
    entry_targets: dict[str, list[CrmTenantMappingTarget]] = {}
    entry_ids: dict[str, str] = {}
    seen_rows: set[tuple[str, str | None, str | None]] = set()
    for row in rows:
        values = _record_values(row)
        entry_raw = values.get("entry")
        if entry_raw is None:
            if values.get("target") is not None:
                raise CrmTenantMappingIntegrityError("mapping target lacks its entry")
            continue
        entry_values = _mapping(entry_raw, "entry")
        company_id = _required_str(entry_values, "company_id")
        entry_id = _required_str(entry_values, "entry_id")
        target_raw = values.get("target")
        target_id = None
        entity_key = values.get("entity_key")
        if target_raw is not None:
            target_id = _required_str(_mapping(target_raw, "target"), "target_id")
        row_identity = (entry_id, target_id, entity_key if isinstance(entity_key, str) else None)
        if row_identity in seen_rows:
            raise CrmTenantMappingIntegrityError("mapping persisted links are duplicated")
        seen_rows.add(row_identity)
        expected_entry = CrmTenantMappingEntry(
            revision.revision_id, CrmTenantMappingCompanyEntry(company_id, ())
        )
        if (
            entry_values.get("revision_id") != revision.revision_id
            or entry_id != expected_entry.entry_id
        ):
            raise CrmTenantMappingIntegrityError("mapping entry identity is malformed")
        if company_id in entry_ids and entry_ids[company_id] != entry_id:
            raise CrmTenantMappingIntegrityError("mapping company has duplicate entries")
        entry_ids[company_id] = entry_id
        entry_targets.setdefault(company_id, [])
        if target_raw is None:
            continue
        target_values = _mapping(target_raw, "target")
        target = CrmTenantMappingTarget(
            _required_str(target_values, "entity_key"),
            _relationship_kind(target_values),
        )
        provisional = CrmTenantMappingEntry(
            revision.revision_id,
            CrmTenantMappingCompanyEntry(company_id, (target,)),
        )
        expected_target = CrmTenantMappingEntryTarget(provisional, target)
        if (
            target_values.get("entry_id") != entry_id
            or target_values.get("target_id") != expected_target.target_id
        ):
            raise CrmTenantMappingIntegrityError("mapping target identity is malformed")
        _assert_target_entity_link(values, target.entity_key)
        entry_targets[company_id].append(target)
    company_entries: list[CrmTenantMappingCompanyEntry] = []
    targets: list[CrmTenantMappingEntryTarget] = []
    for company_id in sorted(entry_targets, key=int):
        company_targets = tuple(sorted(entry_targets[company_id]))
        if len(set(company_targets)) != len(company_targets):
            raise CrmTenantMappingIntegrityError("mapping entry has duplicate targets")
        company_entry = CrmTenantMappingCompanyEntry(company_id, company_targets)
        entry = CrmTenantMappingEntry(revision.revision_id, company_entry)
        if entry.entry_id != entry_ids[company_id]:
            raise CrmTenantMappingIntegrityError(
                "mapping entry ID conflicts with canonical identity"
            )
        company_entries.append(company_entry)
        targets.extend(CrmTenantMappingEntryTarget(entry, target) for target in company_targets)
    entries = tuple(CrmTenantMappingEntry(revision.revision_id, item) for item in company_entries)
    return entries, tuple(targets)


def _assert_target_entity_link(values: Mapping[str, object], entity_key: str) -> None:
    labels = values.get("entity_labels")
    if (
        values.get("entity_key") != entity_key
        or not isinstance(labels, list)
        or "Entity" not in labels
    ):
        raise CrmTenantMappingIntegrityError("mapping target Entity link is malformed")


def _revision_from_values(
    values: Mapping[str, object], scope: CrmTenantMappingScope
) -> CrmTenantMappingRevision:
    _assert_scope_values(values, scope)
    provenance = _provenance_from_values(values, scope)
    try:
        revision = CrmTenantMappingRevision(
            scope,
            _required_str(values, "revision_id"),
            _required_int(values, "revision_number"),
            _required_str(values, "manifest_digest"),
            _required_int(values, "company_entry_count"),
            _required_int(values, "target_count"),
            _required_str(values, "preparation_request_id"),
            CrmTenantMappingAuthorization(
                _required_str(values, "authorization_actor"),
                _required_str(values, "authorization_reference"),
                _required_str(values, "authorization_digest"),
                _required_str(values, "authorized_at"),
                _required_str(values, "authorization_expires_at"),
            ),
            _revision_state(values),
            provenance,
        )
        if revision.revision_id != mapping_revision_id(scope, revision.revision_number):
            raise CrmTenantMappingIntegrityError("mapping revision ID is not deterministic")
        return revision
    except ValueError as exc:
        raise CrmTenantMappingIntegrityError("mapping revision properties are malformed") from exc


def _boundary_from_values(
    values: Mapping[str, object], scope: CrmTenantMappingScope
) -> CrmTenantMappingExpectedHeadBoundary:
    present = values.get("expected_head_present")
    if not isinstance(present, bool):
        raise CrmTenantMappingIntegrityError("mapping expected-head presence is malformed")
    head_id = _required_str(values, "expected_head_id")
    if not present:
        if any(
            values.get(key) is not None
            for key in (
                "expected_active_revision_id",
                "expected_active_revision_number",
                "expected_active_manifest_digest",
            )
        ):
            raise CrmTenantMappingIntegrityError(
                "absent mapping expected head carries predecessor fields"
            )
        return CrmTenantMappingExpectedHeadBoundary(scope, head_id, None)
    try:
        expected = CrmTenantMappingExpectedHead(
            head_id,
            _required_str(values, "expected_active_revision_id"),
            _required_int(values, "expected_active_revision_number"),
            _required_str(values, "expected_active_manifest_digest"),
        )
        return CrmTenantMappingExpectedHeadBoundary(scope, head_id, expected)
    except ValueError as exc:
        raise CrmTenantMappingIntegrityError("mapping expected head is malformed") from exc


def _provenance_from_values(
    values: Mapping[str, object], scope: CrmTenantMappingScope
) -> CrmTenantMappingRollbackProvenance | None:
    revision_id = values.get("rollback_of_revision_id")
    number = values.get("rollback_of_revision_number")
    digest = values.get("rollback_of_manifest_digest")
    if revision_id is None and number is None and digest is None:
        return None
    try:
        provenance = CrmTenantMappingRollbackProvenance(
            _required_str(values, "rollback_of_revision_id"),
            _required_int(values, "rollback_of_revision_number"),
            _required_str(values, "rollback_of_manifest_digest"),
        )
        if provenance.rollback_of_revision_id != mapping_revision_id(
            scope, provenance.rollback_of_revision_number
        ):
            raise CrmTenantMappingIntegrityError(
                "mapping rollback provenance revision ID is not deterministic"
            )
        return provenance
    except ValueError as exc:
        raise CrmTenantMappingIntegrityError("mapping rollback provenance is malformed") from exc


def _rejection_from_values(
    values: Mapping[str, object], state: str
) -> tuple[
    CrmTenantMappingRejection | None,
    str | None,
    CrmTenantMappingAuthorization | None,
    str | None,
]:
    keys = (
        "rejection_actor",
        "rejection_reference",
        "rejection_reason",
        "rejected_at",
        "rejection_authorization_actor",
        "rejection_authorization_reference",
        "rejection_authorization_digest",
        "rejection_authorized_at",
        "rejection_authorization_expires_at",
        "rejection_request_fingerprint",
    )
    present = [values.get(key) is not None for key in keys]
    if state != "rejected":
        if any(present):
            raise CrmTenantMappingIntegrityError(
                "non-rejected mapping revision carries rejection metadata"
            )
        return None, None, None, None
    if not all(present):
        raise CrmTenantMappingIntegrityError("rejected mapping revision lacks rejection metadata")
    try:
        return (
            CrmTenantMappingRejection(
                _required_str(values, "rejection_actor"),
                _required_str(values, "rejection_reference"),
                _required_str(values, "rejection_reason"),
            ),
            _required_str(values, "rejected_at"),
            CrmTenantMappingAuthorization(
                _required_str(values, "rejection_authorization_actor"),
                _required_str(values, "rejection_authorization_reference"),
                _required_str(values, "rejection_authorization_digest"),
                _required_str(values, "rejection_authorized_at"),
                _required_str(values, "rejection_authorization_expires_at"),
            ),
            _required_str(values, "rejection_request_fingerprint"),
        )
    except ValueError as exc:
        raise CrmTenantMappingIntegrityError("mapping rejection metadata is malformed") from exc
