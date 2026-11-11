"""Cypher queries for recording and reconciling IngestSkip ledger entries."""

WRITE_INGEST_SKIPS = """
UNWIND $rows AS row
MERGE (s:IngestSkip {skip_id: row.skip_id})
ON CREATE SET
    s.ingest_run_id = row.ingest_run_id,
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

COUNT_INGEST_SKIPS = """
MATCH (s:IngestSkip {ingest_run_id: $ingest_run_id})
RETURN count(s) AS skip_count
"""
