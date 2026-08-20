from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import src.repositories.neo4j.entity as entity_module
from src.graph.converters import GraphRecord, GraphValue
from src.graph.mappers_entities import map_entity_filter_option
from src.graph.queries.entities import LIST_ENTITY_FILTER_OPTIONS
from src.repositories.neo4j.entity import Neo4jEntityRepository
from src.types import EntityFilterOption


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
