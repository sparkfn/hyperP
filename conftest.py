"""Root pytest hooks for active/historical CI selection."""

from __future__ import annotations

from ci_support.pytest_selection_gate import pytest_cmdline_main, pytest_ignore_collect

__all__ = ["pytest_cmdline_main", "pytest_ignore_collect"]
