"""End-to-end conformance tests for one bounded source unit per attempt."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _bounded_ingestion_fixture import (
    FakeClock,
    FakeShutdown,
    FixtureDescriptor,
    InFlightFixtureDescriptor,
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
        BoundedIngestionBudget(max_unit_seconds=60, drain_reserve_seconds=155),
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


def test_source_backoff_beyond_next_weekly_opening_selects_the_later_occurrence() -> None:
    scheduled = occurrence()
    retry_at = scheduled.next_eligible_at + timedelta(days=10)
    descriptor = FixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),))},
        fetch_failure=SourceBackoffError(retry_at),
    )
    control = MemoryControl()

    result = _runner(FakeClock(scheduled.starts_at)).run_one(descriptor, context(), control)

    assert result.status == "paused_with_checkpoint"
    assert result.pause_reason == "source_backoff"
    assert control.pauses == [("source_backoff", scheduled.next_eligible_at + timedelta(days=14))]
    assert descriptor.fetch_calls == 1


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


def test_connector_receives_executable_deadline_and_cancellation_signal() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    shutdown = FakeShutdown(False)
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = MemoryControl()

    result = _runner(FakeClock(now), shutdown).run_one(descriptor, context(), control)

    assert result.status == "completed"
    assert descriptor.seen_deadline == now + timedelta(seconds=60)
    assert descriptor.seen_cancellation is not None
    assert descriptor.seen_cancellation is not shutdown


def test_reservation_elapsed_time_rebases_connector_lifecycle_deadline() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    clock = FakeClock(now)

    def advance_after_reservation() -> None:
        clock.now += timedelta(seconds=7)

    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = MemoryControl(on_reserve=advance_after_reservation)

    result = _runner(clock).run_one(descriptor, context(), control)

    assert result.status == "completed"
    assert descriptor.create_calls == 1
    assert descriptor.seen_deadline == now + timedelta(seconds=67)


def test_unscheduled_lifecycle_overrun_fails_without_commit() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    clock = FakeClock(now)

    def cross_lifecycle_deadline() -> None:
        clock.now += timedelta(seconds=66)

    shutdown = FakeShutdown(False)
    descriptor = FixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),), terminal=True)},
        on_close=cross_lifecycle_deadline,
    )
    control = MemoryControl()

    result = _runner(clock, shutdown).run_one(descriptor, context(), control)

    assert result.status == "failed"
    assert result.failure_category == "overrun"
    assert descriptor.close_calls == 1
    assert control.writer_invocations == 0


def test_lifecycle_overrun_after_fetch_never_commits() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    clock = FakeClock(now)

    def consume_lifecycle_budget() -> None:
        clock.now += timedelta(seconds=61)

    descriptor = FixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),), terminal=True)},
        on_fetch=consume_lifecycle_budget,
    )
    control = MemoryControl()

    result = _runner(clock).run_one(descriptor, context(), control)

    assert result.status == "failed"
    assert result.failure_category == "overrun"
    assert descriptor.cancel_calls == 0
    assert descriptor.close_calls == 1
    assert control.writer_invocations == 0


class _FetchStartedDeadlineClock(FakeClock):
    def __init__(self, now: datetime, descriptor: InFlightFixtureDescriptor) -> None:
        super().__init__(now)
        self._descriptor = descriptor

    def __call__(self) -> datetime:
        if self._descriptor.fetch_started.is_set():
            return self.now + timedelta(seconds=60)
        return self.now


def test_in_flight_fetch_waits_for_deadline_signal_then_supervisor_cancels_without_writer() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    descriptor = InFlightFixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),), terminal=True)},
        cooperate_with_cancellation=True,
    )
    control = MemoryControl()

    result = _runner(_FetchStartedDeadlineClock(now, descriptor)).run_one(
        descriptor,
        context(),
        control,
    )

    assert result.status == "failed"
    assert result.failure_category == "overrun"
    assert descriptor.fetch_started.is_set()
    assert descriptor.cancel_started.is_set()
    assert descriptor.fetch_finished.is_set()
    assert descriptor.cancel_while_fetch_in_flight is True
    assert descriptor.deadline_cancellation_observed is True
    assert descriptor.fetch_thread_id != descriptor.cancel_thread_id
    assert descriptor.close_finished.is_set()
    assert control.writer_invocations == 0


def test_non_cooperative_fetch_exceeding_close_bound_fails_cleanup_without_writer() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    descriptor = InFlightFixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),), terminal=True)},
        cooperate_with_cancellation=False,
    )
    control = MemoryControl()
    clock = _FetchStartedDeadlineClock(now, descriptor)

    try:
        result = _runner(clock).run_one(descriptor, context(), control)
    finally:
        descriptor.fetch_release.set()

    assert descriptor.close_finished.wait(timeout=1.0)
    assert result.status == "failed"
    assert result.failure_category == "source"
    assert result.safe_message == "connector_cleanup_failed"
    assert descriptor.cancel_calls == 1
    assert descriptor.close_calls == 1
    assert control.writer_invocations == 0


def test_non_cooperative_close_exceeding_close_bound_fails_cleanup_without_writer() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    descriptor = InFlightFixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),), terminal=True)},
        cooperate_with_cancellation=True,
        block_close=True,
    )
    control = MemoryControl()
    clock = _FetchStartedDeadlineClock(now, descriptor)

    try:
        result = _runner(clock).run_one(descriptor, context(), control)
    finally:
        descriptor.close_release.set()

    assert descriptor.close_finished.wait(timeout=1.0)
    assert result.status == "failed"
    assert result.failure_category == "source"
    assert result.safe_message == "connector_cleanup_failed"
    assert descriptor.cancel_calls == 1
    assert descriptor.close_started.is_set()
    assert control.writer_invocations == 0


class _ShutdownAfterFetchStartsClock(FakeClock):
    def __init__(
        self,
        now: datetime,
        descriptor: InFlightFixtureDescriptor,
        shutdown: FakeShutdown,
    ) -> None:
        super().__init__(now)
        self._descriptor = descriptor
        self._shutdown = shutdown

    def __call__(self) -> datetime:
        if self._descriptor.fetch_started.is_set():
            self._shutdown.is_requested = True
        return self.now


def test_shutdown_during_in_flight_fetch_cleans_up_and_persists_shutdown_pause() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    shutdown = FakeShutdown()
    descriptor = InFlightFixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),), terminal=True)},
        cooperate_with_cancellation=True,
    )
    control = MemoryControl()
    clock = _ShutdownAfterFetchStartsClock(now, descriptor, shutdown)

    result = _runner(clock, shutdown).run_one(descriptor, context(), control)

    assert result.status == "paused_with_checkpoint"
    assert result.pause_reason == "shutdown"
    assert descriptor.cancel_calls == 1
    assert descriptor.close_finished.is_set()
    assert control.pauses == [("shutdown", now)]
    assert control.failures == []
    assert control.writer_invocations == 0


def test_reserved_graph_budgets_allow_slow_commit_and_finalize() -> None:
    scheduled = occurrence()
    clock = FakeClock(scheduled.starts_at)

    def spend_graph_budget() -> None:
        clock.now += timedelta(seconds=30)

    descriptor = FixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),), terminal=True)},
    )
    control = MemoryControl(on_commit=spend_graph_budget, on_finalize=spend_graph_budget)

    result = _runner(clock).run_one(
        descriptor,
        context(occurrence_context=scheduled),
        control,
    )

    assert result.status == "completed"
    assert control.writer_invocations == 1
    assert control.finalized == 1
    assert clock.now == scheduled.starts_at + timedelta(seconds=60)


def test_source_lifecycle_overrun_prevents_graph_transitions() -> None:
    scheduled = occurrence()
    clock = FakeClock(scheduled.starts_at)

    def leave_insufficient_graph_budget() -> None:
        clock.now = scheduled.cutoff_at - timedelta(seconds=60)

    descriptor = FixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),), terminal=True)},
        on_close=leave_insufficient_graph_budget,
    )
    control = MemoryControl()

    result = _runner(clock).run_one(
        descriptor,
        context(occurrence_context=scheduled),
        control,
    )

    assert result.status == "failed"
    assert result.failure_category == "overrun"
    assert result.safe_message == "connector_lifecycle_deadline_exceeded"
    assert control.writer_invocations == 0
    assert control.finalized == 0
