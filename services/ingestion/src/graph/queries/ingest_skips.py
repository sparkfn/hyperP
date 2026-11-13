"""Cypher queries for recording and reconciling IngestSkip ledger entries.

``RECORD_INGEST_REFRESH_UNIT`` accounts one known-owner refresh unit inside the
unit's own domain transaction.  One refresh attempt is shared by the whole run
and each unit is counted once, when the attempt's marker relationship to the
phase-scoped coverage unit is newly created.
"""

WRITE_INGEST_SKIPS = """
UNWIND $rows AS row
MERGE (s:IngestSkip {skip_id: row.skip_id})
ON CREATE SET
    s.ingest_run_id = row.ingest_run_id,
    s.attempt_key = row.attempt_key,
    s.source_system = row.source_system,
    s.stage = row.stage,
    s.reason = row.reason,
    s.source_record_id = row.source_record_id,
    s.locator = row.locator,
    s.created_at = datetime()
WITH s, row
MATCH (ir:IngestRun {ingest_run_id: row.ingest_run_id, control_instance_id: $control_instance_id})
MERGE (s)-[:SKIPPED_IN]->(ir)
"""

WRITE_INGEST_ATTEMPT = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id, control_instance_id: $control_instance_id})
MERGE (attempt:IngestRunAttempt {
    ingest_run_id: $ingest_run_id,
    attempt_key: $attempt_key
})
SET attempt.emitted_count = $emitted_count,
    attempt.persisted_count = $persisted_count,
    attempt.duplicate_count = $duplicate_count,
    attempt.pipeline_skip_count = $pipeline_skip_count,
    attempt.connector_skip_count = $connector_skip_count,
    attempt.record_error_count = $record_error_count,
    attempt.updated_at = datetime()
MERGE (attempt)-[:ATTEMPT_OF]->(ir)
"""

RECORD_INGEST_REFRESH_UNIT = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id, control_instance_id: $control_instance_id})
MERGE (attempt:IngestRunAttempt {
    ingest_run_id: $ingest_run_id,
    attempt_key: $attempt_key
})
ON CREATE SET
    attempt.emitted_count = 0,
    attempt.persisted_count = 0,
    attempt.duplicate_count = 0,
    attempt.pipeline_skip_count = 0,
    attempt.connector_skip_count = 0,
    attempt.record_error_count = 0,
    attempt.accounting_scope = 'known_owner_refresh_v1',
    attempt.created_at = datetime()
SET attempt.updated_at = datetime()
MERGE (attempt)-[:ATTEMPT_OF]->(ir)
MATCH (coverage:BitrixBackfillCoverage {
    control_instance_id: $control_instance_id,
    generation_id: $generation_id,
    stream_key: $stream_key,
    source_identity: $source_identity,
    source_boundary: $source_boundary
})
WITH ir, attempt, coverage
OPTIONAL MATCH (attempt)-[accounted:ACCOUNTED_UNIT]->(coverage)
WITH ir, attempt, coverage, count(accounted) AS accounted_units
FOREACH (_ IN CASE WHEN accounted_units = 0 THEN [1] ELSE [] END |
    SET attempt.emitted_count = coalesce(attempt.emitted_count, 0) + 1,
        attempt.persisted_count = coalesce(attempt.persisted_count, 0) + $persisted_count,
        attempt.duplicate_count = coalesce(attempt.duplicate_count, 0) + $duplicate_count,
        attempt.pipeline_skip_count = coalesce(attempt.pipeline_skip_count, 0) + $pipeline_skip_count,
        attempt.record_error_count = coalesce(attempt.record_error_count, 0) + $record_error_count)
MERGE (attempt)-[:ACCOUNTED_UNIT]->(coverage)
SET ir.skip_ledger_reconciled = false
RETURN attempt.emitted_count AS emitted_count
"""

RECONCILE_INGEST_RUN_ACCOUNTING = """
MATCH (ir:IngestRun {ingest_run_id: $ingest_run_id, control_instance_id: $control_instance_id})
CALL (ir) {
    OPTIONAL MATCH (attempt:IngestRunAttempt)-[:ATTEMPT_OF]->(ir)
    RETURN coalesce(sum(attempt.emitted_count), 0) AS emitted_count,
           coalesce(sum(attempt.persisted_count), 0) AS persisted_count,
           coalesce(sum(attempt.duplicate_count), 0) AS duplicate_count,
           coalesce(sum(attempt.pipeline_skip_count), 0) AS pipeline_skip_count,
           coalesce(sum(attempt.connector_skip_count), 0) AS connector_skip_count,
           coalesce(sum(attempt.record_error_count), 0) AS record_error_count
}
CALL (ir) {
    OPTIONAL MATCH (attempt:IngestRunAttempt)-[:ATTEMPT_OF]->(ir)
    WITH ir, collect(attempt.attempt_key) AS attempt_keys
    OPTIONAL MATCH (skip:IngestSkip {ingest_run_id: ir.ingest_run_id})
    WHERE skip.attempt_key IN attempt_keys
    RETURN count(skip) AS stored_skips
}
WITH ir, emitted_count, persisted_count, duplicate_count, pipeline_skip_count,
     connector_skip_count, record_error_count, stored_skips,
     pipeline_skip_count + connector_skip_count AS expected_skips,
     emitted_count = persisted_count + duplicate_count + pipeline_skip_count + record_error_count
         AND stored_skips = pipeline_skip_count + connector_skip_count AS reconciled
SET ir.emitted_count = emitted_count,
    ir.persisted_count = persisted_count,
    ir.duplicate_count = duplicate_count,
    ir.pipeline_skip_count = pipeline_skip_count,
    ir.connector_skip_count = connector_skip_count,
    ir.record_error_count = record_error_count,
    ir.skip_ledger_reconciled = reconciled
RETURN emitted_count, persisted_count, duplicate_count, pipeline_skip_count,
       connector_skip_count, record_error_count, stored_skips, expected_skips, reconciled
"""
