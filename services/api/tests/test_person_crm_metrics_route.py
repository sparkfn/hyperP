from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.auth.deps import get_current_user_or_oauth_client
from src.auth.models import AuthUser
from src.auth.oauth_client_models import OAuthClientUser
from src.repositories.deps import get_crm_activity_metrics_repo, get_crm_deal_metrics_repo
from src.routes.person_crm import router
from src.types_crm import (
    BitrixDealScope,
    PersonCrmActivityMetrics,
    PersonCrmActivityMetricsComplete,
    PersonCrmDealMetrics,
)


class _DealRepo:
    def __init__(self, metrics: PersonCrmDealMetrics | None) -> None:
        self.metrics = metrics
        self.scope_calls: list[tuple[str, str, int]] = []

    async def get_person_crm_deal_metrics(self, person_id: str) -> PersonCrmDealMetrics | None:
        return self.metrics

    async def resolve_bitrix_deal_scope(
        self, person_id: str, source_instance: str, deal_limit: int
    ) -> BitrixDealScope | None:
        self.scope_calls.append((person_id, source_instance, deal_limit))
        if self.metrics is None:
            return None
        return BitrixDealScope(
            canonical_person_id="canonical-1",
            deal_ids=("10",),
            resolved_deal_count=1,
            deal_limit_exhausted=False,
        )


class _ActivityRepo:
    def __init__(self, metrics: PersonCrmActivityMetrics) -> None:
        self.metrics = metrics
        self.scopes: list[BitrixDealScope] = []

    async def get_person_crm_activity_metrics(
        self, scope: BitrixDealScope
    ) -> PersonCrmActivityMetrics:
        self.scopes.append(scope)
        return self.metrics


async def _active_user() -> AuthUser:
    return AuthUser(
        email="reader@example.com",
        google_sub="reader-sub",
        role="employee",
        entity_key="fundbox",
    )


async def _oauth_without_scope() -> OAuthClientUser:
    return OAuthClientUser(
        email="oauth:limited",
        google_sub="limited-client",
        role="employee",
        entity_key="fundbox",
        client_id="limited-client",
        key_scopes=[],
    )


def _complete() -> PersonCrmActivityMetricsComplete:
    return PersonCrmActivityMetricsComplete(
        source_instance="bitrix-primary",
        fetched_at="2026-09-05T00:00:00+00:00",
        cache_disposition="miss",
        queried_deal_count=1,
        resolved_deal_count=1,
        request_count=1,
        page_count=1,
        row_count=0,
        activity_count=0,
        call_count=0,
        activity_kind_breakdown=[],
        call_classification_breakdown=[],
        recent_30d_activity_count=0,
        recent_30d_call_count=0,
        recent_30d_daily_activity_counts=[0] * 30,
        recent_30d_daily_call_counts=[0] * 30,
    )


def _app(deal_repo: _DealRepo, activity_repo: _ActivityRepo) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_crm_deal_metrics_repo] = lambda: deal_repo
    app.dependency_overrides[get_crm_activity_metrics_repo] = lambda: activity_repo
    app.dependency_overrides[get_current_user_or_oauth_client] = _active_user
    return app


@pytest.mark.anyio
async def test_split_routes_return_enveloped_payloads_and_resolve_bounded_scope() -> None:
    deal_repo = _DealRepo(PersonCrmDealMetrics(deal_count=2))
    activity_repo = _ActivityRepo(_complete())
    transport = ASGITransport(app=_app(deal_repo, activity_repo))

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        deal_response = await client.get(
            "/v1/persons/person-1/crm/deal-metrics", headers={"x-request-id": "deal-id"}
        )
        activity_response = await client.get(
            "/v1/persons/person-1/crm/activity-metrics",
            headers={"x-request-id": "activity-id"},
        )

    assert deal_response.status_code == 200
    assert deal_response.json()["data"]["deal_count"] == 2
    assert deal_response.json()["meta"]["request_id"] == "deal-id"
    assert activity_response.status_code == 200
    assert activity_response.json()["data"]["status"] == "complete"
    assert activity_response.json()["data"]["activity_count"] == 0
    assert activity_response.json()["meta"]["request_id"] == "activity-id"
    assert len(deal_repo.scope_calls) == 1
    assert deal_repo.scope_calls[0][0] == "person-1"
    assert activity_repo.scopes[0].deal_ids == ("10",)


@pytest.mark.anyio
@pytest.mark.parametrize("suffix", ["deal-metrics", "activity-metrics"])
async def test_split_routes_return_404_for_missing_person(suffix: str) -> None:
    app = _app(_DealRepo(None), _ActivityRepo(_complete()))
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/v1/persons/missing/crm/{suffix}")

    assert response.status_code == 404


@pytest.mark.anyio
async def test_split_routes_enforce_persons_read_scope() -> None:
    app = _app(_DealRepo(PersonCrmDealMetrics()), _ActivityRepo(_complete()))
    app.dependency_overrides[get_current_user_or_oauth_client] = _oauth_without_scope
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        responses = await asyncio.gather(
            client.get("/v1/persons/person-1/crm/deal-metrics"),
            client.get("/v1/persons/person-1/crm/activity-metrics"),
        )

    assert [response.status_code for response in responses] == [403, 403]


def test_openapi_has_exact_split_operation_ids_and_no_combined_route() -> None:
    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()

    assert "/v1/persons/{person_id}/crm/metrics" not in schema["paths"]
    assert schema["paths"]["/v1/persons/{person_id}/crm/deal-metrics"]["get"][
        "operationId"
    ] == "get_person_crm_deal_metrics"
    assert schema["paths"]["/v1/persons/{person_id}/crm/activity-metrics"]["get"][
        "operationId"
    ] == "get_person_crm_activity_metrics"
