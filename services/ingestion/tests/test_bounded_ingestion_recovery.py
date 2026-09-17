"""Crash-boundary recovery proofs for bounded checkpoint commits."""

from __future__ import annotations

from datetime import UTC, datetime

from _bounded_ingestion_fixture import (
    FakeClock,
    FixtureDescriptor,
    MemoryControl,
    SimulatedKillError,
    context,
    unit,
)
from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.bounded_ingestion_runner import BoundedIngestionRunner


def _runner(now: datetime) -> BoundedIngestionRunner:
    return BoundedIngestionRunner(
        BoundedIngestionBudget(max_unit_seconds=60, drain_reserve_seconds=120),
        FakeClock(now),
        shutdown=FakeClockShutdown(),
    )


class FakeClockShutdown:
    def requested(self) -> bool:
        return False


def test_kill_before_commit_replays_the_same_page_without_a_skip() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    killed_descriptor = FixtureDescriptor(
        {0: unit(0, (("identity-1", "v1"),), terminal=True)},
        fetch_failure=SimulatedKillError("killed before graph commit"),
    )
    control = MemoryControl()

    failed = _runner(now).run_one(killed_descriptor, context(), control)

    assert failed.status == "failed"
    assert control.active_versions == {}
    assert control.checkpoint_pages == []
    assert control.terminal_watermark is False

    resumed_descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    completed = _runner(now).run_one(
        resumed_descriptor,
        context(attempt_generation=2, fence=2),
        control,
    )

    assert completed.status == "completed"
    assert control.active_versions == {"identity-1": "v1"}
    assert control.checkpoint_pages == [1]
    assert control.writer_invocations == 1


def test_kill_after_writer_before_checkpoint_rolls_back_then_replays_once() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = MemoryControl(crash_after_writer=True)

    failed = _runner(now).run_one(descriptor, context(), control)
    assert failed.status == "failed"
    assert failed.failure_category == "writer"

    assert control.active_versions == {}
    assert control.checkpoint_pages == []
    assert control.receipts == {}
    assert control.terminal_watermark is False

    control.crash_after_writer = False
    completed = _runner(now).run_one(descriptor, context(attempt_generation=2, fence=2), control)

    assert completed.status == "completed"
    assert control.active_versions == {"identity-1": "v1"}
    assert control.checkpoint_pages == [1]
    assert control.writer_invocations == 2


def test_replay_after_committed_receipt_does_not_repeat_writer_or_checkpoint_counters() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = MemoryControl()

    committed = _runner(now).run_one(descriptor, context(), control)
    replayed = _runner(now).run_one(descriptor, context(attempt_generation=2, fence=2), control)

    assert committed.status == "completed"
    assert replayed.status == "completed"
    assert control.receipts.keys() == {"page-0"}
    assert control.active_versions == {"identity-1": "v1"}
    assert control.checkpoint_pages == [1]
    assert control.writer_invocations == 1


def test_stale_commit_rejection_cannot_advance_checkpoint_or_terminal_state() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = MemoryControl(commit_allowed=False)

    result = _runner(now).run_one(descriptor, context(fence=2), control)

    assert result.status == "failed"
    assert result.failure_category == "lease"
    assert control.active_versions == {}
    assert control.checkpoint_pages == []
    assert control.terminal_watermark is False
