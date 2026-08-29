"""POSIX-independent CLI argument contract for CRM-deal graph discovery."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse graph-discovery arguments without importing artifact filesystem code."""
    parser = argparse.ArgumentParser(
        prog="python -m src.crm_deal_identity_repair_control",
        description="Seal staging-only graph discovery for historical Bitrix CRM-deal repair.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("inventory", "qualify", "quiesce", "allocate", "status", "pause", "resume"),
        default="inventory",
    )
    parser.add_argument("--repair-id", required=True)
    parser.add_argument("--source-contract-uuid")
    parser.add_argument("--source-system", default="bitrix_chat")
    parser.add_argument(
        "--representative-replay-limit",
        "--negative-control-limit",
        dest="representative_replay_limit",
        type=int,
        default=100,
    )
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--artifact-id")
    parser.add_argument("--source-instance-id", default="legacy-default")
    parser.add_argument("--control-instance-id", default="legacy-default")
    parser.add_argument("--approval-reference")
    parser.add_argument("--unit-ceiling", type=int)
    parser.add_argument("--stop-condition", action="append", default=[])
    parser.add_argument("--rollback-authority-reference")
    parser.add_argument("--rollback-authority-policy")
    parser.add_argument("--owner-id")
    parser.add_argument("--control-token")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--approval-overlay")
    parser.add_argument("--task-proof-file")
    parser.add_argument("--task-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--stale-run-id")
    arguments = parser.parse_args(argv)
    _validate_arguments(parser, arguments)
    return arguments


def _validate_arguments(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> None:
    if arguments.retention_days < 1:
        parser.error("--retention-days must be positive")
    if arguments.representative_replay_limit < 1:
        parser.error("--representative-replay-limit must be positive")
    if arguments.command == "inventory" and not arguments.source_contract_uuid:
        parser.error("--source-contract-uuid is required for inventory")
    if arguments.command in {"quiesce", "allocate", "pause", "resume"}:
        required_control = ("owner_id", "control_token", "expected_revision")
        if any(getattr(arguments, name) in (None, "") for name in required_control):
            parser.error("repair control commands require owner, token, and expected revision")
        if arguments.expected_revision < 0:
            parser.error("--expected-revision must be non-negative")
        if arguments.command == "allocate" and not arguments.approval_overlay:
            parser.error("allocate requires --approval-overlay")
        if arguments.command in {"quiesce", "resume"}:
            if not arguments.task_proof_file:
                parser.error(
                    "quiesce and resume require --task-proof-file; live inspection is disabled"
                )
            if arguments.task_timeout_seconds <= 0:
                parser.error("--task-timeout-seconds must be positive")
    if arguments.command == "qualify":
        required = (
            "source_contract_uuid",
            "artifact_id",
            "approval_reference",
            "unit_ceiling",
            "rollback_authority_reference",
            "rollback_authority_policy",
        )
        if (
            any(getattr(arguments, name) in (None, "") for name in required)
            or not arguments.stop_condition
        ):
            parser.error(
                "qualify requires artifact, boundary, approval, stop-condition, "
                "and rollback arguments"
            )
        if arguments.unit_ceiling < 1:
            parser.error("--unit-ceiling must be positive")
