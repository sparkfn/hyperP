from __future__ import annotations

from typing import cast

import pytest
from fastapi import HTTPException, Request
from neo4j import AsyncManagedTransaction
from src.auth.models import AuthUser
from src.graph.queries.ingestion import CREATE_SOURCE_RECORD
from src.repositories.neo4j.ingest import _persist_records
from src.repositories.protocols.ingest import IngestRecordsResponse, IngestRepository
from src.routes.ingest import ingest_records
from src.types_requests import IngestRecord, IngestRecordsRequest


def _record(*, entity_key: str | None = None) -> IngestRecord:
    return IngestRecord(
        source_record_id="chat-77-person-1",
        entity_key=entity_key,
        observed_at="2026-07-20T08:00:00Z",
        record_hash="hash-1",
    )


def test_ingest_record_accepts_an_optional_entity_key() -> None:
    assert _record(entity_key="speedzone").entity_key == "speedzone"


class _UnexpectedRepo:
    def __init__(self) -> None:
        self.called = False

    async def ingest_records(self, *args: object) -> None:
        self.called = True


@pytest.mark.asyncio
async def test_bitrix_chat_api_records_require_an_entity_key_before_repository_write() -> None:
    repo = _UnexpectedRepo()
    request = Request({"type": "http", "headers": []})
    body = IngestRecordsRequest(ingest_type="api", records=[_record()])

    with pytest.raises(HTTPException) as exc_info:
        await ingest_records(
            "bitrix_chat",
            body,
            request,
            AuthUser(email="admin@example.test", google_sub=None, role="admin"),
            cast("IngestRepository", repo),
        )

    assert exc_info.value.status_code == 400
    assert "entity_key" in str(exc_info.value.detail)
    assert repo.called is False


@pytest.mark.asyncio
async def test_non_shared_source_rejects_entity_key_before_repository_write() -> None:
    repo = _UnexpectedRepo()
    request = Request({"type": "http", "headers": []})
    body = IngestRecordsRequest(
        ingest_type="api",
        records=[_record(entity_key="speedzone")],
    )

    with pytest.raises(HTTPException) as exc_info:
        await ingest_records(
            "fundbox_pos",
            body,
            request,
            AuthUser(email="employee@example.test", google_sub=None, role="employee"),
            cast("IngestRepository", repo),
        )

    assert exc_info.value.status_code == 400
    assert "entity_key" in str(exc_info.value.detail)
    assert repo.called is False


class _AcceptingRepo:
    def __init__(self) -> None:
        self.called = False

    async def ingest_records(self, *args: object) -> IngestRecordsResponse:
        self.called = True
        return IngestRecordsResponse(
            ingest_run_id="run-1",
            accepted_count=1,
            rejected_count=0,
        )


@pytest.mark.asyncio
async def test_non_shared_source_still_accepts_record_without_entity_key() -> None:
    repo = _AcceptingRepo()

    response = await ingest_records(
        "fundbox_pos",
        IngestRecordsRequest(ingest_type="api", records=[_record()]),
        Request({"type": "http", "headers": []}),
        AuthUser(email="employee@example.test", google_sub=None, role="employee"),
        cast("IngestRepository", repo),
    )

    assert response.data.accepted_count == 1
    assert repo.called is True


def test_source_record_graph_write_links_optional_known_entity_owner() -> None:
    assert "OPTIONAL MATCH (entity:Entity {entity_key: $entity_key})" in CREATE_SOURCE_RECORD
    assert "CREATE (sr)-[:OWNED_BY]->(entity)" in CREATE_SOURCE_RECORD
    assert "entity_key: $entity_key" in CREATE_SOURCE_RECORD
    assert "RETURN sr.source_record_pk AS source_record_pk" in CREATE_SOURCE_RECORD


class _EmptyResult:
    async def single(self) -> None:
        return None


class _UnknownEntityTx:
    async def run(self, query: str, **parameters: object) -> _EmptyResult:
        assert query == CREATE_SOURCE_RECORD
        _ = parameters
        return _EmptyResult()


@pytest.mark.asyncio
async def test_unknown_entity_record_is_rejected_instead_of_reported_accepted() -> None:
    results, accepted, rejected = await _persist_records(
        cast("AsyncManagedTransaction", _UnknownEntityTx()),
        "bitrix_chat",
        "run-1",
        [_record(entity_key="missing")],
    )

    assert (accepted, rejected) == (0, 1)
    assert [(item.source_record_id, item.status) for item in results] == [
        ("chat-77-person-1", "rejected")
    ]
