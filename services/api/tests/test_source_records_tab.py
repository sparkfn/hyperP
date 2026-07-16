"""Tests for the source-records tab endpoints: filters, facets, display fields."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.auth.deps import get_current_user_or_oauth_client, require_active_user
from src.auth.models import AuthUser
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
    PossibleMatchDetail,
    SourceRecord,
    SourceRecordEntityFacet,
)


class FakeSourceRepo:
    def __init__(self) -> None:
        self.last_entity_key: str | None = None
        self.last_record_type: str | None = None

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
        self,
        person_id: str,
        skip: int,
        limit: int,
        entity_key: str | None = None,
        record_type: str | None = None,
    ) -> tuple[list[SourceRecord], int]:
        self.last_entity_key = entity_key
        self.last_record_type = record_type
        record = SourceRecord(
            source_record_pk="pk1",
            source_system="eko_phppos",
            source_record_id="8841",
            record_type="conversation",
            lifecycle_status="active",
            extraction_confidence=0.82,
            link_status="linked",
            observed_at="2026-04-02T03:14:00Z",
            ingested_at="2026-04-02T03:14:00Z",
        )
        return [record], 1

    async def get_source_record_entity_facets(
        self, person_id: str
    ) -> list[SourceRecordEntityFacet]:
        return [
            SourceRecordEntityFacet(
                source_system="eko_phppos",
                entity_key="eko",
                entity_display_name="EKO Sports",
                count=2,
            )
        ]

    async def get_bankruptcy_cases(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[BankruptcyCase], int]:
        return [], 0

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

    async def get_shared_identifier_candidates(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[Person], int]:
        return [], 0

    async def get_possible_match_detail(
        self, person_id: str, candidate_person_id: str
    ) -> PossibleMatchDetail | None:
        return None

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


async def _source_user() -> AuthUser:
    return AuthUser(
        email="person@example.com",
        google_sub="employee-sub",
        role="employee",
        entity_key="fundbox",
        display_name="Person",
    )


@pytest.fixture
def source_app() -> tuple[FastAPI, FakeSourceRepo]:
    app = FastAPI()
    app.include_router(router)
    repo = FakeSourceRepo()
    app.dependency_overrides[get_person_repo] = lambda: repo
    app.dependency_overrides[require_active_user] = _source_user
    app.dependency_overrides[get_current_user_or_oauth_client] = _source_user
    return app, repo


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.anyio
async def test_source_records_returns_display_fields(
    source_app: tuple[FastAPI, FakeSourceRepo],
) -> None:
    app, _ = source_app
    async with _client(app) as client:
        response = await client.get("/v1/persons/p1/source-records")

    assert response.status_code == 200
    item = response.json()["data"][0]
    assert item["observed_at_display"] == "02 Apr 2026, 03:14 AM"
    assert item["ingested_at_display"] == "02 Apr 2026, 03:14 AM"
    assert item["extraction_confidence_display"] == "82%"
    assert item["lifecycle_status"] == "active"


def test_source_record_lifecycle_type_rejects_unknown_state() -> None:
    with pytest.raises(ValueError, match="lifecycle_status"):
        SourceRecord(
            source_record_pk="pk1",
            source_system="eko_phppos",
            source_record_id="8841",
            record_type="identity",
            lifecycle_status="latest",  # type: ignore[arg-type]
            link_status="linked",
            observed_at="2026-04-02T03:14:00Z",
            ingested_at="2026-04-02T03:14:00Z",
        )


@pytest.mark.anyio
async def test_source_records_forwards_filters(
    source_app: tuple[FastAPI, FakeSourceRepo],
) -> None:
    app, repo = source_app
    async with _client(app) as client:
        await client.get("/v1/persons/p1/source-records?entity_key=eko&record_type=identity")

    assert repo.last_entity_key == "eko"
    assert repo.last_record_type == "identity"


@pytest.mark.anyio
async def test_source_record_entities_facets(
    source_app: tuple[FastAPI, FakeSourceRepo],
) -> None:
    app, _ = source_app
    async with _client(app) as client:
        response = await client.get("/v1/persons/p1/source-record-entities")

    assert response.status_code == 200
    facets = response.json()["data"]
    assert facets[0]["entity_display_name"] == "EKO Sports"
    assert facets[0]["count"] == 2
