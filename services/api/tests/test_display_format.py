"""Tests for presentation formatting helpers."""

from __future__ import annotations

from src.display_format import (
    format_confidence_pct,
    format_display_date,
    format_display_datetime,
)


def test_format_display_date_basic() -> None:
    assert format_display_date("2026-04-02T05:30:00Z") == "02 Apr 2026"


def test_format_display_date_converts_to_utc() -> None:
    # 00:30 at +08:00 is 16:30 the previous day in UTC.
    assert format_display_date("2026-04-02T00:30:00+08:00") == "01 Apr 2026"


def test_format_display_date_empty_returns_empty() -> None:
    assert format_display_date("") == ""
    assert format_display_date("not-a-date") == ""


def test_format_display_datetime_basic() -> None:
    assert format_display_datetime("2026-04-02T03:14:00Z") == "02 Apr 2026, 03:14 AM"


def test_format_display_datetime_pm() -> None:
    assert format_display_datetime("2026-04-02T15:14:00Z") == "02 Apr 2026, 03:14 PM"


def test_format_display_datetime_midnight() -> None:
    assert format_display_datetime("2026-04-02T00:05:00Z") == "02 Apr 2026, 12:05 AM"


def test_format_confidence_pct() -> None:
    assert format_confidence_pct(0.82) == "82%"
    assert format_confidence_pct(1.0) == "100%"
    assert format_confidence_pct(0.826) == "83%"
    assert format_confidence_pct(0.0) == "0%"


def test_format_confidence_pct_none() -> None:
    assert format_confidence_pct(None) is None
