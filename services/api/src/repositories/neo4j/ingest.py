"""Neo4j implementation of IngestRepository."""

from __future__ import annotations

from uuid import uuid4

from neo4j import AsyncManagedTransaction

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
from src.repositories.protocols.ingest import (
    IngestRecordResult,
    IngestRecordsResponse,
    IngestRunCreationResult,
    IngestRunDetailResponse,
    IngestRunResponse,
)
from src.types_requests import IngestRecord, IngestRunUpdateRequest


class Neo4jIngestRepository:
    async def ingest_records(
        self,
        source_key: str,
        ingest_type: str,
        ingest_run_id: str | None,
        records: list[IngestRecord],
    ) -> IngestRecordsResponse | None:
        async with get_session(write=True) as session:
            return await session.execute_write(
                _ingest_records_tx, source_key, ingest_type, ingest_run_id, records
            )

    async def create_run(
        self,
        source_key: str,
        run_type: str,
        mode: str,
        dump_path: str | None,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> IngestRunCreationResult | None:
        async with get_session(write=True) as session:
            return await session.execute_write(
                _create_run_tx,
                source_key,
                run_type,
                mode,
                dump_path,
                metadata,
                idempotency_key,
            )

    async def update_run(
        self,
        source_key: str,
        ingest_run_id: str,
        body: IngestRunUpdateRequest,
    ) -> IngestRunResponse | None:
        async with get_session(write=True) as session:
            return await session.execute_write(_update_run_tx, source_key, ingest_run_id, body)

    async def get_run(self, ingest_run_id: str) -> IngestRunDetailResponse | None:
        async with get_session() as session:
            result = await session.run(GET_INGEST_RUN, ingest_run_id=ingest_run_id)
            record = await result.single()
        if record is None:
            return None
        run = record["run"]
        if not isinstance(run, dict):
            return None
        return IngestRunDetailResponse(
            ingest_run_id=to_str(run.get("ingest_run_id")),
            run_type=to_str(run.get("run_type")),
            mode=to_str(run.get("mode")) or "batch",
            dump_path=to_str(run.get("dump_path")) or None,
            status=to_str(run.get("status")),
            record_count=int(run.get("record_count") or 0),
            rejected_count=int(run.get("rejected_count") or 0),
            started_at=to_str(record["started_at"]) or None,
            finished_at=to_str(record["finished_at"]) or None,
            source_key=to_str(record["source_key"]) or None,
            failure_category=to_str(run.get("failure_category")) or None,
            failure_exception_class=to_str(run.get("failure_exception_class")) or None,
            failure_message=to_str(run.get("failure_message")) or None,
            failure_task_id=to_str(run.get("failure_task_id")) or None,
            failure_checkpoint=to_str(run.get("failure_checkpoint")) or None,
        )


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
            result = await tx.run(
                CREATE_SOURCE_RECORD,
                source_key=source_key,
                ingest_run_id=run_id,
                source_record_id=record.source_record_id,
                entity_key=record.entity_key,
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
            if await result.single() is None:
                raise ValueError("Source record owner entity does not exist")
            results.append(
                IngestRecordResult(source_record_id=record.source_record_id, status="accepted")
            )
            accepted += 1
        except Exception:  # noqa: BLE001 — record-level rejection must not fail the batch
            results.append(
                IngestRecordResult(source_record_id=record.source_record_id, status="rejected")
            )
            rejected += 1
    return results, accepted, rejected


async def _create_run_tx(
    tx: AsyncManagedTransaction,
    source_key: str,
    run_type: str,
    mode: str,
    dump_path: str | None,
    metadata: dict[str, str],
    idempotency_key: str,
) -> IngestRunCreationResult | None:
    result = await tx.run(
        CREATE_INGEST_RUN,
        source_key=source_key,
        run_type=run_type,
        mode=mode,
        dump_path=dump_path,
        metadata=metadata,
        idempotency_key=idempotency_key,
        creation_token=str(uuid4()),
    )
    record = await result.single()
    if record is None:
        return None
    return IngestRunCreationResult(
        run=IngestRunResponse(
            ingest_run_id=to_str(record["ingest_run_id"]),
            status=to_str(record["status"]),
            mode=to_str(record["mode"]),
            dump_path=to_str(record["dump_path"]) or None,
            started_at=to_str(record["started_at"]),
        ),
        created=record["created"] is True,
    )


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
        mode=to_str(record["mode"]) or "batch",
        dump_path=to_str(record["dump_path"]) or None,
        finished_at=to_str(record["finished_at"]) or None,
    )
