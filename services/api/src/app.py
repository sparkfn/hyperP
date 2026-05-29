"""FastAPI app factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.params import Depends as DependsMarker

from src.auth.deps import require_active_user
from src.auth.oauth_clients import ensure_oauth_client_constraints
from src.auth.oauth_tokens import validate_oauth_runtime_config
from src.config import config
from src.error_handlers import register_error_handlers
from src.frontend_app import build_frontend_app
from src.graph.client import close_driver, get_session
from src.graph.queries.users import CREATE_USER_CONSTRAINT
from src.llm.service import close_llm_service
from src.redis_client import close_redis
from src.routes import (
    admin,
    dumps,
    entities,
    events,
    health,
    ingest,
    merge,
    oauth,
    person_sales,
    persons,
    reports,
    review,
    survivorship,
)
from src.routes import auth as auth_routes
from src.routes import oauth_clients as oauth_client_routes
from src.routes import users as users_routes
from src.routes.public_pages import person_links_router, public_router

logger = logging.getLogger("profile_unifier_api")


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


_PERSON_INDEXES = [
    "CREATE INDEX idx_person_completeness IF NOT EXISTS FOR (p:Person) ON (p.profile_completeness_score)",
    "CREATE INDEX idx_person_high_value IF NOT EXISTS FOR (p:Person) ON (p.is_high_value)",
    "CREATE INDEX idx_person_high_risk IF NOT EXISTS FOR (p:Person) ON (p.is_high_risk)",
    "CREATE INDEX idx_person_updated_at IF NOT EXISTS FOR (p:Person) ON (p.updated_at)",
]


async def _ensure_person_indexes() -> None:
    """Create person indexes if they do not exist."""
    try:
        async with get_session(write=True) as session:
            for cypher in _PERSON_INDEXES:
                await session.run(cypher)
    except Exception:  # noqa: BLE001 — index setup is best-effort at startup
        logger.exception("Failed to create Person indexes")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage the Neo4j driver lifecycle alongside the FastAPI process."""
    validate_oauth_runtime_config()
    await _ensure_user_constraint()
    await _ensure_oauth_client_constraints()
    await _ensure_person_indexes()
    yield
    await close_driver()
    await close_redis()
    await close_llm_service()


def build_app() -> FastAPI:
    """Construct and configure the FastAPI application."""
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    logging.basicConfig(level=config.log_level.upper())
    app = FastAPI(
        title="Profile Unifier API",
        version="0.1.0",
        lifespan=_lifespan,
        root_path=config.root_path,
        swagger_ui_parameters={"operationsSorter": "alpha"},
        openapi_tags=[
            {"name": "Admin", "description": "User management, OAuth client registry, and source-system field trust."},
            {"name": "Auth", "description": "Session authentication: current user info and logout."},
            {"name": "Entities", "description": "Source entity (business unit) directory and their person links."},
            {"name": "Events", "description": "Downstream event polling for external integrations."},
            {"name": "Ingestion", "description": "Source record ingest runs and raw record submission."},
            {"name": "OAuth", "description": "Machine-to-machine OAuth2 client credentials token flow."},
            {"name": "Persons", "description": "Person profiles, identifiers, connections, timeline, matches, merge, and survivorship."},
            {"name": "Public", "description": "Unauthenticated share-link person page endpoints."},
            {"name": "Reports", "description": "Saved Cypher report definitions and execution."},
            {"name": "Review", "description": "Human review queue: assign, action, and resolve match decisions."},
            {"name": "System", "description": "Health check and internal data dump endpoints."},
        ],
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Health, auth, and public (share-link) endpoints — no auth required.
    app.include_router(health.router)
    app.include_router(auth_routes.router)
    app.include_router(oauth.router)
    app.include_router(public_router)
    # The users router is admin-only via its handlers.
    app.include_router(users_routes.router)

    # All other routes require an active (non-first_time) user by default.
    active: list[DependsMarker] = [Depends(require_active_user)]
    app.include_router(person_links_router, dependencies=active)
    app.include_router(entities.router, dependencies=active)
    app.include_router(reports.router, dependencies=active)
    app.include_router(persons.router, dependencies=active)
    app.include_router(person_sales.router, dependencies=active)
    app.include_router(review.router, dependencies=active)
    app.include_router(merge.router, dependencies=active)
    app.include_router(survivorship.router, dependencies=active)
    app.include_router(ingest.router, dependencies=active)
    app.include_router(dumps.router, dependencies=active)
    app.include_router(admin.router, dependencies=active)
    app.include_router(oauth_client_routes.router, dependencies=active)
    app.include_router(events.router, dependencies=active)

    # Frontend-facing API contract, mounted once per UI version. Each mount is a
    # fresh FastAPI instance exposing the same authenticated router set with the
    # /v1 prefix stripped: frontend (v1) -> /app/v1, frontend2 (v2) -> /app/v2.
    app.mount("/app/v1", build_frontend_app())
    app.mount("/app/v2", build_frontend_app())

    register_error_handlers(app)
    return app


app: FastAPI = build_app()
