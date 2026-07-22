"""Accepted graph-change invalidation contracts for profile analysis."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from neo4j import ManagedTransaction
from src.graph import queries
from src.profile_analysis_dirty import mark_profile_analysis_dirty


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)


class _Tx:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.calls.append((query, parameters))
        return _Result(self._rows)


def test_dirty_query_resolves_all_accepted_person_context_once() -> None:
    query = queries.MARK_PROFILE_ANALYSIS_DIRTY

    assert "MATCH (source)-[:LINKED_TO]->(direct:Person)" in query
    assert "IDENTIFIED_BY|LIVES_AT|HAS_FACT" in query
    assert "PURCHASED|BOUGHT_VEHICLE|OWNS_VEHICLE" in query
    assert "MATCH (left:Person)-[knows:KNOWS]->(right:Person)" in query
    assert "WITH DISTINCT person_id" in query
    assert "MATCH (person:Person {person_id: person_id, status: 'active'})" in query
    assert "coalesce(person.analysis_input_revision, 0) + 1" in query
    assert "person.analysis_dirty_at = datetime()" in query


def test_dirty_helper_deduplicates_inputs_and_returns_typed_active_ids() -> None:
    tx = _Tx([{"person_id": "person-1"}, {"person_id": "person-2"}])

    dirtied = mark_profile_analysis_dirty(
        cast(ManagedTransaction, tx),
        source_record_pks=("new", "old", "new"),
        person_ids=("person-2", "person-2"),
    )

    assert dirtied == ("person-1", "person-2")
    assert tx.calls == [
        (
            queries.MARK_PROFILE_ANALYSIS_DIRTY,
            {
                "source_record_pks": ["new", "old"],
                "person_ids": ["person-2"],
            },
        )
    ]


def test_dirty_helper_is_a_noop_without_accepted_change_context() -> None:
    tx = _Tx([])

    assert mark_profile_analysis_dirty(cast(ManagedTransaction, tx)) == ()
    assert tx.calls == []


def test_retirement_query_collects_affected_people_before_deactivation() -> None:
    query = queries.RETIRE_SOURCE_EVIDENCE

    assert "collect(DISTINCT person.person_id)" in query
    assert "collect(DISTINCT knows_from.person_id)" in query
    assert "collect(DISTINCT knows_to.person_id)" in query
    assert "analysis_input_revision" in query
    assert "status: 'active'" in query
    assert query.index("collect(DISTINCT person.person_id)") < query.index("rel.is_active = false")


def test_retirement_does_not_dirty_provisional_pending_review_links() -> None:
    query = queries.RETIRE_SOURCE_EVIDENCE

    assert "accepted_records" in query
    assert "UNWIND accepted_records AS source" in query
    assert query.index("UNWIND accepted_records AS source") < query.index(
        "collect(DISTINCT direct.person_id)"
    )
