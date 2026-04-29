"""Ingest repository protocol — types and interface for ingestion operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from src.types_requests import IngestRecord, IngestRunUpdateRequest


@dataclass
class IngestRecordResult:
    source_record_id: str
    status: str


@dataclass
class IngestRecordsResponse:
    ingest_run_id: str
    accepted_count: int
    rejected_count: int
    results: list[IngestRecordResult] = field(default_factory=list)


@dataclass
class IngestRunResponse:
    ingest_run_id: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class IngestRunDetailResponse:
    ingest_run_id: str
    run_type: str
    status: str
    record_count: int
    rejected_count: int
    started_at: str | None
    finished_at: str | None
    source_key: str | None


class IngestRepository(Protocol):
    async def ingest_records(
        self,
        source_key: str,
        ingest_type: str,
        ingest_run_id: str | None,
        records: list[IngestRecord],
    ) -> IngestRecordsResponse | None:
        """Returns None if the source system is not found or inactive."""
        ...

    async def create_run(
        self,
        source_key: str,
        run_type: str,
        metadata: dict[str, str],
    ) -> IngestRunResponse | None:
        """Returns None if the source system is not found or inactive."""
        ...

    async def update_run(
        self,
        source_key: str,
        ingest_run_id: str,
        body: IngestRunUpdateRequest,
    ) -> IngestRunResponse | None: ...

    async def get_run(self, ingest_run_id: str) -> IngestRunDetailResponse | None: ...
