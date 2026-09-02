from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import src.repositories.neo4j.person as person_module
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.auth.deps import get_current_user_or_oauth_client, require_active_user
from src.auth.models import AuthUser
from src.graph.converters import GraphRecord, GraphValue
from src.graph.queries.persons_list import (
    GET_PERSON_LIST_CORE_SUMMARY,
    GET_PERSON_LIST_CRM_SUMMARY,
)
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
    def __init__(
        self,
        core_record: _Record | None,
        crm_record: _Record | None = None,
    ) -> None:
        self.core_record = core_record
        self.crm_record = crm_record
        self.calls: list[str] = []

    async def run(self, query: str) -> _Result:
        self.calls.append(query)
        return _Result(
            self.crm_record if query == GET_PERSON_LIST_CRM_SUMMARY else self.core_record
        )


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
        ),
        _Record({"deals_this_month_count": 9, "all_deals_count": 24}),
    )
    _install_session(monkeypatch, session)

    summary = await Neo4jPersonRepository().get_list_summary()

    assert summary == PersonListSummary(
        all_profiles_count=42,
        high_risk_count=7,
        high_value_count=5,
        no_contact_count=3,
        deals_this_month_count=9,
        all_deals_count=24,
    )
    assert session.calls == [GET_PERSON_LIST_CORE_SUMMARY, GET_PERSON_LIST_CRM_SUMMARY]


@pytest.mark.anyio
async def test_repository_defaults_missing_summary_record_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(None, None)
    _install_session(monkeypatch, session)

    assert await Neo4jPersonRepository().get_list_summary() == PersonListSummary()


@pytest.mark.anyio
async def test_summary_cache_reuses_value_and_returns_copies(
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
        ),
        _Record({"deals_this_month_count": 9, "all_deals_count": 24}),
    )
    _install_session(monkeypatch, session)
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)
    repo = Neo4jPersonRepository()

    first = await repo.get_list_summary()
    first.all_profiles_count = 999
    second = await repo.get_list_summary()

    assert second.all_profiles_count == 42
    assert session.calls == [GET_PERSON_LIST_CORE_SUMMARY, GET_PERSON_LIST_CRM_SUMMARY]


@pytest.mark.anyio
async def test_summary_cache_refreshes_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(
        _Record({"all_profiles_count": 1}),
        _Record({"deals_this_month_count": 2, "all_deals_count": 3}),
    )
    _install_session(monkeypatch, session)
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)
    now = [100.0]
    monkeypatch.setattr(person_module, "monotonic", lambda: now[0])
    repo = Neo4jPersonRepository()

    assert (await repo.get_list_summary()).all_profiles_count == 1
    now[0] = 131.0
    session.core_record = _Record({"all_profiles_count": 2})
    session.crm_record = _Record({"deals_this_month_count": 4, "all_deals_count": 5})
    assert (await repo.get_list_summary()).all_profiles_count == 2

    crm_refresh = repo._crm_summary_task
    if crm_refresh is not None:
        await crm_refresh

    assert session.calls == [
        GET_PERSON_LIST_CORE_SUMMARY,
        GET_PERSON_LIST_CRM_SUMMARY,
        GET_PERSON_LIST_CORE_SUMMARY,
        GET_PERSON_LIST_CRM_SUMMARY,
    ]


@pytest.mark.anyio
async def test_zero_ttl_disables_summary_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(
        _Record({"all_profiles_count": 1}),
        _Record({"deals_this_month_count": 2, "all_deals_count": 3}),
    )
    _install_session(monkeypatch, session)
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 0)
    repo = Neo4jPersonRepository()

    await repo.get_list_summary()
    session.core_record = _Record({"all_profiles_count": 2})
    session.crm_record = _Record({"deals_this_month_count": 4, "all_deals_count": 5})
    assert (await repo.get_list_summary()).all_profiles_count == 2

    assert session.calls == [
        GET_PERSON_LIST_CORE_SUMMARY,
        GET_PERSON_LIST_CRM_SUMMARY,
        GET_PERSON_LIST_CORE_SUMMARY,
        GET_PERSON_LIST_CRM_SUMMARY,
    ]


@pytest.mark.anyio
async def test_cold_summary_awaits_nonzero_crm_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)

    class _ExactRepo(Neo4jPersonRepository):
        async def _load_core_summary(self) -> PersonListSummary:
            return PersonListSummary(all_profiles_count=42)

        async def _load_crm_summary(self) -> tuple[int, int]:
            return 7, 19

    summary = await _ExactRepo().get_list_summary()

    assert summary.deals_this_month_count == 7
    assert summary.all_deals_count == 19


@pytest.mark.anyio
async def test_concurrent_cold_crm_summary_requests_are_coalesced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)

    class _BlockingRepo(Neo4jPersonRepository):
        def __init__(self) -> None:
            super().__init__()
            self.crm_calls = 0
            self.crm_started = asyncio.Event()
            self.release = asyncio.Event()

        async def _load_core_summary(self) -> PersonListSummary:
            return PersonListSummary(all_profiles_count=42)

        async def _load_crm_summary(self) -> tuple[int, int]:
            self.crm_calls += 1
            self.crm_started.set()
            await self.release.wait()
            return 7, 19

    repo = _BlockingRepo()
    first = asyncio.create_task(repo.get_list_summary())
    await repo.crm_started.wait()
    second = asyncio.create_task(repo.get_list_summary())
    await asyncio.sleep(0)
    repo.release.set()

    summaries = await asyncio.gather(first, second)

    assert [summary.all_deals_count for summary in summaries] == [19, 19]
    assert repo.crm_calls == 1


@pytest.mark.anyio
async def test_expired_crm_summary_returns_stale_exact_value_while_refreshing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)
    now = [100.0]
    monkeypatch.setattr(person_module, "monotonic", lambda: now[0])

    class _RefreshingRepo(Neo4jPersonRepository):
        def __init__(self) -> None:
            super().__init__()
            self.crm_values = [(7, 19), (8, 20)]
            self.refresh_started = asyncio.Event()
            self.release_refresh = asyncio.Event()

        async def _load_core_summary(self) -> PersonListSummary:
            return PersonListSummary(all_profiles_count=42)

        async def _load_crm_summary(self) -> tuple[int, int]:
            value = self.crm_values.pop(0)
            if value == (8, 20):
                self.refresh_started.set()
                await self.release_refresh.wait()
            return value

    repo = _RefreshingRepo()
    assert (await repo.get_list_summary()).all_deals_count == 19
    now[0] = 131.0

    stale = await repo.get_list_summary()
    await repo.refresh_started.wait()
    assert stale.all_deals_count == 19

    refresh_task = repo._crm_summary_task
    assert refresh_task is not None
    repo.release_refresh.set()
    await refresh_task
    await asyncio.sleep(0)
    assert (await repo.get_list_summary()).all_deals_count == 20


@pytest.mark.anyio
async def test_failed_crm_stale_refresh_retains_prior_exact_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)
    now = [100.0]
    monkeypatch.setattr(person_module, "monotonic", lambda: now[0])

    class _FailingRefreshRepo(Neo4jPersonRepository):
        def __init__(self) -> None:
            super().__init__()
            self.crm_calls = 0
            self.refresh_attempted = asyncio.Event()

        async def _load_core_summary(self) -> PersonListSummary:
            return PersonListSummary(all_profiles_count=42)

        async def _load_crm_summary(self) -> tuple[int, int]:
            self.crm_calls += 1
            if self.crm_calls == 1:
                return 7, 19
            self.refresh_attempted.set()
            raise RuntimeError("CRM unavailable")

    repo = _FailingRefreshRepo()
    assert (await repo.get_list_summary()).all_deals_count == 19
    now[0] = 131.0
    assert (await repo.get_list_summary()).all_deals_count == 19
    await repo.refresh_attempted.wait()
    refresh_task = repo._crm_summary_task
    assert refresh_task is not None
    with pytest.raises(RuntimeError, match="CRM unavailable"):
        await refresh_task
    assert repo._crm_summary_cache == (7, 19)


@pytest.mark.anyio
async def test_concurrent_summary_cache_misses_are_coalesced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)

    class _BlockingRepo(Neo4jPersonRepository):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def _load_core_summary(self) -> PersonListSummary:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return PersonListSummary(all_profiles_count=42)

        async def _load_crm_summary(self) -> tuple[int, int]:
            return 0, 0

    repo = _BlockingRepo()
    first = asyncio.create_task(repo.get_list_summary())
    await repo.started.wait()
    second = asyncio.create_task(repo.get_list_summary())
    await asyncio.sleep(0)
    repo.release.set()

    assert await asyncio.gather(first, second) == [
        PersonListSummary(all_profiles_count=42),
        PersonListSummary(all_profiles_count=42),
    ]
    assert repo.calls == 1


@pytest.mark.anyio
async def test_summary_loader_exceptions_are_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)

    class _FlakyRepo(Neo4jPersonRepository):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _load_core_summary(self) -> PersonListSummary:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary failure")
            return PersonListSummary(all_profiles_count=42)

        async def _load_crm_summary(self) -> tuple[int, int]:
            return 0, 0

    repo = _FlakyRepo()
    with pytest.raises(RuntimeError, match="temporary failure"):
        await repo.get_list_summary()

    assert (await repo.get_list_summary()).all_profiles_count == 42
    assert repo.calls == 2


@pytest.mark.anyio
async def test_cancelled_summary_refresh_releases_cache_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)

    class _CancellableRepo(Neo4jPersonRepository):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.first_started = asyncio.Event()

        async def _load_core_summary(self) -> PersonListSummary:
            self.calls += 1
            if self.calls == 1:
                self.first_started.set()
                await asyncio.Event().wait()
            return PersonListSummary(all_profiles_count=42)

        async def _load_crm_summary(self) -> tuple[int, int]:
            return 0, 0

    repo = _CancellableRepo()
    first = asyncio.create_task(repo.get_list_summary())
    await repo.first_started.wait()
    second = asyncio.create_task(repo.get_list_summary())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert (await asyncio.wait_for(second, timeout=1)).all_profiles_count == 42
    assert repo.calls == 2


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
            "deals_this_month_count": 0,
            "all_deals_count": 0,
        },
        "meta": {
            "request_id": "req-summary",
            "next_cursor": None,
            "total_count": None,
        },
        "display_items": None,
    }
