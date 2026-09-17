"""Additive schema for durable bounded-ingestion control state."""

from __future__ import annotations

CREATE_BOUNDED_INGESTION_SCHEMA: tuple[str, ...] = (
    """CREATE CONSTRAINT ingestion_reset_generation_unique IF NOT EXISTS
FOR (reset:IngestionResetGeneration)
REQUIRE (reset.environment, reset.generation) IS UNIQUE""",
    """CREATE CONSTRAINT bounded_ingestion_scope_key_unique IF NOT EXISTS
FOR (scope:BoundedIngestionScope)
REQUIRE scope.scope_key IS UNIQUE""",
    """CREATE CONSTRAINT bounded_ingestion_logical_key_unique IF NOT EXISTS
FOR (run:IngestionLogicalRun)
REQUIRE run.bounded_logical_key IS UNIQUE""",
    """CREATE CONSTRAINT bounded_ingestion_receipt_identity_unique IF NOT EXISTS
FOR (receipt:BoundedIngestionReceipt)
REQUIRE (receipt.logical_run_id, receipt.replay_id) IS UNIQUE""",
    """CREATE CONSTRAINT bounded_ingestion_retry_identity_unique IF NOT EXISTS
FOR (retry:BoundedIngestionRetry)
REQUIRE (retry.logical_run_id, retry.replay_id, retry.source_record_id) IS UNIQUE""",
    """CREATE INDEX bounded_ingestion_logical_status IF NOT EXISTS
FOR (run:IngestionLogicalRun)
ON (run.bounded_status, run.next_eligible_at)""",
    """CREATE INDEX bounded_ingestion_retry_status IF NOT EXISTS
FOR (retry:BoundedIngestionRetry)
ON (retry.logical_run_id, retry.status, retry.eligible_at)""",
)
