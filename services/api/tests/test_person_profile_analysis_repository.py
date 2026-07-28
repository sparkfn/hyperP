"""Neo4j repository contracts for Person profile-analysis reads and request writes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

import pytest
import src.repositories.neo4j.person as person_module
from neo4j import AsyncManagedTransaction
from src.graph.converters import GraphRecord, GraphValue
from src.graph.queries.profile_analysis import (
    CREATE_PROFILE_ANALYSIS_REQUEST,
    GET_PERSON_PROFILE_ANALYSES,
    GET_PERSON_PROFILE_ANALYSIS_HISTORY,
    MARK_PROFILE_ANALYSIS_REQUEST_DISPATCH_FAILED,
)
from src.repositories.neo4j.person import Neo4jPersonRepository
from src.types_profile_analysis import ProfileAnalysisType


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


type _ProfileAnalysisWriteFn = Callable[
    [AsyncManagedTransaction, str, ProfileAnalysisType, bool, str],
    Awaitable[GraphRecord | None],
]


class _WriteSession:
    def __init__(self, transaction: _Session) -> None:
        self.transaction = transaction
        self.calls: list[tuple[_ProfileAnalysisWriteFn, str, ProfileAnalysisType, bool, str]] = []

    async def execute_write(
        self,
        function: _ProfileAnalysisWriteFn,
        person_id: str,
        analysis_type: ProfileAnalysisType,
        force: bool,
        request_id: str,
    ) -> GraphRecord | None:
        self.calls.append((function, person_id, analysis_type, force, request_id))
        return await function(
            cast(AsyncManagedTransaction, self.transaction),
            person_id,
            analysis_type,
            force,
            request_id,
        )


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


def _install_session(monkeypatch: pytest.MonkeyPatch, session: _Session) -> list[bool]:
    write_modes: list[bool] = []

    @asynccontextmanager
    async def fake_get_session(write: bool = False) -> AsyncIterator[_Session]:
        write_modes.append(write)
        yield session

    monkeypatch.setattr(person_module, "get_session", fake_get_session)
    return write_modes


@pytest.mark.anyio
async def test_request_profile_analysis_uses_write_transaction_and_maps_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _Session(
        _Record(
            {
                "person_id": "canonical-person",
                "state": "queued",
                "request_id": "request-1",
                "force_attempts_remaining": 3,
                "force_available_at": None,
            }
        )
    )
    session = _WriteSession(transaction)
    write_modes: list[bool] = []

    @asynccontextmanager
    async def fake_get_session(write: bool = False) -> AsyncIterator[_WriteSession]:
        write_modes.append(write)
        yield session

    monkeypatch.setattr(person_module, "get_session", fake_get_session)
    monkeypatch.setattr(person_module, "uuid4", lambda: "request-1")

    result = await Neo4jPersonRepository().request_profile_analysis(
        "merged-person",
        "sales",
        False,
    )

    assert write_modes == [True]
    assert len(session.calls) == 1
    assert session.calls[0][1:] == ("merged-person", "sales", False, "request-1")
    assert transaction.calls == [
        (
            CREATE_PROFILE_ANALYSIS_REQUEST,
            {
                "person_id": "merged-person",
                "analysis_type": "sales",
                "force": False,
                "request_id": "request-1",
            },
        )
    ]
    assert result is not None
    assert result.request_id == "request-1"
    assert result.person_id == "canonical-person"
    assert result.analysis_type == "sales"
    assert result.state == "queued"
    assert result.force is False
    assert result.force_attempts_remaining == 3
    assert result.force_available_at is None


@pytest.mark.anyio
async def test_current_profile_analysis_retrieval_remains_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(None)
    write_modes = _install_session(monkeypatch, session)

    assert await Neo4jPersonRepository().get_profile_analyses("person-1") is None
    assert write_modes == [False]
    assert session.calls == [(GET_PERSON_PROFILE_ANALYSES, {"person_id": "person-1"})]


@pytest.mark.anyio
async def test_dispatch_failure_marker_uses_write_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(None)
    write_modes = _install_session(monkeypatch, session)

    await Neo4jPersonRepository().mark_profile_analysis_request_dispatch_failed("request-1")

    assert write_modes == [True]
    assert session.calls == [
        (
            MARK_PROFILE_ANALYSIS_REQUEST_DISPATCH_FAILED,
            {"request_id": "request-1"},
        )
    ]


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
    write_modes = _install_session(monkeypatch, session)

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
    assert write_modes == [False]
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
