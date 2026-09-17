"""Named root-pytest selection gate for active versus historical validation."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from ci_support.selection_manifest import (
    ACTIVE_PROFILE,
    HISTORICAL_PROFILE,
    is_historical_test,
)


def profile() -> str:
    """Return the configured profile and require acknowledgment for historical mode."""
    value = os.getenv("HYPERP_CI_PROFILE", ACTIVE_PROFILE)
    if value not in {ACTIVE_PROFILE, HISTORICAL_PROFILE}:
        raise pytest.UsageError(f"unsupported HYPERP_CI_PROFILE: {value}")
    if value == HISTORICAL_PROFILE and os.getenv("HYPERP_HISTORICAL_REACTIVATION_ACK") != "1":
        raise pytest.UsageError(
            "historical profile requires the restoration-command acknowledgment"
        )
    return value


def explicit_historical_targets(
    arguments: Sequence[str],
    invocation_dir: Path,
) -> tuple[str, ...]:
    """Resolve pytest initial targets from its invocation directory before import."""
    targets: list[str] = []
    for argument in arguments:
        path = Path(argument.split("::", maxsplit=1)[0])
        candidate = path if path.is_absolute() else invocation_dir / path
        if is_historical_test(candidate):
            targets.append(argument)
    return tuple(targets)


def reject_explicit_historical_targets(
    arguments: Sequence[str],
    invocation_dir: Path,
) -> None:
    """Reject direct dormant paths/node IDs before pytest collects their modules."""
    targets = explicit_historical_targets(arguments, invocation_dir)
    if targets:
        raise pytest.UsageError(
            "active profile refuses direct historical test selection before import: "
            + ", ".join(targets)
        )


def pytest_cmdline_main(config: pytest.Config) -> int | None:
    """Run the direct-target gate at pytest startup, before collection/import."""
    if profile() == ACTIVE_PROFILE:
        reject_explicit_historical_targets(config.args, Path(config.invocation_params.dir))
    return None


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool:
    """Block exact dormant modules during recursive active-profile collection."""
    del config
    return profile() == ACTIVE_PROFILE and is_historical_test(Path(collection_path))
