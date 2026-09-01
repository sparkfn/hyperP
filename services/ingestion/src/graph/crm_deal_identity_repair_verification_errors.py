"""Shared verification errors."""

from __future__ import annotations


class RepairVerificationDriftError(RuntimeError):
    """Raised when immutable or acknowledged verification evidence differs."""


class RepairVerificationReplayRaceError(RuntimeError):
    """Internal signal that an exact delivery lost to an acknowledged commit.

    The signal deliberately aborts the current write transaction before it can
    perform any derived-state writes.  The repository catches only this type
    and reopens the operation as an acknowledged, read-only replay.
    """
