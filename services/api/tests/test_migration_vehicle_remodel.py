"""Unit tests for the vehicle-remodel MachineUnit purge migration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.migrations.vehicle_remodel_drop_machine_units import (
    drop_machine_unit_constraints,
    drop_machine_unit_nodes,
)


def _make_count_cursor(count: int) -> AsyncMock:
    """Cursor whose `.single()` returns {"deleted_count": count}."""
    cursor = AsyncMock()
    cursor.single.return_value = {"deleted_count": count}
    return cursor


def _make_iter_cursor(records: list[dict]) -> AsyncMock:
    """Cursor whose `async for` yields the given records."""
    cursor = AsyncMock()
    cursor.__aiter__.return_value = iter(records)
    return cursor


@pytest.mark.asyncio
async def test_drop_machine_unit_nodes_returns_zero_when_empty() -> None:
    session = AsyncMock()
    session.run.return_value = _make_count_cursor(0)
    assert await drop_machine_unit_nodes(session) == 0


@pytest.mark.asyncio
async def test_drop_machine_unit_nodes_returns_count_when_present() -> None:
    session = AsyncMock()
    session.run.return_value = _make_count_cursor(42)
    assert await drop_machine_unit_nodes(session) == 42


@pytest.mark.asyncio
async def test_drop_machine_unit_constraints_empty_when_nothing_to_drop() -> None:
    session = AsyncMock()
    # First (and only) probe returns no constraints → loop exits immediately.
    session.run.return_value = _make_iter_cursor([])
    assert await drop_machine_unit_constraints(session) == []


@pytest.mark.asyncio
async def test_drop_machine_unit_constraints_drops_each_and_reprobes() -> None:
    session = AsyncMock()
    # Probe 1: two constraints → drop one, reprobe. Probe 2: one constraint → drop, reprobe.
    # Probe 3: empty → exit loop. The DROP CONSTRAINT calls return AsyncMock() cursors
    # we never iterate, so any value works — `None` is fine.
    session.run.side_effect = [
        _make_iter_cursor(
            [
                {"name": "machine_unit_id_unique"},
                {"name": "machine_unit_serial_unique"},
            ]
        ),
        None,  # DROP CONSTRAINT machine_unit_id_unique
        _make_iter_cursor([{"name": "machine_unit_serial_unique"}]),
        None,  # DROP CONSTRAINT machine_unit_serial_unique
        _make_iter_cursor([]),  # empty probe → exit loop
    ]
    dropped = await drop_machine_unit_constraints(session)
    assert sorted(dropped) == ["machine_unit_id_unique", "machine_unit_serial_unique"]
    # Two DROPs plus three probes = five total session.run calls.
    assert session.run.await_count == 5