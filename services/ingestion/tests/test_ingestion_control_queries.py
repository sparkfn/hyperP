"""Structural regression coverage for logical-run control Cypher."""

from __future__ import annotations

from src.graph.queries.ingestion_control import (
    ADVANCE_BITRIX_UNIT_CHECKPOINT,
    ADVANCE_LOGICAL_CHECKPOINT,
    CLAIM_QUEUED_ATTEMPT,
    CREATE_LOGICAL_RUN_AND_ATTEMPT,
    CREATE_LOGICAL_RUN_CONSTRAINTS,
    CREATE_RESUME_ATTEMPT,
    FAIL_LOGICAL_RUN,
    FINALIZE_LOGICAL_RUN,
    PAUSE_LOGICAL_RUN,
    REQUEST_LOGICAL_RUN_STOP,
    TRANSITION_LOGICAL_PHASE,
)


def test_control_plane_defines_logical_run_and_checkpoint_constraints() -> None:
    schema = "\n".join(CREATE_LOGICAL_RUN_CONSTRAINTS)

    assert "IngestionLogicalRun" in schema
    assert "IngestionCheckpoint" in schema
    assert "logical_run_id" in schema
    assert "ingestion_checkpoint_control_logical_phase_unique" in schema
    assert "ingestion_logical_run_source_control_idempotency_unique" in schema
    assert "ingestion_checkpoint_identity_unique" not in schema
    assert "ingestion_logical_run_source_idempotency_unique" not in schema


def test_checkpoint_writes_are_fenced_by_attempt_and_generation() -> None:
    assert "logical.active_generation = $generation" in ADVANCE_LOGICAL_CHECKPOINT
    assert "attempt.generation = $generation" in ADVANCE_LOGICAL_CHECKPOINT
    assert "checkpoint.generation = $generation" in ADVANCE_LOGICAL_CHECKPOINT
    assert "cursor_json = $cursor_json" in ADVANCE_LOGICAL_CHECKPOINT
    assert "checkpoint.status = 'active'" in ADVANCE_LOGICAL_CHECKPOINT
    assert "checkpoint.connector_version = $connector_version" in ADVANCE_LOGICAL_CHECKPOINT
    assert "checkpoint.source_window_json = $source_window_json" in ADVANCE_LOGICAL_CHECKPOINT
    assert "SET checkpoint.cursor_json" in ADVANCE_LOGICAL_CHECKPOINT
    assert "SET checkpoint.source_window_json" not in ADVANCE_LOGICAL_CHECKPOINT


def test_bitrix_unit_checkpoint_is_bound_to_the_full_stream_fence() -> None:
    for field in (
        "logical_run_id",
        "ingest_run_id",
        "attempt_generation",
        "stream_generation",
        "fencing_token",
    ):
        assert f"{field}: ${field}" in ADVANCE_BITRIX_UNIT_CHECKPOINT
    assert "checkpoint.source_window_json = $source_window_json" in (ADVANCE_BITRIX_UNIT_CHECKPOINT)
    assert "checkpoint.committed_count" in ADVANCE_BITRIX_UNIT_CHECKPOINT


def test_stop_and_pause_have_distinct_durable_states() -> None:
    assert "logical.status = 'stop_requested'" in REQUEST_LOGICAL_RUN_STOP
    assert "logical.status = 'paused_with_checkpoint'" in PAUSE_LOGICAL_RUN
    assert "checkpoint.status = 'paused'" in PAUSE_LOGICAL_RUN


def test_resume_increments_generation_and_supersedes_stale_attempt() -> None:
    assert "logical.active_generation + 1 AS generation" in CREATE_RESUME_ATTEMPT
    assert "'superseded'" in CREATE_RESUME_ATTEMPT
    assert "resumed_from_run_id" in CREATE_RESUME_ATTEMPT
    assert "OPTIONAL MATCH (logical)-[active_relation:ACTIVE_ATTEMPT]" in CREATE_RESUME_ATTEMPT
    assert "OPTIONAL MATCH (logical)-[:HAS_ATTEMPT]->(historical_prior:IngestRun {" in (
        CREATE_RESUME_ATTEMPT
    )
    assert "control_instance_id: $control_instance_id" in CREATE_RESUME_ATTEMPT
    assert "coalesce(active_prior, latest_prior) AS prior" in CREATE_RESUME_ATTEMPT
    assert "CASE WHEN active_relation IS NULL THEN [] ELSE [active_relation] END" in (
        CREATE_RESUME_ATTEMPT
    )
    assert "logical.connector_version = $logical_connector_version" in CREATE_RESUME_ATTEMPT
    assert "checkpoint.connector_version = $checkpoint_connector_version" in CREATE_RESUME_ATTEMPT
    source_match = CREATE_RESUME_ATTEMPT.index("MATCH (logical)-[:FOR_SOURCE]->(source")
    generation_write = CREATE_RESUME_ATTEMPT.index("SET logical.active_generation")
    assert source_match < generation_write


def test_worker_claim_requires_the_predetermined_task_id() -> None:
    assert "attempt.worker_task_id = $worker_task_id" in CLAIM_QUEUED_ATTEMPT
    assert "attempt.status = 'started'" in CLAIM_QUEUED_ATTEMPT


def test_new_logical_run_starts_with_a_queued_attempt_and_checkpoint() -> None:
    assert "status: 'queued'" in CREATE_LOGICAL_RUN_AND_ATTEMPT
    assert "IngestionCheckpoint" in CREATE_LOGICAL_RUN_AND_ATTEMPT
    assert "HAS_ATTEMPT" in CREATE_LOGICAL_RUN_AND_ATTEMPT
    assert "cursor_json" in CREATE_LOGICAL_RUN_AND_ATTEMPT
    assert "logical.current_phase = $initial_phase" in CREATE_LOGICAL_RUN_AND_ATTEMPT
    assert "logical.configuration_fingerprint = $configuration_fingerprint" in (
        CREATE_LOGICAL_RUN_AND_ATTEMPT
    )
    assert (
        "coalesce(logical.creation_token = $creation_token, false) AS created"
        in CREATE_LOGICAL_RUN_AND_ATTEMPT
    )


def test_completion_is_fenced_and_refuses_a_pending_stop() -> None:
    assert "logical.active_generation = $generation" in FINALIZE_LOGICAL_RUN
    assert "logical.stop_requested_at IS NULL" in FINALIZE_LOGICAL_RUN
    assert "DELETE active_relation" in FINALIZE_LOGICAL_RUN


def test_phase_transition_completes_current_checkpoint_and_fences_successor() -> None:
    assert "current.status = 'completed'" in TRANSITION_LOGICAL_PHASE
    compatibility_check = TRANSITION_LOGICAL_PHASE.index("WHERE existing_next IS NULL")
    current_completion = TRANSITION_LOGICAL_PHASE.index("current.status = 'completed'")
    assert compatibility_check < current_completion
    base_fence = TRANSITION_LOGICAL_PHASE.index("logical.active_generation = $generation")
    optional_successor = TRANSITION_LOGICAL_PHASE.index("OPTIONAL MATCH (existing_next")
    assert base_fence < optional_successor
    assert "next.status = 'active'" in TRANSITION_LOGICAL_PHASE


def test_phase_transition_allows_a_new_connector_contract() -> None:
    assert "current.connector_version = logical.connector_version" in (TRANSITION_LOGICAL_PHASE)
    assert "current.schema_version = logical.checkpoint_schema_version" in (
        TRANSITION_LOGICAL_PHASE
    )
    assert "logical.connector_version = $connector_version" not in (TRANSITION_LOGICAL_PHASE)
    assert "logical.checkpoint_schema_version = $checkpoint_schema_version" not in (
        TRANSITION_LOGICAL_PHASE
    )
    assert "existing_next.connector_version = $connector_version" in (TRANSITION_LOGICAL_PHASE)
    assert "existing_next.schema_version = $checkpoint_schema_version" in (TRANSITION_LOGICAL_PHASE)


def test_failure_is_fenced_and_releases_active_ownership() -> None:
    assert "logical.active_generation = $generation" in FAIL_LOGICAL_RUN
    assert "attempt.status = 'failed'" in FAIL_LOGICAL_RUN
    assert "DELETE active_relation" in FAIL_LOGICAL_RUN


def test_repair_block_rejects_logical_run_reuse_and_resume_worker_admission() -> None:
    assert "existing.status IN ['paused_with_checkpoint', 'failed']" not in (
        CREATE_LOGICAL_RUN_AND_ATTEMPT
    )
    assert "dispatch.block_reason = 'crm_deal_identity_repair_quiesce'" in (
        CREATE_LOGICAL_RUN_AND_ATTEMPT
    )
    assert "dispatch.block_reason = 'crm_deal_identity_repair_quiesce'" in CLAIM_QUEUED_ATTEMPT
    assert "dispatch.block_reason = 'crm_deal_identity_repair_quiesce'" in CREATE_RESUME_ATTEMPT


def test_repair_block_keeps_only_stage_history_logical_runs_admissible() -> None:
    for query in (
        CREATE_LOGICAL_RUN_AND_ATTEMPT,
        CLAIM_QUEUED_ATTEMPT,
        CREATE_RESUME_ATTEMPT,
    ):
        assert "crm_stage_history" in query
    assert "$initial_phase STARTS WITH 'crm_stage_history'" in CREATE_LOGICAL_RUN_AND_ATTEMPT
    assert "logical.current_phase STARTS WITH 'crm_stage_history'" in CLAIM_QUEUED_ATTEMPT
    assert "logical.current_phase STARTS WITH 'crm_stage_history'" in CREATE_RESUME_ATTEMPT
