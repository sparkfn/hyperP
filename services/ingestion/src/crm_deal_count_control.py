"""Operator control for the Person CRM deal-count projection invariant."""

from __future__ import annotations

import argparse
import json
import sys

from src.config import get_settings
from src.graph.client import Neo4jClient
from src.graph.crm_deal_count import inspect_crm_deal_count_invariant, repair_crm_deal_counts

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
            updated_count = repair_crm_deal_counts(client, batch_size=args.batch_size)
        invariant = inspect_crm_deal_count_invariant(client)
        payload: dict[str, bool | int | str] = {
            "command": args.command,
            "drifted_person_count": invariant.drifted_person_count,
            "index_online": invariant.index_online,
            "invalid_person_count": invariant.invalid_person_count,
            "status": "ok" if invariant.valid else "invariant_failed",
        }
        if args.command == "backfill":
            payload["updated_person_count"] = updated_count
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0 if invariant.valid else _INVARIANT_FAILURE_EXIT
    except Exception as exc:
        print(f"CRM deal-count control failed: {type(exc).__name__}", file=sys.stderr)
        print(json.dumps({"status": "operational_error"}, separators=(",", ":")))
        return _OPERATIONAL_FAILURE_EXIT
    finally:
        if client is not None:
            client.close()


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
