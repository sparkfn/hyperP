"""Regression tests for non-destructive OAuth application startup."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from src import app


@pytest.mark.asyncio
async def test_startup_does_not_delete_oauth_clients_or_clear_tokens() -> None:
    delete_clients = AsyncMock()
    clear_tokens = AsyncMock()

    with (
        patch.object(app, "validate_oauth_runtime_config"),
        patch.object(app, "_ensure_source_record_identity_lock", new=AsyncMock()),
        patch.object(app, "_ensure_user_constraint", new=AsyncMock()),
        patch.object(app, "_ensure_oauth_client_constraints", new=AsyncMock()),
        patch.object(app, "_ensure_person_indexes", new=AsyncMock()),
        patch.object(app, "wipe_oauth_clients", new=delete_clients, create=True),
        patch.object(app, "clear_all_tokens", new=clear_tokens, create=True),
        patch.object(
            app,
            "claim_oauth_wipe_migration",
            new=AsyncMock(return_value=True),
            create=True,
        ),
        patch.object(app, "close_driver", new=AsyncMock()),
        patch.object(app, "close_redis", new=AsyncMock()),
        patch.object(app, "close_llm_service", new=AsyncMock()),
        patch.object(app, "close_proclaude_service", new=AsyncMock()),
    ):
        async with app._lifespan(FastAPI()):
            pass

    delete_clients.assert_not_awaited()
    clear_tokens.assert_not_awaited()
