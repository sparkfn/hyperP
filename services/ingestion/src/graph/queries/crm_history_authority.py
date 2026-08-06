"""Fenced append-only authority ledger for future typed CRM stage events.

No caller is wired to this writer while stage traversal is unsupported.
"""

from __future__ import annotations

CREATE_CRM_HISTORY_AUTHORITY_CONSTRAINTS: tuple[str, ...] = (
    """CREATE CONSTRAINT crm_history_conflict_group_identity_unique IF NOT EXISTS
FOR (group:CrmHistoryConflictGroup)
REQUIRE group.event_identity IS UNIQUE""",
    """CREATE CONSTRAINT crm_history_hash_variant_identity_unique IF NOT EXISTS
FOR (variant:CrmHistoryHashVariant)
REQUIRE (variant.event_identity, variant.canonical_hash) IS UNIQUE""",
    """CREATE CONSTRAINT crm_history_authority_decision_id_unique IF NOT EXISTS
FOR (decision:CrmHistoryAuthorityDecision)
REQUIRE decision.decision_id IS UNIQUE""",
    """CREATE CONSTRAINT crm_history_authority_head_identity_unique IF NOT EXISTS
FOR (head:CrmHistoryAuthorityHead)
REQUIRE head.event_identity IS UNIQUE""",
)

# The logical-run/attempt match is deliberately inside this write.  A replay is
# idempotent only for the same immutable decision_id; a stale generation or
# failed head compare-and-swap returns no row and therefore creates no nodes.
APPEND_CRM_HISTORY_AUTHORITY_DECISION = """
MATCH (logical:IngestionLogicalRun {logical_run_id: $logical_run_id})
      -[:ACTIVE_ATTEMPT]->(attempt:IngestRun {ingest_run_id: $ingest_run_id})
WHERE logical.active_generation = $generation
  AND attempt.generation = $generation
  AND logical.status IN ['running', 'stop_requested']
OPTIONAL MATCH (existing:CrmHistoryAuthorityDecision {decision_id: $decision_id})
      -[:DECIDES_FOR]->(existing_group:CrmHistoryConflictGroup)
OPTIONAL MATCH (existing)-[:SELECTS_VARIANT]->(existing_variant:CrmHistoryHashVariant)
OPTIONAL MATCH (head:CrmHistoryAuthorityHead {event_identity: $event_identity})
WITH logical, attempt, existing, existing_group, existing_variant, head
WHERE (
  existing IS NOT NULL
  AND existing_group.event_identity = $event_identity
  AND existing_variant.canonical_hash = $canonical_hash
  AND existing_variant.hash_version = $hash_version
) OR (
  existing IS NULL
  AND $next_fence_token > $expected_fence_token
  AND (
    (head IS NULL AND $expected_head_version = 0 AND $expected_fence_token = 0)
    OR (head.head_version = $expected_head_version
      AND head.fence_token = $expected_fence_token)
  )
)
MERGE (group:CrmHistoryConflictGroup {event_identity: $event_identity})
ON CREATE SET group.created_at = datetime()
MERGE (variant:CrmHistoryHashVariant {
  event_identity: $event_identity,
  canonical_hash: $canonical_hash
})
ON CREATE SET variant.created_at = datetime(), variant.hash_version = $hash_version
MERGE (resolved_head:CrmHistoryAuthorityHead {event_identity: $event_identity})
ON CREATE SET resolved_head.head_version = 0,
              resolved_head.fence_token = 0,
              resolved_head.created_at = datetime()
FOREACH (_ IN CASE WHEN existing IS NULL THEN [1] ELSE [] END |
  CREATE (decision:CrmHistoryAuthorityDecision {
    decision_id: $decision_id,
    decision_kind: $decision_kind,
    recorded_at: datetime(),
    available_at: datetime($available_at),
    correction_of_decision_id: $correction_of_decision_id,
    logical_parent_source_system: $logical_parent_source_system,
    logical_parent_source_record_id: $logical_parent_source_record_id,
    run_id: attempt.ingest_run_id,
    run_generation: logical.active_generation,
    fence_token: $next_fence_token
  })
  CREATE (decision)-[:DECIDES_FOR]->(group)
  CREATE (decision)-[:SELECTS_VARIANT]->(variant)
  SET resolved_head.head_version = resolved_head.head_version + 1,
      resolved_head.fence_token = $next_fence_token,
      resolved_head.decision_id = decision.decision_id,
      resolved_head.updated_at = datetime()
)
WITH resolved_head, coalesce(existing.decision_id, $decision_id) AS resolved_decision_id
RETURN resolved_decision_id AS decision_id,
       resolved_head.head_version AS head_version,
       resolved_head.fence_token AS fence_token
"""
