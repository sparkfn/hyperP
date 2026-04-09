"""Shared HTTP helpers: request id, pagination, response envelope builders."""

from __future__ import annotations

import uuid

from fastapi import HTTPException, Request

from src.graph.converters import decode_cursor, encode_cursor
from src.types import ApiError, ApiErrorBody, ApiResponse, ResponseMeta


def request_id(request: Request) -> str:
    """Return the X-Request-Id header or a freshly generated UUID."""
    incoming = request.headers.get("x-request-id")
    return incoming or str(uuid.uuid4())


def clamp_limit(raw: int | None, default: int, maximum: int) -> int:
    """Clamp a paging limit to [1, maximum]."""
    value = raw if raw and raw > 0 else default
    return min(value, maximum)


def page_window(cursor: str | None, raw_limit: int | None) -> tuple[int, int]:
    """Decode pagination params into (skip, limit)."""
    return decode_cursor(cursor), clamp_limit(raw_limit, default=20, maximum=100)


def next_cursor(skip: int, limit: int, has_more: bool) -> str | None:
    """Return the encoded next cursor when more pages exist."""
    return encode_cursor(skip + limit) if has_more else None


def envelope[T](data: T, request: Request, cursor: str | None = None) -> ApiResponse[T]:
    """Wrap a payload in the standard response envelope."""
    return ApiResponse[T](
        data=data,
        meta=ResponseMeta(request_id=request_id(request), next_cursor=cursor),
    )


def http_error(
    status_code: int,
    code: str,
    message: str,
    request: Request,
    details: dict[str, str] | None = None,
) -> HTTPException:
    """Build an HTTPException whose detail is the standard error envelope."""
    body = ApiError(
        error=ApiErrorBody(code=code, message=message, details=details),
        meta=ResponseMeta(request_id=request_id(request)),
    )
    return HTTPException(status_code=status_code, detail=body.model_dump())
