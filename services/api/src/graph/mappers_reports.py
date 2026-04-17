"""Map raw Neo4j records to Report domain models."""

from __future__ import annotations

import json

from src.graph.converters import GraphRecord, to_iso_or_empty, to_optional_str, to_str
from src.graph.mappers import _as_dict
from src.types_reports import ReportDetail, ReportParameterDef, ReportSummary


def map_report_summary(record: GraphRecord) -> ReportSummary:
    """Map a raw report record to a ReportSummary."""
    r = _as_dict(record.get("report"))
    return ReportSummary(
        report_key=to_str(r.get("report_key")),
        display_name=to_str(r.get("display_name")),
        description=to_optional_str(r.get("description")),
        category=to_optional_str(r.get("category")),
    )


def map_report_detail(record: GraphRecord) -> ReportDetail:
    """Map a raw report record to a full ReportDetail including query and params."""
    r = _as_dict(record.get("report"))
    raw_params = to_str(r.get("parameters_json"), "[]")
    parsed: list[dict[str, object]] = json.loads(raw_params)
    parameters = [
        ReportParameterDef(
            name=str(p.get("name", "")),
            label=str(p.get("label", "")),
            param_type=str(p.get("param_type", "string")),
            required=bool(p.get("required", False)),
            default_value=str(p["default_value"]) if p.get("default_value") is not None else None,
        )
        for p in parsed
    ]
    return ReportDetail(
        report_key=to_str(r.get("report_key")),
        display_name=to_str(r.get("display_name")),
        description=to_optional_str(r.get("description")),
        category=to_optional_str(r.get("category")),
        cypher_query=to_str(r.get("cypher_query")),
        parameters=parameters,
        created_at=to_iso_or_empty(r.get("created_at")),
        updated_at=to_iso_or_empty(r.get("updated_at")),
    )
