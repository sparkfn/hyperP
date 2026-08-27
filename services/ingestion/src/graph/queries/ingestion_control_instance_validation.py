"""Validation and prospective-identity Cypher for #272 migration."""

from __future__ import annotations

COUNT_INVALID_CONTROL_ROWS = """
MATCH (node)
WHERE (node:IngestRun OR node:IngestionLogicalRun OR node:IngestionCheckpoint
  OR node:BitrixIngestionStream OR node:BitrixDispatchControl OR node:BitrixBackfillGeneration
  OR node:BitrixKnownOwnerRefreshSet OR node:BitrixKnownOwnerRefreshMember
  OR node:BitrixBackfillCoverage OR node:BitrixActivityOwnerRetry
  OR node:BitrixBackfillDispatchOutbox OR node:StageHistoryUnit
  OR node:StageHistoryOccurrence OR node:StageHistoryRetry
  OR node:StageHistoryReviewCommand OR node:StageHistoryUnitAccounting)
  AND (
    node.control_instance_id IS NULL
    OR NOT node.control_instance_id =~ '^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$'
    OR ($legacy_only AND node.control_instance_id <> 'legacy-default')
  )
RETURN count(node) AS invalid
"""

# Every relationship checked here is in the #272 control topology; future census nodes are deliberately excluded.
COUNT_CONTROL_RELATIONSHIP_MISMATCHES = """
CALL {
 MATCH (logical:IngestionLogicalRun)-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]->(run:IngestRun)
 WHERE logical.control_instance_id <> run.control_instance_id RETURN count(*) AS count
 UNION ALL
 MATCH (checkpoint:IngestionCheckpoint)-[:CHECKPOINT_FOR]->(logical:IngestionLogicalRun)
 WHERE checkpoint.control_instance_id <> logical.control_instance_id RETURN count(*) AS count
 UNION ALL
 MATCH (checkpoint:IngestionCheckpoint)-[:PRODUCED_BY]->(run:IngestRun)
 WHERE checkpoint.control_instance_id <> run.control_instance_id RETURN count(*) AS count
 UNION ALL
 MATCH (generation:BitrixBackfillGeneration)-[:HAS_LOGICAL_RUN|HAS_STREAM|HAS_KNOWN_OWNER_SET|HAS_COVERAGE|HAS_OWNER_RETRY]->(child)
 WHERE generation.control_instance_id <> child.control_instance_id RETURN count(*) AS count
 UNION ALL
 MATCH (owner_set:BitrixKnownOwnerRefreshSet)-[:HAS_MEMBER]->(member:BitrixKnownOwnerRefreshMember)
 WHERE owner_set.control_instance_id <> member.control_instance_id RETURN count(*) AS count
 UNION ALL
 MATCH (corrective:BitrixBackfillGeneration)-[:HAS_SUCCESSOR]->(successor:BitrixBackfillGeneration)
 WHERE corrective.control_instance_id <> successor.control_instance_id RETURN count(*) AS count
 UNION ALL
 MATCH (logical:IngestionLogicalRun)-[:HAS_STAGE_HISTORY_UNIT|HAS_STAGE_HISTORY_REVIEW_COMMAND]->(child)
 WHERE logical.control_instance_id <> child.control_instance_id RETURN count(*) AS count
 UNION ALL
 MATCH (unit:StageHistoryUnit)-[:CONTAINS_STAGE_HISTORY_OCCURRENCE|HAS_STAGE_HISTORY_ACCOUNTING]->(child)
 WHERE unit.control_instance_id <> child.control_instance_id RETURN count(*) AS count
 UNION ALL
 MATCH (occurrence:StageHistoryOccurrence)-[:HAS_STAGE_HISTORY_RETRY]->(retry:StageHistoryRetry)
 WHERE occurrence.control_instance_id <> retry.control_instance_id RETURN count(*) AS count
}
RETURN sum(count) AS mismatches
"""

PROSPECTIVE_COLLISIONS = """
CALL {
 MATCH (n:IngestRun) WHERE n.worker_task_id IS NOT NULL
 WITH n.control_instance_id AS control, n.worker_task_id AS key, count(*) AS count
 WHERE count > 1 RETURN count(*) AS collisions
 UNION ALL
 MATCH (n:IngestRun) WHERE n.source_key IS NOT NULL AND n.idempotency_key IS NOT NULL
 WITH n.source_key AS source, n.control_instance_id AS control, n.idempotency_key AS key, count(*) AS count
 WHERE count > 1 RETURN count(*) AS collisions
 UNION ALL
 MATCH (n:IngestionLogicalRun)
 WITH n.source_key AS source, n.control_instance_id AS control, n.idempotency_key AS key, count(*) AS count
 WHERE count > 1 RETURN count(*) AS collisions
 UNION ALL
 MATCH (n:IngestionCheckpoint) WHERE n.logical_run_id IS NOT NULL AND n.phase IS NOT NULL
 WITH n.control_instance_id AS control, n.logical_run_id AS run, n.phase AS phase, count(*) AS count
 WHERE count > 1 RETURN count(*) AS collisions
 UNION ALL
 MATCH (n:IngestionCheckpoint) WHERE n.checkpoint_key IS NOT NULL
 WITH n.control_instance_id AS control, n.checkpoint_key AS key, count(*) AS count
 WHERE count > 1 RETURN count(*) AS collisions
 UNION ALL
 MATCH (n:BitrixIngestionStream)
 WITH n.source_key AS source, n.control_instance_id AS control, n.stream_key AS key, count(*) AS count
 WHERE count > 1 RETURN count(*) AS collisions
 UNION ALL
 MATCH (n:BitrixBackfillGeneration)
 WITH n.control_instance_id AS control, n.generation_id AS key, count(*) AS count
 WHERE count > 1 RETURN count(*) AS collisions
 UNION ALL
 MATCH (n:BitrixKnownOwnerRefreshSet)
 WITH n.control_instance_id AS control, n.generation_id AS generation,
      n.membership_set_id AS membership, count(*) AS count
 WHERE count > 1 RETURN count(*) AS collisions
 UNION ALL
 MATCH (n:BitrixKnownOwnerRefreshMember)
 WITH n.control_instance_id AS control, n.generation_id AS generation,
      n.membership_set_id AS membership, n.deal_id AS deal, count(*) AS count
 WHERE count > 1 RETURN count(*) AS collisions
 UNION ALL
 MATCH (n:BitrixBackfillCoverage)
 WITH n.control_instance_id AS control, n.generation_id AS generation, n.stream_key AS stream,
      n.source_identity AS identity, n.source_boundary AS boundary, count(*) AS count
 WHERE count > 1 RETURN count(*) AS collisions
 UNION ALL
 MATCH (n:BitrixDispatchControl)
 WITH n.source_key AS source, n.control_instance_id AS control, count(*) AS count
 WHERE count > 1 RETURN count(*) AS collisions
 UNION ALL
 MATCH (n:BitrixBackfillDispatchOutbox)
 WITH n.control_instance_id AS control, n.successor_generation_id AS successor, count(*) AS count
 WHERE count > 1 RETURN count(*) AS collisions
 UNION ALL
 MATCH (n:BitrixActivityOwnerRetry)
 WITH n.control_instance_id AS control, n.generation_id AS generation,
      n.source_identity AS identity, n.source_boundary AS boundary, count(*) AS count
 WHERE count > 1 RETURN count(*) AS collisions
}
RETURN coalesce(sum(collisions), 0) AS collisions
"""

COUNT_SOURCE_AMBIGUITIES = """
CALL {
 MATCH (run:IngestRun) OPTIONAL MATCH (run)-[:FROM_SOURCE]->(source:SourceSystem)
 WITH run, collect(DISTINCT source.source_key) AS keys
 WHERE size(keys) > 1 OR (run.source_key IS NOT NULL AND size(keys) = 1 AND run.source_key <> keys[0])
 RETURN count(run) AS count
 UNION ALL
 MATCH (run:IngestRun)<-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]-(logical:IngestionLogicalRun)
 WITH run, count(DISTINCT logical) AS parents
 WHERE parents <> 1
 RETURN count(run) AS count
 UNION ALL
 MATCH (logical:IngestionLogicalRun) OPTIONAL MATCH (logical)-[:FOR_SOURCE]->(source:SourceSystem)
 WITH logical, collect(DISTINCT source.source_key) AS keys
 WHERE size(keys) <> 1 OR (logical.source_key IS NOT NULL AND logical.source_key <> keys[0])
 RETURN count(logical) AS count
 UNION ALL
 MATCH (checkpoint:IngestionCheckpoint)
 WHERE checkpoint.logical_run_id IS NOT NULL AND checkpoint.checkpoint_key IS NOT NULL
 RETURN count(checkpoint) AS count
 UNION ALL
 MATCH (checkpoint:IngestionCheckpoint)
 WHERE checkpoint.logical_run_id IS NOT NULL
 OPTIONAL MATCH (checkpoint)-[:CHECKPOINT_FOR]->(logical:IngestionLogicalRun)
 WITH checkpoint, collect(DISTINCT logical.logical_run_id) AS parents
 WHERE size(parents) <> 1 OR parents[0] <> checkpoint.logical_run_id
 RETURN count(checkpoint) AS count
 UNION ALL
 MATCH (checkpoint:IngestionCheckpoint)-[:CHECKPOINT_FOR]->(logical:IngestionLogicalRun)
 WHERE checkpoint.control_instance_id <> logical.control_instance_id
 RETURN count(checkpoint) AS count
 UNION ALL
 MATCH (checkpoint:IngestionCheckpoint)
 WHERE checkpoint.checkpoint_key IS NOT NULL
 OPTIONAL MATCH (checkpoint)-[:CHECKPOINT_FOR]->(source:SourceSystem)
 WITH checkpoint, collect(DISTINCT source.source_key) AS source_keys
 WHERE size(source_keys) > 1
    OR (checkpoint.source_key IS NULL AND size(source_keys) = 0)
    OR (checkpoint.source_key IS NOT NULL AND size(source_keys) = 1
        AND checkpoint.source_key <> source_keys[0])
 RETURN count(checkpoint) AS count
 UNION ALL
 MATCH (checkpoint:IngestionCheckpoint)-[:PRODUCED_BY]->(run:IngestRun)
 WHERE checkpoint.control_instance_id <> run.control_instance_id
 RETURN count(checkpoint) AS count
 UNION ALL
 MATCH (attempt:IngestRun)
 WHERE attempt.logical_run_id IS NOT NULL
 OPTIONAL MATCH (attempt)<-[:HAS_ATTEMPT|ACTIVE_ATTEMPT]-(logical:IngestionLogicalRun)
 WITH attempt, collect(DISTINCT logical) AS parents
 WHERE size(parents) <> 1
    OR parents[0].logical_run_id <> attempt.logical_run_id
    OR parents[0].control_instance_id <> attempt.control_instance_id
    OR (attempt.source_key IS NOT NULL AND attempt.source_key <> parents[0].source_key)
 RETURN count(attempt) AS count
 UNION ALL
 MATCH (checkpoint:IngestionCheckpoint)
 WHERE checkpoint.logical_run_id IS NOT NULL
 OPTIONAL MATCH (checkpoint)-[:CHECKPOINT_FOR]->(logical:IngestionLogicalRun)
 OPTIONAL MATCH (checkpoint)-[:PRODUCED_BY]->(attempt:IngestRun)
 WITH checkpoint, collect(DISTINCT logical) AS logicals, collect(DISTINCT attempt) AS attempts
 WHERE size(attempts) = 0
    OR any(attempt IN attempts WHERE
      attempt.control_instance_id <> checkpoint.control_instance_id
      OR attempt.logical_run_id <> checkpoint.logical_run_id
      OR (size(logicals) = 1 AND attempt.source_key IS NOT NULL
          AND attempt.source_key <> logicals[0].source_key)
    )
 RETURN count(checkpoint) AS count
 UNION ALL
 MATCH (stream:BitrixIngestionStream)
 WHERE stream.logical_run_id IS NOT NULL
 OPTIONAL MATCH (logical:IngestionLogicalRun {
   logical_run_id: stream.logical_run_id,
   control_instance_id: stream.control_instance_id
 })
 WITH stream, collect(DISTINCT logical) AS logicals
 WHERE size(logicals) <> 1
    OR logicals[0].source_key <> stream.source_key
 RETURN count(stream) AS count
 UNION ALL
 MATCH (stream:BitrixIngestionStream)
 WHERE stream.ingest_run_id IS NOT NULL
 OPTIONAL MATCH (attempt:IngestRun {
   ingest_run_id: stream.ingest_run_id,
   control_instance_id: stream.control_instance_id
 })
 WITH stream, collect(DISTINCT attempt) AS attempts
 WHERE size(attempts) <> 1
    OR attempts[0].logical_run_id <> stream.logical_run_id
 RETURN count(stream) AS count
 UNION ALL
 MATCH (owner_set:BitrixKnownOwnerRefreshSet)
 OPTIONAL MATCH (owner_set)<-[:HAS_KNOWN_OWNER_SET]-(generation:BitrixBackfillGeneration)
 WITH owner_set, collect(DISTINCT generation) AS parents
 WHERE size(parents) <> 1
    OR parents[0].control_instance_id <> owner_set.control_instance_id
    OR parents[0].generation_id <> owner_set.generation_id
 RETURN count(owner_set) AS count
 UNION ALL
 MATCH (member:BitrixKnownOwnerRefreshMember)
 OPTIONAL MATCH (member)<-[:HAS_MEMBER]-(owner_set:BitrixKnownOwnerRefreshSet)
 WITH member, collect(DISTINCT owner_set) AS parents
 WHERE size(parents) <> 1
    OR parents[0].control_instance_id <> member.control_instance_id
    OR parents[0].generation_id <> member.generation_id
    OR parents[0].membership_set_id <> member.membership_set_id
 RETURN count(member) AS count
 UNION ALL
 MATCH (coverage:BitrixBackfillCoverage)
 OPTIONAL MATCH (coverage)<-[:HAS_COVERAGE]-(generation:BitrixBackfillGeneration)
 WITH coverage, collect(DISTINCT generation) AS parents
 WHERE size(parents) <> 1
    OR parents[0].control_instance_id <> coverage.control_instance_id
    OR parents[0].generation_id <> coverage.generation_id
 RETURN count(coverage) AS count
 UNION ALL
 MATCH (retry:BitrixActivityOwnerRetry)
 OPTIONAL MATCH (retry)<-[:HAS_OWNER_RETRY]-(generation:BitrixBackfillGeneration)
 WITH retry, collect(DISTINCT generation) AS parents
 WHERE size(parents) <> 1
    OR parents[0].control_instance_id <> retry.control_instance_id
    OR parents[0].generation_id <> retry.generation_id
 RETURN count(retry) AS count
 UNION ALL
 MATCH (outbox:BitrixBackfillDispatchOutbox)
 OPTIONAL MATCH (successor:BitrixBackfillGeneration {
   control_instance_id: outbox.control_instance_id,
   generation_id: outbox.successor_generation_id
 })
 OPTIONAL MATCH (corrective:BitrixBackfillGeneration {
   control_instance_id: outbox.control_instance_id
 })-[:HAS_SUCCESSOR]->(successor)
 WITH outbox, collect(DISTINCT successor) AS successors,
      collect(DISTINCT corrective) AS correctives
 WHERE size(successors) <> 1 OR size(correctives) <> 1
 RETURN count(outbox) AS count
}
RETURN coalesce(sum(count), 0) AS ambiguities
"""
