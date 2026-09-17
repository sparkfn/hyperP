"""Active current-state reader checks without dormant repair query discovery."""

from __future__ import annotations

from pathlib import Path

from src.crm_deal_identity_repair.reader_classification import (
    RelationshipReader,
    _has_active_predicate,
    assert_reader_contract,
)

_ROOT = Path(__file__).resolve().parents[3]


def _active_reader_sources() -> tuple[Path, ...]:
    return (
        _ROOT / "services/api/src/graph/queries/review.py",
        _ROOT / "services/ingestion/src/graph/queries/crm_history.py",
        _ROOT / "services/ingestion/src/graph/queries/knows.py",
        _ROOT / "services/ingestion/src/graph/queries/persons.py",
        _ROOT / "services/ingestion/src/graph/queries/sales.py",
        _ROOT / "services/ingestion/src/graph/queries/vehicle.py",
    )


def test_active_materializers_are_classified_and_filter_current_relationships() -> None:
    readers = {
        reader.identifier: reader
        for reader in assert_reader_contract(*_active_reader_sources())
    }
    expected = {
        "api/graph/queries/review.py:LINK_REVIEW_SALES_BOUGHT_VEHICLE",
        "api/graph/queries/review.py:LINK_REVIEW_SALES_PURCHASED_ORDER",
        "ingestion/graph/queries/crm_history.py:LINK_CONVERSATION_TO_CRM_HISTORY",
        "ingestion/graph/queries/crm_history.py:LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS",
        "ingestion/graph/queries/knows.py:LINK_PERSON_KNOWS",
        "ingestion/graph/queries/persons.py:LINK_PERSON_TO_ADDRESS",
        "ingestion/graph/queries/persons.py:LINK_PERSON_TO_IDENTIFIER",
        "ingestion/graph/queries/sales.py:LINK_PERSON_PURCHASED_ORDER",
        "ingestion/graph/queries/vehicle.py:LINK_PERSON_BOUGHT_VEHICLE",
    }
    assert expected <= set(readers)
    assert all(_has_active_predicate(readers[identifier]) for identifier in expected)


def test_active_predicate_fails_closed_for_inactive_or_unbound_relationships() -> None:
    inactive = RelationshipReader(
        "ingestion/graph/queries/example.py",
        "INACTIVE",
        "authoritative_mutation",
        "MERGE (person)-[purchase:PURCHASED {is_active: false}]->(:Order)",
    )
    unbound = RelationshipReader(
        "ingestion/graph/queries/example.py",
        "UNBOUND",
        "authoritative",
        "MATCH (source:SourceRecord)-[link:LINKED_TO]->(:Person) "
        "MATCH (:Person)-[other:KNOWS]->(:Person) "
        "WHERE coalesce(link.is_active, true) = true RETURN source",
    )

    assert not _has_active_predicate(inactive)
    assert not _has_active_predicate(unbound)
