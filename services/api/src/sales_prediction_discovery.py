"""Run aggregate, read-only feasibility checks for issue #124.

This module deliberately does not create training rows, predictions, models, or
API endpoints. It produces only aggregated evidence needed to decide whether a
point-in-time ``conversion_30d`` dataset is feasible.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, TypedDict

from src.graph.client import close_driver, get_session
from src.graph.converters import GraphValue
from src.graph.queries.sales_prediction_discovery import (
    DISCOVERY_DEAL_ORDER_LINKAGE,
    DISCOVERY_DEAL_RECORDS,
    DISCOVERY_INTERACTION_RECORDS,
    DISCOVERY_LATE_ARRIVAL,
    DISCOVERY_SALES_RECORDS,
    DISCOVERY_SOURCE_COVERAGE,
)
from src.repositories.neo4j._utils import record_to_dict
from src.sales_prediction_discovery_mapping import (
    DiscoveryRow,
    DiscoveryScalar,
    aggregate_deals,
    aggregate_interactions,
    aggregate_sales,
)

_ENTITY_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_CONFIGURATION_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class QueryParameters(TypedDict):
    as_of_at: str
    entity_keys: list[str]
    late_arrival_seconds: int


@dataclass(frozen=True)
class DiscoverySettings:
    """Stable inputs recorded alongside a discovery run."""

    as_of_at: str
    entity_keys: tuple[str, ...]
    late_arrival_seconds: int
    configuration_version: str


@dataclass(frozen=True)
class DiscoveryOutput:
    """Privacy-safe, aggregate feasibility evidence."""

    generated_at: str
    settings: DiscoverySettings
    source_coverage: list[DiscoveryRow]
    deal_coverage: list[DiscoveryRow]
    interaction_coverage: list[DiscoveryRow]
    order_coverage: list[DiscoveryRow]
    deal_order_linkage: list[DiscoveryRow]
    late_arrival: list[DiscoveryRow]


def parse_as_of_at(value: str) -> str:
    """Validate and normalize an explicit UTC cutoff timestamp."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("must include an explicit UTC offset")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_entity_keys(value: str) -> tuple[str, ...]:
    """Parse a comma-separated entity list without admitting empty keys."""
    keys = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not keys:
        raise argparse.ArgumentTypeError("must include at least one entity key")
    invalid = [key for key in keys if _ENTITY_KEY.fullmatch(key) is None]
    if invalid:
        raise argparse.ArgumentTypeError("entity keys must use lowercase letters, numbers, _ or -")
    return keys


def parse_non_empty(value: str) -> str:
    """Reject blank configuration labels that cannot identify a reproducible run."""
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("must not be empty")
    if _CONFIGURATION_VERSION.fullmatch(normalized) is None:
        raise argparse.ArgumentTypeError(
            "must use only letters, numbers, period, underscore, or hyphen"
        )
    return normalized


def _to_discovery_scalar(value: GraphValue) -> DiscoveryScalar:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise ValueError(f"discovery query returned a non-scalar value: {type(value).__name__}")


async def _query_rows(
    query: str,
    *,
    as_of_at: str,
    entity_keys: list[str],
    late_arrival_seconds: int,
) -> list[DiscoveryRow]:
    async with get_session() as session:
        result = await session.run(
            query,
            as_of_at=as_of_at,
            entity_keys=entity_keys,
            late_arrival_seconds=late_arrival_seconds,
        )
        rows: list[DiscoveryRow] = []
        async for record in result:
            graph_row = record_to_dict(record.keys(), list(record.values()))
            rows.append({key: _to_discovery_scalar(value) for key, value in graph_row.items()})
        return rows


async def run_discovery(settings: DiscoverySettings) -> DiscoveryOutput:
    """Collect the fixed aggregate evidence set using read-only Neo4j sessions."""
    parameters: QueryParameters = {
        "as_of_at": settings.as_of_at,
        "entity_keys": list(settings.entity_keys),
        "late_arrival_seconds": settings.late_arrival_seconds,
    }
    (
        source_coverage,
        private_deal_rows,
        private_interaction_rows,
        private_sales_rows,
        deal_order_linkage,
        late_arrival,
    ) = await asyncio.gather(
        _query_rows(DISCOVERY_SOURCE_COVERAGE, **parameters),
        _query_rows(DISCOVERY_DEAL_RECORDS, **parameters),
        _query_rows(DISCOVERY_INTERACTION_RECORDS, **parameters),
        _query_rows(DISCOVERY_SALES_RECORDS, **parameters),
        _query_rows(DISCOVERY_DEAL_ORDER_LINKAGE, **parameters),
        _query_rows(DISCOVERY_LATE_ARRIVAL, **parameters),
    )
    return DiscoveryOutput(
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        settings=settings,
        source_coverage=source_coverage,
        deal_coverage=aggregate_deals(private_deal_rows, settings.as_of_at),
        interaction_coverage=aggregate_interactions(private_interaction_rows, settings.as_of_at),
        order_coverage=aggregate_sales(private_sales_rows, settings.as_of_at),
        deal_order_linkage=deal_order_linkage,
        late_arrival=late_arrival,
    )


def render_markdown(output: DiscoveryOutput) -> str:
    """Render aggregate evidence only; never include raw payloads or identifiers."""
    sections = [
        ("Source coverage", output.source_coverage),
        ("Deal coverage", output.deal_coverage),
        ("Interaction coverage", output.interaction_coverage),
        ("Order coverage", output.order_coverage),
        ("Deal-to-order linkage", output.deal_order_linkage),
        ("Late-arrival coverage", output.late_arrival),
    ]
    lines = [
        "# Sales prediction feasibility discovery output",
        "",
        f"Generated: {output.generated_at}",
        f"As of: {output.settings.as_of_at}",
        f"Configuration version: {_markdown_cell(output.settings.configuration_version)}",
        f"Entities: {_markdown_cell(', '.join(output.settings.entity_keys))}",
        "",
    ]
    for title, rows in sections:
        lines.extend([f"## {title}", ""])
        lines.extend(_render_table(rows))
        lines.append("")
    lines.extend(
        [
            "## Interpretation boundary",
            "",
            "This output is discovery evidence, not a model-readiness decision. "
            "Apply approved outcome mappings, maturity rules, and privacy review "
            "before choosing `go`, `collect_more_data`, `rules_only`, or `stop`.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_table(rows: list[DiscoveryRow]) -> list[str]:
    if not rows:
        return ["No rows returned."]
    columns = list(rows[0])
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    rendered = [header, separator]
    for row in rows:
        values = [_markdown_cell(row.get(column)) for column in columns]
        rendered.append("| " + " | ".join(values) + " |")
    return rendered


def _markdown_cell(value: DiscoveryScalar) -> str:
    flattened = " ".join(str(value if value is not None else "").splitlines())
    return html.escape(flattened, quote=True).replace("|", "\\|")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of-at", required=True, type=parse_as_of_at)
    parser.add_argument("--entities", required=True, type=parse_entity_keys)
    parser.add_argument("--configuration-version", required=True, type=parse_non_empty)
    parser.add_argument("--late-arrival-hours", type=int, default=72)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    return parser


def _write_json(path: Path, output: DiscoveryOutput) -> None:
    path.write_text(json.dumps(asdict(output), indent=2, sort_keys=True) + "\n", encoding="utf-8")


async def _main(arguments: Sequence[str], stdout: TextIO) -> int:
    args = build_parser().parse_args(arguments)
    if args.late_arrival_hours < 1:
        raise ValueError("--late-arrival-hours must be positive")
    if _datetime_from_utc(args.as_of_at) > datetime.now(UTC):
        raise ValueError("--as-of-at must not be in the future")
    if args.json_output.resolve() == args.markdown_output.resolve():
        raise ValueError("JSON and Markdown output paths must be different")
    settings = DiscoverySettings(
        as_of_at=args.as_of_at,
        entity_keys=args.entities,
        late_arrival_seconds=args.late_arrival_hours * 60 * 60,
        configuration_version=args.configuration_version,
    )
    try:
        output = await run_discovery(settings)
        _write_json(args.json_output, output)
        args.markdown_output.write_text(render_markdown(output), encoding="utf-8")
    finally:
        await close_driver()
    message = f"Wrote aggregate discovery output to {args.json_output} and {args.markdown_output}"
    print(message, file=stdout)
    return 0


def _datetime_from_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the command-line entry point."""
    import sys

    return asyncio.run(_main(arguments if arguments is not None else sys.argv[1:], sys.stdout))


if __name__ == "__main__":
    raise SystemExit(main())
