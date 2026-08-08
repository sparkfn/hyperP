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
OPTIONAL MATCH (existing)-[:CORRECTS]->(
  existing_correction_target:CrmHistoryAuthorityDecision
)
OPTIONAL MATCH (correction_target:CrmHistoryAuthorityDecision {
  decision_id: $correction_of_decision_id
})-[:DECIDES_FOR]->(correction_group:CrmHistoryConflictGroup)
OPTIONAL MATCH (known_variant:CrmHistoryHashVariant {
  event_identity: $event_identity,
  canonical_hash: $canonical_hash
})
WITH logical, attempt, existing, existing_group, existing_variant,
     existing_correction_target, correction_target, correction_group, known_variant
WHERE $decision_kind IN ['accepted', 'variant', 'parent', 'correction']
  AND $next_fence_token > $expected_fence_token
  AND (known_variant IS NULL OR known_variant.hash_version = $hash_version)
  AND (
    ($decision_kind = 'correction'
      AND correction_target IS NOT NULL
      AND correction_group.event_identity = $event_identity
      AND correction_target.decision_id <> $decision_id
      AND datetime($available_at) >= correction_target.available_at)
    OR ($decision_kind <> 'correction'
      AND $correction_of_decision_id IS NULL
      AND existing_correction_target IS NULL)
  )
  AND (existing IS NULL OR (
    existing_group.event_identity = $event_identity
    AND existing_variant.canonical_hash = $canonical_hash
    AND existing_variant.hash_version = $hash_version
    AND existing.decision_kind = $decision_kind
    AND existing.available_at = datetime($available_at)
    AND coalesce(existing.correction_of_decision_id, '') =
        coalesce($correction_of_decision_id, '')
    AND existing.logical_parent_source_system = $logical_parent_source_system
    AND existing.logical_parent_source_record_id = $logical_parent_source_record_id
    AND existing.run_id = attempt.ingest_run_id
    AND existing.run_generation = logical.active_generation
    AND existing.prior_head_version = $expected_head_version
    AND existing.prior_fence_token = $expected_fence_token
    AND existing.head_version = $expected_head_version + 1
    AND existing.fence_token = $next_fence_token
    AND (
      ($decision_kind = 'correction'
        AND existing_correction_target.decision_id = $correction_of_decision_id)
      OR $decision_kind <> 'correction'
    )
  ))
CALL (logical, attempt, existing, correction_target) {
  WITH existing
  WHERE existing IS NOT NULL
  RETURN existing.decision_id AS decision_id,
         existing.head_version AS head_version,
         existing.fence_token AS fence_token
  UNION
  WITH logical, attempt, existing, correction_target
  WHERE existing IS NULL
  CALL () {
    WITH $event_identity AS event_identity
    WHERE ($expected_head_version = 0 AND $expected_fence_token = 0)
      OR EXISTS {
        MATCH (:CrmHistoryAuthorityHead {event_identity: event_identity})
      }
    MERGE (resolved_head:CrmHistoryAuthorityHead {event_identity: event_identity})
    ON CREATE SET resolved_head.head_version = 0,
                  resolved_head.fence_token = 0,
                  resolved_head.created_at = datetime(),
                  resolved_head.authority_creation_token = $decision_id
    SET resolved_head.head_version = resolved_head.head_version
    WITH resolved_head,
         coalesce(resolved_head.authority_creation_token = $decision_id, false) AS created
    REMOVE resolved_head.authority_creation_token
    WITH resolved_head, created
    OPTIONAL MATCH (current_decision:CrmHistoryAuthorityDecision {
      decision_id: resolved_head.decision_id
    })
    WHERE (created AND $expected_head_version = 0 AND $expected_fence_token = 0)
      OR (NOT created
        AND resolved_head.head_version = $expected_head_version
        AND resolved_head.fence_token = $expected_fence_token
        AND (resolved_head.head_version = 0 OR (
          current_decision IS NOT NULL
          AND datetime($available_at) >= current_decision.available_at
        )))
    RETURN resolved_head
  }
  MERGE (group:CrmHistoryConflictGroup {event_identity: $event_identity})
  ON CREATE SET group.created_at = datetime()
  MERGE (variant:CrmHistoryHashVariant {
    event_identity: $event_identity,
    canonical_hash: $canonical_hash
  })
  ON CREATE SET variant.created_at = datetime(), variant.hash_version = $hash_version
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
    prior_head_version: $expected_head_version,
    prior_fence_token: $expected_fence_token,
    head_version: resolved_head.head_version + 1,
    fence_token: $next_fence_token
  })
  CREATE (decision)-[:DECIDES_FOR]->(group)
  CREATE (decision)-[:SELECTS_VARIANT]->(variant)
  FOREACH (_correction IN CASE WHEN correction_target IS NULL THEN [] ELSE [1] END |
    CREATE (decision)-[:CORRECTS]->(correction_target)
  )
  SET resolved_head.head_version = decision.head_version,
      resolved_head.fence_token = decision.fence_token,
      resolved_head.decision_id = decision.decision_id,
      resolved_head.updated_at = datetime()
  RETURN decision.decision_id AS decision_id,
         decision.head_version AS head_version,
         decision.fence_token AS fence_token
}
RETURN decision_id, head_version, fence_token
"""
