"""MCP server exposing HyperP's schema-visible FastAPI operations as tools."""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.params import Depends as DependsMarker
from fastapi.responses import JSONResponse
from fastapi_mcp import AuthConfig, FastApiMCP
from pydantic import BaseModel

from src.auth.deps import require_active_user
from src.auth.oauth_client_models import OAuthTokenResponse
from src.error_handlers import register_error_handlers
from src.route_catalog import FRONTEND_ROUTERS, ROOT_ROUTERS
from src.router_copy import copy_router_routes
from src.routes import oauth as oauth_routes


def _identity_path(path: str) -> str:
    """Keep root-route paths unchanged in the MCP source application."""
    return path


class McpOAuthTokenRequest(BaseModel):
    """JSON representation of OAuth token form data for MCP tool calls."""

    grant_type: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scope: str | None = None


async def _mcp_oauth_token(
    payload: McpOAuthTokenRequest,
) -> OAuthTokenResponse | JSONResponse:
    """Issue an OAuth token using MCP's JSON request representation."""
    return await oauth_routes.token(
        grant_type=payload.grant_type,
        client_id=payload.client_id,
        client_secret=payload.client_secret,
        scope=payload.scope,
    )


def _copy_root_routes(app: FastAPI) -> None:
    """Copy root operations, adapting form-only OAuth token input for MCP."""
    for router in ROOT_ROUTERS:
        copy_router_routes(
            app,
            router,
            dependencies=[],
            path_filter=lambda path: path != "/v1/oauth/token",
            path_transform=_identity_path,
        )
    app.add_api_route(
        "/v1/oauth/token",
        _mcp_oauth_token,
        response_model=OAuthTokenResponse,
        tags=["OAuth"],
        summary="Issue an access token using OAuth2 client credentials.",
        methods=["POST"],
        operation_id="token_v1_oauth_token_post",
    )


def build_mcp_source_app() -> FastAPI:
    """Build the canonical API surface used to generate MCP tools.

    Frontend operations use their unversioned `/app/v2` paths. Root operations
    retain their public HTTP paths. The MCP transport adds its own authentication
    dependency, while copied frontend operations retain their existing endpoint
    authorization rules.
    """
    app = FastAPI(
        title="HyperP MCP API",
        description="HyperP API operations available to authenticated MCP clients.",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    frontend_dependencies: list[DependsMarker] = [Depends(require_active_user)]

    for router in FRONTEND_ROUTERS:
        copy_router_routes(app, router, dependencies=frontend_dependencies)
    _copy_root_routes(app)

    register_error_handlers(app)
    return app


def build_mcp_server(source_app: FastAPI) -> FastApiMCP:
    """Create an authenticated MCP server backed by *source_app*."""
    return FastApiMCP(
        source_app,
        name="HyperP MCP",
        description="Authenticated Model Context Protocol tools for HyperP.",
        auth_config=AuthConfig(dependencies=[Depends(require_active_user)]),
        headers=["authorization"],
    )


def mount_mcp(app: FastAPI) -> None:
    """Mount the authenticated Streamable HTTP MCP endpoint at ``/mcp``."""
    mcp = build_mcp_server(build_mcp_source_app())
    app.state.hyperp_mcp = mcp
    mcp.mount_http(app, mount_path="/mcp")


async def shutdown_mcp(app: FastAPI) -> None:
    """Release the MCP bridge client and its Streamable HTTP manager."""
    mcp = getattr(app.state, "hyperp_mcp", None)
    if not isinstance(mcp, FastApiMCP):
        return

    transport = mcp._http_transport  # noqa: SLF001 - no public shutdown hook exists
    if transport is not None:
        await transport.shutdown()
    await mcp._http_client.aclose()  # noqa: SLF001 - client is owned by FastApiMCP
