"""Emit and verify the issue #440 active/historical CI manifests."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from ci_support.selection_manifest import (  # noqa: E402
    ACTIVE_QUERY_SENTINELS,
    HISTORICAL_SOURCE_PATHS,
    HISTORICAL_TEST_MODULES,
    query_commands,
    tool_exclude_args,
    validate_default_nodes,
    validate_default_query_manifest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    excludes = subparsers.add_parser("tool-excludes")
    excludes.add_argument("--tool", choices=("ruff", "mypy"), required=True)
    nodes = subparsers.add_parser("verify-nodes")
    nodes.add_argument("--service", choices=("api", "ingestion"), required=True)
    nodes.add_argument("--path", type=Path, required=True)
    queries = subparsers.add_parser("query-manifest")
    queries.add_argument("--workflow", type=Path, required=True)
    subparsers.add_parser("historical-manifest")
    return parser


def _emit_historical_manifest() -> None:
    for path in sorted(HISTORICAL_TEST_MODULES):
        print(f"historical-test={path}")
    for path in HISTORICAL_SOURCE_PATHS:
        print(f"historical-source={path}")


def main() -> int:
    args = _parser().parse_args()
    if args.command == "tool-excludes":
        print("\n".join(tool_exclude_args(args.tool)))
        return 0
    if args.command == "verify-nodes":
        nodes = validate_default_nodes(args.service, args.path.read_text(encoding="utf-8"))
        for node in nodes:
            print(f"collected-node={node}")
        return 0
    if args.command == "query-manifest":
        workflow_text = args.workflow.read_text(encoding="utf-8")
        commands = validate_default_query_manifest(workflow_text)
        print(f"workflow={args.workflow.as_posix()}")
        for sentinel in ACTIVE_QUERY_SENTINELS:
            print(f"active-query-sentinel={sentinel}")
        for command in commands:
            print(f"configured-query={command}")
        return 0
    _emit_historical_manifest()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
