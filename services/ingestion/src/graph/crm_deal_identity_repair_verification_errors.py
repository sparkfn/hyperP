"""Shared verification errors."""

from __future__ import annotations


class RepairVerificationDriftError(RuntimeError):
    """Raised when immutable or acknowledged verification evidence differs."""
