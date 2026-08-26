"""Bounded, restart-safe baseline queries for identity-link stream readiness."""

BASELINE_MIGRATION_KEY = "identity_link_revision_baseline_v2"

INITIALIZE_IDENTITY_LINK_BASELINE = """
MERGE (migration:DataMigration {migration_key: $migration_key})
ON CREATE SET migration.created_at = datetime(), migration.after_link_key = '',
              migration.after_source_record_pk = ''
MERGE (counter:IdentityLinkRevisionCounter {stream_key: 'identity_link_revision_stream_v1'})
ON CREATE SET counter.current_revision = 0, counter.created_at = datetime()
RETURN migration.completed_at AS completed_at, coalesce(migration.after_link_key, '') AS after_link_key
"""


LIST_IDENTITY_LINK_PROVENANCE_BACKFILL_BATCH = """
MATCH (record:SourceRecord)-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
WHERE record.source_record_pk > $after_source_record_pk
  AND record.source_instance_id IS NOT NULL
  AND trim(record.source_instance_id) <> ''
  AND record.source_record_id =~ '^bitrix-crm-(deal|contact|lead|company)-[0-9]+$'
WITH record, split(record.source_record_id, '-') AS source_id_parts
WITH record, source_id_parts[2] AS source_entity_type, source_id_parts[3] AS source_entity_id
WITH record, source_entity_type, source_entity_id,
     CASE source_entity_type
       WHEN 'deal' THEN 'crm_deal_identity_v2'
       WHEN 'contact' THEN 'crm_contact_identity_v1'
       WHEN 'lead' THEN 'crm_lead_identity_v1'
       WHEN 'company' THEN 'crm_company_reference_v1'
     END AS identity_policy_version
WITH record, source_entity_type, source_entity_id, identity_policy_version,
     'ilk1:11:bitrix_chat' + toString(size(record.source_instance_id)) + ':' +
     record.source_instance_id + toString(size(source_entity_type)) + ':' + source_entity_type +
     toString(size(source_entity_id)) + ':' + source_entity_id +
     toString(size(identity_policy_version)) + ':' + identity_policy_version AS identity_link_key
WHERE record.source_entity_type IS NULL OR record.source_entity_type <> source_entity_type
   OR record.source_entity_id IS NULL OR record.source_entity_id <> source_entity_id
   OR record.identity_policy_version IS NULL
   OR record.identity_policy_version <> identity_policy_version
   OR record.identity_link_key IS NULL OR record.identity_link_key <> identity_link_key
ORDER BY record.source_record_pk
LIMIT $batch_size
SET record.source_entity_type = source_entity_type,
    record.source_entity_id = source_entity_id,
    record.identity_policy_version = identity_policy_version,
    record.identity_link_key = identity_link_key,
    record.updated_at = datetime()
RETURN record.source_record_pk AS source_record_pk
ORDER BY source_record_pk
"""

ADVANCE_IDENTITY_LINK_PROVENANCE_BACKFILL = """
MATCH (migration:DataMigration {migration_key: $migration_key, lease_owner: $owner_id})
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL AND migration.lease_until >= now
SET migration.after_source_record_pk = $after_source_record_pk, migration.updated_at = now,
    migration.lease_until = now + duration({seconds: $lease_seconds}),
    migration.provenance_processed_count = coalesce(migration.provenance_processed_count, 0)
      + $processed_count
RETURN migration.after_source_record_pk AS after_source_record_pk
"""

COMPLETE_IDENTITY_LINK_PROVENANCE_BACKFILL = """
MATCH (migration:DataMigration {migration_key: $migration_key, lease_owner: $owner_id})
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL AND migration.lease_until >= now
SET migration.provenance_completed_at = coalesce(migration.provenance_completed_at, now),
    migration.updated_at = now,
    migration.lease_until = now + duration({seconds: $lease_seconds})
RETURN migration.provenance_completed_at AS provenance_completed_at
"""

LIST_IDENTITY_LINK_BASELINE_BATCH = """
MATCH (record:SourceRecord)-[:FROM_SOURCE]->(:SourceSystem {source_key: 'bitrix_chat'})
WHERE record.identity_link_key IS NOT NULL
  AND record.source_entity_type IN ['deal', 'contact', 'lead', 'company']
  AND record.identity_link_key > $after_link_key
WITH record
ORDER BY record.identity_link_key, toInteger(coalesce(record.source_record_version, '0')) DESC,
         record.source_record_pk DESC
WITH record.identity_link_key AS link_key, collect(record)[0] AS current
OPTIONAL MATCH (existing_head:IdentityLinkHead {link_key: link_key})
WITH link_key, current, existing_head
WHERE existing_head IS NULL
OPTIONAL MATCH (current)-[link:LINKED_TO]->(person:Person {status: 'active'})
WHERE coalesce(link.is_active, true) = true
RETURN link_key, current.source_instance_id AS source_instance_id,
       current.source_entity_type AS source_entity_type, current.source_entity_id AS source_entity_id,
       current.identity_policy_version AS identity_policy_version, current.record_type AS record_type,
       current.lifecycle_status AS lifecycle_status, current.link_status AS link_status,
       current.retired_at AS retired_at, current.source_record_pk AS source_record_pk, toString(coalesce(current.rejected_at, current.retired_at,
         current.activated_at, current.ingested_at)) AS effective_at,
       collect(DISTINCT person.person_id) AS person_ids
ORDER BY link_key
LIMIT $batch_size
"""

ADVANCE_IDENTITY_LINK_BASELINE = """
MATCH (migration:DataMigration {migration_key: $migration_key, lease_owner: $owner_id})
WITH migration, datetime() AS now
WHERE migration.completed_at IS NULL AND migration.lease_until >= now
SET migration.after_link_key = $after_link_key, migration.updated_at = now,
    migration.lease_until = now + duration({seconds: $lease_seconds}),
    migration.processed_count = coalesce(migration.processed_count, 0) + $processed_count
RETURN migration.after_link_key AS after_link_key
"""

COMPLETE_IDENTITY_LINK_BASELINE = """
MATCH (migration:DataMigration {migration_key: $migration_key, lease_owner: $owner_id})
MERGE (counter:IdentityLinkRevisionCounter {stream_key: 'identity_link_revision_stream_v1'})
WHERE migration.completed_at IS NULL AND migration.lease_until >= datetime()
SET migration.completed_at = datetime(), migration.updated_at = datetime(),
    counter.baseline_completed_at = datetime(), counter.updated_at = datetime()
REMOVE migration.lease_owner, migration.lease_until
RETURN migration.completed_at AS completed_at
"""


ACQUIRE_IDENTITY_LINK_BASELINE_LEASE = """
MERGE (migration:DataMigration {migration_key: $migration_key})
ON CREATE SET migration.created_at = datetime(), migration.after_link_key = '',
              migration.after_source_record_pk = ''
WITH migration, datetime() AS now
WITH migration, now, migration.completed_at IS NOT NULL AS completed,
     migration.lease_owner = $owner_id OR migration.lease_until IS NULL OR migration.lease_until < now
       AS available
FOREACH (_ IN CASE WHEN NOT completed AND available THEN [1] ELSE [] END |
  SET migration.lease_owner = $owner_id,
      migration.lease_until = now + duration({seconds: $lease_seconds}),
      migration.updated_at = now
)
RETURN completed AS completed, (NOT completed AND available) AS acquired,
       coalesce(migration.after_link_key, '') AS after_link_key,
       coalesce(migration.after_source_record_pk, '') AS after_source_record_pk
"""

RELEASE_IDENTITY_LINK_BASELINE_LEASE = """
MATCH (migration:DataMigration {migration_key: $migration_key, lease_owner: $owner_id})
WHERE migration.completed_at IS NULL
REMOVE migration.lease_owner, migration.lease_until
SET migration.updated_at = datetime()
RETURN true AS released
"""
