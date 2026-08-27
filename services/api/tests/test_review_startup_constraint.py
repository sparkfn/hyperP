"""Startup contract for review lifecycle serialization."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from src import app
from src.graph.queries.source_records import CREATE_SOURCE_RECORD_IDENTITY_LOCK_CONSTRAINT

EXPECTED_CONSTRAINT = """CREATE CONSTRAINT source_record_identity_lock_triple_unique IF NOT EXISTS
FOR (lock:SourceRecordIdentityLock)
REQUIRE (lock.source_system, lock.source_instance_id, lock.source_record_id) IS UNIQUE"""


def test_api_uses_the_ingestion_identity_lock_tuple_constraint() -> None:
    assert CREATE_SOURCE_RECORD_IDENTITY_LOCK_CONSTRAINT == EXPECTED_CONSTRAINT


@pytest.mark.asyncio
async def test_identity_lock_constraint_precedes_review_capable_startup() -> None:
    calls: list[str] = []

    def recorder(name: str) -> AsyncMock:
        async def record() -> None:
            calls.append(name)

        return AsyncMock(side_effect=record)

    with (
        patch.object(app, "validate_oauth_runtime_config"),
        patch.object(app, "_ensure_source_record_identity_lock", new=recorder("lock")),
        patch.object(app, "_ensure_user_constraint", new=recorder("user")),
        patch.object(app, "_ensure_oauth_client_constraints", new=recorder("oauth")),
        patch.object(app, "_ensure_person_indexes", new=recorder("indexes")),
        patch.object(app, "close_driver", new=AsyncMock()),
        patch.object(app, "close_redis", new=AsyncMock()),
        patch.object(app, "close_llm_service", new=AsyncMock()),
        patch.object(app, "close_proclaude_service", new=AsyncMock()),
    ):
        async with app._lifespan(FastAPI()):
            calls.append("traffic")

    assert calls == ["lock", "user", "oauth", "indexes", "traffic"]
