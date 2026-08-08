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
WHERE generation.status IN ['allocated', 'backfilling']
  AND generation.boundary_digest = $boundary_digest
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
MERGE (generation)-[:HAS_STREAM]->(stream)
SET generation.status = 'backfilling',
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
WHERE generation.status IN ['allocated', 'backfilling']
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
WHERE generation.status IN ['backfilling', 'reconciling']
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
MATCH (coverage:BitrixBackfillCoverage {
  generation_id: $generation_id,
  stream_key: 'crm_deals'
})
WHERE coverage.scope_state = 'in_scope'
  AND coverage.terminal = true
  AND coverage.deal_id IS NOT NULL
  AND coverage.category_id IS NOT NULL
RETURN coverage.deal_id AS deal_id,
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
