"""Shared doubles for the maintained Person-list summary counter (issue #489).

The API writers keep the maintained counter in step with the graph. Domain
tests drive those writers through a fake transaction, so the fake answers the
counter's capture and apply queries without knowing anything about the
counter's contents.
"""

from __future__ import annotations

from typing import Final

from src.graph.queries.person_list_summary import (
    APPLY_PERSON_LIST_SUMMARY_DELTAS,
    CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS,
    CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID,
    CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS,
    FIND_DEAL_ELEMENT_IDS_FOR_PERSONS,
)

#: The counter's read queries; a fake answers them with no rows.
SUMMARY_CAPTURE_QUERIES: Final[frozenset[str]] = frozenset(
    {
        CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS,
        CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS,
        CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID,
        FIND_DEAL_ELEMENT_IDS_FOR_PERSONS,
    }
)

#: The counter's single write; a fake answers it with a usable counter row.
SUMMARY_APPLY_QUERY: Final[str] = APPLY_PERSON_LIST_SUMMARY_DELTAS

#: A valid counter row: the doubles publish zero deltas.
SUMMARY_APPLY_ROW: Final[dict[str, object]] = {
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


def is_summary_capture_query(query: str) -> bool:
    """Whether the query is one of the counter's read queries."""
    return query in SUMMARY_CAPTURE_QUERIES


def is_summary_apply_query(query: str) -> bool:
    """Whether the query is the counter's single write."""
    return query == SUMMARY_APPLY_QUERY
