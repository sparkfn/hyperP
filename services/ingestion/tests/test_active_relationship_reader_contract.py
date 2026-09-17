"""Exhaustive active current-state reader and classifier regression contracts."""

from __future__ import annotations

from pathlib import Path

from ci_support.selection_manifest import HISTORICAL_SOURCE_PATHS
from src.crm_deal_identity_repair.reader_classification import (
    RelationshipReader,
    _AUDIT_READERS,
    _AUTHORITATIVE_MUTATION_READERS,
    _AUTHORITATIVE_READERS,
    _MUTATION_READERS,
    _has_active_predicate,
    discover_relationship_readers,
)

_ROOT = Path(__file__).resolve().parents[3]


def _active_reader_sources() -> tuple[Path, ...]:
    historical = set(HISTORICAL_SOURCE_PATHS)
    roots = (
        _ROOT / "services/api/src",
        _ROOT / "services/ingestion/src",
    )
    candidates = (
        path
        for root in roots
        for path in root.rglob("*.py")
        if path.name != "__init__.py"
        and path.relative_to(_ROOT).as_posix() not in historical
    )
    return tuple(sorted(candidates))


def _registry_source_path(identifier: str) -> str:
    module, _symbol = identifier.split(":", maxsplit=1)
    service, relative_path = module.split("/", maxsplit=1)
    return f"services/{service}/src/{relative_path}"


def _active_registry_identifiers() -> frozenset[str]:
    registry = (
        _AUDIT_READERS
        | _AUTHORITATIVE_READERS
        | _AUTHORITATIVE_MUTATION_READERS
        | _MUTATION_READERS
    )
    historical = set(HISTORICAL_SOURCE_PATHS)
    return frozenset(
        identifier
        for identifier in registry
        if _registry_source_path(identifier) not in historical
    )


def test_every_active_relationship_reader_is_registered_and_current_filtered() -> None:
    readers = discover_relationship_readers(*_active_reader_sources())
    identifiers = {reader.identifier for reader in readers}

    assert readers
    assert identifiers == _active_registry_identifiers()
    assert all(
        _has_active_predicate(reader)
        for reader in readers
        if reader.classification in {"authoritative", "authoritative_mutation"}
    )


def test_active_materializers_remain_classified_and_current_filtered() -> None:
    readers = {
        reader.identifier: reader
        for reader in discover_relationship_readers(*_active_reader_sources())
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


def test_active_predicate_requires_each_reused_binding_scope() -> None:
    reader = RelationshipReader(
        "ingestion/graph/queries/example.py",
        "REUSED",
        "authoritative",
        "MATCH (first:SourceRecord)-[link:LINKED_TO]->(:Person) "
        "WHERE coalesce(link.is_active, true) = true WITH first "
        "MATCH (second:SourceRecord)-[link:LINKED_TO]->(:Person) RETURN second",
    )

    assert not _has_active_predicate(reader)


def test_active_anonymous_pattern_expression_fails_closed() -> None:
    reader = RelationshipReader(
        "api/graph/queries/example.py",
        "COUNT_RETIRED_PURCHASES",
        "authoritative",
        "RETURN count { (person)-[:PURCHASED]->(:Order) } AS order_count",
    )

    assert not _has_active_predicate(reader)
