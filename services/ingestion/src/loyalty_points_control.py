"""Operator control for the PHPPOS Order loyalty-points invariant."""

from __future__ import annotations

import argparse
import json
import sys

from src.config import get_settings
from src.graph.client import Neo4jClient
from src.graph.loyalty_points_migration import (
    count_invalid_loyalty_points,
    repair_loyalty_points,
)

_INVARIANT_FAILURE_EXIT = 2
_OPERATIONAL_FAILURE_EXIT = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "backfill"))
    parser.add_argument("--batch-size", type=int, default=500)
    return parser


def run(arguments: list[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    client: Neo4jClient | None = None
    try:
        client = Neo4jClient(get_settings())
        updated_count = 0
        if args.command == "backfill":
            updated_count = repair_loyalty_points(client, batch_size=args.batch_size)
        counts = count_invalid_loyalty_points(client)
        payload: dict[str, int | str] = {
            "command": args.command,
            "invalid_order_count": counts.invalid_order_count,
            "invalid_points_gained_count": counts.invalid_points_gained_count,
            "invalid_points_used_count": counts.invalid_points_used_count,
            "status": "ok" if counts.invalid_order_count == 0 else "invariant_failed",
        }
        if args.command == "backfill":
            payload["updated_field_count"] = updated_count
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0 if counts.invalid_order_count == 0 else _INVARIANT_FAILURE_EXIT
    except Exception as exc:
        print(f"loyalty points control failed: {type(exc).__name__}", file=sys.stderr)
        print(
            json.dumps(
                {"error_code": _error_code(exc), "status": "operational_error"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
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


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
