"""Asia/Singapore occurrence boundaries and immutable deadline tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from _bounded_ingestion_fixture import (
    FakeClock,
    FakeShutdown,
    FixtureDescriptor,
    MemoryControl,
    context,
    occurrence,
    unit,
)
from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.bounded_ingestion_models import BoundedRunResult, OccurrenceContext
from src.bounded_ingestion_runner import BoundedIngestionRunner
from src.bounded_ingestion_window import occurrence_from_payload, occurrence_to_payload


def _run_at(
    now: datetime,
    *,
    occurrence_context: OccurrenceContext | None = None,
) -> tuple[BoundedRunResult, FixtureDescriptor, MemoryControl]:
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = MemoryControl()
    runner = BoundedIngestionRunner(
        BoundedIngestionBudget(max_unit_seconds=60, drain_reserve_seconds=155),
        FakeClock(now),
        FakeShutdown(),
    )
    result = runner.run_one(descriptor, context(occurrence_context=occurrence_context), control)
    return result, descriptor, control


def test_scheduled_window_opens_at_exactly_0900_asia_singapore() -> None:
    scheduled = occurrence()

    before, before_descriptor, before_control = _run_at(
        scheduled.starts_at - timedelta(microseconds=1), occurrence_context=scheduled
    )
    opening, opening_descriptor, opening_control = _run_at(
        scheduled.starts_at, occurrence_context=scheduled
    )

    assert before.status == "paused_with_checkpoint"
    assert before.pause_reason == "schedule_window_closed"
    assert before_descriptor.create_calls == 0
    assert before_control.terminal_watermark is False
    assert opening.status == "completed"
    assert opening_descriptor.create_calls == 1
    assert opening_control.terminal_watermark is True
    assert scheduled.local_iso(scheduled.starts_at).startswith("2026-09-17T09:00:00+08:00")


def test_drain_boundary_cutoff_midnight_and_intervening_days_never_admit_a_source_call() -> None:
    scheduled = occurrence()
    blocked_times = (
        scheduled.drain_starts_at,
        scheduled.cutoff_at,
        scheduled.cutoff_at + timedelta(minutes=1),
        scheduled.cutoff_at + timedelta(days=1),
        scheduled.next_eligible_at - timedelta(microseconds=1),
    )

    for now in blocked_times:
        result, descriptor, control = _run_at(now, occurrence_context=scheduled)
        assert result.status == "paused_with_checkpoint"
        assert result.pause_reason == "schedule_window_closed"
        assert descriptor.create_calls == 0
        assert control.checkpoint_pages == []
        assert control.terminal_watermark is False


def test_next_weekly_opening_resumes_the_same_unfinished_checkpoint_window() -> None:
    first = occurrence()
    next_occurrence = occurrence(starts_at=first.next_eligible_at)
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = MemoryControl()
    runner = BoundedIngestionRunner(
        BoundedIngestionBudget(max_unit_seconds=60, drain_reserve_seconds=155),
        FakeClock(next_occurrence.starts_at),
        FakeShutdown(),
    )

    result = runner.run_one(descriptor, context(occurrence_context=next_occurrence), control)

    assert result.status == "completed"
    assert next_occurrence.starts_at == datetime(2026, 9, 24, 1, tzinfo=UTC)
    assert control.checkpoint_pages == [1]
    assert control.terminal_watermark is True


def test_delayed_delivery_cannot_slide_the_persisted_deadline() -> None:
    scheduled = occurrence()
    restored = occurrence_from_payload(occurrence_to_payload(scheduled))
    delayed_after_cutoff = scheduled.cutoff_at + timedelta(hours=2)

    result, descriptor, control = _run_at(delayed_after_cutoff, occurrence_context=restored)

    assert restored.starts_at == scheduled.starts_at
    assert restored.cutoff_at == scheduled.cutoff_at
    assert restored.next_eligible_at == datetime(2026, 9, 24, 1, tzinfo=UTC)
    assert result.pause_reason == "schedule_window_closed"
    assert descriptor.create_calls == 0
    assert control.terminal_watermark is False


def test_slow_unit_is_rejected_before_start_when_it_cannot_finish_by_cutoff() -> None:
    starts_at = datetime(2026, 9, 17, 1, tzinfo=UTC)
    scheduled = occurrence(
        starts_at=starts_at,
        drain_after=timedelta(hours=13, minutes=59, seconds=30),
    )
    now = scheduled.cutoff_at - timedelta(seconds=30)

    result, descriptor, control = _run_at(now, occurrence_context=scheduled)

    assert result.status == "paused_with_checkpoint"
    assert result.pause_reason == "schedule_window_closed"
    assert descriptor.create_calls == 0
    assert control.checkpoint_pages == []
