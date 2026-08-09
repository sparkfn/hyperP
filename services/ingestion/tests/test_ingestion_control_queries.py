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
    assert "DELETE active_relation" in CREATE_RESUME_ATTEMPT
    assert "checkpoint.connector_version = $connector_version" in CREATE_RESUME_ATTEMPT
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


def test_failure_is_fenced_and_releases_active_ownership() -> None:
    assert "logical.active_generation = $generation" in FAIL_LOGICAL_RUN
    assert "attempt.status = 'failed'" in FAIL_LOGICAL_RUN
    assert "DELETE active_relation" in FAIL_LOGICAL_RUN
