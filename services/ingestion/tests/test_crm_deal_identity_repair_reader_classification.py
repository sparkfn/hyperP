"""Contract tests for repair-retired relationship reader safety."""

from __future__ import annotations

from pathlib import Path

import pytest
import src.crm_deal_identity_repair.reader_classification as reader_classification
from src.crm_deal_identity_repair.reader_classification import (
    _AUDIT_READERS,
    _AUTHORITATIVE_MUTATION_READERS,
    _AUTHORITATIVE_READERS,
    _MUTATION_READERS,
    RelationshipReader,
    _has_active_predicate,
    approved_reader_sources,
    assert_reader_contract,
    discover_relationship_readers,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_all_executable_relationship_readers_are_explicitly_classified() -> None:
    readers = assert_reader_contract(*approved_reader_sources(_REPO_ROOT))

    assert readers
    assert {reader.identifier for reader in readers} == (
        _AUDIT_READERS
        | _AUTHORITATIVE_READERS
        | _AUTHORITATIVE_MUTATION_READERS
        | _MUTATION_READERS
    )
    assert all(
        reader.classification
        in {"authoritative", "authoritative_mutation", "audit", "audit_mutation"}
        for reader in readers
    )


def test_authoritative_readers_exclude_inactive_relationships() -> None:
    readers = assert_reader_contract(*approved_reader_sources(_REPO_ROOT))
    authoritative = [
        reader
        for reader in readers
        if reader.classification in {"authoritative", "authoritative_mutation"}
    ]

    assert authoritative
    for reader in authoritative:
        assert _has_active_predicate(reader)

    by_key = {reader.identifier: reader for reader in authoritative}
    assert (
        "coalesce(link.is_active, true) = true"
        in by_key[
            "ingestion/graph/queries/sales_prediction.py:SALES_PREDICTION_DEAL_VERSIONS_FOR_PARENTS"
        ].query
    )
    assert (
        "coalesce(link.is_active, true) = true"
        in by_key["ingestion/graph/queries/sales.py:RESOLVE_SALES_CUSTOMER"].query
    )


def test_audit_allowlist_retains_repair_retirement_evidence() -> None:
    readers = {
        reader.identifier: reader
        for reader in discover_relationship_readers(*approved_reader_sources(_REPO_ROOT))
    }

    audit_identifiers = {
        reader.identifier for reader in readers.values() if reader.classification == "audit"
    }
    assert audit_identifiers == _AUDIT_READERS
    identifiers = set(readers)
    assert "api/graph/queries/sales_prediction_discovery.py:DISCOVERY_DEAL_RECORDS" in identifiers
    assert "api/graph/queries/users.py:GET_ENTITIES_FOR_REVIEW_CASE" in identifiers
    assert (
        "ingestion/graph/queries/crm_deal_identity_repair_verification.py:READ_RETIRED_RELATIONSHIP_SNAPSHOTS"
        in audit_identifiers
    )


def test_new_authority_readers_remain_active_filtered() -> None:
    readers = {
        reader.identifier: reader
        for reader in assert_reader_contract(*approved_reader_sources(_REPO_ROOT))
    }
    for identifier, classification in (
        ("ingestion/graph/queries/pair_audit_recalc.py:READ_PAIR_AUDIT_BRIDGE", "authoritative"),
        (
            "ingestion/graph/queries/persons.py:FETCH_ACTIVE_PERSON_AUTHORITY_WITH_OVERRIDES",
            "authoritative",
        ),
        (
            "api/graph/queries/crm_deal_count.py:RECOMPUTE_PERSON_CRM_DEAL_COUNTS",
            "authoritative_mutation",
        ),
        (
            "ingestion/graph/queries/crm_deal_count.py:RECOMPUTE_SOURCE_PERSON_CRM_DEAL_COUNTS",
            "authoritative_mutation",
        ),
        (
            "ingestion/graph/queries/crm_history.py:CREATE_CALL_FROM_HISTORY",
            "authoritative_mutation",
        ),
        (
            "ingestion/graph/queries/crm_history.py:ACTIVATE_PENDING_CALLS_FOR_DEAL",
            "authoritative_mutation",
        ),
    ):
        reader = readers[identifier]
        assert reader.classification == classification
        assert _has_active_predicate(reader)
        if classification == "authoritative_mutation":
            inactive = RelationshipReader(
                reader.module,
                reader.symbol,
                reader.classification,
                reader.query.replace("coalesce(deal_link.is_active, true) = true", "true").replace(
                    "coalesce(link.is_active, true) = true", "true"
                ),
            )
            assert not _has_active_predicate(inactive)


def test_active_predicate_must_cover_each_relationship_binding() -> None:
    reader = RelationshipReader(
        "ingestion/graph/queries/example.py",
        "MULTI_LINK_READ",
        "authoritative",
        """MATCH (source:SourceRecord)-[link:LINKED_TO]->(:Person)
        MATCH (:Person)-[other:KNOWS]->(:Person)
        WHERE _LINK_ACTIVE
        RETURN source""",
    )

    assert not _has_active_predicate(reader)


def test_clause_boundaries_discover_pattern_expressions_after_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "services" / "api" / "src" / "graph" / "queries" / "boundaries.py"
    module.parent.mkdir(parents=True)
    expressions = {
        "COMPREHENSION": "size([(p)-[r:PURCHASED]->() | r])",
        "COUNT": "COUNT { MATCH (p)-[r:PURCHASED]->() }",
        "EXISTS": "EXISTS { MATCH (p)-[r:PURCHASED]->() }",
    }
    boundary_prefixes = {
        "WITH": "CREATE (seed:Person) WITH seed RETURN ",
        "RETURN": "CREATE (seed:Person) RETURN ",
        "UNWIND": "CREATE (seed:Person) UNWIND [seed] AS item RETURN ",
        # The query binding's AST segment starts with a string literal before
        # CALL, so detection must not depend on a line-start CALL token.
        "CALL": "CREATE (seed:Person) CALL { RETURN ",
    }
    source_lines: list[str] = []
    identifiers: set[str] = set()
    for boundary, prefix in boundary_prefixes.items():
        for expression_name, expression in expressions.items():
            symbol = f"{boundary}_{expression_name}"
            suffix = " AS value } RETURN value" if boundary == "CALL" else " AS value"
            source_lines.append(f'{symbol} = """{prefix}{expression}{suffix}"""\n')
            identifiers.add(f"api/graph/queries/boundaries.py:{symbol}")
    module.write_text("".join(source_lines), encoding="utf-8")
    expected = frozenset(identifiers)
    monkeypatch.setattr(reader_classification, "_AUDIT_READERS", frozenset())
    monkeypatch.setattr(reader_classification, "_MUTATION_READERS", frozenset())
    monkeypatch.setattr(reader_classification, "_AUTHORITATIVE_MUTATION_READERS", frozenset())
    monkeypatch.setattr(reader_classification, "_AUTHORITATIVE_READERS", expected)

    readers = discover_relationship_readers(module)
    assert {reader.identifier for reader in readers} == expected
    with pytest.raises(
        RuntimeError, match="authoritative relationship reader lacks active predicate"
    ):
        assert_reader_contract(module)


def test_repeated_binding_name_requires_a_predicate_in_each_scope() -> None:
    reader = RelationshipReader(
        "ingestion/graph/queries/example.py",
        "REPEATED_LINK_READ",
        "authoritative",
        """MATCH (first:SourceRecord)-[link:LINKED_TO]->(:Person)
        WHERE coalesce(link.is_active, true) = true
        WITH first
        MATCH (second:SourceRecord)-[link:LINKED_TO]->(:Person)
        RETURN second""",
    )

    assert not _has_active_predicate(reader)


def test_all_repairable_merge_materializers_are_authoritative_and_active() -> None:
    readers = {
        reader.identifier: reader
        for reader in assert_reader_contract(*approved_reader_sources(_REPO_ROOT))
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
        "ingestion/graph/queries/vehicle.py:LINK_CHAT_SOURCE_RECORD_MENTIONS_VEHICLE",
        "ingestion/graph/queries/vehicle.py:LINK_PERSON_BOUGHT_VEHICLE",
        "ingestion/graph/queries/vehicle.py:LINK_PERSON_OWNS_VEHICLE",
        "ingestion/graph/queries/vehicle.py:LINK_SOURCE_RECORD_MENTIONS_VEHICLE",
    }
    materializers = {
        identifier
        for identifier, reader in readers.items()
        if reader.classification == "authoritative_mutation" and "MERGE" in reader.query.upper()
    }
    assert expected <= materializers
    assert all(_has_active_predicate(readers[identifier]) for identifier in expected)


def test_current_materializers_are_classified_and_preserve_retired_links() -> None:
    readers = {
        reader.identifier: reader
        for reader in assert_reader_contract(*approved_reader_sources(_REPO_ROOT))
    }
    for identifier in (
        "ingestion/graph/queries/sales.py:LINK_PERSON_PURCHASED_ORDER",
        "api/graph/queries/review.py:LINK_REVIEW_SALES_PURCHASED_ORDER",
        "ingestion/graph/queries/vehicle.py:LINK_PERSON_BOUGHT_VEHICLE",
    ):
        reader = readers[identifier]
        assert reader.classification == "authoritative_mutation"
        assert _has_active_predicate(reader)
        assert "is_active" in reader.query


def test_merge_active_predicate_requires_literal_true_or_documented_lifecycle_exception() -> None:
    inactive_merge = RelationshipReader(
        "ingestion/graph/queries/example.py",
        "INACTIVE_MERGE",
        "authoritative_mutation",
        "MERGE (p)-[purchase:PURCHASED {is_active: false}]->(:Order)",
    )
    variable_merge = RelationshipReader(
        "ingestion/graph/queries/example.py",
        "VARIABLE_MERGE",
        "authoritative_mutation",
        "MERGE (p)-[purchase:PURCHASED {is_active: $is_active}]->(:Order)",
    )
    documented_lifecycle_merge = RelationshipReader(
        "ingestion/graph/queries/vehicle.py",
        "LINK_PERSON_BOUGHT_VEHICLE",
        "authoritative_mutation",
        "MERGE (p)-[rel:BOUGHT_VEHICLE {is_active: $is_active}]->(:Vehicle)",
    )

    assert not _has_active_predicate(inactive_merge)
    assert not _has_active_predicate(variable_merge)
    assert _has_active_predicate(documented_lifecycle_merge)


def test_anonymous_pattern_expression_fails_active_predicate_validation() -> None:
    reader = RelationshipReader(
        "api/graph/queries/example.py",
        "COUNT_RETIRED_PURCHASES",
        "authoritative",
        "RETURN count { (person)-[:PURCHASED]->(:Order) } AS order_count",
    )

    assert not _has_active_predicate(reader)


def test_reader_discovery_fails_closed_for_valid_cypher_pattern_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "services" / "api" / "src" / "graph" / "queries" / "patterns.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        'NAMED_PATH = """match path = (person)-[purchase:PURCHASED]->(:Order) return path"""\n'
        'COMPREHENSION = """RETURN size([(person)-[purchase:PURCHASED]->() | purchase])"""\n'
        'EXISTS = """RETURN EXISTS { MATCH (person)-[purchase:PURCHASED]->(:Order) }"""\n'
        'PURE_CREATE = """CREATE\n(person)-[purchase:PURCHASED]->(:Order)"""\n'
        'WRITE_THEN_MATCH = """CREATE (seed:Person) WITH seed '
        'MATCH (person)-[purchase:PURCHASED]->(:Order) RETURN person"""\n',
        encoding="utf-8",
    )
    identifiers = frozenset(
        {
            "api/graph/queries/patterns.py:NAMED_PATH",
            "api/graph/queries/patterns.py:COMPREHENSION",
            "api/graph/queries/patterns.py:EXISTS",
            "api/graph/queries/patterns.py:WRITE_THEN_MATCH",
        }
    )
    monkeypatch.setattr(reader_classification, "_AUDIT_READERS", frozenset())
    monkeypatch.setattr(reader_classification, "_MUTATION_READERS", frozenset())
    monkeypatch.setattr(reader_classification, "_AUTHORITATIVE_MUTATION_READERS", frozenset())
    monkeypatch.setattr(reader_classification, "_AUTHORITATIVE_READERS", identifiers)

    readers = reader_classification.discover_relationship_readers(module)
    assert {reader.identifier for reader in readers} == identifiers
    with pytest.raises(
        RuntimeError, match="authoritative relationship reader lacks active predicate"
    ):
        assert_reader_contract(module)


def test_authoritative_mutation_without_active_filter_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "services" / "ingestion" / "src" / "graph" / "queries" / "dirty.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        'BROKEN = """MATCH (left:Person)-[knows:KNOWS]->(right:Person) '
        'SET left.analysis_dirty_at = datetime() RETURN left"""\n',
        encoding="utf-8",
    )
    identifier = "ingestion/graph/queries/dirty.py:BROKEN"
    monkeypatch.setattr(reader_classification, "_AUDIT_READERS", frozenset())
    monkeypatch.setattr(reader_classification, "_AUTHORITATIVE_READERS", frozenset())
    monkeypatch.setattr(reader_classification, "_MUTATION_READERS", frozenset())
    monkeypatch.setattr(
        reader_classification, "_AUTHORITATIVE_MUTATION_READERS", frozenset({identifier})
    )

    with pytest.raises(
        RuntimeError, match="authoritative relationship reader lacks active predicate"
    ):
        assert_reader_contract(module)


def test_unclassified_reader_fails_closed(tmp_path: Path) -> None:
    module = tmp_path / "services" / "api" / "src" / "graph" / "queries" / "queries.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "UNCLASSIFIED = '''MATCH (source:SourceRecord)-[:LINKED_TO]->(:Person) RETURN source'''\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unclassified relationship reader"):
        assert_reader_contract(module)
