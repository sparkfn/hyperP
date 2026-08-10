"""Cypher for Bitrix corrective-generation control and coverage export."""

from __future__ import annotations

CREATE_BITRIX_BACKFILL_CONSTRAINTS = (
    """CREATE CONSTRAINT bitrix_backfill_generation_id_unique IF NOT EXISTS
FOR (generation:BitrixBackfillGeneration)
REQUIRE generation.generation_id IS UNIQUE""",
    """CREATE CONSTRAINT bitrix_known_owner_set_id_unique IF NOT EXISTS
FOR (owner_set:BitrixKnownOwnerRefreshSet)
REQUIRE (owner_set.generation_id, owner_set.membership_set_id) IS UNIQUE""",
    """CREATE CONSTRAINT bitrix_known_owner_member_unique IF NOT EXISTS
FOR (member:BitrixKnownOwnerRefreshMember)
REQUIRE (member.generation_id, member.membership_set_id, member.deal_id) IS UNIQUE""",
    """CREATE CONSTRAINT bitrix_backfill_coverage_identity_unique IF NOT EXISTS
FOR (coverage:BitrixBackfillCoverage)
REQUIRE (coverage.generation_id, coverage.stream_key,
         coverage.source_identity, coverage.source_boundary) IS UNIQUE""",
    """CREATE CONSTRAINT bitrix_backfill_inventory_digest_unique IF NOT EXISTS
FOR (inventory:BitrixBackfillInventory)
REQUIRE inventory.inventory_digest IS UNIQUE""",
    """CREATE CONSTRAINT bitrix_dispatch_control_source_unique IF NOT EXISTS
FOR (control:BitrixDispatchControl)
REQUIRE control.source_key IS UNIQUE""",
    """CREATE CONSTRAINT bitrix_dispatch_outbox_successor_unique IF NOT EXISTS
FOR (outbox:BitrixBackfillDispatchOutbox)
REQUIRE outbox.successor_generation_id IS UNIQUE""",
)

ALLOCATE_BITRIX_BACKFILL_GENERATION = """
MERGE (generation:BitrixBackfillGeneration {generation_id: $generation_id})
ON CREATE SET generation.status = 'allocated',
              generation.repository_sha = $repository_sha,
              generation.image_digest = $image_digest,
              generation.configuration_digest = $configuration_digest,
              generation.source_contract_uuid = $source_contract_uuid,
              generation.boundary_digest = $boundary_digest,
              generation.created_at = datetime(),
              generation.updated_at = datetime(),
              generation.creation_token = $creation_token
WITH generation, generation.creation_token = $creation_token AS created
REMOVE generation.creation_token
WITH generation, created
WHERE created OR (
  generation.repository_sha = $repository_sha
  AND generation.image_digest = $image_digest
  AND generation.configuration_digest = $configuration_digest
  AND generation.source_contract_uuid = $source_contract_uuid
  AND generation.boundary_digest = $boundary_digest
)
RETURN generation.generation_id AS generation_id,
       generation.status AS status,
       created AS created
"""

ATTACH_BACKFILL_LOGICAL_RUN = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status IN ['allocated', 'backfilling', 'activating', 'active']
  AND (
    generation.boundary_digest = $boundary_digest
    OR generation.generation_kind = 'live_successor'
  )
  AND generation.configuration_digest = $configuration_digest
MATCH (logical:IngestionLogicalRun {logical_run_id: $logical_run_id})
MATCH (stream:BitrixIngestionStream {
  source_key: 'bitrix_chat',
  stream_key: $stream_key,
  logical_run_id: $logical_run_id
})
OPTIONAL MATCH (other:BitrixBackfillGeneration)-[:HAS_LOGICAL_RUN]->(logical)
WHERE other.generation_id <> $generation_id
WITH generation, logical, stream, other
WHERE other IS NULL
MERGE (generation)-[:HAS_LOGICAL_RUN {stream_key: $stream_key}]->(logical)
MERGE (generation)-[generation_stream:HAS_STREAM]->(stream)
SET generation_stream.status = 'active',
    generation_stream.logical_run_id = stream.logical_run_id,
    generation_stream.ingest_run_id = stream.ingest_run_id,
    generation_stream.attempt_generation = stream.attempt_generation,
    generation_stream.stream_generation = stream.stream_generation,
    generation_stream.fencing_token = stream.fencing_token,
    generation_stream.attached_at = coalesce(generation_stream.attached_at, datetime()),
    generation_stream.updated_at = datetime(),
    generation.status = CASE
      WHEN generation.generation_kind = 'live_successor' THEN generation.status
      ELSE 'backfilling'
    END,
    generation.updated_at = datetime(),
    logical.bitrix_generation_id = $generation_id,
    logical.bitrix_stream_key = $stream_key
RETURN logical.logical_run_id AS logical_run_id
"""

LIST_KNOWN_OWNER_IDS = """
MATCH (deal:CrmLogicalDeal {source_key: 'bitrix_chat'})
WHERE deal.current_scope_state IN ['in_scope', 'indeterminate']
RETURN deal.deal_id AS deal_id
ORDER BY toInteger(deal.deal_id), deal.deal_id
"""

MATERIALIZE_KNOWN_OWNER_SET = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status IN ['allocated', 'backfilling', 'activating', 'active']
OPTIONAL MATCH (deal:CrmLogicalDeal {source_key: 'bitrix_chat'})
WHERE deal.current_scope_state IN ['in_scope', 'indeterminate']
WITH generation, [id IN collect(deal.deal_id) WHERE id IS NOT NULL] AS unsorted
UNWIND CASE WHEN unsorted = [] THEN [NULL] ELSE unsorted END AS value
WITH generation, value ORDER BY toInteger(value), value
WITH generation, [value IN collect(value) WHERE value IS NOT NULL] AS current_ids
WHERE current_ids = $deal_ids
MERGE (owner_set:BitrixKnownOwnerRefreshSet {
  generation_id: $generation_id,
  membership_set_id: $membership_set_id
})
ON CREATE SET owner_set.digest = $digest,
              owner_set.member_count = size($deal_ids),
              owner_set.created_at = datetime(),
              owner_set.sealed_at = datetime()
WITH generation, owner_set
WHERE owner_set.digest = $digest AND owner_set.member_count = size($deal_ids)
MERGE (generation)-[:HAS_KNOWN_OWNER_SET]->(owner_set)
FOREACH (ordinal IN CASE WHEN $deal_ids = [] THEN [] ELSE range(0, size($deal_ids) - 1) END |
  MERGE (member:BitrixKnownOwnerRefreshMember {
    generation_id: $generation_id,
    membership_set_id: $membership_set_id,
    deal_id: $deal_ids[ordinal]
  })
  ON CREATE SET member.ordinal = ordinal,
                member.created_at = datetime()
  MERGE (owner_set)-[:HAS_MEMBER]->(member)
)
RETURN owner_set.membership_set_id AS membership_set_id,
       owner_set.member_count AS member_count,
       owner_set.digest AS digest
"""

GET_KNOWN_OWNER_SET = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
      -[:HAS_KNOWN_OWNER_SET]->
      (owner_set:BitrixKnownOwnerRefreshSet {membership_set_id: $membership_set_id})
OPTIONAL MATCH (owner_set)-[:HAS_MEMBER]->(member:BitrixKnownOwnerRefreshMember)
WITH owner_set, member ORDER BY member.ordinal
RETURN owner_set.digest AS digest,
       owner_set.member_count AS member_count,
       [value IN collect(member.deal_id) WHERE value IS NOT NULL] AS deal_ids
"""

UPSERT_BITRIX_BACKFILL_COVERAGE = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status IN ['backfilling', 'reconciling', 'activating', 'active']
MATCH (generation)-[:HAS_LOGICAL_RUN]->(logical:IngestionLogicalRun {
  logical_run_id: $logical_run_id
})
MATCH (generation)-[:HAS_STREAM]->(stream:BitrixIngestionStream {
  source_key: 'bitrix_chat',
  stream_key: $stream_key,
  logical_run_id: $logical_run_id,
  ingest_run_id: $ingest_run_id,
  attempt_generation: $attempt_generation,
  stream_generation: $stream_generation,
  fencing_token: $fencing_token,
  status: 'active'
})
MERGE (coverage:BitrixBackfillCoverage {
  generation_id: $generation_id,
  stream_key: $stream_key,
  source_identity: $source_identity,
  source_boundary: $source_boundary
})
ON CREATE SET coverage.created_at = datetime(), coverage.creation_token = $creation_token
WITH generation, coverage, coverage.creation_token = $creation_token AS created
REMOVE coverage.creation_token
WITH generation, coverage, created
WHERE created OR coverage.outcome_digest = $outcome_digest
FOREACH (_ IN CASE WHEN created THEN [1] ELSE [] END |
  SET coverage.disposition = $disposition,
      coverage.source_observation_hash = $source_observation_hash,
      coverage.terminal = $terminal,
      coverage.deal_id = $deal_id,
      coverage.scope_state = $scope_state,
      coverage.entity_key = $entity_key,
      coverage.category_id = $category_id,
      coverage.stage_id = $stage_id,
      coverage.census_epoch = $census_epoch,
      coverage.detail = $detail,
      coverage.outcome_digest = $outcome_digest,
      coverage.updated_at = datetime()
)
MERGE (generation)-[:HAS_COVERAGE]->(coverage)
RETURN coverage.source_identity AS source_identity
"""

GET_BITRIX_COVERAGE_RECONCILIATION = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
MATCH (generation)-[:HAS_LOGICAL_RUN {stream_key: $stream_key}]->
      (logical:IngestionLogicalRun)
MATCH (generation)-[:HAS_COVERAGE]->(coverage:BitrixBackfillCoverage {
  stream_key: $stream_key
})
WITH generation, logical,
     count(coverage) AS coverage_count,
     count(CASE WHEN coverage.terminal THEN 1 END) AS terminal_count,
     count(CASE WHEN coverage.disposition = 'created' THEN 1 END) AS created_count,
     count(CASE WHEN coverage.disposition = 'existing_same_hash' THEN 1 END) AS duplicate_count,
     count(CASE WHEN coverage.disposition = 'updated_projection' THEN 1 END) AS projection_count,
     count(CASE WHEN coverage.disposition = 'scope_unchanged' THEN 1 END) AS unchanged_count,
     count(CASE WHEN coverage.disposition = 'excluded_out_of_scope' THEN 1 END) AS excluded_count,
     count(CASE WHEN coverage.disposition = 'quarantined_owner_unresolved' THEN 1 END)
       AS quarantine_count,
     count(CASE WHEN coverage.disposition = 'conflict' THEN 1 END) AS conflict_count,
     count(CASE WHEN coverage.disposition = 'failed' THEN 1 END) AS failed_count
OPTIONAL MATCH (checkpoint:IngestionCheckpoint {logical_run_id: logical.logical_run_id})
WITH generation, logical, coverage_count, terminal_count, created_count, duplicate_count,
     projection_count, unchanged_count, excluded_count, quarantine_count, conflict_count,
     failed_count, checkpoint
ORDER BY checkpoint.updated_at DESC
WITH generation, logical, coverage_count, terminal_count, created_count, duplicate_count,
     projection_count, unchanged_count, excluded_count, quarantine_count, conflict_count,
     failed_count, collect(checkpoint)[0] AS checkpoint
RETURN generation.status AS generation_status,
       logical.status AS logical_status,
       coverage_count,
       terminal_count,
       created_count,
       duplicate_count,
       projection_count,
       unchanged_count,
       excluded_count,
       quarantine_count,
       conflict_count,
       failed_count,
       checkpoint.committed_count AS checkpoint_committed_count,
       checkpoint.duplicate_count AS checkpoint_duplicate_count,
       checkpoint.excluded_count AS checkpoint_excluded_count,
       checkpoint.retry_count AS checkpoint_retry_count
"""

EXPORT_FROZEN_OWNER_COVERAGE = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status IN ['frozen', 'qualified', 'accepted']
MATCH (deal:CrmLogicalDeal {source_key: 'bitrix_chat', current_scope_state: 'in_scope'})
MATCH (coverage:BitrixBackfillCoverage {
  generation_id: $generation_id,
  stream_key: 'crm_deals'
})
WHERE coverage.deal_id = deal.deal_id
  AND coverage.scope_state = 'in_scope'
  AND coverage.terminal = true
  AND coverage.category_id IS NOT NULL
WITH generation, deal, coverage
ORDER BY coverage.updated_at DESC, coverage.source_boundary DESC
WITH generation, deal, collect(coverage)[0] AS coverage
RETURN deal.deal_id AS deal_id,
       coverage.category_id AS category_id,
       coverage.stage_id AS stage_id,
       coverage.source_observation_hash AS source_observation_hash,
       generation.source_contract_uuid AS source_contract_uuid,
       generation.configuration_digest AS configuration_digest,
       generation.image_digest AS image_digest,
       generation.boundary_digest AS boundary_digest,
       generation.owner_count AS expected_owner_count,
       generation.owner_set_digest AS expected_owner_set_digest
ORDER BY toInteger(coverage.deal_id), coverage.deal_id
"""

REGISTER_BITRIX_BACKFILL_INVENTORY = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status = 'allocated'
MERGE (inventory:BitrixBackfillInventory {inventory_digest: $inventory_digest})
ON CREATE SET inventory.manifest_json = $manifest_json,
              inventory.executed_stream_keys = $executed_stream_keys,
              inventory.reviewed_by = $reviewed_by,
              inventory.backup_id = $backup_id,
              inventory.backup_restore_evidence_digest = $backup_restore_evidence_digest,
              inventory.minimum_fence_image_digest = $minimum_fence_image_digest,
              inventory.created_at = datetime()
WITH generation, inventory
WHERE inventory.manifest_json = $manifest_json
  AND inventory.executed_stream_keys = $executed_stream_keys
MERGE (generation)-[:USES_INVENTORY]->(inventory)
SET generation.inventory_digest = $inventory_digest,
    generation.updated_at = datetime()
RETURN generation.generation_id AS generation_id
"""

GET_BITRIX_BACKFILL_GENERATION = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
OPTIONAL MATCH (generation)-[:HAS_COVERAGE]->(coverage:BitrixBackfillCoverage)
RETURN generation.generation_id AS generation_id,
       generation.status AS status,
       coalesce(generation.generation_kind, 'corrective') AS generation_kind,
       generation.inventory_digest AS inventory_digest,
       generation.corrective_generation_id AS corrective_generation_id,
       toString(generation.frozen_at) AS frozen_at,
       count(coverage) AS material_write_count,
       generation.repository_sha AS repository_sha,
       generation.image_digest AS image_digest,
       generation.configuration_digest AS configuration_digest,
       generation.boundary_digest AS boundary_digest,
       generation.source_contract_uuid AS source_contract_uuid
"""

GET_BITRIX_BACKFILL_INVENTORY = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
      -[:USES_INVENTORY]->(inventory:BitrixBackfillInventory)
RETURN inventory.manifest_json AS manifest_json,
       inventory.inventory_digest AS inventory_digest
"""

CAS_BITRIX_BACKFILL_GENERATION_STATUS = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status IN $expected_statuses
  AND generation.repository_sha = $repository_sha
  AND generation.image_digest = $image_digest
  AND generation.configuration_digest = $configuration_digest
  AND generation.boundary_digest = $boundary_digest
SET generation.status = $next_status,
    generation.transition_evidence_digest = $evidence_digest,
    generation.transitioned_by = $actor,
    generation.updated_at = datetime()
RETURN generation.generation_id AS generation_id,
       generation.status AS status
"""

GET_MAX_BITRIX_RESUME_WORKER_GENERATION = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
OPTIONAL MATCH (generation)-[:HAS_LOGICAL_RUN]->(:IngestionLogicalRun)
               -[:HAS_ATTEMPT]->(attempt:IngestRun)
WITH attempt.worker_task_id AS worker_task_id
WHERE worker_task_id =~ '.*:resume:[0-9]+'
RETURN coalesce(
  max(toInteger(last(split(worker_task_id, ':resume:')))),
  0
) AS max_resume_generation
"""


LIST_BITRIX_GENERATION_LOGICAL_RUNS = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
OPTIONAL MATCH (generation)-[relation:HAS_LOGICAL_RUN]->(logical:IngestionLogicalRun)
OPTIONAL MATCH (generation)-[:HAS_STREAM]->(stream:BitrixIngestionStream {
  logical_run_id: logical.logical_run_id
})
RETURN relation.stream_key AS stream_key,
       logical.logical_run_id AS logical_run_id,
       logical.status AS logical_status,
       logical.active_generation AS attempt_generation,
       stream.status AS stream_status
ORDER BY CASE relation.stream_key
  WHEN 'crm_deals' THEN 0
  WHEN 'crm_activities' THEN 1
  ELSE 2
END
"""

GET_OWNER_COVERAGE_FOR_FREEZE = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status = 'reconciling'
  AND generation.reconciliation_digest = $reconciliation_digest
MATCH (deal:CrmLogicalDeal {source_key: 'bitrix_chat', current_scope_state: 'in_scope'})
MATCH (generation)-[:HAS_COVERAGE]->(coverage:BitrixBackfillCoverage {
  stream_key: 'crm_deals'
})
WHERE coverage.deal_id = deal.deal_id
  AND coverage.scope_state = 'in_scope'
  AND coverage.terminal = true
  AND coverage.category_id IS NOT NULL
WITH deal, coverage
ORDER BY coverage.updated_at DESC, coverage.source_boundary DESC
WITH deal, collect(coverage)[0] AS coverage
RETURN deal.deal_id AS deal_id,
       coverage.category_id AS category_id,
       coverage.stage_id AS stage_id,
       coverage.source_observation_hash AS source_observation_hash
ORDER BY toInteger(coverage.deal_id), coverage.deal_id
"""

FREEZE_BITRIX_BACKFILL_GENERATION = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status = 'reconciling'
  AND generation.repository_sha = $repository_sha
  AND generation.image_digest = $image_digest
  AND generation.configuration_digest = $configuration_digest
  AND generation.boundary_digest = $boundary_digest
MATCH (generation)-[:HAS_LOGICAL_RUN]->(logical:IngestionLogicalRun)
WITH generation, collect(logical) AS logicals
WHERE size(logicals) > 0
  AND all(logical IN logicals WHERE logical.status IN ['completed', 'completed_with_errors'])
MATCH (generation)-[generation_stream:HAS_STREAM]->(stream:BitrixIngestionStream)
SET generation_stream.logical_run_id = coalesce(
      generation_stream.logical_run_id, stream.logical_run_id
    ),
    generation_stream.ingest_run_id = coalesce(
      generation_stream.ingest_run_id, stream.ingest_run_id
    ),
    generation_stream.attempt_generation = coalesce(
      generation_stream.attempt_generation, stream.attempt_generation
    ),
    generation_stream.stream_generation = coalesce(
      generation_stream.stream_generation, stream.stream_generation
    ),
    generation_stream.fencing_token = coalesce(
      generation_stream.fencing_token, stream.fencing_token
    ),
    generation_stream.attached_at = coalesce(
      generation_stream.attached_at, stream.started_at, datetime()
    ),
    generation_stream.updated_at = datetime()
WITH generation, logicals, collect(stream) AS streams,
     collect(generation_stream) AS generation_streams
WHERE size(streams) = size(logicals)
  AND size(generation_streams) = size(logicals)
FOREACH (stream IN streams |
  SET stream.status = 'superseded',
      stream.ended_at = datetime(),
      stream.fence_lock_version = coalesce(stream.fence_lock_version, 0) + 1
)
FOREACH (generation_stream IN generation_streams |
  SET generation_stream.status = 'superseded',
      generation_stream.ended_at = datetime(),
      generation_stream.updated_at = datetime()
)
WITH generation, logicals
UNWIND logicals AS logical
OPTIONAL MATCH (checkpoint:IngestionCheckpoint {logical_run_id: logical.logical_run_id})
SET checkpoint.status = 'archived', checkpoint.archived_at = datetime()
WITH DISTINCT generation
MATCH (deal:CrmLogicalDeal {source_key: 'bitrix_chat', current_scope_state: 'in_scope'})
MATCH (generation)-[:HAS_COVERAGE]->(coverage:BitrixBackfillCoverage {
  stream_key: 'crm_deals'
})
WHERE coverage.deal_id = deal.deal_id
  AND coverage.scope_state = 'in_scope'
  AND coverage.terminal = true
  AND coverage.category_id IS NOT NULL
WITH generation, deal, coverage
ORDER BY coverage.updated_at DESC, coverage.source_boundary DESC
WITH generation, deal, collect(coverage)[0] AS coverage
RETURN generation.generation_id AS generation_id,
       deal.deal_id AS deal_id,
       coverage.category_id AS category_id,
       coverage.stage_id AS stage_id,
       coverage.source_observation_hash AS source_observation_hash
ORDER BY toInteger(deal_id), deal_id
"""

RECORD_BITRIX_QUALIFICATION = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status = 'frozen'
  AND generation.repository_sha = $repository_sha
  AND generation.image_digest = $image_digest
  AND generation.configuration_digest = $configuration_digest
  AND generation.boundary_digest = $boundary_digest
SET generation.status = 'qualified',
    generation.owner_artifact_id = $owner_artifact_id,
    generation.stage_artifact_id = $stage_artifact_id,
    generation.owner_recommendation = $owner_recommendation,
    generation.stage_recommendation = $stage_recommendation,
    generation.qualification_evidence_digest = $qualification_evidence_digest,
    generation.qualified_at = datetime(),
    generation.updated_at = datetime()
RETURN generation.generation_id AS generation_id
"""

REJECT_BITRIX_BACKFILL_GENERATION = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status IN ['allocated', 'backfilling', 'reconciling', 'frozen', 'qualified']
SET generation.status = 'rejected',
    generation.rejected_by = $actor,
    generation.rejection_reason = $reason,
    generation.rejection_remediation = $remediation,
    generation.rejected_at = datetime(),
    generation.updated_at = datetime()
MERGE (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat'})
SET dispatch.blocked = true,
    dispatch.block_reason = 'rejected_generation',
    dispatch.blocked_generation_id = $generation_id,
    dispatch.updated_at = datetime()
WITH generation
OPTIONAL MATCH (generation)-[generation_stream:HAS_STREAM]->(stream:BitrixIngestionStream)
SET generation_stream.logical_run_id = coalesce(
      generation_stream.logical_run_id, stream.logical_run_id
    ),
    generation_stream.ingest_run_id = coalesce(
      generation_stream.ingest_run_id, stream.ingest_run_id
    ),
    generation_stream.attempt_generation = coalesce(
      generation_stream.attempt_generation, stream.attempt_generation
    ),
    generation_stream.stream_generation = coalesce(
      generation_stream.stream_generation, stream.stream_generation
    ),
    generation_stream.fencing_token = coalesce(
      generation_stream.fencing_token, stream.fencing_token
    ),
    generation_stream.attached_at = coalesce(
      generation_stream.attached_at, stream.started_at, datetime()
    ),
    stream.status = 'superseded',
    stream.ended_at = datetime(),
    stream.fence_lock_version = coalesce(stream.fence_lock_version, 0) + 1,
    generation_stream.status = 'superseded',
    generation_stream.ended_at = datetime(),
    generation_stream.updated_at = datetime()
RETURN DISTINCT generation.generation_id AS generation_id
"""

ALLOCATE_BITRIX_SUCCESSOR_GENERATION = """
MATCH (corrective:BitrixBackfillGeneration {generation_id: $corrective_generation_id})
WHERE corrective.status = 'accepted'
MERGE (successor:BitrixBackfillGeneration {generation_id: $successor_generation_id})
ON CREATE SET successor.status = 'allocated',
              successor.generation_kind = 'live_successor',
              successor.corrective_generation_id = $corrective_generation_id,
              successor.repository_sha = corrective.repository_sha,
              successor.image_digest = corrective.image_digest,
              successor.configuration_digest = corrective.configuration_digest,
              successor.source_contract_uuid = corrective.source_contract_uuid,
              successor.boundary_digest = $successor_boundary_digest,
              successor.inventory_digest = corrective.inventory_digest,
              successor.reconciliation_digest = corrective.reconciliation_digest,
              successor.created_at = datetime(),
              successor.updated_at = datetime(),
              successor.creation_token = $creation_token
WITH corrective, successor, successor.creation_token = $creation_token AS created
REMOVE successor.creation_token
WITH corrective, successor, created
WHERE created OR (
  successor.generation_kind = 'live_successor'
  AND successor.corrective_generation_id = $corrective_generation_id
  AND successor.configuration_digest = corrective.configuration_digest
  AND successor.boundary_digest = $successor_boundary_digest
)
MERGE (corrective)-[:HAS_SUCCESSOR]->(successor)
RETURN successor.generation_id AS generation_id, created AS created
"""

ACTIVATE_BITRIX_SUCCESSOR_GENERATION = """
MATCH (corrective:BitrixBackfillGeneration {generation_id: $corrective_generation_id})
      -[:HAS_SUCCESSOR]->
      (successor:BitrixBackfillGeneration {generation_id: $successor_generation_id})
WHERE corrective.status = 'accepted'
  AND successor.status IN ['allocated', 'activating']
MERGE (outbox:BitrixBackfillDispatchOutbox {
  successor_generation_id: $successor_generation_id
})
ON CREATE SET outbox.status = 'pending',
              outbox.evidence_digest = $evidence_digest,
              outbox.occurrence = $occurrence,
              outbox.inventory_digest = successor.inventory_digest,
              outbox.created_at = datetime()
WITH successor, outbox
WHERE outbox.evidence_digest = $evidence_digest
  AND outbox.occurrence = $occurrence
OPTIONAL MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat'})
WITH successor, outbox, dispatch
WHERE dispatch IS NULL
   OR dispatch.blocked = false
   OR dispatch.blocked_generation_id = $corrective_generation_id
SET successor.status = 'activating',
    successor.activated_by = $actor,
    successor.activation_evidence_digest = $evidence_digest,
    successor.updated_at = datetime()
MERGE (pending_dispatch:BitrixDispatchControl {source_key: 'bitrix_chat'})
SET pending_dispatch.blocked = true,
    pending_dispatch.block_reason = 'successor_publication_pending',
    pending_dispatch.blocked_generation_id = $corrective_generation_id,
    pending_dispatch.updated_at = datetime()
RETURN successor.generation_id AS generation_id
"""

VERIFY_BITRIX_SUCCESSOR_TAIL = """
MATCH (corrective:BitrixBackfillGeneration {generation_id: $corrective_generation_id})
      -[:HAS_SUCCESSOR]->
      (successor:BitrixBackfillGeneration {generation_id: $successor_generation_id})
OPTIONAL MATCH (corrective)-[old_relation:HAS_STREAM]->(:BitrixIngestionStream)
WITH corrective, successor, collect(old_relation) AS old_relations
MATCH (successor)-[:USES_INVENTORY]->(inventory:BitrixBackfillInventory)
OPTIONAL MATCH (successor)-[relation:HAS_LOGICAL_RUN]->(logical:IngestionLogicalRun)
WITH corrective, successor, old_relations, inventory.executed_stream_keys AS expected_streams,
     collect(logical) AS live_runs,
     [key IN collect(relation.stream_key) WHERE key IS NOT NULL | key]
       AS actual_streams
OPTIONAL MATCH (successor)-[:HAS_COVERAGE]->(coverage:BitrixBackfillCoverage)
RETURN corrective.status AS corrective_status,
       successor.status AS successor_status,
       size(old_relations) > 0
         AND all(relation IN old_relations WHERE relation.status = 'superseded')
         AS predecessor_frozen,
       expected_streams,
       actual_streams,
       size(live_runs) AS cadence_run_count,
       all(run IN live_runs WHERE run.status IN ['completed', 'completed_with_errors'])
         AS cadence_complete,
       count(coverage) AS successor_coverage_count,
       all(item IN collect(coverage) WHERE item.terminal = true
         AND item.disposition NOT IN ['conflict', 'failed'])
         AND all(expected IN expected_streams
           WHERE expected IN [item IN collect(coverage) | item.stream_key])
         AS coverage_complete
"""

RECORD_BITRIX_BACKFILL_RECONCILIATION = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status IN ['backfilling', 'reconciling']
  AND generation.repository_sha = $repository_sha
  AND generation.image_digest = $image_digest
  AND generation.configuration_digest = $configuration_digest
  AND generation.boundary_digest = $boundary_digest
UNWIND $stream_keys AS requested_stream
MATCH (generation)-[:HAS_LOGICAL_RUN {stream_key: requested_stream}]->
      (logical:IngestionLogicalRun)
WHERE logical.status IN ['completed', 'completed_with_errors']
MATCH (generation)-[:HAS_COVERAGE]->(coverage:BitrixBackfillCoverage {
  stream_key: requested_stream
})
WITH generation, requested_stream, logical,
     count(coverage) AS coverage_count,
     count(CASE WHEN coverage.terminal THEN 1 END) AS terminal_count,
     count(CASE WHEN coverage.disposition = 'created' THEN 1 END) AS created_count,
     count(CASE WHEN coverage.disposition = 'existing_same_hash' THEN 1 END)
       AS duplicate_count,
     count(CASE WHEN coverage.disposition = 'updated_projection' THEN 1 END)
       AS projection_count,
     count(CASE WHEN coverage.disposition = 'scope_unchanged' THEN 1 END)
       AS unchanged_count,
     count(CASE WHEN coverage.disposition = 'excluded_out_of_scope' THEN 1 END)
       AS excluded_count,
     count(CASE WHEN coverage.disposition = 'quarantined_owner_unresolved' THEN 1 END)
       AS quarantine_count,
     count(CASE WHEN coverage.disposition = 'conflict' THEN 1 END) AS conflict_count,
     count(CASE WHEN coverage.disposition = 'failed' THEN 1 END) AS failed_count
CALL (logical) {
  OPTIONAL MATCH (checkpoint:IngestionCheckpoint {logical_run_id: logical.logical_run_id})
  WITH checkpoint ORDER BY checkpoint.updated_at DESC
  RETURN collect(checkpoint)[0] AS checkpoint
}
WITH generation, requested_stream, coverage_count, terminal_count, created_count,
     duplicate_count, projection_count, unchanged_count, excluded_count,
     quarantine_count, conflict_count, failed_count, checkpoint,
     created_count + duplicate_count + projection_count + unchanged_count
       + excluded_count + quarantine_count + conflict_count + failed_count AS accounted
WHERE coverage_count = terminal_count
  AND terminal_count = accounted
  AND conflict_count = 0
  AND failed_count = 0
  AND checkpoint.committed_count = created_count + projection_count
  AND checkpoint.duplicate_count = duplicate_count + unchanged_count
  AND checkpoint.excluded_count = excluded_count + quarantine_count + conflict_count
  AND checkpoint.retry_count = failed_count
WITH generation, collect(requested_stream) AS verified_streams
WHERE size(verified_streams) = size($stream_keys)
MATCH (generation)-[:HAS_STREAM]->(stream:BitrixIngestionStream)
WHERE stream.stream_key IN $stream_keys
SET stream.fence_lock_version = coalesce(stream.fence_lock_version, 0) + 1
WITH generation, verified_streams, collect(stream) AS streams
WHERE size(streams) = size($stream_keys)
SET generation.status = 'reconciling',
    generation.reconciliation_digest = $reconciliation_digest,
    generation.reconciled_by = $actor,
    generation.reconciled_at = datetime(),
    generation.updated_at = datetime()
RETURN generation.generation_id AS generation_id,
       verified_streams
"""

COMPLETE_BITRIX_BACKFILL_FREEZE = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status = 'reconciling'
  AND generation.reconciliation_digest = $reconciliation_digest
SET generation.status = 'frozen',
    generation.owner_count = $owner_count,
    generation.owner_set_digest = $owner_set_digest,
    generation.frozen_at = datetime(),
    generation.updated_at = datetime()
RETURN generation.generation_id AS generation_id
"""

CONFIRM_BITRIX_SUCCESSOR_PUBLICATION = """
MATCH (corrective:BitrixBackfillGeneration {generation_id: $corrective_generation_id})
      -[:HAS_SUCCESSOR]->
      (successor:BitrixBackfillGeneration {generation_id: $successor_generation_id})
MATCH (outbox:BitrixBackfillDispatchOutbox {
  successor_generation_id: $successor_generation_id,
  evidence_digest: $evidence_digest
})
WHERE corrective.status = 'accepted'
  AND successor.status = 'activating'
OPTIONAL MATCH (dispatch:BitrixDispatchControl {source_key: 'bitrix_chat'})
WITH corrective, successor, outbox, dispatch
WHERE dispatch IS NULL
   OR dispatch.blocked = false
   OR dispatch.blocked_generation_id = $corrective_generation_id
SET successor.status = 'active',
    successor.scheduling_enabled = true,
    successor.activated_at = datetime(),
    successor.updated_at = datetime(),
    outbox.status = 'published',
    outbox.canvas_id = $canvas_id,
    outbox.published_at = datetime()
MERGE (active_dispatch:BitrixDispatchControl {source_key: 'bitrix_chat'})
SET active_dispatch.blocked = false,
    active_dispatch.block_reason = NULL,
    active_dispatch.blocked_generation_id = NULL,
    active_dispatch.unblocked_by = $actor,
    active_dispatch.updated_at = datetime()
RETURN successor.generation_id AS generation_id
"""

GET_ACTIVE_BITRIX_SUCCESSOR_SCHEDULE = """
MATCH (successor:BitrixBackfillGeneration {
  generation_kind: 'live_successor',
  status: 'active',
  scheduling_enabled: true
})-[:USES_INVENTORY]->(inventory:BitrixBackfillInventory)
RETURN successor.generation_id AS generation_id,
       successor.configuration_digest AS configuration_digest,
       inventory.manifest_json AS manifest_json
ORDER BY successor.activated_at DESC
LIMIT 1
"""

RECORD_BITRIX_ACTIVITY_OWNER_RETRY = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status IN ['backfilling', 'reconciling', 'activating', 'active']
MATCH (generation)-[:HAS_STREAM]->(stream:BitrixIngestionStream {
  stream_key: 'crm_activities',
  logical_run_id: $logical_run_id,
  ingest_run_id: $ingest_run_id,
  attempt_generation: $attempt_generation,
  stream_generation: $stream_generation,
  fencing_token: $fencing_token,
  status: 'active'
})
OPTIONAL MATCH (generation)-[:HAS_OWNER_RETRY]->(
  reviewed_owner_retry:BitrixActivityOwnerRetry {
    generation_id: $generation_id,
    owner_deal_id: $owner_deal_id,
    status: 'reviewed_excluded'
  }
)
WHERE reviewed_owner_retry IS NULL
   OR reviewed_owner_retry.source_identity <> $source_identity
   OR reviewed_owner_retry.source_boundary <> $source_boundary
WITH generation,
     stream,
     min(reviewed_owner_retry.review_evidence_digest) AS reviewed_owner_evidence_digest
MERGE (retry:BitrixActivityOwnerRetry {
  generation_id: $generation_id,
  source_identity: $source_identity,
  source_boundary: $source_boundary
})
ON CREATE SET retry.created_at = datetime(),
              retry.attempt_count = 0,
              retry.status = 'retryable'
SET retry.owner_deal_id = $owner_deal_id,
    retry.owner_state = $owner_state,
    retry.status = CASE
      WHEN retry.status = 'reviewed_excluded' THEN retry.status
      WHEN reviewed_owner_evidence_digest IS NOT NULL THEN 'reviewed_excluded'
      ELSE 'retryable'
    END,
    retry.review_basis_evidence_digest = CASE
      WHEN reviewed_owner_evidence_digest IS NOT NULL THEN reviewed_owner_evidence_digest
      ELSE retry.review_basis_evidence_digest
    END,
    retry.review_reused_at = CASE
      WHEN reviewed_owner_evidence_digest IS NOT NULL THEN datetime()
      ELSE retry.review_reused_at
    END,
    retry.attempt_count = retry.attempt_count + 1,
    retry.updated_at = datetime()
MERGE (generation)-[:HAS_OWNER_RETRY]->(retry)
RETURN retry.attempt_count AS attempt_count,
       retry.status AS status
"""

RESOLVE_BITRIX_ACTIVITY_OWNER_RETRY = """
MATCH (retry:BitrixActivityOwnerRetry {
  generation_id: $generation_id,
  source_identity: $source_identity,
  source_boundary: $source_boundary,
  status: 'retryable'
})
SET retry.status = $resolution,
    retry.resolved_at = datetime(),
    retry.updated_at = datetime()
RETURN retry.source_identity AS source_identity
"""
