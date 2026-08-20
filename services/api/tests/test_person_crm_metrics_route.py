from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.auth.deps import get_current_user_or_oauth_client
from src.auth.models import AuthUser
from src.auth.oauth_client_models import OAuthClientUser
from src.repositories.deps import get_crm_metrics_repo
from src.routes.person_crm import router
from src.types_crm import PersonCrmMetrics


class _CrmRepo:
    def __init__(self, metrics: PersonCrmMetrics | None) -> None:
        self.metrics = metrics

    async def get_person_crm_metrics(self, person_id: str) -> PersonCrmMetrics | None:
        return self.metrics


async def _active_user() -> AuthUser:
    return AuthUser(
        email="reader@example.com",
        google_sub="reader-sub",
        role="employee",
        entity_key="fundbox",
    )


async def _oauth_client_without_scope() -> OAuthClientUser:
    return OAuthClientUser(
        email="oauth:limited",
        google_sub="limited-client",
        role="employee",
        entity_key="fundbox",
        client_id="limited-client",
        key_scopes=[],
    )


@pytest.mark.anyio
async def test_crm_metrics_route_returns_enveloped_payload() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_crm_metrics_repo] = lambda: _CrmRepo(PersonCrmMetrics())
    app.dependency_overrides[get_current_user_or_oauth_client] = _active_user
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/v1/persons/person-1/crm/metrics",
            headers={"x-request-id": "req-crm"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "data": PersonCrmMetrics().model_dump(),
        "meta": {"request_id": "req-crm", "next_cursor": None, "total_count": None},
        "display_items": None,
    }


@pytest.mark.anyio
async def test_crm_metrics_route_returns_404_for_missing_person() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_crm_metrics_repo] = lambda: _CrmRepo(None)
    app.dependency_overrides[get_current_user_or_oauth_client] = _active_user
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/v1/persons/missing/crm/metrics")

    assert response.status_code == 404


@pytest.mark.anyio
async def test_crm_metrics_route_enforces_persons_read_scope() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_or_oauth_client] = _oauth_client_without_scope
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/v1/persons/person-1/crm/metrics")

    assert response.status_code == 403
