"""CAS-only metadata queries for issue #310 repair control."""

from __future__ import annotations

CREATE_CRM_DEAL_REPAIR_CONTROL_SCHEMA: tuple[str, ...] = (
    "CREATE CONSTRAINT crm_deal_repair_control_run_unique IF NOT EXISTS FOR (n:CrmDealRepairControl) REQUIRE n.run_id IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_publication_reservation_unique IF NOT EXISTS FOR (n:CrmDealRepairPublicationReservation) REQUIRE (n.control_instance_id, n.publication_key) IS UNIQUE",
    "CREATE CONSTRAINT crm_deal_repair_allocation_completion_unique IF NOT EXISTS FOR (n:CrmDealRepairAllocationCompletion) REQUIRE (n.run_id, n.completion_id) IS UNIQUE",
    "CREATE INDEX crm_deal_repair_control_state IF NOT EXISTS FOR (n:CrmDealRepairControl) ON (n.state, n.control_instance_id, n.revision)",
)

READ_COMPLETED_CLAIM_REPLAY = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id, status: 'qualified',
  boundary_digest: $boundary_digest, control_instance_id: $control_instance_id, execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, repair_id: $repair_id,
  control_instance_id: $control_instance_id, owner_id: $owner_id, token_digest: $token_digest,
  boundary_digest: $boundary_digest, claim_expected_revision: $expected_revision})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id: $control_instance_id,
  blocked: true, repair_run_id: $run_id, repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: control.revision})
WHERE control.state IN ['quiesced', 'allocated']
RETURN control.control_instance_id AS control_instance_id, control.run_id AS run_id,
       control.owner_id AS owner_id, control.token_digest AS token_digest, control.revision AS revision,
       control.state AS state, control.boundary_digest AS boundary_digest
"""


CLAIM_REPAIR_DISPATCH = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id, status: 'qualified',
  boundary_digest: $boundary_digest, control_instance_id: $control_instance_id, execution_allowed: false})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id: $control_instance_id})
SET dispatch.repair_cas_lock = coalesce(dispatch.repair_cas_lock, 0) + 1
WITH run, dispatch
OPTIONAL MATCH (reservation:CrmDealRepairPublicationReservation {control_instance_id: $control_instance_id})
WHERE reservation.state IN ['preparing', 'publishing']
WITH run, dispatch, count(reservation) AS unsettled
OPTIONAL MATCH (existing:CrmDealRepairControl {run_id: $run_id})
WITH run, dispatch, unsettled, existing
WHERE unsettled = 0
  AND (
    (existing IS NULL AND coalesce(dispatch.blocked, false) = false
      AND coalesce(dispatch.repair_revision, 0) = $expected_revision)
    OR
    (existing IS NOT NULL AND existing.owner_id = $owner_id AND existing.token_digest = $token_digest
      AND existing.state = 'quiescing'
      AND dispatch.repair_run_id = $run_id AND dispatch.repair_owner_id = $owner_id
      AND dispatch.repair_token_digest = $token_digest
      AND (
        (existing.revision = $expected_revision AND dispatch.repair_revision = $expected_revision)
        OR (existing.revision = $expected_revision + 1
          AND dispatch.repair_revision = $expected_revision + 1)
      ))
  )
MERGE (control:CrmDealRepairControl {run_id: $run_id})
ON CREATE SET control.repair_id = $repair_id, control.control_instance_id = $control_instance_id,
  control.owner_id = $owner_id, control.token_digest = $token_digest, control.boundary_digest = $boundary_digest,
  control.state = 'quiescing', control.revision = $expected_revision + 1,
  control.claim_expected_revision = $expected_revision, control.created_at = datetime()
WITH dispatch, control, existing
WHERE control.repair_id = $repair_id AND control.control_instance_id = $control_instance_id
  AND control.owner_id = $owner_id AND control.token_digest = $token_digest AND control.boundary_digest = $boundary_digest
SET control.revision = CASE
  WHEN existing IS NULL THEN control.revision
  WHEN existing.revision = $expected_revision THEN control.revision + 1
  ELSE control.revision
END,
    control.state = CASE WHEN existing.state IN ['quiesced', 'allocated'] THEN existing.state
      ELSE 'quiescing' END,
    control.updated_at = datetime(),
    dispatch.blocked = true, dispatch.block_reason = 'crm_deal_identity_repair_quiesce',
    dispatch.repair_run_id = $run_id, dispatch.repair_owner_id = $owner_id, dispatch.repair_token_digest = $token_digest,
    dispatch.repair_revision = control.revision, dispatch.updated_at = datetime()
RETURN control.control_instance_id AS control_instance_id, control.run_id AS run_id,
       control.owner_id AS owner_id, control.token_digest AS token_digest, control.revision AS revision,
       control.state AS state, control.boundary_digest AS boundary_digest
"""

LOCK_REPAIR_TOPOLOGY = """
UNWIND $captures AS captured
MATCH (stream:BitrixIngestionStream {source_key: 'bitrix_chat',
  control_instance_id: $control_instance_id, stream_key: captured.stream_key,
  logical_run_id: captured.logical_run_id, ingest_run_id: captured.ingest_run_id,
  attempt_generation: captured.attempt_generation, stream_generation: captured.stream_generation,
  fencing_token: captured.fencing_token, status: 'active'})
MATCH (logical:IngestionLogicalRun {logical_run_id: captured.logical_run_id,
  control_instance_id: $control_instance_id, status: 'stop_requested'})
MATCH (attempt:IngestRun {ingest_run_id: captured.ingest_run_id,
  control_instance_id: $control_instance_id, generation: captured.attempt_generation,
  status: captured.attempt_status})
MATCH (generation:BitrixBackfillGeneration {control_instance_id: $control_instance_id})
  -[fence:HAS_STREAM]->(stream)
WHERE any(captured_fence IN captured.fences WHERE captured_fence.generation_id = generation.generation_id
  AND captured_fence.stream_generation = fence.stream_generation
  AND captured_fence.fencing_token = fence.fencing_token)
WITH stream, logical, attempt, fence
ORDER BY stream.stream_key, stream.logical_run_id, stream.attempt_generation, fence.fencing_token
SET stream.repair_quiescence_lock = coalesce(stream.repair_quiescence_lock, 0) + 1,
    logical.repair_quiescence_lock = coalesce(logical.repair_quiescence_lock, 0) + 1,
    attempt.repair_quiescence_lock = coalesce(attempt.repair_quiescence_lock, 0) + 1,
    fence.repair_quiescence_lock = coalesce(fence.repair_quiescence_lock, 0) + 1
REMOVE stream.repair_quiescence_lock, logical.repair_quiescence_lock,
  attempt.repair_quiescence_lock, fence.repair_quiescence_lock
RETURN count(DISTINCT stream) AS locked_stream_count, count(DISTINCT fence) AS locked_fence_count
"""


COMPLETE_QUIESCENCE = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id, status: 'qualified',
  boundary_digest: $boundary_digest, control_instance_id: $control_instance_id, execution_allowed: false})
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token_digest: $token_digest,
  revision: $expected_revision, state: 'quiescing', boundary_digest: $boundary_digest})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id: $control_instance_id,
  blocked: true, repair_run_id: $run_id, repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: $expected_revision})
MATCH (capture:CrmDealRepairTopologyCapture {run_id: $run_id, control_instance_id: $control_instance_id,
  topology_digest: $topology_digest})
SET dispatch.repair_commit_lock = coalesce(dispatch.repair_commit_lock, 0) + 1
WITH run, control, dispatch, capture
OPTIONAL MATCH (stale:IngestRun {ingest_run_id: $stale_run_id})
WITH run, control, dispatch, capture, stale
WHERE capture.captures_json = $topology_json
  AND NOT EXISTS {
    MATCH (reservation:CrmDealRepairPublicationReservation {control_instance_id: $control_instance_id})
    WHERE reservation.state IN ['preparing', 'publishing']
  }
  AND size($captures) = COUNT {
    MATCH (stream:BitrixIngestionStream {
      source_key: 'bitrix_chat', control_instance_id: $control_instance_id, status: 'active'
    })
    WHERE stream.stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
  }
  AND all(captured IN $captures WHERE EXISTS {
    MATCH (stream:BitrixIngestionStream {source_key: 'bitrix_chat',
      control_instance_id: $control_instance_id, stream_key: captured.stream_key,
      logical_run_id: captured.logical_run_id, ingest_run_id: captured.ingest_run_id,
      attempt_generation: captured.attempt_generation, stream_generation: captured.stream_generation,
      fencing_token: captured.fencing_token, status: 'active'})
    MATCH (logical:IngestionLogicalRun {logical_run_id: captured.logical_run_id,
      control_instance_id: $control_instance_id, status: 'stop_requested'})
    MATCH (attempt:IngestRun {ingest_run_id: captured.ingest_run_id,
      control_instance_id: $control_instance_id, generation: captured.attempt_generation,
      status: captured.attempt_status})
    WHERE size(captured.checkpoint_ids) = COUNT {
        MATCH (:IngestionCheckpoint {
          control_instance_id: $control_instance_id, logical_run_id: captured.logical_run_id
        })
      }
      AND all(checkpoint_id IN captured.checkpoint_ids WHERE EXISTS {
        MATCH (:IngestionCheckpoint {control_instance_id: $control_instance_id,
          logical_run_id: captured.logical_run_id, checkpoint_id: checkpoint_id})
      })
      AND size(captured.continuation_ids) = size([(logical)-[:HAS_CONTINUATION|CONTINUES_AS]->
        (continuation:IngestionLogicalRun {control_instance_id: $control_instance_id}) | continuation])
      AND all(continuation_id IN captured.continuation_ids WHERE EXISTS {
        MATCH (logical)-[:HAS_CONTINUATION|CONTINUES_AS]->(:IngestionLogicalRun {
          control_instance_id: $control_instance_id, logical_run_id: continuation_id
        })
      })
      AND size(captured.fences) = size([(generation:BitrixBackfillGeneration {
        control_instance_id: $control_instance_id})-[fence:HAS_STREAM]->(stream) | fence])
      AND all(captured_fence IN captured.fences WHERE EXISTS {
        MATCH (:BitrixBackfillGeneration {control_instance_id: $control_instance_id,
          generation_id: captured_fence.generation_id})-[fence:HAS_STREAM]->(stream)
        WHERE fence.stream_generation = captured_fence.stream_generation
          AND fence.fencing_token = captured_fence.fencing_token
      })
  })
  AND size($publications) = COUNT {
    MATCH (:CrmDealRepairPublicationReservation {control_instance_id: $control_instance_id})
  }
  AND all(publication IN $publications WHERE EXISTS {
    MATCH (reservation:CrmDealRepairPublicationReservation {
      control_instance_id: $control_instance_id, publication_key: publication.publication_key,
      reservation_id: publication.reservation_id, state: publication.state, revision: publication.revision
    })
    WHERE reservation.workflow_task_id = publication.workflow_task_id
  })
  AND (
    ($stale_snapshot.state = 'absent' AND stale IS NULL)
    OR (
      $stale_snapshot.state = 'orphan'
      AND stale IS NOT NULL
      AND (
        (stale.control_instance_id IS NULL AND $stale_snapshot.control_instance_id IS NULL)
        OR stale.control_instance_id = $stale_snapshot.control_instance_id
      )
          AND stale.status = $stale_snapshot.status
          AND NOT EXISTS { MATCH (:IngestionLogicalRun)-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(stale) }
          AND NOT EXISTS { MATCH (:IngestionCheckpoint)-[:PRODUCED_BY]->(stale) }
          AND NOT EXISTS { MATCH (:BitrixIngestionStream {ingest_run_id: $stale_run_id}) }
    )
    OR (
      $stale_snapshot.state = 'owned'
      AND stale IS NOT NULL
      AND stale.control_instance_id = $control_instance_id
      AND stale.status = $stale_snapshot.status
      AND size($stale_snapshot.logical_run_ids) = size([(logical:IngestionLogicalRun {
        control_instance_id: $control_instance_id})-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(stale) | logical])
      AND all(logical_run_id IN $stale_snapshot.logical_run_ids WHERE EXISTS {
        MATCH (:IngestionLogicalRun {control_instance_id: $control_instance_id,
          logical_run_id: logical_run_id})-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(stale)
      })
      AND size($stale_snapshot.checkpoint_ids) = COUNT {
        MATCH (:IngestionCheckpoint {control_instance_id: $control_instance_id})-[:PRODUCED_BY]->(stale)
      }
      AND all(checkpoint_id IN $stale_snapshot.checkpoint_ids WHERE EXISTS {
        MATCH (:IngestionCheckpoint {control_instance_id: $control_instance_id,
          checkpoint_id: checkpoint_id})-[:PRODUCED_BY]->(stale)
      })
      AND NOT EXISTS {
        MATCH (foreign:IngestionLogicalRun)-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(stale)
        WHERE foreign.control_instance_id <> $control_instance_id
      }
      AND NOT EXISTS {
        MATCH (foreign:IngestionCheckpoint)-[:PRODUCED_BY]->(stale)
        WHERE foreign.control_instance_id <> $control_instance_id
      }
      AND size($stale_snapshot.streams) = COUNT {
        MATCH (:BitrixIngestionStream {ingest_run_id: $stale_run_id})
      }
      AND all(captured_stream IN $stale_snapshot.streams WHERE EXISTS {
        MATCH (:BitrixIngestionStream {source_key: 'bitrix_chat',
          control_instance_id: $control_instance_id, stream_key: captured_stream.stream_key,
          logical_run_id: captured_stream.logical_run_id, ingest_run_id: $stale_run_id,
          attempt_generation: captured_stream.attempt_generation,
          stream_generation: captured_stream.stream_generation,
          fencing_token: captured_stream.fencing_token, status: captured_stream.status})
      })
      AND all(continuation IN $stale_snapshot.continuations WHERE EXISTS {
            MATCH (logical:IngestionLogicalRun {logical_run_id: continuation.logical_run_id,
              control_instance_id: $control_instance_id})
            WHERE size(continuation.continuation_ids) = size([(logical)-[:HAS_CONTINUATION|CONTINUES_AS]->
              (next:IngestionLogicalRun {control_instance_id: $control_instance_id}) | next])
              AND all(continuation_id IN continuation.continuation_ids WHERE EXISTS {
                MATCH (logical)-[:HAS_CONTINUATION|CONTINUES_AS]->(:IngestionLogicalRun {
                  control_instance_id: $control_instance_id, logical_run_id: continuation_id
                })
              })
          })
    )
  )
CALL {
  WITH control
  UNWIND $captures AS captured
  MATCH (stream:BitrixIngestionStream {source_key: 'bitrix_chat',
    control_instance_id: $control_instance_id, stream_key: captured.stream_key,
    logical_run_id: captured.logical_run_id, ingest_run_id: captured.ingest_run_id,
    attempt_generation: captured.attempt_generation, stream_generation: captured.stream_generation,
    fencing_token: captured.fencing_token, status: 'active'})
  MATCH (logical:IngestionLogicalRun {logical_run_id: captured.logical_run_id,
    control_instance_id: $control_instance_id, status: 'stop_requested'})
  MATCH (attempt:IngestRun {ingest_run_id: captured.ingest_run_id,
    control_instance_id: $control_instance_id, generation: captured.attempt_generation,
    status: captured.attempt_status})
  SET stream.status = 'superseded', stream.finished_at = datetime(), stream.updated_at = datetime(),
      logical.status = 'paused_with_checkpoint', logical.updated_at = datetime(),
      attempt.status = 'failed', attempt.failure_category = 'crm_deal_identity_repair_quiesce',
      attempt.finished_at = datetime()
  RETURN count(stream) AS superseded_count
}
WITH control, dispatch, stale, superseded_count
WHERE superseded_count = size($captures)
FOREACH (_ IN CASE WHEN $stale_snapshot.state IN ['orphan', 'owned'] THEN [1] ELSE [] END |
  SET stale.status = 'failed', stale.failure_category = 'crm_deal_identity_repair_quiesce',
      stale.failure_message = 'sealed stale run terminalized by repair control', stale.finished_at = datetime(),
      stale.repair_control_run_id = $run_id
)
SET control.state = 'quiesced', control.revision = control.revision + 1,
    control.proof_payload_json = $proof_payload_json, control.proof_digest = $proof_digest,
    control.proof_hmac = $proof_hmac, control.proof_expires_at = $proof_expires_at,
    control.topology_digest = $topology_digest,
    control.updated_at = datetime(),
    dispatch.repair_revision = control.revision, dispatch.updated_at = datetime()
RETURN control.control_instance_id AS control_instance_id, control.run_id AS run_id,
       control.owner_id AS owner_id, control.token_digest AS token_digest, control.revision AS revision,
       control.state AS state, control.boundary_digest AS boundary_digest,
       control.proof_payload_json AS proof_payload_json, control.proof_digest AS proof_digest,
       control.proof_hmac AS proof_hmac, control.proof_expires_at AS proof_expires_at
"""

REQUEST_REPAIR_TOPOLOGY_STOP = """
MATCH (:BitrixBackfillGeneration {control_instance_id: $control_instance_id})
  -[membership:HAS_LOGICAL_RUN]->(logical:IngestionLogicalRun {source_key: 'bitrix_chat',
    control_instance_id: $control_instance_id})
WHERE membership.stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
  AND logical.status IN ['queued', 'running', 'stop_requested', 'paused_with_checkpoint']
WITH DISTINCT logical
OPTIONAL MATCH (logical)-[:HAS_CONTINUATION|CONTINUES_AS]->
  (continuation:IngestionLogicalRun {control_instance_id: $control_instance_id})
WITH logical, collect(continuation) AS continuations
ORDER BY logical.logical_run_id
SET logical.status = CASE WHEN logical.status IN ['queued', 'running', 'paused_with_checkpoint'] THEN 'stop_requested' ELSE logical.status END,
    logical.stop_requested_at = coalesce(logical.stop_requested_at, datetime()),
    logical.stop_requested_by = coalesce(logical.stop_requested_by, $owner_id),
    logical.stop_reason = coalesce(logical.stop_reason, 'crm_deal_identity_repair_quiesce'),
    logical.updated_at = datetime()
FOREACH (continuation IN continuations |
  SET continuation.status = CASE WHEN continuation.status IN ['queued', 'running', 'paused_with_checkpoint']
      THEN 'stop_requested' ELSE continuation.status END,
      continuation.stop_requested_at = coalesce(continuation.stop_requested_at, datetime()),
      continuation.stop_requested_by = coalesce(continuation.stop_requested_by, $owner_id),
      continuation.stop_reason = coalesce(continuation.stop_reason, 'crm_deal_identity_repair_quiesce'),
      continuation.updated_at = datetime()
)
RETURN count(DISTINCT logical) AS stopped_count
"""

READ_REPAIR_TOPOLOGY_SNAPSHOT = """
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id: $control_instance_id})
CALL {
  WITH dispatch
  MATCH (:BitrixBackfillGeneration {control_instance_id: $control_instance_id})
    -[membership:HAS_LOGICAL_RUN]->(logical:IngestionLogicalRun {source_key: 'bitrix_chat',
      control_instance_id: $control_instance_id})
  WHERE membership.stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
    AND logical.status IN ['queued', 'running', 'stop_requested', 'paused_with_checkpoint']
  WITH DISTINCT logical
  OPTIONAL MATCH (logical)-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->
    (matched_attempt:IngestRun {control_instance_id: $control_instance_id})
  WITH logical, collect(DISTINCT matched_attempt) AS attempts
  UNWIND CASE WHEN size(attempts) = 1 THEN attempts ELSE [NULL] END AS attempt
  OPTIONAL MATCH (stream:BitrixIngestionStream {source_key: 'bitrix_chat',
    control_instance_id: $control_instance_id, logical_run_id: logical.logical_run_id,
    ingest_run_id: attempt.ingest_run_id, attempt_generation: attempt.generation, status: 'active'})
  WHERE stream.stream_key IN ['crm_deals', 'crm_activities', 'openlines_conversations']
  CALL {
    WITH logical
    OPTIONAL MATCH (checkpoint:IngestionCheckpoint {control_instance_id: $control_instance_id,
      logical_run_id: logical.logical_run_id})
    RETURN collect(checkpoint.checkpoint_id) AS checkpoint_ids
  }
  RETURN collect({
    stream_key: stream.stream_key, logical_run_id: logical.logical_run_id,
    ingest_run_id: attempt.ingest_run_id, attempt_generation: attempt.generation,
    stream_generation: stream.stream_generation, fencing_token: stream.fencing_token,
    attempt_status: attempt.status,
    checkpoint_ids: checkpoint_ids,
    continuation_ids: [(logical)-[:HAS_CONTINUATION|CONTINUES_AS]->(continuation:IngestionLogicalRun {
      control_instance_id: $control_instance_id}) | continuation.logical_run_id],
    fences: [(generation:BitrixBackfillGeneration {control_instance_id: $control_instance_id})
      -[fence:HAS_STREAM]->(stream) | {
        generation_id: generation.generation_id, stream_generation: fence.stream_generation,
        fencing_token: fence.fencing_token
      }]
  }) AS captures
}
CALL {
  WITH dispatch
  OPTIONAL MATCH (reservation:CrmDealRepairPublicationReservation {
    control_instance_id: $control_instance_id
  })
  RETURN [item IN collect(CASE WHEN reservation IS NULL THEN NULL ELSE {
    publication_key: reservation.publication_key, reservation_id: reservation.reservation_id,
    state: reservation.state, revision: reservation.revision, workflow_task_id: reservation.workflow_task_id
  } END) WHERE item IS NOT NULL] AS publications
}
CALL {
  WITH dispatch
  OPTIONAL MATCH (stale:IngestRun {ingest_run_id: $stale_run_id})
  WITH collect(stale) AS stale_runs
  WITH stale_runs, CASE WHEN size(stale_runs) = 0 THEN NULL ELSE stale_runs[0] END AS stale
  WITH stale, CASE
    WHEN size(stale_runs) = 0 THEN 'absent'
    WHEN size(stale_runs) > 1 THEN 'ambiguous'
    WHEN NOT EXISTS { MATCH (:IngestionLogicalRun)-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(stale) }
      AND NOT EXISTS { MATCH (:IngestionCheckpoint)-[:PRODUCED_BY]->(stale) } THEN 'orphan'
    WHEN stale.control_instance_id = $control_instance_id THEN 'owned'
    ELSE 'ambiguous'
  END AS state
  CALL {
    WITH stale
    OPTIONAL MATCH (checkpoint:IngestionCheckpoint {control_instance_id: $control_instance_id})
      -[:PRODUCED_BY]->(stale)
    RETURN collect(checkpoint.checkpoint_id) AS checkpoint_ids
  }
  CALL {
    WITH stale
    OPTIONAL MATCH (stream:BitrixIngestionStream {ingest_run_id: $stale_run_id})
    RETURN [item IN collect(CASE WHEN stream IS NULL THEN NULL ELSE {
      stream_key: stream.stream_key, logical_run_id: stream.logical_run_id,
      attempt_generation: stream.attempt_generation, stream_generation: stream.stream_generation,
      fencing_token: stream.fencing_token, status: stream.status
    } END) WHERE item IS NOT NULL] AS streams
  }
  RETURN {
    state: state, control_instance_id: stale.control_instance_id, status: stale.status,
    logical_run_ids: [(logical:IngestionLogicalRun {control_instance_id: $control_instance_id})
      -[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(stale) | logical.logical_run_id],
    checkpoint_ids: checkpoint_ids,
    streams: streams,
    continuations: [(logical:IngestionLogicalRun {control_instance_id: $control_instance_id})
      -[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(stale) | {
        logical_run_id: logical.logical_run_id,
        continuation_ids: [(logical)-[:HAS_CONTINUATION|CONTINUES_AS]->
          (continuation:IngestionLogicalRun {control_instance_id: $control_instance_id}) |
            continuation.logical_run_id]
      }]
  } AS stale
}
RETURN captures, publications, stale
"""

SUPERSEDE_REPAIR_TOPOLOGY = """
UNWIND $captures AS capture
MATCH (stream:BitrixIngestionStream {source_key: 'bitrix_chat', control_instance_id: $control_instance_id,
  stream_key: capture.stream_key, logical_run_id: capture.logical_run_id, ingest_run_id: capture.ingest_run_id,
  attempt_generation: capture.attempt_generation, stream_generation: capture.stream_generation,
  fencing_token: capture.fencing_token, status: 'active'})
MATCH (logical:IngestionLogicalRun {logical_run_id: capture.logical_run_id,
  control_instance_id: $control_instance_id, status: 'stop_requested'})
MATCH (attempt:IngestRun {ingest_run_id: capture.ingest_run_id, control_instance_id: $control_instance_id,
  generation: capture.attempt_generation})
SET stream.status = 'superseded', stream.finished_at = datetime(), stream.updated_at = datetime(),
    logical.status = 'paused_with_checkpoint', logical.updated_at = datetime(),
    attempt.status = 'failed', attempt.failure_category = 'crm_deal_identity_repair_quiesce',
    attempt.finished_at = datetime()
RETURN count(stream) AS superseded_count
"""

STORE_REPAIR_TOPOLOGY_CAPTURE = """
MATCH (control:CrmDealRepairControl {run_id: $run_id, control_instance_id: $control_instance_id,
  state: 'quiescing'})
MERGE (capture:CrmDealRepairTopologyCapture {run_id: $run_id, topology_digest: $topology_digest})
ON CREATE SET capture.control_instance_id = $control_instance_id, capture.captures_json = $captures_json,
  capture.created_at = datetime()
WITH capture
WHERE capture.control_instance_id = $control_instance_id AND capture.captures_json = $captures_json
RETURN capture.captures_json AS captures_json
"""

READ_REPAIR_TOPOLOGY_CAPTURE = """
MATCH (capture:CrmDealRepairTopologyCapture {run_id: $run_id, topology_digest: $topology_digest,
  control_instance_id: $control_instance_id})
RETURN capture.captures_json AS captures_json
"""

PAUSE_REPAIR_CONTROL = """
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token_digest: $token_digest})
WHERE (control.revision = $expected_revision AND control.state IN ['quiesced', 'allocated'])
   OR (control.revision = $expected_revision + 1 AND control.state = 'paused'
       AND control.last_transition = 'pause'
       AND control.last_transition_expected_revision = $expected_revision)
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id: control.control_instance_id,
  blocked: true, repair_run_id: $run_id, repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: control.revision})
FOREACH (_ IN CASE WHEN control.revision = $expected_revision THEN [1] ELSE [] END |
  SET control.paused_from_state = control.state, control.state = 'paused',
      control.revision = control.revision + 1, control.last_transition = 'pause',
      control.last_transition_expected_revision = $expected_revision, control.updated_at = datetime(),
      dispatch.repair_revision = control.revision, dispatch.updated_at = datetime()
)
RETURN control.control_instance_id AS control_instance_id, control.run_id AS run_id,
       control.owner_id AS owner_id, control.token_digest AS token_digest, control.revision AS revision,
       control.state AS state, control.boundary_digest AS boundary_digest
"""

RESUME_REPAIR_CONTROL = """
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token_digest: $token_digest})
WHERE (control.revision = $expected_revision AND control.state = 'paused'
       AND control.paused_from_state IN ['quiesced', 'allocated'])
   OR (control.revision = $expected_revision + 1 AND control.state IN ['quiesced', 'allocated']
       AND control.last_transition = 'resume'
       AND control.last_transition_expected_revision = $expected_revision)
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id: control.control_instance_id,
  blocked: true, repair_run_id: $run_id, repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: control.revision})
FOREACH (_ IN CASE WHEN control.revision = $expected_revision THEN [1] ELSE [] END |
  SET control.state = control.paused_from_state, control.paused_from_state = NULL,
      control.revision = control.revision + 1, control.last_transition = 'resume',
      control.last_transition_expected_revision = $expected_revision, control.updated_at = datetime(),
      dispatch.repair_revision = control.revision, dispatch.updated_at = datetime()
)
RETURN control.control_instance_id AS control_instance_id, control.run_id AS run_id,
       control.owner_id AS owner_id, control.token_digest AS token_digest, control.revision AS revision,
       control.state AS state, control.boundary_digest AS boundary_digest
"""

SEAL_QUIESCENCE_BOUNDARY = """
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token_digest: $token_digest,
  state: 'quiesced', revision: $revision})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id: control.control_instance_id,
  blocked: true, repair_run_id: $run_id, repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: $revision})
SET control.sealed_boundary_digest = $sealed_boundary_digest,
    control.sealed_source_records_digest = $sealed_source_records_digest,
    control.sealed_source_instance_digest = $sealed_source_instance_digest,
    control.sealed_stale_run_evidence_digest = $sealed_stale_run_evidence_digest,
    control.sealed_control_digest = $sealed_control_digest,
    control.sealed_inventory_digest = $sealed_inventory_digest,
    control.sealed_inventory_row_count = $sealed_inventory_row_count,
    control.sealed_eligible_unit_count = $sealed_eligible_unit_count,
    control.sealed_negative_control_count = $sealed_negative_control_count,
    control.updated_at = datetime()
RETURN control.run_id AS run_id
"""


READ_ALLOCATION_SEALED_BOUNDARY = """
MATCH (control:CrmDealRepairControl {run_id: $run_id})
RETURN control.sealed_boundary_digest AS boundary_digest,
       control.sealed_source_records_digest AS source_records_digest,
       control.sealed_source_instance_digest AS source_instance_digest,
       control.sealed_stale_run_evidence_digest AS stale_run_evidence_digest,
       control.sealed_control_digest AS control_digest,
       control.sealed_inventory_digest AS inventory_digest,
       control.sealed_inventory_row_count AS inventory_row_count,
       control.sealed_eligible_unit_count AS eligible_unit_count,
       control.sealed_negative_control_count AS negative_control_count
"""


READ_ALLOCATION_REPLAY = """
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id,
  token_digest: $token_digest})
MATCH (completion:CrmDealRepairAllocationCompletion {run_id: $run_id,
  request_action: 'allocate', request_expected_revision: $expected_revision,
  request_digest: $request_digest})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: control.control_instance_id, blocked: true, repair_run_id: $run_id,
  repair_owner_id: $owner_id, repair_token_digest: $token_digest,
  repair_revision: control.revision})
WHERE control.state = 'allocated'
RETURN control.control_instance_id AS control_instance_id, control.run_id AS run_id,
       control.owner_id AS owner_id, control.token_digest AS token_digest, control.revision AS revision,
       control.state AS state, control.boundary_digest AS boundary_digest
"""


ALLOCATE_REPAIR_UNITS = """
MATCH (run:CrmDealRepairRun {repair_id: $repair_id, run_id: $run_id, status: 'qualified',
  boundary_digest: $boundary_digest, execution_allowed: false})
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id: run.control_instance_id})
SET dispatch.repair_allocation_lock = coalesce(dispatch.repair_allocation_lock, 0) + 1
WITH run, dispatch
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token_digest: $token_digest,
  revision: $expected_revision, boundary_digest: $boundary_digest})
WHERE dispatch.blocked = true
  AND dispatch.repair_run_id = $run_id
  AND dispatch.repair_owner_id = $owner_id
  AND dispatch.repair_token_digest = $token_digest
  AND dispatch.repair_revision = $expected_revision
  AND control.control_instance_id = run.control_instance_id
  AND control.state IN ['quiesced', 'allocated']
  AND control.proof_digest = $proof_digest AND control.proof_expires_at > datetime()
  AND run.inventory_digest = $actual_inventory_digest
  AND run.inventory_row_count = $actual_inventory_row_count
  AND run.eligible_unit_count = $actual_eligible_unit_count
  AND run.negative_control_count = $actual_negative_control_count
  AND NOT EXISTS { MATCH (reservation:CrmDealRepairPublicationReservation {control_instance_id: control.control_instance_id})
                   WHERE reservation.state IN ['preparing', 'publishing'] }
OPTIONAL MATCH (prior:CrmDealRepairAllocationCompletion {run_id: $run_id})
WITH control, dispatch, collect(prior) AS prior_completions
WHERE size(prior_completions) = 0 OR (
  size(prior_completions) = 1
  AND prior_completions[0].completion_id = $completion_id
  AND prior_completions[0].boundary_digest = $boundary_digest
  AND prior_completions[0].overlay_digest = $overlay_digest
  AND prior_completions[0].allocation_digest = $allocation_digest
  AND prior_completions[0].unit_count = $unit_count
  AND prior_completions[0].request_action = 'allocate'
  AND prior_completions[0].request_expected_revision = $expected_revision
  AND prior_completions[0].request_digest = $request_digest
)
MERGE (completion:CrmDealRepairAllocationCompletion {run_id: $run_id, completion_id: $completion_id})
ON CREATE SET completion.boundary_digest = $boundary_digest, completion.overlay_digest = $overlay_digest,
  completion.allocation_digest = $allocation_digest, completion.unit_count = $unit_count,
  completion.request_action = 'allocate', completion.request_expected_revision = $expected_revision,
  completion.request_digest = $request_digest, completion.created_at = datetime()
WITH control, dispatch, completion
WHERE completion.boundary_digest = $boundary_digest AND completion.overlay_digest = $overlay_digest
  AND completion.allocation_digest = $allocation_digest AND completion.unit_count = $unit_count
  AND completion.request_action = 'allocate'
  AND completion.request_expected_revision = $expected_revision
  AND completion.request_digest = $request_digest
CALL (control) {
  WITH control
  UNWIND $units AS unit
  MERGE (allocated:CrmDealRepairUnit {run_id: $run_id, unit_id: unit.unit_id})
  ON CREATE SET allocated += unit, allocated.created_at = datetime()
  WITH allocated, unit
  WHERE allocated.generation = unit.generation
    AND allocated.sequence = unit.sequence
    AND allocated.attempt = unit.attempt
    AND allocated.boundary_digest = unit.boundary_digest
    AND allocated.inventory_fingerprint = unit.inventory_fingerprint
    AND allocated.state = unit.state
    AND allocated.inventory_key = unit.inventory_key
    AND allocated.source_record_pk = unit.source_record_pk
    AND allocated.inventory_graph_fingerprint = unit.inventory_graph_fingerprint
    AND allocated.inventory_stored_payload_fingerprint = unit.inventory_stored_payload_fingerprint
    AND allocated.inventory_binding_digest = unit.inventory_binding_digest
  RETURN count(allocated) AS stored_count
}
WITH control, dispatch, completion, stored_count
OPTIONAL MATCH (stored:CrmDealRepairUnit {run_id: $run_id})
WITH control, dispatch, completion, stored_count, collect(stored) AS stored_units
WHERE stored_count = $unit_count
  AND size(stored_units) = $unit_count
  AND all(stored IN stored_units WHERE stored.unit_id IN $unit_ids)
SET control.state = 'allocated',
    control.revision = CASE WHEN control.state = 'quiesced' THEN control.revision + 1 ELSE control.revision END,
    control.updated_at = datetime(),
    dispatch.repair_revision = control.revision, dispatch.updated_at = datetime()
RETURN control.control_instance_id AS control_instance_id, control.run_id AS run_id,
       control.owner_id AS owner_id, control.token_digest AS token_digest, control.revision AS revision,
       control.state AS state, control.boundary_digest AS boundary_digest
"""

READ_REPAIR_CONTROL_STATUS = """
OPTIONAL MATCH (run:CrmDealRepairRun {repair_id: $repair_id})
OPTIONAL MATCH (control:CrmDealRepairControl {run_id: run.run_id})
OPTIONAL MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat',
  control_instance_id: run.control_instance_id})
OPTIONAL MATCH (completion:CrmDealRepairAllocationCompletion {run_id: run.run_id})
WITH run, control, dispatch, collect(completion) AS completions
RETURN run.run_id AS run_id, run.status AS qualification_status, control.state AS control_state,
       dispatch.blocked AS dispatch_blocked, dispatch.repair_revision AS dispatch_revision,
       CASE WHEN control.proof_digest IS NULL THEN 'not_quiesced' ELSE 'quiesced' END
         AS quiescence_state,
       CASE WHEN size(completions) = 0 THEN 'not_allocated' ELSE 'allocated' END
         AS allocation_state,
       control.paused_from_state AS paused_from_state,
       CASE WHEN size(completions) = 0 THEN NULL ELSE completions[0].unit_count END
         AS allocated_unit_count
"""

READ_REPAIR_CONTROL_PROOF = """
MATCH (control:CrmDealRepairControl {run_id: $run_id, owner_id: $owner_id, token_digest: $token_digest})
WHERE control.proof_expires_at > datetime()
  AND (
    (control.revision = $revision AND control.state IN ['quiesced', 'allocated'])
    OR (
      control.state = 'allocated'
      AND EXISTS {
        MATCH (:CrmDealRepairAllocationCompletion {run_id: $run_id,
          request_action: 'allocate', request_expected_revision: $revision})
      }
    )
  )
RETURN control.proof_digest AS proof_digest
"""

PREPARE_REPAIR_AWARE_PUBLICATION = """
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id: $control_instance_id})
SET dispatch.repair_publication_lock = coalesce(dispatch.repair_publication_lock, 0) + 1
WITH dispatch
WHERE coalesce(dispatch.blocked, false) = false
MERGE (reservation:CrmDealRepairPublicationReservation {
  control_instance_id: $control_instance_id, publication_key: $publication_key
})
ON CREATE SET reservation.reservation_id = $reservation_id, reservation.state = 'preparing',
  reservation.revision = 1, reservation.created_at = datetime()
WITH reservation
WHERE reservation.reservation_id = $reservation_id AND reservation.state = 'preparing'
RETURN reservation.reservation_id AS reservation_id, reservation.control_instance_id AS control_instance_id,
       reservation.publication_key AS publication_key, reservation.state AS state, reservation.revision AS revision
"""

MARK_REPAIR_AWARE_PUBLISHING = """
MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat', control_instance_id: $control_instance_id,
  blocked: false})
MATCH (reservation:CrmDealRepairPublicationReservation {control_instance_id: $control_instance_id,
  publication_key: $publication_key, reservation_id: $reservation_id, state: 'preparing', revision: $expected_revision})
SET reservation.state = 'publishing', reservation.revision = reservation.revision + 1, reservation.updated_at = datetime()
RETURN reservation.reservation_id AS reservation_id, reservation.control_instance_id AS control_instance_id,
       reservation.publication_key AS publication_key, reservation.state AS state, reservation.revision AS revision
"""

CONFIRM_REPAIR_AWARE_PUBLICATION = """
MATCH (reservation:CrmDealRepairPublicationReservation {control_instance_id: $control_instance_id,
  publication_key: $publication_key, reservation_id: $reservation_id, state: 'publishing', revision: $expected_revision})
SET reservation.state = 'confirmed', reservation.revision = reservation.revision + 1,
    reservation.workflow_task_id = $workflow_task_id, reservation.confirmed_at = datetime()
RETURN reservation.reservation_id AS reservation_id, reservation.control_instance_id AS control_instance_id,
       reservation.publication_key AS publication_key, reservation.state AS state, reservation.revision AS revision
"""
