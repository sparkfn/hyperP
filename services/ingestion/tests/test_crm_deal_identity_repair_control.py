"""Argument-contract tests for the read-only CRM-deal repair operator command."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import json

import pytest
from src.crm_deal_identity_repair.cli import parse_arguments
from src.crm_deal_identity_repair_control import (
    _recorded_task_proof,
    _validate_runtime_gate,
)


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


def test_status_gate_remains_read_only_when_repair_flag_is_disabled() -> None:
    _validate_runtime_gate(
        SimpleNamespace(
            deployment_environment="staging",
            crm_deal_identity_repair_enabled=False,
        ),
        require_enabled=False,
    )


def test_control_commands_require_owner_token_revision_and_offline_task_evidence() -> None:
    for command in ("quiesce", "allocate", "pause", "resume"):
        with pytest.raises(SystemExit):
            parse_arguments((command, "--repair-id", "repair-310"))
    with pytest.raises(SystemExit):
        parse_arguments(
            (
                "quiesce",
                "--repair-id",
                "repair-310",
                "--owner-id",
                "owner",
                "--control-token",
                "token",
                "--expected-revision",
                "0",
            )
        )
    with pytest.raises(SystemExit):
        parse_arguments(
            (
                "allocate",
                "--repair-id",
                "repair-310",
                "--owner-id",
                "owner",
                "--control-token",
                "token",
                "--expected-revision",
                "0",
            )
        )


def test_control_parser_keeps_status_read_only_and_bounded_task_timeout() -> None:
    status = parse_arguments(("status", "--repair-id", "repair-310"))
    assert status.command == "status"
    arguments = parse_arguments(
        (
            "resume",
            "--repair-id",
            "repair-310",
            "--owner-id",
            "owner",
            "--control-token",
            "token",
            "--expected-revision",
            "2",
            "--task-proof-file",
            "offline-proof.json",
            "--task-timeout-seconds",
            "1.5",
        )
    )
    assert arguments.task_timeout_seconds == 1.5


def test_offline_task_proof_never_constructs_live_inspection_and_preserves_identities(
    tmp_path: Path,
) -> None:
    path = tmp_path / "proof.json"
    path.write_text(
        json.dumps(
            {
                "expected_workers": ["worker-a"],
                "responders": ["worker-a"],
                "tasks": [
                    {
                        "task_id": "task-310",
                        "task_name": "src.bitrix_backfill",
                        "queue": "ingestion",
                        "kwargs_digest": "sha256:kwargs",
                    }
                ],
                "broker_queued": False,
            }
        ),
        encoding="utf-8",
    )
    workers, tasks, inspector, broker = _recorded_task_proof(path)
    inspection = inspector.inspect(workers, tasks, 1.0)
    assert workers == ("worker-a",)
    assert inspection.proves_absence(
        expected_workers=workers, broker=broker, tasks=tasks, timeout_seconds=1.0
    )


def test_runtime_gate_is_default_off_but_status_is_staging_read_only() -> None:
    with pytest.raises(RuntimeError):
        _validate_runtime_gate(
            SimpleNamespace(
                deployment_environment="staging", crm_deal_identity_repair_enabled=False
            )
        )
    _validate_runtime_gate(
        SimpleNamespace(
            deployment_environment="staging", crm_deal_identity_repair_enabled=False
        ),
        require_enabled=False,
    )


def test_offline_task_proof_rejects_schema_extensions_duplicates_and_non_boolean_flags(
    tmp_path: Path,
) -> None:
    base = {
        "expected_workers": ["worker-a"],
        "responders": ["worker-a"],
        "tasks": [{"task_id": "task", "task_name": "name", "queue": "ingestion", "kwargs_digest": "digest"}],
        "broker_queued": False,
    }
    for changed in (
        {**base, "unexpected": True},
        {**base, "responders": ["worker-a", "worker-a"]},
        {**base, "inspection_failed": "false"},
        {**base, "timed_out": 0},
    ):
        path = tmp_path / "invalid-proof.json"
        path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(RuntimeError):
            _recorded_task_proof(path)
