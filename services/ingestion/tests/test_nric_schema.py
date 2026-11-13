"""Tests for init.cypher schema constraints and conditional fulltext index drop."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.graph.queries.maintenance import (
    DROP_PERSON_NAME_SEARCH_FULLTEXT_INDEX,
    SHOW_PERSON_NAME_SEARCH_FULLTEXT_INDEX,
)
from src.graph.schema_init import _split_statements, apply_schema


def test_init_cypher_person_name_search_has_no_preferred_nric() -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    init_text = (repo_root / "infra" / "neo4j" / "init.cypher").read_text(encoding="utf-8")
    statements = _split_statements(init_text)

    # Find the person_name_search statement
    search_stmts = [s for s in statements if "person_name_search" in s]
    assert len(search_stmts) == 1
    stmt = search_stmts[0]

    # Must contain full name, email, phone
    assert "preferred_full_name" in stmt
    assert "preferred_email" in stmt
    assert "preferred_phone" in stmt

    # Must NOT contain preferred_nric
    assert "preferred_nric" not in stmt

    # Assert no new index was added for NRIC
    create_index_stmts = [
        s for s in statements if s.upper().startswith("CREATE") and "INDEX" in s.upper()
    ]
    for idx_stmt in create_index_stmts:
        # No index statement specifies ON (id.normalized_value) or name containing nric
        tokens = idx_stmt.split()
        if len(tokens) >= 3:
            idx_name = tokens[2] if tokens[1].upper() == "INDEX" else tokens[3]
            assert "nric" not in idx_name.lower()


def test_conditional_drop_fulltext_index_when_preferred_nric_present() -> None:
    mock_session = MagicMock()
    mock_client = MagicMock()
    mock_client.session.return_value.__enter__.return_value = mock_session

    # Case 1: live index has preferred_nric in properties -> drops index
    mock_record = {"properties": ["preferred_full_name", "preferred_nric", "preferred_email"]}
    mock_res = MagicMock()
    mock_res.single.return_value = mock_record

    executed_statements: list[str] = []

    def mock_run(stmt: str, **kwargs: object) -> MagicMock:
        executed_statements.append(stmt)
        if stmt == SHOW_PERSON_NAME_SEARCH_FULLTEXT_INDEX:
            return mock_res
        return MagicMock()

    mock_session.run = mock_run

    apply_schema(mock_client)
    assert DROP_PERSON_NAME_SEARCH_FULLTEXT_INDEX in executed_statements


def test_conditional_drop_fulltext_index_not_dropped_when_clean() -> None:
    mock_session = MagicMock()
    mock_client = MagicMock()
    mock_client.session.return_value.__enter__.return_value = mock_session

    # Case 2: live index already clean without preferred_nric -> does NOT drop
    mock_record = {"properties": ["preferred_full_name", "preferred_email", "preferred_phone"]}
    mock_res = MagicMock()
    mock_res.single.return_value = mock_record

    executed_statements: list[str] = []

    def mock_run(stmt: str, **kwargs: object) -> MagicMock:
        executed_statements.append(stmt)
        if stmt == SHOW_PERSON_NAME_SEARCH_FULLTEXT_INDEX:
            return mock_res
        return MagicMock()

    mock_session.run = mock_run

    apply_schema(mock_client)
    assert DROP_PERSON_NAME_SEARCH_FULLTEXT_INDEX not in executed_statements
