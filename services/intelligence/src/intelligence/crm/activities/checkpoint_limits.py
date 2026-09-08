"""Typed resource ceilings for durable CRM activity checkpoints."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CheckpointLimits:
    """Maximum durable checkpoint bytes and descendant entries."""

    max_bytes: int
    max_entries: int

    def __post_init__(self) -> None:
        for value, field in (
            (self.max_bytes, "checkpoint max_bytes"),
            (self.max_entries, "checkpoint max_entries"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field} must be a positive integer")
