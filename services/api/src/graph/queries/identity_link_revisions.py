"""Cypher for immutable, privacy-safe identity-link revisions."""

CREATE_IDENTITY_LINK_SCHEMA: tuple[str, ...] = (
    "CREATE CONSTRAINT identity_link_revision_counter_stream_key_unique IF NOT EXISTS FOR (counter:IdentityLinkRevisionCounter) REQUIRE counter.stream_key IS UNIQUE",
    "CREATE CONSTRAINT identity_link_head_link_key_unique IF NOT EXISTS FOR (head:IdentityLinkHead) REQUIRE head.link_key IS UNIQUE",
    "CREATE CONSTRAINT identity_link_revision_event_id_unique IF NOT EXISTS FOR (revision:IdentityLinkRevision) REQUIRE revision.event_id IS UNIQUE",
    "CREATE CONSTRAINT identity_link_revision_cause_key_unique IF NOT EXISTS FOR (revision:IdentityLinkRevision) REQUIRE revision.cause_key IS UNIQUE",
    "CREATE CONSTRAINT identity_link_revision_global_revision_unique IF NOT EXISTS FOR (revision:IdentityLinkRevision) REQUIRE revision.global_revision IS UNIQUE",
    "CREATE INDEX identity_link_revision_global_revision IF NOT EXISTS FOR (revision:IdentityLinkRevision) ON (revision.global_revision)",
    "CREATE INDEX identity_link_revision_link_global_revision IF NOT EXISTS FOR (revision:IdentityLinkRevision) ON (revision.link_key, revision.global_revision)",
    "CREATE INDEX identity_link_head_link_key IF NOT EXISTS FOR (head:IdentityLinkHead) ON (head.link_key)",
)

APPEND_IDENTITY_LINK_REVISIONS = """
MERGE (counter:IdentityLinkRevisionCounter {stream_key: 'identity_link_revision_stream_v1'})
ON CREATE SET counter.current_revision = 0, counter.created_at = datetime()
// Acquire Neo4j's write lock before observing duplicate causes or heads. This
// serializes global allocation and makes a concurrent duplicate a no-op.
SET counter.updated_at = datetime()
WITH counter, $rows AS input_rows
UNWIND input_rows AS candidate
OPTIONAL MATCH (existing:IdentityLinkRevision {cause_key: candidate.cause_key})
OPTIONAL MATCH (existing_head:IdentityLinkHead {link_key: candidate.link_key})
WITH counter, collect(CASE
  WHEN existing IS NULL AND (NOT $skip_existing_heads OR existing_head IS NULL) THEN candidate
  ELSE NULL
END) AS maybe_rows
WITH counter, [row IN maybe_rows WHERE row IS NOT NULL] AS rows
FOREACH (_ IN CASE WHEN size(rows) = 0 THEN [] ELSE [1] END |
  SET counter.current_revision = counter.current_revision + size(rows), counter.updated_at = datetime()
)
WITH counter, rows, counter.current_revision - size(rows) AS first_global_revision
UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS offset
WITH counter, rows[offset] AS row, first_global_revision + offset AS global_revision
OPTIONAL MATCH (previous_head:IdentityLinkHead {link_key: row.link_key})
OPTIONAL MATCH (previous:IdentityLinkRevision {event_id: previous_head.latest_event_id})
CREATE (revision:IdentityLinkRevision {
  event_id: randomUUID(), global_revision: global_revision, link_key: row.link_key,
  cause_key: row.cause_key, source_system: row.source_system,
  source_instance_id: row.source_instance_id, source_entity_type: row.source_entity_type,
  source_entity_id: row.source_entity_id, identity_policy_version: row.identity_policy_version,
  link_status: row.link_status, hyperp_person_id: row.hyperp_person_id,
  resolution_kind: row.resolution_kind,
  resolution_revision: coalesce(previous_head.latest_resolution_revision, 0) + 1,
  effective_at: datetime(row.effective_at), match_decision_id: row.match_decision_id,
  review_case_id: row.review_case_id, supersedes_event_id: previous_head.latest_event_id,
  created_at: datetime()
})
MERGE (head:IdentityLinkHead {link_key: row.link_key})
ON CREATE SET head.source_system = row.source_system, head.source_instance_id = row.source_instance_id,
  head.source_entity_type = row.source_entity_type, head.source_entity_id = row.source_entity_id,
  head.identity_policy_version = row.identity_policy_version,
  head.first_global_revision = global_revision, head.created_at = datetime()
SET head.latest_global_revision = global_revision, head.latest_resolution_revision = revision.resolution_revision,
  head.latest_event_id = revision.event_id, head.updated_at = datetime()
RETURN revision.event_id AS event_id, revision.global_revision AS global_revision,
  revision.resolution_revision AS resolution_revision
ORDER BY global_revision
"""

GET_IDENTITY_LINK_COUNTER = """
MERGE (counter:IdentityLinkRevisionCounter {stream_key: 'identity_link_revision_stream_v1'})
ON CREATE SET counter.current_revision = 0, counter.created_at = datetime()
OPTIONAL MATCH (migration:DataMigration {migration_key: 'identity_link_revision_baseline_v2'})
RETURN counter.current_revision AS current_revision, counter.baseline_completed_at AS baseline_completed_at,
       migration.completed_at AS migration_completed_at
"""

LIST_IDENTITY_LINK_EVENTS = """
MATCH (revision:IdentityLinkRevision)
WHERE revision.global_revision > $after_revision AND revision.global_revision <= $through_revision
RETURN revision {.event_id, .global_revision, .source_system, .source_instance_id, .source_entity_type,
 .source_entity_id, .identity_policy_version, .link_status, .hyperp_person_id, .resolution_kind,
 .resolution_revision, .effective_at, .match_decision_id, .review_case_id, .supersedes_event_id} AS revision
ORDER BY revision.global_revision ASC
LIMIT $limit
"""

LIST_IDENTITY_LINK_SNAPSHOT = """
MATCH (head:IdentityLinkHead)
WHERE head.first_global_revision <= $snapshot_revision AND head.link_key > $after_link_key
CALL (head) {
  OPTIONAL MATCH (revision:IdentityLinkRevision {link_key: head.link_key})
  WHERE revision.global_revision <= $snapshot_revision
  RETURN revision
  ORDER BY revision.global_revision DESC
  LIMIT 1
}
RETURN head.link_key AS link_key, revision {.event_id, .global_revision, .source_system,
 .source_instance_id, .source_entity_type, .source_entity_id, .identity_policy_version,
 .link_status, .hyperp_person_id, .resolution_kind, .resolution_revision, .effective_at,
 .match_decision_id, .review_case_id, .supersedes_event_id} AS revision
ORDER BY head.link_key ASC
LIMIT $limit
"""


GET_RESOLVED_IDENTITY_LINK_HEADS_FOR_PERSON = """
MATCH (head:IdentityLinkHead)
MATCH (revision:IdentityLinkRevision {event_id: head.latest_event_id, link_status: 'resolved',
  hyperp_person_id: $person_id})
RETURN head.source_system AS source_system, head.source_instance_id AS source_instance_id,
  head.source_entity_type AS source_entity_type, head.source_entity_id AS source_entity_id,
  head.identity_policy_version AS identity_policy_version, revision.match_decision_id AS match_decision_id,
  revision.review_case_id AS review_case_id
ORDER BY head.link_key
"""

GET_AFFECTED_IDENTITY_LINK_HEADS = """
MATCH (event:MergeEvent {merge_event_id: $merge_event_id})-[:AFFECTED_RECORD]->(record:SourceRecord)
MATCH (head:IdentityLinkHead)
WHERE head.source_system = 'bitrix_chat'
  AND head.source_instance_id = record.source_instance_id
  AND head.source_entity_type = record.source_entity_type
  AND head.source_entity_id = record.source_entity_id
  AND head.identity_policy_version = record.identity_policy_version
MATCH (revision:IdentityLinkRevision {event_id: head.latest_event_id})
RETURN DISTINCT head.source_system AS source_system, head.source_instance_id AS source_instance_id,
  head.source_entity_type AS source_entity_type, head.source_entity_id AS source_entity_id,
  head.identity_policy_version AS identity_policy_version,
  revision.match_decision_id AS match_decision_id, revision.review_case_id AS review_case_id
ORDER BY source_system, source_instance_id, source_entity_type, source_entity_id
"""
