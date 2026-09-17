"""Pre-import default CI selection gate for issue #440."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from ci_support.selection_manifest import (
    ACTIVE_PROFILE,
    HISTORICAL_PROFILE,
    REPOSITORY_ROOT,
    is_historical_test,
)


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


def _explicit_historical_targets(arguments: Sequence[str]) -> tuple[str, ...]:
    targets: list[str] = []
    for argument in arguments:
        path_text = argument.split("::", maxsplit=1)[0]
        path = Path(path_text)
        candidate = path if path.is_absolute() else REPOSITORY_ROOT / path
        if is_historical_test(candidate):
            targets.append(argument)
    return tuple(targets)


def _reject_explicit_historical_targets(arguments: Sequence[str]) -> None:
    targets = _explicit_historical_targets(arguments)
    if targets:
        raise pytest.UsageError(
            "active profile refuses direct historical test selection before import: "
            + ", ".join(targets)
        )


def pytest_cmdline_main(config: pytest.Config) -> int | None:
    """Reject direct historical paths/node IDs before pytest begins collection."""
    if _profile() == ACTIVE_PROFILE:
        _reject_explicit_historical_targets(config.args)
    return None


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool:
    """Block exact dormant modules during recursive active-profile collection."""
    del config
    return _profile() == ACTIVE_PROFILE and is_historical_test(Path(collection_path))
