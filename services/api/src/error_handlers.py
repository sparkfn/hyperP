"""Shared FastAPI exception handlers."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.http_utils import request_id
from src.types import ApiError, ApiErrorBody, ResponseMeta

logger = logging.getLogger("profile_unifier_api")


def register_error_handlers(app: FastAPI) -> None:
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
