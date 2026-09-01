"""Repository-bounded mapping-proof validation for projection releases."""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingEntry,
    CrmTenantMappingEntryTarget,
    CrmTenantMappingTarget,
)
from src.crm_tenant_projection_models import CrmTenantProjectionIntegrityError
from src.graph.client import Neo4jClient
from src.graph.crm_tenant_projection_mapping_guard import (
    _empty_mapping_topology_fingerprint,
    _extend_mapping_topology_fingerprint,
    _read_mapping_guard,
    _validate_mapping_guard_record,
)
from src.graph.crm_tenant_projection_values import _RecordValue, _required_string
from src.graph.queries.crm_tenant_projection_mapping_guard import (
    READ_MAPPING_ENTRY_PROOF_PAGE,
    READ_MAPPING_TARGET_PROOF_PAGE,
)

_MAPPING_PROOF_PAGE_LIMIT = 200


def _validate_mapping_topology_fingerprint_bounded(
    client: Neo4jClient,
    release_id: str,
    release_fingerprint: str,
) -> None:
    """Scan each proof page in an independent read transaction."""

    def read_guard(tx: ManagedTransaction) -> _RecordValue | None:
        return _read_mapping_guard(tx, release_id, release_fingerprint)

    guard = client.execute_read(read_guard)
    if guard is None:
        raise CrmTenantProjectionIntegrityError("projection mapping proof is missing")
    _validate_mapping_guard_record(guard)
    entry_count, fingerprint = _scan_entries(client, release_id)
    target_count, fingerprint = _scan_targets(client, release_id, fingerprint)
    if (
        entry_count != _required_integer(guard, "stored_entry_count")
        or target_count != _required_integer(guard, "stored_target_count")
        or fingerprint != _required_string(guard, "stored_topology_fingerprint")
    ):
        raise CrmTenantProjectionIntegrityError(
            "projection mapping topology fingerprint is malformed"
        )


def _scan_entries(client: Neo4jClient, release_id: str) -> tuple[int, str]:
    company_cursor: int | None = None
    entry_cursor: str | None = None
    count = 0
    fingerprint = _empty_mapping_topology_fingerprint()
    while True:

        def read_page(
            tx: ManagedTransaction,
            company_cursor: int | None = company_cursor,
            entry_cursor: str | None = entry_cursor,
        ) -> list[_RecordValue]:
            return _entry_page(tx, release_id, company_cursor, entry_cursor)

        page = client.execute_read(read_page)
        for row in page:
            entry_id, company_id, company_number = _validated_entry(row)
            _advance_entry_cursor(company_cursor, entry_cursor, company_number, entry_id)
            company_cursor = company_number
            entry_cursor = entry_id
            fingerprint = _extend_mapping_topology_fingerprint(
                fingerprint, ["entry", entry_id, company_id]
            )
            count += 1
        if len(page) < _MAPPING_PROOF_PAGE_LIMIT:
            return count, fingerprint


def _scan_targets(
    client: Neo4jClient,
    release_id: str,
    fingerprint: str,
) -> tuple[int, str]:
    cursor: tuple[int, str, str, str] | None = None
    count = 0
    while True:

        def read_page(
            tx: ManagedTransaction,
            cursor: tuple[int, str, str, str] | None = cursor,
        ) -> list[_RecordValue]:
            return _target_page(tx, release_id, cursor)

        page = client.execute_read(read_page)
        for row in page:
            item = _validated_target(row)
            _advance_target_cursor(cursor, item)
            cursor = item[:4]
            fingerprint = _extend_mapping_topology_fingerprint(
                fingerprint,
                ["target", item[4], item[3], item[1], item[2]],
            )
            count += 1
        if len(page) < _MAPPING_PROOF_PAGE_LIMIT:
            return count, fingerprint


def _entry_page(
    tx: ManagedTransaction,
    release_id: str,
    company_cursor: int | None,
    entry_cursor: str | None,
) -> list[_RecordValue]:
    return list(
        tx.run(
            READ_MAPPING_ENTRY_PROOF_PAGE,
            release_id=release_id,
            cursor_company_id=company_cursor,
            cursor_entry_id=entry_cursor,
            page_limit=_MAPPING_PROOF_PAGE_LIMIT,
        )
    )


def _target_page(
    tx: ManagedTransaction,
    release_id: str,
    cursor: tuple[int, str, str, str] | None,
) -> list[_RecordValue]:
    return list(
        tx.run(
            READ_MAPPING_TARGET_PROOF_PAGE,
            release_id=release_id,
            cursor_company_id=None if cursor is None else cursor[0],
            cursor_entity_key=None if cursor is None else cursor[1],
            cursor_relationship_kind=None if cursor is None else cursor[2],
            cursor_target_id=None if cursor is None else cursor[3],
            page_limit=_MAPPING_PROOF_PAGE_LIMIT,
        )
    )


def _validated_entry(row: _RecordValue) -> tuple[str, str, int]:
    revision_id = _required_string(row, "revision_id")
    if revision_id != _required_string(row, "selected_revision_id"):
        raise CrmTenantProjectionIntegrityError("projection mapping entry revision is malformed")
    entry_id = _required_string(row, "entry_id")
    company_id = _required_string(row, "company_id")
    try:
        entry = CrmTenantMappingEntry(revision_id, CrmTenantMappingCompanyEntry(company_id, ()))
        company_number = int(company_id)
    except ValueError as exc:
        raise CrmTenantProjectionIntegrityError(
            "projection mapping entry values are malformed"
        ) from exc
    if entry_id != entry.entry_id:
        raise CrmTenantProjectionIntegrityError("projection mapping entry identity is malformed")
    return entry_id, company_id, company_number


def _validated_target(row: _RecordValue) -> tuple[int, str, str, str, str]:
    revision_id = _required_string(row, "revision_id")
    if revision_id != _required_string(row, "selected_revision_id"):
        raise CrmTenantProjectionIntegrityError("projection mapping target revision is malformed")
    entry_id = _required_string(row, "entry_id")
    company_id = _required_string(row, "company_id")
    target_id = _required_string(row, "target_id")
    entity_key = _required_string(row, "entity_key")
    relationship_kind = _required_string(row, "relationship_kind")
    if relationship_kind != "tenant_member":
        raise CrmTenantProjectionIntegrityError("projection mapping target kind is malformed")
    try:
        target = CrmTenantMappingTarget(entity_key, "tenant_member")
        entry = CrmTenantMappingEntry(
            revision_id, CrmTenantMappingCompanyEntry(company_id, (target,))
        )
        company_number = int(company_id)
    except ValueError as exc:
        raise CrmTenantProjectionIntegrityError(
            "projection mapping target values are malformed"
        ) from exc
    if (
        entry_id != entry.entry_id
        or target_id != CrmTenantMappingEntryTarget(entry, target).target_id
    ):
        raise CrmTenantProjectionIntegrityError("projection mapping target identity is malformed")
    return company_number, entity_key, relationship_kind, target_id, entry_id


def _advance_entry_cursor(
    prior_company: int | None,
    prior_entry: str | None,
    company: int,
    entry: str,
) -> None:
    if (
        prior_company is not None
        and prior_entry is not None
        and (company, entry)
        <= (
            prior_company,
            prior_entry,
        )
    ):
        raise CrmTenantProjectionIntegrityError("projection mapping entry cursor is malformed")


def _advance_target_cursor(
    prior: tuple[int, str, str, str] | None,
    item: tuple[int, str, str, str, str],
) -> None:
    current = item[:4]
    if prior is not None and current <= prior:
        raise CrmTenantProjectionIntegrityError("projection mapping target cursor is malformed")


def _required_integer(row: _RecordValue, key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CrmTenantProjectionIntegrityError(f"persisted {key} is malformed")
    return value
