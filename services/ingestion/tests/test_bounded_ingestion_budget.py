"""Budget and durable-retry completion gates for bounded ingestion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from _bounded_ingestion_fixture import (
    FakeClock,
    FakeShutdown,
    FixtureDescriptor,
    MemoryControl,
    context,
    unit,
)
from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.bounded_ingestion_models import RetryObligation, Usage
from src.bounded_ingestion_runner import BoundedIngestionRunner


def _runner(now: datetime, budget: BoundedIngestionBudget) -> BoundedIngestionRunner:
    return BoundedIngestionRunner(budget, FakeClock(now), FakeShutdown())


def test_budget_pause_never_creates_a_connector_or_terminal_watermark() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    budget = BoundedIngestionBudget(
        max_records=3,
        max_source_requests=1,
        max_pages=1,
        max_bytes=1_000,
        max_extraction_calls=1,
        max_unit_seconds=60,
        drain_reserve_seconds=120,
    )
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = MemoryControl()
    already_reserved = Usage(
        records=3,
        source_requests=1,
        pages=1,
        bytes_read=1_000,
        extraction_calls=1,
    )

    result = _runner(now, budget).run_one(
        descriptor,
        context(reserved_usage=already_reserved),
        control,
    )

    assert result.status == "paused_with_checkpoint"
    assert result.pause_reason == "budget"
    assert descriptor.create_calls == 0
    assert control.checkpoint_pages == []
    assert control.terminal_watermark is False


def test_atomic_usage_reservation_loss_pauses_without_fetch_or_completion() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    budget = BoundedIngestionBudget(max_unit_seconds=60, drain_reserve_seconds=120)
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = MemoryControl(reserve_allowed=False)

    result = _runner(now, budget).run_one(descriptor, context(), control)

    assert result.status == "paused_with_checkpoint"
    assert result.pause_reason == "budget"
    assert descriptor.create_calls == 0
    assert len(control.reservations) == 1
    assert control.terminal_watermark is False


def test_terminal_page_with_durable_retry_obligation_cannot_finalize_or_advance_watermark() -> None:
    now = datetime(2026, 9, 17, 1, tzinfo=UTC)
    retry_at = now + timedelta(minutes=10)
    obligation = RetryObligation("page-0", "identity-1", "v1", "writer", 1, retry_at)
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),), terminal=True)})
    control = MemoryControl(retry_by_replay={"page-0": (obligation,)})
    budget = BoundedIngestionBudget(max_unit_seconds=60, drain_reserve_seconds=120)

    result = _runner(now, budget).run_one(descriptor, context(), control)

    assert result.status == "paused_with_checkpoint"
    assert result.pause_reason == "source_backoff"
    assert control.checkpoint_pages == [1]
    assert control.retry_backlog == 1
    assert control.finalized == 0
    assert control.terminal_watermark is False
    assert control.pauses == [("source_backoff", retry_at)]


def test_budget_policy_rejects_nonfinite_or_underreserved_drain_settings() -> None:
    invalid = (
        {"max_unit_seconds": 0.0, "drain_reserve_seconds": 1.0},
        {"max_unit_seconds": float("inf"), "drain_reserve_seconds": 200.0},
        {"max_unit_seconds": 120.0, "drain_reserve_seconds": 120.0},
        {"max_graph_writers": 0, "max_unit_seconds": 60.0, "drain_reserve_seconds": 120.0},
    )

    for overrides in invalid:
        try:
            BoundedIngestionBudget(**overrides)
        except ValueError:
            continue
        raise AssertionError(f"invalid budget was accepted: {overrides}")
