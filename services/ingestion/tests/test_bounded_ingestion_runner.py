"""End-to-end conformance tests for one bounded source unit per attempt."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _bounded_ingestion_fixture import (
    FakeClock,
    FakeShutdown,
    FixtureDescriptor,
    MemoryControl,
    checkpoint,
    context,
    occurrence,
    scope,
    unit,
)
from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.bounded_ingestion_models import BoundedUnit, SourceBackoffError, Usage
from src.bounded_ingestion_runner import BoundedIngestionRunner
from src.resumable import IngestionUnit


def _runner(clock: FakeClock, shutdown: FakeShutdown | None = None) -> BoundedIngestionRunner:
    return BoundedIngestionRunner(
        BoundedIngestionBudget(max_unit_seconds=60, drain_reserve_seconds=120),
        clock,
        shutdown or FakeShutdown(),
    )


def test_three_pages_complete_over_three_occurrences_without_output_loss_or_duplication() -> None:
    source_window = {"snapshot": "immutable-window-1"}
    run_scope = scope(source_window=source_window)
    descriptor = FixtureDescriptor(
        {
            0: unit(0, (("identity-1", "v1"),), source_window=source_window),
            1: unit(1, (("identity-2", "v1"),), source_window=source_window),
            2: unit(
                2,
                (("identity-3", "v1"),),
                terminal=True,
                source_window=source_window,
            ),
        }
    )
    control = MemoryControl()
    starts = (
        datetime(2026, 9, 17, 1, tzinfo=UTC),
        datetime(2026, 9, 24, 1, tzinfo=UTC),
        datetime(2026, 10, 1, 1, tzinfo=UTC),
    )
    results: list[str] = []
    contexts = []
    for page, starts_at in enumerate(starts):
        attempt = context(
            page,
            attempt_generation=page + 1,
            fence=page + 1,
            occurrence_context=occurrence(starts_at=starts_at),
            run_scope=run_scope,
        )
        contexts.append(attempt)
        results.append(_runner(FakeClock(starts_at)).run_one(descriptor, attempt, control).status)

    assert results == ["paused_with_checkpoint", "paused_with_checkpoint", "completed"]
    assert {attempt.logical_run_id for attempt in contexts} == {"logical-fixture"}
    assert {str(attempt.checkpoint.source_window) for attempt in contexts} == {
        "{'snapshot': 'immutable-window-1'}"
    }
    assert control.active_versions == {
        "identity-1": "v1",
        "identity-2": "v1",
        "identity-3": "v1",
    }
    assert control.writer_invocations == 3
    assert control.checkpoint_pages == [1, 2, 3]
    assert control.terminal_watermark is True


def test_sigterm_before_source_creation_pauses_without_checkpoint_or_terminal_watermark() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = MemoryControl()

    result = _runner(FakeClock(now), FakeShutdown(True)).run_one(descriptor, context(), control)

    assert result.status == "paused_with_checkpoint"
    assert result.pause_reason == "shutdown"
    assert descriptor.create_calls == 0
    assert control.checkpoint_pages == []
    assert control.terminal_watermark is False


def test_source_backoff_preserves_checkpoint_and_uses_retry_after_before_drain() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    retry_at = now + timedelta(minutes=5)
    descriptor = FixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),))},
        fetch_failure=SourceBackoffError(retry_at, "retry after 300 seconds"),
    )
    control = MemoryControl()

    result = _runner(FakeClock(now)).run_one(descriptor, context(), control)

    assert result.status == "paused_with_checkpoint"
    assert result.pause_reason == "source_backoff"
    assert result.safe_message == "retry after 300 seconds"
    assert control.pauses == [("source_backoff", retry_at)]
    assert control.checkpoint_pages == []
    assert control.terminal_watermark is False


def test_source_backoff_at_or_after_drain_waits_for_the_next_weekly_occurrence() -> None:
    scheduled = occurrence()
    retry_at = scheduled.drain_starts_at
    descriptor = FixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),))},
        fetch_failure=SourceBackoffError(retry_at),
    )
    control = MemoryControl()

    result = _runner(FakeClock(scheduled.starts_at)).run_one(descriptor, context(), control)

    assert result.status == "paused_with_checkpoint"
    assert result.pause_reason == "source_backoff"
    assert control.pauses == [("source_backoff", scheduled.next_eligible_at)]
    assert control.terminal_watermark is False


@pytest.mark.parametrize(
    ("usage", "expected_message"),
    [
        (Usage(records=1, source_requests=2, pages=1, bytes_read=1), "source_request_limit"),
        (Usage(records=1, source_requests=1, pages=1, bytes_read=1_001), "response_size"),
        (Usage(records=1, source_requests=1, pages=1, extraction_calls=2), "extraction_limit"),
    ],
)
def test_unit_resource_limits_fail_before_writer_side_effects(
    usage: Usage,
    expected_message: str,
) -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), usage=usage)})
    control = MemoryControl()

    result = _runner(FakeClock(now)).run_one(descriptor, context(), control)

    assert result.status == "failed"
    assert result.failure_category == "source"
    assert result.safe_message is not None
    assert expected_message in result.safe_message
    assert control.writer_invocations == 0
    assert control.checkpoint_pages == []


def test_oversized_record_page_fails_before_writer_side_effects() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    descriptor = FixtureDescriptor(
        {
            0: unit(
                0,
                (
                    ("identity-1", "v1"),
                    ("identity-2", "v1"),
                    ("identity-3", "v1"),
                    ("identity-4", "v1"),
                ),
            )
        }
    )
    control = MemoryControl()

    result = _runner(FakeClock(now)).run_one(descriptor, context(), control)

    assert result.status == "failed"
    assert result.safe_message == "oversized_record_page"
    assert control.writer_invocations == 0


def test_terminal_unit_with_a_repeated_cursor_is_rejected_before_completion() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    repeated = BoundedUnit(
        IngestionUnit(checkpoint(0), checkpoint(0), ({"id": "identity-1", "version": "v1"},)),
        "repeated-terminal-page",
        Usage(records=1, source_requests=1, pages=1, bytes_read=100),
        True,
    )
    descriptor = FixtureDescriptor({0: repeated})
    control = MemoryControl()

    result = _runner(FakeClock(now)).run_one(descriptor, context(), control)

    assert result.status == "failed"
    assert result.safe_message == "repeated_cursor"
    assert control.writer_invocations == 0
    assert control.terminal_watermark is False


def test_live_clock_fences_a_source_or_close_that_crosses_cutoff() -> None:
    scheduled = occurrence()
    clock = FakeClock(scheduled.starts_at)

    def cross_cutoff() -> None:
        clock.now = scheduled.cutoff_at + timedelta(seconds=1)

    descriptor = FixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),), terminal=True)},
        on_close=cross_cutoff,
    )
    control = MemoryControl()

    result = _runner(clock).run_one(descriptor, context(), control)

    assert result.status == "failed"
    assert result.failure_category == "overrun"
    assert control.writer_invocations == 0
    assert control.terminal_watermark is False
