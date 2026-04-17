"""FastAPI app factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.config import config
from src.graph.client import close_driver
from src.http_utils import request_id
from src.routes import admin, entities, events, health, ingest, merge, persons, reports, review, survivorship
from src.types import ApiError, ApiErrorBody, ResponseMeta

logger = logging.getLogger("profile_unifier_api")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage the Neo4j driver lifecycle alongside the FastAPI process."""
    yield
    await close_driver()


def build_app() -> FastAPI:
    """Construct and configure the FastAPI application."""
    logging.basicConfig(level=config.log_level.upper())
    app = FastAPI(title="Profile Unifier API", version="0.1.0", lifespan=_lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(entities.router)
    app.include_router(reports.router)
    app.include_router(persons.router)
    app.include_router(review.router)
    app.include_router(merge.router)
    app.include_router(survivorship.router)
    app.include_router(ingest.router)
    app.include_router(admin.router)
    app.include_router(events.router)

    _register_error_handlers(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if isinstance(exc.detail, dict):
            return JSONResponse(exc.detail, status_code=exc.status_code)
        body = ApiError(
            error=ApiErrorBody(code=_default_code(exc.status_code), message=str(exc.detail)),
            meta=ResponseMeta(request_id=request_id(request)),
        )
        return JSONResponse(body.model_dump(), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
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
