from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.auth.deps import get_current_user_or_oauth_client, require_active_user
from src.auth.models import AuthUser
from src.graph.mappers import map_bankruptcy_case
from src.graph.mappers_entities import map_listed_person
from src.repositories.deps import get_person_repo
from src.routes.persons import router
from src.types import (
    AuditEvent,
    BankruptcyCase,
    ConnectionType,
    ListedPerson,
    MatchDecision,
    Person,
    PersonConnection,
    PersonEntitySummary,
    PersonGraph,
    PersonIdentifier,
    PersonTimelineGroup,
    SourceRecord,
)


def test_map_bankruptcy_case_maps_relevant_fields() -> None:
    case = map_bankruptcy_case(
        {
            "bankruptcy_case": {
                "bankruptcy_case_id": "bc-1",
                "source_system_key": "sgbankruptcy",
                "source_case_id": "case-123",
                "case_number": "B 123/2026",
                "document_type": "Bankruptcy Order",
                "document_date": "2026-04-01",
                "event_type": "order_made",
                "event_date": "2026-04-02",
                "trustee_name": "Jane Trustee",
                "trustee_firm": "Trustee LLP",
                "source_url": "https://example.test/case",
                "first_seen_at": "2026-04-03T00:00:00Z",
                "last_seen_at": "2026-04-04T00:00:00Z",
                "created_at": "2026-04-03T00:00:00Z",
                "updated_at": "2026-04-04T00:00:00Z",
            }
        }
    )

    assert isinstance(case, BankruptcyCase)
    assert case.bankruptcy_case_id == "bc-1"
    assert case.source_system_key == "sgbankruptcy"
    assert case.case_number == "B 123/2026"
    assert case.event_type == "order_made"
    assert case.trustee_name == "Jane Trustee"


def test_map_listed_person_includes_bankruptcy_case_count() -> None:
    person = map_listed_person(
        {
            "person": {
                "person_id": "person-1",
                "status": "active",
                "is_high_value": False,
                "is_high_risk": True,
                "preferred_full_name": "Ana Tan",
                "preferred_phone": None,
                "preferred_email": None,
                "preferred_dob": None,
                "preferred_nric": None,
                "profile_completeness_score": 0.2,
                "golden_profile_computed_at": None,
                "golden_profile_version": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
            },
            "preferred_address": None,
            "source_record_count": 1,
            "connection_count": 0,
            "phone_confidence": None,
            "entities": [],
            "entity_count": 0,
            "identifier_count": 0,
            "order_count": 0,
            "bankruptcy_case_count": 2,
        }
    )

    assert isinstance(person, ListedPerson)
    assert person.bankruptcy_case_count == 2


class FakeBankruptcyRepo:
    async def get_page(
        self, filters: dict[str, object], skip: int, limit: int
    ) -> tuple[list[ListedPerson], int]:
        return [], 0

    async def search_by_identifier(self, identifier_type: str, value: str) -> list[Person]:
        return []

    async def search_by_query(
        self, q: str, status: str | None, skip: int, limit: int
    ) -> tuple[list[Person], bool]:
        return [], False

    async def get_by_id(self, person_id: str) -> Person | None:
        return None

    async def get_source_records(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[SourceRecord], int]:
        return [], 0

    async def get_bankruptcy_cases(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[BankruptcyCase], int]:
        return [
            BankruptcyCase(
                bankruptcy_case_id="bc-1",
                source_system_key="sgbankruptcy",
                source_case_id="case-123",
                case_number="B 123/2026",
                event_type="order_made",
            )
        ], 1

    async def get_identifiers(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonIdentifier], int]:
        return [], 0

    async def get_connections(
        self,
        person_id: str,
        connection_type: ConnectionType,
        identifier_type: str | None,
        skip: int,
        limit: int,
    ) -> tuple[list[PersonConnection], int]:
        return [], 0

    async def get_entities(self, person_id: str) -> list[PersonEntitySummary]:
        return []

    async def get_graph(self, person_id: str, max_hops: int) -> PersonGraph | None:
        return PersonGraph()

    async def get_node_graph(self, element_id: str, max_hops: int) -> PersonGraph | None:
        return PersonGraph()

    async def get_audit(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[AuditEvent], int]:
        return [], 0

    async def get_matches(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[MatchDecision], int]:
        return [], 0

    async def get_timeline(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonTimelineGroup], int]:
        return [], 0

    async def get_timeline_target(
        self, person_id: str, source_record_pk: str
    ) -> PersonTimelineGroup | None:
        return None


async def _bankruptcy_user() -> AuthUser:
    return AuthUser(
        email="person@example.com",
        google_sub="employee-sub",
        role="employee",
        entity_key="fundbox",
        display_name="Person",
    )


@pytest.fixture
def bankruptcy_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_person_repo] = lambda: FakeBankruptcyRepo()
    app.dependency_overrides[require_active_user] = _bankruptcy_user
    app.dependency_overrides[get_current_user_or_oauth_client] = _bankruptcy_user
    return app


@asynccontextmanager
async def bankruptcy_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.anyio
async def test_get_person_bankruptcy_cases_returns_envelope(bankruptcy_app: FastAPI) -> None:
    async with bankruptcy_client(bankruptcy_app) as client:
        response = await client.get("/v1/persons/person-1/bankruptcy-cases?limit=1")

    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["bankruptcy_case_id"] == "bc-1"
    assert body["data"][0]["case_number"] == "B 123/2026"
    assert body["meta"]["total_count"] == 1
