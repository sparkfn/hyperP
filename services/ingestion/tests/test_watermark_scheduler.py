"""Unit tests for the simplified watermark scheduler."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from src.scheduled_ingestion_groups import (
    ScheduledIngestionGroup,
    ScheduledIngestionSpec,
)
from src.watermark_scheduler import dispatch_incremental_group


class FakeRedis:
    """Minimal in-memory Redis stand-in."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, name: str) -> bytes | str | None:
        return self._store.get(name)

    def set(self, name: str, value: str | bytes, **kwargs: object) -> object:
        self._store[name] = value.decode("utf-8") if isinstance(value, bytes) else value
        return True

    def delete(self, *names: str) -> object:
        for n in names:
            self._store.pop(n, None)
        return len(names)


_MONDAY_10AM_SGT = datetime(2026, 9, 21, 2, 0, 0, tzinfo=UTC)

_TEST_GROUP = ScheduledIngestionGroup(
    key="test_group",
    weekday="monday",
    tasks=(
        ScheduledIngestionSpec("source_a"),
        ScheduledIngestionSpec("source_b"),
        ScheduledIngestionSpec("source_c"),
    ),
)


@patch("src.watermark_scheduler.get_ingestion_config")
def test_disabled_scheduling_returns_disabled(mock_config: object) -> None:
    from unittest.mock import MagicMock

    assert isinstance(mock_config, MagicMock)
    cfg = MagicMock()
    cfg.scheduled_ingestion.enabled = False
    mock_config.return_value = cfg

    redis = FakeRedis()
    result = dispatch_incremental_group(
        _TEST_GROUP,
        _MONDAY_10AM_SGT,
        redis,  # type: ignore[arg-type]
    )
    assert result["status"] == "disabled"


@patch("src.watermark_scheduler.get_ingestion_config")
def test_outside_window_returns_window_closed(mock_config: object) -> None:
    from unittest.mock import MagicMock

    assert isinstance(mock_config, MagicMock)
    cfg = MagicMock()
    cfg.scheduled_ingestion.enabled = True
    mock_config.return_value = cfg

    redis = FakeRedis()
    tuesday = datetime(2026, 9, 22, 2, 0, 0, tzinfo=UTC)
    result = dispatch_incremental_group(
        _TEST_GROUP,
        tuesday,
        redis,  # type: ignore[arg-type]
    )
    assert result["status"] == "window_closed"


@patch("src.watermark_scheduler.get_ingestion_config")
def test_dispatches_all_group_specs(mock_config: object) -> None:
    from unittest.mock import MagicMock

    assert isinstance(mock_config, MagicMock)
    cfg = MagicMock()
    cfg.scheduled_ingestion.enabled = True
    mock_config.return_value = cfg

    redis = FakeRedis()
    result = dispatch_incremental_group(
        _TEST_GROUP,
        _MONDAY_10AM_SGT,
        redis,  # type: ignore[arg-type]
    )
    assert result["status"] == "published"
    assert result["dispatched"] == 3
    assert result["skipped"] == 0


@patch("src.watermark_scheduler.get_ingestion_config")
def test_skips_locked_source(mock_config: object) -> None:
    from unittest.mock import MagicMock

    assert isinstance(mock_config, MagicMock)
    cfg = MagicMock()
    cfg.scheduled_ingestion.enabled = True
    mock_config.return_value = cfg

    redis = FakeRedis()
    redis.set("profile_unifier:ingestion:source:source_b", "lock-owner")

    result = dispatch_incremental_group(
        _TEST_GROUP,
        _MONDAY_10AM_SGT,
        redis,  # type: ignore[arg-type]
    )
    assert result["status"] == "published"
    assert result["dispatched"] == 2
    assert result["skipped"] == 1


@patch("src.watermark_scheduler.get_ingestion_config")
def test_sequential_ordering_preserved(mock_config: object) -> None:
    from unittest.mock import MagicMock

    assert isinstance(mock_config, MagicMock)
    cfg = MagicMock()
    cfg.scheduled_ingestion.enabled = True
    mock_config.return_value = cfg

    redis = FakeRedis()
    result = dispatch_incremental_group(
        _TEST_GROUP,
        _MONDAY_10AM_SGT,
        redis,  # type: ignore[arg-type]
    )
    assert result["status"] == "published"
    assert result["dispatched"] == 3
