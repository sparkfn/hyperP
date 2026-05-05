"""Report endpoints: CRUD for stored Cypher reports and execution."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from src.auth.deps import require_admin, require_scope
from src.auth.models import AuthUser
from src.http_utils import envelope, http_error
from src.repositories.deps import get_report_repo
from src.repositories.protocols.report import ReportRepository
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


@router.get(
    "",
    response_model=ApiResponse[list[ReportSummary]],
    dependencies=[Depends(require_scope("persons:read"))],
)
async def list_reports(
    request: Request,
    repo: ReportRepository = Depends(get_report_repo),
) -> ApiResponse[list[ReportSummary]]:
    """Return all stored report definitions (summary only)."""
    reports = await repo.get_all()
    return envelope(reports, request)


@router.get(
    "/{report_key}",
    response_model=ApiResponse[ReportDetail],
    dependencies=[Depends(require_scope("persons:read"))],
)
async def get_report(
    report_key: str,
    request: Request,
    repo: ReportRepository = Depends(get_report_repo),
) -> ApiResponse[ReportDetail]:
    """Return a single report definition with its query and parameters."""
    detail = await repo.get_by_key(report_key)
    if detail is None:
        raise http_error(404, "not_found", f"Report '{report_key}' not found.", request)
    return envelope(detail, request)


@router.post("", response_model=ApiResponse[ReportDetail], status_code=201)
async def create_report(
    body: CreateReportRequest,
    request: Request,
    _user: AuthUser = Depends(require_admin),
    repo: ReportRepository = Depends(get_report_repo),
) -> ApiResponse[ReportDetail]:
    """Create a new stored report definition."""
    params_json = json.dumps([p.model_dump() for p in body.parameters])
    await repo.create(
        report_key=body.report_key,
        display_name=body.display_name,
        description=body.description,
        category=body.category,
        cypher_query=body.cypher_query,
        parameters_json=params_json,
    )
    return await get_report(body.report_key, request, repo)


@router.patch("/{report_key}", response_model=ApiResponse[ReportDetail])
async def update_report(
    report_key: str,
    body: UpdateReportRequest,
    request: Request,
    _user: AuthUser = Depends(require_admin),
    repo: ReportRepository = Depends(get_report_repo),
) -> ApiResponse[ReportDetail]:
    """Update an existing report definition. Merges only supplied fields."""
    existing = await repo.get_by_key(report_key)
    if existing is None:
        raise http_error(404, "not_found", f"Report '{report_key}' not found.", request)

    new_display = body.display_name if body.display_name is not None else existing.display_name
    new_desc = body.description if body.description is not None else existing.description
    new_cat = body.category if body.category is not None else existing.category
    new_query = body.cypher_query if body.cypher_query is not None else existing.cypher_query
    new_params = body.parameters if body.parameters is not None else existing.parameters
    params_json = json.dumps([p.model_dump() for p in new_params])

    await repo.update(
        report_key=report_key,
        display_name=new_display,
        description=new_desc,
        category=new_cat,
        cypher_query=new_query,
        parameters_json=params_json,
    )
    return await get_report(report_key, request, repo)


class DeleteReportResponse(BaseModel):
    status: str
    report_key: str


@router.delete("/{report_key}", response_model=ApiResponse[DeleteReportResponse])
async def delete_report(
    report_key: str,
    request: Request,
    _user: AuthUser = Depends(require_admin),
    repo: ReportRepository = Depends(get_report_repo),
) -> ApiResponse[DeleteReportResponse]:
    """Delete a stored report definition."""
    deleted = await repo.delete(report_key)
    if deleted == 0:
        raise http_error(404, "not_found", f"Report '{report_key}' not found.", request)
    return envelope(DeleteReportResponse(status="deleted", report_key=report_key), request)


@router.post(
    "/{report_key}/execute",
    response_model=ApiResponse[ReportResult],
    dependencies=[Depends(require_scope("persons:read"))],
)
async def execute_report(
    report_key: str,
    body: ExecuteReportRequest,
    request: Request,
    repo: ReportRepository = Depends(get_report_repo),
) -> ApiResponse[ReportResult]:
    """Execute a stored report with the supplied parameters."""
    detail = await repo.get_by_key(report_key)
    if detail is None:
        raise http_error(404, "not_found", f"Report '{report_key}' not found.", request)

    params = _coerce_params(detail, body.parameters)
    result = await repo.execute(detail.cypher_query, params)
    return envelope(result, request)


@router.post("/seed", response_model=ApiResponse[list[str]], status_code=201)
async def seed_reports(
    request: Request,
    _user: AuthUser = Depends(require_admin),
    repo: ReportRepository = Depends(get_report_repo),
) -> ApiResponse[list[str]]:
    """Insert sample report definitions (idempotent via MERGE)."""
    seeded = await repo.seed()
    return envelope(seeded, request)


def _coerce_params(
    detail: ReportDetail,
    raw: dict[str, str | int | float | bool | None],
) -> dict[str, str | int | float | bool | None]:
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
