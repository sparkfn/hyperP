"""Coverage for the API-only bounded Neo4j read session facade."""

from __future__ import annotations

from typing import cast

import pytest
from neo4j import AsyncSession, Query
from src.graph.client import TimedAsyncSession
from src.request_timing import (
    begin_request,
    create_detached_task,
    end_request,
    repository_duration_ms,
)


class _Session:
    def __init__(self) -> None:
        self.query: str | Query | None = None

    async def run(self, query: str | Query, *args: object, **kwargs: object) -> object:
        self.query = query
        return object()

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute_read(self, *args: object, **kwargs: object) -> object:
        return object()

    async def execute_write(self, *args: object, **kwargs: object) -> object:
        return object()


@pytest.mark.anyio
async def test_read_session_wraps_cypher_with_configured_transaction_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config import config

    monkeypatch.setattr(config, "neo4j_web_read_transaction_timeout_seconds", 12.5)
    session = _Session()
    tokens = begin_request("request-read")
    try:
        await TimedAsyncSession(cast(AsyncSession, session), write=False).run("RETURN 1")
    finally:
        end_request(tokens)

    assert isinstance(session.query, Query)
    assert session.query.text == "RETURN 1"
    assert session.query.timeout == 12.5


@pytest.mark.anyio
async def test_background_read_session_does_not_apply_the_web_read_timeout() -> None:
    session = _Session()

    await TimedAsyncSession(cast(AsyncSession, session), write=False).run("RETURN 1")

    assert session.query == "RETURN 1"


@pytest.mark.anyio
async def test_opted_in_detached_read_uses_background_timeout_without_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config import config

    monkeypatch.setattr(config, "neo4j_background_read_transaction_timeout_seconds", 11.0)
    session = _Session()

    async def run() -> None:
        await TimedAsyncSession(cast(AsyncSession, session), write=False).run("RETURN 1")

    await create_detached_task(run(), background_read=True)
    assert isinstance(session.query, Query)
    assert session.query.timeout == 11.0


@pytest.mark.anyio
async def test_write_session_does_not_apply_the_web_read_timeout() -> None:
    session = _Session()

    await TimedAsyncSession(cast(AsyncSession, session), write=True).run("CREATE ()")

    assert session.query == "CREATE ()"


@pytest.mark.anyio
async def test_read_session_records_duration_after_session_close() -> None:
    session = _Session()
    token = begin_request("request-read")
    try:
        async with TimedAsyncSession(cast(AsyncSession, session), write=False) as timed:
            await timed.run("RETURN 1")
        assert repository_duration_ms() >= 0
    finally:
        end_request(token)
