"""Active synthetic regressions for fail-closed relationship-reader discovery."""

from __future__ import annotations

from pathlib import Path

import pytest
import src.crm_deal_identity_repair.reader_classification as reader_classification
from src.crm_deal_identity_repair.reader_classification import (
    assert_reader_contract,
    discover_relationship_readers,
)

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

def test_mixed_create_and_on_create_do_not_hide_a_later_relationship_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a relationship's immediate CREATE clause may make it write-only."""
    module = tmp_path / "services" / "api" / "src" / "graph" / "queries" / "activation.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        'PURE_CREATE = """\n'
        "MATCH (person:Person {person_id: $person_id})\n"
        "cReAtE (person)-[created:PURCHASED]->(:Order)\n"
        "RETURN person\n"
        '"""\n'
        'ACTIVATE = """\n'
        "MATCH (approved:Person {person_id: $person_id})\n"
        "CREATE (seed:Person)\n"
        "mErGe (identifier:Identifier {normalized_value: $value})\n"
        "oN cReAtE sEt identifier.identifier_id = randomUUID()\n"
        "merge (approved)-[rel:IDENTIFIED_BY]->(identifier)\n"
        "RETURN approved\n"
        '"""\n',
        encoding="utf-8",
    )
    identifier = "api/graph/queries/activation.py:ACTIVATE"
    monkeypatch.setattr(reader_classification, "_AUDIT_READERS", frozenset())
    monkeypatch.setattr(reader_classification, "_MUTATION_READERS", frozenset())
    monkeypatch.setattr(reader_classification, "_AUTHORITATIVE_READERS", frozenset())
    monkeypatch.setattr(
        reader_classification, "_AUTHORITATIVE_MUTATION_READERS", frozenset({identifier})
    )

    readers = discover_relationship_readers(module)
    assert [reader.identifier for reader in readers] == [identifier]
    with pytest.raises(
        RuntimeError, match="authoritative relationship reader lacks active predicate"
    ):
        assert_reader_contract(module)

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

def test_relationship_pattern_filler_matrix_is_exhaustive_and_fails_closed(
    tmp_path: Path,
) -> None:
    module = tmp_path / "services" / "api" / "src" / "graph" / "queries" / "generic.py"
    module.parent.mkdir(parents=True)
    cases = (
        ("NAMED_EMPTY", "MATCH (a)-[rel]->(b) RETURN rel", True),
        ("NAMED_STATIC_REPAIRABLE", "MATCH (a)-[rel:LINKED_TO]->(b) RETURN rel", True),
        ("NAMED_STATIC_NON_REPAIRABLE", "MATCH (a)-[rel:CHILD_OF]->(b) RETURN rel", False),
        (
            "NAMED_COMPOUND_REPAIRABLE",
            "MATCH (a)-[rel:CHILD_OF|LINKED_TO]->(b) RETURN rel",
            True,
        ),
        (
            "NAMED_COMPOUND_NON_REPAIRABLE",
            "MATCH (a)-[rel:CHILD_OF|FROM_SOURCE]->(b) RETURN rel",
            False,
        ),
        ("NAMED_DYNAMIC", "MATCH (a)-[rel:$(relationship_type)]->(b) RETURN rel", True),
        ("NAMED_PROPERTY", "MATCH (a)-[rel {source_record_pk: $pk}]->(b) RETURN rel", True),
        ("NAMED_PREDICATE", "MATCH (a)-[rel WHERE rel.is_active]->(b) RETURN rel", True),
        ("NAMED_LENGTH", "MATCH (a)-[rel*1..2]->(b) RETURN rel", True),
        ("ANONYMOUS_EMPTY", "MATCH (a)-->(b) RETURN b", True),
        ("ANONYMOUS_REVERSE", "MATCH (a)<--(b) RETURN b", True),
        ("ANONYMOUS_UNDIRECTED", "MATCH (a)--(b) RETURN b", True),
        ("ANONYMOUS_STATIC_REPAIRABLE", "MATCH (a)-[:LINKED_TO]->(b) RETURN b", True),
        ("ANONYMOUS_STATIC_NON_REPAIRABLE", "MATCH (a)-[:CHILD_OF]->(b) RETURN b", False),
        (
            "ANONYMOUS_COMPOUND_REPAIRABLE",
            "MATCH (a)-[:CHILD_OF|LINKED_TO]->(b) RETURN b",
            True,
        ),
        (
            "ANONYMOUS_COMPOUND_NON_REPAIRABLE",
            "MATCH (a)-[:CHILD_OF|FROM_SOURCE]->(b) RETURN b",
            False,
        ),
        ("ANONYMOUS_DYNAMIC", "MATCH (a)-[:$(relationship_type)]->(b) RETURN b", True),
        ("ANONYMOUS_PROPERTY", "MATCH (a)-[{source_record_pk: $pk}]-(b) RETURN b", True),
        ("ANONYMOUS_PREDICATE", "MATCH (a)-[WHERE true]->(b) RETURN b", True),
        ("ANONYMOUS_LENGTH", "MATCH (a)-[*1..2]->(b) RETURN b", True),
        ("CREATE_NAMED_PROPERTY", "CREATE (a)-[rel {source_record_pk: $pk}]->(b)", False),
        ("CREATE_ANONYMOUS_PROPERTY", "CREATE (a)-[{source_record_pk: $pk}]->(b)", False),
        (
            "NODE_CREATE_THEN_READ_NAMED",
            "CREATE (seed:Person) WITH seed MATCH (a)-[rel:$(relationship_type)]->(b) RETURN b",
            True,
        ),
        (
            "NODE_CREATE_THEN_READ_ANONYMOUS",
            "CREATE (seed:Person) WITH seed MATCH (a)-[{source_record_pk: $pk}]->(b) RETURN b",
            True,
        ),
    )
    module.write_text(
        "".join(f'{symbol} = """{query}"""\n' for symbol, query, _expected in cases),
        encoding="utf-8",
    )

    expected = {symbol for symbol, _query, discovered in cases if discovered}
    assert {reader.symbol for reader in discover_relationship_readers(module)} == expected
    with pytest.raises(RuntimeError, match="unclassified relationship reader"):
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
