from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import src.repositories.neo4j.person as person_module
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.auth.deps import get_current_user_or_oauth_client, require_active_user
from src.auth.models import AuthUser
from src.graph.converters import GraphRecord, GraphValue
from src.graph.queries.persons_list import GET_PERSON_LIST_SUMMARY
from src.repositories.deps import get_person_repo
from src.repositories.neo4j.person import Neo4jPersonRepository
from src.routes.persons import router
from src.types import PersonListSummary


class _Record:
    def __init__(self, values: GraphRecord) -> None:
        self._values = values

    def keys(self) -> list[str]:
        return list(self._values)

    def values(self) -> list[GraphValue]:
        return list(self._values.values())


class _Result:
    def __init__(self, record: _Record | None) -> None:
        self._record = record

    async def single(self) -> _Record | None:
        return self._record


class _Session:
    def __init__(self, record: _Record | None) -> None:
        self.record = record
        self.calls: list[str] = []

    async def run(self, query: str) -> _Result:
        self.calls.append(query)
        return _Result(self.record)


def _install_session(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[_Session]:
        yield session

    monkeypatch.setattr(person_module, "get_session", fake_get_session)


@pytest.mark.anyio
async def test_repository_reads_and_maps_person_list_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(
        _Record(
            {
                "all_profiles_count": 42,
                "high_risk_count": 7,
                "high_value_count": 5,
                "no_contact_count": 3,
            }
        )
    )
    _install_session(monkeypatch, session)

    summary = await Neo4jPersonRepository().get_list_summary()

    assert summary == PersonListSummary(
        all_profiles_count=42,
        high_risk_count=7,
        high_value_count=5,
        no_contact_count=3,
    )
    assert session.calls == [GET_PERSON_LIST_SUMMARY]


@pytest.mark.anyio
async def test_repository_defaults_missing_summary_record_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(None)
    _install_session(monkeypatch, session)

    assert await Neo4jPersonRepository().get_list_summary() == PersonListSummary()


class _SummaryRepo:
    async def get_list_summary(self) -> PersonListSummary:
        return PersonListSummary(
            all_profiles_count=42,
            high_risk_count=7,
            high_value_count=5,
            no_contact_count=3,
        )


async def _summary_user() -> AuthUser:
    return AuthUser(
        email="person@example.com",
        google_sub="employee-sub",
        role="employee",
        entity_key="fundbox",
        display_name="Person",
    )


@pytest.mark.anyio
async def test_summary_route_returns_single_aggregate_payload() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_person_repo] = lambda: _SummaryRepo()
    app.dependency_overrides[require_active_user] = _summary_user
    app.dependency_overrides[get_current_user_or_oauth_client] = _summary_user
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/v1/persons/summary", headers={"x-request-id": "req-summary"})

    assert response.status_code == 200
    assert response.json() == {
        "data": {
            "all_profiles_count": 42,
            "high_risk_count": 7,
            "high_value_count": 5,
            "no_contact_count": 3,
        },
        "meta": {
            "request_id": "req-summary",
            "next_cursor": None,
            "total_count": None,
        },
        "display_items": None,
    }
