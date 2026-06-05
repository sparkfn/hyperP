"""Helpers for copying APIRouter routes onto a mounted sub-application.

Sub-apps (the frontend-facing `/app/vN` contracts and the machine-facing
`/oauth2/v1` contract) re-expose the same endpoint functions under a fresh
`FastAPI` instance with the `/v1` prefix stripped. These helpers centralize the
route-copying logic so every mount stays in sync.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from fastapi import APIRouter, FastAPI
from fastapi.params import Depends as DependsMarker
from fastapi.routing import APIRoute


def strip_v1_prefix(path: str) -> str:
    """Strip the leading ``/v1`` segment from a router path."""
    if path == "/v1":
        return ""
    if path.startswith("/v1/"):
        return path[3:]
    return path


def sorted_methods(methods: Iterable[str] | None) -> list[str] | None:
    """Return a route's HTTP methods in a deterministic order."""
    if methods is None:
        return None
    return sorted(methods)


def copy_router_routes(
    app: FastAPI,
    router: APIRouter,
    *,
    dependencies: list[DependsMarker],
    path_filter: Callable[[str], bool] | None = None,
    path_transform: Callable[[str], str] | None = None,
) -> None:
    """Copy a router's API routes onto *app*, rewriting each route's path.

    When *path_filter* is provided, only routes whose original path satisfies
    the predicate are copied. *path_transform* maps each route's path onto the
    sub-app and defaults to stripping the ``/v1`` prefix. The extra
    *dependencies* are prepended to each route's own dependencies.
    """
    transform = path_transform if path_transform is not None else strip_v1_prefix
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        if path_filter is not None and not path_filter(route.path):
            continue
        app.add_api_route(
            transform(route.path),
            route.endpoint,
            response_model=route.response_model,
            status_code=route.status_code,
            tags=route.tags,
            dependencies=[*dependencies, *route.dependencies],
            summary=route.summary,
            description=route.description,
            response_description=route.response_description,
            responses=route.responses,
            deprecated=route.deprecated,
            methods=sorted_methods(route.methods),
            operation_id=route.operation_id,
            response_model_include=route.response_model_include,
            response_model_exclude=route.response_model_exclude,
            response_model_by_alias=route.response_model_by_alias,
            response_model_exclude_unset=route.response_model_exclude_unset,
            response_model_exclude_defaults=route.response_model_exclude_defaults,
            response_model_exclude_none=route.response_model_exclude_none,
            include_in_schema=route.include_in_schema,
            response_class=route.response_class,
            name=route.name,
            openapi_extra=route.openapi_extra,
        )
