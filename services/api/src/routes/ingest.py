"""Ingestion endpoints — thin pass-through that persists run/record metadata."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from src.auth.deps import require_mutator_for_source, require_scope
from src.auth.models import AuthUser
from src.http_utils import envelope, http_error
from src.repositories.deps import get_ingest_repo
from src.repositories.protocols.ingest import (
    IngestRecordsResponse,
    IngestRepository,
    IngestRunDetailResponse,
    IngestRunResponse,
)
from src.types import ApiResponse
from src.types_requests import (
    IngestRecordsRequest,
    IngestRunCreateRequest,
    IngestRunUpdateRequest,
)

router = APIRouter()


@router.post(
    "/v1/ingest/{source_key}/records",
    response_model=ApiResponse[IngestRecordsResponse],
    dependencies=[Depends(require_scope("ingest:write"))],
)
async def ingest_records(
    source_key: str,
    body: IngestRecordsRequest,
    request: Request,
    _user: AuthUser = Depends(require_mutator_for_source),
    repo: IngestRepository = Depends(get_ingest_repo),
) -> ApiResponse[IngestRecordsResponse]:
    """Persist a batch of source records linked to an ingest run."""
    if not body.records:
        raise http_error(400, "invalid_request", "At least one record is required.", request)

    outcome = await repo.ingest_records(
        source_key, body.ingest_type, body.ingest_run_id, body.records
    )
    if outcome is None:
        raise http_error(
            404, "not_found", f"Source system '{source_key}' not found or inactive.", request
        )
    return envelope(outcome, request)


@router.post(
    "/v1/ingest/{source_key}/runs",
    response_model=ApiResponse[IngestRunResponse],
    status_code=201,
    dependencies=[Depends(require_scope("ingest:write"))],
)
async def create_ingest_run(
    source_key: str,
    body: IngestRunCreateRequest,
    request: Request,
    _user: AuthUser = Depends(require_mutator_for_source),
    repo: IngestRepository = Depends(get_ingest_repo),
) -> ApiResponse[IngestRunResponse]:
    """Create a new ingest run for a bulk sync."""
    result = await repo.create_run(source_key, body.run_type, body.metadata)
    if result is None:
        raise http_error(
            404, "not_found", f"Source system '{source_key}' not found or inactive.", request
        )
    return envelope(result, request)


@router.patch(
    "/v1/ingest/{source_key}/runs/{ingest_run_id}",
    response_model=ApiResponse[IngestRunResponse],
    dependencies=[Depends(require_scope("ingest:write"))],
)
async def update_ingest_run(
    source_key: str,
    ingest_run_id: str,
    body: IngestRunUpdateRequest,
    request: Request,
    _user: AuthUser = Depends(require_mutator_for_source),
    repo: IngestRepository = Depends(get_ingest_repo),
) -> ApiResponse[IngestRunResponse]:
    """Update an ingest run with status and counters."""
    result = await repo.update_run(source_key, ingest_run_id, body)
    if result is None:
        raise http_error(404, "not_found", "Ingest run not found.", request)
    return envelope(result, request)


@router.get(
    "/v1/ingest/runs/{ingest_run_id}",
    response_model=ApiResponse[IngestRunDetailResponse],
    dependencies=[Depends(require_scope("ingest:write"))],
)
async def get_ingest_run(
    ingest_run_id: str,
    request: Request,
    repo: IngestRepository = Depends(get_ingest_repo),
) -> ApiResponse[IngestRunDetailResponse]:
    """Return ingest run status and counters."""
    detail = await repo.get_run(ingest_run_id)
    if detail is None:
        raise http_error(404, "not_found", "Ingest run not found.", request)
    return envelope(detail, request)
