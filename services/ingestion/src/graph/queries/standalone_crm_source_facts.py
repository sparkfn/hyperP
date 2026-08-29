"""Parameterized guarded queries for #302 source-fact page commits."""

from __future__ import annotations

READ_CENSUS_REQUEST = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, generation: $generation})
RETURN census.request_json AS request_json
"""

# A receipt is intentionally serialized through the existing locked census transaction.
# authorization_id/digest are parent-issued trust inputs: #301 does not persist them as
# independently reconstructable authority, so they are immutable receipt/CAS identity.
CLAIM_PAGE = """
MATCH (census:StandaloneCrmCensus {
  census_id: $census_id, generation: $generation, source_key: $source_key,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  census_kind: 'source_sync', request_json: $request_json
})
MATCH (attempt:StandaloneCrmCensusAttempt {
  census_id: $census_id, generation: $generation, fence_token: $fence_token,
  status: 'running', task_id: $parent_task_id, attempt_deadline: datetime($attempt_deadline)
})
MATCH (:StandaloneCrmCensusUnit {
  census_id: $census_id, generation: $generation, stream_kind: $stream_kind,
  state: 'running', frozen_upper_id: $frozen_upper_id
})
MATCH (fence:StandaloneCrmCensusFence {
  census_id: $census_id, generation: $generation, stream_kind: $stream_kind,
  token: $fence_token, owner_id: $fence_owner_id, status: 'active'
})
MATCH (:StandaloneCrmChildPublication {
  census_id: $census_id, generation: $generation, stream_kind: $stream_kind,
  task_name: $task_name, task_id: $task_id, payload_digest: $payload_digest,
  status: 'published'
})
MATCH (:StandaloneCrmHttpCallReservation {
  intent_id: $call_intent_id, census_id: $census_id, generation: $generation,
  fence_token: $fence_token, stream_kind: $stream_kind, call_kind: 'page',
  cursor: $expected_cursor, task_id: $task_id, status: 'succeeded'
})
MATCH (:BitrixSourceInstance {
  source_key: $source_key, source_instance_id: $source_instance_id, status: 'active'
})-[:INSTANCE_OF]->(:SourceSystem {source_key: $source_key, is_active: true})
MATCH (:BitrixExecutionSourceBinding {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
SET census.standalone_crm_source_fact_lock = true
REMOVE census.standalone_crm_source_fact_lock
WITH census, attempt, fence
OPTIONAL MATCH (checkpoint:StandaloneCrmCensusCheckpoint {
  census_id: $census_id, stream_kind: $stream_kind
})
OPTIONAL MATCH (receipt:StandaloneCrmSourceFactPageReceipt {receipt_key: $receipt_key})
WITH census, attempt, fence, checkpoint, receipt
WHERE census.status IN ['running', 'publishing', 'recovering']
  AND coalesce(census.cancel_requested, false) = false
  AND census.created_at = datetime($available_at)
  AND fence.lease_until >= datetime()
  AND datetime() < attempt.attempt_deadline
  AND datetime() < datetime($occurrence_deadline)
  AND coalesce(attempt.call_count, 0) <= $attempt_call_limit
  AND coalesce(census.occurrence_calls, 0) <= $occurrence_call_limit
  AND $expected_cursor < $proposed_cursor
  AND $proposed_cursor <= $frozen_upper_id
WITH census, attempt, checkpoint, receipt,
  CASE WHEN checkpoint IS NULL THEN $checkpoint_absent ELSE
    checkpoint.last_committed_id = $expected_cursor
    AND checkpoint.processed_rows = $expected_processed
    AND checkpoint.skipped_rows = $expected_skipped
    AND checkpoint.generation = $generation
    AND checkpoint.fence_token = $fence_token
    AND checkpoint.frozen_upper_id = $frozen_upper_id
    AND checkpoint.revision_id IS NULL
    AND checkpoint.binding_subject_id IS NULL
    AND checkpoint.binding_offset IS NULL END AS checkpoint_matches,
  CASE WHEN receipt IS NULL THEN false ELSE
    receipt.status = 'committed'
    AND receipt.census_id = $census_id
    AND receipt.generation = $generation
    AND receipt.stream_kind = $stream_kind
    AND receipt.fence_token = $fence_token
    AND receipt.fence_owner_id = $fence_owner_id
    AND receipt.source_key = $source_key
    AND receipt.source_instance_id = $source_instance_id
    AND receipt.control_instance_id = $control_instance_id
    AND receipt.task_name = $task_name
    AND receipt.task_id = $task_id
    AND receipt.payload_digest = $payload_digest
    AND receipt.call_intent_id = $call_intent_id
    AND receipt.authorization_id = $authorization_id
    AND receipt.authorization_digest = $authorization_digest
    AND receipt.available_at = datetime($available_at)
    AND receipt.availability_contract_version = $availability_contract_version
    AND receipt.frozen_upper_id = $frozen_upper_id
    AND receipt.content_digest = $content_digest
    AND receipt.expected_cursor = $expected_cursor
    AND receipt.proposed_cursor = $proposed_cursor END AS receipt_replay
WITH census, attempt, checkpoint, receipt, checkpoint_matches, receipt_replay,
  CASE WHEN receipt IS NOT NULL AND NOT receipt_replay THEN 'conflict'
       WHEN receipt_replay THEN 'replayed'
       WHEN NOT checkpoint_matches THEN 'conflict'
       WHEN coalesce(census.occurrence_rows, 0) + $processed_delta > $occurrence_row_limit
         THEN 'occurrence_exhausted'
       WHEN coalesce(attempt.row_count, 0) + $processed_delta > $attempt_row_limit
         THEN 'attempt_exhausted'
       ELSE 'apply' END AS decision
FOREACH (_ IN CASE WHEN decision = 'apply' THEN [1] ELSE [] END |
  CREATE (:StandaloneCrmSourceFactPageReceipt {
    receipt_key: $receipt_key, status: 'applying', census_id: $census_id,
    generation: $generation, stream_kind: $stream_kind, fence_token: $fence_token,
    fence_owner_id: $fence_owner_id, source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
    task_name: $task_name, task_id: $task_id, payload_digest: $payload_digest,
    call_intent_id: $call_intent_id, authorization_id: $authorization_id,
    authorization_digest: $authorization_digest, available_at: datetime($available_at),
    availability_contract_version: $availability_contract_version,
    frozen_upper_id: $frozen_upper_id, content_digest: $content_digest,
    expected_cursor: $expected_cursor, proposed_cursor: $proposed_cursor, created_at: datetime()
  })
)
RETURN decision AS decision
"""

FINALIZE_PAGE = """
MATCH (census:StandaloneCrmCensus {
  census_id: $census_id, generation: $generation, source_key: $source_key,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  census_kind: 'source_sync', request_json: $request_json
})
MATCH (attempt:StandaloneCrmCensusAttempt {
  census_id: $census_id, generation: $generation, fence_token: $fence_token,
  status: 'running', task_id: $parent_task_id, attempt_deadline: datetime($attempt_deadline)
})
MATCH (:StandaloneCrmCensusUnit {
  census_id: $census_id, generation: $generation, stream_kind: $stream_kind,
  state: 'running', frozen_upper_id: $frozen_upper_id
})
MATCH (fence:StandaloneCrmCensusFence {
  census_id: $census_id, generation: $generation, stream_kind: $stream_kind,
  token: $fence_token, owner_id: $fence_owner_id, status: 'active'
})
MATCH (:StandaloneCrmChildPublication {
  census_id: $census_id, generation: $generation, stream_kind: $stream_kind,
  task_name: $task_name, task_id: $task_id, payload_digest: $payload_digest,
  status: 'published'
})
MATCH (:StandaloneCrmHttpCallReservation {
  intent_id: $call_intent_id, census_id: $census_id, generation: $generation,
  fence_token: $fence_token, stream_kind: $stream_kind, call_kind: 'page',
  cursor: $expected_cursor, task_id: $task_id, status: 'succeeded'
})
MATCH (:BitrixSourceInstance {
  source_key: $source_key, source_instance_id: $source_instance_id, status: 'active'
})-[:INSTANCE_OF]->(:SourceSystem {source_key: $source_key, is_active: true})
MATCH (:BitrixExecutionSourceBinding {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
MATCH (receipt:StandaloneCrmSourceFactPageReceipt {
  receipt_key: $receipt_key, status: 'applying', census_id: $census_id,
  generation: $generation, stream_kind: $stream_kind, fence_token: $fence_token,
  fence_owner_id: $fence_owner_id, source_key: $source_key,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  task_name: $task_name, task_id: $task_id, payload_digest: $payload_digest,
  call_intent_id: $call_intent_id, authorization_id: $authorization_id,
  authorization_digest: $authorization_digest, available_at: datetime($available_at),
  availability_contract_version: $availability_contract_version,
  frozen_upper_id: $frozen_upper_id, content_digest: $content_digest,
  expected_cursor: $expected_cursor, proposed_cursor: $proposed_cursor
})
SET census.standalone_crm_source_fact_lock = true
REMOVE census.standalone_crm_source_fact_lock
WITH census, attempt, fence, receipt
OPTIONAL MATCH (checkpoint:StandaloneCrmCensusCheckpoint {
  census_id: $census_id, stream_kind: $stream_kind
})
WITH census, attempt, fence, receipt, checkpoint
WHERE census.status IN ['running', 'publishing', 'recovering']
  AND coalesce(census.cancel_requested, false) = false
  AND census.created_at = datetime($available_at)
  AND fence.lease_until >= datetime()
  AND datetime() < attempt.attempt_deadline
  AND datetime() < datetime($occurrence_deadline)
  AND coalesce(attempt.call_count, 0) <= $attempt_call_limit
  AND coalesce(census.occurrence_calls, 0) <= $occurrence_call_limit
  AND $expected_cursor < $proposed_cursor
  AND $proposed_cursor <= $frozen_upper_id
  AND ((checkpoint IS NULL AND $checkpoint_absent) OR (
    checkpoint.last_committed_id = $expected_cursor
    AND checkpoint.processed_rows = $expected_processed
    AND checkpoint.skipped_rows = $expected_skipped
    AND checkpoint.generation = $generation
    AND checkpoint.fence_token = $fence_token
    AND checkpoint.frozen_upper_id = $frozen_upper_id
    AND checkpoint.revision_id IS NULL
    AND checkpoint.binding_subject_id IS NULL
    AND checkpoint.binding_offset IS NULL))
  AND coalesce(census.occurrence_rows, 0) + $processed_delta <= $occurrence_row_limit
  AND coalesce(attempt.row_count, 0) + $processed_delta <= $attempt_row_limit
MERGE (stored:StandaloneCrmCensusCheckpoint {census_id: $census_id, stream_kind: $stream_kind})
SET stored.last_committed_id = $proposed_cursor,
  stored.processed_rows = $proposed_processed,
  stored.skipped_rows = $proposed_skipped,
  stored.binding_subject_id = null,
  stored.binding_offset = null,
  stored.generation = $generation,
  stored.fence_token = $fence_token,
  stored.frozen_upper_id = $frozen_upper_id,
  stored.revision_id = null,
  stored.updated_at = datetime(),
  receipt.status = 'committed',
  receipt.processed_rows = $processed_delta,
  receipt.skipped_rows = $skipped_delta,
  receipt.failed_rows = $failed_delta,
  receipt.committed_at = datetime(),
  census.occurrence_rows = coalesce(census.occurrence_rows, 0) + $processed_delta,
  census.standalone_crm_source_fact_failed_rows =
    coalesce(census.standalone_crm_source_fact_failed_rows, 0) + $failed_delta,
  attempt.row_count = coalesce(attempt.row_count, 0) + $processed_delta
RETURN receipt.receipt_key AS receipt_key
"""

STAMP_SOURCE_FACT_LINEAGE = """
MATCH (record:SourceRecord {source_record_pk: $source_record_pk})
SET record.standalone_crm_available_at = datetime($available_at),
  record.standalone_crm_census_id = $census_id,
  record.standalone_crm_stream_kind = $stream_kind,
  record.standalone_crm_generation = $generation,
  record.standalone_crm_fence_token = $fence_token,
  record.standalone_crm_fence_owner_id = $fence_owner_id,
  record.standalone_crm_task_name = $task_name,
  record.standalone_crm_task_id = $task_id,
  record.standalone_crm_payload_digest = $payload_digest,
  record.standalone_crm_call_intent_id = $call_intent_id,
  record.standalone_crm_authorization_id = $authorization_id,
  record.standalone_crm_authorization_digest = $authorization_digest,
  record.standalone_crm_availability_contract_version = $availability_contract_version,
  record.standalone_crm_frozen_upper_id = $frozen_upper_id
RETURN record.source_record_pk AS source_record_pk
"""
