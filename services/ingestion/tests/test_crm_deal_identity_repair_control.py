"""Argument-contract tests for the read-only CRM-deal repair operator command."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from src.crm_deal_identity_repair.cli import parse_arguments
from src.crm_deal_identity_repair_control import _validate_runtime_gate


def test_inventory_command_requires_separate_artifact_identity() -> None:
    arguments = parse_arguments(
        (
            "inventory",
            "--repair-id",
            "issue251-staging-v1",
            "--source-contract-uuid",
            "12345678-1234-5678-9234-567812345678",
        )
    )

    assert arguments.source_system == "bitrix_chat"
    assert arguments.representative_replay_limit == 100


def test_inventory_command_supports_legacy_replay_limit_alias() -> None:
    arguments = parse_arguments(
        (
            "inventory",
            "--repair-id",
            "issue251-staging-v1",
            "--source-contract-uuid",
            "12345678-1234-5678-9234-567812345678",
            "--negative-control-limit",
            "7",
        )
    )

    assert arguments.representative_replay_limit == 7


def test_inventory_command_rejects_a_zero_retention_period() -> None:
    with pytest.raises(SystemExit):
        parse_arguments(
            (
                "inventory",
                "--repair-id",
                "issue251-staging-v1",
                "--source-contract-uuid",
                "12345678-1234-5678-9234-567812345678",
                "--retention-days",
                "0",
            )
        )


def test_inventory_command_requires_staging_and_explicit_enablement() -> None:
    with pytest.raises(RuntimeError, match="DEPLOYMENT_ENVIRONMENT=staging"):
        _validate_runtime_gate(
            SimpleNamespace(
                deployment_environment="development",
                crm_deal_identity_repair_enabled=True,
            )
        )
    with pytest.raises(RuntimeError, match="CRM_DEAL_IDENTITY_REPAIR_ENABLED=true"):
        _validate_runtime_gate(
            SimpleNamespace(
                deployment_environment="staging",
                crm_deal_identity_repair_enabled=False,
            )
        )
