"""Neo4j repository contracts for atomic Person profile-analysis history reads."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import src.repositories.neo4j.person as person_module
from src.graph.converters import GraphRecord, GraphValue
from src.graph.queries.profile_analysis import (
    GET_PERSON_PROFILE_ANALYSES,
    GET_PERSON_PROFILE_ANALYSIS_HISTORY,
)
from src.repositories.neo4j.person import Neo4jPersonRepository


def test_profile_analysis_read_queries_use_scoped_subqueries() -> None:
    assert "CALL (person" in GET_PERSON_PROFILE_ANALYSES
    assert "CALL (person" in GET_PERSON_PROFILE_ANALYSIS_HISTORY


class _Record:
    def __init__(self, values: GraphRecord) -> None:
        self._values = values

    def keys(self) -> list[str]:
        return list(self._values)

    def values(self) -> list[GraphValue]:
        return list(self._values.values())

    def __getitem__(self, key: str) -> GraphValue:
        return self._values[key]


class _Result:
    def __init__(
        self,
        *,
        single_record: _Record | None,
        iter_records: list[_Record] | None = None,
    ) -> None:
        self._single_record = single_record
        self._iter_records = iter_records or []

    async def single(self) -> _Record | None:
        return self._single_record

    def __aiter__(self) -> AsyncIterator[_Record]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[_Record]:
        for record in self._iter_records:
            yield record


class _Session:
    def __init__(self, combined_record: _Record | None) -> None:
        self.calls: list[tuple[str, dict[str, GraphValue]]] = []
        self._combined_record = combined_record

    async def run(self, query: str, **parameters: GraphValue) -> _Result:
        self.calls.append((query, parameters))
        if len(self.calls) == 1:
            return _Result(single_record=self._combined_record)
        return _Result(single_record=_Record({"total": 0}))


def _history_analysis() -> GraphRecord:
    return {
        "analysis_id": "analysis-sales",
        "person_id": "canonical-person",
        "analysis_type": "sales",
        "status": "succeeded",
        "content": "Supported analysis [order-1]",
        "input_revision": 7,
        "input_fingerprint": "sha256-fingerprint",
        "prompt_version": "sales-profile-v1",
        "provider": "proclaude",
        "model": "analysis-model",
        "started_at": "2026-07-21T01:00:00+00:00",
        "completed_at": "2026-07-21T01:02:00+00:00",
        "attempt_number": 2,
        "failure_code": None,
        "retryable": None,
        "next_retry_at": None,
    }


def _install_session(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    @asynccontextmanager
    async def fake_get_session() -> AsyncIterator[_Session]:
        yield session

    monkeypatch.setattr(person_module, "get_session", fake_get_session)


@pytest.mark.anyio
async def test_history_uses_one_combined_read_with_filter_and_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(
        _Record(
            {
                "person_id": "canonical-person",
                "total": 3,
                "analyses": [_history_analysis()],
            }
        )
    )
    _install_session(monkeypatch, session)

    page = await Neo4jPersonRepository().get_profile_analysis_history(
        "merged-person",
        "sales",
        2,
        1,
    )

    assert page is not None
    items, total = page
    assert total == 3
    assert [item.analysis_id for item in items] == ["analysis-sales"]
    assert len(session.calls) == 1
    assert session.calls[0][1] == {
        "person_id": "merged-person",
        "analysis_type": "sales",
        "skip": 2,
        "limit": 1,
    }


@pytest.mark.anyio
async def test_history_combined_read_distinguishes_empty_from_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_session = _Session(_Record({"person_id": "canonical-person", "total": 0, "analyses": []}))
    _install_session(monkeypatch, empty_session)

    assert await Neo4jPersonRepository().get_profile_analysis_history(
        "canonical-person", None, 0, 20
    ) == ([], 0)
    assert len(empty_session.calls) == 1

    missing_session = _Session(None)
    _install_session(monkeypatch, missing_session)

    assert (
        await Neo4jPersonRepository().get_profile_analysis_history("missing-person", None, 0, 20)
        is None
    )
    assert len(missing_session.calls) == 1
