"""Run aggregate, read-only CRM-WON feasibility checks for issue #124.

This discovery command does not create snapshots, labels, training rows,
predictions, models, APIs, or worklists. It reports whether those follow-on
activities can be justified for the ``crm_won_30d`` outcome.
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
    DISCOVERY_DEAL_RECORDS,
    DISCOVERY_INTERACTION_RECORDS,
    DISCOVERY_LATE_ARRIVAL,
    DISCOVERY_SOURCE_COVERAGE,
)
from src.repositories.neo4j._utils import record_to_dict
from src.sales_prediction_discovery_config import StageMapping, load_stage_mapping
from src.sales_prediction_discovery_mapping import (
    DiscoveryRow,
    DiscoveryScalar,
    aggregate_deals,
    aggregate_history_capability,
    aggregate_interactions,
    aggregate_late_arrival,
    aggregate_source_coverage,
    capability_rows,
)
from src.sales_prediction_gate_runner import render_gate_markdown, run_gate, write_gate_json

_ENTITY_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CONFIGURATION_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class QueryParameters(TypedDict):
    report_cutoff_at: str
    entity_keys: list[str]
    late_arrival_seconds: int


@dataclass(frozen=True)
class DiscoverySettings:
    """Stable inputs recorded alongside a discovery run."""

    as_of_at: str
    report_cutoff_at: str
    entity_keys: tuple[str, ...]
    late_arrival_seconds: int
    configuration_version: str
    stage_mapping: StageMapping | None


@dataclass(frozen=True)
class DiscoveryOutput:
    """Privacy-safe, aggregate feasibility evidence."""

    generated_at: str
    settings: DiscoverySettings
    report_schema_version: str
    source_capability: list[DiscoveryRow]
    source_coverage: list[DiscoveryRow]
    deal_coverage: list[DiscoveryRow]
    history_capability: list[DiscoveryRow]
    interaction_coverage: list[DiscoveryRow]
    late_arrival: list[DiscoveryRow]
    mapping_status: list[DiscoveryRow]
    label_capability: list[DiscoveryRow]


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
    if len(normalized) > 80 or _CONFIGURATION_VERSION.fullmatch(normalized) is None:
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
    report_cutoff_at: str,
    entity_keys: list[str],
    late_arrival_seconds: int,
) -> list[DiscoveryRow]:
    async with get_session() as session:
        result = await session.run(
            query,
            report_cutoff_at=report_cutoff_at,
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
        "report_cutoff_at": settings.report_cutoff_at,
        "entity_keys": list(settings.entity_keys),
        "late_arrival_seconds": settings.late_arrival_seconds,
    }
    (
        source_coverage,
        private_deal_rows,
        private_interaction_rows,
        late_arrival,
    ) = await asyncio.gather(
        _query_rows(DISCOVERY_SOURCE_COVERAGE, **parameters),
        _query_rows(DISCOVERY_DEAL_RECORDS, **parameters),
        _query_rows(DISCOVERY_INTERACTION_RECORDS, **parameters),
        _query_rows(DISCOVERY_LATE_ARRIVAL, **parameters),
    )
    return DiscoveryOutput(
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        settings=settings,
        report_schema_version="issue-124-crm-won-v1",
        source_capability=capability_rows(settings.entity_keys),
        source_coverage=aggregate_source_coverage(source_coverage, settings.entity_keys),
        deal_coverage=aggregate_deals(
            private_deal_rows,
            settings.as_of_at,
            settings.report_cutoff_at,
            settings.entity_keys,
            _stage_catalog(settings.stage_mapping),
        ),
        history_capability=aggregate_history_capability(private_deal_rows, settings.entity_keys),
        interaction_coverage=aggregate_interactions(
            private_interaction_rows,
            settings.as_of_at,
            settings.report_cutoff_at,
            settings.entity_keys,
        ),
        late_arrival=aggregate_late_arrival(late_arrival, settings.entity_keys),
        mapping_status=_mapping_status(settings.stage_mapping, settings.entity_keys),
        label_capability=_label_capability(settings.stage_mapping, settings.entity_keys),
    )


def render_markdown(output: DiscoveryOutput) -> str:
    """Render aggregate evidence only; never include raw payloads or identifiers."""
    sections = [
        ("Source capability", output.source_capability),
        ("Source coverage at report cutoff", output.source_coverage),
        ("Deal coverage", output.deal_coverage),
        ("Snapshot and stage-history capability", output.history_capability),
        ("Optional interaction coverage", output.interaction_coverage),
        ("Late-arrival coverage", output.late_arrival),
        ("Stage-mapping status", output.mapping_status),
        ("CRM-WON label capability", output.label_capability),
    ]
    lines = [
        "# Sales prediction feasibility discovery output",
        "",
        f"Report schema: {_markdown_cell(output.report_schema_version)}",
        f"Generated: {output.generated_at}",
        f"As of: {output.settings.as_of_at}",
        f"Report cutoff: {output.settings.report_cutoff_at}",
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
            "This output is discovery evidence, not a label, dataset, model, or readiness "
            "decision. `crm_won_30d` remains unavailable unless approved mappings and "
            "authoritative stage-transition history exist.",
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
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--as-of-at", type=parse_as_of_at)
    parser.add_argument("--report-cutoff-at", type=parse_as_of_at)
    parser.add_argument("--entities", required=True, type=parse_entity_keys)
    parser.add_argument("--configuration-version", type=parse_non_empty)
    parser.add_argument("--late-arrival-hours", type=int, default=72)
    parser.add_argument("--stage-mapping", type=Path)
    parser.add_argument("--expected-mapping-version", type=parse_non_empty)
    parser.add_argument("--expected-policy-version", type=parse_non_empty)
    parser.add_argument("--selector-version", type=parse_non_empty, default="open-episode-entry-v1")
    parser.add_argument(
        "--eligibility-version", type=parse_non_empty, default="crm-won-eligibility-v1"
    )
    parser.add_argument("--restatement-version", type=parse_non_empty, default="authority-head-v1")
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    return parser


def _mapping_status(
    mapping: StageMapping | None, entity_keys: tuple[str, ...]
) -> list[DiscoveryRow]:
    if mapping is None:
        return [
            {
                "entity_key": entity_key,
                "mapping_status": "mapping_not_supplied",
                "mapping_artifact_valid": None,
                "entity_mapping_present": False,
                "policy_version": None,
                "configuration_hash": None,
                "approval_status": "approval_unverified",
                "claimed_approval_status": None,
                "external_approval_reference_status": "missing",
                "reopen_revert_policy_status": None,
            }
            for entity_key in entity_keys
        ]
    policies = {policy.entity_key: policy for policy in mapping.entities}
    rows: list[DiscoveryRow] = []
    for entity_key in entity_keys:
        policy = policies.get(entity_key)
        entity_mapping_present = policy is not None
        rows.append(
            {
                "entity_key": entity_key,
                "mapping_status": (
                    "mapping_supplied_unverified"
                    if entity_mapping_present
                    else "mapping_missing_for_entity"
                ),
                "mapping_artifact_valid": True,
                "entity_mapping_present": entity_mapping_present,
                "policy_version": mapping.policy_version,
                "configuration_hash": mapping.configuration_hash,
                "approval_status": mapping.approval_status,
                "claimed_approval_status": mapping.claimed_approval_status,
                "external_approval_reference_status": (
                    "supplied" if mapping.external_approval_reference is not None else "missing"
                ),
                "reopen_revert_policy_status": (
                    policy.reopen_revert_policy_status if policy is not None else None
                ),
            }
        )
    return rows


def _label_capability(
    mapping: StageMapping | None, entity_keys: tuple[str, ...]
) -> list[DiscoveryRow]:
    policies = {} if mapping is None else {item.entity_key: item for item in mapping.entities}
    rows: list[DiscoveryRow] = []
    for entity_key in entity_keys:
        reasons = ["authoritative_transition_history_missing", "horizon_unreconstructable"]
        if mapping is None:
            reasons.insert(0, "mapping_not_supplied")
        elif entity_key not in policies:
            reasons.insert(0, "mapping_missing_for_entity")
        elif mapping.approval_status != "approved":
            reasons.insert(0, "mapping_approval_unverified")
        policy = policies.get(entity_key)
        if policy is not None and policy.reopen_revert_policy_status != "defined":
            reasons.insert(0, "reopen_revert_policy_pending")
        rows.append(
            {
                "entity_key": entity_key,
                "label_status": "label_unavailable",
                "reasons": ",".join(reasons),
            }
        )
    return rows


def _stage_catalog(mapping: StageMapping | None) -> dict[str, frozenset[str]]:
    if mapping is None:
        return {}
    return {
        item.entity_key: frozenset(
            (
                *item.open_stage_ids,
                *item.won_stage_ids,
                *item.lost_stage_ids,
                *item.excluded_stage_ids,
            )
        )
        for item in mapping.entities
    }


def _write_json(path: Path, output: DiscoveryOutput) -> None:
    path.write_text(
        json.dumps(asdict(output), allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


async def _main(arguments: Sequence[str], stdout: TextIO) -> int:
    args = build_parser().parse_args(arguments)
    if args.json_output.resolve() == args.markdown_output.resolve():
        raise ValueError("JSON and Markdown output paths must be different")
    try:
        if args.gate:
            if args.expected_mapping_version is None or args.expected_policy_version is None:
                raise ValueError("gate mode requires expected mapping and policy versions")
            gate_output = await run_gate(
                entity_keys=args.entities,
                expected_mapping_version=args.expected_mapping_version,
                expected_policy_version=args.expected_policy_version,
                selector_version=args.selector_version,
                eligibility_version=args.eligibility_version,
                restatement_version=args.restatement_version,
            )
            write_gate_json(args.json_output, gate_output)
            args.markdown_output.write_text(render_gate_markdown(gate_output), encoding="utf-8")
            output_kind = "Gate 1"
        else:
            settings = _discovery_settings(args)
            output = await run_discovery(settings)
            _write_json(args.json_output, output)
            args.markdown_output.write_text(render_markdown(output), encoding="utf-8")
            output_kind = "aggregate discovery"
    finally:
        await close_driver()
    message = f"Wrote {output_kind} output to {args.json_output} and {args.markdown_output}"
    print(message, file=stdout)
    return 0


def _discovery_settings(args: argparse.Namespace) -> DiscoverySettings:
    if args.as_of_at is None or args.report_cutoff_at is None:
        raise ValueError("discovery mode requires --as-of-at and --report-cutoff-at")
    if args.configuration_version is None:
        raise ValueError("discovery mode requires --configuration-version")
    if args.late_arrival_hours < 1:
        raise ValueError("--late-arrival-hours must be positive")
    if _datetime_from_utc(args.as_of_at) > datetime.now(UTC):
        raise ValueError("--as-of-at must not be in the future")
    if _datetime_from_utc(args.report_cutoff_at) > datetime.now(UTC):
        raise ValueError("--report-cutoff-at must not be in the future")
    if _datetime_from_utc(args.report_cutoff_at) < _datetime_from_utc(args.as_of_at):
        raise ValueError("--report-cutoff-at must not precede --as-of-at")
    return DiscoverySettings(
        as_of_at=args.as_of_at,
        report_cutoff_at=args.report_cutoff_at,
        entity_keys=args.entities,
        late_arrival_seconds=args.late_arrival_hours * 60 * 60,
        configuration_version=args.configuration_version,
        stage_mapping=load_stage_mapping(args.stage_mapping) if args.stage_mapping else None,
    )


def _datetime_from_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the command-line entry point."""
    import sys

    return asyncio.run(_main(arguments if arguments is not None else sys.argv[1:], sys.stdout))


if __name__ == "__main__":
    raise SystemExit(main())
