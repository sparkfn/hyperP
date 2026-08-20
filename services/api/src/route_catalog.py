"""Canonical router groupings for HyperP's HTTP API contracts."""

from __future__ import annotations

from fastapi import APIRouter

from src.routes import (
    admin,
    dumps,
    entities,
    events,
    health,
    ingest,
    merge,
    person_crm,
    person_profile_analysis,
    person_sales,
    persons,
    reports,
    review,
    survivorship,
)
from src.routes import auth as auth_routes
from src.routes import oauth as oauth_routes
from src.routes import oauth_clients as oauth_client_routes
from src.routes import users as users_routes
from src.routes.public_pages import person_links_router, public_router

# Authenticated routes used by the active frontend API contract. The same
# routes also supply the MCP tools, so an operation added to a listed router is
# automatically available through both transports.
FRONTEND_ROUTERS: tuple[APIRouter, ...] = (
    auth_routes.router,
    person_links_router,
    entities.router,
    reports.router,
    persons.router,
    person_profile_analysis.router,
    person_sales.router,
    person_crm.router,
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

# Root routes are not part of the authenticated frontend mount. MCP includes
# them in its canonical source app so every schema-visible API operation has a
# tool while the MCP transport itself remains authenticated.
ROOT_ROUTERS: tuple[APIRouter, ...] = (
    health.router,
    oauth_routes.router,
    public_router,
)
