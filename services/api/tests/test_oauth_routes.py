"""FastAPI route tests for OAuth client credentials."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from src.app import build_app
from src.auth import deps as auth_deps
from src.auth.deps import (
    OAuthClientUser,
    get_current_user_or_oauth_client,
    require_active_user,
    require_human_admin,
    require_scope,
)
from src.auth.models import AuthUser
from src.auth.oauth_client_models import OAuthClient
from src.auth.oauth_tokens import OAuthClientClaims
from src.repositories.deps import (
    get_admin_repo,
    get_entity_repo,
    get_event_repo,
    get_ingest_repo,
    get_person_repo,
    get_report_repo,
    get_review_repo,
)
from src.repositories.protocols.admin import AdminRepository, SourceSystemInfo
from src.repositories.protocols.entity import EntityRepository
from src.repositories.protocols.event import EventRepository
from src.repositories.protocols.ingest import (
    IngestRecordsResponse,
    IngestRepository,
    IngestRunDetailResponse,
    IngestRunResponse,
)
from src.repositories.protocols.person import PersonListFilters, PersonRepository
from src.repositories.protocols.report import ReportRepository
from src.repositories.protocols.review import ReviewListFilters, ReviewRepository
from src.types import (
    AuditEvent,
    ConnectionType,
    DownstreamEvent,
    EntityPerson,
    EntitySummary,
    ListedPerson,
    MatchDecision,
    Person,
    PersonConnection,
    PersonEntitySummary,
    PersonGraph,
    PersonIdentifier,
    ReviewCaseDetail,
    ReviewCaseSummary,
    SourceRecord,
    TrustTier,
)
from src.types_reports import ReportDetail, ReportResult, ReportSummary
from src.types_requests import IngestRecord, IngestRunUpdateRequest


def _client(
    *,
    client_id: str = "hpc_test",
    entity_key: str | None = None,
    scopes: list[str] | None = None,
) -> OAuthClient:
    return OAuthClient(
        client_id=client_id,
        name="POS sync",
        entity_key=entity_key,
        scopes=scopes or ["persons:read"],
        created_by="admin@example.com",
        created_at=datetime.now(UTC).replace(tzinfo=None),
        disabled_at=None,
        last_used_at=None,
        secrets=[],
    )


def _admin_user() -> AuthUser:
    return AuthUser(
        email="admin@example.com",
        google_sub="admin-sub",
        role="admin",
        entity_key=None,
        display_name="Admin",
    )


def _employee_user() -> AuthUser:
    return AuthUser(
        email="person@example.com",
        google_sub="employee-sub",
        role="employee",
        entity_key="fundbox",
        display_name="Person",
    )


def _claims(
    *,
    client_id: str = "hpc_reader",
    scopes: list[str] | None = None,
    jti: str = "jti-1",
    entity_key: str | None = "fundbox",
) -> OAuthClientClaims:
    token_scopes = scopes or ["persons:read"]
    return OAuthClientClaims(
        iss="http://testserver/api",
        aud="hyperp-api-test",
        sub=client_id,
        client_id=client_id,
        scope=" ".join(token_scopes),
        scopes=token_scopes,
        iat=1,
        nbf=1,
        exp=9999999999,
        jti=jti,
        entity_key=entity_key,
    )


async def _resolve_oauth_principal(
    claims: OAuthClientClaims,
    client: OAuthClient | None,
    *,
    revoked: bool = False,
) -> AuthUser | OAuthClientUser:
    scope = {"type": "http", "headers": []}
    request = Request(scope)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="oauth.jwt")
    with (
        patch("src.auth.deps.verify_client_access_token", return_value=claims),
        patch("src.auth.deps.is_token_revoked", new=AsyncMock(return_value=revoked)),
        patch("src.auth.deps.get_oauth_client_by_id", new=AsyncMock(return_value=client)),
    ):
        return await get_current_user_or_oauth_client(request, credentials)


async def _assert_oauth_http_error(
    claims: OAuthClientClaims,
    client: OAuthClient | None,
    *,
    revoked: bool = False,
) -> HTTPException:
    scope = {"type": "http", "headers": []}
    request = Request(scope)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="oauth.jwt")
    with (
        patch("src.auth.deps.verify_client_access_token", return_value=claims),
        patch("src.auth.deps.is_token_revoked", new=AsyncMock(return_value=revoked)),
        patch("src.auth.deps.get_oauth_client_by_id", new=AsyncMock(return_value=client)),
        patch("src.auth.deps.get_current_user", new=AsyncMock()) as get_google,
        pytest.raises(HTTPException) as exc_info,
    ):
        await get_current_user_or_oauth_client(request, credentials)
    get_google.assert_not_awaited()
    return exc_info.value


async def _override_admin() -> AuthUser:
    return _admin_user()


async def _override_employee() -> AuthUser:
    return _employee_user()


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


async def _override_oauth_admin_client() -> OAuthClientUser:
    return OAuthClientUser(
        email="oauth:hpc_admin",
        google_sub="hpc_admin",
        role="admin",
        entity_key=None,
        display_name="Admin client",
        source="oauth_client",
        client_id="hpc_admin",
        key_scopes=["admin"],
    )


class _PersonRouteRepo:
    async def get_page(
        self, filters: PersonListFilters, skip: int, limit: int
    ) -> tuple[list[ListedPerson], int]:
        _ = filters, skip, limit
        return [], 0

    async def search_by_identifier(self, identifier_type: str, value: str) -> list[Person]:
        _ = identifier_type, value
        return []

    async def search_by_query(
        self, q: str, status: str | None, skip: int, limit: int
    ) -> tuple[list[Person], bool]:
        _ = q, status, skip, limit
        return [], False

    async def get_by_id(self, person_id: str) -> Person | None:
        _ = person_id
        return Person(person_id="p1", status="active")

    async def get_source_records(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[SourceRecord], int]:
        _ = person_id, skip, limit
        return [], 0

    async def get_identifiers(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonIdentifier], int]:
        _ = person_id, skip, limit
        return [], 0

    async def get_connections(
        self,
        person_id: str,
        connection_type: ConnectionType,
        identifier_type: str | None,
        skip: int,
        limit: int,
    ) -> tuple[list[PersonConnection], int]:
        _ = person_id, connection_type, identifier_type, skip, limit
        return [], 0

    async def get_entities(self, person_id: str) -> list[PersonEntitySummary]:
        _ = person_id
        return []

    async def get_graph(self, person_id: str, max_hops: int) -> PersonGraph | None:
        _ = person_id, max_hops
        return PersonGraph()

    async def get_node_graph(self, element_id: str, max_hops: int) -> PersonGraph | None:
        _ = element_id, max_hops
        return PersonGraph()

    async def get_audit(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[AuditEvent], int]:
        _ = person_id, skip, limit
        return [], 0

    async def get_matches(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[MatchDecision], bool]:
        _ = person_id, skip, limit
        return [], False


class _ReportRouteRepo:
    async def get_all(self) -> list[ReportSummary]:
        return []

    async def get_by_key(self, report_key: str) -> ReportDetail | None:
        _ = report_key
        return ReportDetail(
            report_key="entity_person_summary",
            display_name="Entity Person Summary",
            cypher_query="RETURN 1",
        )

    async def create(
        self,
        report_key: str,
        display_name: str,
        description: str | None,
        category: str | None,
        cypher_query: str,
        parameters_json: str,
    ) -> None:
        _ = report_key, display_name, description, category, cypher_query, parameters_json

    async def update(
        self,
        report_key: str,
        display_name: str,
        description: str | None,
        category: str | None,
        cypher_query: str,
        parameters_json: str,
    ) -> None:
        _ = report_key, display_name, description, category, cypher_query, parameters_json

    async def delete(self, report_key: str) -> int:
        _ = report_key
        return 1

    async def execute(
        self,
        query: str,
        params: dict[str, str | int | float | bool | None],
    ) -> ReportResult:
        _ = query, params
        return ReportResult(columns=[], rows=[], row_count=0)

    async def seed(self) -> list[str]:
        return []


class _EntityRouteRepo:
    def __init__(self) -> None:
        self.get_all_calls = 0

    async def get_all(self) -> list[EntitySummary]:
        self.get_all_calls += 1
        return [EntitySummary(entity_key="fundbox", display_name="Fundbox")]

    async def list_persons(
        self,
        entity_key: str,
        skip: int,
        limit: int,
        sort_by: str,
        sort_order: str,
    ) -> tuple[list[EntityPerson], bool]:
        _ = entity_key, skip, limit, sort_by, sort_order
        return [], False


class _EventRouteRepo:
    def __init__(self) -> None:
        self.get_page_calls = 0

    async def get_page(
        self,
        since: str,
        event_type: str | None,
        skip: int,
        limit: int,
    ) -> tuple[list[DownstreamEvent], bool]:
        _ = since, event_type, skip, limit
        self.get_page_calls += 1
        return [], False


class _AdminRouteRepo:
    def __init__(self) -> None:
        self.get_all_source_systems_calls = 0

    async def get_all_source_systems(self) -> list[SourceSystemInfo]:
        self.get_all_source_systems_calls += 1
        return [
            SourceSystemInfo(
                source_key="fundbox_pos",
                is_active=True,
                field_trust={"email": "tier_1"},
                display_name="Fundbox POS",
            )
        ]

    async def get_field_trust(self, source_key: str) -> None:
        _ = source_key

    async def update_field_trust(self, source_key: str, updates: dict[str, TrustTier]) -> None:
        _ = source_key, updates


class _IngestRouteRepo:
    async def ingest_records(
        self,
        source_key: str,
        ingest_type: str,
        ingest_run_id: str | None,
        records: list[IngestRecord],
    ) -> IngestRecordsResponse | None:
        _ = source_key, ingest_type, ingest_run_id, records
        return IngestRecordsResponse(ingest_run_id="run-1", accepted_count=1, rejected_count=0)

    async def create_run(
        self,
        source_key: str,
        run_type: str,
        metadata: dict[str, str],
    ) -> IngestRunResponse | None:
        _ = source_key, run_type, metadata
        return IngestRunResponse(ingest_run_id="run-1", status="running")

    async def update_run(
        self,
        source_key: str,
        ingest_run_id: str,
        body: IngestRunUpdateRequest,
    ) -> IngestRunResponse | None:
        _ = source_key, ingest_run_id, body
        return IngestRunResponse(ingest_run_id="run-1", status="completed")

    async def get_run(self, ingest_run_id: str) -> IngestRunDetailResponse | None:
        _ = ingest_run_id
        return IngestRunDetailResponse(
            ingest_run_id="run-1",
            run_type="batch",
            status="running",
            record_count=0,
            rejected_count=0,
            started_at=None,
            finished_at=None,
            source_key="fundbox_pos",
        )


_EVENT_REPO = _EventRouteRepo()
_ADMIN_REPO = _AdminRouteRepo()


async def _override_event_repo() -> EventRepository:
    return _EVENT_REPO


async def _override_admin_repo() -> AdminRepository:
    return _ADMIN_REPO


async def _override_person_repo() -> PersonRepository:
    return _PersonRouteRepo()


async def _override_report_repo() -> ReportRepository:
    return _ReportRouteRepo()


class _ReviewRouteRepo:
    def __init__(self) -> None:
        self.get_page_calls = 0
        self.get_by_id_calls = 0

    async def get_page(
        self, filters: ReviewListFilters, skip: int, limit: int
    ) -> tuple[list[ReviewCaseSummary], bool]:
        _ = filters, skip, limit
        self.get_page_calls += 1
        return [], False

    async def get_by_id(self, review_case_id: str) -> ReviewCaseDetail | None:
        _ = review_case_id
        self.get_by_id_calls += 1
        return None

    async def assign(self, review_case_id: str, assigned_to: str) -> dict[str, str] | None:
        _ = review_case_id, assigned_to
        return None

    async def submit_action(
        self,
        review_case_id: str,
        action_type: str,
        new_state: str,
        resolution: str | None,
        notes: str | None,
        follow_up_at: str | None,
        actor_id: str,
        survivor_person_id: str | None,
    ) -> dict[str, str | bool | None] | None:
        _ = (
            review_case_id,
            action_type,
            new_state,
            resolution,
            notes,
            follow_up_at,
            actor_id,
            survivor_person_id,
        )
        return None


_REVIEW_REPO = _ReviewRouteRepo()


async def _override_review_repo() -> ReviewRepository:
    return _REVIEW_REPO


_ENTITY_REPO = _EntityRouteRepo()


async def _override_entity_repo() -> EntityRepository:
    return _ENTITY_REPO


async def _override_ingest_repo() -> IngestRepository:
    return _IngestRouteRepo()


def test_token_endpoint_rejects_unsupported_grant_type() -> None:
    app = build_app()
    client = TestClient(app)

    res = client.post(
        "/v1/oauth/token",
        data={"grant_type": "password", "client_id": "hpc", "client_secret": "secret"},
    )

    assert res.status_code == 400
    assert res.json()["error"] == "unsupported_grant_type"


def test_token_endpoint_returns_oauth_error_for_bad_credentials() -> None:
    app = build_app()
    client = TestClient(app)

    with patch("src.routes.oauth.validate_client_credentials", new=AsyncMock(return_value=None)):
        res = client.post(
            "/v1/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "hpc_missing",
                "client_secret": "bad",
            },
        )

    assert res.status_code == 401
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"
    assert res.json()["error"] == "invalid_client"


def test_token_endpoint_returns_oauth_error_for_missing_client_secret() -> None:
    app = build_app()
    client = TestClient(app)

    res = client.post(
        "/v1/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "hpc_test",
        },
    )

    assert res.status_code == 400
    body = res.json()
    assert body["error"] == "invalid_request"
    assert "client_secret" in body["error_description"]
    assert "meta" not in body


def test_token_endpoint_issues_access_token() -> None:
    app = build_app()
    client = TestClient(app)

    with (
        patch(
            "src.routes.oauth.validate_client_credentials",
            new=AsyncMock(return_value=(_client(), ["persons:read"])),
        ),
        patch("src.routes.oauth.issue_client_access_token", return_value="jwt-token"),
    ):
        res = client.post(
            "/v1/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "hpc_test",
                "client_secret": "hps_secret",
                "scope": "persons:read",
            },
        )

    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"
    assert res.json() == {
        "access_token": "jwt-token",
        "token_type": "Bearer",
        "expires_in": 900,
        "scope": "persons:read",
    }


def test_token_endpoint_rejects_invalid_requested_scope() -> None:
    app = build_app()
    client = TestClient(app)

    with patch(
        "src.routes.oauth.validate_client_credentials",
        new=AsyncMock(return_value=(_client(), ["persons:read"])),
    ):
        res = client.post(
            "/v1/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "hpc_test",
                "client_secret": "hps_secret",
                "scope": "persons:write",
            },
        )

    assert res.status_code == 400
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"
    assert res.json()["error"] == "invalid_scope"


def test_jwks_endpoint_returns_keys() -> None:
    app = build_app()
    client = TestClient(app)

    with patch("src.routes.oauth.build_jwks", return_value={"keys": []}):
        res = client.get("/v1/oauth/jwks")

    assert res.status_code == 200
    assert res.json() == {"keys": []}


def test_events_route_rejects_oauth_client_without_persons_read_scope_before_repo_call() -> None:
    app = build_app()
    app.dependency_overrides[require_active_user] = _override_oauth_ingest_writer
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_ingest_writer
    app.dependency_overrides[get_event_repo] = _override_event_repo
    _EVENT_REPO.get_page_calls = 0
    client = TestClient(app)

    res = client.get("/v1/events?since=2026-01-01T00:00:00Z")

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "OAuth client lacks required scope: persons:read"
    assert _EVENT_REPO.get_page_calls == 0


def test_source_systems_route_rejects_oauth_client_without_admin_scope_before_repo_call() -> None:
    app = build_app()
    app.dependency_overrides[require_active_user] = _override_oauth_persons_reader
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_persons_reader
    app.dependency_overrides[get_admin_repo] = _override_admin_repo
    _ADMIN_REPO.get_all_source_systems_calls = 0
    client = TestClient(app)

    res = client.get("/v1/source-systems")

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "This action requires administrator privileges."
    assert _ADMIN_REPO.get_all_source_systems_calls == 0


def test_source_systems_route_allows_oauth_admin_client_with_mocked_repo() -> None:
    app = build_app()
    app.dependency_overrides[require_active_user] = _override_oauth_admin_client
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_admin_client
    app.dependency_overrides[get_admin_repo] = _override_admin_repo
    _ADMIN_REPO.get_all_source_systems_calls = 0
    client = TestClient(app)

    res = client.get("/v1/source-systems")

    assert res.status_code == 200
    assert res.json()["data"] == [
        {
            "source_key": "fundbox_pos",
            "is_active": True,
            "field_trust": {"email": "tier_1"},
            "source_system_id": None,
            "display_name": "Fundbox POS",
            "system_type": None,
            "entity_key": None,
            "created_at": None,
            "updated_at": None,
        }
    ]
    assert _ADMIN_REPO.get_all_source_systems_calls == 1


    app = build_app()
    app.dependency_overrides[require_active_user] = _override_oauth_ingest_writer
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_ingest_writer
    app.dependency_overrides[get_person_repo] = _override_person_repo
    client = TestClient(app)

    res = client.get("/v1/persons/p1")

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "OAuth client lacks required scope: persons:read"


def test_entities_route_allows_oauth_client_with_persons_read_scope() -> None:
    app = build_app()
    app.dependency_overrides[require_active_user] = _override_oauth_persons_reader
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_persons_reader
    app.dependency_overrides[get_entity_repo] = _override_entity_repo
    _ENTITY_REPO.get_all_calls = 0
    client = TestClient(app)

    res = client.get("/v1/entities")

    assert res.status_code == 200
    assert res.json()["data"] == [
        {
            "entity_key": "fundbox",
            "display_name": "Fundbox",
            "entity_type": None,
            "country_code": None,
            "is_active": True,
            "person_count": 0,
            "source_record_count": 0,
            "last_ingested_at": None,
            "active_review_cases": 0,
        }
    ]
    assert _ENTITY_REPO.get_all_calls == 1

def test_persons_read_route_allows_human_user_with_mocked_repo() -> None:
    app = build_app()
    app.dependency_overrides[require_active_user] = _override_employee
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_employee
    app.dependency_overrides[get_person_repo] = _override_person_repo
    client = TestClient(app)

    res = client.get("/v1/persons/p1")

    assert res.status_code == 200
    assert res.json()["data"]["person_id"] == "p1"


def test_entities_route_rejects_oauth_client_without_persons_read_scope_before_repo_call() -> None:
    app = build_app()
    app.dependency_overrides[require_active_user] = _override_oauth_ingest_writer
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_ingest_writer
    app.dependency_overrides[get_entity_repo] = _override_entity_repo
    _ENTITY_REPO.get_all_calls = 0
    client = TestClient(app)

    res = client.get("/v1/entities")

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "OAuth client lacks required scope: persons:read"
    assert _ENTITY_REPO.get_all_calls == 0


    app = build_app()
    app.dependency_overrides[require_active_user] = _override_oauth_persons_reader
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_persons_reader
    app.dependency_overrides[get_ingest_repo] = _override_ingest_repo
    client = TestClient(app)

    with patch("src.auth.deps.get_entity_for_source", new=AsyncMock(return_value="fundbox")):
        res = client.post("/v1/ingest/fundbox_pos/runs", json={"run_type": "batch"})

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "OAuth client lacks required scope: ingest:write"


def test_ingest_write_route_allows_human_user_with_mocked_repo() -> None:
    app = build_app()
    app.dependency_overrides[require_active_user] = _override_employee
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_employee
    app.dependency_overrides[get_ingest_repo] = _override_ingest_repo
    client = TestClient(app)

    with patch("src.auth.deps.get_entity_for_source", new=AsyncMock(return_value="fundbox")):
        res = client.post("/v1/ingest/fundbox_pos/runs", json={"run_type": "batch"})

    assert res.status_code == 201
    assert res.json()["data"]["ingest_run_id"] == "run-1"


def test_ingest_run_detail_route_rejects_oauth_client_without_ingest_write_scope() -> None:
    app = build_app()
    app.dependency_overrides[require_active_user] = _override_oauth_persons_reader
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_persons_reader
    app.dependency_overrides[get_ingest_repo] = _override_ingest_repo
    client = TestClient(app)

    res = client.get("/v1/ingest/runs/run-1")

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "OAuth client lacks required scope: ingest:write"


    app = build_app()
    app.dependency_overrides[require_active_user] = _override_oauth_ingest_writer
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_ingest_writer
    app.dependency_overrides[get_report_repo] = _override_report_repo
    client = TestClient(app)

    res = client.post("/v1/reports/entity_person_summary/execute", json={})

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "OAuth client lacks required scope: persons:read"


def test_reports_metadata_route_rejects_oauth_client_without_persons_read_scope() -> None:
    app = build_app()
    app.dependency_overrides[require_active_user] = _override_oauth_ingest_writer
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_ingest_writer
    app.dependency_overrides[get_report_repo] = _override_report_repo
    client = TestClient(app)

    res = client.get("/v1/reports")

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "OAuth client lacks required scope: persons:read"


    app = build_app()
    app.dependency_overrides[require_active_user] = _override_oauth_persons_reader
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_persons_reader
    app.dependency_overrides[get_review_repo] = _override_review_repo
    _REVIEW_REPO.get_page_calls = 0
    client = TestClient(app)

    res = client.get("/v1/review-cases")

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "OAuth clients cannot access human workflow routes."
    assert _REVIEW_REPO.get_page_calls == 0


def test_public_link_route_rejects_oauth_client_before_repo_call() -> None:
    app = build_app()
    app.dependency_overrides[require_active_user] = _override_oauth_persons_reader
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_persons_reader
    app.dependency_overrides[get_person_repo] = _override_person_repo
    client = TestClient(app)

    with patch.object(_PersonRouteRepo, "get_by_id", new_callable=AsyncMock) as get_by_id:
        res = client.post("/v1/persons/p1/public-link")

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "OAuth clients cannot access human workflow routes."
    get_by_id.assert_not_awaited()


def test_oauth_client_management_rejects_admin_scoped_oauth_client() -> None:
    app = build_app()
    app.dependency_overrides[require_active_user] = _override_oauth_admin_client
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_oauth_admin_client
    client = TestClient(app)

    res = client.get("/v1/admin/oauth-clients")

    assert res.status_code == 403
    assert res.json()["error"]["message"] == "OAuth clients cannot access human workflow routes."


def test_oauth_client_secret_route_returns_not_found_for_human_admin() -> None:
    app = build_app()
    app.dependency_overrides[require_active_user] = _override_admin
    app.dependency_overrides[require_human_admin] = _override_admin
    client = TestClient(app)

    with patch(
        "src.routes.oauth_clients.create_oauth_client_secret",
        new=AsyncMock(return_value=None),
    ):
        res = client.post(
            "/v1/admin/oauth-clients/hpc_missing/secrets",
            json={"expires_in_days": 30},
        )

    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"


def test_oauth_client_user_admin_scope_sets_admin_role() -> None:
    principal = OAuthClientUser(
        email="oauth:hpc_admin",
        google_sub="hpc_admin",
        role="admin",
        entity_key=None,
        display_name="Admin client",
        source="oauth_client",
        client_id="hpc_admin",
        key_scopes=["admin"],
    )

    assert principal.role == "admin"
    assert principal.key_scopes == ["admin"]


def test_oauth_client_user_non_admin_scope_is_employee_role() -> None:
    principal = OAuthClientUser(
        email="oauth:hpc_reader",
        google_sub="hpc_reader",
        role="employee",
        entity_key="fundbox",
        display_name="Reader client",
        source="oauth_client",
        client_id="hpc_reader",
        key_scopes=["persons:read"],
    )

    assert principal.role == "employee"
    assert principal.entity_key == "fundbox"


@pytest.mark.asyncio
async def test_require_scope_allows_human_auth_user() -> None:
    scope = {"type": "http", "headers": []}
    request = Request(scope)
    human_user = _employee_user()
    dependency = require_scope("persons:read")

    principal = await dependency(request, human_user)

    assert principal is human_user


@pytest.mark.asyncio
async def test_require_scope_accepts_oauth_client_with_matching_scope() -> None:
    scope = {"type": "http", "headers": []}
    request = Request(scope)
    dependency = require_scope("persons:read")
    principal = OAuthClientUser(
        email="oauth:hpc_reader",
        google_sub="hpc_reader",
        role="employee",
        entity_key="fundbox",
        display_name="Reader client",
        source="oauth_client",
        client_id="hpc_reader",
        key_scopes=["persons:read"],
    )

    accepted = await dependency(request, principal)

    assert accepted is principal


@pytest.mark.asyncio
async def test_require_scope_rejects_oauth_client_without_scope() -> None:
    scope = {"type": "http", "headers": []}
    request = Request(scope)
    dependency = require_scope("persons:read")
    principal = OAuthClientUser(
        email="oauth:hpc_writer",
        google_sub="hpc_writer",
        role="employee",
        entity_key="fundbox",
        display_name="Writer client",
        source="oauth_client",
        client_id="hpc_writer",
        key_scopes=["ingest:write"],
    )

    with pytest.raises(HTTPException) as exc_info:
        await dependency(request, principal)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_human_user_rejects_oauth_client() -> None:
    scope = {"type": "http", "headers": []}
    request = Request(scope)
    principal = OAuthClientUser(
        email="oauth:hpc_reader",
        google_sub="hpc_reader",
        role="employee",
        entity_key="fundbox",
        display_name="Reader client",
        source="oauth_client",
        client_id="hpc_reader",
        key_scopes=["persons:read"],
    )

    with pytest.raises(HTTPException) as exc_info:
        await auth_deps.require_human_user(request, principal)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"]["message"] == (
        "OAuth clients cannot access human workflow routes."
    )


@pytest.mark.asyncio
async def test_require_human_user_accepts_human_auth_user() -> None:
    scope = {"type": "http", "headers": []}
    request = Request(scope)
    human_user = _employee_user()

    principal = await auth_deps.require_human_user(request, human_user)

    assert principal is human_user


@pytest.mark.asyncio
async def test_get_current_user_or_oauth_client_returns_oauth_client_user() -> None:
    scope = {"type": "http", "headers": []}
    request = Request(scope)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="oauth.jwt")
    claims = OAuthClientClaims(
        iss="http://testserver/api",
        aud="hyperp-api-test",
        sub="hpc_reader",
        client_id="hpc_reader",
        scope="persons:read",
        scopes=["persons:read"],
        iat=1,
        nbf=1,
        exp=9999999999,
        jti="jti-1",
        entity_key="fundbox",
    )

    with (
        patch("src.auth.deps.verify_client_access_token", return_value=claims),
        patch("src.auth.deps.is_token_revoked", new=AsyncMock(return_value=False)),
        patch(
            "src.auth.deps.get_oauth_client_by_id",
            new=AsyncMock(return_value=_client(client_id="hpc_reader", entity_key="fundbox")),
        ),
    ):
        principal = await get_current_user_or_oauth_client(request, credentials)

    assert isinstance(principal, OAuthClientUser)
    assert principal.client_id == "hpc_reader"
    assert principal.role == "employee"
    assert principal.entity_key == "fundbox"
    assert principal.key_scopes == ["persons:read"]


@pytest.mark.asyncio
async def test_revoked_oauth_token_jti_rejected_without_google_fallback() -> None:
    exc = await _assert_oauth_http_error(
        _claims(client_id="hpc_reader", jti="jti-revoked"),
        _client(client_id="hpc_reader", entity_key="fundbox"),
        revoked=True,
    )

    assert exc.status_code == 401
    assert exc.detail["error"]["code"] == "token_revoked"


@pytest.mark.asyncio
async def test_oauth_token_scope_no_longer_assigned_to_client_is_rejected() -> None:
    exc = await _assert_oauth_http_error(
        _claims(client_id="hpc_reader", scopes=["persons:read"]),
        _client(client_id="hpc_reader", entity_key="fundbox", scopes=["ingest:write"]),
    )

    assert exc.status_code == 403
    assert exc.detail["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_old_admin_oauth_token_rejected_after_client_loses_admin_scope() -> None:
    exc = await _assert_oauth_http_error(
        _claims(client_id="hpc_admin", scopes=["admin"]),
        _client(client_id="hpc_admin", scopes=["persons:read"]),
    )

    assert exc.status_code == 403
    assert exc.detail["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_oauth_token_entity_key_mismatch_is_rejected() -> None:
    exc = await _assert_oauth_http_error(
        _claims(client_id="hpc_reader", entity_key="fundbox"),
        _client(client_id="hpc_reader", entity_key="otherbox"),
    )

    assert exc.status_code == 403
    assert exc.detail["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_oauth_token_accepted_with_current_assigned_scope_and_entity() -> None:
    principal = await _resolve_oauth_principal(
        _claims(client_id="hpc_reader", scopes=["persons:read"], entity_key="fundbox"),
        _client(
            client_id="hpc_reader",
            entity_key="fundbox",
            scopes=["persons:read", "ingest:write"],
        ),
    )

    assert isinstance(principal, OAuthClientUser)
    assert principal.client_id == "hpc_reader"
    assert principal.role == "employee"
    assert principal.entity_key == "fundbox"
    assert principal.key_scopes == ["persons:read"]


@pytest.mark.asyncio
async def test_oauth_token_allows_known_scope_when_current_client_has_admin() -> None:
    principal = await _resolve_oauth_principal(
        _claims(client_id="hpc_admin", scopes=["persons:read"], entity_key=None),
        _client(client_id="hpc_admin", scopes=["admin"]),
    )

    assert isinstance(principal, OAuthClientUser)
    assert principal.client_id == "hpc_admin"
    assert principal.role == "admin"
    assert principal.entity_key is None
    assert principal.key_scopes == ["admin"]


@pytest.mark.asyncio
async def test_oauth_token_missing_client_is_rejected_without_google_fallback() -> None:
    scope = {"type": "http", "headers": []}
    request = Request(scope)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="oauth.jwt")
    claims = OAuthClientClaims(
        iss="http://testserver/api",
        aud="hyperp-api-test",
        sub="hpc_missing",
        client_id="hpc_missing",
        scope="persons:read",
        scopes=["persons:read"],
        iat=1,
        nbf=1,
        exp=9999999999,
        jti="jti-missing",
        entity_key="fundbox",
    )

    with (
        patch("src.auth.deps.verify_client_access_token", return_value=claims),
        patch("src.auth.deps.is_token_revoked", new=AsyncMock(return_value=False)),
        patch("src.auth.deps.get_oauth_client_by_id", new=AsyncMock(return_value=None)),
        patch("src.auth.deps.get_current_user", new=AsyncMock()) as get_google,
        pytest.raises(HTTPException) as exc_info,
    ):
        await get_current_user_or_oauth_client(request, credentials)

    get_google.assert_not_awaited()
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_oauth_token_disabled_client_is_rejected_without_google_fallback() -> None:
    scope = {"type": "http", "headers": []}
    request = Request(scope)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="oauth.jwt")
    claims = OAuthClientClaims(
        iss="http://testserver/api",
        aud="hyperp-api-test",
        sub="hpc_reader",
        client_id="hpc_reader",
        scope="persons:read",
        scopes=["persons:read"],
        iat=1,
        nbf=1,
        exp=9999999999,
        jti="jti-disabled",
        entity_key="fundbox",
    )
    disabled_client = _client().model_copy(
        update={"disabled_at": datetime.now(UTC).replace(tzinfo=None)}
    )

    with (
        patch("src.auth.deps.verify_client_access_token", return_value=claims),
        patch("src.auth.deps.is_token_revoked", new=AsyncMock(return_value=False)),
        patch(
            "src.auth.deps.get_oauth_client_by_id",
            new=AsyncMock(return_value=disabled_client),
        ),
        patch("src.auth.deps.get_current_user", new=AsyncMock()) as get_google,
        pytest.raises(HTTPException) as exc_info,
    ):
        await get_current_user_or_oauth_client(request, credentials)

    get_google.assert_not_awaited()
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_invalid_oauth_token_falls_back_to_google_auth_path() -> None:
    scope = {"type": "http", "headers": []}
    request = Request(scope)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="google.jwt")
    google_user = AuthUser(
        email="person@example.com",
        google_sub="google-sub",
        role="employee",
        entity_key="fundbox",
        display_name="Person",
    )

    with (
        patch("src.auth.deps.verify_client_access_token", side_effect=ValueError("not hyperp")),
        patch(
            "src.auth.deps.get_current_user",
            new=AsyncMock(return_value=google_user),
        ) as get_google,
    ):
        principal = await get_current_user_or_oauth_client(request, credentials)

    get_google.assert_awaited_once_with(request, credentials)
    assert principal is google_user
