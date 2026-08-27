"""FastAPI app factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.auth.oauth_clients import ensure_oauth_client_constraints
from src.auth.oauth_tokens import validate_oauth_runtime_config
from src.config import config
from src.error_handlers import register_error_handlers
from src.frontend_app import build_frontend_app
from src.graph.client import close_driver, get_session
from src.graph.queries.indexes import PERSON_INDEXES
from src.graph.queries.users import CREATE_USER_CONSTRAINT
from src.llm.service import close_llm_service
from src.mcp_app import mount_mcp, shutdown_mcp
from src.oauth2_app import build_oauth2_app
from src.proclaude.service import close_proclaude_service
from src.redis_client import close_redis
from src.repositories.neo4j.bootstrap import (
    ensure_source_record_identity_lock_constraint,
)
from src.route_catalog import ROOT_ROUTERS

logger = logging.getLogger("profile_unifier_api")


async def _ensure_source_record_identity_lock() -> None:
    """Install review activation's identity serialization prerequisite."""
    await ensure_source_record_identity_lock_constraint()


async def _ensure_user_constraint() -> None:
    """Create the :User uniqueness constraint if it does not exist."""
    try:
        async with get_session(write=True) as session:
            await session.run(CREATE_USER_CONSTRAINT)
    except Exception:  # noqa: BLE001 — constraint setup is best-effort at startup
        logger.exception("Failed to create :User uniqueness constraint")


async def _ensure_oauth_client_constraints() -> None:
    """Create OAuth client uniqueness constraints if they do not exist."""
    await ensure_oauth_client_constraints()


async def _ensure_person_indexes() -> None:
    """Create person indexes if they do not exist."""
    try:
        async with get_session(write=True) as session:
            for cypher in PERSON_INDEXES:
                await session.run(cypher)
    except Exception:  # noqa: BLE001 — index setup is best-effort at startup
        logger.exception("Failed to create Person indexes")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage the Neo4j driver lifecycle alongside the FastAPI process."""
    validate_oauth_runtime_config()
    await _ensure_source_record_identity_lock()
    await _ensure_user_constraint()
    await _ensure_oauth_client_constraints()
    await _ensure_person_indexes()
    try:
        yield
    finally:
        await shutdown_mcp(_app)
        await close_driver()
        await close_redis()
        await close_llm_service()
        await close_proclaude_service()


def build_app() -> FastAPI:
    """Construct and configure the FastAPI application."""
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    logging.basicConfig(level=config.log_level.upper())
    app = FastAPI(
        title="Profile Unifier API",
        version="0.1.0",
        lifespan=_lifespan,
        root_path=config.root_path,
        # Interactive API docs are disabled on the root app — it exposes only
        # health, the machine OAuth2 token flow, and public share-link pages, so
        # there is no contract worth publishing here. Authenticated business
        # routes live on the mounted sub-apps (which keep their own docs).
        # openapi.json is left off too so the root surface publishes no schema at
        # all; openapi_tags is omitted with it since no schema is generated.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # The root app exposes only cross-cutting and unauthenticated surfaces.
    # Every authenticated business route is served exclusively through the
    # mounted sub-apps below — /app/v2 (active frontend2 UI contract) and
    # /oauth2/v1 (machine clients). The business routers live in src/routes/*
    # and are copied into those mounts; the root app no longer registers them.
    for router in ROOT_ROUTERS:
        app.include_router(router)

    # Frontend-facing API contract. A fresh FastAPI instance exposing the
    # authenticated router set with the /v1 prefix stripped, mounted for the
    # active frontend2 UI at /app/v2. (The legacy v1 frontend and its /app/v1
    # mount have been retired; services/frontend/ source is kept but no longer
    # built or routed.)
    app.mount("/app/v2", build_frontend_app())

    # Machine-facing OAuth2 contract: token flow + read-only person list/detail,
    # accepting OAuth2 client credentials only. Served externally at
    # /api/oauth2/v1/... via the existing /api/ nginx route.
    app.mount("/oauth2/v1", build_oauth2_app())

    # The MCP surface is generated from the canonical route catalog and is
    # independently authenticated. It is publicly proxied by nginx at /mcp.
    mount_mcp(app)

    register_error_handlers(app)
    return app


app: FastAPI = build_app()
