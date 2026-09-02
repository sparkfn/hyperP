from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import src.repositories.neo4j.entity as entity_module
from src.graph.converters import GraphRecord, GraphValue
from src.graph.mappers_entities import map_entity_filter_option
from src.graph.queries.entities import LIST_ENTITY_FILTER_OPTIONS
from src.repositories.neo4j.entity import Neo4jEntityRepository
from src.request_timing import begin_request, current_request_id, end_request
from src.types import EntityFilterOption, EntitySummary


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
