"""Strict persisted mapping reconstruction and query-boundary tests."""

from __future__ import annotations

import inspect
import re
from typing import cast

import pytest
from neo4j import Record
from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingEntry,
    CrmTenantMappingEntryTarget,
    CrmTenantMappingManifest,
    CrmTenantMappingRevision,
    CrmTenantMappingScope,
    CrmTenantMappingTarget,
)
from src.crm_tenant_mapping_models import (
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingIntegrityError,
    CrmTenantMappingPrepareCommand,
    mapping_head_id,
    mapping_revision_id,
)
from src.graph import crm_tenant_mapping as mapping_graph
from src.graph import crm_tenant_mapping_freshness as mapping_freshness
from src.graph.crm_tenant_mapping_read import _components_from_rows
from src.graph.crm_tenant_mapping_write import _revision_properties
from src.graph.queries import crm_tenant_mapping as queries

_DIGEST = "sha256:" + "a" * 64


class _Row:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def keys(self) -> list[str]:
        return list(self._values)

    def __getitem__(self, key: str) -> object:
        return self._values[key]


def _scope() -> CrmTenantMappingScope:
    return CrmTenantMappingScope("bitrix_chat", "portal-a", "control-a")


def _revision() -> CrmTenantMappingRevision:
    scope = _scope()
    manifest = CrmTenantMappingManifest(
        scope, (CrmTenantMappingCompanyEntry("10", (CrmTenantMappingTarget("entity-a"),)),)
    )
    return CrmTenantMappingRevision(
        scope,
        mapping_revision_id(scope, 1),
        1,
        manifest.digest,
        1,
        1,
        "request-a",
        CrmTenantMappingAuthorization(
            "actor", "reference", _DIGEST, "2026-08-29T00:00:00Z", "2026-08-30T00:00:00Z"
        ),
        "prepared",
    )


def _target_row(entity_key: str = "entity-a") -> Record:
    revision = _revision()
    target = CrmTenantMappingTarget(entity_key)
    entry = CrmTenantMappingEntry(
        revision.revision_id, CrmTenantMappingCompanyEntry("10", (target,))
    )
    entry_target = CrmTenantMappingEntryTarget(entry, target)
    return cast(
        Record,
        _Row(
            {
                "entry": {
                    "revision_id": revision.revision_id,
                    "entry_id": entry.entry_id,
                    "company_id": "10",
                },
                "target": {
                    "entry_id": entry.entry_id,
                    "target_id": entry_target.target_id,
                    "entity_key": entity_key,
                    "relationship_kind": "tenant_member",
                },
                "entity_key": entity_key,
                "entity_labels": ["Entity", "Canonical"],
            }
        ),
    )


def test_strict_component_reader_reconstructs_canonical_entry_and_target() -> None:
    entries, targets = _components_from_rows([_target_row()], _revision())

    assert entries[0].company_id == "10"
    assert targets[0].entity_key == "entity-a"


def test_strict_component_reader_rejects_duplicate_or_wrong_entity_target_links() -> None:
    with pytest.raises(CrmTenantMappingIntegrityError, match="duplicated"):
        _components_from_rows([_target_row(), _target_row()], _revision())
    malformed = _target_row()
    cast(_Row, malformed)._values["entity_key"] = "entity-b"
    with pytest.raises(CrmTenantMappingIntegrityError, match="Entity link"):
        _components_from_rows([malformed], _revision())


def test_mapping_write_queries_are_parameterized_and_cannot_mutate_entities_or_heads() -> None:
    write_queries = (
        queries.LOCK_SCOPE,
        queries.ALLOCATE_REVISION_NUMBER,
        queries.CHECK_REVISION_ID,
        queries.CREATE_REVISION,
        queries.CREATE_ENTRIES,
        queries.CREATE_TARGETS,
        queries.LOCK_REVISION_FOR_REJECTION,
        queries.REJECT_REVISION,
    )
    entity_or_person_write = re.compile(r"(?:CREATE|MERGE)\s*\([^)]*:(?:ENTITY|PERSON)\b")
    entity_or_person_set = re.compile(r"SET\s+(?:ENTITY|PERSON)\b")

    for query in write_queries:
        normalized = query.upper()
        assert "$" in normalized
        assert "CRMTENANTMAPPINGACTIVEHEAD" not in normalized
        assert "PERSON" not in normalized
        assert "DELETE" not in normalized
        assert entity_or_person_write.search(normalized) is None
        assert entity_or_person_set.search(normalized) is None

    validation = queries.VALIDATE_ENTITIES.upper()
    assert "$ENTITY_KEYS" in validation
    assert "OPTIONAL MATCH (ENTITY:ENTITY" in validation
    assert "CREATE" not in validation
    assert "MERGE" not in validation
    assert "SET" not in validation


def test_active_reader_uses_only_the_exact_head_and_never_a_latest_revision_fallback() -> None:
    source = inspect.getsource(mapping_graph.Neo4jCrmTenantMappingRepository.get_active_revision)
    queries_source = inspect.getsource(queries).upper()

    assert "HEAD.ACTIVE_REVISION_ID" in source.upper()
    assert "HEAD.ACTIVE_MANIFEST_DIGEST" in source.upper()
    assert "REVISION_NUMBER DESC" not in queries_source
    assert (
        "CRMTENANTMAPPINGACTIVEHEAD"
        not in "\n".join(
            (
                queries.LOCK_SCOPE,
                queries.ALLOCATE_REVISION_NUMBER,
                queries.CHECK_REVISION_ID,
                queries.CREATE_REVISION,
                queries.CREATE_ENTRIES,
                queries.CREATE_TARGETS,
                queries.LOCK_REVISION_FOR_REJECTION,
                queries.REJECT_REVISION,
            )
        ).upper()
    )


def test_freshness_validators_prevalidate_then_linearize_in_a_fresh_read() -> None:
    freshness_source = inspect.getsource(mapping_freshness)

    for name in (
        "validate_source_sync",
        "validate_mapping_prepare",
        "validate_mapping_rollback",
    ):
        method = inspect.getsource(getattr(mapping_graph.Neo4jCrmTenantMappingRepository, name))
        assert method.count("execute_read(") == 2
        assert "self.get_active_head" not in method
        assert "self.get_active_revision" not in method
        assert "self.get_revision" not in method
    assert "_read_snapshot(tx" in freshness_source
    assert "def prevalidate_source_sync" in freshness_source
    assert "def prevalidate_mapping_prepare" in freshness_source
    assert "def prevalidate_mapping_rollback" in freshness_source
    assert "def validate_source_sync_at_linearization" in freshness_source
    assert "def validate_mapping_prepare_at_linearization" in freshness_source
    assert "def validate_mapping_rollback_at_linearization" in freshness_source
    assert "VALIDATE_SOURCE_SYNC_AT_LINEARIZATION" in freshness_source
    assert "VALIDATE_MAPPING_PREPARE_AT_LINEARIZATION" in freshness_source
    assert "VALIDATE_MAPPING_ROLLBACK_AT_LINEARIZATION" in freshness_source
    for query in (
        queries.VALIDATE_SOURCE_SYNC_AT_LINEARIZATION,
        queries.VALIDATE_MAPPING_PREPARE_AT_LINEARIZATION,
        queries.VALIDATE_MAPPING_ROLLBACK_AT_LINEARIZATION,
    ):
        normalized = query.upper()
        assert "$" in normalized
        assert "RETURN REVISION.REVISION_ID AS REVISION_ID" in normalized
    assert "REVISION.STATE = 'ACTIVE'" in queries.VALIDATE_SOURCE_SYNC_AT_LINEARIZATION.upper()
    assert (
        "REVISION.STATE = 'PREPARED'" in queries.VALIDATE_MAPPING_PREPARE_AT_LINEARIZATION.upper()
    )
    rollback = queries.VALIDATE_MAPPING_ROLLBACK_AT_LINEARIZATION.upper()
    assert "HISTORICAL.STATE IN ['ACTIVE', 'SUPERSEDED']" in rollback
    assert "ROLLBACK_OF_REVISION_ID = $ROLLBACK_OF_REVISION_ID" in rollback


def test_persisted_revision_properties_are_immutable_prepared_metadata() -> None:
    command = CrmTenantMappingPrepareCommand(
        _scope(),
        "request-a",
        CrmTenantMappingManifest(_scope(), (CrmTenantMappingCompanyEntry("10", ()),)),
        CrmTenantMappingExpectedHeadBoundary(_scope(), mapping_head_id(_scope()), None),
        CrmTenantMappingAuthorization(
            "actor", "reference", _DIGEST, "2026-08-29T00:00:00Z", "2026-08-30T00:00:00Z"
        ),
        "2026-08-29T01:00:00Z",
    )
    properties = _revision_properties(
        command, command.manifest, mapping_revision_id(_scope(), 1), 1, None
    )

    assert properties["state"] == "prepared"
    assert properties["company_entry_count"] == 1
    assert properties["target_count"] == 0
