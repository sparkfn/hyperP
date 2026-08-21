from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import src.repositories.neo4j.person as person_module
from src.graph.converters import GraphRecord, GraphValue
from src.repositories.neo4j.person import Neo4jPersonRepository
from src.types import ListedPerson


class _Record:
    def __init__(self, person_id: str) -> None:
        self._values: GraphRecord = {"person": {"person_id": person_id}}

    def keys(self) -> list[str]:
        return list(self._values)

    def values(self) -> list[GraphValue]:
        return list(self._values.values())


class _Result:
    def __init__(self, records: list[_Record], total: int | None = None) -> None:
        self._records = records
        self._total = total

    def __aiter__(self) -> AsyncIterator[_Record]:
        async def iterate() -> AsyncIterator[_Record]:
            for record in self._records:
                yield record

        return iterate()

    async def single(self) -> dict[str, int] | None:
        return None if self._total is None else {"total": self._total}


class _Session:
    def __init__(self, records: list[_Record], total: int = 7) -> None:
        self.records = records
        self.total = total
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def run(self, query: str, params: dict[str, object]) -> _Result:
        self.calls.append((query, params))
        if query == "COUNT":
            return _Result([], total=self.total)
        return _Result(self.records)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("row_count", "limit", "expected_count", "expected_has_more"),
    [(0, 2, 0, False), (2, 2, 2, False), (3, 2, 2, True)],
)
async def test_count_free_page_uses_one_limit_plus_one_query(
    monkeypatch: pytest.MonkeyPatch,
    row_count: int,
    limit: int,
    expected_count: int,
    expected_has_more: bool,
) -> None:
    session = _Session([_Record(f"person-{index}") for index in range(row_count)])
    session_count = 0

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[_Session]:
        nonlocal session_count
        session_count += 1
        yield session

    def fail_count_query(**kwargs: object) -> str:
        raise AssertionError(f"count query must not be built: {kwargs}")

    monkeypatch.setattr(person_module, "get_session", fake_get_session)
    monkeypatch.setattr(person_module, "build_list_persons_query", lambda *args, **kwargs: "LIST")
    monkeypatch.setattr(person_module, "build_count_persons_query", fail_count_query)
    monkeypatch.setattr(
        person_module,
        "map_listed_person",
        lambda record: ListedPerson(
            person_id=str(record["person"]["person_id"]),
            status="active",
        ),
    )

    page = await Neo4jPersonRepository().get_page({}, 4, limit, include_total=False)

    assert len(page.items) == expected_count
    assert page.has_more is expected_has_more
    assert page.total_count is None
    assert session_count == 1
    assert session.calls == [("LIST", {"skip": 4, "limit": limit + 1})]


@pytest.mark.anyio
async def test_exact_total_page_executes_list_and_count_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_Record("person-1"), _Record("person-2"), _Record("person-3")])
    session_count = 0

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[_Session]:
        nonlocal session_count
        session_count += 1
        yield session

    list_builder_args: tuple[object, ...] | None = None
    list_builder_kwargs: dict[str, object] | None = None
    count_builder_args: tuple[object, ...] | None = None
    count_builder_kwargs: dict[str, object] | None = None

    def build_list(*args: object, **kwargs: object) -> str:
        nonlocal list_builder_args, list_builder_kwargs
        list_builder_args = args
        list_builder_kwargs = kwargs
        return "LIST"

    def build_count(*args: object, **kwargs: object) -> str:
        nonlocal count_builder_args, count_builder_kwargs
        count_builder_args = args
        count_builder_kwargs = kwargs
        return "COUNT"

    monkeypatch.setattr(person_module, "get_session", fake_get_session)
    monkeypatch.setattr(person_module, "build_list_persons_query", build_list)
    monkeypatch.setattr(person_module, "build_count_persons_query", build_count)
    monkeypatch.setattr(
        person_module,
        "map_listed_person",
        lambda record: ListedPerson(
            person_id=str(record["person"]["person_id"]),
            status="active",
        ),
    )

    filters = {
        "crm_deal_count_min": 0,
        "crm_deal_count_max": 0,
        "sort_by": "crm_deal_count",
        "sort_order": "asc",
        "entity_key_mode": "and",
        "source_key_mode": "and",
    }
    page = await Neo4jPersonRepository().get_page(filters, 4, 2, include_total=True)

    assert [item.person_id for item in page.items] == ["person-1", "person-2"]
    assert page.has_more is True
    assert page.total_count == 7
    assert session_count == 2
    expected_params = {"crm_deal_count_min": 0, "crm_deal_count_max": 0}
    assert ("LIST", {**expected_params, "skip": 4, "limit": 3}) in session.calls
    assert ("COUNT", expected_params) in session.calls
    assert list_builder_args == count_builder_args == ("crm_deal_count", "asc")
    assert (
        list_builder_kwargs
        == count_builder_kwargs
        == {
            "has_q": False,
            "active_filters": frozenset(
                {
                    "crm_deal_count_min",
                    "crm_deal_count_max",
                    "sort_by",
                    "sort_order",
                    "entity_key_mode",
                    "source_key_mode",
                }
            ),
            "entity_mode": "and",
            "source_mode": "and",
        }
    )


@pytest.mark.anyio
async def test_exact_total_controls_continuation_when_list_snapshot_disagrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(
        [_Record("person-1"), _Record("person-2"), _Record("list-only-extra")],
        total=2,
    )

    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[_Session]:
        yield session

    monkeypatch.setattr(person_module, "get_session", fake_get_session)
    monkeypatch.setattr(person_module, "build_list_persons_query", lambda *args, **kwargs: "LIST")
    monkeypatch.setattr(person_module, "build_count_persons_query", lambda *args, **kwargs: "COUNT")
    monkeypatch.setattr(
        person_module,
        "map_listed_person",
        lambda record: ListedPerson(
            person_id=str(record["person"]["person_id"]),
            status="active",
        ),
    )

    page = await Neo4jPersonRepository().get_page({}, 0, 2, include_total=True)

    assert len(page.items) == 2
    assert page.total_count == 2
    assert page.has_more is False
