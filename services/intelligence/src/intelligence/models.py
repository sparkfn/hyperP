"""Strict durable value types for the Intelligence foundation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RunState = Literal[
    "queued",
    "running",
    "publishing",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "stale_recovered",
]
TerminalRunState = Literal["completed", "failed", "cancelled", "timed_out", "stale_recovered"]


@dataclass(frozen=True)
class WorkspaceLayout:
    """The versioned private workspace locations owned by this runtime."""

    root: Path
    state_directory: Path
    state_database: Path
    staging: Path
    runs: Path
    manifests: Path
    rejected_manifests: Path
    logs: Path
    outputs: Path
    backups: Path


@dataclass(frozen=True)
class Run:
    """A durable runtime record without configuration, arguments, or secrets."""

    run_id: str
    command: str
    state: RunState
    fence: int
    created_at: float
    heartbeat_at: float | None
    cancellation_requested: bool = False
    recovery_reason: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    limits: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class Health:
    """CLI health result."""

    healthy: bool
    reason: str | None


@dataclass(frozen=True)
class OutputInventory:
    """One accepted immutable output file."""

    relative_path: str
    sha256: str
    byte_count: int


@dataclass(frozen=True)
class RunLogInventory:
    """Checksummed bounded NDJSON run log evidence."""

    relative_path: str
    sha256: str
    byte_count: int


@dataclass(frozen=True)
class ReconciledPublication:
    """A terminal outcome recovered after interrupted output publication."""

    run: Run
    outputs: tuple[OutputInventory, ...]
    state: TerminalRunState
    reason: str | None
