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
from src.graph.queries.person_list_summary import (
    APPLY_PERSON_LIST_SUMMARY_DELTAS,
    CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS,
    CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID,
    CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS,
    ENSURE_PERSON_LIST_SUMMARY_COUNTER,
    GET_PERSON_LIST_SUMMARY_COUNTER,
    GET_PERSON_LIST_SUMMARY_CURRENT_MONTH,
)
from src.graph.queries.persons_list import (
    GET_PERSON_LIST_CORE_SUMMARY,
    GET_PERSON_LIST_CRM_SUMMARY,
)
from src.repositories.deps import get_person_repo
from src.repositories.neo4j.person import Neo4jPersonRepository
from src.request_timing import begin_request, current_request_id, end_request
from src.routes.persons import router
from src.types import PersonListCoreSummary, PersonListCrmSummary, PersonListSummary


class _Record:
    def __init__(self, values: GraphRecord) -> None:
        self._values = values

    def keys(self) -> list[str]:
        return list(self._values)

    def values(self) -> list[GraphValue]:
        return list(self._values.values())

    def get(self, key: str, default: GraphValue = None) -> GraphValue:
        return self._values.get(key, default)


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
        counter_record: _Record | None = None,
    ) -> None:
        self.core_record = core_record
        self.crm_record = crm_record
        self.counter_record = counter_record if counter_record is not None else MISSING_COUNTER
        self.current_month = "2026-11"
        self.calls: list[str] = []

    async def run(self, query: str, _params: object = None, **_kwargs: object) -> _Result:
        self.calls.append(query)
        if query == GET_PERSON_LIST_SUMMARY_COUNTER:
            return _Result(self.counter_record)
        if query == GET_PERSON_LIST_SUMMARY_CURRENT_MONTH:
            return _Result(_Record({"current_month": self.current_month}))
        return _Result(
            self.crm_record if query == GET_PERSON_LIST_CRM_SUMMARY else self.core_record
        )

    @property
    def aggregate_calls(self) -> list[str]:
        """The retained aggregate queries, without keyed state or clock reads."""
        return [
            query
            for query in self.calls
            if query not in {GET_PERSON_LIST_SUMMARY_COUNTER, GET_PERSON_LIST_SUMMARY_CURRENT_MONTH}
        ]

    @property
    def counter_calls(self) -> list[str]:
        """The counter-node reads."""
        return [query for query in self.calls if query == GET_PERSON_LIST_SUMMARY_COUNTER]

    @property
    def clock_calls(self) -> list[str]:
        """The scalar realtime-clock reads."""
        return [query for query in self.calls if query == GET_PERSON_LIST_SUMMARY_CURRENT_MONTH]


def _counter_record(
    *,
    ready: bool = False,
    generation: int | None = None,
    generation_valid: bool = False,
    month: str | None = None,
    month_valid: bool = False,
    current_month: str = "2026-11",
    core_counts_valid: bool = False,
    crm_counts_valid: bool = False,
    active_person_count: int | None = None,
    high_risk_count: int | None = None,
    high_value_count: int | None = None,
    no_contact_count: int | None = None,
    deals_this_month_count: int | None = None,
    all_deals_count: int | None = None,
) -> _Record:
    """Build one keyed counter row."""
    return _Record(
        {
            "ready": ready,
            "generation": generation,
            "generation_valid": generation_valid,
            "month": month,
            "month_valid": month_valid,
            "current_month": current_month,
            "active_person_count": active_person_count,
            "high_risk_count": high_risk_count,
            "high_value_count": high_value_count,
            "no_contact_count": no_contact_count,
            "deals_this_month_count": deals_this_month_count,
            "all_deals_count": all_deals_count,
            "core_counts_valid": core_counts_valid,
            "crm_counts_valid": crm_counts_valid,
        }
    )


#: The default counter state: no singleton, every validity flag false.
MISSING_COUNTER = _counter_record()


def _ready_counter(
    *,
    generation: int = 7,
    current_month: str = "2026-11",
    month_valid: bool = True,
    core_counts_valid: bool = True,
    crm_counts_valid: bool = True,
) -> _Record:
    """Build a ready counter row with usable core and CRM values."""
    return _counter_record(
        ready=True,
        generation=generation,
        generation_valid=True,
        month=current_month,
        month_valid=month_valid,
        current_month=current_month,
        core_counts_valid=core_counts_valid,
        crm_counts_valid=crm_counts_valid,
        active_person_count=42,
        high_risk_count=7,
        high_value_count=5,
        no_contact_count=3,
        deals_this_month_count=9,
        all_deals_count=24,
    )


class _RolloverSession(_Session):
    """Return a pre-boundary counter state, then a post-boundary keyed state."""

    def __init__(self, core_record: _Record, crm_record: _Record) -> None:
        super().__init__(core_record, crm_record)
        self.counter_reads = 0

    async def run(self, query: str, _params: object = None, **_kwargs: object) -> _Result:
        if query == GET_PERSON_LIST_SUMMARY_CURRENT_MONTH:
            self.current_month = "2026-12"
        if query == GET_PERSON_LIST_SUMMARY_COUNTER:
            self.counter_reads += 1
            if self.counter_reads == 1:
                self.counter_record = _ready_counter()
            else:
                self.counter_record = _counter_record(
                    ready=True,
                    generation=7,
                    generation_valid=True,
                    month="2026-11",
                    month_valid=False,
                    current_month="2026-12",
                    core_counts_valid=True,
                    crm_counts_valid=True,
                    active_person_count=42,
                    high_risk_count=7,
                    high_value_count=5,
                    no_contact_count=3,
                    deals_this_month_count=9,
                    all_deals_count=24,
                )
        return await super().run(query, _params, **_kwargs)


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
    assert session.aggregate_calls == [GET_PERSON_LIST_CORE_SUMMARY, GET_PERSON_LIST_CRM_SUMMARY]


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
    assert session.aggregate_calls == [GET_PERSON_LIST_CORE_SUMMARY, GET_PERSON_LIST_CRM_SUMMARY]


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

    assert session.aggregate_calls == [
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

    assert session.aggregate_calls == [
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
    _install_session(monkeypatch, _Session(None, None))

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
    _install_session(monkeypatch, _Session(None, None))

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
    _install_session(monkeypatch, _Session(None, None))
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
    _install_session(monkeypatch, _Session(None, None))
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
async def test_crm_stale_refresh_does_not_inherit_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)
    _install_session(monkeypatch, _Session(None, None))
    now = [100.0]
    monkeypatch.setattr(person_module, "monotonic", lambda: now[0])

    class _ContextRepository(Neo4jPersonRepository):
        def __init__(self) -> None:
            super().__init__()
            self.crm_calls = 0
            self.refresh_started = asyncio.Event()
            self.release = asyncio.Event()
            self.request_ids: list[str | None] = []

        async def _load_core_summary(self) -> PersonListSummary:
            return PersonListSummary(all_profiles_count=42)

        async def _load_crm_summary(self) -> tuple[int, int]:
            self.crm_calls += 1
            self.request_ids.append(current_request_id())
            if self.crm_calls == 2:
                self.refresh_started.set()
                await self.release.wait()
            return self.crm_calls, self.crm_calls

    repo = _ContextRepository()
    token = begin_request("request-crm")
    try:
        assert (await repo.get_list_summary()).all_deals_count == 1
        now[0] = 131.0
        assert (await repo.get_list_summary()).all_deals_count == 1
        await repo.refresh_started.wait()
        assert repo.request_ids == ["request-crm", None]
    finally:
        end_request(token)

    refresh = repo._crm_summary_task
    assert refresh is not None
    repo.release.set()
    await refresh


@pytest.mark.anyio
async def test_concurrent_summary_cache_misses_are_coalesced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)
    _install_session(monkeypatch, _Session(None, None))

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
    _install_session(monkeypatch, _Session(None, None))

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
    _install_session(monkeypatch, _Session(None, None))

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

    async def get_list_core_summary(self) -> PersonListCoreSummary:
        return PersonListCoreSummary(
            all_profiles_count=42,
            high_risk_count=7,
            high_value_count=5,
            no_contact_count=3,
        )

    async def get_list_crm_summary(self) -> PersonListCrmSummary:
        return PersonListCrmSummary(deals_this_month_count=9, all_deals_count=24)


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


@pytest.mark.anyio
async def test_summary_subroutes_keep_core_available_when_crm_is_independent() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_person_repo] = lambda: _SummaryRepo()
    app.dependency_overrides[require_active_user] = _summary_user
    app.dependency_overrides[get_current_user_or_oauth_client] = _summary_user
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        core = await client.get("/v1/persons/summary/core")
        crm = await client.get("/v1/persons/summary/crm")

    assert core.status_code == 200
    assert core.json()["data"] == {
        "all_profiles_count": 42,
        "high_risk_count": 7,
        "high_value_count": 5,
        "no_contact_count": 3,
    }
    assert crm.status_code == 200
    assert crm.json()["data"] == {"deals_this_month_count": 9, "all_deals_count": 24}


def test_summary_month_queries_sample_realtime_after_counter_lock() -> None:
    month_sample = "datetime.truncate('month', datetime.realtime())"
    assert "WITH c, datetime.realtime() AS current_time" in GET_PERSON_LIST_SUMMARY_COUNTER
    assert "right('0' + toString(datetime.truncate('month', current_time).month), 2)" in (
        GET_PERSON_LIST_SUMMARY_COUNTER
    )
    assert "substring(" not in GET_PERSON_LIST_SUMMARY_COUNTER
    assert "right('0' + toString(scan_month.month), 2)" in APPLY_PERSON_LIST_SUMMARY_DELTAS
    assert "coalesce(p.is_high_risk = true, false)" in CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS
    assert "coalesce(p.is_high_value = true, false)" in CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS
    assert "coalesce((deal IS NOT NULL" in CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS
    assert "coalesce((deal IS NOT NULL" in CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID
    assert "SET c.updated_at" in APPLY_PERSON_LIST_SUMMARY_DELTAS
    assert APPLY_PERSON_LIST_SUMMARY_DELTAS.index(
        "SET c.updated_at"
    ) < APPLY_PERSON_LIST_SUMMARY_DELTAS.index(f"{month_sample} AS scan_month")
    assert f"{month_sample} AS final_month" in APPLY_PERSON_LIST_SUMMARY_DELTAS


@pytest.mark.anyio
async def test_ready_counter_serves_all_six_fields_with_one_keyed_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(None, None, counter_record=_ready_counter())
    _install_session(monkeypatch, session)
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)

    summary = await Neo4jPersonRepository().get_list_summary()

    assert summary == PersonListSummary(
        all_profiles_count=42,
        high_risk_count=7,
        high_value_count=5,
        no_contact_count=3,
        deals_this_month_count=9,
        all_deals_count=24,
    )
    assert session.counter_calls == [GET_PERSON_LIST_SUMMARY_COUNTER]
    assert session.clock_calls == [GET_PERSON_LIST_SUMMARY_CURRENT_MONTH]
    assert session.aggregate_calls == []


@pytest.mark.anyio
async def test_combined_counter_read_rechecks_month_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _RolloverSession(
        _Record({"all_profiles_count": 101}),
        _Record({"deals_this_month_count": 5, "all_deals_count": 6}),
    )
    _install_session(monkeypatch, session)
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 0)

    summary = await Neo4jPersonRepository().get_list_summary()

    assert summary == PersonListSummary(
        all_profiles_count=42,
        high_risk_count=7,
        high_value_count=5,
        no_contact_count=3,
        deals_this_month_count=5,
        all_deals_count=6,
    )
    assert GET_PERSON_LIST_CORE_SUMMARY not in session.aggregate_calls
    assert session.aggregate_calls == [GET_PERSON_LIST_CRM_SUMMARY]
    assert session.counter_reads == 2
    assert session.clock_calls == [GET_PERSON_LIST_SUMMARY_CURRENT_MONTH]


@pytest.mark.anyio
async def test_crm_counter_read_rechecks_month_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _RolloverSession(
        _Record({"all_profiles_count": 101}),
        _Record({"deals_this_month_count": 5, "all_deals_count": 6}),
    )
    _install_session(monkeypatch, session)
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 0)

    summary = await Neo4jPersonRepository().get_list_crm_summary()

    assert summary == PersonListCrmSummary(deals_this_month_count=5, all_deals_count=6)
    assert session.aggregate_calls == [GET_PERSON_LIST_CRM_SUMMARY]
    assert session.counter_reads == 3
    assert session.clock_calls == [GET_PERSON_LIST_SUMMARY_CURRENT_MONTH]


@pytest.mark.anyio
async def test_crm_counter_becoming_ready_during_fallback_rechecks_month() -> None:
    initial = person_module._CounterState.from_values(
        _counter_record(ready=False, generation=7, generation_valid=True)._values
    )
    ready = person_module._CounterState.from_values(_ready_counter()._values)
    after_rollover = person_module._CounterState.from_values(
        _counter_record(
            ready=False, generation=7, generation_valid=True, current_month="2026-12"
        )._values
    )

    class _ReadyDuringFallbackRepo(Neo4jPersonRepository):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0
            self.loads = 0

        async def _crm_summary_for(self, state: person_module._CounterState) -> tuple[int, int]:
            self.loads += 1
            return (5, 6) if self.loads == 1 else (7, 8)

        async def _read_counter_state(self) -> person_module._CounterState:
            self.reads += 1
            return ready if self.reads == 1 else after_rollover

        async def _read_database_month(self) -> str:
            return "2026-12"

    repo = _ReadyDuringFallbackRepo()
    assert await repo._crm_summary_with_boundary_recheck(initial) == (7, 8)
    assert repo.reads == 2
    assert repo.loads == 2


@pytest.mark.anyio
async def test_core_and_crm_fast_paths_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(
        _Record(
            {
                "all_profiles_count": 1,
                "high_risk_count": 2,
                "high_value_count": 3,
                "no_contact_count": 4,
            }
        ),
        _Record({"deals_this_month_count": 5, "all_deals_count": 6}),
        counter_record=_ready_counter(crm_counts_valid=False),
    )
    _install_session(monkeypatch, session)
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)

    summary = await Neo4jPersonRepository().get_list_summary()

    # The core counts come from the counter, the CRM counts from the retained
    # aggregate: an unusable CRM counter never invalidates the core one.
    assert summary.all_profiles_count == 42
    assert summary.deals_this_month_count == 5
    assert summary.all_deals_count == 6
    assert GET_PERSON_LIST_CORE_SUMMARY not in session.aggregate_calls
    assert GET_PERSON_LIST_CRM_SUMMARY in session.aggregate_calls


@pytest.mark.anyio
async def test_crm_failure_does_not_break_the_core_summary(
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
        None,
    )
    _install_session(monkeypatch, session)
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)

    class _FailingCrmRepo(Neo4jPersonRepository):
        async def _load_crm_summary(self) -> tuple[int, int]:
            raise RuntimeError("CRM unavailable")

    repo = _FailingCrmRepo()

    core = await repo.get_list_core_summary()
    assert core.all_profiles_count == 42
    with pytest.raises(RuntimeError, match="CRM unavailable"):
        await repo.get_list_crm_summary()


@pytest.mark.parametrize(
    "counter_record",
    [
        pytest.param(MISSING_COUNTER, id="missing"),
        pytest.param(
            _counter_record(ready=False, generation=3, generation_valid=True),
            id="unready",
        ),
        pytest.param(
            _counter_record(ready=True, generation=3, generation_valid=False),
            id="malformed-generation",
        ),
        pytest.param(
            _counter_record(ready=True, generation=3, generation_valid=True),
            id="malformed-counts",
        ),
    ],
)
@pytest.mark.anyio
async def test_unusable_counter_states_fall_back_to_the_retained_aggregates(
    monkeypatch: pytest.MonkeyPatch,
    counter_record: _Record,
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
        counter_record=counter_record,
    )
    _install_session(monkeypatch, session)
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)

    summary = await Neo4jPersonRepository().get_list_summary()

    assert summary == PersonListSummary(
        all_profiles_count=42,
        high_risk_count=7,
        high_value_count=5,
        no_contact_count=3,
        deals_this_month_count=9,
        all_deals_count=24,
    )


@pytest.mark.anyio
async def test_zero_ttl_keeps_no_cached_fallback(
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

    assert repo._summary_cache is None
    assert repo._crm_summary_cache is None


@pytest.mark.anyio
async def test_generation_change_drops_the_cached_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(
        _Record({"all_profiles_count": 42}),
        _Record({"deals_this_month_count": 9, "all_deals_count": 24}),
    )
    _install_session(monkeypatch, session)
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)
    repo = Neo4jPersonRepository()

    assert (await repo.get_list_summary()).all_profiles_count == 42

    session.counter_record = _counter_record(ready=True, generation=8, generation_valid=True)
    session.core_record = _Record({"all_profiles_count": 43})

    assert (await repo.get_list_summary()).all_profiles_count == 43
    assert session.aggregate_calls.count(GET_PERSON_LIST_CORE_SUMMARY) == 2


@pytest.mark.anyio
async def test_month_rollover_drops_only_the_cached_crm_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(
        _Record({"all_profiles_count": 42}),
        _Record({"deals_this_month_count": 9, "all_deals_count": 24}),
        counter_record=_counter_record(
            ready=False, generation=3, generation_valid=True, current_month="2026-11"
        ),
    )
    _install_session(monkeypatch, session)
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)
    repo = Neo4jPersonRepository()

    assert (await repo.get_list_summary()).all_deals_count == 24

    session.counter_record = _counter_record(
        ready=False, generation=3, generation_valid=True, current_month="2026-12"
    )

    assert (await repo.get_list_summary()).all_deals_count == 24
    # The month-scoped cache was dropped, the month-independent core cache kept.
    assert session.aggregate_calls.count(GET_PERSON_LIST_CRM_SUMMARY) == 2
    assert session.aggregate_calls.count(GET_PERSON_LIST_CORE_SUMMARY) == 1


@pytest.mark.anyio
async def test_stale_detached_crm_completion_does_not_replace_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)
    now = [100.0]
    monkeypatch.setattr(person_module, "monotonic", lambda: now[0])
    session = _Session(
        _Record({"all_profiles_count": 42}),
        _Record({"deals_this_month_count": 7, "all_deals_count": 19}),
    )
    _install_session(monkeypatch, session)

    class _RefreshingRepo(Neo4jPersonRepository):
        def __init__(self) -> None:
            super().__init__()
            self.crm_calls = 0
            self.refresh_started = asyncio.Event()
            self.release_refresh = asyncio.Event()

        async def _load_core_summary(self) -> PersonListSummary:
            return PersonListSummary(all_profiles_count=42)

        async def _load_crm_summary(self) -> tuple[int, int]:
            self.crm_calls += 1
            if self.crm_calls == 1:
                return 7, 19
            self.refresh_started.set()
            await self.release_refresh.wait()
            return 8, 20

    repo = _RefreshingRepo()
    assert (await repo.get_list_summary()).all_deals_count == 19
    now[0] = 131.0
    stale = await repo.get_list_summary()
    await repo.refresh_started.wait()
    assert stale.all_deals_count == 19

    # The counter is reset while the detached refresh is still running.
    session.counter_record = _counter_record(ready=True, generation=99, generation_valid=True)
    repo.release_refresh.set()
    refresh_task = repo._crm_summary_task
    assert refresh_task is not None
    await refresh_task
    await asyncio.sleep(0)

    assert repo._crm_summary_cache == (7, 19)


@pytest.mark.anyio
async def test_repeatedly_stale_aggregate_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(None, None)
    _install_session(monkeypatch, session)
    monkeypatch.setattr(person_module.config, "person_list_summary_cache_ttl_seconds", 30)

    class _MovingGenerationSession(_Session):
        def __init__(self) -> None:
            super().__init__(None, None)
            self.reads = 0

        async def run(self, query: str, params: object = None, **kwargs: object) -> _Result:
            if query == GET_PERSON_LIST_SUMMARY_COUNTER:
                self.reads += 1
                self.counter_record = _counter_record(
                    ready=False, generation=self.reads, generation_valid=True
                )
            return await super().run(query, params, **kwargs)

    moving = _MovingGenerationSession()
    _install_session(monkeypatch, moving)
    repo = Neo4jPersonRepository()

    with pytest.raises(RuntimeError, match="raced the counter generation"):
        await repo.get_list_summary()


def test_counter_bootstrap_creates_the_singleton_unready_and_never_resets_it() -> None:
    assert "ON CREATE SET" in ENSURE_PERSON_LIST_SUMMARY_COUNTER
    assert "c.ready = false" in ENSURE_PERSON_LIST_SUMMARY_COUNTER
    # One write only: the bootstrap never promotes or resets an existing counter.
    assert ENSURE_PERSON_LIST_SUMMARY_COUNTER.count("SET ") == 1
    assert "c.ready = true" not in ENSURE_PERSON_LIST_SUMMARY_COUNTER
    assert "c.ready = c.ready" not in ENSURE_PERSON_LIST_SUMMARY_COUNTER
