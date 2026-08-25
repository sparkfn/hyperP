"""Contracts for CRM canonical identifier scope migration."""

from __future__ import annotations

from src.graph.queries.identifier_scope_migrations import (
    BACKFILL_IDENTIFIER_SCOPES_BATCH,
    CONSOLIDATE_SCOPED_IDENTIFIER_DUPLICATES_BATCH,
    DELETE_EMPTY_UNSCOPED_CRM_IDENTIFIERS_BATCH,
    MIGRATE_CRM_IDENTIFIER_RELATIONSHIPS_BATCH,
)
from src.graph.schema_init import DEFERRED_IDENTIFIER_SCOPE_CONSTRAINTS


def test_crm_identifier_migration_rewires_provenance_to_instance_scoped_nodes() -> None:
    query = MIGRATE_CRM_IDENTIFIER_RELATIONSHIPS_BATCH

    assert "legacy.identifier_scope IS NULL" in query
    assert "legacy.identifier_type IN $crm_identifier_types" in query
    assert "source.source_instance_id" in query
    assert "$legacy_source_instance_id" in query
    assert "identifier_scope: identifier_scope" in query
    assert "MERGE (person)-[scoped_rel:IDENTIFIED_BY" in query
    assert "ON CREATE SET scoped_rel = properties(legacy_rel)" in query
    assert "DELETE legacy_rel" in query
    assert "LIMIT $batch_size" in query
    assert "legacy_rel.source_record_pk IS NULL" in query
    assert "CREATE (person)-[scoped_rel:IDENTIFIED_BY]->(scoped)" in query
    assert "$migration_key" in query


def test_identifier_scope_migration_handles_orphans_and_global_identifiers() -> None:
    assert "AND NOT (legacy)--()" in DELETE_EMPTY_UNSCOPED_CRM_IDENTIFIERS_BATCH
    assert "DELETE legacy" in DELETE_EMPTY_UNSCOPED_CRM_IDENTIFIERS_BATCH
    assert "DETACH DELETE legacy" not in DELETE_EMPTY_UNSCOPED_CRM_IDENTIFIERS_BATCH
    assert "$global_identifier_scope" in BACKFILL_IDENTIFIER_SCOPES_BATCH
    assert "$legacy_source_instance_id" in BACKFILL_IDENTIFIER_SCOPES_BATCH


def test_scoped_identifier_constraint_is_deferred_until_after_data_migration() -> None:
    schema = "\n".join(DEFERRED_IDENTIFIER_SCOPE_CONSTRAINTS)

    assert "identifier_identity_scope_unique" in schema
    expected = "REQUIRE (id.identifier_type, id.identifier_scope, id.normalized_value) IS UNIQUE"
    assert expected in schema


def test_identifier_scope_migration_consolidates_duplicates_before_unique_constraint() -> None:
    query = CONSOLIDATE_SCOPED_IDENTIFIER_DUPLICATES_BATCH

    assert "WHERE id.identifier_scope IS NOT NULL" in query
    assert "WHERE size(identifiers) > 1" in query
    assert "MERGE (person)-[scoped_rel:IDENTIFIED_BY" in query
    assert "unexpected_count = 0" in query
    assert "DETACH DELETE duplicate" in query
    assert "LIMIT $batch_size" in query
    assert "$migration_key" in query
