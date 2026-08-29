from __future__ import annotations

from dataclasses import dataclass

from neo4j import ManagedTransaction

from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingEntry,
    CrmTenantMappingEntryTarget,
    CrmTenantMappingTarget,
)
from src.crm_tenant_mapping_models import CrmTenantMappingRevisionSnapshot
from src.crm_tenant_projection_models import (
    CrmTenantProjectionConflictError,
    CrmTenantProjectionIntegrityError,
)
from src.crm_tenant_projection_records import _digest
from src.graph.crm_tenant_projection_values import _RecordValue, _required_int, _required_string
from src.graph.queries.crm_tenant_projection_mapping_guard import (
    READ_MAPPING_ENTRY_PROOF_PAGE,
    READ_MAPPING_TARGET_PROOF_PAGE,
    VALIDATE_MAPPING_PROOF_GUARD,
)
from src.models import JsonValue

_MAPPING_PROOF_PAGE_LIMIT = 200
_MAPPING_TOPOLOGY_NAMESPACE = "crm-tenant-projection-mapping-topology-v1"


@dataclass(frozen=True)
class _MappingProof:
    revision_number: int
    entry_count: int
    target_count: int
    topology_fingerprint: str


def _mapping_proof(snapshot: CrmTenantMappingRevisionSnapshot) -> _MappingProof:
    fingerprint = _empty_mapping_topology_fingerprint()
    for entry in snapshot.entries:
        fingerprint = _extend_mapping_topology_fingerprint(
            fingerprint, ["entry", entry.entry_id, entry.company_id]
        )
    for target in snapshot.targets:
        fingerprint = _extend_mapping_topology_fingerprint(
            fingerprint,
            [
                "target",
                target.entry_id,
                target.target_id,
                target.entity_key,
                target.relationship_kind,
            ],
        )
    return _MappingProof(
        snapshot.revision.revision_number,
        len(snapshot.entries),
        len(snapshot.targets),
        fingerprint,
    )


def _validate_mapping_proof_guard(
    tx: ManagedTransaction,
    release_id: str,
    release_fingerprint: str,
) -> None:
    record = _read_mapping_guard(tx, release_id, release_fingerprint)
    if record is None:
        raise CrmTenantProjectionConflictError("projection prepared mapping became stale")
    _validate_mapping_guard_record(record)


def _validate_mapping_guard_record(record: _RecordValue) -> None:
    for key in (
        "bad_revision_links",
        "bad_entry_links",
        "bad_target_links",
        "orphan_entries",
        "orphan_targets",
        "bad_entry_owners",
        "bad_target_owners",
        "bad_target_entities",
    ):
        if _required_int(record, key) != 0:
            raise CrmTenantProjectionIntegrityError("projection mapping topology is malformed")
    if _required_int(record, "entry_count") != _required_int(record, "stored_entry_count"):
        raise CrmTenantProjectionIntegrityError("projection mapping entry count is malformed")
    if _required_int(record, "target_count") != _required_int(record, "stored_target_count"):
        raise CrmTenantProjectionIntegrityError("projection mapping target count is malformed")
    _required_string(record, "stored_topology_fingerprint")


def _validate_mapping_topology_fingerprint(
    tx: ManagedTransaction,
    release_id: str,
    release_fingerprint: str,
) -> None:
    entry_count, target_count, fingerprint = _scan_mapping_topology_proof(tx, release_id)
    record = _read_mapping_guard(tx, release_id, release_fingerprint)
    if record is None:
        raise CrmTenantProjectionIntegrityError("projection mapping proof is missing")
    _validate_mapping_guard_record(record)
    if (
        entry_count != _required_int(record, "stored_entry_count")
        or target_count != _required_int(record, "stored_target_count")
        or fingerprint != _required_string(record, "stored_topology_fingerprint")
    ):
        raise CrmTenantProjectionIntegrityError(
            "projection mapping topology fingerprint is malformed"
        )


def _scan_mapping_topology_proof(tx: ManagedTransaction, release_id: str) -> tuple[int, int, str]:
    entry_count, fingerprint = _scan_mapping_entries(tx, release_id)
    target_count, fingerprint = _scan_mapping_targets(tx, release_id, fingerprint)
    return entry_count, target_count, fingerprint


def _scan_mapping_entries(tx: ManagedTransaction, release_id: str) -> tuple[int, str]:
    cursor_company_id: int | None = None
    cursor_entry_id: str | None = None
    count = 0
    fingerprint = _empty_mapping_topology_fingerprint()
    while True:
        rows = list(
            tx.run(
                READ_MAPPING_ENTRY_PROOF_PAGE,
                release_id=release_id,
                cursor_company_id=cursor_company_id,
                cursor_entry_id=cursor_entry_id,
                page_limit=_MAPPING_PROOF_PAGE_LIMIT,
            )
        )
        for row in rows:
            entry_id = _required_string(row, "entry_id")
            company_id = _required_string(row, "company_id")
            if _required_string(row, "revision_id") != _required_string(
                row, "selected_revision_id"
            ):
                raise CrmTenantProjectionIntegrityError(
                    "projection mapping entry revision is malformed"
                )
            try:
                entry = CrmTenantMappingEntry(
                    _required_string(row, "revision_id"),
                    CrmTenantMappingCompanyEntry(company_id, ()),
                )
                cursor_company_id = int(company_id)
            except ValueError as exc:
                raise CrmTenantProjectionIntegrityError(
                    "projection mapping entry values are malformed"
                ) from exc
            if entry_id != entry.entry_id:
                raise CrmTenantProjectionIntegrityError(
                    "projection mapping entry identity is malformed"
                )
            cursor_entry_id = entry_id
            fingerprint = _extend_mapping_topology_fingerprint(
                fingerprint, ["entry", entry_id, company_id]
            )
            count += 1
        if len(rows) < _MAPPING_PROOF_PAGE_LIMIT:
            return count, fingerprint


def _scan_mapping_targets(
    tx: ManagedTransaction, release_id: str, fingerprint: str
) -> tuple[int, str]:
    cursor_company_id: int | None = None
    cursor_entity_key: str | None = None
    cursor_relationship_kind: str | None = None
    cursor_target_id: str | None = None
    count = 0
    while True:
        rows = list(
            tx.run(
                READ_MAPPING_TARGET_PROOF_PAGE,
                release_id=release_id,
                cursor_company_id=cursor_company_id,
                cursor_entity_key=cursor_entity_key,
                cursor_relationship_kind=cursor_relationship_kind,
                cursor_target_id=cursor_target_id,
                page_limit=_MAPPING_PROOF_PAGE_LIMIT,
            )
        )
        for row in rows:
            revision_id = _required_string(row, "revision_id")
            entry_id = _required_string(row, "entry_id")
            company_id = _required_string(row, "company_id")
            target_id = _required_string(row, "target_id")
            entity_key = _required_string(row, "entity_key")
            relationship_kind = _required_string(row, "relationship_kind")
            if revision_id != _required_string(row, "selected_revision_id"):
                raise CrmTenantProjectionIntegrityError(
                    "projection mapping target revision is malformed"
                )
            if relationship_kind != "tenant_member":
                raise CrmTenantProjectionIntegrityError(
                    "projection mapping target kind is malformed"
                )
            try:
                target = CrmTenantMappingTarget(entity_key, "tenant_member")
                entry = CrmTenantMappingEntry(
                    revision_id, CrmTenantMappingCompanyEntry(company_id, (target,))
                )
                cursor_company_id = int(company_id)
            except ValueError as exc:
                raise CrmTenantProjectionIntegrityError(
                    "projection mapping target values are malformed"
                ) from exc
            if (
                entry.entry_id != entry_id
                or target_id != CrmTenantMappingEntryTarget(entry, target).target_id
            ):
                raise CrmTenantProjectionIntegrityError(
                    "projection mapping target identity is malformed"
                )
            cursor_entity_key = entity_key
            cursor_relationship_kind = relationship_kind
            cursor_target_id = target_id
            fingerprint = _extend_mapping_topology_fingerprint(
                fingerprint,
                ["target", entry_id, target_id, entity_key, relationship_kind],
            )
            count += 1
        if len(rows) < _MAPPING_PROOF_PAGE_LIMIT:
            return count, fingerprint


def _empty_mapping_topology_fingerprint() -> str:
    return _digest(_MAPPING_TOPOLOGY_NAMESPACE, [])


def _extend_mapping_topology_fingerprint(fingerprint: str, item: list[str]) -> str:
    values: list[JsonValue] = [*item]
    item_fingerprint = _digest(_MAPPING_TOPOLOGY_NAMESPACE, values)
    return _digest(_MAPPING_TOPOLOGY_NAMESPACE, [fingerprint, item_fingerprint])


def _read_mapping_guard(
    tx: ManagedTransaction, release_id: str, release_fingerprint: str
) -> _RecordValue | None:
    return tx.run(
        VALIDATE_MAPPING_PROOF_GUARD,
        release_id=release_id,
        release_fingerprint=release_fingerprint,
        prepared_state="prepared",
        mapping_entry_link="HAS_MAPPING_ENTRY",
        mapping_target_link="HAS_MAPPING_TARGET",
        targets_entity_link="TARGETS_ENTITY",
    ).single()
