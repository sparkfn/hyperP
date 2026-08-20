"""Tests for the authenticated MCP API contract."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.testclient import TestClient
from src.app import build_app
from src.auth.deps import get_current_user_or_oauth_client, require_active_user
from src.auth.models import AuthUser
from src.mcp_app import build_mcp_server, build_mcp_source_app
from src.repositories.deps import get_entity_repo
from src.repositories.protocols.entity import EntityRepository
from src.types import EntityFilterOption, EntityPerson, EntitySummary, SourceSystemSummary


async def _active_user() -> AuthUser:
    return AuthUser(
        email="admin@example.com",
        google_sub="admin-user",
        role="admin",
        entity_key=None,
    )


class _EntityRepo:
    async def get_all(self) -> list[EntitySummary]:
        return [
            EntitySummary(
                entity_key="eko",
                display_name="Eko",
                source_system_count=1,
                person_count=2,
            )
        ]

    async def get_filter_options(self) -> list[EntityFilterOption]:
        return [EntityFilterOption(entity_key="eko", display_name="Eko")]

    async def get_source_systems(self) -> list[SourceSystemSummary]:
        return []

    async def list_persons(
        self,
        entity_key: str,
        skip: int,
        limit: int,
        sort_by: str,
        sort_order: str,
    ) -> tuple[list[EntityPerson], bool]:
        _ = (entity_key, skip, limit, sort_by, sort_order)
        return [], False


def _entity_repo() -> EntityRepository:
    return _EntityRepo()


def _source_operation_ids(app: FastAPI) -> set[str]:
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
    )
    operation_ids: list[str] = []
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_id = operation["operationId"]
            assert isinstance(operation_id, str)
            operation_ids.append(operation_id)
    assert len(operation_ids) == len(set(operation_ids))
    return set(operation_ids)


def test_mcp_tools_match_every_canonical_api_operation() -> None:
    source_app = build_mcp_source_app()
    mcp = build_mcp_server(source_app)

    operation_ids = _source_operation_ids(source_app)
    tool_names = {tool.name for tool in mcp.tools}

    assert tool_names == operation_ids


def _initialize_mcp(client: TestClient) -> str:
    response = client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer test-token",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        },
    )

    assert response.status_code == 200
    return response.headers["mcp-session-id"]


@pytest.mark.asyncio
async def test_mcp_transport_forwards_authorization_to_the_api_operation() -> None:
    source_app = build_mcp_source_app()
    source_app.dependency_overrides[require_active_user] = _active_user
    source_app.dependency_overrides[get_current_user_or_oauth_client] = _active_user
    source_app.dependency_overrides[get_entity_repo] = _entity_repo
    mcp = build_mcp_server(source_app)
    transport_app = FastAPI()
    transport_app.dependency_overrides[require_active_user] = _active_user
    mcp.mount_http(transport_app, mount_path="/mcp")

    try:
        with TestClient(transport_app) as client:
            session_id = _initialize_mcp(client)
            response = client.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": "Bearer test-token",
                    "Mcp-Session-Id": session_id,
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "list_entities_entities_get",
                        "arguments": {},
                    },
                },
            )
    finally:
        await mcp._http_client.aclose()  # noqa: SLF001 - bridge client owned by FastApiMCP

    assert response.status_code == 200
    content = response.json()["result"]["content"]
    payload = json.loads(content[0]["text"])
    assert payload["data"][0]["entity_key"] == "eko"


def test_mcp_oauth_token_tool_accepts_mcp_json_arguments() -> None:
    client = TestClient(build_mcp_source_app())

    response = client.post("/v1/oauth/token", json={})

    assert response.status_code == 400
    assert response.json()["error_description"] == "Missing required form field: grant_type."


def test_mcp_transport_requires_an_authenticated_principal() -> None:
    client = TestClient(build_app())

    response = client.get("/mcp")

    assert response.status_code == 401


def test_mcp_transport_accepts_an_authenticated_initialize_request() -> None:
    app = build_app()
    app.dependency_overrides[require_active_user] = _active_user
    client = TestClient(app)

    response = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        },
    )

    assert response.status_code == 200
    assert response.headers["mcp-session-id"]
    assert response.json()["result"]["serverInfo"]["name"] == "HyperP MCP"
