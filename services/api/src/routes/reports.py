"""Report endpoints: CRUD for stored Cypher reports and execution."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request

from src.graph.client import get_session
from src.graph.converters import GraphRecord, GraphValue
from src.graph.mappers_reports import map_report_detail, map_report_summary
from src.graph.queries import (
    CREATE_REPORT,
    DELETE_REPORT,
    GET_REPORT,
    LIST_REPORTS,
    SEED_REPORT_QUERY,
    SEED_REPORTS,
    UPDATE_REPORT,
)
from src.http_utils import envelope, http_error
from src.types import ApiResponse
from src.types_reports import (
    CreateReportRequest,
    ExecuteReportRequest,
    ReportDetail,
    ReportResult,
    ReportSummary,
    UpdateReportRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports")


def _record_to_dict(keys: list[str], values: list[GraphValue]) -> GraphRecord:
    return dict(zip(keys, values, strict=True))


# ---------------------------------------------------------------------------
# List / Get
# ---------------------------------------------------------------------------

@router.get("", response_model=ApiResponse[list[ReportSummary]])
async def list_reports(request: Request) -> ApiResponse[list[ReportSummary]]:
    """Return all stored report definitions (summary only)."""
    async with get_session() as session:
        result = await session.run(LIST_REPORTS)
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    reports = [map_report_summary(rec) for rec in records]
    return envelope(reports, request)


@router.get("/{report_key}", response_model=ApiResponse[ReportDetail])
async def get_report(report_key: str, request: Request) -> ApiResponse[ReportDetail]:
    """Return a single report definition with its Cypher query and parameters."""
    async with get_session() as session:
        result = await session.run(GET_REPORT, report_key=report_key)
        record = await result.single()
    if record is None:
        raise http_error(404, "not_found", f"Report '{report_key}' not found.", request)
    row = _record_to_dict(record.keys(), list(record.values()))
    return envelope(map_report_detail(row), request)


# ---------------------------------------------------------------------------
# Create / Update / Delete
# ---------------------------------------------------------------------------

@router.post("", response_model=ApiResponse[ReportDetail], status_code=201)
async def create_report(
    body: CreateReportRequest, request: Request,
) -> ApiResponse[ReportDetail]:
    """Create a new stored report definition."""
    params_json = json.dumps([p.model_dump() for p in body.parameters])
    async with get_session(write=True) as session:
        await session.run(
            CREATE_REPORT,
            report_key=body.report_key,
            display_name=body.display_name,
            description=body.description,
            category=body.category,
            cypher_query=body.cypher_query,
            parameters_json=params_json,
        )
    return await get_report(body.report_key, request)


@router.patch("/{report_key}", response_model=ApiResponse[ReportDetail])
async def update_report(
    report_key: str, body: UpdateReportRequest, request: Request,
) -> ApiResponse[ReportDetail]:
    """Update an existing report definition. Merges only supplied fields."""
    existing = await _fetch_detail(report_key)
    if existing is None:
        raise http_error(404, "not_found", f"Report '{report_key}' not found.", request)

    new_display = body.display_name if body.display_name is not None else existing.display_name
    new_desc = body.description if body.description is not None else existing.description
    new_cat = body.category if body.category is not None else existing.category
    new_query = body.cypher_query if body.cypher_query is not None else existing.cypher_query
    new_params = body.parameters if body.parameters is not None else existing.parameters
    params_json = json.dumps([p.model_dump() for p in new_params])

    async with get_session(write=True) as session:
        await session.run(
            UPDATE_REPORT,
            report_key=report_key,
            display_name=new_display,
            description=new_desc,
            category=new_cat,
            cypher_query=new_query,
            parameters_json=params_json,
        )
    return await get_report(report_key, request)


@router.delete("/{report_key}", response_model=ApiResponse[dict[str, str]])
async def delete_report(
    report_key: str, request: Request,
) -> ApiResponse[dict[str, str]]:
    """Delete a stored report definition."""
    async with get_session(write=True) as session:
        result = await session.run(DELETE_REPORT, report_key=report_key)
        record = await result.single()
    deleted = record["deleted_count"] if record else 0
    if deleted == 0:
        raise http_error(404, "not_found", f"Report '{report_key}' not found.", request)
    return envelope({"status": "deleted", "report_key": report_key}, request)


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

@router.post("/{report_key}/execute", response_model=ApiResponse[ReportResult])
async def execute_report(
    report_key: str, body: ExecuteReportRequest, request: Request,
) -> ApiResponse[ReportResult]:
    """Execute a stored report with the supplied parameters."""
    detail = await _fetch_detail(report_key)
    if detail is None:
        raise http_error(404, "not_found", f"Report '{report_key}' not found.", request)

    params = _coerce_params(detail, body.parameters)

    async with get_session() as session:
        result = await session.run(detail.cypher_query, **params)
        columns: list[str] = []
        rows: list[dict[str, str | int | float | bool | None]] = []
        async for record in result:
            if not columns:
                columns = list(record.keys())
            row: dict[str, str | int | float | bool | None] = {}
            for key in columns:
                val = record[key]
                row[key] = _scalar(val)
            rows.append(row)

    return envelope(
        ReportResult(columns=columns, rows=rows, row_count=len(rows)),
        request,
    )


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

@router.post("/seed", response_model=ApiResponse[list[str]], status_code=201)
async def seed_reports(request: Request) -> ApiResponse[list[str]]:
    """Insert sample report definitions (idempotent via MERGE)."""
    seeded: list[str] = []
    async with get_session(write=True) as session:
        for seed in SEED_REPORTS:
            await session.run(SEED_REPORT_QUERY, **seed)
            seeded.append(seed["report_key"])
    return envelope(seeded, request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_detail(report_key: str) -> ReportDetail | None:
    """Fetch a full report detail by key, returning None if missing."""
    async with get_session() as session:
        result = await session.run(GET_REPORT, report_key=report_key)
        record = await result.single()
    if record is None:
        return None
    row = _record_to_dict(record.keys(), list(record.values()))
    return map_report_detail(row)


def _coerce_params(
    detail: ReportDetail,
    raw: dict[str, str | int | float | bool | None],
) -> dict[str, str | int | float | bool | None]:
    """Coerce user-supplied parameter values to the declared types."""
    param_defs = {p.name: p for p in detail.parameters}
    coerced: dict[str, str | int | float | bool | None] = {}
    for name, pdef in param_defs.items():
        value = raw.get(name, pdef.default_value)
        if value is None:
            coerced[name] = None
            continue
        if pdef.param_type == "integer":
            coerced[name] = int(value)
        elif pdef.param_type == "float":
            coerced[name] = float(value)
        elif pdef.param_type == "boolean":
            coerced[name] = str(value).lower() in ("true", "1", "yes")
        else:
            coerced[name] = str(value)
    return coerced


def _scalar(value: object) -> str | int | float | bool | None:
    """Convert a Neo4j value to a JSON-safe scalar."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return str(value)
    return str(value)
