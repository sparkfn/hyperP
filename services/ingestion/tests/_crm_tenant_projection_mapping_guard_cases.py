"""Focused bounded mapping-guard cases for Issue #305."""

from __future__ import annotations

import pytest
from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingEntry,
    CrmTenantMappingEntryTarget,
    CrmTenantMappingTarget,
)
from src.crm_tenant_projection_models import CrmTenantProjectionIntegrityError
from src.graph import crm_tenant_projection_mapping_guard as mapping_guard

_DIGEST = "sha256:" + "a" * 64


def test_mapping_proof_guard_rejects_bad_target_entity_topology() -> None:
    class _Rows:
        def single(self) -> dict[str, object]:
            return {
                "stored_entry_count": 0,
                "stored_target_count": 0,
                "stored_topology_fingerprint": mapping_guard._empty_mapping_topology_fingerprint(),
                "bad_revision_links": 0,
                "bad_entry_links": 0,
                "bad_target_links": 0,
                "orphan_entries": 0,
                "orphan_targets": 0,
                "bad_entry_owners": 0,
                "bad_target_owners": 0,
                "bad_target_entities": 1,
                "entry_count": 0,
                "target_count": 0,
            }

    class _Tx:
        def run(self, _query: str, **_parameters: object) -> _Rows:
            return _Rows()

    with pytest.raises(CrmTenantProjectionIntegrityError, match="mapping topology"):
        mapping_guard._validate_mapping_proof_guard(_Tx(), "release", _DIGEST)


@pytest.mark.parametrize(
    ("scanner", "query_name", "row", "message"),
    (
        (
            mapping_guard._scan_mapping_entries,
            "entry",
            {
                "selected_revision_id": "revision-a",
                "revision_id": "revision-a",
                "entry_id": "entry-a",
                "company_id": "not-a-company-id",
            },
            "entry values",
        ),
        (
            mapping_guard._scan_mapping_targets,
            "target",
            {
                "selected_revision_id": "revision-a",
                "revision_id": "revision-a",
                "entry_id": "entry-a",
                "company_id": "not-a-company-id",
                "target_id": "target-a",
                "entity_key": "entity-a",
                "relationship_kind": "tenant_member",
            },
            "target values",
        ),
    ),
)
def test_mapping_proof_scan_classifies_malformed_canonical_values_as_integrity(
    scanner: object,
    query_name: str,
    row: dict[str, object],
    message: str,
) -> None:
    class _Rows:
        def __iter__(self) -> object:
            return iter((row,))

    class _Tx:
        def run(self, query: str, **_parameters: object) -> _Rows:
            if query_name == "entry":
                assert query == mapping_guard.READ_MAPPING_ENTRY_PROOF_PAGE
            else:
                assert query == mapping_guard.READ_MAPPING_TARGET_PROOF_PAGE
            return _Rows()

    assert callable(scanner)
    with pytest.raises(CrmTenantProjectionIntegrityError, match=message):
        if query_name == "entry":
            scanner(_Tx(), "release")
        else:
            scanner(_Tx(), "release", mapping_guard._empty_mapping_topology_fingerprint())


def test_mapping_target_proof_scan_accepts_canonical_target_identity() -> None:
    target = CrmTenantMappingTarget("entity-a")
    entry = CrmTenantMappingEntry("revision-a", CrmTenantMappingCompanyEntry("303", (target,)))
    target_id = CrmTenantMappingEntryTarget(entry, target).target_id

    class _Rows:
        def __iter__(self) -> object:
            return iter(
                (
                    {
                        "selected_revision_id": "revision-a",
                        "revision_id": "revision-a",
                        "entry_id": entry.entry_id,
                        "company_id": "303",
                        "target_id": target_id,
                        "entity_key": "entity-a",
                        "relationship_kind": "tenant_member",
                    },
                )
            )

    class _Tx:
        def run(self, query: str, **_parameters: object) -> _Rows:
            assert query == mapping_guard.READ_MAPPING_TARGET_PROOF_PAGE
            return _Rows()

    count, fingerprint = mapping_guard._scan_mapping_targets(
        _Tx(), "release", mapping_guard._empty_mapping_topology_fingerprint()
    )

    assert count == 1
    assert fingerprint != mapping_guard._empty_mapping_topology_fingerprint()
