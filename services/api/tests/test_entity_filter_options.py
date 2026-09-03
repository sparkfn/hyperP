from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from neo4j import Query
import src.repositories.neo4j.entity as entity_module
from src.graph.converters import GraphRecord, GraphValue
from src.graph.mappers_entities import map_entity_filter_option
from src.graph.queries.entities import LIST_ENTITY_FILTER_OPTIONS
from src.repositories.neo4j.entity import Neo4jEntityRepository
from src.request_timing import begin_request, current_request_id, end_request
from src.types import EntityFilterOption, EntityMetrics, EntitySummary


class _Record:
    def __init__(self, values: GraphRecord) -> None:
        self._values = values

    def keys(self) -> list[str]:
        return list(self._values)

    def values(self) -> list[GraphValue]:
        return list(self._values.values())


class _Result:
    def __init__(self, records: list[_Record]) -> None:
        self._records = records

    def __aiter__(self) -> AsyncIterator[_Record]:
        async def iterate() -> AsyncIterator[_Record]:
            for record in self._records:
                yield record

        return iterate()


class _Session:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, query: str) -> _Result:
        self.calls.append(query)
        return _Result(
            [
                _Record({"entity_key": "eko", "display_name": "Eko"}),
                _Record({"entity_key": "legacy", "display_name": None}),
            ]
        )


def test_filter_options_query_is_lightweight() -> None:
    upper = LIST_ENTITY_FILTER_OPTIONS.upper()
    assert "MATCH (E:ENTITY)" in upper
    for forbidden in (
        ":PERSON",
        ":SOURCERECORD",
        "LINKED_TO",
        "OWNED_BY",
        "OPERATED_BY",
        "FROM_SOURCE",
        "CALL",
        "COUNT(",
        "MAX(",
    ):
        assert forbidden not in upper


def test_filter_option_mapper_preserves_null_display_name() -> None:
    assert map_entity_filter_option(
        {"entity_key": "legacy", "display_name": None}
    ) == EntityFilterOption(entity_key="legacy", display_name=None)


@pytest.mark.anyio
async def test_repository_executes_lightweight_filter_options_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session()

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[_Session]:
        yield session

    monkeypatch.setattr(entity_module, "get_session", fake_get_session)

    options = await Neo4jEntityRepository().get_filter_options()

    assert options == [
        EntityFilterOption(entity_key="eko", display_name="Eko"),
        EntityFilterOption(entity_key="legacy", display_name=None),
    ]
    assert session.calls == [LIST_ENTITY_FILTER_OPTIONS]


@pytest.mark.anyio
async def test_exact_entity_aggregate_uses_bounded_background_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _AggregateSession:
        def __init__(self) -> None:
            self.query: str | Query | None = None

        async def run(self, query: str | Query) -> _Result:
            self.query = query
            return _Result([])

    session = _AggregateSession()

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[_AggregateSession]:
        yield session

    monkeypatch.setattr(entity_module, "get_session", fake_get_session)
    monkeypatch.setattr(
        entity_module.config,
        "neo4j_background_read_transaction_timeout_seconds",
        47.0,
    )

    assert await Neo4jEntityRepository()._load_all() == []
    assert isinstance(session.query, Query)
    assert session.query.text == entity_module.LIST_ENTITIES
    assert session.query.timeout == 47.0


@pytest.mark.anyio
async def test_entity_summary_cache_coalesces_exact_aggregate_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.repositories.neo4j.entity as entity_module

    monkeypatch.setattr(entity_module.config, "entity_summary_cache_ttl_seconds", 30)

    class _BlockingRepository(Neo4jEntityRepository):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def _load_all(self) -> list[EntitySummary]:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return [EntitySummary(entity_key="eko", person_count=2, source_record_count=3)]

    repo = _BlockingRepository()
    first = asyncio.create_task(repo.get_all())
    await repo.started.wait()
    second = asyncio.create_task(repo.get_all())
    repo.release.set()

    first_items, second_items = await asyncio.gather(first, second)
    assert first_items == second_items
    assert repo.calls == 1


@pytest.mark.anyio
async def test_entity_summary_expiry_returns_stale_value_and_refreshes_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entity_module.config, "entity_summary_cache_ttl_seconds", 30)
    now = [100.0]
    monkeypatch.setattr(entity_module, "monotonic", lambda: now[0])

    class _RefreshingRepository(Neo4jEntityRepository):
        def __init__(self) -> None:
            super().__init__()
            self.values = [1, 2]
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.request_ids: list[str | None] = []

        async def _load_all(self) -> list[EntitySummary]:
            value = self.values.pop(0)
            self.request_ids.append(current_request_id())
            if value == 2:
                self.started.set()
                await self.release.wait()
            return [EntitySummary(entity_key="eko", person_count=value, source_record_count=value)]

    repo = _RefreshingRepository()
    token = begin_request("request-entity")
    try:
        assert (await repo.get_all())[0].person_count == 1
        now[0] = 131.0
        assert (await repo.get_all())[0].person_count == 1
        await repo.started.wait()
        assert repo.request_ids == ["request-entity", None]
    finally:
        end_request(token)

    refresh = repo._summary_refresh_task
    assert refresh is not None
    repo.release.set()
    await refresh
    await asyncio.sleep(0)
    assert (await repo.get_all())[0].person_count == 2


@pytest.mark.anyio
async def test_entity_summary_stale_refresh_failure_is_bounded_and_clears_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entity_module.config, "entity_summary_cache_ttl_seconds", 30)
    monkeypatch.setattr(entity_module.config, "entity_summary_cache_max_stale_seconds", 30)
    now = [100.0]
    monkeypatch.setattr(entity_module, "monotonic", lambda: now[0])

    class _FailingRefreshRepository(Neo4jEntityRepository):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _load_all(self) -> list[EntitySummary]:
            self.calls += 1
            if self.calls == 1:
                return [EntitySummary(entity_key="eko", person_count=2, source_record_count=3)]
            raise RuntimeError("neo4j unavailable")

    repo = _FailingRefreshRepository()
    assert (await repo.get_all())[0].person_count == 2
    now[0] = 131.0
    assert (await repo.get_all())[0].person_count == 2
    refresh = repo._summary_refresh_task
    assert refresh is not None
    with pytest.raises(RuntimeError, match="neo4j unavailable"):
        await refresh
    await asyncio.sleep(0)
    assert repo._summary_refresh_task is None

    now[0] = 161.0
    with pytest.raises(RuntimeError, match="neo4j unavailable"):
        await repo.get_all()
    assert repo.calls == 3


@pytest.mark.anyio
async def test_entity_summary_cutoff_awaits_active_refresh_without_duplicate_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entity_module.config, "entity_summary_cache_ttl_seconds", 30)
    monkeypatch.setattr(entity_module.config, "entity_summary_cache_max_stale_seconds", 30)
    now = [100.0]
    monkeypatch.setattr(entity_module, "monotonic", lambda: now[0])

    class _SlowRefreshRepository(Neo4jEntityRepository):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.refresh_started = asyncio.Event()
            self.release_refresh = asyncio.Event()

        async def _load_all(self) -> list[EntitySummary]:
            self.calls += 1
            if self.calls == 2:
                self.refresh_started.set()
                await self.release_refresh.wait()
            return [
                EntitySummary(
                    entity_key="eko",
                    person_count=self.calls,
                    source_record_count=self.calls,
                )
            ]

    repo = _SlowRefreshRepository()
    assert (await repo.get_all())[0].person_count == 1
    now[0] = 131.0
    assert (await repo.get_all())[0].person_count == 1
    await asyncio.wait_for(repo.refresh_started.wait(), timeout=1)

    now[0] = 161.0
    cutoff_request = asyncio.create_task(repo.get_all())
    await asyncio.sleep(0)
    assert repo.calls == 2
    repo.release_refresh.set()

    assert (await asyncio.wait_for(cutoff_request, timeout=1))[0].person_count == 2
    assert repo.calls == 2


@pytest.mark.anyio
async def test_entity_summary_cutoff_propagates_active_refresh_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entity_module.config, "entity_summary_cache_ttl_seconds", 30)
    monkeypatch.setattr(entity_module.config, "entity_summary_cache_max_stale_seconds", 30)
    now = [100.0]
    monkeypatch.setattr(entity_module, "monotonic", lambda: now[0])

    class _FailingSlowRefreshRepository(Neo4jEntityRepository):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.refresh_started = asyncio.Event()
            self.release_refresh = asyncio.Event()

        async def _load_all(self) -> list[EntitySummary]:
            self.calls += 1
            if self.calls == 1:
                return [EntitySummary(entity_key="eko", person_count=1, source_record_count=1)]
            self.refresh_started.set()
            await self.release_refresh.wait()
            raise RuntimeError("neo4j unavailable")

    repo = _FailingSlowRefreshRepository()
    await repo.get_all()
    now[0] = 131.0
    await repo.get_all()
    await asyncio.wait_for(repo.refresh_started.wait(), timeout=1)

    now[0] = 161.0
    cutoff_request = asyncio.create_task(repo.get_all())
    await asyncio.sleep(0)
    assert repo.calls == 2
    repo.release_refresh.set()

    with pytest.raises(RuntimeError, match="neo4j unavailable"):
        await asyncio.wait_for(cutoff_request, timeout=1)
    assert repo.calls == 2
    await asyncio.sleep(0)
    assert repo._summary_refresh_task is None


@pytest.mark.anyio
async def test_entity_metrics_omit_uncomputed_review_case_count() -> None:
    class _MetricsRepository(Neo4jEntityRepository):
        async def get_all(self) -> list[EntitySummary]:
            return [
                EntitySummary(
                    entity_key="eko",
                    person_count=2,
                    source_record_count=3,
                    active_review_cases=9,
                )
            ]

    metrics = await _MetricsRepository().get_metrics()

    assert metrics == [EntityMetrics(entity_key="eko", person_count=2, source_record_count=3)]
    assert "active_review_cases" not in metrics[0].model_dump()


@pytest.mark.anyio
async def test_metadata_uses_only_lightweight_query_without_exact_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MetadataSession:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, query: str) -> _Result:
            self.calls.append(query)
            return _Result([_Record({"entity": {"entity_key": "eko", "display_name": "Eko"}})])

    session = _MetadataSession()

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[_MetadataSession]:
        yield session

    monkeypatch.setattr(entity_module, "get_session", fake_get_session)
    metadata = await Neo4jEntityRepository().get_metadata()

    assert [item.entity_key for item in metadata] == ["eko"]
    assert session.calls == [entity_module.LIST_ENTITY_METADATA]
