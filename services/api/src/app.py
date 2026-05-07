"""FastAPI app factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.params import Depends as DependsMarker
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.auth.deps import require_active_user
from src.auth.oauth_clients import ensure_oauth_client_constraints
from src.auth.oauth_tokens import validate_oauth_runtime_config
from src.config import config
from src.graph.client import close_driver, get_session
from src.graph.queries.users import CREATE_USER_CONSTRAINT
from src.http_utils import request_id
from src.llm.service import close_llm_service
from src.redis_client import close_redis
from src.routes import (
    admin,
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
from src.types import ApiError, ApiErrorBody, ResponseMeta

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


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage the Neo4j driver lifecycle alongside the FastAPI process."""
    validate_oauth_runtime_config()
    await _ensure_user_constraint()
    await _ensure_oauth_client_constraints()
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
    app.include_router(admin.router, dependencies=active)
    app.include_router(oauth_client_routes.router, dependencies=active)
    app.include_router(events.router, dependencies=active)

    _register_error_handlers(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict):
            return JSONResponse(exc.detail, status_code=exc.status_code)
        body = ApiError(
            error=ApiErrorBody(code=_default_code(exc.status_code), message=str(exc.detail)),
            meta=ResponseMeta(request_id=request_id(request)),
        )
        return JSONResponse(body.model_dump(), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        body = ApiError(
            error=ApiErrorBody(code="invalid_request", message=str(exc.errors())),
            meta=ResponseMeta(request_id=request_id(request)),
        )
        return JSONResponse(body.model_dump(), status_code=400)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error", exc_info=exc)
        body = ApiError(
            error=ApiErrorBody(code="internal_error", message="An internal error occurred."),
            meta=ResponseMeta(request_id=request_id(request)),
        )
        return JSONResponse(body.model_dump(), status_code=500)


def _default_code(status_code: int) -> str:
    if status_code == 404:
        return "not_found"
    if status_code == 401:
        return "unauthorized"
    if status_code == 403:
        return "forbidden"
    if status_code == 409:
        return "conflict"
    if status_code == 422:
        return "unprocessable_entity"
    if status_code >= 500:
        return "internal_error"
    return "invalid_request"


app: FastAPI = build_app()
