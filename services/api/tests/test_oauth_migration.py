"""Tests for the one-shot OAuth wipe-and-recreate startup migration."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from src.app import _wipe_oauth_clients_on_startup


@pytest.mark.asyncio
async def test_startup_wipes_when_migration_newly_claimed() -> None:
    with (
        patch("src.app.claim_oauth_wipe_migration", new=AsyncMock(return_value=True)),
        patch("src.app.wipe_oauth_clients", new=AsyncMock()) as wipe_nodes,
        patch("src.app.clear_all_tokens", new=AsyncMock()) as wipe_redis,
    ):
        await _wipe_oauth_clients_on_startup()
    wipe_nodes.assert_awaited_once()
    wipe_redis.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_skips_wipe_when_already_applied() -> None:
    with (
        patch("src.app.claim_oauth_wipe_migration", new=AsyncMock(return_value=False)),
        patch("src.app.wipe_oauth_clients", new=AsyncMock()) as wipe_nodes,
        patch("src.app.clear_all_tokens", new=AsyncMock()) as wipe_redis,
    ):
        await _wipe_oauth_clients_on_startup()
    wipe_nodes.assert_not_awaited()
    wipe_redis.assert_not_awaited()
