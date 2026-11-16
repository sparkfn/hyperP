"""Route-level tests for the read-only Profiles surface."""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi.testclient import TestClient
from src.app import build_app
from src.auth.deps import get_current_user_or_oauth_client, require_active_user
from src.auth.models import AuthUser
from src.frontend_app import build_frontend_app
from src.oauth2_app import build_oauth2_app
from src.repositories.deps import get_profile_repo
from src.repositories.protocols.profile import ProfileListFilters
from src.types import PersonIdentifier, Profile, ProfileDetail, SourceRecord


async def _active_user() -> AuthUser:
    return AuthUser(
        email="user@example.com",
        google_sub="google-user",
        role="employee",
        entity_key="eko",
    )


def _source_record(pk: str) -> SourceRecord:
    return SourceRecord(
        source_record_pk=pk,
        source_system="fundbox",
        source_record_id=pk,
        lifecycle_status="parked",
        link_status="profile",
        observed_at="2026-01-01T00:00:00Z",
        ingested_at="2026-01-01T00:00:00Z",
    )


def _profile_detail() -> ProfileDetail:
    return ProfileDetail(
        profile_id="profile-1",
        status="open",
        evidence_version=2,
        materialized_at="2026-01-01T00:00:00Z",
        record_count=1,
        identifier_count=1,
        source_records=[_source_record("sr-1")],
        identifiers=[
            PersonIdentifier(
                identifier_type="phone",
                normalized_value="+6591234567",
                is_verified=True,
                score=0.0,
                golden_rank=None,
            )
        ],
    )


@dataclass
class _ProfileRepo:
    page: tuple[list[Profile], int] = field(
        default_factory=lambda: (
            [
                Profile(
                    profile_id="profile-1",
                    status="open",
                    evidence_version=2,
                    materialized_at="2026-01-01T00:00:00Z",
                    record_count=1,
                    identifier_count=1,
                )
            ],
            1,
        )
    )
    detail: ProfileDetail | None = field(default_factory=_profile_detail)
    calls: list[tuple[ProfileListFilters, int, int]] = field(default_factory=list)

    async def get_page(
        self,
        filters: ProfileListFilters,
        skip: int,
        limit: int,
    ) -> tuple[list[Profile], int]:
        self.calls.append((filters, skip, limit))
        return self.page

    async def get_by_id(self, profile_id: str) -> ProfileDetail | None:
        if self.detail is None or self.detail.profile_id != profile_id:
            return None
        return self.detail


def _client(repo: _ProfileRepo | None = None) -> tuple[TestClient, _ProfileRepo]:
    resolved = repo or _ProfileRepo()
    app = build_frontend_app()
    app.dependency_overrides[require_active_user] = _active_user
    app.dependency_overrides[get_current_user_or_oauth_client] = _active_user
    app.dependency_overrides[get_profile_repo] = lambda: resolved
    return TestClient(app), resolved


def test_list_profiles_defaults_to_open_and_envelopes_the_page() -> None:
    client, repo = _client()

    response = client.get("/profiles")

    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["profile_id"] == "profile-1"
    assert body["meta"]["total_count"] == 1
    assert body["meta"]["next_cursor"] is None
    assert repo.calls == [(ProfileListFilters(q=None, status="open"), 0, 20)]


def test_list_profiles_forwards_q_status_and_cursor() -> None:
    client, repo = _client()

    response = client.get(
        "/profiles",
        params={"q": "  Ada  ", "status": "promoted", "cursor": "MjA=", "limit": 5},
    )

    assert response.status_code == 200
    assert repo.calls == [(ProfileListFilters(q="Ada", status="promoted"), 20, 5)]


def test_list_profiles_maps_explicit_all_status_to_no_filter() -> None:
    client, repo = _client()

    response = client.get("/profiles", params={"status": "all"})

    assert response.status_code == 200
    assert repo.calls == [(ProfileListFilters(q=None, status=None), 0, 20)]


def test_list_profiles_rejects_short_queries() -> None:
    client, repo = _client()

    response = client.get("/profiles", params={"q": "a"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert repo.calls == []


def test_get_profile_returns_detail_and_404s_when_missing() -> None:
    client, _repo = _client()

    found = client.get("/profiles/profile-1")
    missing = client.get("/profiles/profile-missing")

    assert found.status_code == 200
    detail = found.json()["data"]
    assert detail["status"] == "open"
    assert detail["source_records"][0]["lifecycle_status"] == "parked"
    assert detail["identifiers"][0]["identifier_type"] == "phone"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "profile_not_found"


def test_profiles_surface_requires_authentication() -> None:
    response = TestClient(build_frontend_app()).get("/profiles")

    assert response.status_code == 401


def test_profiles_operations_are_mounted_on_the_frontend_contract_only() -> None:
    frontend_paths = TestClient(build_app()).get("/app/v2/openapi.json").json()["paths"]
    assert frontend_paths["/profiles"]["get"]["operationId"] == "list_profiles"
    assert frontend_paths["/profiles/{profile_id}"]["get"]["operationId"] == "get_profile"

    oauth2_schema = TestClient(build_oauth2_app()).get("/openapi.json").json()
    oauth2_ids = {
        operation["operationId"]
        for path_item in oauth2_schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    assert "list_profiles" not in oauth2_ids
    assert "get_profile" not in oauth2_ids
