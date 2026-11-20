"""CLI plan and verify commands for legacy NRIC rehashing."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Literal

from neo4j.exceptions import DriverError, Neo4jError
from pydantic import ValidationError

from src.config import get_settings
from src.graph.client import Neo4jClient
from src.nric_rehash_plan import (
    NricRehashError,
    PlanReport,
    VerifyReport,
    build_plan_report,
    build_verify_report,
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
    """Build the plan/verify CLI parser; future subcommands can be added here."""
    parser = argparse.ArgumentParser(prog="python -m src.nric_rehash")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "verify"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--batch-size", type=_positive_batch_size, default=500)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one plan/verify command and print exactly one sanitized JSON object."""
    arguments = build_parser().parse_args(argv)
    command: Literal["plan", "verify"] = arguments.command
    batch_size: int = arguments.batch_size
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
            report: PlanReport | VerifyReport
            exit_code: int
            if command == "plan":
                report = build_plan_report(
                    client,
                    batch_size,
                    settings.nric_hmac_key,
                    settings.nric_hmac_key_version,
                )
                exit_code = 0 if report["status"] == "ok" else 1
            else:
                report = build_verify_report(client, settings.nric_hmac_key_version)
                exit_code = 0 if report["ok"] else 1
        finally:
            client.close()
    except NricRehashError as exc:
        _print_error(exc.code)
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
