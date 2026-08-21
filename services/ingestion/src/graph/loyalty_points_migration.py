"""Operator-driven repair for legacy PHPPOS Order loyalty-point properties."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import TypedDict

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient
from src.graph.queries.loyalty_points_migration import (
    ACQUIRE_LOYALTY_POINTS_MIGRATION,
    APPLY_LOYALTY_POINTS_MIGRATION_BATCH,
    COMPLETE_LOYALTY_POINTS_MIGRATION,
    COUNT_INVALID_LOYALTY_POINTS,
    FETCH_LOYALTY_POINTS_MIGRATION_BATCH,
    RELEASE_LOYALTY_POINTS_MIGRATION,
    TARGET_LOYALTY_ORDER_SOURCES,
)
from src.loyalty_points import normalize_loyalty_field

MIGRATION_KEY = "phppos_order_loyalty_points_v1"
DEFAULT_BATCH_SIZE = 500
_LEASE_SECONDS = 5 * 60


@dataclass(frozen=True)
class LoyaltyPointsInvalidCounts:
    """Counts of targeted orders and fields that are not integer/null."""

    invalid_order_count: int
    invalid_points_used_count: int
    invalid_points_gained_count: int


class _Update(TypedDict):
    source_system_key: str
    source_order_id: str
    expected_points_used: object
    expected_points_used_is_nan: bool
    expected_points_gained: object
    expected_points_gained_is_nan: bool
    points_used: int | None
    points_gained: int | None


def count_invalid_loyalty_points(client: Neo4jClient) -> LoyaltyPointsInvalidCounts:
    """Return the zero-invalid invariant counts for targeted PHPPOS orders."""

    def _work(tx: ManagedTransaction) -> LoyaltyPointsInvalidCounts:
        record = tx.run(
            COUNT_INVALID_LOYALTY_POINTS,
            source_system_keys=list(TARGET_LOYALTY_ORDER_SOURCES),
        ).single()
        return _invalid_counts(record)

    return client.execute_read(_work)


def repair_loyalty_points(
    client: Neo4jClient,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    reopen_completed: bool = True,
) -> int:
    """Repair targeted properties in restart-safe leased keyset batches."""
    if batch_size < 1:
        raise ValueError("loyalty migration batch_size must be positive")
    owner_id = str(uuid.uuid4())
    acquired, completed = _acquire(client, owner_id, reopen_completed)
    if completed and not reopen_completed:
        return 0
    if not acquired:
        raise RuntimeError("loyalty points migration lease is held by another operator")
    updated_total = 0
    migration_completed = False
    try:
        while True:
            updated, processed = _repair_batch(client, owner_id, batch_size)
            updated_total += updated
            if processed == 0:
                break
        invalid_count, migration_completed = _complete(client, owner_id)
        if invalid_count != 0 or not migration_completed:
            raise RuntimeError(
                f"loyalty points migration verification failed with {invalid_count} invalid orders"
            )
        return updated_total
    finally:
        if not migration_completed:
            _release(client, owner_id)


def _acquire(client: Neo4jClient, owner_id: str, reopen_completed: bool) -> tuple[bool, bool]:
    def _work(tx: ManagedTransaction) -> tuple[bool, bool]:
        record = tx.run(
            ACQUIRE_LOYALTY_POINTS_MIGRATION,
            migration_key=MIGRATION_KEY,
            owner_id=owner_id,
            lease_seconds=_LEASE_SECONDS,
            reopen_completed=reopen_completed,
        ).single()
        if record is None:
            return False, False
        return bool(record["acquired"]), bool(record["completed"])

    return client.execute_write(_work)


def _repair_batch(client: Neo4jClient, owner_id: str, batch_size: int) -> tuple[int, int]:
    def _work(tx: ManagedTransaction) -> tuple[int, int]:
        candidates = list(
            tx.run(
                FETCH_LOYALTY_POINTS_MIGRATION_BATCH,
                migration_key=MIGRATION_KEY,
                owner_id=owner_id,
                source_system_keys=list(TARGET_LOYALTY_ORDER_SOURCES),
                batch_size=batch_size,
            )
        )
        if not candidates:
            return 0, 0
        updates = [_update(candidate) for candidate in candidates]
        record = tx.run(
            APPLY_LOYALTY_POINTS_MIGRATION_BATCH,
            migration_key=MIGRATION_KEY,
            owner_id=owner_id,
            lease_seconds=_LEASE_SECONDS,
            updates=updates,
        ).single()
        if record is None:
            raise RuntimeError("loyalty points migration lost its lease")
        return int(record["updated_field_count"]), len(updates)

    return client.execute_write(_work)


def _update(record: Record) -> _Update:
    source = record["source_system_key"]
    source_order_id = record["source_order_id"]
    if not isinstance(source, str) or not isinstance(source_order_id, str):
        raise RuntimeError("targeted Order has an invalid source identity")
    points_used = record["points_used"]
    points_gained = record["points_gained"]
    return {
        "source_system_key": source,
        "source_order_id": source_order_id,
        "expected_points_used": points_used,
        "expected_points_used_is_nan": _is_nan(points_used),
        "expected_points_gained": points_gained,
        "expected_points_gained_is_nan": _is_nan(points_gained),
        "points_used": normalize_loyalty_field(
            points_used,
            source=source,
            source_order_id=source_order_id,
            field="points_used",
        ),
        "points_gained": normalize_loyalty_field(
            points_gained,
            source=source,
            source_order_id=source_order_id,
            field="points_gained",
        ),
    }


def _is_nan(value: object) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _complete(client: Neo4jClient, owner_id: str) -> tuple[int, bool]:
    def _work(tx: ManagedTransaction) -> tuple[int, bool]:
        record = tx.run(
            COMPLETE_LOYALTY_POINTS_MIGRATION,
            migration_key=MIGRATION_KEY,
            owner_id=owner_id,
            source_system_keys=list(TARGET_LOYALTY_ORDER_SOURCES),
        ).single()
        if record is None:
            return -1, False
        return int(record["invalid_order_count"]), bool(record["completed"])

    return client.execute_write(_work)


def _release(client: Neo4jClient, owner_id: str) -> None:
    def _work(tx: ManagedTransaction) -> None:
        tx.run(
            RELEASE_LOYALTY_POINTS_MIGRATION,
            migration_key=MIGRATION_KEY,
            owner_id=owner_id,
        ).consume()

    client.execute_write(_work)


def _invalid_counts(record: Record | None) -> LoyaltyPointsInvalidCounts:
    if record is None:
        return LoyaltyPointsInvalidCounts(0, 0, 0)
    return LoyaltyPointsInvalidCounts(
        invalid_order_count=int(record["invalid_order_count"]),
        invalid_points_used_count=int(record["invalid_points_used_count"]),
        invalid_points_gained_count=int(record["invalid_points_gained_count"]),
    )
