from __future__ import annotations

from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.app import build_app
from src.auth.deps import get_current_user_or_oauth_client, require_active_user
from src.auth.models import AuthUser
from src.repositories.deps import get_entity_repo
from src.repositories.protocols.entity import EntityRepository
from src.types import EntityPerson, EntitySummary, SourceSystemSummary
from starlette.routing import Mount


async def _active_user() -> AuthUser:
    return AuthUser(
        email="user@example.com",
        google_sub="google-user",
        role="employee",
        entity_key="eko",
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


def test_frontend_app_docs_are_mounted_under_app_v1() -> None:
    client = TestClient(build_app())

    docs = client.get("/app/v1/docs")
    openapi = client.get("/app/v1/openapi.json")

    assert docs.status_code == 200
    assert openapi.status_code == 200
    assert openapi.json()["info"]["title"] == "HyperP Frontend API"


def test_frontend_app_docs_are_mounted_under_app_v2() -> None:
    client = TestClient(build_app())

    docs = client.get("/app/v2/docs")
    openapi = client.get("/app/v2/openapi.json")

    assert docs.status_code == 200
    assert openapi.status_code == 200
    assert openapi.json()["info"]["title"] == "HyperP Frontend API"


def test_root_app_does_not_publish_docs_or_schema() -> None:
    # The root app carries only health, the machine OAuth2 token flow, and public
    # share-link pages — no contract worth exposing. Interactive docs and the
    # OpenAPI schema must stay disabled there (sub-app docs are unaffected).
    client = TestClient(build_app())

    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_frontend_app_v2_requires_active_user() -> None:
    app = build_app()
    app.dependency_overrides[get_entity_repo] = _entity_repo
    client = TestClient(app)

    response = client.get("/app/v2/entities")

    assert response.status_code == 401


def test_frontend_app_openapi_uses_unversioned_contract_paths() -> None:
    client = TestClient(build_app())

    paths = openapi_paths(client)

    assert "/persons" in paths
    assert "/review-cases" in paths
    assert "/admin/oauth-clients" in paths
    assert "/v1/persons" not in paths
    assert "/v1/review-cases" not in paths


def test_frontend_app_requires_active_user() -> None:
    app = build_app()
    app.dependency_overrides[get_entity_repo] = _entity_repo
    client = TestClient(app)

    response = client.get("/app/v1/entities")

    assert response.status_code == 401


def test_frontend_app_allows_authenticated_frontend_route() -> None:
    app = build_app()
    frontend_app = mounted_frontend_app(app)
    frontend_app.dependency_overrides[require_active_user] = _active_user
    frontend_app.dependency_overrides[get_current_user_or_oauth_client] = _active_user
    frontend_app.dependency_overrides[get_entity_repo] = _entity_repo
    client = TestClient(app)

    response = client.get("/app/v1/entities")

    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "entity_key": "eko",
            "display_name": "Eko",
            "entity_type": None,
            "country_code": None,
            "is_active": True,
            "person_count": 2,
            "source_record_count": 0,
            "last_ingested_at": None,
            "active_review_cases": 0,
        }
    ]


def test_frontend_app_excludes_public_and_oauth_token_routes() -> None:
    client = TestClient(build_app())

    oauth_response = client.post("/app/v1/oauth/token")
    public_response = client.get("/app/v1/public/persons/token")

    assert oauth_response.status_code == 404
    assert public_response.status_code == 404


def openapi_paths(client: TestClient) -> dict[str, object]:
    payload = client.get("/app/v1/openapi.json").json()
    paths = payload["paths"]
    assert isinstance(paths, dict)
    return paths


def mounted_frontend_app(app: FastAPI) -> FastAPI:
    for route in app.routes:
        if isinstance(route, Mount) and route.path == "/app/v1":
            return cast(FastAPI, route.app)
    raise AssertionError("/app/v1 mount not found")
