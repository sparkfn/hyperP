"""Operator control for the maintained Person-list summary counter (issue #489).

The person-list summary is a maintained projection: its six published values
are refreshed by the nightly reconciliation task and, on demand, by the
explicit ``backfill`` command below. The command recomputes the authoritative
aggregates and publishes them in a single write transaction, activating the
counter when it was not ready.

There is no read-only variant: a check that recomputes repairs the counter, so
the command is deliberately the only thing this tool does. It never runs
automatically at startup; it is an operator action.

Invoke with ``python -m src.person_list_summary_control backfill``.
"""

from __future__ import annotations

import argparse
import json
import sys

from pydantic.types import JsonValue

from src.config import get_settings
from src.graph.client import Neo4jClient
from src.graph.person_list_summary import (
    backfill_person_list_summary,
    person_list_summary_field_drifts,
)

_OPERATIONAL_FAILURE_EXIT = 1


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser of the person-list summary control."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("backfill",),
        help="Recompute and publish the counter; 'backfill' is the only command.",
    )
    return parser


def run(arguments: list[str] | None = None) -> int:
    """Run the control and return its process exit code."""
    args = build_parser().parse_args(arguments)
    client: Neo4jClient | None = None
    try:
        client = Neo4jClient(get_settings())
        publication = backfill_person_list_summary(client)
        drift_payload: dict[str, JsonValue] = {}
        for report in person_list_summary_field_drifts(publication):
            drift_payload[report.field] = report.drift if report.drift is not None else "invalid"
        payload: dict[str, JsonValue] = {
            "activated": publication.activated,
            "active_person_count": publication.new.active_person_count,
            "all_deals_count": publication.new.all_deals_count,
            "command": args.command,
            "deals_this_month_count": publication.new.deals_this_month_count,
            "drift": drift_payload,
            "high_risk_count": publication.new.high_risk_count,
            "high_value_count": publication.new.high_value_count,
            "month": publication.new_month,
            "no_contact_count": publication.new.no_contact_count,
            "previous_month": publication.previous.month,
            "status": "ok",
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(
            f"Person-list summary control failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        print(json.dumps({"status": "operational_error"}, separators=(",", ":")))
        return _OPERATIONAL_FAILURE_EXIT
    finally:
        if client is not None:
            client.close()


def main() -> None:
    """Entry point of the person-list summary control."""
    raise SystemExit(run())


if __name__ == "__main__":
    main()
