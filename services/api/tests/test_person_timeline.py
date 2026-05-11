from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic.types import JsonValue
from src.auth.deps import get_current_user_or_oauth_client, require_active_user
from src.auth.models import AuthUser
from src.graph.mappers import map_timeline_group
from src.repositories.deps import get_person_repo
from src.routes.persons import router
from src.types import (
    AuditEvent,
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


def _record(
    *,
    observed_at: str | None,
    ingested_at: str,
    payload: dict[str, JsonValue] | None,
) -> dict[str, object]:
    return {
        "source_record": {
            "source_record_pk": "sr-1",
            "source_record_id": "external-1",
            "source_record_version": "v1",
            "record_type": "system",
            "extraction_confidence": None,
            "link_status": "linked",
            "observed_at": observed_at,
            "ingested_at": ingested_at,
            "normalized_payload": payload,
        },
        "source_system": "pos",
        "linked_person_id": "person-1",
    }


def test_map_timeline_group_uses_observed_at_as_source_timestamp() -> None:
    group = map_timeline_group(
        _record(
            observed_at="2026-04-28T14:32:00Z",
            ingested_at="2026-05-01T01:00:00Z",
            payload={
                "identifiers": [{"identifier_type": "phone", "normalized_value": "+6591234567"}],
                "address": {"normalized_full": "10 Orchard Road"},
                "attributes": [{"attribute_name": "full_name", "attribute_value": "Ana Tan"}],
                "summary": "Customer asked about renewal.",
            },
        )
    )

    assert isinstance(group, PersonTimelineGroup)
    assert group.source_record_pk == "sr-1"
    assert group.occurred_at == "2026-04-28T14:32:00Z"
    assert group.timestamp_kind == "source"
    assert [(fact.category, fact.label, fact.value) for fact in group.facts] == [
        ("source", "Summary", "Customer asked about renewal."),
        ("identity", "Full name", "Ana Tan"),
        ("contact", "Phone", "+6591234567"),
        ("address", "Address", "10 Orchard Road"),
    ]


def test_map_timeline_group_labels_ingested_at_as_fallback_timestamp() -> None:
    group = map_timeline_group(
        _record(
            observed_at=None,
            ingested_at="2026-05-01T01:00:00Z",
            payload={
                "identifiers": [
                    {"identifier_type": "email", "normalized_value": "ana@example.com"}
                ]
            },
        )
    )

    assert group.occurred_at == "2026-05-01T01:00:00Z"
    assert group.timestamp_kind == "fallback"
    assert group.facts[0].category == "contact"
    assert group.facts[0].label == "Email"


class FakeTimelineRepo:
    async def get_page(
        self, filters: dict[str, object], skip: int, limit: int
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
        return None

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
        return None

    async def get_node_graph(self, element_id: str, max_hops: int) -> PersonGraph | None:
        _ = element_id, max_hops
        return None

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

    async def get_timeline(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonTimelineGroup], int]:
        _ = skip, limit
        return [
            PersonTimelineGroup(
                source_record_pk="sr-new",
                source_system="pos",
                source_record_id="external-new",
                record_type="system",
                link_status="linked",
                linked_person_id=person_id,
                occurred_at="2026-04-30T09:10:00Z",
                timestamp_kind="source",
                ingested_at="2026-05-01T01:00:00Z",
                facts=[],
            )
        ], 2

    async def get_timeline_target(
        self, person_id: str, source_record_pk: str
    ) -> PersonTimelineGroup | None:
        if source_record_pk != "sr-new":
            return None
        return PersonTimelineGroup(
            source_record_pk="sr-new",
            source_system="pos",
            source_record_id="external-new",
            record_type="system",
            link_status="linked",
            linked_person_id=person_id,
            occurred_at="2026-04-30T09:10:00Z",
            timestamp_kind="source",
            ingested_at="2026-05-01T01:00:00Z",
            facts=[],
        )


async def _override_user() -> AuthUser:
    return AuthUser(
        email="person@example.com",
        google_sub="employee-sub",
        role="employee",
        entity_key="fundbox",
        display_name="Person",
    )


@pytest.fixture
def timeline_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_person_repo] = lambda: FakeTimelineRepo()
    app.dependency_overrides[require_active_user] = _override_user
    app.dependency_overrides[get_current_user_or_oauth_client] = _override_user
    return app


@asynccontextmanager
async def timeline_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.anyio
async def test_get_person_timeline_returns_envelope(timeline_app: FastAPI) -> None:
    async with timeline_client(timeline_app) as client:
        response = await client.get("/v1/persons/person-1/timeline?limit=1")

    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["source_record_pk"] == "sr-new"
    assert body["meta"]["total_count"] == 2
    assert body["meta"]["next_cursor"] is not None


@pytest.mark.anyio
async def test_get_person_timeline_target_returns_one_group(timeline_app: FastAPI) -> None:
    async with timeline_client(timeline_app) as client:
        response = await client.get("/v1/persons/person-1/timeline/target?source_record_pk=sr-new")

    assert response.status_code == 200
    assert response.json()["data"]["source_record_pk"] == "sr-new"


@pytest.mark.anyio
async def test_get_person_timeline_target_404s_for_missing_record(timeline_app: FastAPI) -> None:
    async with timeline_client(timeline_app) as client:
        response = await client.get("/v1/persons/person-1/timeline/target?source_record_pk=missing")

    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "source_record_not_found"
