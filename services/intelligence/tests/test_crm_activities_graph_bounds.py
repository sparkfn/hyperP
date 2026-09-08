"""Focused graph-bound contracts for the CRM activities archive repository."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest
from intelligence.crm.activities.models import ArchiveRequest
from intelligence.graph.queries.crm_activities import (
    PREFLIGHT_REFERENCE_FANOUT,
    PREFLIGHT_STRUCTURAL_INVALID,
)
from intelligence.repositories.neo4j.crm_activities import Neo4jCrmActivitiesRepository


class _FakeRow:
    def __init__(self, values: Mapping[str, object]) -> None:
        self._values = values

    def data(self) -> dict[str, object]:
        return dict(self._values)

    def get(self, key: str) -> object:
        return self._values.get(key)


class _FakeResult:
    def __init__(self, rows: tuple[_FakeRow, ...]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[_FakeRow]:
        return iter(self._rows)

    def single(self) -> _FakeRow | None:
        if len(self._rows) > 1:
            raise RuntimeError("fake result is not singular")
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, results: tuple[_FakeResult, ...]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *unused: object) -> None:
        del unused

    def run(self, query: str, parameters: dict[str, object]) -> _FakeResult:
        self.calls.append((query, parameters))
        if not self._results:
            raise RuntimeError("fake result was not configured")
        return self._results.pop(0)


class _FakeDriver:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self.closed = False

    def session(self, **unused: object) -> _FakeSession:
        del unused
        return self._session

    def close(self) -> None:
        self.closed = True


def _row(identity: str) -> _FakeRow:
    return _FakeRow(
        {
            "source_record_pk": identity,
            "source_record_id": f"record-{identity}",
            "source_record_version": "1",
            "source_version_key": f"version-{identity}",
            "record_hash": f"hash-{identity}",
            "source_instance_id": "bitrix-primary",
            "source_key": "bitrix_chat",
            "record_type": "crm_history",
            "lifecycle_status": "active",
            "history_family": "activity",
            "history_kind": "call",
            "history_source": "bitrix_crm_activity",
            "projection_version": "2",
            "projection_source": "bitrix_crm_activity_v2",
            "event_at": None,
            "link_status": None,
            "observed_at": None,
            "ingested_at": None,
            "available_at": None,
            "stored_parent": None,
            "child_parents": [],
            "details_parents": [],
            "people": [],
            "malformed_person_association_count": 0,
            "duplicate_delivery_count": 0,
            "user_capabilities": [],
        }
    )


def _repository(
    monkeypatch: pytest.MonkeyPatch, results: tuple[_FakeResult, ...]
) -> tuple[Neo4jCrmActivitiesRepository, _FakeSession, _FakeDriver]:
    session = _FakeSession(results)
    driver = _FakeDriver(session)
    monkeypatch.setattr(
        "intelligence.repositories.neo4j.crm_activities.GraphDatabase.driver",
        lambda uri, auth: driver,
    )
    return Neo4jCrmActivitiesRepository("bolt://graph", "neo4j", "password"), session, driver


def test_repository_uses_parameterized_bounded_reads_and_finite_preflights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, session, driver = _repository(
        monkeypatch,
        (
            _FakeResult((_row("activity-a"), _row("activity-b"))),
            _FakeResult((_row("activity-a"), _row("activity-b"))),
            _FakeResult((_FakeRow({"invalid_count": 0}),)),
            _FakeResult((_FakeRow({"invalid_count": 1}),)),
        ),
    )
    request = ArchiveRequest(
        "snapshot-a",
        "bitrix-primary",
        page_size=2,
        max_references_per_record=2,
    )
    try:
        assert [item.source_record_pk for item in repository.page(request, "").records] == [
            "activity-a",
            "activity-b",
        ]
        assert [
            item.source_record_pk
            for item in repository.by_identities(request, ("activity-b", "activity-a")).records
        ] == ["activity-a", "activity-b"]
        assert repository.structural_invalid_count(request) == 0
        assert repository.reference_fanout_invalid_count(request) == 1
    finally:
        repository.close()

    assert driver.closed
    assert session.calls[0][1] == {
        "source_instance_id": "bitrix-primary",
        "source_key": "bitrix_chat",
        "after_source_record_pk": "",
        "limit": 2,
    }
    assert session.calls[1][1]["source_record_pks"] == ["activity-b", "activity-a"]
    assert session.calls[2][1] == {
        "source_instance_id": "bitrix-primary",
        "source_key": "bitrix_chat",
    }
    assert session.calls[3][0] == PREFLIGHT_REFERENCE_FANOUT
    assert session.calls[3][1] == {
        "source_instance_id": "bitrix-primary",
        "source_key": "bitrix_chat",
        "max_references_per_record": 2,
    }


@pytest.mark.parametrize("value", (None, True, -1, "1"))
def test_reference_fanout_preflight_fails_closed_for_nonfinite_count(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    rows = () if value is None else (_FakeRow({"invalid_count": value}),)
    repository, _, _ = _repository(monkeypatch, (_FakeResult(rows),))
    try:
        with pytest.raises(RuntimeError, match="reference fan-out preflight"):
            repository.reference_fanout_invalid_count(
                ArchiveRequest("snapshot-a", "bitrix-primary")
            )
    finally:
        repository.close()


def test_reference_fanout_query_is_closed_read_only_and_untruncated() -> None:
    lower = PREFLIGHT_REFERENCE_FANOUT.lower()
    assert "$max_references_per_record" in PREFLIGHT_REFERENCE_FANOUT
    assert "CHILD_OF" in PREFLIGHT_REFERENCE_FANOUT
    assert "DETAILS_HISTORY_ITEM" in PREFLIGHT_REFERENCE_FANOUT
    assert "coalesce(link.is_active, true) = true" in PREFLIGHT_REFERENCE_FANOUT
    assert "child_parent)-[:FROM_SOURCE]->(child_source:SourceSystem)" in PREFLIGHT_REFERENCE_FANOUT
    assert (
        "details_parent)-[:FROM_SOURCE]->(details_source:SourceSystem)"
        in PREFLIGHT_REFERENCE_FANOUT
    )
    assert "WITH DISTINCT" in PREFLIGHT_REFERENCE_FANOUT
    assert "RETURN count(link) AS active_person_count" in PREFLIGHT_REFERENCE_FANOUT
    assert "count(DISTINCT record) AS source_record_count" in PREFLIGHT_STRUCTURAL_INVALID
    assert "limit" not in lower
    for forbidden in ("raw_payload", "create", "merge", "set", "delete"):
        assert forbidden not in lower
