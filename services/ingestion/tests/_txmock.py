"""Shared test mock for Neo4j transaction recording.

Pipeline test files (sales, knows, bankruptcy, rental-flats, pair-audit) each
mock the Neo4j ``ManagedTransaction`` to assert on the queries a pipeline issues.
This base centralises the ``self.calls: list[tuple[str, dict[str, object]]]``
recording pattern so each per-file mock only overrides ``run`` to route queries
to its canned results.

It also carries the canned answers for the maintained Person-list summary
counter (issue #489): the writers capture the counter's contributions before
their mutation and publish the delta afterwards, so every fake transaction has
to answer those queries.
"""

from __future__ import annotations

from collections.abc import Iterator

from src.graph.queries.person_list_summary import (
    APPLY_PERSON_LIST_SUMMARY_DELTAS,
    CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS,
    CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID,
    CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS,
    INVALIDATE_PERSON_LIST_SUMMARY,
)
from src.graph.queries.profile_analysis_dirty import FIND_SOURCE_IDENTITY_DEAL_ELEMENT_IDS

#: The maintained-summary counter reads a fake transaction must answer.
SUMMARY_CAPTURE_QUERIES: frozenset[str] = frozenset(
    {
        CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS,
        CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS,
        CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID,
        FIND_SOURCE_IDENTITY_DEAL_ELEMENT_IDS,
    }
)

#: The maintained-summary counter's single write.
SUMMARY_APPLY_QUERY: str = APPLY_PERSON_LIST_SUMMARY_DELTAS

#: A valid counter row: the doubles publish zero deltas.
SUMMARY_APPLY_ROW: dict[str, object] = {
    "ready": True,
    "core_applied": True,
    "all_deals_applied": True,
    "monthly_applied": True,
    "current_month_key": "2026-11",
    "active_person_count": 0,
    "high_risk_count": 0,
    "high_value_count": 0,
    "no_contact_count": 0,
    "all_deals_count": 0,
    "deals_this_month_count": 0,
}


class _RecordingTx:
    """Records every ``run()`` call as ``(query, kwargs)`` in ``self.calls``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _record(self, query: str, kw: dict[str, object]) -> None:
        self.calls.append((query, dict(kw)))


class _SummaryCounterResult:
    """A canned result for one maintained-summary counter query."""

    def __init__(self, record: dict[str, object] | None = None) -> None:
        self._record = record

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(() if self._record is None else (self._record,))

    def single(self) -> dict[str, object] | None:
        return self._record


def is_summary_capture_query(query: str) -> bool:
    """Whether the query is one of the counter's read queries."""
    return query in SUMMARY_CAPTURE_QUERIES


def is_summary_apply_query(query: str) -> bool:
    """Whether the query is the counter's single write."""
    return query == SUMMARY_APPLY_QUERY


def summary_counter_result(query: str) -> _SummaryCounterResult | None:
    """Return the canned counter result for a counter query, else None.

    A capture reads nothing, the invalidation reports an unready counter with
    the next generation, and the apply reports a usable counter row.
    """
    if is_summary_capture_query(query):
        return _SummaryCounterResult()
    if query == INVALIDATE_PERSON_LIST_SUMMARY:
        return _SummaryCounterResult({"ready": False, "generation": 1})
    if is_summary_apply_query(query):
        return _SummaryCounterResult(dict(SUMMARY_APPLY_ROW))
    return None
