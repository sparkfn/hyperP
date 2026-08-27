"""Append-only CRM-history authority ledger primitives."""

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

# The caller must already hold and assert the complete Bitrix stream fence in the
# same transaction.  This query additionally requires the active logical attempt
# for provenance.  Existing semantic decisions replay across attempts without
# advancing the head; same-ID/different-semantics rows are returned as conflicts.
APPEND_CRM_HISTORY_AUTHORITY_DECISION = """
MATCH (logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id, control_instance_id: $control_instance_id
})-[:ACTIVE_ATTEMPT]->(attempt:IngestRun {
  ingest_run_id: $ingest_run_id, control_instance_id: $control_instance_id
})
WHERE logical.active_generation = $generation
  AND attempt.generation = $generation
  AND logical.status IN ['running', 'stop_requested']
OPTIONAL MATCH (existing:CrmHistoryAuthorityDecision {decision_id: $decision_id})
      -[:DECIDES_FOR]->(existing_group:CrmHistoryConflictGroup)
OPTIONAL MATCH (existing)-[:SELECTS_VARIANT]->(existing_variant:CrmHistoryHashVariant)
OPTIONAL MATCH (existing)-[:CORRECTS]->(
  existing_correction_target:CrmHistoryAuthorityDecision
)
OPTIONAL MATCH (existing)-[:USES_PARENT_ASSOCIATION]->(
  existing_association:CrmHistoryParentAssociationDecision
)
OPTIONAL MATCH (association:CrmHistoryParentAssociationDecision {
  decision_id: $association_decision_id,
  event_identity: $event_identity
})
CALL (association) {
  OPTIONAL MATCH (association)-[:SELECTS_STAGE_HISTORY_PARENT]->(
    association_parent:SourceRecord
  )
  RETURN collect(DISTINCT association_parent) AS association_parents
}
OPTIONAL MATCH (correction_target:CrmHistoryAuthorityDecision {
  decision_id: $correction_of_decision_id
})-[:DECIDES_FOR]->(correction_group:CrmHistoryConflictGroup)
OPTIONAL MATCH (known_variant:CrmHistoryHashVariant {
  event_identity: $event_identity,
  canonical_hash: $canonical_hash
})
WITH logical, attempt, existing, existing_group, existing_variant,
     existing_association, association, association_parents,
     existing_correction_target, correction_target, correction_group, known_variant,
     CASE
       WHEN existing IS NULL THEN true
       ELSE existing_group.event_identity = $event_identity
         AND existing_variant.canonical_hash = $canonical_hash
         AND existing_variant.hash_version = $hash_version
         AND existing.decision_kind = $decision_kind
         AND coalesce(existing.authority_state,
           CASE existing.decision_kind
             WHEN 'accepted' THEN 'effective'
             WHEN 'variant' THEN 'withheld_conflict'
             WHEN 'parent' THEN 'withheld_parent'
             WHEN 'correction' THEN 'corrected'
             ELSE ''
           END) = $authority_state
         AND existing.available_at = datetime($available_at)
         AND coalesce(existing.correction_of_decision_id, '') =
             coalesce($correction_of_decision_id, '')
         AND existing.logical_parent_source_system = $logical_parent_source_system
         AND existing.logical_parent_source_record_id = $logical_parent_source_record_id
         AND coalesce(existing_association.decision_id, '') =
             coalesce($association_decision_id, '')
         AND coalesce(existing.expected_invalidation_target_count, 0) =
             $expected_invalidation_target_count
         AND coalesce(existing.expected_invalidation_target_digests, []) =
             $expected_invalidation_target_digests
         AND coalesce(existing.review_command_id, '') = coalesce($review_command_id, '')
         AND (
           ($decision_kind = 'correction'
             AND existing_correction_target.decision_id = $correction_of_decision_id)
           OR ($decision_kind <> 'correction' AND existing_correction_target IS NULL)
         )
     END AS semantic_match
WITH logical, attempt, existing, existing_group, existing_variant,
     existing_association, association, association_parents,
     CASE WHEN size(association_parents) = 1 THEN association_parents[0] ELSE NULL END
       AS association_parent,
     existing_correction_target, correction_target, correction_group, known_variant,
     semantic_match
WHERE ($decision_kind = 'accepted' AND $authority_state = 'effective')
   OR ($decision_kind = 'variant'
       AND $authority_state IN ['withheld_conflict', 'rejected'])
   OR ($decision_kind = 'parent'
       AND $authority_state IN ['withheld_parent', 'rejected'])
   OR ($decision_kind = 'correction' AND $authority_state = 'corrected')
CALL (logical, attempt, existing, correction_target, correction_group, known_variant,
      association, association_parents, association_parent, semantic_match) {
  WITH existing, semantic_match
  WHERE existing IS NOT NULL
  FOREACH (_ IN CASE WHEN semantic_match THEN [1] ELSE [] END |
    SET existing.authority_state = coalesce(existing.authority_state, $authority_state),
        existing.prior_authority_token = coalesce(
          existing.prior_authority_token, existing.prior_fence_token
        ),
        existing.authority_token = coalesce(existing.authority_token, existing.fence_token)
  )
  RETURN existing.decision_id AS decision_id,
         existing.head_version AS head_version,
         coalesce(existing.authority_token, existing.fence_token) AS authority_token,
         true AS replayed,
         semantic_match AS returned_semantic_match
  UNION
  WITH logical, attempt, existing, correction_target, correction_group, known_variant,
       association, association_parents, association_parent, semantic_match
  WHERE existing IS NULL
    AND (known_variant IS NULL OR known_variant.hash_version = $hash_version)
    AND (NOT $require_existing_variant OR known_variant IS NOT NULL)
    AND (
      NOT $require_selected_association
      OR ($authority_state IN ['effective', 'corrected']
        AND association.association_state = 'selected_active'
        AND size(association_parents) = 1
        AND association.selected_parent_source_record_pk =
            association_parent.source_record_pk
        AND association.logical_parent_source_record_id = association_parent.source_record_id
        AND association_parent.record_type = 'crm_deal'
        AND association_parent.lifecycle_status = 'active'
        AND EXISTS {
          MATCH (association_parent)-[:FROM_SOURCE]->(:SourceSystem {
            source_key: association.logical_parent_source_system
          })
        })
    )
    AND (
      ($decision_kind = 'correction'
        AND correction_target IS NOT NULL
        AND correction_group.event_identity = $event_identity
        AND correction_target.decision_id <> $decision_id
        AND datetime($available_at) >= correction_target.available_at)
      OR ($decision_kind <> 'correction' AND $correction_of_decision_id IS NULL)
    )
  CALL () {
    WITH $event_identity AS event_identity
    WHERE ($expected_head_version = 0 AND $expected_authority_token = 0)
      OR EXISTS {
        MATCH (:CrmHistoryAuthorityHead {event_identity: event_identity})
      }
    MERGE (resolved_head:CrmHistoryAuthorityHead {event_identity: event_identity})
    ON CREATE SET resolved_head.head_version = 0,
                  resolved_head.authority_token = 0,
                  resolved_head.fence_token = 0,
                  resolved_head.created_at = datetime()
    RETURN resolved_head
  }
  WITH logical, attempt, correction_target, known_variant, association, resolved_head,
       coalesce(resolved_head.authority_token, resolved_head.fence_token, 0)
         AS current_authority_token
  OPTIONAL MATCH (current_decision:CrmHistoryAuthorityDecision {
    decision_id: resolved_head.decision_id
  })
  WITH logical, attempt, correction_target, known_variant, association, resolved_head,
       current_authority_token, current_decision
  WHERE resolved_head.head_version = $expected_head_version
    AND current_authority_token = $expected_authority_token
    AND $next_authority_token = $expected_authority_token + 1
    AND (resolved_head.head_version = 0 OR (
      current_decision IS NOT NULL
      AND datetime($available_at) >= current_decision.available_at
    ))
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
    authority_state: $authority_state,
    recorded_at: datetime(),
    available_at: datetime($available_at),
    correction_of_decision_id: $correction_of_decision_id,
    logical_parent_source_system: $logical_parent_source_system,
    logical_parent_source_record_id: $logical_parent_source_record_id,
    association_decision_id: $association_decision_id,
    expected_invalidation_target_count: $expected_invalidation_target_count,
    expected_invalidation_target_digests: $expected_invalidation_target_digests,
    review_command_id: $review_command_id,
    run_id: attempt.ingest_run_id,
    run_generation: logical.active_generation,
    prior_head_version: $expected_head_version,
    prior_authority_token: $expected_authority_token,
    authority_token: $next_authority_token,
    prior_fence_token: $expected_authority_token,
    head_version: $expected_head_version + 1,
    fence_token: $next_authority_token
  })
  CREATE (decision)-[:DECIDES_FOR]->(group)
  CREATE (decision)-[:SELECTS_VARIANT]->(variant)
  FOREACH (_association IN CASE WHEN association IS NULL THEN [] ELSE [1] END |
    CREATE (decision)-[:USES_PARENT_ASSOCIATION]->(association)
  )
  FOREACH (_correction IN CASE WHEN correction_target IS NULL THEN [] ELSE [1] END |
    CREATE (decision)-[:CORRECTS]->(correction_target)
  )
  SET resolved_head.head_version = decision.head_version,
      resolved_head.authority_token = decision.authority_token,
      resolved_head.fence_token = decision.authority_token,
      resolved_head.authority_state = decision.authority_state,
      resolved_head.decision_id = decision.decision_id,
      resolved_head.selected_variant_hash = CASE
        WHEN decision.authority_state IN ['effective', 'corrected']
          THEN $canonical_hash ELSE NULL END,
      resolved_head.association_decision_id = CASE
        WHEN decision.authority_state IN ['effective', 'corrected']
          THEN $association_decision_id ELSE NULL END,
      resolved_head.updated_at = datetime()
  RETURN decision.decision_id AS decision_id,
         decision.head_version AS head_version,
         decision.authority_token AS authority_token,
         false AS replayed,
         true AS returned_semantic_match
}
RETURN decision_id, head_version, authority_token, replayed,
       returned_semantic_match AS semantic_match
"""

GET_CRM_HISTORY_AUTHORITY_HEAD = """
OPTIONAL MATCH (head:CrmHistoryAuthorityHead {event_identity: $event_identity})
OPTIONAL MATCH (decision:CrmHistoryAuthorityDecision {decision_id: head.decision_id})
RETURN coalesce(head.head_version, 0) AS head_version,
       coalesce(head.authority_token, head.fence_token, 0) AS authority_token,
       head.authority_state AS authority_state,
       head.decision_id AS decision_id,
       head.selected_variant_hash AS selected_variant_hash,
       head.association_decision_id AS association_decision_id,
       decision.logical_parent_source_system AS logical_parent_source_system,
       decision.logical_parent_source_record_id AS logical_parent_source_record_id
"""
