"""Query-contract tests for inactive CRM-deal relationship handling."""

from __future__ import annotations

from src.graph.queries.crm_history import ACTIVATE_PENDING_CALLS_FOR_DEAL, CREATE_CALL_FROM_HISTORY
from src.graph.queries.knows import (
    RESOLVE_PERSON_FROM_SOURCE_RECORD_ID,
    RESOLVE_PERSON_FROM_SOURCE_RECORD_PK,
)
from src.graph.queries.source_records import LOCK_AND_GET_SOURCE_STATE


def test_crm_history_owner_inheritance_ignores_inactive_deal_links() -> None:
    for query in (CREATE_CALL_FROM_HISTORY, ACTIVATE_PENDING_CALLS_FOR_DEAL):
        assert "[deal_link:LINKED_TO]" in query
        assert "coalesce(deal_link.is_active, true) = true" in query


def test_source_lifecycle_continuity_ignores_inactive_record_links() -> None:
    assert "[link:LINKED_TO]" in LOCK_AND_GET_SOURCE_STATE
    assert "coalesce(link.is_active, true) = true" in LOCK_AND_GET_SOURCE_STATE


def test_relationship_materialization_ignores_inactive_record_links() -> None:
    for query in (
        RESOLVE_PERSON_FROM_SOURCE_RECORD_ID,
        RESOLVE_PERSON_FROM_SOURCE_RECORD_PK,
    ):
        assert "[link:LINKED_TO]" in query
        assert "coalesce(link.is_active, true) = true" in query
