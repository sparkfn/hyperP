"""Atomic Bitrix stream-fence regression coverage."""

from src.graph.queries.ingestion_control import (
    LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
    SET_FENCED_BITRIX_STREAM_STATUS,
)


def test_fence_takes_stream_write_lock_before_identity_recheck() -> None:
    lock = LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE.index("SET stream.fence_lock_version")
    identity_recheck = LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE.index("WHERE stream.logical_run_id")

    assert lock < identity_recheck
    assert "stream.ingest_run_id = $ingest_run_id" in LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE
    assert "stream.attempt_generation = $attempt_generation" in (
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE
    )
    assert "stream.stream_generation = $stream_generation" in (LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE)
    assert "stream.fencing_token = $fencing_token" in LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE
    assert "stream.status = 'active'" in LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE


def test_terminal_stream_transition_rechecks_the_complete_fence() -> None:
    for field in (
        "source_key",
        "stream_key",
        "logical_run_id",
        "ingest_run_id",
        "attempt_generation",
        "stream_generation",
        "fencing_token",
    ):
        assert f"{field}: ${field}" in SET_FENCED_BITRIX_STREAM_STATUS
    assert "stream.status = 'active'" in SET_FENCED_BITRIX_STREAM_STATUS
    assert "'completed'" in SET_FENCED_BITRIX_STREAM_STATUS
    assert "'terminated'" in SET_FENCED_BITRIX_STREAM_STATUS
