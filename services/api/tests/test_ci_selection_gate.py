"""Behavioral pre-import selection contracts for the active CI profile."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import conftest as selection_gate


def test_recursive_active_collection_ignores_exact_historical_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPERP_CI_PROFILE", "active")
    historical = "services/ingestion/tests/test_crm_deal_identity_repair_inventory.py"

    assert selection_gate.pytest_ignore_collect(historical, SimpleNamespace())


def test_active_profile_rejects_explicit_historical_path_and_node_before_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPERP_CI_PROFILE", "active")
    node = (
        "services/ingestion/tests/test_crm_deal_identity_repair_inventory.py::"
        "test_inventory_includes_historical_versions_and_active_inactive_links"
    )

    with pytest.raises(pytest.UsageError, match="refuses direct historical"):
        selection_gate.pytest_cmdline_main(SimpleNamespace(args=(node,)))


def test_historical_profile_requires_acknowledgment_before_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPERP_CI_PROFILE", "historical")
    monkeypatch.delenv("HYPERP_HISTORICAL_REACTIVATION_ACK", raising=False)

    with pytest.raises(pytest.UsageError, match="requires the restoration-command"):
        selection_gate.pytest_cmdline_main(SimpleNamespace(args=()))


def test_acknowledged_historical_profile_permits_explicit_historical_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPERP_CI_PROFILE", "historical")
    monkeypatch.setenv("HYPERP_HISTORICAL_REACTIVATION_ACK", "1")
    node = "services/api/tests/test_crm_deal_identity_repair_reader_classification.py::test_x"

    assert selection_gate.pytest_cmdline_main(SimpleNamespace(args=(node,))) is None
