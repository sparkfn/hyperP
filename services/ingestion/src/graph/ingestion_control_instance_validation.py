"""Runtime validation helpers for #272 control migration."""

from __future__ import annotations

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient
from src.graph.queries.ingestion_control_instance_validation import (
    COUNT_CONTROL_RELATIONSHIP_MISMATCHES,
    COUNT_INVALID_CONTROL_ROWS,
    COUNT_SOURCE_AMBIGUITIES,
    PROSPECTIVE_COLLISIONS,
)


def validate_rows_and_collisions(client: Neo4jClient, *, legacy_only: bool) -> None:
    def _read(tx: ManagedTransaction) -> tuple[int, int, int, int]:
        invalid = tx.run(COUNT_INVALID_CONTROL_ROWS, legacy_only=legacy_only).single()
        mismatches = tx.run(COUNT_CONTROL_RELATIONSHIP_MISMATCHES).single()
        ambiguities = tx.run(COUNT_SOURCE_AMBIGUITIES).single()
        collisions = tx.run(PROSPECTIVE_COLLISIONS).single()
        return (
            _int_or_zero(invalid, "invalid"),
            _int_or_zero(mismatches, "mismatches"),
            _int_or_zero(ambiguities, "ambiguities"),
            _int_or_zero(collisions, "collisions"),
        )

    invalid, mismatches, ambiguities, collisions = client.execute_read(_read)
    if invalid or mismatches or ambiguities or collisions:
        raise RuntimeError(
            "control-instance migration validation failed "
            f"invalid={invalid} relationships={mismatches} ambiguities={ambiguities} "
            f"collisions={collisions}"
        )


def _int_or_zero(record: Record | None, key: str) -> int:
    if record is None:
        return 0
    value: object = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"control-instance migration returned invalid {key}")
    return value
