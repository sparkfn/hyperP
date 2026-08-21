"""Operator control for the person-list completeness-score invariant."""

from __future__ import annotations

import argparse
import json
import sys

from src.config import get_settings
from src.graph.client import Neo4jClient
from src.graph.migrations import (
    backfill_missing_person_completeness_scores,
    count_missing_person_completeness_scores,
)

_INVARIANT_FAILURE_EXIT = 2
_OPERATIONAL_FAILURE_EXIT = 1


def build_parser() -> argparse.ArgumentParser:
    """Build the command parser for read-only checks and explicit repair."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("check", "backfill"),
        help="check the invariant or backfill missing scores then verify it",
    )
    return parser


def run(arguments: list[str] | None = None) -> int:
    """Run the requested operation and return its process exit code."""
    args = build_parser().parse_args(arguments)
    client: Neo4jClient | None = None
    try:
        client = Neo4jClient(get_settings())
        if args.command == "check":
            return _check(client)
        updated = backfill_missing_person_completeness_scores(client, skip_if_completed=False)
        missing_count = count_missing_person_completeness_scores(client)
        _print(
            {
                "command": "backfill",
                "missing_count": missing_count,
                "status": "ok" if missing_count == 0 else "invariant_failed",
                "updated_count": updated,
            }
        )
        return 0 if missing_count == 0 else _INVARIANT_FAILURE_EXIT
    except Exception as exc:
        error_code = _error_code(exc)
        print(
            f"person completeness control failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        _print({"error_code": error_code, "status": "operational_error"})
        return _OPERATIONAL_FAILURE_EXIT
    finally:
        if client is not None:
            client.close()


def _error_code(exc: Exception) -> str:
    if isinstance(exc, (ValueError, TypeError)):
        return "configuration_error"
    if type(exc).__module__.startswith("neo4j"):
        return "neo4j_error"
    return "unexpected_error"


def _check(client: Neo4jClient) -> int:
    missing_count = count_missing_person_completeness_scores(client)
    _print(
        {
            "command": "check",
            "missing_count": missing_count,
            "status": "ok" if missing_count == 0 else "invariant_failed",
        }
    )
    return 0 if missing_count == 0 else _INVARIANT_FAILURE_EXIT


def _print(payload: dict[str, int | str]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main() -> None:
    """Run the control as a Python module entry point."""
    raise SystemExit(run())


if __name__ == "__main__":
    main()
