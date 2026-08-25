"""Cypher primitives for durable Bitrix CRM stage-history ingestion.

The repository coordinator executes these statements inside one Neo4j write
transaction.  Every mutation independently revalidates the active
``crm_stage_history`` stream fence so a stale worker cannot persist a partial
unit even if a future caller accidentally omits the initial locking query.

Stage history deliberately uses dedicated relationships.  None of these
queries creates the activity-oriented ``CHILD_OF``, ``OWNED_BY``, ``LINKED_TO``,
``DETAILS_HISTORY_ITEM``, or ``REPRESENTS_HISTORY_ITEM`` relationships.
"""

from __future__ import annotations

from src.graph.queries.crm_history_authority import (
    APPEND_CRM_HISTORY_AUTHORITY_DECISION,
)

CREATE_STAGE_HISTORY_INGESTION_CONSTRAINTS: tuple[str, ...] = (
    """CREATE CONSTRAINT stage_history_unit_identity_unique IF NOT EXISTS
FOR (unit:StageHistoryUnit)
REQUIRE unit.unit_id IS UNIQUE""",
    """CREATE CONSTRAINT stage_history_unit_run_page_unique IF NOT EXISTS
FOR (unit:StageHistoryUnit)
REQUIRE (unit.logical_run_id, unit.page_sequence) IS UNIQUE""",
    """CREATE CONSTRAINT stage_history_occurrence_identity_unique IF NOT EXISTS
FOR (occurrence:StageHistoryOccurrence)
REQUIRE occurrence.occurrence_id IS UNIQUE""",
    """CREATE CONSTRAINT stage_history_identity_lock_unique IF NOT EXISTS
FOR (lock:StageHistoryIdentityLock)
REQUIRE lock.event_identity IS UNIQUE""",
    """CREATE CONSTRAINT stage_history_parent_decision_id_unique IF NOT EXISTS
FOR (decision:CrmHistoryParentAssociationDecision)
REQUIRE decision.decision_id IS UNIQUE""",
    """CREATE CONSTRAINT stage_history_retry_identity_unique IF NOT EXISTS
FOR (retry:StageHistoryRetry)
REQUIRE (retry.occurrence_id, retry.retry_sequence) IS UNIQUE""",
    """CREATE CONSTRAINT stage_history_review_command_id_unique IF NOT EXISTS
FOR (command:StageHistoryReviewCommand)
REQUIRE command.command_id IS UNIQUE""",
    """CREATE CONSTRAINT stage_history_invalidation_intent_id_unique IF NOT EXISTS
FOR (intent:CrmHistoryInvalidationIntent)
REQUIRE intent.intent_id IS UNIQUE""",
    """CREATE CONSTRAINT stage_history_unit_accounting_identity_unique IF NOT EXISTS
FOR (accounting:StageHistoryUnitAccounting)
REQUIRE accounting.unit_id IS UNIQUE""",
    """CREATE INDEX stage_history_unit_run_sequence IF NOT EXISTS
FOR (unit:StageHistoryUnit)
ON (unit.logical_run_id, unit.page_sequence)""",
    """CREATE INDEX stage_history_unit_status IF NOT EXISTS
FOR (unit:StageHistoryUnit)
ON (unit.logical_run_id, unit.status)""",
    """CREATE INDEX stage_history_occurrence_run_disposition IF NOT EXISTS
FOR (occurrence:StageHistoryOccurrence)
ON (occurrence.logical_run_id, occurrence.terminal_disposition)""",
    """CREATE INDEX stage_history_occurrence_event_identity IF NOT EXISTS
FOR (occurrence:StageHistoryOccurrence)
ON (occurrence.event_identity)""",
    """CREATE INDEX stage_history_parent_decision_event_state IF NOT EXISTS
FOR (decision:CrmHistoryParentAssociationDecision)
ON (decision.event_identity, decision.association_state)""",
    """CREATE INDEX stage_history_retry_claim_scan IF NOT EXISTS
FOR (retry:StageHistoryRetry)
ON (retry.status, retry.next_attempt_at, retry.lease_expires_at)""",
    """CREATE INDEX stage_history_review_command_claim_scan IF NOT EXISTS
FOR (command:StageHistoryReviewCommand)
ON (command.status, command.lease_expires_at)""",
    """CREATE INDEX stage_history_invalidation_claim_scan IF NOT EXISTS
FOR (intent:CrmHistoryInvalidationIntent)
ON (intent.status, intent.sequence, intent.lease_expires_at)""",
    """CREATE INDEX stage_history_source_record_family IF NOT EXISTS
FOR (record:SourceRecord)
ON (record.record_type, record.history_family)""",
)

_ACTIVE_STAGE_FENCE = """
MATCH (stream:BitrixIngestionStream {
  source_key: $source_key,
  stream_key: 'crm_stage_history',
  logical_run_id: $logical_run_id,
  ingest_run_id: $ingest_run_id,
  attempt_generation: $attempt_generation,
  stream_generation: $stream_generation,
  fencing_token: $fencing_token,
  status: 'active'
})
MATCH (logical:IngestionLogicalRun {logical_run_id: $logical_run_id})
      -[:ACTIVE_ATTEMPT]->(attempt:IngestRun {ingest_run_id: $ingest_run_id})
WHERE logical.active_generation = $attempt_generation
  AND attempt.generation = $attempt_generation
  AND logical.mode = $required_run_type
  AND logical.status IN ['running', 'stop_requested']
"""


LOCK_STAGE_HISTORY_UNIT_FENCE = (
    _ACTIVE_STAGE_FENCE
    + """
SET stream.fence_lock_version = coalesce(stream.fence_lock_version, 0) + 1
RETURN stream.fence_lock_version AS fence_lock_version,
       logical.stop_requested_at IS NOT NULL AS stop_requested
"""
)


GET_STAGE_HISTORY_COMMITTED_UNIT = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (unit:StageHistoryUnit {
  logical_run_id: $logical_run_id,
  page_sequence: $page_sequence
})
WHERE unit.unit_id = $unit_id
  AND unit.artifact_id = $artifact_id
  AND unit.unit_digest = $unit_digest
RETURN unit.status AS status,
       unit.unit_digest AS unit_digest,
       unit.next_cursor_json AS next_cursor_json,
       unit.next_checkpoint_revision AS next_checkpoint_revision,
       unit.fetched_count AS fetched_count
"""
)


GET_STAGE_HISTORY_AUTHORITY_HEAD = (
    _ACTIVE_STAGE_FENCE
    + """
OPTIONAL MATCH (head:CrmHistoryAuthorityHead {event_identity: $event_identity})
OPTIONAL MATCH (decision:CrmHistoryAuthorityDecision {decision_id: head.decision_id})
OPTIONAL MATCH (decision)-[:USES_PARENT_ASSOCIATION]->(
  association:CrmHistoryParentAssociationDecision
)
OPTIONAL MATCH (association)-[:SELECTS_STAGE_HISTORY_PARENT]->(parent:SourceRecord)
WITH head, decision, association, collect(DISTINCT parent) AS selected_parents
WITH head, decision, association, selected_parents,
     CASE WHEN size(selected_parents) = 1 THEN selected_parents[0] ELSE NULL END
       AS selected_parent
RETURN coalesce(head.head_version, 0) AS head_version,
       coalesce(head.authority_token, head.fence_token, 0) AS authority_token,
       head.authority_state AS authority_state,
       head.decision_id AS decision_id,
       head.selected_variant_hash AS selected_variant_hash,
       head.association_decision_id AS association_decision_id,
       decision.logical_parent_source_system AS logical_parent_source_system,
       decision.logical_parent_source_record_id AS logical_parent_source_record_id,
       CASE
         WHEN NOT head.authority_state IN ['effective', 'corrected'] THEN true
         ELSE association IS NOT NULL
           AND association.decision_id = head.association_decision_id
           AND association.association_state = 'selected_active'
           AND decision.logical_parent_source_system =
               association.logical_parent_source_system
           AND decision.logical_parent_source_record_id =
               association.logical_parent_source_record_id
           AND selected_parent IS NOT NULL
           AND selected_parent.source_record_pk =
               association.selected_parent_source_record_pk
           AND selected_parent.source_record_id =
               association.logical_parent_source_record_id
           AND selected_parent.record_type = 'crm_deal'
           AND selected_parent.lifecycle_status = 'active'
           AND EXISTS {
             MATCH (selected_parent)-[:FROM_SOURCE]->(:SourceSystem {
               source_key: association.logical_parent_source_system
             })
           }
       END AS selected_association_current
"""
)


CREATE_STAGE_HISTORY_UNIT = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (checkpoint:IngestionCheckpoint {
  logical_run_id: $logical_run_id,
  phase: $phase,
  generation: $attempt_generation,
  status: 'active'
})
WHERE checkpoint.connector_version = $connector_version
  AND checkpoint.schema_version = $checkpoint_schema_version
  AND checkpoint.replay_boundary = $replay_boundary
  AND $replay_boundary = 'exclusive_artifact_page_sequence'
  AND checkpoint.source_window_json = $source_window_json
  AND checkpoint.cursor_json = $expected_cursor_json
  AND coalesce(checkpoint.revision, 0) = $expected_checkpoint_revision
  AND $page_sequence = coalesce($expected_last_page_sequence, 0) + 1
  AND $expected_checkpoint_revision = coalesce($expected_last_page_sequence, 0)
  AND coalesce(checkpoint.committed_count, 0) = $expected_committed_count
  AND coalesce(checkpoint.duplicate_count, 0) = $expected_duplicate_count
  AND coalesce(checkpoint.excluded_count, 0) = $expected_excluded_count
  AND coalesce(checkpoint.retry_count, 0) = $expected_retry_count
OPTIONAL MATCH (existing:StageHistoryUnit {unit_id: $unit_id})
WITH stream, logical, attempt, checkpoint, existing
WHERE existing IS NULL OR (
  existing.logical_run_id = $logical_run_id
  AND existing.artifact_id = $artifact_id
  AND existing.page_sequence = $page_sequence
  AND existing.unit_digest = $unit_digest
  AND existing.fetched_count = $fetched_count
  AND existing.expected_cursor_json = $expected_cursor_json
  AND existing.expected_checkpoint_revision = $expected_checkpoint_revision
  AND coalesce(existing.expected_last_page_sequence, 0) =
      coalesce($expected_last_page_sequence, 0)
  AND existing.replay_boundary = $replay_boundary
)
MERGE (unit:StageHistoryUnit {unit_id: $unit_id})
ON CREATE SET unit.logical_run_id = $logical_run_id,
              unit.artifact_id = $artifact_id,
              unit.page_sequence = $page_sequence,
              unit.unit_digest = $unit_digest,
              unit.fetched_count = $fetched_count,
              unit.status = 'persisting',
              unit.expected_cursor_json = $expected_cursor_json,
              unit.expected_checkpoint_revision = $expected_checkpoint_revision,
              unit.expected_last_page_sequence =
                coalesce($expected_last_page_sequence, 0),
              unit.replay_boundary = $replay_boundary,
              unit.created_at = datetime()
MERGE (logical)-[:HAS_STAGE_HISTORY_UNIT]->(unit)
RETURN unit.unit_id AS unit_id,
       unit.status AS status,
       unit.unit_digest AS unit_digest
"""
)


UPSERT_STAGE_HISTORY_OCCURRENCE = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (unit:StageHistoryUnit {
  unit_id: $unit_id,
  logical_run_id: $logical_run_id,
  status: 'persisting'
})
WHERE unit.unit_digest = $unit_digest
  AND $terminal_disposition IN [
    'excluded_out_of_scope',
    'canonical_effective',
    'canonical_pending_parent',
    'parent_waiting',
    'parent_ambiguous',
    'same_hash_replay',
    'differing_hash_conflict'
  ]
  AND (
    ($terminal_disposition = 'excluded_out_of_scope'
      AND $parse_scope = 'out_of_scope'
      AND $identity_hash_state IS NULL
      AND $association_state IS NULL
      AND $authority_state IS NULL
      AND $retry_state = 'none')
    OR ($terminal_disposition <> 'excluded_out_of_scope'
      AND $parse_scope = 'in_scope'
      AND $identity_hash_state IN [
        'new_variant', 'existing_same_hash', 'new_conflict_variant'
      ]
      AND $association_state IN [
        'selected_active', 'selected_pending_review', 'waiting', 'ambiguous', 'rejected'
      ]
      AND $authority_state IN [
        'effective', 'withheld_parent', 'withheld_conflict', 'rejected', 'corrected'
      ]
      AND $retry_state IN [
        'none', 'pending', 'claimed', 'resolved', 'rejected', 'quarantined'
      ])
  )
MERGE (occurrence:StageHistoryOccurrence {occurrence_id: $occurrence_id})
ON CREATE SET occurrence.logical_run_id = $logical_run_id,
              occurrence.unit_id = $unit_id,
              occurrence.artifact_id = $artifact_id,
              occurrence.artifact_row_sequence = $artifact_row_sequence,
              occurrence.row_digest = $row_digest,
              occurrence.event_identity = $event_identity,
              occurrence.canonical_hash = $canonical_hash,
              occurrence.hash_version = $hash_version,
              occurrence.identity_hash_state = $identity_hash_state,
              occurrence.parse_scope = $parse_scope,
              occurrence.association_state = $association_state,
              occurrence.authority_state = $authority_state,
              occurrence.retry_state = $retry_state,
              occurrence.logical_parent_source_system = $logical_parent_source_system,
              occurrence.logical_parent_source_record_id =
                $logical_parent_source_record_id,
              occurrence.source_observed_at = datetime($source_observed_at),
              occurrence.terminal_disposition = $terminal_disposition,
              occurrence.created_at = datetime()
WITH unit, occurrence
WHERE occurrence.logical_run_id = $logical_run_id
  AND occurrence.unit_id = $unit_id
  AND occurrence.artifact_id = $artifact_id
  AND occurrence.artifact_row_sequence = $artifact_row_sequence
  AND occurrence.row_digest = $row_digest
  AND occurrence.event_identity = $event_identity
  AND occurrence.canonical_hash = $canonical_hash
  AND occurrence.hash_version = $hash_version
  AND coalesce(occurrence.identity_hash_state, '') =
      coalesce($identity_hash_state, '')
  AND occurrence.parse_scope = $parse_scope
  AND coalesce(occurrence.association_state, '') = coalesce($association_state, '')
  AND coalesce(occurrence.authority_state, '') = coalesce($authority_state, '')
  AND occurrence.retry_state = $retry_state
  AND occurrence.logical_parent_source_system = $logical_parent_source_system
  AND occurrence.logical_parent_source_record_id =
      $logical_parent_source_record_id
  AND occurrence.source_observed_at = datetime($source_observed_at)
  AND occurrence.terminal_disposition = $terminal_disposition
MERGE (unit)-[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(occurrence)
RETURN occurrence.occurrence_id AS occurrence_id,
       occurrence.terminal_disposition AS terminal_disposition
"""
)


UPSERT_STAGE_HISTORY_FAILED_OCCURRENCE = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (unit:StageHistoryUnit {
  unit_id: $unit_id,
  logical_run_id: $logical_run_id,
  status: 'persisting'
})
WHERE unit.unit_digest = $unit_digest
  AND $terminal_disposition IN ['malformed_excluded', 'capture_rejected_valid']
  AND (($terminal_disposition = 'malformed_excluded' AND $parse_scope = 'malformed')
    OR ($terminal_disposition = 'capture_rejected_valid'
      AND $parse_scope IN ['in_scope', 'out_of_scope']))
  AND $retry_state = 'none'
MERGE (occurrence:StageHistoryOccurrence {occurrence_id: $occurrence_id})
ON CREATE SET occurrence.logical_run_id = $logical_run_id,
              occurrence.unit_id = $unit_id,
              occurrence.artifact_id = $artifact_id,
              occurrence.artifact_row_sequence = $artifact_row_sequence,
              occurrence.row_digest = $row_digest,
              occurrence.safe_error_code = $safe_error_code,
              occurrence.parse_scope = $parse_scope,
              occurrence.retry_state = $retry_state,
              occurrence.source_observed_at = datetime($source_observed_at),
              occurrence.terminal_disposition = $terminal_disposition,
              occurrence.created_at = datetime()
WITH unit, occurrence
WHERE occurrence.logical_run_id = $logical_run_id
  AND occurrence.unit_id = $unit_id
  AND occurrence.artifact_id = $artifact_id
  AND occurrence.artifact_row_sequence = $artifact_row_sequence
  AND occurrence.row_digest = $row_digest
  AND coalesce(occurrence.safe_error_code, '') = coalesce($safe_error_code, '')
  AND occurrence.source_observed_at = datetime($source_observed_at)
  AND occurrence.terminal_disposition = $terminal_disposition
  AND occurrence.parse_scope = $parse_scope
  AND occurrence.retry_state = $retry_state
  AND occurrence.event_identity IS NULL
  AND occurrence.canonical_hash IS NULL
  AND occurrence.identity_hash_state IS NULL
MERGE (unit)-[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(occurrence)
RETURN occurrence.occurrence_id AS occurrence_id,
       occurrence.terminal_disposition AS terminal_disposition
"""
)


UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (unit:StageHistoryUnit {
  unit_id: $unit_id,
  logical_run_id: $logical_run_id,
  status: 'persisting'
})-[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(
  occurrence:StageHistoryOccurrence {occurrence_id: $occurrence_id}
)
MATCH (source:SourceSystem {source_key: $source_key})
WHERE unit.unit_digest = $unit_digest
  AND occurrence.event_identity = $event_identity
  AND occurrence.canonical_hash = $canonical_hash
MERGE (identity_lock:StageHistoryIdentityLock {event_identity: $event_identity})
ON CREATE SET identity_lock.created_at = datetime()
SET identity_lock.lock_version = coalesce(identity_lock.lock_version, 0) + 1
WITH unit, occurrence, source, identity_lock
OPTIONAL MATCH (known_variant:CrmHistoryHashVariant {
  event_identity: $event_identity,
  canonical_hash: $canonical_hash
})-[:EVIDENCED_BY]->(known_record:SourceRecord)
WITH unit, occurrence, source, identity_lock, known_variant,
     collect(DISTINCT known_record) AS known_records
WITH unit, occurrence, source, identity_lock, known_variant,
     CASE WHEN size(known_records) = 1 THEN known_records[0] ELSE NULL END
       AS known_record
OPTIONAL MATCH (different_variant:CrmHistoryHashVariant {
  event_identity: $event_identity
})
WHERE different_variant.canonical_hash <> $canonical_hash
WITH unit, occurrence, source, identity_lock, known_variant, known_record,
     count(DISTINCT different_variant) AS prior_different_variant_count
OPTIONAL MATCH (prior:SourceRecord {
  source_system: $source_key,
  source_instance_id: $source_instance_id,
  source_record_id: $event_identity,
  record_type: 'crm_history',
  history_family: 'stage'
})
WITH unit, occurrence, source, identity_lock, known_variant, known_record,
     prior_different_variant_count,
     coalesce(max(toInteger(prior.source_record_version)), 0) + 1 AS next_version
CALL (unit, occurrence, source, known_variant, known_record, next_version) {
  WITH occurrence, known_variant, known_record
  WHERE known_variant IS NOT NULL
    AND known_record IS NOT NULL
    AND known_variant.hash_version = $hash_version
    AND known_record.source_record_pk = $source_record_pk
    AND known_record.source_system = $source_key
    AND known_record.source_instance_id = $source_instance_id
    AND known_record.source_record_id = $event_identity
    AND known_record.source_version_key = $source_version_key
    AND known_record.record_type = 'crm_history'
    AND known_record.history_family = 'stage'
    AND known_record.history_kind = $history_kind
    AND known_record.history_source = $history_source
    AND known_record.history_projection_version = $history_projection_version
    AND known_record.history_projection_source = $history_projection_source
    AND coalesce(known_record.event_category_id, '') =
        coalesce($event_category_id, '')
    AND coalesce(known_record.event_stage_id, '') = coalesce($event_stage_id, '')
    AND coalesce(known_record.event_stage_semantic_id, '') =
        coalesce($event_stage_semantic_id, '')
    AND known_record.event_at = datetime($event_at)
    AND valueType(known_record.observed_at) STARTS WITH 'ZONED DATETIME'
    AND known_record.record_hash = $canonical_hash
    AND known_record.raw_payload = $raw_payload
    AND known_record.lifecycle_status = 'pending_review'
    AND known_record.is_latest = false
    AND known_record.link_status = 'stage_authority_only'
  RETURN known_record AS record, known_variant AS variant, false AS created
  UNION
  WITH unit, occurrence, source, known_variant, known_record, next_version
  WHERE known_variant IS NULL AND known_record IS NULL
  CREATE (record:SourceRecord {
    source_record_pk: $source_record_pk,
    source_system: $source_key,
    source_instance_id: $source_instance_id,
    source_record_id: $event_identity,
    source_record_version: toString(next_version),
    source_version_key: $source_version_key,
    record_type: 'crm_history',
    history_family: 'stage',
    history_kind: $history_kind,
    history_source: $history_source,
    history_projection_version: $history_projection_version,
    history_projection_source: $history_projection_source,
    event_category_id: $event_category_id,
    event_stage_id: $event_stage_id,
    event_stage_semantic_id: $event_stage_semantic_id,
    event_at: datetime($event_at),
    observed_at: datetime($source_observed_at),
    record_hash: $canonical_hash,
    raw_payload: $raw_payload,
    lifecycle_status: 'pending_review',
    is_latest: false,
    link_status: 'stage_authority_only',
    created_at: datetime(),
    updated_at: datetime()
  })
  CREATE (record)-[:FROM_SOURCE]->(source)
  CREATE (variant:CrmHistoryHashVariant {
    event_identity: $event_identity,
    canonical_hash: $canonical_hash,
    hash_version: $hash_version,
    created_at: datetime()
  })
  CREATE (variant)-[:EVIDENCED_BY]->(record)
  RETURN record, variant, true AS created
}
MERGE (occurrence)-[:OBSERVED_STAGE_HISTORY_VARIANT]->(variant)
RETURN record.source_record_pk AS source_record_pk,
       record.source_record_version AS source_record_version,
       variant.event_identity AS event_identity,
       variant.canonical_hash AS canonical_hash,
       created,
       prior_different_variant_count
"""
)


RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (unit:StageHistoryUnit {
  unit_id: $unit_id,
  logical_run_id: $logical_run_id,
  status: 'persisting'
})-[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(
  occurrence:StageHistoryOccurrence {occurrence_id: $occurrence_id}
)
MERGE (parent_identity_lock:SourceRecordIdentityLock {
  source_system: $logical_parent_source_system,
  source_instance_id: $logical_parent_source_instance_id,
  source_record_id: $logical_parent_source_record_id
})
SET parent_identity_lock.locked_at = datetime()
WITH occurrence, parent_identity_lock
OPTIONAL MATCH (parent:SourceRecord {
  source_instance_id: $logical_parent_source_instance_id,
  source_record_id: $logical_parent_source_record_id,
  record_type: 'crm_deal'
})-[:FROM_SOURCE]->(:SourceSystem {source_key: $logical_parent_source_system})
WHERE parent.lifecycle_status IN ['active', 'pending_review']
WITH occurrence,
     [candidate IN collect(parent)
      WHERE candidate IS NOT NULL AND candidate.lifecycle_status = 'active'] AS active,
     [candidate IN collect(parent)
      WHERE candidate IS NOT NULL AND candidate.lifecycle_status = 'pending_review'] AS pending
RETURN occurrence.occurrence_id AS occurrence_id,
       size(active) AS active_count,
       size(pending) AS pending_count,
       CASE
         WHEN size(active) = 1 THEN 'selected_active'
         WHEN size(active) > 1 THEN 'ambiguous'
         WHEN size(pending) = 1 THEN 'selected_pending_review'
         WHEN size(pending) > 1 THEN 'ambiguous'
         ELSE 'waiting'
       END AS association_state,
       CASE
         WHEN size(active) = 1 THEN active[0].source_record_pk
         WHEN size(active) = 0 AND size(pending) = 1 THEN pending[0].source_record_pk
         ELSE NULL
       END AS selected_parent_source_record_pk
"""
)


APPEND_STAGE_HISTORY_PARENT_DECISION = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (occurrence:StageHistoryOccurrence {occurrence_id: $occurrence_id})
WHERE occurrence.event_identity = $event_identity
  AND occurrence.logical_parent_source_system = $logical_parent_source_system
  AND occurrence.logical_parent_source_record_id =
      $logical_parent_source_record_id
MERGE (parent_identity_lock:SourceRecordIdentityLock {
  source_system: $logical_parent_source_system,
  source_instance_id: $logical_parent_source_instance_id,
  source_record_id: $logical_parent_source_record_id
})
SET parent_identity_lock.locked_at = datetime()
WITH occurrence, parent_identity_lock
OPTIONAL MATCH (parent:SourceRecord {
  source_instance_id: $logical_parent_source_instance_id,
  source_record_id: $logical_parent_source_record_id,
  record_type: 'crm_deal'
})-[:FROM_SOURCE]->(:SourceSystem {source_key: $logical_parent_source_system})
WHERE parent.lifecycle_status IN ['active', 'pending_review']
WITH occurrence,
     [candidate IN collect(parent)
      WHERE candidate IS NOT NULL AND candidate.lifecycle_status = 'active'] AS active,
     [candidate IN collect(parent)
      WHERE candidate IS NOT NULL AND candidate.lifecycle_status = 'pending_review'] AS pending
WITH occurrence, active, pending,
     CASE
       WHEN size(active) = 1 THEN 'selected_active'
       WHEN size(active) > 1 THEN 'ambiguous'
       WHEN size(pending) = 1 THEN 'selected_pending_review'
       WHEN size(pending) > 1 THEN 'ambiguous'
       ELSE 'waiting'
     END AS recounted_state,
     CASE
       WHEN size(active) = 1 THEN active[0]
       WHEN size(active) = 0 AND size(pending) = 1 THEN pending[0]
       ELSE NULL
     END AS recounted_parent
WHERE $association_state IN [
    'selected_active', 'selected_pending_review', 'waiting', 'ambiguous', 'rejected'
  ]
  AND (
    ($association_state = 'rejected'
      AND $review_command_id IS NOT NULL
      AND $selected_parent_source_record_pk IS NULL)
    OR ($association_state <> 'rejected'
      AND $association_state = recounted_state
      AND coalesce($selected_parent_source_record_pk, '') =
          coalesce(recounted_parent.source_record_pk, ''))
  )
  AND $active_candidate_count = size(active)
  AND $pending_candidate_count = size(pending)
MERGE (decision:CrmHistoryParentAssociationDecision {decision_id: $decision_id})
ON CREATE SET decision.event_identity = occurrence.event_identity,
              decision.occurrence_id = occurrence.occurrence_id,
              decision.association_state = $association_state,
              decision.logical_parent_source_system = $logical_parent_source_system,
              decision.logical_parent_source_record_id =
                $logical_parent_source_record_id,
              decision.selected_parent_source_record_pk =
                $selected_parent_source_record_pk,
              decision.active_candidate_count = size(active),
              decision.pending_candidate_count = size(pending),
              decision.available_at = datetime($available_at),
              decision.recorded_at = datetime(),
              decision.review_command_id = $review_command_id
WITH occurrence, recounted_parent, decision
WHERE decision.event_identity = occurrence.event_identity
  AND decision.occurrence_id = occurrence.occurrence_id
  AND decision.association_state = $association_state
  AND decision.logical_parent_source_system = $logical_parent_source_system
  AND decision.logical_parent_source_record_id =
      $logical_parent_source_record_id
  AND coalesce(decision.selected_parent_source_record_pk, '') =
      coalesce($selected_parent_source_record_pk, '')
  AND decision.active_candidate_count = $active_candidate_count
  AND decision.pending_candidate_count = $pending_candidate_count
  AND decision.available_at = datetime($available_at)
  AND coalesce(decision.review_command_id, '') = coalesce($review_command_id, '')
MERGE (decision)-[:ASSOCIATION_FOR]->(occurrence)
SET occurrence.association_state = decision.association_state,
    occurrence.current_association_decision_id = decision.decision_id,
    occurrence.projection_updated_at = datetime()
FOREACH (_ IN CASE
  WHEN $association_state IN ['selected_active', 'selected_pending_review']
    AND recounted_parent IS NOT NULL
  THEN [1]
  ELSE []
END |
  MERGE (decision)-[:SELECTS_STAGE_HISTORY_PARENT]->(recounted_parent)
)
RETURN decision.decision_id AS decision_id,
       decision.association_state AS association_state,
       decision.active_candidate_count AS active_candidate_count,
       decision.pending_candidate_count AS pending_candidate_count
"""
)


PERSIST_STAGE_HISTORY_REVIEW_COMMAND = (
    _ACTIVE_STAGE_FENCE
    + """
  AND $review_kind IN [
  'resolve_parent', 'reject_parent', 'resolve_conflict', 'apply_correction'
]
MERGE (command:StageHistoryReviewCommand {command_id: $command_id})
ON CREATE SET command.review_kind = $review_kind,
              command.target_event_identity = $target_event_identity,
              command.target_occurrence_id = $target_occurrence_id,
              command.request_payload_digest = $request_payload_digest,
              command.reviewer_actor = $reviewer_actor,
              command.authorization_reference = $authorization_reference,
              command.expected_head_version = $expected_head_version,
              command.expected_authority_token = $expected_authority_token,
              command.expected_authority_state = $expected_authority_state,
              command.expected_variant_set_digest = $expected_variant_set_digest,
              command.retry_sequence = $retry_sequence,
              command.selected_variant_hash = $selected_variant_hash,
              command.selected_association_decision_id =
                $selected_association_decision_id,
              command.correction_of_decision_id = $correction_of_decision_id,
              command.available_at = datetime($available_at),
              command.status = 'pending',
              command.created_at = datetime(),
              command.updated_at = datetime()
WITH logical, command
WHERE command.review_kind = $review_kind
  AND command.target_event_identity = $target_event_identity
  AND coalesce(command.target_occurrence_id, '') =
      coalesce($target_occurrence_id, '')
  AND command.request_payload_digest = $request_payload_digest
  AND command.reviewer_actor = $reviewer_actor
  AND command.authorization_reference = $authorization_reference
  AND command.expected_head_version = $expected_head_version
  AND command.expected_authority_token = $expected_authority_token
  AND command.expected_authority_state = $expected_authority_state
  AND command.expected_variant_set_digest = $expected_variant_set_digest
  AND coalesce(command.retry_sequence, 0) = coalesce($retry_sequence, 0)
  AND coalesce(command.selected_variant_hash, '') =
      coalesce($selected_variant_hash, '')
  AND coalesce(command.selected_association_decision_id, '') =
      coalesce($selected_association_decision_id, '')
  AND coalesce(command.correction_of_decision_id, '') =
      coalesce($correction_of_decision_id, '')
  AND command.available_at = datetime($available_at)
  AND command.status IN ['pending', 'claimed', 'completed', 'failed', 'superseded']
MERGE (logical)-[:HAS_STAGE_HISTORY_REVIEW_COMMAND]->(command)
RETURN command.command_id AS command_id,
       command.status AS status,
       command.available_at AS available_at
"""
)


CLAIM_STAGE_HISTORY_REVIEW_COMMAND = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (command:StageHistoryReviewCommand {command_id: $command_id})
WHERE command.review_kind = $review_kind
  AND command.target_event_identity = $target_event_identity
  AND coalesce(command.target_occurrence_id, '') = coalesce($target_occurrence_id, '')
  AND command.request_payload_digest = $request_payload_digest
  AND command.reviewer_actor = $reviewer_actor
  AND command.authorization_reference = $authorization_reference
  AND command.expected_head_version = $expected_head_version
  AND command.expected_authority_token = $expected_authority_token
  AND command.expected_authority_state = $expected_authority_state
  AND command.expected_variant_set_digest = $expected_variant_set_digest
  AND coalesce(command.retry_sequence, 0) = coalesce($retry_sequence, 0)
  AND command.available_at = datetime($available_at)
  AND coalesce(command.selected_variant_hash, '') = coalesce($selected_variant_hash, '')
  AND coalesce(command.selected_association_decision_id, '') =
      coalesce($selected_association_decision_id, '')
  AND coalesce(command.correction_of_decision_id, '') =
      coalesce($correction_of_decision_id, '')
  AND ((command.status = 'pending')
   OR (command.status = 'claimed' AND command.lease_expires_at < datetime())
  )
SET command.status = 'claimed',
    command.lease_owner = $lease_owner,
    command.lease_attempt_id = $ingest_run_id,
    command.lease_attempt_generation = $attempt_generation,
    command.lease_stream_generation = $stream_generation,
    command.lease_fencing_token = $fencing_token,
    command.lease_expires_at = datetime($lease_expires_at),
    command.claim_count = coalesce(command.claim_count, 0) + 1,
    command.claimed_at = datetime(),
    command.updated_at = datetime()
RETURN command.command_id AS command_id,
       command.status AS status,
       command.claim_count AS claim_count
"""
)


LOCK_STAGE_HISTORY_REVIEW_EVENT = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (command:StageHistoryReviewCommand {
  command_id: $command_id,
  target_event_identity: $event_identity,
  status: 'claimed'
})
WHERE command.lease_owner = $lease_owner
  AND command.lease_attempt_id = $ingest_run_id
  AND command.lease_attempt_generation = $attempt_generation
  AND command.lease_stream_generation = $stream_generation
  AND command.lease_fencing_token = $fencing_token
  AND command.lease_expires_at >= datetime()
MERGE (identity_lock:StageHistoryIdentityLock {event_identity: $event_identity})
ON CREATE SET identity_lock.created_at = datetime()
SET identity_lock.lock_version = coalesce(identity_lock.lock_version, 0) + 1
RETURN identity_lock.lock_version AS lock_version
"""
)


COMPLETE_STAGE_HISTORY_REVIEW_COMMAND = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (command:StageHistoryReviewCommand {command_id: $command_id})
WHERE $completion_status IN ['completed', 'failed', 'superseded']
  AND (
    (command.status = 'claimed'
      AND command.lease_owner = $lease_owner
      AND command.lease_attempt_id = $ingest_run_id
      AND command.lease_attempt_generation = $attempt_generation
      AND command.lease_stream_generation = $stream_generation
      AND command.lease_fencing_token = $fencing_token
      AND command.lease_expires_at >= datetime())
    OR (command.status = $completion_status
      AND command.result_digest = $result_digest
      AND command.result_authority_decision_id = $result_authority_decision_id
      AND command.result_authority_state = $result_authority_state
      AND command.result_head_version = $result_head_version
      AND command.result_authority_token = $result_authority_token
      AND command.result_invalidation_count = $result_invalidation_count)
  )
SET command.status = $completion_status,
    command.result_digest = $result_digest,
    command.result_authority_decision_id = $result_authority_decision_id,
    command.result_authority_state = $result_authority_state,
    command.result_head_version = $result_head_version,
    command.result_authority_token = $result_authority_token,
    command.result_invalidation_count = $result_invalidation_count,
    command.completed_at = coalesce(command.completed_at, datetime()),
    command.updated_at = datetime(),
    command.lease_owner = NULL,
    command.lease_attempt_id = NULL,
    command.lease_attempt_generation = NULL,
    command.lease_stream_generation = NULL,
    command.lease_fencing_token = NULL,
    command.claimed_at = NULL,
    command.lease_expires_at = NULL
RETURN command.command_id AS command_id,
       command.status AS status,
       command.result_digest AS result_digest,
       command.result_authority_decision_id AS authority_decision_id,
       command.result_authority_state AS authority_state,
       command.result_head_version AS head_version,
       command.result_authority_token AS authority_token,
       command.result_invalidation_count AS invalidation_count
"""
)


UPSERT_STAGE_HISTORY_RETRY = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (occurrence:StageHistoryOccurrence {occurrence_id: $occurrence_id})
WHERE occurrence.logical_run_id = $logical_run_id
MERGE (retry:StageHistoryRetry {
  occurrence_id: $occurrence_id,
  retry_sequence: $retry_sequence
})
ON CREATE SET retry.status = 'pending',
              retry.retry_id = $retry_id,
              retry.reason_code = $reason_code,
              retry.max_attempts = $max_attempts,
              retry.review_command_id = $review_command_id,
              retry.attempt_count = 0,
              retry.next_attempt_at = datetime($next_attempt_at),
              retry.initial_next_attempt_at = datetime($next_attempt_at),
              retry.created_at = datetime(),
              retry.updated_at = datetime()
WITH occurrence, retry
WHERE retry.status IN ['pending', 'claimed', 'resolved', 'rejected', 'quarantined']
  AND retry.retry_id = $retry_id
  AND retry.reason_code = $reason_code
  AND retry.max_attempts = $max_attempts
  AND coalesce(retry.review_command_id, '') = coalesce($review_command_id, '')
  AND retry.initial_next_attempt_at = datetime($next_attempt_at)
MERGE (occurrence)-[:HAS_STAGE_HISTORY_RETRY]->(retry)
SET occurrence.retry_state = retry.status,
    occurrence.current_retry_sequence = retry.retry_sequence,
    occurrence.projection_updated_at = datetime()
RETURN retry.retry_sequence AS retry_sequence,
       retry.status AS status
"""
)


CLAIM_STAGE_HISTORY_RETRY = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (occurrence:StageHistoryOccurrence {occurrence_id: $occurrence_id})
MATCH (retry:StageHistoryRetry {
  occurrence_id: $occurrence_id,
  retry_sequence: $retry_sequence
})
WHERE EXISTS { MATCH (occurrence)-[:HAS_STAGE_HISTORY_RETRY]->(retry) }
  AND coalesce(retry.attempt_count, 0) < retry.max_attempts
  AND ((retry.status = 'pending' AND retry.next_attempt_at <= datetime())
   OR (retry.status = 'claimed' AND retry.lease_expires_at < datetime()))
SET retry.status = 'claimed',
    retry.lease_owner = $lease_owner,
    retry.lease_attempt_id = $ingest_run_id,
    retry.lease_attempt_generation = $attempt_generation,
    retry.lease_stream_generation = $stream_generation,
    retry.lease_fencing_token = $fencing_token,
    retry.lease_expires_at = datetime($lease_expires_at),
    retry.attempt_count = coalesce(retry.attempt_count, 0) + 1,
    retry.claimed_at = datetime(),
    retry.updated_at = datetime()
SET occurrence.retry_state = retry.status,
    occurrence.current_retry_sequence = retry.retry_sequence,
    occurrence.projection_updated_at = datetime()
RETURN retry.retry_sequence AS retry_sequence,
       retry.attempt_count AS attempt_count,
       retry.lease_expires_at AS lease_expires_at
"""
)


RESOLVE_STAGE_HISTORY_RETRY = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (occurrence:StageHistoryOccurrence {occurrence_id: $occurrence_id})
MATCH (retry:StageHistoryRetry {
  occurrence_id: $occurrence_id,
  retry_sequence: $retry_sequence,
  status: 'claimed',
  lease_owner: $lease_owner,
  lease_attempt_id: $ingest_run_id,
  lease_attempt_generation: $attempt_generation,
  lease_stream_generation: $stream_generation,
  lease_fencing_token: $fencing_token
})
WHERE EXISTS { MATCH (occurrence)-[:HAS_STAGE_HISTORY_RETRY]->(retry) }
  AND retry.lease_expires_at >= datetime()
  AND $resolution IN ['resolved', 'rejected', 'quarantined']
SET retry.status = $resolution,
    retry.resolution_decision_id = $resolution_decision_id,
    retry.resolved_at = datetime(),
    retry.updated_at = datetime(),
    retry.lease_owner = NULL,
    retry.lease_attempt_id = NULL,
    retry.lease_attempt_generation = NULL,
    retry.lease_stream_generation = NULL,
    retry.lease_fencing_token = NULL,
    retry.claimed_at = NULL,
    retry.lease_expires_at = NULL
SET occurrence.retry_state = retry.status,
    occurrence.current_retry_sequence = retry.retry_sequence,
    occurrence.projection_updated_at = datetime()
RETURN retry.retry_sequence AS retry_sequence,
       retry.status AS status
"""
)


# Compatibility alias; authority writes use the shared in-transaction ledger.
APPEND_STAGE_HISTORY_AUTHORITY_TRANSITION = APPEND_CRM_HISTORY_AUTHORITY_DECISION


PROJECT_STAGE_HISTORY_AUTHORITY_HEAD = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (head:CrmHistoryAuthorityHead {
  event_identity: $event_identity,
  decision_id: $authority_decision_id,
  head_version: $authority_head_version,
  authority_token: $authority_token,
  authority_state: $authority_state
})
MATCH (decision:CrmHistoryAuthorityDecision {
  decision_id: $authority_decision_id,
  head_version: $authority_head_version,
  authority_token: $authority_token,
  authority_state: $authority_state
})-[:DECIDES_FOR]->(:CrmHistoryConflictGroup {event_identity: $event_identity})
MATCH (occurrence:StageHistoryOccurrence {event_identity: $event_identity})
WHERE occurrence.parse_scope = 'in_scope'
  AND $authority_state IN [
    'effective', 'withheld_parent', 'withheld_conflict', 'rejected', 'corrected'
  ]
SET occurrence.authority_state = head.authority_state,
    occurrence.current_authority_decision_id = head.decision_id,
    occurrence.projection_updated_at = datetime()
RETURN count(occurrence) AS projected_occurrence_count
"""
)


APPEND_STAGE_HISTORY_INVALIDATION_INTENTS = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (decision:CrmHistoryAuthorityDecision {decision_id: $authority_decision_id})
WHERE decision.head_version = $authority_head_version
  AND decision.authority_token = $authority_token
  AND decision.expected_invalidation_target_count = $expected_intent_count
  AND decision.expected_invalidation_target_digests = $expected_target_digests
  AND $expected_intent_count = size($intents)
  AND $expected_intent_count = size($expected_target_digests)
  AND all(item IN $intents
          WHERE item.affected_parent_digest IN $expected_target_digests)
CALL (decision) {
  WITH decision
  UNWIND $intents AS item
  MERGE (intent:CrmHistoryInvalidationIntent {intent_id: item.intent_id})
  ON CREATE SET intent.authority_decision_id = $authority_decision_id,
                intent.target_kind = 'crm_stage_timeline',
                intent.affected_parent_digest = item.affected_parent_digest,
                intent.reason = item.reason,
                intent.sequence = $authority_head_version,
                intent.available_at = datetime(item.available_at),
                intent.status = 'pending',
                intent.payload_json = item.payload_json,
                intent.created_at = datetime(),
                intent.updated_at = datetime()
  WITH decision, item, intent
  WHERE intent.authority_decision_id = $authority_decision_id
    AND intent.target_kind = 'crm_stage_timeline'
    AND intent.affected_parent_digest = item.affected_parent_digest
    AND intent.reason = item.reason
    AND intent.sequence = $authority_head_version
    AND intent.available_at = datetime(item.available_at)
    AND intent.payload_json = item.payload_json
  MERGE (decision)-[:EMITS_INVALIDATION]->(intent)
  RETURN count(intent) AS intent_count,
         count(DISTINCT intent.intent_id) AS distinct_intent_count,
         count(DISTINCT intent.affected_parent_digest) AS distinct_target_count,
         collect(DISTINCT intent.affected_parent_digest) AS actual_target_digests
}
WITH decision, intent_count, distinct_intent_count, distinct_target_count,
     actual_target_digests
WHERE intent_count = $expected_intent_count
  AND distinct_intent_count = $expected_intent_count
  AND distinct_target_count = $expected_intent_count
  AND all(expected IN $expected_target_digests
          WHERE expected IN actual_target_digests)
RETURN intent_count,
       actual_target_digests
"""
)


UPSERT_STAGE_HISTORY_UNIT_ACCOUNTING = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (unit:StageHistoryUnit {
  unit_id: $unit_id,
  logical_run_id: $logical_run_id,
  status: 'persisting'
})
OPTIONAL MATCH (unit)-[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(
  occurrence:StageHistoryOccurrence
)
WITH unit,
     count(occurrence) AS actual_fetched_count,
     sum(CASE WHEN occurrence.terminal_disposition = 'malformed_excluded'
              THEN 1 ELSE 0 END) AS actual_malformed_excluded_count,
     sum(CASE WHEN occurrence.terminal_disposition = 'capture_rejected_valid'
              THEN 1 ELSE 0 END) AS actual_capture_rejected_valid_count,
     sum(CASE WHEN occurrence.terminal_disposition = 'excluded_out_of_scope'
              THEN 1 ELSE 0 END) AS actual_excluded_out_of_scope_count,
     sum(CASE WHEN occurrence.terminal_disposition = 'canonical_effective'
              THEN 1 ELSE 0 END) AS actual_canonical_effective_count,
     sum(CASE WHEN occurrence.terminal_disposition = 'canonical_pending_parent'
              THEN 1 ELSE 0 END) AS actual_canonical_pending_parent_count,
     sum(CASE WHEN occurrence.terminal_disposition = 'parent_waiting'
              THEN 1 ELSE 0 END) AS actual_parent_waiting_count,
     sum(CASE WHEN occurrence.terminal_disposition = 'parent_ambiguous'
              THEN 1 ELSE 0 END) AS actual_parent_ambiguous_count,
     sum(CASE WHEN occurrence.terminal_disposition = 'same_hash_replay'
              THEN 1 ELSE 0 END) AS actual_same_hash_replay_count,
     sum(CASE WHEN occurrence.terminal_disposition = 'differing_hash_conflict'
              THEN 1 ELSE 0 END) AS actual_differing_hash_conflict_count,
     sum(CASE WHEN occurrence.identity_hash_state = 'new_variant'
              THEN 1 ELSE 0 END) AS actual_new_variant_count,
     sum(CASE WHEN occurrence.identity_hash_state = 'existing_same_hash'
              THEN 1 ELSE 0 END) AS actual_existing_same_hash_count,
     sum(CASE WHEN occurrence.identity_hash_state = 'new_conflict_variant'
              THEN 1 ELSE 0 END) AS actual_new_conflict_variant_count,
     sum(CASE WHEN occurrence.association_state = 'selected_active'
              THEN 1 ELSE 0 END) AS actual_selected_active_count,
     sum(CASE WHEN occurrence.association_state = 'selected_pending_review'
              THEN 1 ELSE 0 END) AS actual_selected_pending_review_count,
     sum(CASE WHEN occurrence.association_state = 'waiting'
              THEN 1 ELSE 0 END) AS actual_waiting_count,
     sum(CASE WHEN occurrence.association_state = 'ambiguous'
              THEN 1 ELSE 0 END) AS actual_ambiguous_count,
     sum(CASE WHEN occurrence.association_state = 'rejected'
              THEN 1 ELSE 0 END) AS actual_association_rejected_count,
     sum(CASE WHEN occurrence.authority_state = 'effective'
              THEN 1 ELSE 0 END) AS actual_effective_count,
     sum(CASE WHEN occurrence.authority_state = 'withheld_parent'
              THEN 1 ELSE 0 END) AS actual_withheld_parent_count,
     sum(CASE WHEN occurrence.authority_state = 'withheld_conflict'
              THEN 1 ELSE 0 END) AS actual_withheld_conflict_count,
     sum(CASE WHEN occurrence.authority_state = 'rejected'
              THEN 1 ELSE 0 END) AS actual_authority_rejected_count,
     sum(CASE WHEN occurrence.authority_state = 'corrected'
              THEN 1 ELSE 0 END) AS actual_corrected_count,
     sum(CASE WHEN occurrence.retry_state = 'none'
              THEN 1 ELSE 0 END) AS actual_retry_none_count,
     sum(CASE WHEN occurrence.retry_state = 'pending'
              THEN 1 ELSE 0 END) AS actual_retry_pending_count,
     sum(CASE WHEN occurrence.retry_state = 'claimed'
              THEN 1 ELSE 0 END) AS actual_retry_claimed_count,
     sum(CASE WHEN occurrence.retry_state = 'resolved'
              THEN 1 ELSE 0 END) AS actual_retry_resolved_count,
     sum(CASE WHEN occurrence.retry_state = 'rejected'
              THEN 1 ELSE 0 END) AS actual_retry_rejected_count,
     sum(CASE WHEN occurrence.retry_state = 'quarantined'
              THEN 1 ELSE 0 END) AS actual_retry_quarantined_count,
     sum(CASE WHEN occurrence IS NOT NULL
                   AND occurrence.terminal_disposition IS NULL
              THEN 1 ELSE 0 END) AS nonterminal_count,
     sum(CASE
          WHEN occurrence IS NOT NULL
            AND NOT occurrence.terminal_disposition IN [
              'malformed_excluded',
              'capture_rejected_valid',
              'excluded_out_of_scope',
              'canonical_effective',
              'canonical_pending_parent',
              'parent_waiting',
              'parent_ambiguous',
              'same_hash_replay',
              'differing_hash_conflict'
            ]
          THEN 1 ELSE 0 END) AS unknown_disposition_count
WHERE $run_kind IN ['artifact_replay', 'failed_capture']
  AND $fetched_count >= 0
  AND $fetched_count <= 50
  AND all(value IN [
    $fetched_count,
    $malformed_excluded_count,
    $capture_rejected_valid_count,
    $excluded_out_of_scope_count,
    $canonical_effective_count,
    $canonical_pending_parent_count,
    $parent_waiting_count,
    $parent_ambiguous_count,
    $same_hash_replay_count,
    $differing_hash_conflict_count,
    $new_variant_count,
    $existing_same_hash_count,
    $new_conflict_variant_count,
    $selected_active_count, $selected_pending_review_count,
    $waiting_count, $ambiguous_count, $association_rejected_count,
    $effective_count, $withheld_parent_count, $withheld_conflict_count,
    $authority_rejected_count, $corrected_count,
    $retry_none_count, $retry_pending_count, $retry_claimed_count,
    $retry_resolved_count, $retry_rejected_count, $retry_quarantined_count
  ] WHERE value >= 0)
  AND nonterminal_count = 0
  AND unknown_disposition_count = 0
  AND actual_fetched_count = unit.fetched_count
  AND actual_fetched_count = $fetched_count
  AND actual_malformed_excluded_count = $malformed_excluded_count
  AND actual_capture_rejected_valid_count = $capture_rejected_valid_count
  AND actual_excluded_out_of_scope_count = $excluded_out_of_scope_count
  AND actual_canonical_effective_count = $canonical_effective_count
  AND actual_canonical_pending_parent_count = $canonical_pending_parent_count
  AND actual_parent_waiting_count = $parent_waiting_count
  AND actual_parent_ambiguous_count = $parent_ambiguous_count
  AND actual_same_hash_replay_count = $same_hash_replay_count
  AND actual_differing_hash_conflict_count = $differing_hash_conflict_count
  AND actual_new_variant_count = $new_variant_count
  AND actual_existing_same_hash_count = $existing_same_hash_count
  AND actual_new_conflict_variant_count = $new_conflict_variant_count
  AND actual_selected_active_count = $selected_active_count
  AND actual_selected_pending_review_count = $selected_pending_review_count
  AND actual_waiting_count = $waiting_count
  AND actual_ambiguous_count = $ambiguous_count
  AND actual_association_rejected_count = $association_rejected_count
  AND actual_effective_count = $effective_count
  AND actual_withheld_parent_count = $withheld_parent_count
  AND actual_withheld_conflict_count = $withheld_conflict_count
  AND actual_authority_rejected_count = $authority_rejected_count
  AND actual_corrected_count = $corrected_count
  AND actual_retry_none_count = $retry_none_count
  AND actual_retry_pending_count = $retry_pending_count
  AND actual_retry_claimed_count = $retry_claimed_count
  AND actual_retry_resolved_count = $retry_resolved_count
  AND actual_retry_rejected_count = $retry_rejected_count
  AND actual_retry_quarantined_count = $retry_quarantined_count
  AND $selected_active_count + $selected_pending_review_count +
      $waiting_count + $ambiguous_count + $association_rejected_count =
      $new_variant_count + $existing_same_hash_count + $new_conflict_variant_count
  AND $effective_count + $withheld_parent_count + $withheld_conflict_count +
      $authority_rejected_count + $corrected_count =
      $new_variant_count + $existing_same_hash_count + $new_conflict_variant_count
  AND $retry_none_count + $retry_pending_count + $retry_claimed_count +
      $retry_resolved_count + $retry_rejected_count + $retry_quarantined_count =
      $fetched_count
  AND $fetched_count =
    $malformed_excluded_count +
    $capture_rejected_valid_count +
    $excluded_out_of_scope_count +
    $canonical_effective_count +
    $canonical_pending_parent_count +
    $parent_waiting_count +
    $parent_ambiguous_count +
    $same_hash_replay_count +
    $differing_hash_conflict_count
  AND (
    ($run_kind = 'failed_capture'
      AND $fetched_count =
        $malformed_excluded_count + $capture_rejected_valid_count
      AND $new_variant_count + $existing_same_hash_count +
        $new_conflict_variant_count = 0)
    OR ($run_kind = 'artifact_replay'
      AND $malformed_excluded_count = 0
      AND $capture_rejected_valid_count = 0
      AND $new_variant_count + $existing_same_hash_count +
        $new_conflict_variant_count =
          $fetched_count - $excluded_out_of_scope_count
      AND $existing_same_hash_count = $same_hash_replay_count
      AND $new_conflict_variant_count = $differing_hash_conflict_count)
  )
MERGE (accounting:StageHistoryUnitAccounting {unit_id: $unit_id})
ON CREATE SET accounting.logical_run_id = $logical_run_id,
              accounting.run_kind = $run_kind,
              accounting.fetched_count = $fetched_count,
              accounting.malformed_excluded_count = $malformed_excluded_count,
              accounting.capture_rejected_valid_count = $capture_rejected_valid_count,
              accounting.excluded_out_of_scope_count = $excluded_out_of_scope_count,
              accounting.canonical_effective_count = $canonical_effective_count,
              accounting.canonical_pending_parent_count =
                $canonical_pending_parent_count,
              accounting.parent_waiting_count = $parent_waiting_count,
              accounting.parent_ambiguous_count = $parent_ambiguous_count,
              accounting.same_hash_replay_count = $same_hash_replay_count,
              accounting.differing_hash_conflict_count =
                $differing_hash_conflict_count,
              accounting.new_variant_count = $new_variant_count,
              accounting.existing_same_hash_count = $existing_same_hash_count,
              accounting.new_conflict_variant_count = $new_conflict_variant_count,
              accounting.selected_active_count = $selected_active_count,
              accounting.selected_pending_review_count = $selected_pending_review_count,
              accounting.waiting_count = $waiting_count,
              accounting.ambiguous_count = $ambiguous_count,
              accounting.association_rejected_count = $association_rejected_count,
              accounting.effective_count = $effective_count,
              accounting.withheld_parent_count = $withheld_parent_count,
              accounting.withheld_conflict_count = $withheld_conflict_count,
              accounting.authority_rejected_count = $authority_rejected_count,
              accounting.corrected_count = $corrected_count,
              accounting.retry_none_count = $retry_none_count,
              accounting.retry_pending_count = $retry_pending_count,
              accounting.retry_claimed_count = $retry_claimed_count,
              accounting.retry_resolved_count = $retry_resolved_count,
              accounting.retry_rejected_count = $retry_rejected_count,
              accounting.retry_quarantined_count = $retry_quarantined_count,
              accounting.created_at = datetime()
WITH unit, accounting
WHERE accounting.logical_run_id = $logical_run_id
  AND accounting.run_kind = $run_kind
  AND accounting.fetched_count = $fetched_count
  AND accounting.malformed_excluded_count = $malformed_excluded_count
  AND accounting.capture_rejected_valid_count = $capture_rejected_valid_count
  AND accounting.excluded_out_of_scope_count = $excluded_out_of_scope_count
  AND accounting.canonical_effective_count = $canonical_effective_count
  AND accounting.canonical_pending_parent_count = $canonical_pending_parent_count
  AND accounting.parent_waiting_count = $parent_waiting_count
  AND accounting.parent_ambiguous_count = $parent_ambiguous_count
  AND accounting.same_hash_replay_count = $same_hash_replay_count
  AND accounting.differing_hash_conflict_count = $differing_hash_conflict_count
  AND accounting.new_variant_count = $new_variant_count
  AND accounting.existing_same_hash_count = $existing_same_hash_count
  AND accounting.new_conflict_variant_count = $new_conflict_variant_count
  AND accounting.selected_active_count = $selected_active_count
  AND accounting.selected_pending_review_count = $selected_pending_review_count
  AND accounting.waiting_count = $waiting_count
  AND accounting.ambiguous_count = $ambiguous_count
  AND accounting.association_rejected_count = $association_rejected_count
  AND accounting.effective_count = $effective_count
  AND accounting.withheld_parent_count = $withheld_parent_count
  AND accounting.withheld_conflict_count = $withheld_conflict_count
  AND accounting.authority_rejected_count = $authority_rejected_count
  AND accounting.corrected_count = $corrected_count
  AND accounting.retry_none_count = $retry_none_count
  AND accounting.retry_pending_count = $retry_pending_count
  AND accounting.retry_claimed_count = $retry_claimed_count
  AND accounting.retry_resolved_count = $retry_resolved_count
  AND accounting.retry_rejected_count = $retry_rejected_count
  AND accounting.retry_quarantined_count = $retry_quarantined_count
MERGE (unit)-[:HAS_STAGE_HISTORY_ACCOUNTING]->(accounting)
RETURN accounting.unit_id AS unit_id,
       accounting.fetched_count AS terminal_count,
       accounting.new_variant_count + accounting.existing_same_hash_count +
         accounting.new_conflict_variant_count AS identity_hash_count
"""
)


COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (checkpoint:IngestionCheckpoint {
  logical_run_id: $logical_run_id,
  phase: $phase,
  generation: $attempt_generation,
  status: 'active'
})
MATCH (unit:StageHistoryUnit {
  unit_id: $unit_id,
  logical_run_id: $logical_run_id,
  status: 'persisting'
})-[:HAS_STAGE_HISTORY_ACCOUNTING]->(
  accounting:StageHistoryUnitAccounting {unit_id: $unit_id}
)
WHERE unit.unit_digest = $unit_digest
  AND unit.page_sequence = $page_sequence
  AND unit.page_sequence = coalesce($expected_last_page_sequence, 0) + 1
  AND coalesce(unit.expected_last_page_sequence, 0) =
      coalesce($expected_last_page_sequence, 0)
  AND unit.expected_cursor_json = $expected_cursor_json
  AND unit.expected_checkpoint_revision = $expected_checkpoint_revision
  AND unit.replay_boundary = $replay_boundary
  AND checkpoint.connector_version = $connector_version
  AND checkpoint.schema_version = $checkpoint_schema_version
  AND checkpoint.replay_boundary = $replay_boundary
  AND $replay_boundary = 'exclusive_artifact_page_sequence'
  AND checkpoint.source_window_json = $source_window_json
  AND checkpoint.cursor_json = $expected_cursor_json
  AND $next_cursor_json <> $expected_cursor_json
  AND coalesce(checkpoint.revision, 0) = $expected_checkpoint_revision
  AND $expected_checkpoint_revision = coalesce($expected_last_page_sequence, 0)
  AND coalesce(checkpoint.committed_count, 0) = $expected_committed_count
  AND coalesce(checkpoint.duplicate_count, 0) = $expected_duplicate_count
  AND coalesce(checkpoint.excluded_count, 0) = $expected_excluded_count
  AND coalesce(checkpoint.retry_count, 0) = $expected_retry_count
  AND all(value IN [
    $committed_delta, $duplicate_delta, $excluded_delta, $retry_delta,
    $next_committed_count, $next_duplicate_count,
    $next_excluded_count, $next_retry_count
  ] WHERE value >= 0)
  AND $committed_delta =
    accounting.canonical_effective_count +
    accounting.canonical_pending_parent_count +
    accounting.parent_waiting_count +
    accounting.parent_ambiguous_count +
    accounting.differing_hash_conflict_count
  AND $duplicate_delta = accounting.same_hash_replay_count
  AND $excluded_delta =
    accounting.malformed_excluded_count +
    accounting.capture_rejected_valid_count +
    accounting.excluded_out_of_scope_count
  AND $retry_delta =
    accounting.canonical_pending_parent_count +
    accounting.parent_waiting_count +
    accounting.parent_ambiguous_count
  AND $next_committed_count = $expected_committed_count + $committed_delta
  AND $next_duplicate_count = $expected_duplicate_count + $duplicate_delta
  AND $next_excluded_count = $expected_excluded_count + $excluded_delta
  AND $next_retry_count = $expected_retry_count + $retry_delta
CALL (unit) {
  MATCH (unit)-[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(
    occurrence:StageHistoryOccurrence
  )
  RETURN count(occurrence) AS occurrence_count,
         count(DISTINCT occurrence.occurrence_id) AS distinct_occurrence_count,
         count(CASE WHEN occurrence.terminal_disposition IS NOT NULL THEN 1 END)
           AS terminal_count
}
WITH checkpoint, logical, unit, accounting, occurrence_count,
     distinct_occurrence_count, terminal_count
WHERE occurrence_count = unit.fetched_count
  AND distinct_occurrence_count = unit.fetched_count
  AND terminal_count = unit.fetched_count
  AND accounting.fetched_count = unit.fetched_count
SET unit.status = 'committed',
    unit.next_cursor_json = $next_cursor_json,
    unit.next_checkpoint_revision = $expected_checkpoint_revision + 1,
    unit.committed_delta = $committed_delta,
    unit.duplicate_delta = $duplicate_delta,
    unit.excluded_delta = $excluded_delta,
    unit.retry_delta = $retry_delta,
    unit.committed_at = datetime(),
    checkpoint.cursor_json = $next_cursor_json,
    checkpoint.revision = $expected_checkpoint_revision + 1,
    checkpoint.last_page_sequence = $page_sequence,
    checkpoint.last_committed_record_id = $unit_id,
    checkpoint.committed_count = $next_committed_count,
    checkpoint.duplicate_count = $next_duplicate_count,
    checkpoint.excluded_count = $next_excluded_count,
    checkpoint.retry_count = $next_retry_count,
    checkpoint.updated_at = datetime(),
    logical.current_phase = $phase,
    logical.committed_count = $next_committed_count,
    logical.duplicate_count = $next_duplicate_count,
    logical.excluded_count = $next_excluded_count,
    logical.retry_count = $next_retry_count,
    logical.updated_at = datetime()
RETURN unit.unit_id AS unit_id,
       unit.status AS status,
       unit.page_sequence AS page_sequence,
       checkpoint.cursor_json AS cursor_json,
       checkpoint.revision AS revision,
       logical.stop_requested_at IS NOT NULL AS stop_requested
"""
)


GET_STAGE_HISTORY_STATUS = """
MATCH (logical:IngestionLogicalRun {logical_run_id: $logical_run_id})
WHERE logical.source_key = 'bitrix_chat'
  AND logical.mode IN [
    'bounded_smoke_replay', 'authoritative_backfill_replay',
    'authoritative_catch_up_replay', 'capture_failure_accounting',
    'parent_reconcile', 'conflict_review', 'correction_review'
  ]
OPTIONAL MATCH (logical)-[:HAS_ATTEMPT]->(attempt:IngestRun)
WHERE attempt.generation = logical.active_generation
OPTIONAL MATCH (stream:BitrixIngestionStream {
  source_key: logical.source_key,
  stream_key: 'crm_stage_history',
  logical_run_id: logical.logical_run_id
})
OPTIONAL MATCH (checkpoint:IngestionCheckpoint {
  logical_run_id: logical.logical_run_id,
  phase: logical.current_phase
})
OPTIONAL MATCH (logical)-[:HAS_STAGE_HISTORY_UNIT]->(unit:StageHistoryUnit)
OPTIONAL MATCH (unit)-[:HAS_STAGE_HISTORY_ACCOUNTING]->(
  accounting:StageHistoryUnitAccounting
)
RETURN logical.logical_run_id AS logical_run_id,
       logical.mode AS run_type,
       logical.status AS logical_status,
       attempt.ingest_run_id AS ingest_run_id,
       attempt.status AS attempt_status,
       stream.status AS stream_status,
       stream.stream_generation AS stream_generation,
       checkpoint.phase AS phase,
       checkpoint.revision AS checkpoint_revision,
       checkpoint.last_page_sequence AS checkpoint_last_page_sequence,
       count(DISTINCT unit) AS unit_count,
       count(DISTINCT CASE WHEN unit.status = 'committed' THEN unit END)
         AS committed_unit_count,
       coalesce(sum(accounting.fetched_count), 0) AS fetched_count
"""


GET_STAGE_HISTORY_RECONCILIATION = """
MATCH (logical:IngestionLogicalRun {logical_run_id: $logical_run_id})
WHERE logical.source_key = 'bitrix_chat'
  AND logical.mode IN [
    'bounded_smoke_replay', 'authoritative_backfill_replay',
    'authoritative_catch_up_replay', 'capture_failure_accounting'
  ]
CALL (logical) {
  OPTIONAL MATCH (logical)-[:HAS_STAGE_HISTORY_UNIT]->(unit:StageHistoryUnit)
  OPTIONAL MATCH (unit)-[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(
    occurrence:StageHistoryOccurrence
  )
  OPTIONAL MATCH (unit)-[:HAS_STAGE_HISTORY_ACCOUNTING]->(
    accounting:StageHistoryUnitAccounting
  )
  WITH unit, accounting,
       count(DISTINCT occurrence) AS occurrence_count,
       count(DISTINCT CASE WHEN occurrence.terminal_disposition IS NOT NULL
                           THEN occurrence END) AS terminal_occurrence_count,
       count(CASE WHEN occurrence.terminal_disposition = 'malformed_excluded'
                  THEN 1 END) AS malformed_excluded_count,
       count(CASE WHEN occurrence.terminal_disposition = 'capture_rejected_valid'
                  THEN 1 END) AS capture_rejected_valid_count,
       count(CASE WHEN occurrence.terminal_disposition = 'excluded_out_of_scope'
                  THEN 1 END) AS excluded_out_of_scope_count,
       count(CASE WHEN occurrence.terminal_disposition = 'canonical_effective'
                  THEN 1 END) AS canonical_effective_count,
       count(CASE WHEN occurrence.terminal_disposition = 'canonical_pending_parent'
                  THEN 1 END) AS canonical_pending_parent_count,
       count(CASE WHEN occurrence.terminal_disposition = 'parent_waiting'
                  THEN 1 END) AS parent_waiting_count,
       count(CASE WHEN occurrence.terminal_disposition = 'parent_ambiguous'
                  THEN 1 END) AS parent_ambiguous_count,
       count(CASE WHEN occurrence.terminal_disposition = 'same_hash_replay'
                  THEN 1 END) AS same_hash_replay_count,
       count(CASE WHEN occurrence.terminal_disposition = 'differing_hash_conflict'
                  THEN 1 END) AS differing_hash_conflict_count,
       count(CASE WHEN occurrence.identity_hash_state = 'new_variant' THEN 1 END)
         AS new_variant_count,
       count(CASE WHEN occurrence.identity_hash_state = 'existing_same_hash' THEN 1 END)
         AS existing_same_hash_count,
       count(CASE WHEN occurrence.identity_hash_state = 'new_conflict_variant' THEN 1 END)
         AS new_conflict_variant_count,
       count(CASE WHEN occurrence.association_state = 'selected_active' THEN 1 END)
         AS selected_active_count,
       count(CASE WHEN occurrence.association_state = 'selected_pending_review' THEN 1 END)
         AS selected_pending_review_count,
       count(CASE WHEN occurrence.association_state = 'waiting' THEN 1 END)
         AS waiting_count,
       count(CASE WHEN occurrence.association_state = 'ambiguous' THEN 1 END)
         AS ambiguous_count,
       count(CASE WHEN occurrence.association_state = 'rejected' THEN 1 END)
         AS association_rejected_count,
       count(CASE WHEN occurrence.authority_state = 'effective' THEN 1 END)
         AS effective_count,
       count(CASE WHEN occurrence.authority_state = 'withheld_parent' THEN 1 END)
         AS withheld_parent_count,
       count(CASE WHEN occurrence.authority_state = 'withheld_conflict' THEN 1 END)
         AS withheld_conflict_count,
       count(CASE WHEN occurrence.authority_state = 'rejected' THEN 1 END)
         AS authority_rejected_count,
       count(CASE WHEN occurrence.authority_state = 'corrected' THEN 1 END)
         AS corrected_count,
       count(CASE WHEN occurrence.retry_state = 'none' THEN 1 END) AS retry_none_count,
       count(CASE WHEN occurrence.retry_state = 'pending' THEN 1 END)
         AS retry_pending_count,
       count(CASE WHEN occurrence.retry_state = 'claimed' THEN 1 END)
         AS retry_claimed_count,
       count(CASE WHEN occurrence.retry_state = 'resolved' THEN 1 END)
         AS retry_resolved_count,
       count(CASE WHEN occurrence.retry_state = 'rejected' THEN 1 END)
         AS retry_rejected_count,
       count(CASE WHEN occurrence.retry_state = 'quarantined' THEN 1 END)
         AS retry_quarantined_count
  ORDER BY unit.page_sequence
  RETURN [item IN collect(CASE WHEN unit IS NULL THEN NULL ELSE {
    unit_id: unit.unit_id,
    page_sequence: unit.page_sequence,
    unit_status: unit.status,
    fetched_count: accounting.fetched_count,
    occurrence_count: occurrence_count,
    terminal_occurrence_count: terminal_occurrence_count,
    balanced: accounting IS NOT NULL
      AND occurrence_count = accounting.fetched_count
      AND terminal_occurrence_count = accounting.fetched_count
      AND malformed_excluded_count = accounting.malformed_excluded_count
      AND capture_rejected_valid_count = accounting.capture_rejected_valid_count
      AND excluded_out_of_scope_count = accounting.excluded_out_of_scope_count
      AND canonical_effective_count = accounting.canonical_effective_count
      AND canonical_pending_parent_count = accounting.canonical_pending_parent_count
      AND parent_waiting_count = accounting.parent_waiting_count
      AND parent_ambiguous_count = accounting.parent_ambiguous_count
      AND same_hash_replay_count = accounting.same_hash_replay_count
      AND differing_hash_conflict_count = accounting.differing_hash_conflict_count
      AND new_variant_count = accounting.new_variant_count
      AND existing_same_hash_count = accounting.existing_same_hash_count
      AND new_conflict_variant_count = accounting.new_conflict_variant_count
  } END) WHERE item IS NOT NULL] AS units
}
CALL (logical) {
  OPTIONAL MATCH (logical)-[:HAS_STAGE_HISTORY_UNIT]->(:StageHistoryUnit)
        -[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(:StageHistoryOccurrence)
        -[:OBSERVED_STAGE_HISTORY_VARIANT]->(variant:CrmHistoryHashVariant)
  OPTIONAL MATCH (variant)-[:EVIDENCED_BY]->(
    record:SourceRecord {record_type: 'crm_history', history_family: 'stage'}
  )
  WITH variant, count(DISTINCT record) AS evidence_count
  RETURN count(DISTINCT variant) AS variant_count,
         coalesce(sum(evidence_count), 0) AS source_record_count,
         count(CASE WHEN variant IS NOT NULL AND evidence_count <> 1 THEN 1 END)
           AS invalid_variant_evidence_count
}
CALL (logical) {
  OPTIONAL MATCH (logical)-[:HAS_STAGE_HISTORY_UNIT]->(:StageHistoryUnit)
        -[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(:StageHistoryOccurrence)
        -[:OBSERVED_STAGE_HISTORY_VARIANT]->(variant:CrmHistoryHashVariant)
  OPTIONAL MATCH (variant)-[:EVIDENCED_BY]->(record:SourceRecord)
  WITH record, count(DISTINCT variant) AS variant_evidence_count
  RETURN count(CASE WHEN record IS NOT NULL AND variant_evidence_count <> 1 THEN 1 END)
    AS shared_variant_evidence_count
}
CALL (logical) {
  OPTIONAL MATCH (logical)-[:HAS_STAGE_HISTORY_UNIT]->(:StageHistoryUnit)
        -[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(occurrence:StageHistoryOccurrence)
  OPTIONAL MATCH (occurrence)-[:OBSERVED_STAGE_HISTORY_VARIANT]->(
    variant:CrmHistoryHashVariant
  )
  WITH occurrence, collect(DISTINCT variant) AS observed_variants
  RETURN count(DISTINCT CASE WHEN occurrence.identity_hash_state IS NOT NULL
    THEN [occurrence.event_identity, occurrence.canonical_hash] END)
      AS occurrence_variant_identity_count,
    count(CASE WHEN occurrence.identity_hash_state IS NOT NULL AND (
      size(observed_variants) <> 1
      OR observed_variants[0].event_identity <> occurrence.event_identity
      OR observed_variants[0].canonical_hash <> occurrence.canonical_hash
    ) THEN 1 END) AS invalid_occurrence_variant_link_count,
    count(CASE WHEN occurrence.identity_hash_state IS NULL AND
      size(observed_variants) <> 0 THEN 1 END)
      AS invalid_empty_occurrence_variant_link_count
}
CALL (logical) {
  OPTIONAL MATCH (logical)-[:HAS_STAGE_HISTORY_UNIT]->(:StageHistoryUnit)
        -[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(occurrence:StageHistoryOccurrence)
  OPTIONAL MATCH (decision:CrmHistoryParentAssociationDecision)
        -[:ASSOCIATION_FOR]->(occurrence)
  OPTIONAL MATCH (decision)-[:SELECTS_STAGE_HISTORY_PARENT]->(parent:SourceRecord)
  WITH decision, collect(DISTINCT parent) AS selected_parents
  WITH decision, selected_parents,
       CASE
         WHEN decision IS NULL THEN false
         WHEN decision.association_state IN [
           'selected_active', 'selected_pending_review'
         ] THEN size(selected_parents) <> 1
           OR coalesce(selected_parents[0].source_record_pk, '') <>
              coalesce(decision.selected_parent_source_record_pk, '')
         ELSE size(selected_parents) <> 0
           OR decision.selected_parent_source_record_pk IS NOT NULL
       END AS invalid_parent_association
  RETURN count(CASE WHEN invalid_parent_association THEN 1 END)
    AS invalid_parent_association_count
}
CALL (logical) {
  OPTIONAL MATCH (logical)-[:HAS_STAGE_HISTORY_UNIT]->(:StageHistoryUnit)
        -[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(occurrence:StageHistoryOccurrence)
  WITH collect(DISTINCT occurrence.event_identity) AS event_identities
  OPTIONAL MATCH (head:CrmHistoryAuthorityHead)
  WHERE head.event_identity IN event_identities
  OPTIONAL MATCH (decision:CrmHistoryAuthorityDecision {decision_id: head.decision_id})
  OPTIONAL MATCH (decision)-[:DECIDES_FOR]->(group:CrmHistoryConflictGroup)
  OPTIONAL MATCH (decision)-[:SELECTS_VARIANT]->(selected:CrmHistoryHashVariant)
  OPTIONAL MATCH (decision)-[:USES_PARENT_ASSOCIATION]->(
    association:CrmHistoryParentAssociationDecision
  )
  OPTIONAL MATCH (association)-[:SELECTS_STAGE_HISTORY_PARENT]->(parent:SourceRecord)
  WITH head, decision, group, association,
       collect(DISTINCT selected) AS selected_variants,
       collect(DISTINCT parent) AS selected_parents
  WITH head, decision, group, association, selected_variants, selected_parents,
       CASE WHEN size(selected_variants) = 1 THEN selected_variants[0] ELSE NULL END
         AS selected_variant,
       CASE WHEN size(selected_parents) = 1 THEN selected_parents[0] ELSE NULL END
         AS selected_parent
  WITH head, decision, group, association, selected_variants, selected_parents,
       selected_variant, selected_parent,
       head IS NOT NULL AND (
         decision IS NULL
         OR group IS NULL
         OR coalesce(group.event_identity, '') <> coalesce(head.event_identity, '')
         OR decision.head_version <> head.head_version
         OR coalesce(decision.authority_token, decision.fence_token) <>
             coalesce(head.authority_token, head.fence_token)
         OR coalesce(decision.authority_state, '') <>
             coalesce(head.authority_state, '')
         OR size(selected_variants) <> 1
         OR coalesce(selected_variant.event_identity, '') <>
             coalesce(head.event_identity, '')
       ) AS base_invalid
  WITH head, base_invalid OR CASE
    WHEN head.authority_state IN ['effective', 'corrected'] THEN (
      coalesce(selected_variant.canonical_hash, '') <>
        coalesce(head.selected_variant_hash, '')
      OR association IS NULL
      OR coalesce(association.decision_id, '') <>
          coalesce(head.association_decision_id, '')
      OR coalesce(association.event_identity, '') <>
          coalesce(head.event_identity, '')
      OR association.association_state <> 'selected_active'
      OR decision.logical_parent_source_system <>
          association.logical_parent_source_system
      OR decision.logical_parent_source_record_id <>
          association.logical_parent_source_record_id
      OR size(selected_parents) <> 1
      OR selected_parent.source_record_pk <>
          association.selected_parent_source_record_pk
      OR selected_parent.source_record_id <>
          association.logical_parent_source_record_id
      OR selected_parent.record_type <> 'crm_deal'
      OR selected_parent.lifecycle_status <> 'active'
      OR NOT EXISTS {
        MATCH (selected_parent)-[:FROM_SOURCE]->(:SourceSystem {
          source_key: association.logical_parent_source_system
        })
      }
    )
    ELSE head.selected_variant_hash IS NOT NULL
      OR head.association_decision_id IS NOT NULL
  END AS invalid_head
  RETURN count(CASE WHEN invalid_head THEN 1 END) AS invalid_authority_head_count,
         count(CASE WHEN invalid_head
                     AND head.authority_state IN ['effective', 'corrected']
                    THEN 1 END) AS invalid_effective_head_count
}
CALL (logical) {
  OPTIONAL MATCH (logical)-[:HAS_STAGE_HISTORY_UNIT]->(:StageHistoryUnit)
        -[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(occurrence:StageHistoryOccurrence)
  WITH collect(DISTINCT occurrence.event_identity) AS event_identities
  OPTIONAL MATCH (group:CrmHistoryConflictGroup)<-[:DECIDES_FOR]-(
    decision:CrmHistoryAuthorityDecision
  )
  WHERE group.event_identity IN event_identities
  OPTIONAL MATCH (decision)-[:EMITS_INVALIDATION]->(
    intent:CrmHistoryInvalidationIntent
  )
  WITH decision,
       count(DISTINCT intent) AS actual_intent_count,
       count(DISTINCT intent.affected_parent_digest) AS actual_target_count,
       collect(DISTINCT intent.affected_parent_digest) AS actual_target_digests
  RETURN count(CASE WHEN decision IS NOT NULL AND (
    actual_intent_count <> coalesce(decision.expected_invalidation_target_count, 0)
    OR actual_target_count <> coalesce(decision.expected_invalidation_target_count, 0)
    OR any(expected IN coalesce(decision.expected_invalidation_target_digests, [])
           WHERE NOT expected IN actual_target_digests)
  ) THEN 1 END) AS invalid_invalidation_transition_count,
  sum(actual_intent_count) AS invalidation_intent_count
}
CALL (logical) {
  OPTIONAL MATCH (logical)-[:HAS_STAGE_HISTORY_UNIT]->(committed:StageHistoryUnit {
    status: 'committed'
  })
  WITH committed
  ORDER BY committed.page_sequence
  RETURN [unit IN collect(committed) WHERE unit IS NOT NULL |
    unit.page_sequence] AS committed_page_sequences,
    [unit IN collect(committed) WHERE unit IS NOT NULL |
      unit.unit_id] AS committed_unit_ids,
    [unit IN collect(committed) WHERE unit IS NOT NULL |
      unit.unit_digest] AS committed_unit_digests
}
CALL (logical) {
  OPTIONAL MATCH (logical)-[:HAS_STAGE_HISTORY_UNIT]->(unit:StageHistoryUnit)
  OPTIONAL MATCH (unit)-[:HAS_STAGE_HISTORY_ACCOUNTING]->(
    accounting:StageHistoryUnitAccounting
  )
  RETURN count(DISTINCT CASE WHEN unit.status <> 'committed' THEN unit END)
           AS nonterminal_unit_count,
         coalesce(sum(accounting.fetched_count), 0) AS total_fetched_count,
         coalesce(sum(accounting.malformed_excluded_count), 0)
           AS total_malformed_excluded_count,
         coalesce(sum(accounting.capture_rejected_valid_count), 0)
           AS total_capture_rejected_valid_count,
         coalesce(sum(accounting.excluded_out_of_scope_count), 0)
           AS total_excluded_out_of_scope_count,
         coalesce(sum(accounting.canonical_effective_count), 0)
           AS total_canonical_effective_count,
         coalesce(sum(accounting.canonical_pending_parent_count), 0)
           AS total_canonical_pending_parent_count,
         coalesce(sum(accounting.parent_waiting_count), 0)
           AS total_parent_waiting_count,
         coalesce(sum(accounting.parent_ambiguous_count), 0)
           AS total_parent_ambiguous_count,
         coalesce(sum(accounting.same_hash_replay_count), 0)
           AS total_same_hash_replay_count,
         coalesce(sum(accounting.differing_hash_conflict_count), 0)
           AS total_differing_hash_conflict_count,
         coalesce(sum(accounting.new_variant_count), 0) AS total_new_variant_count,
         coalesce(sum(accounting.existing_same_hash_count), 0)
           AS total_existing_same_hash_count,
         coalesce(sum(accounting.new_conflict_variant_count), 0)
           AS total_new_conflict_variant_count
}
CALL (logical) {
  OPTIONAL MATCH (logical)-[:HAS_STAGE_HISTORY_UNIT]->(:StageHistoryUnit)
        -[:CONTAINS_STAGE_HISTORY_OCCURRENCE]->(occurrence:StageHistoryOccurrence)
  OPTIONAL MATCH (association:CrmHistoryParentAssociationDecision {
    decision_id: occurrence.current_association_decision_id
  })-[:ASSOCIATION_FOR]->(occurrence)
  OPTIONAL MATCH (head:CrmHistoryAuthorityHead {event_identity: occurrence.event_identity})
  OPTIONAL MATCH (occurrence)-[:HAS_STAGE_HISTORY_RETRY]->(retry:StageHistoryRetry)
  WHERE retry.retry_sequence = occurrence.current_retry_sequence
  RETURN count(CASE WHEN occurrence.association_state = 'selected_active' THEN 1 END)
           AS total_selected_active_count,
         count(CASE WHEN occurrence.association_state = 'selected_pending_review' THEN 1 END)
           AS total_selected_pending_review_count,
         count(CASE WHEN occurrence.association_state = 'waiting' THEN 1 END)
           AS total_waiting_count,
         count(CASE WHEN occurrence.association_state = 'ambiguous' THEN 1 END)
           AS total_ambiguous_count,
         count(CASE WHEN occurrence.association_state = 'rejected' THEN 1 END)
           AS total_association_rejected_count,
         count(CASE WHEN occurrence.authority_state = 'effective' THEN 1 END)
           AS total_effective_count,
         count(CASE WHEN occurrence.authority_state = 'withheld_parent' THEN 1 END)
           AS total_withheld_parent_count,
         count(CASE WHEN occurrence.authority_state = 'withheld_conflict' THEN 1 END)
           AS total_withheld_conflict_count,
         count(CASE WHEN occurrence.authority_state = 'rejected' THEN 1 END)
           AS total_authority_rejected_count,
         count(CASE WHEN occurrence.authority_state = 'corrected' THEN 1 END)
           AS total_corrected_count,
         count(CASE WHEN occurrence.retry_state = 'none' THEN 1 END)
           AS total_retry_none_count,
         count(CASE WHEN occurrence.retry_state = 'pending' THEN 1 END)
           AS total_retry_pending_count,
         count(CASE WHEN occurrence.retry_state = 'claimed' THEN 1 END)
           AS total_retry_claimed_count,
         count(CASE WHEN occurrence.retry_state = 'resolved' THEN 1 END)
           AS total_retry_resolved_count,
         count(CASE WHEN occurrence.retry_state = 'rejected' THEN 1 END)
           AS total_retry_rejected_count,
         count(CASE WHEN occurrence.retry_state = 'quarantined' THEN 1 END)
           AS total_retry_quarantined_count,
         count(CASE WHEN occurrence.identity_hash_state IS NOT NULL AND (
           association IS NULL
           OR association.occurrence_id <> occurrence.occurrence_id
           OR association.association_state <> occurrence.association_state
         ) THEN 1 END) AS invalid_current_association_projection_count,
         count(CASE WHEN occurrence.identity_hash_state IS NULL AND (
           occurrence.association_state IS NOT NULL
           OR occurrence.current_association_decision_id IS NOT NULL
         ) THEN 1 END) AS invalid_empty_association_projection_count,
         count(CASE WHEN occurrence.identity_hash_state IS NOT NULL AND (
           head IS NULL
           OR head.decision_id <> occurrence.current_authority_decision_id
           OR head.authority_state <> occurrence.authority_state
         ) THEN 1 END) AS invalid_current_authority_projection_count,
         count(CASE WHEN occurrence.identity_hash_state IS NULL AND (
           occurrence.authority_state IS NOT NULL
           OR occurrence.current_authority_decision_id IS NOT NULL
         ) THEN 1 END) AS invalid_empty_authority_projection_count,
         count(CASE WHEN occurrence.retry_state <> 'none' AND (
           retry IS NULL OR retry.status <> occurrence.retry_state
         ) THEN 1 END) AS invalid_current_retry_projection_count,
         count(CASE WHEN occurrence.retry_state = 'none' AND
           occurrence.current_retry_sequence IS NOT NULL
         THEN 1 END) AS invalid_empty_retry_projection_count
}
CALL (logical) {
  OPTIONAL MATCH (checkpoint:IngestionCheckpoint {
    logical_run_id: logical.logical_run_id,
    phase: logical.current_phase
  })
  RETURN checkpoint.revision AS checkpoint_revision,
         checkpoint.last_page_sequence AS checkpoint_last_page_sequence,
         checkpoint.cursor_json AS checkpoint_cursor_json,
         checkpoint.source_window_json AS checkpoint_source_window_json,
         checkpoint.replay_boundary AS replay_boundary,
         checkpoint.last_committed_record_id AS last_committed_unit_id,
         checkpoint.committed_count AS checkpoint_committed_count,
         checkpoint.duplicate_count AS checkpoint_duplicate_count,
         checkpoint.excluded_count AS checkpoint_excluded_count,
         checkpoint.retry_count AS checkpoint_retry_count
}
CALL (logical) {
  OPTIONAL MATCH (logical)-[:HAS_STAGE_HISTORY_REVIEW_COMMAND]->(
    command:StageHistoryReviewCommand
  )
  RETURN count(DISTINCT command) AS review_command_count,
         count(DISTINCT CASE WHEN command.status = 'claimed'
                             AND command.lease_expires_at < datetime()
                             THEN command END) AS expired_review_claim_count
}
WITH logical, units, variant_count, source_record_count,
     invalid_variant_evidence_count, shared_variant_evidence_count,
     occurrence_variant_identity_count, invalid_occurrence_variant_link_count,
     invalid_empty_occurrence_variant_link_count,
     invalid_parent_association_count,
     invalid_authority_head_count, invalid_effective_head_count,
     invalid_invalidation_transition_count,
     invalidation_intent_count, committed_page_sequences, committed_unit_ids,
     committed_unit_digests,
     nonterminal_unit_count, total_fetched_count,
     total_malformed_excluded_count, total_capture_rejected_valid_count,
     total_excluded_out_of_scope_count, total_canonical_effective_count,
     total_canonical_pending_parent_count, total_parent_waiting_count,
     total_parent_ambiguous_count, total_same_hash_replay_count,
     total_differing_hash_conflict_count, total_new_variant_count,
     total_existing_same_hash_count, total_new_conflict_variant_count,
     total_selected_active_count, total_selected_pending_review_count,
     total_waiting_count, total_ambiguous_count, total_association_rejected_count,
     total_effective_count, total_withheld_parent_count,
     total_withheld_conflict_count, total_authority_rejected_count,
     total_corrected_count, total_retry_none_count, total_retry_pending_count,
     total_retry_claimed_count, total_retry_resolved_count,
     total_retry_rejected_count, total_retry_quarantined_count,
     invalid_current_association_projection_count,
     invalid_empty_association_projection_count,
     invalid_current_authority_projection_count,
     invalid_empty_authority_projection_count,
     invalid_current_retry_projection_count,
     invalid_empty_retry_projection_count,
     checkpoint_revision, checkpoint_last_page_sequence, checkpoint_cursor_json,
     checkpoint_source_window_json,
     replay_boundary,
     last_committed_unit_id,
     checkpoint_committed_count, checkpoint_duplicate_count,
     checkpoint_excluded_count, checkpoint_retry_count, review_command_count,
     expired_review_claim_count,
     CASE WHEN size(committed_page_sequences) = 0 THEN []
          ELSE range(1, size(committed_page_sequences)) END AS expected_pages
RETURN logical.logical_run_id AS logical_run_id,
       logical.mode AS run_type,
       units,
       all(unit IN units WHERE unit.balanced) AS units_balanced,
       variant_count,
       source_record_count,
       invalid_variant_evidence_count,
       invalid_variant_evidence_count = 0
         AND shared_variant_evidence_count = 0
         AND invalid_occurrence_variant_link_count = 0
         AND invalid_empty_occurrence_variant_link_count = 0
         AND occurrence_variant_identity_count = variant_count
         AND variant_count = source_record_count AS variant_source_records_balanced,
       shared_variant_evidence_count,
       occurrence_variant_identity_count,
       invalid_occurrence_variant_link_count,
       invalid_empty_occurrence_variant_link_count,
       invalid_parent_association_count,
       invalid_parent_association_count = 0 AS parent_associations_balanced,
       invalid_authority_head_count,
       invalid_effective_head_count,
       invalid_invalidation_transition_count,
       invalidation_intent_count,
       committed_page_sequences,
       committed_page_sequences = expected_pages AS committed_pages_contiguous,
       checkpoint_revision,
       checkpoint_revision = size(committed_page_sequences)
         AS checkpoint_revision_balanced,
       checkpoint_last_page_sequence = CASE
         WHEN size(committed_page_sequences) = 0 THEN NULL
         ELSE committed_page_sequences[-1]
       END AS checkpoint_cursor_page_balanced,
       checkpoint_cursor_json = '{"last_page_sequence":' +
         CASE WHEN checkpoint_last_page_sequence IS NULL THEN 'null'
              ELSE toString(checkpoint_last_page_sequence) END +
         ',"revision":' + toString(checkpoint_revision) + '}'
         AS checkpoint_cursor_json_balanced,
       replay_boundary,
       replay_boundary = 'exclusive_artifact_page_sequence'
         AS replay_boundary_valid,
       last_committed_unit_id,
       CASE WHEN size(committed_unit_ids) = 0 THEN last_committed_unit_id IS NULL
            ELSE last_committed_unit_id = committed_unit_ids[-1] END
         AS checkpoint_last_unit_balanced,
       nonterminal_unit_count,
       total_fetched_count,
       total_malformed_excluded_count,
       total_capture_rejected_valid_count,
       total_excluded_out_of_scope_count,
       total_canonical_effective_count,
       total_canonical_pending_parent_count,
       total_parent_waiting_count,
       total_parent_ambiguous_count,
       total_same_hash_replay_count,
       total_differing_hash_conflict_count,
       total_new_variant_count,
       total_existing_same_hash_count,
       total_new_conflict_variant_count,
       total_selected_active_count,
       total_selected_pending_review_count,
       total_waiting_count,
       total_ambiguous_count,
       total_association_rejected_count,
       total_effective_count,
       total_withheld_parent_count,
       total_withheld_conflict_count,
       total_authority_rejected_count,
       total_corrected_count,
       total_retry_none_count,
       total_retry_pending_count,
       total_retry_claimed_count,
       total_retry_resolved_count,
       total_retry_rejected_count,
       total_retry_quarantined_count,
       total_selected_active_count + total_selected_pending_review_count +
         total_waiting_count + total_ambiguous_count +
         total_association_rejected_count =
         total_new_variant_count + total_existing_same_hash_count +
         total_new_conflict_variant_count AS current_association_partition_balanced,
       total_effective_count + total_withheld_parent_count +
         total_withheld_conflict_count + total_authority_rejected_count +
         total_corrected_count =
         total_new_variant_count + total_existing_same_hash_count +
         total_new_conflict_variant_count AS current_authority_partition_balanced,
       total_retry_none_count + total_retry_pending_count +
         total_retry_claimed_count + total_retry_resolved_count +
         total_retry_rejected_count + total_retry_quarantined_count =
         total_fetched_count AS current_retry_partition_balanced,
       invalid_current_association_projection_count,
       invalid_empty_association_projection_count,
       invalid_current_authority_projection_count,
       invalid_empty_authority_projection_count,
       invalid_current_retry_projection_count,
       invalid_empty_retry_projection_count,
       checkpoint_source_window_json,
       committed_unit_ids,
       committed_unit_digests,
       checkpoint_committed_count =
         total_canonical_effective_count + total_canonical_pending_parent_count +
         total_parent_waiting_count + total_parent_ambiguous_count +
         total_differing_hash_conflict_count
         AND logical.committed_count = checkpoint_committed_count
         AS committed_counter_balanced,
       checkpoint_duplicate_count = total_same_hash_replay_count
         AND logical.duplicate_count = checkpoint_duplicate_count
         AS duplicate_counter_balanced,
       checkpoint_excluded_count = total_malformed_excluded_count +
         total_capture_rejected_valid_count + total_excluded_out_of_scope_count
         AND logical.excluded_count = checkpoint_excluded_count
         AS excluded_counter_balanced,
       checkpoint_retry_count = total_canonical_pending_parent_count +
         total_parent_waiting_count + total_parent_ambiguous_count
         AND logical.retry_count = checkpoint_retry_count
         AS retry_counter_balanced,
       review_command_count,
       expired_review_claim_count
"""


STAGE_HISTORY_MUTATION_QUERIES: tuple[str, ...] = (
    CREATE_STAGE_HISTORY_UNIT,
    UPSERT_STAGE_HISTORY_OCCURRENCE,
    UPSERT_STAGE_HISTORY_FAILED_OCCURRENCE,
    UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD,
    APPEND_STAGE_HISTORY_PARENT_DECISION,
    PROJECT_STAGE_HISTORY_AUTHORITY_HEAD,
    PERSIST_STAGE_HISTORY_REVIEW_COMMAND,
    CLAIM_STAGE_HISTORY_REVIEW_COMMAND,
    COMPLETE_STAGE_HISTORY_REVIEW_COMMAND,
    UPSERT_STAGE_HISTORY_RETRY,
    CLAIM_STAGE_HISTORY_RETRY,
    RESOLVE_STAGE_HISTORY_RETRY,
    APPEND_STAGE_HISTORY_INVALIDATION_INTENTS,
    UPSERT_STAGE_HISTORY_UNIT_ACCOUNTING,
    COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT,
)

GET_STAGE_HISTORY_REVIEW_ASSOCIATION = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (association:CrmHistoryParentAssociationDecision {
  decision_id: $association_decision_id,
  event_identity: $event_identity
})-[:ASSOCIATION_FOR]->(occurrence:StageHistoryOccurrence {
  occurrence_id: $occurrence_id,
  event_identity: $event_identity
})
WHERE association.association_state IN ['selected_active', 'selected_pending_review']
MERGE (parent_identity_lock:SourceRecordIdentityLock {
  source_system: association.logical_parent_source_system,
  source_instance_id: $logical_parent_source_instance_id,
  source_record_id: association.logical_parent_source_record_id
})
SET parent_identity_lock.locked_at = datetime()
WITH association, parent_identity_lock
OPTIONAL MATCH (association)-[:SELECTS_STAGE_HISTORY_PARENT]->(parent:SourceRecord)
WITH association, collect(DISTINCT parent) AS selected_parents
WITH association, selected_parents, selected_parents[0] AS selected_parent
WHERE size(selected_parents) = 1
  AND selected_parent.source_record_pk =
      association.selected_parent_source_record_pk
  AND selected_parent.source_instance_id = $logical_parent_source_instance_id
  AND selected_parent.source_record_id =
      association.logical_parent_source_record_id
  AND selected_parent.record_type = 'crm_deal'
  AND selected_parent.lifecycle_status = 'active'
  AND EXISTS {
    MATCH (selected_parent)-[:FROM_SOURCE]->(:SourceSystem {
      source_key: association.logical_parent_source_system
    })
  }
RETURN association.decision_id AS decision_id,
       association.association_state AS association_state,
       association.logical_parent_source_system AS logical_parent_source_system,
       association.logical_parent_source_record_id AS logical_parent_source_record_id,
       association.selected_parent_source_record_pk AS selected_parent_source_record_pk
"""
)

GET_COMPLETED_STAGE_HISTORY_REVIEW_COMMAND = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (command:StageHistoryReviewCommand {command_id: $command_id, status: 'completed'})
WHERE command.review_kind = $review_kind
  AND command.target_event_identity = $target_event_identity
  AND coalesce(command.target_occurrence_id, '') = coalesce($target_occurrence_id, '')
  AND command.request_payload_digest = $request_payload_digest
  AND command.reviewer_actor = $reviewer_actor
  AND command.authorization_reference = $authorization_reference
  AND command.available_at = datetime($available_at)
  AND command.expected_head_version = $expected_head_version
  AND command.expected_authority_token = $expected_authority_token
  AND command.expected_authority_state = $expected_authority_state
  AND command.expected_variant_set_digest = $expected_variant_set_digest
  AND coalesce(command.retry_sequence, 0) = coalesce($retry_sequence, 0)
  AND coalesce(command.selected_variant_hash, '') = coalesce($selected_variant_hash, '')
  AND coalesce(command.selected_association_decision_id, '') =
      coalesce($selected_association_decision_id, '')
  AND coalesce(command.correction_of_decision_id, '') =
      coalesce($correction_of_decision_id, '')
RETURN command.command_id AS command_id,
       command.result_authority_decision_id AS authority_decision_id,
       command.result_authority_state AS authority_state,
       command.result_head_version AS head_version,
       command.result_authority_token AS authority_token,
       command.result_invalidation_count AS invalidation_count,
       command.result_digest AS result_digest
"""
)

GET_STAGE_HISTORY_REVIEW_VARIANT_SET = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (variant:CrmHistoryHashVariant {event_identity: $event_identity})
WITH variant
ORDER BY variant.canonical_hash
RETURN collect(variant.canonical_hash) AS canonical_hashes
"""
)

GET_STAGE_HISTORY_REVIEW_OCCURRENCE = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (occurrence:StageHistoryOccurrence {
  occurrence_id: $occurrence_id,
  event_identity: $event_identity
})
RETURN occurrence.occurrence_id AS occurrence_id,
       occurrence.canonical_hash AS canonical_hash,
       occurrence.association_state AS association_state,
       occurrence.current_association_decision_id AS current_association_decision_id,
       occurrence.retry_state AS retry_state,
       occurrence.logical_parent_source_system AS logical_parent_source_system,
       occurrence.logical_parent_source_record_id AS logical_parent_source_record_id,
       occurrence.source_observed_at AS source_observed_at
"""
)

RESOLVE_STAGE_HISTORY_REVIEW_PARENT_CANDIDATES = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (occurrence:StageHistoryOccurrence {
  occurrence_id: $occurrence_id,
  event_identity: $event_identity
})
MERGE (parent_identity_lock:SourceRecordIdentityLock {
  source_system: occurrence.logical_parent_source_system,
  source_instance_id: $logical_parent_source_instance_id,
  source_record_id: occurrence.logical_parent_source_record_id
})
SET parent_identity_lock.locked_at = datetime()
WITH occurrence, parent_identity_lock
OPTIONAL MATCH (parent:SourceRecord {
  source_instance_id: $logical_parent_source_instance_id,
  source_record_id: occurrence.logical_parent_source_record_id,
  record_type: 'crm_deal'
})-[:FROM_SOURCE]->(:SourceSystem {
  source_key: occurrence.logical_parent_source_system
})
WHERE parent.lifecycle_status IN ['active', 'pending_review']
WITH occurrence,
     [candidate IN collect(parent)
      WHERE candidate IS NOT NULL AND candidate.lifecycle_status = 'active'] AS active,
     [candidate IN collect(parent)
      WHERE candidate IS NOT NULL AND candidate.lifecycle_status = 'pending_review'] AS pending
RETURN occurrence.logical_parent_source_system AS logical_parent_source_system,
       occurrence.logical_parent_source_record_id AS logical_parent_source_record_id,
       size(active) AS active_count,
       size(pending) AS pending_count,
       CASE
         WHEN size(active) = 1 THEN 'selected_active'
         WHEN size(active) > 1 THEN 'ambiguous'
         WHEN size(pending) = 1 THEN 'selected_pending_review'
         WHEN size(pending) > 1 THEN 'ambiguous'
         ELSE 'waiting'
       END AS association_state,
       CASE
         WHEN size(active) = 1 THEN active[0].source_record_pk
         WHEN size(active) = 0 AND size(pending) = 1 THEN pending[0].source_record_pk
         ELSE NULL
       END AS selected_parent_source_record_pk
"""
)

RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (occurrence:StageHistoryOccurrence {occurrence_id: $occurrence_id})
MATCH (retry:StageHistoryRetry {
  occurrence_id: $occurrence_id,
  retry_sequence: $retry_sequence,
  status: 'claimed',
  lease_owner: $lease_owner,
  lease_attempt_id: $ingest_run_id,
  lease_attempt_generation: $attempt_generation,
  lease_stream_generation: $stream_generation,
  lease_fencing_token: $fencing_token
})
WHERE EXISTS { MATCH (occurrence)-[:HAS_STAGE_HISTORY_RETRY]->(retry) }
  AND retry.lease_expires_at >= datetime()
  AND retry.review_command_id = $review_command_id
  AND $resolution IN ['pending', 'resolved', 'rejected', 'quarantined']
SET retry.status = $resolution,
    retry.resolution_decision_id = $resolution_decision_id,
    retry.review_command_id = $review_command_id,
    retry.next_attempt_at = CASE WHEN $resolution = 'pending'
      THEN datetime($next_attempt_at) ELSE retry.next_attempt_at END,
    retry.resolved_at = CASE WHEN $resolution = 'pending' THEN NULL ELSE datetime() END,
    retry.updated_at = datetime(),
    retry.lease_owner = NULL,
    retry.lease_attempt_id = NULL,
    retry.lease_attempt_generation = NULL,
    retry.lease_stream_generation = NULL,
    retry.lease_fencing_token = NULL,
    retry.claimed_at = NULL,
    retry.lease_expires_at = NULL
SET occurrence.retry_state = retry.status,
    occurrence.current_retry_sequence = retry.retry_sequence,
    occurrence.projection_updated_at = datetime()
RETURN count(retry) AS resolved_retry_count
"""
)


PROJECT_STAGE_HISTORY_REVIEW_OUTCOME = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (command:StageHistoryReviewCommand {
  command_id: $command_id,
  target_event_identity: $event_identity,
  target_occurrence_id: $occurrence_id,
  status: 'claimed'
})
MATCH (target:StageHistoryOccurrence {
  occurrence_id: $occurrence_id,
  event_identity: $event_identity
})
MATCH (head:CrmHistoryAuthorityHead {
  event_identity: $event_identity,
  decision_id: $authority_decision_id,
  head_version: $authority_head_version,
  authority_token: $authority_token,
  authority_state: $authority_state
})
MATCH (decision:CrmHistoryAuthorityDecision {
  decision_id: $authority_decision_id,
  review_command_id: $command_id,
  head_version: $authority_head_version,
  authority_token: $authority_token,
  authority_state: $authority_state
})-[:DECIDES_FOR]->(:CrmHistoryConflictGroup {event_identity: $event_identity})
WHERE command.lease_owner = $lease_owner
  AND command.lease_attempt_id = $ingest_run_id
  AND command.lease_attempt_generation = $attempt_generation
  AND command.lease_stream_generation = $stream_generation
  AND command.lease_fencing_token = $fencing_token
  AND command.lease_expires_at >= datetime()
  AND $association_state IN [
    'selected_active', 'selected_pending_review', 'waiting', 'ambiguous', 'rejected'
  ]
  AND $authority_state IN [
    'effective', 'withheld_parent', 'withheld_conflict', 'rejected', 'corrected'
  ]
  AND ($retry_state IS NULL OR $retry_state IN [
    'none', 'pending', 'claimed', 'resolved', 'rejected', 'quarantined'
  ])
SET target.association_state = $association_state,
    target.current_association_decision_id = coalesce(
      $association_decision_id, target.current_association_decision_id
    ),
    target.retry_state = coalesce($retry_state, target.retry_state),
    target.projection_updated_at = datetime()
WITH target, head
MATCH (event_occurrence:StageHistoryOccurrence {event_identity: $event_identity})
WHERE event_occurrence.parse_scope = 'in_scope'
SET event_occurrence.authority_state = head.authority_state,
    event_occurrence.current_authority_decision_id = head.decision_id,
    event_occurrence.projection_updated_at = datetime()
RETURN target.association_state AS association_state,
       target.authority_state AS authority_state,
       target.retry_state AS retry_state,
       count(event_occurrence) AS projected_occurrence_count
"""
)


CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW = (
    _ACTIVE_STAGE_FENCE
    + """
MATCH (command:StageHistoryReviewCommand {
  command_id: $review_command_id,
  target_occurrence_id: $occurrence_id,
  status: 'claimed'
})
MATCH (retry:StageHistoryRetry {
  occurrence_id: $occurrence_id,
  retry_sequence: $retry_sequence
})
MATCH (occurrence:StageHistoryOccurrence {occurrence_id: $occurrence_id})
WHERE command.lease_owner = $lease_owner
  AND command.lease_attempt_id = $ingest_run_id
  AND command.lease_attempt_generation = $attempt_generation
  AND command.lease_stream_generation = $stream_generation
  AND command.lease_fencing_token = $fencing_token
  AND command.lease_expires_at >= datetime()
  AND EXISTS { MATCH (occurrence)-[:HAS_STAGE_HISTORY_RETRY]->(retry) }
  AND coalesce(retry.attempt_count, 0) < retry.max_attempts
  AND retry.next_attempt_at <= datetime()
  AND (retry.status = 'pending'
    OR (retry.status = 'claimed' AND retry.lease_expires_at < datetime()))
SET retry.status = 'claimed',
    retry.lease_owner = $lease_owner,
    retry.lease_attempt_id = $ingest_run_id,
    retry.lease_attempt_generation = $attempt_generation,
    retry.lease_stream_generation = $stream_generation,
    retry.lease_fencing_token = $fencing_token,
    retry.lease_expires_at = datetime($lease_expires_at),
    retry.attempt_count = coalesce(retry.attempt_count, 0) + 1,
    retry.claimed_at = datetime(),
    retry.review_command_id = $review_command_id,
    retry.updated_at = datetime()
SET occurrence.retry_state = retry.status,
    occurrence.current_retry_sequence = retry.retry_sequence,
    occurrence.projection_updated_at = datetime()
RETURN retry.retry_sequence AS retry_sequence,
       retry.status AS status,
       retry.attempt_count AS attempt_count,
       retry.max_attempts AS max_attempts
"""
)

STAGE_HISTORY_REVIEW_MUTATION_QUERIES: tuple[str, ...] = (
    PERSIST_STAGE_HISTORY_REVIEW_COMMAND,
    CLAIM_STAGE_HISTORY_REVIEW_COMMAND,
    LOCK_STAGE_HISTORY_REVIEW_EVENT,
    COMPLETE_STAGE_HISTORY_REVIEW_COMMAND,
    CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW,
    RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW,
    PROJECT_STAGE_HISTORY_REVIEW_OUTCOME,
)


CLASSIFY_STAGE_HISTORY_OBSERVATION = """
OPTIONAL MATCH (variant:CrmHistoryHashVariant {event_identity: $event_identity})
WITH collect(DISTINCT variant) AS variants
OPTIONAL MATCH (parent:SourceRecord {
  source_instance_id: $logical_parent_source_instance_id,
  source_record_id: $logical_parent_source_record_id,
  record_type: 'crm_deal'
})-[:FROM_SOURCE]->(:SourceSystem {source_key: $logical_parent_source_system})
WHERE parent.lifecycle_status IN ['active', 'pending_review']
WITH variants,
     [candidate IN collect(parent)
      WHERE candidate IS NOT NULL AND candidate.lifecycle_status = 'active'] AS active,
     [candidate IN collect(parent)
      WHERE candidate IS NOT NULL AND candidate.lifecycle_status = 'pending_review'] AS pending
OPTIONAL MATCH (head:CrmHistoryAuthorityHead {event_identity: $event_identity})
WITH variants, active, pending, head,
     size([known IN variants WHERE known.canonical_hash = $canonical_hash]) AS exact_count,
     CASE
       WHEN size(active) = 1 THEN 'selected_active'
       WHEN size(active) > 1 THEN 'ambiguous'
       WHEN size(pending) = 1 THEN 'selected_pending_review'
       WHEN size(pending) > 1 THEN 'ambiguous'
       ELSE 'waiting'
     END AS association_state
RETURN exact_count,
       size(variants) AS variant_count,
       association_state,
       head.authority_state AS current_authority_state
"""

GET_STAGE_HISTORY_REVIEW_COMMAND_CONTEXT = """
MATCH (logical:IngestionLogicalRun)-[:HAS_STAGE_HISTORY_REVIEW_COMMAND]->(
  command:StageHistoryReviewCommand {command_id: $command_id}
)
WHERE logical.source_key = 'bitrix_chat'
  AND logical.mode IN [
    'parent_reconcile', 'conflict_review', 'correction_review'
  ]
MATCH (logical)-[:ACTIVE_ATTEMPT]->(attempt:IngestRun)
MATCH (stream:BitrixIngestionStream {
  source_key: 'bitrix_chat',
  stream_key: 'crm_stage_history',
  logical_run_id: logical.logical_run_id,
  ingest_run_id: attempt.ingest_run_id,
  attempt_generation: logical.active_generation,
  status: 'active'
})
WHERE attempt.generation = logical.active_generation
RETURN logical.logical_run_id AS logical_run_id,
       logical.mode AS run_type,
       logical.status AS logical_status,
       logical.configuration_fingerprint AS configuration_fingerprint,
       attempt.ingest_run_id AS ingest_run_id,
       attempt.worker_task_id AS worker_task_id,
       attempt.generation AS attempt_generation,
       stream.stream_generation AS stream_generation,
       stream.fencing_token AS fencing_token,
       command.command_id AS command_id,
       command.review_kind AS review_kind,
       command.status AS command_status,
       command.target_event_identity AS target_event_identity,
       command.target_occurrence_id AS target_occurrence_id,
       command.request_payload_digest AS request_payload_digest,
       command.reviewer_actor AS reviewer_actor,
       command.authorization_reference AS authorization_reference,
       toString(command.available_at) AS available_at,
       command.expected_head_version AS expected_head_version,
       command.expected_authority_token AS expected_authority_token,
       command.expected_authority_state AS expected_authority_state,
       command.expected_variant_set_digest AS expected_variant_set_digest,
       command.retry_sequence AS retry_sequence,
       command.selected_variant_hash AS selected_variant_hash,
       command.selected_association_decision_id AS selected_association_decision_id,
       command.correction_of_decision_id AS correction_of_decision_id
"""


GET_STAGE_HISTORY_REVIEW_RESUME_CONTEXT = """
MATCH (logical:IngestionLogicalRun)-[:HAS_STAGE_HISTORY_REVIEW_COMMAND]->(
  command:StageHistoryReviewCommand {command_id: $command_id}
)
WHERE logical.source_key = 'bitrix_chat'
  AND logical.mode IN [
    'parent_reconcile', 'conflict_review', 'correction_review'
  ]
OPTIONAL MATCH (logical)-[:ACTIVE_ATTEMPT]->(attempt:IngestRun)
RETURN logical.logical_run_id AS logical_run_id,
       logical.mode AS run_type,
       logical.status AS logical_status,
       logical.configuration_fingerprint AS configuration_fingerprint,
       attempt.worker_task_id AS worker_task_id,
       command.command_id AS command_id,
       command.review_kind AS review_kind,
       command.status AS command_status,
       command.target_event_identity AS target_event_identity,
       command.target_occurrence_id AS target_occurrence_id,
       command.request_payload_digest AS request_payload_digest,
       command.reviewer_actor AS reviewer_actor,
       command.authorization_reference AS authorization_reference,
       toString(command.available_at) AS available_at,
       command.expected_head_version AS expected_head_version,
       command.expected_authority_token AS expected_authority_token,
       command.expected_authority_state AS expected_authority_state,
       command.expected_variant_set_digest AS expected_variant_set_digest,
       command.retry_sequence AS retry_sequence,
       command.selected_variant_hash AS selected_variant_hash,
       command.selected_association_decision_id AS selected_association_decision_id,
       command.correction_of_decision_id AS correction_of_decision_id
"""
