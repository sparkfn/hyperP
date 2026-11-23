"""CLI plan, apply, and verify commands for legacy NRIC rehashing."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from neo4j.exceptions import DriverError, Neo4jError
from pydantic import ValidationError

from src.config import get_settings
from src.graph.client import Neo4jClient
from src.nric_rehash_apply import run_nric_rehash_apply
from src.nric_rehash_handoff import validate_handoff_dir
from src.nric_rehash_plan import (
    NricRehashError,
    PlanReport,
    VerifyReport,
    build_plan_report,
    build_verify_report,
)
from src.nric_rehash_report import ApplyReport

_APPLY_EXIT_ONE_CODES = frozenset(
    {
        "null_scope_present",
        "unsupported_edge",
        "residual_edges",
        "missing_id",
        "unexpected_scope",
        "residual_plaintext",
        "handoff_write_failed",
    }
)


def _positive_batch_size(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer >= 1") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the plan/apply/verify CLI parser."""
    parser = argparse.ArgumentParser(prog="python -m src.nric_rehash")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "verify"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--batch-size", type=_positive_batch_size, default=500)
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("--batch-size", type=_positive_batch_size, default=500)
    apply_parser.add_argument(
        "--acknowledge-offline",
        action="store_true",
        help=(
            "Required. Records operator acknowledgement; it is NOT proof that writers are stopped."
        ),
    )
    apply_parser.add_argument("--expect-database", required=True)
    apply_parser.add_argument("--handoff-dir", required=True, type=Path)
    return parser


def _dispatch(
    command: Literal["plan", "apply", "verify"],
    client: Neo4jClient,
    *,
    batch_size: int,
    key: str,
    key_version: str,
    expected_database: str,
    acknowledge_offline: bool,
    handoff_dir: Path | None,
) -> tuple[PlanReport | ApplyReport | VerifyReport, int]:
    if command == "plan":
        report = build_plan_report(client, batch_size, key, key_version)
        return report, 0 if report["status"] == "ok" else 1
    if command == "apply":
        if handoff_dir is None:
            raise NricRehashError("handoff_dir_unsafe")
        return run_nric_rehash_apply(
            client,
            batch_size=batch_size,
            key=key,
            key_version=key_version,
            expected_database=expected_database,
            acknowledge_offline=acknowledge_offline,
            handoff_dir=handoff_dir,
        )
    report = build_verify_report(client, key_version)
    return report, 0 if report["ok"] else 1


def _resolve_apply_options(
    arguments: argparse.Namespace,
    command: Literal["plan", "apply", "verify"],
) -> tuple[str, bool, Path | None] | None:
    if command != "apply":
        return "", False, None
    if not arguments.acknowledge_offline:
        _print_error("offline_ack_required")
        return None
    handoff_dir: Path = arguments.handoff_dir
    try:
        validate_handoff_dir(handoff_dir)
    except NricRehashError as exc:
        _print_error(exc.code)
        return None
    return arguments.expect_database, True, handoff_dir


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and print sanitized aggregate output."""
    arguments = build_parser().parse_args(argv)
    command: Literal["plan", "apply", "verify"] = arguments.command
    batch_size: int = arguments.batch_size
    apply_options = _resolve_apply_options(arguments, command)
    if apply_options is None:
        return 2
    expected_database, acknowledge_offline, handoff_dir = apply_options
    try:
        settings = get_settings()
    except ValidationError:
        _print_error("invalid_configuration")
        return 2

    try:
        if not settings.nric_hmac_key or not settings.nric_hmac_key_version:
            raise NricRehashError("nric_key_missing")
        client = Neo4jClient(settings)
        try:
            report, exit_code = _dispatch(
                command,
                client,
                batch_size=batch_size,
                key=settings.nric_hmac_key,
                key_version=settings.nric_hmac_key_version,
                expected_database=expected_database,
                acknowledge_offline=acknowledge_offline,
                handoff_dir=handoff_dir,
            )
        finally:
            client.close()
    except NricRehashError as exc:
        _print_error(exc.code)
        if command == "apply" and exc.code in _APPLY_EXIT_ONE_CODES:
            return 1
        return 2
    except (Neo4jError, DriverError):
        _print_error("neo4j_error")
        return 2

    print(json.dumps(report, sort_keys=True))
    return exit_code


def _print_error(code: str) -> None:
    print(json.dumps({"status": "error", "code": code}, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
