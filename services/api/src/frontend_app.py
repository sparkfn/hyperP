"""Frontend-facing FastAPI sub-application."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import APIRouter, Depends, FastAPI
from fastapi.params import Depends as DependsMarker
from fastapi.routing import APIRoute

from src.auth.deps import require_active_user
from src.error_handlers import register_error_handlers
from src.routes import (
    admin,
    dumps,
    entities,
    events,
    ingest,
    merge,
    person_sales,
    persons,
    reports,
    review,
    survivorship,
)
from src.routes import auth as auth_routes
from src.routes import oauth_clients as oauth_client_routes
from src.routes import users as users_routes
from src.routes.public_pages import person_links_router

_FRONTEND_ROUTERS: tuple[APIRouter, ...] = (
    auth_routes.router,
    person_links_router,
    entities.router,
    reports.router,
    persons.router,
    person_sales.router,
    review.router,
    merge.router,
    survivorship.router,
    ingest.router,
    dumps.router,
    admin.router,
    oauth_client_routes.router,
    events.router,
    users_routes.router,
)


def build_frontend_app() -> FastAPI:
    """Build the frontend-facing API contract mounted by the main app."""
    app = FastAPI(
        title="HyperP Frontend API",
        version="0.1.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    dependencies: list[DependsMarker] = [Depends(require_active_user)]
    for router in _FRONTEND_ROUTERS:
        _copy_router_routes(app, router, dependencies=dependencies)
    register_error_handlers(app)
    return app


def _copy_router_routes(
    app: FastAPI,
    router: APIRouter,
    *,
    dependencies: list[DependsMarker],
) -> None:
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        path = _strip_v1_prefix(route.path)
        endpoint = route.endpoint
        app.add_api_route(
            path,
            endpoint,
            response_model=route.response_model,
            status_code=route.status_code,
            tags=route.tags,
            dependencies=[*dependencies, *route.dependencies],
            summary=route.summary,
            description=route.description,
            response_description=route.response_description,
            responses=route.responses,
            deprecated=route.deprecated,
            methods=_sorted_methods(route.methods),
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


def _strip_v1_prefix(path: str) -> str:
    if path == "/v1":
        return ""
    if path.startswith("/v1/"):
        return path[3:]
    return path


def _sorted_methods(methods: Iterable[str] | None) -> list[str] | None:
    if methods is None:
        return None
    return sorted(methods)
