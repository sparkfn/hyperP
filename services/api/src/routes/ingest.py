"""Ingestion endpoints — thin pass-through that persists run/record metadata."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from neo4j import AsyncManagedTransaction
from pydantic import BaseModel

from src.auth.deps import require_mutator_for_source
from src.auth.models import AuthUser
from src.graph.client import get_session
from src.graph.converters import to_str
from src.graph.queries import (
    CHECK_SOURCE_SYSTEM,
    CREATE_INGEST_RUN,
    CREATE_INGEST_RUN_INLINE,
    CREATE_SOURCE_RECORD,
    GET_INGEST_RUN,
    UPDATE_INGEST_RUN,
    UPDATE_INGEST_RUN_COUNTERS,
)
from src.http_utils import envelope, http_error
from src.types import (
    ApiResponse,
    IngestRecord,
    IngestRecordsRequest,
    IngestRunCreateRequest,
    IngestRunUpdateRequest,
)

router = APIRouter()


class IngestRecordResult(BaseModel):
    source_record_id: str
    status: str


class IngestRecordsResponse(BaseModel):
    accepted_count: int
    rejected_count: int
    ingest_run_id: str
    results: list[IngestRecordResult]


class IngestRunResponse(BaseModel):
    ingest_run_id: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None


class IngestRunDetailResponse(BaseModel):
    ingest_run_id: str
    run_type: str
    status: str
    record_count: int
    rejected_count: int
    started_at: str | None
    finished_at: str | None
    source_key: str | None


@router.post(
    "/v1/ingest/{source_key}/records", response_model=ApiResponse[IngestRecordsResponse]
)
async def ingest_records(
    source_key: str,
    body: IngestRecordsRequest,
    request: Request,
    _user: AuthUser = Depends(require_mutator_for_source),
) -> ApiResponse[IngestRecordsResponse]:
    """Persist a batch of source records linked to an ingest run."""
    if not body.records:
        raise http_error(400, "invalid_request", "At least one record is required.", request)

    async with get_session(write=True) as session:
        outcome = await session.execute_write(
            _ingest_records_tx, source_key, body.ingest_type, body.ingest_run_id, body.records
        )

    if outcome is None:
        raise http_error(
            404, "not_found", f"Source system '{source_key}' not found or inactive.", request
        )
    return envelope(outcome, request)


async def _ingest_records_tx(
    tx: AsyncManagedTransaction,
    source_key: str,
    ingest_type: str,
    ingest_run_id: str | None,
    records: list[IngestRecord],
) -> IngestRecordsResponse | None:
    ss_check = await tx.run(CHECK_SOURCE_SYSTEM, source_key=source_key)
    if await ss_check.single() is None:
        return None

    run_id = ingest_run_id
    if run_id is None:
        run_result = await tx.run(
            CREATE_INGEST_RUN_INLINE, source_key=source_key, ingest_type=ingest_type
        )
        run_record = await run_result.single()
        if run_record is None:
            return None
        run_id = to_str(run_record["ingest_run_id"])

    results, accepted, rejected = await _persist_records(tx, source_key, run_id, records)
    await tx.run(
        UPDATE_INGEST_RUN_COUNTERS,
        ingest_run_id=run_id,
        accepted=accepted,
        rejected=rejected,
    )
    return IngestRecordsResponse(
        accepted_count=accepted,
        rejected_count=rejected,
        ingest_run_id=run_id,
        results=results,
    )


async def _persist_records(
    tx: AsyncManagedTransaction,
    source_key: str,
    run_id: str,
    records: list[IngestRecord],
) -> tuple[list[IngestRecordResult], int, int]:
    results: list[IngestRecordResult] = []
    accepted = 0
    rejected = 0
    for record in records:
        try:
            await tx.run(
                CREATE_SOURCE_RECORD,
                source_key=source_key,
                ingest_run_id=run_id,
                source_record_id=record.source_record_id,
                source_record_version=record.source_record_version,
                record_type=record.record_type,
                extraction_confidence=record.extraction_confidence,
                extraction_method=record.extraction_method,
                conversation_ref=record.conversation_ref,
                observed_at=record.observed_at,
                record_hash=record.record_hash,
                raw_payload=record.raw_payload,
                attributes=record.attributes,
            )
            results.append(
                IngestRecordResult(source_record_id=record.source_record_id, status="accepted")
            )
            accepted += 1
        except Exception:  # noqa: BLE001 — record-level rejection should not fail the batch
            results.append(
                IngestRecordResult(source_record_id=record.source_record_id, status="rejected")
            )
            rejected += 1
    return results, accepted, rejected


@router.post(
    "/v1/ingest/{source_key}/runs",
    response_model=ApiResponse[IngestRunResponse],
    status_code=201,
)
async def create_ingest_run(
    source_key: str,
    body: IngestRunCreateRequest,
    request: Request,
    _user: AuthUser = Depends(require_mutator_for_source),
) -> ApiResponse[IngestRunResponse]:
    """Create a new ingest run for a bulk sync."""
    async with get_session(write=True) as session:
        result = await session.execute_write(
            _create_run_tx, source_key, body.run_type, body.metadata
        )
    if result is None:
        raise http_error(
            404, "not_found", f"Source system '{source_key}' not found or inactive.", request
        )
    return envelope(result, request)


async def _create_run_tx(
    tx: AsyncManagedTransaction,
    source_key: str,
    run_type: str,
    metadata: dict[str, str],
) -> IngestRunResponse | None:
    result = await tx.run(
        CREATE_INGEST_RUN, source_key=source_key, run_type=run_type, metadata=metadata
    )
    record = await result.single()
    if record is None:
        return None
    return IngestRunResponse(
        ingest_run_id=to_str(record["ingest_run_id"]),
        status=to_str(record["status"]),
        started_at=to_str(record["started_at"]),
    )


@router.patch(
    "/v1/ingest/{source_key}/runs/{ingest_run_id}",
    response_model=ApiResponse[IngestRunResponse],
)
async def update_ingest_run(
    source_key: str,
    ingest_run_id: str,
    body: IngestRunUpdateRequest,
    request: Request,
    _user: AuthUser = Depends(require_mutator_for_source),
) -> ApiResponse[IngestRunResponse]:
    """Update an ingest run with status and counters."""
    async with get_session(write=True) as session:
        result = await session.execute_write(_update_run_tx, source_key, ingest_run_id, body)
    if result is None:
        raise http_error(404, "not_found", "Ingest run not found.", request)
    return envelope(result, request)


async def _update_run_tx(
    tx: AsyncManagedTransaction,
    source_key: str,
    ingest_run_id: str,
    body: IngestRunUpdateRequest,
) -> IngestRunResponse | None:
    result = await tx.run(
        UPDATE_INGEST_RUN,
        source_key=source_key,
        ingest_run_id=ingest_run_id,
        status=body.status,
        finished_at=body.finished_at,
        metadata=body.metadata,
    )
    record = await result.single()
    if record is None:
        return None
    return IngestRunResponse(
        ingest_run_id=to_str(record["ingest_run_id"]),
        status=to_str(record["status"]),
        finished_at=to_str(record["finished_at"]) or None,
    )


@router.get(
    "/v1/ingest/runs/{ingest_run_id}", response_model=ApiResponse[IngestRunDetailResponse]
)
async def get_ingest_run(
    ingest_run_id: str, request: Request
) -> ApiResponse[IngestRunDetailResponse]:
    """Return ingest run status and counters."""
    async with get_session() as session:
        result = await session.run(GET_INGEST_RUN, ingest_run_id=ingest_run_id)
        record = await result.single()
    if record is None:
        raise http_error(404, "not_found", "Ingest run not found.", request)

    run = record["run"]
    if not isinstance(run, dict):
        raise http_error(500, "internal_error", "Unexpected ingest run shape.", request)

    detail = IngestRunDetailResponse(
        ingest_run_id=to_str(run.get("ingest_run_id")),
        run_type=to_str(run.get("run_type")),
        status=to_str(run.get("status")),
        record_count=int(run.get("record_count") or 0),
        rejected_count=int(run.get("rejected_count") or 0),
        started_at=to_str(record["started_at"]) or None,
        finished_at=to_str(record["finished_at"]) or None,
        source_key=to_str(record["source_key"]) or None,
    )
    return envelope(detail, request)
