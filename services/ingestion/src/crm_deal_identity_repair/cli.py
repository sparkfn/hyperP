"""POSIX-independent CLI argument contract for CRM-deal repair control."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

_INTEGRATION_COMMANDS = (
    "apply",
    "verify",
    "rollback-status",
    "rollback",
    "accept",
    "release-dispatch",
)
_UNIT_COMMANDS = frozenset({"apply", "verify", "rollback-status", "rollback"})
_ROLLBACK_COMMANDS = frozenset({"rollback-status", "rollback"})
_CONTROL_COMMANDS = frozenset({"quiesce", "allocate", "pause", "resume", *_INTEGRATION_COMMANDS})


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m src.crm_deal_identity_repair_control")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("inventory", "qualify", "status", *_CONTROL_COMMANDS),
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
    parser.add_argument("--run-id")
    parser.add_argument("--owner-id")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--approval-id")
    parser.add_argument("--unit-id")
    parser.add_argument("--authorization-reference")
    parser.add_argument("--predecessor-transition-id")
    arguments = parser.parse_args(argv)
    _validate_arguments(parser, arguments)
    return arguments


def _validate_arguments(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> None:
    if arguments.retention_days < 1 or arguments.representative_replay_limit < 1:
        parser.error("retention and representative replay limits must be positive")
    if arguments.command == "inventory" and not arguments.source_contract_uuid:
        parser.error("--source-contract-uuid is required for inventory")
    _validate_qualification(parser, arguments)
    _validate_control(parser, arguments)
    _validate_integration(parser, arguments)


def _validate_qualification(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> None:
    if arguments.command != "qualify":
        return
    required = (
        "source_contract_uuid",
        "artifact_id",
        "approval_reference",
        "unit_ceiling",
        "rollback_authority_reference",
        "rollback_authority_policy",
    )
    if any(getattr(arguments, name) in (None, "") for name in required):
        parser.error("qualify requires artifact, boundary, approval, and rollback arguments")
    if not arguments.stop_condition or arguments.unit_ceiling < 1:
        parser.error("qualify requires stop conditions and a positive unit ceiling")


def _validate_control(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> None:
    if arguments.command not in _CONTROL_COMMANDS:
        return
    required = ("run_id", "owner_id", "expected_revision")
    if any(getattr(arguments, name) in (None, "") for name in required):
        parser.error(f"{arguments.command} requires run ownership and expected revision")
    if arguments.expected_revision < 0:
        parser.error("--expected-revision must be non-negative")
    if arguments.command == "allocate" and not arguments.approval_id:
        parser.error("allocate requires --approval-id")


def _validate_integration(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> None:
    if arguments.command not in _INTEGRATION_COMMANDS:
        return
    if not arguments.approval_id:
        parser.error(f"{arguments.command} requires --approval-id")
    if (arguments.command in _UNIT_COMMANDS) != (arguments.unit_id is not None):
        parser.error(f"{arguments.command} has an invalid --unit-id scope")
    evidence = (arguments.authorization_reference, arguments.predecessor_transition_id)
    if arguments.command in _ROLLBACK_COMMANDS:
        if any(not value for value in evidence):
            parser.error(f"{arguments.command} requires rollback authorization evidence")
    elif any(value is not None for value in evidence):
        parser.error(f"{arguments.command} does not accept rollback authorization evidence")
