"""Active synthetic regressions for fail-closed relationship-reader discovery."""

from __future__ import annotations

from pathlib import Path

import pytest
import src.crm_deal_identity_repair.reader_classification as reader_classification
from src.crm_deal_identity_repair.reader_classification import (
    assert_reader_contract,
    discover_relationship_readers,
)


def _set_registry(
    monkeypatch: pytest.MonkeyPatch,
    *,
    authoritative: frozenset[str] = frozenset(),
    mutations: frozenset[str] = frozenset(),
) -> None:
    monkeypatch.setattr(reader_classification, "_AUDIT_READERS", frozenset())
    monkeypatch.setattr(reader_classification, "_MUTATION_READERS", frozenset())
    monkeypatch.setattr(
        reader_classification,
        "_AUTHORITATIVE_MUTATION_READERS",
        mutations,
    )
    monkeypatch.setattr(reader_classification, "_AUTHORITATIVE_READERS", authoritative)


def test_clause_boundary_discovery_after_create_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tmp_path / "services/api/src/graph/queries/boundaries.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        'COUNT = """CREATE (seed:Person) RETURN COUNT { MATCH (p)-[r:PURCHASED]->() }"""\n'
        'EXISTS = """CREATE (seed:Person) RETURN EXISTS { MATCH (p)-[r:PURCHASED]->() }"""\n',
        encoding="utf-8",
    )
    identifiers = frozenset(
        {
            "api/graph/queries/boundaries.py:COUNT",
            "api/graph/queries/boundaries.py:EXISTS",
        }
    )
    _set_registry(monkeypatch, authoritative=identifiers)

    assert {reader.identifier for reader in discover_relationship_readers(module)} == identifiers
    with pytest.raises(RuntimeError, match="lacks active predicate"):
        assert_reader_contract(module)


def test_mixed_create_on_create_and_later_relationship_merge_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tmp_path / "services/api/src/graph/queries/activation.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        'ACTIVATE = """CREATE (seed:Person) WITH seed '
        'MERGE (person)-[rel:IDENTIFIED_BY]->(:Identifier) RETURN person"""\n',
        encoding="utf-8",
    )
    identifier = "api/graph/queries/activation.py:ACTIVATE"
    _set_registry(monkeypatch, mutations=frozenset({identifier}))

    with pytest.raises(RuntimeError, match="lacks active predicate"):
        assert_reader_contract(module)


def test_named_path_comprehension_and_exists_forms_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tmp_path / "services/api/src/graph/queries/patterns.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        'PATH = """MATCH path = (person)-[purchase:PURCHASED]->(:Order) RETURN path"""\n'
        'COMP = """RETURN size([(person)-[purchase:PURCHASED]->() | purchase])"""\n'
        'EXISTS = """RETURN EXISTS { MATCH (person)-[purchase:PURCHASED]->(:Order) }"""\n',
        encoding="utf-8",
    )
    identifiers = frozenset(
        {
            "api/graph/queries/patterns.py:PATH",
            "api/graph/queries/patterns.py:COMP",
            "api/graph/queries/patterns.py:EXISTS",
        }
    )
    _set_registry(monkeypatch, authoritative=identifiers)

    assert {reader.identifier for reader in discover_relationship_readers(module)} == identifiers
    with pytest.raises(RuntimeError, match="lacks active predicate"):
        assert_reader_contract(module)


@pytest.mark.parametrize(
    ("symbol", "query", "discover"),
    (
        ("NAMED", "MATCH (a)-[rel:LINKED_TO]->(b) RETURN rel", True),
        ("ANON", "MATCH (a)-[:LINKED_TO]->(b) RETURN b", True),
        ("DYNAMIC", "MATCH (a)-[rel:$(relationship_type)]->(b) RETURN rel", True),
        ("NON_REPAIR", "MATCH (a)-[rel:CHILD_OF]->(b) RETURN rel", False),
        ("CREATE", "CREATE (a)-[rel:LINKED_TO]->(b)", False),
    ),
)
def test_relationship_pattern_filler_matrix_remains_exhaustive_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    query: str,
    discover: bool,
) -> None:
    module = tmp_path / "services/api/src/graph/queries/generic.py"
    module.parent.mkdir(parents=True)
    module.write_text(f'{symbol} = """{query}"""\n', encoding="utf-8")
    identifier = f"api/graph/queries/generic.py:{symbol}"
    _set_registry(monkeypatch, authoritative=frozenset({identifier}) if discover else frozenset())

    readers = discover_relationship_readers(module)
    assert bool(readers) is discover
    if discover:
        with pytest.raises(RuntimeError, match="lacks active predicate"):
            assert_reader_contract(module)


def test_unclassified_reader_rejection_remains_fail_closed(tmp_path: Path) -> None:
    module = tmp_path / "services/api/src/graph/queries/unclassified.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        'UNCLASSIFIED = """MATCH (source)-[:LINKED_TO]->(:Person) RETURN source"""\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unclassified relationship reader"):
        assert_reader_contract(module)
