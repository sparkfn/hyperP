"""CLI parser and status isolation tests for cleanup."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from intelligence.crm.activities.cleanup import cli
from intelligence.crm.activities.cleanup.status import CleanupStatus


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest="command", required=True)
    cli.add_parser(actions)
    return parser


def test_cli_has_exact_operation_signatures() -> None:
    parser = _parser()
    dry = parser.parse_args(
        [
            "cleanup",
            "dry-run",
            "--checkpoint-id",
            "checkpoint-a",
            "--accepted-run-id",
            "run-a",
            "--snapshot-id",
            "snapshot-a",
            "--manifest-digest",
            "a" * 64,
            "--environment-id",
            "environment-a",
            "--database-identity",
            "database-a",
            "--cleanup-run-id",
            "cleanup-a",
            "--batch-size",
            "1",
            "--quiescence-run-id",
            "quiescence-a",
            "--quiescence-digest",
            "c" * 64,
        ]
    )
    assert dry.batch_size == 1
    execute = parser.parse_args(
        [
            "cleanup",
            "execute",
            "--checkpoint-id",
            "checkpoint-a",
            "--accepted-run-id",
            "run-a",
            "--snapshot-id",
            "snapshot-a",
            "--manifest-digest",
            "a" * 64,
            "--environment-id",
            "environment-a",
            "--database-identity",
            "database-a",
            "--cleanup-run-id",
            "cleanup-a",
            "--receipt-run-id",
            "receipt-a",
            "--receipt-digest",
            "b" * 64,
            "--quiescence-run-id",
            "quiescence-a",
            "--quiescence-digest",
            "c" * 64,
        ]
    )
    assert not hasattr(execute, "batch_size")
    assert execute.quiescence_run_id == "quiescence-a"
    with pytest.raises(SystemExit):
        parser.parse_args(["cleanup", "status", "--batch-size", "1"])


def test_status_main_uses_only_read_only_status_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = CleanupStatus("cleanup-a", "a" * 64, "ready", 0, 0, 0, (), None, "b" * 64, "c" * 64)
    monkeypatch.setattr(cli, "_workspace", lambda: Path("workspace"))
    monkeypatch.setattr(cli, "read_status", lambda _workspace, _run: expected)
    arguments = argparse.Namespace(
        crm_activity_cleanup_command="status", cleanup_run_id="cleanup-a"
    )
    assert cli.main(arguments) == 0
