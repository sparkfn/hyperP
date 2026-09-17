"""Finite shared resource policy for one bounded occurrence."""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.bounded_ingestion_models import Usage


@dataclass(frozen=True)
class BoundedIngestionBudget:
    max_records: int = 5_000
    max_source_requests: int = 500
    max_pages: int = 500
    max_bytes: int = 50_000_000
    max_extraction_calls: int = 500
    max_unit_seconds: float = 120.0
    max_graph_transaction_seconds: float = 30.0
    drain_reserve_seconds: float = 300.0
    max_graph_writers: int = 1

    def __post_init__(self) -> None:
        integer_values = (
            self.max_records,
            self.max_source_requests,
            self.max_pages,
            self.max_bytes,
            self.max_extraction_calls,
            self.max_graph_writers,
        )
        if any(value < 1 for value in integer_values):
            raise ValueError("bounded-ingestion caps must be positive")
        float_values = (
            self.max_unit_seconds,
            self.max_graph_transaction_seconds,
            self.drain_reserve_seconds,
        )
        if any(value <= 0 or not math.isfinite(value) for value in float_values):
            raise ValueError("bounded-ingestion durations must be positive and finite")
        if self.max_graph_transaction_seconds > self.max_unit_seconds:
            raise ValueError("graph timeout cannot exceed maximum unit duration")
        if self.drain_reserve_seconds <= self.max_unit_seconds:
            raise ValueError("drain reserve must exceed maximum unit duration")

    def permits(self, used: Usage, requested: Usage) -> bool:
        projected = used.add(requested)
        return (
            projected.records <= self.max_records
            and projected.source_requests <= self.max_source_requests
            and projected.pages <= self.max_pages
            and projected.bytes_read <= self.max_bytes
            and projected.extraction_calls <= self.max_extraction_calls
        )
