"""Contract tests for repair-retired relationship reader safety."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.crm_deal_identity_repair.reader_classification import (
    _AUDIT_READERS,
    _AUTHORITATIVE_READERS,
    _has_active_predicate,
    approved_reader_sources,
    assert_reader_contract,
    discover_relationship_readers,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_all_executable_relationship_readers_are_explicitly_classified() -> None:
    readers = assert_reader_contract(*approved_reader_sources(_REPO_ROOT))

    assert readers
    assert {reader.identifier for reader in readers} == _AUDIT_READERS | _AUTHORITATIVE_READERS
    assert all(reader.classification in {"authoritative", "audit"} for reader in readers)


def test_authoritative_readers_exclude_inactive_relationships() -> None:
    readers = assert_reader_contract(*approved_reader_sources(_REPO_ROOT))
    authoritative = [reader for reader in readers if reader.classification == "authoritative"]

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


def test_unclassified_reader_fails_closed(tmp_path: Path) -> None:
    module = tmp_path / "services" / "api" / "src" / "graph" / "queries" / "queries.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "UNCLASSIFIED = '''MATCH (source:SourceRecord)-[:LINKED_TO]->(:Person) RETURN source'''\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unclassified relationship reader"):
        assert_reader_contract(module)
