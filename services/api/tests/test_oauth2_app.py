"""Tests for the machine-facing /oauth2/v1 sub-application."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from src.auth.deps import OAuthClientUser, get_current_user_or_oauth_client
from src.auth.models import AuthUser
from src.auth.oauth_client_models import OAuthClient, OAuthClientSecret
from src.oauth2_app import build_oauth2_app
from src.repositories.deps import get_person_repo
from src.repositories.protocols.person import PersonListFilters, PersonRepository
from src.types import ListedPerson, Person


def _client() -> OAuthClient:
    return OAuthClient(
        client_id="hpc_test",
        name="POS sync",
        entity_key=None,
        scopes=["persons:read"],
        created_by="admin@example.com",
        created_at=datetime.now(UTC).replace(tzinfo=None),
        disabled_at=None,
        last_used_at=None,
        secrets=[
            OAuthClientSecret(
                secret_id="sec_1",
                secret_prefix="hps_xxxxxx",
                created_at=datetime.now(UTC).replace(tzinfo=None),
                revoked_at=None,
                last_used_at=None,
            )
        ],
    )


class _PersonRepo:
    async def get_page(
        self, filters: PersonListFilters, skip: int, limit: int
    ) -> tuple[list[ListedPerson], int]:
        _ = filters, skip, limit
        return [], 0

    async def get_by_id(self, person_id: str) -> Person | None:
        _ = person_id
        return Person(person_id="p1", status="active")


async def _override_person_repo() -> PersonRepository:
    return _PersonRepo()  # type: ignore[return-value]  # partial stub for route tests


async def _override_oauth_persons_reader() -> OAuthClientUser:
    return OAuthClientUser(
        email="oauth:hpc_reader",
        google_sub="hpc_reader",
        role="employee",
        entity_key="fundbox",
        display_name="Reader client",
        source="oauth_client",
        client_id="hpc_reader",
        key_scopes=["persons:read"],
    )


async def _override_oauth_ingest_writer() -> OAuthClientUser:
    return OAuthClientUser(
        email="oauth:hpc_ingest",
        google_sub="hpc_ingest",
        role="employee",
        entity_key="fundbox",
        display_name="Ingest client",
        source="oauth_client",
        client_id="hpc_ingest",
        key_scopes=["ingest:write"],
    )


async def _override_human_employee() -> AuthUser:
    return AuthUser(
        email="person@example.com",
        google_sub="employee-sub",
        role="employee",
        entity_key="fundbox",
        display_name="Person",
    )


def test_oauth2_token_endpoint_issues_access_token() -> None:
    app = build_oauth2_app()
    client = TestClient(app)

    with (
        patch(
            "src.routes.oauth.validate_client_credentials",
            new=AsyncMock(return_value=(_client(), ["persons:read"])),
        ),
        patch("src.routes.oauth.issue_client_access_token", return_value="jwt-token"),
        patch("src.routes.oauth.register_token", new=AsyncMock(return_value=None)),
    ):
        res = client.post(
            "/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "hpc_test",
                "client_secret": "hps_secret",
                "scope": "persons:read",
            },
        )

    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "no-store"
    assert res.json()["access_token"] == "jwt-token"
    assert res.json()["scope"] == "persons:read"


def test_oauth2_jwks_endpoint_returns_keys() -> None:
    app = build_oauth2_app()
    client = TestClient(app)

    with patch("src.routes.oauth.build_jwks", return_value={"keys": []}):
        res = client.get("/jwks")

    assert res.status_code == 200
    assert res.json() == {"keys": []}


def test_oauth2_persons_list_allows_oauth_client_with_persons_read() -> None:
    app = build_oauth2_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_persons_reader
    app.dependency_overrides[get_person_repo] = _override_person_repo
    client = TestClient(app)

    res = client.get("/persons")

    assert res.status_code == 200
    assert res.json()["data"] == []


def test_oauth2_persons_detail_allows_oauth_client_with_persons_read() -> None:
    app = build_oauth2_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_persons_reader
    app.dependency_overrides[get_person_repo] = _override_person_repo
    client = TestClient(app)

    res = client.get("/persons/p1")

    assert res.status_code == 200
    assert res.json()["data"]["person_id"] == "p1"


def test_oauth2_persons_rejects_oauth_client_without_persons_read_scope() -> None:
    app = build_oauth2_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_ingest_writer
    app.dependency_overrides[get_person_repo] = _override_person_repo
    client = TestClient(app)

    res = client.get("/persons")

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "OAuth client lacks required scope: persons:read"


def test_oauth2_persons_rejects_human_user() -> None:
    app = build_oauth2_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_human_employee
    app.dependency_overrides[get_person_repo] = _override_person_repo
    client = TestClient(app)

    res = client.get("/persons/p1")

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "This endpoint requires OAuth2 client credentials."


def test_oauth2_does_not_expose_person_subresources() -> None:
    app = build_oauth2_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_persons_reader
    app.dependency_overrides[get_person_repo] = _override_person_repo
    client = TestClient(app)

    # Only list + canonical detail are mounted; deeper sub-resources are not.
    assert client.get("/persons/p1/source-records").status_code == 404
    assert client.get("/persons/p1/connections").status_code == 404
    assert client.get("/persons/p1/identifiers").status_code == 404
