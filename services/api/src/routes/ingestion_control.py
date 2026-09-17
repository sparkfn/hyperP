"""Authenticated bounded logical-run status, pause, and resume endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from src.auth.deps import require_human_admin
from src.auth.models import AuthUser
from src.celery_client import (
    BoundedLogicalRunRecoveryRequest,
    enqueue_bounded_logical_run_recovery,
)
from src.http_utils import envelope, http_error
from src.repositories.deps import get_ingestion_control_repo
from src.repositories.protocols.ingestion_control import (
    BoundedLogicalRunControlResult,
    BoundedLogicalRunStatusRecord,
    IngestionControlRepository,
)
from src.types import ApiResponse
from src.types_ingestion_control import BoundedLogicalRunStatus, BoundedLogicalRunUsage
from src.types_requests import BoundedRunPauseRequest, BoundedRunResumeRequest

router = APIRouter(tags=["Ingestion"])


@router.get(
    "/v1/ingest/logical-runs/{logical_run_id}",
    operation_id="get_bounded_logical_run",
    response_model=ApiResponse[BoundedLogicalRunStatus],
)
async def get_bounded_logical_run(
    logical_run_id: str,
    request: Request,
    _user: AuthUser = Depends(require_human_admin),
    repo: IngestionControlRepository = Depends(get_ingestion_control_repo),
) -> ApiResponse[BoundedLogicalRunStatus]:
    """Return safe, bounded progress for one durable logical run."""
    result = await repo.get_bounded_run(logical_run_id)
    if result is None:
        raise http_error(404, "not_found", "Bounded logical run not found.", request)
    return envelope(_to_status_response(result), request)


@router.post(
    "/v1/ingest/logical-runs/{logical_run_id}/pause",
    operation_id="pause_bounded_logical_run",
    response_model=ApiResponse[BoundedLogicalRunStatus],
)
async def pause_bounded_logical_run(
    logical_run_id: str,
    body: BoundedRunPauseRequest,
    request: Request,
    _user: AuthUser = Depends(require_human_admin),
    repo: IngestionControlRepository = Depends(get_ingestion_control_repo),
) -> ApiResponse[BoundedLogicalRunStatus]:
    """Record a durable manual pause intent for one exact logical run identity."""
    result = await repo.pause_bounded_run(
        logical_run_id,
        body.source_key,
        body.control_instance_id,
        body.reset_generation,
        body.reason,
    )
    return _control_response(result, request)


@router.post(
    "/v1/ingest/logical-runs/{logical_run_id}/resume",
    operation_id="resume_bounded_logical_run",
    response_model=ApiResponse[BoundedLogicalRunStatus],
)
async def resume_bounded_logical_run(
    logical_run_id: str,
    body: BoundedRunResumeRequest,
    request: Request,
    _user: AuthUser = Depends(require_human_admin),
    repo: IngestionControlRepository = Depends(get_ingestion_control_repo),
) -> ApiResponse[BoundedLogicalRunStatus]:
    """Release a manual pause and best-effort publish recovery after persistence."""
    result = await repo.resume_bounded_run(
        logical_run_id,
        body.source_key,
        body.control_instance_id,
        body.reset_generation,
    )
    response = _control_response(result, request)
    if result.publish_recovery:
        enqueue_bounded_logical_run_recovery(
            BoundedLogicalRunRecoveryRequest(
                logical_run_id=logical_run_id,
                source_key=body.source_key,
                control_instance_id=body.control_instance_id,
                reset_generation=body.reset_generation,
            )
        )
    return response


def _control_response(
    result: BoundedLogicalRunControlResult,
    request: Request,
) -> ApiResponse[BoundedLogicalRunStatus]:
    if result.outcome == "not_found":
        raise http_error(404, "not_found", "Bounded logical run not found.", request)
    if result.outcome == "conflict":
        raise http_error(
            409,
            "bounded_run_state_conflict",
            "Logical run is not eligible for that control action.",
            request,
        )
    if result.status is None:
        raise RuntimeError("updated bounded control action omitted its status")
    return envelope(_to_status_response(result.status), request)


def _to_status_response(record: BoundedLogicalRunStatusRecord) -> BoundedLogicalRunStatus:
    return BoundedLogicalRunStatus(
        logical_run_id=record.logical_run_id,
        source_key=record.source_key,
        control_instance_id=record.control_instance_id,
        entity_key=record.entity_key,
        status=record.status,
        pause_reason=record.pause_reason,
        occurrence_id=record.occurrence_id,
        timezone=record.timezone,
        starts_at=record.starts_at,
        drain_starts_at=record.drain_starts_at,
        cutoff_at=record.cutoff_at,
        next_eligible_at=record.next_eligible_at,
        usage=BoundedLogicalRunUsage(
            records=record.usage.records,
            source_requests=record.usage.source_requests,
            pages=record.usage.pages,
            bytes_read=record.usage.bytes_read,
            extraction_calls=record.usage.extraction_calls,
        ),
        phase=record.phase,
        checkpointed_at=record.checkpointed_at,
        retry_backlog=record.retry_backlog,
        failure_category=record.failure_category,
    )
