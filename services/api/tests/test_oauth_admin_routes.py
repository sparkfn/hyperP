"""Route tests for OAuth client admin management endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.auth.deps import require_human_admin
from src.auth.models import AuthUser
from src.auth.oauth_token_registry import TokenRecord
from src.routes import oauth_clients as routes


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_human_admin] = lambda: AuthUser(
        email="a@x", google_sub="a", role="admin", entity_key=None, display_name="A"
    )
    return TestClient(app)


def test_rotate_secret_returns_201_and_purges_tokens(client: TestClient) -> None:
    from src.auth.oauth_client_models import RotateSecretResponse

    clear_mock = AsyncMock(return_value=2)
    with (
        patch.object(
            routes,
            "rotate_oauth_client_secret",
            new=AsyncMock(
                return_value=RotateSecretResponse(
                    client_id="hpc_a",
                    client_secret="hps_x",
                    secret_id="sec_2",
                    secret_prefix="hps_x",
                )
            ),
        ),
        patch.object(routes, "clear_client_tokens", new=clear_mock),
    ):
        res = client.post("/v1/admin/oauth-clients/hpc_a/rotate-secret")
    assert res.status_code == 201
    assert res.json()["client_secret"] == "hps_x"
    # Rotation must purge the client's now-dead tokens from the registry.
    clear_mock.assert_awaited_once_with("hpc_a")


def test_rotate_secret_404_when_missing(client: TestClient) -> None:
    with patch.object(routes, "rotate_oauth_client_secret", new=AsyncMock(return_value=None)):
        res = client.post("/v1/admin/oauth-clients/ghost/rotate-secret")
    assert res.status_code == 404


def test_patch_client_204(client: TestClient) -> None:
    with patch.object(routes, "update_oauth_client", new=AsyncMock(return_value=True)):
        res = client.patch(
            "/v1/admin/oauth-clients/hpc_a", json={"access_token_ttl_seconds": 1800}
        )
    assert res.status_code == 204


def test_patch_client_404(client: TestClient) -> None:
    with patch.object(routes, "update_oauth_client", new=AsyncMock(return_value=False)):
        res = client.patch("/v1/admin/oauth-clients/ghost", json={"name": "y"})
    assert res.status_code == 404


def test_list_tokens_returns_views(client: TestClient) -> None:
    rec = TokenRecord(
        jti="t1",
        client_id="hpc_a",
        secret_id="sec_1",
        scope="persons:read",
        issued_at=1,
        expires_at=2,
        last_used_at=None,
        last_used_ip="203.0.113.5",
    )
    with patch.object(routes, "list_client_tokens", new=AsyncMock(return_value=[rec])):
        res = client.get("/v1/admin/oauth-clients/hpc_a/tokens")
    assert res.status_code == 200
    body = res.json()
    assert body[0]["jti"] == "t1"
    assert body[0]["last_used_ip"] == "203.0.113.5"


def test_revoke_token_204(client: TestClient) -> None:
    rec = TokenRecord(
        jti="t1",
        client_id="hpc_a",
        secret_id="sec_1",
        scope="persons:read",
        issued_at=1,
        expires_at=2,
        last_used_at=None,
        last_used_ip=None,
    )
    with (
        patch.object(routes, "list_client_tokens", new=AsyncMock(return_value=[rec])),
        patch.object(routes, "revoke_token_entry", new=AsyncMock(return_value=True)),
        patch.object(routes, "revoke_token", new=AsyncMock(return_value=None)),
    ):
        res = client.post("/v1/admin/oauth-clients/hpc_a/tokens/t1/revoke")
    assert res.status_code == 204


def test_revoke_token_404_when_absent(client: TestClient) -> None:
    with patch.object(routes, "list_client_tokens", new=AsyncMock(return_value=[])):
        res = client.post("/v1/admin/oauth-clients/hpc_a/tokens/ghost/revoke")
    assert res.status_code == 404
