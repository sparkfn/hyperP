"""Pre-import default CI selection gate for issue #440."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ci_support.selection_manifest import ACTIVE_PROFILE, HISTORICAL_PROFILE, is_historical_test


def _profile() -> str:
    profile = os.getenv("HYPERP_CI_PROFILE", ACTIVE_PROFILE)
    if profile not in {ACTIVE_PROFILE, HISTORICAL_PROFILE}:
        raise pytest.UsageError(f"unsupported HYPERP_CI_PROFILE: {profile}")
    if (
        profile == HISTORICAL_PROFILE
        and os.getenv("HYPERP_HISTORICAL_REACTIVATION_ACK") != "1"
    ):
        raise pytest.UsageError(
            "historical profile requires the restoration-command acknowledgment"
        )
    return profile


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool:
    """Block exact dormant modules before pytest imports them in the active profile."""
    del config
    return _profile() == ACTIVE_PROFILE and is_historical_test(collection_path)
