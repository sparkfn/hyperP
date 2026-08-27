"""Internal-only Celery-dispatch CLI for bounded standalone CRM census control."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Protocol

from src.standalone_crm_census_requests import operator_request_from_json, operator_request_json
from src.standalone_crm_census_tasks import (
    cancel_parent_census_task,
    classify_reserved_call_unknown_task,
    reconcile_parent_census_task,
    repair_child_publication_task,
    run_parent_census_task,
    start_parent_census_task,
    status_parent_census_task,
)


class CeleryDispatcher(Protocol):
    """Narrow task signature used by this CLI; never exposes control services directly."""

    def delay(self, *args: str) -> object: ...


TaskMap = dict[str, CeleryDispatcher]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--request-json", required=True)
    for command in ("status", "resume", "reconcile"):
        subparser = commands.add_parser(command)
        subparser.add_argument("census_id")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("census_id")
    cancel.add_argument("--actor", required=True)
    cancel.add_argument("--reason", required=True)
    repair = commands.add_parser("repair")
    repair.add_argument("publication_id")
    classify_unknown = commands.add_parser("classify-call-unknown")
    classify_unknown.add_argument("census_id")
    classify_unknown.add_argument("intent_id")
    return parser


def task_map() -> TaskMap:
    return {
        "start": start_parent_census_task,
        "status": status_parent_census_task,
        "cancel": cancel_parent_census_task,
        "resume": run_parent_census_task,
        "reconcile": reconcile_parent_census_task,
        "repair": repair_child_publication_task,
        "classify-call-unknown": classify_reserved_call_unknown_task,
    }


def dispatch(args: argparse.Namespace, *, tasks: TaskMap | None = None) -> object:
    """Validate CLI data and submit exactly one internal Celery task."""
    selected = task_map() if tasks is None else tasks
    command = args.command
    if command == "start":
        request = operator_request_from_json(args.request_json)
        return selected[command].delay(operator_request_json(request))
    if command == "cancel":
        return selected[command].delay(args.census_id, args.actor, args.reason)
    if command == "repair":
        return selected[command].delay(args.publication_id)
    if command == "classify-call-unknown":
        return selected[command].delay(args.census_id, args.intent_id)
    return selected[command].delay(args.census_id)


def main(argv: Sequence[str] | None = None, *, tasks: TaskMap | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = dispatch(args, tasks=tasks)
    task_id = getattr(result, "id", None)
    print(json.dumps({"command": args.command, "task_id": task_id}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
