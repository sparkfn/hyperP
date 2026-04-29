"""Shared utilities for Neo4j repository implementations."""

from __future__ import annotations

from src.graph.converters import GraphRecord, GraphValue


def record_to_dict(keys: list[str], values: list[GraphValue]) -> GraphRecord:
    return dict(zip(keys, values, strict=True))


def to_total(record: object | None) -> int:
    if record is None:
        return 0
    try:
        val = record["total"]  # type: ignore[index]  # neo4j Record supports subscript
        return int(val) if val is not None else 0
    except (KeyError, TypeError, ValueError):
        return 0
