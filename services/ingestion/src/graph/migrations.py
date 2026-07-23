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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from neo4j import ManagedTransaction, Record

from src.graph import queries
from src.graph.client import Neo4jClient
from src.graph.fundbox_source_migration import migrate_fundbox_source_keys
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
from src.raw_payload import decode_raw_payload

logger = logging.getLogger(__name__)

SOURCE_RECORD_LIFECYCLE_MIGRATION_KEY = "source_record_lifecycle_v1"
SOURCE_RECORD_LIFECYCLE_BATCH_SIZE = 500
SOURCE_RECORD_LIFECYCLE_LEASE_SECONDS = 5 * 60
SOURCE_RECORD_LIFECYCLE_LOCK_POLL_SECONDS = 1.0

START_BITRIX_CHAT_SOURCE_MIGRATION = """
MERGE (migration:DataMigration {migration_key: 'bitrix_chat_source_v1'})
ON CREATE SET migration.created_at = datetime()
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
MATCH ()-[relationship:IDENTIFIED_BY|LIVES_AT|KNOWS|HAS_FACT]->()
OPTIONAL MATCH (source:SourceRecord)
WHERE source.source_record_pk = relationship.source_record_pk
  AND (source.lifecycle_status = 'active'
    OR (source.lifecycle_status IS NULL AND source.is_latest = true))
WITH relationship,
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
RETURN count(relationship) AS updated
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


def apply_data_migrations(client: Neo4jClient) -> None:
    """Run every idempotent data migration in order."""
    backfill_record_type_subtypes(client)
    migrate_bitrix_chat_source(client)
    migrate_fundbox_source_keys(client)
    migrate_source_record_lifecycle(client)
    migrate_projection_relationship_lifecycle(client)
    reconcile_source_record_lifecycle(client)
    reconcile_projection_relationship_lifecycle(client)
