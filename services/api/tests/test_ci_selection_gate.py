"""Behavioral root-plugin contracts for active historical-test selection."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ci_support import pytest_selection_gate as gate

_ROOT = Path(__file__).resolve().parents[3]
_HISTORICAL_ROOT_PATH = "services/ingestion/tests/test_crm_deal_identity_repair_inventory.py"


def test_recursive_active_collection_ignores_exact_historical_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPERP_CI_PROFILE", "active")

    assert gate.pytest_ignore_collect(_ROOT / _HISTORICAL_ROOT_PATH, object())


@pytest.mark.parametrize(
    ("invocation_dir", "target"),
    (
        (_ROOT, _HISTORICAL_ROOT_PATH),
        (_ROOT / "services/ingestion", "tests/test_crm_deal_identity_repair_inventory.py::test_x"),
        (_ROOT / "services/api", str((_ROOT / _HISTORICAL_ROOT_PATH).resolve()) + "::test_x"),
    ),
)
def test_explicit_historical_targets_resolve_from_pytest_invocation_directory(
    invocation_dir: Path,
    target: str,
) -> None:
    assert gate.explicit_historical_targets((target,), invocation_dir) == (target,)


@pytest.mark.parametrize(
    ("invocation_dir", "target"),
    (
        (_ROOT, _HISTORICAL_ROOT_PATH),
        (_ROOT / "services/ingestion", "tests/test_crm_deal_identity_repair_inventory.py"),
        (_ROOT / "services/api", str((_ROOT / _HISTORICAL_ROOT_PATH).resolve())),
    ),
)
def test_root_plugin_rejects_explicit_historical_targets_before_import(
    invocation_dir: Path,
    target: str,
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", target],
        cwd=invocation_dir,
        env={**os.environ, "HYPERP_CI_PROFILE": "active"},
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "active profile refuses direct historical test selection before import" in result.stderr


def test_historical_profile_requires_acknowledgment_before_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPERP_CI_PROFILE", "historical")
    monkeypatch.delenv("HYPERP_HISTORICAL_REACTIVATION_ACK", raising=False)

    with pytest.raises(pytest.UsageError, match="requires the restoration-command"):
        gate.profile()


def test_acknowledged_historical_profile_permits_explicit_historical_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPERP_CI_PROFILE", "historical")
    monkeypatch.setenv("HYPERP_HISTORICAL_REACTIVATION_ACK", "1")
    node = (
        _HISTORICAL_ROOT_PATH
        + "::test_inventory_includes_historical_versions_and_active_inactive_links"
    )

    assert gate.profile() == "historical"
    assert gate.explicit_historical_targets((node,), _ROOT) == (node,)
