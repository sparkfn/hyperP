"""Query-contract tests for active-only survivorship evidence readers."""

from __future__ import annotations

from src.graph.queries.persons import (
    COUNT_PERSON_SOURCE_RECORDS,
    COUNT_PERSON_TIMELINE,
    GET_PERSON_ENTITIES,
    GET_PERSON_LOYALTY,
    GET_PERSON_SOURCE_RECORD_ENTITY_FACETS,
    GET_PERSON_SOURCE_RECORDS,
    GET_PERSON_TIMELINE,
    GET_PERSON_TIMELINE_TARGET,
)
from src.graph.queries.survivorship import (
    CHECK_SOURCE_RECORD_LINKED,
    GET_ADDRESS_FOR_SR,
    GET_FACT_VALUE,
    GET_FIELD_OPTIONS,
    GET_IDENTIFIER_VALUE_FOR_SR,
    GET_PERSON_FACTS,
)


def test_repair_retired_relationships_are_not_authoritative_to_survivorship() -> None:
    assert "coalesce(f.is_active, true) = true" in GET_PERSON_FACTS
    assert "coalesce(link.is_active, true) = true" in CHECK_SOURCE_RECORD_LINKED
    assert "coalesce(f.is_active, true) = true" in GET_FACT_VALUE
    assert "coalesce(rel.is_active, true) = true" in GET_IDENTIFIER_VALUE_FOR_SR
    assert "coalesce(la.is_active, true) = true" in GET_ADDRESS_FOR_SR
    assert "coalesce(f.is_active, true) = true" in GET_FIELD_OPTIONS


def test_repair_retired_links_are_not_authoritative_to_person_views() -> None:
    for query in (
        COUNT_PERSON_SOURCE_RECORDS,
        COUNT_PERSON_TIMELINE,
        GET_PERSON_ENTITIES,
        GET_PERSON_LOYALTY,
        GET_PERSON_SOURCE_RECORD_ENTITY_FACETS,
        GET_PERSON_SOURCE_RECORDS,
        GET_PERSON_TIMELINE,
        GET_PERSON_TIMELINE_TARGET,
    ):
        assert "[link:LINKED_TO]" in query
        assert "coalesce(link.is_active, true) = true" in query
