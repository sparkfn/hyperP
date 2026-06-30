"""Frontend-facing FastAPI sub-application."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI
from fastapi.params import Depends as DependsMarker

from src.auth.deps import require_active_user
from src.error_handlers import register_error_handlers
from src.router_copy import copy_router_routes
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
        swagger_ui_parameters={"operationsSorter": "alpha"},
        openapi_tags=[
            {
                "name": "Admin",
                "description": (
                    "User management, OAuth client registry, "
                    "and source-system field trust."
                ),
            },
            {
                "name": "Auth",
                "description": "Session authentication: current user info and logout.",
            },
            {
                "name": "Entities",
                "description": "Source entity (business unit) directory and their person links.",
            },
            {
                "name": "Events",
                "description": "Downstream event polling for external integrations.",
            },
            {
                "name": "Ingestion",
                "description": "Source record ingest runs and raw record submission.",
            },
            {
                "name": "Persons",
                "description": (
                    "Person profiles, identifiers, connections, timeline, matches, "
                    "merge, and survivorship."
                ),
            },
            {"name": "Reports", "description": "Saved Cypher report definitions and execution."},
            {
                "name": "Review",
                "description": "Human review queue: assign, action, and resolve match decisions.",
            },
        ],
    )
    dependencies: list[DependsMarker] = [Depends(require_active_user)]
    for router in _FRONTEND_ROUTERS:
        copy_router_routes(app, router, dependencies=dependencies)
    register_error_handlers(app)
    return app
