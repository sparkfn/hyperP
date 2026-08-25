"""Idempotent data migrations applied on ingestion startup.

Distinct from :mod:`src.graph.schema_init` (which applies constraints/indexes)
and :mod:`src.graph.bootstrap` (which seeds entity/source-system metadata):
these rewrite existing data so it matches the current domain model. Every
migration here must be safe to run repeatedly.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal, cast

from neo4j import ManagedTransaction, Record

from src.graph import queries
from src.graph.bitrix_crm_entity_migration import migrate_bitrix_crm_entities
from src.graph.client import Neo4jClient
from src.graph.fundbox_source_migration import migrate_fundbox_source_keys
from src.graph.queries.identifier_scope_migrations import (
    BACKFILL_IDENTIFIER_SCOPES_BATCH,
    CONSOLIDATE_SCOPED_IDENTIFIER_DUPLICATES_BATCH,
    DELETE_EMPTY_UNSCOPED_CRM_IDENTIFIERS_BATCH,
    MIGRATE_CRM_IDENTIFIER_RELATIONSHIPS_BATCH,
)
from src.graph.queries.lifecycle_migrations import (
    ACQUIRE_SOURCE_RECORD_LIFECYCLE_MIGRATION,
    ADVANCE_SOURCE_RECORD_LIFECYCLE_MIGRATION,
    CLAIM_SOURCE_RECORD_LIFECYCLE_IDENTITY,
    CLEAN_LEGACY_SOURCE_RECORD_LIFECYCLE_BATCH,
    CLEAN_SOURCE_RECORD_LIFECYCLE_BATCH,
    COMPLETE_SOURCE_RECORD_LIFECYCLE_IDENTITY,
    COMPLETE_SOURCE_RECORD_LIFECYCLE_MIGRATION,
    INITIALIZE_SOURCE_RECORD_LIFECYCLE_MIGRATION,
    MIGRATE_SOURCE_RECORD_LIFECYCLE_BATCH,
    PREPARE_LEGACY_SOURCE_RECORD_LIFECYCLE_BATCH,
    PREPARE_SOURCE_RECORD_LIFECYCLE_BATCH,
    RELEASE_SOURCE_RECORD_LIFECYCLE_MIGRATION,
)
from src.graph.queries.source_instance_migrations import (
    COMPLETE_SOURCE_RECORD_SOURCE_INSTANCE_MIGRATION,
    MIGRATE_SOURCE_RECORD_IDENTITY_LOCKS,
    MIGRATE_SOURCE_RECORD_SOURCE_INSTANCES_BATCH,
)
from src.identifier_scopes import (
    CRM_CANONICAL_IDENTIFIER_TYPES,
    GLOBAL_IDENTIFIER_SCOPE,
)
from src.raw_payload import decode_raw_payload
from src.source_instances import LEGACY_DEFAULT_SOURCE_INSTANCE_ID

logger = logging.getLogger(__name__)

SOURCE_RECORD_LIFECYCLE_MIGRATION_KEY = "source_record_lifecycle_v1"
SOURCE_RECORD_SOURCE_INSTANCE_MIGRATION_KEY = "source_record_source_instance_v1"
IDENTIFIER_SCOPE_MIGRATION_KEY = "identifier_scope_v1"
SOURCE_RECORD_SOURCE_INSTANCE_BATCH_SIZE = 500
IDENTIFIER_SCOPE_MIGRATION_BATCH_SIZE = 500
SOURCE_RECORD_LIFECYCLE_BATCH_SIZE = 500
SOURCE_RECORD_LIFECYCLE_LEASE_SECONDS = 5 * 60
SOURCE_RECORD_LIFECYCLE_LOCK_POLL_SECONDS = 1.0
PERSON_COMPLETENESS_MIGRATION_KEY = "person_completeness_score_v1"
PERSON_COMPLETENESS_MIGRATION_BATCH_SIZE = 500

START_BITRIX_CHAT_SOURCE_MIGRATION = """
MERGE (migration:DataMigration {migration_key: 'bitrix_chat_source_v1'})
ON CREATE SET migration.created_at = datetime()
RETURN migration.completed_at AS completed_at
"""


START_CRM_DEAL_STAGE_PROJECTION_MIGRATION = """
MERGE (migration:DataMigration {migration_key: 'crm_deal_stage_projection_v1'})
ON CREATE SET migration.created_at = datetime()
RETURN migration.completed_at AS completed_at
"""


LIST_CRM_DEALS_MISSING_STAGE_PROJECTION = """
MATCH (record:SourceRecord {record_type: 'crm_deal'})
      -[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
WHERE record.crm_deal_stage_id IS NULL
RETURN record.source_record_pk AS source_record_pk,
       record.raw_payload AS raw_payload
"""


SET_CRM_DEAL_STAGE_PROJECTION = """
MATCH (record:SourceRecord {source_record_pk: $source_record_pk})
SET record.crm_deal_stage_id = $crm_deal_stage_id
RETURN record.source_record_pk AS source_record_pk
"""


COMPLETE_CRM_DEAL_STAGE_PROJECTION_MIGRATION = """
MATCH (migration:DataMigration {migration_key: 'crm_deal_stage_projection_v1'})
WHERE migration.completed_at IS NULL
SET migration.completed_at = datetime()
RETURN migration.completed_at AS completed_at
"""


COMPLETE_BITRIX_CHAT_SOURCE_MIGRATION = """
MATCH (migration:DataMigration {migration_key: 'bitrix_chat_source_v1'})
WHERE migration.completed_at IS NULL
SET migration.completed_at = datetime()
RETURN migration.completed_at AS completed_at
"""


REHOME_LEGACY_BITRIX_RECORDS = """
MATCH (canonical:SourceSystem {source_key: 'bitrix_chat'})
MATCH (legacy:SourceSystem {source_key: 'bitrix_openlines'})
MATCH (record:SourceRecord)-[legacy_link:FROM_SOURCE]->(legacy)
MERGE (record)-[:FROM_SOURCE]->(canonical)
SET record.source_version_key = NULL
DELETE legacy_link
RETURN count(record) AS updated
"""


REHOME_LEGACY_BITRIX_RUNS = """
MATCH (canonical:SourceSystem {source_key: 'bitrix_chat'})
MATCH (legacy:SourceSystem {source_key: 'bitrix_openlines'})
MATCH (run:IngestRun)-[legacy_link:FROM_SOURCE]->(legacy)
MERGE (run)-[:FROM_SOURCE]->(canonical)
DELETE legacy_link
RETURN count(run) AS updated
"""


DEDUPLICATE_LEGACY_BITRIX_PROJECTIONS = """
MATCH (record:SourceRecord)-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
MATCH (start)-[legacy_projection:IDENTIFIED_BY|LIVES_AT|KNOWS]->(end)
WHERE legacy_projection.source_record_pk = record.source_record_pk
  AND legacy_projection.source_system_key = 'bitrix_openlines'
MATCH (start)-[canonical_projection]->(end)
WHERE canonical_projection <> legacy_projection
  AND type(canonical_projection) = type(legacy_projection)
  AND canonical_projection.source_record_pk = record.source_record_pk
  AND canonical_projection.source_system_key = 'bitrix_chat'
WITH collect(DISTINCT legacy_projection) AS duplicates
FOREACH (duplicate IN duplicates | DELETE duplicate)
RETURN size(duplicates) AS removed
"""


REWRITE_LEGACY_BITRIX_PROJECTION_KEYS = """
MATCH (record:SourceRecord)-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
MATCH ()-[projection:IDENTIFIED_BY|LIVES_AT|KNOWS]->()
WHERE projection.source_record_pk = record.source_record_pk
  AND projection.source_system_key = 'bitrix_openlines'
SET projection.source_system_key = 'bitrix_chat',
    projection.updated_at = datetime()
RETURN count(projection) AS updated
"""


REWRITE_DIRECT_BITRIX_PROJECTION_KEYS = """
MATCH (record:SourceRecord)-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
MATCH (record)-[projection:DESCRIBES_ADDRESS|MENTIONS_VEHICLE]->()
WHERE projection.source_system_key = 'bitrix_openlines'
SET projection.source_system_key = 'bitrix_chat',
    projection.updated_at = datetime()
RETURN count(projection) AS updated
"""


LIST_BITRIX_RECORDS_FOR_OWNERSHIP = """
MATCH (record:SourceRecord)-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
OPTIONAL MATCH (record)-[:OWNED_BY]->(owner:Entity)
WITH record, collect(owner.entity_key) AS owner_entity_keys
WHERE record.entity_key IS NULL
   OR trim(record.entity_key) = ''
   OR size(owner_entity_keys) <> 1
   OR head(owner_entity_keys) <> record.entity_key
RETURN record.source_record_pk AS source_record_pk,
       record.entity_key AS entity_key,
       record.raw_payload AS raw_payload,
       owner_entity_keys
"""


LINK_BITRIX_RECORD_TO_ENTITY = """
MATCH (record:SourceRecord {source_record_pk: $source_record_pk})
MATCH (entity:Entity {entity_key: $entity_key})
OPTIONAL MATCH (record)-[stale_owner:OWNED_BY]->(:Entity)
WITH record, entity, collect(stale_owner) AS stale_owners
FOREACH (stale_owner IN stale_owners | DELETE stale_owner)
MERGE (record)-[:OWNED_BY]->(entity)
SET record.entity_key = entity.entity_key,
    record.updated_at = datetime()
RETURN entity.entity_key AS entity_key
"""


FINALIZE_BITRIX_SOURCE_MIGRATION = """
MATCH (canonical:SourceSystem {source_key: 'bitrix_chat'})
OPTIONAL MATCH (legacy:SourceSystem {source_key: 'bitrix_openlines'})
FOREACH (_ IN CASE WHEN legacy IS NOT NULL AND (
    legacy.is_active <> false OR legacy.is_active IS NULL OR legacy.retired_at IS NULL
  ) THEN [1] ELSE [] END |
  SET legacy.is_active = false,
      legacy.retired_at = coalesce(legacy.retired_at, datetime()),
      legacy.updated_at = datetime()
)
WITH canonical, legacy
OPTIONAL MATCH (canonical)-[ownership:OPERATED_BY]->(:Entity)
OPTIONAL MATCH (legacy)-[legacy_ownership:OPERATED_BY]->(:Entity)
DELETE ownership, legacy_ownership
RETURN count(ownership) + count(legacy_ownership) AS removed
"""


_MigrationPhase = Literal["prepare", "migrate", "cleanup"]


@dataclass(frozen=True)
class _MigrationState:
    phase: _MigrationPhase
    total_records: int


MIGRATE_PROJECTION_RELATIONSHIP_LIFECYCLE = """
MERGE (migration:DataMigration {migration_key: 'projection_relationship_lifecycle_v1'})
ON CREATE SET migration.created_at = datetime(), migration.lock_version = 0
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration
WHERE migration.completed_at IS NULL
CALL {
  MATCH ()-[relationship:IDENTIFIED_BY|LIVES_AT|KNOWS|HAS_FACT]->()
  WHERE relationship.is_active IS NULL
  OPTIONAL MATCH (source:SourceRecord)
  WHERE source.source_record_pk = relationship.source_record_pk
    AND (source.lifecycle_status = 'active'
      OR (source.lifecycle_status IS NULL AND source.is_latest = true))
  WITH relationship,
       relationship.source_record_pk IS NULL OR count(source) > 0 AS is_active
  SET relationship.is_active = is_active
  FOREACH (_ IN CASE WHEN is_active THEN [1] ELSE [] END |
    SET relationship.activated_at = coalesce(relationship.activated_at, datetime()))
  FOREACH (_ IN CASE WHEN is_active THEN [] ELSE [1] END |
    SET relationship.retired_at = coalesce(relationship.retired_at, datetime()))
  RETURN count(relationship) AS updated
}
SET migration.completed_at = datetime(),
    migration.updated_relationships = updated
RETURN updated
"""


RECONCILE_SOURCE_RECORD_LIFECYCLE = """
MATCH (migration:DataMigration {migration_key: 'source_record_lifecycle_v1'})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration
CALL {
  MATCH (version:SourceRecord)
  WHERE version.source_version_key IS NULL OR version.lifecycle_status IS NULL
  SET version.legacy_repair_id = CASE
    WHEN version.source_record_pk IS NULL OR version.source_record_pk = ''
    THEN coalesce(version.legacy_repair_id, randomUUID())
    ELSE version.legacy_repair_id END
  RETURN count(version) AS candidates
}
CALL {
  MATCH (version:SourceRecord)
  WHERE version.source_version_key IS NULL
  OPTIONAL MATCH (version)-[:FROM_SOURCE]->(ss:SourceSystem)
  WITH version, [key IN collect(DISTINCT ss.source_key) WHERE key IS NOT NULL] AS source_keys
  WITH version,
       CASE WHEN version.source_record_pk IS NULL OR version.source_record_pk = ''
            THEN version.legacy_repair_id ELSE version.source_record_pk END AS stable_pk,
       source_keys
  WITH version, stable_pk,
       CASE WHEN size(source_keys) = 1 THEN head(source_keys)
            ELSE 'legacy-orphan:' + stable_pk END AS source_system,
       coalesce(version.source_record_id, 'legacy-pk:' + stable_pk) AS source_record_id,
       coalesce(toString(version.source_record_version), 'legacy-pk:' + stable_pk)
         AS source_record_version
  OPTIONAL MATCH (duplicate:SourceRecord)
  WHERE duplicate <> version
    AND coalesce(duplicate.source_record_id, 'legacy-pk:' +
      coalesce(duplicate.source_record_pk, duplicate.legacy_repair_id)) = source_record_id
    AND coalesce(toString(duplicate.source_record_version), 'legacy-pk:' +
      coalesce(duplicate.source_record_pk, duplicate.legacy_repair_id)) = source_record_version
  OPTIONAL MATCH (duplicate)-[:FROM_SOURCE]->(duplicate_source:SourceSystem)
  WITH version, stable_pk, source_system, source_record_id, source_record_version,
       count(CASE WHEN duplicate_source.source_key = source_system THEN 1 END) AS duplicates
  WITH version, source_system, source_record_id, source_record_version,
       CASE WHEN duplicates = 0 THEN '' ELSE stable_pk END AS duplicate_discriminator
  SET version.source_version_key =
        'sv1:' + toString(size(source_system)) + ':' + source_system +
        toString(size(source_record_id)) + ':' + source_record_id +
        toString(size(source_record_version)) + ':' + source_record_version +
        toString(size(duplicate_discriminator)) + ':' + duplicate_discriminator
  RETURN count(version) AS keyed
}
CALL {
  MATCH (version:SourceRecord)
  WHERE version.lifecycle_status IS NULL
  OPTIONAL MATCH (version)-[:FROM_SOURCE]->(ss:SourceSystem)
  WITH version, [key IN collect(DISTINCT ss.source_key) WHERE key IS NOT NULL] AS source_keys
  WITH version,
       CASE WHEN size(source_keys) = 1 THEN head(source_keys)
            ELSE 'legacy-orphan:' + coalesce(version.source_record_pk, version.legacy_repair_id)
       END AS source_system,
       coalesce(version.source_record_id, 'legacy-pk:' +
         coalesce(version.source_record_pk, version.legacy_repair_id)) AS source_record_id
  ORDER BY coalesce(toInteger(version.source_record_version), -1),
           version.ingested_at,
           coalesce(version.source_record_pk, version.legacy_repair_id)
  WITH source_system, source_record_id, collect(version) AS versions
  CALL (source_system, source_record_id) {
    MATCH (complete:SourceRecord)
    WHERE complete.source_version_key IS NOT NULL
      AND complete.lifecycle_status IS NOT NULL
      AND complete.lifecycle_status = 'active'
      AND complete.source_record_id = source_record_id
    OPTIONAL MATCH (complete)-[:FROM_SOURCE]->(complete_source:SourceSystem)
    WITH complete, source_system,
         [key IN collect(DISTINCT complete_source.source_key) WHERE key IS NOT NULL]
           AS complete_source_keys
    WITH complete, source_system,
         CASE WHEN size(complete_source_keys) = 1 THEN head(complete_source_keys)
              ELSE 'legacy-orphan:' +
                coalesce(complete.source_record_pk, complete.legacy_repair_id) END
           AS complete_source_system
    WHERE complete_source_system = source_system
    RETURN count(complete) AS active_count
  }
  WITH versions, active_count,
       [candidate IN versions
        WHERE coalesce(candidate.link_status, '') <> 'pending_review'] AS accepted
  WITH versions,
       CASE WHEN active_count = 0 AND size(accepted) > 0 THEN last(accepted)
            ELSE NULL END AS active_version
  UNWIND versions AS version
  SET version.lifecycle_status = CASE
        WHEN version.link_status = 'pending_review' THEN 'pending_review'
        WHEN version = active_version THEN 'active'
        ELSE 'superseded' END,
      version.is_latest = version = active_version
  RETURN count(version) AS updated
}
RETURN candidates, keyed, updated
"""


RECONCILE_PROJECTION_RELATIONSHIP_LIFECYCLE = """
MATCH (migration:DataMigration {migration_key: 'projection_relationship_lifecycle_v1'})
SET migration.lock_version = coalesce(migration.lock_version, 0) + 1
WITH migration
MATCH (person:Person)-[relationship:IDENTIFIED_BY|LIVES_AT|KNOWS|HAS_FACT]->(target)
OPTIONAL MATCH (source:SourceRecord)
WHERE source.source_record_pk = relationship.source_record_pk
  AND (source.lifecycle_status = 'active'
    OR (source.lifecycle_status IS NULL AND source.is_latest = true))
WITH person, target, relationship,
     CASE WHEN relationship.source_record_pk IS NULL
          THEN coalesce(relationship.is_active, true)
          ELSE count(source) > 0 END AS expected_is_active
WHERE relationship.is_active IS NULL
   OR relationship.is_active <> expected_is_active
SET relationship.is_active = expected_is_active
FOREACH (_ IN CASE WHEN expected_is_active THEN [1] ELSE [] END |
  SET relationship.activated_at = coalesce(relationship.activated_at, datetime()),
      relationship.retired_at = null)
FOREACH (_ IN CASE WHEN expected_is_active THEN [] ELSE [1] END |
  SET relationship.retired_at = coalesce(relationship.retired_at, datetime()))
WITH count(relationship) AS updated,
     collect(DISTINCT person.person_id)
       + collect(DISTINCT CASE
           WHEN type(relationship) = 'KNOWS' AND target:Person THEN target.person_id
           ELSE null
         END) AS affected_person_ids
CALL (affected_person_ids) {
  UNWIND affected_person_ids AS person_id
  WITH DISTINCT person_id
  MATCH (affected:Person {person_id: person_id, status: 'active'})
  SET affected.analysis_input_revision =
        coalesce(affected.analysis_input_revision, 0) + 1,
      affected.analysis_dirty_at = datetime()
  RETURN count(affected) AS dirtied
}
RETURN updated
"""


def backfill_record_type_subtypes(client: Neo4jClient) -> int:
    """Reclassify legacy ``system`` / ``public_record`` records into subtypes.

    Maps existing SourceRecords with ``record_type`` of ``system`` or the
    intermediate ``public_record`` by ``source_system`` to the current subtypes
    (identity / bankruptcy / rental_flat / relationship). Returns the number of
    records updated; ``0`` once the backfill has already run.
    """

    def _work(tx: ManagedTransaction) -> int:
        record = tx.run(queries.BACKFILL_RECORD_TYPE_SUBTYPES).single()
        return int(record["updated"]) if record is not None else 0

    updated = client.execute_write(_work)
    if updated:
        logger.info("Backfilled record_type on %d legacy 'system' source records", updated)
    return updated


def count_missing_person_completeness_scores(client: Neo4jClient) -> int:
    """Return list-visible Persons that lack the required numeric completeness score."""

    def _work(tx: ManagedTransaction) -> int:
        record = tx.run(queries.COUNT_MISSING_PERSON_COMPLETENESS_SCORES).single()
        return int(record["missing_count"]) if record is not None else 0

    return client.execute_read(_work)


def backfill_missing_person_completeness_scores(
    client: Neo4jClient,
    *,
    skip_if_completed: bool = True,
) -> int:
    """Repair invalid list-visible completeness scores in restart-safe batches."""

    def _start(tx: ManagedTransaction) -> bool:
        record = tx.run(
            queries.START_PERSON_COMPLETENESS_MIGRATION,
            migration_key=PERSON_COMPLETENESS_MIGRATION_KEY,
            force=not skip_if_completed,
        ).single()
        return bool(record["completed"]) if record is not None else False

    if client.execute_write(_start) and skip_if_completed:
        return 0

    updated_total = 0
    while True:

        def _batch(tx: ManagedTransaction) -> int:
            record = tx.run(
                queries.BACKFILL_MISSING_PERSON_COMPLETENESS_SCORES_BATCH,
                migration_key=PERSON_COMPLETENESS_MIGRATION_KEY,
                batch_size=PERSON_COMPLETENESS_MIGRATION_BATCH_SIZE,
            ).single()
            return int(record["updated"]) if record is not None else 0

        updated = client.execute_write(_batch)
        updated_total += updated
        if updated == 0:
            break

    def _complete(tx: ManagedTransaction) -> tuple[int, bool]:
        record = tx.run(
            queries.COMPLETE_PERSON_COMPLETENESS_MIGRATION,
            migration_key=PERSON_COMPLETENESS_MIGRATION_KEY,
        ).single()
        if record is None:
            return 0, False
        return int(record["missing_count"]), bool(record["completed"])

    missing_count, completed = client.execute_write(_complete)
    if missing_count != 0 or not completed:
        raise RuntimeError("Person completeness migration did not reach a valid completed state")
    if updated_total:
        logger.info("Backfilled completeness scores on %d non-merged Persons", updated_total)
    return updated_total


def _run_migration_query(
    client: Neo4jClient,
    query: str,
    **params: object,
) -> Record | None:
    def _work(tx: ManagedTransaction) -> Record | None:
        # Neo4j types this boundary as dict[str, Any]; callers remain concrete.
        return tx.run(query, **params).single()  # type: ignore[arg-type]

    return client.execute_write(_work)


def _required_bool(record: Record | None, key: str) -> bool:
    if record is None:
        raise RuntimeError(f"Lifecycle migration lost its lease while reading {key}")
    value: object = record[key]
    if not isinstance(value, bool):
        raise RuntimeError(f"Lifecycle migration returned invalid {key}")
    return value


def _required_int(record: Record | None, key: str) -> int:
    if record is None:
        raise RuntimeError(f"Lifecycle migration lost its lease while reading {key}")
    value: object = record[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"Lifecycle migration returned invalid {key}")
    return value


def _required_phase(record: Record | None) -> _MigrationPhase:
    if record is None:
        raise RuntimeError("Lifecycle migration lost its lease while reading phase")
    value: object = record["phase"]
    if value not in {"prepare", "migrate", "cleanup"}:
        raise RuntimeError("Lifecycle migration returned invalid phase")
    return cast(_MigrationPhase, value)


def _lease_params(owner_id: str) -> dict[str, object]:
    return {
        "migration_key": SOURCE_RECORD_LIFECYCLE_MIGRATION_KEY,
        "owner_id": owner_id,
        "lease_seconds": SOURCE_RECORD_LIFECYCLE_LEASE_SECONDS,
    }


def _wait_for_migration_lease(client: Neo4jClient, owner_id: str) -> bool:
    while True:
        record = _run_migration_query(
            client,
            ACQUIRE_SOURCE_RECORD_LIFECYCLE_MIGRATION,
            **_lease_params(owner_id),
        )
        if _required_bool(record, "completed"):
            return False
        if _required_bool(record, "acquired"):
            return True
        logger.info("Waiting for SourceRecord lifecycle migration lease")
        time.sleep(SOURCE_RECORD_LIFECYCLE_LOCK_POLL_SECONDS)


def _initialize_source_record_migration(client: Neo4jClient, owner_id: str) -> _MigrationState:
    record = _run_migration_query(
        client,
        INITIALIZE_SOURCE_RECORD_LIFECYCLE_MIGRATION,
        **_lease_params(owner_id),
    )
    return _MigrationState(
        phase=_required_phase(record),
        total_records=_required_int(record, "total_records"),
    )


def _drain_migration_batches(
    client: Neo4jClient,
    owner_id: str,
    query: str,
    result_key: str,
    *,
    batch_size: int | None = None,
) -> int:
    total = 0
    effective_batch_size = batch_size or SOURCE_RECORD_LIFECYCLE_BATCH_SIZE
    while True:
        record = _run_migration_query(
            client,
            query,
            **_lease_params(owner_id),
            batch_size=effective_batch_size,
        )
        processed = _required_int(record, result_key)
        if processed == 0:
            return total
        total += processed


def _advance_migration_phase(
    client: Neo4jClient,
    owner_id: str,
    expected_phase: _MigrationPhase,
    next_phase: _MigrationPhase,
) -> _MigrationPhase:
    record = _run_migration_query(
        client,
        ADVANCE_SOURCE_RECORD_LIFECYCLE_MIGRATION,
        **_lease_params(owner_id),
        expected_phase=expected_phase,
        next_phase=next_phase,
    )
    return _required_phase(record)


def _claim_source_record_identity(client: Neo4jClient, owner_id: str) -> bool:
    record = _run_migration_query(
        client,
        CLAIM_SOURCE_RECORD_LIFECYCLE_IDENTITY,
        **_lease_params(owner_id),
    )
    return _required_bool(record, "claimed")


def _complete_source_record_identity(client: Neo4jClient, owner_id: str) -> None:
    record = _run_migration_query(
        client,
        COMPLETE_SOURCE_RECORD_LIFECYCLE_IDENTITY,
        **_lease_params(owner_id),
    )
    if not _required_bool(record, "completed_identity"):
        raise RuntimeError("Lifecycle migration failed to complete its current identity")


def _migrate_source_record_identities(client: Neo4jClient, owner_id: str) -> int:
    total = 0
    while _claim_source_record_identity(client, owner_id):
        total += _drain_migration_batches(
            client,
            owner_id,
            MIGRATE_SOURCE_RECORD_LIFECYCLE_BATCH,
            "updated",
        )
        _complete_source_record_identity(client, owner_id)
    return total


def _complete_source_record_migration(client: Neo4jClient, owner_id: str) -> int:
    record = _run_migration_query(
        client,
        COMPLETE_SOURCE_RECORD_LIFECYCLE_MIGRATION,
        **_lease_params(owner_id),
    )
    return _required_int(record, "updated_records")


def _release_source_record_migration(client: Neo4jClient, owner_id: str) -> None:
    _run_migration_query(
        client,
        RELEASE_SOURCE_RECORD_LIFECYCLE_MIGRATION,
        migration_key=SOURCE_RECORD_LIFECYCLE_MIGRATION_KEY,
        owner_id=owner_id,
    )


def migrate_source_record_lifecycle(client: Neo4jClient) -> int:
    """Backfill lifecycle state in leased, restart-safe identity batches."""
    owner_id = uuid.uuid4().hex
    if not _wait_for_migration_lease(client, owner_id):
        return 0

    updated_this_run = 0
    try:
        state = _initialize_source_record_migration(client, owner_id)
        logger.info(
            "SourceRecord lifecycle migration volume=%d phase=%s row_batch_size=%d",
            state.total_records,
            state.phase,
            SOURCE_RECORD_LIFECYCLE_BATCH_SIZE,
        )
        phase = state.phase
        if phase == "prepare":
            _drain_migration_batches(
                client,
                owner_id,
                PREPARE_LEGACY_SOURCE_RECORD_LIFECYCLE_BATCH,
                "processed",
            )
            _drain_migration_batches(
                client, owner_id, PREPARE_SOURCE_RECORD_LIFECYCLE_BATCH, "processed"
            )
            phase = _advance_migration_phase(client, owner_id, "prepare", "migrate")
        if phase == "migrate":
            updated_this_run = _migrate_source_record_identities(client, owner_id)
            phase = _advance_migration_phase(client, owner_id, "migrate", "cleanup")
        if phase == "cleanup":
            _drain_migration_batches(
                client, owner_id, CLEAN_SOURCE_RECORD_LIFECYCLE_BATCH, "processed"
            )
            _drain_migration_batches(
                client,
                owner_id,
                CLEAN_LEGACY_SOURCE_RECORD_LIFECYCLE_BATCH,
                "processed",
            )
        updated_total = _complete_source_record_migration(client, owner_id)
        logger.info(
            "Migrated lifecycle state on %d source records (%d in this run)",
            updated_total,
            updated_this_run,
        )
        return updated_this_run
    except Exception:
        try:
            _release_source_record_migration(client, owner_id)
        except Exception:
            logger.exception("Failed to release SourceRecord lifecycle migration lease")
        raise


def migrate_source_record_source_instances(client: Neo4jClient) -> int:
    """Assign the deterministic legacy instance and safely re-key to ``sv2``.

    This runs after the lifecycle migration so every row has stable lifecycle
    identity material.  Each new key includes its stable graph PK as a
    discriminator, which makes the re-key collision-safe even for malformed
    historical duplicate versions.
    """

    def _migrate_locks(tx: ManagedTransaction) -> int:
        record = tx.run(
            MIGRATE_SOURCE_RECORD_IDENTITY_LOCKS,
            legacy_source_instance_id=LEGACY_DEFAULT_SOURCE_INSTANCE_ID,
            batch_size=SOURCE_RECORD_SOURCE_INSTANCE_BATCH_SIZE,
        ).single()
        return int(record["updated"]) if record is not None else 0

    def _migrate_batch(tx: ManagedTransaction) -> int:
        record = tx.run(
            MIGRATE_SOURCE_RECORD_SOURCE_INSTANCES_BATCH,
            legacy_source_instance_id=LEGACY_DEFAULT_SOURCE_INSTANCE_ID,
            migration_key=SOURCE_RECORD_SOURCE_INSTANCE_MIGRATION_KEY,
            batch_size=SOURCE_RECORD_SOURCE_INSTANCE_BATCH_SIZE,
        ).single()
        return int(record["updated"]) if record is not None else 0

    updated = 0
    while True:
        migrated_locks = client.execute_write(_migrate_locks)
        updated += migrated_locks
        if migrated_locks == 0:
            break
    while True:
        batch_updated = client.execute_write(_migrate_batch)
        updated += batch_updated
        if batch_updated == 0:
            break

    def _complete(tx: ManagedTransaction) -> None:
        tx.run(
            COMPLETE_SOURCE_RECORD_SOURCE_INSTANCE_MIGRATION,
            migration_key=SOURCE_RECORD_SOURCE_INSTANCE_MIGRATION_KEY,
        ).consume()

    client.execute_write(_complete)

    # Older deployments used this constraint name for a pair-key constraint.
    # New deployments install the differently named triple constraint in
    # ``apply_schema``; removing the retired one here unlocks distinct portals
    # after every historical lock has a deterministic default instance.
    with client.session() as session:
        session.run("DROP CONSTRAINT source_record_identity_lock_unique IF EXISTS").consume()

    if updated:
        logger.info("Migrated source-instance identity on %d graph rows", updated)
    return updated


def migrate_identifier_scopes(client: Neo4jClient) -> int:
    """Split CRM canonical IDs by portal while retaining global generic IDs."""

    params: dict[str, object] = {
        "migration_key": IDENTIFIER_SCOPE_MIGRATION_KEY,
        "crm_identifier_types": sorted(CRM_CANONICAL_IDENTIFIER_TYPES),
        "legacy_source_instance_id": LEGACY_DEFAULT_SOURCE_INSTANCE_ID,
        "global_identifier_scope": GLOBAL_IDENTIFIER_SCOPE,
        "batch_size": IDENTIFIER_SCOPE_MIGRATION_BATCH_SIZE,
    }

    def _drain(query: str, result_key: str) -> int:
        total = 0
        while True:
            def _work(tx: ManagedTransaction) -> int:
                record = tx.run(query, **params).single()  # type: ignore[arg-type]
                return int(record[result_key]) if record is not None else 0

            updated = client.execute_write(_work)
            total += updated
            if updated == 0:
                return total

    rewired = _drain(MIGRATE_CRM_IDENTIFIER_RELATIONSHIPS_BATCH, "updated")
    deleted = _drain(DELETE_EMPTY_UNSCOPED_CRM_IDENTIFIERS_BATCH, "deleted")
    backfilled = _drain(BACKFILL_IDENTIFIER_SCOPES_BATCH, "updated")
    consolidated = _drain(CONSOLIDATE_SCOPED_IDENTIFIER_DUPLICATES_BATCH, "consolidated")
    total = rewired + deleted + backfilled + consolidated
    if total:
        logger.info(
            "Migrated identifier scopes (rewired=%d deleted=%d backfilled=%d consolidated=%d)",
            rewired,
            deleted,
            backfilled,
            consolidated,
        )
    return total


def migrate_projection_relationship_lifecycle(client: Neo4jClient) -> int:
    """Backfill active state on legacy projection relationships."""

    def _work(tx: ManagedTransaction) -> int:
        record = tx.run(MIGRATE_PROJECTION_RELATIONSHIP_LIFECYCLE).single()
        return int(record["updated"]) if record is not None else 0

    updated = client.execute_write(_work)
    if updated:
        logger.info("Migrated lifecycle state on %d projection relationships", updated)
    return updated


def reconcile_source_record_lifecycle(client: Neo4jClient) -> int:
    """Repair lifecycle fields on records arriving after the marker migration."""

    def _work(tx: ManagedTransaction) -> int:
        record = tx.run(RECONCILE_SOURCE_RECORD_LIFECYCLE).single()
        return int(record["updated"]) if record is not None else 0

    return client.execute_write(_work)


def reconcile_projection_relationship_lifecycle(client: Neo4jClient) -> int:
    """Repair active state on projections arriving after the marker migration."""

    def _work(tx: ManagedTransaction) -> int:
        record = tx.run(RECONCILE_PROJECTION_RELATIONSHIP_LIFECYCLE).single()
        return int(record["updated"]) if record is not None else 0

    return client.execute_write(_work)


def _bitrix_record_entity_key(record: Mapping[str, object]) -> str:
    stored_entity = record.get("entity_key")
    if isinstance(stored_entity, str) and stored_entity.strip():
        return stored_entity.strip()
    payload = decode_raw_payload(record.get("raw_payload"))
    tenant = payload.get("tenant") if payload is not None else None
    if isinstance(tenant, str) and tenant.strip():
        return tenant.strip()
    owner_entity_keys = record.get("owner_entity_keys")
    if isinstance(owner_entity_keys, list) and len(owner_entity_keys) == 1:
        owner_entity_key = owner_entity_keys[0]
        if isinstance(owner_entity_key, str) and owner_entity_key.strip():
            return owner_entity_key.strip()
    source_record_pk = record.get("source_record_pk")
    raise RuntimeError(f"Bitrix source record {source_record_pk!r} has no record-scoped entity")


def migrate_bitrix_chat_source(client: Neo4jClient) -> int:
    """Rehome the retired Open Lines source and establish record ownership atomically."""

    def _work(tx: ManagedTransaction) -> int:
        raw_marker = tx.run(START_BITRIX_CHAT_SOURCE_MIGRATION).single()
        if raw_marker is None:
            raise RuntimeError("Bitrix source migration marker could not be created")
        marker = cast("Mapping[str, object]", raw_marker)
        if marker.get("completed_at") is not None:
            return 0
        tx.run(REHOME_LEGACY_BITRIX_RECORDS).single()
        tx.run(REHOME_LEGACY_BITRIX_RUNS).single()
        tx.run(DEDUPLICATE_LEGACY_BITRIX_PROJECTIONS).single()
        tx.run(REWRITE_LEGACY_BITRIX_PROJECTION_KEYS).single()
        tx.run(REWRITE_DIRECT_BITRIX_PROJECTION_KEYS).single()
        linked = 0
        for raw_record in tx.run(LIST_BITRIX_RECORDS_FOR_OWNERSHIP):
            record = cast("Mapping[str, object]", raw_record)
            source_record_pk = record.get("source_record_pk")
            if not isinstance(source_record_pk, str) or not source_record_pk:
                raise RuntimeError("Bitrix source record is missing source_record_pk")
            entity_key = _bitrix_record_entity_key(record)
            result = tx.run(
                LINK_BITRIX_RECORD_TO_ENTITY,
                source_record_pk=source_record_pk,
                entity_key=entity_key,
            ).single()
            if result is None:
                raise RuntimeError(
                    f"Bitrix source record {source_record_pk!r} maps to unknown entity "
                    f"{entity_key!r}"
                )
            linked += 1
        tx.run(FINALIZE_BITRIX_SOURCE_MIGRATION).single()
        if tx.run(COMPLETE_BITRIX_CHAT_SOURCE_MIGRATION).single() is None:
            raise RuntimeError("Bitrix source migration could not be marked complete")
        return linked

    linked = client.execute_write(_work)
    if linked:
        logger.info("Established record-scoped ownership on %d Bitrix records", linked)
    return linked


def _crm_deal_stage_id_from_raw_payload(raw_payload: object) -> str | None:
    payload = decode_raw_payload(raw_payload)
    if payload is None:
        return None
    stage_id = payload.get("stage_id")
    if not isinstance(stage_id, str):
        stage_id = payload.get("STAGE_ID")
    return stage_id if isinstance(stage_id, str) and stage_id else None


def migrate_crm_deal_stage_projection(client: Neo4jClient) -> int:
    """Backfill the stage projection used by CRM metric aggregation."""

    def _work(tx: ManagedTransaction) -> int:
        marker = tx.run(START_CRM_DEAL_STAGE_PROJECTION_MIGRATION).single()
        if marker is None:
            raise RuntimeError("CRM deal stage projection marker could not be created")
        if marker["completed_at"] is not None:
            return 0
        updated = 0
        for row in tx.run(LIST_CRM_DEALS_MISSING_STAGE_PROJECTION):
            source_record_pk = row["source_record_pk"]
            if not isinstance(source_record_pk, str) or not source_record_pk:
                raise RuntimeError("CRM deal source record is missing source_record_pk")
            stage_id = _crm_deal_stage_id_from_raw_payload(row["raw_payload"])
            if stage_id is None:
                continue
            result = tx.run(
                SET_CRM_DEAL_STAGE_PROJECTION,
                source_record_pk=source_record_pk,
                crm_deal_stage_id=stage_id,
            ).single()
            if result is None:
                raise RuntimeError("CRM deal stage projection could not be persisted")
            updated += 1
        if tx.run(COMPLETE_CRM_DEAL_STAGE_PROJECTION_MIGRATION).single() is None:
            raise RuntimeError("CRM deal stage projection migration could not be marked complete")
        return updated

    updated = client.execute_write(_work)
    if updated:
        logger.info("Projected stage IDs on %d existing CRM deal records", updated)
    return updated


def apply_data_migrations(
    client: Neo4jClient,
    *,
    bitrix_crm_category_entities: Mapping[str, str] | None = None,
    included_bitrix_crm_category_ids: Collection[str] | None = None,
) -> None:
    """Run idempotent graph migrations required before ingestion can begin.

    Recurring lifecycle repair deliberately lives outside this initialization
    path.  It can be expensive on a populated graph and is scheduled by the
    lifecycle worker after graph initialization has released its global lock.
    """
    backfill_record_type_subtypes(client)
    backfill_missing_person_completeness_scores(client)
    migrate_bitrix_chat_source(client)
    migrate_crm_deal_stage_projection(client)
    migrate_bitrix_crm_entities(
        client,
        bitrix_crm_category_entities or {},
        included_bitrix_crm_category_ids or (),
    )
    migrate_fundbox_source_keys(client)
    migrate_source_record_lifecycle(client)
    migrate_source_record_source_instances(client)
    migrate_identifier_scopes(client)
    migrate_projection_relationship_lifecycle(client)
