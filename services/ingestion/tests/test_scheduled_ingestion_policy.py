"""Fake-clock contracts for the scheduler's immutable weekly windows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from src.scheduled_ingestion_policy import weekly_occurrence


def test_bitrix_cutoff_resumes_at_next_thursday_opening() -> None:
    occurrence = weekly_occurrence(
        group_key="bitrix_chat",
        weekday="thursday",
        now=datetime(2026, 9, 17, 15, 0, tzinfo=UTC),
        drain_reserve_seconds=300,
    )

    assert occurrence.eligibility == "closed"
    assert occurrence.context.starts_at == datetime(2026, 9, 17, 1, 0, tzinfo=UTC)
    assert occurrence.context.cutoff_at == datetime(2026, 9, 17, 15, 0, tzinfo=UTC)
    assert occurrence.context.next_eligible_at == datetime(2026, 9, 24, 1, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 9, 21, 0, 59, tzinfo=UTC), "before_open"),
        (datetime(2026, 9, 21, 1, 0, tzinfo=UTC), "open"),
        (datetime(2026, 9, 21, 14, 55, tzinfo=UTC), "draining"),
        (datetime(2026, 9, 21, 15, 0, tzinfo=UTC), "closed"),
        (datetime(2026, 9, 22, 1, 0, tzinfo=UTC), "closed"),
    ],
)
def test_policy_preserves_exact_utc_boundaries(
    now: datetime,
    expected: str,
) -> None:
    occurrence = weekly_occurrence(
        group_key="fundbox",
        weekday="monday",
        now=now,
        drain_reserve_seconds=300,
    )

    assert occurrence.eligibility == expected
    assert occurrence.context.drain_starts_at == datetime(2026, 9, 21, 14, 55, tzinfo=UTC)


def test_policy_rejects_naive_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        weekly_occurrence(
            group_key="fundbox",
            weekday="monday",
            now=datetime(2026, 9, 21, 9, 0),
            drain_reserve_seconds=300,
        )


def test_delayed_delivery_stays_on_the_original_occurrence() -> None:
    """A retry on a later day cannot slide the window or catch up an intervening one."""
    for now, expected_eligibility in (
        (datetime(2026, 9, 21, 6, 0, tzinfo=UTC), "open"),
        (datetime(2026, 9, 22, 6, 0, tzinfo=UTC), "closed"),
        (datetime(2026, 9, 23, 6, 0, tzinfo=UTC), "closed"),
    ):
        occurrence = weekly_occurrence(
            group_key="fundbox",
            weekday="monday",
            now=now,
            drain_reserve_seconds=300,
        )

        assert occurrence.context.starts_at == datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
        assert occurrence.context.next_eligible_at == datetime(2026, 9, 28, 1, 0, tzinfo=UTC)
        assert occurrence.eligibility == expected_eligibility


def test_occurrence_id_is_stable_across_every_tick_of_the_window() -> None:
    first = weekly_occurrence(
        group_key="bitrix_chat",
        weekday="thursday",
        now=datetime(2026, 9, 17, 1, 0, tzinfo=UTC),
        drain_reserve_seconds=300,
    )
    last = weekly_occurrence(
        group_key="bitrix_chat",
        weekday="thursday",
        now=datetime(2026, 9, 17, 14, 59, tzinfo=UTC),
        drain_reserve_seconds=300,
    )

    assert (
        first.context.occurrence_id
        == last.context.occurrence_id
        == "scheduled:bitrix_chat:2026-09-17"
    )
    assert first.eligibility == "open"
    assert last.eligibility == "draining"


def test_hour_bucket_coalesces_ticks_within_one_absolute_hour() -> None:
    from src.scheduled_ingestion_policy import occurrence_hour_bucket

    assert occurrence_hour_bucket(datetime(2026, 9, 21, 1, 0, 30, tzinfo=UTC)) == "20260921T01"
    assert occurrence_hour_bucket(datetime(2026, 9, 21, 1, 59, 59, tzinfo=UTC)) == "20260921T01"
    assert occurrence_hour_bucket(datetime(2026, 9, 21, 2, 0, 0, tzinfo=UTC)) == "20260921T02"
    assert (
        occurrence_hour_bucket(datetime(2026, 9, 21, 9, 0, tzinfo=ZoneInfo("Asia/Singapore")))
        == "20260921T01"
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        occurrence_hour_bucket(datetime(2026, 9, 21, 1, 0))


def test_scheduled_group_weekday_leaves_sunday_without_a_source_group() -> None:
    from src.scheduled_ingestion_policy import scheduled_group_for_weekday

    assert scheduled_group_for_weekday(datetime(2026, 9, 21, 1, 0, tzinfo=UTC)) == "monday"
    assert scheduled_group_for_weekday(datetime(2026, 9, 20, 1, 0, tzinfo=UTC)) is None


def test_simulated_hourly_ticks_over_two_weeks_never_catch_up_a_later_day() -> None:
    """Thirteen days of hourly ticks resolve to one window per configured day."""
    occurrences: dict[str, int] = {}
    open_days: set[datetime] = set()
    open_ticks = 0
    for hour in range(24 * 13):
        now = datetime(2026, 9, 14, tzinfo=UTC) + timedelta(hours=hour)
        occurrence = weekly_occurrence(
            group_key="fundbox",
            weekday="monday",
            now=now,
            drain_reserve_seconds=300,
        )
        occurrences[occurrence.context.occurrence_id] = (
            occurrences.get(occurrence.context.occurrence_id, 0) + 1
        )
        if occurrence.eligibility == "open":
            open_ticks += 1
            open_days.add(occurrence.context.starts_at)

    assert sorted(occurrences) == [
        "scheduled:fundbox:2026-09-14",
        "scheduled:fundbox:2026-09-21",
    ]
    assert open_days == {
        datetime(2026, 9, 14, 1, 0, tzinfo=UTC),
        datetime(2026, 9, 21, 1, 0, tzinfo=UTC),
    }
    assert open_ticks == 28
