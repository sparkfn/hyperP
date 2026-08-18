"""Read-only orchestration and aggregate rendering for CRM-WON Gate 1."""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path

from src.graph.client import get_session
from src.graph.converters import GraphValue
from src.graph.queries.sales_prediction_gate import (
    GATE_DEAL_VERSIONS_FOR_PARENTS,
    GATE_RELEASE,
    GATE_STAGE_EVENTS_PAGE,
)
from src.repositories.neo4j._utils import record_to_dict
from src.sales_prediction_gate_labels import (
    build_labels,
    parse_deal_rows,
    parse_stage_rows,
    parse_timestamp,
)
from src.sales_prediction_gate_models import GateRelease, GateReport
from src.sales_prediction_gate_report import build_gate_report, report_as_dict

type GateScalar = str | int | float | bool | None
type GateRow = dict[str, GateScalar]
type GateParameter = str | int | list[dict[str, str]] | None

_PAGE_SIZE = 2_000


async def run_gate(
    *,
    entity_keys: tuple[str, ...],
    expected_mapping_version: str,
    expected_policy_version: str,
    selector_version: str,
    eligibility_version: str,
    restatement_version: str,
) -> GateReport:
    """Run Gate 1 only against the persisted accepted analytical release."""
    release = _parse_gate_release(
        await _query_rows(GATE_RELEASE), expected_mapping_version, expected_policy_version
    )
    stage_rows, deal_rows = await _paged_evidence()
    final_release = _parse_gate_release(
        await _query_rows(GATE_RELEASE), expected_mapping_version, expected_policy_version
    )
    if final_release != release:
        raise ValueError("accepted CRM stage release changed during Gate 1 execution")
    events, invalid_parents = parse_stage_rows(stage_rows)
    versions = parse_deal_rows(deal_rows)
    labels = build_labels(
        release, events, versions, entity_keys, invalid_event_parents=invalid_parents
    )
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return build_gate_report(
        labels,
        release,
        entity_keys,
        generated_at=generated_at,
        selector_version=selector_version,
        eligibility_version=eligibility_version,
        restatement_version=restatement_version,
    )


async def _query_rows(
    query: str, parameters: dict[str, GateParameter] | None = None
) -> list[GateRow]:
    async with get_session() as session:
        result = await session.run(query, parameters or {})
        rows: list[GateRow] = []
        async for record in result:
            graph_row = record_to_dict(record.keys(), list(record.values()))
            rows.append({key: _to_scalar(value) for key, value in graph_row.items()})
        return rows


async def _paged_evidence() -> tuple[list[GateRow], list[GateRow]]:
    stage_rows: list[GateRow] = []
    deal_rows_by_version: dict[str, GateRow] = {}
    after_event_identity: str | None = None
    while True:
        page = await _query_rows(
            GATE_STAGE_EVENTS_PAGE,
            {"after_event_identity": after_event_identity, "limit": _PAGE_SIZE},
        )
        if not page:
            break
        stage_rows.extend(page)
        parents = _page_parents(page)
        for row in await _query_rows(GATE_DEAL_VERSIONS_FOR_PARENTS, {"parents": parents}):
            version_key = _required_text(row, "version_key")
            previous = deal_rows_by_version.get(version_key)
            if previous is not None and previous != row:
                raise ValueError("Gate 1 deal version changed during paginated reads")
            deal_rows_by_version[version_key] = row
        next_cursor = _required_text(page[-1], "event_identity")
        if after_event_identity is not None and next_cursor <= after_event_identity:
            raise ValueError("Gate 1 stage pagination did not advance")
        after_event_identity = next_cursor
        if len(page) < _PAGE_SIZE:
            break
    return stage_rows, list(deal_rows_by_version.values())


def _page_parents(page: list[GateRow]) -> list[dict[str, str]]:
    parents = {
        (
            _required_text(row, "parent_source_system"),
            _required_text(row, "parent_source_record_id"),
        )
        for row in page
    }
    return [
        {"source_system": source_system, "source_record_id": source_record_id}
        for source_system, source_record_id in sorted(parents)
    ]


def _required_text(row: GateRow, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Gate 1 query row has invalid {key}")
    return value


def _to_scalar(value: GraphValue) -> GateScalar:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise ValueError(f"Gate 1 query returned a non-scalar value: {type(value).__name__}")


def _parse_gate_release(
    rows: list[GateRow], expected_mapping: str, expected_policy: str
) -> GateRelease:
    if len(rows) != 1:
        raise ValueError("accepted CRM stage release query returned an invalid row count")
    row = rows[0]
    mapping = row.get("mapping_version")
    policy = row.get("policy_version")
    accepted = parse_timestamp(row.get("accepted_at"))
    max_event = parse_timestamp(row.get("max_event_at"))
    max_available = parse_timestamp(row.get("max_available_at"))
    if mapping != expected_mapping or policy != expected_policy:
        raise ValueError("accepted CRM stage release version does not match Gate 1 inputs")
    if accepted is None or max_event is None or max_available is None:
        raise ValueError("accepted CRM stage release has no valid evidence cutoff")
    projection_count = _integer(row, "projection_count")
    source_complete = (
        bool(row.get("enabled"))
        and row.get("boundary_bound") is True
        and row.get("reconciliation_bound") is True
        and row.get("mapping_bound") is True
    )
    release_consistent = (
        bool(row.get("enabled"))
        and projection_count > 0
        and projection_count == _integer(row, "distinct_projection_count")
        and _integer(row, "invalid_projection_timestamp_count") == 0
        and _integer(row, "wrong_mapping_count") == 0
        and _integer(row, "wrong_policy_count") == 0
    )
    return GateRelease(
        enabled=bool(row.get("enabled")),
        mapping_version=expected_mapping,
        policy_version=expected_policy,
        accepted_at=accepted,
        evidence_cutoff_at=min(accepted, max_event, max_available),
        source_accounting_complete=source_complete,
        analytical_release_consistent=release_consistent,
        restated_event_count=_integer(row, "restated_event_count"),
    )


def _integer(row: GateRow, key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Gate 1 release row has invalid {key}")
    return value


def render_gate_markdown(report: GateReport) -> str:
    """Render only aggregate Gate 1 evidence; restricted provenance stays private."""
    lines = [
        "# CRM WON 30-day Gate 1",
        "",
        f"Report schema: {_cell(report.metadata.report_schema_version)}",
        f"Generated: {report.generated_at}",
        f"Selector: {_cell(report.metadata.selector_version)}",
        f"Mapping: {_cell(report.metadata.mapping_version)}",
        f"Policy: {_cell(report.metadata.policy_version)}",
        f"Eligibility: {_cell(report.metadata.eligibility_version)}",
        f"Accepted source boundary: {report.metadata.accepted_source_boundary_status}",
        f"Evidence cutoff: {report.metadata.evidence_cutoff_status}",
        f"Restatement: {report.metadata.restatement_status}",
        "",
        "## Population decisions",
        "",
    ]
    decision_rows: list[GateRow] = []
    for item in report.populations:
        metrics = item.metrics
        decision_rows.append(
            {
                "entity_key": item.entity_key,
                "recommendation": item.recommendation,
                "matured_eligible_deals": metrics.matured_eligible_deals,
                "positive_deals": metrics.positive_deals,
                "negative_deals": metrics.negative_deals,
                "usable_months": metrics.usable_months,
                "rolling_temporal_folds": metrics.rolling_temporal_folds,
                "determinate_rate": f"{metrics.analytically_determinate_rate:.2%}",
                "timestamp_rate": f"{metrics.valid_timestamp_rate:.2%}",
                "person_linkage_rate": f"{metrics.deterministic_person_linkage_rate:.2%}",
                "unknown_censored_rate": f"{metrics.data_quality_unknown_censored_rate:.2%}",
                "amount_known_rate": f"{metrics.amount_known_rate:.2%}",
                "amount_zero_rate": f"{metrics.amount_zero_rate:.2%}",
                "amount_revision_availability": metrics.amount_revision_availability,
                "optional_interactions": metrics.optional_interaction_coverage,
            }
        )
    lines.extend(_table(decision_rows))
    for item in report.populations:
        lines.extend(["", f"### {item.entity_key} threshold results", ""])
        threshold_rows: list[GateRow] = [
            {
                "threshold": threshold.name,
                "required": threshold.required,
                "observed": threshold.observed,
                "passed": threshold.passed,
            }
            for threshold in item.thresholds
        ]
        lines.extend(_table(threshold_rows))
    lines.extend(["", "## Monthly aggregate counts", ""])
    lines.extend(_table([dict(row) for row in report.monthly_counts]))
    lines.extend(
        [
            "",
            "## Decision boundary",
            "",
            "Recommendations are deterministic threshold results. The final decision remains ",
            "human-recorded once per population. Optional interactions are non-blocking for ",
            "CRM-only viability.",
            "",
        ]
    )
    return "\n".join(lines)


def write_gate_json(path: Path, output: GateReport) -> None:
    path.write_text(
        json.dumps(report_as_dict(output), allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _table(rows: list[GateRow]) -> list[str]:
    if not rows:
        return ["No rows returned."]
    columns = list(rows[0])
    rendered = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        rendered.append("| " + " | ".join(_cell(row.get(column)) for column in columns) + " |")
    return rendered


def _cell(value: GateScalar) -> str:
    flattened = " ".join(str(value if value is not None else "").splitlines())
    return html.escape(flattened, quote=True).replace("|", "\\|")
