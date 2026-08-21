"""Contract and operator tests for the PHPPOS loyalty property migration."""

from __future__ import annotations

import json

import pytest
from src import loyalty_points_control as control
from src.graph.loyalty_points_migration import LoyaltyPointsInvalidCounts
from src.graph.queries.loyalty_points_migration import (
    APPLY_LOYALTY_POINTS_MIGRATION_BATCH,
    COUNT_INVALID_LOYALTY_POINTS,
    FETCH_LOYALTY_POINTS_MIGRATION_BATCH,
    TARGET_LOYALTY_ORDER_SOURCES,
)


def test_queries_target_only_phppos_orders_and_verify_integer_or_null() -> None:
    assert TARGET_LOYALTY_ORDER_SOURCES == ("eko_phppos", "speedzone_phppos")
    for query in (COUNT_INVALID_LOYALTY_POINTS, FETCH_LOYALTY_POINTS_MIGRATION_BATCH):
        assert "o.source_system_key IN $source_system_keys" in query
        assert "valueType(o.points_used)" in query
        assert "valueType(o.points_gained)" in query
        assert "STARTS WITH 'INTEGER'" in query
    assert "ORDER BY source_system_key, source_order_id" in FETCH_LOYALTY_POINTS_MIGRATION_BATCH
    assert "LIMIT $batch_size" in FETCH_LOYALTY_POINTS_MIGRATION_BATCH
    assert "o.points_used = row.points_used" in APPLY_LOYALTY_POINTS_MIGRATION_BATCH
    assert "o.points_gained = row.points_gained" in APPLY_LOYALTY_POINTS_MIGRATION_BATCH
    assert "migration.source_cursor" in APPLY_LOYALTY_POINTS_MIGRATION_BATCH
    assert "migration.order_cursor" in APPLY_LOYALTY_POINTS_MIGRATION_BATCH


class _ControlClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_check_reports_zero_invalid_invariant(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _ControlClient()
    monkeypatch.setattr(control, "get_settings", lambda: object())
    monkeypatch.setattr(control, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(
        control,
        "count_invalid_loyalty_points",
        lambda _client: LoyaltyPointsInvalidCounts(0, 0, 0),
    )

    assert control.run(["check"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "command": "check",
        "invalid_order_count": 0,
        "invalid_points_gained_count": 0,
        "invalid_points_used_count": 0,
        "status": "ok",
    }
    assert client.closed


def test_backfill_reports_updates_and_fails_closed_on_remaining_invalid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _ControlClient()
    monkeypatch.setattr(control, "get_settings", lambda: object())
    monkeypatch.setattr(control, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(control, "repair_loyalty_points", lambda _client, *, batch_size: 4)
    monkeypatch.setattr(
        control,
        "count_invalid_loyalty_points",
        lambda _client: LoyaltyPointsInvalidCounts(1, 1, 0),
    )

    assert control.run(["backfill", "--batch-size", "10"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "command": "backfill",
        "invalid_order_count": 1,
        "invalid_points_gained_count": 0,
        "invalid_points_used_count": 1,
        "status": "invariant_failed",
        "updated_field_count": 4,
    }
    assert client.closed
