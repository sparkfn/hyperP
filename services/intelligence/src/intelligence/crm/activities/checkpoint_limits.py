"""Typed resource ceilings for durable CRM activity checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Recovery deliberately shares the persisted byte and entry ceilings rather
# than adding a third caller-configured limit. Keep the public two-argument
# CheckpointLimits constructor stable; this short wall-clock cap only prevents
# an attacker-controlled checkpoint directory from monopolising resume.
TEMP_RECOVERY_MAX_SECONDS: Final[float] = 1.0


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
