"""Authenticated routing contracts for Person profile analyses."""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.app import build_app
from src.auth.deps import get_current_user_or_oauth_client, require_active_user
from src.auth.models import AuthUser
from src.auth.oauth_client_models import OAuthClientUser
from src.config import AppConfig, get_config
from src.repositories.deps import get_person_repo
from src.types_profile_analysis import (
    PersonProfileAnalyses,
    ProfileAnalysisHistoryItem,
    ProfileAnalysisRequestResult,
    ProfileAnalysisSlot,
    ProfileAnalysisType,
)
from starlette.routing import Mount


def _pending() -> PersonProfileAnalyses:
    pending_slot = ProfileAnalysisSlot(
        current=None,
        stale=False,
        expired=False,
        valid=False,
        invalid_reason="missing",
        refresh_state="pending",
        failure_code=None,
        auto_request_allowed=True,
        next_retry_at=None,
        next_retry_at_display=None,
        force_attempts_remaining=3,
        force_available_at=None,
        force_available_at_display=None,
    )
    return PersonProfileAnalyses(
        input_revision=0,
        refresh_state="pending",
        sales=pending_slot,
        contact_tracing=pending_slot,
    )


def _history_item() -> ProfileAnalysisHistoryItem:
    return ProfileAnalysisHistoryItem(
        analysis_id="analysis-sales",
        person_id="canonical-person",
        analysis_type="sales",
        status="succeeded",
        content="Supported analysis [order-1]",
        input_revision=7,
        input_fingerprint="sha256-fingerprint",
        prompt_version="sales-profile-v1",
        provider="proclaude",
        model="analysis-model",
        started_at="2026-07-21T01:00:00+00:00",
        completed_at="2026-07-21T01:02:00+00:00",
        completed_at_display="21 Jul 2026, 01:02 AM",
        attempt_number=2,
        failure_code=None,
        retryable=None,
        next_retry_at=None,
    )


class _ProfileAnalysisRepo:
    def __init__(self) -> None:
        self.missing = False
        self.empty_history = False
        self.current = _pending()
        self.history_args: tuple[str, ProfileAnalysisType | None, int, int] | None = None
        self.request_args: tuple[str, ProfileAnalysisType, bool] | None = None
        self.force_limited = False

    async def get_profile_analyses(self, person_id: str) -> PersonProfileAnalyses | None:
        _ = person_id
        return None if self.missing else self.current

    async def get_profile_analysis_history(
        self,
        person_id: str,
        analysis_type: ProfileAnalysisType | None,
        skip: int,
        limit: int,
    ) -> tuple[list[ProfileAnalysisHistoryItem], int] | None:
        self.history_args = (person_id, analysis_type, skip, limit)
        if self.missing:
            return None
        if self.empty_history:
            return [], 0
        return [_history_item(), _history_item()], 5

    async def request_profile_analysis(
        self,
        person_id: str,
        analysis_type: ProfileAnalysisType,
        force: bool,
    ) -> ProfileAnalysisRequestResult | None:
        self.request_args = (person_id, analysis_type, force)
        if self.missing:
            return None
        return ProfileAnalysisRequestResult(
            request_id="request-1",
            person_id=person_id,
            analysis_type=analysis_type,
            state="force_limited" if self.force_limited else "queued",
            force=force,
            force_attempts_remaining=0 if self.force_limited else (2 if force else 3),
            force_available_at=("2026-07-27T02:00:00+00:00" if self.force_limited else None),
            force_available_at_display=("27 Jul 2026, 02:00 AM" if self.force_limited else None),
        )

    async def mark_profile_analysis_request_dispatch_failed(self, request_id: str) -> None:
        _ = request_id


async def _active_user() -> AuthUser:
    return AuthUser(
        email="person@example.com",
        google_sub="employee-sub",
        role="employee",
        entity_key="fundbox",
    )


def _inactive_user() -> AuthUser:
    return AuthUser(
        email="pending@example.com",
        google_sub="pending-sub",
        role="first_time",
        entity_key=None,
    )


def _persons_read_oauth_client() -> OAuthClientUser:
    return OAuthClientUser(
        email="oauth:profile-reader",
        google_sub="profile-reader",
        role="employee",
        entity_key="fundbox",
        display_name="Profile reader",
        client_id="profile-reader",
        key_scopes=["persons:read"],
    )


def _mounted_app(app: FastAPI, path: str) -> FastAPI:
    for route in app.routes:
        if isinstance(route, Mount) and route.path == path:
            return cast(FastAPI, route.app)
    raise AssertionError(f"{path} mount not found")


def _authenticated_client(repo: _ProfileAnalysisRepo) -> TestClient:
    app = build_app()
    frontend = _mounted_app(app, "/app/v2")
    frontend.dependency_overrides[require_active_user] = _active_user
    frontend.dependency_overrides[get_current_user_or_oauth_client] = _active_user
    frontend.dependency_overrides[get_person_repo] = lambda: repo
    frontend.dependency_overrides[get_config] = lambda: AppConfig(
        NEO4J_PASSWORD="test",
        PROFILE_ANALYSIS_ENABLED=True,
        _env_file=None,
    )
    return TestClient(app)


def _principal_client(
    repo: _ProfileAnalysisRepo,
    principal: AuthUser | OAuthClientUser,
) -> TestClient:
    app = build_app()
    frontend = _mounted_app(app, "/app/v2")

    async def _override_principal() -> AuthUser | OAuthClientUser:
        return principal

    frontend.dependency_overrides[get_current_user_or_oauth_client] = _override_principal
    frontend.dependency_overrides[get_person_repo] = lambda: repo
    frontend.dependency_overrides[get_config] = lambda: AppConfig(
        NEO4J_PASSWORD="test",
        PROFILE_ANALYSIS_ENABLED=True,
        _env_file=None,
    )
    return TestClient(app)


def test_current_route_is_mounted_for_authenticated_frontend_only() -> None:
    repo = _ProfileAnalysisRepo()
    client = _authenticated_client(repo)

    response = client.get("/app/v2/persons/person-1/profile-analyses")

    assert response.status_code == 200
    assert response.json()["data"]["refresh_state"] == "pending"
    assert client.get("/v1/persons/person-1/profile-analyses").status_code == 404
    assert client.get("/v1/public/persons/token/profile-analyses").status_code == 404
    assert client.get("/oauth2/v1/persons/person-1/profile-analyses").status_code == 404


def test_current_route_reports_disabled_generation_without_queued_state() -> None:
    repo = _ProfileAnalysisRepo()
    failed_slot = ProfileAnalysisSlot(
        current=None,
        stale=False,
        expired=False,
        valid=False,
        invalid_reason="missing",
        refresh_state="failed",
        failure_code="provider_unavailable",
        auto_request_allowed=False,
        next_retry_at=None,
        next_retry_at_display=None,
        force_attempts_remaining=3,
        force_available_at=None,
        force_available_at_display=None,
    )
    repo.current = PersonProfileAnalyses(
        input_revision=3,
        refresh_state="failed",
        sales=failed_slot,
        contact_tracing=failed_slot,
    )
    app = build_app()
    frontend = _mounted_app(app, "/app/v2")
    frontend.dependency_overrides[require_active_user] = _active_user
    frontend.dependency_overrides[get_current_user_or_oauth_client] = _active_user
    frontend.dependency_overrides[get_person_repo] = lambda: repo
    frontend.dependency_overrides[get_config] = lambda: AppConfig(
        NEO4J_PASSWORD="test",
        PROFILE_ANALYSIS_ENABLED=False,
        _env_file=None,
    )

    response = TestClient(app).get("/app/v2/persons/person-1/profile-analyses")

    assert response.status_code == 200
    assert response.json()["data"]["refresh_state"] == "disabled"
    assert response.json()["data"]["sales"]["refresh_state"] == "disabled"
    assert response.json()["data"]["contact_tracing"]["refresh_state"] == "disabled"
    assert response.json()["data"]["sales"]["failure_code"] is None
    assert response.json()["data"]["contact_tracing"]["failure_code"] is None


def test_history_route_is_excluded_from_root_public_and_oauth_mounts() -> None:
    client = _authenticated_client(_ProfileAnalysisRepo())

    assert client.get("/v1/persons/person-1/profile-analyses/history").status_code == 404
    assert client.get("/v1/public/persons/token/profile-analyses/history").status_code == 404
    assert client.get("/oauth2/v1/persons/person-1/profile-analyses/history").status_code == 404


def test_current_route_requires_authentication() -> None:
    client = TestClient(build_app())

    response = client.get("/app/v2/persons/person-1/profile-analyses")

    assert response.status_code == 401


def test_history_route_requires_authentication() -> None:
    client = TestClient(build_app())

    response = client.get("/app/v2/persons/person-1/profile-analyses/history")

    assert response.status_code == 401


def test_current_route_rejects_inactive_human() -> None:
    client = _principal_client(_ProfileAnalysisRepo(), _inactive_user())

    response = client.get("/app/v2/persons/person-1/profile-analyses")

    assert response.status_code == 403


def test_history_route_rejects_inactive_human() -> None:
    client = _principal_client(_ProfileAnalysisRepo(), _inactive_user())

    response = client.get("/app/v2/persons/person-1/profile-analyses/history")

    assert response.status_code == 403


def test_current_route_rejects_persons_read_oauth_client() -> None:
    client = _principal_client(_ProfileAnalysisRepo(), _persons_read_oauth_client())

    response = client.get("/app/v2/persons/person-1/profile-analyses")

    assert response.status_code == 403


def test_history_route_rejects_persons_read_oauth_client() -> None:
    client = _principal_client(_ProfileAnalysisRepo(), _persons_read_oauth_client())

    response = client.get("/app/v2/persons/person-1/profile-analyses/history")

    assert response.status_code == 403


def test_current_route_404s_for_missing_person() -> None:
    repo = _ProfileAnalysisRepo()
    repo.missing = True

    response = _authenticated_client(repo).get("/app/v2/persons/missing/profile-analyses")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "person_not_found"


def test_request_route_queues_one_independent_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _ProfileAnalysisRepo()
    queued: list[str] = []
    monkeypatch.setattr(
        "src.routes.person_profile_analysis.enqueue_profile_analysis_request",
        lambda request_id: queued.append(request_id),
    )

    response = _authenticated_client(repo).post(
        "/app/v2/persons/person-1/profile-analyses/requests",
        json={"analysis_type": "sales", "force": False},
    )

    assert response.status_code == 202
    assert repo.request_args == ("person-1", "sales", False)
    assert queued == ["request-1"]


def test_request_route_returns_force_limit_availability() -> None:
    repo = _ProfileAnalysisRepo()
    repo.force_limited = True

    response = _authenticated_client(repo).post(
        "/app/v2/persons/person-1/profile-analyses/requests",
        json={"analysis_type": "sales", "force": True},
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "profile_analysis_force_limit"
    assert response.json()["error"]["details"] == {
        "force_available_at": "2026-07-27T02:00:00+00:00",
        "force_available_at_display": "27 Jul 2026, 02:00 AM",
    }


def test_history_route_filters_and_paginates_with_count() -> None:
    repo = _ProfileAnalysisRepo()
    client = _authenticated_client(repo)

    response = client.get(
        "/app/v2/persons/person-1/profile-analyses/history?analysis_type=sales&cursor=Mg==&limit=2"
    )

    assert response.status_code == 200
    assert repo.history_args == ("person-1", "sales", 2, 2)
    assert response.json()["meta"] == {
        "request_id": response.json()["meta"]["request_id"],
        "next_cursor": "NA==",
        "total_count": 5,
    }


def test_history_route_rejects_unknown_analysis_type() -> None:
    response = _authenticated_client(_ProfileAnalysisRepo()).get(
        "/app/v2/persons/person-1/profile-analyses/history?analysis_type=summary"
    )

    assert response.status_code == 400


def test_history_route_returns_empty_page_for_existing_person() -> None:
    repo = _ProfileAnalysisRepo()
    repo.empty_history = True

    response = _authenticated_client(repo).get("/app/v2/persons/person-1/profile-analyses/history")

    assert response.status_code == 200
    assert response.json()["data"] == []
    assert response.json()["meta"]["total_count"] == 0
    assert response.json()["meta"]["next_cursor"] is None


def test_history_route_404s_for_missing_person() -> None:
    repo = _ProfileAnalysisRepo()
    repo.missing = True

    response = _authenticated_client(repo).get("/app/v2/persons/missing/profile-analyses/history")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "person_not_found"


def test_profile_analysis_routes_are_excluded_from_oauth_schema() -> None:
    app = build_app()
    frontend_paths = _mounted_app(app, "/app/v2").openapi()["paths"]
    oauth_paths = _mounted_app(app, "/oauth2/v1").openapi()["paths"]

    assert "/persons/{person_id}/profile-analyses" in frontend_paths
    assert "/persons/{person_id}/profile-analyses/history" in frontend_paths
    assert "/persons/{person_id}/profile-analyses" not in oauth_paths
    assert "/persons/{person_id}/profile-analyses/history" not in oauth_paths
