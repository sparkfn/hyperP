"""Security regression tests for stage/authority reader isolation."""

from __future__ import annotations

from src.graph.queries import entities, graph, persons, persons_list, review
from src.graph.queries.sales_prediction_discovery import (
    DISCOVERY_DEAL_RECORDS,
    DISCOVERY_INTERACTION_RECORDS,
)
from src.profile_analysis_runtime_queries import FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS


def test_person_readers_admit_only_legacy_or_typed_activities() -> None:
    predicate = "history_family IS NULL OR sr.history_family = 'activity'"
    assert predicate in persons.GET_PERSON_SOURCE_RECORDS
    assert predicate in persons.GET_PERSON_TIMELINE
    assert "history_projection_version" in persons.GET_PERSON_SOURCE_RECORDS


def test_graph_explorer_blocks_stage_and_authority_nodes() -> None:
    query = graph.get_node_graph_query(2)
    assert "CrmHistoryAuthorityDecision" in query
    assert (
        "NOT n:SourceRecord OR n.history_family IS NULL OR n.history_family = 'activity'"
        in query
    )
    assert "start.history_family IS NULL OR start.history_family = 'activity'" in query


def test_profile_analysis_does_not_consume_stage_evidence() -> None:
    assert FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS.count("history_family IS NULL") == 4


def test_person_entity_and_list_readers_fail_closed_for_stage_history() -> None:
    predicate = "history_family IS NULL OR"
    queries = (
        persons.GET_PERSON_SOURCE_RECORDS,
        persons.GET_PERSON_TIMELINE,
        persons_list.build_list_persons_query(None, None, has_q=False),
        entities.LIST_ENTITIES,
        entities.get_entity_persons_query("preferred_full_name", "ASC"),
    )

    assert all(predicate in query for query in queries)


def test_review_and_sales_prediction_readers_exclude_stage_history() -> None:
    predicate = "history_family IS NULL OR"

    assert predicate in review.ACTIVATE_PENDING_REVIEW_RECORD
    assert predicate in review.REJECT_PENDING_REVIEW_RECORD
    assert predicate in DISCOVERY_DEAL_RECORDS
    assert predicate in DISCOVERY_INTERACTION_RECORDS
